"""语言栈检测：按仓库文件签名路由到对应 TestAdapter。"""

from __future__ import annotations

from pathlib import Path

from .base import TestAdapter
from .cargo_adapter import CargoAdapter
from .ctest_adapter import CTestAdapter
from .flutter_adapter import FlutterAdapter
from .go_adapter import GoAdapter
from .gradle_adapter import GradleAdapter
from .pytest_adapter import PytestAdapter
from .shell_adapter import ShellAdapter
from .ts_adapter import TSAdapter

ADAPTERS: list[type[TestAdapter]] = [
    PytestAdapter,
    GoAdapter,
    TSAdapter,
    GradleAdapter,
    FlutterAdapter,
    CargoAdapter,
    CTestAdapter,
    ShellAdapter,
]


def detect_stack(path: Path, overrides: dict[str, str] | None = None) -> TestAdapter | None:
    """返回与仓库匹配的 Adapter 实例；overrides 可按 stack 名强制选择。

    若 overrides 指定 stack 且其 Adapter 存在，直接返回该 Adapter（不校验文件签名），
    用于多语言仓库明确指定栈。
    """
    if overrides:
        for adapter_cls in ADAPTERS:
            if adapter_cls.stack in overrides:
                return adapter_cls()

    for adapter_cls in ADAPTERS:
        adapter = adapter_cls()
        if adapter.is_supported(path):
            return adapter
    return None


def list_stacks() -> list[str]:
    return [adapter.stack for adapter in ADAPTERS]


def get_adapter(stack: str) -> TestAdapter | None:
    for adapter_cls in ADAPTERS:
        if adapter_cls.stack == stack:
            return adapter_cls()
    return None


__all__ = ["detect_stack", "list_stacks", "get_adapter", "ADAPTERS"]
