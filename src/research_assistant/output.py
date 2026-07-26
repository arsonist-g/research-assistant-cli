"""输出渲染（横切约定 #5，api-contract.md §3/§4）。

stdout 默认 JSON（供脚本解析）；--output markdown 给人/AI 可读的 markdown。
错误：stdout 仍出 JSON `{error:{...}}`（供脚本解析），stderr 同时出一行人类可读提示。

格式而非受众：json 是结构化格式、markdown 是可读格式，二者对举；
不再用 "human" 这种指代受众的词（它其实就是 markdown）。
"""

from __future__ import annotations

import json
import sys
from typing import Any, IO


def _safe_write(stream: IO[str], text: str) -> None:
    """按流编码安全写出（Windows 控制台编码兜底）。"""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(text)


def emit_json(data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    _safe_write(sys.stdout, payload)
    if not payload.endswith("\n"):
        _safe_write(sys.stdout, "\n")


def emit_markdown(data: Any) -> None:
    _safe_write(sys.stdout, _markdown_render(data))
    _safe_write(sys.stdout, "\n")


def emit(data: Any, fmt: str) -> None:
    if fmt == "json":
        emit_json(data)
    else:
        emit_markdown(data)


def emit_error(message: str) -> None:
    """stderr 单行错误提示（plain text，不参与 --output 格式选择）。"""
    _safe_write(sys.stderr, f"error: {message}\n")


# ---------------------------------------------------------------------------
# markdown 渲染（标准 markdown：列表用 -、加粗 **、引用 >、斜体 _）
# ---------------------------------------------------------------------------


def _truncate(text: Any, limit: int = 120) -> str:
    s = "" if text is None else str(text)
    s = " ".join(s.replace("\r", " ").replace("\n", " ").split())
    if limit > 0 and len(s) > limit:
        return s[: max(0, limit - 3)] + "..."
    return s


def _markdown_render(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, dict):
        # 错误体：引用块形式
        if "error" in data and isinstance(data["error"], dict):
            err = data["error"]
            line = f"{pad}> **[ERROR] {err.get('code', 'INTERNAL')}**: {err.get('message', '')}"
            if err.get("provider"):
                line += f" _(provider: {err['provider']})_"
            return line
        # 结果列表（results / data / anchors 等）：先出元数据，再以列表渲染主体
        for list_key in ("results", "data", "anchors", "contents", "checks", "targets"):
            if list_key in data and isinstance(data[list_key], list):
                return _render_list_markdown(data, list_key, indent)
        # 普通 dict：每项一行 `- **key**: value`
        lines: list[str] = []
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}- **{k}**:")
                lines.append(_markdown_render(v, indent + 1))
            else:
                lines.append(f"{pad}- **{k}**: {_truncate(v, 200)}")
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return f"{pad}_(空)_"
        lines: list[str] = []
        for i, item in enumerate(data, 1):
            if isinstance(item, dict):
                title = item.get("title") or item.get("url") or item.get("id") or item.get("name") or ""
                extra = item.get("url") if item.get("url") != title else ""
                summary_keys = ("content", "text", "snippet", "description", "markdown")
                summary = ""
                for sk in summary_keys:
                    val = item.get(sk)
                    if val:
                        # 摘要/正文不截断：长度由源决定（搜索引擎/浏览器给多长就多长），
                        # 只合并空白（含换行）避免破坏 markdown，不丢内容
                        summary = " ".join(str(val).split())
                        break
                head = f"{pad}{i}. **{title}**" if title else f"{pad}{i}."
                if extra and extra != title:
                    head += f" — `{extra}`"
                lines.append(head)
                if summary:
                    lines.append(f"{pad}   {summary}")
                # 次要字段（子列表项）
                for k, v in item.items():
                    if k in ("title", "url", "id", "name", "content", "text", "snippet", "description", "markdown"):
                        continue
                    if isinstance(v, (dict, list)):
                        continue
                    if v in (None, "", 0, 0.0):
                        continue
                    lines.append(f"{pad}   - {k}: {_truncate(v, 80)}")
            else:
                lines.append(f"{pad}{i}. {item}")
        return "\n".join(lines)
    return f"{pad}{_truncate(data, 400)}"


def _render_list_markdown(data: dict[str, Any], list_key: str, indent: int) -> str:
    pad = "  " * indent
    lines: list[str] = []
    # 先渲染标量元数据字段
    for k, v in data.items():
        if k == list_key:
            continue
        if isinstance(v, (dict, list)):
            continue
        lines.append(f"{pad}- **{k}**: {_truncate(v, 160)}")
    items = data[list_key]
    if lines:
        lines.append("")
    lines.append(f"{pad}**{list_key}** ({len(items)}):")
    lines.append(_markdown_render(items, indent + 1))
    return "\n".join(lines)
