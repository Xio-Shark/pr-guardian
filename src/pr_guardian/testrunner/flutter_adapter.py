"""Dart/Flutter 测试适配器：flutter test，解析 --machine JSON 事件流。"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, summarize_cases


class FlutterAdapter(TestAdapter):
    stack = "dart"

    def is_supported(self, path: Path) -> bool:
        if (path / "pubspec.yaml").exists():
            return True
        return (path / "pubspec.lock").exists() or (path / "lib" / "main.dart").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        if (path / "pubspec.yaml").exists():
            return ["flutter", "test", "--machine"]
        return ["dart", "test", "--machine"]

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        cases = self._parse_machine_json(result.stdout)
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result

    @staticmethod
    def _parse_machine_json(stdout: str) -> list[TestCase]:
        cases: list[TestCase] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type != "testDone":
                continue
            test_name = event.get("testID")
            result_status = event.get("result")
            if result_status == "success":
                status = TestStatus.PASSED
                message = None
            elif result_status == "failure":
                status = TestStatus.FAILED
                message = str(event.get("error", "")) or None
            else:
                status = TestStatus.SKIPPED
                message = None
            cases.append(
                TestCase(
                    suite="flutter",
                    name=str(test_name or ""),
                    status=status,
                    failure_message=message,
                )
            )
        return cases


__all__ = ["FlutterAdapter"]
