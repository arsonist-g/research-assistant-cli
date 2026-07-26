"""locate engine：分块、XML 解析、quote→行号映射、上下文窗口。纯逻辑，不打模型。"""

from __future__ import annotations

from research_assistant.locate import engine


class TestChunkMd:
    def test_paragraph_splits_on_blank_lines_with_line_numbers(self):
        text = "第一段第一行\n第一段第二行\n\n第二段"
        chunks = engine.chunk_md(text, "paragraph")
        assert len(chunks) == 2
        assert chunks[0].line_start == 1
        assert chunks[0].line_end == 2
        assert chunks[1].line_start == 4
        assert chunks[1].line_end == 4

    def test_paragraph_skips_pure_whitespace_blocks(self):
        text = "   \n\n实内容\n\n  \n"
        chunks = engine.chunk_md(text, "paragraph")
        assert len(chunks) == 1
        assert "实内容" in chunks[0].text

    def test_lines_scope_uses_fixed_window(self):
        text = "\n".join(f"line{i}" for i in range(10))
        chunks = engine.chunk_md(text, "lines", window=4)
        assert len(chunks) == 3  # 4+4+2
        assert chunks[0].line_start == 1 and chunks[0].line_end == 4
        assert chunks[2].line_start == 9 and chunks[2].line_end == 10

    def test_empty_text_yields_no_chunks(self):
        assert engine.chunk_md("", "paragraph") == []


class TestParseAnchorsXml:
    def test_parses_well_formed_xml(self):
        xml = '<anchors><anchor><quote>关键句</quote><relevance>0.9</relevance></anchor></anchors>'
        out = engine.parse_anchors_xml(xml)
        assert out == [("关键句", 0.9)]

    def test_parses_when_wrapped_in_prose(self):
        # 模型偶尔回带说明文字，正则仍能抽出 anchor
        content = 'Here is the result:\n<anchor><quote>目标</quote><relevance>0.8</relevance></anchor>\nDone.'
        out = engine.parse_anchors_xml(content)
        assert out == [("目标", 0.8)]

    def test_missing_relevance_defaults_to_half(self):
        xml = '<anchor><quote>无相关度</quote></anchor>'
        out = engine.parse_anchors_xml(xml)
        assert out == [("无相关度", 0.5)]

    def test_relevance_clamped_to_unit_range(self):
        xml = '<anchor><quote>溢出</quote><relevance>1.5</relevance></anchor>'
        assert engine.parse_anchors_xml(xml)[0][1] == 1.0
        xml2 = '<anchor><quote>负值</quote><relevance>-0.3</relevance></anchor>'
        assert engine.parse_anchors_xml(xml2)[0][1] == 0.0

    def test_multiple_anchors(self):
        xml = (
            '<anchor><quote>甲</quote><relevance>0.7</relevance></anchor>'
            '<anchor><quote>乙</quote><relevance>0.4</relevance></anchor>'
        )
        out = engine.parse_anchors_xml(xml)
        assert len(out) == 2 and out[0] == ("甲", 0.7)

    def test_empty_or_irrelevant_returns_empty(self):
        assert engine.parse_anchors_xml("<anchors></anchors>") == []
        assert engine.parse_anchors_xml("no xml here") == []

    def test_decodes_xml_entities_in_quote(self):
        xml = '<anchor><quote>a &lt; b &amp; c</quote><relevance>0.5</relevance></anchor>'
        assert engine.parse_anchors_xml(xml)[0][0] == "a < b & c"


class TestFindQuoteLines:
    def test_exact_match_returns_line_range(self):
        full = "intro\n目标句子在这里\nmore"
        ls, le = engine.find_quote_lines(full, "目标句子在这里")
        assert ls == 2 and le == 2

    def test_multiline_quote_not_supported_returns_none(self):
        # 设计边界：find_quote_lines 归一化空白后做单段匹配，行间换行 ≠ 空格，
        # 故跨行 quote 无法命中（模型 prompt 约束输出单句 quote，单行内可命中）。
        # locate() 主流程对此有兜底——span 为 None 时退回 chunk 自身范围。
        # 此用例锁定该已知行为；若未来支持跨行匹配，需同步更新。
        full = "a\n第一段内容\n第二段内容\nb"
        assert engine.find_quote_lines(full, "第一段内容\n第二段内容") is None

    def test_whitespace_normalized(self):
        # quote 多了空格，归一化后仍能匹配
        full = "the quick brown fox jumps"
        ls, le = engine.find_quote_lines(full, "the   quick   brown")
        assert ls == 1

    def test_not_found_returns_none(self):
        assert engine.find_quote_lines("aaa\nbbb", "不存在的句子zzz") is None

    def test_empty_quote_returns_none(self):
        assert engine.find_quote_lines("aaa", "") is None


class TestBuildContext:
    def test_returns_surrounding_k_lines(self):
        lines = ["l1", "l2", "l3", "l4", "l5", "l6"]
        ctx = engine.build_context(lines, line_start=3, line_end=3, k=1)
        assert ctx == "l2\nl3\nl4"

    def test_clamps_at_document_boundaries(self):
        lines = ["l1", "l2", "l3"]
        # 首行附近：lo 不会小于 1
        ctx = engine.build_context(lines, line_start=1, line_end=1, k=5)
        assert ctx == "l1\nl2\nl3"
        # 末行附近：hi 不会超过 len
        ctx2 = engine.build_context(lines, line_start=3, line_end=3, k=5)
        assert ctx2 == "l1\nl2\nl3"
