"""任务归档、可审查的副本回收与有界历史导出。"""

import json
import os
from pathlib import Path
import shutil
import time

from .config import app_data_dir
from .files import FileError, atomic_write_bytes, bytes_revision
from .tool_types import ToolResult, spec


def result(data):
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def tree_usage(root, *, limit=200_000):
    total, entries = 0, 0
    digest = __import__("hashlib").sha256()
    if not root.exists():
        return {"bytes": 0, "entries": 0, "revision": digest.hexdigest()}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise FileError("STORAGE_UNSAFE_PATH", "副本包含链接或重解析点，拒绝自动清理或挂载")
            entries += 1
            if entries > limit:
                raise FileError("STORAGE_SCAN_LIMIT", "副本目录项超限，请本机检查存储")
            total += info.st_size if path.is_file() else 0
            digest.update(f"{path.relative_to(root).as_posix()}:{info.st_mtime_ns}:{info.st_size}".encode())
    return {"bytes": total, "entries": entries, "revision": digest.hexdigest()}


def usage(workspace):
    home = getattr(workspace, "_isolation_home", workspace)
    store = home.isolated.store
    if store.exists() and home.isolated._link(store):
        raise FileError("STORAGE_UNSAFE_PATH", "副本存储是链接或重解析点")
    return tree_usage(store)


def check_quota(workspace, quota_mb, *, additional=0):
    used = usage(workspace)["bytes"]
    if used + additional > quota_mb * 1024 * 1024:
        raise FileError("STORAGE_QUOTA", "任务副本超出项目容量预算；先归档并预览清理旧任务", {"used_bytes": used, "additional_bytes": additional, "quota_mb": quota_mb})


def _idle(store, task):
    with store._db() as db:
        processes = [json.loads(row[0]) for row in db.execute("SELECT data FROM processes WHERE task_id=?", (task["task_id"],))]
    # 历史仍显示 running 时宁可留存副本，避免误删不明进程的工作目录。
    if any(p.get("state") == "running" for p in processes):
        raise FileError("TASK_BUSY", "任务有运行或重启后不明的进程，拒绝回收")
    home = store.workspace
    workspaces = [home, *list(home._isolated.workspaces.values())] if home._isolated else [home]
    for ws in workspaces:
        manager = ws._processes
        if manager and any(p.get("task_id") == task["task_id"] and p["state"] == "running" for p in manager.summaries()):
            raise FileError("TASK_BUSY", "任务仍有运行进程")


def archive(ctx, args):
    store = ctx.workspace.tasks
    with ctx.workspace.write_lock:
        task = store.get(args["task_id"])
        if task.get("archived") == args.get("archived", True):
            return result({"task": task, "replayed": True})
        if task["revision"] != args["expected_revision"]:
            raise FileError("TASK_REVISION_MISMATCH", "任务版本已变化")
        if task["status"] not in ("completed", "cancelled"):
            raise FileError("TASK_NOT_CLOSED", "仅完成或取消的任务可归档")
        _idle(store, task)
        task.update(archived=args.get("archived", True), archived_at=time.time(), revision=task["revision"] + 1)
        with store._db() as db:
            store._save(db, task)
            store._event(db, task["task_id"], "archived" if task["archived"] else "unarchived", {})
        return result({"task": task})


def export(ctx, args):
    store = ctx.workspace.tasks
    with ctx.workspace.write_lock, store._db() as db:
        task = store._load(db, args["task_id"])
        data = {"format": "chatcode-task-v1", "task": task, "events": [],
                "observations": [dict(path=p, revision=r) for p, r in db.execute("SELECT path,revision FROM observations WHERE task_id=?", (task["task_id"],))]}
        used = 0
        for seq, kind, raw, created in db.execute("SELECT seq,kind,data,created FROM events WHERE task_id=? ORDER BY seq", (task["task_id"],)):
            used += len(raw.encode())
            if used > 8 * 1024 * 1024:
                raise FileError("EXPORT_TOO_LARGE", "任务导出事件超过 8 MiB；请分页读取 task_get")
            data["events"].append({"seq": seq, "kind": kind, "data": json.loads(raw), "at": created})
        data["processes"] = [json.loads(row[0]) for row in db.execute("SELECT data FROM processes WHERE task_id=?", (task["task_id"],))]
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
        path = store.path.parent / "exports" / (task["task_id"] + ".json")
        atomic_write_bytes(path, payload, mode=0o600)
        return result({"task_id": task["task_id"], "path": str(path), "bytes": len(payload), "sha256": bytes_revision(payload),
                       "includes_file_bodies": False, "includes_process_log_bodies": False})


