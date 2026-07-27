"""browser 平台 live 测试（真实起浏览器，默认跳过）。

默认跳过（marker live_browser + pyproject addopts 排除）。手动运行：

    .venv/Scripts/python.exe -m pytest tests/test_browser_live.py -m live_browser -s

验证 browser 平台端到端：
  - browser search：关键词进 → results 非空，url 是真实目标（非 bing /ck/a 跳转）。
  - browser fetch：grok.com（CF 挑战 + SPA）→ 落盘 md 非空，不含 CF 挑战特征。

依赖：DrissionPage 已装、本地 Edge/Chromium 可执行、外网可达。
注意：browser fetch 是 headed（弹真实 Edge 窗口，CF 识别 headless）；browser search 是 headless。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_browser]

from research_assistant.config import Config
from research_assistant.fetch import fetch_with_browser
from research_assistant.fetch import search_engine as se
from research_assistant.fetch.browser import _channel_executable


def _cfg() -> Config:
    return Config()  # 默认 browser channel=msedge


def _skip_if_unusable() -> None:
    if _channel_executable("msedge") is None:
        pytest.skip("本地未找到 msedge 浏览器可执行")


async def test_search_returns_real_urls():
    """browser search：关键词返回非空结果，url 是真实目标（非 bing /ck/a 跳转）。"""
    _skip_if_unusable()
    out = await se.search_engine(_cfg(), "python asyncio", engine="bing-intl", limit=5, max_pages=2)
    assert out["engine"] == "bing-intl"
    assert out["results"], "search 返回空结果"
    # url 应是真实目标，不是 bing /ck/a 跳转包装
    assert all("/ck/a" not in r["url"] for r in out["results"]), (
        f"url 仍是 bing 跳转: {[r['url'] for r in out['results'][:2]]}"
    )
    for r in out["results"][:3]:
        assert r["url"].startswith("http"), r
        assert r["title"], r


async def test_fetch_passes_cf_and_returns_content():
    """browser fetch：grok.com（CF + SPA）应取回非空、非挑战页正文。"""
    _skip_if_unusable()
    url = "https://grok.com/release-notes"
    raw = await fetch_with_browser(_cfg(), [url], login=False, concurrency=1)
    md = raw.get(url)
    assert md, f"fetch 返回空: {url}"
    assert "just a moment" not in md.lower(), "内容仍含 CF 挑战特征，疑似被拦"
    assert "release" in md.lower() or "grok" in md.lower(), f"正文不含预期关键词: {url}"


async def test_fetch_timeout_aborts_within_budget():
    """短 timeout + 慢页面：fetch_with_browser 必在合理时间内返回，证明 finally 杀 PID + shutdown 清理生效。

    grok.com 有 CF 挑战，solve 至少数秒；timeout=2 必触发 per-future 超时。若 finally 的
    先杀 PID 再 shutdown 失效，残余 worker 会干等到 solve 自然结束（最坏 1-2 分钟），elapsed 会破阈值。
    """
    import time
    _skip_if_unusable()
    url = "https://grok.com/release-notes"
    start = time.monotonic()
    await fetch_with_browser(_cfg(), [url], login=False, concurrency=1, timeout=2)
    elapsed = time.monotonic() - start
    assert elapsed < 25, (
        f"fetch 用了 {elapsed:.1f}s，疑似 finally 杀 PID + shutdown 清理失效（残余 worker 干等）"
    )
