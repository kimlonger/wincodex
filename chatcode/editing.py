"""工作区编辑提交：字节版本复核、写入日志、失败回滚和启动恢复。

同一工作区的服务修改通过文件锁串行提交。外部编辑器不参与锁，因此每次落盘与
恢复前都复核字节版本；发生冲突时保留恢复材料，不覆盖外部改动。
"""

from __future__ import annotations

import json
import difflib
import hashlib
import os
import re
import shutil
import stat
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import app_data_dir
from .files import FileError, atomic_write_bytes, bytes_revision, read_text_file
from .workspace import PathRejected
from .tool_types import ACTIVE_TASK

if TYPE_CHECKING:
    from .workspace import Workspace


@dataclass(frozen=True)
class PreparedEdit:
    path: str
    before: bytes | None
    after: bytes | None
    mode: int | None = None


class EditEngine:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        identity = os.path.normcase(str(workspace.root)).encode("utf-8")
        self.store = app_data_dir() / "changes" / bytes_revision(identity)[:24]

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _target(self, relative: str) -> Path:
        return self.workspace.resolve_for_write(relative).absolute

    def _verify(self, edit: PreparedEdit) -> Path:
        target = self._target(edit.path)
        actual = self._read(target)
        if actual != edit.before:
            raise FileError("REVISION_MISMATCH", f"文件在准备修改后发生变化，请重新读取：{edit.path}", {
                "path": edit.path,
                "expected": bytes_revision(edit.before) if edit.before is not None else None,
                "actual": bytes_revision(actual) if actual is not None else None,
            })
        return target

    def _save(self, directory: Path, manifest: dict) -> None:
        atomic_write_bytes(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"), mode=0o600)

    def _rollback(self, directory: Path, manifest: dict, indexes: list[int]) -> list[dict]:
        failures = []
        for index in reversed(indexes):
            item = manifest["files"][index]
            try:
                target = self._target(item["path"])
                current = self._read(target)
                revision = bytes_revision(current) if current is not None else None
                if revision == item["before_revision"]:
                    continue
                if revision != item["after_revision"]:
                    raise FileError("RECOVERY_CONFLICT", "目标已有后续改动，已保留外部内容")
                if item["before_revision"] is None:
                    target.unlink(missing_ok=True)
                else:
                    original = (directory / f"{index}.before").read_bytes()
                    if bytes_revision(original) != item["before_revision"]:
                        raise FileError("RECOVERY_CORRUPT", "恢复材料校验失败")
                    atomic_write_bytes(target, original, mode=item.get("mode"))
            except (OSError, FileError, PathRejected) as exc:
                failures.append({"path": item["path"], "error": str(exc)})
        manifest["state"] = "recovery_required" if failures else "rolled_back"
        manifest["recovery_errors"] = failures
        self._save(directory, manifest)
        return failures

    def recover(self) -> list[dict]:
        """只恢复被中断的提交；有后续外部修改的记录保留供人工检查。"""
        results = []
        with self.workspace.write_lock:
            if not self.store.exists():
                return results
            for directory in sorted(self.store.iterdir()):
                if not directory.is_dir() or directory.is_symlink():
                    continue
                try:
                    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                        raise ValueError("编辑恢复日志结构无效")
                    if not isinstance(manifest.get("workspace"), str) or os.path.normcase(manifest["workspace"]) != os.path.normcase(str(self.workspace.root)):
                        continue
                    if manifest.get("state") == "committing":
                        errors = self._rollback(directory, manifest, list(range(len(manifest["files"]))))
                        results.append({"change_id": manifest["change_id"], "state": manifest["state"], "errors": errors})
                    elif manifest.get("state") == "recovery_required":
                        results.append({"change_id": manifest["change_id"], "state": "recovery_required", "errors": manifest.get("recovery_errors", [])})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    results.append({"change_id": directory.name, "state": "recovery_required", "errors": [str(exc)]})
        return results

    def _prune(self, keep_id: str) -> None:
        """保留最近 100 条已完成记录、合计约 256 MiB；未完成恢复材料始终保留。"""
        records = []
        for directory in self.store.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("state") not in ("committed", "rolled_back"):
                    continue
                size = sum(path.stat().st_size for path in directory.iterdir() if path.is_file() and not path.is_symlink())
                records.append((directory.stat().st_mtime_ns, directory, size))
            except (OSError, ValueError, AttributeError):
                continue
        records.sort(reverse=True)
        used = 0
        for index, (_stamp, directory, size) in enumerate(records):
            used += size
            if directory.name != keep_id and (index >= 100 or used > 256 * 1024 * 1024):
                # 只删除本引擎命名的工作区历史目录，绝不递归删除源文件路径。
                if directory.resolve().is_relative_to(self.store.resolve()):
                    shutil.rmtree(directory)

    def _manifest(self, change_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[0-9a-f]{32}", change_id):
            raise FileError("INVALID_CHANGE_ID", "change_id 格式无效")
        directory = self.store / change_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.store.resolve()):
            raise FileError("INVALID_CHANGE_ID", "历史目录越界")
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FileError("CHANGE_NOT_FOUND", "找不到该改动记录，记录可能已过保留期限") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise FileError("HISTORY_CORRUPT", "改动记录结构无效")
        if os.path.normcase(manifest.get("workspace", "")) != os.path.normcase(str(self.workspace.root)):
            raise FileError("INVALID_CHANGE_ID", "改动记录不属于当前工作区")
        return directory, manifest

    def _records(self) -> list[dict]:
        if not self.store.exists():
            return []
        records = []
        for directory in self.store.iterdir():
            try:
                _directory, manifest = self._manifest(directory.name)
                records.append(manifest)
            except FileError:
                continue
        return sorted(records, key=lambda m: m.get("created_at", 0), reverse=True)

    def replay(self, operation_id: str, fingerprint: str) -> dict | None:
        with self.workspace.write_lock:
            for manifest in self._records():
                if manifest.get("operation_id") != operation_id:
                    continue
                active = ACTIVE_TASK.get()
                task_id = active[1] if active and active[0] == str(self.workspace.root) else None
                if manifest.get("task_id") != task_id:
                    raise FileError("TASK_RESOURCE_MISMATCH", "该编辑操作 ID 属于另一任务，请核对任务或使用新 ID")
                if manifest.get("fingerprint") != fingerprint:
                    raise FileError("OPERATION_ID_REUSED", "operation_id 已被其他请求使用，请提供新的 ID")
                if manifest["state"] != "committed":
                    raise FileError("OPERATION_FAILED", "该请求未成功提交，请检查改动状态后使用新的 operation_id", {"change_id": manifest["change_id"], "state": manifest["state"]})
                return {**manifest["result"], "replayed": True, "undone_by": manifest.get("undone_by")}
        return None

    def history(self, *, limit: int = 20) -> list[dict]:
        with self.workspace.write_lock:
            return [{key: item.get(key) for key in ("change_id", "state", "created_at", "operation_id", "undo_of", "undone_by", "task_id")}
                    | {"paths": [file["path"] for file in item["files"]]}
                    for item in self._records()[:limit]]

    def details(self, change_id: str, *, max_chars: int = 40_000) -> dict:
        with self.workspace.write_lock:
            directory, manifest = self._manifest(change_id)
            diffs, changes, used = [], [], 0
            truncated = False
            for index, item in enumerate(manifest["files"]):
                self.workspace.resolve_path(item["path"])
                changes.append(dict(item))
                texts = []
                for side in ("before", "after"):
                    expected = item[f"{side}_revision"]
                    if expected is None:
                        texts.append("")
                    else:
                        snapshot_path = directory / f"{index}.{side}"
                        try:
                            snapshot = read_text_file(snapshot_path, max_bytes=400_000)
                            if snapshot.revision != expected:
                                raise FileError("HISTORY_CORRUPT", "历史快照校验失败")
                            texts.append(snapshot.text)
                        except FileError as exc:
                            if exc.code not in ("TOO_LARGE", "BINARY_FILE", "DECODE_FAILED"):
                                raise
                            digest = hashlib.sha256()
                            with snapshot_path.open("rb") as handle:
                                while chunk := handle.read(64_000):
                                    digest.update(chunk)
                            if digest.hexdigest() != expected:
                                raise FileError("HISTORY_CORRUPT", "历史快照校验失败")
                            texts.append(f"[二进制或大文件快照，SHA-256: {expected}]\n")
                diff = "".join(difflib.unified_diff(texts[0].splitlines(keepends=True), texts[1].splitlines(keepends=True),
                                                  fromfile="a/" + item["path"], tofile="b/" + item["path"]))
                remaining = max(0, max_chars - used)
                truncated |= len(diff) > remaining
                diffs.append(diff[:remaining])
                used += min(len(diff), remaining)
            return {"change_id": change_id, "state": manifest["state"], "changes": changes, "count": len(changes),
                    "diff": "\n".join(diffs), "truncated": truncated, "undone_by": manifest.get("undone_by")}

    def undo(self, change_id: str, *, dry_run: bool = False) -> dict:
        with self.workspace.write_lock:
            directory, manifest = self._manifest(change_id)
            active = ACTIVE_TASK.get()
            if active and active[0] == str(self.workspace.root) and manifest.get("task_id") != active[1]:
                raise FileError("TASK_RESOURCE_MISMATCH", "该改动属于另一任务，不能在当前任务中撤销")
            if manifest["state"] != "committed":
                raise FileError("INVALID_CHANGE_STATE", "只能撤销成功提交的改动")
            for record in self._records():
                if record.get("undo_of") == change_id and record["state"] == "committed":
                    return {**record["result"], "replayed": True}
            edits = []
            for index, item in enumerate(manifest["files"]):
                values = []
                for side in ("after", "before"):
                    value = (directory / f"{index}.{side}").read_bytes() if item[f"{side}_revision"] is not None else None
                    if value is not None and bytes_revision(value) != item[f"{side}_revision"]:
                        raise FileError("HISTORY_CORRUPT", "撤销快照校验失败")
                    values.append(value)
                edits.append(PreparedEdit(item["path"], *values, mode=item.get("mode")))
            result = self.commit(edits, dry_run=dry_run, undo_of=change_id)
            if not dry_run:
                manifest["undone_by"] = result["change_id"]
                try:
                    self._save(directory, manifest)
                except OSError:
                    result["history_warning"] = "撤销已成功，原记录标记未能更新"
            return result

    def commit(self, edits: list[PreparedEdit], *, dry_run: bool = False, operation_id: str | None = None,
               fingerprint: str | None = None, undo_of: str | None = None) -> dict:
        with self.workspace.write_lock:
            if operation_id and not dry_run:
                previous = self.replay(operation_id, fingerprint)
                if previous is not None:
                    return previous
            targets = [self._verify(edit) for edit in edits]
            keys = [os.path.normcase(str(path)) for path in targets]
            if len(set(keys)) != len(keys):
                raise FileError("DUPLICATE_PATH", "同一次提交不能包含重复文件")
            changes = [{
                "path": edit.path,
                "revision_before": bytes_revision(edit.before) if edit.before is not None else None,
                "revision_after": bytes_revision(edit.after) if edit.after is not None else None,
            } for edit in edits]
            if dry_run:
                return {"dry_run": True, "changes": changes, "count": len(edits)}
            active = [(edit, target) for edit, target in zip(edits, targets) if edit.before != edit.after]
            if not active:
                return {"changes": changes, "count": 0, "change_id": None, "state": "unchanged"}
            change_id = uuid.uuid4().hex
            directory = self.store / change_id
            directory.mkdir(parents=True, mode=0o700)
            manifest = {"version": 1, "workspace": str(self.workspace.root), "change_id": change_id,
                        "state": "preparing", "files": [], "created_at": time.time(),
                        "operation_id": operation_id, "fingerprint": fingerprint, "undo_of": undo_of}
            active_task = ACTIVE_TASK.get()
            if active_task and active_task[0] == str(self.workspace.root):
                manifest.update(task_id=active_task[1], task_call_id=active_task[2])
            for index, (edit, target) in enumerate(active):
                mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
                manifest["files"].append({
                    "path": edit.path,
                    "before_revision": bytes_revision(edit.before) if edit.before is not None else None,
                    "after_revision": bytes_revision(edit.after) if edit.after is not None else None,
                    "mode": mode,
                })
                if edit.before is not None:
                    atomic_write_bytes(directory / f"{index}.before", edit.before, mode=0o600)
                if edit.after is not None:
                    atomic_write_bytes(directory / f"{index}.after", edit.after, mode=0o600)
            manifest["state"] = "committing"
            self._save(directory, manifest)
            written: list[int] = []
            try:
                for index, (edit, _target) in enumerate(active):
                    target = self._verify(edit)
                    if edit.after is None:
                        target.unlink()
                    else:
                        atomic_write_bytes(target, edit.after, mode=edit.mode)
                    written.append(index)
                manifest["state"] = "committed"
                manifest["result"] = {"change_id": change_id, "state": "committed", "changes": changes, "count": len(active)}
                self._save(directory, manifest)
            except (OSError, FileError, PathRejected) as exc:
                try:
                    failures = self._rollback(directory, manifest, written)
                except OSError as recovery_error:
                    failures = [{"error": str(recovery_error)}]
                code = getattr(exc, "code", "WRITE_FAILED")
                raise FileError(code, f"提交失败：{exc}；" + ("恢复未完成，已保留恢复材料" if failures else "已回滚本次写入"), {
                    "change_id": change_id, "recovery_errors": failures,
                    "state": "recovery_required" if failures else "rolled_back",
                }) from exc
            result = {"change_id": change_id, "state": "committed", "changes": changes, "count": len(active)}
            try:
                self._prune(change_id)
            except OSError as exc:
                result["history_warning"] = f"历史清理未完成：{exc}"
            return result
