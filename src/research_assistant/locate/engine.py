"""locate 包：锚点法速览预筛（PM §4.3 / ADR locate 非强制）。

锚点法：模型输出原文里实际存在的一句话（quote）+ 相关度；行号由代码字符串匹配算出
（模型擅长找关键句、代码擅长计数）。行号一律基于已落盘 md 副本（稳定性，基线 §7.4）。

流程：
    读 md → 切块（paragraph / lines 窗口）→ 高并发小模型批量打分（输出 quote+relevance XML）
    → 代码匹配 quote 到行号区间 → top N + 周边 K 行 context
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..providers import openai_compat


@dataclass
class Chunk:
    text: str
    line_start: int  # 1-based
    line_end: int  # 1-based


@dataclass
class Anchor:
    quote: str
    line_start: int
    line_end: int
    scope: str
    context: str
    relevance: float


def chunk_md(text: str, scope: str, window: int = 40) -> list[Chunk]:
    """把 md 文本切成块。scope=paragraph 按空行分段；scope=lines 按固定行窗口。"""
    lines = text.splitlines()
    if scope == "lines":
        chunks: list[Chunk] = []
        i = 0
        while i < len(lines):
            block = lines[i : i + window]
            if any(s.strip() for s in block):
                chunks.append(Chunk("\n".join(block), i + 1, i + len(block)))
            i += window
        return chunks
    # paragraph：按空行分段，记录行号
    chunks: list[Chunk] = []
    start = 0
    buf: list[str] = []
    for idx, line in enumerate(lines):
        if line.strip() == "":
            if any(s.strip() for s in buf):
                chunks.append(Chunk("\n".join(buf), start + 1, start + len(buf)))
            buf = []
            start = idx + 1
        else:
            if not buf:
                start = idx
            buf.append(line)
    if any(s.strip() for s in buf):
        chunks.append(Chunk("\n".join(buf), start + 1, start + len(buf)))
    return chunks


_PROMPT_SYSTEM = (
    "You locate the most relevant passage in a text chunk for a given question. "
    "If (and only if) the chunk contains content relevant to the question, output an XML <anchor> with:\n"
    "  <quote>: an EXACT, verbatim sentence or phrase COPIED from the chunk (do not paraphrase).\n"
    "  <relevance>: a float 0.0-1.0.\n"
    "If the chunk is irrelevant, output <anchors></anchors> with nothing inside. "
    "Output ONLY the XML, no prose."
)


def _build_user(query: str, chunk_text: str) -> str:
    return (
        f"Question: {query}\n\n"
        f"Chunk:\n\"\"\"\n{chunk_text}\n\"\"\"\n\n"
        f"Output the <anchors> XML now."
    )


_ANCHOR_RE = re.compile(r"<anchor>(.*?)</anchor>", re.DOTALL)
_QUOTE_RE = re.compile(r"<quote>(.*?)</quote>", re.DOTALL)
_REL_RE = re.compile(r"<relevance>(.*?)</relevance>", re.DOTALL)


def parse_anchors_xml(content: str) -> list[tuple[str, float]]:
    """从模型输出解析 (quote, relevance) 列表（容忍外层包裹的非 XML 文本）。"""
    results: list[tuple[str, float]] = []
    for m in _ANCHOR_RE.finditer(content):
        block = m.group(1)
        q = _QUOTE_RE.search(block)
        r = _REL_RE.search(block)
        if not q:
            continue
        quote = _decode_xml_text(q.group(1)).strip()
        if not quote:
            continue
        rel = 0.5
        if r:
            try:
                rel = float(_decode_xml_text(r.group(1)).strip())
            except ValueError:
                rel = 0.5
        results.append((quote, max(0.0, min(1.0, rel))))
    # 兜底：用 ElementTree 再试一次（模型偶尔回合法 XML）
    if not results:
        try:
            root = ET.fromstring(content.strip())
            for a in root.iter("anchor"):
                q = a.findtext("quote")
                r = a.findtext("relevance")
                if q and q.strip():
                    try:
                        rel = float(r) if r else 0.5
                    except ValueError:
                        rel = 0.5
                    results.append((q.strip(), max(0.0, min(1.0, rel))))
        except ET.ParseError:
            pass
    return results


def _decode_xml_text(s: str) -> str:
    return (
        s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def find_quote_lines(full_text: str, quote: str) -> tuple[int, int] | None:
    """在全文中定位 quote 的行号区间（1-based）。归一化空白后做子串匹配。

    模型可能微调标点/空白，故采用归一化匹配；找不到返回 None。
    """
    norm_quote = _normalize_ws(quote)
    if not norm_quote:
        return None
    # 取 quote 的特征前缀/后缀（避免模型夹带多余字）
    lines = full_text.splitlines()
    norm_lines = [_normalize_ws(l) for l in lines]
    joined = "\n".join(norm_lines)

    # 直接整体匹配
    pos = _fuzzy_find(joined, norm_quote)
    if pos is None:
        # 退化为首句匹配（取 quote 第一个句子）
        first_sentence = re.split(r"(?<=[。.!？?])\s", norm_quote)
        probe = first_sentence[0] if first_sentence else norm_quote[:60]
        if len(probe) < 8:
            return None
        pos = _fuzzy_find(joined, probe)
        if pos is None:
            return None
    # 把字符偏移映射回行号（基于 joined 的换行结构）
    return _offset_to_lines(joined, pos, norm_quote)


def _fuzzy_find(haystack: str, needle: str) -> int | None:
    """在归一化文本里找 needle；先精确，失败则用最长 token 序列近似。"""
    idx = haystack.find(needle)
    if idx >= 0:
        return idx
    # 容忍 needle 中间多了少量字符：取首尾各 ~20 字符做定位
    if len(needle) > 40:
        head, tail = needle[:20], needle[-20:]
        hi = haystack.find(head)
        if hi >= 0:
            tj = haystack.find(tail, hi)
            if tj >= 0:
                return hi
    return None


def _offset_to_lines(joined: str, pos: int, needle: str) -> tuple[int, int]:
    """joined 是 '\\n'.join(norm_lines)；把 [pos, pos+len) 映射到 1-based 行号。"""
    # 计算每行起始偏移
    line_starts = [0]
    for i, ch in enumerate(joined):
        if ch == "\n":
            line_starts.append(i + 1)
    end = min(pos + len(needle), len(joined))
    start_line = _bisect_line(line_starts, pos)
    end_line = _bisect_line(line_starts, max(pos, end - 1))
    return start_line + 1, end_line + 1


def _bisect_line(line_starts: list[int], offset: int) -> int:
    # 最大 i 使 line_starts[i] <= offset
    import bisect

    return bisect.bisect_right(line_starts, offset) - 1


def build_context(lines: list[str], line_start: int, line_end: int, k: int) -> str:
    """取 [line_start, line_end] 周围各 K 行的上下文文本。"""
    lo = max(1, line_start - k)
    hi = min(len(lines), line_end + k)
    return "\n".join(lines[lo - 1 : hi])


async def locate(
    config: Config,
    md_text: str,
    query: str,
    *,
    scope: str = "paragraph",
    top: int = 5,
    context_k: int = 3,
    concurrency: int | None = None,
) -> list[Anchor]:
    """主入口：对 md_text 跑锚点定位，返回 top N anchors（按 relevance 降序）。"""
    if concurrency is None:
        loc_cfg = config.provider("locate")
        concurrency = loc_cfg.concurrency if loc_cfg else 8

    chunks = chunk_md(md_text, scope)
    if not chunks:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def score_one(chunk: Chunk) -> list[tuple[str, float, Chunk]]:
        async with sem:
            resp = await openai_compat.chat_completion(
                config,
                [
                    {"role": "system", "content": _PROMPT_SYSTEM},
                    {"role": "user", "content": _build_user(query, chunk.text)},
                ],
                provider_type="locate",
                temperature=0.0,
            )
            msg = openai_compat.extract_message(resp)
            content = openai_compat.strip_think(msg.get("content") or "")
            if isinstance(content, list):
                content = "\n".join(seg.get("text", "") for seg in content if isinstance(seg, dict))
            parsed = parse_anchors_xml(content)
            return [(q, r, chunk) for q, r in parsed]

    batch_results = await asyncio.gather(*[score_one(c) for c in chunks], return_exceptions=True)

    lines = md_text.splitlines()
    anchors: list[Anchor] = []
    for res in batch_results:
        if isinstance(res, Exception):
            continue
        for quote, rel, chunk in res:
            span = find_quote_lines(md_text, quote)
            if span is None:
                # 退回 chunk 自身范围（行号稳定性优先用全文匹配，失败才回退）
                ls, le = chunk.line_start, chunk.line_end
            else:
                ls, le = span
            anchors.append(
                Anchor(
                    quote=quote,
                    line_start=ls,
                    line_end=le,
                    scope=scope,
                    context=build_context(lines, ls, le, context_k),
                    relevance=rel,
                )
            )

    # 去重（相同行区间）+ 排序 + top N
    seen: set[tuple[int, int]] = set()
    unique: list[Anchor] = []
    for a in sorted(anchors, key=lambda x: x.relevance, reverse=True):
        key = (a.line_start, a.line_end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique[:top]
