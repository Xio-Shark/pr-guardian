"""测试结果回写：把 TestRunResult 映射为 GitHub Check Run + PR 汇总评论。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pr_guardian.github_api import GitHubAPIClient
from pr_guardian.models import TestRunResult, TestStatus


class CheckRunReporter:
    """把测试运行结果发布为 Check Run，让 CI 能直接看到测试通过/失败和失败定位。"""

    CHECK_RUN_NAME = "PR Guardian Tests"

    def __init__(self, client: GitHubAPIClient) -> None:
        self.client = client

    def publish(
        self,
        head_sha: str,
        result: TestRunResult,
        *,
        repo_path: Path | None = None,
    ) -> dict[str, Any]:
        conclusion = "failure" if result.failed > 0 or result.exit_code != 0 else "success"
        annotations = self._build_annotations(result)
        summary_lines = [
            f"Stack: `{result.stack}`",
            f"Command: `{' '.join(result.command)}`",
            f"Duration: {result.duration_ms / 1000:.2f}s",
            f"Exit code: {result.exit_code}",
            "",
            f"Total: {result.count}",
            f"Passed: {result.passed}",
            f"Failed: {result.failed}",
            f"Skipped: {result.skipped}",
            "",
            "## Failed Tests",
        ]
        for case in result.cases:
            if case.status in {TestStatus.FAILED, TestStatus.ERROR}:
                summary_lines.append(f"- ❌ `{case.suite}/{case.name}` — {case.failure_message or 'no message'}")

        output: dict[str, Any] = {
            "title": "PR Guardian Tests",
            "summary": "\n".join(summary_lines),
        }
        if annotations:
            output["annotations"] = annotations

        response = self.client.create_check_run(
            name=self.CHECK_RUN_NAME,
            head_sha=head_sha,
            status="completed",
            conclusion=conclusion,
            output=output,
            annotations=annotations,
        )
        return {
            "check_run_id": response.get("id"),
            "conclusion": conclusion,
            "annotation_count": len(annotations),
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
        }

    def _build_annotations(self, result: TestRunResult) -> list[dict[str, Any]]:
        annotations: list[dict[str, Any]] = []
        for case in result.cases:
            if case.status not in {TestStatus.FAILED, TestStatus.ERROR}:
                continue
            if not case.file or case.line is None:
                continue
            line = max(case.line, 1)
            if line < 1:
                continue
            annotations.append(
                {
                    "path": case.file,
                    "start_line": line,
                    "end_line": line,
                    "annotation_level": "failure",
                    "title": f"Test failed: {case.name}",
                    "message": case.failure_message or "Test failed",
                }
            )
            if len(annotations) >= 50:
                break
        return annotations


def result_to_json(result: TestRunResult) -> str:
    """序列化运行结果，供 --dry-run 输出与调试。"""
    return json.dumps(
        {
            "stack": result.stack,
            "command": result.command,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "count": result.count,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "cases": [case.model_dump() for case in result.cases],
        },
        ensure_ascii=False,
        indent=2,
    )


__all__ = ["CheckRunReporter", "result_to_json"]
