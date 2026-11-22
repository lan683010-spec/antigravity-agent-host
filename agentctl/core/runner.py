"""Subprocess runner with live event normalization, async background spawn, timeout, and cancellation."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from agentctl.adapters import get_adapter
from agentctl.core.store import RunStore, generate_task_id
from agentctl.core.worktree import capture_git_diff, create_worktree
from agentctl.models import (
    CommandSpec,
    NormalizedEvent,
    RunRequest,
    TaskMeta,
    TurnRecord,
    utc_now_iso,
)


SANDBOX_ZH = {
    "read-only": "🔒 只读安全模式：仅允许查看与分析代码，禁止修改任何文件",
    "workspace-write": "✍️ 工作区读写模式：允许在项目目录内修改代码并执行测试",
    "danger-full-access": "⚡ 完全权限模式：允许跨目录读写文件与执行系统命令",
}
STATUS_ZH = {"pending": "等待启动", "running": "正在执行", "completed": "已完成",
             "failed": "执行失败", "timeout": "执行超时", "cancelled": "已取消"}
ZH_REPLY_INSTRUCTION = (
    "\n\n[主控输出要求]\n请用简体中文汇报最终结果。在不违反指定输出格式或 JSON schema 的前提下，"
    "先以【最终回复中文解读】开头，用一小段通俗中文说明完成了什么、修改的文件、验证结果和未完成事项，"
    "再以【原始详细输出】开头给出详细信息。不要只返回命令；代码、路径和标识符保持原样。"
    "若必须输出 JSON，请在允许的说明字段中使用简体中文，不添加 schema 未允许的字段。"
)


def command_summary_zh(meta: dict[str, Any], prompt: str | None = None,
                       turn_index: int | None = None) -> str:
    turns = meta.get("turns") or []
    index = turn_index or max(1, len(turns) + (meta.get("status") == "running"))
    task = prompt if prompt is not None else meta.get("prompt", "")
    argv = meta.get("command_argv") or []
    permission = SANDBOX_ZH.get(meta.get("sandbox"), "未记录沙箱模式")
    actual = ""
    if any(x in argv for x in ("--dangerously-bypass-approvals-and-sandbox",
                               "--dangerously-skip-permissions", "--yolo")):
        actual = "实际命令已启用跳过审批选项，请以实际 CLI 权限设置为准。"
    if "disabled" in argv and "--sandbox" in argv:
        actual += "实际 CLI 沙箱已关闭，所选工作区模式不代表系统强制隔离。"
    return (
        f"主控 Agent（{meta.get('dispatcher') or 'Antigravity Main Agent'}）向子 Agent "
        f"{meta.get('agent', '未知')} 下达任务：{task or '未记录任务内容'}\n"
        f"当前是第 {index} 轮会话，{'继续已有会话' if index > 1 else '新建会话'}。\n"
        f"工作目录：{meta.get('effective_cwd') or meta.get('cwd') or '未记录'}\n"
        f"所选权限：{permission}。{actual}\n"
        "运行方式：通过命令行子进程执行，自动收集过程记录与最终回复。"
        + ("本轮在独立 Git 工作树中运行。" if meta.get("worktree_path") else "")
    )


def reply_summary_zh(reply: str | None) -> str:
    """Prefer the agent's Chinese explanation; never invent a translation of unknown prose."""
    if not reply or not reply.strip():
        return "尚未收到最终回复。"
    match = re.search(r"【最终回复中文解读】\s*(.*?)(?:【原始详细输出】|$)", reply, re.S)
    text = match.group(1).strip() if match else reply.strip()
    # Exclude code blocks from the plain-language synopsis.
    prose = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    chinese = [line.strip() for line in prose.splitlines() if re.search(r"[\u4e00-\u9fff]", line)]
    if chinese:
        summary = "\n".join(chinese[:8])
        return summary[:1200] + ("…（完整回复见下方）" if len(summary) > 1200 or len(chinese) > 8 else "")
    # Conservative translations of common CLI-only responses, including negative outcomes.
    findings = []
    for pattern, meaning in (
        (r"\b(?:no files? (?:were )?(?:changed|modified)|no changes(?: made)?)\b", "回复表示没有修改文件。"),
        (r"\b(?:all tests passed|tests? (?:have )?passed|[1-9]\d* passed)\b", "回复报告有测试通过；具体范围见原始输出。"),
        (r"\b(?:tests? failed|[1-9]\d* failed|not all tests passed)\b", "回复报告有测试失败，需要查看失败详情。"),
        (r"\b(?:permission denied|access denied)\b", "操作因权限不足被拒绝。"),
        (r"\b(?:file not found|no such file or directory)\b", "未找到所需的文件或目录。"),
        (r"\b(?:timed out|timeout)\b", "操作超时，未能在规定时间内完成。"),
    ):
        if "有测试通过" in meaning and re.search(r"\b(?:not|no|never|0)\b", prose, re.I):
            continue
        if re.search(pattern, prose, re.I):
            findings.append(meaning)
    if findings:
        return "".join(findings) + "（根据常见返回语句提炼）"
    return "已收到回复，但其中没有可提取的中文说明；当前离线规则无法可靠翻译这段内容，请查看下方原文。"


