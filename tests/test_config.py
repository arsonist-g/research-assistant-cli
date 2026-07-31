"""config 模块：TOML 读写、provider 查询、env 覆盖、缺失文件、key 遮蔽。"""

from __future__ import annotations

from research_assistant import config
from research_assistant.config import Config, ProviderConfig
from research_assistant.errors import ConfigError


class TestLoad:
    def test_missing_file_returns_empty_config(self, home):
        cfg = config.load()
        assert cfg.schema_version == config.SCHEMA_VERSION
        assert cfg.providers == []
        assert cfg.proxy.url == ""
        assert cfg.browser.channel == "msedge"

    def test_loads_providers_proxy_browser(self, home):
        (home / "config.toml").write_text(
            """
schema_version = 1
[[provider]]
type = "exa"
base_url = "https://api.exa.ai"
api_key = "key-exa"
model = "exa-pro"

[[provider]]
type = "locate"
concurrency = 4

[proxy]
url = "http://127.0.0.1:7890"

[browser]
channel = "chrome"
daemon_port = 18000
""",
            encoding="utf-8",
        )
        cfg = config.load()
        assert len(cfg.providers) == 2
        exa = cfg.provider("exa")
        assert exa is not None
        assert exa.api_key == "key-exa"
        assert exa.base_url == "https://api.exa.ai"
        assert cfg.provider("locate").concurrency == 4
        assert cfg.proxy.url == "http://127.0.0.1:7890"
        assert cfg.browser.channel == "chrome"
        assert cfg.browser.daemon_port == 18000

    def test_invalid_toml_raises_config_error(self, home):
        (home / "config.toml").write_text("this is = = not toml", encoding="utf-8")
        import pytest

        with pytest.raises(ConfigError):
            config.load()


class TestEnvOverride:
    def test_env_overrides_config_values(self, home, monkeypatch):
        (home / "config.toml").write_text(
            '[undefined]\nx=1\n[[provider]]\ntype="exa"\napi_key="file-key"\nbase_url="http://file"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("RA_EXA_API_KEY", "env-key")
        monkeypatch.setenv("RA_EXA_TIMEOUT", "99")
        cfg = config.load()
        exa = cfg.provider("exa")
        assert exa.api_key == "env-key"  # env 胜过文件
        assert exa.timeout == 99
        assert exa.base_url == "http://file"  # 未设 env 的字段保留文件值

    def test_provider_from_env_only(self, home, monkeypatch):
        # config 文件里没有，但 env 齐全 → 也能取到（允许纯 env 配置）
        (home / "config.toml").write_text("schema_version = 1\n", encoding="utf-8")
        monkeypatch.setenv("RA_TAVILY_API_KEY", "env-only")
        cfg = config.load()
        tav = cfg.provider("tavily")
        assert tav is not None
        assert tav.api_key == "env-only"

    def test_bad_env_timeout_is_ignored(self, home, monkeypatch):
        (home / "config.toml").write_text(
            '[[provider]]\ntype="exa"\ntimeout=30\n', encoding="utf-8"
        )
        monkeypatch.setenv("RA_EXA_TIMEOUT", "not-a-number")
        cfg = config.load()
        assert cfg.provider("exa").timeout == 30  # 保持文件值


class TestRequireProvider:
    def test_returns_provider_when_configured(self, home):
        (home / "config.toml").write_text(
            '[[provider]]\ntype="exa"\napi_key="k"\n', encoding="utf-8"
        )
        cfg = config.load()
        assert cfg.require_provider("exa").api_key == "k"

    def test_raises_config_error_when_missing(self, home):
        import pytest

        cfg = config.load()
        with pytest.raises(ConfigError) as ei:
            cfg.require_provider("nope")
        assert ei.value.provider == "nope"


class TestSaveLoadRoundtrip:
    def test_roundtrip_preserves_providers(self, home):
        cfg = Config(
            providers=[
                ProviderConfig(type="exa", api_key="k", base_url="http://x", model="m"),
                ProviderConfig(type="locate", concurrency=16),
            ]
        )
        config.save(cfg)
        loaded = config.load()
        assert loaded.provider("exa").api_key == "k"
        assert loaded.provider("locate").concurrency == 16

    def test_locate_concurrency_serialized_others_not(self, home):
        # 仅 locate 写 concurrency 字段（schema 约定）
        cfg = Config(providers=[ProviderConfig(type="exa", concurrency=99)])
        config.save(cfg)
        text = (home / "config.toml").read_text(encoding="utf-8")
        assert "concurrency" not in text  # 非 locate 不写


class TestBrowserInstances:
    """max_browser_instances：默认 3，config 文件可配，env RA_BROWSER_MAX_INSTANCES 覆盖，save/load 回环。"""

    def test_default_is_3(self, home):
        cfg = config.load()
        assert cfg.browser.max_browser_instances == 3

    def test_loads_from_config(self, home):
        (home / "config.toml").write_text(
            "[browser]\nmax_browser_instances = 5\n", encoding="utf-8"
        )
        assert config.load().browser.max_browser_instances == 5

    def test_env_overrides_config(self, home, monkeypatch):
        (home / "config.toml").write_text(
            "[browser]\nmax_browser_instances = 5\n", encoding="utf-8"
        )
        monkeypatch.setenv("RA_BROWSER_MAX_INSTANCES", "7")
        assert config.load().browser.max_browser_instances == 7

    def test_env_used_when_config_absent(self, home, monkeypatch):
        monkeypatch.setenv("RA_BROWSER_MAX_INSTANCES", "9")
        assert config.load().browser.max_browser_instances == 9

    def test_roundtrip_preserves(self, home):
        from research_assistant.config import BrowserConfig

        cfg = Config(browser=BrowserConfig(max_browser_instances=6))
        config.save(cfg)
        assert config.load().browser.max_browser_instances == 6


class TestExecutablePath:
    """executable_path：默认空（按 channel 探测），config 文件可配，load/save 回环。"""

    def test_default_empty(self, home):
        assert config.load().browser.executable_path == ""

    def test_loads_from_config(self, home):
        (home / "config.toml").write_text(
            '[browser]\nexecutable_path = "C:/custom/msedge.exe"\n', encoding="utf-8"
        )
        assert config.load().browser.executable_path == "C:/custom/msedge.exe"

    def test_roundtrip_preserves(self, home):
        from research_assistant.config import BrowserConfig

        cfg = Config(browser=BrowserConfig(executable_path="/usr/local/bin/chrome"))
        config.save(cfg)
        assert config.load().browser.executable_path == "/usr/local/bin/chrome"


class TestMaskSecret:
    def test_empty(self):
        assert config.mask_secret("") == ""

    def test_short_fully_masked(self):
        assert config.mask_secret("abc") == "***"

    def test_long_keeps_prefix_and_suffix(self):
        masked = config.mask_secret("sk-abcdefghijklmnop1234567890")
        assert masked.startswith("sk-a")
        assert masked.endswith("7890")
        assert "*" in masked
