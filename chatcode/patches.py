"""严格解析 unified diff：按 hunk 计数读取，保留文件末尾换行语义。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .files import FileError, split_lines, normalize_newlines


_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_count: int
    lines: list[str] = field(default_factory=list)
    no_newline: set[str] = field(default_factory=set)


@dataclass
class FilePatch:
    old_path: str | None
    path: str | None
    hunks: list[Hunk] = field(default_factory=list)


def _path(header: str) -> str | None:
    value = header[4:].split("\t", 1)[0].strip()
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise FileError("BAD_PATCH", "无法解析补丁文件名") from exc
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise FileError("BAD_PATCH", "补丁文件名不能为空")
    return value


def parse_patch(text: str) -> list[FilePatch]:
    lines = normalize_newlines(text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    result: list[FilePatch] = []
    current: FilePatch | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise FileError("BAD_PATCH", "--- 后缺少 +++ 文件头")
            current = FilePatch(_path(line), _path(lines[index + 1]))
            if current.old_path is None and current.path is None:
                raise FileError("BAD_PATCH", "补丁缺少实际文件路径")
            if current.old_path and current.path and current.old_path != current.path:
                raise FileError("BAD_PATCH", "unified diff 的重命名请拆成删除和新建")
            result.append(current)
            index += 2
            continue
        if line.startswith("@@"):
            match = _HEADER.match(line)
            if current is None or match is None:
                raise FileError("BAD_PATCH", "补丁缺少文件头或 hunk 头无效")
            hunk = Hunk(int(match[1]), int(match[2] or 1), int(match[4] or 1))
            index += 1
            old_count = new_count = 0
            previous = ""
            while index < len(lines):
                body = lines[index]
                if body == "\\ No newline at end of file":
                    if not previous:
                        raise FileError("BAD_PATCH", "末尾换行标记缺少对应内容")
                    hunk.no_newline.update(("old", "new") if previous == " " else ("old",) if previous == "-" else ("new",))
                    index += 1
                    continue
                if old_count == hunk.old_count and new_count == hunk.new_count:
                    break
                if not body or body[0] not in " +-":
                    raise FileError("BAD_PATCH", "hunk 内容不足或缺少行前缀")
                tag = body[0]
                old_count += tag in " -"
                new_count += tag in " +"
                if old_count > hunk.old_count or new_count > hunk.new_count:
                    raise FileError("BAD_PATCH", "hunk 行数与头部不一致")
                hunk.lines.append(body)
                previous = tag
                index += 1
            if old_count != hunk.old_count or new_count != hunk.new_count:
                raise FileError("BAD_PATCH", "hunk 行数与头部不一致")
            current.hunks.append(hunk)
            continue
        if line and not line.startswith(("diff ", "index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")):
            raise FileError("BAD_PATCH", f"无法识别补丁行：{line[:100]}")
        index += 1
    if not result or any(not item.hunks for item in result):
        raise FileError("BAD_PATCH", "补丁需要文件头和至少一个完整 hunk")
    return result


def apply_file_patch(text: str, patch: FilePatch) -> str:
    original = split_lines(text)
    output: list[str] = []
    cursor = 0
    final_newline = text.endswith("\n")
    for hunk in patch.hunks:
        expected = [line[1:] for line in hunk.lines if line[0] in " -"]
        position = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if position < cursor or position > len(original):
            raise FileError("HUNK_FAILED", "hunk 位置越界或互相重叠")
        if original[position:position + len(expected)] != expected:
            candidates = [i for i in range(cursor, len(original) - len(expected) + 1)
                          if original[i:i + len(expected)] == expected]
            if len(candidates) != 1:
                raise FileError("HUNK_FAILED", "补丁原文不匹配或有多个候选，请重新读取后定位", {"candidate_lines": [i + 1 for i in candidates[:10]]})
            position = candidates[0]
        output.extend(original[cursor:position])
        output.extend(line[1:] for line in hunk.lines if line[0] in " +")
        cursor = position + len(expected)
        if cursor == len(original):
            final_newline = "new" not in hunk.no_newline
    output.extend(original[cursor:])
    return "\n".join(output) + ("\n" if output and final_newline else "")
