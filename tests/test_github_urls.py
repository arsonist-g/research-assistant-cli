"""github_urls：URL 识别判定表、行号片段、ref 候选顺序、切片裁剪（纯解析，无网络）。

覆盖准则：URL 形状 = 判定表（host 类别 x 段数 x 第 3 段类型 x 首段是否保留字）；
行号片段与 slice_lines = 边界值分析；candidates = 顺序断言（先短后长）。

oracle 标注（见各断言的注释）：
- specified：期望值直接取自 api-contract.md「Delta：GitHub provider（2026-09-14，DEC-029）」
  或 github_urls 模块 docstring 的显式口径。
- derived：由上述口径手工推导。
- implicit：契约未规定、按现状刻画（回归护栏），并已在缺陷报告里列为待确认问题。
"""

from __future__ import annotations

import pytest

from research_assistant.github_urls import (
    GithubRef,
    is_github_url,
    parse_github_url,
    slice_lines,
)

# 全部用合成占位（owner=o / repo=r），不含任何真实仓库或个人信息。


class TestRecognizedUrls:
    """认得出的形状：仓库页恰好两段；文件页 blob / raw / raw 主机（specified）。"""

    @pytest.mark.parametrize(
        "url, kind, owner, repo, rest",
        [
            ("https://github.com/o/r", "repo", "o", "r", ()),
            ("https://github.com/o/r/", "repo", "o", "r", ()),
            ("http://github.com/o/r", "repo", "o", "r", ()),
            ("https://github.com/o/r.git", "repo", "o", "r", ()),
            ("https://github.com/o/r.git/", "repo", "o", "r", ()),
            ("https://github.com/o/r/blob/main/a.py", "file", "o", "r", ("main", "a.py")),
            ("https://github.com/o/r/raw/main/a.py", "file", "o", "r", ("main", "a.py")),
            ("https://github.com/o/r/blob/v1.0.0/src/a.py", "file", "o", "r", ("v1.0.0", "src", "a.py")),
            ("https://github.com/o/r/blob/feature/foo/a.ts", "file", "o", "r", ("feature", "foo", "a.ts")),
            ("https://github.com/o/r.git/blob/main/a.py", "file", "o", "r", ("main", "a.py")),
            ("https://github.com/o/r/blob/main/README.md?plain=1", "file", "o", "r", ("main", "README.md")),
            ("https://raw.githubusercontent.com/o/r/main/a.py", "file", "o", "r", ("main", "a.py")),
            ("https://raw.githubusercontent.com/o/r/feature/foo/a.ts", "file", "o", "r", ("feature", "foo", "a.ts")),
            ("  https://github.com/o/r  ", "repo", "o", "r", ()),
        ],
    )
    def test_shape(self, url, kind, owner, repo, rest):
        ref = parse_github_url(url)
        assert ref is not None, f"{url} 应为可识别 URL"
        assert (ref.kind, ref.owner, ref.repo, ref.rest) == (kind, owner, repo, rest)

    @pytest.mark.parametrize(
        "url, raw, host",
        [
            # raw 主机：URL 本身即内容地址（data-model：raw bool）
            ("https://raw.githubusercontent.com/o/r/main/a.py", True, "raw.githubusercontent.com"),
            # github.com 的 /raw/ 形态仍是网页地址，需再推导 raw 通道
            ("https://github.com/o/r/raw/main/a.py", False, "github.com"),
            ("https://github.com/o/r/blob/main/a.py", False, "github.com"),
            ("https://github.com/o/r", False, "github.com"),
        ],
    )
    def test_raw_flag_and_host(self, url, raw, host):
        ref = parse_github_url(url)
        assert ref is not None
        assert (ref.raw, ref.host) == (raw, host)


