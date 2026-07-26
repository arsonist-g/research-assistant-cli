"""登录态层（ADR-0006）：内嵌 cdt 扩展桥。

- MV3 扩展（bundled，装在用户日常浏览器）用 chrome.cookies 读明文 cookie（绕 ABE）→ 推 daemon。
- cookie daemon（Python 移植自 cdt）缓存并暴露 /cookies。
- fetch 启动时取 cookie，add_cookies 注入隔离 profile（避 lock）。
"""

from .daemonctl import daemon_status, ensure_daemon, get_cookies, to_playwright_cookies

__all__ = ["daemon_status", "ensure_daemon", "get_cookies", "to_playwright_cookies"]
