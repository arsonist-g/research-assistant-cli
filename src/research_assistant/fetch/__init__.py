"""fetch 包：跨工具爬取增强（普通接口 → 失败回退浏览器，PM §4.2）。"""

from .html2md import html_to_md
from .normal import fetch_normal
from .browser import fetch_with_browser, browser_probe
from .snapshot import write_snapshot
from . import cfbypass

# 注意：search_engine 子模块的入口函数也叫 search_engine，不在此重导出——否则
# `from ..fetch import search_engine` 拿到函数后会覆盖系统绑定的子模块（Python 同名陷阱）。
# 调用方直接 from ..fetch.search_engine import search_engine。

__all__ = [
    "html_to_md",
    "fetch_normal",
    "fetch_with_browser",
    "browser_probe",
    "write_snapshot",
    "cfbypass",
]
