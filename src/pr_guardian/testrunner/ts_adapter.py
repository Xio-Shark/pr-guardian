"""TypeScript/JavaScript 测试适配器：vitest / jest，输出 JUnit（或解析 stdout）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus
from .base import TestAdapter, parse_junit_xml, summarize_cases


class TSAdapter(TestAdapter):
    stack = "typescript"

    def is_supported(self, path: Path) -> bool:
        pkg = path / "package.json"
        if not pkg.exists():
            return False
        try:
            content = pkg.read_text(encoding="utf-8")
        except OSError:
            return False
        return "vitest" in content or "jest" in content or "svelte" in content

    def _detect_runner(self, path: Path) -> str:
        pkg_path = path / "package.json"
        if pkg_path.exists():
            content = pkg_path.read_text(encoding="utf-8")
            if "jest" in content and "vitest" not in content:
                return "jest"
        return "vitest"

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        runner = self._detect_runner(path)
        if runner == "jest":
            command = ["npx", "jest", "--ci"]
            if report_path is not None:
                command += ["--reporters", "default", "--reporters", "jest-junit", "--outputFile", str(report_path)]
            return command
        command = ["npx", "vitest", "run"]
        if report_path is not None:
            command += ["--reporter", "junit", "--outputFile", str(report_path)]
        return command

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        if report_path is not None and report_path.exists():
            cases = parse_junit_xml(report_path)
        else:
            cases = self._parse_stdout(result.stdout)
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result

    @staticmethod
    def _parse_stdout(stdout: str) -> list[TestCase]:
        cases: list[TestCase] = []
        lines = stdout.splitlines()
        for line in lines:
            match = re.search(r"(\d+)\s*(passed|failed|skipped)", line)
            if not match:
                continue
            count = int(match.group(1))
            status = match.group(2)
            if status == "passed":
                cases.append(TestCase(suite="vitest", name=f"passed:{count}", status=TestStatus.PASSED))
            elif status == "failed":
                cases.append(TestCase(suite="vitest", name=f"failed:{count}", status=TestStatus.FAILED))
            else:
                cases.append(TestCase(suite="vitest", name=f"skipped:{count}", status=TestStatus.SKIPPED))
        # 无逐用例 JSON 时至少给出计数；若有 json 行再细解析
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            test_name = event.get("name")
            if not test_name:
                continue
            status = TestStatus.PASSED if event.get("result") == "pass" else TestStatus.FAILED
            cases.append(
                TestCase(
                    suite="vitest",
                    name=str(test_name),
                    status=status,
                    failure_message=event.get("error", {}).get("message") if status == TestStatus.FAILED else None,
                )
            )
        return cases


__all__ = ["TSAdapter"]
