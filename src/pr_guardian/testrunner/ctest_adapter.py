"""C++/CMake 测试适配器：ctest，解析 CTest XML 报告。"""

from __future__ import annotations

from pathlib import Path

from ..models import TestRunResult
from .base import TestAdapter, parse_junit_xml, summarize_cases


class CTestAdapter(TestAdapter):
    stack = "cpp"

    def is_supported(self, path: Path) -> bool:
        return (path / "CMakeLists.txt").exists() or (path / "CMakeCache.txt").exists()

    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        build_dir = path / "build"
        return ["ctest", "--test-dir", str(build_dir), "--output-on-failure"]

    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        # CTest 在 build/Testing/Temporary/LastTest.log 有日志，xml 在 build/Testing/*.xml
        build_dir = path / "build"
        cases = []
        # 优先 CTest XML（LastTestsFailed 或 Testing XML）
        testing_dir = build_dir / "Testing"
        if testing_dir.is_dir():
            for xml_file in sorted(testing_dir.glob("*.xml")):
                cases.extend(parse_junit_xml(xml_file))
        result.cases = cases
        count, passed, failed, skipped = summarize_cases(cases)
        result.count, result.passed, result.failed, result.skipped = count, passed, failed, skipped
        return result


__all__ = ["CTestAdapter"]
