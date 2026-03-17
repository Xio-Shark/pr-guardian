# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUntypedBaseClass=false
from __future__ import annotations

from pr_guardian.models import Diff, DiffFile, Evidence, Finding, Policy, Severity
from pr_guardian.rules.base import FindingFactory, Rule, path_matches_any


class ChangelogBreakingRule(Rule):
    """把 breaking change 和发布说明绑定，避免 API 变更只存在于代码而没有对外沟通。"""

    @property
    def rule_id(self) -> str:
        return "quality/changelog-breaking"

    @property
    def title(self) -> str:
        return "Breaking Change Changelog 检查"

    @property
    def default_severity(self) -> Severity:
        return Severity.ERROR

    @property
    def tags(self) -> list[str]:
        return ["quality", "changelog", "semver"]

    BREAKING_KEYWORDS: list[str] = [
        "BREAKING CHANGE",
        "breaking change",
        "!: ",
    ]

    PUBLIC_API_PATTERNS: list[str] = [
        "src/public/**",
        "api/**",
        "packages/*/src/index.*",
    ]

    CHANGELOG_PATTERNS: list[str] = [
        "CHANGELOG*",
        "**/CHANGELOG*",
        "changelog*",
        "**/changelog*",
        ".changeset/**",
    ]

    VERSION_PATTERNS: list[str] = [
        "package.json",
        "**/package.json",
        "pyproject.toml",
        "**/pyproject.toml",
        "setup.py",
        "**/setup.py",
        "VERSION",
        "**/VERSION",
        "version.txt",
        "**/version.txt",
    ]

    _public_api_patterns: list[str]
    _changelog_patterns: list[str]

    @property
    def description(self) -> str:
        return "当 PR 包含 breaking changes 时，要求同步更新 changelog 或版本信息。"

    def execute(self, diff: Diff, policy: Policy, pr_title: str = "", pr_body: str = "") -> list[Finding]:
        # 优先允许策略覆盖路径约定，是为了兼容不同仓库的发布目录结构。
        self._public_api_patterns = self._resolve_public_api_patterns(policy)
        self._changelog_patterns = self._resolve_changelog_patterns(policy)

        if not self._has_breaking_change(diff, pr_title, pr_body):
            return []

        if self._has_changelog_update(diff):
            return []

        severity = policy.severity_overrides.get(self.rule_id, self.default_severity)
        evidence = [self._build_primary_evidence(diff, pr_title, pr_body)]
        finding = FindingFactory.create(
            rule=self,
            severity=severity,
            message=(
                "检测到可能的 breaking change，但未发现 changelog/version 更新。"
                "请补充 CHANGELOG、.changeset 或版本文件变更。"
            ),
            evidence=evidence,
        )
        return [finding]

    def _has_breaking_change(self, diff: Diff, pr_title: str, pr_body: str) -> bool:
        combined_text = f"{pr_title}\n{pr_body}"
        if any(keyword in combined_text for keyword in self.BREAKING_KEYWORDS):
            return True

        for diff_file in diff.files:
            # 标题没写 breaking 时，公共 API 变更仍然是值得提醒的强信号。
            if self._is_public_api_change(diff_file):
                return True

        return False

    def _is_public_api_change(self, diff_file: DiffFile) -> bool:
        if not path_matches_any(diff_file.path, self._public_api_patterns):
            return False
        if diff_file.status == "removed":
            return True
        return diff_file.status in {"added", "modified", "renamed"}

    def _has_changelog_update(self, diff: Diff) -> bool:
        for diff_file in diff.files:
            if path_matches_any(diff_file.path, self._changelog_patterns):
                return True
            if path_matches_any(diff_file.path, self.VERSION_PATTERNS):
                return True
        return False

    def _resolve_public_api_patterns(self, policy: Policy) -> list[str]:
        custom_patterns = self._read_string_list(policy, "public_api_paths")
        if custom_patterns:
            return custom_patterns
        return self.PUBLIC_API_PATTERNS

    def _resolve_changelog_patterns(self, policy: Policy) -> list[str]:
        custom_patterns = self._read_string_list(policy, "changelog_files")
        return custom_patterns or self.CHANGELOG_PATTERNS

    def _read_string_list(self, policy: Policy, field_name: str) -> list[str]:
        raw_value: object = getattr(policy, field_name, [])
        if not isinstance(raw_value, list):
            return []
        return [value for value in raw_value if isinstance(value, str)]

    def _build_primary_evidence(self, diff: Diff, pr_title: str, pr_body: str) -> Evidence:
        if any(keyword in f"{pr_title}\n{pr_body}" for keyword in self.BREAKING_KEYWORDS):
            # 优先引用 PR 元信息，是为了把问题直接指向开发者声明入口。
            snippet = (pr_title or pr_body or "PR 元信息包含 breaking change 标记")[:200]
            file_name = diff.files[0].path if diff.files else "pull_request"
            return Evidence(file=file_name, line=1, snippet=snippet)

        for diff_file in diff.files:
            if self._is_public_api_change(diff_file):
                return Evidence(file=diff_file.path, line=1, snippet="公共 API 路径发生变更")

        fallback_file = diff.files[0].path if diff.files else "pull_request"
        return Evidence(file=fallback_file, line=1, snippet="检测到潜在 breaking change")
