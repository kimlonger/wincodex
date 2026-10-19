"""MCP 工具集：让网页对话能查文件、改文件、跑命令、看 git。

每个工具返回两层结果：
  - content：给模型看的精炼文本（有截断，省 token 也省网络往返）
  - structuredContent：同样受预算限制的机器结果，分页信息用于继续读取
"""

from __future__ import annotations

import fnmatch
import codecs
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import replace
import time
from pathlib import Path
from typing import Any, Callable

from . import files as fileio
from .config import (
    APP_TITLE,
    EXEC_DEFAULT_TIMEOUT,
    EXEC_MAX_OUTPUT_BYTES,
    EXEC_MAX_TIMEOUT,
    DESTRUCTIVE_COMMAND_PATTERNS,
    READ_MAX_BYTES,
    READ_MAX_LINES,
    SEARCH_MAX_LINE_CHARS,
    SEARCH_MAX_MATCHES,
    Settings,
    VERSION,
)
from .files import FileError, read_text_file
from .workspace import PathRejected, Workspace
from .editing import PreparedEdit
from .patches import parse_patch, apply_file_patch
from .validation import validate, ValidationError


from .tool_types import ToolError, ToolResult, ToolContext, ACTIVE_TASK


Handler = Callable[[ToolContext, dict[str, Any]], ToolResult]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _require_str(args: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = args.get(key, default)
    if value is None:
        raise ToolError("MISSING_ARGUMENT", f"缺少必需参数：{key}")
    if not isinstance(value, str):
        raise ToolError("INVALID_ARGUMENT", f"参数 {key} 必须是字符串")
    return value


def _optional_int(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError("INVALID_ARGUMENT", f"参数 {key} 必须是整数") from None


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n…（已截断）", True


def _numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines) - 1))
    out = []
    for i, line in enumerate(lines):
        out.append(f"{start + i:>{width}}| {line}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# 1. server_info
# --------------------------------------------------------------------------


def tool_server_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from .sandbox import network_policy
    settings = ctx.settings
    connection = ctx.registry.settings if ctx.registry is not None else settings
    info = {
        "name": "chatcode-mcp",
        "display_name": APP_TITLE,
        "version": VERSION,
        "workspace": str(ctx.workspace.root),
        "original_workspace": str((ctx.task_home or ctx.workspace).root),
        "platform": os.name,
        "auth_mode": connection.auth_mode,
        "exec_enabled": settings.allow_exec and not settings.read_only,
        "read_only": settings.read_only,
        "file_policy": {"sensitive_files_blocked": not ctx.workspace.policy.allow_sensitive,
                        "custom_rules": len(ctx.workspace.policy.patterns), "loaded_at_startup": True,
                        "command_sandbox": settings.execution_mode == "native"},
        "execution_mode": settings.execution_mode,
        "sandbox_policy": {"requires_task_copy": True, **network_policy(), "fallback_to_host": False} if settings.execution_mode == "native" else None,
        "public_url": connection.public_url or connection.discovered_public_url,
        "mcp_url": connection.mcp_url(),
        "uptime_seconds": round(time.time() - ctx.started_at, 1),
        "tools": sum(not settings.read_only or is_read_only_tool(name) for name in TOOLS),
        "processes": ctx.workspace._processes.summaries() if ctx.workspace._processes is not None else [],
        "configured_languages": sorted(settings.language_servers),
        "builtin_code_languages": ["python"],
        "builtin_declaration_candidates": ["java", "go"],
        "desktop": ctx.registry.desktop.status() if ctx.registry is not None and hasattr(ctx.registry, "desktop") else {"configured": False},
        "task_workflow": {"persistent": True, "automatic_command_replay": False,
                          "scope_is_permission_boundary": False, "tools_require_task_id": False,
                          "isolation_modes": ["direct", "copy"], "copy_is_os_sandbox": False},
    }
    text = (
        f"{APP_TITLE} v{info['version']}\n"
        f"工作区：{info['workspace']}\n"
        f"认证模式：{info['auth_mode']}\n"
        f"命令执行：{'开启' if info['exec_enabled'] else '关闭'}\n"
        f"公网地址：{info['mcp_url']}\n"
        f"可用工具：{info['tools']} 个"
    )
    return ToolResult(text, info)


def tool_list_workspaces(ctx, args):
    data = ctx.registry.describe() if ctx.registry is not None else {
        "workspaces": [{"path": str(ctx.workspace.root)}], "context_required": False}
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


# --------------------------------------------------------------------------
# 2. list_dir
# --------------------------------------------------------------------------


def tool_list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = args.get("path") or "."
    recursive = bool(args.get("recursive", False))
    max_entries = _optional_int(args, "max_entries", 500)

    try:
        resolved = ctx.workspace.resolve_existing(str(rel))
    except PathRejected as exc:
        raise ToolError(exc.code, exc.message) from None

    if not resolved.absolute.is_dir():
        raise ToolError("NOT_A_DIRECTORY", f"不是目录：{rel}")

    dirs: list[str] = []
    file_items: list[dict[str, Any]] = []
    for entry_rel, is_dir in ctx.workspace.iter_entries(
        resolved.relative, recursive=recursive, max_entries=max_entries
    ):
        if is_dir:
            dirs.append(entry_rel)
        else:
            try:
                size = (ctx.workspace.root / entry_rel).stat().st_size
            except OSError:
                size = 0
            file_items.append({"path": entry_rel, "size": size})

    structured = {
        "path": resolved.relative,
        "recursive": recursive,
        "directories": dirs,
        "files": file_items,
        "count": len(dirs) + len(file_items),
    }
    lines = [f"📁 {resolved.relative}{'（递归）' if recursive else ''}"]
    for d in dirs[:200]:
        lines.append(f"  [目录] {d}/")
    for f in file_items[:300]:
        lines.append(f"  {f['path']}  ({f['size']} B)")
    if structured["count"] > max_entries:
        lines.append(f"… 超过 {max_entries} 条，已截断")
    text = "\n".join(lines)
    text, _ = _truncate(text, 30_000)
    return ToolResult(text, structured)


# --------------------------------------------------------------------------
# 3. read_file
# --------------------------------------------------------------------------


def tool_read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    resolved = ctx.workspace.resolve_existing(_require_str(args, "path"))
    data = fileio.read_window(resolved.absolute, offset=args.get("offset", 1),
                              limit=min(args.get("limit", READ_MAX_LINES), READ_MAX_LINES))
    lines = data.pop("lines")
    data["path"] = resolved.relative
    text = f"文件：{resolved.relative}  行 {data['start_line']}-{data['end_line']}/{data['total_lines']}  版本 {data['revision']}\n"
    text += _numbered(lines, data["start_line"])
    if data["truncated"]:
        text += f"\n[部分内容，next_offset={data['next_offset']}，截断行={data['truncated_lines']}]"
    return ToolResult(text, data)


def tool_read_files(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    requests = args.get("requests")
    if requests is not None and args.get("paths") is not None:
        raise ToolError("INVALID_ARGUMENT", "paths 和 requests 请只提供一种")
    if requests is None:
        requests = [{"path": path} for path in args.get("paths", [])]
    if not requests or len(requests) > 20:
        raise ToolError("INVALID_ARGUMENT", "一次提供 1 到 20 个文件读取请求")
    results, chunks, budget = [], [], 100_000
    for request in requests:
        try:
            if budget < 1:
                raise ToolError("OUTPUT_BUDGET", "总输出预算耗尽，请单独读取余下文件")
            resolved = ctx.workspace.resolve_existing(request["path"])
            data = fileio.read_window(resolved.absolute, offset=request.get("offset", 1),
                                      limit=min(request.get("limit", READ_MAX_LINES), READ_MAX_LINES), max_chars=min(40_000, budget))
            data.pop("lines")
            budget -= len(data["content"])
            data.update(path=resolved.relative, ok=True)
            results.append(data)
            chunks.append(f"===== {resolved.relative} 版本 {data['revision']} next_offset={data['next_offset']} =====\n" + data["content"])
        except (ToolError, FileError, PathRejected) as exc:
            results.append({"path": request["path"], "ok": False, "code": exc.code, "error": exc.message})
            chunks.append(f"===== {request['path']} =====\n[{exc.code}] {exc.message}")
    return ToolResult("\n\n".join(chunks), {"files": results, "count": len(results)})


# --------------------------------------------------------------------------
# 5. search_text
# --------------------------------------------------------------------------


def tool_search_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if args.get("regex", False):
        from .searching import regex_search
        return regex_search(ctx, args)
    return _search_text(ctx, args)


def _search_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = _require_str(args, "pattern")
    path = args.get("path") or "."
    is_regex = bool(args.get("regex", False))
    max_matches = _optional_int(args, "max_matches", SEARCH_MAX_MATCHES)
    first = args.get("offset", 0)
    include = args.get("include")  # 如 "*.py"

    try:
        resolved = ctx.workspace.resolve_existing(str(path))
    except PathRejected as exc:
        raise ToolError(exc.code, exc.message) from None

    if is_regex:
        try:
            matcher = re.compile(pattern)
        except re.error as exc:
            raise ToolError("BAD_REGEX", f"正则表达式非法：{exc}") from None
    else:
        matcher = re.compile(re.escape(pattern))

    targets: list[Path]
    scan_limit_reached = False
    if resolved.absolute.is_file():
        targets = [resolved.absolute]
    else:
        targets = []
        for entry_index, (entry_rel, is_dir) in enumerate(ctx.workspace.iter_entries(resolved.relative, recursive=True, max_entries=5001)):
            if entry_index == 5000:
                scan_limit_reached = True
                break
            if is_dir:
                continue
            if include and not fnmatch.fnmatch(Path(entry_rel).name, include):
                continue
            targets.append(ctx.workspace.root / entry_rel)

    matches: list[dict[str, Any]] = []
    scanned, skipped_count, seen, output_bytes = 0, 0, 0, 0
    skipped, more = [], False
    for target in targets:
        if more:
            break
        try:
            target = ctx.workspace.resolve_existing(target.relative_to(ctx.workspace.root).as_posix()).absolute
            content = read_text_file(target, max_bytes=READ_MAX_BYTES)
        except (FileError, PathRejected) as exc:
            skipped_count += 1
            if len(skipped) < 20:
                skipped.append({"path": target.relative_to(ctx.workspace.root).as_posix(), "code": exc.code})
            continue
        scanned += 1
        for lineno, line in enumerate(content.text.split("\n"), start=1):
            if matcher.search(line):
                seen += 1
                if seen <= first:
                    continue
                shown = line[:SEARCH_MAX_LINE_CHARS]
                item = {
                        "path": target.relative_to(ctx.workspace.root).as_posix(),
                        "line": lineno,
                        "text": shown,
                        "truncated": len(line) > SEARCH_MAX_LINE_CHARS,
                    }
                cost = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if len(matches) >= max_matches or output_bytes + cost > 60_000:
                    more = True
                    break
                matches.append(item)
                output_bytes += cost

    lines = []
    current = None
    for m in matches:
        if m["path"] != current:
            current = m["path"]
            lines.append(f"\n{m['path']}")
        lines.append(f"  {m['line']}: {m['text']}")
    summary = f"匹配 {len(matches)} 处（扫描 {scanned} 个文件）"
    text = summary + "\n" + "\n".join(lines)
    text, _ = _truncate(text, 40_000)
    return ToolResult(text, {"pattern": pattern, "matches": matches, "scanned_files": scanned, "count": len(matches),
                            "next_offset": first + len(matches) if more else None, "truncated": more or scan_limit_reached,
                            "scan_limit_reached": scan_limit_reached, "skipped_files": skipped, "skipped_count": skipped_count})


# --------------------------------------------------------------------------
# 6. write_file —— 整文件写入
# --------------------------------------------------------------------------


def _check_revision(existing, expected_revision, *, required=False):
    if existing is not None and required and not expected_revision:
        raise ToolError("REVISION_REQUIRED", "修改已有文件需要 expected_revision，请先 read_file 读取最新版本")
    if expected_revision and (existing is None or existing.revision != expected_revision):
        raise ToolError("REVISION_MISMATCH", "文件版本已变化，请重新读取后修改", {
            "expected": expected_revision, "actual": existing.revision if existing else None})


def _commit(ctx, edits, *, dry_run=False):
    try:
        return ctx.workspace.editor.commit(edits, dry_run=dry_run)
    except (FileError, PathRejected) as exc:
        raise ToolError(exc.code, exc.message, getattr(exc, "details", {})) from None
    except OSError as exc:
        raise ToolError("WRITE_FAILED", f"写入失败：{exc}") from None


def tool_write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = _require_str(args, "path")
    text = _require_str(args, "content")
    try:
        resolved = ctx.workspace.resolve_for_write(rel)
        existing = read_text_file(resolved.absolute) if resolved.absolute.exists() else None
        _check_revision(existing, args.get("expected_revision"), required=True)
        data = fileio.encode_text(text, newline=existing.newline if existing else "\n",
                                 has_bom=existing.has_bom if existing else False,
                                 encoding=existing.encoding if existing else "utf-8")
        if existing and fileio.normalize_newlines(text) == existing.text:
            data = existing.raw
    except (FileError, PathRejected) as exc:
        raise ToolError(exc.code, exc.message, getattr(exc, "details", {})) from None
    result = _commit(ctx, [PreparedEdit(resolved.relative, existing.raw if existing else None, data)])
    result.update(path=resolved.relative, revision=fileio.bytes_revision(data), bytes_written=len(data),
                  bytes_before=existing.size_bytes if existing else 0, created=existing is None)
    return ToolResult(f"已{'创建' if existing is None else '写入'}：{resolved.relative}（版本 {result['revision']}）", result)


def tool_replace_in_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rel = _require_str(args, "path")
    old = fileio.normalize_newlines(_require_str(args, "old_text"))
    new = fileio.normalize_newlines(_require_str(args, "new_text"))
    if not old:
        raise ToolError("INVALID_ARGUMENT", "old_text 必须是非空字符串")
    try:
        resolved = ctx.workspace.resolve_for_write(rel)
        content = read_text_file(resolved.absolute)
        _check_revision(content, args.get("expected_revision"), required=True)
        occurrences = content.text.count(old)
        if not occurrences:
            raise ToolError("NOT_FOUND", "未找到原文，请重新读取并保留准确的缩进和上下文")
        if occurrences > 1 and not args.get("replace_all", False):
            raise ToolError("AMBIGUOUS_MATCH", f"找到 {occurrences} 处匹配，请提供唯一上下文或 replace_all=true")
        count = occurrences if args.get("replace_all", False) else 1
        updated = content.text.replace(old, new, count)
        data = content.raw if updated == content.text else fileio.encode_text(
            updated, newline=content.newline, has_bom=content.has_bom, encoding=content.encoding)
    except (FileError, PathRejected) as exc:
        raise ToolError(exc.code, exc.message, getattr(exc, "details", {})) from None
    result = _commit(ctx, [PreparedEdit(resolved.relative, content.raw, data)])
    result.update(path=resolved.relative, replacements=count, revision=fileio.bytes_revision(data), previous_revision=content.revision)
    return ToolResult(f"已替换 {resolved.relative} 中 {count} 处（版本 {result['revision']}）", result)


def tool_apply_patch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        patches = parse_patch(_require_str(args, "patch"))
        expected = args.get("expected_revisions") or {}
        edits = []
        for item in patches:
            rel = item.path or item.old_path
            resolved = ctx.workspace.resolve_for_write(rel)
            existing = read_text_file(resolved.absolute) if resolved.absolute.exists() else None
            if item.old_path is None and existing is not None:
                raise ToolError("ALREADY_EXISTS", f"新建目标已存在：{rel}")
            if item.old_path is not None and existing is None:
                raise ToolError("NOT_FOUND", f"补丁目标不存在：{rel}")
            expected_revision = expected.get(resolved.relative, expected.get(rel))
            _check_revision(existing, expected_revision, required=existing is not None and any(h.old_count == 0 for h in item.hunks))
            updated = apply_file_patch(existing.text if existing else "", item)
            if item.path is None and updated:
                raise ToolError("BAD_PATCH", "删除文件的补丁必须删除全部内容")
            data = None if item.path is None else fileio.encode_text(
                updated, newline=existing.newline if existing else "\n",
                has_bom=existing.has_bom if existing else False, encoding=existing.encoding if existing else "utf-8")
            if existing is not None and item.path is not None and updated == existing.text:
                data = existing.raw
            edits.append(PreparedEdit(resolved.relative, existing.raw if existing else None, data))
    except (FileError, PathRejected) as exc:
        raise ToolError("PATCH_FAILED", exc.message, {"cause": exc.code, **getattr(exc, "details", {})}) from None
    result = _commit(ctx, edits, dry_run=bool(args.get("dry_run", False)))
    label = "补丁试运行通过" if result.get("dry_run") else "补丁已提交"
    return ToolResult(label + f"，涉及 {len(edits)} 个文件", result)


# --------------------------------------------------------------------------
# 9. exec_command
# --------------------------------------------------------------------------


def _looks_destructive(command: str) -> bool:
    low = command.lower()
    return any(re.search(p, low) for p in DESTRUCTIVE_COMMAND_PATTERNS)


def tool_exec_command(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from .process_tools import exec_command
    return exec_command(ctx, args)


# --------------------------------------------------------------------------
# 10-12. git 工具
# --------------------------------------------------------------------------


def _run_git(ctx: ToolContext, git_args: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *git_args],
            cwd=str(ctx.workspace.root),
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise ToolError("GIT_MISSING", "系统里找不到 git，请先安装") from None
    except subprocess.TimeoutExpired:
        raise ToolError("GIT_TIMEOUT", "git 命令执行超时") from None
    decode = lambda b: b.decode("utf-8", errors="replace")
    return completed.returncode, decode(completed.stdout), decode(completed.stderr)


def tool_git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    code, out, err = _run_git(ctx, ["status", "--porcelain=v1", "-z", "--", "."])
    if code != 0:
        raise ToolError("GIT_FAILED", f"git status 失败：{err.strip() or out.strip()}")
    entries = []
    records = iter(out.split("\0"))
    for line in records:
        if not line:
            continue
        item = {"status": line[:2].strip(), "path": line[3:]}
        if "R" in line[:2] or "C" in line[:2]:
            item["original_path"] = next(records, "")
        entries.append(item)
    # porcelain 路径相对仓库根目录；转换为实际工作区相对路径。
    root_code, prefix, _ = _run_git(ctx, ["rev-parse", "--show-prefix"])
    if root_code:
        raise ToolError("GIT_FAILED", "无法定位 Git 工作目录")
    prefix = prefix.rstrip("\r\n")
    if prefix:
        entries = [item for item in entries if item["path"].startswith(prefix)]
        for item in entries:
            item["path"] = item["path"][len(prefix):]
            if "original_path" in item:
                original = item["original_path"]
                item["original_path"] = original[len(prefix):] if original.startswith(prefix) else None
    text = f"工作区：{ctx.workspace.root}\n" + ("\n".join(f"{item['status']} {item['path']}" for item in entries) or "（干净，无改动）")
    return ToolResult(text, {"entries": entries, "clean": not entries, "count": len(entries)})


@contextmanager
def _git_output(ctx, arguments):
    # 输出落临时文件，避免大补丁先完整进入内存才截断。
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            completed = subprocess.run(["git", "--no-pager", "-c", "color.ui=false", *arguments],
                cwd=str(ctx.workspace.root), stdout=output, stderr=errors, timeout=30)
        except FileNotFoundError:
            raise ToolError("GIT_MISSING", "系统里找不到 git，请先安装") from None
        except subprocess.TimeoutExpired:
            raise ToolError("GIT_TIMEOUT", "git 命令执行超时") from None
        if completed.returncode:
            errors.seek(0)
            raise ToolError("GIT_FAILED", errors.read(4000).decode("utf-8", errors="replace"))
        output.seek(0)
        yield output


def tool_git_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    base = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative"]
    if args.get("staged", False):
        base.append("--cached")
    paths = []
    if args.get("path"):
        paths = [":(literal)" + ctx.workspace.resolve_path(args["path"]).relative]
    # 先选择允许的路径，防止 git_diff 成为读取敏感文件的另一入口。
    with _git_output(ctx, [*base, "--name-only", "-z", "--", *paths]) as names:
        raw = names.read(512_001)
    scan_limited = len(raw) > 512_000
    if scan_limited:
        raw = raw[:512_000].rsplit(b"\0", 1)[0]
    selected, skipped, files_limited, command_size = [], 0, False, 0
    for name in raw.decode("utf-8", errors="replace").split("\0"):
        if not name:
            continue
        try:
            relative = ctx.workspace.resolve_path(name).relative
        except PathRejected:
            skipped += 1
            continue
        command_size += len(relative.encode("utf-16-le")) // 2 + 16
        if len(selected) >= 200 or command_size > 20_000:
            files_limited = True
            break
        selected.append(":(literal)" + relative)
    offset, budget = args.get("offset", 0), args.get("max_bytes", 32_000)
    if offset and not args.get("diff_revision"):
        raise ToolError("MISSING_REVISION", "续读 git_diff 时请携带上一页的 diff_revision")
    # 不传空的路径列表给 git，否则会返回包括被拒绝文件在内的全部补丁。
    with tempfile.TemporaryFile() as empty:
        source = _git_output(ctx, [*base, "--", *selected]) if selected else None
        with (source if source is not None else nullcontext(empty)) as output:
            digest, total = hashlib.sha256(), 0
            while chunk := output.read(64_000):
                digest.update(chunk)
                total += len(chunk)
            revision = "sha256:" + digest.hexdigest()
            if args.get("diff_revision") and args["diff_revision"] != revision:
                raise ToolError("REVISION_MISMATCH", "diff 已变化，请从 offset=0 重新读取")
            if offset > total:
                raise ToolError("INVALID_CURSOR", "diff 游标超过总字节数")
            output.seek(offset)
            raw = output.read(budget)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            shown = decoder.decode(raw, final=offset + len(raw) == total)
            consumed = len(raw) - len(decoder.getstate()[0])
    next_offset = offset + consumed if offset + consumed < total else None
    data = {"diff": shown, "diff_revision": revision, "offset": offset, "next_offset": next_offset,
            "total_bytes": total, "truncated": next_offset is not None or files_limited or scan_limited,
            "empty": total == 0, "skipped_files_count": skipped, "files_truncated": files_limited or scan_limited}
    text = shown or ("（本页无内容）" if total else "（无可访问的改动）")
    if data["truncated"]:
        text += f"\n[部分结果，next_offset={next_offset}；files_truncated={data['files_truncated']}，文件过多时请指定 path]"
    return ToolResult(text, data)


def tool_git_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    limit = min(_optional_int(args, "limit", 10), 50)
    code, out, err = _run_git(ctx, ["log", f"-{limit}", "--oneline", "--decorate"])
    if code != 0:
        raise ToolError("GIT_FAILED", f"git log 失败：{err.strip() or out.strip()}")
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, rest = line.partition(" ")
        commits.append({"sha": sha, "message": rest})
    text = out.rstrip() or "（还没有提交）"
    return ToolResult(text, {"commits": commits, "count": len(commits)})


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS: dict[str, dict[str, Any]] = {
    "list_workspaces": {
        "description": "查看本机已登记项目及其 workspace_id/workspace_token。只可选择已登记项目，不可远程添加目录。",
        "inputSchema": _schema({}, []), "handler": tool_list_workspaces, "read_only": True,
    },
    "server_info": {
        "description": "查看服务器状态：工作区路径、认证模式、公网地址、可用工具数。",
        "inputSchema": _schema({}, []),
        "handler": tool_server_info,
    },
    "list_dir": {
        "description": "列出目录内容。recursive=true 可递归列出整个项目（自动跳过 node_modules/.git 等噪音目录）。",
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "相对路径，默认 '.'"},
                "recursive": {"type": "boolean", "description": "是否递归，默认 false"},
                "max_entries": {"type": "integer", "description": "最多返回多少条，默认 500"},
            },
            [],
        ),
        "handler": tool_list_dir,
    },
    "read_file": {
        "description": "读取文件内容（带行号）。返回的版本号可用于后续修改时做冲突检测。",
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "文件相对路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                "limit": {"type": "integer", "description": "读取多少行，默认全部"},
            },
            ["path"],
        ),
        "handler": tool_read_file,
    },
    "read_files": {
        "description": "一次性读取多个文件（最多 20 个）。比逐个 read_file 快得多，适合先批量了解代码再动手。",
        "inputSchema": _schema(
            {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "文件路径数组，与 requests 二选一"},
                "requests": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object", "properties": {
                    "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, "required": ["path"], "additionalProperties": False}},
            },
            [],
        ),
        "handler": tool_read_files,
    },
    "search_text": {
        "description": "在项目中搜索文本或正则，返回文件路径+行号+内容。",
        "inputSchema": _schema(
            {
                "pattern": {"type": "string", "description": "要搜索的文本或正则"},
                "path": {"type": "string", "description": "在哪个目录下搜索，默认 '.'"},
                "regex": {"type": "boolean", "description": "pattern 是否按正则解析，默认 false"},
                "include": {"type": "string", "description": "文件名过滤，如 '*.py'"},
                "max_matches": {"type": "integer", "description": "最多多少条匹配，默认 200"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 100_000, "description": "跳过的匹配行数，按 next_offset 续读"},
            },
            ["pattern"],
        ),
        "handler": tool_search_text,
    },
    "write_file": {
        "description": "整文件写入（新建或覆盖）。改大段代码或新建文件时用它。",
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "文件相对路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "expected_revision": {"type": "string", "description": "修改已有文件时必填：read_file 返回的 revision；新建文件可省略"},
            },
            ["path", "content"],
        ),
        "handler": tool_write_file,
    },
    "replace_in_file": {
        "description": "精确替换文件中的一段文本。改动小、定位准，是日常改代码的首选工具。",
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "文件相对路径"},
                "old_text": {"type": "string", "description": "要被替换的原文，需与文件完全一致（含缩进）"},
                "new_text": {"type": "string", "description": "替换后的新文本"},
                "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false"},
                "expected_revision": {"type": "string", "description": "必填：read_file 返回的 revision，用于防止覆盖其他改动"},
            },
            ["path", "old_text", "new_text", "expected_revision"],
        ),
        "handler": tool_replace_in_file,
    },
    "apply_patch": {
        "description": "应用 unified diff 补丁，支持修改、新建和删除多个文件；失败时回滚并报告恢复状态。",
        "inputSchema": _schema(
            {
                "patch": {"type": "string", "description": "unified diff 文本，需含 +++ 与 @@ 头"},
                "dry_run": {"type": "boolean", "description": "只试算不落盘，默认 false"},
                "expected_revisions": {"type": "object", "additionalProperties": {"type": "string"}, "description": "可选：文件路径到读取版本的映射"},
            },
            ["patch"],
        ),
        "handler": tool_apply_patch,
    },
    "exec_command": {
        "description": "在工作区内执行命令（跑测试、构建、安装依赖）。有超时和输出上限，破坏性命令会被拒绝。",
        "inputSchema": _schema(
            {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，上限 300"},
                "cwd": {"type": "string", "description": "工作区内的工作目录，默认工作区根"},
            },
            ["command"],
        ),
        "handler": tool_exec_command,
    },
    "git_status": {
        "description": "查看 git 工作区改动状态。",
        "inputSchema": _schema({}, []),
        "handler": tool_git_status,
    },
    "git_diff": {
        "description": "查看未提交的改动内容。",
        "inputSchema": _schema({"path": {"type": "string", "description": "只看某个文件或目录，可含已删除文件"},
            "staged": {"type": "boolean", "description": "查看暂存区，默认 false"},
            "offset": {"type": "integer", "minimum": 0, "maximum": 9_007_199_254_740_991,
                       "description": "字节游标，使用 next_offset 续读"},
            "max_bytes": {"type": "integer", "minimum": 256, "maximum": 60_000},
            "diff_revision": {"type": "string", "description": "续读时必填，用于检测 diff 变化"}}, []),
        "handler": tool_git_diff,
    },
    "git_log": {
        "description": "查看最近提交记录。",
        "inputSchema": _schema({"limit": {"type": "integer", "description": "几条，默认 10"}}, []),
        "handler": tool_git_log,
    },
}


