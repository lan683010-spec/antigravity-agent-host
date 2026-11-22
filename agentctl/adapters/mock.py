"""Built-in mock agent adapter for offline testing without burning tokens."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest

_MOCK_WORKER = r"""
import json, sys, uuid

mode = sys.argv[1]
session_id = sys.argv[2] if mode == "resume" else f"mock-{uuid.uuid4().hex[:8]}"
prompt = sys.stdin.read().strip()

print(json.dumps({"type": "init", "session_id": session_id, "model": "mock-1"}), flush=True)
print(json.dumps({"type": "tool_call", "session_id": session_id, "tool": "mock_echo"}), flush=True)
reply = f"[MOCK:{mode}] {prompt}"
print(json.dumps({"type": "assistant", "session_id": session_id, "text": reply}), flush=True)
print(json.dumps({"type": "result", "session_id": session_id, "result": reply, "usage": {"inputTokens": 10, "outputTokens": 5}}), flush=True)
"""


class MockAdapter(BaseAgentAdapter):
    name = "mock"
    description = "Offline mock agent for testing orchestrator pipelines"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        return [sys.executable, "-c", _MOCK_WORKER], {}

    def build_run_command(
        self,
        request: RunRequest,
        *,
        effective_cwd: str,
        final_message_path: Path,
    ) -> CommandSpec:
        exe, env = self.resolve_executable()
        return CommandSpec(argv=[*exe, "run"], stdin_text=request.prompt, env_overrides=env)

    def build_resume_command(
        self,
        session_id: str,
        prompt: str,
        *,
        effective_cwd: str,
        sandbox: str,
        model: str | None,
        final_message_path: Path,
        extra_args: list[str] | None = None,
    ) -> CommandSpec:
        exe, env = self.resolve_executable()
        return CommandSpec(
            argv=[*exe, "resume", session_id],
            stdin_text=prompt,
            env_overrides=env,
        )

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        data = self.safe_json_loads(line)
        if not data:
            return None
        t = data.get("type")
        sid = data.get("session_id")
        if t == "init":
            return NormalizedEvent(kind="init", session_id=sid, model=data.get("model"), raw=data)
        if t == "tool_call":
            return NormalizedEvent(kind="tool_call", session_id=sid, tool_name=data.get("tool"), raw=data)
        if t == "assistant":
            return NormalizedEvent(kind="assistant_message", session_id=sid, text=data.get("text"), raw=data)
        if t == "result":
            return NormalizedEvent(
                kind="result",
                session_id=sid,
                text=data.get("result"),
                usage=data.get("usage"),
                raw=data,
            )
        return NormalizedEvent(kind="raw", session_id=sid, raw=data)

    def health_check(self) -> HealthReport:
        return HealthReport(
            agent=self.name,
            available=True,
            authenticated=True,
            version="mock-1.0",
            executable=sys.executable,
            auth_detail="Offline mock adapter ready",
        )
