"""search 聚合命令单元测试：默认 providers 解析（含 browser 免配置兜底 → 开箱即用）。"""

from __future__ import annotations

from research_assistant.commands.search import _resolve_wanted


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
