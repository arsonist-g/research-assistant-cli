"""search 聚合命令单元测试：默认 providers 解析、--timeout 越界校验、慢源超时跳过。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from research_assistant.commands.search import _resolve_wanted
from research_assistant.config import Config


def test_default_includes_browser_when_nothing_configured():
    """未配置任何 provider → 默认仍含 browser（本地浏览器免配置）→ 开箱即用。"""
    assert _resolve_wanted("", set()) == ["browser"]


def test_default_includes_configured_plus_browser():
    """配置了 exa → 默认 = exa + browser。"""
    assert _resolve_wanted("", {"exa"}) == ["exa", "browser"]


def test_default_excludes_unconfigured_exa_tavily():
    """未配置 tavily → 默认不含 tavily（需配置），但 browser 永远在。"""
    wanted = _resolve_wanted("", {"exa"})
    assert "tavily" not in wanted
    assert "browser" in wanted


def test_explicit_providers_overrides_default():
    """--providers 显式指定 → 用指定（可排除 browser）。"""
    assert _resolve_wanted("exa,tavily", {"exa"}) == ["exa", "tavily"]


def test_explicit_providers_can_be_browser_only():
    assert _resolve_wanted("browser", set()) == ["browser"]


async def test_run_skips_slow_provider(monkeypatch):
    """某 provider 慢于 --timeout → wait_for 超时，跳过该源，其余继续聚合（不阻塞）。"""
    from research_assistant.commands import search as search_cmd

    async def fake_from_provider(config, ptype, **kw):
        if ptype == "slow":
            await asyncio.sleep(5)  # 远超 timeout=1
            return [{"url": "slow"}]
        return [{"url": f"{ptype}-1"}]

    monkeypatch.setattr(search_cmd, "_from_provider", fake_from_provider)

    args = SimpleNamespace(
        query="q", providers="fast,slow", limit=5, timeout=1,
        include_domains=None, exclude_domains=None,
        start_date=None, end_date=None, text=False,
    )
    result = await search_cmd.run(args, Config())

    assert result["sources"] == ["fast"]  # slow 超时被跳过
    assert any(c["url"] == "fast-1" for c in result["candidates"])
    assert all(c["url"] != "slow" for c in result["candidates"])


def test_normalize_provider_results_tags_source():
    """_normalize_provider_results 给每条候选源打 source 标签（来自哪家 provider）。"""
    from research_assistant.commands.search import _normalize_provider_results

    result = {"results": [{"url": "http://a", "title": "A"}, {"url": "http://b"}]}
    out = _normalize_provider_results(result, limit=5, source="exa")
    assert [c["source"] for c in out] == ["exa", "exa"]
    assert out[0]["url"] == "http://a" and out[0]["title"] == "A"


async def test_run_preserves_candidate_source(monkeypatch):
    """run 聚合去重后，每条 candidate 保留 source 标签（_merge_unique 不丢来源）。"""
    from research_assistant.commands import search as search_cmd

    async def fake_from_provider(config, ptype, **kw):
        return [{"url": f"{ptype}-1", "source": ptype}]

    monkeypatch.setattr(search_cmd, "_from_provider", fake_from_provider)
    args = SimpleNamespace(
        query="q", providers="exa,browser", limit=5, timeout=10,
        include_domains=None, exclude_domains=None,
        start_date=None, end_date=None, text=False,
    )
    result = await search_cmd.run(args, Config())

    assert result["sources"] == ["exa", "browser"]
    assert all("source" in c for c in result["candidates"])
    assert {c["source"] for c in result["candidates"]} == {"exa", "browser"}
