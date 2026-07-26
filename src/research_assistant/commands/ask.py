"""ask 命令：自然语言问一个自带 web 搜索的 LLM，拿它的 markdown 回答 + 抽出的信源。

    research-assistant ask "<natural-language question>"

和 search（聚合 exa/tavily 搜索 API）的区别：ask 走 openai_compat（你配的、model 自带
web 搜索的 LLM）。你发自然语言意图，LLM 理解后自己搜、自己组织答案；返回 LLM 的
markdown 回答（引用链接嵌在正文里）+ 抽出的 citations 列表。适合主 agent 遇到小问题、
不想自己调一堆工具、直接发自然语言拿结果的轻量场景。

model 必须自带 web 搜索（如 grok 类）：CLI 只发普通 chat 请求，搜索由 model 自身能力
完成；若 model 不会搜，content 里不会有真实信源、citations 为空。

响应：{content(str, markdown), citations[]{url, title?, snippet?}}
"""

from __future__ import annotations

import argparse
from typing import Any

from ..config import Config
from ..providers import openai_compat

NAME = "ask"
ALIASES: list[str] = []
HELP = "Ask a web-enabled LLM in natural language; returns its markdown answer plus sources."

_ASK_SYSTEM_PROMPT = (
    "You are a web research assistant. Search the web to answer the user's question. "
    "Answer in clear markdown and cite sources inline as links. "
    "Prefer official documentation and reputable sites."
)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("query", help="Natural-language question.")
    p.add_argument("--system", help="Override the system prompt.")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": args.system or _ASK_SYSTEM_PROMPT},
        {"role": "user", "content": args.query},
    ]
    response = await openai_compat.chat_completion(config, messages, temperature=0.0)
    msg = openai_compat.extract_message(response)
    content = openai_compat.strip_think(msg.get("content") or "")
    citations = openai_compat.extract_citations(response, msg)
    out: dict[str, Any] = {"content": content}
    if citations:
        out["citations"] = citations
    return out
