"""输出渲染（横切约定 #5，api-contract.md §3/§4）。

stdout 默认 markdown（对人/AI 友好、省 token）；--output json 出结构化 JSON（供脚本/jq 解析）。
错误：stdout 仍出 JSON `{error:{...}}`（错误体始终 JSON，便于程序判定），stderr 同时出一行人类可读提示。

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
        # 聚合搜索等场景：列表元素带 source 标签 → 按 source 分组渲染，让每条可见其来自哪家
        if all(isinstance(x, dict) and "source" in x for x in data):
            return _render_list_grouped_by_source(data, indent)
        lines: list[str] = []
        for i, item in enumerate(data, 1):
            lines.append(_render_list_item(item, i, indent))
        return "\n".join(lines)
    return f"{pad}{_truncate(data, 400)}"


# 列表项的标题/正文字段（渲染时单独处理，不当次要字段重复列出）
_LIST_ITEM_PRIMARY = ("title", "url", "id", "name", "content", "text", "snippet", "description", "markdown")


def _render_list_item(item: Any, i: int, indent: int, skip_keys: tuple[str, ...] = ()) -> str:
    """渲染列表中的单项：标题行 + 摘要 + 次要字段。

    skip_keys：额外跳过不渲染的字段（分组渲染时传 ("source",)，避免与组标题重复）。
    """
    pad = "  " * indent
    if not isinstance(item, dict):
        return f"{pad}{i}. {item}"
    # 标题优先级：title > name > url > id（name 比 url/id 更适合人读，如库名 vs 路径/网址）
    title = item.get("title") or item.get("name") or item.get("url") or item.get("id") or ""
    # 标题外的附加标识，反引号呈现（url 优先，其次 id；与 title 不同才显示）
    extra = ""
    for ek in ("url", "id"):
        ev = item.get(ek)
        if ev and ev != title:
            extra = ev
            break
    summary = ""
    for sk in ("content", "text", "snippet", "description", "markdown"):
        val = item.get(sk)
        if val:
            # 摘要/正文不截断：长度由源决定（搜索引擎/浏览器给多长就多长），
            # 只合并空白（含换行）避免破坏 markdown，不丢内容
            summary = " ".join(str(val).split())
            break
    if title:
        head = f"{pad}{i}. **{title}**"
        if extra:
            head += f" — `{extra}`"
        lines = [head]
        if summary:
            lines.append(f"{pad}   {summary}")
    else:
        # 无标题项（如纯文本片段列表）：序号后直接接摘要，避免空标题行
        lines = [f"{pad}{i}. {summary}".rstrip() if summary else f"{pad}{i}."]
    # 次要字段（子列表项）
    for k, v in item.items():
        if k in _LIST_ITEM_PRIMARY or k in skip_keys:
            continue
        if isinstance(v, (dict, list)):
            continue
        if v in (None, "", 0, 0.0):
            continue
        lines.append(f"{pad}   - {k}: {_truncate(v, 80)}")
    return "\n".join(lines)


def _render_list_grouped_by_source(items: list[Any], indent: int) -> str:
    """带 source 标签的列表按 source 分组渲染（聚合搜索：让候选源可见其来自哪家 provider）。

    保持 source 首次出现顺序；编号跨组连续，便于「第 N 条」全局引用。
    """
    pad = "  " * indent
    groups: dict[Any, list[Any]] = {}
    order: list[Any] = []
    for it in items:
        src = it.get("source")
        if src not in groups:
            groups[src] = []
            order.append(src)
        groups[src].append(it)
    lines: list[str] = []
    idx = 0
    for src in order:
        grp = groups[src]
        lines.append(f"{pad}**{src}** ({len(grp)}):")
        for it in grp:
            idx += 1
            lines.append(_render_list_item(it, idx, indent + 1, skip_keys=("source",)))
        lines.append("")  # 组间空行
    return "\n".join(lines).rstrip()


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
