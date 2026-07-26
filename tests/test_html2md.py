"""html2md：空输入、trafilatura 路径、tag 剥离兜底。"""

from __future__ import annotations

from research_assistant.fetch import html2md


class TestHtmlToMd:
    def test_empty_input_returns_empty(self):
        assert html2md.html_to_md("") == ""
        assert html2md.html_to_md("   \n  ") == ""

    def test_strips_scripts_and_styles_in_fallback(self, monkeypatch):
        # 强制走 tag 剥离兜底路径
        monkeypatch.setattr(html2md, "_TRAFILATURA_AVAILABLE", False)
        html = """
        <html><body>
        <script>alert('x')</script>
        <style>.a{color:red}</style>
        <p>可见正文内容</p>
        </body></html>
        """
        md = html2md.html_to_md(html)
        assert "可见正文内容" in md
        assert "alert" not in md
        assert "color:red" not in md
        assert "<p>" not in md

    def test_fallback_collapses_whitespace(self, monkeypatch):
        monkeypatch.setattr(html2md, "_TRAFILATURA_AVAILABLE", False)
        md = html2md.html_to_md("<div>a\n\n  b   c</div>")
        assert "  " not in md  # 多空白被合并
        assert "a" in md and "b" in md and "c" in md

    def test_trafilatura_path_extracts_main_text(self):
        # 有正文结构的 HTML，trafilatura 应抽取出正文（不验证确切格式，只验证含目标文本）
        html = """
        <html><head><title>T</title></head><body>
        <article><p>This is the main article body about playwright stealth.</p></article>
        </body></html>
        """
        md = html2md.html_to_md(html)
        # trafilatura 成功则含正文；失败退化到 fallback 也含正文，两种都通过
        assert "main article body" in md.lower() or "playwright stealth" in md.lower()
