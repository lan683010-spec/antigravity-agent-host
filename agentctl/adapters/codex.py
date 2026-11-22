"""Adapter for OpenAI Codex CLI (`codex exec --json` / `codex exec resume`)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


class CodexAdapter(BaseAgentAdapter):
    name = "codex"
    description = "OpenAI Codex CLI (`codex exec --json` / `codex exec resume`)"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        """Resolve `node.exe + codex.js` on Windows to avoid cmd/ps1 wrappers."""
        env_overrides: dict[str, str] = {}

        if os.name == "nt":
            candidate_roots: list[Path] = []
            found_cmd = shutil.which("codex.cmd")
            if found_cmd:
                candidate_roots.append(Path(found_cmd).parent)
            appdata = os.environ.get("APPDATA")
            if appdata:
                candidate_roots.append(Path(appdata) / "npm")

            node_exe = shutil.which("node") or shutil.which("node.exe")
            for root in candidate_roots:
                codex_js = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
                if codex_js.is_file():
                    local_node = root / "node.exe"
                    resolved_node = str(local_node) if local_node.is_file() else node_exe
                    if resolved_node:
                        return [resolved_node, str(codex_js)], env_overrides

            if found_cmd:
                return [found_cmd], env_overrides

        found = shutil.which("codex")
        if found:
            if os.name == "nt" and found.lower().endswith(".ps1"):
                sibling_cmd = Path(found).with_suffix(".cmd")
                if sibling_cmd.is_file():
                    return [str(sibling_cmd)], env_overrides
            return [found], env_overrides

        raise FileNotFoundError(
            "OpenAI Codex CLI (`codex`) not found. "
            "Install via: npm.cmd install -g @openai/codex"
        )

    def _sandbox_flags(self, sandbox: str, *, is_resume: bool) -> list[str]:
        if sandbox == "danger-full-access":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if is_resume:
            # `codex exec resume` inherits thread sandbox unless bypassing
            return []
        if sandbox == "read-only":
            return ["-s", "read-only"]
        # Default: workspace-write with automatic approval (`--approve-for-me` implies workspace-write)
        return ["--approve-for-me"]

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
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--trust",
            "-C",
            effective_cwd,
            "-o",
            str(final_message_path),
            *self._sandbox_flags(request.sandbox, is_resume=False),
        ]
        if request.model:
            argv.extend(["-m", request.model])
        if request.output_schema:
            argv.extend(["--output-schema", request.output_schema])
        if request.extra_args:
            argv.extend(request.extra_args)
        # Pass "-" so Codex reads the full UTF-8 prompt cleanly from stdin
        argv.append("-")
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
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "--trust",
            "-o",
            str(final_message_path),
            *self._sandbox_flags(sandbox, is_resume=True),
        ]
        if model:
            argv.extend(["-m", model])
        if extra_args:
            argv.extend(extra_args)
        argv.extend([session_id, "-"])
        return CommandSpec(
            argv=argv,
            stdin_text=prompt,
            env_overrides=env_overrides,
        )

    @staticmethod
    def _extract_text_from_item(item: dict[str, Any]) -> str:
        if isinstance(item.get("text"), str):
            return item["text"]
        if isinstance(item.get("content"), str):
            return item["content"]
        if isinstance(item.get("content"), list):
            parts = [
                b.get("text", "")
                for b in item["content"]
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ]
            return "".join(parts)
        return ""

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        data = self.safe_json_loads(stripped)
        if data is None:
            return NormalizedEvent(kind="raw", text=stripped)

        ev_type = data.get("type", "")
        session_id = data.get("thread_id") or data.get("session_id")

        if ev_type == "thread.started":
            return NormalizedEvent(
                kind="init",
                session_id=session_id,
                subtype=ev_type,
                raw=data,
            )

        if ev_type in ("item.started", "item.updated", "item.completed"):
            item = data.get("item")
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "reasoning":
                    return NormalizedEvent(
                        kind="thinking",
                        session_id=session_id,
                        subtype=ev_type,
                        text=self._extract_text_from_item(item),
                        raw=data,
                    )
                if item_type == "agent_message":
                    text = self._extract_text_from_item(item)
                    return NormalizedEvent(
                        kind="assistant_message",
                        session_id=session_id,
                        subtype=ev_type,
                        text=text,
                        raw=data,
                    )
                if item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    tool_desc = item.get("command") or item.get("tool") or item_type
                    return NormalizedEvent(
                        kind="tool_call",
                        session_id=session_id,
                        subtype=ev_type,
                        tool_name=str(item_type),
                        text=str(tool_desc),
                        raw=data,
                    )

        if ev_type == "turn.completed":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            return NormalizedEvent(
                kind="result",
                session_id=session_id,
                subtype=ev_type,
                usage=usage,
                raw=data,
            )

        if ev_type in ("turn.failed", "error"):
            err_obj = data.get("error") or data.get("message") or stripped
            if isinstance(err_obj, dict):
                err_msg = err_obj.get("message") or str(err_obj)
            else:
                err_msg = str(err_obj)
            return NormalizedEvent(
                kind="error",
                session_id=session_id,
                subtype=ev_type,
                text=err_msg,
                is_error=True,
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
            )

        env = {**os.environ, **env_overrides}
        rc_v, out_v, err_v = self.run_probe([*exe, "--version"], env=env)
        version = out_v.splitlines()[0] if rc_v == 0 and out_v else None

        rc_s, out_s, err_s = self.run_probe([*exe, "login", "status"], env=env)
        # Note: Codex CLI prints "Logged in using ChatGPT" to stderr on some versions
        combined = "\n".join(x for x in (out_s, err_s) if x).strip()
        lower = combined.lower()
        authenticated = (
            "logged in" in lower
            or bool(os.environ.get("OPENAI_API_KEY"))
        )

        return HealthReport(
            agent=self.name,
            available=rc_v == 0,
            authenticated=authenticated,
            version=version,
            executable=" ".join(exe),
            auth_detail=combined or None,
            error=err_v if rc_v != 0 else None,
        )
