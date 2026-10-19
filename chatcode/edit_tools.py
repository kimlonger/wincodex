"""批量精确编辑、改动历史与按版本撤销。"""

from __future__ import annotations

import ast
import json
import stat

from .code_index import select_symbol
from .editing import PreparedEdit
from .files import FileError, bytes_revision, encode_text, normalize_newlines, read_text_file
from .tool_types import ToolError, ToolResult, spec


def _result(data):
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def _attach_diff(engine, result):
    if result.get("change_id"):
        try:
            details = engine.details(result["change_id"], max_chars=20_000)
            result["diff"], result["diff_truncated"] = details["diff"], details["truncated"]
        except (FileError, OSError) as exc:
            result["diff_error"] = {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}
    return result


def _edited_text(content, edits: list, path, ctx) -> str:
    text = content.text
    starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    intervals = []
    for edit in edits:
        replacement = normalize_newlines(edit.get("new_text", ""))
        if "old_text" in edit:
            if any(key in edit for key in ("start_line", "end_line", "symbol", "symbol_id")):
                raise ToolError("INVALID_ARGUMENT", "每项 edit 只能使用一种定位方式")
            old = normalize_newlines(edit["old_text"])
            if not old:
                raise ToolError("INVALID_ARGUMENT", "old_text 不能为空")
            expected = edit.get("expected_matches", 1)
            if text.count(old) != expected:
                raise ToolError("AMBIGUOUS_MATCH", "原文匹配数量与 expected_matches 不符", {"actual": text.count(old), "expected": expected})
            cursor = 0
            for _ in range(expected):
                start = text.index(old, cursor)
                intervals.append((start, start + len(old), replacement))
                cursor = start + len(old)
        else:
            if "symbol" in edit or "symbol_id" in edit:
                if "start_line" in edit or "end_line" in edit:
                    raise ToolError("INVALID_ARGUMENT", "符号与行范围定位不能混用")
                use_lsp = path.suffix not in (".py", ".pyi") or len(edit.get("symbol_id", "").split(":")) == 5
                if use_lsp:
                    from .language_tools import select_lsp_symbol
                    from .lsp import offset
                    snapshot, symbol = select_lsp_symbol(ctx, path, symbol=edit.get("symbol", ""), symbol_id=edit.get("symbol_id", ""))
                else:
                    snapshot, symbol = select_symbol(path, symbol=edit.get("symbol", ""), symbol_id=edit.get("symbol_id", ""))
                if snapshot.revision != content.revision:
                    raise ToolError("REVISION_MISMATCH", "符号文件在编辑准备期间变化")
                if use_lsp:
                    intervals.append((offset(text, symbol["range"]["start"]), offset(text, symbol["range"]["end"], allow_eof=True), replacement))
                    continue
                first, last = symbol["start_line"], symbol["end_line"]
            elif "start_line" in edit and "end_line" in edit:
                first, last = edit["start_line"], edit["end_line"]
            else:
                raise ToolError("INVALID_ARGUMENT", "edit 需要 old_text、符号或完整行范围")
            if not 1 <= first <= content.line_count + 1 or not first - 1 <= last <= content.line_count:
                raise ToolError("INVALID_RANGE", "行范围越界；插入时 end_line=start_line-1")
            start = starts[first - 1] if first - 1 < len(starts) else len(text)
            end = starts[last] if last < len(starts) else len(text)
            if replacement and end > start and text[start:end].endswith("\n") and not replacement.endswith("\n"):
                replacement += "\n"
            if start == end and replacement:
                if start < len(text) and not replacement.endswith("\n"):
                    replacement += "\n"
                if start == len(text) and text and not text.endswith("\n"):
                    replacement = "\n" + replacement
            intervals.append((start, end, replacement))
    intervals.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1] or current[0] == previous[0]:
            raise ToolError("OVERLAPPING_EDITS", "同一文件的 edits 存在重叠或同位置插入")
    for start, end, replacement in reversed(intervals):
        text = text[:start] + replacement + text[end:]
    return text


