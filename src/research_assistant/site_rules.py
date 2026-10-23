"""站点规则库（数据驱动，TOML，只从用户目录加载）。

规则不随包分发，也不进源码仓库：站点规则由使用者维护，放在

    <config_dir>/rules/*.toml        默认 ~/.research-assistant/rules/

这样网站改版只需要改规则文件，不需要给本仓库提 commit。代码里不出现任何站点名。

一条规则只描述「做什么」，代码提供命名能力「怎么做」：

    [[rule]]
    id = "discourse-linuxdo-topic"
    hosts = ["linux.do", "www.linux.do"]
    path_patterns = ['^/t/(?P<id>\\d+)(?:/(?P<post>\\d+))?(?:\\.json)?/?$']
    transport = "browser"              # browser | http
    login = true
    request = "https://{host}/t/topic/{id}.json"
    parser = "discourse_topic"         # 命名解析器；留空 = 取 HTML 后默认抽取
    preprocess = [{ name = "strip_hidden_nodes" }]

能力注册表（代码）：

    预处理器  strip_hidden_nodes / remove_selectors
    解析器    discourse_topic / json_path / css_extract / html_article
    传输      browser（登录态 + CF）/ http（直取，失败落回原链路）

规则无法表达时按四级升级：改规则数据 → 用参数化能力 → 加通用命名能力 → 站点插件；
任何规则未命中或失败都落回原链路，不阻塞抓取。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger("research_assistant.site_rules")

RULE_SCHEMA = 1
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteRule:
    """一条从 TOML 加载的站点规则（不含站点专属代码）。"""

    id: str
    hosts: frozenset[str]
    host_patterns: tuple[re.Pattern[str], ...]
    path_patterns: tuple[re.Pattern[str], ...]
    transport: str = "browser"  # browser | http | html（html = 只做预处理，不改请求）
    needs_login: bool = True
    allow_cross_host: bool = False
    request_template: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    parser: str = ""
    parser_config: dict[str, Any] = field(default_factory=dict)
    preprocessors: tuple[dict[str, Any], ...] = ()
    priority: int = 0
    rev: int = 1
    updated: str = ""
    source: str = ""

    def match(self, url: str) -> "RuleMatch | None":
        try:
            p = urlparse(url)
        except ValueError:
            return None
        if p.scheme and p.scheme not in ("http", "https"):
            return None
        host = (p.hostname or "").lower()
        if not self._host_matches(host):
            return None

        m = None
        for rx in self.path_patterns:
            m = rx.match(p.path or "")
            if m:
                break
        if not m:
            return None
        meta = {k: v for k, v in m.groupdict().items() if v is not None}

        request_url = url
        if self.transport != "html" and self.request_template:
            try:
                request_url = self.request_template.format(host=host, **meta)
            except (KeyError, IndexError, ValueError) as e:
                logger.warning("规则 %s 的 request 模板无法展开 %s: %s", self.id, url, e)
                return None
        req = urlparse(request_url)
        if req.scheme not in ("http", "https"):
            logger.warning("规则 %s 生成了非 http(s) 请求地址: %s", self.id, request_url)
            return None
        req_host = (req.hostname or "").lower()
        if req_host != host and not self.allow_cross_host:
            logger.warning(
                "规则 %s 想把 %s 的请求发到 %s；未声明 allow_cross_host，已跳过",
                self.id, host, req_host,
            )
            return None
        return RuleMatch(rule=self, source_url=url, request_url=request_url, meta=meta)

    def _host_matches(self, host: str) -> bool:
        if host in self.hosts:
            return True
        return any(rx.search(host) for rx in self.host_patterns)

    def preprocess_html(self, html: str, match: "RuleMatch") -> str:
        """按规则声明顺序执行命名预处理器；未声明的规则原样返回。"""
        for spec in self.preprocessors:
            params = dict(spec)
            name = str(params.pop("name", "")).strip()
            fn = PREPROCESSORS.get(name)
            if fn is None:
                logger.warning("规则 %s 引用未知预处理器 %s，已跳过", self.id, name)
                continue
            try:
                html = fn(html, **params)
            except Exception as e:
                logger.warning("规则 %s 的预处理器 %s 失败: %s", self.id, name, e)
        return html

    def parse(self, body: str, match: "RuleMatch") -> str | None:
        """用规则声明的命名解析器把响应转 markdown；没有解析器返回 None。"""
        if not self.parser:
            return None
        fn = PARSERS.get(self.parser)
        if fn is None:
            logger.warning("规则 %s 引用未知解析器 %s", self.id, self.parser)
            return None
        try:
            return fn(body, match, self.parser_config)
        except Exception as e:
            logger.warning("规则 %s 的解析器 %s 失败: %s", self.id, self.parser, e)
            return None


@dataclass(frozen=True)
class RuleMatch:
    """一次命中：规则 + 原始 URL + 实际请求 URL + 命名捕获组。"""

    rule: SiteRule
    source_url: str
    request_url: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rule_id(self) -> str:
        return self.rule.id


@dataclass
class RuleLoadReport:
    """规则加载/校验报告。"""

    rules: tuple[SiteRule, ...] = ()
    files: tuple[str, ...] = ()
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rules_dir": str(user_rules_dir()),
            "files": list(self.files),
            "rule_count": len(self.rules),
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 命名预处理器
# ---------------------------------------------------------------------------


def _pp_strip_hidden(html: str, **_: Any) -> str:
    from .fetch.html2md import strip_hidden_nodes

    return strip_hidden_nodes(html)


def _pp_remove_selectors(html: str, selectors: Any = None, **_: Any) -> str:
    """删除 CSS 选择器命中的节点（规则参数：selectors = [".ad", "nav"]）。"""
    if not selectors:
        return html
    if isinstance(selectors, str):
        selectors = [selectors]
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(html)
        removed = 0
        for sel in selectors:
            try:
                for el in root.cssselect(str(sel)):
                    el.drop_tree()
                    removed += 1
            except Exception as e:
                logger.warning("remove_selectors: 选择器 %r 失败: %s", sel, e)
        if not removed:
            return html
        return lxml_html.tostring(root, encoding="unicode")
    except Exception as e:
        logger.warning("remove_selectors 预处理失败: %s", e)
        return html


PREPROCESSORS: dict[str, Callable[..., str]] = {
    "strip_hidden_nodes": _pp_strip_hidden,
    "remove_selectors": _pp_remove_selectors,
}


# ---------------------------------------------------------------------------
# 命名解析器
# ---------------------------------------------------------------------------


def _parse_discourse_topic(body: str, match: RuleMatch, cfg: dict[str, Any]) -> str | None:
    """Discourse 话题 JSON → 逐帖 markdown。服务所有 Discourse 站，不绑定域名。"""
    text = (body or "").lstrip("﻿").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "post_stream" not in data:
        return None

    title = str(data.get("title") or "").strip()
    posts = ((data.get("post_stream") or {}).get("posts")) or []
    if not isinstance(posts, list):
        return None
    if not title and not posts:
        return None

    from .fetch.html2md import html_to_md  # 延迟导入，避免包初始化顺序问题

    total = data.get("posts_count") or len(posts)
    lines: list[str] = []
    if title:
        lines.extend([f"# {title}", ""])
    lines.extend([f"> 来源: {match.source_url} · 共 {total} 帖（本页 {len(posts)} 帖）", ""])

    rendered = 0
    for post in posts:
        if not isinstance(post, dict):
            continue
        cooked = post.get("cooked") or ""
        md = html_to_md(match.rule.preprocess_html(cooked, match), match.source_url) if cooked else ""
        if not md.strip():
            continue  # 懒加载占位 / 空帖
        number = post.get("post_number")
        author = str(post.get("username") or post.get("name") or "").strip()
        when = _short_time(post.get("created_at"))
        head = f"## #{number}" if number is not None else "##"
        extra = " · ".join(x for x in (author, when) if x)
        if extra:
            head += f" {extra}"
        lines.extend([head, "", md, ""])
        rendered += 1

    if rendered == 0:
        return None
    return "\n".join(lines).strip()


def _parse_html_article(body: str, match: RuleMatch, cfg: dict[str, Any]) -> str | None:
    """HTML 文章正文（trafilatura + 规则预处理）。"""
    from .fetch.html2md import html_to_md

    md = html_to_md(match.rule.preprocess_html(body, match), match.source_url)
    return md or None


def _parse_css_extract(body: str, match: RuleMatch, cfg: dict[str, Any]) -> str | None:
    """按 CSS 选择器抽字段（规则参数：title/content/author/published/remove）。"""
    try:
        from lxml import html as lxml_html
    except Exception:
        return None

    cleaned = match.rule.preprocess_html(body, match)
    try:
        root = lxml_html.fromstring(cleaned)
    except Exception:
        return None

    remove = cfg.get("remove") or []
    if isinstance(remove, str):
        remove = [remove]
    for sel in remove:
        try:
            for el in root.cssselect(str(sel)):
                el.drop_tree()
        except Exception as e:
            logger.warning("css_extract: remove 选择器 %r 失败: %s", sel, e)

    title = _css_text(root, cfg.get("title"))
    author = _css_text(root, cfg.get("author"))
    published = _css_text(root, cfg.get("published"))

    content_el = _css_first(root, cfg.get("content"))
    if content_el is not None:
        content_html = lxml_html.tostring(content_el, encoding="unicode")
    else:
        content_html = lxml_html.tostring(root, encoding="unicode")

    from .fetch.html2md import html_to_md

    md = html_to_md(content_html, match.source_url)
    if not md:
        return None

    lines: list[str] = []
    if title:
        lines.extend([f"# {title}", ""])
    meta_line = " · ".join(x for x in (author, published) if x)
    source = f"> 来源: {match.source_url}" + (f" · {meta_line}" if meta_line else "")
    lines.extend([source, "", md])
    return "\n".join(lines).strip()


def _css_first(root: Any, selector: Any) -> Any | None:
    if not selector:
        return None
    sels = selector if isinstance(selector, list) else [selector]
    for sel in sels:
        try:
            els = root.cssselect(str(sel))
            if els:
                return els[0]
        except Exception:
            continue
    return None


def _css_text(root: Any, selector: Any) -> str:
    el = _css_first(root, selector)
    if el is None:
        return ""
    try:
        return " ".join(el.text_content().split())
    except Exception:
        return ""


def _parse_json_path(body: str, match: RuleMatch, cfg: dict[str, Any]) -> str | None:
    """通用 JSON 映射：title + sections[].select/heading/body。

    支持的 JSONPath 子集：`$`、`.key`、`['key']`、`[0]`、`[*]`，可组合为 `$.a[*].b`。
    没有 sections 时把整个 JSON 作为围栏代码块返回。
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None

    sections = cfg.get("section") or cfg.get("sections") or []
    if isinstance(sections, dict):
        sections = [sections]

    from .fetch.html2md import html_to_md

    lines: list[str] = []
    title = _jsonpath_first(data, str(cfg.get("title") or ""))
    if title not in (None, ""):
        lines.extend([f"# {title}", ""])
    lines.extend([f"> 来源: {match.source_url}", ""])

    if not sections:
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
        lines.extend(["```json", pretty, "```"])
        return "\n".join(lines).strip()

    rendered = 0
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        select = str(sec.get("select") or "")
        items = _jsonpath_get(data, select) if select else [data]
        for item in items:
            body_val = _jsonpath_first(item, str(sec.get("body") or ""))
            if body_val in (None, ""):
                continue
            body_type = str(sec.get("body_type") or "text").lower()
            if body_type == "html":
                md = html_to_md(match.rule.preprocess_html(str(body_val), match), match.source_url)
            else:
                md = str(body_val).strip()
            if not md:
                continue
            heading = _jsonpath_first(item, str(sec.get("heading") or ""))
            if heading not in (None, ""):
                lines.extend([f"## {heading}", ""])
            lines.extend([md, ""])
            rendered += 1

    if rendered == 0 and not title:
        return None
    return "\n".join(lines).strip()


