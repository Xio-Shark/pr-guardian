# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import math
import re
from typing import Protocol

from .models import Diff, DiffFile, Evidence, Policy


class FindingLike(Protocol):
    rule_id: str
    tags: list[str]
    evidence: list[Evidence]

    def model_dump(self, mode: str = "json") -> dict[str, object]: ...


_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
}

_RISK_PRIORITY: dict[str, int] = {
    "security": 0,
    "correctness": 1,
    "ci": 2,
    "monorepo": 3,
    "docs": 4,
}


def detect_language(path: str) -> str:
    lowered_path = path.lower()
    for suffix, language in _LANGUAGE_BY_SUFFIX.items():
        if lowered_path.endswith(suffix):
            return language
    return "text"


def _should_include_file(diff_file: DiffFile, findings: list[FindingLike]) -> bool:
    if not findings:
        return True
    for finding in findings:
        for evidence in finding.evidence:
            if evidence.file == diff_file.path:
                return True
    return False


class ContextBuilder:
    def __init__(self, policy: Policy):
        self.policy: Policy = policy

    def build_context(self, diff: Diff, findings: list[FindingLike]) -> dict[str, object]:
        payload: dict[str, object] = {
            "repo": getattr(diff, "repo", "unknown/unknown"),
            "pr": self._build_pr_metadata(diff),
            "config": {
                "llm_enabled": self.policy.llm_enabled,
                "llm_provider": self.policy.llm_provider,
                "llm_model": self.policy.llm_model,
                "llm_max_context_tokens": self.policy.llm_max_context_tokens,
                "require_evidence": self.policy.require_evidence,
            },
            "stats": {
                "files_changed": len(diff.files),
                "additions": sum(diff_file.additions for diff_file in diff.files),
                "deletions": sum(diff_file.deletions for diff_file in diff.files),
            },
            "changed_files": [],
            "deterministic_findings": [finding.model_dump(mode="json") for finding in findings],
            "ci_summary": {},
        }

        max_tokens = self.policy.llm_max_context_tokens
        base_tokens = self.estimate_tokens(json.dumps(payload, ensure_ascii=False))
        remaining_tokens = max(max_tokens - base_tokens, 0)

        candidates = [
            diff_file
            for diff_file in diff.files
            if _should_include_file(diff_file, findings)
        ]
        def file_rank(diff_file: DiffFile) -> int:
            return self._file_risk_rank(diff_file.path, findings)

        candidates.sort(key=file_rank)

        for diff_file in candidates:
            current_context = self.crop_file_context(diff_file)
            context_tokens = self.estimate_tokens(json.dumps(current_context, ensure_ascii=False))

            if context_tokens > remaining_tokens:
                fitted_context = self._fit_within_budget(diff_file, remaining_tokens)
                if fitted_context is None:
                    continue
                current_context = fitted_context
                context_tokens = self.estimate_tokens(json.dumps(current_context, ensure_ascii=False))

            changed_files_list: list[dict[str, object]] = payload["changed_files"]  # type: ignore[assignment]
            changed_files_list.append(current_context)
            remaining_tokens -= context_tokens
            if remaining_tokens <= 0:
                break

        return payload

    def crop_file_context(self, diff_file: DiffFile, max_lines: int = 200) -> dict[str, object]:
        patch = diff_file.patch or ""
        redacted_patch = self.redact_secrets(patch)
        patch_lines = redacted_patch.splitlines()
        cropped_patch = "\n".join(patch_lines[:max_lines])

        hotspots = [
            {
                "line_start": hunk.new_start,
                "line_end": hunk.new_start + max(hunk.new_count, 1) - 1,
                "snippet": "",
            }
            for hunk in diff_file.hunks
        ]

        return {
            "path": diff_file.path,
            "language": detect_language(diff_file.path),
            "patch": cropped_patch,
            "hotspots": hotspots,
        }

    def redact_secrets(self, text: str) -> str:
        redacted_text = text
        redaction_patterns = [
            (r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED]"),
            (r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*", "Bearer [REDACTED]"),
            (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED]"),
            (
                r"-----BEGIN [^-]+ PRIVATE KEY-----[\s\S]*?-----END [^-]+ PRIVATE KEY-----",
                "[REDACTED]",
            ),
        ]
        for pattern, replacement in redaction_patterns:
            redacted_text = re.sub(pattern, replacement, redacted_text)
        return redacted_text

    def estimate_tokens(self, text: str) -> int:
        chinese_characters = 0
        other_characters = 0
        for character in text:
            if "\u4e00" <= character <= "\u9fff":
                chinese_characters += 1
            elif not character.isspace():
                other_characters += 1
        return chinese_characters + math.ceil(other_characters / 4)

    def _build_pr_metadata(self, diff: Diff) -> dict[str, object]:
        pr_info = getattr(diff, "pr", None)
        if isinstance(pr_info, dict):
            return {
                "number": pr_info.get("number"),
                "title": pr_info.get("title", ""),
                "base_sha": pr_info.get("base_sha", ""),
                "head_sha": pr_info.get("head_sha", ""),
            }
        return {"number": None, "title": "", "base_sha": "", "head_sha": ""}

    def _file_risk_rank(self, path: str, findings: list[FindingLike]) -> int:
        file_rank = len(_RISK_PRIORITY)
        for finding in findings:
            matched = any(evidence.file == path for evidence in finding.evidence)
            if not matched:
                continue
            finding_rank = self._finding_risk_rank(finding)
            if finding_rank < file_rank:
                file_rank = finding_rank
        return file_rank

    def _finding_risk_rank(self, finding: FindingLike) -> int:
        tokens: list[str] = [finding.rule_id]
        tokens.extend(finding.tags)
        for token in tokens:
            lowered = str(token).lower()
            for risk, rank in _RISK_PRIORITY.items():
                if risk in lowered:
                    return rank
        return len(_RISK_PRIORITY)

    def _fit_within_budget(self, diff_file: DiffFile, remaining_tokens: int) -> dict[str, object] | None:
        if remaining_tokens <= 0:
            return None

        line_limit = 200
        while line_limit >= 10:
            current_context = self.crop_file_context(diff_file, max_lines=line_limit)
            current_tokens = self.estimate_tokens(json.dumps(current_context, ensure_ascii=False))
            if current_tokens <= remaining_tokens:
                return current_context
            line_limit = line_limit // 2

        return None


__all__ = ["ContextBuilder", "detect_language", "_should_include_file"]
