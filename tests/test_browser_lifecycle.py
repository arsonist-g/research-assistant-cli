"""浏览器进程 / profile 生命周期测试（DEC-030：弃 auto_port、自持端口与 profile、按 profile 清孤儿进程树）。

契约来源（唯一行为权威）：
  - `project-memory/memory/DECISIONS.md` DEC-030 Decision 1–4
  - `project-memory/spec/architecture.md` §2.3 Delta（2026-09-18）
  - `.../backend-design/research-assistant/data-model.md` §2.1 Delta（2026-09-18）

覆盖的契约点：
  - `_alloc_port`：9600–59600 内取一个空闲端口，逐次随机。
  - `_build_dp_options`：`set_local_port` 设调试端口（`is_auto_port` 为假），`--user-data-dir` 指向
    per-call `profile_dir`（auto_port 会在启动时把它改写成 %TEMP%\\DrissionPage\\autoPortData\\<port>）。
  - `_launch_page`：薄包装 `ChromiumPage`；构造抛错时先按 profile 目录名杀浏览器进程树，再原样抛出。
  - `_close_browser`：杀完 pid 后等它真的消失（有上限）再返回。
  - `_profile_browser_pids` / `_kill_profile_browsers`：按命令行里的 profile 目录名匹配浏览器进程，
    只取根进程（父不在匹配集里），经既有 `_kill_pid` 做进程树杀。
  - `cleanup_orphans`：对「属主已死且超期」的 profile 目录，先杀命令行引用该目录名的进程树再删目录；
    绝不 taskkill 已死属主 pid（PID 复用会误杀无关进程树）。
  - `cleanup_browser_locks`：超期死锁只删锁目录，完全不 taskkill。
  - fetch 单 url / fetch 多 tab / browser search 三个启动点统一走 `_launch_page`。

不联网、不起真实浏览器：与进程相关的用例只操作本文件自己 spawn 的 python 子进程（命令行带
`--user-data-dir=<profile 目录>`，与真实浏览器进程的命令行形状一致），用例结束按树回收。
真实链路（真浏览器）在 `tests/test_browser_live.py` 的 live_browser 用例里，默认不跑。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 脚手架：命令行匹配用的假「浏览器」进程（父 + 孙，二者命令行都带 --user-data-dir=<profile 目录>）
# ---------------------------------------------------------------------------

_SLEEP_SECONDS = 120  # 用例期间一直活着；结束由本文件按树回收
_CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

# 父进程：再起一个孙进程（命令行同样带 marker），把孙进程 pid 打到 stdout，然后长睡。
_PARENT_SRC = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%d)', sys.argv[1]])\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(%d)\n" % (_SLEEP_SECONDS, _SLEEP_SECONDS)
)


def _pid_exists(pid: int) -> bool:
    """独立于被测实现的「进程是否存在」判定（Windows 走 OpenProcess，POSIX 走 kill 0）。"""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    """用例收尾用：按树强杀（对已退出的 pid 无害）。"""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _readline_with_timeout(proc: subprocess.Popen, timeout: float) -> str:
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return box[0].strip() if box else ""


@pytest.fixture
def cmdline_procs(tmp_path):
    """spawn 一组「父 + 孙」python 进程（命令行都带 `--user-data-dir=<profile 目录>`）。

    返回 `spawn(profile_dir) -> (parent_pid, grandchild_pid)`；用例结束把两代进程按树回收。
    """
    spawned: list[int] = []

    def spawn(profile_dir: Path) -> tuple[int, int]:
        marker = f"--user-data-dir={profile_dir}"
        parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_SRC, marker],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        spawned.append(parent.pid)
        reported = _readline_with_timeout(parent, 20.0)
        if not reported.isdigit():
            pytest.fail(f"测试脚手架失败：孙进程未启动（parent={parent.pid} 输出={reported!r}）")
        spawned.append(int(reported))
        return parent.pid, int(reported)

    yield spawn

    for pid in reversed(spawned):
        _kill_tree(pid)


def _make_meta_dir(base: Path, name: str, pid: int, age_seconds: int) -> Path:
    """按既有落盘格式造一个带属主标记的目录（profile / lock 共用 .ra-meta 格式）。"""
    d = base / name
    d.mkdir(parents=True)
    (d / ".ra-meta").write_text(
        f"pid={pid}\nstarted_at={int(time.time()) - age_seconds}\n", encoding="utf-8"
    )
    return d


def _port_is_free(port: int) -> bool:
    """独立于被测实现的端口占用判定：能在 127.0.0.1 上 bind 即视为空闲。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _cfg():
    from research_assistant.config import Config

    return Config()


