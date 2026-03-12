import argparse
import logging
import os
import signal
import sys
import time
import psycopg2
from typing import List, Tuple, Optional
try:
    from ..config import get_config
    from ..db import Db
    from ..platforms.itdog import ItDogPlatform
    from ..scripts.alert_telegram import is_success, send_telegram, build_message
except Exception:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import get_config  # type: ignore
    from db import Db  # type: ignore
    from platforms.itdog import ItDogPlatform  # type: ignore
    from scripts.alert_telegram import is_success, send_telegram, build_message  # type: ignore


def _is_pid_namespace_init() -> bool:
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    parts = line.split()[1:]
                    return bool(parts) and parts[-1] == "1"
    except Exception:
        pass
    return os.getpid() == 1


def _reap_children(_signum=None, _frame=None) -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return


def _install_sigchld_reaper() -> None:
    if not _is_pid_namespace_init():
        return
    try:
        signal.signal(signal.SIGCHLD, _reap_children)
    except Exception:
        return
    _reap_children()


def connect():
    cfg = get_config()
    return psycopg2.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        user=cfg.db_user,
        password=cfg.db_password,
        dbname=cfg.db_name,
    )


def recover_expired_leases(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "update monitor_waiting_tasks set status='waiting', lease_until=null, worker_id=null where status='leased' and lease_until < now()"
        )
        n = cur.rowcount
    conn.commit()
    return n


def lease_tasks(conn, worker_id: str, limit: int, lease_minutes: int) -> List[Tuple[int, int, str]]:
    sql = """
    with cte as (
      select id
      from monitor_waiting_tasks
      where status='waiting'
      order by created_at
      limit %s
      for update skip locked
    )
    update monitor_waiting_tasks
    set status='leased',
        lease_until=now() + (%s || ' minutes')::interval,
        worker_id=%s
    where id in (select id from cte)
    returning id, target_id, domain
    """
    rows: List[Tuple[int, int, str]] = []
    with conn.cursor() as cur:
        cur.execute(sql, (limit, lease_minutes, worker_id))
        for r in cur.fetchall():
            rows.append((int(r[0]), int(r[1]), str(r[2])))
    conn.commit()
    return rows


def mark_waiting_task(conn, waiting_id: int, status: str, attempts_inc: int = 1, error_message: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update monitor_waiting_tasks set status=%s, attempts=attempts+%s, error_message=%s where id=%s",
            (status, attempts_inc, error_message, waiting_id),
        )
    conn.commit()


def process_one(db: Db, waiting_id: int, domain: str, proxy: Optional[str], headless: bool, ua: Optional[str], referer: Optional[str], cookie: Optional[str]) -> bool:
    t0 = time.time()
    task_id = db.create_task(platform="itdog", domain=domain, proxy_server=proxy, headless=headless)
    status = "success"
    browser_ms = None
    collect_ms = None
    insert_ms = None
    total_ms = None
    error_type = None
    error_message = None
    count = 0
    screenshot_path = None
    try:
        logging.info(f"worker start domain={domain} waiting_id={waiting_id} proxy={proxy or ''} headless={headless}")
        platform = ItDogPlatform()
        results, screenshot_path, timing = platform.run(domain=domain, proxy_server=proxy, headless=headless, user_agent=ua, referer=referer, cookie=cookie)
        browser_ms = timing.get("browser_launch_ms")
        collect_ms = timing.get("collect_ms")
        real_proxy_ip = timing.get("real_proxy_ip")
        if real_proxy_ip:
            logging.info(f"worker update task proxy exit task_id={task_id} ip={real_proxy_ip}")
            try:
                db.update_task_proxy(task_id, real_proxy_ip)
            except Exception as e:
                logging.warning(f"worker update_task_proxy failed task_id={task_id} err={e}")
        for r in results:
            r["task_id"] = task_id
        insert_start = time.time()
        count = db.insert_results(results)
        insert_ms = (time.time() - insert_start) * 1000.0
        if screenshot_path:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        parsed_count = len(results)
        ocr = None
        try:
            from PIL import Image
            import pytesseract
            img = Image.open(screenshot_path) if screenshot_path else None
            if img:
                txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
                ips = set()
                for line in txt.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    for part in line.split():
                        if part.count(".") == 3:
                            ips.add(part)
                ocr = len(ips) if ips else None
        except Exception:
            ocr = None
        if count != parsed_count or (ocr is not None and ocr != parsed_count):
            status = "partial"
        try:
            threshold_env = os.getenv("ALERT_FAIL_THRESHOLD") or os.getenv("FAIL_RATIO_THRESHOLD")
            threshold = float(threshold_env) if threshold_env else 0.2
        except Exception:
            threshold = 0.2
        if parsed_count > 0:
            fail = 0
            try:
                for r in results:
                    code = r.get("status_code")
                    fail += 0 if is_success(code) else 1
            except Exception:
                fail = 0
            ratio = (fail / parsed_count) if parsed_count else 0.0
            logging.info(f"fail_ratio domain={domain} task_id={task_id} total_nodes={parsed_count} fail={fail} ratio={ratio:.4f} threshold={threshold}")
            if ratio > threshold:
                try:
                    from datetime import datetime, timezone
                    msg = build_message(domain, parsed_count, datetime.now(timezone.utc), ratio)
                    ok, info = send_telegram(msg, None, None)
                    logging.info(f"telegram_send ok={ok} info={info}")
                except Exception as e:
                    logging.warning(f"telegram_send error domain={domain} err={e}")
        total_ms = (time.time() - t0) * 1000.0
        logging.info(f"worker done domain={domain} waiting_id={waiting_id} status={status} count={count} parsed={parsed_count} ocr={ocr} total_ms={total_ms}")
        return True
    except Exception as e:
        status = "failed"
        error_type = type(e).__name__
        error_message = str(e)
        total_ms = (time.time() - t0) * 1000.0
        logging.exception(f"worker failed domain={domain} waiting_id={waiting_id} error={error_message}")
        return False
    finally:
        db.update_task_metrics(
            task_id=task_id,
            status=status,
            count=count,
            browser_launch_ms=browser_ms,
            collect_ms=collect_ms,
            insert_ms=insert_ms,
            total_ms=total_ms,
            error_type=error_type,
            error_message=error_message,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-minutes", type=int, default=10)
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(h)
    root.setLevel(logging.INFO)
    _install_sigchld_reaper()
    cfg = get_config()
    headless = cfg.headless
    if args.headless:
        headless = True
    if args.no_headless:
        headless = False
    proxy = cfg.default_proxy
    ua = cfg.user_agent
    referer = cfg.referer
    cookie = cfg.cookie
    worker_id = os.getenv("HOSTNAME") or os.getenv("COMPUTERNAME") or f"pid-{os.getpid()}"
    conn = connect()
    db = Db()
    logging.info(f"worker start worker_id={worker_id} headless={headless} proxy={proxy}")
    try:
        while True:
            n_recovered = recover_expired_leases(conn)
            if n_recovered:
                logging.info(f"worker recovered {n_recovered} expired leases")
            leased = lease_tasks(conn, worker_id, args.batch_size, args.lease_minutes)
            if not leased:
                time.sleep(args.poll_seconds)
                continue
            for waiting_id, target_id, domain in leased:
                ok = process_one(db, waiting_id, domain, proxy, headless, ua, referer, cookie)
                mark_waiting_task(conn, waiting_id, "done" if ok else "failed", 1, None if ok else "worker error")
    finally:
        conn.close()
        db.close()


if __name__ == "__main__":
    main()
