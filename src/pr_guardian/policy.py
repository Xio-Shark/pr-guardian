from copy import deepcopy
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_ENABLED_RULES = [
    "security/secrets-scan",
    "deps/lockfile-consistency",
    "monorepo/affected-tests",
    "ci/min-permissions",
    "quality/changelog-breaking",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": {"gate": True, "auto_fix": False},
    "scope": {"include": ["**/*"], "exclude": []},
    "rules": {
        "enabled": DEFAULT_ENABLED_RULES,
        "severity_overrides": {},
        "allowlist": {},
        "lockfile_mappings": {},
    },
    "llm": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o",
        "max_context_tokens": 8000,
        "budget_usd_per_pr": 0.50,
        "strategy": {
            "only_when": ["large_diff", "security_related"],
            "rubric": ["correctness", "security", "performance"],
        },
    },
    "policy": {
        "deny_paths": [".github/workflows/**"],
        "max_changed_lines_for_autofix": 50,
        "require_evidence": True,
    },
}

SUPPORTED_LLM_PROVIDERS = {
    "openai",
    "anthropic",
    "azure-openai",
    "glm",
    "minimax",
    "kimi",
}


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class LLMStrategy(BaseModel):
    only_when: list[str] = Field(default_factory=lambda: ["large_diff", "security_related"])
    rubric: list[str] = Field(default_factory=lambda: ["correctness", "security", "performance"])


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai"
    model: str = "gpt-4o"
    max_context_tokens: int = 8000
    budget_usd_per_pr: float = 0.50
    strategy: LLMStrategy = Field(default_factory=LLMStrategy)


class SecurityPolicy(BaseModel):
    deny_paths: list[str] = Field(default_factory=lambda: [".github/workflows/**"])
    max_changed_lines_for_autofix: int = 50
    require_evidence: bool = True


class Policy(BaseModel):
    gate: bool = True
    auto_fix: bool = False
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)
    enabled_rules: list[str] = Field(default_factory=lambda: deepcopy(DEFAULT_ENABLED_RULES))
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)
    allowlist: dict[str, list[str]] = Field(default_factory=dict)
    lockfile_mappings: dict[str, list[str]] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    policy: SecurityPolicy = Field(default_factory=SecurityPolicy)


class PolicyLoader:
    def load(self, config_path: Path | None = None) -> Policy:
        target_path = config_path or (Path.cwd() / ".pr-guardian.yml")
        raw_config = _load_yaml(target_path)
        merged_config = _apply_defaults(raw_config)
        return Policy(
            gate=merged_config["mode"]["gate"],
            auto_fix=merged_config["mode"]["auto_fix"],
            include=merged_config["scope"]["include"],
            exclude=merged_config["scope"]["exclude"],
            enabled_rules=merged_config["rules"]["enabled"],
            severity_overrides=merged_config["rules"]["severity_overrides"],
            allowlist=merged_config["rules"]["allowlist"],
            lockfile_mappings=merged_config["rules"]["lockfile_mappings"],
            llm=merged_config["llm"],
            policy=merged_config["policy"],
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"配置文件根节点必须是对象: {path}")
    return loaded


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    # 深合并默认配置，是为了允许用户只覆盖局部字段而不重复整份策略。
    merged = deepcopy(base)
    for key, value in override.items():
        current_value = merged.get(key)
        if isinstance(current_value, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current_value, value)
        else:
            merged[key] = value
    return merged


def _apply_defaults(config: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(DEFAULT_CONFIG, config)


def validate_policy(policy: Policy) -> list[str]:
    errors: list[str] = []

    if policy.llm.provider not in SUPPORTED_LLM_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_LLM_PROVIDERS))
        errors.append(f"llm.provider 不支持: {policy.llm.provider}，可选值: {supported}")

    for rule_id in policy.severity_overrides:
        if rule_id not in policy.enabled_rules:
            errors.append(f"severity_overrides 包含未启用规则: {rule_id}")

    conflict_patterns = sorted(set(policy.include).intersection(policy.exclude))
    for pattern in conflict_patterns:
        errors.append(f"include/exclude 存在冲突模式: {pattern}")

    return errors


__all__ = [
    "DEFAULT_CONFIG",
    "LLMConfig",
    "LLMStrategy",
    "Policy",
    "PolicyLoader",
    "SecurityPolicy",
    "validate_policy",
    "_apply_defaults",
    "_load_yaml",
]
