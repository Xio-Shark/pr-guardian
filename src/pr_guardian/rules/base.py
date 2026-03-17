# pyright: reportMissingImports=false
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ..models import Diff, DiffFile, Evidence, Finding, Policy, Severity


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def match_glob_pattern(path: str, pattern: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_pattern = _normalize_path(pattern)

    regex_parts: list[str] = []
    index = 0
    while index < len(normalized_pattern):
        char = normalized_pattern[index]
        if char == "*":
            next_index = index + 1
            has_double_star = next_index < len(normalized_pattern) and normalized_pattern[next_index] == "*"
            if has_double_star:
                index = next_index
                if index + 1 < len(normalized_pattern) and normalized_pattern[index + 1] == "/":
                    # 把 "**/" 视为可跨越任意目录层级，才能同时匹配根目录和嵌套目录。
                    regex_parts.append("(?:.*/)?")
                    index += 1
                else:
                    regex_parts.append(".*")
            else:
                regex_parts.append("[^/]*")
        else:
            regex_parts.append(re.escape(char))
        index += 1

    regex = "^" + "".join(regex_parts) + "$"
    return re.fullmatch(regex, normalized_path) is not None


def path_matches_any(path: str, patterns: list[str]) -> bool:
    return any(match_glob_pattern(path, pattern) for pattern in patterns)


class Rule(ABC):
    """把规则契约收敛到统一接口，避免各规则各自定义输出格式。"""

    @property
    @abstractmethod
    def rule_id(self) -> str:
        """要求稳定 ID，是为了让配置覆盖、去重和回写引用能跨运行保持一致。"""

    @property
    @abstractmethod
    def title(self) -> str:
        """保留人类可读标题，是为了让输出面向审查者而不是内部实现细节。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """保留规则意图说明，是为了让扩展和排障时能快速理解适用边界。"""

    @property
    @abstractmethod
    def default_severity(self) -> Severity:
        """提供默认级别，是为了在没有策略覆盖时仍能稳定做门禁。"""

    @property
    def tags(self) -> list[str]:
        """标签让报告分组和上下文排序能复用同一套风险信号。"""
        return []

    @abstractmethod
    def execute(self, diff: Diff, policy: Policy) -> list[Finding]:
        """统一执行入口，是为了让主流程无需感知具体规则实现。"""

    def should_skip_file(self, path: str, policy: Policy) -> bool:
        """先做范围裁剪，是为了让所有规则遵循同一套 include/exclude 语义。"""
        if policy.include and not path_matches_any(path, policy.include):
            return True
        if policy.exclude and path_matches_any(path, policy.exclude):
            return True
        return False


class FindingFactory:
    _counters: dict[str, int] = {}

    @classmethod
    def create(
        cls,
        rule: Rule,
        message: str,
        evidence: list[Evidence],
        severity: Severity | None = None,
    ) -> Finding:
        # 用递增后缀生成稳定 ID，方便测试断言和多通道输出引用同一 finding。
        current_count = cls._counters.get(rule.rule_id, 0) + 1
        cls._counters[rule.rule_id] = current_count
        effective_severity = severity or rule.default_severity

        return Finding(
            id=f"{rule.rule_id}#{current_count}",
            rule_id=rule.rule_id,
            title=rule.title,
            severity=effective_severity,
            message=message,
            evidence=evidence,
            tags=rule.tags,
            confidence=1.0,
        )


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, type[Rule]] = {}

    def register(self, rule_class: type[Rule]) -> None:
        rule_id = rule_class().rule_id
        self._rules[rule_id] = rule_class

    def get(self, rule_id: str) -> type[Rule] | None:
        return self._rules.get(rule_id)

    def list_rules(self) -> list[str]:
        return sorted(self._rules.keys())

    def create_instance(self, rule_id: str) -> Rule | None:
        rule_class = self.get(rule_id)
        if rule_class is None:
            return None
        return rule_class()


__all__ = [
    "Rule",
    "FindingFactory",
    "RuleRegistry",
    "match_glob_pattern",
    "path_matches_any",
    "DiffFile",
    "Evidence",
]
