# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUntypedBaseClass=false, reportMissingSuperCall=false
from __future__ import annotations

import re

import yaml

from pr_guardian.models import Diff, DiffFile, Evidence, Finding, Location, Policy, Severity
from pr_guardian.rules.base import FindingFactory, Rule


class MinPermissionsRule(Rule):
    """强制显式 permissions，是为了把 GitHub Actions 权限收敛成可审计配置。"""

    RULE_ID: str = "ci/min-permissions"
    RULE_TITLE: str = "GitHub Actions 权限最小化检查"
    DEFAULT_SEVERITY: Severity = Severity.ERROR
    RULE_TAGS: list[str] = ["ci", "security", "permissions"]

    _WORKFLOW_FILE_PATTERN: re.Pattern[str] = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
    _RISKY_SCOPES: set[str] = {"contents: write", "packages: write", "actions: write"}
    _OIDC_SCOPE: str = "id-token: write"

    def __init__(self) -> None:
        self._line_entries_by_file: dict[str, list[tuple[int, str]]] = {}
        self._active_allowlist: list[dict[str, object]] = []
        self._active_severity: Severity = self.default_severity

    @property
    def rule_id(self) -> str:
        return self.RULE_ID

    @property
    def title(self) -> str:
        return self.RULE_TITLE

    @property
    def default_severity(self) -> Severity:
        return self.DEFAULT_SEVERITY

    @property
    def tags(self) -> list[str]:
        return self.RULE_TAGS.copy()

    @property
    def description(self) -> str:
        return "检查 workflow 是否声明 permissions，并拦截高风险 write 权限。"

    def execute(self, diff: Diff, policy: Policy) -> list[Finding]:
        findings: list[Finding] = []
        self._active_severity = policy.severity_overrides.get(self.rule_id, self.default_severity)
        self._active_allowlist = self._resolve_permission_allowlist(policy)

        for diff_file in diff.files:
            if not self._is_workflow_file(diff_file):
                continue
            if self.should_skip_file(diff_file.path, policy):
                continue
            if not diff_file.patch:
                continue

            # 同时保留新增行和上下文行，才能在 finding 中回指到稳定的 YAML 行号。
            line_entries = self._extract_new_file_lines(diff_file.patch)
            self._line_entries_by_file[diff_file.path] = line_entries
            workflow = self._parse_workflow("\n".join(line for _, line in line_entries))
            if not workflow:
                continue

            findings.extend(self._check_permissions(workflow, diff_file.path))

        return findings

    def _parse_workflow(self, content: str) -> dict[str, object]:
        try:
            parsed: object = yaml.safe_load(content)  # pyright: ignore[reportAny]
        except yaml.YAMLError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _check_permissions(self, workflow: dict[str, object], file_path: str) -> list[Finding]:
        findings: list[Finding] = []
        line_entries = self._line_entries_by_file.get(file_path, [])

        jobs_value = workflow.get("jobs")
        jobs = jobs_value if isinstance(jobs_value, dict) else {}

        has_workflow_permissions = "permissions" in workflow
        has_job_permissions = any(isinstance(job, dict) and "permissions" in job for job in jobs.values())

        if not has_workflow_permissions and not has_job_permissions:
            # 缺少 permissions 本身就是风险，因为默认权限会随仓库设置漂移。
            line_number, snippet = self._find_line(line_entries, r"^\s*jobs\s*:\s*$")
            findings.append(
                FindingFactory.create(
                    self,
                    severity=self._active_severity,
                    message="workflow 或 job 必须显式声明 permissions，建议使用 `permissions: read-all`。",
                    evidence=[Evidence(file=file_path, line=line_number, snippet=snippet)],
                )
            )
            return findings

        if has_workflow_permissions:
            findings.extend(
                self._check_permission_block(
                    permission_value=workflow.get("permissions"),
                    file_path=file_path,
                    line_entries=line_entries,
                    scope_prefix="workflow",
                )
            )

        for job_name, job in jobs.items():
            if not isinstance(job, dict) or "permissions" not in job:
                continue
            findings.extend(
                self._check_permission_block(
                    permission_value=job.get("permissions"),
                    file_path=file_path,
                    line_entries=line_entries,
                    scope_prefix=f"job `{job_name}`",
                )
            )

        return findings

    def _check_permission_block(
        self,
        permission_value: object,
        file_path: str,
        line_entries: list[tuple[int, str]],
        scope_prefix: str,
    ) -> list[Finding]:
        findings: list[Finding] = []

        if isinstance(permission_value, str) and permission_value.strip().lower() == "write-all":
            line_number, snippet = self._find_line(line_entries, r"^\s*permissions\s*:\s*write-all\s*$")
            findings.append(
                FindingFactory.create(
                    self,
                    severity=self._active_severity,
                    message=f"{scope_prefix} 使用 `permissions: write-all`，权限过宽。",
                    evidence=[Evidence(file=file_path, line=line_number, snippet=snippet)],
                )
            )
            return findings

        if not isinstance(permission_value, dict):
            return findings

        if not self._is_dangerous_permission(permission_value):
            return findings

        for scope_name, access_level in permission_value.items():
            if not isinstance(scope_name, str) or not isinstance(access_level, str):
                continue

            normalized_scope = f"{scope_name.strip().lower()}: {access_level.strip().lower()}"
            if normalized_scope not in self._RISKY_SCOPES and normalized_scope != self._OIDC_SCOPE:
                continue
            if normalized_scope in {"contents: write", "packages: write"} and self._is_scope_allowlisted(
                file_path,
                normalized_scope,
            ):
                # 只对显式例外放行，避免临时授权逐渐演变成默认习惯。
                continue

            line_pattern = rf"^\s*{re.escape(scope_name)}\s*:\s*{re.escape(access_level)}\s*$"
            line_number, snippet = self._find_line(line_entries, line_pattern)
            findings.append(
                FindingFactory.create(
                    self,
                    severity=self._active_severity,
                    message=f"{scope_prefix} 包含高风险权限 `{normalized_scope}`，请改为只读或移除。",
                    evidence=[Evidence(file=file_path, line=line_number, snippet=snippet)],
                )
            )

        return findings

    def _is_workflow_file(self, diff_file: DiffFile) -> bool:
        normalized_path = diff_file.path.replace("\\", "/")
        return self._WORKFLOW_FILE_PATTERN.fullmatch(normalized_path) is not None

    def _extract_new_file_lines(self, patch: str) -> list[tuple[int, str]]:
        line_entries: list[tuple[int, str]] = []
        new_line_number = 0

        for raw_line in patch.splitlines():
            if raw_line.startswith("@@"):
                matched = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", raw_line)
                if matched:
                    new_line_number = int(matched.group(1))
                continue

            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                line_entries.append((new_line_number, raw_line[1:]))
                new_line_number += 1
                continue

            if raw_line.startswith(" "):
                line_entries.append((new_line_number, raw_line[1:]))
                new_line_number += 1

        return line_entries

    def _resolve_permission_allowlist(self, policy: Policy) -> list[dict[str, object]]:
        allowlist = getattr(policy, "allowlist", {})
        if not isinstance(allowlist, dict):
            return []

        configured = allowlist.get("permission_allowlist")
        if configured is None:
            configured = allowlist.get(self.rule_id, [])
        if not isinstance(configured, list):
            return []

        normalized_entries: list[dict[str, object]] = []
        for configured_entry in configured:
            if not isinstance(configured_entry, dict):
                continue

            path_value = configured_entry.get("path")
            scopes_value = configured_entry.get("scopes")
            if not isinstance(path_value, str) or not isinstance(scopes_value, list):
                continue

            normalized_entries.append(
                {
                    "path": path_value.replace("\\", "/"),
                    "scopes": [scope.strip().lower() for scope in scopes_value if isinstance(scope, str)],
                }
            )

        return normalized_entries

    def _is_dangerous_permission(self, permission: dict[str, object]) -> bool:
        for scope_name, access_level in permission.items():
            if not isinstance(access_level, str):
                continue
            normalized_scope = f"{scope_name.strip().lower()}: {access_level.strip().lower()}"
            if normalized_scope in self._RISKY_SCOPES or normalized_scope == self._OIDC_SCOPE:
                return True
        return False

    def _is_scope_allowlisted(self, file_path: str, scope: str) -> bool:
        normalized_path = file_path.replace("\\", "/")
        normalized_scope = scope.strip().lower()

        for allowlist_entry in self._active_allowlist:
            allowlist_path = allowlist_entry.get("path")
            allowlist_scopes = allowlist_entry.get("scopes")
            if not isinstance(allowlist_path, str) or not isinstance(allowlist_scopes, list):
                continue
            if allowlist_path != normalized_path:
                continue
            if normalized_scope in allowlist_scopes:
                return True

        return False

    def _find_line(self, line_entries: list[tuple[int, str]], pattern: str) -> tuple[int, str]:
        matcher = re.compile(pattern)
        for line_number, line_text in line_entries:
            if matcher.fullmatch(line_text):
                return line_number, line_text
        if line_entries:
            return line_entries[0]
        return 1, ""

    def _format_location(self, file_path: str, line_number: int) -> Location:
        return Location(file=file_path, start_line=line_number, end_line=line_number, side="RIGHT")
