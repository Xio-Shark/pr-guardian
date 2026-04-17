"""把核心数据结构收敛到同一套模型，避免规则层、LLM 层和回写层各自解释 PR 语义。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """统一严重级别，是为了让门禁和 GitHub 输出共用同一套判定标准。"""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Location(BaseModel):
    """保留 PR 左右侧坐标，是为了让回写评论能稳定落到 diff 上。"""

    file: str
    start_line: int | None = None
    end_line: int | None = None
    side: Literal["LEFT", "RIGHT"] | None = None


class Evidence(BaseModel):
    """要求证据结构化，是为了让 finding 可追溯且便于后续自动化消费。"""

    file: str
    line: int
    snippet: str


class FixSuggestion(BaseModel):
    """把修复建议建模成范围替换，是为了兼容后续自动修复和 UI 展示。"""

    description: str
    replacement: str | None = None
    file: str
    line_start: int
    line_end: int


class Finding(BaseModel):
    """统一 finding 结构，是为了让规则层和 LLM 层产出可以直接汇总。"""

    id: str
    rule_id: str
    title: str
    severity: Severity
    message: str
    evidence: list[Evidence]
    tags: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    fix: FixSuggestion | None = None


class Hunk(BaseModel):
    """保存行级映射，是为了让后续规则和评论定位不必重新解析 patch。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, int | None, int | None]]


class DiffFile(BaseModel):
    """把文件变更和 hunk 打包，是为了让规则只关心审查语义而不关心来源。"""

    path: str
    status: Literal["added", "removed", "modified", "renamed"]
    patch: str | None = None
    additions: int
    deletions: int
    hunks: list[Hunk]


class Diff(BaseModel):
    """保留 PR 级 diff 视图，是为了让跨文件规则能在同一入口上工作。"""

    files: list[DiffFile]


class Policy(BaseModel):
    """把执行开关集中建模，是为了让规则、LLM 和回写层读取同一份策略。"""

    gate: bool
    auto_fix: bool
    include: list[str]
    exclude: list[str]
    enabled_rules: list[str]
    severity_overrides: dict[str, Severity]
    allowlist: dict[str, list[str]] = Field(default_factory=dict)
    lockfile_mappings: dict[str, list[str]] = Field(default_factory=dict)
    llm_enabled: bool
    llm_provider: str
    llm_model: str
    llm_max_context_tokens: int
    llm_budget_usd: float
    deny_paths: list[str]
    max_changed_lines_for_autofix: int
    require_evidence: bool


__all__ = [
    "Severity",
    "Location",
    "Evidence",
    "Finding",
    "FixSuggestion",
    "Diff",
    "DiffFile",
    "Hunk",
    "Policy",
]
