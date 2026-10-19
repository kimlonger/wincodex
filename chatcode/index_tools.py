"""增量索引管理和按预算组装项目上下文。"""

import json

from .files import FileError, read_text_file
from .tool_types import ToolResult, spec
from .workspace import PathRejected


def _result(data):
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def refresh(ctx, args):
    return _result(ctx.workspace.index.refresh(max_files=args.get("max_files", 500), force=args.get("force", False)))


def status(ctx, args):
    return _result(ctx.workspace.index.status())


def context(ctx, args):
    budget = args.get("budget_chars", 16000)
    updated = ctx.workspace.index.refresh()
    matches = ctx.workspace.index.search(args["query"], limit=100, budget_chars=min(budget, 20000))
    records, used, seen, skipped = [], 0, set(), []
    for item in sorted(matches["symbols"], key=lambda s: (s["name"].casefold() != args["query"].casefold(), s["path"], s["start_line"])):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        try:
            content = read_text_file(ctx.workspace.resolve_existing(item["path"]).absolute, max_bytes=2 * 1024 * 1024)
            if content.revision != item["revision"]:
                skipped.append({"path": item["path"], "code": "INDEX_STALE"})
                continue
            lines = content.text.split("\n")
            start, end = item["start_line"], min(item["end_line"], item["start_line"] + 59)
            record = {"path": item["path"], "revision": content.revision, "symbol": item["qualified_name"],
                      "start_line": start, "end_line": end, "content": "\n".join(lines[start - 1:end]),
                      "semantic": item.get("semantic", False), "excerpt_truncated": end < item["end_line"]}
            encoded = len(json.dumps(record, ensure_ascii=False))
            remaining = budget - used
            if encoded > remaining:
                trim = len(record["content"]) - (encoded - remaining)
                if trim < 200:
                    break
                record["content"] = record["content"][:trim]
                record["end_line"] = start + record["content"].count("\n")
                record["excerpt_truncated"] = True
                encoded = len(json.dumps(record, ensure_ascii=False))
            if used + encoded > budget:
                break
            records.append(record)
            used += encoded
            if len(records) >= args.get("max_files", 8):
                break
        except (FileError, PathRejected, OSError) as exc:
            skipped.append({"path": item["path"], "code": getattr(exc, "code", "IO_ERROR")})
    return _result({"query": args["query"], "files": records, "budget_chars": budget, "used_chars": used,
                    "truncated": len(records) < len(seen) or matches["truncated"] or len(records) < len({i['path'] for i in matches['symbols']}),
                    "index_complete": updated["complete"], "skipped": skipped,
                    "notice": "仅提供匹配定义附近片段；Java/Go 无 LSP 时为声明候选，不等于完整依赖或语义上下文。"})


EXTRA_TOOLS = {
    "index_status": spec("查看持久符号索引容量、文件数与最近刷新状态。", status, {}),
    "refresh_index": spec("按文件时间/大小增量更新持久索引；单次有时间和文件预算，complete=false 时可续调。force=true 强制重建当前批次。", refresh,
        {"max_files": {"type": "integer", "minimum": 1, "maximum": 5000}, "force": {"type": "boolean"}}),
    "context_pack": spec("按符号查询组装有字符预算的代码片段，携带实际文件版本；减少网页对话上下文占用。", context,
        {"query": {"type": "string", "minLength": 1, "maxLength": 200},
         "budget_chars": {"type": "integer", "minimum": 1000, "maximum": 60000},
         "max_files": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]),
}
