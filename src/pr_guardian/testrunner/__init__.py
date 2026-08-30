"""多语言测试执行层，对外暴露 detect/run 便捷入口。"""

from __future__ import annotations

from pathlib import Path

from ..models import TestRunResult
from .base import TestAdapter, parse_junit_xml, summarize_cases
from .detect import detect_stack, get_adapter, list_stacks

__all__ = [
    "TestAdapter",
    "TestRunResult",
    "detect_stack",
    "get_adapter",
    "list_stacks",
    "parse_junit_xml",
    "summarize_cases",
]


def run_tests(path: Path, overrides: dict[str, str] | None = None) -> TestRunResult | None:
    """便捷入口：检测栈并执行测试；无法识别时返回 None。"""
    adapter = detect_stack(path, overrides)
    if adapter is None:
        return None
    return adapter.run(path)
