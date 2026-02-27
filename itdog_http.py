import json
import sys
import re
from html import unescape
from urllib.request import Request, urlopen
from urllib.parse import quote_plus
import ssl
from typing import Any, Dict, List, Optional
import logging
logger = logging.getLogger("itdog_http")

#
# 脚本目的：
# - 自动化采集 https://www.itdog.cn/http/ 的 HTTP测速结果
# - 优先使用静态页面解析，数量不足时回退到浏览器自动化以获取完整节点
# - 将采集结果输出为结构化 JSON，并记录详细运行日志到 run.log


def _fill_domain_input(page, domain: str) -> None:
    """在页面中定位并填写待测域名，支持多种选择器作为回退。"""
    candidates = [
        'input[name="url"]',
        'input[placeholder*="URL"]',
        'input[placeholder*="域名"]',
        'input[placeholder*="网址"]',
        'input[type="text"]',
        'textarea[name="url"]',
    ]
    for sel in candidates:
        logger.debug("尝试选择器: %s", sel)
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.fill(domain)
                logger.info("成功使用选择器定位到 '域名输入框' : %s", sel)
                return
            except Exception:
                logger.error("选择器 %s 填写域名 %s 时出错", sel, domain, exc_info=True)
                pass
    logger.error("所有候选选择器均未能定位到域名输入框，domain=%s", domain)
    raise RuntimeError("未找到域名输入框")


def _click_start(page) -> None:
    """触发测速：优先点击“快速测试/开始测试”等按钮，必要时回退到任意按钮。"""
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
    logger.error("未找到 '开始测试' 按钮")
    raise RuntimeError("未找到开始测试按钮")

def _wait_finish(page, timeout_ms: int) -> None:
    """等待页面渲染完成与进度提示到 100%，并确保网络空闲。"""
    try:
        logger.debug("等待页面进度提示或完成")
        page.wait_for_selector("text=当前进度", timeout=timeout_ms)
    except Exception:
        logger.warning("未检测到“当前进度”文本，继续等待网络空闲")  # 记录异常信息
        pass
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    page.wait_for_timeout(1000)
    try:
        page.wait_for_selector("text=100%", timeout=timeout_ms)
    except Exception:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)


def _extract_rows(page) -> List[List[str]]:
    """从当前可视表格中提取每行文本列数据（优先匹配含“查看”的行）。"""
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
    """将行数组映射为字段字典，并从“检测点”拆分出线路与地区。"""
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
    operator = ""
    line = data.get("线路", "")
    if line in {"电信", "联通", "移动", "海外", "港澳台"}:
        operator = line
    else:
        ippos = data.get("IP位置", "")
        for op in ("电信", "联通", "移动", "铁通", "教育网", "广电", "港澳台"):
            if op in ippos:
                operator = op
                break
    data["运营商"] = operator
    return data


def _http_fetch_html(url: str, timeout_s: int = 60) -> str:
    logger.debug("http_fetch start url=%s timeout_s=%s", url, timeout_s)
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout_s) as resp:
        content = resp.read()
    logger.debug("http_fetch ok url=%s size=%s", url, len(content))
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode("latin1", errors="ignore")


