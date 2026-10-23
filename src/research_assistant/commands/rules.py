"""rules 命令：站点规则库的诊断与测试（规则只从用户目录加载）。

    research-assistant rules list
    research-assistant rules validate
    research-assistant rules explain <url>
    research-assistant rules test [--rule ID]

规则 = URL 层（匹配/变换/请求方式）；parser / preprocess 是规则引用的「响应适配器」与
「HTML 预处理器」，不是规则本身。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from ..config import Config
from ..site_rules import SiteRule, load_rules, match_site_rule, user_rules_dir, validate_rules

NAME = "rules"
ALIASES: list[str] = []
HELP = "Inspect, validate, explain, and test user site rules."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    sub = p.add_subparsers(dest="rules_action", required=True, metavar="<action>")

    sub.add_parser("list", help="List loaded rules.").set_defaults(_handler=_list)

    sub.add_parser("validate", help="Validate rule files and report errors.").set_defaults(
        _handler=_validate
    )

    explain = sub.add_parser("explain", help="Show which rule matches a URL and what it will do.")
    explain.add_argument("url", metavar="URL", help="URL to match against the rule set.")
    explain.set_defaults(_handler=_explain)

    test = sub.add_parser("test", help="Run offline fixtures for rules.")
    test.add_argument("--rule", metavar="ID", help="Only test this rule id.")
    test.set_defaults(_handler=_test)


# ---------------------------------------------------------------------------
# list / validate / explain
# ---------------------------------------------------------------------------


def _rule_summary(rule: SiteRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "rev": rule.rev,
        "updated": rule.updated,
        "priority": rule.priority,
        "transport": rule.transport,
        "hosts": sorted(rule.hosts),
        "host_patterns": [p.pattern for p in rule.host_patterns],
        "parser": rule.parser or None,
        "preprocess": [str(x.get("name", "")) for x in rule.preprocessors],
        "source": rule.source,
    }


async def _list(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    report = validate_rules()
    return {
        "rules_dir": str(user_rules_dir()),
        "count": len(report.rules),
        "rules": [_rule_summary(r) for r in report.rules],
        "errors": report.errors,
        "warnings": report.warnings,
    }


async def _validate(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    return validate_rules().to_dict()


async def _explain(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    hit = match_site_rule(args.url)
    if hit is None:
        return {
            "url": args.url,
            "matched": False,
            "rules_dir": str(user_rules_dir()),
            "rule_count": len(load_rules()),
        }
    rule = hit.rule
    return {
        "url": args.url,
        "matched": True,
        "rule": _rule_summary(rule),
        "request_url": hit.request_url,
        "meta": hit.meta,
        "transport": rule.transport,
        "parser": rule.parser or None,
        "preprocess": [str(x.get("name", "")) for x in rule.preprocessors],
    }


# ---------------------------------------------------------------------------
# test（离线 fixture）
# ---------------------------------------------------------------------------


def _fixtures_dir() -> Path:
    return user_rules_dir() / "fixtures"


def _read_cases(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _test_match(rule: SiteRule, case: dict[str, Any]) -> dict[str, Any]:
    url = str(case.get("url") or "")
    hit = rule.match(url) if url else None
    if hit is None:
        return {"kind": "match", "url": url, "ok": False, "error": "没有命中规则"}
    expect_request = case.get("request")
    expect_meta = case.get("expect_meta") or {}
    problems: list[str] = []
    if expect_request is not None and hit.request_url != str(expect_request):
        problems.append(f"request_url={hit.request_url!r}，期望 {expect_request!r}")
    for k, v in expect_meta.items():
        if str(hit.meta.get(k)) != str(v):
            problems.append(f"meta[{k}]={hit.meta.get(k)!r}，期望 {v!r}")
    return {"kind": "match", "url": url, "ok": not problems, "errors": problems}


def _test_reject(rule: SiteRule, case: dict[str, Any]) -> dict[str, Any]:
    url = str(case.get("url") or "")
    hit = rule.match(url) if url else None
    return {
        "kind": "reject",
        "url": url,
        "ok": hit is None,
        **({"error": f"不应命中，实际 request={hit.request_url}"} if hit is not None else {}),
    }


def _test_parse(rule: SiteRule, case: dict[str, Any], fixture_dir: Path) -> dict[str, Any]:
    url = str(case.get("url") or "")
    hit = rule.match(url) if url else None
    if hit is None:
        return {"kind": "parse", "url": url, "ok": False, "error": "没有命中规则"}
    response_name = str(case.get("response") or "")
    if not response_name:
        return {"kind": "parse", "url": url, "ok": False, "error": "缺少 response 文件"}
    response_path = fixture_dir / response_name
    if not response_path.is_file():
        return {"kind": "parse", "url": url, "ok": False, "error": f"响应文件不存在: {response_path}"}
    try:
        body = response_path.read_text(encoding="utf-8")
    except Exception as e:
        return {"kind": "parse", "url": url, "ok": False, "error": f"读取响应失败: {e}"}

    md = rule.parse(body, hit)
    if md is None:
        return {"kind": "parse", "url": url, "ok": False, "error": "解析器返回 None"}
    problems: list[str] = []
    for want in case.get("contains") or []:
        if str(want) not in md:
            problems.append(f"缺少片段: {want!r}")
    for unwanted in case.get("not_contains") or []:
        if str(unwanted) in md:
            problems.append(f"出现了不应有的片段: {unwanted!r}")
    return {"kind": "parse", "url": url, "ok": not problems, "errors": problems}


def _test_rule(rule: SiteRule) -> dict[str, Any]:
    fixture_dir = _fixtures_dir() / rule.id
    cases_path = fixture_dir / "cases.toml"
    if not cases_path.is_file():
        return {"rule": rule.id, "status": "skipped", "reason": f"没有 fixture: {cases_path}"}
    try:
        cases = _read_cases(cases_path)
    except Exception as e:
        return {"rule": rule.id, "status": "failed", "error": f"cases.toml 解析失败: {e}"}

    results: list[dict[str, Any]] = []
    for case in cases.get("match") or []:
        results.append(_test_match(rule, case))
    for case in cases.get("reject") or []:
        results.append(_test_reject(rule, case))
    for case in cases.get("parse") or []:
        results.append(_test_parse(rule, case, fixture_dir))

    failed = [r for r in results if not r.get("ok")]
    return {
        "rule": rule.id,
        "status": "passed" if not failed else "failed",
        "cases": results,
        "failed": len(failed),
        "total": len(results),
    }


async def _test(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    wanted = getattr(args, "rule", None)
    rules = [r for r in load_rules() if not wanted or r.id == wanted]
    if wanted and not rules:
        return {"ok": False, "error": f"没有找到规则: {wanted}", "results": []}
    results = [_test_rule(r) for r in rules]
    failed = [r for r in results if r.get("status") == "failed"]
    return {
        "ok": not failed,
        "rules_dir": str(user_rules_dir()),
        "fixtures_dir": str(_fixtures_dir()),
        "results": results,
        "passed": sum(1 for r in results if r.get("status") == "passed"),
        "failed": len(failed),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
    }
