from __future__ import annotations

from dataclasses import dataclass
import re


HUNK_HEADER_PATTERN = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)
DIFF_HEADER_PATTERN = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class PatchLine:
    patch_line: int
    line_type: str
    content: str
    old_line: int | None
    new_line: int | None


@dataclass(frozen=True)
class Hunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[PatchLine]
    line_mappings: list[tuple[int | None, int | None]]


@dataclass(frozen=True)
class FileDiff:
    old_path: str
    new_path: str
    status: str
    patch: str
    hunks: list[Hunk]


@dataclass(frozen=True)
class Diff:
    files: list[FileDiff]


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    match = HUNK_HEADER_PATTERN.match(line)
    if match is None:
        raise ValueError(f"无法解析 hunk 头: {line}")

    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    return (old_start, old_count, new_start, new_count)


def parse_patch(patch: str) -> list[Hunk]:
    if not patch.strip():
        return []

    patch_lines = patch.splitlines()
    hunks: list[Hunk] = []
    current_hunk_lines: list[PatchLine] = []
    current_mappings: list[tuple[int | None, int | None]] = []

    old_line = 0
    new_line = 0
    header = ""
    old_start = 0
    old_count = 0
    new_start = 0
    new_count = 0
    in_hunk = False
    last_old_line: int | None = None
    last_new_line: int | None = None

    def flush_current_hunk() -> None:
        if not in_hunk:
            return
        hunks.append(
            Hunk(
                header=header,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=current_hunk_lines.copy(),
                line_mappings=current_mappings.copy(),
            )
        )

    for patch_line_index, line in enumerate(patch_lines, start=1):
        if line.startswith("@@"):
            # 遇到新 hunk 先落盘上一个，避免跨 hunk 的行号状态串在一起。
            flush_current_hunk()
            old_start, old_count, new_start, new_count = _parse_hunk_header(line)
            old_line = old_start
            new_line = new_start
            header = line
            in_hunk = True
            current_hunk_lines = []
            current_mappings = []
            last_old_line = None
            last_new_line = None
            continue

        if not in_hunk:
            continue

        if line.startswith(" "):
            mapped_old_line = old_line
            mapped_new_line = new_line
            current_hunk_lines.append(
                PatchLine(patch_line_index, "context", line[1:], mapped_old_line, mapped_new_line)
            )
            current_mappings.append((mapped_old_line, mapped_new_line))
            old_line += 1
            new_line += 1
            last_old_line = mapped_old_line
            last_new_line = mapped_new_line
            continue

        if line.startswith("+"):
            mapped_new_line = new_line
            current_hunk_lines.append(PatchLine(patch_line_index, "add", line[1:], None, mapped_new_line))
            current_mappings.append((None, mapped_new_line))
            new_line += 1
            last_old_line = None
            last_new_line = mapped_new_line
            continue

        if line.startswith("-"):
            mapped_old_line = old_line
            current_hunk_lines.append(PatchLine(patch_line_index, "remove", line[1:], mapped_old_line, None))
            current_mappings.append((mapped_old_line, None))
            old_line += 1
            last_old_line = mapped_old_line
            last_new_line = None
            continue

        if line.startswith("\\"):
            # 保留 no-newline 元信息，才能让定位仍然指向刚刚那一行真实变更。
            current_hunk_lines.append(
                PatchLine(patch_line_index, "no_newline", line, last_old_line, last_new_line)
            )
            current_mappings.append((last_old_line, last_new_line))

    flush_current_hunk()
    return hunks


def parse_diff(diff_text: str) -> Diff:
    if not diff_text.strip():
        return Diff(files=[])

    matches = list(DIFF_HEADER_PATTERN.finditer(diff_text))
    if not matches:
        return Diff(files=[])

    files: list[FileDiff] = []
    for index, match in enumerate(matches):
        start_index = match.start()
        end_index = matches[index + 1].start() if index + 1 < len(matches) else len(diff_text)
        file_block = diff_text[start_index:end_index]

        old_path = match.group(1)
        new_path = match.group(2)

        status = "modified"
        if "new file mode" in file_block:
            status = "added"
        elif "deleted file mode" in file_block:
            status = "removed"
        elif "rename from " in file_block or "rename to " in file_block:
            status = "renamed"

        patch_lines: list[str] = []
        hunk_started = False
        for line in file_block.splitlines():
            if line.startswith("@@"):
                hunk_started = True
            if hunk_started:
                # 只保留 hunk 区段，是为了让后续规则忽略 Git 元数据噪音。
                patch_lines.append(line)
        patch = "\n".join(patch_lines)
        hunks = parse_patch(patch)

        files.append(
            FileDiff(
                old_path=old_path,
                new_path=new_path,
                status=status,
                patch=patch,
                hunks=hunks,
            )
        )

    return Diff(files=files)


def get_new_line_number(patch: str, patch_line: int) -> int | None:
    for hunk in parse_patch(patch):
        for line in hunk.lines:
            if line.patch_line == patch_line:
                return line.new_line
    return None


def get_old_line_number(patch: str, patch_line: int) -> int | None:
    for hunk in parse_patch(patch):
        for line in hunk.lines:
            if line.patch_line == patch_line:
                return line.old_line
    return None
