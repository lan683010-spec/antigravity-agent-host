"""Adapter for Anthropic Claude Code CLI (`claude -p --output-format stream-json`)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


class ClaudeAdapter(BaseAgentAdapter):
    name = "claude"
    description = "Anthropic Claude Code CLI (`claude -p --output-format stream-json`)"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        env_overrides: dict[str, str] = {}
        if os.name == "nt":
            candidate_roots: list[Path] = []
            found_cmd = shutil.which("claude.cmd")
            if found_cmd:
                candidate_roots.append(Path(found_cmd).parent)
            appdata = os.environ.get("APPDATA")
            if appdata:
                candidate_roots.append(Path(appdata) / "npm")

            node_exe = shutil.which("node") or shutil.which("node.exe")
            for root in candidate_roots:
                cli_js = (
                    root
                    / "node_modules"
                    / "@anthropic-ai"
                    / "claude-code"
                    / "cli.js"
                )
                if cli_js.is_file() and node_exe:
                    return [node_exe, str(cli_js)], env_overrides

            if found_cmd:
                return [found_cmd], env_overrides

        found = shutil.which("claude")
        if found:
            if os.name == "nt" and found.lower().endswith(".ps1"):
                sibling_cmd = Path(found).with_suffix(".cmd")
                if sibling_cmd.is_file():
                    return [str(sibling_cmd)], env_overrides
            return [found], env_overrides

        raise FileNotFoundError(
            "Claude Code CLI (`claude`) not found. "
            "Install via: npm.cmd install -g @anthropic-ai/claude-code"
        )

    def _sandbox_flags(self, sandbox: str) -> list[str]:
        if sandbox == "danger-full-access":
            return ["--dangerously-skip-permissions"]
        if sandbox == "read-only":
            return ["--permission-mode", "plan"]
        return ["--permission-mode", "acceptEdits"]

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
            "--verbose",
            "--output-format",
            "stream-json",
            *self._sandbox_flags(request.sandbox),
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if request.extra_args:
            argv.extend(request.extra_args)
        return CommandSpec(
            argv=argv,
            stdin_text=request.prompt,
            env_overrides=env_overrides,
        )

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
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--resume",
            session_id,
            *self._sandbox_flags(sandbox),
        ]
        if model:
            argv.extend(["--model", model])
        if extra_args:
            argv.extend(extra_args)
        return CommandSpec(
            argv=argv,
            stdin_text=prompt,
            env_overrides=env_overrides,
        )

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        data = self.safe_json_loads(stripped)
        if data is None:
            return NormalizedEvent(kind="raw", text=stripped)

        ev_type = data.get("type")
        subtype = data.get("subtype")
        session_id = data.get("session_id")

        if ev_type == "system" and subtype == "init":
            return NormalizedEvent(
                kind="init",
                session_id=session_id,
                model=data.get("model"),
                subtype=subtype,
                raw=data,
            )

        if ev_type == "assistant":
            msg = data.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            return NormalizedEvent(
                                kind="thinking",
                                session_id=session_id,
                                text=block.get("thinking") or block.get("text"),
                                raw=data,
                            )
                        if btype == "tool_use":
                            t_name = block.get("name") or "tool"
                            t_input = block.get("input")
                            desc = ""
                            if isinstance(t_input, dict):
                                desc = (
                                    t_input.get("command")
                                    or t_input.get("file_path")
                                    or t_input.get("pattern")
                                    or ""
                                )
                            return NormalizedEvent(
                                kind="tool_call",
                                session_id=session_id,
                                tool_name=str(t_name),
                                text=str(desc) if desc else None,
                                raw=data,
                            )
                        if btype == "text":
                            return NormalizedEvent(
                                kind="assistant_message",
                                session_id=session_id,
                                text=block.get("text"),
                                raw=data,
                            )

        if ev_type == "result":
            is_error = bool(data.get("is_error")) or subtype == "error"
            return NormalizedEvent(
                kind="error" if is_error else "result",
                session_id=session_id,
                subtype=subtype,
                text=str(data.get("result") or ""),
                is_error=is_error,
                usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
                raw=data,
            )

        return NormalizedEvent(kind="raw", session_id=session_id, subtype=str(ev_type), raw=data)

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
            os.environ.get("ANTHROPIC_API_KEY")
            or (Path.home() / ".claude.json").is_file()
            or (Path.home() / ".claude").is_dir()
        )
        return HealthReport(
            agent=self.name,
            available=rc_v == 0,
            authenticated=authenticated,
            version=version,
            executable=" ".join(exe),
            auth_detail="Claude config / API key detected" if authenticated else "Run `claude login`",
            error=err_v if rc_v != 0 else None,
            description=self.description,
        )
