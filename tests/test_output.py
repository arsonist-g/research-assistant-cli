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

    def test_list_grouped_by_source(self):
        """带 source 标签的列表 → 按 source 分组渲染；组标题带计数，编号跨组连续，组内不重复列 source。"""
        data = [
            {"url": "http://a", "title": "A", "source": "exa"},
            {"url": "http://b", "title": "B", "source": "browser"},
            {"url": "http://c", "title": "C", "source": "exa"},
        ]
        rendered = _markdown_render(data)
        # 组顺序 = source 首次出现顺序（exa 先于 browser）
        assert rendered.index("**exa**") < rendered.index("**browser**")
        assert "**exa** (2):" in rendered
        assert "**browser** (1):" in rendered
        # 编号跨组连续：exa A=1, C=2；browser B=3
        assert "1. **A**" in rendered
        assert "2. **C**" in rendered
        assert "3. **B**" in rendered
        # 分组后靠组标题说明来源，组内不再重复列 source 字段
        assert "- source:" not in rendered

    def test_list_without_source_not_grouped(self):
        """无 source 标签的普通列表不分组（向后兼容 fetch/locate 等命令的输出）。"""
        data = [{"title": "A", "url": "http://a"}, {"title": "B", "url": "http://b"}]
        rendered = _markdown_render(data)
        assert "**exa**" not in rendered and "**browser**" not in rendered
        assert "1. **A**" in rendered and "2. **B**" in rendered

    def test_list_item_name_as_title_id_as_extra(self):
        """name + id 同时存在（如 ctx7 library）：name 当标题（人读），id 作反引号 extra，两个都不丢。"""
        data = {"results": [{"id": "/foo/bar", "name": "Foo Bar", "description": "a lib"}]}
        rendered = _markdown_render(data)
        assert "**Foo Bar**" in rendered       # name 当标题
        assert "`/foo/bar`" in rendered        # id 作 extra（反引号），链式仍可见
        assert "a lib" in rendered             # description 仍作摘要

    def test_list_item_without_title_inlines_summary(self):
        """无 title/url/id 的项（如 ctx7 docs contents）：序号后直接接摘要，不留空标题行。"""
        data = {"contents": [{"text": "纯文本片段", "source_url": "https://src.com"}]}
        rendered = _markdown_render(data)
        assert "1. 纯文本片段" in rendered                  # 序号与摘要在同一行
        assert "- source_url: https://src.com" in rendered  # source_url 保留键名
