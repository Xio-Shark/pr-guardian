"""测试执行抽象层：每种语言栈用一个 Adapter 封装检测、命令构建、报告解析。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import TestCase, TestRunResult, TestStatus


class TestAdapter(ABC):
    """把语言栈差异收敛到统一接口，避免主流程塞满 if-else。"""

    stack: str = ""

    @abstractmethod
    def is_supported(self, path: Path) -> bool:
        """按仓库根目录签名判断是否支持该语言栈。"""

    @abstractmethod
    def build_command(self, path: Path, report_path: Path | None = None) -> list[str]:
        """构建测试命令；需要生成 JUnit 报告时传入 report_path。"""

    @abstractmethod
    def parse_report(self, result: TestRunResult, path: Path, report_path: Path | None = None) -> TestRunResult:
        """把命令输出 / JUnit 文件解析成统一 TestCase 列表。"""

    def run(self, path: Path, report_path: Path | None = None) -> TestRunResult:
        """默认真实执行入口：构建命令 → 执行 → 解析 → 返回统一结果。"""
        from .executor import run_command

        command = self.build_command(path, report_path)
        exec_result = run_command(command, cwd=path)
        result = TestRunResult(
            stack=self.stack,
            command=command,
            exit_code=exec_result["exit_code"],
            duration_ms=exec_result["duration_ms"],
            count=0,
            passed=0,
            failed=0,
            skipped=0,
            stdout=exec_result["stdout"],
            junit_path=str(report_path) if report_path else None,
        )
        return self.parse_report(result, path, report_path)


def parse_junit_xml(xml_path: Path) -> list[TestCase]:
    """通用 JUnit XML 解析器（pytest / vitest / gradle / ctest 共用）。"""
    import xml.etree.ElementTree as ET

    root = ET.parse(str(xml_path)).getroot()
    cases: list[TestCase] = []
    for suite in root.iter("testsuite"):
        suite_name = suite.get("name") or ""
        for testcase in suite.iter("testcase"):
            name = testcase.get("name") or ""
            duration_secs = float(testcase.get("time") or 0)
            file_name = testcase.get("file") or testcase.get("classname")
            line_num = _safe_int(testcase.get("line"))
            failure = testcase.find("failure")
            error = testcase.find("error")
            skipped = testcase.find("skipped")
            if error is not None:
                status = TestStatus.ERROR
                message = _clean_xml_text(error.get("message") or "")
            elif failure is not None:
                status = TestStatus.FAILED
                message = _clean_xml_text(failure.get("message") or "")
            elif skipped is not None:
                status = TestStatus.SKIPPED
                message = _clean_xml_text(skipped.get("message") or "")
            else:
                status = TestStatus.PASSED
                message = None
            cases.append(
                TestCase(
                    suite=suite_name,
                    name=name,
                    status=status,
                    duration_ms=duration_secs * 1000,
                    failure_message=message,
                    file=file_name,
                    line=line_num,
                )
            )
    return cases


def summarize_cases(cases: list[TestCase]) -> tuple[int, int, int, int]:
    """按状态汇总计数，避免各报告层重复实现统计。"""
    passed = sum(1 for c in cases if c.status == TestStatus.PASSED)
    failed = sum(1 for c in cases if c.status == TestStatus.FAILED)
    skipped = sum(1 for c in cases if c.status == TestStatus.SKIPPED)
    error = sum(1 for c in cases if c.status == TestStatus.ERROR)
    return len(cases), passed, failed + error, skipped


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _clean_xml_text(text: str) -> str:
    return text.strip()[:500]


__all__ = ["TestAdapter", "parse_junit_xml", "summarize_cases"]
