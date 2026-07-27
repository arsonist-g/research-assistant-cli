"""浏览器搜索引擎（browser 平台 search 能力）：无头 DrissionPage 抓必应/谷歌结果页。

关键词进 → 结果列表出（url/title/snippet）。无头跑（结果页无 CF 挑战，headless 快且不打扰用户）；
偶发 consent/挑战由 cfbypass.solve 兜底（无挑战 is_cf_challenge 秒返 True，开销极低）。

翻页：首页底部的页数表里，每个页码 <a> 的 href 就是该页完整链接（必应 /search?q=...&first=N、
谷歌 /search?q=...&start=N）。一次抽全所有页码链接、按页码升序、逐个访问；累计结果数 ≥ limit 即停，
按 url 去重，最多翻 max_pages 页（默认 10，--max-pages 可调）。不必反复点"下一页"。

结果 URL：优先结果块的 <cite>（必应/谷歌在 cite 显示真实 URL）；cite 不可用（面包屑或缺失）时
回退标题链接的 href（必应是 /ck/a 跳转包装，解其 u= base64 取真实 URL）。

选择器集中放 SELECTORS（CSS，DrissionPage ele/eles），实测于 2026-07。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote_plus, urljoin

from ..config import Config
from ..errors import ArgsError, ResearchAssistantError
from . import cfbypass
from .browser import (
    _acquire_browser_slot,
    _build_dp_options,
    _channel_executable,
    _close_browser,
    _new_profile_dir,
    _release_browser_slot,
    _safe_rmtree,
    _wait_network_idle,
    _write_suppress_prefs,
)

logger = logging.getLogger("research_assistant.fetch.search_engine")

DEFAULT_MAX_PAGES = 10  # 默认翻页上限，防失控；可由调用方（CLI --max-pages）覆盖
_SEARCH_CONCURRENCY = 4  # 搜索翻页并发 tab 数（与 fetch 默认并发对齐）

# 引擎搜索 URL 模板（{q}=URL 编码后的关键词）。翻页靠页数表链接，不硬编码 offset 参数。
ENGINE_URLS: dict[str, str] = {
    "bing-cn": "https://cn.bing.com/search?q={q}",
    "bing-intl": "https://www.bing.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
}

# 选择器（CSS，DrissionPage ele/eles），实测于 2026-07。
# - item: 结果块；link: 标题链接（取 title）；snippet: 摘要；cite: 真实 URL（优先于 link href）。
# - pages: 首页底部页数表里的页码 <a>（一次抽所有页，按页码排序逐个访问）。
SELECTORS: dict[str, dict[str, list[str]]] = {
    "bing-cn": {
        "item": ["css:li.b_algo"],
        "link": ["css:h2 a"],
        "snippet": ["css:p.b_lineclamp2", "css:.b_caption p", "css:div.b_lineclamp4"],
        "cite": ["css:.b_caption cite", "css:cite"],
        "pages": ["css:li.b_pag a[href]"],
    },
    "bing-intl": {
        "item": ["css:li.b_algo"],
        "link": ["css:h2 a"],
        "snippet": ["css:p.b_lineclamp2", "css:.b_caption p", "css:div.b_lineclamp4"],
        "cite": ["css:.b_caption cite", "css:cite"],
        "pages": ["css:li.b_pag a[href]"],
    },
    "google": {
        "item": ["css:div.g", "css:div.MXl0lf"],
        "link": ["css:h3", "css:a h3"],
        "snippet": ["css:div.VwiC3b", "css:span.aCOpRe"],
        "cite": ["css:cite"],
        "pages": ["css:table.AaVjTc a[href]", "css:div[role=navigation] a[href]"],
    },
}


def _engine_url(engine: str, query: str) -> str:
    """构造引擎首页搜索 URL。engine 非法抛 ArgsError。"""
    if engine not in ENGINE_URLS:
        raise ArgsError(f"未知搜索引擎: {engine}（可选 bing-cn|bing-intl|google）")
    return ENGINE_URLS[engine].format(q=quote_plus(query))


def _decode_bing_url(url: str) -> str | None:
    """bing /ck/a 跳转链接解出真实 URL（u= 参数 base64url）。非 ck/a 或失败返回 None。

    结果块 cite 不可用时的回退：标题链接 href 是 https://www.bing.com/ck/a?...&u=a1<base64url> 跳转包装。
    """
    if "/ck/a" not in url:
        return None
    try:
        import base64
        from urllib.parse import urlparse, parse_qs

        u = (parse_qs(urlparse(url).query).get("u") or [""])[0]
        if not u:
            return None
        payload = u[2:] if u[:2].lower() == "a1" else u
        payload += "=" * (-len(payload) % 4)  # 补 base64 padding
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace")
        return decoded if decoded.startswith("http") else None
    except Exception:
        return None


def _first(container: Any, selectors: list[str]) -> Any | None:
    """按顺序试多个 CSS 选择器，返回首个查到的元素（DrissionPage ele）；都无返回 None。"""
    for sel in selectors:
        try:
            el = container.ele(sel, timeout=1.5)
            if el:
                return el
        except Exception:
            continue
    return None


def _all(container: Any, selectors: list[str]) -> list[Any]:
    """按顺序试多个 CSS 选择器，返回首个查到非空的结果列表（DrissionPage eles）；都无返回 []。"""
    for sel in selectors:
        try:
            els = container.eles(sel, timeout=1.5)
            if els:
                return list(els)
        except Exception:
            continue
    return []


def _parse_page_num(a: Any, text: str) -> int | None:
    """从页码 <a> 解析页码 int。必应 a 文本是纯数字；谷歌 aria-label="Page N"。非页码返回 None。"""
    t = (text or "").strip()
    if t.isdigit():
        return int(t)
    try:
        label = a.attr("aria-label") or ""
    except Exception:
        label = ""
    m = re.search(r"\d+", label)
    return int(m.group(1)) if m else None


def _extract_results(page: Any, engine: str) -> list[dict[str, str]]:
    """从当前结果页抽 [{url, title, snippet}]。

    url 取标题链接 href：必应是 /ck/a 跳转，解其 u= base64 得完整真实 URL；非 ck 直接用 href。
    （cite 元素只显示根域名、不完整，实测确认不用——完整真实 URL 只在标题链接的 ck/a 跳转里。）
    """
    sels = SELECTORS[engine]
    items = _all(page, sels["item"])
    out: list[dict[str, str]] = []
    for it in items:
        link_el = _first(it, sels["link"])
        if link_el is None:
            continue
        raw = (getattr(link_el, "link", None) or link_el.attr("href") or "").strip()
        if not raw or raw.startswith(("javascript:", "#")):
            continue
        url = _decode_bing_url(raw) or raw
        title = (getattr(link_el, "text", "") or "").strip()
        snippet_el = _first(it, sels["snippet"])
        snippet = (getattr(snippet_el, "text", "") or "").strip() if snippet_el else ""
        out.append({"url": url, "title": title, "snippet": snippet})
    return out


def _extract_page_links(page: Any, engine: str) -> list[tuple[int, str]]:
    """从首页底部页数表抽所有页码链接 [(page_num, abs_url)]，按页码升序、去重。

    每个页码 <a> 的 href 就是该页完整链接（必应 &first=N / 谷歌 &start=N）。排除当前页（1）与
    非页码（下一页/上一页，文本非数字、aria-label 无数字）。相对 href 按当前页 url 解析为绝对。
    """
    sels = SELECTORS[engine]
    links = _all(page, sels["pages"])
    by_num: dict[int, str] = {}
    base = getattr(page, "url", "") or ""
    for a in links:
        try:
            text = (getattr(a, "text", "") or "").strip()
            href = (getattr(a, "link", None) or a.attr("href") or "").strip()
        except Exception:
            continue
        if not href or href.startswith(("javascript:", "#")):
            continue
        num = _parse_page_num(a, text)
        if num is None or num <= 1:  # 跳过当前页(1)与非页码(下一页/上一页)
            continue
        if not href.startswith(("http://", "https://")):
            href = urljoin(base, href)
        by_num.setdefault(num, href)
    return sorted(by_num.items())


def _run_engine_sync(config: Config, query: str, engine: str, limit: int, max_pages: int, timeout: int = 60) -> list[dict[str, str]]:
    """同步：headless DrissionPage，1 browser 多 tab 智能并发翻页。

    1. 首页（tab0）：get → _wait_network_idle → CF 兜底 solve → _extract_results(N) + _extract_page_links。
    2. 推断：据已抓每页结果数算需补页数 ceil((limit-已抓)/per_page)，受 max_pages 与页数表长度约束。
    3. 并发：主线程 new_tab 建 tab，ThreadPoolExecutor 每页 tab get → idle → solve → _extract_results。
    4. 两阶段补页：累计 < limit 且页数表还有未抓页 → 据已抓页最少结果数重算下批页数，取下一批。
    5. 边缘：页数表页数有限（如必应某词只 3 页）用完仍 < limit → 返回已有。
    """
    if _channel_executable(config.browser.channel) is None:
        raise ResearchAssistantError(
            f"未找到本地浏览器 ({config.browser.channel})，搜索引擎不可用"
        )
    from DrissionPage import ChromiumPage

    deadline = time.monotonic() + timeout  # 整体超时：超时退出翻页，返回已收集的
    profile_dir = _new_profile_dir()
    lock_dir = _acquire_browser_slot(config)
    page = None
    browser_pid = 0
    seen: set[str] = set()
    collected: list[dict[str, str]] = []

    def absorb(results: list[dict[str, str]]) -> None:
        for r in results:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            collected.append(r)

    def fetch_in_tab(tab: Any, url: str) -> list[dict[str, str]]:
        logger.info("GET %s", url)
        tab.get(url)
        _wait_network_idle(tab)
        try:  # 偶发 CF/挑战兜底（无挑战 solve 秒过，开销极低）
            if cfbypass.is_cf_challenge(tab):
                logger.info("结果页遇 CF 挑战，调用 cfbypass.solve")
                cfbypass.solve(tab)
        except Exception as e:
            logger.debug("solve 兜底跳过: %s", e)
        return _extract_results(tab, engine)

    try:
        _write_suppress_prefs(profile_dir)
        page = ChromiumPage(_build_dp_options(config, profile_dir, headless=True))
        browser_pid = getattr(getattr(page, "browser", None), "process_id", 0) or 0
        logger.info(
            "搜索引擎(%s) headless 多 tab(%s)，query=%r limit=%d max_pages=%d",
            engine, config.browser.channel, query, limit, max_pages,
        )

        # 1) 首页（tab0）
        first_results = fetch_in_tab(page, _engine_url(engine, query))
        per_page_est = max(len(first_results), 1)
        absorb(first_results)
        logger.info("首页：累计 %d/%d（本页 %d 结果）", len(collected), limit, per_page_est)
        if len(collected) >= limit:
            return collected

        # 首页页数表抽所有页码链接
        page_links = _extract_page_links(page, engine)
        if not page_links:
            return collected

        # 2-4) 智能并发翻页（两阶段补页）
        idx = 0  # page_links 游标
        pages_done = 1  # 含首页
        pages_cap = max(1, max_pages)
        while len(collected) < limit and idx < len(page_links) and pages_done < pages_cap and time.monotonic() < deadline:
            # 据已抓每页结果数推断本批该抓多少页（ceil 补足，受剩余页数额度约束）
            need_for_limit = math.ceil(max(limit - len(collected), 1) / max(per_page_est, 1))
            batch_size = max(1, min(need_for_limit, pages_cap - pages_done))
            batch = page_links[idx: idx + batch_size]
            idx += len(batch)
            if not batch:
                break
            pages_done += len(batch)

            # 主线程建 tab，工作线程并发抓（new_tab 浏览器级只主线程；tab 级 WS 工作线程安全）
            tabs = [page.new_tab(background=True) for _ in batch]

            def work(iu: tuple[int, tuple[int, str]]) -> tuple[int, list[dict[str, str]]]:
                i, (num, purl) = iu
                tab = tabs[i]
                try:
                    results = fetch_in_tab(tab, purl)
                    return num, results
                except Exception as e:
                    logger.warning("翻页 tab 抓取失败（页码 %d）: %s", num, e)
                    return num, []
                finally:
                    try:
                        tab.close()
                    except Exception:
                        pass

            batch_results: list[list[dict[str, str]]] = []
            with ThreadPoolExecutor(max_workers=min(len(batch), _SEARCH_CONCURRENCY)) as ex:
                for num, results in ex.map(work, enumerate(batch)):
                    batch_results.append(results)
                    logger.info("页码 %d：本页 %d 结果", num, len(results))

            for results in batch_results:
                absorb(results)

            # 据本批更新 per_page_est（取最少，保守估计下批页数）
            nonzero = [len(r) for r in batch_results if r]
            if nonzero:
                per_page_est = max(min(nonzero), 1)

        return collected
    finally:
        _close_browser(page, browser_pid)
        _safe_rmtree(profile_dir)
        _release_browser_slot(lock_dir)


async def search_engine(
    config: Config, query: str, engine: str = "bing-intl", limit: int = 10,
    max_pages: int = DEFAULT_MAX_PAGES, timeout: int = 60,
) -> dict[str, Any]:
    """浏览器搜索引擎：返回 {query, engine, results[]{url, title, snippet}}。

    无头 DrissionPage：首页 → 从页数表抽所有页码链接 → 按页码升序逐个访问，累计 ≥ limit 去重，
    最多 max_pages 页（默认 10）。engine 默认 bing-intl（国内必应对部分敏感词会拒搜，国际版更稳）。
    timeout 限整体查询时长（秒），超时退出翻页、返回已收集的部分结果。
    """
    results = await asyncio.to_thread(_run_engine_sync, config, query, engine, limit, max_pages, timeout)
    return {"query": query, "engine": engine, "results": results}
