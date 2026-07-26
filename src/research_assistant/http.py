"""共享 httpx 客户端工厂（横切复用，ADR-0011）。

各 provider 自写 HTTP（ADR-0010），但共用：
    - httpx.AsyncClient 实例（连接池复用）
    - 代理注入（ADR-0007）
    - 超时（按 provider config）
    - 统一 User-Agent
    - 统一的 HTTP 错误 → ResearchAssistantError 映射
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import __version__
from .errors import NetworkError, ProviderError, ResearchAssistantError

USER_AGENT = f"research-assistant/{__version__}"


def make_client(
    *,
    proxy_url: str = "",
    timeout: float = 30.0,
    base_url: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """构造一个注入了代理/超时/UA 的 AsyncClient。调用方负责 close（或用上下文）。"""
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=min(15.0, timeout)),
        "headers": {"User-Agent": USER_AGENT, **(headers or {})},
        "follow_redirects": True,
        # 代理由 resolve_proxy（ADR-0007）统一解析后显式传入；禁止 httpx 再二次读 env 代理变量，
        # 否则 trust_env 默认会把 HTTPS_PROXY 叠加（导致 --proxy none 仍走系统代理）。
        "trust_env": False,
    }
    if base_url:
        client_kwargs["base_url"] = base_url.rstrip("/") + "/"
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**client_kwargs)


def handle_http_error(provider: str, exc: httpx.HTTPStatusError, *, extra_body_text: str | None = None) -> ResearchAssistantError:
    """把 httpx HTTP 状态错误映射为 ProviderError（带 details）。"""
    status = exc.response.status_code
    body_text = extra_body_text if extra_body_text is not None else ""
    if not body_text:
        try:
            body_text = exc.response.text[:1000]
        except Exception:
            pass
    detail: dict[str, Any] = {"status_code": status}

    # 尝试解析 provider 的 JSON error 体，抽取更友好的信息
    # 覆盖常见形态：{"error":"str"} / {"error":{"message":..}} / {"message":..} / {"detail":..}
    provider_msg = ""
    if body_text and body_text.lstrip().startswith("{"):
        try:
            j = json.loads(body_text)
            err_obj = j.get("error") if isinstance(j, dict) else None
            if isinstance(err_obj, dict):
                provider_msg = str(err_obj.get("message") or err_obj.get("type") or "")
                if err_obj.get("code"):
                    detail["provider_code"] = err_obj.get("code")
            elif isinstance(err_obj, str) and err_obj.strip():
                provider_msg = err_obj.strip()
            elif isinstance(j, dict):
                provider_msg = str(j.get("message") or j.get("detail") or "")
        except (ValueError, json.JSONDecodeError):
            pass
    elif body_text:
        detail["body"] = body_text

    # 常见归类
    if status in (401, 403):
        msg = f"{provider}: 鉴权失败（{status}），请检查 API key"
    elif status == 429:
        msg = f"{provider}: 请求被限流（429），稍后重试"
    elif 400 <= status < 500:
        msg = f"{provider}: 请求被拒绝（{status}）"
    elif 500 <= status < 600:
        msg = f"{provider}: 服务端错误（{status}）"
    else:
        msg = f"{provider}: 非 2xx 响应（{status}）"
    if provider_msg:
        msg = f"{msg} — {provider_msg}"
    return ProviderError(msg, provider=provider, details=detail)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """发起请求并返回解析后的 JSON；统一错误映射。"""
    try:
        resp = await client.request(method, url, json=json_body, params=params, headers=headers)
    except httpx.HTTPError as e:
        from .errors import wrap_provider_http_error

        raise wrap_provider_http_error(provider, e) from e
    if resp.status_code >= 400:
        raise handle_http_error(provider, httpx.HTTPStatusError(
            f"{provider} HTTP {resp.status_code}", request=resp.request, response=resp
        ))
    try:
        return resp.json()
    except ValueError as e:
        raise NetworkError(f"{provider}: 响应非 JSON ({e})", provider=provider) from e
