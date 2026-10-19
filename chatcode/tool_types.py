"""各工具模块共用的上下文、结果和错误类型。"""

from dataclasses import dataclass, field
import time
from typing import Any
from contextvars import ContextVar

ACTIVE_TASK = ContextVar("chatcode_active_task", default=None)

from .config import Settings
from .workspace import Workspace


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


@dataclass
class ToolResult:
    text: str
    structured: dict[str, Any]
    is_error: bool = False
    content: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    workspace: Workspace
    settings: Settings
    started_at: float = field(default_factory=time.time)
    workspace_id: str = ""
    workspace_token: str = ""
    registry: Any = None
    task_id: str = ""
    task_home: Any = None


def spec(description: str, handler, properties: dict, required=(), *, read_only=True, output=None) -> dict:
    return {"description": description, "handler": handler,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
            "outputSchema": output or {"type": "object"}, "read_only": read_only}
