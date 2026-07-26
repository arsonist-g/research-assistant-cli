"""HTML → markdown（仅 Playwright 回退路径用，D6）。

trafilatura with include_formatting=True 保留代码块/格式（基线 §10）。
"""

from __future__ import annotations

import re

_TRAFILATURA_AVAILABLE = True
try:
    import trafilatura  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _TRAFILATURA_AVAILABLE = False


def html_to_md(html: str, url: str = "") -> str:
    """把 HTML 转成 markdown。trafilatura 不可用或抽取失败时退化为 tag 剥离。"""
    if not html or not html.strip():
        return ""
    if _TRAFILATURA_AVAILABLE:
        try:
            md = trafilatura.extract(
                html,
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
    return _strip_html_fallback(html)


def _strip_html_fallback(html: str) -> str:
    """极简兜底：去掉标签、合并空白。"""
    html = re.sub(r"(?is)<script.*?</script>", "", html)
    html = re.sub(r"(?is)<style.*?</style>", "", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()