def _extract_rows_from_html(html: str) -> List[List[str]]:
    """不启动浏览器，直接在HTML源码中解析表格行数据。"""
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
    """使用 Playwright 浏览器采集：遍历标签、滚动懒加载、点击分页以覆盖尽可能多的节点。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logger.warning("playwright import failed, skip browser fallback")
        return {"domain": domain, "count": 0, "results": []}

    def _scroll_all(page):
        last_height = 0
        for _ in range(20):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
            height = page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height

    def _collect_rows_current(page) -> List[List[str]]:
        _scroll_all(page)
        rows = _extract_rows(page)
        if rows:
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

    def _click_if_exists(page, text: str):
        try:
            el = page.get_by_text(text, exact=True)
            if el.count() > 0:
                el.first.click()
                page.wait_for_timeout(300)
                return True
        except Exception:
            pass
        return False

    def _collect_all_tabs(page) -> List[List[str]]:
        """遍历所有标签页并分页滚动采集，尽可能覆盖全部节点。"""
        tabs = ["全部", "中国电信", "中国联通", "中国移动", "港澳台、海外"]
        all_rows: List[List[str]] = []
        for t in tabs:
            logger.debug("tab start %s", t)
            _click_if_exists(page, t)
            page.wait_for_load_state("networkidle", timeout=5000)
            _scroll_all(page)
            part = _collect_rows_current(page)
            if part:
                all_rows.extend(part)
            logger.debug("tab got rows=%s", len(all_rows))
            # 分页遍历：先尝试“下一页/Next/»”，再尝试数字页码
            for _ in range(30):
                clicked = False
                for next_text in ["下一页", "下页", "Next", "下一页 »", "»"]:
                    if _click_if_exists(page, next_text):
                        clicked = True
                        page.wait_for_load_state("networkidle", timeout=5000)
                        _scroll_all(page)
                        more = _collect_rows_current(page)
                        if more:
                            all_rows.extend(more)
                        logger.debug("tab next got rows=%s", len(all_rows))
                        break
                if not clicked:
                    pagers = page.locator(".pagination a, .dataTables_paginate a, a.page-link, button.page-link")
                    count = pagers.count()
                    if count == 0:
                        break
                    progressed = False
                    for i in range(count):
                        a = pagers.nth(i)
                        txt = a.inner_text().strip()
                        if re.fullmatch(r"\d+", txt):
                            try:
                                a.click()
                                page.wait_for_load_state("networkidle", timeout=5000)
                                _scroll_all(page)
                                more = _collect_rows_current(page)
                                if more:
                                    all_rows.extend(more)
                                progressed = True
                                logger.debug("tab page %s rows=%s", txt, len(all_rows))
                            except Exception:
                                logger.exception("page click failed text=%s", txt)
                                pass
                    if not progressed:
                        break
        return all_rows

    with sync_playwright() as p:
        logger.info("browser launch headless=%s", headless)
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        page = ctx.new_page()
        logger.info("打开浏览器页面: https://www.itdog.cn/http/")
        page.goto("https://www.itdog.cn/http/", wait_until="domcontentloaded")
        _fill_domain_input(page, domain)
        _click_start(page)
        _wait_finish(page, timeout_ms=timeout_s * 1000)
        rows = _collect_all_tabs(page)
        data_rows = [_map_row(r) for r in rows]
        logger.info("browser collected rows=%s", len(data_rows))
        browser.close()
    result: Dict[str, Any] = {"domain": domain, "count": len(data_rows), "results": data_rows}
    return result


def run_itdog_http(domain: str, headless: bool = False, timeout_s: int = 120) -> Dict[str, Any]:
    """统一入口：先静态解析，若不足则回退到浏览器采集并做合并去重。"""
    logger.info("run start domain=%s headless=%s timeout_s=%s", domain, headless, timeout_s)
    encoded = quote_plus(domain)
    try:
        html = _http_fetch_html(f"https://www.itdog.cn/http/?url={encoded}", timeout_s=timeout_s)
    except Exception:
        logger.exception("http fetch failed, try unverified ssl")
        try:
            # 证书异常兜底：使用不验证证书的上下文进行单次请求（仅此处）
            cafile = None
            try:
                import certifi  # type: ignore

                cafile = certifi.where()
            except Exception:
                cafile = None
            headers = {
                "user-agent": "Mozilla/5.0",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            req = Request(f"https://www.itdog.cn/http/?url={encoded}", headers=headers)
            ctx = ssl._create_unverified_context()
            with urlopen(req, context=ctx, timeout=timeout_s) as resp:
                content = resp.read()
            try:
                html = content.decode("utf-8", errors="ignore")
            except Exception:
                html = content.decode("latin1", errors="ignore")
        except Exception:
            logger.exception("unverified ssl fetch failed")
            html = ""
    rows = _extract_rows_from_html(html)
    data_rows = [_map_row(r) for r in rows]
    logger.info("static parsed rows=%s", len(data_rows))
    if len(data_rows) >= 100:
        return {"domain": domain, "count": len(data_rows), "results": data_rows}
    browser_result = run_itdog_http_playwright(domain, headless=headless, timeout_s=timeout_s)
    if browser_result.get("results"):
        # 合并去重策略：以“检测点+响应IP”为唯一键
        seen = set()
        merged: List[Dict[str, Any]] = []
        for r in data_rows + browser_result["results"]:
            key = (r.get("检测点", ""), r.get("响应IP", ""))
            if key not in seen:
                seen.add(key)
                merged.append(r)
        logger.info("merged rows=%s", len(merged))
        return {"domain": domain, "count": len(merged), "results": merged}
    return {"domain": domain, "count": len(data_rows), "results": data_rows}


def main(argv: Optional[List[str]] = None) -> None:
    """命令行入口：初始化日志文件、解析参数、执行采集并输出 JSON。"""
    if not logger.handlers:
        handler = logging.FileHandler("run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("用法: python itdog_http.py <域名或URL>")
        sys.exit(1)
    domain = args[0]
    logger.info("cli start domain=%s", domain)
    out = run_itdog_http(domain)
    logger.info("cli finish domain=%s count=%s", domain, out.get("count"))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
