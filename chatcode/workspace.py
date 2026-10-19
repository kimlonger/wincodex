"""工作区边界：所有文件操作都被限制在一棵目录树内。

这是整个项目唯一的安全边界核心。模型可能在对话里被诱导去读 ../.ssh/id_rsa 或
/etc/passwd，所以每个路径都必须先过这里。
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import EXCLUDED_DIR_NAMES
from .locking import WorkspaceLock
from .access import FilePolicy


_LOCKS: dict[str, WorkspaceLock] = {}
_LOCKS_GUARD = threading.Lock()


class PathRejected(Exception):
    """路径越界或被显式拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedPath:
    absolute: Path
    relative: str


class Workspace:
    """把用户给的相对路径解析成树内绝对路径。"""

    def __init__(self, root: Path, *, allow_sensitive_files: bool = False, policy=None) -> None:
        self.root = Path(root).resolve()
        self.policy = policy if policy is not None else FilePolicy(self.root, allow_sensitive=allow_sensitive_files)
        self._root_str = str(self.root)
        with _LOCKS_GUARD:
            self.write_lock = _LOCKS.setdefault(os.path.normcase(self._root_str), WorkspaceLock(self.root))
        self._editor = None
        self._processes = None
        self._language_services = None
        self._process_lock = threading.Lock()
        self.closed = False
        self.run_id = uuid.uuid4().hex
        self._tasks = None
        self._isolated = None
        self._index = None

    @property
    def index(self):
        with self._process_lock:
            if self._index is None:
                from .persistent_index import ProjectIndex
                self._index = ProjectIndex(self)
            return self._index

    @property
    def isolated(self):
        # 初始化不持有 _process_lock，副本创建随后会使用该锁。
        with self.write_lock:
            if self._isolated is None:
                from .isolation import IsolatedTasks
                self._isolated = IsolatedTasks(self)
            return self._isolated

    @property
    def tasks(self):
        with self._process_lock:
            if self._tasks is None:
                if self.closed:
                    raise PathRejected("SERVICE_STOPPING", "服务已停止")
                from .tasks import TaskStore
                self._tasks = TaskStore(self)
            return self._tasks

    @property
    def editor(self):
        with self.write_lock:
            if self._editor is None:
                from .editing import EditEngine

                self._editor = EditEngine(self)
            return self._editor

    @property
    def processes(self):
        with self._process_lock:
            if self.closed:
                raise PathRejected("SERVICE_STOPPING", "服务正在停止，不能创建新的进程管理器")
            if self._processes is None:
                from .processes import ProcessManager
                self._processes = ProcessManager(self)
            return self._processes

    # ---- 路径校验 ----

    def _normalize(self, raw: str, *, must_exist: bool = True) -> ResolvedPath:
        if raw is None:
            raw = "."
        raw = str(raw).strip()
        if not raw:
            raw = "."
        # NUL 字节在多数文件系统上会截断字符串，是经典绕过手法
        if "\x00" in raw:
            raise PathRejected("PATH_INVALID", "路径包含 NUL 字节")
        # 绝对路径一律拒绝：工具只认工作区内的相对路径
        if raw.startswith("/") or raw.startswith("\\"):
            raise PathRejected("PATH_ABSOLUTE", f"只接受工作区内的相对路径，收到：{raw!r}")
        if len(raw) > 1 and raw[1] == ":":
            raise PathRejected("PATH_ABSOLUTE", f"不接受盘符绝对路径：{raw!r}")
        if os.name == "nt" and ":" in raw:
            raise PathRejected("PATH_INVALID", "不接受 Windows 备用数据流路径")

        candidate = (self.root / raw).resolve()
        if not self._is_inside(candidate):
            raise PathRejected("PATH_ESCAPE", f"路径跳出工作区：{raw!r}")
        try:
            rel = candidate.relative_to(self.root).as_posix()
        except ValueError:
            raise PathRejected("PATH_ESCAPE", f"路径跳出工作区：{raw!r}") from None
        lexical = Path(os.path.normpath(str(self.root / raw))).relative_to(self.root).as_posix()
        if self.policy.blocked(rel) or self.policy.blocked(lexical):
            raise PathRejected("ACCESS_DENIED", f"文件访问策略拒绝此路径：{raw!r}")
        if must_exist and not candidate.exists():
            raise PathRejected("PATH_NOT_FOUND", f"路径不存在：{raw!r}")
        return ResolvedPath(absolute=candidate, relative=rel or ".")

    def _is_inside(self, path: Path) -> bool:
        """含符号链接检查：解析后的真实路径必须在树内。"""
        try:
            real = path.resolve()
        except OSError:
            return False
        return real.is_relative_to(self.root)

    # ---- 对外接口 ----

    def resolve_existing(self, raw: str = ".") -> ResolvedPath:
        return self._normalize(raw, must_exist=True)

    def resolve_path(self, raw: str) -> ResolvedPath:
        """校验可不存在的路径，用于 Git 查询已删除文件。"""
        return self._normalize(raw, must_exist=False)

    def resolve_for_write(self, raw: str) -> ResolvedPath:
        """写入目标允许不存在；父目录的真实路径必须落在本树内。"""
        resolved = self._normalize(raw, must_exist=False)
        if (self.root / raw).is_symlink():
            raise PathRejected("PATH_SYMLINK", f"拒绝写入符号链接：{raw!r}")
        parent = resolved.absolute.parent
        if not self._is_inside(parent):
            raise PathRejected("PATH_ESCAPE", f"写入目标跳出工作区：{raw!r}")
        # 符号链接写入会把内容写到树外，直接拒绝
        if resolved.absolute.is_symlink():
            raise PathRejected("PATH_SYMLINK", f"拒绝写入符号链接：{raw!r}")
        return resolved

    def is_ignored_dir(self, name: str) -> bool:
        return name in EXCLUDED_DIR_NAMES

    def iter_entries(self, rel_dir: str = ".", *, recursive: bool = True, max_entries: int = 2000):
        """遍历目录，跳过噪音目录。产出 (相对路径, 是否目录)。"""
        start = self.resolve_existing(rel_dir)
        base = start.absolute
        count = 0

        if recursive:
            walker = os.walk(base, topdown=True, followlinks=False)
            for dirpath, dirnames, filenames in walker:
                # 就地裁剪，避免走进 node_modules 这类大目录
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not self.is_ignored_dir(d) and self._safe_entry(Path(dirpath) / d)
                )
                for name in sorted(filenames):
                    if count >= max_entries:
                        return
                    full = Path(dirpath) / name
                    if not self._safe_entry(full):
                        continue
                    try:
                        rel = full.relative_to(self.root).as_posix()
                    except ValueError:
                        continue
                    yield rel, False
                    count += 1
                if dirpath != str(base):
                    if count >= max_entries:
                        return
                    full = Path(dirpath)
                    try:
                        rel = full.relative_to(self.root).as_posix()
                    except ValueError:
                        continue
                    yield rel, True
                    count += 1
        else:
            try:
                names = sorted(os.listdir(base))
            except OSError:
                return
            for name in names:
                if count >= max_entries:
                    return
                full = base / name
                if not self._safe_entry(full):
                    continue
                if full.is_dir() and self.is_ignored_dir(name):
                    continue
                yield full.relative_to(self.root).as_posix(), full.is_dir()
                count += 1

    def _safe_entry(self, path: Path) -> bool:
        try:
            # Path.is_junction 需要 Python 3.12；3.11 使用 Windows reparse tag。
            reparse_tag = getattr(path.lstat(), "st_reparse_tag", 0) if os.name == "nt" else 0
            return (
                not path.is_symlink()
                and reparse_tag not in (0xA0000003, 0xA000000C)
                and not getattr(path, "is_junction", lambda: False)()
                and self._is_inside(path)
                and not self.policy.blocked(path.relative_to(self.root).as_posix())
            )
        except OSError:
            return False