from .project_tools import EXTRA_TOOLS as PROJECT_TOOLS
TOOLS.update(PROJECT_TOOLS)
from .edit_tools import EXTRA_TOOLS as EDIT_TOOLS
TOOLS.update(EDIT_TOOLS)
from .process_tools import EXTRA_TOOLS as PROCESS_TOOLS, EXEC_SPEC
TOOLS.update(PROCESS_TOOLS)
TOOLS["exec_command"] = EXEC_SPEC
from . import language_tools
TOOLS.update(language_tools.EXTRA_TOOLS)
from .task_tools import EXTRA_TOOLS as TASK_TOOLS
from .maintenance import EXTRA_TOOLS as MAINTENANCE_TOOLS
TASK_TOOLS = {**TASK_TOOLS, **MAINTENANCE_TOOLS}
TOOLS.update(TASK_TOOLS)
from .index_tools import EXTRA_TOOLS as INDEX_TOOLS
TOOLS.update(INDEX_TOOLS)
from .language_setup import status as language_status
from .sandbox import status as sandbox_status
from .tool_types import spec
TOOLS["language_status"] = spec("检测本机 Java/Go 工具链、语言服务配置及会话健康；不会自动启用进程。", language_status, {})
TOOLS["sandbox_status"] = spec("检查本机 Windows 原生沙箱是否可用；不下载、不运行项目代码。", sandbox_status, {})
from .desktop_tools import EXTRA_TOOLS as DESKTOP_TOOLS
TOOLS.update(DESKTOP_TOOLS)
for _name in ("code_outline", "read_code", "find_symbol"):
    TOOLS[_name]["handler"] = getattr(language_tools, _name)
