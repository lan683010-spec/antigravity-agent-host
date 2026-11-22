"""Command-line interface for agentctl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from agentctl.adapters import get_adapter, list_adapters
from agentctl.adapters.custom import save_custom_adapter
from agentctl.core.runner import AgentRunner
from agentctl.core.store import RunStore
from agentctl.models import NormalizedEvent, RunRequest


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _resolve_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if prompt_file:
        text = Path(prompt_file).read_text(encoding="utf-8")
        if prompt:
            return f"{prompt}\n\n{text}"
        return text
    if prompt == "-":
        return sys.stdin.read()
    if not prompt:
        raise ValueError("Provide --prompt <text> or --prompt-file <path>.")
    return prompt


def _format_event_progress(ev: NormalizedEvent) -> str | None:
    if ev.kind == "init":
        if ev.subtype == "dispatch":
            return f"[dispatch] {ev.text or ''}"
        return f"[init] session={ev.session_id or '?'} model={ev.model or '?'}"
    if ev.kind == "thinking":
        snippet = (ev.text or "").replace("\n", " ").strip()[:90]
        return f"[thinking] {snippet}" if snippet else None
    if ev.kind == "tool_call":
        detail = f": {ev.text}" if ev.text else ""
        sub = f" ({ev.subtype})" if ev.subtype else ""
        return f"[tool]{sub} {ev.tool_name or 'unknown'}{detail}"
    if ev.kind == "error":
        return f"[error] {ev.text or 'unknown error'}"
    if ev.kind == "result":
        return "[result] completed"
    return None


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.agent:
        adapters = [get_adapter(args.agent)]
    else:
        adapters = list_adapters(include_mock=args.include_mock)

    reports = [adapter.health_check() for adapter in adapters]
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2))
    else:
        for r in reports:
            avail = "OK" if r.available else "MISSING"
            auth = "OK" if r.authenticated else "NOT_LOGGED_IN"
            print(f"- {r.agent:<8} install={avail:<7} auth={auth:<13} version={r.version or '-'}")
            if r.auth_detail:
                first_line = r.auth_detail.splitlines()[0]
                print(f"    auth_detail: {first_line}")
            if r.executable:
                print(f"    executable : {r.executable}")
            if r.error:
                print(f"    error      : {r.error}")
    return 0 if any(r.available for r in reports) else 1


def _cmd_run(args: argparse.Namespace, runner: AgentRunner) -> int:
    prompt = _resolve_prompt(args.prompt, args.prompt_file)
    request = RunRequest(
        agent=args.agent,
        prompt=prompt,
        cwd=args.cwd,
        sandbox=args.sandbox,
        model=args.model,
        timeout_sec=args.timeout,
        use_worktree=args.worktree,
        worktree_base=args.worktree_base,
        output_schema=args.output_schema,
        extra_args=args.extra_arg or [],
        title=args.title,
        dispatcher=args.dispatcher,
    )

    if args.async_run:
        meta = runner.start_task_async(request, wait_sec=args.wait)
    else:
        def _on_event(ev: NormalizedEvent) -> None:
            if args.json or args.quiet:
                return
            line = _format_event_progress(ev)
            if line:
                print(line, file=sys.stderr, flush=True)

        meta = runner.run_task(request, on_event=_on_event)

    if args.json:
        print(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"task_id    : {meta.task_id}")
        print(f"agent      : {meta.agent}")
        print(f"status     : {meta.status}")
        print(f"session_id : {meta.session_id or '-'}")
        print(f"duration_ms: {meta.duration_ms}")
        if meta.worktree_path:
            print(f"worktree   : {meta.worktree_path} ({meta.worktree_branch})")
        if meta.error:
            print(f"error      : {meta.error}")
        if meta.final_response:
            print("\n--- FINAL RESPONSE ---")
            print(meta.final_response)
    return 0 if meta.status in ("completed", "running") else 1


def _cmd_resume(args: argparse.Namespace, runner: AgentRunner) -> int:
    prompt = _resolve_prompt(args.prompt, args.prompt_file)

    if args.async_run:
        meta = runner.resume_task_async(
            args.task_id,
            prompt,
            timeout_sec=args.timeout,
            model=args.model,
            extra_args=args.extra_arg or [],
            wait_sec=args.wait,
        )
    else:
        def _on_event(ev: NormalizedEvent) -> None:
            if args.json or args.quiet:
                return
            line = _format_event_progress(ev)
            if line:
                print(line, file=sys.stderr, flush=True)

        meta = runner.resume_task(
            args.task_id,
            prompt,
            timeout_sec=args.timeout,
            model=args.model,
            extra_args=args.extra_arg or [],
            on_event=_on_event,
        )

    if args.json:
        print(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"task_id    : {meta.task_id}")
        print(f"agent      : {meta.agent}")
        print(f"status     : {meta.status}")
        print(f"turns      : {len(meta.turns)}")
        print(f"duration_ms: {meta.duration_ms}")
        if meta.error:
            print(f"error      : {meta.error}")
        if meta.final_response:
            print("\n--- FINAL RESPONSE ---")
            print(meta.final_response)
    return 0 if meta.status in ("completed", "running") else 1


def _cmd_list(args: argparse.Namespace, store: RunStore) -> int:
    tasks = store.list_tasks(limit=args.limit)
    if args.json:
        print(json.dumps([t.to_dict() for t in tasks], ensure_ascii=False, indent=2))
        return 0
    if not tasks:
        print("No tasks recorded yet.")
        return 0
    for t in tasks:
        snippet = t.prompt.replace("\n", " ")[:60]
        print(
            f"{t.task_id}  agent={t.agent:<6}  status={t.status:<9}  "
            f"turns={len(t.turns)}  prompt={snippet!r}"
        )
    return 0


def _cmd_status(args: argparse.Namespace, store: RunStore) -> int:
    meta = store.load_meta(args.task_id)
    print(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_delete(args: argparse.Namespace, store: RunStore) -> int:
    deleted = [args.task_id] if store.delete_task(args.task_id) else []
    print(json.dumps({"deleted": deleted}, ensure_ascii=False) if args.json
          else f"已删除 {len(deleted)} 个任务。")
    return 0


def _cmd_clean(args: argparse.Namespace, store: RunStore) -> int:
    deleted = store.clean_tasks(agent=args.agent)
    print(json.dumps({"deleted": deleted}, ensure_ascii=False) if args.json
          else f"已清理 {len(deleted)} 个陈旧任务，保留运行中的任务。")
    return 0


def _cmd_logs(args: argparse.Namespace, store: RunStore) -> int:
    path = store.raw_log_path(args.task_id) if args.raw else store.events_log_path(args.task_id)
    if not path.is_file():
        print(f"Log file not found: {path}", file=sys.stderr)
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.tail and args.tail > 0:
        lines = lines[-args.tail :]
    for line in lines:
        print(line)
    return 0


def _cmd_diff(args: argparse.Namespace, store: RunStore) -> int:
    path = store.diff_path(args.task_id)
    if not path.is_file():
        print("No git diff recorded for this task.")
        return 0
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    print(content if content else "(clean working tree — no git changes)")
    return 0


def _cmd_stop(args: argparse.Namespace, runner: AgentRunner) -> int:
    meta = runner.stop_task(args.task_id)
    if args.json:
        print(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Task {meta.task_id} status: {meta.status}")
    return 0


def _cmd_register_adapter(args: argparse.Namespace) -> int:
    spec = json.loads(args.spec_json)
    adapter = save_custom_adapter(spec)
    print(json.dumps({"registered": adapter.name, "description": adapter.description}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="Unified multi-agent CLI orchestrator (Codex, Cursor, Claude, Gemini, and custom agents).",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Override task store directory (default: <workspace>/.agent-runs)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="Check installed agents and login status")
    p_doc.add_argument("--agent", help="Check only a single agent")
    p_doc.add_argument("--include-mock", action="store_true", help="Include offline mock adapter")
    p_doc.add_argument("--json", action="store_true", help="Output JSON")

    p_run = sub.add_parser("run", help="Dispatch a new task to an agent")
    p_run.add_argument("--agent", required=True, help="Agent name (codex, cursor, claude, gemini, mock, or custom)")
    p_run.add_argument("--cwd", default=".", help="Working directory for the agent")
    p_run.add_argument("--prompt", help="Task prompt (use '-' to read from stdin)")
    p_run.add_argument("--prompt-file", help="Read task prompt from a UTF-8 file")
    p_run.add_argument("--title", help="Short human-readable task title")
    p_run.add_argument("--dispatcher", help="Dispatcher identity label")
    p_run.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Execution sandbox level (default: workspace-write)",
    )
    p_run.add_argument("--model", help="Override agent model")
    p_run.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    p_run.add_argument("--worktree", action="store_true", help="Run in an isolated git worktree")
    p_run.add_argument("--worktree-base", help="Base git ref for --worktree")
    p_run.add_argument("--output-schema", help="JSON schema file for final output (Codex)")
    p_run.add_argument("--extra-arg", action="append", help="Extra raw CLI flag passed to agent")
    p_run.add_argument("--async", dest="async_run", action="store_true", help="Spawn in background immediately")
    p_run.add_argument("--wait", type=float, default=0.0, help="Wait up to N seconds when --async is set")
    p_run.add_argument("--quiet", action="store_true", help="Suppress stderr progress lines")
    p_run.add_argument("--json", action="store_true", help="Output full TaskMeta as JSON")

    p_res = sub.add_parser("resume", help="Continue a previous task in the same agent session")
    p_res.add_argument("task_id", help="Task ID returned by `agentctl run`")
    p_res.add_argument("--prompt", help="Follow-up prompt (use '-' to read from stdin)")
    p_res.add_argument("--prompt-file", help="Read follow-up prompt from a UTF-8 file")
    p_res.add_argument("--model", help="Override model for this turn")
    p_res.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    p_res.add_argument("--extra-arg", action="append", help="Extra raw CLI flag passed to agent")
    p_res.add_argument("--async", dest="async_run", action="store_true", help="Spawn in background immediately")
    p_res.add_argument("--wait", type=float, default=0.0, help="Wait up to N seconds when --async is set")
    p_res.add_argument("--quiet", action="store_true", help="Suppress stderr progress lines")
    p_res.add_argument("--json", action="store_true", help="Output full TaskMeta as JSON")

    p_ls = sub.add_parser("list", help="List recent tasks")
    p_ls.add_argument("--limit", type=int, default=20)
    p_ls.add_argument("--json", action="store_true")

    p_st = sub.add_parser("status", help="Inspect full metadata of a task")
    p_st.add_argument("task_id")
    p_st.add_argument("--json", action="store_true")

    p_delete = sub.add_parser("delete", help="Permanently delete a task")
    p_delete.add_argument("task_id")
    p_delete.add_argument("--json", action="store_true")

    p_clean = sub.add_parser("clean", help="Delete all finished tasks across workspaces")
    p_clean.add_argument("--agent", help="Only clean tasks belonging to this agent")
    p_clean.add_argument("--json", action="store_true")

    p_log = sub.add_parser("logs", help="View normalized or raw JSONL logs of a task")
    p_log.add_argument("task_id")
    p_log.add_argument("--raw", action="store_true", help="Show raw agent stdout JSONL")
    p_log.add_argument("--tail", type=int, default=50, help="Number of trailing lines (0 = all)")

    p_diff = sub.add_parser("diff", help="View git diff snapshot captured after a task")
    p_diff.add_argument("task_id")

    p_stop = sub.add_parser("stop", help="Terminate a currently running task")
    p_stop.add_argument("task_id")
    p_stop.add_argument("--json", action="store_true")

    p_reg = sub.add_parser("register-adapter", help="Register a custom CLI agent adapter via JSON spec")
    p_reg.add_argument("--spec-json", required=True, help="JSON specification string")

    p_wk = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p_wk.add_argument("spec_file", type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    store = RunStore(args.runs_dir)
    runner = AgentRunner(store)

    try:
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "run":
            return _cmd_run(args, runner)
        if args.command == "resume":
            return _cmd_resume(args, runner)
        if args.command == "list":
            return _cmd_list(args, store)
        if args.command == "status":
            return _cmd_status(args, store)
        if args.command == "delete":
            return _cmd_delete(args, store)
        if args.command == "clean":
            return _cmd_clean(args, store)
        if args.command == "logs":
            return _cmd_logs(args, store)
        if args.command == "diff":
            return _cmd_diff(args, store)
        if args.command == "stop":
            return _cmd_stop(args, runner)
        if args.command == "register-adapter":
            return _cmd_register_adapter(args)
        if args.command == "_worker":
            runner.run_worker_spec(args.spec_file)
            return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
