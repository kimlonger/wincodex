"""独立任务文件副本与可撤销合入；不限制进程的系统权限。"""

import copy
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .config import app_data_dir, EXCLUDED_DIR_NAMES
from .editing import PreparedEdit
from .files import FileError, atomic_write_bytes, bytes_revision
from .tool_types import ACTIVE_TASK
from .workspace import Workspace


class IsolatedTasks:
    MAX_FILES = 5000
    MAX_BYTES = 64 * 1024 * 1024
    MAX_FILE_BYTES = 8 * 1024 * 1024
    BUILD_SUFFIXES = {".exe", ".dll", ".pdb", ".pyc", ".pyo", ".obj", ".o", ".class"}

    def __init__(self, home):
        self.home = home
        key = bytes_revision(os.path.normcase(str(home.root)).encode())[:24]
        self.store = app_data_dir() / "isolated" / key
        if self.store.resolve().is_relative_to(home.root):
            raise FileError("ISOLATION_UNSAFE_PATH", "应用数据目录必须位于原项目之外，才能创建独立任务副本")
        self.workspaces = {}

    @staticmethod
    def _link(path):
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    def directory(self, task_id):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise FileError("INVALID_TASK_ID", "任务 ID 无效")
        target = self.store / task_id
        # 只使用服务自行构造的路径，不接受任务记录中的任意绝对路径。
        for path in (self.store.parent.parent, self.store.parent, self.store, target):
            if path.exists() and self._link(path):
                raise FileError("ISOLATION_UNSAFE_PATH", "任务副本存储目录包含链接或重解析点")
        if not target.resolve().is_relative_to(self.store.resolve()):
            raise FileError("ISOLATION_UNSAFE_PATH", "任务副本目录越界")
        return target

    def scan(self, workspace):
        files, used, visited, omitted = {}, 0, 0, 0
        for directory, dirs, names in os.walk(workspace.root, followlinks=False):
            base = Path(directory)
            kept = []
            for name in sorted(dirs):
                path = base / name
                rel = path.relative_to(workspace.root).as_posix()
                visited += 1
                if (name in EXCLUDED_DIR_NAMES or self._link(path) or workspace.policy.blocked(rel)):
                    omitted += 1
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                visited += 1
                path = base / name
                rel = path.relative_to(workspace.root).as_posix()
                if self._link(path) or workspace.policy.blocked(rel) or path.suffix.lower() in self.BUILD_SUFFIXES:
                    omitted += 1
                    continue
                if len(rel.encode("utf-8")) > 4000:
                    raise FileError("ISOLATION_LIMIT", "文件相对路径超过 4000 UTF-8 字节，无法创建任务副本")
                resolved = workspace.resolve_existing(rel).absolute
                if not resolved.is_file():
                    raise FileError("ISOLATION_UNSAFE_PATH", "副本只支持普通文件")
                size = resolved.stat().st_size
                if size > self.MAX_FILE_BYTES or used + size > self.MAX_BYTES or len(files) >= self.MAX_FILES:
                    raise FileError("ISOLATION_LIMIT", "任务副本超过 5000 文件、合计 64 MiB 或单文件 8 MiB 上限；请拆分项目")
                with resolved.open("rb") as handle:
                    data = handle.read(min(self.MAX_FILE_BYTES, self.MAX_BYTES - used) + 1)
                if len(data) > self.MAX_FILE_BYTES or used + len(data) > self.MAX_BYTES:
                    raise FileError("ISOLATION_LIMIT", "文件在复制期间增长，已停止创建副本")
                files[rel] = (data, stat.S_IMODE(resolved.stat().st_mode))
                used += len(data)
            if visited > 20_000:
                raise FileError("ISOLATION_LIMIT", "目录项超过 20000，请拆分项目")
        return files, omitted

    def create(self, task_id, *, quota_mb=2048):
        directory = self.directory(task_id)
        if directory.exists():
            raise FileError("ISOLATION_EXISTS", "任务副本目录已存在，未覆盖")
        directory.mkdir(parents=True)
        try:
            files, omitted = self.scan(self.home)
            from .maintenance import check_quota
            check_quota(self.home, quota_mb, additional=sum(len(data) * 2 + 500 for data, _mode in files.values()))
            baseline = {}
            for index, (path, (data, mode)) in enumerate(files.items()):
                atomic_write_bytes(directory / "base" / str(index), data, mode=0o600)
                atomic_write_bytes(directory / "work" / path, data, mode=mode)
                baseline[path] = {"revision": bytes_revision(data), "snapshot": str(index), "mode": mode}
            (directory / "work").mkdir(exist_ok=True)
            manifest = {"version": 1, "task_id": task_id, "source": str(self.home.root), "files": baseline,
                        "omitted_entries": omitted}
            encoded = json.dumps(manifest, ensure_ascii=False).encode()
            if len(encoded) > 4_000_000:
                raise FileError("ISOLATION_LIMIT", "任务基线索引超过 4 MB，请拆分项目")
            atomic_write_bytes(directory / "manifest.json", encoded, mode=0o600)
            return {"mode": "copy", "state": "working", "file_count": len(files), "omitted_entries": omitted,
                    "os_sandbox": False, "work_directory": str(directory / "work")}
        except BaseException:
            # target 已核对且为本次新建的 UUID 目录，尚未交给任务执行。
            checked = self.directory(task_id)
            if checked == directory and checked.resolve().is_relative_to(self.store.resolve()):
                shutil.rmtree(checked)
            raise

    def workspace(self, task_id):
        with self.home._process_lock:
            if self.home.closed:
                raise FileError("SERVICE_STOPPING", "服务已停止")
            if task_id not in self.workspaces:
                directory = self.directory(task_id)
                root = directory / "work"
                if not root.is_dir() or self._link(root):
                    raise FileError("ISOLATION_MISSING", "任务副本不存在或路径不安全；不会回退到原目录执行")
                workspace = Workspace(root, policy=copy.deepcopy(self.home.policy))
                workspace.run_id = self.home.run_id
                workspace._tasks = self.home._tasks
                workspace._isolation_home = self.home
                self.workspaces[task_id] = workspace
            return self.workspaces[task_id]

    def _manifest(self, task_id):
        directory = self.directory(task_id)
        path = directory / "manifest.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            raise FileError("ISOLATION_CORRUPT", "任务基线记录不存在或无效")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise FileError("ISOLATION_CORRUPT", "任务基线索引无法解析") from None
        if not isinstance(manifest, dict) or manifest.get("source") != str(self.home.root) or manifest.get("task_id") != task_id:
            raise FileError("ISOLATION_CORRUPT", "任务副本与项目不匹配")
        files = manifest.get("files")
        if not isinstance(files, dict) or len(files) > self.MAX_FILES:
            raise FileError("ISOLATION_CORRUPT", "任务基线文件索引无效")
        for name, item in files.items():
            if (not isinstance(name, str) or not isinstance(item, dict)
                    or re.fullmatch(r"[0-9]+", str(item.get("snapshot", ""))) is None
                    or re.fullmatch(r"[0-9a-f]{64}", str(item.get("revision", ""))) is None
                    or not isinstance(item.get("mode"), int)):
                raise FileError("ISOLATION_CORRUPT", "任务基线条目无效")
        return directory, manifest

    def prepare(self, task_id):
        directory, manifest = self._manifest(task_id)
        workspace = self.workspace(task_id)
        current, omitted = self.scan(workspace)
        edits, conflicts, rows, diff = [], [], [], []
        for path in sorted(set(current) | set(manifest["files"])):
            item = manifest["files"].get(path)
            before = None
            if item:
                target = workspace.root / path
                workspace.resolve_path(path)
                for parent in [target, *target.parents]:
                    if parent == workspace.root:
                        break
                    if os.path.lexists(parent) and self._link(parent):
                        raise FileError("ISOLATION_UNSAFE_PATH", f"基线文件路径包含链接：{path}")
                # 基线文件被换成链接或被忽略，拒绝把它当成普通删除。
                if target.exists() and self._link(target):
                    raise FileError("ISOLATION_UNSAFE_PATH", f"基线文件已变成链接：{path}")
                snapshot = directory / "base" / item["snapshot"]
                if (self._link(directory / "base") or not snapshot.resolve().is_relative_to((directory / "base").resolve())
                        or self._link(snapshot)):
                    raise FileError("ISOLATION_CORRUPT", "基线快照路径无效")
                before = snapshot.read_bytes()
                if bytes_revision(before) != item["revision"]:
                    raise FileError("ISOLATION_CORRUPT", "基线快照校验失败")
            after, mode = current.get(path, (None, item["mode"] if item else None))
            if before == after:
                continue
            source = self.home.resolve_for_write(path).absolute
            if source.exists():
                with source.open("rb") as handle:
                    actual = handle.read(self.MAX_FILE_BYTES + 1)
                if len(actual) > self.MAX_FILE_BYTES:
                    raise FileError("ISOLATION_SOURCE_CHANGED", f"原文件已超过副本大小限制，未覆盖：{path}")
            else:
                actual = None
            if actual is not None:
                mode = stat.S_IMODE(source.stat().st_mode)
            if actual != before:
                conflicts.append(path)
            edits.append(PreparedEdit(path, before, after, mode))
            row = {"path": path, "revision_before": bytes_revision(before) if before is not None else None,
                   "revision_after": bytes_revision(after) if after is not None else None,
                   "source_revision": bytes_revision(actual) if actual is not None else None,
                   "action": "create" if before is None else "delete" if after is None else "update"}
            rows.append(row)
            if len(rows) > 200:
                raise FileError("ISOLATION_CHANGE_LIMIT", "单次合入最多 200 个文件，请拆分任务")
            if sum(map(len, diff)) < 40_000:
                if b"\x00" in (before or b"")[:8192] or b"\x00" in (after or b"")[:8192]:
                    text = f"Binary file: {path}\n"
                else:
                    old = (before or b"")[:50_000].decode("utf-8", "replace").splitlines(True)[:500]
                    new = (after or b"")[:50_000].decode("utf-8", "replace").splitlines(True)[:500]
                    text = "".join(difflib.unified_diff(old, new, fromfile="a/" + path, tofile="b/" + path))
                    if len(before or b"") > 50_000 or len(after or b"") > 50_000 or len(old) == 500 or len(new) == 500:
                        text += "\n[文件 diff 为前部摘录；实际合入使用完整字节版本]\n"
                diff.append(text[:max(0, 40_000 - sum(map(len, diff)))])
        revision = bytes_revision(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode())
        return edits, {"changes": rows, "count": len(rows), "conflicts": conflicts, "diff_revision": revision,
                       "diff": "".join(diff), "diff_truncated": sum(map(len, diff)) >= 40_000,
                       "omitted_entries": omitted, "os_sandbox": False}

    def tree_revision(self, task_id):
        files, _omitted = self.scan(self.workspace(task_id))
        state = [(path, bytes_revision(data), mode) for path, (data, mode) in sorted(files.items())]
        return bytes_revision(json.dumps(state, ensure_ascii=False).encode())

    def shutdown(self):
        for workspace in list(self.workspaces.values()):
            with workspace._process_lock:
                workspace.closed = True
                manager, services = workspace._processes, workspace._language_services
            if manager is not None:
                manager.shutdown()
            if services is not None:
                services.close()


def apply_scope(workspace, task_id):
    return ACTIVE_TASK.set((str(workspace.root), task_id, 0))