def edit_files(ctx, args):
    engine = ctx.workspace.editor
    fingerprint = bytes_revision(json.dumps({k: v for k, v in args.items() if k != "dry_run"}, sort_keys=True, ensure_ascii=True).encode())
    operation_id = args.get("operation_id")
    with ctx.workspace.write_lock:
        if operation_id and not args.get("dry_run", False):
            replay = engine.replay(operation_id, fingerprint)
            if replay is not None:
                return _result(_attach_diff(engine, replay))
        prepared = []
        for item in args["files"]:
            resolved = ctx.workspace.resolve_for_write(item["path"])
            existing = read_text_file(resolved.absolute) if resolved.absolute.exists() else None
            action = item.get("action", "update")
            if action == "create":
                if existing is not None:
                    raise ToolError("ALREADY_EXISTS", f"新建目标已存在：{resolved.relative}")
                if "content" not in item or item.get("edits") or item.get("destination"):
                    raise ToolError("INVALID_ARGUMENT", "create 需要 content，不能带 edits/destination")
            else:
                if existing is None:
                    raise ToolError("NOT_FOUND", f"目标文件不存在：{resolved.relative}")
                if item.get("expected_revision") != existing.revision:
                    raise ToolError("REVISION_MISMATCH", f"修改已有文件需要最新 expected_revision：{resolved.relative}", {"actual": existing.revision})
            if action in ("delete", "move"):
                if "content" in item or "edits" in item:
                    raise ToolError("INVALID_ARGUMENT", "delete/move 不能同时编辑内容")
                prepared.append(PreparedEdit(resolved.relative, existing.raw, None))
                if action == "move":
                    if not item.get("destination"):
                        raise ToolError("INVALID_ARGUMENT", "move 需要 destination")
                    destination = ctx.workspace.resolve_for_write(item["destination"])
                    if destination.absolute.exists():
                        raise ToolError("ALREADY_EXISTS", "移动目标已存在")
                    prepared.append(PreparedEdit(destination.relative, None, existing.raw, mode=stat.S_IMODE(resolved.absolute.stat().st_mode)))
                continue
            if "content" in item and "edits" in item:
                raise ToolError("INVALID_ARGUMENT", "content 与 edits 二选一")
            if "content" in item:
                text = normalize_newlines(item["content"])
            elif existing is not None and item.get("edits"):
                text = _edited_text(existing, item["edits"], resolved.absolute, ctx)
            else:
                raise ToolError("INVALID_ARGUMENT", "需要 content 或非空 edits")
            if args.get("validate_syntax", True):
                try:
                    if resolved.absolute.suffix in (".py", ".pyi"):
                        ast.parse(text, filename=resolved.relative)
                    elif resolved.absolute.suffix == ".json":
                        json.loads(text)
                except (SyntaxError, ValueError) as exc:
                    raise ToolError("SYNTAX_ERROR", f"预检失败：{exc}", {"path": resolved.relative, "line": getattr(exc, "lineno", None)}) from None
            data = existing.raw if existing and text == existing.text else encode_text(text,
                    newline=existing.newline if existing else "\n", has_bom=existing.has_bom if existing else False,
                    encoding=existing.encoding if existing else "utf-8")
            prepared.append(PreparedEdit(resolved.relative, existing.raw if existing else None, data))
        if sum(len(edit.after or b"") for edit in prepared) > 20 * 1024 * 1024:
            raise ToolError("TOO_LARGE", "单次提交内容超过 20 MiB，请拆分操作")
        result = engine.commit(prepared, dry_run=args.get("dry_run", False), operation_id=operation_id, fingerprint=fingerprint)
        return _result(_attach_diff(engine, result) if not result.get("dry_run") else result)


def get_changes(ctx, args):
    if args.get("change_id"):
        return _result(ctx.workspace.editor.details(args["change_id"], max_chars=args.get("max_chars", 40_000)))
    changes = ctx.workspace.editor.history(limit=args.get("limit", 20))
    return _result({"changes": changes, "count": len(changes)})


def undo_changes(ctx, args):
    return _result(ctx.workspace.editor.undo(args["change_id"], dry_run=args.get("dry_run", False)))


_edit = {"type": "object", "properties": {
    "old_text": {"type": "string"}, "new_text": {"type": "string"},
    "expected_matches": {"type": "integer", "minimum": 1, "maximum": 1000},
    "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 0},
    "symbol": {"type": "string"}, "symbol_id": {"type": "string"}}, "required": ["new_text"], "additionalProperties": False}
_file = {"type": "object", "properties": {"path": {"type": "string"}, "expected_revision": {"type": "string"},
    "action": {"type": "string", "enum": ["create", "update", "delete", "move"]}, "destination": {"type": "string"},
    "content": {"type": "string"}, "edits": {"type": "array", "minItems": 1, "maxItems": 100, "items": _edit}},
    "required": ["path"], "additionalProperties": False}
EXTRA_TOOLS = {
    "edit_files": spec("批量精确编辑文本文件：create/update/delete/move；已有文件必须带 expected_revision。edits 均针对原始快照，支持唯一字符串、完整行范围或 Python 符号。返回 change_id 和 diff；operation_id 可避免网络重试重复提交。", edit_files,
                       {"files": {"type": "array", "minItems": 1, "maxItems": 20, "items": _file},
                        "operation_id": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[A-Za-z0-9._-]+$"},
                        "dry_run": {"type": "boolean"}, "validate_syntax": {"type": "boolean"}}, ["files"], read_only=False),
    "get_changes": spec("查看最近的服务改动记录；指定 change_id 获取该次修改的精确 diff，无需 Git 仓库。", get_changes,
                        {"change_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                         "max_chars": {"type": "integer", "minimum": 100, "maximum": 60_000}}),
    "undo_changes": spec("撤销指定 change_id；提交前复核全部文件版本，发现后续修改则拒绝覆盖；支持 dry_run。", undo_changes,
                         {"change_id": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["change_id"], read_only=False),
}
