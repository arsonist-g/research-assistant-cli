"""代理横切（ADR-0007）：所有网络工具前置。

逻辑：
    config proxy.url 空（默认）→ 自动检测系统代理并带上
    config proxy.url 显式填 → 用配置代理覆盖自动检测

避免"填了代理端口但代理软件未开 → 工具走死代理不通"陷阱——默认零配置即可用。
"""

from __future__ import annotations

import os
from urllib.request import getproxies

# CLI --proxy 优先级最高，进程内缓存
_override: str | None = None


def set_override(url: str | None) -> None:
    """设置本次 CLI 调用的代理覆盖（--proxy flag，横切）。"""
    global _override
    _override = url.strip() if url else None


def detect_system_proxy() -> str:
    """自动检测系统代理：返回单个 proxy URL（优先 https）。

    覆盖 Windows/macOS/Linux 的环境变量（HTTP_PROXY/HTTPS_PROXY）与系统设置。
    检测不到返回空串。
    """
    # 环境变量优先（httpx/requests 同款约定）
    for env_key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = os.getenv(env_key)
        if val and val.strip():
            return val.strip()
    sys_proxies = getproxies() or {}
    # getproxies 在 Windows 返回 {'http': '...', 'https': '...'}（已是完整 URL）
    for key in ("https", "http"):
        val = sys_proxies.get(key)
        if val and val.strip():
            return val.strip()
    return ""


def resolve_proxy(config_proxy_url: str = "") -> str:
    """解析本次应使用的代理 URL（横切入口，所有网络工具调用）。

    优先级：CLI --proxy 覆盖 > config 显式 > 系统自动检测。
    "none" 为特殊值：强制直连（不走代理）——用于免 key 服务（如 firecrawl）经标记代理出口 IP 被拦时。
    返回空串表示不走代理（直连）。
    """
    if _override is not None:
        if _override.lower() == "none":
            return ""  # 强制直连
        return _override
    if config_proxy_url.strip():
        return config_proxy_url.strip()
    return detect_system_proxy()