TOOLS["find_symbol"]["inputSchema"]["properties"]["language"] = {"type": "string", "description": "可选：使用已配置的语言服务查询工作区符号"}
TOOLS["find_symbol"]["inputSchema"]["properties"].update(offset={"type": "integer", "minimum": 0, "maximum": 100000},
    budget_chars={"type": "integer", "minimum": 1000, "maximum": 60000})
for _name in ("code_outline", "read_code"):
    TOOLS[_name]["inputSchema"]["properties"]["backend"] = {"type": "string", "enum": ["auto", "lsp"]}
TOOLS["code_outline"]["description"] = "读取代码大纲：Python AST；已配置语言服务时用 LSP；Java/Go 未配置时给出明确标注的非语义声明候选。"
TOOLS["read_code"]["description"] = "按 qualified_name 或 symbol_id 读取定义；Java/Go 候选范围可能近似，语义重命名必须使用 LSP；过期版本拒绝。"
TOOLS["find_symbol"]["description"] = "持久增量索引查找 Python/Java/Go 定义候选；支持分页和字符预算。指定 language 时改用已配置 LSP 的工作区符号。"
TOOLS["search_text"]["inputSchema"]["properties"]["timeout_ms"] = {
    "type": "integer", "minimum": 100, "maximum": 10_000, "description": "正则搜索总时限，默认 5000 毫秒"}
