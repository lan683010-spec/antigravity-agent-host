"""Adapter for Cursor CLI (`cursor-agent` / `agent`)."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest

_VERSION_DIR_RE = re.compile(
    r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:-(\d{2})-(\d{2})-(\d{2}))?-[a-f0-9]+$"
)


def _version_sort_key(name: str) -> tuple[int, int, int, int, int, int]:
    match = _VERSION_DIR_RE.match(name)
    if not match:
        return (0, 0, 0, 0, 0, 0)
    year, month, day, hh, mm, ss = match.groups()
    return (
        int(year),
        int(month),
        int(day),
        int(hh or 0),
        int(mm or 0),
        int(ss or 0),
    )


class CursorAdapter(BaseAgentAdapter):
    name = "cursor"
    description = "Cursor CLI agent (`cursor-agent -p --output-format stream-json`)"

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        """Resolve direct node.exe + index.js on Windows to avoid cmd/ps1 quoting & policy issues."""
        env_overrides: dict[str, str] = {}
        local_app_data = os.environ.get("LOCALAPPDATA")

        if os.name == "nt" and local_app_data:
            base_dir = Path(local_app_data) / "cursor-agent"
            compile_cache = Path(local_app_data) / "cursor-compile-cache"
            env_overrides["NODE_COMPILE_CACHE"] = str(compile_cache)
            env_overrides["CURSOR_INVOKED_AS"] = "cursor-agent.cmd"

            direct_node = base_dir / "node.exe"
            direct_index = base_dir / "index.js"
            if direct_node.is_file() and direct_index.is_file():
                return [str(direct_node), str(direct_index)], env_overrides

            versions_dir = base_dir / "versions"
            if versions_dir.is_dir():
                candidates = [
                    d
                    for d in versions_dir.iterdir()
                    if d.is_dir() and _VERSION_DIR_RE.match(d.name)
                ]
                candidates.sort(key=lambda d: _version_sort_key(d.name), reverse=True)
                for ver_dir in candidates:
                    node_exe = ver_dir / "node.exe"
                    index_js = ver_dir / "index.js"
                    if node_exe.is_file() and index_js.is_file():
                        return [str(node_exe), str(index_js)], env_overrides

            cmd_shim = base_dir / "cursor-agent.cmd"
            if cmd_shim.is_file():
                return [str(cmd_shim)], env_overrides

        for candidate in ("cursor-agent", "agent"):
            found = shutil.which(candidate)
            if found:
                # Avoid .ps1 shims on Windows when a sibling .cmd exists
                if os.name == "nt" and found.lower().endswith(".ps1"):
                    sibling_cmd = Path(found).with_suffix(".cmd")
                    if sibling_cmd.is_file():
                        return [str(sibling_cmd)], env_overrides
                return [found], env_overrides

        raise FileNotFoundError(
            "Cursor CLI (`cursor-agent` / `agent`) not found. "
            "Install via: irm 'https://cursor.com/install?win32=true' | iex"
        )

    def _sandbox_flags(self, sandbox: str) -> list[str]:
        if sandbox == "read-only":
            return ["--mode", "plan"]
        if sandbox == "danger-full-access":
            return ["--force", "--sandbox", "disabled"]
        # Cursor CLI sandbox mode requires macOS or Linux; on Windows use allowlist mode (`disabled`)
        if os.name == "nt":
            return ["--force", "--sandbox", "disabled"]
        return ["--force", "--sandbox", "enabled"]

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
            "--output-format",
            "stream-json",
            "--trust",
            "--workspace",
            effective_cwd,
            *self._sandbox_flags(request.sandbox),
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if request.extra_args:
            argv.extend(request.extra_args)
        argv.append(request.prompt)
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
            "-p",
            "--output-format",
            "stream-json",
            "--trust",
            "--workspace",
            effective_cwd,
            "--resume",
            session_id,
            *self._sandbox_flags(sandbox),
        ]
        if model:
            argv.extend(["--model", model])
        if extra_args:
            argv.extend(extra_args)
        argv.append(prompt)
        return CommandSpec(argv=argv, stdin_text=None, env_overrides=env_overrides)

    @staticmethod
    def _extract_message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

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

        if ev_type == "thinking":
            return NormalizedEvent(
                kind="thinking",
                session_id=session_id,
                subtype=subtype,
                text=data.get("text"),
                raw=data,
            )

        if ev_type == "tool_call":
            tool_call = data.get("tool_call")
            tool_name = None
            if isinstance(tool_call, dict) and tool_call:
                tool_name = next(iter(tool_call.keys()))
            return NormalizedEvent(
                kind="tool_call",
                session_id=session_id,
                subtype=subtype,
                tool_name=tool_name or data.get("name"),
                raw=data,
            )

        if ev_type == "assistant":
            text = self._extract_message_text(data.get("message"))
            return NormalizedEvent(
                kind="assistant_message",
                session_id=session_id,
                text=text,
                raw=data,
            )

        if ev_type == "result":
            is_error = bool(data.get("is_error")) or subtype == "error"
            result_text = data.get("result")
            if result_text is not None and not isinstance(result_text, str):
                result_text = str(result_text)
            return NormalizedEvent(
                kind="error" if is_error else "result",
                session_id=session_id,
                subtype=subtype,
                text=result_text,
                is_error=is_error,
                usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
                raw=data,
            )

        if ev_type == "error":
            msg = data.get("error") or data.get("message") or stripped
            return NormalizedEvent(
                kind="error",
                session_id=session_id,
                text=str(msg),
                is_error=True,
                raw=data,
            )

        return NormalizedEvent(kind="raw", session_id=session_id, subtype=subtype, raw=data)

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

        rc_s, out_s, err_s = self.run_probe([*exe, "status"], env=env)
        combined = "\n".join(x for x in (out_s, err_s) if x).strip()
        lower = combined.lower()
        authenticated = rc_s == 0 and (
            "logged in" in lower or "api key" in lower or bool(os.environ.get("CURSOR_API_KEY"))
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
