# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from pr_guardian.github_api import GitHubAPIClient
from pr_guardian.models import Finding, Policy, Severity


class GitHubReporter:
    """把结构化 finding 映射成 GitHub 三通道输出，避免各发布路径各自拼消息。"""

    SUMMARY_MARKER = "<!-- pr-guardian:summary -->"
    FINGERPRINT_MARKER_PREFIX = "<!-- pr-guardian:fingerprint="
    FINGERPRINT_MARKER_PATTERN = re.compile(r"<!--\s*pr-guardian:fingerprint=([a-f0-9]{16,64})\s*-->")
    CATEGORY_TITLES = {
        "security": "Security",
        "correctness": "Correctness",
        "ci": "CI",
        "monorepo": "Monorepo",
        "docs": "Docs",
    }

    def __init__(self, client: GitHubAPIClient, policy: Policy) -> None:
        self.client = client
        self.policy = policy

    def report_findings(
        self,
        pr_number: int,
        head_sha: str,
        findings: list[Finding],
        commit_id: str,
    ) -> dict[str, dict[str, Any]]:
        return {
            "check_run": self._publish_check_run(head_sha=head_sha, findings=findings),
            "review": self._publish_review_comments(pr_number=pr_number, commit_id=commit_id, findings=findings),
            "summary": self._publish_summary_comment(pr_number=pr_number, findings=findings),
        }

    def _publish_check_run(self, head_sha: str, findings: list[Finding]) -> dict[str, Any]:
        grouped = self._group_by_severity(findings)
        has_error = len(grouped[Severity.ERROR]) > 0
        conclusion = "failure" if has_error else "success"

        summary_lines = [
            f"Total findings: {len(findings)}",
            f"Errors: {len(grouped[Severity.ERROR])}",
            f"Warnings: {len(grouped[Severity.WARNING])}",
            f"Info: {len(grouped[Severity.INFO])}",
        ]

        annotations: list[dict[str, Any]] = []
        for finding in findings:
            if finding.severity not in {Severity.ERROR, Severity.WARNING}:
                continue
            if not finding.evidence:
                continue
            evidence = finding.evidence[0]
            if evidence.line < 1:
                continue
            annotations.append(
                {
                    "path": evidence.file,
                    "start_line": evidence.line,
                    "end_line": evidence.line,
                    "annotation_level": "failure" if finding.severity == Severity.ERROR else "warning",
                    "title": finding.title,
                    "message": finding.message,
                }
            )
            # Check Run 单次注解数量有限，截断能避免整次发布因为超额而失败。
            if len(annotations) >= 50:
                break

        response = self.client.create_check_run(
            name="PR Guardian Results",
            head_sha=head_sha,
            status="completed",
            conclusion=conclusion,
            output={
                "title": "PR Guardian Results",
                "summary": "\n".join(summary_lines),
            },
            annotations=annotations,
        )
        return {
            "id": response.get("id"),
            "conclusion": conclusion,
            "annotation_count": len(annotations),
        }

    def _publish_review_comments(
        self,
        pr_number: int,
        commit_id: str,
        findings: list[Finding],
    ) -> dict[str, Any]:
        # 用 fingerprint 去重，避免同一 PR 多次重跑后重复刷出相同行内评论。
        existing_fingerprints = self._list_existing_review_fingerprints(pr_number)
        comments: list[dict[str, Any]] = []

        for finding in findings:
            if not finding.evidence:
                continue
            evidence = finding.evidence[0]
            if evidence.line < 1:
                continue
            fingerprint = self._get_fingerprint(finding)
            if fingerprint in existing_fingerprints:
                continue
            comments.append(
                {
                    "path": evidence.file,
                    "line": evidence.line,
                    "side": "RIGHT",
                    "body": (
                        f"{self._format_finding_body(finding)}\n\n"
                        f"{self.FINGERPRINT_MARKER_PREFIX}{fingerprint} -->"
                    ),
                }
            )

        if not comments:
            return {"created": False, "comment_count": 0}

        review = self.client.create_review(
            pr_number=pr_number,
            commit_id=commit_id,
            body="PR Guardian inline findings",
            comments=comments,
            event="COMMENT",
        )
        return {
            "created": True,
            "comment_count": len(comments),
            "id": review.get("id"),
        }

    def _publish_summary_comment(
        self,
        pr_number: int,
        findings: list[Finding],
    ) -> dict[str, Any]:
        grouped = self._group_by_severity(findings)
        grouped_categories = self._group_by_category(findings)

        body_lines = [
            self.SUMMARY_MARKER,
            "## PR Guardian Summary",
            f"- Total: {len(findings)}",
            f"- Error: {len(grouped[Severity.ERROR])}",
            f"- Warning: {len(grouped[Severity.WARNING])}",
            f"- Info: {len(grouped[Severity.INFO])}",
            "",
        ]

        ordered_categories = ["Security", "Correctness", "CI", "Monorepo", "Docs"]
        for category in ordered_categories:
            items = grouped_categories.get(category, [])
            if not items:
                continue
            body_lines.append(f"## {category}")
            for finding in items:
                location = "n/a"
                if finding.evidence:
                    first_evidence = finding.evidence[0]
                    location = f"{first_evidence.file}:{first_evidence.line}"
                body_lines.append(
                    f"- [{finding.severity.value.upper()}] **{finding.title}** (`{location}`) - {finding.message}"
                )
            body_lines.append("")

        other_items = grouped_categories.get("Other", [])
        if other_items:
            body_lines.append("## Other")
            for finding in other_items:
                location = "n/a"
                if finding.evidence:
                    first_evidence = finding.evidence[0]
                    location = f"{first_evidence.file}:{first_evidence.line}"
                body_lines.append(
                    f"- [{finding.severity.value.upper()}] **{finding.title}** (`{location}`) - {finding.message}"
                )

        body = "\n".join(body_lines).strip()
        # 用固定 marker 锁定同一条汇总评论，保证重复执行时走更新而不是追加。
        existing_summary = self._find_existing_summary_comment(pr_number)
        if existing_summary and existing_summary.get("body", "") == body:
            return {"created": False, "updated": False, "unchanged": True, "comment_id": existing_summary.get("id")}

        if existing_summary and existing_summary.get("id") is not None:
            updated = self.client._request(
                "PATCH",
                f"/repos/{self.client.repo}/issues/comments/{existing_summary['id']}",
                json={"body": body},
            )
            if isinstance(updated, dict):
                return {"created": False, "updated": True, "comment_id": updated.get("id")}

        created = self.client.create_pr_comment(pr_number=pr_number, body=body)
        return {"created": True, "updated": False, "comment_id": created.get("id")}

    def _group_by_severity(self, findings: list[Finding]) -> dict[Severity, list[Finding]]:
        grouped: dict[Severity, list[Finding]] = {
            Severity.ERROR: [],
            Severity.WARNING: [],
            Severity.INFO: [],
        }
        for finding in findings:
            grouped[finding.severity].append(finding)
        return grouped

    def _group_by_category(self, findings: list[Finding]) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = defaultdict(list)
        for finding in findings:
            prefix = finding.rule_id.split("/", maxsplit=1)[0].lower()
            title = self.CATEGORY_TITLES.get(prefix, "Other")
            grouped[title].append(finding)
        return dict(grouped)

    def _format_finding_body(self, finding: Finding) -> str:
        lines = [
            f"### {finding.title}",
            f"- Rule: `{finding.rule_id}`",
            f"- Severity: `{finding.severity.value}`",
            f"- Message: {finding.message}",
        ]
        if finding.fix is not None:
            lines.append(f"- Fix: {finding.fix.description}")
        return "\n".join(lines)

    def _get_fingerprint(self, finding: Finding) -> str:
        evidence_key = ""
        if finding.evidence:
            first_evidence = finding.evidence[0]
            evidence_key = f"{first_evidence.file}:{first_evidence.line}:{first_evidence.snippet}"
        # 把证据位置纳入指纹，才能让同一规则在不同位置的 finding 并存。
        payload = "|".join(
            [
                finding.rule_id,
                finding.severity.value,
                finding.title,
                finding.message,
                evidence_key,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _list_existing_review_fingerprints(self, pr_number: int) -> set[str]:
        if not hasattr(self.client, "_request"):
            return set()
        response = self.client._request(
            "GET",
            f"/repos/{self.client.repo}/pulls/{pr_number}/comments",
            params={"per_page": 100},
        )
        if not isinstance(response, list):
            return set()

        fingerprints: set[str] = set()
        for item in response:
            if not isinstance(item, dict):
                continue
            body = item.get("body", "")
            if not isinstance(body, str):
                continue
            for match in self.FINGERPRINT_MARKER_PATTERN.findall(body):
                fingerprints.add(match)
        return fingerprints

    def _find_existing_summary_comment(self, pr_number: int) -> dict[str, Any] | None:
        comments = self.client.list_pr_comments(pr_number)
        for comment in comments:
            body = comment.get("body")
            if isinstance(body, str) and self.SUMMARY_MARKER in body:
                return comment
        return None
