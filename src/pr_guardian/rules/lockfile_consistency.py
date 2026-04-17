# pyright: reportMissingImports=false
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ..models import Diff, DiffFile, Evidence, Finding, Policy, Severity

from .base import FindingFactory, Rule


class LockfileConsistencyRule(Rule):
    """把 manifest 和 lockfile 成对校验，避免依赖解析结果只在本地机器上变化。"""

    MANIFEST_TO_LOCKFILE: dict[str, list[str]] = {
        "package.json": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
        "pyproject.toml": ["poetry.lock", "Pipfile.lock"],
        "setup.py": ["poetry.lock"],
        "requirements.txt": ["poetry.lock"],
        "Gemfile": ["Gemfile.lock"],
        "go.mod": ["go.sum"],
        "Cargo.toml": ["Cargo.lock"],
        "pom.xml": [],
        "build.gradle": ["gradle.lockfile"],
    }

    @property
    def description(self) -> str:
        return "当依赖清单变更时，要求对应 lockfile 一并变更。"

    @property
    def rule_id(self) -> str:
        return "deps/lockfile-consistency"

    @property
    def title(self) -> str:
        return "Lockfile 一致性检查"

    @property
    def default_severity(self) -> Severity:
        return Severity.ERROR

    @property
    def tags(self) -> list[str]:
        return ["dependencies", "lockfile"]

    def __init__(self) -> None:
        self._runtime_manifest_mapping: dict[str, list[str]] = dict(self.MANIFEST_TO_LOCKFILE)

    def execute(self, diff: Diff, policy: Policy) -> list[Finding]:
        self._runtime_manifest_mapping = self._manifest_to_lockfile(policy)
        findings: list[Finding] = []
        for changed_file in diff.files:
            if self.should_skip_file(changed_file.path, policy):
                continue
            if not self._is_manifest_file(changed_file.path):
                continue
            if self._has_corresponding_lockfile_changed(diff, changed_file.path):
                continue

            expected_lockfiles = self._get_expected_lockfiles(changed_file.path)
            if not expected_lockfiles:
                continue

            effective_severity = policy.severity_overrides.get(self.rule_id, self.default_severity)
            finding = FindingFactory.create(
                rule=self,
                severity=effective_severity,
                message=(
                    f"检测到 `{changed_file.path}` 变更，但未发现对应 lockfile 变更。"
                    f"预期之一: {', '.join(expected_lockfiles)}"
                ),
                evidence=[self._build_evidence(changed_file)],
            )
            findings.append(finding)

        return findings

    def _is_manifest_file(self, path: str) -> bool:
        file_name = PurePosixPath(path.replace("\\", "/")).name
        return file_name in self._runtime_manifest_mapping

    def _get_expected_lockfiles(self, manifest_path: str) -> list[str]:
        file_name = PurePosixPath(manifest_path.replace("\\", "/")).name
        return self._runtime_manifest_mapping.get(file_name, [])

    def _has_corresponding_lockfile_changed(self, diff: Diff, manifest_path: str) -> bool:
        expected_lockfiles = self._get_expected_lockfiles(manifest_path)
        if not expected_lockfiles:
            return True

        normalized_manifest_path = PurePosixPath(manifest_path.replace("\\", "/"))
        manifest_parent = normalized_manifest_path.parent
        changed_paths = {PurePosixPath(changed.path.replace("\\", "/")) for changed in diff.files}

        for lockfile_name in expected_lockfiles:
            # 只检查同目录兄弟 lockfile，才能避免 monorepo 中同名文件互相误判。
            lockfile_path = manifest_parent / lockfile_name
            if lockfile_path in changed_paths:
                return True
        return False

    def _manifest_to_lockfile(self, policy: Policy) -> dict[str, list[str]]:
        manifest_to_lockfile = dict(self.MANIFEST_TO_LOCKFILE)
        custom_mapping = self._extract_custom_mapping(policy)
        manifest_to_lockfile.update(custom_mapping)
        return manifest_to_lockfile

    def _extract_custom_mapping(self, policy: Policy) -> dict[str, list[str]]:
        raw_mapping: Any = getattr(policy, "lockfile_mappings", {})
        if not isinstance(raw_mapping, dict):
            return {}

        normalized_mapping: dict[str, list[str]] = {}
        for manifest_name, lockfiles in raw_mapping.items():
            if not isinstance(manifest_name, str):
                continue
            if not isinstance(lockfiles, list):
                continue
            normalized_lockfiles = [lockfile for lockfile in lockfiles if isinstance(lockfile, str)]
            normalized_mapping[manifest_name] = normalized_lockfiles
        return normalized_mapping

    def _build_evidence(self, diff_file: DiffFile) -> Evidence:
        return Evidence(file=diff_file.path, line=1, snippet=diff_file.patch or "manifest changed")
