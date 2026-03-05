import argparse
import json
import os
import sys
import time
from typing import List, Optional
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
        platform = ItDogPlatform()
        results, screenshot_path, timing = platform.run(domain=domain, proxy_server=proxy, headless=headless, user_agent=ua, referer=referer, cookie=cookie)
        browser_ms = timing.get("browser_launch_ms")
        collect_ms = timing.get("collect_ms")
        for r in results:
            r["task_id"] = task_id
        insert_start = time.time()
        count = db.insert_results(results)
        insert_ms = (time.time() - insert_start) * 1000.0
        if screenshot_path:
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        parsed_count = len(results)
        ocr = ocr_count(screenshot_path) if screenshot_path else None
        if count != parsed_count or (ocr is not None and ocr != parsed_count):
            status = "partial"
        total_ms = (time.time() - t0) * 1000.0
    except Exception as e:
        status = "failed"
        error_type = type(e).__name__
        error_message = str(e)
        total_ms = (time.time() - t0) * 1000.0
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
    try:
        for d in domains:
            run_once(db, args.platform, d, proxy, headless, ua, referer, cookie)
    finally:
        db.close()


if __name__ == "__main__":
    main()