TOOLS["search_text"]["inputSchema"]["properties"]["pattern"]["maxLength"] = 4096


# 对资源开销参数设置明确边界，协议层和直接调用共用同一份校验。
for _tool_name, _spec in TOOLS.items():
    _spec["inputSchema"]["properties"].setdefault("task_id", {"type": "string", "pattern": "^[0-9a-f]{32}$",
        "description": "关联本项目任务；非任务工具携带此 ID 时自动记录执行与文件版本"})
    _spec["inputSchema"]["properties"].update(
        workspace_id={"type": "string", "pattern": "^[0-9a-f]{24}$", "description": "server_info/list_workspaces 返回的项目 ID"},
        workspace_token={"type": "string", "minLength": 1, "maxLength": 100, "description": "与 workspace_id 配套的上下文令牌；多项目时必须提供"})
    for _key, _property in _spec["inputSchema"]["properties"].items():
        if _property.get("type") == "integer":
            _property.setdefault("minimum", 1)
            if _key == "timeout":
                _property.setdefault("maximum", EXEC_MAX_TIMEOUT)
            elif _key in ("max_entries", "max_matches", "offset", "limit"):
                _property.setdefault("maximum", 1_000_000 if _key == "offset" else 5000)
    if "paths" in _spec["inputSchema"]["properties"]:
        _spec["inputSchema"]["properties"]["paths"].update(minItems=1, maxItems=20)


