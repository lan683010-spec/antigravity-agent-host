"""Template for adding future CLI coding agents (e.g., Claude Code, Gemini CLI, OpenCode).

To integrate a new agent:
1. Copy this file to `agentctl/adapters/<agent_name>.py`.
2. Implement `resolve_executable`, `build_run_command`, `build_resume_command`,
   `parse_stdout_line`, and `health_check`.
3. Register the class in `agentctl/adapters/__init__.py`.

Example headless flags for popular future agents:
- Claude Code: `claude -p --output-format stream-json --verbose [--resume <id>]`
- Gemini CLI:  `gemini -p --output-format stream-json [--resume <id>]`
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


class ExampleAgentAdapter(BaseAgentAdapter):
    name = "example"
    description = "Template adapter (not registered by default)"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        found = shutil.which("example-agent")
        if not found:
            raise FileNotFoundError("example-agent CLI not found in PATH")
        return [found], {}

    def build_run_command(
        self,
        request: RunRequest,
        *,
        effective_cwd: str,
        final_message_path: Path,
    ) -> CommandSpec:
        exe, env = self.resolve_executable()
        argv = [*exe, "-p", "--output-format", "stream-json", request.prompt]
        return CommandSpec(argv=argv, env_overrides=env)

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
        argv = [*exe, "-p", "--output-format", "stream-json", "--resume", session_id, prompt]
        return CommandSpec(argv=argv, env_overrides=env)

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        data = self.safe_json_loads(line)
        if not data:
            return NormalizedEvent(kind="raw", text=line.strip())
        return NormalizedEvent(kind="raw", raw=data)

    def health_check(self) -> HealthReport:
        return HealthReport(agent=self.name, available=False, authenticated=False)
