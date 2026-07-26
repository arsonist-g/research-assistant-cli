"""locate 命令：锚点法速览预筛（PM §4.3）。

    research-assistant locate <md_path> <query> [--concurrency N] [--top N=5]
                              [--scope lines|paragraph] [--context K=3]

行号基于已落盘 md 副本（基线 §7.4 稳定性）。
响应：{md_path, query, anchors[]{quote, line_start, line_end, scope, context, relevance}}
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ArgsError
from ..locate import locate as locate_engine

NAME = "locate"
ALIASES: list[str] = []
HELP = "Anchor-based relevance scan of a local markdown file."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("md_path", help="Path to the local markdown file.")
    p.add_argument("query", help="Question / topic to locate.")
    p.add_argument("--concurrency", type=int, default=None, help="Override locate model concurrency.")
    p.add_argument("--top", type=int, default=5, help="Max anchors to return (default 5).")
    p.add_argument("--scope", choices=["lines", "paragraph"], default="paragraph", help="Chunking scope.")
    p.add_argument("--context", type=int, default=3, help="Context lines each side (default 3).")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    path = Path(args.md_path).expanduser()
    if not path.is_file():
        raise ArgsError(f"locate: 文件不存在: {path}")
    try:
        md_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ArgsError(f"locate: 读取失败: {e}") from e

    if not md_text.strip():
        return {"md_path": str(path), "query": args.query, "anchors": []}

    anchors = await locate_engine(
        config,
        md_text,
        args.query,
        scope=args.scope,
        top=args.top,
        context_k=args.context,
        concurrency=args.concurrency,
    )
    return {
        "md_path": str(path),
        "query": args.query,
        "anchors": [
            {
                "quote": a.quote,
                "line_start": a.line_start,
                "line_end": a.line_end,
                "scope": a.scope,
                "context": a.context,
                "relevance": a.relevance,
            }
            for a in anchors
        ],
    }