def is_read_only_tool(name: str) -> bool:
    return TOOLS[name].get("read_only", name in {
        "server_info", "list_dir", "read_file", "read_files", "search_text", "git_status", "git_diff", "git_log"})


def _call_tool(name: str, ctx: ToolContext, args: dict[str, Any] | None) -> ToolResult:
    entry = TOOLS.get(name)
    if entry is None:
        raise ToolError("UNKNOWN_TOOL", f"没有这个工具：{name}", {"available": sorted(TOOLS)})
    if ctx.settings.read_only and not is_read_only_tool(name):
        raise ToolError("READ_ONLY", "服务器处于只读模式，禁止修改文件或执行命令")
    if ctx.settings.execution_mode == "native" and name in ("git_status", "git_diff", "git_log", "desktop_action"):
        raise ToolError("SANDBOX_TOOL_DISABLED", "原生沙箱执行模式不启动宿主 Git 或桌面输入；请使用任务 diff 与原生沙箱命令")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("INVALID_ARGUMENT", "arguments 必须是对象")
    try:
        validate(args, entry["inputSchema"])
    except ValidationError as exc:
        raise ToolError("INVALID_ARGUMENT", str(exc)) from None
    if "workspace_id" in args or "workspace_token" in args:
        if args.get("workspace_id") != ctx.workspace_id or args.get("workspace_token") != ctx.workspace_token:
            raise ToolError("WORKSPACE_CONTEXT_INVALID", "嵌套调用不能改变所属项目，请重新取得对应项目上下文")
    args = {key: value for key, value in args.items() if key not in ("workspace_id", "workspace_token")}
    if name not in TASK_TOOLS:
        args.pop("task_id", None)
    handler: Handler = entry["handler"]
    try:
        result = handler(ctx, args)
        if "outputSchema" in entry:
            validate(result.structured, entry["outputSchema"], "result")
        if ctx.workspace_id:
            result.structured.update(workspace_id=ctx.workspace_id, workspace_token=ctx.workspace_token)
        return result
    except (FileError, PathRejected) as exc:
        raise ToolError(exc.code, exc.message, getattr(exc, "details", {})) from None
    except ValidationError as exc:
        raise ToolError("INVALID_RESULT", str(exc)) from None
    except OSError as exc:
        raise ToolError("IO_ERROR", str(exc)) from None


