"""Base adapter interface for pluggable CLI coding agents."""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from agentctl.models import CommandSpec, HealthReport, NormalizedEvent, RunRequest


class BaseAgentAdapter(ABC):
    """Contract every coding-agent adapter implements."""

    name: str = ""
    description: str = ""

    @abstractmethod
    def resolve_executable(self) -> tuple[list[str], dict[str, str]]:
        """Return (argv_prefix, env_overrides) to launch the CLI cleanly."""

    @abstractmethod
    def build_run_command(
        self,
        request: RunRequest,
        *,
        effective_cwd: str,
        final_message_path: Path,
    ) -> CommandSpec:
        """Build command invocation for a new headless task."""

    @abstractmethod
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
        """Build command invocation to continue an existing agent session."""

    @abstractmethod
    def parse_stdout_line(self, line: str) -> NormalizedEvent | None:
        """Parse one stdout line into a NormalizedEvent."""

    @abstractmethod
    def health_check(self) -> HealthReport:
        """Check whether the agent CLI is installed and authenticated."""

    @staticmethod
    def safe_json_loads(line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def run_probe(
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
                shell=False,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except FileNotFoundError as exc:
            return 127, "", str(exc)
        except subprocess.TimeoutExpired:
            return 124, "", f"Probe timed out after {timeout}s"
