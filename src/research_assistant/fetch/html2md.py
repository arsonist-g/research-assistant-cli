"""HTML → markdown 通用抽取（浏览器回退路径用，D6）。

**默认不做任何站点专属处理**：`html_to_md` 只负责 HTML → markdown，避免把某个站点的
特殊情况变成所有站点的行为。站点专属的预处理（例如删除反 AI 诱饵用的隐藏节点）由
`site_rules` 里的规则通过 `preprocess` 传入，只对命中的站点生效。

`strip_hidden_nodes` 是给规则调用的工具函数，不是默认步骤。
"""

from __future__ import annotations

import re
from typing import Callable

_TRAFILATURA_AVAILABLE = True
try:
    import trafilatura  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _TRAFILATURA_AVAILABLE = False

# 让文本「不可见」的内联样式。只认明确的隐藏写法，避免误删正常排版：
# - display:none / visibility:hidden：完全不渲染
# - font-size:0：文字宽度归零；数值必须**整值**为 0（`0.875rem` 是正常字号，不能命中）
# - left/top/text-indent 负到屏外（>=3 位数字，避免误伤 -1px 这类微调）
# - opacity:0 / color:transparent / clip / clip-path：视觉不可见
# 数值类条件统一要求后面是 `;` / `!` / `}` / 结尾，防 `\b` 在 `0` 与 `.` 之间误判。
_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|%)?(?=\s*(?:[;!}]|$))"
    r"|(?:left|top|text-indent)\s*:\s*-\d{3,}"
    r"|opacity\s*:\s*0(?:\.0+)?(?=\s*(?:[;!}]|$))"
    r"|color\s*:\s*transparent\b"
    r"|clip\s*:\s*rect\(\s*(?:0(?:px)?|1px)\s*[,) ]"
    r"|clip-path\s*:\s*inset\(\s*50%",
    re.IGNORECASE,
)

# 浏览器渲染后不会显示其内容的标签（noscript 在启用 JS 的页面里不渲染；template 不渲染）
_NON_RENDERED_TAGS = frozenset({"noscript", "template"})


def strip_hidden_nodes(html: str) -> str:
    """删除隐藏节点，返回清理后的 HTML。lxml 不可用或解析失败时原样返回。

    **由站点规则按需调用**（例如 linux.do 的 Discourse 规则），不要放进 `html_to_md` 的
    默认路径：别的站点可能靠隐藏节点承载标签页 / 折叠内容，全局删除会改变它们的结构。
    """
    if not html or not html.strip():
        return html
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(html)
    except Exception:
        return html

    drop: list = []
    try:
        for el in root.iter():
            if not isinstance(el.tag, str):  # 注释 / 处理指令
                continue
            tag = el.tag.lower()
            if tag in _NON_RENDERED_TAGS:
                drop.append(el)
                continue
            if el.get("hidden") is not None:
                drop.append(el)
                continue
            if (el.get("aria-hidden") or "").strip().lower() == "true":
                drop.append(el)
                continue
            style = el.get("style") or ""
            if style and _HIDDEN_STYLE_RE.search(style):
                drop.append(el)
    except Exception:
        return html

    if not drop:
        return html
    for el in drop:
        try:
            el.drop_tree()
        except Exception:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    try:
        return lxml_html.tostring(root, encoding="unicode")
    except Exception:
        return html


def html_to_md(html: str, url: str = "", preprocess: Callable[[str], str] | None = None) -> str:
    """把 HTML 转成 markdown。trafilatura 不可用或抽取失败时退化为 tag 剥离。

    preprocess 是站点规则提供的 HTML 预处理（如 `strip_hidden_nodes`）；不传则原样抽取。
    """
    if not html or not html.strip():
        return ""
    try:
        cleaned = preprocess(html) if preprocess is not None else html
    except Exception:
        cleaned = html  # 规则预处理失败不应让整页抓取失败
    if _TRAFILATURA_AVAILABLE:
        try:
            md = trafilatura.extract(
                cleaned,
                include_formatting=True,
                include_links=True,
                include_images=False,
                output_format="markdown",
                url=url,
            )
            if md and md.strip():
                return md.strip()
        except Exception:
            pass
    return _strip_html_fallback(cleaned)


def _strip_html_fallback(html: str) -> str:
    """极简兜底：去掉标签、合并空白。"""
    html = re.sub(r"(?is)<script.*?</script>", "", html)
    html = re.sub(r"(?is)<style.*?</style>", "", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()
