"""可选的本地 LSP stdio 客户端，命令仅来自启动配置。

只提供 UTF-16 协议位置。服务端主动 applyEdit 被拒绝，所有写入经过编辑引擎。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
from urllib.parse import urlparse
from urllib.request import url2pathname

from .files import FileError
from .processes import _WindowsJob


LANGUAGES = {".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
             ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
             ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".vue": "vue", ".svelte": "svelte"}


def file_path(workspace, uri):
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
        raise FileError("PATH_ESCAPE", "语言服务返回了非本地文件 URI")
    try:
        path = Path(url2pathname(parsed.path)).resolve()
        relative = path.relative_to(workspace.root).as_posix()
        return workspace.resolve_existing(relative)
    except (ValueError, OSError) as exc:
        raise FileError("PATH_ESCAPE", "语言服务返回了工作区以外的路径") from exc


def utf16_index(line: str, units: int) -> int:
    if type(units) is not int or units < 0:
        raise FileError("INVALID_RANGE", "UTF-16 列位置无效")
    count = 0
    for index, char in enumerate(line):
        if count == units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
        if count > units:
            raise FileError("INVALID_RANGE", "UTF-16 列位置落在字符中间")
    if count != units:
        raise FileError("INVALID_RANGE", "UTF-16 列位置超出当前行")
    return len(line)


def offset(text: str, position: dict, *, allow_eof=False) -> int:
    lines = text.split("\n")
    line = position.get("line")
    # pylsp 的整文件替换会在无末尾换行时返回后一行的 0 列。
    # 只对范围终点接受这一种 EOF 表示，起点及更远位置仍拒绝。
    if allow_eof and type(line) is int and line == len(lines) and position.get("character") == 0:
        return len(text)
    if type(line) is not int or not 0 <= line < len(lines):
        raise FileError("INVALID_RANGE", "语言服务行号越界")
    column = utf16_index(lines[line], position.get("character", -1))
    return sum(len(value) + 1 for value in lines[:line]) + column


def position(text: str, line: int, column: int) -> dict:
    lines = text.split("\n")
    if not 1 <= line <= len(lines) or not 1 <= column <= len(lines[line - 1]) + 1:
        raise FileError("INVALID_RANGE", "行号/列号越界；工具使用从 1 开始的 Unicode 字符列")
    return {"line": line - 1, "character": len(lines[line - 1][:column - 1].encode("utf-16-le")) // 2}


class Session:
    def __init__(self, workspace, language, argv):
        self.workspace, self.language = workspace, language
        self.lock, self.write_lock, self.pending_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self.ready, self.pending, self.documents, self.capabilities = threading.Event(), {}, {}, {}
        self.next_id, self.closed, self.error = 0, False, None
        self.stderr_tail = bytearray()
        self.windows_job = None
        self.configuration = ({"java": {"import": {"generatesMetadataFilesAtProjectRoot": False},
            "autobuild": {"enabled": False}, "maven": {"downloadSources": False, "updateSnapshots": False},
            "eclipse": {"downloadSources": False}}} if language == "java" else {})
        if language == "java" and Path(argv[0]).stem.lower() == "java":
            argv = [argv[0], "-Djava.import.generatesMetadataFilesAtProjectRoot=false",
                    *(part for part in argv[1:] if not part.startswith("-Djava.import.generatesMetadataFilesAtProjectRoot="))]
        executable = shutil.which(argv[0]) or argv[0]
        launch, use_shell = [executable, *argv[1:]], False
        if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
            launch, use_shell = subprocess.list2cmdline(launch), True
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                   "cwd": str(workspace.root), "bufsize": 0, "shell": use_shell}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            options["start_new_session"] = True
        try:
            self.process = subprocess.Popen(launch, **options)
        except OSError as exc:
            raise FileError("LSP_START_FAILED", f"无法启动 {language} 语言服务：{exc}") from exc
        if os.name == "nt":
            try:
                self.windows_job = _WindowsJob(self.process)
            except OSError:
                pass
        self.reader = threading.Thread(target=self._read, daemon=True, name=f"lsp-{language}-reader")
        self.stderr_reader = threading.Thread(target=self._stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _stderr(self):
        try:
            while data := self.process.stderr.read(4096):
                self.stderr_tail.extend(data)
                del self.stderr_tail[:-8192]
        except (OSError, ValueError):
            pass

    def _send(self, message):
        raw = json.dumps(message, ensure_ascii=True).encode()
        with self.write_lock:
            if self.closed or self.process.poll() is not None:
                raise FileError("LSP_CLOSED", "语言服务已退出")
            try:
                packet = f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw
                sent = 0
                while sent < len(packet):
                    count = self.process.stdin.write(memoryview(packet)[sent:])
                    if not count:
                        raise BrokenPipeError("语言服务输入已关闭")
                    sent += count
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise FileError("LSP_CLOSED", str(exc)) from exc

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _fail(self, message):
        self.error = message
        with self.pending_lock:
            pending = list(self.pending.values())
        for mailbox in pending:
            try:
                mailbox.put_nowait({"error": {"message": message}})
            except queue.Full:
                pass

    def _reply(self, message):
        method, params = message["method"], message.get("params") or {}
        result = None
        if method == "workspace/configuration":
            result = []
            for item in params.get("items", []):
                value = self.configuration
                for part in (item.get("section") or "").split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                result.append(value)
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.workspace.root.as_uri(), "name": self.workspace.root.name}]
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "通过 rename_symbol/edit_files 的事务提交接口修改文件"}
        elif method not in ("client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create", "window/showMessageRequest"):
            self._send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "客户端不支持此请求"}})
            return
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _read(self):
        try:
            while True:
                headers, header_size = {}, 0
                while True:
                    line = self.process.stdout.readline(8193)
                    if not line:
                        raise EOFError("语言服务输出已关闭")
                    header_size += len(line)
                    if header_size > 8192:
                        raise ValueError("语言服务协议头过大")
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("语言服务消息大小无效")
                data = bytearray()
                while len(data) < length:
                    chunk = self.process.stdout.read(length - len(data))
                    if not chunk:
                        raise EOFError("语言服务消息不完整")
                    data.extend(chunk)
                message = json.loads(data)
                if not isinstance(message, dict):
                    raise ValueError("语言服务消息必须是对象")
                if "method" in message:
                    if "id" in message:
                        self._reply(message)
                else:
                    with self.pending_lock:
                        mailbox = self.pending.get(message.get("id"))
                    if mailbox:
                        mailbox.put_nowait(message)
        except Exception as exc:
            self._fail(str(exc))

    def request(self, method, params, *, timeout=10):
        with self.pending_lock:
            self.next_id += 1
            identity, mailbox = self.next_id, queue.Queue(maxsize=1)
            self.pending[identity] = mailbox
        try:
            self._send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
            try:
                response = mailbox.get(timeout=timeout)
            except queue.Empty:
                self.notify("$/cancelRequest", {"id": identity})
                raise FileError("LSP_TIMEOUT", "语言服务响应超时，索引可能尚未就绪，请稍后重试") from None
            if "error" in response:
                raise FileError("LSP_ERROR", str(response["error"].get("message", "语言服务错误")),
                                {"stderr": bytes(self.stderr_tail).decode("utf-8", "replace")})
            return response.get("result")
        finally:
            with self.pending_lock:
                self.pending.pop(identity, None)

    def _initialize(self):
        try:
            response = self.request("initialize", {"processId": os.getpid(), "rootUri": self.workspace.root.as_uri(),
                "initializationOptions": {"settings": self.configuration},
                "workspaceFolders": [{"uri": self.workspace.root.as_uri(), "name": self.workspace.root.name}],
                "capabilities": {"general": {"positionEncodings": ["utf-16"]},
                    "workspace": {"configuration": True, "workspaceFolders": True, "applyEdit": False,
                                  "workspaceEdit": {"documentChanges": True, "resourceOperations": []}},
                    "textDocument": {"synchronization": {"dynamicRegistration": False, "didSave": False},
                                     "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                     "references": {"dynamicRegistration": False}, "rename": {"dynamicRegistration": False}}}}, timeout=30)
            self.capabilities = response.get("capabilities", {})
            if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
                raise FileError("LSP_ENCODING_UNSUPPORTED", "当前客户端只支持 UTF-16 LSP 位置编码")
            self.notify("initialized", {})
            if self.configuration:
                self.notify("workspace/didChangeConfiguration", {"settings": self.configuration})
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready.set()

    def await_ready(self):
        if not self.ready.wait(timeout=8):
            raise FileError("LSP_STARTING", "语言服务正在初始化，请稍后重试")
        if self.error:
            raise FileError("LSP_INITIALIZE_FAILED", self.error, {"stderr": bytes(self.stderr_tail).decode("utf-8", "replace")})

    def sync(self, path, content):
        uri = path.as_uri()
        previous = self.documents.get(uri)
        if previous is None:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": self.language, "version": 1, "text": content.text}})
            self.documents[uri] = (content.revision, 1)
        elif previous[0] != content.revision:
            version = previous[1] + 1
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": content.text}]})
            self.documents[uri] = (content.revision, version)
        if len(self.documents) > 100:
            oldest = next(key for key in self.documents if key != uri)
            self.notify("textDocument/didClose", {"textDocument": {"uri": oldest}})
            self.documents.pop(oldest)

    def close(self):
        if self.closed:
            return
        if self.ready.is_set() and not self.error and self.process.poll() is None:
            try:
                self.request("shutdown", {}, timeout=1)
                self.notify("exit", {})
                self.process.wait(timeout=1)
            except (FileError, subprocess.TimeoutExpired):
                pass
        self.closed = True
        if self.windows_job:
            self.windows_job.close()
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self._fail("语言服务已停止")
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class LanguageServices:
    def __init__(self, workspace, settings):
        self.workspace, self.settings = workspace, settings
        self.sessions, self.lock = {}, threading.RLock()
        self.closed = False

    def configured(self, path):
        return LANGUAGES.get(path.suffix.lower()) in self.settings.language_servers

    def get(self, path=None, *, language=None):
        language = language or LANGUAGES.get(path.suffix.lower())
        if self.settings.execution_mode == "native":
            raise FileError("SANDBOX_LSP_DISABLED", "当前原生沙箱模式不启动宿主语言服务；可用 Java/Go 声明导航、文本候选与原生沙箱构建检查")
        if not self.settings.allow_exec or self.settings.read_only:
            raise FileError("EXEC_DISABLED", "语言服务是本地进程，当前服务器已禁用执行")
        argv = self.settings.language_servers.get(language)
        if not argv:
            raise FileError("LSP_NOT_CONFIGURED", f"未配置 {language or '该语言'} 的服务，请通过 --language-server 或高级设置配置")
        with self.lock:
            if self.closed:
                raise FileError("SERVICE_STOPPING", "服务正在停止")
            session = self.sessions.get(language)
            if session is not None and session.process.poll() is not None:
                session.close()
                self.sessions.pop(language, None)
                session = None
            if session is None:
                from .config import app_data_dir
                from .files import bytes_revision
                cache = app_data_dir() / "language-cache" / bytes_revision(os.path.normcase(str(self.workspace.root)).encode())[:24]
                argv = [part.replace("{workspace_cache}", str(cache)) for part in argv]
                session = self.sessions[language] = Session(self.workspace, language, argv)
        session.await_ready()
        return session

    def close(self):
        with self.lock:
            self.closed = True
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()


def services(ctx):
    with ctx.workspace._process_lock:
        if ctx.workspace.closed:
            raise FileError("SERVICE_STOPPING", "服务正在停止，不能创建语言服务")
        if ctx.workspace._language_services is None:
            ctx.workspace._language_services = LanguageServices(ctx.workspace, ctx.settings)
        return ctx.workspace._language_services
