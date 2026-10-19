"""按项目持久化任务与执行证据；摘要是模型报告，文件/进程状态现场复核。"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .config import app_data_dir
from .files import FileError, read_window
from .workspace import PathRejected


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskStore:
    def __init__(self, workspace):
        self.workspace = workspace
        self.key = hashlib.sha256(os.path.normcase(str(workspace.root)).encode()).hexdigest()[:24]
        self.path = app_data_dir() / "tasks" / self.key / "tasks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, operation_id TEXT UNIQUE, fingerprint TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, data TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id,seq);
                CREATE TABLE IF NOT EXISTS observations(task_id TEXT,path TEXT,revision TEXT,PRIMARY KEY(task_id,path));
                CREATE TABLE IF NOT EXISTS processes(id TEXT PRIMARY KEY,task_id TEXT,data TEXT);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            raise FileError("TASK_STORAGE_ERROR", f"任务记录读写失败：{exc}") from None
        finally:
            db.close()

    def _load(self, db, task_id):
        row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise FileError("TASK_NOT_FOUND", "当前项目找不到此任务，请先核对项目身份和任务 ID")
        return json.loads(row[0])

    def _save(self, db, task):
        task["updated_at"] = time.time()
        if len(_json(task).encode()) > 64_000:
            raise FileError("TASK_TOO_LARGE", "任务摘要和进度超过 64 KB，请精简记录")
        db.execute("UPDATE tasks SET data=? WHERE id=?", (_json(task), task["task_id"]))

    def _event(self, db, task_id, kind, data):
        cursor = db.execute("INSERT INTO events(task_id,kind,data,created) VALUES(?,?,?,?)", (task_id, kind, _json(data), time.time()))
        return cursor.lastrowid

    def create(self, args, *, quota_mb=2048):
        fingerprint = hashlib.sha256(_json(args).encode()).hexdigest()
        with self.workspace.write_lock, self._db() as db:
            previous = db.execute("SELECT fingerprint,data FROM tasks WHERE operation_id=?", (args["operation_id"],)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise FileError("OPERATION_ID_REUSED", "任务创建 ID 已用于另一份请求")
                return {**json.loads(previous[1]), "replayed": True}
            saved = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks")]
            if sum(not task.get("archived") for task in saved) >= 1000 or len(saved) >= 10000:
                raise FileError("TASK_LIMIT", "未归档任务上限 1000，全部历史上限 10000；先归档结束任务")
            task = {"task_id": uuid.uuid4().hex, "title": args.get("title") or args["goal"][:80],
                    "goal": args["goal"], "scope": args.get("scope", ""),
                    "acceptance_criteria": args.get("acceptance_criteria", []), "required_checks": args.get("required_checks", []),
                    "status": "running", "revision": 1, "summary": "", "reason": "", "steps": [], "decisions": [],
                    "next_steps": [], "delivery": None, "owner_run": self.workspace.run_id,
                    "created_at": time.time(), "updated_at": time.time()}
            if len(_json(task).encode()) > 64_000:
                raise FileError("TASK_TOO_LARGE", "任务目标与验收条件超过 64 KB")
            if args.get("isolation", "direct") == "copy":
                task["isolation"] = self.workspace.isolated.create(task["task_id"], quota_mb=quota_mb)
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (task["task_id"], args["operation_id"], fingerprint, _json(task)))
            self._event(db, task["task_id"], "created", {"goal": task["goal"]})
            return task

    def execution_workspace(self, task_id):
        task = self.get(task_id)
        if task.get("isolation", {}).get("mode") == "copy":
            if task["isolation"].get("released"):
                raise FileError("TASK_COPY_RELEASED", "任务副本已清理；历史仍可 task_get/task_export，撤回已合入修改仍使用 task_revert")
            return self.workspace.isolated.workspace(task_id)
        return self.workspace

    def get(self, task_id):
        with self._db() as db:
            task = self._load(db, task_id)
        task["recovery_required"] = task["status"] == "running" and task["owner_run"] != self.workspace.run_id
        return task

    def listing(self, *, offset=0, limit=20, status=None, archived=False):
        with self._db() as db:
            tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid DESC")]
        if status:
            tasks = [task for task in tasks if task["status"] == status]
        tasks = [task for task in tasks if bool(task.get("archived")) == archived]
        selected = [{key: task[key] for key in ("task_id", "title", "status", "revision", "updated_at")}
                    | {"archived": bool(task.get("archived")), "recovery_required": task["status"] == "running" and task["owner_run"] != self.workspace.run_id}
                    | {"isolation": {key: task.get("isolation", {}).get(key) for key in ("mode", "state")}}
                    for task in tasks[offset:offset + limit]]
        return {"tasks": selected, "count": len(selected), "next_offset": offset + limit if offset + limit < len(tasks) else None}

    def events(self, task_id, *, after_seq=None, limit=20):
        with self._db() as db:
            self._load(db, task_id)
            if after_seq is None:
                rows = list(db.execute("SELECT seq,kind,data,created FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id, limit)))[::-1]
            else:
                rows = list(db.execute("SELECT seq,kind,data,created FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after_seq, limit)))
            last = rows[-1][0] if rows else (after_seq or 0)
            more = db.execute("SELECT 1 FROM events WHERE task_id=? AND seq>? LIMIT 1", (task_id, last)).fetchone() is not None
        return {"events": [{"seq": row[0], "kind": row[1], "data": json.loads(row[2]), "at": row[3]} for row in rows],
                "next_after_seq": last, "has_more": more}

    def update(self, args):
        with self.workspace.write_lock, self._db() as db:
            task = self._load(db, args["task_id"])
            if task["revision"] != args["expected_revision"]:
                raise FileError("TASK_REVISION_MISMATCH", "任务已被其他对话更新，请重新读取")
            if task["status"] in ("completed", "cancelled"):
                raise FileError("TASK_CLOSED", "任务已结束，请创建新任务")
            status = args.get("status", task["status"])
            if status in ("blocked", "waiting_user", "failed") and not args.get("reason", task["reason"]):
                raise FileError("REASON_REQUIRED", "请说明任务阻塞、等待或失败的原因")
            for key in ("summary", "reason", "steps", "decisions", "next_steps"):
                if key in args:
                    task[key] = args[key]
            task.update(status=status, revision=task["revision"] + 1, owner_run=self.workspace.run_id)
            self._save(db, task)
            self._event(db, task["task_id"], "updated", {"status": status, "summary": task["summary"][:1000]})
            return task

    def begin(self, task_id, tool, *, mutating=False):
        with self._db() as db:
            task = self._load(db, task_id)
            if mutating and task.get("archived"):
                raise FileError("TASK_ARCHIVED", "任务已归档，不能继续执行修改")
            if mutating and self.workspace.closed:
                raise FileError("SERVICE_STOPPING", "服务正在停止")
            if mutating and (task["status"] != "running" or task["owner_run"] != self.workspace.run_id):
                raise FileError("TASK_NOT_RUNNING", "任务未在本次服务中运行；先读取 task_context，核对后用 task_update 恢复")
            if db.execute("SELECT count(*) FROM events WHERE task_id=?", (task_id,)).fetchone()[0] >= 10_000:
                raise FileError("TASK_EVENT_LIMIT", "任务执行记录已达上限，请保存摘要并创建后续任务")
            return self._event(db, task_id, "tool_started", {"tool": tool, "mutating": mutating})

    def record(self, task_id, tool, call_id, result=None, error=None):
        data = result.structured if result is not None else {}
        summary = {key: data[key] for key in ("process_id", "change_id", "state", "exit_code", "running", "check_name", "replayed", "dry_run", "count", "performed", "verification_required", "operation_id") if key in data}
        summary.update(tool=tool, call_id=call_id, failed=error is not None or bool(result and result.is_error))
        if error is not None:
            summary["code"] = getattr(error, "code", "INTERNAL_ERROR")
        with self._db() as db:
            self._load(db, task_id)
            self._event(db, task_id, "tool_finished", summary)
            observations = []
            if data.get("path") and "revision" in data and not summary["failed"]:
                observations.append((data["path"], data["revision"]))
            for item in data.get("files", []):
                if isinstance(item, dict) and item.get("path") and "revision" in item:
                    observations.append((item["path"], item["revision"]))
            for item in data.get("changes", []):
                if (not summary["failed"] and not data.get("dry_run") and isinstance(item, dict)
                        and item.get("path") and "revision_after" in item):
                    observations.append((item["path"], item["revision_after"]))
            for path, revision in observations:
                previous = db.execute("SELECT revision FROM observations WHERE task_id=? AND path=?", (task_id, path)).fetchone()
                if previous is not None and previous[0] != revision:
                    self._event(db, task_id, "file_version_changed", {"path": path})
                db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?)", (task_id, path, revision))

    def process_update(self, data):
        task_id = data.get("task_id")
        if not task_id:
            return
        # 不复制命令、环境变量或完整日志到任务数据库。
        record = {key: data.get(key) for key in ("process_id", "task_id", "state", "exit_code", "timed_out", "started_at", "finished_at", "check_name", "validation_warning", "workspace_revision")}
        with self._db() as db:
            self._load(db, task_id)
            old = db.execute("SELECT data FROM processes WHERE id=?", (record["process_id"],)).fetchone()
            if old is None or json.loads(old[0]) != record:
                db.execute("INSERT OR REPLACE INTO processes VALUES(?,?,?)", (record["process_id"], task_id, _json(record)))
                self._event(db, task_id, "process_state", record)

    def context(self, task_id):
        task = self.get(task_id)
        if task.get("isolation", {}).get("released"):
            return {"task": task, **self.events(task_id), "files": [], "processes": [], "changes": [],
                    "project_materials": [], "unfinished_calls": [], "unfinished_call_count": 0,
                    "execution_directory": None, "original_directory": str(self.workspace.root),
                    "files_truncated": False, "processes_truncated": False, "historical_only": True,
                    "notice": "任务副本已清理，此包仅含历史记录；没有回退到原项目，也没有重新验证旧副本。已合入修改仍可按凭证 task_revert。"}
        workspace = self.execution_workspace(task_id)
        process_key = hashlib.sha256(os.path.normcase(str(workspace.root)).encode()).hexdigest()[:24]
        with self._db() as db:
            observations = list(db.execute("SELECT path,revision FROM observations WHERE task_id=? ORDER BY path LIMIT 201", (task_id,)))
            processes = [json.loads(row[0]) for row in db.execute("SELECT data FROM processes WHERE task_id=? ORDER BY rowid DESC LIMIT 101", (task_id,))]
            pending = {}
            for seq, kind, raw in db.execute("SELECT seq,kind,data FROM events WHERE task_id=? AND kind IN ('tool_started','tool_finished') ORDER BY seq", (task_id,)):
                data = json.loads(raw)
                if kind == "tool_started" and data.get("mutating"):
                    pending[seq] = {"call_id": seq, "tool": data["tool"]}
                elif kind == "tool_finished":
                    pending.pop(data["call_id"], None)
        files, budget = [], 20 * 1024 * 1024
        for path, expected in observations[:200]:
            item = {"path": path, "expected_revision": expected}
            try:
                target = workspace.resolve_path(path).absolute
                if not target.exists():
                    current = None
                elif not target.is_file() or target.stat().st_size > budget:
                    item["state"] = "not_checked_budget"
                    files.append(item)
                    continue
                else:
                    digest = hashlib.sha256()
                    with target.open("rb") as handle:
                        while chunk := handle.read(min(64_000, budget + 1)):
                            budget -= len(chunk)
                            if budget < 0:
                                raise FileError("TOO_LARGE", "文件核对预算耗尽")
                            digest.update(chunk)
                    current = digest.hexdigest()
                item.update(current_revision=current, state="unchanged" if current == expected else "changed")
            except (OSError, FileError, PathRejected) as exc:
                item.update(state="unavailable", code=getattr(exc, "code", "READ_FAILED"))
            files.append(item)
        for process in processes[:100]:
            metadata = app_data_dir() / "processes" / process_key / process["process_id"] / "process.json"
            try:
                with metadata.open("rb") as handle:
                    saved = json.loads(handle.read(16_000))
                if saved.get("task_id") == task_id:
                    for key in ("state", "exit_code", "timed_out", "finished_at", "validation_warning", "workspace_revision"):
                        if key in saved:
                            process[key] = saved[key]
            except (OSError, ValueError):
                pass
            manager = workspace._processes
            if manager is not None:
                with manager.lock:
                    job = manager.jobs.get(process["process_id"])
            else:
                job = None
            process["live_in_this_service"] = job is not None
            process["running"] = bool(job is not None and not job["done"].is_set())
            if job is None and process["state"] == "running":
                process["state"] = "unknown_after_restart"
            if process in processes[:5]:
                for name in ("stdout", "stderr"):
                    try:
                        path = metadata.parent / (name + ".log")
                        with path.open("rb") as handle:
                            handle.seek(max(0, path.stat().st_size - 2000))
                            process[name + "_tail"] = handle.read(2000).decode("utf-8", errors="replace")
                    except OSError:
                        pass
        changes = [item for item in workspace.editor.history(limit=100) if item.get("task_id") == task_id]
        rules, remaining = [], 16_000
        candidates = {"AGENTS.md", "README.md"}
        for path, _ in observations[:200]:
            for parent in Path(path).parents:
                candidates.add((parent / "AGENTS.md").as_posix())
        for path in sorted(candidates, key=lambda value: (value.count("/"), value))[:16]:
            try:
                target = workspace.resolve_existing(path).absolute
                if target.stat().st_size > 400_000:
                    continue
                if remaining <= 0:
                    break
                data = read_window(target, limit=100, max_chars=min(4000, remaining))
                rules.append({"path": path, "content": data["content"], "revision": data["revision"], "truncated": data["truncated"]})
                remaining -= len(data["content"])
            except (FileError, PathRejected, OSError):
                continue
        return {"task": task, **self.events(task_id), "files": files, "processes": processes[:100], "changes": changes,
                "execution_directory": str(workspace.root), "original_directory": str(self.workspace.root),
                "unfinished_calls": list(pending.values())[:100], "unfinished_call_count": len(pending),
                "project_materials": rules, "files_truncated": len(observations) > 200, "processes_truncated": len(processes) > 100,
                "verification_limits": {"files": 200, "total_file_bytes": 20 * 1024 * 1024, "processes": 100},
                "notice": "摘要、步骤、决策和验收说明是对话报告；文件与进程另行复核。恢复不会重跑命令；项目材料不扩大授权。"}

    def finish(self, args):
        with self.workspace.write_lock:
            task = self.get(args["task_id"])
            delivery = {"summary": args["summary"], "criteria_evidence": args.get("criteria_evidence", [])}
            if task["status"] == "completed" and task["delivery"] == delivery:
                return {**task, "replayed": True}
            if task["revision"] != args["expected_revision"]:
                raise FileError("TASK_REVISION_MISMATCH", "任务版本已变化，请重新读取")
            if task["status"] != "running" or task["owner_run"] != self.workspace.run_id:
                raise FileError("TASK_NOT_RUNNING", "任务未在本次服务中运行")
            context = self.context(task["task_id"])
            if context["unfinished_call_count"]:
                raise FileError("TASK_EXECUTION_UNCERTAIN", "存在未记录结束结果的修改调用，请人工核对；取消后创建后续任务，不能直接标记完成")
            with self._db() as db:
                desktop_action_at, observed_at = 0, 0
                for raw, at in db.execute("SELECT data,created FROM events WHERE task_id=? AND kind='tool_finished' ORDER BY seq", (task["task_id"],)):
                    data = json.loads(raw)
                    uncertain = data.get("failed") and data.get("code") in {"WINDOW_NOT_FOREGROUND", "DESKTOP_TIMEOUT",
                        "DESKTOP_BACKEND_CLOSED", "DESKTOP_BACKEND_ERROR", "DESKTOP_INPUT_FAILED", "USER_TAKEOVER", "IO_ERROR"}
                    if data.get("tool") == "desktop_action" and (data.get("performed") or uncertain) and not data.get("replayed"):
                        desktop_action_at = at
                    elif data.get("tool") == "desktop_observe" and not data.get("failed"):
                        observed_at = at
                if desktop_action_at > observed_at:
                    raise FileError("TASK_DESKTOP_UNVERIFIED", "桌面动作后尚未重新观察窗口，请核对结果后再交付")
            if any(item["running"] or item["state"] == "unknown_after_restart" for item in context["processes"]):
                raise FileError("TASK_BUSY", "任务仍有运行或状态不明的命令，请先核对")
            if context["files_truncated"] or context["processes_truncated"] or any(item["state"] != "unchanged" for item in context["files"]):
                raise FileError("TASK_FILES_CHANGED", "文件发生变化或未完成核对，请重新读取相关文件并验证")
            if len(delivery["criteria_evidence"]) != len(task["acceptance_criteria"]):
                raise FileError("TASK_EVIDENCE_REQUIRED", "请按验收条件顺序提供对应说明；说明属于对话报告")
            checks = {}
            tree_revision = self.workspace.isolated.tree_revision(task["task_id"]) if task.get("isolation") else None
            for process in sorted(context["processes"], key=lambda item: item.get("started_at") or 0):
                if process.get("check_name"):
                    name = process["check_name"]
                    checks["tests" if name == "test" else name] = process
            for name in task["required_checks"]:
                check = checks.get("tests" if name == "test" else name)
                if not check or check["state"] != "exited" or check["exit_code"] != 0 or check.get("validation_warning"):
                    raise FileError("TASK_CHECKS_FAILED", f"必需检查尚未通过：{name}")
                if tree_revision is not None and check.get("workspace_revision") != tree_revision:
                    raise FileError("TASK_CHECKS_STALE", f"任务副本在检查后发生变化，请重新执行：{name}")
                with self._db() as db:
                    mutations = [created for raw, created in db.execute("SELECT data,created FROM events WHERE task_id=? AND kind='tool_finished'", (task["task_id"],))
                        if (data := json.loads(raw)).get("tool") in {"write_file", "replace_in_file", "apply_patch", "edit_files", "rename_symbol", "undo_changes", "exec_command"}
                        and not data.get("failed") and not data.get("replayed") and not data.get("dry_run") and data.get("state") != "unchanged"]
                    mutations.extend(row[0] for row in db.execute("SELECT created FROM events WHERE task_id=? AND kind='file_version_changed'", (task["task_id"],)))
                if mutations and check["started_at"] < max(mutations):
                    raise FileError("TASK_CHECKS_STALE", f"检查早于最近修改或命令，请重新执行：{name}")
            with self._db() as db:
                latest = self._load(db, task["task_id"])
                if latest["revision"] != task["revision"]:
                    raise FileError("TASK_REVISION_MISMATCH", "任务已被其他对话更新")
                latest.update(status="completed", revision=latest["revision"] + 1, delivery=delivery)
                if tree_revision is not None:
                    latest["isolation"]["delivery_tree_revision"] = tree_revision
                self._save(db, latest)
                self._event(db, task["task_id"], "completed", delivery)
                return latest

    def cancel(self, task_id, expected_revision, reason):
        with self.workspace.write_lock:
            with self._db() as db:
                task = self._load(db, task_id)
                if task["revision"] != expected_revision:
                    raise FileError("TASK_REVISION_MISMATCH", "任务已变化，请重新读取")
                if task["status"] == "completed":
                    raise FileError("TASK_CLOSED", "任务已完成")
                task.update(status="cancelled", reason=reason, revision=task["revision"] + 1)
                self._save(db, task)
                self._event(db, task_id, "cancelled", {"reason": reason})
            manager = self.execution_workspace(task_id)._processes
            if manager is not None:
                for job in list(manager.jobs.values()):
                    if job.get("task_id") == task_id and not job["done"].is_set():
                        manager.stop(job["process_id"])
            return task

    def isolated_changes(self, task_id, offset=0, limit=50):
        with self.workspace.write_lock:
            task = self.get(task_id)
            if task.get("isolation", {}).get("released"):
                raise FileError("TASK_COPY_RELEASED", "副本已清理，不能再预览或合入；任务历史和已合入撤回凭证继续保留")
            if task.get("isolation", {}).get("mode") != "copy":
                raise FileError("TASK_NOT_ISOLATED", "此任务未使用独立文件副本")
            _edits, result = self.workspace.isolated.prepare(task_id)
            rows = result["changes"]
            result["changes"] = rows[offset:offset + limit]
            conflicts = set(result["conflicts"])
            result["conflict_count"] = len(conflicts)
            result["conflicts"] = [row["path"] for row in result["changes"] if row["path"] in conflicts]
            while len(_json(result).encode()) > 110_000:
                if result["diff"]:
                    result["diff"] = result["diff"][:len(result["diff"]) // 2]
                    result["diff_truncated"] = True
                elif result["changes"]:
                    result["changes"].pop()
                    result["conflicts"] = [row["path"] for row in result["changes"] if row["path"] in conflicts]
                else:
                    break
            result["next_offset"] = offset + len(result["changes"]) if offset + len(result["changes"]) < len(rows) else None
            result["diff_is_global_excerpt"] = True
            return {"task_id": task_id, "task_revision": task["revision"], "isolation_state": task["isolation"]["state"], **result}

    def _transition(self, task, state, receipt, diff_revision=None):
        with self._db() as db:
            latest = self._load(db, task["task_id"])
            if latest["revision"] != task["revision"]:
                raise FileError("TASK_REVISION_MISMATCH", "任务已变化；文件操作有独立恢复记录，请核对后重试")
            latest["isolation"]["state"] = state
            latest["isolation"]["apply_receipt" if state == "applied" else "revert_receipt"] = receipt
            if diff_revision:
                latest["isolation"]["apply_diff_revision"] = diff_revision
            latest["revision"] += 1
            self._save(db, latest)
            self._event(db, latest["task_id"], state, receipt)
            return {"task": latest, **receipt}

    def apply_isolated(self, args):
        from .isolation import apply_scope
        from .tool_types import ACTIVE_TASK
        with self.workspace.write_lock:
            task = self.get(args["task_id"])
            if task.get("isolation", {}).get("mode") != "copy":
                raise FileError("TASK_NOT_ISOLATED", "此任务未使用独立文件副本")
            if args.get("dry_run", True):
                return {"dry_run": True, **self.isolated_changes(task["task_id"])}
            if task["isolation"]["state"] == "applied" and args.get("diff_revision") == task["isolation"].get("apply_diff_revision"):
                return {"task": task, **task["isolation"]["apply_receipt"], "replayed": True}
            if task["revision"] != args.get("expected_revision"):
                raise FileError("TASK_REVISION_MISMATCH", "合入必须携带当前任务版本")
            if task["status"] != "completed" or task["isolation"]["state"] != "working":
                raise FileError("TASK_NOT_READY", "先完成副本内任务及检查；已合入或撤回的副本不能再次合入")
            if not args.get("diff_revision"):
                raise FileError("MISSING_REVISION", "先 task_changes 预览，再携带 diff_revision 合入")
            if len(_json(task).encode()) > 62_000:
                raise FileError("TASK_TOO_LARGE", "任务记录没有足够空间保存合入凭证")
            manager = self.workspace._processes
            if manager is not None and any(item["state"] == "running" for item in manager.summaries()):
                raise FileError("SOURCE_BUSY", "原项目仍有本服务命令运行，结束后再合入")
            scope = apply_scope(self.workspace, task["task_id"])
            try:
                operation_id = "isolated-apply-" + task["task_id"]
                result = self.workspace.editor.replay(operation_id, args["diff_revision"])
                if result is None:
                    if self.workspace.isolated.tree_revision(task["task_id"]) != task["isolation"].get("delivery_tree_revision"):
                        raise FileError("TASK_COPY_CHANGED", "副本在任务完成后发生变化，请创建后续任务重新检查")
                    edits, preview = self.workspace.isolated.prepare(task["task_id"])
                    if preview["diff_revision"] != args["diff_revision"]:
                        raise FileError("REVISION_MISMATCH", "预览后副本或原项目发生变化，请重新预览")
                    if preview["conflicts"]:
                        raise FileError("ISOLATION_CONFLICT", "原项目包含与任务冲突的修改，未覆盖", {"paths": preview["conflicts"]})
                    if not edits:
                        raise FileError("NO_CHANGES", "任务副本没有需要合入的文件变化")
                    result = self.workspace.editor.commit(edits, operation_id=operation_id, fingerprint=args["diff_revision"])
                receipt = {key: result[key] for key in ("change_id", "count", "state")}
                return self._transition(task, "applied", receipt, args["diff_revision"])
            finally:
                ACTIVE_TASK.reset(scope)

    def revert_isolated(self, args):
        from .isolation import apply_scope
        from .tool_types import ACTIVE_TASK
        with self.workspace.write_lock:
            task = self.get(args["task_id"])
            isolation = task.get("isolation", {})
            if isolation.get("state") == "reverted":
                return {"task": task, **isolation["revert_receipt"], "replayed": True}
            if task["revision"] != args["expected_revision"]:
                raise FileError("TASK_REVISION_MISMATCH", "任务版本已变化，请重新读取")
            if isolation.get("state") != "applied":
                raise FileError("TASK_NOT_APPLIED", "此任务没有可撤回的合入记录")
            if len(_json(task).encode()) > 62_500:
                raise FileError("TASK_TOO_LARGE", "任务记录没有足够空间保存撤回凭证")
            manager = self.workspace._processes
            if manager is not None and any(item["state"] == "running" for item in manager.summaries()):
                raise FileError("SOURCE_BUSY", "原项目仍有本服务命令运行，结束后再撤回")
            scope = apply_scope(self.workspace, task["task_id"])
            try:
                result = self.workspace.editor.undo(isolation["apply_receipt"]["change_id"], dry_run=args.get("dry_run", True))
                if args.get("dry_run", True):
                    return {"task_id": task["task_id"], **result}
                receipt = {key: result[key] for key in ("change_id", "count", "state")}
                return self._transition(task, "reverted", receipt)
            finally:
                ACTIVE_TASK.reset(scope)

    def interrupt(self):
        with self.workspace.write_lock, self._db() as db:
            for row in list(db.execute("SELECT data FROM tasks")):
                task = json.loads(row[0])
                if task["owner_run"] == self.workspace.run_id and task["status"] == "running":
                    task.update(status="interrupted", revision=task["revision"] + 1, reason="服务已停止，恢复前请重新核对状态")
                    self._save(db, task)
                    self._event(db, task["task_id"], "interrupted", {})
