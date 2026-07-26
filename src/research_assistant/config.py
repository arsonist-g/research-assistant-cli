"""配置存储（TOML，~/.research-assistant/config.toml，D3）。

schema 见 backend-design data-model.md §2.1：
    schema_version = 1
    [[provider]]     # N 个，按 type 区分
    type / base_url / api_key / model / timeout / concurrency(仅 locate)
    [proxy]          url（空=自动检测，显式=覆盖，ADR-0007）
    [browser]        channel / extension_status / daemon_port / profile_strategy

读写：tomllib 读（3.11+ 内置，3.10 回退 tomli）；tomli_w 写（setup 持久化）。
Key 同时支持环境变量覆盖（RA_<TYPE>_API_KEY 等），env 优先于 config 文件。
真实 key 绝不进源码——只从此文件或环境读取（§10.4 安全红线）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# tomllib 3.11+ 内置，3.10 用 tomli
if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover - 3.10 分支
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

import tomli_w

from .errors import ConfigError

SCHEMA_VERSION = 1
CONFIG_DIR_ENV = "RESEARCH_ASSISTANT_HOME"
DEFAULT_CONFIG_DIR = Path.home() / ".research-assistant"


def config_dir() -> Path:
    """配置根目录：env 覆盖 → 默认 ~/.research-assistant。"""
    env = os.getenv(CONFIG_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_DIR


def config_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.toml"


def profiles_dir() -> Path:
    """per-call 临时浏览器 profile 根目录（ADR-0005）。"""
    return config_dir() / ".profiles"


def logs_dir() -> Path:
    return config_dir() / ".logs"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    """单个 provider 的连接配置。"""

    type: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 30
    concurrency: int = 8  # 仅 locate 用

    def merged_env(self) -> "ProviderConfig":
        """返回被环境变量覆盖后的副本（env 优先）。"""
        prefix = f"RA_{self.type.upper()}"
        out = ProviderConfig(
            type=self.type,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
            concurrency=self.concurrency,
        )
        env_base = os.getenv(f"{prefix}_BASE_URL")
        env_key = os.getenv(f"{prefix}_API_KEY")
        env_model = os.getenv(f"{prefix}_MODEL")
        env_timeout = os.getenv(f"{prefix}_TIMEOUT")
        env_concurrency = os.getenv(f"{prefix}_CONCURRENCY")
        if env_base:
            out.base_url = env_base
        if env_key:
            out.api_key = env_key
        if env_model:
            out.model = env_model
        if env_timeout:
            try:
                out.timeout = int(env_timeout)
            except ValueError:
                pass
        if env_concurrency:
            try:
                out.concurrency = int(env_concurrency)
            except ValueError:
                pass
        return out


@dataclass
class ProxyConfig:
    url: str = ""  # 空 = 自动检测系统代理（ADR-0007）


@dataclass
class BrowserConfig:
    channel: str = "msedge"  # msedge | chrome
    extension_status: str = "missing"  # installed | missing
    daemon_port: int = 17890  # cookie daemon HTTP 端口（WS 同端口，ADR-0006）
    profile_strategy: str = "per_call_temp"  # ADR-0005
    max_browser_instances: int = 3  # 并发浏览器进程上限（跨 CLI 限流，防无限开）


@dataclass
class Config:
    schema_version: int = SCHEMA_VERSION
    providers: list[ProviderConfig] = field(default_factory=list)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    path: str = ""

    # ---- provider 查询 ----
    def provider(self, ptype: str) -> ProviderConfig | None:
        """按 type 取 provider（环境变量覆盖后）。未配置返回 None。"""
        for p in self.providers:
            if p.type == ptype:
                return p.merged_env()
        # config 中无、但环境变量齐全时也能用（允许纯 env 配置）
        env_prefix = f"RA_{ptype.upper()}_"
        if any(k.startswith(env_prefix) for k in os.environ):
            return ProviderConfig(type=ptype).merged_env()
        return None

    def require_provider(self, ptype: str) -> ProviderConfig:
        p = self.provider(ptype)
        if p is None:
            raise ConfigError(
                f"未配置 provider: {ptype}",
                provider=ptype,
                details={"hint": "运行 `research-assistant setup` 或在 config.toml 添加 [[provider]]"},
            )
        return p


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件解析失败: {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"无法读取配置文件: {path}: {e}") from e


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。文件不存在返回空 Config（首次使用）。"""
    p = config_path(path)
    raw = _load_raw(p)
    cfg = Config(schema_version=int(raw.get("schema_version", SCHEMA_VERSION)), path=str(p))

    for item in raw.get("provider", []):
        if not isinstance(item, dict) or "type" not in item:
            continue
        cfg.providers.append(
            ProviderConfig(
                type=str(item["type"]),
                base_url=str(item.get("base_url", "")),
                api_key=str(item.get("api_key", "")),
                model=str(item.get("model", "")),
                timeout=int(item.get("timeout", 30)),
                concurrency=int(item.get("concurrency", 8)),
            )
        )

    proxy_raw = raw.get("proxy", {})
    if isinstance(proxy_raw, dict):
        cfg.proxy = ProxyConfig(url=str(proxy_raw.get("url", "")))

    browser_raw = raw.get("browser", {})
    if isinstance(browser_raw, dict):
        # env RA_BROWSER_MAX_INSTANCES 优先于 config 文件
        env_max = os.getenv("RA_BROWSER_MAX_INSTANCES")
        max_inst = (
            int(env_max)
            if env_max and env_max.isdigit()
            else int(browser_raw.get("max_browser_instances", 3))
        )
        cfg.browser = BrowserConfig(
            channel=str(browser_raw.get("channel", "msedge")),
            extension_status=str(browser_raw.get("extension_status", "missing")),
            daemon_port=int(browser_raw.get("daemon_port", 17890)),
            profile_strategy=str(browser_raw.get("profile_strategy", "per_call_temp")),
            max_browser_instances=max_inst,
        )

    return cfg


def save(cfg: Config, path: str | os.PathLike[str] | None = None) -> Path:
    """把 Config 持久化为 TOML（setup 用，幂等更新）。"""
    p = config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "schema_version": cfg.schema_version,
        "provider": [
            {
                "type": pc.type,
                "base_url": pc.base_url,
                "api_key": pc.api_key,
                "model": pc.model,
                "timeout": pc.timeout,
                **({"concurrency": pc.concurrency} if pc.type == "locate" else {}),
            }
            for pc in cfg.providers
        ],
        "proxy": {"url": cfg.proxy.url},
        "browser": {
            "channel": cfg.browser.channel,
            "extension_status": cfg.browser.extension_status,
            "daemon_port": cfg.browser.daemon_port,
            "profile_strategy": cfg.browser.profile_strategy,
            "max_browser_instances": cfg.browser.max_browser_instances,
        },
    }
    try:
        with p.open("wb") as f:
            f.write(tomli_w.dumps(data).encode("utf-8"))
    except OSError as e:
        raise ConfigError(f"无法写入配置文件: {p}: {e}") from e
    return p


def mask_secret(value: str) -> str:
    """遮蔽 key（doctor --show-config 用，§Security）。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
