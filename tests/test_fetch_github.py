"""fetch 命令的 GitHub 前置接管层（DEC-029）+ 快照落盘 frontmatter 契约测试。

契约来源（测试只依据这些，不依据实现输出）：
- api-contract §2.2：GitHub 文件 / 仓库 URL 在**普通接口之前**走 GitHub API；取到则
  method="github"；认不出 / 超时 / 异常 / 配额耗尽 → 静默落回原链路。
- api-contract §3：fetch 返回 FetchResult[]{url, method∈{github,normal,browser}, status, md_path, error?}。
- requirements §4.2：接管是 best-effort —— 任何失败都不抛错、不置失败态。
- data-model §1.2：落盘 frontmatter 的 fetch_method 与 FetchResult.method 为同一枚举。
- snapshot 模块 docstring：默认 tmp-doc/<YYYY-MM-DD>/scrape-<slug>-<HH-MM-SS>-<rand>.md，
  同名已存在则加序号（-1/-2/...），绝不裸覆盖。

覆盖判据：接管结果的等价类（命中 / 未命中 / 抛异常 / 超时）× 每 URL 的层级分派（哪个层服务
哪个 URL，命中即短路不再走后面的层），外加快照落盘的路径 / 命名 / frontmatter 契约。

不联网、不起浏览器：fetch_github_text / fetch_normal / fetch_with_browser 全部替换为 fake。
每个 fake 都记录收到的调用；「某层不该被调用」一律靠**调用记录为空**来断言 —— run() 用
gather(return_exceptions=True) 把 normal 层的异常收进结果并按失败处理，只抛错的 fake 会被
静默吞掉，断言就失效了（抛错仍保留，作为没被吞掉时的二重保险）。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import pytest


# 形状明确的 GitHub URL（github_urls.py 认；repo 恰好两段）
GH_URL_FILE = "https://github.com/octocat/Hello-World/blob/main/README.md"
GH_URL_REPO = "https://github.com/octocat/Hello-World"
PLAIN_URL = "https://example.com/page"


class _RunArgs:
    """fake argparse.Namespace for fetch run（与 tests/test_fetch.py 同形）。"""

    def __init__(self, urls, timeout=10, concurrency=4, no_browser=True, fmt="markdown", write=None):
        self.urls = urls
        self.write = write
        self.format = fmt
        self.timeout = timeout
        self.no_browser = no_browser
        self.no_login = False
        self.concurrency = concurrency


def _forbid_tier(name, calls):
    """造一个「被调用即失败」的层级 fake：**先记进 calls 再抛错**。

    只抛错不足以防漏：normal 层的异常会被 run() 的 gather(return_exceptions=True) 吞掉并当成
    该层失败，测试照样通过。故「该层全程未被调用」由 calls 为空来断言，抛错只作二重保险。
    """

    async def _tier(config, *args, **kwargs):
        calls.append(args[0] if args else None)
        raise AssertionError(f"{name} 层不应被调用（args={args}）")

    return _tier


# ---------------------------------------------------------------------------
# run()：接管命中 / 未命中 / 异常 / 超时 / 混合批次的 method 判定与顺序
# ---------------------------------------------------------------------------


async def test_fetch_run_github_interception_success(monkeypatch, home, tmp_path):
    """接管命中：method="github"、status="success"、有 md_path；normal/browser 层对该 URL 不被调用。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)  # 默认落盘走 tmp-path 下的 tmp-doc/，不碰仓库真实目录
    gh_calls: list[tuple[str, str]] = []
    normal_calls: list[str] = []
    browser_calls: list[str] = []

    async def fake_github(config, url, fmt):
        gh_calls.append((url, fmt))
        return "# README 正文"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", fake_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", _forbid_tier("normal", normal_calls))
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", browser_calls))

    # no_browser=False：把 browser 层也纳入可观测范围（若命中后仍误走回退链，该层会留下记录）
    result = await fetch_cmd.run(_RunArgs([GH_URL_FILE], no_browser=False), Config())

    assert gh_calls == [(GH_URL_FILE, "markdown")]  # 接管被调用，且带上了 --format（derived）
    assert normal_calls == []  # 命中即短路（契约 §2.2：接管在普通接口之前）
    assert browser_calls == []
    [r] = result["results"]
    assert r["url"] == GH_URL_FILE
    assert r["method"] == "github"  # 契约 §3 枚举
    assert r["status"] == "success"
    assert r["md_path"]
    assert "error" not in r

    # 默认落盘位置：tmp-doc/<YYYY-MM-DD>/scrape-*.md
    assert re.fullmatch(r"tmp-doc[\\/]\d{4}-\d{2}-\d{2}[\\/]scrape-.+\.md", r["md_path"])
    text = Path(r["md_path"]).read_text(encoding="utf-8")
    assert "fetch_method: github" in text  # data-model §1.2：同一枚举
    assert "# README 正文" in text


