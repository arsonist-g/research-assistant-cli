"""GitHub provider：零配置、base/raw 推导、错误分流、取文件双通道、repo 两次调用、渲染、fetch 接管。

mock 掉 research_assistant.http.make_client 注入 MockTransport，断言请求 URL/headers/params（不发网络）。
oracle 标注：specified = api-contract.md「Delta：GitHub provider」+ providers/github.py 模块 docstring；
derived = 由口径手工推导；implicit = 契约未规定、按现状刻画（回归护栏，已在报告里列为待确认）。
"""

from __future__ import annotations

import argparse
import datetime

import httpx
import pytest

from research_assistant import http as ra_http
import research_assistant.providers.github as gh
from research_assistant.config import Config, ProviderConfig
from research_assistant.errors import ArgsError, NetworkError, ProviderError
from research_assistant.providers.github import (
    GithubProvider,
    _error_for,
    _raw_base_for,
    _resolve_base,
)

pytestmark = pytest.mark.usefixtures("clean_env")  # conftest：清 RA_*/代理变量，隔离宿主环境

_API_BASE = "https://api.github.com"
_RAW_HOST_BASE = "https://raw.githubusercontent.com"
_FAKE_KEY = "ghp-test-placeholder"  # 占位凭据，非真实 token
_API_VERSION = "2026-03-10"  # 模块 docstring 钉住的版本头
_FILE_URL = "https://github.com/o/r/blob/main/a.py"
_REPO_URL = "https://github.com/o/r"

# 合成元数据（非真实仓库）
_REPO_META = {
    "full_name": "o/r",
    "description": "demo repo",
    "visibility": "public",
    "default_branch": "main",
    "stargazers_count": 1234,
    "forks_count": 56,
    "subscribers_count": 78,
    "open_issues_count": 9,
    "language": "Python",
    "license": {"spdx_id": "MIT", "name": "MIT License"},
    "topics": ["alpha", "beta"],
    "size": 2048,
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2021-02-02T00:00:00Z",
    "pushed_at": "2022-03-03T00:00:00Z",
    "archived": False,
    "fork": True,
    "homepage": "https://example.com/o/r",
    "html_url": "https://github.com/o/r",
}


class _Stub:
    """MockTransport handler：记录全部请求，响应由 route(request) 决定。"""

    def __init__(self, route):
        self._route = route
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._route(request)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _patch_client(monkeypatch, stub: _Stub) -> None:
    """让 github provider 的 make_client 返回走 MockTransport 的 client（与 test_context7 同法）。"""

    def fake_make_client(*, proxy_url="", timeout=30.0, base_url="", headers=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(stub))

    monkeypatch.setattr(ra_http, "make_client", fake_make_client)
    # 系统代理自动探测与用例无关：钉成直连，避免用例依赖宿主网络配置
    monkeypatch.setattr(gh, "resolve_proxy", lambda url="": "")


def _provider(base_url: str = "", api_key: str = "", *, entry: bool = True) -> GithubProvider:
    cfg = Config()
    if entry:
        cfg.providers.append(ProviderConfig(type="github", base_url=base_url, api_key=api_key))
    return GithubProvider(config=cfg)


class TestConfig:
    """DEC-028/029：两项都可缺省，零配置即可用；base_url 原样使用。"""

    def test_no_provider_entry_does_not_raise(self):
        cfg = _provider(entry=False)._cfg()
        assert (cfg.type, cfg.base_url, cfg.api_key) == ("github", _API_BASE, "")

    def test_no_base_url_and_no_api_key(self):
        cfg = _provider()._cfg()
        assert (cfg.base_url, cfg.api_key) == (_API_BASE, "")

    def test_config_none_does_not_raise(self):
        cfg = GithubProvider(config=None)._cfg()
        assert (cfg.base_url, cfg.api_key) == (_API_BASE, "")

    def test_explicit_base_used_verbatim(self):
        # 前后空白与尾斜杠归一，路径本身原样保留（不自动补 /api/v3）
        cfg = _provider("  https://ghe.example.com/api/v3/  ", "k")._cfg()
        assert (cfg.base_url, cfg.api_key) == ("https://ghe.example.com/api/v3", "k")

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("", _API_BASE),
            ("https://api.github.com", _API_BASE),
            ("https://api.github.com/", _API_BASE),
            ("  https://api.github.com  ", _API_BASE),
            # GHE 必须调用方写全；裸域名原样保留，不猜 /api/v3
            ("https://ghe.example.com", "https://ghe.example.com"),
            ("https://ghe.example.com/api/v3", "https://ghe.example.com/api/v3"),
            ("https://gateway.example.com/github/", "https://gateway.example.com/github"),
        ],
    )
    def test_resolve_base(self, raw, expected):
        assert _resolve_base(raw) == expected

    @pytest.mark.parametrize(
        "base, expected",
        [
            (_API_BASE, _RAW_HOST_BASE),
            ("https://api.github.com/", _RAW_HOST_BASE),
            # 非 api.github.com：一律不推导 raw 通道（走 contents API）
            ("https://ghe.example.com/api/v3", ""),
            ("https://gateway.example.com/github", ""),
            ("", ""),
        ],
    )
    def test_raw_base_for(self, base, expected):
        assert _raw_base_for(base) == expected


