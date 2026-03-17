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
    """规则基类，所有具体规则必须继承此基类。"""

    @property
    @abstractmethod
    def rule_id(self) -> str:
        """返回规则唯一标识，如 'security/secrets-scan'。"""

    @property
    @abstractmethod
    def title(self) -> str:
        """返回规则人类可读标题。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """返回规则详细描述。"""

    @property
    @abstractmethod
    def default_severity(self) -> Severity:
        """返回默认严重级别。"""

    @property
    def tags(self) -> list[str]:
        """返回规则标签，如 ['security', 'performance']。"""
        return []

    @abstractmethod
    def execute(self, diff: Diff, policy: Policy) -> list[Finding]:
        """执行规则检查，返回发现列表。"""

    def should_skip_file(self, path: str, policy: Policy) -> bool:
        """判断文件是否应被排除。"""
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
