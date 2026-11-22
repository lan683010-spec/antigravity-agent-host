"""Shared data models for agentctl."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
TaskStatus = Literal["pending", "running", "completed", "failed", "timeout", "cancelled"]
EventKind = Literal[
    "init",
    "thinking",
    "tool_call",
    "assistant_message",
    "result",
    "error",
    "raw",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRequest:
    agent: str
    prompt: str
    cwd: str
    sandbox: SandboxMode = "workspace-write"
    model: str | None = None
    timeout_sec: int = 600
    use_worktree: bool = False
    worktree_base: str | None = None
    output_schema: str | None = None
    extra_args: list[str] = field(default_factory=list)
    title: str | None = None
    dispatcher: str | None = None


@dataclass
class NormalizedEvent:
    kind: EventKind
    timestamp: str = field(default_factory=utc_now_iso)
    session_id: str | None = None
    text: str | None = None
    tool_name: str | None = None
    subtype: str | None = None
    is_error: bool = False
    usage: dict[str, Any] | None = None
    model: str | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CommandSpec:
    argv: list[str]
    stdin_text: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class TurnRecord:
    turn_index: int
    prompt: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    status: TaskStatus = "running"
    duration_ms: int | None = None
    final_response: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    command_argv: list[str] | None = None


@dataclass
class TaskMeta:
    task_id: str
    agent: str
    status: TaskStatus
    cwd: str
    effective_cwd: str
    sandbox: SandboxMode
    prompt: str
    created_at: str
    updated_at: str
    pid: int | None = None
    session_id: str | None = None
    model: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    final_response: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    title: str | None = None
    dispatcher: str | None = None
    command_argv: list[str] | None = None
    command_summary_zh: str | None = None
    result_summary_zh: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskMeta":
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class HealthReport:
    agent: str
    available: bool
    authenticated: bool | None
    version: str | None = None
    executable: str | None = None
    auth_detail: str | None = None
    error: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