class TestErrorFor:
    """错误分流：401 凭据 / 403 配额耗尽 / 404 不可见 / 其余通用（specified）。"""

    def test_401_bad_credentials(self):
        err = _error_for(httpx.Response(401, json={"message": "Bad credentials"}))
        assert isinstance(err, ProviderError)
        assert (err.code, err.provider) == ("PROVIDER", "github")
        assert err.details["status_code"] == 401
        assert "认证" in err.message
        assert "rate_limit_reset" not in err.details

    def test_403_with_zero_remaining_is_quota_exhausted(self):
        epoch = 1700000000
        err = _error_for(
            httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(epoch)},
                text="API rate limit exceeded",
            )
        )
        assert err.code == "PROVIDER"
        assert err.details["status_code"] == 403
        assert err.details["rate_limit_remaining"] == "0"
        # reset 是 epoch 秒转本地可读时间；用标准库独立推导（不调用被测函数）
        local = datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        assert err.details["rate_limit_reset"].startswith(local)
        assert "配额" in err.message

    def test_403_without_zero_remaining_is_not_quota(self):
        err = _error_for(
            httpx.Response(403, headers={"X-RateLimit-Remaining": "42"}, text="Forbidden")
        )
        assert err.details["status_code"] == 403
        assert err.details["rate_limit_remaining"] == "42"
        assert "rate_limit_reset" not in err.details
        assert "配额" not in err.message

    def test_quota_without_reset_header_is_tolerated(self):
        err = _error_for(httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, text="x"))
        assert not err.details.get("rate_limit_reset")  # implicit：缺失 → 空串
        assert "配额" in err.message

    def test_404(self):
        err = _error_for(httpx.Response(404, json={"message": "Not Found"}))
        assert err.details["status_code"] == 404
        assert "不存在" in err.message
        assert "私有仓库" in err.message

    def test_other_non_2xx_carries_body(self):
        err = _error_for(httpx.Response(500, text="boom"))
        assert err.details["status_code"] == 500
        assert err.details["body"] == "boom"  # implicit：响应体截断透出
        assert "500" in err.message


class TestCapabilities:
    def test_declares_file_and_repo(self):
        caps = {c.name: c for c in _provider().capabilities()}
        assert set(caps) == {"file", "repo"}
        for cap in caps.values():
            assert cap.args[0].name_or_flags == ["url"]
            assert cap.args[0].kind == "positional"

    def test_command_and_alias(self):
        assert (GithubProvider.type, GithubProvider.command) == ("github", "github")
        assert GithubProvider.aliases == ["gh"]


