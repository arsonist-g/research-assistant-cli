"""网页副本落盘（fetch 与 browser fetch 共用，D8 只写快照）。

写带 frontmatter 的 markdown：默认 tmp-doc/webcopy-<slug>-<ts>.md，或 --output 指定路径。
frontmatter 记录来源 url、抓取时间、抓取方式（normal/browser），便于后续 locate 与溯源。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def write_snapshot(url: str, method: str, md: str, output: str | None) -> str:
    """落盘网页副本（只写快照，不覆盖既有文件之外的状态）。返回写入路径。

    output 给定 → 写到该路径（父目录自动创建）；
    否则 → tmp-doc/webcopy-<slug>-<ts>.md。
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    frontmatter = (
        "---\n"
        f"url: {url}\n"
        f"fetched_at: {fetched_at}\n"
        f"fetch_method: {method}\n"
        "---\n\n"
    )
    body = frontmatter + md + "\n"

    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        return str(out_path)

    slug = _slugify(url)
    ts = datetime.now().strftime("%m-%d-%H-%M")
    tmp_dir = Path("tmp-doc")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"webcopy-{slug}-{ts}.md"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _slugify(url: str) -> str:
    """从 url 抽主机名做文件名 slug（点 → 连字符，去非法字符）。"""
    m = re.search(r"https?://([^/]+)", url)
    host = m.group(1).replace(".", "-") if m else "page"
    host = re.sub(r"[^a-zA-Z0-9\-]", "", host)
    return host or "page"