def call_tool(name: str, ctx: ToolContext, args: dict[str, Any] | None) -> ToolResult:
    task_id = args.get("task_id", ctx.task_id) if isinstance(args, dict) else ctx.task_id
    if name in TASK_TOOLS or name not in TOOLS or not task_id:
        return _call_tool(name, ctx, args)
    if not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
        raise ToolError("INVALID_ARGUMENT", "task_id 格式无效")
    if ctx.task_id and task_id != ctx.task_id:
        raise ToolError("TASK_CONTEXT_INVALID", "嵌套查询不能更换所属任务")
    ctx = replace(ctx, task_id=task_id)
    mutating = not is_read_only_tool(name)
    home = ctx.task_home or ctx.workspace
    try:
        with home.write_lock if mutating else nullcontext():
            store = home.tasks
            identity = store.begin(task_id, name, mutating=mutating)
            execution = store.execution_workspace(task_id)
            ctx = replace(ctx, workspace=execution, task_home=home, settings=replace(ctx.settings, workspace=execution.root))
            scope = ACTIVE_TASK.set((str(ctx.workspace.root), task_id, identity))
            try:
                result = _call_tool(name, ctx, args)
            except Exception as exc:
                try:
                    store.record(task_id, name, identity, error=exc)
                except Exception:
                    pass
                raise
            finally:
                ACTIVE_TASK.reset(scope)
            try:
                store.record(task_id, name, identity, result=result)
            except (FileError, OSError) as exc:
                result.structured["task_tracking_warning"] = str(exc)
            result.structured.update(task_id=task_id, task_call_id=identity)
            return result
    except (FileError, PathRejected) as exc:
        raise ToolError(exc.code, exc.message, getattr(exc, "details", {})) from None
