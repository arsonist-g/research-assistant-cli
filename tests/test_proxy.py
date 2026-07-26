"""proxy 横切：覆盖优先级、none 哨兵、系统检测。"""

from __future__ import annotations

from research_assistant import proxy


class TestResolveProxyPriority:
    def test_cli_override_beats_config_and_detect(self, clean_env, monkeypatch):
        # config 显式 + 系统检测都有值，但 CLI override 优先
        monkeypatch.setattr(proxy, "detect_system_proxy", lambda: "http://sys:7890")
        proxy.set_override("http://cli:8080")
        assert proxy.resolve_proxy("http://config:1080") == "http://cli:8080"

    def test_config_beats_detect_when_no_override(self, clean_env, monkeypatch):
        monkeypatch.setattr(proxy, "detect_system_proxy", lambda: "http://sys:7890")
        proxy.set_override(None)
        assert proxy.resolve_proxy("http://config:1080") == "http://config:1080"

    def test_detect_used_when_no_override_no_config(self, clean_env, monkeypatch):
        monkeypatch.setattr(proxy, "detect_system_proxy", lambda: "http://sys:7890")
        assert proxy.resolve_proxy("") == "http://sys:7890"

    def test_empty_when_nothing_configured(self, clean_env, monkeypatch):
        monkeypatch.setattr(proxy, "detect_system_proxy", lambda: "")
        assert proxy.resolve_proxy("") == ""

    def test_none_sentinel_forces_direct_connection(self, clean_env, monkeypatch):
        # "none" 强制直连，即便 config 和检测都有代理
        monkeypatch.setattr(proxy, "detect_system_proxy", lambda: "http://sys:7890")
        proxy.set_override("none")
        assert proxy.resolve_proxy("http://config:1080") == ""


class TestSetOverride:
    def test_set_override_strips_whitespace(self):
        proxy.set_override("  http://x:1  ")
        assert proxy._override == "http://x:1"

    def test_set_override_none_clears(self):
        proxy.set_override("http://x:1")
        proxy.set_override(None)
        assert proxy._override is None

    def test_set_override_empty_string_clears(self):
        proxy.set_override("http://x:1")
        proxy.set_override("")
        assert proxy._override is None