def event_counts(events: list[dict[str, Any]]) -> tuple[int, int]:
    """Count stable item IDs once per turn, not each streamed update."""
    seen: set[tuple[Any, ...]] = set()
    pending: dict[tuple[Any, ...], int] = {}
    counts = {"thinking": 0, "tool_call": 0}
    turn = 0
    for event in events:
        if event.get("subtype") == "dispatch":
            turn += 1
        kind = event.get("kind")
        if kind not in counts:
            continue
        raw = event.get("raw") or {}
        item = raw.get("item") or {}
        identity = item.get("id") or raw.get("call_id") or raw.get("tool_call_id") or raw.get("id")
        if identity:
            key = (turn, kind, str(identity))
            if key in seen:
                continue
            seen.add(key)
        else:
            phase = event.get("subtype")
            key = (turn, kind, event.get("tool_name"))
            if phase in ("delta", "updated", "item.updated"):
                continue
            if phase in ("started", "item.started"):
                pending[key] = pending.get(key, 0) + 1
            elif phase in ("completed", "item.completed") and pending.get(key, 0):
                pending[key] -= 1
                continue
        counts[kind] += 1
    return counts["thinking"], counts["tool_call"]


def result_summary_zh(meta: dict[str, Any], events: list[dict[str, Any]],
                      diff: str | None, *, partial: bool = False) -> str:
    thinking, tools = event_counts(events)
    duration = meta.get("duration_ms")
    elapsed = f"{duration / 1000:.1f} 秒" if duration is not None else "暂未记录"
    files = []
    in_status = False
    for line in (diff or "").splitlines():
        if line == "# git status --short":
            in_status = True
        elif line.startswith("# "):
            in_status = False
        elif in_status and line.strip():
            # capture_git_diff strips the first status line's leading space.
            path = re.sub(r"^[ MADRCU?!]{1,2}\s+", "", line).strip()
            if path not in files:
                files.append(path)
    file_summary = ("工作区变更文件：" + "、".join(files) + "（Git 快照可能含任务前已有改动）。"
                    if files else "未在 Git 快照中发现变更；这不代表非 Git 文件一定未被修改。")
    if diff is None:
        file_summary = "未取得 Git 快照，无法据此确认修改文件。"
    event_files = []
    for event in events:
        item = (event.get("raw") or {}).get("item") or {}
        if item.get("type") == "file_change" and item.get("status") == "completed":
            for change in item.get("changes") or []:
                path = change.get("path")
                if path and path not in event_files:
                    event_files.append(path)
    if event_files:
        file_summary += "\n工具报告已完成变更的文件（会话累计）：" + "、".join(event_files) + "。"
    scope = "当前可见事件中" if partial else "整个会话累计"
    overview = (f"执行概况：子 Agent {meta.get('agent', '未知')} "
                f"{STATUS_ZH.get(meta.get('status'), '状态未知')}；本轮耗时：{elapsed}。\n"
                f"{scope}记录思考 {thinking} 次、工具调用 {tools} 次（有标识的事件已去重）。\n"
                f"{file_summary}\n")
    if meta.get("status") in ("running", "pending"):
        return overview + "最终回复中文解读：任务尚未结束，完成后会自动生成解读。"
    if meta.get("error"):
        overview += "异常解读：" + reply_summary_zh(str(meta["error"])) + "\n"
    return overview + "最终回复中文解读：" + reply_summary_zh(meta.get("final_response"))


def kill_process_tree(pid: int) -> bool:
    """Force-terminate a process and its children on Windows or POSIX."""
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            shell=False,
        )
        return proc.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


