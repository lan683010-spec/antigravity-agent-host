"""Adapter for Google Gemini CLI (`gemini -p --output-format stream-json`)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


class GeminiAdapter(BaseAgentAdapter):
    name = "gemini"
    description = "Google Gemini CLI (`gemini -p --output-format stream-json`)"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        env_overrides: dict[str, str] = {}
        if os.name == "nt":
            found_cmd = shutil.which("gemini.cmd")
            if found_cmd:
                return [found_cmd], env_overrides

        found = shutil.which("gemini")
        if found:
            if os.name == "nt" and found.lower().endswith(".ps1"):
                sibling_cmd = Path(found).with_suffix(".cmd")
                if sibling_cmd.is_file():
                    return [str(sibling_cmd)], env_overrides
            return [found], env_overrides

        raise FileNotFoundError(
            "Gemini CLI (`gemini`) not found. "
            "Install via: npm.cmd install -g @google/gemini-cli"
        )

    def _sandbox_flags(self, sandbox: str) -> list[str]:
        if sandbox == "danger-full-access":
            return ["--yolo"]
        if sandbox == "workspace-write":
            return ["--approval-mode", "auto_edit"]
        return []

    def build_run_command(
        self,
        request: RunRequest,
        *,
        effective_cwd: str,
        final_message_path: Path,
    ) -> CommandSpec:
        exe, env_overrides = self.resolve_executable()
        argv = [
            *exe,
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
            *self._sandbox_flags(request.sandbox),
        ]
        if request.model:
            argv.extend(["-m", request.model])
        if request.extra_args:
            argv.extend(request.extra_args)
        return CommandSpec(argv=argv, stdin_text=None, env_overrides=env_overrides)

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
        exe, env_overrides = self.resolve_executable()
        argv = [
            *exe,
            "--resume",
            session_id,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            *self._sandbox_flags(sandbox),
        ]
        if model:
            argv.extend(["-m", model])
        if extra_args:
            argv.extend(extra_args)
        return CommandSpec(argv=argv, stdin_text=None, env_overrides=env_overrides)

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        data = self.safe_json_loads(stripped)
        if data is None:
            return NormalizedEvent(kind="raw", text=stripped)

        ev_type = str(data.get("type") or "")
        session_id = data.get("session_id") or data.get("sessionId")

        if ev_type in ("init", "session_start"):
            return NormalizedEvent(
                kind="init",
                session_id=session_id,
                model=data.get("model"),
                raw=data,
            )
        if ev_type in ("thinking", "thought"):
            return NormalizedEvent(
                kind="thinking",
                session_id=session_id,
                text=data.get("text") or data.get("thought"),
                raw=data,
            )
        if ev_type in ("tool_call", "tool_use"):
            return NormalizedEvent(
                kind="tool_call",
                session_id=session_id,
                tool_name=data.get("tool_name") or data.get("name") or "tool",
                text=str(data.get("args") or data.get("input") or ""),
                raw=data,
            )
        if ev_type in ("message", "assistant", "content"):
            return NormalizedEvent(
                kind="assistant_message",
                session_id=session_id,
                text=data.get("text") or data.get("content"),
                raw=data,
            )
        if ev_type in ("result", "done", "turn_complete"):
            return NormalizedEvent(
                kind="result",
                session_id=session_id,
                text=data.get("result") or data.get("text"),
                usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
                raw=data,
            )
        if ev_type == "error":
            return NormalizedEvent(
                kind="error",
                session_id=session_id,
                text=str(data.get("error") or data.get("message") or stripped),
                is_error=True,
                raw=data,
            )
        return NormalizedEvent(kind="raw", session_id=session_id, subtype=ev_type, raw=data)

    def health_check(self) -> HealthReport:
        try:
            exe, env_overrides = self.resolve_executable()
        except FileNotFoundError as exc:
            return HealthReport(
                agent=self.name,
                available=False,
                authenticated=False,
                error=str(exc),
                description=self.description,
            )

        env = {**os.environ, **env_overrides}
        rc_v, out_v, err_v = self.run_probe([*exe, "--version"], env=env)
        version = out_v.splitlines()[0] if rc_v == 0 and out_v else None
        authenticated = bool(
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or (Path.home() / ".gemini" / "oauth_creds.json").is_file()
        )
        return HealthReport(
            agent=self.name,
            available=rc_v == 0,
            authenticated=authenticated,
            version=version,
            executable=" ".join(exe),
            auth_detail="Gemini credentials detected" if authenticated else "Run `gemini` to sign in",
            error=err_v if rc_v != 0 else None,
            description=self.description,
        )
