import json
import sys
import re
from html import unescape
from urllib.request import Request, urlopen
from typing import Any, Dict, List, Optional


def _fill_domain_input(page, domain: str) -> None:
    candidates = [
        'input[name="url"]',
        'input[placeholder*="URL"]',
        'input[placeholder*="域名"]',
        'input[placeholder*="网址"]',
        'input[type="text"]',
        'textarea[name="url"]',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.fill(domain)
                return
            except Exception:
                pass
    raise RuntimeError("未找到域名输入框")


def _click_start(page) -> None:
    texts = ["快速测试", "开始测试", "测试", "开始", "立即测试"]
    for t in texts:
        try:
            page.get_by_text(t, exact=True).first.click()
            return
        except Exception:
            pass
    buttons = page.locator("button")
    if buttons.count() > 0:
        buttons.first.click()
        return
    raise RuntimeError("未找到开始测试按钮")


def _wait_finish(page, timeout_ms: int) -> None:
    try:
        page.wait_for_selector("text=当前进度", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    page.wait_for_timeout(1000)
    try:
        page.wait_for_selector("text=100%", timeout=timeout_ms)
    except Exception:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)


def _extract_rows(page) -> List[List[str]]:
    rows = page.eval_on_selector_all(
        'a:has-text("查看")',
        """els => els.map(el => {
            const tr = el.closest('tr');
            if (!tr) return null;
            const tds = Array.from(tr.querySelectorAll('td'));
            const vals = tds.map(td => td.innerText.trim());
            return vals;
        }).filter(Boolean)""",
    )
    if rows and isinstance(rows[0], list):
        return rows
    tables = page.locator("table")
    if tables.count() > 0:
        body_rows = tables.first.locator("tbody tr")
        result = []
        for i in range(body_rows.count()):
            tds = body_rows.nth(i).locator("td")
            vals = []
            for j in range(tds.count()):
                vals.append(tds.nth(j).inner_text().strip())
            if vals:
                result.append(vals)
        return result
    return []


def _map_row(row: List[str]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if not row:
        return data
    keys = ["检测点", "响应IP", "IP位置", "状态", "总耗时", "解析", "连接", "下载", "重定向", "Head"]
    for i, k in enumerate(keys):
        if i < len(row):
            data[k] = row[i]
    if "检测点" in data:
        d = data["检测点"].replace("\n", " ").strip()
        parts = [p for p in d.split() if p]
        if len(parts) >= 2:
            data["线路"] = parts[0]
            data["地区"] = " ".join(parts[1:])
        else:
            data["地区"] = d
    return data


def _http_fetch_html(url: str, timeout_s: int = 60) -> str:
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout_s) as resp:
        content = resp.read()
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode("latin1", errors="ignore")


def _extract_rows_from_html(html: str) -> List[List[str]]:
    trs = re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S | re.I)
    rows: List[List[str]] = []
    for tr in trs:
        if "查看" not in tr:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)
        vals: List[str] = []
        for td in tds:
            txt = re.sub(r"<[^>]+>", "", td, flags=re.S | re.I)
            txt = unescape(txt).strip()
            txt = re.sub(r"\s+", " ", txt)
            vals.append(txt)
        if vals:
            rows.append(vals)
    return rows


def run_itdog_http_playwright(domain: str, headless: bool = True, timeout_s: int = 120) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"domain": domain, "count": 0, "results": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://www.itdog.cn/http/", wait_until="domcontentloaded")
        _fill_domain_input(page, domain)
        _click_start(page)
        _wait_finish(page, timeout_ms=timeout_s * 1000)
        rows = _extract_rows(page)
        data_rows = [_map_row(r) for r in rows]
        browser.close()
    result: Dict[str, Any] = {"domain": domain, "count": len(data_rows), "results": data_rows}
    return result


def run_itdog_http(domain: str, headless: bool = False, timeout_s: int = 120) -> Dict[str, Any]:
    html = _http_fetch_html(f"https://www.itdog.cn/http/?url={domain}", timeout_s=timeout_s)
    rows = _extract_rows_from_html(html)
    data_rows = [_map_row(r) for r in rows]
    if data_rows:
        return {"domain": domain, "count": len(data_rows), "results": data_rows}
    return run_itdog_http_playwright(domain, headless=headless, timeout_s=timeout_s)


def main(argv: Optional[List[str]] = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("用法: python itdog_http.py <域名或URL>")
        sys.exit(1)
    domain = args[0]
    out = run_itdog_http(domain)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
