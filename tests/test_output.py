"""output 渲染：JSON/markdown 分发、错误体、截断、列表渲染。"""

from __future__ import annotations

import json

from research_assistant import output
from research_assistant.output import _markdown_render, _truncate


class TestEmitJson:
    def test_emits_valid_json_with_newline(self, capsys):
        output.emit_json({"a": 1, "b": "中文"})
        out = capsys.readouterr().out
        assert out.endswith("\n")
        parsed = json.loads(out)
        assert parsed == {"a": 1, "b": "中文"}  # ensure_ascii=False 保留中文

    def test_emit_dispatches_by_format(self, capsys):
        output.emit({"x": 1}, "json")
        assert json.loads(capsys.readouterr().out) == {"x": 1}

    def test_emit_markdown_falls_through_for_unknown_format(self, capsys):
        # 非 json 格式一律走 markdown 渲染（即便传了未识别的值也不崩）
        output.emit({"x": 1}, "garbage")
        out = capsys.readouterr().out
        assert "x" in out and "1" in out


class TestEmitError:
    def test_writes_prefixed_message_to_stderr(self, capsys):
        output.emit_error("boom")
        err = capsys.readouterr().err
        assert err.startswith("error: ") and "boom" in err


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 120) == "hello"

    def test_long_text_truncated_with_ellipsis(self):
        s = "a" * 200
        out = _truncate(s, 50)
        assert out.endswith("...")
        assert len(out) == 50

    def test_collapses_whitespace_and_newlines(self):
        assert _truncate("a\nb\r  c", 120) == "a b c"

    def test_none_becomes_empty(self):
        assert _truncate(None, 120) == ""


class TestMarkdownRender:
    def test_error_body_renders_compact_line(self):
        line = _markdown_render({"error": {"code": "CONFIG", "message": "缺 key", "provider": "exa"}})
        assert "[ERROR]" in line
        assert "CONFIG" in line and "缺 key" in line and "exa" in line

    def test_empty_list_renders_placeholder(self):
        assert _markdown_render([]) == "_(空)_"

    def test_dict_with_results_renders_list_section(self):
        data = {"count": 2, "results": [{"title": "A", "url": "http://a"}, {"title": "B"}]}
        rendered = _markdown_render(data)
        assert "results" in rendered and "(2)" in rendered
        assert "A" in rendered and "B" in rendered

    def test_list_of_dicts_extracts_title_and_summary(self):
        rendered = _markdown_render([{"title": "Doc", "snippet": "a short snippet"}])
        assert "Doc" in rendered
        assert "a short snippet" in rendered

    def test_scalar_dict_renders_key_value(self):
        rendered = _markdown_render({"name": "researcher", "count": 3})
        assert "**name**" in rendered and "researcher" in rendered
        assert "**count**" in rendered and "3" in rendered

    def test_long_snippet_not_truncated(self):
        """markdown 渲染时 snippet 不截断（长度由源决定，搜索引擎/浏览器给多长就多长）。"""
        long_snippet = "x" * 250
        rendered = _markdown_render([{"title": "T", "snippet": long_snippet}])
        assert long_snippet in rendered  # 全文，未截断、无省略号
        assert "..." not in rendered