class _FakeBrowserRef:
    def __init__(self, pid: int) -> None:
        self.process_id = pid


class _FakePage:
    """假 ChromiumPage：只需浏览器 pid / 新 tab / 导航，够三个启动点跑到收尾。"""

    def __init__(self, pid: int = 4242) -> None:
        self.process_id = pid
        self.browser = _FakeBrowserRef(pid)

    def new_tab(self, background: bool = False):
        return _FakePage(self.process_id)

    def get(self, url: str) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def dp_options_env(monkeypatch):
    """把 `_build_dp_options` 的外部依赖（可执行探测 / 版本 / 代理）钉死，不依赖真实浏览器。"""
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_resolve_executable", lambda cfg: ("C:/fake/msedge.exe", "test"))
    monkeypatch.setattr(b, "_file_version", lambda exe: "152.0.4191.66")
    monkeypatch.setattr(b, "resolve_proxy", lambda u: "")
    return b


# ---------------------------------------------------------------------------
# _alloc_port：自持调试端口（DEC-030 决策 1）
# ---------------------------------------------------------------------------


def test_alloc_port_in_range_and_free():
    """oracle: specified（DEC-030 决策 1：9600–59600 内取一个空闲端口）。

    端口随机 → 只断言区间与「能 bind」，绝不写死字面量。
    """
    from research_assistant.fetch import browser as b

    port = b._alloc_port()
    assert isinstance(port, int)
    assert 9600 <= port <= 59600, f"端口越出契约区间: {port}"
    assert _port_is_free(port), f"_alloc_port 返回了被占用/未释放的端口: {port}"


def test_alloc_port_is_random_per_call():
    """oracle: specified（DEC-030 决策 1 / data-model Delta：端口由本进程自行分配，逐次独立）。

    8 次抽样只要求「不是同一个值」——正确实现全同的概率可忽略，恒定返回值的实现必然被抓。
    """
    from research_assistant.fetch import browser as b

    ports = {b._alloc_port() for _ in range(8)}
    assert len(ports) >= 2, f"8 次分配返回同一个端口，疑似写死: {ports}"


# ---------------------------------------------------------------------------
# _build_dp_options：弃 auto_port、profile 真的落到 per-call 目录（DEC-030 决策 1）
# ---------------------------------------------------------------------------


def test_build_dp_options_uses_per_call_profile_without_auto_port(dp_options_env, tmp_path):
    """oracle: specified（DEC-030 决策 1：弃 auto_port，set_user_data_path 因此真正生效）。

    `is_auto_port` 为真时 DrissionPage `handle_options` 会覆盖 --user-data-dir 到
    %TEMP%\\DrissionPage\\autoPortData\\<port>（实测 library 源码），故两条断言缺一不可。
    """
    b = dp_options_env
    profile_dir = tmp_path / "fetch-20260918-1234-abcdef"
    co = b._build_dp_options(_cfg(), profile_dir)

    assert not co.is_auto_port, "仍开着 DrissionPage auto_port —— 它会改写 profile 路径到 %TEMP%"
    assert Path(co.user_data_path) == profile_dir
    assert f"--user-data-dir={profile_dir}" in co.arguments


def test_build_dp_options_sets_debug_port_in_range(dp_options_env, tmp_path):
    """oracle: derived（DEC-030 决策 1「co.set_local_port(port)」，端口区间来自同句 9600–59600）。

    DrissionPage 的 `set_local_port` 只写 `option.address`；启动时 `connect_browser` 才把它变成
    `--remote-debugging-port=`（且 `get_launch_args` 会丢弃手写的该参数），故观察点取 address。
    """
    b = dp_options_env
    co = b._build_dp_options(_cfg(), tmp_path / "fetch-20260918-1234-abcdef")

    address = co.address or ""
    assert address.startswith("127.0.0.1:"), f"调试端口未由 set_local_port 设定（address={address!r}）"
    port = int(address.split(":")[1])
    assert 9600 <= port <= 59600, f"调试端口越出契约区间: {port}"


# ---------------------------------------------------------------------------
# _launch_page：启动失败不留孤儿（DEC-030 决策 2）
# ---------------------------------------------------------------------------


