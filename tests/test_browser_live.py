"""browser 平台 live 测试（真实起浏览器，默认跳过）。

默认跳过（marker live_browser + pyproject addopts 排除）。手动运行：

    .venv/Scripts/python.exe -m pytest tests/test_browser_live.py -m live_browser -s

验证 browser 平台端到端：
  - browser search：关键词进 → results 非空，url 是真实目标（非 bing /ck/a 跳转）。
  - browser fetch per-future 超时：短 timeout + 慢页面 → 必在预算内返回（finally 杀 PID + shutdown 生效）。
  - browser fetch 的进程/profile 收尾（DEC-030）：取回内容；不在 %TEMP%\\DrissionPage\\autoPortData
    新增目录；无命令行引用 profiles 根的浏览器进程残留；per-call profile 目录已删。

CF（Cloudflare 盾）绕过不在此文件验证：见 tests/test_cf_live.py（nopecha 5 秒盾 + Turnstile，marker live_cf）。

依赖：DrissionPage 已装、本地 Edge/Chromium 可执行、外网可达。
注意：browser fetch 与 browser search 都是 headless（DEC-028，不弹浏览器窗口）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live_browser]

from research_assistant import config as config_mod
from research_assistant.config import Config
from research_assistant.fetch import fetch_with_browser
from research_assistant.fetch import search_engine as se
from research_assistant.fetch.browser import _channel_executable


def _cfg() -> Config:
    return Config()  # 默认 browser channel=msedge


def _skip_if_unusable() -> None:
    if _channel_executable("msedge") is None:
        pytest.skip("本地未找到 msedge 浏览器可执行")


async def test_search_returns_real_urls():
    """browser search：关键词返回非空结果，url 是真实目标（非 bing /ck/a 跳转）。"""
    _skip_if_unusable()
    out = await se.search_engine(_cfg(), "python asyncio", engine="bing-intl", limit=5, max_pages=2)
    assert out["engine"] == "bing-intl"
    assert out["results"], "search 返回空结果"
    # url 应是真实目标，不是 bing /ck/a 跳转包装
    assert all("/ck/a" not in r["url"] for r in out["results"]), (
        f"url 仍是 bing 跳转: {[r['url'] for r in out['results'][:2]]}"
    )
    for r in out["results"][:3]:
        assert r["url"].startswith("http"), r
        assert r["title"], r


async def test_fetch_timeout_aborts_within_budget():
    """短 timeout + 慢页面：fetch_with_browser 必在合理时间内返回，证明 finally 杀 PID + shutdown 清理生效。

    grok.com 有 CF 挑战，solve 至少数秒；timeout=2 必触发 per-future 超时。若 finally 的
    先杀 PID 再 shutdown 失效，残余 worker 会干等到 solve 自然结束（最坏 1-2 分钟），elapsed 会破阈值。
    """
    import time
    _skip_if_unusable()
    url = "https://grok.com/release-notes"
    start = time.monotonic()
    await fetch_with_browser(_cfg(), [url], login=False, concurrency=1, timeout=2)
    elapsed = time.monotonic() - start
    assert elapsed < 25, (
        f"fetch 用了 {elapsed:.1f}s，疑似 finally 杀 PID + shutdown 清理失效（残余 worker 干等）"
    )


# ---------------------------------------------------------------------------
# DEC-030：真浏览器跑完不留 %TEMP% 影子 profile、不留浏览器进程、不留 per-call profile 目录
# ---------------------------------------------------------------------------


def _dir_names(root: Path) -> set:
    """root 下的子目录名集合（root 不存在 → 空集）。"""
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def _cmdlines_containing(needle: str) -> list:
    """独立实现（不调用被测代码）：列出命令行含 needle 的进程。Windows 走 CIM，POSIX 走 ps。"""
    if sys.platform.startswith("win"):
        # needle 走环境变量、不进命令行：否则这条 PowerShell 自己的命令行也含 needle，会自命中。
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:RA_PROBE_NEEDLE) } | "
            'ForEach-Object { "{0} {1}" -f $_.ProcessId, $_.Name }'
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "RA_PROBE_NEEDLE": needle},
        )
    else:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30
        )
        return [line.strip() for line in out.stdout.splitlines() if needle in line]
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


async def test_fetch_uses_per_call_profile_and_leaves_no_orphan(home):
    """DEC-030 端到端：一次真实 fetch 后 ① 取回内容；② %TEMP%\\DrissionPage\\autoPortData 不新增目录
    （修前 auto_port 会把真实 profile 改写到那里）；③ 无命令行引用 profiles 根的浏览器进程残留；
    ④ per-call profile 目录已删。
    """
    _skip_if_unusable()
    url = "https://example.com/"
    profiles_root = config_mod.profiles_dir()
    auto_port_root = Path(tempfile.gettempdir()) / "DrissionPage" / "autoPortData"
    auto_port_before = _dir_names(auto_port_root)

    raw = await fetch_with_browser(_cfg(), [url], login=False, concurrency=1)

    md = raw.get(url)
    assert md, f"fetch 返回空: {url}"  # ①

    new_dirs = _dir_names(auto_port_root) - auto_port_before
    assert not new_dirs, f"仍在 %TEMP% 留 DrissionPage auto_port profile: {sorted(new_dirs)}"  # ②

    leftovers = _cmdlines_containing(str(profiles_root))
    assert not leftovers, "有浏览器进程残留并引用 profiles 根:\n" + "\n".join(leftovers)  # ③

    leftover_dirs = sorted(p.name for p in profiles_root.iterdir()) if profiles_root.exists() else []
    assert leftover_dirs == [], f"per-call profile 目录未删: {leftover_dirs}"  # ④
