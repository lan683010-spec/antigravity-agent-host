#!/usr/bin/env python3
"""Zero-dependency MCP Server + MCP App backend for Antigravity Agent Host.

Exposes tools to dispatch, resume, inspect, and stop external CLI agents
(Codex, Cursor, Claude Code, Gemini CLI, and custom JSON adapters) and serves
the interactive MCP App UI at `ui://agent-host/app.html`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure bundled agentctl is importable
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from agentctl.adapters import get_adapter, list_adapters  # noqa: E402
from agentctl.adapters.custom import save_custom_adapter  # noqa: E402
from agentctl.core.runner import AgentRunner, command_summary_zh, result_summary_zh  # noqa: E402
from agentctl.core.store import RunStore, register_runs_root  # noqa: E402
from agentctl.models import RunRequest  # noqa: E402

APP_URI = "ui://agent-host/app.html"
APP_HTML_PATH = Path(__file__).resolve().parent / "app.html"

INLINE_UI_META = {
    "ui": {
        "resourceUri": APP_URI,
        "visibility": ["model", "app"],
    }
}

PANE_UI_META = {
    "ui": {
        "resourceUri": APP_URI,
        "visibility": ["model", "app"],
    },
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Agent Host"},
            {"type": "global", "title": "Agent Host"},
        ]
    },
}


def _default_workspace_runs_dir() -> Path:
    default_ws = Path(r"G:\antigravity workspace\.agent-runs")
    if default_ws.parent.is_dir():
        default_ws.mkdir(parents=True, exist_ok=True)
        register_runs_root(default_ws)
        return default_ws
    cwd_runs = (Path.cwd() / ".agent-runs").resolve()
    cwd_runs.mkdir(parents=True, exist_ok=True)
    register_runs_root(cwd_runs)
    return cwd_runs


def _build_snapshot(
    store: RunStore,
    *,
    selected_task_id: str | None = None,
    include_health: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    tasks = [t.to_dict() for t in store.list_tasks(limit=limit, all_workspaces=True)]
    active_id = selected_task_id or (tasks[0]["task_id"] if tasks else None)
    selected_meta: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    diff_text = ""

    if active_id:
        try:
            selected_meta = store.load_meta(active_id).to_dict()
            events = store.read_events(active_id, limit=250)
            diff_text = store.read_diff(active_id)
        except Exception:
            selected_meta = None

    health: list[dict[str, Any]] = []
    if include_health:
        for adapter in list_adapters(include_mock=True):
            try:
                health.append(adapter.health_check().to_dict())
            except Exception as exc:
                health.append(
                    {
                        "agent": adapter.name,
                        "available": False,
                        "authenticated": False,
                        "error": str(exc),
                    }
                )

    return {
        "selectedTaskId": active_id,
        "selectedTask": selected_meta,
        "events": events,
        "diff": diff_text,
        "tasks": tasks,
        "health": health,
    }


def _format_task_summary(meta: dict[str, Any], events: list[dict[str, Any]], diff: str) -> str:
    cmd_str = " ".join(meta.get("command_argv") or [])
    running = meta.get("status") in ("pending", "running")
    result_zh = (None if running else meta.get("result_summary_zh")) or result_summary_zh(
        meta, events, diff, partial=True)
    lines = [
        "### 🗣️ 最终结果通俗解读（简体中文）",
        result_zh,
        "\n### 🗣️ 主控命令通俗解读（简体中文）",
        meta.get("command_summary_zh") or command_summary_zh(meta),
        f"\n### 原始详细记录：任务 `{meta.get('task_id')}`",
        f"- **子 Agent**: `{meta.get('agent')}` | **原始状态**: `{meta.get('status')}` | **会话编号**: `{meta.get('session_id') or '-'}`",
        f"- **沙箱**: `{meta.get('sandbox')}` | **工作目录**: `{meta.get('effective_cwd')}`",
        f"- **命令行（原始）**: `{cmd_str}`",
        f"- **当前可见事件**: {len(events)} 条 | **Git 差异**: {len(diff)} 个字符",
    ]
    if meta.get("error"):
        lines.append(f"- **异常原文**: {meta['error']}")
    if meta.get("final_response"):
        lines.append("\n#### 最终回复原文\n" + str(meta["final_response"]))
    return "\n".join(lines)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "delete_agent_task",
        "title": "Delete Agent Tasks",
        "description": "Permanently delete one task (stopping it if running), or clean finished tasks across workspaces, optionally by agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Single task to delete."},
                "clean_finished": {"type": "boolean", "description": "Clean all non-running tasks."},
                "agent": {"type": "string", "description": "Optional agent filter for cleanup; all means every agent."},
            },
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "open_agent_host",
        "title": "Agent Host Dashboard",
        "description": "Open the Codex-Host style multi-agent visual dashboard to inspect dispatched commands, live agent thinking/tool execution streams, git diffs, and agent health.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional task_id to focus in the dashboard.",
                },
                "include_health": {
                    "type": "boolean",
                    "description": "Whether to run CLI health checks for Codex, Cursor, Claude, and Gemini.",
                },
            },
        },
        "_meta": PANE_UI_META,
    },
    {
        "name": "dispatch_agent_task",
        "title": "Dispatch Task to External Agent",
        "description": "Command an external CLI coding agent (codex, cursor, claude, gemini, mock, or custom) to execute a task and render its live visual execution card.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Target agent name: 'codex', 'cursor', 'claude', 'gemini', 'mock', or a registered custom agent.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed task instructions and acceptance criteria sent to the agent.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory path (defaults to current workspace).",
                },
                "title": {
                    "type": "string",
                    "description": "Short human-readable title for the task thread.",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write", "danger-full-access"],
                    "description": "Execution sandbox permission level (default: workspace-write).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for the target agent.",
                },
                "worktree": {
                    "type": "boolean",
                    "description": "Run in an isolated Git worktree branch for safe parallel execution.",
                },
                "async_mode": {
                    "type": "boolean",
                    "description": "If true (recommended for long tasks), spawns in background after waiting `wait_sec` so live progress can be watched in the Agent Host dashboard.",
                },
                "wait_sec": {
                    "type": "number",
                    "description": "Seconds to wait before returning when async_mode=true (default: 8).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum execution time in seconds (default: 600).",
                },
            },
            "required": ["agent", "prompt"],
        },
        "_meta": INLINE_UI_META,
    },
    {
        "name": "resume_agent_task",
        "title": "Resume Multi-Turn Agent Session",
        "description": "Send follow-up instructions or review feedback to an existing agent task session (`codex exec resume`, `cursor --resume`, `claude --resume`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Existing task_id to resume.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Follow-up prompt or revision instructions.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override.",
                },
                "async_mode": {
                    "type": "boolean",
                    "description": "If true, runs in background after `wait_sec`.",
                },
                "wait_sec": {
                    "type": "number",
                    "description": "Seconds to wait when async_mode=true (default: 8).",
                },
            },
            "required": ["task_id", "prompt"],
        },
        "_meta": INLINE_UI_META,
    },
    {
        "name": "inspect_agent_task",
        "title": "Inspect Agent Task & Live Stream",
        "description": "Fetch live status, normalized thinking/tool events, and git diff for a task (or list recent tasks).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to inspect (if omitted, returns latest task and task list).",
                },
                "wait_sec": {
                    "type": "number",
                    "description": "Optional seconds to wait if the task is currently running (default: 0).",
                },
                "include_health": {
                    "type": "boolean",
                    "description": "Include CLI health check status.",
                },
            },
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "stop_agent_task",
        "title": "Stop Running Agent Task",
        "description": "Terminate a running external agent process tree by task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to terminate.",
                },
            },
            "required": ["task_id"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "check_agents_health",
        "title": "Check Installed CLI Agents Health",
        "description": "Verify installation, version, executable path, and authentication status for Codex, Cursor, Claude Code, Gemini CLI, and custom registered agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Optional single agent name to check.",
                },
            },
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "register_custom_agent",
        "title": "Register New Custom CLI Agent Adapter",
        "description": "Dynamically register a new CLI coding agent (e.g. pi, opencode, aider, qwen) without modifying code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short lowercase identifier for the agent (e.g., 'pi', 'opencode', 'aider').",
                },
                "executable": {
                    "type": "string",
                    "description": "Executable name in PATH or full path to binary.",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of the agent.",
                },
                "run_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI argument template for new tasks. Placeholders: {prompt}, {cwd}, {sandbox}, {model}.",
                },
                "resume_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI argument template for resuming sessions. Placeholders: {session_id}, {prompt}, {cwd}, {model}.",
                },
                "stdin_prompt": {
                    "type": "boolean",
                    "description": "Whether to pass the prompt via stdin instead of argv.",
                },
            },
            "required": ["name", "executable"],
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
]


def handle_tool_call(name: str, args: dict[str, Any], store: RunStore, runner: AgentRunner) -> dict[str, Any]:
    if name == "delete_agent_task":
        task_id = args.get("task_id")
        clean = args.get("clean_finished", False)
        agent = args.get("agent")
        if not isinstance(clean, bool) or bool(task_id) == clean:
            raise ValueError("Specify either task_id or clean_finished=true")
        if agent is not None and not isinstance(agent, str):
            raise ValueError("agent must be a string")
        if clean:
            deleted = store.clean_tasks(agent=None if agent == "all" else agent)
        else:
            deleted = [task_id] if store.delete_task(task_id) else []
        snap = _build_snapshot(store)
        snap["deleted"] = deleted
        return {
            "content": [{"type": "text", "text": f"已{'清理' if clean else '删除'} {len(deleted)} 个任务。"}],
            "structuredContent": snap,
        }

    if name == "open_agent_host":
        snap = _build_snapshot(
            store,
            selected_task_id=args.get("task_id"),
            include_health=bool(args.get("include_health", True)),
        )
        summary = f"Agent Host Dashboard opened. Total recorded tasks: {len(snap['tasks'])}."
        if snap["selectedTask"]:
            summary += "\n\n" + _format_task_summary(snap["selectedTask"], snap["events"], snap["diff"])
        return {
            "content": [{"type": "text", "text": summary}],
            "structuredContent": snap,
        }

    if name == "dispatch_agent_task":
        cwd = str(args.get("cwd") or r"G:\antigravity workspace")
        cwd_path = Path(cwd).expanduser().resolve()
        if (cwd_path / ".agent-runs").is_dir() or cwd_path.is_dir():
            ws_runs = cwd_path / ".agent-runs"
            ws_runs.mkdir(parents=True, exist_ok=True)
            store = RunStore(ws_runs)
            runner = AgentRunner(store)

        req = RunRequest(
            agent=str(args["agent"]),
            prompt=str(args["prompt"]),
            cwd=str(cwd_path),
            sandbox=str(args.get("sandbox") or "workspace-write"),  # type: ignore[arg-type]
            model=args.get("model"),
            timeout_sec=int(args.get("timeout_sec") or 600),
            use_worktree=bool(args.get("worktree", False)),
            title=args.get("title"),
            dispatcher="Antigravity Main Agent",
        )
        async_mode = bool(args.get("async_mode", True))
        wait_sec = float(args.get("wait_sec", 8.0))
        if async_mode:
            meta = runner.start_task_async(req, wait_sec=wait_sec)
        else:
            meta = runner.run_task(req)

        snap = _build_snapshot(store, selected_task_id=meta.task_id, include_health=False)
        text = _format_task_summary(meta.to_dict(), snap["events"], snap["diff"])
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": snap,
        }

    if name == "resume_agent_task":
        task_id = str(args["task_id"])
        prompt = str(args["prompt"])
        async_mode = bool(args.get("async_mode", True))
        wait_sec = float(args.get("wait_sec", 8.0))
        if async_mode:
            meta = runner.resume_task_async(
                task_id,
                prompt,
                model=args.get("model"),
                wait_sec=wait_sec,
            )
        else:
            meta = runner.resume_task(
                task_id,
                prompt,
                model=args.get("model"),
            )
        snap = _build_snapshot(store, selected_task_id=meta.task_id, include_health=False)
        text = _format_task_summary(meta.to_dict(), snap["events"], snap["diff"])
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": snap,
        }

    if name == "inspect_agent_task":
        task_id = args.get("task_id")
        wait_sec = float(args.get("wait_sec") or 0.0)
        if task_id and wait_sec > 0:
            runner.wait_for_task(str(task_id), wait_sec=wait_sec)
        snap = _build_snapshot(
            store,
            selected_task_id=str(task_id) if task_id else None,
            include_health=bool(args.get("include_health", False)),
        )
        if snap["selectedTask"]:
            text = _format_task_summary(snap["selectedTask"], snap["events"], snap["diff"])
        else:
            text = "No tasks found."
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": snap,
        }

    if name == "stop_agent_task":
        task_id = str(args["task_id"])
        meta = runner.stop_task(task_id)
        snap = _build_snapshot(store, selected_task_id=meta.task_id, include_health=False)
        return {
            "content": [{"type": "text", "text": f"Task `{task_id}` stopped (status={meta.status})."}],
            "structuredContent": snap,
        }

    if name == "check_agents_health":
        target = args.get("agent")
        adapters = [get_adapter(str(target))] if target else list_adapters(include_mock=True)
        reports = [a.health_check().to_dict() for a in adapters]
        lines = ["### External CLI Agents Health Status"]
        for r in reports:
            avail = "INSTALLED" if r["available"] else "MISSING"
            auth = "LOGGED_IN" if r["authenticated"] else "NOT_LOGGED_IN"
            lines.append(
                f"- **{r['agent']}**: `{avail}` | `{auth}` | version: `{r.get('version') or '-'}` | `{r.get('auth_detail') or r.get('error') or ''}`"
            )
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structuredContent": {"health": reports},
        }

    if name == "register_custom_agent":
        spec = {
            "name": args["name"],
            "executable": args["executable"],
            "description": args.get("description") or f"Custom CLI agent ({args['name']})",
            "run_args": args.get("run_args") or ["-p", "{prompt}"],
            "resume_args": args.get("resume_args") or ["--resume", "{session_id}", "-p", "{prompt}"],
            "stdin_prompt": bool(args.get("stdin_prompt", False)),
        }
        adapter = save_custom_adapter(spec)
        report = adapter.health_check().to_dict()
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Registered custom agent `{adapter.name}` ({adapter.description}). Available={report['available']}",
                }
            ],
            "structuredContent": {"adapter": spec, "health": report},
        }

    raise ValueError(f"Unknown tool: {name}")


def _sanitize_str(s: str) -> str:
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def _send_response(msg_id: Any, result: Any) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    raw = _sanitize_str(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def _send_error(msg_id: Any, code: int, message: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
    raw = _sanitize_str(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(description="Agent Host MCP Server")
    parser.add_argument("--plugin-root", default=str(PLUGIN_ROOT))
    parser.add_argument("--data-dir", default=None)
    parser.parse_args()

    store = RunStore(_default_workspace_runs_dir())
    runner = AgentRunner(store)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue

        method = req.get("method", "")
        msg_id = req.get("id")
        params = req.get("params") or {}

        # Notifications have no id
        if msg_id is None:
            continue

        try:
            if method == "initialize":
                protocol_version = params.get("protocolVersion") or "2024-11-05"
                _send_response(
                    msg_id,
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {
                            "name": "agent-host-orchestrator",
                            "version": "1.0.0",
                        },
                    },
                )
            elif method == "ping":
                _send_response(msg_id, {})
            elif method == "tools/list":
                _send_response(msg_id, {"tools": TOOLS})
            elif method == "tools/call":
                t_name = str(params.get("name") or "")
                t_args = params.get("arguments") or {}
                res = handle_tool_call(t_name, t_args, store, runner)
                _send_response(msg_id, res)
            elif method == "resources/list":
                _send_response(
                    msg_id,
                    {
                        "resources": [
                            {
                                "uri": APP_URI,
                                "name": "Agent Host Dashboard",
                                "description": "Codex-Host style multi-agent visual monitor for Antigravity",
                                "mimeType": "text/html;profile=mcp-app",
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                )
            elif method == "resources/read":
                uri = params.get("uri")
                if uri != APP_URI:
                    _send_error(msg_id, -32602, f"Unknown resource URI: {uri}")
                else:
                    html = (
                        APP_HTML_PATH.read_text(encoding="utf-8")
                        if APP_HTML_PATH.is_file()
                        else "<html><body>Agent Host UI not found</body></html>"
                    )
                    _send_response(
                        msg_id,
                        {
                            "contents": [
                                {
                                    "uri": APP_URI,
                                    "mimeType": "text/html;profile=mcp-app",
                                    "text": html,
                                    "_meta": {"ui": {"prefersBorder": False}},
                                }
                            ]
                        },
                    )
            else:
                _send_error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            _send_response(
                msg_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                },
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
