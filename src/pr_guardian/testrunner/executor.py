"""命令执行封装：统一 subprocess 调用、超时、输出截断。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 200 * 1024  # 200KB，防止异常输出撑爆内存


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行命令并返回结构化结果；超时返回 exit_code=124（与常见 shell timeout 语义一致）。"""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code = int(completed.returncode)
        stdout = (completed.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (completed.stderr or "")[:MAX_OUTPUT_BYTES]
    except subprocess.TimeoutExpired:
        exit_code = 124
        stdout = ""
        stderr = f"命令超时（{timeout_seconds}s）"
    duration_ms = int((time.monotonic() - start) * 1000)

    combined = stdout
    if stderr:
        combined = f"{stdout}\n{stderr}"[:MAX_OUTPUT_BYTES]

    return {
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout": combined,
    }


__all__ = ["run_command", "MAX_OUTPUT_BYTES"]