def test_launch_page_returns_page_and_browser_pid(monkeypatch, tmp_path):
    """oracle: specified（DEC-030 决策 2：三个启动点统一走 `_launch_page(config, profile_dir)`）。

    pid 的取值口径沿用既有实现（`page.browser.process_id`），故假 page 两处都给了同一个 pid。
    """
    from research_assistant.fetch import browser as b

    profile_dir = tmp_path / "fetch-20260918-1234-abcdef"
    seen: list[tuple] = []
    monkeypatch.setattr(b, "_build_dp_options", lambda cfg, d: seen.append((cfg, d)) or "OPTS")
    monkeypatch.setattr("DrissionPage.ChromiumPage", lambda opts: _FakePage(4242), raising=False)

    cfg = _cfg()
    page, browser_pid = b._launch_page(cfg, profile_dir)

    assert page is not None
    assert browser_pid == 4242
    assert seen == [(cfg, profile_dir)], "_launch_page 必须用本 call 的 config + profile_dir 建 options"


def test_launch_page_kills_profile_browsers_before_reraising(monkeypatch, tmp_path):
    """oracle: specified（DEC-030 决策 2：构造抛错时按 profile 目录名认领并杀掉已起的进程树，再原样抛出）。

    「先杀再抛」的顺序也断言：ChromiumPage 抛错后，_kill_profile_browsers 必须先于异常离开本函数。
    """
    from research_assistant.fetch import browser as b

    profile_dir = tmp_path / "fetch-20260918-1234-abcdef"
    events: list[str] = []

    class _Boom(RuntimeError):
        pass

    def _boom(opts):
        events.append("construct")
        raise _Boom("BrowserConnectError")

    monkeypatch.setattr(b, "_build_dp_options", lambda cfg, d: "OPTS")
    monkeypatch.setattr("DrissionPage.ChromiumPage", _boom, raising=False)
    monkeypatch.setattr(
        b, "_kill_profile_browsers", lambda name: events.append("kill") or 1, raising=False
    )

    with pytest.raises(_Boom):
        b._launch_page(_cfg(), profile_dir)

    assert events == ["construct", "kill"], "未在抛出前按 profile 目录名清理已起的浏览器进程"


# ---------------------------------------------------------------------------
# _close_browser：杀完等退出（DEC-030 决策 4）
# ---------------------------------------------------------------------------


