"""providers 注册表：自动发现、command/alias 索引、register 校验。"""

from __future__ import annotations

import pytest

from research_assistant.providers import registry
from research_assistant.providers.base import Capability, Provider


class TestDiscovery:
    def test_all_five_providers_discovered(self):
        classes = registry.all_provider_classes()
        # 加 provider = 1 文件，无需改 CLI；此处锁定 MVP 的 5 个
        assert {"exa", "tavily", "context7", "firecrawl", "openai_compat"} <= set(classes)

    def test_every_command_resolves_to_its_type(self):
        classes = registry.all_provider_classes()
        for ptype, cls in classes.items():
            if cls.command:
                assert registry.resolve_type(cls.command) == ptype

    def test_every_alias_resolves_to_its_type(self):
        classes = registry.all_provider_classes()
        for ptype, cls in classes.items():
            for alias in (cls.aliases or []):
                assert registry.resolve_type(alias) == ptype

    def test_known_alias(self):
        # exa 别名 x（来自 exa.py）
        assert registry.resolve_type("x") == "exa"

    def test_browser_platform_discovered(self):
        # browser 作为平台（与 exa/tavily/firecrawl 同级），由 registry 自动发现
        assert "browser" in registry.all_provider_classes()

    def test_browser_alias(self):
        assert registry.resolve_type("br") == "browser"

    def test_unknown_command_returns_none(self):
        assert registry.resolve_type("no-such-command") is None


class TestRegister:
    def test_register_without_type_raises(self):
        # 缺 type 属性 → ValueError，且不污染注册表（raise 在写入前）
        class NoType(Provider):
            type = ""
            command = "_should_not_register"

        with pytest.raises(ValueError):
            registry.register(NoType)
        assert "_should_not_register" not in registry._COMMAND_INDEX

    def test_register_indexes_command_and_alias(self):
        # 注册一个临时 provider，验证 command + alias 都建索引；用后清理避免污染其他用例
        class DemoProvider(Provider):
            type = "_test_demo"
            command = "_demo"
            aliases = ["_d"]

            def capabilities(self):
                return []

        try:
            returned = registry.register(DemoProvider)
            assert returned is DemoProvider  # 装饰器返回原类
            assert registry.resolve_type("_demo") == "_test_demo"
            assert registry.resolve_type("_d") == "_test_demo"
            assert registry.all_provider_classes()["_test_demo"] is DemoProvider
        finally:
            registry._REGISTRY.pop("_test_demo", None)
            registry._COMMAND_INDEX.pop("_demo", None)
            registry._COMMAND_INDEX.pop("_d", None)


class TestCapabilities:
    """各 provider 的 capability 声明回归：锁定每家工具的端点清单，防止端点被意外删减。"""

    @staticmethod
    def _caps(ptype: str) -> list[str]:
        cls = registry.all_provider_classes()[ptype]
        return [c.name for c in cls(config=None).capabilities()]

    def test_tavily_has_four_endpoints(self):
        assert self._caps("tavily") == ["search", "extract", "map", "crawl"]

    def test_exa_has_five_endpoints(self):
        assert self._caps("exa") == ["search", "similar", "contents", "answer", "research"]

    def test_firecrawl_has_nine_endpoints(self):
        assert self._caps("firecrawl") == [
            "scrape", "search", "map", "crawl", "extract",
            "interact", "agent", "monitor", "parse",
        ]

    def test_context7_has_two_endpoints(self):
        assert self._caps("context7") == ["library", "docs"]

    def test_openai_compat_has_chat(self):
        assert self._caps("openai_compat") == ["chat"]

    def test_browser_has_two_endpoints(self):
        assert self._caps("browser") == ["fetch", "search"]


class TestBrowserProvider:
    """browser provider（本地浏览器平台）的 capability 契约。"""

    @staticmethod
    def _cap(name: str) -> Capability:
        cls = registry.all_provider_classes()["browser"]
        for c in cls(config=None).capabilities():
            if c.name == name:
                return c
        raise AssertionError(f"browser 缺少 capability: {name}")

    def test_fetch_args(self):
        cap = self._cap("fetch")
        names = {a.name_or_flags[0] for a in cap.args}
        assert "urls" in names  # positional
        assert "--no-login" in names
        assert "--concurrency" in names
        assert "--write" in names

    def test_search_default_engine_is_bing_intl(self):
        cap = self._cap("search")
        engine = next(a for a in cap.args if a.name_or_flags == ["--engine"])
        assert engine.default == "bing-intl"
        assert engine.choices == ["bing-cn", "bing-intl", "google"]

    def test_search_has_query_and_limit(self):
        cap = self._cap("search")
        names = {a.name_or_flags[0] for a in cap.args}
        assert "query" in names
        assert "--limit" in names
