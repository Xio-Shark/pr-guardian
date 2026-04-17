# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportImplicitOverride=false, reportUnknownParameterType=false
from __future__ import annotations

import fnmatch

from ..models import Diff, DiffFile, Evidence, Finding, Policy, Severity
from .base import FindingFactory, Rule

DEFAULT_PATH_TO_TEST = {
    "src/**/*.py": ["tests/**/*.py"],
    "*/src/**/*.py": ["*/tests/**/*.py"],
    "packages/*/src/**/*.js": ["packages/*/tests/**/*.test.js"],
    "packages/*/src/**/*.ts": ["packages/*/tests/**/*.test.ts"],
    "lib/**/*.py": ["test/**/*.py"],
}


class AffectedTestsRule(Rule):
    """要求 PR 主动声明测试，是为了让受影响范围和验证动作在评审时一起出现。"""

    _RULE_ID: str = "monorepo/affected-tests"
    _TITLE: str = "受影响测试检查"
    _DEFAULT_SEVERITY: Severity = Severity.WARNING
    _TAGS: list[str] = ["monorepo", "testing"]
    TEST_LABEL_PATTERNS: list[str] = ["tests:", "test:"]

    @property
    def rule_id(self) -> str:
        return self._RULE_ID

    @property
    def title(self) -> str:
        return self._TITLE

    @property
    def default_severity(self) -> Severity:
        return self._DEFAULT_SEVERITY

    @property
    def tags(self) -> list[str]:
        return self._TAGS

    @property
    def description(self) -> str:
        return "当代码路径变更时，检查 PR 是否声明已执行对应测试。"

    def execute(
        self,
        diff: Diff,
        policy: Policy,
        pr_body: str = "",
        test_mappings: dict[str, list[str]] | None = None,
    ) -> list[Finding]:
        mappings = self._resolve_mappings(policy=policy, override_mappings=test_mappings)

        # 先聚合影响范围再给单条 finding，避免一个 PR 因多个文件刷出重复提醒。
        all_affected_tests: set[str] = set()
        impacted_files: list[str] = []
        for changed_file in diff.files:
            if self.should_skip_file(changed_file.path, policy):
                continue
            affected_paths = self._get_affected_test_paths(changed_file.path, mappings)
            if affected_paths:
                all_affected_tests.update(affected_paths)
                impacted_files.append(changed_file.path)

        if not all_affected_tests:
            return []

        affected_test_paths = sorted(all_affected_tests)
        if self._is_test_declared(pr_body=pr_body, test_paths=affected_test_paths):
            return []

        evidence = [self._build_evidence(path, affected_test_paths) for path in impacted_files]
        severity = policy.severity_overrides.get(self.rule_id, self.default_severity)
        message = f"检测到改动可能影响测试套件: {', '.join(affected_test_paths)}；请在 PR 描述中声明已执行测试。"
        finding = FindingFactory.create(self, message=message, evidence=evidence, severity=severity)
        return [finding]

    def _resolve_mappings(
        self,
        policy: Policy,
        override_mappings: dict[str, list[str]] | None,
    ) -> dict[str, list[str]]:
        if override_mappings:
            return override_mappings

        policy_mappings = getattr(policy, "test_mappings", None)
        if isinstance(policy_mappings, dict):
            normalized_mappings: dict[str, list[str]] = {}
            for source_pattern, test_paths in policy_mappings.items():
                if not isinstance(source_pattern, str) or not isinstance(test_paths, list):
                    continue
                filtered_paths = [path for path in test_paths if isinstance(path, str)]
                if filtered_paths:
                    normalized_mappings[source_pattern] = filtered_paths
            if normalized_mappings:
                return normalized_mappings

        return DEFAULT_PATH_TO_TEST

    def _build_evidence(self, changed_file: str, affected_test_paths: list[str]) -> Evidence:
        return Evidence(
            file=changed_file,
            line=1,
            snippet=f"映射到测试: {', '.join(affected_test_paths)}",
        )

    def _get_affected_test_paths(self, changed_file: str, mappings: dict[str, list[str]]) -> list[str]:
        affected_paths: list[str] = []
        for source_pattern, test_paths in mappings.items():
            if not self._match_glob_pattern(changed_file, source_pattern):
                continue
            for test_path in test_paths:
                if test_path not in affected_paths:
                    affected_paths.append(test_path)
        return affected_paths

    def _is_test_declared(self, pr_body: str, test_paths: list[str]) -> bool:
        normalized_body = pr_body.lower().strip()
        if not normalized_body:
            return False

        if any(pattern in normalized_body for pattern in self.TEST_LABEL_PATTERNS):
            return True

        # 接受通用测试命令标记，是为了兼容团队只写执行命令而不逐个列文件的习惯。
        broad_markers = ["test", "tests", "- [x] test", "- [x] tests", "pytest", "jest", "go test"]
        if any(marker in normalized_body for marker in broad_markers):
            return True

        for test_path in test_paths:
            if test_path.lower() in normalized_body:
                return True
        return False

    def _match_glob_pattern(self, path: str, pattern: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("./")
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")

        if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
            return True

        if "**/" not in normalized_pattern:
            return False

        wildcard_collapsed = normalized_pattern
        while "**/" in wildcard_collapsed:
            # 额外裁剪 "**/"，是为了让映射模式同时覆盖根目录和嵌套包结构。
            wildcard_collapsed = wildcard_collapsed.replace("**/", "", 1)
            if fnmatch.fnmatchcase(normalized_path, wildcard_collapsed):
                return True
        return False


__all__ = ["AffectedTestsRule", "DEFAULT_PATH_TO_TEST", "DiffFile"]
