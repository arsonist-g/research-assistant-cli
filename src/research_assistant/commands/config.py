"""config 命令：配置相关的只读查询。

    research-assistant config fields   # 打印各 provider 配置字段表（只读，不连网，不动配置）

未来可扩展更多子命令（如 config show 打印当前值，与 doctor --show-config 区分）。
"""

from __future__ import annotations

import argparse
from typing import Any

from ..config import Config
from ._provider_meta import PROVIDER_META

NAME = "config"
ALIASES: list[str] = []
HELP = "Read-only configuration queries (provider field reference)."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, help=HELP, description=HELP)
    sub = p.add_subparsers(dest="subcommand", required=True, metavar="<fields>")
    f = sub.add_parser(
        "fields",
        help="Print each provider type's config fields (api_key/model required, default base_url, keyless tier, notes).",
    )
    f.set_defaults(_handler=run_fields)


async def run_fields(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    """各 provider 配置字段表（{provider_type: fields}）。只读：不读 config.toml，不连网。

    返回 dict-of-dict 而非 list，使 markdown 渲染走普通 dict 路径（每字段 200 字符宽），
    notes 不被 list 的 80 字符截断。
    """
    out: dict[str, Any] = {}
    for ptype, m in PROVIDER_META.items():
        out[m.type] = {
            "label": m.label,
            "api_key": m.api_key,
            "model": "required" if m.needs_model else "n/a",
            "default_base_url": m.default_base or "(set by user)",
            "keyless": m.keyless or "-",
            "notes": m.notes or "-",
        }
    return out
