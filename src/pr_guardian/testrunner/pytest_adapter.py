"""Python 测试适配器：pytest，输出 JUnit XML。"""

from __future__ import annotations

from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, parse_junit_xml, summarize_cases


class PytestAdapter(TestAdapter):
    stack = "python"

    def is_supported(self, path: Path) -> bool:
        return (path / "pyproject.toml").exists() or (path / "pytest.ini").exists() or (path / "setup.cfg").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        command = ["python3", "-m", "pytest"]
        if report_path is not None:
            command += ["--junit-xml", str(report_path)]
        return command

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        if report_path is not None and report_path.exists():
            cases = parse_junit_xml(report_path)
        else:
            cases = self._parse_stdout(result.stdout)
        result.cases = cases
        total, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = total, passed, failed, skipped
        return result

    def _parse_stdout(self, stdout: str) -> list[TestCase]:
        # 兜底：没有 JUnit 时从 "N passed, M failed" 统计行解析
        import re

        cases: list[TestCase] = []
        for line in stdout.splitlines():
            match = re.search(r"(\d+)\s+passed", line)
            if match:
                cases.append(TestCase(suite="pytest-suite", name=f"passed:{match.group(1)}", status=TestStatus.PASSED))
        return cases


__all__ = ["PytestAdapter"]
