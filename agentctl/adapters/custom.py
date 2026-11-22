"""Dynamic JSON-configurable adapter for user-defined or future CLI coding agents.

Allows registering new CLI agents (e.g., `opencode`, `pi`, `aider`, `qwen`) without
editing Python source files. Configurations are stored in:
  ~/.gemini/config/plugins/agent-host/custom_agents.json
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


def custom_agents_config_path() -> Path:
    env_path = os.environ.get("AGENTCTL_CUSTOM_AGENTS_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (
        Path.home()
        / ".gemini"
        / "config"
        / "plugins"
        / "agent-host"
        / "custom_agents.json"
    )


class CustomCliAdapter(BaseAgentAdapter):
    """Generic configurable CLI agent adapter driven by a JSON specification."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.name = str(spec["name"]).strip().lower()
        self.description = str(
            spec.get("description") or f"Custom CLI agent ({self.name})"
        )
        self.binary = str(spec.get("executable") or self.name)
        self.run_args_template: list[str] = list(
            spec.get("run_args") or ["-p", "{prompt}"]
        )
        self.resume_args_template: list[str] = list(
            spec.get("resume_args") or ["--resume", "{session_id}", "-p", "{prompt}"]
        )
        self.stdin_prompt: bool = bool(spec.get("stdin_prompt", False))
        self.version_args: list[str] = list(spec.get("version_args") or ["--version"])
        self.env_vars: dict[str, str] = dict(spec.get("env") or {})

    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        found = shutil.which(self.binary)
        if not found and os.name == "nt" and not self.binary.lower().endswith(".cmd"):
            found = shutil.which(f"{self.binary}.cmd")
        if not found:
            if Path(self.binary).is_file():
                found = str(Path(self.binary).resolve())
            else:
                raise FileNotFoundError(
                    f"Custom agent executable '{self.binary}' not found in PATH."
                )
        return [found], dict(self.env_vars)

    def _expand_args(
        self,
        template: list[str],
        *,
        prompt: str,
        effective_cwd: str,
        sandbox: str,
        model: str | None,
        session_id: str | None = None,
    ) -> list[str]:
        out: list[str] = []
        for token in template:
            if "{model}" in token and not model:
                continue
            expanded = (
                token.replace("{prompt}", prompt)
                .replace("{cwd}", effective_cwd)
                .replace("{sandbox}", sandbox)
                .replace("{model}", model or "")
                .replace("{session_id}", session_id or "")
            )
            out.append(expanded)
        return out

    def build_run_command(
        self,
        request: RunRequest,
        *,
        effective_cwd: str,
        final_message_path: Path,
    ) -> CommandSpec:
        exe, env_overrides = self.resolve_executable()
        args = self._expand_args(
            self.run_args_template,
            prompt=request.prompt,
            effective_cwd=effective_cwd,
            sandbox=request.sandbox,
            model=request.model,
        )
        if request.extra_args:
            args.extend(request.extra_args)
        return CommandSpec(
            argv=[*exe, *args],
            stdin_text=request.prompt if self.stdin_prompt else None,
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
        args = self._expand_args(
            self.resume_args_template,
            prompt=prompt,
            effective_cwd=effective_cwd,
            sandbox=sandbox,
            model=model,
            session_id=session_id,
        )
        if extra_args:
            args.extend(extra_args)
        return CommandSpec(
            argv=[*exe, *args],
            stdin_text=prompt if self.stdin_prompt else None,
            env_overrides=env_overrides,
        )

    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        data = self.safe_json_loads(stripped)
        if data is None:
            return NormalizedEvent(kind="assistant_message", text=stripped)

        ev_type = str(data.get("type") or data.get("kind") or "")
        sid = data.get("session_id") or data.get("thread_id") or data.get("sessionId")
        text = data.get("text") or data.get("content") or data.get("result")

        if ev_type in ("init", "thread.started", "system"):
            return NormalizedEvent(kind="init", session_id=sid, model=data.get("model"), raw=data)
        if ev_type in ("thinking", "reasoning"):
            return NormalizedEvent(kind="thinking", session_id=sid, text=str(text or ""), raw=data)
        if ev_type in ("tool_call", "tool_use", "command_execution"):
            return NormalizedEvent(
                kind="tool_call",
                session_id=sid,
                tool_name=str(data.get("tool") or data.get("name") or ev_type),
                text=str(text or data.get("command") or ""),
                raw=data,
            )
        if ev_type in ("assistant", "assistant_message", "message"):
            return NormalizedEvent(kind="assistant_message", session_id=sid, text=str(text or ""), raw=data)
        if ev_type in ("result", "turn.completed"):
            return NormalizedEvent(kind="result", session_id=sid, text=str(text or ""), raw=data)
        if ev_type in ("error", "turn.failed"):
            return NormalizedEvent(
                kind="error",
                session_id=sid,
                text=str(data.get("error") or text or stripped),
                is_error=True,
                raw=data,
            )
        return NormalizedEvent(kind="raw", session_id=sid, subtype=ev_type, raw=data)

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
        rc_v, out_v, err_v = self.run_probe([*exe, *self.version_args], env=env)
        version = out_v.splitlines()[0] if rc_v == 0 and out_v else "custom"
        return HealthReport(
            agent=self.name,
            available=rc_v == 0,
            authenticated=rc_v == 0,
            version=version,
            executable=" ".join(exe),
            auth_detail=f"Custom CLI adapter ({self.binary})",
            error=err_v if rc_v != 0 else None,
            description=self.description,
        )


def load_custom_adapters() -> dict[str, CustomCliAdapter]:
    path = custom_agents_config_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("agents", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return {}
        out: dict[str, CustomCliAdapter] = {}
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                adapter = CustomCliAdapter(item)
                out[adapter.name] = adapter
        return out
    except Exception:
        return {}


def save_custom_adapter(spec: dict[str, Any]) -> CustomCliAdapter:
    adapter = CustomCliAdapter(spec)
    path = custom_agents_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("agents", raw) if isinstance(raw, dict) else raw
            if isinstance(items, list):
                existing = [
                    x for x in items
                    if isinstance(x, dict) and str(x.get("name", "")).lower() != adapter.name
                ]
        except Exception:
            existing = []
    existing.append(spec)
    path.write_text(
        json.dumps({"agents": existing}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return adapter
