"""网页副本落盘（fetch 与 browser fetch 共用，D8 只写快照）。

写带 frontmatter 的 markdown：默认 tmp-doc/webcopy-<slug>-<ts>-<rand>.md，或 --write 指定路径。
frontmatter 记录来源 url、抓取时间、抓取方式（normal/browser），便于后续 locate 与溯源。

文件名防撞（多 agent 并发 / 同主机多页不会互相覆盖）：
  slug 含主机名 + URL 路径段；ts 精确到秒；再追加 6 位随机后缀；写盘前若同名已存在则加序号。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path


def write_snapshot(url: str, method: str, md: str, write_path: str | None) -> str:
    """落盘网页副本（只写快照，不覆盖既有文件之外的状态）。返回写入路径。

    write_path 给定 → 写到该路径（父目录自动创建）；
    否则 → tmp-doc/webcopy-<slug>-<ts>-<rand>.md，同名已存在则追加序号，绝不裸覆盖。
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

    if write_path:
        out_path = Path(write_path).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        return str(out_path)

    slug = _slugify(url)
    ts = datetime.now().strftime("%m-%d-%H-%M-%S")
    rand = secrets.token_hex(3)
    tmp_dir = Path("tmp-doc")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(tmp_dir / f"webcopy-{slug}-{ts}-{rand}.md")
    path.write_text(body, encoding="utf-8")
    return str(path)


def _unique_path(candidate: Path) -> Path:
    """candidate 已存在则追加 -1/-2/... 直到不冲突，绝不覆盖既有文件。"""
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    parent = candidate.parent
    i = 1
    while True:
        nxt = parent / f"{stem}-{i}{suffix}"
        if not nxt.exists():
            return nxt
        i += 1


def _slugify(url: str) -> str:
    """从 url 抽主机名 + 路径段做文件名 slug（去非法字符，路径截断防过长）。"""
    m = re.search(r"https?://([^/]+)(/.*)?", url)
    host = m.group(1).replace(".", "-") if m else "page"
    host = re.sub(r"[^a-zA-Z0-9\-]", "", host) or "page"
    path = (m.group(2) or "").strip("/")
    if path:
        path = re.sub(r"[^a-zA-Z0-9\-/]", "", path).strip("/")
        path = path.replace("/", "-")[:40].strip("-")
    return f"{host}-{path}" if path else host
