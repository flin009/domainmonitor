import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict

import psycopg2
import psycopg2.extras

try:
    from ..config import get_config
except Exception:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import get_config  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass


try:
    from zoneinfo import ZoneInfo
    _CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _CN_TZ = timezone(timedelta(hours=8))


def _ensure_env_loaded() -> None:
    if os.getenv("TG_BOT_TOKEN") and os.getenv("TG_CHAT_ID"):
        return
    root = os.path.dirname(os.path.dirname(__file__))
    p = os.path.join(root, ".env")
    try:
        if os.path.exists(p):
            try:
                from dotenv import load_dotenv as _ld
                _ld(p)
            except Exception:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("#") or "=" not in s:
                            continue
                        k, v = s.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in ("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAM_CHAT_ID"):
                            os.environ[k] = v
    except Exception:
        pass

try:
    import httpx
except Exception:
    httpx = None  # type: ignore


def connect():
    cfg = get_config()
    return psycopg2.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        user=cfg.db_user,
        password=cfg.db_password,
        dbname=cfg.db_name,
    )


def pick_domain(conn, preferred: Optional[str]) -> Optional[str]:
    if preferred:
        return preferred
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT domain
            FROM monitor_targets
            ORDER BY priority DESC, id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def list_domains(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT domain
            FROM monitor_targets
            WHERE enabled=true
            ORDER BY priority DESC, id DESC
            """
        )
        rows = cur.fetchall()
        return [str(r[0]) for r in rows]


def latest_task_for_domain(conn, domain: str) -> Optional[Tuple[int, datetime]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, created_at
            FROM monitor_tasks
            WHERE domain=%s AND status<>'running'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (domain,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0]), row[1]


def fetch_status_codes(conn, task_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status_code
            FROM monitor_results
            WHERE task_id=%s
            ORDER BY id ASC
            """,
            (task_id,),
        )
        return [r[0] for r in cur.fetchall()]


def is_success(code: Optional[str]) -> bool:
    if code is None:
        return False
    s = str(code).strip()
    if not s:
        return False
    m = re.match(r"^\d{3}$", s)
    if not m:
        return False
    return s.startswith("2") or s.startswith("3")


def build_message(domain: str, nodes: int, when: datetime, ratio: float) -> str:
    ts = when.astimezone(_CN_TZ).isoformat()
    pct = f"{ratio*100:.2f}%"
    return (
        f"🔥🔥🔥报警实例:  {domain}\n\n"
        f"名称: 域名失败率 > 20% (监控节点数 {nodes})\n"
        f"时间: {ts}\n"
        f"级别： Critical\n"
        f"状态: PROBLEM\n"
        f"详情: {domain} 检测失败率 {pct}"
    )


def send_telegram(text: str, token: Optional[str], chat_id: Optional[str]) -> Tuple[bool, str]:
    if httpx is None:
        return False, "httpx 未安装"
    _ensure_env_loaded()
    tok = token or os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False, "缺少 TG_BOT_TOKEN/TG_CHAT_ID 环境变量或命令行参数"
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    try:
        resp = httpx.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10.0,
        )
        if resp.status_code >= 400:
            return False, f"telegram 返回 {resp.status_code}: {resp.text}"
        return True, "ok"
    except Exception as e:
        return False, f"发送失败: {e}"


def main():
    ap = argparse.ArgumentParser(description="检查最近一次检测失败率并按阈值向 Telegram 报警")
    ap.add_argument("--domain", help="指定检测目标域名；不传则从 monitor_targets 选择优先级最高的一个")
    ap.add_argument("--threshold", type=float, default=0.2, help="失败率阈值，默认 0.2 即 20%%")
    ap.add_argument("--dry-run", action="store_true", help="仅计算与打印，不发送 Telegram")
    ap.add_argument("--token", help="Telegram Bot Token，可用 TG_BOT_TOKEN 环境变量代替")
    ap.add_argument("--chat-id", help="Telegram Chat ID，可用 TG_CHAT_ID 环境变量代替")
    ap.add_argument("--loop-seconds", type=int, default=0, help="循环模式，轮询所有目标的间隔秒；默认 0 为单次运行")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.loop_seconds and args.loop_seconds > 0:
            alerted: Dict[str, int] = {}
            while True:
                domains = list_domains(conn)
                if not domains:
                    print("无可用监控目标")
                for domain in domains:
                    latest = latest_task_for_domain(conn, domain)
                    if not latest:
                        print(f"{domain} 暂无已完成的检测任务")
                        continue
                    task_id, created_at = latest
                    codes = fetch_status_codes(conn, task_id)
                    total = len(codes)
                    if total == 0:
                        print(f"{domain} 最近一次任务无结果 task_id={task_id}")
                        continue
                    fail = sum(0 if is_success(c) else 1 for c in codes)
                    ratio = fail / total
                    print(f"domain={domain} task_id={task_id} total_nodes={total} fail={fail} fail_ratio={ratio:.4f} created_at={created_at}")
                    if ratio > args.threshold:
                        if alerted.get(domain) == task_id:
                            continue
                        msg = build_message(domain, total, created_at, ratio)
                        if args.dry_run:
                            print("dry-run: 将发送 Telegram 消息：")
                            print(msg)
                        else:
                            ok, info = send_telegram(msg, args.token, args.chat_id)
                            print(f"telegram_send ok={ok} info={info}")
                        alerted[domain] = task_id
                time.sleep(args.loop_seconds)
        else:
            domain = pick_domain(conn, args.domain)
            if not domain:
                print("无可用监控目标")
                return 1
            latest = latest_task_for_domain(conn, domain)
            if not latest:
                print(f"{domain} 暂无已完成的检测任务")
                return 2
            task_id, created_at = latest
            codes = fetch_status_codes(conn, task_id)
            total = len(codes)
            if total == 0:
                print(f"{domain} 最近一次任务无结果 task_id={task_id}")
                return 3
            fail = sum(0 if is_success(c) else 1 for c in codes)
            ratio = fail / total
            print(f"domain={domain} task_id={task_id} total_nodes={total} fail={fail} fail_ratio={ratio:.4f} created_at={created_at}")
            if ratio > args.threshold:
                msg = build_message(domain, total, created_at, ratio)
                if args.dry_run:
                    print("dry-run: 将发送 Telegram 消息：")
                    print(msg)
                    return 0
                ok, info = send_telegram(msg, args.token, args.chat_id)
                print(f"telegram_send ok={ok} info={info}")
                return 0 if ok else 4
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