class TestRejectedUrls:
    """认不出的形状一律 None（specified：落回原链路，不猜）。"""

    @pytest.mark.parametrize(
        "url",
        [
            # 目录页 / 非文件路由
            "https://github.com/o/r/tree/main",
            "https://github.com/o/r/tree/main/src",
            "https://github.com/o/r/issues",
            "https://github.com/o/r/issues/1",
            "https://github.com/o/r/pulls",
            "https://github.com/o/r/actions",
            "https://github.com/o/r/releases",
            "https://github.com/o/r/blame/main/a.py",
            "https://github.com/o/r/archive/refs/heads/main.zip",
            "https://github.com/o/r/commits/main",
            "https://github.com/o/r/settings",
            # blob/raw 但没有文件路径
            "https://github.com/o/r/blob/main",
            "https://github.com/o/r/blob",
            "https://github.com/o/r/raw",
            # 单段路径与保留顶层路由
            "https://github.com/o",
            "https://github.com/",
            "https://github.com/topics",
            "https://github.com/stars",
            "https://github.com/marketplace/actions/checkout",
            "https://github.com/orgs/foo",
            "https://github.com/organizations/foo/settings",
            "https://github.com/settings/profile",
            "https://github.com/search?q=github",
            # 别的 host / 别的 API
            "https://gist.github.com/o/abc123",
            "https://example.com/o/r/blob/main/a.py",
            "https://gitlab.com/o/r",
            # 非 http(s) scheme
            "ftp://github.com/o/r",
            "file://github.com/o/r",
            "mailto:someone@example.com",
            "javascript:alert(1)",
            # raw 主机但段数不足
            "https://raw.githubusercontent.com/o/r/main",
            "https://raw.githubusercontent.com/o/r",
            # 空输入
            "",
            None,
        ],
    )
    def test_rejected(self, url):
        assert parse_github_url(url) is None


class TestLineFragments:
    """#L10 / #L10-L20 / #L10-20 → 1-based 闭区间（specified）。"""

    @pytest.mark.parametrize(
        "url, start, end",
        [
            ("https://github.com/o/r/blob/main/a.py#L10", 10, 10),
            ("https://github.com/o/r/blob/main/a.py#L10-L20", 10, 20),
            ("https://github.com/o/r/blob/main/a.py#L10-20", 10, 20),
            ("https://github.com/o/r/blob/main/a.py#L1-L1", 1, 1),
            ("https://github.com/o/r/raw/main/a.py#L7", 7, 7),
            ("https://raw.githubusercontent.com/o/r/main/a.py#L10-L20", 10, 20),
            ("https://github.com/o/r/blob/main/a.py", None, None),
        ],
    )
    def test_fragment(self, url, start, end):
        ref = parse_github_url(url)
        assert ref is not None
        assert (ref.line_start, ref.line_end) == (start, end)


class TestRefHintsAndCandidates:
    """ref/path 猜法（首段）与「先短后长」的候选顺序（specified + derived）。"""

    @pytest.mark.parametrize(
        "url, ref_hint, path_hint",
        [
            ("https://github.com/o/r/blob/main/a.py", "main", "a.py"),
            ("https://github.com/o/r/blob/feature/foo/a.ts", "feature", "foo/a.ts"),
            ("https://github.com/o/r/blob/main/src/deep/a.py", "main", "src/deep/a.py"),
            ("https://github.com/o/r", "", ""),
        ],
    )
    def test_hints(self, url, ref_hint, path_hint):
        ref = parse_github_url(url)
        assert ref is not None
        assert (ref.ref_hint, ref.path_hint) == (ref_hint, path_hint)

    @pytest.mark.parametrize(
        "url, expected",
        [
            # 单候选：ref 无斜杠
            ("https://github.com/o/r/blob/main/a.py", [("main", "a.py")]),
            # ref 含斜杠：第 1 段优先（短），第 2 段兜底（长）
            (
                "https://github.com/o/r/blob/feature/foo/a.ts",
                [("feature", "foo/a.ts"), ("feature/foo", "a.ts")],
            ),
            (
                "https://github.com/o/r/blob/main/src/deep/a.py",
                [("main", "src/deep/a.py"), ("main/src", "deep/a.py"), ("main/src/deep", "a.py")],
            ),
            # raw 主机同样保留全部路径段（契约：raw.githubusercontent.com/<o>/<r>/<ref>/<path>）
            ("https://raw.githubusercontent.com/o/r/feature/foo/a.ts", [("feature", "foo/a.ts"), ("feature/foo", "a.ts")]),
        ],
    )
    def test_candidates_order_short_ref_first(self, url, expected):
        ref = parse_github_url(url)
        assert ref is not None
        assert ref.candidates() == expected

    def test_candidates_empty_for_repo(self):
        ref = parse_github_url("https://github.com/o/r")
        assert ref is not None and ref.candidates() == []

    def test_candidates_empty_when_no_path(self):
        # rest 只有 1 段（blob/{ref}）不构成文件页；此处直接构造以钉住 candidates 的前置条件
        ref = GithubRef(kind="file", owner="o", repo="r", rest=("main",))
        assert ref.candidates() == []