class TestFileHandler:
    async def test_raw_channel_first_and_request_shape(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x = 1\n"))
        _patch_client(monkeypatch, stub)

        out = await _provider(api_key=_FAKE_KEY).file(argparse.Namespace(url=_FILE_URL))

        # raw 首选：命中即短路，只发 1 个请求
        assert stub.urls == [f"{_RAW_HOST_BASE}/o/r/main/a.py"]
        req = stub.requests[0]
        assert req.headers["x-github-api-version"] == _API_VERSION
        assert req.headers["accept"] == "text/plain"
        assert "authorization" not in req.headers  # 不把 token 发给 raw 主机
        assert out == {
            "kind": "file",
            "owner": "o",
            "repo": "r",
            "ref": "main",
            "path": "a.py",
            "source": "raw",
            "bytes": 6,
            "contents": [
                {"text": "x = 1\n", "source_url": "https://github.com/o/r/blob/main/a.py"}
            ],
        }

    async def test_contents_fallback_when_raw_404(self, monkeypatch):
        def route(r):
            if r.url.host == "raw.githubusercontent.com":
                return httpx.Response(404, text="404: Not Found")
            return httpx.Response(200, text="print(1)\n")

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider(api_key=_FAKE_KEY).file(argparse.Namespace(url=_FILE_URL))

        assert stub.urls == [
            f"{_RAW_HOST_BASE}/o/r/main/a.py",
            f"{_API_BASE}/repos/o/r/contents/a.py?ref=main",
        ]
        contents_req = stub.requests[1]
        assert contents_req.headers["accept"] == "application/vnd.github.raw+json"
        assert contents_req.headers["authorization"] == f"Bearer {_FAKE_KEY}"
        assert out["source"] == "api"
        assert (out["ref"], out["path"]) == ("main", "a.py")
        assert out["contents"][0]["text"] == "print(1)\n"
        assert out["bytes"] == 9

    async def test_contents_anonymous_omits_authorization(self, monkeypatch):
        def route(r):
            if r.url.host == "raw.githubusercontent.com":
                return httpx.Response(404, text="404")
            return httpx.Response(200, text="k\n")

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider().file(argparse.Namespace(url=_FILE_URL))

        assert "authorization" not in stub.requests[1].headers
        assert out["source"] == "api"

    async def test_ref_candidates_tried_short_to_long(self, monkeypatch):
        def route(r):
            if r.url.host == "raw.githubusercontent.com":
                return httpx.Response(404, text="404")
            if r.url.params.get("ref") == "feature/foo":
                return httpx.Response(200, text="export const a = 1\n")
            return httpx.Response(404, text="404")

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider().file(
            argparse.Namespace(url="https://github.com/o/r/blob/feature/foo/a.ts")
        )

        # 每个候选「raw 先、contents 后」；候选 ref 先短后长，命中即短路
        assert [(r.url.host, r.url.path, r.url.params.get("ref")) for r in stub.requests] == [
            ("raw.githubusercontent.com", "/o/r/feature/foo/a.ts", None),
            ("api.github.com", "/repos/o/r/contents/foo/a.ts", "feature"),
            ("raw.githubusercontent.com", "/o/r/feature/foo/a.ts", None),
            ("api.github.com", "/repos/o/r/contents/a.ts", "feature/foo"),
        ]
        assert (out["ref"], out["path"], out["source"]) == ("feature/foo", "a.ts", "api")

    async def test_ghe_base_never_derives_raw_channel(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="ghe\n"))
        _patch_client(monkeypatch, stub)

        out = await _provider("https://ghe.example.com/api/v3", "k").file(
            argparse.Namespace(url=_FILE_URL)
        )

        assert stub.urls == ["https://ghe.example.com/api/v3/repos/o/r/contents/a.py?ref=main"]
        assert out["source"] == "api"

    async def test_line_range_sliced_from_raw_text(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="l1\nl2\nl3\nl4\n"))
        _patch_client(monkeypatch, stub)

        out = await _provider().file(argparse.Namespace(url=_FILE_URL + "#L2-L3"))

        assert out["contents"][0]["text"] == "l2\nl3"
        assert out["bytes"] == 5
        assert (out["line_start"], out["line_end"]) == (2, 3)

    async def test_all_candidates_failed_raises_provider_error(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(404, text="404"))
        _patch_client(monkeypatch, stub)

        with pytest.raises(ProviderError) as ei:
            await _provider().file(
                argparse.Namespace(url="https://github.com/o/r/blob/feature/foo/a.ts")
            )
        assert ei.value.code == "PROVIDER"
        assert ei.value.details["candidates_tried"] == 2
        assert (ei.value.details["owner"], ei.value.details["repo"]) == ("o", "r")
        assert len(stub.requests) == 4  # 2 候选 x (raw + contents)

    async def test_repo_url_to_file_command_is_args_error(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x"))
        _patch_client(monkeypatch, stub)

        with pytest.raises(ArgsError) as ei:
            await _provider().file(argparse.Namespace(url=_REPO_URL))
        assert ei.value.code == "ARGS"
        assert stub.requests == []

    async def test_unrecognized_url_to_file_command_is_args_error(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x"))
        _patch_client(monkeypatch, stub)

        with pytest.raises(ArgsError) as ei:
            await _provider().file(argparse.Namespace(url="https://example.com/o/r/blob/x/a.py"))
        assert ei.value.code == "ARGS"
        assert stub.requests == []

    async def test_transport_error_becomes_network_error(self, monkeypatch):
        def route(r):
            raise httpx.ConnectError("boom", request=r)

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        with pytest.raises(NetworkError) as ei:
            await _provider().file(argparse.Namespace(url=_FILE_URL))
        assert ei.value.code == "NETWORK"


class TestRepoHandler:
    async def test_exactly_two_calls_and_field_mapping(self, monkeypatch):
        def route(r):
            if r.url.path.endswith("/readme"):
                return httpx.Response(200, text="# r\n")
            return httpx.Response(200, json=_REPO_META)

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider(api_key=_FAKE_KEY).repo(argparse.Namespace(url=_REPO_URL))

        # spec：恰好 2 次 API 调用（/repos + /readme）
        assert stub.urls == [f"{_API_BASE}/repos/o/r", f"{_API_BASE}/repos/o/r/readme"]
        assert stub.requests[0].headers["accept"] == "application/vnd.github+json"
        assert stub.requests[1].headers["accept"] == "application/vnd.github.raw+json"
        assert stub.requests[0].headers["authorization"] == f"Bearer {_FAKE_KEY}"
        assert out == {
            "kind": "repo",
            "full_name": "o/r",
            "description": "demo repo",
            "visibility": "public",
            "default_branch": "main",
            "stargazers_count": 1234,
            "forks_count": 56,
            "subscribers_count": 78,
            "open_issues_count": 9,
            "language": "Python",
            "license": "MIT",
            "topics": ["alpha", "beta"],
            "size_kb": 2048,
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2021-02-02T00:00:00Z",
            "pushed_at": "2022-03-03T00:00:00Z",
            "archived": False,
            "fork": True,
            "homepage": "https://example.com/o/r",
            "html_url": "https://github.com/o/r",
            "contents": [{"text": "# r\n", "source_url": "https://github.com/o/r#readme"}],
        }

    async def test_readme_404_is_tolerated(self, monkeypatch):
        def route(r):
            if r.url.path.endswith("/readme"):
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=_REPO_META)

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider().repo(argparse.Namespace(url=_REPO_URL))

        assert len(stub.requests) == 2  # README 404 不重试、不影响元数据
        assert "contents" not in out  # contents 是可缺省字段
        assert out["full_name"] == "o/r"

    async def test_repos_failure_short_circuits(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(404, json={"message": "Not Found"}))
        _patch_client(monkeypatch, stub)

        with pytest.raises(ProviderError) as ei:
            await _provider().repo(argparse.Namespace(url=_REPO_URL))
        assert ei.value.code == "PROVIDER"
        assert len(stub.requests) == 1  # /repos 失败即结束，不再请求 /readme

    async def test_file_url_to_repo_command_is_args_error(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x"))
        _patch_client(monkeypatch, stub)

        with pytest.raises(ArgsError) as ei:
            await _provider().repo(argparse.Namespace(url=_FILE_URL))
        assert ei.value.code == "ARGS"
        assert stub.requests == []

    async def test_minimal_meta_maps_missing_fields_to_none(self, monkeypatch):
        def route(r):
            if r.url.path.endswith("/readme"):
                return httpx.Response(404)
            return httpx.Response(200, json={"full_name": "o/r"})

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await _provider().repo(argparse.Namespace(url=_REPO_URL))

        assert out["topics"] == []  # spec：topics[]
        assert out["license"] is None
        assert out["size_kb"] is None
        for key in (
            "description", "visibility", "default_branch", "stargazers_count",
            "forks_count", "subscribers_count", "open_issues_count", "language",
            "created_at", "updated_at", "pushed_at", "archived", "fork",
            "homepage", "html_url",
        ):
            assert out[key] is None


class TestRenderFile:
    """扩展名 → 语言标记的围栏；--format text|html 只出正文（specified）。"""

    @pytest.mark.parametrize(
        "text, path, fmt, expected",
        [
            ("a = 1\n", "a.py", "markdown", "```python\na = 1\n```\n"),
            ("a = 1", "a.py", "markdown", "```python\na = 1\n```\n"),  # 缺尾换行补齐
            ("# t\n", "README.md", "markdown", "```markdown\n# t\n```\n"),
            ("x\n", "a.weirdext", "markdown", "```\nx\n```\n"),  # 认不出扩展名 → 不加语言标记
            ("FROM x\n", "Dockerfile", "markdown", "```dockerfile\nFROM x\n```\n"),
            ("k: v\n", "conf.yml", "markdown", "```yaml\nk: v\n```\n"),
            ("a = 1\n", "a.py", "text", "a = 1\n"),
            ("a = 1\n", "a.py", "html", "a = 1\n"),
            ("```\ninner\n", "a.py", "markdown", "````python\n```\ninner\n````\n"),  # 围栏按正文加长
        ],
    )
    def test_render(self, text, path, fmt, expected):
        assert GithubProvider._render_file(text, path, fmt) == expected


class TestRenderRepo:
    """紧凑 markdown（spec：标题 + 关键元数据 + README）。"""

    def test_compact_markdown(self):
        meta = dict(_REPO_META, archived=True)
        expected = (
            "# o/r\n"
            "\n"
            "demo repo\n"
            "\n"
            "- stars 1234 · forks 56 · watchers 78 · open_issues+PR 9\n"
            "- language: Python\n"
            "- license: MIT\n"
            "- default_branch: main\n"
            "- pushed_at: 2022-03-03T00:00:00Z\n"
            "- topics: alpha, beta\n"
            "- archived: true\n"
            "- homepage: https://example.com/o/r\n"
        )
        assert GithubProvider._render_repo(meta, None, "markdown") == expected

    def test_minimal_meta(self):
        assert GithubProvider._render_repo({"full_name": "o/r"}, None, "markdown") == "# o/r\n\n"

    def test_readme_appended_under_heading(self):
        assert (
            GithubProvider._render_repo({"full_name": "o/r"}, "# Hi\n", "markdown")
            == "# o/r\n\n\n## README\n\n# Hi\n\n"
        )

    def test_html_degrades_to_markdown(self):
        # implicit：契约只规定「源码文件没有 HTML 对应物」，repo 侧按 markdown 输出
        assert GithubProvider._render_repo({"full_name": "o/r"}, None, "html") == "# o/r\n\n"


class TestApiVersionHeader:
    async def test_pinned_version_header_on_api_call(self, monkeypatch):
        # 模块 docstring：显式钉住 api 版本（不钉则默认 2022-11-28）
        stub = _Stub(lambda r: httpx.Response(200, json={"full_name": "o/r"}))
        _patch_client(monkeypatch, stub)

        await _provider().repo(argparse.Namespace(url=_REPO_URL))

        assert stub.requests[0].headers["x-github-api-version"] == _API_VERSION


class TestFetchGithubText:
    """fetch 接管入口：best-effort，任何失败返回 None、绝不抛异常（specified）。"""

    async def test_unrecognized_url_returns_none_without_request(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x"))
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), "https://example.com/o/r") is None
        assert stub.requests == []

    async def test_file_success_markdown(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x = 1\n"))
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _FILE_URL) == "```python\nx = 1\n```\n"

    async def test_file_success_text_format(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="x = 1\n"))
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _FILE_URL, "text") == "x = 1\n"

    async def test_line_range_applied_in_text_mode(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(200, text="l1\nl2\nl3\n"))
        _patch_client(monkeypatch, stub)

        out = await gh.fetch_github_text(Config(), _FILE_URL + "#L2-L3", "text")
        assert out == "l2\nl3"

    async def test_repo_success_includes_readme(self, monkeypatch):
        def route(r):
            if r.url.path.endswith("/readme"):
                return httpx.Response(200, text="# r\n")
            return httpx.Response(200, json=_REPO_META)

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        out = await gh.fetch_github_text(Config(), _REPO_URL)

        assert out is not None
        assert out.startswith("# o/r\n")
        assert "## README" in out
        assert out.endswith("# r\n\n")

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    async def test_http_error_returns_none(self, monkeypatch, status):
        stub = _Stub(lambda r: httpx.Response(status, text="err"))
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _FILE_URL) is None

    async def test_quota_exhausted_returns_none(self, monkeypatch):
        stub = _Stub(
            lambda r: httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
                text="rate limited",
            )
        )
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _REPO_URL) is None

    async def test_transport_error_returns_none(self, monkeypatch):
        def route(r):
            raise httpx.ConnectTimeout("timeout", request=r)

        stub = _Stub(route)
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _FILE_URL) is None

    async def test_file_not_found_on_all_channels_returns_none(self, monkeypatch):
        stub = _Stub(lambda r: httpx.Response(404, text="404"))
        _patch_client(monkeypatch, stub)

        assert await gh.fetch_github_text(Config(), _FILE_URL) is None