async def test_fetch_run_github_miss_falls_back_to_normal(monkeypatch, home, tmp_path):
    """接管取不到（返回 None）→ 该 URL 落回普通层，method="normal"、成功，不是新的失败态。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    gh_calls: list[str] = []
    normal_calls: list[str] = []
    browser_calls: list[str] = []

    async def fake_github(config, url, fmt):
        gh_calls.append(url)
        return None

    async def fake_normal(config, url, fmt):
        normal_calls.append(url)
        return "normal-body"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", fake_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", browser_calls))

    result = await fetch_cmd.run(_RunArgs([GH_URL_FILE]), Config())

    assert gh_calls == [GH_URL_FILE]  # 先试接管
    assert normal_calls == [GH_URL_FILE]  # 再照常走既有普通层
    assert browser_calls == []  # 普通层已成功，无需回退
    [r] = result["results"]
    assert r["method"] == "normal"
    assert r["status"] == "success"
    assert "error" not in r


async def test_fetch_run_github_miss_normal_fail_no_browser_marks_failed(monkeypatch, home):
    """接管空 + 普通层失败 + --no-browser → status="failed"，沿用命令既有错误文案。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    gh_calls: list[str] = []

    async def none_github(config, url, fmt):
        gh_calls.append(url)
        return None

    async def none_normal(config, url, fmt):
        return None

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", none_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", none_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", []))

    result = await fetch_cmd.run(_RunArgs([GH_URL_FILE]), Config())

    assert gh_calls == [GH_URL_FILE]  # 接管确实试过；否则本条对「接管被移除」不敏感
    [r] = result["results"]
    assert r["status"] == "failed"
    assert r["md_path"] is None
    # implicit（现状刻画，非规格断言）：契约 §3 未规定失败时 method 取什么值，此处沿用既有回退链形态
    assert r["method"] == "browser"
    # existing（HEAD 版本既有文案，本 delta 未改）：失败文案来自既有命令
    assert r["error"] == "普通接口失败且已禁用浏览器回退"


@pytest.mark.parametrize("kind", ["generic", "provider"])
async def test_fetch_run_github_exception_is_swallowed(monkeypatch, home, tmp_path, kind):
    """接管抛异常（网络错 / 配额耗尽）→ run() 不抛出，URL 仍落回普通层（best-effort，不新增失败态）。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config
    from research_assistant.errors import ProviderError

    monkeypatch.chdir(tmp_path)
    exc = (
        RuntimeError("github 接管失败")
        # api-contract §4：配额耗尽 / 401 / 404 一律按 PROVIDER 报出 → 接管层须吞掉这一类
        if kind == "generic"
        else ProviderError("github: 配额耗尽", provider="github", details={"rate_limit_remaining": 0})
    )
    gh_calls: list[str] = []
    normal_calls: list[str] = []

    async def boom_github(config, url, fmt):
        gh_calls.append(url)
        raise exc

    async def fake_normal(config, url, fmt):
        normal_calls.append(url)
        return "normal-body"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", boom_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", []))

    result = await fetch_cmd.run(_RunArgs([GH_URL_FILE]), Config())

    assert gh_calls == [GH_URL_FILE]  # 接管确实被调用并抛了；否则本条对「接管被移除」不敏感
    assert normal_calls == [GH_URL_FILE]  # 异常没打断回退链
    [r] = result["results"]
    assert r["method"] == "normal"
    assert r["status"] == "success"  # 不是新的失败态


async def test_fetch_run_github_timeout_falls_through(monkeypatch, home, tmp_path):
    """接管超过 --timeout → 不抛出、不阻塞整批，URL 落回普通层，且没有真等满慢接管。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    gh_started: list[str] = []
    normal_calls: list[str] = []
    gh_finished = asyncio.Event()  # 慢接管跑完才会 set；被超时取消则永不 set

    async def slow_github(config, url, fmt):
        gh_started.append(url)
        await asyncio.sleep(5)  # 远超 timeout=0.1
        gh_finished.set()
        return "# 不该出现"

    async def fake_normal(config, url, fmt):
        normal_calls.append(url)
        return "normal-body"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", slow_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", []))

    started = time.monotonic()
    result = await fetch_cmd.run(_RunArgs([GH_URL_FILE], timeout=0.1), Config())
    elapsed = time.monotonic() - started

    assert gh_started == [GH_URL_FILE]  # 接管确实被尝试；否则本条对「接管被移除」不敏感
    assert normal_calls == [GH_URL_FILE]
    assert not gh_finished.is_set()  # derived：慢接管被取消，没有硬等它跑完
    [r] = result["results"]
    assert r["method"] == "normal"  # 接管没有硬等到内容出来
    assert r["status"] == "success"
    assert elapsed < 3.0  # derived：超时必须生效，不能真等满 5s


