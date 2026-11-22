"""Persistent run store under `.agent-runs/<task_id>/` with global workspace indexing."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentctl.models import NormalizedEvent, TaskMeta

DEFAULT_RUNS_DIR_NAME = ".agent-runs"


def global_plugin_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "plugins" / "agent-host"


def runs_roots_index_path() -> Path:
    return global_plugin_dir() / "runs_roots.json"


def register_runs_root(root: Path) -> None:
    """Record a workspace `.agent-runs` root in the global plugin index."""
    try:
        resolved = str(root.resolve())
        idx_path = runs_roots_index_path()
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        roots: list[str] = []
        if idx_path.is_file():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    roots = [str(x) for x in data if isinstance(x, str)]
            except Exception:
                roots = []
        if resolved not in roots:
            roots.insert(0, resolved)
            idx_path.write_text(
                json.dumps(roots[:30], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


def get_known_runs_roots(primary: Path | None = None) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except Exception:
            return
        key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)

    if primary is not None:
        _add(primary)
    _add(default_runs_dir())

    idx_path = runs_roots_index_path()
    if idx_path.is_file():
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        _add(Path(item))
        except Exception:
            pass

    return out


def default_runs_dir() -> Path:
    env_dir = os.environ.get("AGENTCTL_RUNS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_runs = (Path.cwd() / DEFAULT_RUNS_DIR_NAME).resolve()
    if cwd_runs.is_dir():
        return cwd_runs
    package_root = Path(__file__).resolve().parent.parent.parent
    return package_root / DEFAULT_RUNS_DIR_NAME


def generate_task_id(agent: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{stamp}-{agent}-{suffix}"


class RunStore:
    """Manages per-task directories and atomic metadata persistence."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_runs_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        register_runs_root(self.root)

    def resolve_task_root(self, task_id: str) -> Path:
        """Locate the runs root that owns `task_id` across known workspaces."""
        if (self.root / task_id / "meta.json").is_file():
            return self.root
        for candidate in get_known_runs_roots(self.root):
            if (candidate / task_id / "meta.json").is_file():
                return candidate
        return self.root

    def _deletable_task_dirs(self, task_id: str) -> list[Path]:
        # Only direct children of registered roots may be deleted. Reject Windows
        # drive/stream syntax and aliases as well as traversal on every platform.
        if (not isinstance(task_id, str) or not task_id
                or task_id in (".", "..") or task_id.endswith((".", " "))
                or any(c in task_id for c in '/\\:*?"<>|\x00')):
            raise ValueError("Invalid task_id")
        paths = []
        for root in get_known_runs_roots(self.root):
            root = root.resolve()
            path = root / task_id
            if path.is_dir():
                resolved = path.resolve()
                if path.is_symlink() or resolved.parent != root or resolved != path:
                    raise ValueError(f"Unsafe task directory: {path}")
                paths.append(path)
        return paths

    def delete_task(self, task_id: str) -> bool:
        """Permanently remove every copy of a task across known workspaces."""
        paths = self._deletable_task_dirs(task_id)
        for path in paths:
            try:
                data = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            pid = data.get("pid")
            if data.get("status") == "running" and isinstance(pid, int) and pid > 0:
                # Lazy import avoids the runner/store import cycle.
                from agentctl.core.runner import kill_process_tree
                try:
                    kill_process_tree(pid)
                except OSError:
                    pass
            shutil.rmtree(path, ignore_errors=True)
        remaining = [path for path in paths if path.exists()]
        if remaining:
            raise OSError(f"Could not delete task directories: {remaining}")
        return bool(paths)

    def clean_tasks(self, *, agent: str | None = None, include_running: bool = False) -> list[str]:
        """Remove finished tasks without a display limit; optionally filter by agent."""
        candidates: dict[str, list[dict[str, Any]]] = {}
        for root in get_known_runs_roots(self.root):
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    data = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("task_id") == entry.name:
                        candidates.setdefault(entry.name, []).append(data)
                except (OSError, ValueError):
                    continue
        deleted = []
        for task_id, copies in candidates.items():
            # A running copy in another workspace must also be preserved.
            if all((include_running or m.get("status") != "running")
                   and (agent is None or m.get("agent") == agent) for m in copies):
                if self.delete_task(task_id):
                    deleted.append(task_id)
        return deleted

    def task_dir(self, task_id: str) -> Path:
        base = self.resolve_task_root(task_id)
        path = base / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def meta_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "meta.json"

    def raw_log_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "raw.jsonl"

    def events_log_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "events.jsonl"

    def stderr_log_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "stderr.log"

    def final_message_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "final.md"

    def diff_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "diff.patch"

    def save_meta(self, meta: TaskMeta) -> None:
        path = self.meta_path(meta.task_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def load_meta(self, task_id: str) -> TaskMeta:
        base = self.resolve_task_root(task_id)
        path = base / task_id / "meta.json"
        if not path.is_file():
            raise FileNotFoundError(f"Task '{task_id}' not found in {self.root}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskMeta.from_dict(data)

    def append_raw(self, task_id: str, line: str) -> None:
        with self.raw_log_path(task_id).open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(line if line.endswith("\n") else f"{line}\n")

    def append_event(self, task_id: str, event: NormalizedEvent) -> None:
        with self.events_log_path(task_id).open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def write_final_message(self, task_id: str, content: str) -> None:
        self.final_message_path(task_id).write_text(content, encoding="utf-8")

    def read_events(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        path = self.events_log_path(task_id)
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if limit and limit > 0:
            lines = lines[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
            except Exception:
                continue
        return events

    def read_diff(self, task_id: str) -> str:
        path = self.diff_path(task_id)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def list_tasks(self, limit: int = 30, all_workspaces: bool = True) -> list[TaskMeta]:
        roots = get_known_runs_roots(self.root) if all_workspaces else [self.root]
        seen_ids: set[str] = set()
        metas: list[TaskMeta] = []
        for r in roots:
            if not r.is_dir():
                continue
            for entry in r.iterdir():
                meta_file = entry / "meta.json"
                if entry.is_dir() and meta_file.is_file():
                    try:
                        data = json.loads(meta_file.read_text(encoding="utf-8"))
                        m = TaskMeta.from_dict(data)
                        if m.task_id not in seen_ids:
                            seen_ids.add(m.task_id)
                            metas.append(m)
                    except Exception:
                        continue
        metas.sort(key=lambda m: m.created_at, reverse=True)
        return metas[:limit]
