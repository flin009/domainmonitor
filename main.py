import argparse
import json
import os
import sys
import time
from typing import List, Optional
import logging
try:
    from .config import get_config
    from .db import Db
    from .platforms.itdog import ItDogPlatform
except Exception:
    sys.path.append(os.path.dirname(__file__))
    from config import get_config  # type: ignore
    from db import Db  # type: ignore
    from platforms.itdog import ItDogPlatform  # type: ignore


def ocr_count(path: str) -> Optional[int]:
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        return None
    try:
        img = Image.open(path)
        txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
        ips = set()
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            for part in line.split():
                if part.count(".") == 3:
                    ips.add(part)
        return len(ips) if ips else None
    except Exception:
        return None


def run_once(db: Db, platform_name: str, domain: str, proxy: Optional[str], headless: bool, ua: Optional[str], referer: Optional[str], cookie: Optional[str]) -> None:
    t0 = time.time()
    task_id = db.create_task(platform=platform_name, domain=domain, proxy_server=proxy, headless=headless)
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
        logging.info(f"start detection domain={domain} platform={platform_name} headless={headless} proxy={proxy or ''}")
        platform = ItDogPlatform()
        results, screenshot_path, timing = platform.run(domain=domain, proxy_server=proxy, headless=headless, user_agent=ua, referer=referer, cookie=cookie)
        browser_ms = timing.get("browser_launch_ms")
        collect_ms = timing.get("collect_ms")
        logging.info(f"metrics domain={domain} browser_ms={browser_ms} collect_ms={collect_ms} req_total={timing.get('request_total')} req_blocked={timing.get('request_blocked')} req_allowed={timing.get('request_allowed')} req_xhr={timing.get('request_xhr')}")
        real_proxy_ip = timing.get("real_proxy_ip")
        if real_proxy_ip:
            logging.info(f"update task proxy to exit ip domain={domain} task_id={task_id} proxy_ip={real_proxy_ip}")
            try:
                db.update_task_proxy(task_id, real_proxy_ip)
            except Exception as e:
                logging.warning(f"update_task_proxy failed task_id={task_id} err={e}")
        try:
            summary = {
                "domain": domain,
                "request_total": timing.get("request_total"),
                "request_blocked": timing.get("request_blocked"),
                "request_allowed": timing.get("request_allowed"),
                "request_xhr": timing.get("request_xhr"),
                "browser_launch_ms": browser_ms,
                "collect_ms": collect_ms,
                "real_proxy_ip": real_proxy_ip,
            }
            print(json.dumps(summary, ensure_ascii=False))
        except Exception:
            pass
        for r in results:
            r["task_id"] = task_id
        insert_start = time.time()
        count = db.insert_results(results)
        insert_ms = (time.time() - insert_start) * 1000.0
        logging.info(f"inserted results domain={domain} task_id={task_id} count={count} insert_ms={insert_ms}")
        if screenshot_path:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            logging.info(f"screenshot saved path={screenshot_path}")
        parsed_count = len(results)
        ocr = ocr_count(screenshot_path) if screenshot_path else None
        if count != parsed_count or (ocr is not None and ocr != parsed_count):
            status = "partial"
            logging.warning(f"count mismatch domain={domain} parsed={parsed_count} inserted={count} ocr={ocr}")
        total_ms = (time.time() - t0) * 1000.0
    except Exception as e:
        status = "failed"
        error_type = type(e).__name__
        error_message = str(e)
        total_ms = (time.time() - t0) * 1000.0
        logging.exception(f"detection failed domain={domain} task_id={task_id} error={error_message}")
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
        logging.info(f"task complete domain={domain} task_id={task_id} status={status} total_ms={total_ms}")


def main():
    root = logging.getLogger()
    has_file = False
    for h in root.handlers:
        try:
            if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "").endswith("run.log"):
                has_file = True
                break
        except Exception:
            pass
    if not has_file:
        fh = logging.FileHandler("run.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(sh)
    root.setLevel(logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--domains", nargs="+", required=True)
    parser.add_argument("--platform", default="itdog")
    parser.add_argument("--proxy")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()
    cfg = get_config()
    headless = cfg.headless
    if args.headless:
        headless = True
    if args.no_headless:
        headless = False
    proxy = args.proxy or cfg.default_proxy
    ua = cfg.user_agent
    referer = cfg.referer
    cookie = cfg.cookie
    domains: List[str] = args.domains[:5]
    db = Db()
    logging.info(f"main entry args domains={args.domains} platform={args.platform} proxy={args.proxy} headless={args.headless} no_headless={args.no_headless}")
    logging.info(f"main entry config headless={cfg.headless} default_proxy={cfg.default_proxy} user_agent={ua} referer={referer} cookie={cookie}")
    logging.info(f"main entry effective domains={domains} headless={headless} proxy={proxy}")
    try:
        for d in domains:
            run_once(db, args.platform, d, proxy, headless, ua, referer, cookie)
    finally:
        db.close()


if __name__ == "__main__":
    main()