class AgentRunner:
    """Executes headless coding agent runs and persists structured telemetry."""

    def __init__(self, store: RunStore | None = None) -> None:
        self.store = store or RunStore()

    def _prepare_new_task(self, request: RunRequest) -> tuple[TaskMeta, CommandSpec]:
        adapter = get_adapter(request.agent)
        cwd_path = Path(request.cwd).expanduser().resolve()
        if not cwd_path.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {cwd_path}")

        task_id = generate_task_id(adapter.name)
        worktree_path: str | None = None
        worktree_branch: str | None = None
        effective_cwd = str(cwd_path)

        if request.use_worktree:
            wt_dir, wt_branch = create_worktree(
                cwd_path,
                task_id,
                self.store.root,
                base_ref=request.worktree_base,
            )
            worktree_path = str(wt_dir)
            worktree_branch = wt_branch
            effective_cwd = worktree_path

        final_msg_path = self.store.final_message_path(task_id)
        cmd = adapter.build_run_command(
            replace(request, prompt=request.prompt + ZH_REPLY_INSTRUCTION),
            effective_cwd=effective_cwd,
            final_message_path=final_msg_path,
        )

        now = utc_now_iso()
        meta = TaskMeta(
            task_id=task_id,
            agent=adapter.name,
            status="running",
            cwd=str(cwd_path),
            effective_cwd=effective_cwd,
            sandbox=request.sandbox,
            prompt=request.prompt,
            model=request.model,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            created_at=now,
            updated_at=now,
            title=request.title,
            dispatcher=request.dispatcher or "Antigravity Main Agent",
            command_argv=cmd.argv,
        )
        meta.command_summary_zh = command_summary_zh(meta.to_dict(), turn_index=1)
        self.store.save_meta(meta)
        return meta, cmd

    def run_task(
        self,
        request: RunRequest,
        *,
        on_event: Callable[[NormalizedEvent], None] | None = None,
    ) -> TaskMeta:
        meta, cmd = self._prepare_new_task(request)
        return self._execute_turn(
            meta=meta,
            prompt=request.prompt,
            cmd=cmd,
            timeout_sec=request.timeout_sec,
            on_event=on_event,
        )

    def start_task_async(
        self,
        request: RunRequest,
        *,
        wait_sec: float = 0.0,
    ) -> TaskMeta:
        """Spawn a detached worker process to run the task in the background."""
        meta, cmd = self._prepare_new_task(request)
        spec_path = self.store.task_dir(meta.task_id) / "worker_spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "task_id": meta.task_id,
                    "prompt": request.prompt,
                    "timeout_sec": request.timeout_sec,
                    "cmd": {
                        "argv": cmd.argv,
                        "stdin_text": cmd.stdin_text,
                        "env_overrides": cmd.env_overrides,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._spawn_detached_worker(meta.task_id, spec_path)
        if wait_sec > 0:
            return self.wait_for_task(meta.task_id, wait_sec=wait_sec)
        return self.store.load_meta(meta.task_id)

    def resume_task(
        self,
        task_id: str,
        prompt: str,
        *,
        timeout_sec: int = 600,
        model: str | None = None,
        extra_args: list[str] | None = None,
        on_event: Callable[[NormalizedEvent], None] | None = None,
    ) -> TaskMeta:
        meta, cmd = self._prepare_resume_task(
            task_id,
            prompt,
            model=model,
            extra_args=extra_args,
        )
        return self._execute_turn(
            meta=meta,
            prompt=prompt,
            cmd=cmd,
            timeout_sec=timeout_sec,
            on_event=on_event,
        )

    def resume_task_async(
        self,
        task_id: str,
        prompt: str,
        *,
        timeout_sec: int = 600,
        model: str | None = None,
        extra_args: list[str] | None = None,
        wait_sec: float = 0.0,
    ) -> TaskMeta:
        meta, cmd = self._prepare_resume_task(
            task_id,
            prompt,
            model=model,
            extra_args=extra_args,
        )
        spec_path = self.store.task_dir(meta.task_id) / "worker_spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "task_id": meta.task_id,
                    "prompt": prompt,
                    "timeout_sec": timeout_sec,
                    "cmd": {
                        "argv": cmd.argv,
                        "stdin_text": cmd.stdin_text,
                        "env_overrides": cmd.env_overrides,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._spawn_detached_worker(meta.task_id, spec_path)
        if wait_sec > 0:
            return self.wait_for_task(meta.task_id, wait_sec=wait_sec)
        return self.store.load_meta(meta.task_id)

    def _prepare_resume_task(
        self,
        task_id: str,
        prompt: str,
        *,
        model: str | None = None,
        extra_args: list[str] | None = None,
    ) -> tuple[TaskMeta, CommandSpec]:
        meta = self.store.load_meta(task_id)
        if not meta.session_id:
            raise RuntimeError(
                f"Task '{task_id}' has no recorded session_id and cannot be resumed."
            )

        adapter = get_adapter(meta.agent)
        effective_model = model or meta.model
        final_msg_path = self.store.final_message_path(task_id)
        cmd = adapter.build_resume_command(
            meta.session_id,
            prompt + ZH_REPLY_INSTRUCTION,
            effective_cwd=meta.effective_cwd,
            sandbox=meta.sandbox,
            model=effective_model,
            final_message_path=final_msg_path,
            extra_args=extra_args,
        )

        meta.status = "running"
        meta.updated_at = utc_now_iso()
        meta.error = None
        meta.prompt = prompt
        meta.model = effective_model
        meta.final_response = None
        meta.result_summary_zh = None
        meta.duration_ms = None
        meta.exit_code = None
        meta.command_argv = cmd.argv
        meta.command_summary_zh = command_summary_zh(meta.to_dict())
        self.store.save_meta(meta)
        return meta, cmd

    def _spawn_detached_worker(self, task_id: str, spec_path: Path) -> None:
        package_parent = str(Path(__file__).resolve().parent.parent.parent)
        env = {
            **os.environ,
            "PYTHONPATH": package_parent
            + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        }
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentctl",
                "--runs-dir",
                str(self.store.resolve_task_root(task_id)),
                "_worker",
                str(spec_path),
            ],
            cwd=package_parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            close_fds=True,
        )

    def run_worker_spec(self, spec_file: Path) -> TaskMeta:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        task_id = spec["task_id"]
        meta = self.store.load_meta(task_id)
        cmd_data = spec["cmd"]
        cmd = CommandSpec(
            argv=list(cmd_data["argv"]),
            stdin_text=cmd_data.get("stdin_text"),
            env_overrides=dict(cmd_data.get("env_overrides") or {}),
        )
        return self._execute_turn(
            meta=meta,
            prompt=spec["prompt"],
            cmd=cmd,
            timeout_sec=int(spec.get("timeout_sec", 600)),
            on_event=None,
        )

    def wait_for_task(self, task_id: str, *, wait_sec: float = 15.0) -> TaskMeta:
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            meta = self.store.load_meta(task_id)
            if meta.status != "running":
                return meta
            time.sleep(0.35)
        return self.store.load_meta(task_id)

    def stop_task(self, task_id: str) -> TaskMeta:
        meta = self.store.load_meta(task_id)
        if meta.pid is not None and meta.status == "running":
            kill_process_tree(meta.pid)
        if meta.status == "running":
            meta.status = "cancelled"
            meta.error = "Stopped by user / orchestrator"
            meta.pid = None
            meta.updated_at = utc_now_iso()
            meta.result_summary_zh = result_summary_zh(
                meta.to_dict(), self.store.read_events(task_id, limit=0), self.store.read_diff(task_id))
            self.store.save_meta(meta)
        return meta

    def _execute_turn(
        self,
        *,
        meta: TaskMeta,
        prompt: str,
        cmd: CommandSpec,
        timeout_sec: int,
        on_event: Callable[[NormalizedEvent], None] | None,
    ) -> TaskMeta:
        adapter = get_adapter(meta.agent)
        task_id = meta.task_id
        turn = TurnRecord(
            turn_index=len(meta.turns) + 1,
            prompt=prompt,
            started_at=utc_now_iso(),
            command_argv=cmd.argv,
        )
        start_perf = time.perf_counter()

        # Emit an initial dispatch event so the live stream immediately records the command
        dispatch_ev = NormalizedEvent(
            kind="init",
            subtype="dispatch",
            text=meta.command_summary_zh or command_summary_zh(meta.to_dict(), prompt, turn.turn_index),
            model=meta.model,
            raw={
                "dispatcher": meta.dispatcher or "Antigravity Main Agent",
                "command_argv": cmd.argv,
                "cwd": meta.effective_cwd,
                "prompt": prompt,
            },
        )
        self.store.append_event(task_id, dispatch_ev)

        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            **cmd.env_overrides,
        }

        final_msg_path = self.store.final_message_path(task_id)
        if final_msg_path.exists():
            final_msg_path.unlink(missing_ok=True)

        try:
            proc = subprocess.Popen(
                cmd.argv,
                cwd=meta.effective_cwd,
                stdin=subprocess.PIPE if cmd.stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                shell=False,
            )
        except OSError as exc:
            meta.status = turn.status = "failed"
            meta.error = turn.error = f"子 Agent 进程启动失败：{exc}"
            meta.duration_ms = turn.duration_ms = int((time.perf_counter() - start_perf) * 1000)
            meta.updated_at = turn.finished_at = utc_now_iso()
            meta.pid = None
            meta.turns.append(turn.__dict__)
            self.store.append_event(task_id, NormalizedEvent(kind="error", text=meta.error, is_error=True))
            meta.result_summary_zh = result_summary_zh(
                meta.to_dict(), self.store.read_events(task_id, limit=0), None)
            self.store.save_meta(meta)
            return meta

        meta.pid = proc.pid
        meta.command_argv = cmd.argv
        meta.updated_at = utc_now_iso()
        self.store.save_meta(meta)

        if cmd.stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(cmd.stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        stderr_lines: list[str] = []
        stderr_path = self.store.stderr_log_path(task_id)

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            with stderr_path.open("a", encoding="utf-8", errors="replace") as err_fh:
                for err_line in proc.stderr:
                    stderr_lines.append(err_line)
                    err_fh.write(err_line)
                    err_fh.flush()

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            kill_process_tree(proc.pid)

        timer = threading.Timer(timeout_sec, _on_timeout)
        timer.daemon = True
        timer.start()

        last_assistant_text: str | None = None
        result_text: str | None = None
        last_error: str | None = None
        saw_error_event = False
        usage: dict[str, object] | None = None

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                self.store.append_raw(task_id, raw_line)
                event = adapter.parse_stdout_line(raw_line)
                if event is None:
                    continue

                self.store.append_event(task_id, event)
                if event.session_id and not meta.session_id:
                    meta.session_id = event.session_id
                    self.store.save_meta(meta)
                if event.model and not meta.model:
                    meta.model = event.model

                if event.kind == "assistant_message" and event.text:
                    last_assistant_text = event.text
                elif event.kind == "result":
                    if event.text:
                        result_text = event.text
                    if event.usage:
                        usage = event.usage
                elif event.kind == "error":
                    saw_error_event = True
                    if event.text:
                        last_error = event.text

                if on_event is not None:
                    on_event(event)
        finally:
            exit_code = proc.wait()
            timer.cancel()
            stderr_thread.join(timeout=5)
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

        duration_ms = int((time.perf_counter() - start_perf) * 1000)

        file_final = (
            final_msg_path.read_text(encoding="utf-8", errors="replace").strip()
            if final_msg_path.is_file()
            else ""
        )
        final_response = file_final or result_text or last_assistant_text or ""
        if final_response:
            self.store.write_final_message(task_id, final_response)

        try:
            diff_text = capture_git_diff(meta.effective_cwd)
        except OSError:
            # Missing Git must not prevent completion or the Chinese result summary.
            diff_text = None
        if diff_text is not None:
            self.store.diff_path(task_id).write_text(diff_text, encoding="utf-8")

        latest_meta = self.store.load_meta(task_id)
        if timed_out.is_set():
            status = "timeout"
            error_msg = f"Timed out after {timeout_sec}s"
        elif latest_meta.status == "cancelled" or meta.status == "cancelled":
            status = "cancelled"
            error_msg = latest_meta.error or meta.error
        elif exit_code != 0 or saw_error_event:
            status = "failed"
            stderr_tail = "".join(stderr_lines[-20:]).strip()
            error_msg = last_error or stderr_tail or f"Process exited with code {exit_code}"
        else:
            status = "completed"
            error_msg = None

        finished_at = utc_now_iso()
        turn.finished_at = finished_at
        turn.exit_code = exit_code
        turn.status = status
        turn.duration_ms = duration_ms
        turn.final_response = final_response or None
        turn.error = error_msg
        turn.usage = usage

        meta.status = status
        meta.pid = None
        meta.exit_code = exit_code
        meta.duration_ms = duration_ms
        meta.final_response = final_response or None
        meta.error = error_msg
        meta.usage = usage
        meta.updated_at = finished_at
        meta.turns.append(turn.__dict__)
        meta.result_summary_zh = result_summary_zh(
            meta.to_dict(), self.store.read_events(task_id, limit=0), diff_text)
        self.store.save_meta(meta)
        return meta
