"""Go 测试适配器：go test ./...，解析 go test -json 事件流（Go 无原生 JUnit）。"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, summarize_cases


class GoAdapter(TestAdapter):
    stack = "go"

    def is_supported(self, path: Path) -> bool:
        return (path / "go.mod").exists() or (path / "go.sum").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        return ["go", "test", "./...", "-json"]

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        cases = self._parse_go_json(result.stdout)
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result

    @staticmethod
    def _parse_go_json(stdout: str) -> list[TestCase]:
        cases: list[TestCase] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("Action") != "pass" and event.get("Action") != "fail":
                continue
            if not event.get("Test"):
                continue
            action = event.get("Action")
            if action == "pass":
                status = TestStatus.PASSED
                message = None
            else:
                status = TestStatus.FAILED
                message = event.get("Output", "").strip() or None
            cases.append(
                TestCase(
                    suite=event.get("Package", ""),
                    name=str(event.get("Test", "")),
                    status=status,
                    duration_ms=float(event.get("Elapsed", 0) or 0) * 1000,
                    failure_message=message,
                    file=event.get("Test"),
                )
            )
        return cases


__all__ = ["GoAdapter"]
