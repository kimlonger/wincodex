"""有界的持久符号索引；仅缓存定义元数据，查询时重新校验文件版本和访问策略。"""

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import stat
from contextlib import contextmanager

from .code_index import outline
from .config import app_data_dir
from .files import FileError, bytes_revision, read_text_file
from .workspace import PathRejected


class ProjectIndex:
    MAX_ENTRIES = 200_000
    MAX_FILES = 100_000
    MAX_DB_BYTES = 128 * 1024 * 1024

    def __init__(self, workspace):
        self.workspace = workspace
        key = bytes_revision(os.path.normcase(str(workspace.root)).encode())[:24]
        self.path = app_data_dir() / "indexes" / key / "symbols.sqlite3"
        self.lock = threading.RLock()
        self._scan = None
        self._pending = None
        self._seen = set()
        self._force = False
        self._visited = 0

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA max_page_count=32768")
        db.execute("PRAGMA journal_size_limit=1048576")
        db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,stamp TEXT,revision TEXT,symbols TEXT,error TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def stamp(path):
        info = path.stat()
        return f"{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}:{info.st_ino}"

    def _entries(self):
        # 未变化的文件仅 lstat；实际读取/返回前仍走 resolve_existing 和内容哈希。
        # 避免对每个已缓存文件反复解析完整祖先路径，使大项目热刷新退化。
        for directory, dirs, names in os.walk(self.workspace.root, followlinks=False):
            base = Path(directory)
            if base != self.workspace.root and not self.workspace._safe_entry(base):
                dirs[:] = []
                continue
            dirs[:] = sorted(name for name in dirs if not self.workspace.is_ignored_dir(name) and self.workspace._safe_entry(base / name))
            for name in sorted(names):
                path = base / name
                relative = path.relative_to(self.workspace.root).as_posix()
                if self.workspace.policy.blocked(relative):
                    continue
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    yield relative, path, f"{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_size}:{info.st_ino}"
                except OSError:
                    continue

    def refresh(self, *, max_files=500, max_seconds=5, force=False):
        from .lsp import LANGUAGES
        parse_seconds, changed, reused, pending, visited = 0, 0, 0, 0, 0
        errors, scan_complete = [], True
        with self.lock, self._db() as db:
            try:
                if self._scan is None or force:
                    self._scan, self._pending, self._seen, self._visited = iter(self._entries()), None, set(), 0
                    self._force = force
                seen = self._seen
                cached = {row[0]: row[1] for row in db.execute("SELECT path,stamp FROM files")}
                import itertools
                entries = itertools.chain([self._pending] if self._pending else [], self._scan)
                self._pending = None
                for relative, path, stamp in entries:
                    if visited >= 5000 or changed >= max_files or parse_seconds > max_seconds:
                        self._pending = (relative, path, stamp)
                        scan_complete = False
                        pending = 1
                        break
                    visited += 1
                    self._visited += 1
                    if self._visited > self.MAX_ENTRIES or len(seen) >= self.MAX_FILES:
                        scan_complete = False
                        self._scan = None
                        break
                    if Path(relative).suffix.lower() not in LANGUAGES:
                        continue
                    seen.add(relative)
                    try:
                        if not self._force and cached.get(relative) == stamp:
                            reused += 1
                            continue
                        revision, symbols, error = "", [], ""
                        parse_started = time.monotonic()
                        try:
                            path = self.workspace.resolve_existing(relative).absolute
                            content, symbols = outline(path)
                            revision = content.revision
                            if self.stamp(path) != stamp:
                                pending += 1
                                continue
                            if len(symbols) > 2000 or len(json.dumps(symbols).encode()) > 200_000:
                                symbols, error = [], "SYMBOL_LIMIT"
                        except FileError as exc:
                            error = exc.code
                        finally:
                            parse_seconds += time.monotonic() - parse_started
                        db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                                   (relative, stamp, revision, json.dumps(symbols, ensure_ascii=False), error))
                        changed += 1
                        if error and len(errors) < 20:
                            errors.append({"path": relative, "code": error})
                    except (FileError, PathRejected, OSError) as exc:
                        if len(errors) < 20:
                            errors.append({"path": relative, "code": getattr(exc, "code", "IO_ERROR")})
                # 未完整枚举时不把未扫描到的文件误当作已删除。
                removed = set(cached) - seen if scan_complete else set()
                if scan_complete:
                    self._scan = None
                for relative in removed:
                    db.execute("DELETE FROM files WHERE path=?", (relative,))
                result = {"updated": changed, "reused": reused, "pending": pending, "removed": len(removed),
                          "scan_complete": scan_complete, "complete": scan_complete and not pending,
                          "visited": visited, "errors": errors, "updated_at": time.time()}
                result["pending_is_lower_bound"] = not scan_complete
                db.execute("INSERT OR REPLACE INTO metadata VALUES('refresh',?)", (json.dumps(result),))
                return result
            except sqlite3.Error as exc:
                raise FileError("INDEX_STORAGE_ERROR", f"索引容量或存储错误：{exc}") from None

    def status(self):
        with self.lock, self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='refresh'").fetchone()
            return {"files": db.execute("SELECT count(*) FROM files").fetchone()[0],
                    "bytes": self.path.stat().st_size, "max_bytes": self.MAX_DB_BYTES,
                    "max_files": self.MAX_FILES, "last_refresh": json.loads(row[0]) if row else None,
                    "persistent": True, "stores_source_bodies": False}

    def search(self, query, *, base=".", offset=0, limit=100, budget_chars=30_000):
        found, skipped, used, matches = [], [], 0, 0
        with self.lock, self._db() as db:
            for relative, stamp, revision, raw, error in db.execute("SELECT * FROM files ORDER BY path"):
                if base != "." and relative != base and not relative.startswith(base.rstrip("/") + "/"):
                    continue
                if self.workspace.policy.blocked(relative):
                    continue
                try:
                    if error:
                        if len(skipped) < 20:
                            skipped.append({"path": relative, "code": error})
                        continue
                    candidates = [item for item in json.loads(raw) if query.casefold() in item["qualified_name"].casefold()]
                    if not candidates:
                        continue
                    path = self.workspace.resolve_existing(relative).absolute
                    if self.stamp(path) != stamp:
                        raise FileError("INDEX_STALE", "文件已变化，需再次刷新索引")
                    # stat 只是增量提示；实际返回前使用内容哈希验证。
                    if read_text_file(path, max_bytes=2 * 1024 * 1024).revision != revision:
                        raise FileError("INDEX_STALE", "文件内容哈希已变化")
                    for item in candidates:
                        matches += 1
                        if matches <= offset:
                            continue
                        record = {"path": relative, "revision": revision, **item}
                        size = len(json.dumps(record, ensure_ascii=False))
                        if not found and size > budget_chars:
                            raise FileError("OUTPUT_BUDGET_TOO_SMALL", "单个符号超过本次字符预算；增加 budget_chars 或按文件读取", {"required_chars": size})
                        if len(found) >= limit or used + size > budget_chars:
                            return {"symbols": found, "count": len(found), "next_offset": offset + len(found),
                                    "truncated": True, "skipped": skipped, "backend": "persistent_index"}
                        found.append(record)
                        used += size
                except (FileError, PathRejected, OSError) as exc:
                    if getattr(exc, "code", "") == "OUTPUT_BUDGET_TOO_SMALL":
                        raise
                    if len(skipped) < 20:
                        skipped.append({"path": relative, "code": getattr(exc, "code", "IO_ERROR")})
        return {"symbols": found, "count": len(found), "next_offset": None, "truncated": False,
                "skipped": skipped, "backend": "persistent_index"}