def test_close_browser_waits_until_pid_really_gone(monkeypatch):
    """oracle: specified（DEC-030 决策 4：杀完 pid 后等其真正消失（有上限）再返回）。

    把 kill 换成空操作、让真子进程自己 0.4s 后退出：只有「真的等到进程消失」的实现才会在返回时
    看到子进程已退出（杀很快返回、真进程仍在的实现会在断言处被抓）。
    """
    from research_assistant.fetch import browser as b

    killed: list[int] = []
    monkeypatch.setattr(b, "_kill_pid", lambda pid: killed.append(pid))

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.4)"],
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 10
        while not _pid_exists(child.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _pid_exists(child.pid), "测试脚手架失败：子进程未启动"

        start = time.monotonic()
        b._close_browser(None, child.pid)
        elapsed = time.monotonic() - start

        assert killed == [child.pid]
        assert child.poll() is not None, "pid 仍活着时 _close_browser 就返回了（没有等进程真正退出）"
        assert elapsed < 15, f"等待无上限（{elapsed:.1f}s），失败即报错不应长等"
    finally:
        if child.poll() is None:
            child.kill()


# ---------------------------------------------------------------------------
# _profile_browser_pids / _kill_profile_browsers：按 profile 目录名认领进程树（DEC-030 决策 3）
# ---------------------------------------------------------------------------


def test_profile_browser_pids_matches_cmdline_and_returns_roots_only(cmdline_procs, tmp_path):
    """oracle: specified（DEC-030 决策 3：按命令行引用该目录名认领浏览器进程；只取根进程）。

    真进程验证：目标 profile 的父 + 孙进程命令行都含该目录名，父的父不在匹配集里 →
    期望恰好 [父 pid]；另一组（命令行带别的 profile 目录名）不得混入。
    """
    from research_assistant.fetch import browser as b

    target = tmp_path / "fetch-20260918-target-aaaaaa"
    target.mkdir()
    parent_pid, grandchild_pid = cmdline_procs(target)

    other = tmp_path / "fetch-20260918-other-bbbbbb"
    other.mkdir()
    other_parent_pid, _ = cmdline_procs(other)

    pids = b._profile_browser_pids(target.name)

    assert pids == [parent_pid], f"期望只返回根进程；实际 {pids}（孙进程 {grandchild_pid} 不该在）"
    assert other_parent_pid not in pids, "混入了别的 profile 名下的进程"


def test_kill_profile_browsers_kills_via_kill_pid_on_roots_only(
    monkeypatch, cmdline_procs, tmp_path
):
    """oracle: specified（DEC-030 决策 3：只杀根进程，`/T` 带子树；杀走既有 `_kill_pid`）。"""
    from research_assistant.fetch import browser as b

    target = tmp_path / "fetch-20260918-kill-cccccc"
    target.mkdir()
    parent_pid, grandchild_pid = cmdline_procs(target)

    killed: list[int] = []
    monkeypatch.setattr(b, "_kill_pid", lambda pid: killed.append(pid))

    reaped = b._kill_profile_browsers(target.name)

    assert killed == [parent_pid], f"应对根进程调 _kill_pid（子树交给 /T）；实际 {killed}"
    assert grandchild_pid not in killed
    assert reaped >= 1


def test_kill_profile_browsers_really_kills_matching_trees(cmdline_procs, tmp_path):
    """oracle: specified（DEC-030 决策 3：杀的是「进程树」，子进程不残留）。"""
    from research_assistant.fetch import browser as b

    target = tmp_path / "fetch-20260918-real-dddddd"
    target.mkdir()
    parent_pid, grandchild_pid = cmdline_procs(target)

    reaped = b._kill_profile_browsers(target.name)
    assert reaped >= 1

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (
        _pid_exists(parent_pid) or _pid_exists(grandchild_pid)
    ):
        time.sleep(0.1)
    assert not _pid_exists(parent_pid), f"根进程 {parent_pid} 未被杀"
    assert not _pid_exists(grandchild_pid), f"子进程 {grandchild_pid} 残留（未做进程树杀）"


# ---------------------------------------------------------------------------
# cleanup_orphans / cleanup_browser_locks：按 profile 清孤儿，不对已死 pid 发 taskkill（DEC-030 决策 3）
# ---------------------------------------------------------------------------


def test_cleanup_orphans_kills_profile_tree_and_never_taskkills_dead_owner(monkeypatch, home):
    """oracle: specified（DEC-030 决策 3 + Context：属主已死且超期的 profile 目录 → 先杀命令行引用该
    目录名的进程树，再删目录；绝不 taskkill 已死属主 pid）。

    三个目录做边界对照：超期+属主死（该清）、未超期+属主死（宽限内不动）、超期+属主活（在用不动）。
    杀进程走 `_kill_pid`（既有的 `_kill_profile_browsers` 也是经它落地），故这里拦截 `_kill_pid`
    与 `_profile_browser_pids` 即可覆盖「删前先按 profile 名清进程树」。
    """
    from research_assistant.config import profiles_dir
    from research_assistant.fetch import browser as b

    base = profiles_dir()
    base.mkdir(parents=True, exist_ok=True)
    stale = _make_meta_dir(base, "fetch-stale-aaaaaa", pid=999999, age_seconds=b.ORPHAN_AGE_SECONDS + 100)
    fresh = _make_meta_dir(base, "fetch-fresh-bbbbbb", pid=999999, age_seconds=5)
    live = _make_meta_dir(base, "fetch-live-cccccc", pid=os.getpid(), age_seconds=b.ORPHAN_AGE_SECONDS + 100)

    located: list[str] = []
    killed: list[int] = []
    monkeypatch.setattr(
        b, "_profile_browser_pids", lambda name: located.append(name) or [1111, 2222], raising=False
    )
    monkeypatch.setattr(b, "_kill_pid", lambda pid: killed.append(pid))

    reaped = b.cleanup_orphans()

    assert reaped == 1
    assert not stale.exists(), "超期孤儿 profile 目录未清"
    assert fresh.exists() and live.exists(), "宽限内 / 属主仍活的目录被误清"
    assert located == ["fetch-stale-aaaaaa"], f"未按 profile 目录名定位进程: {located}"
    assert set(killed) == {1111, 2222}, f"未杀掉该 profile 名下的浏览器进程树: {killed}"
    assert 999999 not in killed, "对已死属主 pid 发了 taskkill（Windows PID 复用会误杀无关进程树）"


def test_cleanup_browser_locks_removes_stale_lock_without_taskkill(monkeypatch, home):
    """oracle: specified（DEC-030 决策 3：`cleanup_browser_locks` 不再对已死 pid 发 taskkill，只删锁目录）。"""
    from research_assistant.fetch import browser as b

    base = b._browser_locks_dir()
    stale = _make_meta_dir(base, "lock-stale-aaaaaa", pid=999999, age_seconds=b.ORPHAN_AGE_SECONDS + 100)
    fresh = _make_meta_dir(base, "lock-fresh-bbbbbb", pid=999999, age_seconds=5)
    live = _make_meta_dir(base, "lock-live-cccccc", pid=os.getpid(), age_seconds=b.ORPHAN_AGE_SECONDS + 100)

    killed: list[int] = []
    monkeypatch.setattr(b, "_kill_pid", lambda pid: killed.append(pid))

    reaped = b.cleanup_browser_locks()

    assert reaped == 1
    assert not stale.exists(), "超期死锁目录未清"
    assert fresh.exists() and live.exists(), "宽限内 / 属主仍活的锁目录被误清"
    assert killed == [], f"cleanup_browser_locks 不该发任何 taskkill；实际杀了 {killed}"


# ---------------------------------------------------------------------------
# 三个启动点统一走 _launch_page（DEC-030 决策 2）
# ---------------------------------------------------------------------------


def _patch_launch_scaffold(monkeypatch, module, launched: list):
    """把启动前后的杂事钉死，只留「启动走了谁」这一个观察点。"""
    # 启动点统一走 _launch_page 后，模块不再需要暴露 _build_dp_options（raising=False 兼容两者）
    monkeypatch.setattr(module, "_build_dp_options", lambda cfg, d: "OPTS", raising=False)
    monkeypatch.setattr(module, "_write_suppress_prefs", lambda d: None, raising=False)
    monkeypatch.setattr(module, "_inject_cookies_dp", lambda p, c: None, raising=False)
    monkeypatch.setattr(module, "_close_browser", lambda p, pid: None)
    # 当前实现直接调 ChromiumPage：钉住它，红跑时不会真起浏览器
    monkeypatch.setattr("DrissionPage.ChromiumPage", lambda opts: _FakePage(777), raising=False)
    monkeypatch.setattr(
        module, "_launch_page", lambda cfg, d: launched.append(d) or (_FakePage(777), 777), raising=False
    )


def test_fetch_single_sync_launches_via_launch_page(monkeypatch, home):
    """oracle: specified（DEC-030 决策 2：fetch 单 url 启动点走 `_launch_page`）。"""
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_new_profile_dir", lambda: home / "p")
    monkeypatch.setattr(b, "_acquire_browser_slot", lambda cfg: home / "l")
    monkeypatch.setattr(b, "_release_browser_slot", lambda d: None)
    monkeypatch.setattr(b, "_safe_rmtree", lambda d: None)
    monkeypatch.setattr(b, "_fetch_one_tab", lambda tab, url, fmt: "md")
    launched: list = []
    _patch_launch_scaffold(monkeypatch, b, launched)

    assert b._fetch_single_sync(_cfg(), "http://x", [], "markdown") == "md"
    assert launched == [home / "p"], "fetch 单 url 未走 _launch_page"


def test_fetch_all_tabs_sync_launches_via_launch_page(monkeypatch, home):
    """oracle: specified（DEC-030 决策 2：fetch 多 tab 启动点走 `_launch_page`）。"""
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_new_profile_dir", lambda: home / "p")
    monkeypatch.setattr(b, "_acquire_browser_slot", lambda cfg: home / "l")
    monkeypatch.setattr(b, "_release_browser_slot", lambda d: None)
    monkeypatch.setattr(b, "_safe_rmtree", lambda d: None)
    monkeypatch.setattr(b, "_fetch_one_tab", lambda tab, url, fmt: "md")
    launched: list = []
    _patch_launch_scaffold(monkeypatch, b, launched)

    out = b._fetch_all_tabs_sync(_cfg(), ["http://x"], [], 1, "markdown", 5.0)
    assert out == {"http://x": "md"}
    assert launched == [home / "p"], "fetch 多 tab 未走 _launch_page"


def test_run_engine_sync_launches_via_launch_page(monkeypatch, home):
    """oracle: specified（DEC-030 决策 2：browser search 启动点走 `_launch_page`）。"""
    from research_assistant.fetch import search_engine as se

    monkeypatch.setattr(se, "_channel_executable", lambda channel: "C:/fake/msedge.exe")
    monkeypatch.setattr(se, "_new_profile_dir", lambda: home / "p")
    monkeypatch.setattr(se, "_acquire_browser_slot", lambda cfg: home / "l")
    monkeypatch.setattr(se, "_release_browser_slot", lambda d: None)
    monkeypatch.setattr(se, "_safe_rmtree", lambda d: None)
    monkeypatch.setattr(se, "_wait_network_idle", lambda tab, **k: None)
    monkeypatch.setattr(se.cfbypass, "is_cf_challenge", lambda tab: False)
    monkeypatch.setattr(se, "_extract_results", lambda tab, engine: [])
    monkeypatch.setattr(se, "_extract_page_links", lambda tab, engine: [])
    launched: list = []
    _patch_launch_scaffold(monkeypatch, se, launched)

    out = se._run_engine_sync(_cfg(), "python asyncio", "bing-intl", 1, 1, timeout=5)

    assert out == []
    assert launched == [home / "p"], "browser search 未走 _launch_page"
