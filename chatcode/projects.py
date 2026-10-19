"""连接级项目注册表；只能由本机登记，远程调用显式绑定项目。"""

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import secrets
import threading

from .tool_types import ToolContext, ToolError
from .workspace import Workspace


@dataclass
class Project:
    workspace_id: str
    workspace_token: str
    workspace: Workspace
    settings: object
    recovery: list

    def info(self):
        return {"workspace_id": self.workspace_id, "workspace_token": self.workspace_token,
                "name": self.workspace.root.name, "path": str(self.workspace.root),
                "read_only": self.settings.read_only, "exec_enabled": self.settings.allow_exec}


class ProjectRegistry:
    def __init__(self, settings, initial):
        self.settings = settings
        self.lock = threading.RLock()
        self.entries = {}
        self.active_id = ""
        self.require_context = False
        self.closed = False
        self.shutdown_errors = []
        from .desktop import DesktopBridge
        self.desktop = DesktopBridge(settings)
        self._register(initial, settings)

    @property
    def active(self):
        with self.lock:
            return self.entries[self.active_id]

    def _register(self, workspace, settings):
        identity = hashlib.sha256(os.path.normcase(str(workspace.root)).encode("utf-8")).hexdigest()[:24]
        if identity in self.entries:
            return self.entries[identity]
        if len(self.entries) >= 20:
            raise ValueError("本次连接最多登记 20 个项目，请停止服务后重新选择项目")
        recovery = workspace.editor.recover()
        project = Project(identity, secrets.token_urlsafe(24), workspace, settings, recovery)
        self.entries[identity] = project
        if not self.active_id:
            self.active_id = identity
        if len(self.entries) > 1:
            self.require_context = True
        return project

    def register(self, path, *, settings=None, activate=False):
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError(f"项目目录不存在：{root}")
        with self.lock:
            if self.closed:
                raise ValueError("服务正在停止")
            candidate = settings or self.settings
            # 连接级限制不能由项目配置放宽，连接地址和凭据始终来自原服务。
            local = replace(self.settings, workspace=root, projects=[],
                language_servers=dict(candidate.language_servers),
                read_only=self.settings.read_only or candidate.read_only,
                allow_exec=self.settings.allow_exec and candidate.allow_exec and not candidate.read_only,
                allow_sensitive_files=self.settings.allow_sensitive_files and candidate.allow_sensitive_files)
            identity = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:24]
            project = self.entries.get(identity)
            if project is None:
                project = self._register(Workspace(root, allow_sensitive_files=local.allow_sensitive_files), local)
            if activate:
                self.active_id = project.workspace_id
            return project

    def bind(self, name, args, started_at):
        if not isinstance(args, dict):
            raise ToolError("INVALID_ARGUMENT", "arguments 必须是对象")
        identity, token = args.get("workspace_id"), args.get("workspace_token")
        with self.lock:
            if self.closed:
                raise ToolError("SERVICE_STOPPING", "服务正在停止")
            if identity is not None or token is not None:
                project = self.entries.get(identity) if isinstance(identity, str) else None
                if project is None or not isinstance(token, str) or not secrets.compare_digest(token.encode("utf-8"), project.workspace_token.encode("utf-8")):
                    raise ToolError("WORKSPACE_CONTEXT_INVALID", "项目身份无效或已过期，请读取 list_workspaces 并核对目标项目")
            else:
                if self.require_context and name not in ("server_info", "list_workspaces"):
                    raise ToolError("WORKSPACE_REQUIRED", "已登记多个项目，必须携带 workspace_id 和 workspace_token；先核对目标，不要把旧任务转到当前项目")
                project = self.entries[self.active_id]
            return ToolContext(project.workspace, project.settings, started_at,
                workspace_id=project.workspace_id, workspace_token=project.workspace_token, registry=self)

    def describe(self):
        with self.lock:
            return {"active_workspace_id": self.active_id, "context_required": self.require_context,
                    "workspaces": [dict(entry.info(), active=key == self.active_id) for key, entry in self.entries.items()]}

    def shutdown(self):
        with self.lock:
            self.closed = True
            entries = list(self.entries.values())
        self.desktop.close()
        for project in entries:
            workspace = project.workspace
            with workspace._process_lock:
                workspace.closed = True
                processes = workspace._processes
                services = workspace._language_services
            if processes is not None:
                processes.shutdown()
            if services is not None:
                services.close()
            if workspace._isolated is not None:
                workspace._isolated.shutdown()
            if workspace._tasks is not None:
                try:
                    workspace._tasks.interrupt()
                except Exception as exc:
                    self.shutdown_errors.append(f"项目 {workspace.root.name} 的任务中断状态保存失败：{exc}")
