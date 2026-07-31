"""浏览器抓取：1 浏览器多 tab + headed + CF auto-detect + 网络静默等待。

fetch 浏览器层与 browser 平台共用。1 个 ChromiumPage(browser)+ N tab（每 url 一个），共享
cookie（注一次），ThreadPoolExecutor 并发各 tab：导航 → cfbypass.solve（无 CF 秒过，有 CF 解题）
→ _wait_network_idle（网络稳定：idle 静默 / 稳态 polling / timeout 兜底）→ html2md。
启动前清孤儿 profile + browser 锁（崩溃自愈）。浏览器进程数受 config.browser.max_browser_instances
跨进程限流（.browser-locks/ PID 标记）。

演进：去 headless（2026-07，统一 headed+CF auto-detect）；多 tab（2026-07，原每 url 独立浏览器
改 1 browser 多 tab 省内存）；网络静默等待（2026-07，替代 html 长度轮询，JS/API 加载更稳）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from .. import config as config_mod
from ..config import Config
from ..errors import AntibotError, ConfigError, ResearchAssistantError
from ..proxy import resolve_proxy
from .html2md import html_to_md
from . import cfbypass

logger = logging.getLogger("research_assistant.fetch.browser")

# 孤儿判定：profile/锁 超过该秒数且属主进程已死 → 视为孤儿
ORPHAN_AGE_SECONDS = 600


# ---------------------------------------------------------------------------
# channel 可执行探测
# ---------------------------------------------------------------------------


def _channel_executable(channel: str) -> str | None:
    """返回本地浏览器可执行路径（用于探测），找不到返回 None。"""
    if sys.platform.startswith("win"):
        candidates = {
            "msedge": [
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ],
            "chrome": [
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ],
        }
    elif sys.platform == "darwin":
        candidates = {
            "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
            "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        }
    else:
        candidates = {
            "msedge": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
            "chrome": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"],
        }
    for path in candidates.get(channel, []):
        if path and os.path.exists(path):
            return path
    return None


def _resolve_executable(config: Config) -> tuple[str | None, str]:
    """返回 (可执行路径或 None, 来源描述)。

    executable_path 配置且文件存在 → 用它（覆盖 channel 探测，支持非标准安装位置）；
    否则按 channel 探测标准安装路径。来源描述用于 doctor/browser_probe 的诊断信息。
    """
    configured = config.browser.executable_path
    if configured:
        src = f"executable_path={configured}"
        return (configured if os.path.exists(configured) else None), src
    return _channel_executable(config.browser.channel), f"channel={config.browser.channel}"


def browser_probe(config: Config) -> tuple[bool, str]:
    """探测浏览器可执行就绪性（doctor 用）。返回 (是否可用, 诊断信息)。

    返回 bool 而非让调用方靠「"可用" in 文案」判断——诊断文案里"不可用"含"可用"子串，
    子串匹配会把不可用误判成可用。
    """
    exe, src = _resolve_executable(config)
    if exe:
        return True, f"{src} 可用"
    if config.browser.executable_path:
        return False, f"{src} 路径不存在（fetch 回退将不可用）"
    return False, f"{src} 未找到本地浏览器可执行（fetch 回退将不可用）"


# ---------------------------------------------------------------------------
# per-call profile + 原子 id（ADR-0005）
# ---------------------------------------------------------------------------


def _new_profile_dir() -> Path:
    """生成一个全新的 per-call profile 目录（原子 id，防并发碰撞）。"""
    base = config_mod.profiles_dir()
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(50):
        pid = os.getpid()
        ident = f"{int(time.time())}-{pid}-{uuid.uuid4().hex[:6]}"
        d = base / f"fetch-{ident}"
        # 原子创建：不存在才建（os.makedirs exist_ok=False + 捕获）
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        # 写属主标记
        (d / ".ra-meta").write_text(
            f"pid={pid}\nstarted_at={int(time.time())}\n", encoding="utf-8"
        )
        return d
    raise ResearchAssistantError("无法分配独立 profile 目录（重试耗尽）")


def _parse_meta(d: Path) -> tuple[int, int] | None:
    meta = d / ".ra-meta"
    if not meta.exists():
        return None
    pid, started = 0, 0
    for line in meta.read_text(encoding="utf-8").splitlines():
        if line.startswith("pid="):
            pid = int(line.split("=", 1)[1].strip() or "0")
        elif line.startswith("started_at="):
            started = int(line.split("=", 1)[1].strip() or "0")
    return pid, started


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform.startswith("win"):
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _kill_pid(pid: int) -> None:
    try:
        if sys.platform.startswith("win"):
            os.system(f"taskkill /PID {pid} /T /F >nul 2>&1")
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def cleanup_orphans() -> int:
    """扫描 profile 目录，清理孤儿（属主进程已死 且 超过 ORPHAN_AGE_SECONDS）。

    搬 cdt 三件套之"启动前孤儿清理"——崩溃后下次 fetch 自愈。返回清理数量。
    """
    base = config_mod.profiles_dir()
    if not base.exists():
        return 0
    now = int(time.time())
    reaped = 0
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("fetch-"):
            continue
        meta = _parse_meta(d)
        if meta is None:
            # 无标记且足够旧 → 清掉
            try:
                age = now - int(d.stat().st_mtime)
            except OSError:
                continue
            if age > ORPHAN_AGE_SECONDS:
                _safe_rmtree(d)
                reaped += 1
            continue
        pid, started = meta
        # 属主进程还活着 → 不动（正在用的）
        if _pid_alive(pid):
            continue
        # 属主已死：若已超期则清理（刚崩的给一点宽限，避免误伤正在退出的）
        if (now - started) > ORPHAN_AGE_SECONDS:
            _kill_pid(pid)
            _safe_rmtree(d)
            reaped += 1
    return reaped


def _safe_rmtree(d: Path) -> None:
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 浏览器进程数限流（跨 CLI，config.browser.max_browser_instances）
# ---------------------------------------------------------------------------


def _browser_locks_dir() -> Path:
    """browser 进程锁目录（config_dir/.browser-locks/）。每个活跃 browser 一个 lock-*/ 标记。"""
    d = config_mod.config_dir() / ".browser-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_browser_locks() -> int:
    """清 .browser-locks/ 下死 PID 锁（崩溃自愈，复用 _pid_alive/_kill_pid）。返回清理数。"""
    base = _browser_locks_dir()
    now = int(time.time())
    reaped = 0
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("lock-"):
            continue
        meta = _parse_meta(d)
        if meta is None:
            continue
        pid, started = meta
        if _pid_alive(pid):
            continue
        if (now - started) > ORPHAN_AGE_SECONDS:
            _kill_pid(pid)
            _safe_rmtree(d)
            reaped += 1
    return reaped


def _acquire_browser_slot(config: Config) -> Path:
    """获取一个 browser 进程槽（跨 CLI 限流）。返回锁标记目录。

    先 cleanup_browser_locks 清死锁，再数活 PID 锁；>= max_browser_instances 抛 ConfigError。
    """
    cleanup_browser_locks()
    base = _browser_locks_dir()
    alive = 0
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith("lock-"):
            continue
        meta = _parse_meta(d)
        if meta is None:
            continue
        if _pid_alive(meta[0]):
            alive += 1
    max_inst = config.browser.max_browser_instances
    if alive >= max_inst:
        raise ConfigError(
            f"浏览器进程数已达上限 {max_inst}（有并发 fetch/search 在跑）",
            details={"hint": "调高 config [browser] max_browser_instances，或等现有任务结束"},
        )
    pid = os.getpid()
    for _ in range(50):
        ident = f"{int(time.time())}-{pid}-{uuid.uuid4().hex[:6]}"
        d = base / f"lock-{ident}"
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        (d / ".ra-meta").write_text(
            f"pid={pid}\nstarted_at={int(time.time())}\n", encoding="utf-8"
        )
        return d
    raise ResearchAssistantError("无法分配 browser 锁目录（重试耗尽）")


def _release_browser_slot(lock_dir: Path | None) -> None:
    if lock_dir is not None:
        _safe_rmtree(lock_dir)


# ---------------------------------------------------------------------------
# 登录 cookie / DrissionPage 选项 / 防弹窗
# ---------------------------------------------------------------------------


async def _get_login_cookies(config: Config) -> list[dict[str, Any]]:
    if config.browser.extension_status != "installed":
        return []
    try:
        from ..loginstate import get_cookies, to_playwright_cookies

        raw = await get_cookies(config)
        return to_playwright_cookies(raw)
    except Exception:
        # 登录态不可用不阻塞 fetch（按公开页处理）
        return []


def _build_dp_options(config: Config, profile_dir: Path, *, headless: bool = False) -> Any:
    """构造 DrissionPage ChromiumOptions：用户 Edge + 隔离 profile + 反检测/防弹窗 args。

    headless=False（默认，fetch/CF 路径）：CF 识别 headless，必须 headed。
    headless=True（search_engine 用）：结果页无 CF 挑战，headless 更快且不打扰用户。
    DrissionPage 直接 CDP 驱动用户 Edge，不经自动化框架运行时，无 playwright 注入痕迹。
    防弹窗照 cdt 配方（launch args + 后续 _write_suppress_prefs 的 Preferences 双保险）。
    """
    from DrissionPage import ChromiumOptions

    exe, _ = _resolve_executable(config)
    co = ChromiumOptions()
    co.set_browser_path(exe)
    co.set_user_data_path(str(profile_dir))
    co.headless(headless)
    co.auto_port(True)
    w, h = cfbypass.REAL_VIEWPORT["width"], cfbypass.REAL_VIEWPORT["height"]
    co.set_argument(f"--window-size={w},{h}")
    # 启动即挪到屏外（与启动后 _hide_window 双保险）：消除 ChromiumPage 构造到 hide 之间的主窗口闪现。
    co.set_argument("--window-position=-32000,-32000")
    # 设 UI 语言为英语：隔离 profile 抓的多是英文页（CF 挑战页等），Edge 见"页面语言=用户语言"就不弹翻译框。
    # 比 --disable-features=Translate 更可靠——后者实测仍弹（Edge 翻译弹窗不完全受 Chromium Translate feature 控制）。
    co.set_argument("--lang=en-US")
    # 翻译弹窗根治：DrissionPage 默认带 --disable-features=PrivacySandboxSettings4，与 Translate
    # 冲突（Chrome 只认最后一个 --disable-features），先移除默认再设合并值，一次杀翻译+隐私沙盒+Edge 欢迎页。
    co.remove_argument("--disable-features=PrivacySandboxSettings4")
    co.set_argument("--disable-features=Translate,TranslateUI,PrivacySandboxSettings4,msEdgeWelcomeFLX")
    co.set_argument("--disable-blink-features=AutomationControlled")  # 反 webdriver 指纹
    co.set_argument("--disable-extensions")    # 禁扩展，消除 Edge 扩展安装/引导页
    co.set_argument("--no-experiments")
    co.set_argument("--disable-sync")          # 禁同步引擎，配合 Preferences 抑制同步弹窗
    proxy_url = resolve_proxy(config.proxy.url)
    if proxy_url:
        co.set_argument(f"--proxy-server={proxy_url}")
    return co


def _inject_cookies_dp(page: Any, cookies: list[dict[str, Any]]) -> None:
    """用 CDP Network.setCookie 注入 cookie。playwright cookie 字段名与 CDP setCookie 参数
    一致，直接透传（绕过 DrissionPage set.cookies 的格式差异）。失败不阻塞 CF 解题。"""
    if not cookies:
        return
    ok = 0
    for c in cookies:
        try:
            params: dict[str, Any] = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path") or "/",
            }
            if c.get("secure"):
                params["secure"] = True
            if c.get("httpOnly"):
                params["httpOnly"] = True
            if c.get("expires", -1) >= 0:
                params["expires"] = c["expires"]
            ss = (c.get("sameSite") or "").lower()
            if ss in ("strict", "lax", "none"):
                params["sameSite"] = ss.capitalize()
            page.run_cdp("Network.setCookie", **params)
            ok += 1
        except Exception:
            pass
    logger.info("DrissionPage 注入 cookie %d/%d", ok, len(cookies))


def _write_suppress_prefs(user_data_dir: Path) -> None:
    """写隔离 profile 的 Default\\Preferences，抑制翻译/下载框/权限/同步弹窗。

    两层防弹窗之一（另一层是 launch args 的 --disable-features=Translate,...）。
    - translate.enabled=false + intl.accept_languages=en-US,en：Edge 只在页面语言 != 用户语言
      时弹翻译框，CF/nopecha 挑战页都是英文，把接受语言设成 en 后不再提示（最可靠的翻译弹窗根治）。
    - signin.allowed_on_next_launch=false + sync.first_setup_complete=true：抑制 Edge 新 profile
      从 Windows 系统账户读 MSA 自动登录 + 同步确认弹窗（CF 路径不能加 --enable-automation 暴露
      webdriver，故走 Preferences）。
    读-合并-无 BOM 写回，避免破坏 profile 既有的其他偏好。
    """
    import json

    prefs_file = user_data_dir / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    prefs: dict[str, Any] = {}
    if prefs_file.exists():
        try:
            loaded = json.loads(prefs_file.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                prefs = loaded
        except Exception:
            prefs = {}
    prefs.setdefault("translate", {})["enabled"] = False
    prefs.setdefault("intl", {})["accept_languages"] = "en-US,en"
    prefs.setdefault("download", {})["prompt_for_download"] = False
    prefs.setdefault("profile", {}).setdefault("default_content_setting_values", {}).update({
        "notifications": 2, "geolocation": 2, "media_stream_camera": 2, "media_stream_mic": 2,
    })
    prefs.setdefault("signin", {})["allowed_on_next_launch"] = False
    prefs.setdefault("sync", {}).update({
        "requested": False, "first_setup_complete": True,
        "has_setup_completed": True, "keep_everything_synced": False,
    })
    prefs.setdefault("browser", {}).update({"has_seen_welcome_page": True, "check_default_browser": False})
    prefs.setdefault("profile", {})["exit_type"] = "Normal"
    try:
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
        logger.info("已写 profile Preferences 抑制弹窗: %s", prefs_file)
    except Exception as e:
        logger.warning("写 Preferences 失败（仅靠 --disable-features 兜底）: %s", e)


def _hide_window(page: Any) -> None:
    """隐藏 CF 兜底浏览器窗口（Win32 ShowWindow SW_HIDE）。

    窗口仍在（真 headed 渲染，CF 检测不到 headless），但不占视野——区别于 headless（headless 实测
    即便覆盖 UA+screen 仍被 CF 深层指纹识别）。仅 Windows + pywin32 生效；其他平台或缺依赖时
    静默跳过（窗口可见，不影响功能）。
    """
    try:
        from DrissionPage._functions.tools import show_or_hide_browser

        show_or_hide_browser(page, hide=True)
        logger.info("CF 绕过浏览器窗口已隐藏")
    except Exception as e:
        logger.debug("隐藏窗口跳过（%s）", e)


def _close_browser(page: Any, browser_pid: int) -> None:
    """关 DrissionPage 浏览器并确保进程退出（CDT 风格：显式杀进程防残留）。

    page.quit()（force=True 默认，杀 browser 进程）+ 兜底按 pid 杀进程树——防 quit 不彻底时
    Edge 进程残留致窗口不关 + profile 文件锁不释放（Windows 下 rmtree 会因锁失败，profile 堆积）。
    page 为 None（ChromiumPage 构造失败）时跳过 quit；对已退出的 pid 杀无害。
    """
    if page is not None:
        try:
            page.quit()
        except Exception as e:
            logger.debug("page.quit() 异常: %s", e)
    if browser_pid:
        _kill_pid(browser_pid)


# ---------------------------------------------------------------------------
# 网络静默等待（替代 html 长度轮询）
# ---------------------------------------------------------------------------


def _wait_network_idle(
    tab: Any,
    total_timeout: float = 10.0,
    idle_window: float = 2.0,
    poll_interval: float = 0.3,
) -> None:
    """等网络稳定，不只看归零——有些站一进去就有计数器/定时 polling，进行中永不归零。

    判稳定（任一即返）：
    - idle 静默：idle_window（2s）内无新请求发起 → 加载完成（无 polling 或 polling 间隔 >2s）。
    - 稳态 polling：最近 4 个请求间隔稳定（均值 0.3-3s 且各 ±40%）→ 定时 polling，加载完成。
    total_timeout 兜底（超时接受当前）。listen 监听请求发起事件（requestWillBeSent → DataPacket）。
    """
    try:
        tab.listen.start()  # 监听所有 URL
    except Exception as e:
        logger.debug("listen.start 失败，跳过网络等待: %s", e)
        return
    deadline = time.monotonic() + total_timeout
    last_req_time = time.monotonic()  # idle 基准：循环开始（全静默也能判 idle，不必等到首个请求）
    saw_first = False
    intervals: list[float] = []
    while time.monotonic() < deadline:
        try:
            pkt = tab.listen.wait(count=1, timeout=poll_interval, fit_count=True, raise_err=False)
        except Exception:
            pkt = None
        now = time.monotonic()
        if pkt:  # poll_interval 内有新请求发起
            if saw_first:  # 首请求只记时间不记间隔（间隔从第 2 个请求起算）
                intervals.append(now - last_req_time)
                # 稳态 polling 检测：最近 4 间隔稳定
                if len(intervals) >= 4:
                    recent = intervals[-4:]
                    avg = sum(recent) / 4
                    if 0.3 <= avg <= 3.0 and all(abs(x - avg) <= avg * 0.4 for x in recent):
                        logger.debug("网络稳态 polling（间隔均值 %.2fs），加载完成", avg)
                        return
            else:
                saw_first = True
            last_req_time = now
        else:  # poll_interval 无新请求
            if (now - last_req_time) >= idle_window:
                logger.debug("网络 idle %.1fs 无新请求，加载完成", idle_window)
                return
    logger.debug("网络等待 total_timeout %.0fs 到，接受当前", total_timeout)


# ---------------------------------------------------------------------------
# 抓取主流程：单 tab → 单 url → 多 url（1 browser 多 tab）
# ---------------------------------------------------------------------------


def _fetch_one_tab(tab: Any, url: str, fmt: str = "markdown") -> str | None:
    """单 tab 抓取：导航 → CF solve → 网络静默 → (html 直出 | html2md)。不建 browser/profile（由调用方建）。

    tab 是 ChromiumPage/ChromiumTab/MixTab（同套 API，cfbypass.solve 兼容）。
    fmt=html 时返回原始 html（绕过 trafilatura）；否则走 html2md 抽正文。
    失败（CF 未过/内容过短）抛异常，由调用方捕获记 None。
    """
    logger.info("GET %s", url)
    tab.get(url)
    time.sleep(2.5)  # 等 CF widget 渲染
    passed = cfbypass.solve(tab)
    logger.info("cfbypass.solve 返回: %s（url=%s）", passed, url)
    if not passed:
        raise AntibotError(f"CF 挑战未通过（指纹/IP 被拒）: {url}")
    _wait_network_idle(tab)
    html = tab.html or ""
    if len(html) < 200:
        raise AntibotError(f"CF 绕过后内容仍过短: {url}")
    if fmt == "html":
        return html  # 原始 html，绕过 trafilatura
    return html_to_md(html, url)


def _fetch_single_sync(config: Config, url: str, cookies: list[dict[str, Any]], fmt: str = "markdown") -> str | None:
    """单 url 同步：建 1 browser + tab0，hide + 注 cookie，_fetch_one_tab(tab0)，关 browser。"""
    from DrissionPage import ChromiumPage

    profile_dir = _new_profile_dir()
    lock_dir = _acquire_browser_slot(config)
    page = None
    browser_pid = 0
    try:
        _write_suppress_prefs(profile_dir)
        page = ChromiumPage(_build_dp_options(config, profile_dir))
        browser_pid = getattr(getattr(page, "browser", None), "process_id", 0) or 0
        _hide_window(page)
        _inject_cookies_dp(page, cookies)
        return _fetch_one_tab(page, url, fmt)  # page(ChromiumPage)即 tab0，可直接喂 _fetch_one_tab
    finally:
        _close_browser(page, browser_pid)
        _safe_rmtree(profile_dir)
        _release_browser_slot(lock_dir)


async def _fetch_with_stealth(
    config: Config, url: str, cookies: list[dict[str, Any]], fmt: str = "markdown"
) -> str | None:
    """单 url 薄包装（建 browser+tab0 调 _fetch_one_tab）。供 test_cf_live 与单 url 用。"""
    exe, src = _resolve_executable(config)
    if exe is None:
        raise ResearchAssistantError(
            f"未找到本地浏览器 ({src})，浏览器抓取不可用"
        )
    return await asyncio.to_thread(_fetch_single_sync, config, url, cookies, fmt)


def _fetch_all_tabs_sync(
    config: Config, urls: list[str], cookies: list[dict[str, Any]], concurrency: int,
    fmt: str = "markdown", timeout: float = 60.0,
) -> dict[str, str | None]:
    """多 url 1 浏览器多 tab：建 browser + tab0，hide + 注 cookie（共享存储），主线程 new_tab 建 N tab，
    ThreadPoolExecutor 并发各 tab _fetch_one_tab。单 tab 超过 timeout 秒记 None 不等待。

    线程安全：new_tab（browser 级）只主线程调；各 tab 的 get/solve/listen/html/close（tab 级独立 WS）
    在工作线程，单 tab 内顺序。

    超时清理：主线程逐 future.result(timeout)，超时的 tab 记 None；全部处理完后 finally 先
    _close_browser（杀 browser PID，让残余 worker 的 CDP 断开抛异常退出），再 shutdown 线程池，
    避免 shutdown 干等残余 worker（Python 线程不能 kill，但杀进程能连带解阻塞）。
    """
    from DrissionPage import ChromiumPage

    profile_dir = _new_profile_dir()
    lock_dir = _acquire_browser_slot(config)
    page = None
    browser_pid = 0
    ex: ThreadPoolExecutor | None = None
    try:
        _write_suppress_prefs(profile_dir)
        page = ChromiumPage(_build_dp_options(config, profile_dir))
        browser_pid = getattr(getattr(page, "browser", None), "process_id", 0) or 0
        _hide_window(page)
        _inject_cookies_dp(page, cookies)  # 注一次，所有 tab 共享

        # 主线程：预建 tab。tab0(page) 给第 1 个 url；其余 new_tab(background) 空 tab。
        tabs: list[Any] = []
        for i in range(len(urls)):
            if i == 0:
                tabs.append(page)
            else:
                tabs.append(page.new_tab(background=True))

        results: dict[str, str | None] = {}

        def work(idx_url: tuple[int, str]) -> tuple[str, str | None]:
            i, url = idx_url
            tab = tabs[i]
            try:
                md = _fetch_one_tab(tab, url, fmt)
                return url, md
            except Exception as e:
                logger.warning("tab 抓取失败 %s: %s", url, e)
                return url, None
            finally:
                if tab is not page:  # 新 tab 关闭；tab0 留给 _close_browser(page)
                    try:
                        tab.close()
                    except Exception:
                        pass

        ex = ThreadPoolExecutor(max_workers=max(1, concurrency))
        futures = {ex.submit(work, (i, url)): url for i, url in enumerate(urls)}
        for fut in futures:
            url = futures[fut]
            try:
                _, md = fut.result(timeout=timeout)
                results[url] = md
            except FutureTimeoutError:
                logger.warning("tab 抓取超时 %s（%.0fs），记失败", url, timeout)
                results[url] = None
            except Exception as e:
                logger.warning("tab 抓取异常 %s: %s", url, e)
                results[url] = None
        return results
    finally:
        # 先杀 browser：残余 worker 的 CDP 断开、抛异常退出，shutdown 才不干等
        _close_browser(page, browser_pid)
        if ex is not None:
            ex.shutdown(wait=True)
        _safe_rmtree(profile_dir)
        _release_browser_slot(lock_dir)


async def fetch_with_browser(
    config: Config,
    urls: list[str],
    *,
    login: bool = True,
    concurrency: int = 4,
    fmt: str = "markdown",
    timeout: float = 60.0,
) -> dict[str, str | None]:
    """批量用 headed 浏览器抓取（1 browser 多 tab，fetch 浏览器层与 browser fetch 共用）。

    返回 {url: content_or_None}。fmt=html 时每页返回原始 html，否则 markdown（trafilatura 抽正文）。
    单 tab 超过 timeout 秒记 None 不阻塞整批；finally 杀 browser PID 让残余 worker 解阻塞退出。
    失败（CF 未过/无浏览器/内容过短/进程数达上限）记 None 或抛 ConfigError（进程上限）。
    """
    cleanup_orphans()

    cookies: list[dict[str, Any]] = []
    if login:
        cookies = await _get_login_cookies(config)

    return await asyncio.to_thread(
        _fetch_all_tabs_sync, config, urls, cookies, concurrency, fmt, timeout
    )
