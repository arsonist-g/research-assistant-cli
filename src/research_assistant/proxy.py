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

# 本机回环地址：CDP / daemon 的 ws/http 必须直连，不能交给代理
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def ensure_local_no_proxy() -> str:
    """把本机回环地址补进 NO_PROXY，返回最终值。

    DrissionPage 用 websocket-client 连 `ws://127.0.0.1:<调试端口>`；websocket-client 默认读
    环境里的 http_proxy/HTTPS_PROXY。若代理变量残留（代理开着时启动、之后关掉），本机连接会被
    发给代理，表现为「浏览器未开启或已关闭」。保留用户原有 NO_PROXY 条目，只补回环地址。
    """
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for host in _LOOPBACK_HOSTS:
        if host not in parts:
            parts.append(host)
    value = ",".join(parts)
    # 某些库读小写、某些读大写；两个都写上，避免行为随库而异
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    return value


def set_override(url: str | None) -> None:
    """设置本次 CLI 调用的代理覆盖（--proxy flag，横切）。"""
    global _override
    _override = url.strip() if url else None


def forced_direct() -> bool:
    """CLI 是否显式要求直连（--proxy none）。浏览器需要据此加 --no-proxy-server：
    Chromium 在没有 --proxy-server 时会退回系统代理，「不传代理」不等于「直连」。"""
    return _override is not None and _override.lower() == "none"


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
