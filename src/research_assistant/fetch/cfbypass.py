"""Cloudflare 绕过（DrissionPage + 默认 Edge/Chromium）。

不换浏览器内核（仍在用户的 Edge/Chromium 内，cookie 不跨内核）。配方：
  1. DrissionPage 直接 CDP 驱动用户 Edge，无 playwright 注入痕迹（不经自动化框架运行时）。
  2. headless 启动（--headless=new）；UA 必须是本机真实 UA（不含 "Headless" 标记，DEC-028），
     否则 UA 头与 client hints 矛盾会被 CF 拦。
  3. DOM.getDocument(depth=-1, pierce=True) 穿透 closed shadow root + 嵌套 iframe，
     定位 CF Turnstile checkbox 的 backendNodeId。DrissionPage 的 ele() 底层用
     DOM.performSearch(includeUserAgentShadowDOM=True)，只穿透 UA shadow DOM，看不到
     author/closed shadow root 里的 checkbox（CF Turnstile 的 checkbox 正藏在这种 shadow
     root 内）；pierce=True 让 CDP 跨过 closed shadow root，是定位 checkbox 的关键。
  4. camoufox 风格 Bezier 轨迹 humanize 点击（移植自 HumanizeMouseTrajectory 算法）：
     随机落点（rect 25%-75%）+ 随机停顿 + 按下→松开人类时长 + Bezier 鼠标移动轨迹。
  5. 通过判定：标题脱离 "just a moment"/"请稍候"。cf_clearance cookie 单独出现不代表
     通过——CF 可能在验证中发放它、随后又因识别到自动化点击而重新挑战，唯一可靠判据
     是标题不再含挑战特征。

本模块操作 DrissionPage ChromiumPage（同步 API）；browser.py 用 asyncio.to_thread 在
async 上下文中调用。作为 fetch 浏览器路径的 CF 兜底：headless 抓取遇 CF 挑战 → 本模块定位并解题。
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any

from DrissionPage._elements.chromium_element import ChromiumElement

logger = logging.getLogger("research_assistant.cfbypass")

# 窗口尺寸（launch 时 --window-size）；humanize 起点随机范围据此限定
REAL_VIEWPORT = {"width": 1440, "height": 900}


# ---------------------------------------------------------------------------
# 挑战检测
# ---------------------------------------------------------------------------


def cf_title_active(title: str | None) -> bool:
    """标题是否含 CF 挑战特征。纯字符串判定，不依赖浏览器库——playwright 主路径与
    DrissionPage 兜底路径共用此函数。"""
    t = title or ""
    return "just a moment" in t.lower() or "请稍候" in t


def is_cf_challenge(page: Any) -> bool:
    """检测 DrissionPage 是否处于 CF 挑战页（5 秒盾 / Turnstile）。"""
    try:
        title = page.title or ""
    except Exception:
        title = ""
    if cf_title_active(title):
        return True
    # cf-turnstile-response 输入框出现 = 页面嵌了 Turnstile widget（即便标题还没切到挑战态）
    try:
        if page.ele('css:input[name="cf-turnstile-response"]', timeout=1):
            return True
    except Exception:
        pass
    return False


def detect_challenge_type(page: Any) -> str | None:
    """检测 CF challenge 类型（页面 HTML 里的 cType 字段）。

    返回 "non-interactive" / "managed" / "interactive" / "embedded" / None。
    non-interactive 是无感验证（后台 JS 自动跑），不需点击；其余需点 Turnstile。
    """
    try:
        content = page.html or ""
    except Exception:
        return None
    for ctype in ("non-interactive", "managed", "interactive"):
        if f"cType: '{ctype}'" in content:
            return ctype
    if "challenges.cloudflare.com/turnstile/v" in content:
        return "embedded"
    return None


# ---------------------------------------------------------------------------
# 定位 CF frame + 穿透 shadow root 找 checkbox
# ---------------------------------------------------------------------------


def _find_cf_frame(page: Any, timeout: float = 12.0) -> Any:
    """轮询直到 Cloudflare challenge frame 出现，超时返回 None。

    DrissionPage get_frame 底层 DOM.performSearch 能定位跨域 iframe 元素本身；
    但 frame 内的 checkbox 在 closed shadow root 里，要靠 find_turnstile_checkboxes 穿透。
    """
    end = time.monotonic() + timeout
    last_err: Exception | None = None
    loc = "css:iframe[src*='challenges.cloudflare.com']"
    while time.monotonic() < end:
        try:
            frame = page.get_frame(loc, timeout=2)
            if frame:
                logger.info("CF frame 找到: %s", (getattr(frame, "url", "") or "")[:100])
                return frame
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    logger.warning("CF frame %.0fs 内未出现%s", timeout, f"（{last_err}）" if last_err else "")
    return None


def _attrs_dict(node: dict) -> dict:
    a = node.get("attributes") or []
    return {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}


def _walk_find_checkbox(node: dict, path: list[str], out: list[dict]) -> None:
    """递归遍历 pierce=True 的 DOM 树，找 input[type=checkbox] / [role=checkbox]。
    下钻 children / shadowRoots / contentDocument（嵌套 iframe），pierce 已展平它们。"""
    if not isinstance(node, dict):
        return
    ln = (node.get("localName") or "").lower()
    ad = _attrs_dict(node)
    is_cb = (ln == "input" and (ad.get("type") or "").lower() == "checkbox") or (
        ad.get("role") or ""
    ).lower() == "checkbox"
    if is_cb and "backendNodeId" in node:
        out.append({"backendNodeId": node["backendNodeId"], "path": " > ".join(path) or "<root>"})
    seg = f'{ln}#{ad["id"]}' if ad.get("id") else (ln or "node")
    for c in node.get("children") or []:
        _walk_find_checkbox(c, path + [seg], out)
    for sr in node.get("shadowRoots") or []:
        _walk_find_checkbox(sr, path + [f"#{sr.get('shadowRootType') or 'shadow'}"], out)
    cd = node.get("contentDocument")
    if cd:
        _walk_find_checkbox(cd, path + ["iframe-doc"], out)


def find_turnstile_checkboxes(frame: Any) -> list[dict]:
    """用 DOM.getDocument(depth=-1, pierce=True) 拿穿透 closed shadow root + 嵌套 iframe
    的完整树，递归找 checkbox。返回候选列表 [{backendNodeId, path}]。"""
    try:
        root = frame.run_cdp("DOM.getDocument", depth=-1, pierce=True)["root"]
    except Exception as e:
        logger.warning("DOM.getDocument(pierce) 失败: %s", e)
        return []
    out: list[dict] = []
    _walk_find_checkbox(root, [], out)
    return out


# ---------------------------------------------------------------------------
# camoufox 风格 humanize 点击（移植自 HumanizeMouseTrajectory 算法）
# ---------------------------------------------------------------------------


def _bezier_point(points: list[tuple[float, float]], t: float) -> tuple[float, float]:
    """Bernstein 多项式求 Bezier 曲线上 t 处的点。"""
    n = len(points) - 1
    x = y = 0.0
    for i, (px, py) in enumerate(points):
        b = math.comb(n, i) * (t ** i) * ((1 - t) ** (n - i))
        x += px * b
        y += py * b
    return x, y


def _humanize_path(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    """生成人类化鼠标轨迹：Bezier(2 个随机内部 control knots，±80 边界) + 中间点正态抖动
    (mean=1,std=1,freq=0.5) + easeOutQuad 缓动 tween（距离自适应点数，上限 150）。
    每次调用轨迹形状都不同，避免恒定直线/恒速的机械化特征。"""
    (fx, fy), (tx, ty) = start, end
    left, right = min(fx, tx) - 80.0, max(fx, tx) + 80.0
    down, up = min(fy, ty) - 80.0, max(fy, ty) + 80.0
    knots = [(random.uniform(left, right), random.uniform(down, up)) for _ in range(2)]
    control = [start] + knots + [end]
    mid = int(max(abs(fx - tx), abs(fy - ty), 2))
    curve = [_bezier_point(control, i / (mid - 1)) for i in range(mid)]
    # 正态抖动中间点（模拟手部微抖动；首尾不动，保证起止精确）
    distorted = [curve[0]]
    for i in range(1, len(curve) - 1):
        x, y = curve[i]
        if random.random() < 0.5:
            y += round(random.gauss(1.0, 1.0))
        distorted.append((x, y))
    distorted.append(curve[-1])
    # tween：easeOutQuad（开始快、结尾慢，像人手停下）+ 距离自适应点数
    total = sum(math.dist(distorted[i], distorted[i + 1]) for i in range(len(distorted) - 1))
    target = min(150, max(2, int(total ** 0.25 * 20)))
    n = len(distorted) - 1
    out = []
    for i in range(target):
        t = i / (target - 1)
        eased = -t * (t - 2)  # easeOutQuad
        out.append(distorted[min(n, int(eased * n))])
    return out


def _rand_sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _humanize_click(page: Any, frame_ele: Any, cb: ChromiumElement) -> None:
    """人类化点击 checkbox：Bezier 轨迹把鼠标移到目标附近（页坐标）+ 随机落点 +
    按下→松开人类时长。全程在 PAGE target 上 dispatch Input（move 跨整个页面到 frame
    位置，故不能用 frame target）。"""
    (fx, fy), *_ = frame_ele.rect.viewport_corners   # iframe 在页面视口左上角
    (bx, by), *_ = cb.rect.viewport_corners           # checkbox 在 frame 视口左上角
    w, h = cb.rect.size
    # 随机落点：rect 中部 25%-75% 区域，避免每次都点正中（机械化特征）
    tx = fx + bx + random.uniform(0.25, 0.75) * w
    ty = fy + by + random.uniform(0.30, 0.70) * h
    # 起点：视口内随机位置（模拟鼠标当前已在页面上，需走轨迹过去）
    sx = random.randint(120, max(121, REAL_VIEWPORT["width"] - 120))
    sy = random.randint(120, max(121, REAL_VIEWPORT["height"] - 120))
    logger.info("humanize: start=(%d,%d) target=(%.0f,%.0f) cb_size=(%.0f,%.0f)", sx, sy, tx, ty, w, h)
    # Bezier 轨迹逐点 mouseMoved + 小随机间隔（模拟人手移动速度不均）
    for mx, my in _humanize_path((sx, sy), (tx, ty)):
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=round(mx, 1), y=round(my, 1))
        time.sleep(random.uniform(0.002, 0.012))
    # 到达后的人类停顿（看到目标→决定点击）
    time.sleep(random.uniform(0.08, 0.25))
    page.run_cdp("Input.dispatchMouseEvent", type="mousePressed", x=tx, y=ty, button="left", clickCount=1)
    # 按下→松开的人类时长（对抗瞬时点击检测，真人按下会有几十~百毫秒停顿）
    time.sleep(random.uniform(0.04, 0.12))
    page.run_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=tx, y=ty, button="left", clickCount=1)


# ---------------------------------------------------------------------------
# 解题主流程
# ---------------------------------------------------------------------------


def try_click_turnstile(page: Any) -> bool:
    """定位 CF Turnstile checkbox 并 humanize 点击。主路径 page-level Bezier 轨迹点击，
    回退 DrissionPage cb.click()（frame target 物理点击）。任一命中返回 True。"""
    _rand_sleep(1.0, 2.5)  # 人类"看到页面后思考"
    frame = _find_cf_frame(page, timeout=12.0)
    if not frame:
        logger.warning("未找到 CF frame")
        return False
    cbs = find_turnstile_checkboxes(frame)
    if not cbs:
        logger.warning("CF frame 内未找到 checkbox（pierce 未命中）")
        return False
    logger.info("pierced 找到 %d 个 checkbox 候选", len(cbs))
    cb = ChromiumElement(frame, backend_id=cbs[0]["backendNodeId"])
    try:
        logger.info("checkbox size=%s", cb.rect.size)
    except Exception:
        pass
    # 主路径：humanize Bezier 轨迹点击（PAGE target）
    try:
        _humanize_click(page, frame.frame_ele, cb)
        logger.info("[humanize] 点击完成")
        return True
    except Exception as e:
        logger.warning("[humanize] 失败: %s，回退 cb.click()", e)
    # 回退：DrissionPage Clicker.left（frame target 上 Input.dispatchMouseEvent）
    try:
        cb.click()
        logger.info("[回退] cb.click() 完成")
        return True
    except Exception as e2:
        logger.warning("[回退] cb.click() 也失败: %s", e2)
        return False


def _wait_challenge_cleared(page: Any, timeout: float = 12.0) -> bool:
    """点击后循环等标题脱离 CF 挑战态。cf_clearance cookie 单独出现不代表通过——CF 可能
    在验证中发放它、随后又重新挑战。唯一可靠判据是标题不再含 "just a moment"/"请稍候"。"""
    end = time.monotonic() + timeout
    last_title: str | None = None
    while time.monotonic() < end:
        try:
            title = page.title or ""
        except Exception:
            title = "(n/a)"
        if not cf_title_active(title):
            return True
        if title != last_title:
            logger.info("等待通过: title=%r", title)
            last_title = title
        time.sleep(0.5)
    return not cf_title_active(getattr(page, "title", "") or "")


def solve(page: Any, max_attempts: int = 5) -> bool:
    """对 DrissionPage 跑 CF 解题。无挑战返回 True；点击后等验证完成；通过返回 True。

    返回 False 表示未能通过（CF 可能因指纹/IP 仍拒绝，调用方据此报 AntibotError）。
    """
    if not is_cf_challenge(page):
        logger.info("无 CF 挑战，solve 直接返回 True")
        return True

    ctype = detect_challenge_type(page)
    logger.info("CF 挑战 type=%s，开始解题（max_attempts=%d）", ctype, max_attempts)

    if ctype == "non-interactive":
        # non-interactive：CF 后台 JS 自动验证，无需点击。强行点击反而被 CF 判可疑 → 重新挑战。
        logger.info("non-interactive: 等后台 JS 验证（不点击），最多 30s")
        return _wait_challenge_cleared(page, timeout=30.0)

    for attempt in range(max_attempts):
        logger.info("==== attempt %d/%d ====", attempt + 1, max_attempts)
        if not try_click_turnstile(page):
            logger.info("本轮未点击成功，等 CF frame/checkbox 加载后重试")
            _rand_sleep(2.0, 3.5)
            continue
        if _wait_challenge_cleared(page, timeout=12.0):
            logger.info("CF 通过（attempt %d）", attempt + 1)
            return True
        logger.warning("attempt %d 后仍在挑战态 → 重试", attempt + 1)

    final = not cf_title_active(getattr(page, "title", "") or "")
    logger.warning("达到 max_attempts=%d，最终通过: %s", max_attempts, final)
    return final
