"""Rust 测试适配器：cargo test，解析 stdout（Rust 无原生 JUnit 输出）。"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, summarize_cases


class CargoAdapter(TestAdapter):
    stack = "rust"

    def is_supported(self, path: Path) -> bool:
        return (path / "Cargo.toml").exists() or (path / "Cargo.lock").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        return ["cargo", "test"]

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        cases = self._parse_stdout(result.stdout)
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result

    @staticmethod
    def _parse_stdout(stdout: str) -> list[TestCase]:
        cases: list[TestCase] = []
        # 捕获 "test <path>::<name> ... ok" / "FAILED" / "ignored" 行
        ok_pattern = re.compile(r"test ([\w:]+) \.\.\. ok")
        fail_pattern = re.compile(r"test ([\w:]+) \.\.\. FAILED")
        ignore_pattern = re.compile(r"test ([\w:]+) \.\.\. ignored")
        for line in stdout.splitlines():
            stripped = line.strip()
            ok_match = ok_pattern.search(stripped)
            if ok_match:
                cases.append(TestCase(suite="cargo", name=ok_match.group(1), status=TestStatus.PASSED))
                continue
            fail_match = fail_pattern.search(stripped)
            if fail_match:
                cases.append(
                    TestCase(
                        suite="cargo",
                        name=fail_match.group(1),
                        status=TestStatus.FAILED,
                        failure_message=stripped,
                    )
                )
                continue
            ignore_match = ignore_pattern.search(stripped)
            if ignore_match:
                cases.append(TestCase(suite="cargo", name=ignore_match.group(1), status=TestStatus.SKIPPED))
                continue
        return cases


__all__ = ["CargoAdapter"]
