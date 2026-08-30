"""Shell 测试适配器：约定 test.sh 或 Makefile 的 test 目标，解析 exit code。"""

from __future__ import annotations

from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, summarize_cases


class ShellAdapter(TestAdapter):
    stack = "shell"

    def is_supported(self, path: Path) -> bool:
        if (path / "test.sh").exists() or (path / "run_test.sh").exists():
            return True
        makefile = path / "Makefile"
        return makefile.exists() and ("test:" in makefile.read_text(encoding="utf-8", errors="ignore"))

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        if (path / "test.sh").exists():
            return ["bash", "test.sh"]
        if (path / "run_test.sh").exists():
            return ["bash", "run_test.sh"]
        return ["make", "test"]

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        status = TestStatus.PASSED if result.exit_code == 0 else TestStatus.FAILED
        message = result.stdout.strip() if status == TestStatus.FAILED else None
        test_case = TestCase(
            suite="shell",
            name="test.sh" if (path / "test.sh").exists() else "make test",
            status=status,
            failure_message=message,
        )
        result.cases = [test_case]
        count, passed, failed, skipped = summarize_cases(result.cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result


__all__ = ["ShellAdapter"]