def _plan(ctx, days):
    store = ctx.workspace.tasks
    candidates, blocked = [], []
    with store._db() as db:
        tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks")]
    for task in tasks:
        if (not task.get("archived") or task.get("archived_at", time.time()) > time.time() - days * 86400
                or task.get("isolation", {}).get("mode") != "copy"):
            continue
        try:
            _idle(store, task)
            directory = ctx.workspace.isolated.directory(task["task_id"])
            if task["isolation"].get("released") and not directory.exists():
                continue
            info = tree_usage(directory)
            candidates.append({"task_id": task["task_id"], "task_revision": task["revision"], "copy_state": task["isolation"]["state"], **info})
        except FileError as exc:
            blocked.append({"task_id": task["task_id"], "code": exc.code})
    plan = {"candidates": candidates[:100], "blocked": blocked[:100], "more": len(candidates) > 100,
            "older_than_days": days, "files_only_in_task_copy": True,
            "notice": "清理将永久删除已归档任务的文件副本（包括尚未合入的修改）；任务、事件及合入/撤回凭证继续保留。"}
    plan["plan_revision"] = bytes_revision(json.dumps(plan, sort_keys=True).encode())
    return plan


def cleanup(ctx, args):
    store = ctx.workspace.tasks
    with ctx.workspace.write_lock:
        plan = _plan(ctx, args.get("older_than_days", 7))
        if args.get("dry_run", True):
            return result({"dry_run": True, **plan})
        if not args.get("plan_revision") or plan["plan_revision"] != args["plan_revision"]:
            raise FileError("CLEANUP_PLAN_CHANGED", "先预览清理，确认后使用当前 plan_revision；目录变化后需重新预览")
        removed = []
        for item in plan["candidates"]:
            task_id = item["task_id"]
            directory = ctx.workspace.isolated.directory(task_id)
            # 绝对目标、边界、整棵树都在本次调用重新核验；不执行动态 shell 删除。
            if directory.resolve().parent != ctx.workspace.isolated.store.resolve():
                raise FileError("STORAGE_UNSAFE_PATH", "清理目录越界")
            if tree_usage(directory)["revision"] != item["revision"]:
                raise FileError("CLEANUP_PLAN_CHANGED", "副本在清理过程中发生变化")
            copy = ctx.workspace.isolated.workspaces.pop(task_id, None)
            if copy:
                if copy._processes:
                    copy._processes.shutdown()
                if copy._language_services:
                    copy._language_services.close()
                copy.closed = True
            with store._db() as db:
                task = store._load(db, task_id)
                task["isolation"]["released"] = True
                task["revision"] += 1
                store._save(db, task)
                store._event(db, task_id, "copy_released", {"bytes": item["bytes"]})
            if directory.exists():
                shutil.rmtree(directory)
            removed.append(task_id)
        return result({"dry_run": False, "removed_task_ids": removed, "freed_bytes": sum(i["bytes"] for i in plan["candidates"]), "more": plan["more"]})


def storage_status(ctx, args):
    return result({**usage(ctx.workspace), "scope": "task_copies", "quota_mb": ctx.settings.storage_quota_mb,
                   "quota_enforced": "before_copy_creation_and_command_start", "hard_runtime_disk_quota": False})


_id = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
EXTRA_TOOLS = {
    "task_archive": spec("归档或恢复已结束任务；归档不删除文件。清理前请导出需要保留的历史。", archive,
        {"task_id": _id, "expected_revision": {"type": "integer", "minimum": 1}, "archived": {"type": "boolean"}}, ["task_id", "expected_revision"], read_only=False),
    "task_export": spec("将任务、全部有界事件及执行元数据导出为本机 JSON；不含文件正文和原始命令日志。", export, {"task_id": _id}, ["task_id"], read_only=False),
    "task_cleanup": spec("清理已归档任务的文件副本，保留任务历史与合入凭证；默认仅预览。实际删除须 dry_run=false 和 plan_revision。", cleanup,
        {"older_than_days": {"type": "integer", "minimum": 0, "maximum": 3650}, "dry_run": {"type": "boolean"},
         "plan_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, read_only=False),
    "storage_status": spec("查看当前项目任务副本用量和容量预算；不删除数据。", storage_status, {}),
}
