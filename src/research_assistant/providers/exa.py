"""Exa provider（自写 HTTP，ADR-0010）。

API 端点（webReader 查证 docs.exa.ai/reference + /sdks/python-sdk-specification，2026-07-26）：
    鉴权：所有端点统一用 HTTP header `x-api-key: <key>`（或 Authorization: Bearer）。
    POST /search         同步，智能搜索（auto 默认融合 neural + keyword）。
        body: query / numResults / type(keyword|neural|fast|auto) / category /
              includeDomains[] / excludeDomains[] /
              startPublishedDate / endPublishedDate /
              startCrawlDate / endCrawlDate / includeText[] / excludeText[] /
              userLocation / context / moderation /
              contents{text,highlights,livecrawl,subpageTarget,subpages}
        resp: {requestId, resolvedSearchType, results[]{title,url,id,score,text,
              highlights,publishedDate,author}, costDollars}
    POST /findSimilar    同步，按 URL 找语义相似页。
        body: {url, numResults, includeDomains[], excludeDomains[], contents{...}}
        resp: {results[]{url,title,id,score,publishedDate}}
    POST /contents       同步，按 id/URL 取正文/摘要/元数据。
        body: {ids[], text, highlights, summary, livecrawl}
        resp: {contents[]{id,url,title,text,highlights,summary}, statuses[]}
    POST /answer         同步（文档支持 SSE stream=True，本实现只走非流式）。
        body: {query, text?, stream?}
        resp: {answer, citations[]{id,url,title,author,publishedDate,text?}, costDollars}
    异步 Research（提交 + 轮询；SDK 默认 poll_interval=2s, max_wait=300s）：
        POST /research/v1            body: {instructions, model?(exa-research|exa-research-pro),
                                              output:{schema?}|{inferSchema?}}
                                     resp: {id}
        GET  /research/v1/{id}       resp: {id, status(pending|running|completed|failed|canceled),
                                              instructions, model, outputSchema?, data?, citations?}
        GET  /research/v1            列表（limit/cursor 游标分页）。

未验证项（无 key，未 runtime 实测）：
    - Research 路径 v0/v1 文档内部冲突：sidebar OpenAPI 标注三者均为 v1（本实现采纳），
      create-a-task 页 curl 示 /research/v0/tasks，get-a-task 页 curl 示 /research/v1/{id}。
      若 POST /research/v1 返回 404，可尝试改 path 为 research/v0/tasks。
    - Research create 响应字段：REST 文档示例 {id}，get 轮询响应字段用 researchId 或 id
      不一致；本实现读取时两者兼容（优先 id，回退 researchId）。

封装决策（CLI 表面取舍，非 API 限制）：
    - /answer 不暴露 stream=True：SSE 需独立流式输出路径，MVP 走非流式一次返回。
    - /search 不暴露 livecrawl/subpageTarget/subpages（contents 子选项）、context、moderation、
      userLocation、crawlDate：低频或仅在 --text 取正文时相关，按需再扩。
    - /research 不实现 GET /research/v1 列表：CLI 单用户场景用不上批量任务监控。

对齐 api-contract.md §2.1/§3 的 exa 命令与响应。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..config import ProviderConfig
from ..errors import ArgsError, ProviderError
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_BASE_URL = "https://api.exa.ai"

# Research 轮询终态（REST 文档枚举 5 态，canceled/cancelled 两种拼写都收）
RESEARCH_TERMINAL_STATES = ("completed", "failed", "cancelled", "canceled")


def _split_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


@register
class ExaProvider(Provider):
    type = "exa"
    command = "exa"
    aliases = ["x"]
    help = "Exa search / similar / contents / answer / research."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="search",
                help="Search the web with Exa (auto/keyword/neural/fast).",
                args=[
                    ArgSpec(["query"], kind="positional", help="Search query."),
                    ArgSpec(["--num-results"], type=int, default=10, help="Number of results (default 10)."),
                    ArgSpec(
                        ["--type"],
                        choices=["auto", "keyword", "neural", "fast"],
                        default="auto",
                        help="Search type (default auto).",
                    ),
                    ArgSpec(["--include-domains"], nargs="+", metavar="DOMAIN", help="Restrict to domains."),
                    ArgSpec(["--exclude-domains"], nargs="+", metavar="DOMAIN", help="Domains to drop from results."),
                    ArgSpec(
                        ["--category"],
                        help='Data category (e.g. "company", "research paper", "news", "pdf", "github", "tweet", "personal site", "linkedin profile", "financial report").',
                    ),
                    ArgSpec(["--start-date"], metavar="YYYY-MM-DD", help="Results published after this date."),
                    ArgSpec(["--end-date"], metavar="YYYY-MM-DD", help="Results published before this date."),
                    ArgSpec(
                        ["--include-text"],
                        nargs="+",
                        metavar="TEXT",
                        help="Strings that must appear in result text (API currently allows 1, up to 5 words).",
                    ),
                    ArgSpec(
                        ["--exclude-text"],
                        nargs="+",
                        metavar="TEXT",
                        help="Strings that must not appear in result text (first 1000 words checked).",
                    ),
                    ArgSpec(["--text"], action="store_true", help="Include page text in results."),
                    ArgSpec(["--highlights"], action="store_true", help="Include highlights in results."),
                ],
                handler=self.search,
            ),
            Capability(
                name="similar",
                help="Find pages similar to a URL.",
                args=[
                    ArgSpec(["url"], kind="positional", help="Source URL."),
                    ArgSpec(["--num-results"], type=int, default=10, help="Number of results."),
                ],
                handler=self.similar,
            ),
            Capability(
                name="contents",
                help="Fetch contents for Exa IDs (URLs).",
                args=[
                    ArgSpec(["ids"], kind="positional", nargs="+", metavar="ID", help="Exa IDs / URLs (space or comma separated)."),
                    ArgSpec(["--text"], action="store_true", default=True, help="Include text (default on)."),
                    ArgSpec(["--highlights"], action="store_true", help="Include highlights."),
                ],
                handler=self.contents,
            ),
            Capability(
                name="answer",
                help="LLM answer to a question, grounded in Exa search results.",
                args=[
                    ArgSpec(["query"], kind="positional", help="Question to answer."),
                    ArgSpec(["--text"], action="store_true", help="Include full citation text in results."),
                ],
                handler=self.answer,
            ),
            Capability(
                name="research",
                help="Async in-depth research; blocks until done or --poll-timeout (default 300s).",
                args=[
                    ArgSpec(["instructions"], kind="positional", help="Natural-language research instructions."),
                    ArgSpec(
                        ["--model"],
                        default="exa-research",
                        help='Research model: "exa-research" (default, adapts to task) or "exa-research-pro" (hardest tasks).',
                    ),
                    ArgSpec(["--output-schema"], metavar="JSON", help="JSON Schema string for structured output."),
                    ArgSpec(["--infer-schema"], action="store_true", help="Let Exa infer the output schema via LLM."),
                    ArgSpec(["--poll-timeout"], type=int, default=300, help="Seconds to wait for completion (default 300)."),
                    ArgSpec(["--async-submit"], action="store_true", help="Submit only; return the task id without polling."),
                ],
                handler=self.research,
            ),
        ]

    def _cfg(self) -> ProviderConfig:
        cfg = self.config.require_provider("exa")
        if not cfg.api_key:
            raise ArgsError("exa: 缺少 api_key", provider="exa")
        if not cfg.base_url:
            cfg.base_url = DEFAULT_BASE_URL
        return cfg

    async def search(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body: dict[str, Any] = {
            "query": args.query,
            "numResults": args.num_results,
            "type": args.type,
            "contents": {"text": bool(args.text), "highlights": bool(args.highlights)},
        }
        if args.include_domains:
            body["includeDomains"] = _split_csv(args.include_domains)
        if args.exclude_domains:
            body["excludeDomains"] = _split_csv(args.exclude_domains)
        if args.category:
            body["category"] = args.category
        if args.start_date:
            body["startPublishedDate"] = args.start_date
        if args.end_date:
            body["endPublishedDate"] = args.end_date
        if args.include_text:
            body["includeText"] = _split_csv(args.include_text)
        if args.exclude_text:
            body["excludeText"] = _split_csv(args.exclude_text)

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key},
        ) as client:
            data = await http_mod.request_json(client, "POST", "search", provider="exa", json_body=body)

        results = []
        for r in data.get("results", []) or []:
            item: dict[str, Any] = {"url": r.get("url"), "title": r.get("title")}
            if r.get("id"):
                item["id"] = r.get("id")
            if args.text and r.get("text"):
                item["text"] = r.get("text")
            if args.highlights and r.get("highlights"):
                item["highlights"] = r.get("highlights")
            if r.get("score") is not None:
                item["score"] = r.get("score")
            if r.get("publishedDate"):
                item["publishedDate"] = r.get("publishedDate")
            results.append(item)
        return {"results": results}

    async def similar(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body = {"url": args.url, "numResults": args.num_results}
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key},
        ) as client:
            data = await http_mod.request_json(client, "POST", "findSimilar", provider="exa", json_body=body)
        results = [{"url": r.get("url"), "title": r.get("title")} for r in data.get("results", []) or []]
        return {"results": results}

    async def contents(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        ids = _split_csv(args.ids)
        if not ids:
            raise ArgsError("exa contents: 至少提供一个 id", provider="exa")
        body: dict[str, Any] = {"ids": ids, "text": bool(args.text)}
        if args.highlights:
            body["highlights"] = True
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key},
        ) as client:
            data = await http_mod.request_json(client, "POST", "contents", provider="exa", json_body=body)
        contents = []
        for c in data.get("contents", []) or []:
            item: dict[str, Any] = {"id": c.get("id"), "url": c.get("url"), "text": c.get("text")}
            if c.get("highlights"):
                item["highlights"] = c.get("highlights")
            contents.append(item)
        return {"contents": contents}

    async def answer(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        # 非流式：不传 stream（默认 false），一次拿到完整 answer + citations。
        body: dict[str, Any] = {"query": args.query}
        if args.text:
            body["text"] = True
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key},
        ) as client:
            data = await http_mod.request_json(client, "POST", "answer", provider="exa", json_body=body)
        citations = []
        for c in data.get("citations", []) or []:
            item: dict[str, Any] = {"id": c.get("id"), "url": c.get("url"), "title": c.get("title")}
            if c.get("author"):
                item["author"] = c.get("author")
            if c.get("publishedDate"):
                item["publishedDate"] = c.get("publishedDate")
            if args.text and c.get("text"):
                item["text"] = c.get("text")
            citations.append(item)
        return {"answer": data.get("answer"), "citations": citations}

    async def _poll_research(self, client: Any, task_id: str, *, poll_timeout: int) -> dict[str, Any]:
        """轮询 research 任务直到终态（completed/failed/cancelled）或超时。"""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + poll_timeout
        while True:
            data = await http_mod.request_json(
                client, "GET", f"research/v1/{task_id}", provider="exa"
            )
            status = data.get("status")
            if status in RESEARCH_TERMINAL_STATES:
                return data
            if loop.time() >= deadline:
                data = dict(data)
                data["status"] = "timeout"
                return data
            await asyncio.sleep(2)

    async def research(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body: dict[str, Any] = {"instructions": args.instructions, "model": args.model}
        # output 二选一：--output-schema 优先；--infer-schema 次之；都不给则不传（默认 markdown 报告）。
        if args.output_schema:
            try:
                schema = json.loads(args.output_schema)
            except (ValueError, json.JSONDecodeError) as e:
                raise ArgsError(
                    f"exa research: --output-schema 不是合法 JSON ({e})",
                    provider="exa",
                ) from e
            body["output"] = {"schema": schema}
        elif args.infer_schema:
            body["output"] = {"inferSchema": True}

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"x-api-key": cfg.api_key},
        ) as client:
            submit = await http_mod.request_json(
                client, "POST", "research/v1", provider="exa", json_body=body
            )
            task_id = submit.get("id") or submit.get("researchId")
            if not task_id:
                raise ProviderError(
                    "exa research: 提交响应缺 id",
                    provider="exa", details=submit,
                )
            if args.async_submit:
                return {"id": task_id, "status": "submitted"}
            data = await self._poll_research(client, task_id, poll_timeout=args.poll_timeout)

        # 兼容 id / researchId 两种字段名（REST 文档示例不一致）
        out: dict[str, Any] = {
            "id": data.get("id") or data.get("researchId") or task_id,
            "status": data.get("status"),
        }
        if data.get("instructions"):
            out["instructions"] = data.get("instructions")
        if data.get("data") is not None:
            out["data"] = data.get("data")
        if data.get("citations") is not None:
            out["citations"] = data.get("citations")
        if data.get("status") != "completed":
            out["note"] = (
                "task not completed; partial or timeout. "
                "Resubmit with --async-submit and poll research/v1/<id>."
            )
        return out
