"""Java/Kotlin 测试适配器：Gradle test，解析 build/test-results/test/*.xml。"""

from __future__ import annotations

from pathlib import Path

from ..models import TestRunResult
from .base import TestAdapter, parse_junit_xml, summarize_cases


class GradleAdapter(TestAdapter):
    stack = "java"

    def is_supported(self, path: Path) -> bool:
        return (path / "build.gradle").exists() or (path / "build.gradle.kts").exists() or (path / "pom.xml").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        gradlew = path / "gradlew"
        if gradlew.exists():
            return [str(gradlew), "test"]
        return ["gradle", "test"]

    def _default_report_dir(self, path: Path) -> Path:
        return path / "build" / "test-results" / "test"

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        report_dir = report_path if report_path is not None else self._default_report_dir(path)
        cases = []
        if report_dir.is_dir():
            for xml_file in sorted(report_dir.glob("*.xml")):
                cases.extend(parse_junit_xml(xml_file))
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result


__all__ = ["GradleAdapter"]
