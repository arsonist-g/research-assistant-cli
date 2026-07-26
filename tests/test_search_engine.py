"""search_engine 单元测试：URL 构造、结果抽取、翻页链接、去重（不联网，用 fake page）。

翻页循环（_run_engine_sync）依赖真实 DrissionPage，靠端到端实测；本文件只测可纯逻辑化的部分。
"""

from __future__ import annotations

import pytest

import research_assistant.fetch.search_engine as se


class FakeEl:
    """模拟 DrissionPage element/page：.text/.link/.attr/.url 与 .ele()/.eles()。"""

    def __init__(self, text="", link=None, href=None, url=None, children=None, items=None, aria_label=None):
        self.text = text
        self.link = link
        self._href = href
        self.url = url
        self._children = children or {}  # sel -> element
        self._items = items or {}  # sel -> list[element]
        self._aria_label = aria_label

    def ele(self, sel, timeout=None):
        return self._children.get(sel)

    def eles(self, sel, timeout=None):
        return self._items.get(sel, [])

    def attr(self, name):
        if name == "href":
            return self._href or self.link
        if name == "aria-label":
            return self._aria_label
        return None


def test_engine_url_bing_intl():
    assert se._engine_url("bing-intl", "python asyncio") == "https://www.bing.com/search?q=python+asyncio"


def test_engine_url_bing_cn():
    assert se._engine_url("bing-cn", "rust") == "https://cn.bing.com/search?q=rust"


def test_engine_url_encodes_special_chars():
    assert se._engine_url("bing-intl", "a&b=c") == "https://www.bing.com/search?q=a%26b%3Dc"


def test_engine_url_unknown_engine_raises():
    with pytest.raises(Exception):
        se._engine_url("duckduckgo", "x")


def test_extract_results_bing():
    items = [
        FakeEl(
            children={
                "css:h2 a": FakeEl(text="T1", link="https://a.com/1"),
                "css:.b_caption p": FakeEl(text="S1"),
            }
        ),
        FakeEl(
            children={
                "css:h2 a": FakeEl(text="T2", link="https://a.com/2"),
                "css:.b_caption p": FakeEl(text="S2"),
            }
        ),
    ]
    page = FakeEl(items={"css:li.b_algo": items})
    assert se._extract_results(page, "bing-intl") == [
        {"url": "https://a.com/1", "title": "T1", "snippet": "S1"},
        {"url": "https://a.com/2", "title": "T2", "snippet": "S2"},
    ]


def test_extract_results_skips_javascript_and_anchor_links():
    items = [
        FakeEl(children={"css:h2 a": FakeEl(text="T", link="javascript:void(0)")}),
        FakeEl(children={"css:h2 a": FakeEl(text="T2", link="#section")}),
    ]
    page = FakeEl(items={"css:li.b_algo": items})
    assert se._extract_results(page, "bing-intl") == []


def test_extract_results_missing_link_skipped():
    items = [FakeEl(children={"css:.b_caption p": FakeEl(text="no link here")})]
    page = FakeEl(items={"css:li.b_algo": items})
    assert se._extract_results(page, "bing-intl") == []


def test_extract_results_decodes_bing_ck_link():
    """bing 标题链接是 /ck/a 跳转 → 解码出完整真实 URL。"""
    items = [FakeEl(
        children={
            "css:h2 a": FakeEl(text="T1", link="https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly93d3cucnVub29iLmNvbS9weXRob24zL3B5dGhvbi1hc3luY2lvLmh0bWw&ntb=1"),
            "css:.b_caption p": FakeEl(text="S1"),
        }
    )]
    page = FakeEl(items={"css:li.b_algo": items})
    out = se._extract_results(page, "bing-intl")
    assert out[0]["url"] == "https://www.runoob.com/python3/python-asyncio.html"


def test_extract_results_bing_lineclamp2_snippet():
    """必应摘要选择器覆盖 p.b_lineclamp2（实测摘要在此元素，2026-07 补）。"""
    items = [FakeEl(
        children={
            "css:h2 a": FakeEl(text="T1", link="https://a.com/1"),
            "css:p.b_lineclamp2": FakeEl(text="摘要文本"),
        }
    )]
    page = FakeEl(items={"css:li.b_algo": items})
    out = se._extract_results(page, "bing-intl")
    assert out[0]["snippet"] == "摘要文本"


def test_extract_page_links_bing():
    """必应页数表：页码 a（文本数字）的相对 href 按当前页 url 解析为绝对；下一页（非数字）排除。"""
    pages = [
        FakeEl(text="2", href="/search?q=x&first=6"),
        FakeEl(text="3", href="/search?q=x&first=16"),
        FakeEl(text="下一页", href="/search?q=x&first=6"),  # 下一页，文本非数字 → 排除
    ]
    page = FakeEl(url="https://www.bing.com/search?q=x", items={"css:li.b_pag a[href]": pages})
    assert se._extract_page_links(page, "bing-intl") == [
        (2, "https://www.bing.com/search?q=x&first=6"),
        (3, "https://www.bing.com/search?q=x&first=16"),
    ]


def test_extract_page_links_google():
    """谷歌页数表：页码 a（aria-label=Page N）的 href；下一页（pnnext，无数字）排除。"""
    pages = [
        FakeEl(text="2", href="/search?q=x&start=10", aria_label="Page 2"),
        FakeEl(text="3", href="/search?q=x&start=20", aria_label="Page 3"),
        FakeEl(text="Next", href="/search?q=x&start=10"),  # pnnext，aria_label=None、文本非数字 → 排除
    ]
    page = FakeEl(url="https://www.google.com/search?q=x", items={"css:table.AaVjTc a[href]": pages})
    assert se._extract_page_links(page, "google") == [
        (2, "https://www.google.com/search?q=x&start=10"),
        (3, "https://www.google.com/search?q=x&start=20"),
    ]


def test_extract_page_links_empty_when_no_pagination():
    page = FakeEl(items={})
    assert se._extract_page_links(page, "bing-intl") == []


def test_parse_page_num_digit_text():
    assert se._parse_page_num(FakeEl(text="2"), "2") == 2


def test_parse_page_num_non_page_returns_none():
    assert se._parse_page_num(FakeEl(text="下一页"), "下一页") is None
    assert se._parse_page_num(FakeEl(text="Next"), "Next") is None


def test_decode_bing_url_extracts_real_url():
    # 真实 bing /ck/a 跳转：u= 参数 = 前缀 a1 + base64url(真实URL)
    ck = (
        "https://www.bing.com/ck/a?!&&p=x"
        "&u=a1aHR0cHM6Ly93d3cucnVub29iLmNvbS9weXRob24zL3B5dGhvbi1hc3luY2lvLmh0bWw&ntb=1"
    )
    assert se._decode_bing_url(ck) == "https://www.runoob.com/python3/python-asyncio.html"


def test_decode_bing_url_non_ck_returns_none():
    assert se._decode_bing_url("https://example.com/page") is None


def test_decode_bing_url_malformed_returns_none():
    assert se._decode_bing_url("https://www.bing.com/ck/a?u=not-valid-base64-@@@") is None