async def test_fetch_run_mixed_batch_independent_methods_and_order(monkeypatch, home, tmp_path):
    """混合批次：每个 URL 的 method 独立判定，且结果列表保持输入 URL 顺序。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    urls = [GH_URL_FILE, GH_URL_REPO, PLAIN_URL]
    gh_calls: list[str] = []
    normal_calls: list[str] = []

    async def fake_github(config, url, fmt):
        gh_calls.append(url)
        return "# GH 正文" if url == GH_URL_FILE else ""  # 空串 = 这层没接住

    async def fake_normal(config, url, fmt):
        normal_calls.append(url)
        return f"normal:{url}"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", fake_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", []))

    result = await fetch_cmd.run(_RunArgs(urls), Config())

    # 只有形状明确的 GitHub URL 进接管层（非 GitHub URL 压根不试接管）
    assert set(gh_calls) == {GH_URL_FILE, GH_URL_REPO}
    # 未命中的 GitHub URL 与非 GitHub URL 都走普通层；命中的那个不再走普通层
    assert set(normal_calls) == {GH_URL_REPO, PLAIN_URL}
    assert [r["url"] for r in result["results"]] == urls  # 顺序 = 输入顺序
    assert [r["method"] for r in result["results"]] == ["github", "normal", "normal"]
    assert [r["status"] for r in result["results"]] == ["success", "success", "success"]
    assert all(r["md_path"] for r in result["results"])  # 三条都落了盘
    # 落盘标出「这次不是渲染抓取」：命中的那条 frontmatter 为 github
    hit_text = Path(result["results"][0]["md_path"]).read_text(encoding="utf-8")
    assert "fetch_method: github" in hit_text


async def test_fetch_run_write_path_honored_for_github_content(monkeypatch, home, tmp_path):
    """--write <path> 在接管产出内容时生效：md_path = 指定路径，内容带 fetch_method: github。"""
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    normal_calls: list[str] = []
    browser_calls: list[str] = []

    async def fake_github(config, url, fmt):
        return "# README 正文"

    monkeypatch.setattr(fetch_cmd, "fetch_github_text", fake_github)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", _forbid_tier("normal", normal_calls))
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", _forbid_tier("browser", browser_calls))

    target = tmp_path / "out" / "gh.md"
    result = await fetch_cmd.run(
        _RunArgs([GH_URL_FILE], write=str(target), no_browser=False), Config()
    )

    assert normal_calls == []  # 命中即短路，普通层未被调用
    assert browser_calls == []
    [r] = result["results"]
    assert r["status"] == "success"
    assert r["method"] == "github"
    assert r["md_path"] == str(target)
    text = target.read_text(encoding="utf-8")
    assert f"url: {GH_URL_FILE}\n" in text
    assert "fetch_method: github" in text
    assert "# README 正文" in text


# ---------------------------------------------------------------------------
# write_snapshot：frontmatter 枚举、默认目录、绝不覆盖既有文件
# ---------------------------------------------------------------------------


def test_write_snapshot_frontmatter_fetch_method_enum(tmp_path):
    """frontmatter 的 fetch_method 等于传入的枚举值（github/normal/browser 同一枚举）。"""
    from research_assistant.fetch.snapshot import write_snapshot

    for method in ("github", "normal", "browser"):
        target = tmp_path / f"{method}.md"
        path = write_snapshot(GH_URL_REPO, method, "正文", str(target))
        assert path == str(target)
        text = target.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        head, sep, body = text[4:].partition("\n---\n")
        assert sep, "frontmatter 必须有闭合分隔行"
        lines = head.splitlines()
        assert f"fetch_method: {method}" in lines  # data-model §1.2：键在 frontmatter 内，值 = 传入枚举
        assert f"url: {GH_URL_REPO}" in lines
        assert "正文" in body


class _FrozenDateTime(datetime):
    """冻结时钟：now() 恒为 2026-09-14 12:00:00（本地与 UTC 同日，避免跨日抖动）。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz)


def test_write_snapshot_default_path_and_never_overwrites(monkeypatch, tmp_path):
    """默认落盘在 tmp-doc/<YYYY-MM-DD>/；同名冲突时追加序号，既有文件不被覆盖。"""
    from research_assistant.fetch import snapshot as snap

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(snap.secrets, "token_hex", lambda n: "abc123")  # 固定随机后缀
    monkeypatch.setattr(snap, "datetime", _FrozenDateTime)  # 固定日期与时间戳

    first = snap.write_snapshot(GH_URL_REPO, "github", "第一份", None)
    second = snap.write_snapshot(GH_URL_REPO, "github", "第二份", None)
    p1, p2 = Path(first), Path(second)

    assert p1.parts[0] == "tmp-doc"
    assert p1.parts[1] == "2026-09-14"
    # derived：slug = 主机名点换横线 + 路径段，ts = HH-MM-SS，再拼随机后缀
    assert p1.name == "scrape-github-com-octocat-Hello-World-12-00-00-abc123.md"
    # specified：snapshot docstring「同名已存在则加序号（-1/-2/...）」→ 第二份换名为 -1 后缀
    assert p2.name == "scrape-github-com-octocat-Hello-World-12-00-00-abc123-1.md"
    assert p2.parts[0] == "tmp-doc" and p2.parts[1] == "2026-09-14"
    assert p1 != p2
    assert p1.read_text(encoding="utf-8").endswith("第一份\n")  # 既有文件没被第二份覆盖
    assert p2.read_text(encoding="utf-8").endswith("第二份\n")