class TestSliceLines:
    """1-based 闭区间切片；越界按实际范围裁剪（specified + derived 边界）。"""

    _TEXT = "l1\nl2\nl3\nl4"

    def test_no_line_numbers_returns_text_verbatim(self):
        assert slice_lines(self._TEXT, None, None) == self._TEXT

    def test_single_line(self):
        assert slice_lines(self._TEXT, 2, 2) == "l2"

    def test_range_is_inclusive(self):
        assert slice_lines(self._TEXT, 2, 3) == "l2\nl3"

    def test_full_range(self):
        assert slice_lines(self._TEXT, 1, 4) == self._TEXT

    def test_start_beyond_end_of_text_yields_empty(self):
        # 越界不报错：start 已超出 → 空串
        assert slice_lines(self._TEXT, 5, 5) == ""
        assert slice_lines(self._TEXT, 99, 120) == ""

    def test_end_beyond_text_is_clamped(self):
        # 片段行号可能指向更长的历史版本 → 按实际范围裁剪
        assert slice_lines(self._TEXT, 2, 99) == "l2\nl3\nl4"

    def test_last_line_boundary(self):
        assert slice_lines(self._TEXT, 4, 4) == "l4"

    def test_trailing_newline_not_an_extra_line(self):
        assert slice_lines("l1\n", 1, 1) == "l1"

    def test_empty_text(self):
        assert slice_lines("", 1, 1) == ""


class TestIsGithubUrl:
    """fetch 自动接管的判据（specified）。"""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/o/r", True),
            ("https://github.com/o/r/blob/main/a.py", True),
            ("https://raw.githubusercontent.com/o/r/main/a.py", True),
            ("https://github.com/o/r/tree/main", False),
            ("https://gist.github.com/o/abc", False),
            ("", False),
        ],
    )
    def test_is_github_url(self, url, expected):
        assert is_github_url(url) is expected


class TestCharacterization:
    """契约未规定、按现状刻画的行为（implicit，回归护栏而非规范断言）。"""

    @pytest.mark.parametrize(
        "url",
        ["https://www.github.com/o/r", "https://GitHub.com/o/r"],
    )
    def test_host_liberal_and_case_insensitive(self, url):
        # 契约只写 github.com；www 前缀与主机大小写是实现的放宽口径
        ref = parse_github_url(url)
        assert ref is not None and ref.kind == "repo"

    def test_owner_repo_path_case_preserved(self):
        ref = parse_github_url("https://github.com/Owner/Repo/blob/Main/A.PY")
        assert ref is not None
        assert (ref.owner, ref.repo, ref.rest) == ("Owner", "Repo", ("Main", "A.PY"))

    def test_scheme_less_url_not_recognized(self):
        # docstring 只排除「非 http(s) scheme」；无 scheme 的裸串实际因 host 为空而落空
        assert parse_github_url("github.com/o/r") is None

    def test_reversed_line_range_normalized(self):
        # 契约未规定倒序区间；实测归一为 (min, max)
        ref = parse_github_url("https://github.com/o/r/blob/main/a.py#L20-L10")
        assert ref is not None
        assert (ref.line_start, ref.line_end) == (10, 20)

    def test_unknown_fragment_ignored(self):
        # 认不出的片段被静默忽略（不报错、不落回）
        ref = parse_github_url("https://github.com/o/r/blob/main/a.py#readme")
        assert ref is not None
        assert (ref.line_start, ref.line_end) == (None, None)

    def test_non_str_input_returns_none(self):
        assert parse_github_url(123) is None

    def test_slice_start_zero_yields_empty(self):
        # #L0 会解析成 (0, 0)：lo=max(1,0)=1 与 hi=0 组合出空串
        assert slice_lines("l1\nl2", 0, 0) == ""

    def test_repo_url_with_fragment_drops_line_range(self):
        # 仓库页没有行号语义，片段被丢弃
        ref = parse_github_url("https://github.com/o/r#L10")
        assert ref is not None
        assert (ref.kind, ref.line_start, ref.line_end) == ("repo", None, None)