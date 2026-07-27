"""fetch 聚合命令 + browser 层单元测试：format 降级映射、html 直出、并发限流、单 URL 超时。

不联网、不起浏览器（live 路径在 test_browser_live.py / test_cf_live.py）。
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# normal 层 format 降级（每家认什么就给什么，不认的降 markdown）
# ---------------------------------------------------------------------------


class _Cap:
    """fake capability：handler 记录收到的 ns，返回模拟结果。"""

    def __init__(self, name, handler):
        self.name = name
        self.handler = handler


class _Prov:
    def __init__(self, caps):
        self._caps = caps

    def capabilities(self):
        return self._caps


async def test_tavily_extract_format_passthrough_and_downgrade():
    """tavily: markdown/text 原样传；html 降级 markdown（tavily extract 不认 html）。"""
    received = []

    async def handler(ns):
        received.append(ns.format)
        return {"results": [{"url": "u", "content": "body"}]}

    classes = {"tavily": lambda cfg: _Prov([_Cap("extract", handler)])}
    from research_assistant.fetch.normal import _try_tavily_extract

    assert await _try_tavily_extract(classes, None, "http://x", "text") == "body"
    assert await _try_tavily_extract(classes, None, "http://x", "markdown") == "body"
    assert await _try_tavily_extract(classes, None, "http://x", "html") == "body"
    assert received == ["text", "markdown", "markdown"]  # html 被降级成 markdown


async def test_firecrawl_scrape_format_and_html_field():
    """firecrawl: markdown/html 原样传；html 读 data.html；text 降级 markdown 读 data.markdown。"""
    received = []

    async def handler(ns):
        received.append(ns.format)
        return {"data": {"html": "<h1>raw</h1>", "markdown": "**md**"}}

    classes = {"firecrawl": lambda cfg: _Prov([_Cap("scrape", handler)])}
    from research_assistant.fetch.normal import _try_firecrawl_scrape

    assert await _try_firecrawl_scrape(classes, None, "http://x", "html") == "<h1>raw</h1>"
    assert await _try_firecrawl_scrape(classes, None, "http://x", "markdown") == "**md**"
    assert await _try_firecrawl_scrape(classes, None, "http://x", "text") == "**md**"
    assert received == ["html", "markdown", "markdown"]  # text 被降级成 markdown


# ---------------------------------------------------------------------------
# browser _fetch_one_tab：fmt=html 直出原始 html，绕过 trafilatura
# ---------------------------------------------------------------------------


def test_fetch_one_tab_html_vs_markdown(monkeypatch):
    """fmt=html 原样返回 tab.html（绕过 html_to_md）；fmt=markdown 走 html_tomd 去标签。"""
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b.cfbypass, "solve", lambda tab: True)
    monkeypatch.setattr(b, "_wait_network_idle", lambda tab, **k: None)
    monkeypatch.setattr(b.time, "sleep", lambda *a, **k: None)

    raw_html = "<html><body><p>" + "content" * 50 + "</p></body></html>"

    class _Tab:
        def get(self, url):
            pass

        @property
        def html(self):
            return raw_html

    # html：原样返回，不经过 trafilatura
    assert b._fetch_one_tab(_Tab(), "http://x", "html") == raw_html

    # markdown：经 html_to_md 转换，标签去掉、正文保留
    md = b._fetch_one_tab(_Tab(), "http://x", "markdown")
    assert md != raw_html
    assert "<html>" not in md
    assert "content" in md


# ---------------------------------------------------------------------------
# fetch 聚合命令 run：单 URL 超时 + 并发限流（normal 层）
# ---------------------------------------------------------------------------


class _RunArgs:
    """fake argparse.Namespace for fetch run。"""

    def __init__(self, urls, timeout=10, concurrency=4, no_browser=True, fmt="markdown"):
        self.urls = urls
        self.write = None
        self.format = fmt
        self.timeout = timeout
        self.no_browser = no_browser
        self.no_login = False
        self.concurrency = concurrency


async def test_fetch_run_timeout_marks_failed(monkeypatch, home):
    """fetch_normal 慢于 timeout → 该 URL 记 failed，不阻塞整批。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    async def slow_normal(config, url, fmt):
        await asyncio.sleep(10)  # 远超 timeout=1
        return "should_not_reach"

    monkeypatch.setattr(fetch_cmd, "fetch_normal", slow_normal)

    result = await fetch_cmd.run(_RunArgs(["http://x"], timeout=1), Config())
    r = result["results"][0]
    assert r["url"] == "http://x"
    assert r["status"] == "failed"


async def test_fetch_run_semaphore_limits_concurrency(monkeypatch, home):
    """normal 层并发受 --concurrency 限制（峰值 ≤ concurrency 且确实并发，非串行）。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    current = 0
    peak = 0

    async def tracked_normal(config, url, fmt):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return f"md-{url}"

    monkeypatch.setattr(fetch_cmd, "fetch_normal", tracked_normal)

    result = await fetch_cmd.run(
        _RunArgs([f"http://x{i}" for i in range(10)], timeout=10, concurrency=3), Config()
    )
    assert peak <= 3   # 不超过 concurrency 上限
    assert peak >= 2   # 确实并发了（不是串行=1）
    assert len(result["results"]) == 10
    assert all(r["status"] == "success" for r in result["results"])


# ---------------------------------------------------------------------------
# browser 层 per-future 超时：单 tab 慢于 timeout → 记 None，finally 杀 PID
# ---------------------------------------------------------------------------


async def test_fetch_all_tabs_per_future_timeout(monkeypatch, home):
    """单 tab 慢于 timeout → fut.result 超时记 None；finally 先 _close_browser（杀 PID）再 shutdown。"""
    import time
    from research_assistant.fetch import browser as b
    from research_assistant.config import Config

    # mock 掉 browser 构造链（不真起浏览器）
    monkeypatch.setattr(b, "_new_profile_dir", lambda: home / "p")
    monkeypatch.setattr(b, "_acquire_browser_slot", lambda cfg: home / "l")
    monkeypatch.setattr(b, "_release_browser_slot", lambda d: None)
    monkeypatch.setattr(b, "_safe_rmtree", lambda d: None)
    monkeypatch.setattr(b, "_write_suppress_prefs", lambda d: None)
    monkeypatch.setattr(b, "_hide_window", lambda p: None)
    monkeypatch.setattr(b, "_inject_cookies_dp", lambda p, c: None)
    closed = []
    monkeypatch.setattr(b, "_close_browser", lambda p, pid: closed.append(pid))

    class _FakeTab:
        def close(self):
            pass

    class _FakeBrowser:
        process_id = 123

    class _FakePage:
        browser = _FakeBrowser()

        def new_tab(self, background=False):
            return _FakeTab()

    monkeypatch.setattr("DrissionPage.ChromiumPage", lambda opts: _FakePage(), raising=False)

    def slow_fetch(tab, url, fmt):
        time.sleep(0.5)  # 超过 timeout=0.2
        return "slow"

    monkeypatch.setattr(b, "_fetch_one_tab", slow_fetch)

    result = await asyncio.to_thread(
        b._fetch_all_tabs_sync, Config(), ["http://x"], [], 2, "markdown", 0.2
    )
    assert result.get("http://x") is None  # 超时 → 记 None 不阻塞
    assert closed == [123]  # finally 调了 _close_browser（杀 PID，让残余 worker 解阻塞）
