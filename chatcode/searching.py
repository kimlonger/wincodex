"""正则在可终止子进程执行，避免回溯占住服务线程和 GIL。"""

import multiprocessing
from pathlib import Path
import threading

from .tool_types import ToolError, ToolResult

_SLOTS = threading.BoundedSemaphore(2)


def _worker(pipe, root, policy, args):
    try:
        from .config import Settings
        from .tool_types import ToolContext
        from .tools import _search_text
        from .workspace import Workspace
        workspace = Workspace(Path(root), allow_sensitive_files=policy.allow_sensitive)
        workspace.policy = policy
        result = _search_text(ToolContext(workspace, Settings(workspace=Path(root), allow_exec=False)), args)
        pipe.send(("ok", result.text, result.structured))
    except Exception as exc:
        pipe.send(("error", getattr(exc, "code", "SEARCH_FAILED"), str(exc)))
    finally:
        pipe.close()


def regex_search(ctx, args):
    if not _SLOTS.acquire(blocking=False):
        raise ToolError("SEARCH_BUSY", "最多同时进行两个正则搜索，请稍后重试")
    spawn = multiprocessing.get_context("spawn")
    receive, send = spawn.Pipe(duplex=False)
    process = spawn.Process(target=_worker, args=(send, str(ctx.workspace.root), ctx.workspace.policy, args), daemon=True)
    started = False
    try:
        process.start()
        started = True
        send.close()
        if not receive.poll(args.get("timeout_ms", 5000) / 1000):
            raise ToolError("SEARCH_TIMEOUT", "正则搜索超过时限，已终止；请简化表达式、缩小目录或使用普通文本搜索")
        try:
            result = receive.recv()
        except EOFError:
            raise ToolError("SEARCH_FAILED", "正则搜索进程异常退出") from None
        if result[0] == "error":
            raise ToolError(result[1], result[2])
        return ToolResult(result[1], result[2])
    finally:
        if started:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            process.close()
        receive.close()
        send.close()
        _SLOTS.release()
