"""OpenAI 兼容 provider（自写 HTTP，ADR-0010）。

既是 registry 中的 provider（命令 openai chat，供诊断/直调），也是 search/locate
共用的 LLM 主干（module-level chat_completion 函数）。

API（OpenAI Chat Completions 兼容）：
    POST {base_url}/chat/completions    鉴权 Authorization: Bearer <key>
        body: {model, messages[{role,content}], temperature, stream, ...}
        resp: {choices[{message{content}}], usage, ...}

search/locate 只消费信源字段（citations/URLs），不取综合结论（ADR-0009）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import Config, ProviderConfig
from ..errors import ArgsError, ProviderError
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def strip_think(text: str) -> str:
    """去掉 <think>...</think> 推理块（grok 等模型会带），避免污染下游解析。"""
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()



def resolve_cfg(config: Config, provider_type: str = "openai_compat") -> ProviderConfig:
    """取 openai 兼容配置。locate 复用时传 provider_type='locate'。"""
    cfg = config.require_provider(provider_type)
    if not cfg.api_key:
        raise ArgsError(f"{provider_type}: 缺少 api_key", provider=provider_type)
    # base_url 默认 OpenAI 官方（含 /v1/）；用 OpenAI 兼容渠道时 base_url 需自带 /v1/（如 https://host/v1）
    if not cfg.base_url:
        cfg.base_url = "https://api.openai.com/v1"
    if not cfg.model:
        raise ArgsError(f"{provider_type}: 缺少 model", provider=provider_type)
    return cfg


async def chat_completion(
    config: Config,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    timeout: float | None = None,
    response_format: dict[str, Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    provider_type: str = "openai_compat",
    stream: bool = True,
) -> dict[str, Any]:
    """调用 chat/completions，返回完整响应 dict（聚合后）。search/locate 复用。

    默认 stream=True：很多 OpenAI 兼容网关（尤其 grok 类）对慢模型仅流式可靠，
    流式首 token 早到可避开网关空闲超时。聚合后返回与非流式同构的 dict。
    """
    cfg = resolve_cfg(config, provider_type)
    body: dict[str, Any] = {
        "model": model or cfg.model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if response_format:
        body["response_format"] = response_format
    if extra_body:
        body.update(extra_body)

    async with http_mod.make_client(
        proxy_url=resolve_proxy(config.proxy.url),
        timeout=timeout or cfg.timeout,
        base_url=cfg.base_url,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
    ) as client:
        if stream:
            return await _stream_chat(client, body, provider_type, model or cfg.model)
        return await http_mod.request_json(
            client, "POST", "chat/completions", provider=provider_type, json_body=body
        )


async def _stream_chat(client: httpx.AsyncClient, body: dict[str, Any], provider_type: str, model: str) -> dict[str, Any]:
    """读取 SSE 流并聚合成非流式同构响应。"""
    try:
        async with client.stream("POST", "chat/completions", json=body) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", errors="replace")[:1000]
                raise http_mod.handle_http_error(
                    provider_type,
                    httpx.HTTPStatusError(f"{provider_type} HTTP {resp.status_code}", request=resp.request, response=resp),
                    extra_body_text=text,
                )
            content_parts: list[str] = []
            citations: list[Any] = []
            search_results: list[Any] = []
            usage: dict[str, Any] | None = None
            role = "assistant"
            raw_non_sse: list[str] = []  # 非 data: 行：网关可能 HTTP 200 却返回 body JSON error（非 SSE）
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    raw_non_sse.append(line)
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("role"):
                        role = delta["role"]
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    # 部分网关在 delta 里带 citations / search_results
                    for key, store in (("citations", citations), ("search_results", search_results)):
                        val = delta.get(key)
                        if isinstance(val, list):
                            store.extend(val)
                # 顶层或 usage
                for key, store in (("citations", citations), ("search_results", search_results)):
                    val = chunk.get(key)
                    if isinstance(val, list):
                        store.extend(val)
                if chunk.get("usage"):
                    usage = chunk.get("usage")
    except httpx.HTTPError as e:
        from ..errors import wrap_provider_http_error

        raise wrap_provider_http_error(provider_type, e) from e

    # 网关 HTTP 200 却没给 SSE chunk：body 可能是 JSON error 或 HTML 错误页（上游不可用等）。
    # 别静默返回空 content（否则用户以为 research-assistant 坏，实际是 model 上游）。
    if not content_parts and raw_non_sse:
        raw = "\n".join(raw_non_sse)
        msg: str | None = None
        try:
            err_body = json.loads(raw)
            if isinstance(err_body, dict) and err_body.get("error"):
                err = err_body["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
        except json.JSONDecodeError:
            pass
        if msg:
            raise ProviderError(f"{provider_type}: 上游返回错误 — {msg}", provider=provider_type)
        raise ProviderError(
            f"{provider_type}: 上游返回非 SSE 响应（HTTP 200，可能错误页/上游不可用）: {raw[:120]}",
            provider=provider_type,
        )

    message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if citations:
        message["citations"] = citations
    if search_results:
        message["search_results"] = search_results
    result: dict[str, Any] = {
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage:
        result["usage"] = usage
    return result


def extract_message(response: dict[str, Any]) -> dict[str, Any]:
    """从 chat 响应抽取 assistant message（content + 可能的 citations/search_results）。"""
    choices = response.get("choices") or []
    if not choices:
        raise ProviderError("openai_compat: 响应无 choices", provider="openai_compat")
    return choices[0].get("message") or {}


def extract_citations(response: dict[str, Any], message: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """从响应各处抽取信源（URL），用于 search（只消费不综合，ADR-0009）。

    覆盖各家返回方式：message.citations / message.search_results / 顶层 citations /
    content 中的 markdown 链接与裸 URL。
    """
    import re

    msg = message if message is not None else extract_message(response)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, title: str = "", snippet: str = "") -> None:
        url = _clean_url(url)
        if not url or url in seen:
            return
        seen.add(url)
        item: dict[str, Any] = {"url": url}
        if title:
            item["title"] = title
        if snippet:
            item["snippet"] = snippet
        candidates.append(item)

    # message.citations（Grok 风格：URL 字符串列表）
    for c in _as_list(msg.get("citations")):
        if isinstance(c, str):
            add(c)
        elif isinstance(c, dict):
            add(c.get("url"), c.get("title"), c.get("snippet") or c.get("content"))
    # message.search_results
    for c in _as_list(msg.get("search_results")):
        if isinstance(c, dict):
            add(c.get("url"), c.get("title"), c.get("content") or c.get("snippet"))
    # 顶层 citations / search_results
    for key in ("citations", "search_results"):
        for c in _as_list(response.get(key)):
            if isinstance(c, str):
                add(c)
            elif isinstance(c, dict):
                add(c.get("url"), c.get("title"), c.get("content") or c.get("snippet"))

    # content 中的 markdown 链接 [text](url) 与裸 URL（先剥离 <think> 推理块）
    content = strip_think(msg.get("content") or "")
    if isinstance(content, list):  # 部分 provider 返回 content blocks
        content = "\n".join(seg.get("text", "") for seg in content if isinstance(seg, dict))
    for text, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", content):
        add(url, text)
    for url in re.findall(r"(?<![\w/])(https?://[^\s)\]]+)", content):
        add(url)

    return candidates


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


_URL_TRAILING = re.compile(r"[)\].,;:!*_`>'\"<]+$")


def _clean_url(url: str) -> str:
    """清理抽取到的 URL：去首尾空白与尾部 markdown/标点（如 ** ) 等）。"""
    url = (url or "").strip()
    # 去掉包裹的成对引号/括号
    for left, right in (("(", ")"), ("[", "]"), ("<", ">"), ('"', '"')):
        if url.startswith(left) and url.endswith(right):
            url = url[1:-1].strip()
    # 反复去尾部标点（** 等）
    while True:
        m = _URL_TRAILING.search(url)
        if not m or m.start() == 0:
            break
        url = url[: m.start()]
    return url.strip()


@register
class OpenAICompatProvider(Provider):
    type = "openai_compat"
    command = "openai"
    aliases = ["oai"]
    help = "OpenAI-compatible chat completions (LLM backbone; raw diagnostic command)."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="chat",
                help="Raw chat completion (diagnostic / direct call).",
                args=[
                    ArgSpec(["prompt"], kind="positional", help="User prompt."),
                    ArgSpec(["--system"], help="Optional system prompt."),
                    ArgSpec(["--model"], help="Override model."),
                    ArgSpec(["--temperature"], type=float, default=0.0, help="Sampling temperature."),
                    ArgSpec(["--json"], action="store_true", help="Request JSON response format."),
                ],
                handler=self.chat,
            ),
        ]

    async def chat(self, args: Any) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if args.system:
            messages.append({"role": "system", "content": args.system})
        messages.append({"role": "user", "content": args.prompt})
        response_format = {"type": "json_object"} if args.json else None
        data = await chat_completion(
            self.config,
            messages,
            model=args.model,
            temperature=args.temperature,
            response_format=response_format,
        )
        msg = extract_message(data)
        out: dict[str, Any] = {
            "model": data.get("model") or args.model,
            "content": msg.get("content"),
        }
        citations = extract_citations(data, msg)
        if citations:
            out["citations"] = citations
        if data.get("usage"):
            out["usage"] = data.get("usage")
        return out
