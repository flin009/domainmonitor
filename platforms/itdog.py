import re
import os
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from .base import MonitorPlatform
import logging
try:
    from ..config import get_config
except Exception:
    from config import get_config
try:
    import httpx
except Exception:
    httpx = None


def parse_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    return float(m.group(1))


def extract_ip(text: str) -> Optional[str]:
    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    return m.group(0) if m else None


def split_location(text: Optional[str]) -> Tuple[str, str, str, str]:
    if not text:
        return ("未知", "未知", "未知", "未知")
    parts = re.split(r"[·\s\-]", text.strip())
    parts = [p for p in parts if p]
    country = parts[0] if len(parts) > 0 else "未知"
    province = parts[1] if len(parts) > 1 else "未知"
    city = parts[2] if len(parts) > 2 else "未知"
    isp = parts[3] if len(parts) > 3 else "未知"
    return (country, province, city, isp)

def is_suspicious_region(text: Optional[str]) -> bool:
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if len(s) >= 40:
        return True
    if re.fullmatch(r"[A-Za-z0-9._%\\-]{20,}", s or ""):
        return True
    if s.startswith("-") or ("=" in s) or ("%2F" in s) or ("/" in s and "google.com" not in s):
        return True
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", s):
        return True
    return False


class ItDogPlatform(MonitorPlatform):
    def run(
        self,
        domain: str,
        proxy_server: Optional[str],
        headless: bool,
        user_agent: Optional[str] = None,
        referer: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str, Dict[str, float]]:
        cfg = get_config()
        start = time.time()
        browser_launch_ms = None
        real_proxy_ip = None
        with sync_playwright() as p:
            launch_opts = {"headless": headless}
            
            if proxy_server:
                real_proxy_ip = None
                if httpx is not None:
                    try:
                        client = None
                        if hasattr(httpx, "HTTPTransport"):
                            transport = httpx.HTTPTransport(proxy=proxy_server)
                            client = httpx.Client(transport=transport, timeout=5)
                        else:
                            client = httpx.Client(proxies={"http://": proxy_server, "https://": proxy_server}, timeout=5)
                        with client as c:
                            resp = c.get("https://httpbin.org/ip")
                            if resp.status_code == 200:
                                real_proxy_ip = resp.json().get("origin")
                                logging.info(f"detected real proxy ip: {real_proxy_ip}")
                    except Exception as e:
                        logging.warning(f"fail to detect real proxy ip: {e}")
                else:
                    logging.warning("httpx not available, skip proxy ip detection")
                launch_opts["proxy"] = {"server": proxy_server}
            b0 = time.time()
            browser = p.chromium.launch(**launch_opts)
            browser_launch_ms = (time.time() - b0) * 1000.0
            ctx_opts = {}
            if user_agent:
                ctx_opts["user_agent"] = user_agent
            context = browser.new_context(**ctx_opts)
            logging.info(f"launch browser headless={headless} proxy={proxy_server or ''}")
            blocked_types = {"image", "media", "font", "stylesheet"}
            blocked_hosts = {
                "doubleclick.net",
                "googlesyndication.com",
                "google-analytics.com",
                "googletagmanager.com",
                "adservice.google.com",
                "gstatic.com",
            }
            req_total = 0
            req_blocked = 0
            req_allowed = 0
            req_xhr = 0
            def handle_route(route, request):
                try:
                    nonlocal req_total, req_blocked, req_allowed, req_xhr
                    req_total += 1
                    if request.resource_type in {"xhr", "fetch"}:
                        req_xhr += 1
                    if request.resource_type in blocked_types:
                        req_blocked += 1
                        return route.abort()
                    host = urlparse(request.url).hostname or ""
                    for h in blocked_hosts:
                        if host.endswith(h):
                            req_blocked += 1
                            return route.abort()
                except Exception:
                    pass
                req_allowed += 1
                return route.continue_()
            context.route("**/*", handle_route)
            page = context.new_page()
            if referer:
                page.set_extra_http_headers({"Referer": referer})
            if cookie:
                try:
                    page.context.add_cookies(
                        [
                            {
                                "name": "itdog_cookie",
                                "value": cookie,
                                "domain": "www.itdog.cn",
                                "path": "/",
                            }
                        ]
                    )
                except Exception:
                    pass
            logging.info("goto itdog http page")
            try:
                page.goto("https://www.itdog.cn/http/", wait_until="domcontentloaded")
            except Exception as e:
                logging.error(f"请求 itdog 网站失败, url=https://www.itdog.cn/http/, 请检查网络或者代理: {e}")
                raise
            input_locator = page.locator('input[name="url"], input#url, input[placeholder*="http"], input[placeholder*="域名"], input')
            input_locator.first.fill(domain)
            logging.info(f"filled domain={domain}")
            btn = page.locator("button[onclick*=\"check_form('fast')\"]")
            if btn.count() == 0:
                btn = page.get_by_role("button", name=re.compile("检测|开始|测速|测试"))
                if btn.count() > 1:
                    btn = btn.nth(0)
                elif btn.count() == 0:
                    btn = page.locator("button, .btn, .button").first
            btn.click()
            logging.info("start detection clicked")
            done = False
            for _ in range(120):
                try:
                    progress = page.locator("text=当前进度")
                    if progress.count() > 0 and "100%" in progress.first.inner_text():
                        done = True
                        logging.info("progress reached 100%")
                        break
                except Exception:
                    pass
                rows = page.locator("table tbody tr")
                if rows.count() > 0:
                    done = True
                    logging.info(f"table rows appeared count={rows.count()}")
                    break
                time.sleep(1)
            if not done:
                try:
                    page.wait_for_selector("table tbody tr", timeout=30000)
                    done = True
                except PlaywrightTimeoutError:
                    pass
            sections_to_click = []
            for label in ["中国地区", "海外地区", "全部节点"]:
                loc = page.get_by_text(label, exact=False)
                if loc.count() > 0:
                    sections_to_click.append(label)
            for label in sections_to_click:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=2000)
                    page.wait_for_timeout(800)
                    logging.info(f"section clicked label={label}")
                except Exception:
                    pass
            stable = 0
            last_count = -1
            for _ in range(20):
                current = page.locator("table tbody tr").count()
                if current == last_count and current > 0:
                    stable += 1
                else:
                    stable = 0
                last_count = current
                if stable >= 3:
                    break
                page.wait_for_timeout(500)
            screenshot_path = None
            if getattr(cfg, "screenshot_enabled", True):
                safe_domain = re.sub(r"[^a-zA-Z0-9_.-]", "_", domain)
                screenshot_dir = cfg.screenshot_dir
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = os.path.join(screenshot_dir, f"itdog_{int(time.time())}_{safe_domain}.png")
                try:
                    page.evaluate(
                        """
                        async () => {
                          let last = 0;
                          for (let i=0;i<20;i++) {
                            window.scrollTo(0, document.body.scrollHeight);
                            await new Promise(r => setTimeout(r, 400));
                            const h = document.body.scrollHeight;
                            if (Math.abs(h - last) < 5) break;
                            last = h;
                          }
                        }
                        """
                    )
                except Exception:
                    pass
                page.screenshot(path=screenshot_path, full_page=True)
                logging.info(f"screenshot path={screenshot_path}")
            results: List[Dict[str, Any]] = []
            tables = page.locator("table")
            for i in range(tables.count()):
                tbl = tables.nth(i)
                headers = []
                if tbl.locator("thead tr").count() > 0:
                    headers = [h.strip() for h in tbl.locator("thead tr").nth(0).locator("th,td").all_text_contents()]
                else:
                    if tbl.locator("tbody tr").count() > 0:
                        headers = [h.strip() for h in tbl.locator("tbody tr").nth(0).locator("th,td").all_text_contents()]
                header_l = [h.lower() for h in headers]
                key_hits = sum(
                    1
                    for k in ["下载", "连接", "dns", "总", "状态", "ip"]
                    if any(k in h for h in header_l)
                )
                if key_hits < 4:
                    continue
                if any("区域/运营商" in h or "最快" in h or "最慢" in h or "平均" in h for h in headers):
                    continue
                header_map = {j: (headers[j].lower() if j < len(headers) else "") for j in range(max(len(headers), 0))}
                body_rows = tbl.locator("tbody tr")
                start_row = 0
                if headers and tbl.locator("tbody tr").count() > 0:
                    first_row_header_like = [t.strip().lower() for t in tbl.locator("tbody tr").nth(0).locator("th").all_text_contents()]
                    if first_row_header_like:
                        start_row = 1
                for r in range(start_row, body_rows.count()):
                    row = body_rows.nth(r)
                    cells = row.locator("td,th")
                    texts = [c.strip() for c in cells.all_text_contents()]
                    if not texts:
                        continue
                    text_join = " ".join(texts)
                    operator = None
                    region = None
                    download_time = None
                    connect_time = None
                    dns_time = None
                    total_time = None
                    status_code = None
                    ip_location = None
                    response_ip = None
                    for idx, tx in enumerate(texts):
                        key = header_map.get(idx, "")
                        if "运营" in key:
                            operator = tx
                        elif "地区" in key or "区域" in key:
                            region = tx
                        elif "下载" in key:
                            download_time = parse_float(tx)
                        elif "连接" in key:
                            connect_time = parse_float(tx)
                        elif "dns" in key or "解析" in key:
                            dns_time = parse_float(tx)
                        elif "总" in key:
                            total_time = parse_float(tx)
                        elif "状态" in key or "http" in key:
                            m = re.search(r"\b\d{3}\b|失败|错误|--", tx)
                            status_code = m.group(0) if m else tx.strip()
                        elif "响应ip" in key or ("ip" in key and "响应" in key):
                            response_ip = extract_ip(tx)
                        elif ("ip" in key) and ("所在" in key or "地区" in key or "位置" in key):
                            ip_location = tx
                    if (operator is None or region is None) and texts:
                        first = texts[0]
                        isp_match = re.search(r"(电信|联通|移动|广电|教育网)", first)
                        if isp_match and operator is None:
                            operator = isp_match.group(1)
                        if re.search(r"[\u4e00-\u9fff]", first):
                            region_guess = re.sub(r"(电信|联通|移动|广电|教育网)", "", first).strip()
                            if region is None and region_guess:
                                region = region_guess or region
                    has_times = any([download_time is not None, connect_time is not None, dns_time is not None, total_time is not None])
                    has_status = status_code is not None
                    has_identity = response_ip is not None or operator is not None or region is not None
                    tokens = {"--", "失败", "错误", "超时", "无响应", "未解析", "timeout", "Timeout"}
                    token_hit = any(t in text_join for t in tokens)
                    if not (has_times or has_status or has_identity or token_hit):
                        continue
                    if any(re.match(r"^HTTP/\\d\\.\\d", t) for t in texts):
                        continue
                    if re.search(r"^\\s*HTTP/\\d\\.\\d", text_join):
                        continue
                    if not response_ip:
                        response_ip = extract_ip(text_join) or None
                    if not ip_location and response_ip:
                        m2 = re.search(rf"{re.escape(response_ip)}\s*(.+)", text_join)
                        ip_location = m2.group(1).strip() if m2 else None
                    header_like = any(h in text_join for h in ["HTTP/1.1", "Location:", "Content-Type:", "Cache-Control", "Server:", "Set-Cookie:", "X-Frame-Options", "Strict-Transport-Security", "Transfer-Encoding"])
                    def _is_valid_ip(ip: Optional[str]) -> bool:
                        if not ip:
                            return False
                        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip):
                            return True
                        return ":" in ip and re.search(r"[0-9A-Fa-f:]", ip) is not None
                    valid_ip = _is_valid_ip(response_ip)
                    if str(status_code).strip() == "301" and (not valid_ip) and (not has_times):
                        continue
                    if header_like and re.search(r"^\\s*HTTP/\\d\\.\\d", text_join):
                        continue
                    if header_like and (not valid_ip) and (not has_times) and (not token_hit):
                        continue
                    if is_suspicious_region(region):
                        cleaned = None
                        if ip_location:
                            cleaned = ip_location.replace("/google.com", "").replace("/", " ").strip()
                        if cleaned:
                            region = cleaned
                        else:
                            region = None
                    country, province, city, isp = split_location(ip_location)
                    if operator is None:
                        if any("海外" in t for t in texts) or (region and "海外" in region):
                            operator = "海外"
                    if isinstance(region, str) and region.strip().startswith("海外"):
                        region = re.sub(r"^海外[\s\r\n\t]+", "", region).strip()
                    def clean_region_from_ip_location(loc: Optional[str]) -> Optional[str]:
                        if not loc:
                            return None
                        s = loc.replace("/google.com", "")
                        s = s.replace("/", " ").strip()
                        return s or None
                    if (region is None) or (isinstance(region, str) and (region.strip().startswith("HTTP/") or "Content-Type:" in region or "Cache-Control" in region)):
                        rg = clean_region_from_ip_location(ip_location)
                        if rg:
                            region = rg
                    raw_html = row.evaluate("node => node.outerHTML")
                    results.append(
                        {
                            "operator": operator,
                            "region": region,
                            "download_time": download_time,
                            "connect_time": connect_time,
                            "dns_time": dns_time,
                            "total_time": total_time,
                            "status_code": status_code,
                            "ip_location": ip_location,
                            "response_ip": response_ip,
                            "raw": {"source": "itdog", "row_html": raw_html, "texts": texts},
                            "ip_country": country,
                            "ip_province": province,
                            "ip_city": city,
                            "ip_isp": isp,
                        }
                    )
            uniq: List[Dict[str, Any]] = []
            seen = set()
            for r in results:
                key = (
                    (r.get("operator") or "").strip(),
                    (r.get("region") or "").strip(),
                    (r.get("response_ip") or "").strip(),
                    (r.get("status_code") or "").strip(),
                )
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(r)
            results = uniq
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
        collect_ms = (time.time() - start) * 1000.0
        metrics = {
            "browser_launch_ms": browser_launch_ms or 0.0,
            "collect_ms": collect_ms,
            "request_total": req_total,
            "request_blocked": req_blocked,
            "request_allowed": req_allowed,
            "request_xhr": req_xhr,
            "real_proxy_ip": real_proxy_ip,
        }
        logging.info(f"results collected count={len(results)} req_total={req_total} req_blocked={req_blocked} req_allowed={req_allowed} req_xhr={req_xhr}")
        return results, screenshot_path, metrics
