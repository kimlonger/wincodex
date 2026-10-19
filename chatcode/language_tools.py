"""语义引用、跨文件重命名及可选多语言符号读取。"""

from __future__ import annotations

import ast
import json
import keyword
import re
from functools import wraps

from .edit_tools import _attach_diff
from .editing import PreparedEdit
from .files import FileError, bytes_revision, encode_text, read_text_file
from .lsp import file_path, offset, position, services, utf16_index
from .tool_types import ToolError, ToolResult, spec
from .workspace import PathRejected


def _result(data):
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def _protocol_errors(handler):
    @wraps(handler)
    def guarded(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise FileError("LSP_PROTOCOL_ERROR", f"语言服务返回的数据结构或位置无效：{exc}") from None
    return guarded


@_protocol_errors
def _outline_data(ctx, path):
    content = read_text_file(path, max_bytes=2 * 1024 * 1024)
    session = services(ctx).get(path)
    with session.lock:
        session.sync(path, content)
        if not session.capabilities.get("documentSymbolProvider"):
            raise FileError("CAPABILITY_UNAVAILABLE", "语言服务不支持文档符号")
        raw = session.request("textDocument/documentSymbol", {"textDocument": {"uri": path.as_uri()}}) or []
        symbols = []
        lines = content.text.split("\n")
        def visit(nodes, scope="", depth=0):
            if depth > 80:
                raise FileError("LSP_PROTOCOL_ERROR", "符号嵌套过深")
            for node in nodes:
                range_ = node.get("range") or node.get("location", {}).get("range")
                if not range_:
                    continue
                if node.get("location", {}).get("uri", path.as_uri()) != path.as_uri():
                    continue
                start, end = range_["start"], range_["end"]
                offset(content.text, start)
                offset(content.text, end, allow_eof=True)
                qualified = scope + node["name"]
                selection = node.get("selectionRange", range_)["start"]
                definition_line = selection["line"] + 1
                column = utf16_index(lines[selection["line"]], selection["character"]) + 1
                symbols.append({"name": node["name"], "qualified_name": qualified, "kind": node.get("kind"),
                                "signature": node.get("detail") or node["name"], "start_line": start["line"] + 1,
                                "end_line": max(start["line"] + 1, end["line"] + (1 if end["character"] else 0)),
                                "definition_line": definition_line, "column": column, "range": range_,
                                "symbol_id": f"{content.revision}:{start['line']}:{start['character']}:{end['line']}:{end['character']}"})
                visit(node.get("children", []), qualified + ".", depth + 1)
        visit(raw)
    return content, symbols


def select_lsp_symbol(ctx, path, *, symbol="", symbol_id=""):
    content, symbols = _outline_data(ctx, path)
    if symbol_id and not symbol_id.startswith(content.revision + ":"):
        raise FileError("REVISION_MISMATCH", "符号版本已变化，请重新获取大纲")
    matches = [item for item in symbols if item["symbol_id"] == symbol_id] if symbol_id else [item for item in symbols if item["qualified_name"] == symbol]
    if not matches and not symbol_id:
        matches = [item for item in symbols if item["name"] == symbol]
    if len(matches) != 1:
        raise FileError("AMBIGUOUS_SYMBOL" if matches else "SYMBOL_NOT_FOUND", "请使用唯一 qualified_name 或 symbol_id", {"candidates": matches[:20]})
    return content, matches[0]


def code_outline(ctx, args):
    path = ctx.workspace.resolve_existing(args["path"])
    if args.get("backend", "auto") != "lsp" and (path.absolute.suffix in (".py", ".pyi") or not services(ctx).configured(path.absolute) or ctx.settings.execution_mode == "native"):
        from .project_tools import code_outline as builtin
        return builtin(ctx, {key: value for key, value in args.items() if key != "backend"})
    content, symbols = _outline_data(ctx, path.absolute)
    first, limit = args.get("offset", 0), args.get("limit", 100)
    return _result({"path": path.relative, "revision": content.revision, "symbols": symbols[first:first + limit],
                    "backend": "lsp", "count": len(symbols[first:first + limit]), "total": len(symbols),
                    "next_offset": first + limit if first + limit < len(symbols) else None})


def read_code(ctx, args):
    path = ctx.workspace.resolve_existing(args["path"])
    lsp_id = len(args.get("symbol_id", "").split(":")) == 5
    if args.get("backend", "auto") != "lsp" and not lsp_id and (path.absolute.suffix in (".py", ".pyi") or not services(ctx).configured(path.absolute) or ctx.settings.execution_mode == "native"):
        from .project_tools import read_code as builtin
        return builtin(ctx, {key: value for key, value in args.items() if key != "backend"})
    content, symbol = select_lsp_symbol(ctx, path.absolute, symbol=args.get("symbol", ""), symbol_id=args.get("symbol_id", ""))
    if args.get("context_lines", 0):
        context = args["context_lines"]
        lines = content.text.split("\n")
        first, last = max(1, symbol["start_line"] - context), min(content.line_count, symbol["end_line"] + context)
        selected = "\n".join(lines[first - 1:last])
    else:
        first, last = symbol["start_line"], symbol["end_line"]
        selected = content.text[offset(content.text, symbol["range"]["start"]):offset(content.text, symbol["range"]["end"], allow_eof=True)]
    if len(selected) > 60_000:
        raise FileError("TOO_LARGE", "符号超过输出预算，请按行读取")
    return _result({"path": path.relative, "revision": content.revision, "symbol": symbol,
                    "start_line": first, "end_line": last, "content": selected, "backend": "lsp"})


@_protocol_errors
def find_symbol(ctx, args):
    if not args.get("language"):
        from .project_tools import find_symbol as builtin
        return builtin(ctx, args)
    session = services(ctx).get(language=args["language"])
    with session.lock:
        raw = session.request("workspace/symbol", {"query": args["query"]}) or []
    found = []
    base = ctx.workspace.resolve_existing(args.get("path", "."))
    for item in raw:
        try:
            location = item["location"]
            resolved = file_path(ctx.workspace, location["uri"])
            if not resolved.absolute.is_relative_to(base.absolute):
                continue
            content = read_text_file(resolved.absolute)
            point = location["range"]["start"]
            column = utf16_index(content.text.split("\n")[point["line"]], point["character"]) + 1
            found.append({"name": item["name"], "path": resolved.relative, "revision": content.revision,
                          "definition_line": point["line"] + 1, "column": column, "kind": item.get("kind")})
        except (FileError, PathRejected):
            continue
    limit = args.get("limit", 100)
    first, used, selected = args.get("offset", 0), 0, []
    for item in found[first:]:
        size = len(json.dumps(item, ensure_ascii=False))
        if len(selected) >= limit or used + size > args.get("budget_chars", 30000):
            break
        selected.append(item)
        used += size
    more = first + len(selected) < len(found)
    return _result({"symbols": selected, "count": len(selected), "backend": "lsp", "semantic": True,
                    "truncated": more, "next_offset": first + len(selected) if more else None})


def _source(ctx, args):
    resolved = ctx.workspace.resolve_existing(args["path"])
    content = read_text_file(resolved.absolute)
    point = position(content.text, args["line"], args["column"])
    return resolved, content, point


@_protocol_errors
def find_references(ctx, args):
    resolved, content, point = _source(ctx, args)
    manager = services(ctx)
    if not manager.configured(resolved.absolute) or not ctx.settings.allow_exec or ctx.settings.read_only or ctx.settings.execution_mode == "native":
        line = content.text.split("\n")[args["line"] - 1]
        at = args["column"] - 1
        tokens = [match for match in re.finditer(r"[^\W\d]\w*|[$_]\w*", line) if match.start() <= at < match.end()]
        if not tokens:
            raise ToolError("POSITION_NO_SYMBOL", "请把 column 定位到标识符字符上")
        name = tokens[0].group()
        from .tools import call_tool
        first, limit = args.get("offset", 0), args.get("limit", 100)
        data = call_tool("search_text", ctx, {"pattern": r"(?<!\w)" + re.escape(name) + r"(?!\w)", "regex": True,
                                            "max_matches": limit, "offset": first}).structured
        selected = data["matches"]
        return _result({"references": selected, "count": len(selected), "backend": "text_candidates", "semantic": False,
                        "next_offset": data["next_offset"], "truncated": data["truncated"],
                        "skipped_count": data["skipped_count"],
                        "notice": "当前没有可用的已配置语言服务；这些是文本候选，可能包含同名符号、字符串和注释"})
    session = manager.get(resolved.absolute)
    with session.lock:
        session.sync(resolved.absolute, content)
        locations = session.request("textDocument/references", {"textDocument": {"uri": resolved.absolute.as_uri()},
                                    "position": point, "context": {"includeDeclaration": args.get("include_declaration", True)}}) or []
    result, skipped, cache = [], 0, {}
    first, limit = args.get("offset", 0), args.get("limit", 100)
    for location in locations:
        try:
            target = file_path(ctx.workspace, location["uri"])
            if target.relative not in cache:
                cache[target.relative] = read_text_file(target.absolute)
            snapshot = cache[target.relative]
            start = location["range"]["start"]
            source_line = snapshot.text.split("\n")[start["line"]]
            column = utf16_index(source_line, start["character"]) + 1
            result.append({"path": target.relative, "line": start["line"] + 1, "column": column,
                           "revision": snapshot.revision, "text": source_line[:500]})
        except (FileError, PathRejected):
            skipped += 1
    return _result({"references": result[first:first + limit], "count": len(result[first:first + limit]), "backend": "lsp", "semantic": True,
                    "skipped_outside_or_unreadable": skipped, "next_offset": first + limit if first + limit < len(result) else None})


def _workspace_edits(response):
    if not response:
        return {}
    if response.get("changes") and response.get("documentChanges"):
        raise FileError("LSP_PROTOCOL_ERROR", "同时返回两种工作区编辑表示，已拒绝重复应用")
    if "changes" in response:
        return {uri: (None, edits) for uri, edits in response["changes"].items()}
    result = {}
    for item in response.get("documentChanges", []):
        if "kind" in item:
            raise FileError("UNSUPPORTED_WORKSPACE_EDIT", "重命名返回了文件资源操作，请先预览并分步处理")
        document = item["textDocument"]
        if document["uri"] in result:
            raise FileError("OVERLAPPING_EDITS", "同一文件存在多份编辑，已拒绝重复提交")
        result[document["uri"]] = (document.get("version"), item["edits"])
    return result


@_protocol_errors
def rename_symbol(ctx, args):
    fingerprint = bytes_revision(json.dumps({k: v for k, v in args.items() if k != "dry_run"}, sort_keys=True).encode())
    engine = ctx.workspace.editor
    if args.get("operation_id") and not args.get("dry_run", False):
        replay = engine.replay(args["operation_id"], fingerprint)
        if replay is not None:
            replay.update(backend="lsp", semantic=True)
            return _result(_attach_diff(engine, replay))
    resolved, source, point = _source(ctx, args)
    if source.revision != args["expected_revision"]:
        raise FileError("REVISION_MISMATCH", "源文件版本已变化，请重新读取")
    new_name = args["new_name"]
    if resolved.absolute.suffix in (".py", ".pyi") and (not new_name.isidentifier() or keyword.iskeyword(new_name)):
        raise FileError("INVALID_NAME", "新的 Python 名称必须是非关键字标识符")
    session = services(ctx).get(resolved.absolute)
    if not session.capabilities.get("renameProvider"):
        raise FileError("CAPABILITY_UNAVAILABLE", "语言服务不支持符号重命名")
    snapshots = {resolved.absolute.as_uri(): (resolved, source)}
    with session.lock:
        session.sync(resolved.absolute, source)
        for attempt in range(3):
            response = session.request("textDocument/rename", {"textDocument": {"uri": resolved.absolute.as_uri()}, "position": point, "newName": new_name})
            changes = {}
            for uri, change in _workspace_edits(response).items():
                canonical = file_path(ctx.workspace, uri).absolute.as_uri()
                if canonical in changes:
                    raise FileError("OVERLAPPING_EDITS", "同一文件被不同 URI 重复引用")
                changes[canonical] = change
            if len(changes) > 80:
                raise FileError("TOO_MANY_FILES", "单次语义重命名最多涉及 80 个文件")
            unknown = [uri for uri in changes if uri not in snapshots]
            if not unknown:
                break
            for uri in unknown:
                target = file_path(ctx.workspace, uri)
                snapshot = read_text_file(target.absolute)
                snapshots[uri] = (target, snapshot)
                session.sync(target.absolute, snapshot)
            session.sync(resolved.absolute, source)
        else:
            raise FileError("RENAME_UNSTABLE", "语言服务返回的影响文件仍在变化，请稍后重试")
        prepared = []
        for uri, (version, edits) in changes.items():
            target, snapshot = snapshots[uri]
            if version is not None and version != session.documents.get(uri, (None, None))[1]:
                raise FileError("REVISION_MISMATCH", "语言服务返回了过期文档版本")
            intervals = [(offset(snapshot.text, item["range"]["start"]), offset(snapshot.text, item["range"]["end"], allow_eof=True), item["newText"]) for item in edits]
            intervals.sort()
            for previous, current in zip(intervals, intervals[1:]):
                if current[0] < previous[1] or current[0] == previous[0]:
                    raise FileError("OVERLAPPING_EDITS", "语言服务返回了重叠编辑")
            updated = snapshot.text
            for start, end, replacement in reversed(intervals):
                if end < start or not isinstance(replacement, str):
                    raise FileError("INVALID_RANGE", "语言服务编辑范围无效")
                updated = updated[:start] + replacement + updated[end:]
            if target.absolute.suffix in (".py", ".pyi"):
                try:
                    ast.parse(updated)
                except SyntaxError as exc:
                    raise FileError("SYNTAX_ERROR", f"重命名后语法无效：{exc}") from None
            data = snapshot.raw if updated == snapshot.text else encode_text(updated, newline=snapshot.newline, has_bom=snapshot.has_bom, encoding=snapshot.encoding)
            prepared.append(PreparedEdit(target.relative, snapshot.raw, data))
        if sum(len(item.after or b"") for item in prepared) > 20 * 1024 * 1024:
            raise FileError("TOO_LARGE", "重命名内容超过单次 20 MiB 上限")
        result = engine.commit(prepared, dry_run=args.get("dry_run", False), operation_id=args.get("operation_id"), fingerprint=fingerprint)
    result.update(backend="lsp", semantic=True)
    return _result(_attach_diff(engine, result) if not args.get("dry_run") else result)


_source_properties = {"path": {"type": "string"}, "line": {"type": "integer", "minimum": 1}, "column": {"type": "integer", "minimum": 1}}
EXTRA_TOOLS = {
    "find_references": spec("按从 1 开始的行和 Unicode 字符列查引用。已配置语言服务时返回语义引用；否则明确返回 semantic=false 的文本候选。", find_references,
        {**_source_properties, "include_declaration": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
         "offset": {"type": "integer", "minimum": 0, "maximum": 100_000}}, ["path", "line", "column"]),
    "rename_symbol": spec("通过已配置的语言服务跨文件重命名。必须带源文件 expected_revision；影响文件先同步快照、再次计算，然后统一版本校验与事务提交。支持 dry_run 和 operation_id；未配置语言服务时拒绝猜测性重命名。", rename_symbol,
        {**_source_properties, "new_name": {"type": "string", "minLength": 1, "maxLength": 200}, "expected_revision": {"type": "string"},
         "dry_run": {"type": "boolean"}, "operation_id": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[A-Za-z0-9._-]+$"}},
        ["path", "line", "column", "new_name", "expected_revision"], read_only=False),
}
