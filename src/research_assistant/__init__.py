"""research-assistant: 搜索调研子 Agent + 一组可并发的搜索/爬取/定位 CLI 原子工具。

单进程 Python CLI，集成各搜索/文档工具（自写 HTTP，ADR-0010）+ fetch（浏览器爬取增强）
+ locate（锚点速览）+ setup/skills/doctor，通过 npm 包分发。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("research-assistant")
except PackageNotFoundError:  # 源码运行（未 pip install）时的兜底
    __version__ = "0.0.0.dev0"
