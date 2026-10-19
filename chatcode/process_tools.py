"""后台执行、日志续读与配置驱动的项目检查。"""

import json
import re
import tomllib

from .files import read_text_file
from .tool_types import ToolError, ToolResult, spec


def _result(data):
    if data.get("check_name"):
        text = data.get("stdout", "") + "\n" + data.get("stderr", "")
        diagnostics = []
        for line in text.splitlines():
            match = re.match(r"^(.+?):(\d+)(?::(\d+))?:\s*(.*)$", line)
            if match:
                diagnostics.append({"path": match[1], "line": int(match[2]), "column": int(match[3]) if match[3] else None, "message": match[4]})
            if len(diagnostics) >= 100:
                break
        data["diagnostics"], data["diagnostics_complete"] = diagnostics, False
        if not data.get("running") and re.search(r"Ran 0 tests\b|no tests ran\b", text):
            data["validation_warning"] = "检查命令没有发现测试，请确认测试框架和配置"
    failed = not data.get("running", False) and (data.get("timed_out", False) or data.get("state") == "failed" or data.get("exit_code") not in (None, 0))
    failed |= bool(data.get("validation_warning"))
    return ToolResult(json.dumps(data, ensure_ascii=False), data, is_error=failed)


def exec_command(ctx, args):
    from .tools import _looks_destructive
    if not ctx.settings.allow_exec:
        raise ToolError("EXEC_DISABLED", "服务器已禁用命令执行")
    command = args.get("command") or " ".join(args.get("argv", []))
    if _looks_destructive(command):
        raise ToolError("COMMAND_BLOCKED", "命令命中了破坏性操作拦截规则")
    return _result(ctx.workspace.processes.start(command=args.get("command"), argv=args.get("argv"), cwd=args.get("cwd"),
                   shell=args.get("shell", "default"), env=args.get("env"), timeout=args.get("timeout", 60),
                   yield_ms=args.get("yield_time_ms", 1000), operation_id=args.get("operation_id"),
                   check_name=args.get("_check_name"), task_id=ctx.task_id or None, settings=ctx.settings))


def _check_task_owner(ctx, identity):
    if ctx.task_id:
        with ctx.workspace.processes.lock:
            job = ctx.workspace.processes.jobs.get(identity)
            if job is not None and job.get("task_id") != ctx.task_id:
                raise ToolError("TASK_RESOURCE_MISMATCH", "该进程属于另一任务")


def read_process(ctx, args):
    _check_task_owner(ctx, args["process_id"])
    return _result(ctx.workspace.processes.read(args["process_id"], stdout_offset=args.get("stdout_offset", 0),
                   stderr_offset=args.get("stderr_offset", 0), max_bytes=args.get("max_bytes", 32_000), wait_ms=args.get("wait_ms", 0)))


def stop_process(ctx, args):
    _check_task_owner(ctx, args["process_id"])
    data = ctx.workspace.processes.stop(args["process_id"])
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def run_checks(ctx, args):
    if args.get("command") or args.get("argv"):
        request = {key: value for key, value in args.items() if key in ("command", "argv", "cwd", "timeout", "yield_time_ms")}
    else:
        name = args.get("name", "tests")
        config = ctx.workspace.root / ".chatcode.toml"
        checks = {}
        if config.exists():
            resolved = ctx.workspace.resolve_existing(".chatcode.toml")
            try:
                checks = tomllib.loads(read_text_file(resolved.absolute, max_bytes=64_000).text).get("checks", {})
            except ValueError as exc:
                raise ToolError("BAD_CONFIG", str(exc)) from None
        configured = checks.get(name) if isinstance(checks, dict) else None
        if configured is None and isinstance(checks, dict) and name in ("test", "tests"):
            configured = checks.get("test" if name == "tests" else "tests")
        if configured is not None:
            if not isinstance(configured, list) or not configured or not all(isinstance(part, str) for part in configured):
                raise ToolError("BAD_CONFIG", "[checks] 中的检查命令必须是非空 argv 字符串数组")
            request = {"argv": configured}
        else:
            from .project_tools import project_overview
            suggestions = project_overview(ctx, {}).structured["suggested_checks"]
            matches = [item for item in suggestions if item["name"] == name]
            if not matches and name in ("test", "tests"):
                matches = [item for item in suggestions if item["name"] in ("test", "tests")]
            if not matches:
                raise ToolError("CHECK_NOT_CONFIGURED", "请提供 argv/command，或在 .chatcode.toml 的 [checks] 中配置检查名称")
            request = {"command": matches[0]["command"]}
        request.update({key: args[key] for key in ("cwd", "timeout", "yield_time_ms") if key in args})
    request["operation_id"] = args.get("operation_id")
    request["_check_name"] = args.get("name", "tests")
    return exec_command(ctx, request)


_id = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_timeout = {"type": "integer", "minimum": 0, "maximum": 3600, "description": "总超时秒数，0 表示持续运行直到停止；默认 60"}
_yield = {"type": "integer", "minimum": 0, "maximum": 10_000, "description": "先等待多少毫秒，未完成返回 process_id，默认 1000"}
_command = {"type": "string", "minLength": 1}
_argv = {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "string"}}
_cwd = {"type": "string"}
_operation = {"type": "string", "minLength": 1, "maxLength": 100, "pattern": "^[A-Za-z0-9._-]+$",
              "description": "唯一命令 ID；网络重试复用，避免重复启动。更换命令须用新 ID"}
EXEC_SPEC = spec("执行命令或 argv。短命令直接返回；长命令返回 process_id 续读/停止。host 使用当前账户权限；native 使用 Windows AppContainer，必须携带副本任务 ID，不允许退回宿主。", exec_command,
                 {"command": _command, "argv": _argv, "cwd": _cwd, "timeout": _timeout, "yield_time_ms": _yield,
                  "shell": {"type": "string", "enum": ["default", "cmd", "powershell", "bash"]},
                  "env": {"type": "object", "additionalProperties": {"type": "string"}},
                  "operation_id": _operation}, read_only=False)
EXTRA_TOOLS = {
    "read_process": spec("按 stdout/stderr 字节游标续读当前服务后台任务；返回 next_*_offset 和滚动日志丢弃字节数。", read_process,
                         {"process_id": _id, "stdout_offset": {"type": "integer", "minimum": 0}, "stderr_offset": {"type": "integer", "minimum": 0},
                          "max_bytes": {"type": "integer", "minimum": 256, "maximum": 64_000},
                          "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10_000, "description": "等待任务结束的毫秒数，默认 0"}}, ["process_id"]),
    "stop_process": spec("停止当前服务创建的后台任务及其子进程，返回最终状态与日志。", stop_process, {"process_id": _id}, ["process_id"], read_only=False),
    "run_checks": spec("执行命名的项目检查或显式 argv/command；优先读取 .chatcode.toml 的 [checks]，返回任务状态、日志和尽力解析的诊断。", run_checks,
                       {"name": {"type": "string"}, "argv": _argv, "command": _command, "cwd": _cwd,
                        "timeout": _timeout, "yield_time_ms": _yield, "operation_id": _operation}, read_only=False),
}