def _jsonpath_get(data: Any, path: str) -> list[Any]:
    """JSONPath 子集求值，返回匹配值列表。"""
    path = (path or "").strip()
    if path in ("", "$"):
        return [data]
    if path.startswith("$"):
        path = path[1:]
    elif not path.startswith((".", "[")):
        path = "." + path  # 相对路径（section 内字段）允许写 "username"

    tokens: list[tuple[str, str]] = []
    pos = 0
    token_re = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+|\*)\]|\[['\"]([^'\"]+)['\"]\]")
    while pos < len(path):
        m = token_re.match(path, pos)
        if not m:
            raise ValueError(f"不支持的 JSONPath: {path}")
        if m.group(1) is not None:
            tokens.append(("key", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("all" if m.group(2) == "*" else "index", m.group(2)))
        else:
            tokens.append(("key", m.group(3)))
        pos = m.end()

    current: list[Any] = [data]
    for kind, val in tokens:
        nxt: list[Any] = []
        for item in current:
            if kind == "key":
                if isinstance(item, dict) and val in item:
                    nxt.append(item[val])
            elif kind == "index":
                if isinstance(item, list):
                    try:
                        nxt.append(item[int(val)])
                    except (IndexError, ValueError):
                        pass
            elif kind == "all":
                if isinstance(item, list):
                    nxt.extend(item)
        current = nxt
    return current


def _jsonpath_first(data: Any, path: str) -> Any:
    if not path:
        return None
    try:
        vals = _jsonpath_get(data, path)
    except ValueError:
        return None
    return vals[0] if vals else None


def _short_time(value: Any) -> str:
    """ISO 时间 → 本地 `YYYY-MM-DD HH:MM`；解析失败原样返回。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return raw


PARSERS: dict[str, Callable[[str, RuleMatch, dict[str, Any]], str | None]] = {
    "discourse_topic": _parse_discourse_topic,
    "html_article": _parse_html_article,
    "css_extract": _parse_css_extract,
    "json_path": _parse_json_path,
}


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------

_REPORT: RuleLoadReport | None = None


def user_rules_dir() -> Path:
    """用户规则目录：<config_dir>/rules（默认 ~/.research-assistant/rules）。"""
    from . import config as config_mod

    return config_mod.config_dir() / "rules"


def _normalize_preprocessors(raw: Any, report: RuleLoadReport, rule_id: str, source: str) -> tuple[dict[str, Any], ...]:
    if raw in (None, ""):
        return ()
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            spec: dict[str, Any] = {"name": item}
        elif isinstance(item, dict):
            spec = dict(item)
        else:
            report.errors.append({"rule": rule_id, "source": source, "error": f"preprocess 项类型非法: {type(item).__name__}"})
            continue
        name = str(spec.get("name") or "").strip()
        if not name:
            report.errors.append({"rule": rule_id, "source": source, "error": "preprocess 项缺少 name"})
            continue
        if name not in PREPROCESSORS:
            report.errors.append({"rule": rule_id, "source": source, "error": f"未知预处理器: {name}"})
            continue
        spec["name"] = name
        out.append(spec)
    return tuple(out)


def _template_fields(template: str) -> set[str]:
    import string

    fields: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(template):
            if field_name:
                fields.add(field_name.split("[", 1)[0].split(".", 1)[0])
    except ValueError:
        fields.add("__invalid__")
    return fields


def _build_rule(raw: Any, source: str, report: RuleLoadReport) -> SiteRule | None:
    if not isinstance(raw, dict):
        report.errors.append({"rule": "", "source": source, "error": "rule 不是表（table）"})
        return None
    rid = str(raw.get("id") or "").strip()
    if not rid:
        report.errors.append({"rule": "", "source": source, "error": "缺少 id"})
        return None
    if raw.get("enabled", True) is False:
        return None

    hosts = raw.get("hosts") or []
    host_patterns_raw = raw.get("host_patterns") or []
    if isinstance(hosts, str):
        hosts = [hosts]
    if isinstance(host_patterns_raw, str):
        host_patterns_raw = [host_patterns_raw]
    if not hosts and not host_patterns_raw:
        report.errors.append({"rule": rid, "source": source, "error": "缺少 hosts 或 host_patterns"})
        return None

    host_patterns: list[re.Pattern[str]] = []
    for pat in host_patterns_raw:
        try:
            host_patterns.append(re.compile(str(pat)))
        except re.error as e:
            report.errors.append({"rule": rid, "source": source, "error": f"host_pattern 无法编译 {pat!r}: {e}"})
            return None

    patterns_raw = raw.get("path_patterns") or []
    if isinstance(patterns_raw, str):
        patterns_raw = [patterns_raw]
    if not patterns_raw:
        report.errors.append({"rule": rid, "source": source, "error": "缺少 path_patterns"})
        return None
    patterns: list[re.Pattern[str]] = []
    for pat in patterns_raw:
        try:
            patterns.append(re.compile(str(pat)))
        except re.error as e:
            report.errors.append({"rule": rid, "source": source, "error": f"path_pattern 无法编译 {pat!r}: {e}"})
            return None

    parser = str(raw.get("parser") or "").strip()
    if parser and parser not in PARSERS:
        report.errors.append({"rule": rid, "source": source, "error": f"未知解析器: {parser}"})
        return None

    transport = str(raw.get("transport") or ("browser" if parser else "html")).strip().lower()
    if transport not in ("browser", "http", "html"):
        report.errors.append({"rule": rid, "source": source, "error": f"transport 非法: {transport}"})
        return None

    request_template = str(raw.get("request") or "").strip()
    if transport == "html" and request_template:
        report.warnings.append({
            "rule": rid, "source": source,
            "warning": "transport=html 只做预处理，request 模板不会生效（要改写 URL 用 browser/http）",
        })
    if request_template:
        fields = _template_fields(request_template)
        if "__invalid__" in fields:
            report.errors.append({"rule": rid, "source": source, "error": f"request 模板语法错误: {request_template!r}"})
            return None
        allowed = {"host"} | set().union(*(set(p.groupindex) for p in patterns))
        unknown = fields - allowed
        if unknown:
            report.errors.append({
                "rule": rid, "source": source,
                "error": f"request 模板引用了未知字段 {sorted(unknown)}（可用: {sorted(allowed)}）",
            })
            return None

    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        report.errors.append({"rule": rid, "source": source, "error": "headers 必须是表（table）"})
        return None
    headers = {str(k): str(v) for k, v in headers_raw.items()}

    preprocessors = _normalize_preprocessors(raw.get("preprocess"), report, rid, source)

    parser_config = raw.get("parser_config") or {}
    if not isinstance(parser_config, dict):
        report.errors.append({"rule": rid, "source": source, "error": "parser_config 必须是表（table）"})
        return None

    try:
        priority = int(raw.get("priority", 0))
        rev = int(raw.get("rev", 1))
    except (TypeError, ValueError):
        report.errors.append({"rule": rid, "source": source, "error": "priority/rev 必须是整数"})
        return None

    return SiteRule(
        id=rid,
        hosts=frozenset(str(h).strip().lower() for h in hosts if str(h).strip()),
        host_patterns=tuple(host_patterns),
        path_patterns=tuple(patterns),
        transport=transport,
        needs_login=bool(raw.get("login", True)),
        allow_cross_host=bool(raw.get("allow_cross_host", False)),
        request_template=request_template,
        headers=headers,
        parser=parser,
        parser_config=dict(parser_config),
        preprocessors=preprocessors,
        priority=priority,
        rev=rev,
        updated=str(raw.get("updated") or ""),
        source=source,
    )


def _load_report(force: bool = False) -> RuleLoadReport:
    global _REPORT
    if _REPORT is not None and not force:
        return _REPORT

    report = RuleLoadReport()
    directory = user_rules_dir()
    report.files = tuple(str(p) for p in sorted(directory.glob("*.toml"))) if directory.is_dir() else ()
    if not directory.is_dir():
        report.warnings.append({"source": str(directory), "warning": "规则目录不存在（没有规则会被加载）"})

    by_id: dict[str, SiteRule] = {}
    for path in report.files:
        try:
            raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            report.errors.append({"rule": "", "source": path, "error": f"TOML 读取/解析失败: {e}"})
            continue
        schema = raw.get("schema", RULE_SCHEMA)
        if schema != RULE_SCHEMA:
            report.errors.append({"rule": "", "source": path, "error": f"不支持的规则 schema: {schema}（当前 {RULE_SCHEMA}）"})
            continue
        rules_raw = raw.get("rule") or []
        if isinstance(rules_raw, dict):
            rules_raw = [rules_raw]
        if not isinstance(rules_raw, list):
            report.errors.append({"rule": "", "source": path, "error": "rule 必须是表数组（[[rule]]）"})
            continue
        for item in rules_raw:
            rule = _build_rule(item, path, report)
            if rule is None:
                continue
            if rule.id in by_id:
                report.warnings.append({
                    "rule": rule.id, "source": path,
                    "warning": f"id 重复，后加载的覆盖先加载的（{by_id[rule.id].source} → {path}）",
                })
            by_id[rule.id] = rule

    # priority 高者先匹配；同优先级保持文件/声明顺序
    ordered = sorted(by_id.values(), key=lambda r: -r.priority)
    report.rules = tuple(ordered)
    _REPORT = report
    return report


def load_rules(*, force: bool = False) -> tuple[SiteRule, ...]:
    return _load_report(force).rules


def reload_rules() -> RuleLoadReport:
    return _load_report(force=True)


def validate_rules() -> RuleLoadReport:
    return _load_report(force=True)


def match_site_rule(url: str) -> RuleMatch | None:
    """返回命中的规则；没有命中返回 None（调用方落回原链路）。"""
    for rule in load_rules():
        try:
            hit = rule.match(url)
        except Exception as e:  # 规则写错不应拖垮抓取
            logger.debug("规则 %s 匹配失败 %s: %s", rule.id, url, e)
            continue
        if hit is not None:
            return hit
    return None


def rule_by_id(rule_id: str) -> SiteRule | None:
    for rule in load_rules():
        if rule.id == rule_id:
            return rule
    return None


def max_response_bytes() -> int:
    return _MAX_RESPONSE_BYTES
