# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional, cast

import click

from pr_guardian.context_builder import ContextBuilder
from pr_guardian.diffparse import parse_diff
from pr_guardian.github_api import GitHubAPIClient
from pr_guardian.llm.client import LLMClientFactory
from pr_guardian.models import Diff, DiffFile, Hunk, Policy, Severity
from pr_guardian.policy import PolicyLoader
from pr_guardian.rules import registry

_GitHubReporter: Optional[type] = None
try:
    from pr_guardian.report.github_reporter import GitHubReporter as _ImportedReporter

    _GitHubReporter = _ImportedReporter
except ModuleNotFoundError:
    pass
GitHubReporter = _GitHubReporter


logger = logging.getLogger("pr_guardian.main")


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def _log_event(event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _policy_attr(policy: object, name: str, default: Any) -> Any:
    return getattr(policy, name, default)


def _normalize_policy(raw_policy: object) -> Policy:
    if isinstance(raw_policy, Policy):
        return raw_policy

    llm_cfg = getattr(raw_policy, "llm", None)
    policy_cfg = getattr(raw_policy, "policy", None)
    return Policy(
        gate=bool(_policy_attr(raw_policy, "gate", True)),
        auto_fix=bool(_policy_attr(raw_policy, "auto_fix", False)),
        include=list(_policy_attr(raw_policy, "include", ["**/*"])),
        exclude=list(_policy_attr(raw_policy, "exclude", [])),
        enabled_rules=list(_policy_attr(raw_policy, "enabled_rules", [])),
        severity_overrides=dict(_policy_attr(raw_policy, "severity_overrides", {})),
        allowlist=dict(_policy_attr(raw_policy, "allowlist", {})),
        lockfile_mappings=dict(_policy_attr(raw_policy, "lockfile_mappings", {})),
        llm_enabled=bool(getattr(llm_cfg, "enabled", False)),
        llm_provider=str(getattr(llm_cfg, "provider", "openai")),
        llm_model=str(getattr(llm_cfg, "model", "gpt-4o")),
        llm_max_context_tokens=int(getattr(llm_cfg, "max_context_tokens", 8000)),
        llm_budget_usd=float(getattr(llm_cfg, "budget_usd_per_pr", 0.0)),
        deny_paths=list(getattr(policy_cfg, "deny_paths", [])),
        max_changed_lines_for_autofix=int(getattr(policy_cfg, "max_changed_lines_for_autofix", 50)),
        require_evidence=bool(getattr(policy_cfg, "require_evidence", True)),
    )


def _load_policy(config: str) -> Policy:
    loader = PolicyLoader()
    loaded_policy = loader.load(Path(config))
    return _normalize_policy(loaded_policy)


def _build_diff_from_files(files: list[Any]) -> Diff:
    diff_files: list[DiffFile] = []
    for file_item in files:
        path = str(getattr(file_item, "filename", ""))
        patch = getattr(file_item, "patch", None)
        patch_text = patch if isinstance(patch, str) else ""
        parsed = parse_diff(f"diff --git a/{path} b/{path}\n{patch_text}\n") if patch_text else parse_diff("")

        hunks: list[Hunk] = []
        parsed_file = parsed.files[0] if parsed.files else None
        if parsed_file is not None:
            for parsed_hunk in parsed_file.hunks:
                hunk_lines: list[tuple[str, int | None, int | None]] = [
                    (line.line_type, line.old_line, line.new_line) for line in parsed_hunk.lines
                ]
                hunks.append(
                    Hunk(
                        old_start=parsed_hunk.old_start,
                        old_count=parsed_hunk.old_count,
                        new_start=parsed_hunk.new_start,
                        new_count=parsed_hunk.new_count,
                        lines=hunk_lines,
                    )
                )

        raw_status = str(getattr(file_item, "status", "modified"))
        normalized_status: Literal["added", "removed", "modified", "renamed"]
        if raw_status in {"added", "removed", "modified", "renamed"}:
            normalized_status = cast(Literal["added", "removed", "modified", "renamed"], raw_status)
        else:
            normalized_status = "modified"

        diff_files.append(
            DiffFile(
                path=path,
                status=normalized_status,
                patch=patch,
                additions=int(getattr(file_item, "additions", 0)),
                deletions=int(getattr(file_item, "deletions", 0)),
                hunks=hunks,
            )
        )

    return Diff(files=diff_files)


def should_run_llm(diff: Diff, policy: Policy) -> bool:
    if not policy.llm_enabled:
        return False
    if policy.llm_budget_usd <= 0:
        return False
    changed_lines = sum(file.additions + file.deletions for file in diff.files)
    return changed_lines > policy.max_changed_lines_for_autofix


async def _review_impl(repo: str, pr_number: int, token: str, config: str, llm: bool, dry_run: bool) -> None:
    _setup_logging()
    _log_event("review.start", repo=repo, pr_number=pr_number, dry_run=dry_run)

    policy = _load_policy(config)
    if not llm:
        policy.llm_enabled = False
    _log_event("policy.loaded", enabled_rules=policy.enabled_rules, llm_enabled=policy.llm_enabled)

    client = GitHubAPIClient(token, repo)
    try:
        pr_details = await _maybe_await(client.get_pr_details(pr_number))
        files = await _maybe_await(client.get_pr_files(pr_number))
        _log_event("github.pr_loaded", file_count=len(files))

        diff = _build_diff_from_files(files)
        findings: list[Any] = []

        for rule_id in policy.enabled_rules:
            rule = registry.create_instance(rule_id)
            if rule is None:
                _log_event("rule.not_found", rule_id=rule_id)
                continue
            rule_findings = rule.execute(diff, policy)
            findings.extend(rule_findings)
            _log_event("rule.executed", rule_id=rule_id, findings=len(rule_findings))

        if policy.llm_enabled and should_run_llm(diff, policy):
            context = ContextBuilder(policy).build_context(diff, findings)
            llm_client = LLMClientFactory.create(
                policy.llm_provider,
                {
                    "model": policy.llm_model,
                    "budget_usd": policy.llm_budget_usd,
                    "max_context_tokens": policy.llm_max_context_tokens,
                    "api_key": os.getenv("OPENAI_API_KEY", ""),
                },
            )
            try:
                review_method = cast(Any, getattr(llm_client, "review", None))
                if callable(review_method):
                    llm_result = await _maybe_await(review_method(context))
                    llm_findings = getattr(llm_result, "findings", [])
                else:
                    llm_findings = []
                findings.extend(llm_findings)
                _log_event("llm.review_done", findings=len(llm_findings))
            except Exception as llm_error:  # noqa: BLE001
                _log_event("llm.review_failed", error=str(llm_error))

        head_sha = str(pr_details.get("head_sha") or "")
        commit_id = head_sha
        if not dry_run:
            if GitHubReporter is None:
                raise RuntimeError("GitHubReporter 未实现，无法发布结果")
            reporter = GitHubReporter(client, policy)
            await _maybe_await(reporter.report_findings(pr_number, head_sha, findings, commit_id))
            _log_event("report.published", findings=len(findings))
        else:
            print(json.dumps(findings, indent=2, ensure_ascii=False, default=str))
            _log_event("report.dry_run", findings=len(findings))

        has_error_finding = any(getattr(finding, "severity", None) == Severity.ERROR for finding in findings)
        raise SystemExit(1 if has_error_finding else 0)
    finally:
        close_method = getattr(client, "close", None)
        if callable(close_method):
            close_method()


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--repo", required=True, help="GitHub仓库，格式：owner/name")
@click.option("--pr", "pr_number", type=int, required=True, help="PR编号")
@click.option("--token", envvar="GITHUB_TOKEN", required=True)
@click.option("--config", type=click.Path(), default=".pr-guardian.yml")
@click.option("--llm", is_flag=True, default=True)
@click.option("--dry-run", is_flag=True, help="仅输出，不发布")
def review(repo: str, pr_number: int, token: str, config: str, llm: bool, dry_run: bool) -> None:
    asyncio.run(_review_impl(repo, pr_number, token, config, llm, dry_run))


if __name__ == "__main__":
    cli()
