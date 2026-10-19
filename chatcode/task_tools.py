"""任务的 MCP 接口；任务 ID 不提供工作区授权。"""

import json
from .tool_types import ToolResult, spec


def _result(data):
    if isinstance(data.get("changes"), list):
        omitted = 0
        while len(json.dumps(data, ensure_ascii=False).encode()) > 120_000 and data["changes"]:
            data["changes"].pop()
            omitted += 1
        if omitted:
            data["omitted_changes"] = omitted
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def create(ctx, args):
    if ctx.settings.execution_mode == "native":
        if args.get("isolation", "copy") != "copy":
            from .tool_types import ToolError
            raise ToolError("SANDBOX_COPY_REQUIRED", "原生沙箱模式必须创建文件副本任务")
        args = {**args, "isolation": "copy"}
    return _result(ctx.workspace.tasks.create(args, quota_mb=ctx.settings.storage_quota_mb))


def listing(ctx, args):
    return _result(ctx.workspace.tasks.listing(offset=args.get("offset", 0), limit=args.get("limit", 20), status=args.get("status"), archived=args.get("archived", False)))


def get(ctx, args):
    return _result({"task": ctx.workspace.tasks.get(args["task_id"]), **ctx.workspace.tasks.events(
        args["task_id"], after_seq=args.get("after_seq"), limit=args.get("event_limit", 20))})


def update(ctx, args):
    return _result(ctx.workspace.tasks.update(args))


def context(ctx, args):
    data = ctx.workspace.tasks.context(args["task_id"])
    omitted = {}
    # 按优先级减少附加材料，任务本体保留；明确标记所有裁剪。
    for key in ("project_materials", "events", "changes", "processes", "files"):
        while len(json.dumps(data, ensure_ascii=False).encode()) > 120_000 and data[key]:
            data[key].pop(0 if key == "events" else -1)
            omitted[key] = omitted.get(key, 0) + 1
    data["omitted_for_budget"] = omitted
    return _result(data)


def finish(ctx, args):
    return _result(ctx.workspace.tasks.finish(args))


def cancel(ctx, args):
    return _result(ctx.workspace.tasks.cancel(args["task_id"], args["expected_revision"], args.get("reason", "用户取消")))


def changes(ctx, args):
    return _result(ctx.workspace.tasks.isolated_changes(args["task_id"], args.get("offset", 0), args.get("limit", 50)))


def apply(ctx, args):
    return _result(ctx.workspace.tasks.apply_isolated(args))


def revert(ctx, args):
    return _result(ctx.workspace.tasks.revert_isolated(args))


_id = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_revision = {"type": "integer", "minimum": 1}
_text = {"type": "string", "minLength": 1, "maxLength": 2000}
_texts = {"type": "array", "maxItems": 20, "items": _text}
_status = {"type": "string", "enum": ["running", "paused", "blocked", "waiting_user", "failed", "interrupted"]}
EXTRA_TOOLS = {
    "task_create": spec("创建持久任务：保存目标、范围说明、验收条件和必需检查名称。operation_id 用于创建重试去重。", create,
        {"goal": {"type": "string", "minLength": 1, "maxLength": 8000}, "title": {"type": "string", "minLength": 1, "maxLength": 200},
         "scope": {"type": "string", "maxLength": 2000}, "acceptance_criteria": _texts, "required_checks": _texts,
         "isolation": {"type": "string", "enum": ["direct", "copy"], "description": "copy 在独立文件副本工作，完成后预览合入；非系统沙箱。省略保持旧版 direct 行为"},
         "operation_id": {"type": "string", "minLength": 1, "maxLength": 100}}, ["goal", "operation_id"], read_only=False),
    "task_list": spec("列出当前项目的持久任务；任务与后台进程分别管理。", listing,
        {"archived": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
         "status": {"type": "string", "enum": ["running", "paused", "blocked", "waiting_user", "failed", "interrupted", "cancelled", "completed"]}}),
    "task_get": spec("读取任务、版本及事件。after_seq=0 从头分页；省略时返回最近事件。", get,
        {"task_id": _id, "after_seq": {"type": "integer", "minimum": 0}, "event_limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["task_id"]),
    "task_update": spec("按任务版本更新进度、步骤、决定及下一步。恢复前先 task_context 核对，再设 running；不会重跑命令。暂停不停止已运行的进程。", update,
        {"task_id": _id, "expected_revision": _revision, "status": _status, "summary": {"type": "string", "maxLength": 4000},
         "reason": {"type": "string", "maxLength": 2000}, "decisions": _texts, "next_steps": _texts,
         "steps": {"type": "array", "maxItems": 40, "items": {"type": "object", "properties": {
             "text": _text, "status": {"type": "string", "enum": ["todo", "doing", "done"]}}, "required": ["text", "status"], "additionalProperties": False}}},
        ["task_id", "expected_revision"], read_only=False),
    "task_context": spec("换对话或重启后读取恢复包：目标、进度、决定、下一步、项目材料、已读文件实际版本、进程与改动证据。不会运行命令。", context, {"task_id": _id}, ["task_id"]),
    "task_finish": spec("提交交付说明；阻止有运行/未知进程、文件冲突或必需检查未通过的任务完成。验收说明是模型报告，不等于自动证明业务正确。", finish,
        {"task_id": _id, "expected_revision": _revision, "summary": {"type": "string", "minLength": 1, "maxLength": 4000}, "criteria_evidence": _texts},
        ["task_id", "expected_revision", "summary"], read_only=False),
    "task_cancel": spec("取消任务并停止本服务仍可控制的所属进程；不撤销已完成修改，也不按旧 PID 操作其他服务进程。", cancel,
        {"task_id": _id, "expected_revision": _revision, "reason": {"type": "string", "maxLength": 2000}}, ["task_id", "expected_revision"], read_only=False),
    "task_changes": spec("预览独立任务副本相对基线的分页文件列表与全局 diff 摘录，报告原目录冲突及 diff_revision；按 next_offset 续读。", changes,
        {"task_id": _id, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["task_id"]),
    "task_apply": spec("独立副本任务完成后合入原项目；默认 dry_run=true。实际合入须 expected_revision、diff_revision、dry_run=false；冲突拒绝覆盖。命令造成的副本文件变化也纳入合入与撤回。", apply,
        {"task_id": _id, "expected_revision": _revision, "diff_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "dry_run": {"type": "boolean"}}, ["task_id"], read_only=False),
    "task_revert": spec("整批撤回独立任务已合入的文件变化，保留后续外部修改。默认 dry_run=true；执行用 false。不会回滚网络、数据库或目录外的命令副作用。", revert,
        {"task_id": _id, "expected_revision": _revision, "dry_run": {"type": "boolean"}}, ["task_id", "expected_revision"], read_only=False),
}
