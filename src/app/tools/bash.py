from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.config import settings
from app.observability import (
    approval_pending_seconds,
    record_approval_decision,
    record_approval_pending,
    record_audit,
)
from app.security import CommandAssessment, assess_bash_command
from app.tools.base import tool
from app.tools.file import get_workspace_dir
from app.tools.process_manager import (
    _terminate_process_tree,
    get_current_session,
)


class BashUnavailableError(RuntimeError):
    pass


class BashTimeoutError(RuntimeError):
    pass


class BashSessionLostError(RuntimeError):
    pass


def find_bash_executable() -> Path:
    configured = (
        settings.BASH_EXECUTABLE.strip()
        or os.environ.get("CODING_AGENT_BASH_EXECUTABLE", "").strip()
    )
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    for command in ("bash.exe", "bash"):
        discovered = shutil.which(command)
        if discovered:
            candidates.append(Path(discovered))

    roots = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git",
    )
    for root in roots:
        candidates.extend(
            (
                root / "bin" / "bash.exe",
                root / "usr" / "bin" / "bash.exe",
            )
        )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise BashUnavailableError(
        "未找到 Git Bash。请安装 Git for Windows，或通过 BASH_EXECUTABLE 指定 bash.exe。"
    )


def _bash_env(workspace: Path, executable: Path) -> dict[str, str]:
    allowed = {
        "SystemRoot",
        "ComSpec",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed
    }

    git_root = executable.parent.parent
    path_parts = [
        git_root / "mingw64" / "bin",
        git_root / "usr" / "bin",
        git_root / "bin",
    ]
    path_parts.extend(
        Path(part)
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part
    )
    deduplicated_path: list[str] = []
    seen: set[str] = set()
    for part in path_parts:
        key = str(part).lower()
        try:
            exists = part.exists()
        except OSError:
            exists = False
        if key not in seen and exists:
            seen.add(key)
            deduplicated_path.append(str(part))

    env.update(
        {
            "PATH": os.pathsep.join(deduplicated_path),
            "HOME": str(workspace),
            "PWD": str(workspace),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_NO_INPUT": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CODING_AGENT_WORKSPACE": str(workspace),
            "CODING_AGENT_SHELL": "git-bash",
        }
    )
    return env


def _decode_output(value: str) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except Exception:
        return ""


@dataclass
class BashResult:
    status: str
    exit_code: int
    cwd: str
    stdout: str
    stderr: str
    duration_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "cwd": self.cwd,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "duration_ms": self.duration_ms,
            **({"message": self.message} if self.message else {}),
        }


class PersistentBashSession:
    def __init__(self, workspace: Path, executable: Path | None = None):
        self.workspace = workspace.resolve()
        self.executable = executable or find_bash_executable()
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        self.process = subprocess.Popen(
            [str(self.executable), "--noprofile", "--norc"],
            cwd=str(self.workspace),
            env=_bash_env(self.workspace, self.executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self.last_used = time.monotonic()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._queue.put(("line", line.rstrip("\r\n")))
        except Exception as exc:
            self._queue.put(("reader_error", str(exc)))
        finally:
            self._queue.put(("eof", None))

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def execute(self, command: str, timeout: float) -> BashResult:
        if not self.alive:
            raise BashSessionLostError("Bash 会话已经退出，将在下一次调用时自动重建。")
        if self.process.stdin is None:
            raise BashSessionLostError("Bash 会话 stdin 不可用。")

        started = time.monotonic()
        nonce = uuid.uuid4().hex
        max_bytes = max(1000, settings.BASH_OUTPUT_MAX_BYTES)
        script = f"""
set +e
set +u
__ca_out="$(mktemp 2>/dev/null || mktemp -t coding-agent)"
__ca_err="$(mktemp 2>/dev/null || mktemp -t coding-agent)"
__ca_cwd="$(mktemp 2>/dev/null || mktemp -t coding-agent)"
{{
{command}
}} > "$__ca_out" 2> "$__ca_err"
__ca_status=$?
pwd -P > "$__ca_cwd" 2>/dev/null
__ca_out_size=$(wc -c < "$__ca_out" | tr -d ' ')
__ca_err_size=$(wc -c < "$__ca_err" | tr -d ' ')
printf '\\n__CA_DONE_{nonce}__:%s\\n' "$__ca_status"
printf '__CA_CWD_{nonce}__:'
pwd -P | base64 | tr -d '\\n'
printf '\\n'
printf '__CA_OUT_SIZE_{nonce}__:%s\\n' "$__ca_out_size"
printf '__CA_ERR_SIZE_{nonce}__:%s\\n' "$__ca_err_size"
printf '__CA_OUT_{nonce}__:'
head -c {max_bytes} "$__ca_out" | base64 | tr -d '\\n'
printf '\\n'
printf '__CA_ERR_{nonce}__:'
head -c {max_bytes} "$__ca_err" | base64 | tr -d '\\n'
printf '\\n'
rm -f "$__ca_out" "$__ca_err" "$__ca_cwd"
"""
        try:
            self.process.stdin.write(script)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise BashSessionLostError(f"Bash 会话写入失败: {exc}") from exc

        deadline = time.monotonic() + max(1.0, timeout)
        protocol: dict[str, str] = {}
        done_marker = f"__CA_DONE_{nonce}__:"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise BashTimeoutError(
                    f"Bash 命令超过 {timeout:.0f} 秒，Shell 已终止。"
                )
            try:
                event, value = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if event == "eof":
                self.close()
                raise BashSessionLostError(
                    "Bash 会话意外退出，通常是命令调用了 exit 或 Shell 解析失败。"
                )
            if event == "reader_error":
                self.close()
                raise BashSessionLostError(f"Bash 输出读取失败: {value}")
            if value is None:
                continue
            line = value
            if line.startswith(done_marker):
                protocol["exit_code"] = line[len(done_marker):]
                break
            if not line.startswith("__CA_"):
                continue

        prefixes = {
            f"__CA_CWD_{nonce}__:": "cwd",
            f"__CA_OUT_SIZE_{nonce}__:": "out_size",
            f"__CA_ERR_SIZE_{nonce}__:": "err_size",
            f"__CA_OUT_{nonce}__:": "stdout",
            f"__CA_ERR_{nonce}__:": "stderr",
        }
        while len(protocol) < 6:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise BashTimeoutError("Bash 命令完成但输出协议读取超时。")
            try:
                event, value = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if event == "eof":
                self.close()
                raise BashSessionLostError("Bash 会话在读取输出时退出。")
            if value is None:
                continue
            for prefix, key in prefixes.items():
                if value.startswith(prefix):
                    protocol[key] = value[len(prefix):]
                    break

        self.last_used = time.monotonic()
        exit_code = int(protocol.get("exit_code", "1") or 1)
        out_size = int(protocol.get("out_size", "0") or 0)
        err_size = int(protocol.get("err_size", "0") or 0)
        return BashResult(
            status="completed" if exit_code == 0 else "runtime_error",
            exit_code=exit_code,
            cwd=(
                _decode_output(protocol.get("cwd", "")).strip()
                or str(self.workspace)
            ),
            stdout=_decode_output(protocol.get("stdout", "")),
            stderr=_decode_output(protocol.get("stderr", "")),
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_truncated=out_size > max_bytes,
            stderr_truncated=err_size > max_bytes,
        )

    def close(self) -> None:
        if self.process.poll() is None:
            _terminate_process_tree(self.process)


class BashSessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, PersistentBashSession] = {}

    def _close_old_sessions(self) -> None:
        now = time.monotonic()
        idle_limit = max(60, settings.BASH_SESSION_IDLE_SECONDS)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_used > idle_limit or not session.alive
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session:
                session.close()

        while len(self._sessions) >= max(1, settings.MAX_BASH_SESSIONS):
            oldest = min(
                self._sessions,
                key=lambda key: self._sessions[key].last_used,
            )
            self._sessions.pop(oldest).close()

    def execute(
        self,
        *,
        session_id: str,
        command: str,
        workspace: Path,
        parallel: bool,
        timeout: float,
    ) -> BashResult:
        if parallel:
            session = PersistentBashSession(workspace)
            try:
                return session.execute(command, timeout)
            finally:
                session.close()

        with self._lock:
            self._close_old_sessions()
            session = self._sessions.get(session_id)
            if session is None or not session.alive:
                session = PersistentBashSession(workspace)
                self._sessions[session_id] = session
            try:
                return session.execute(command, timeout)
            except BashSessionLostError:
                self._sessions.pop(session_id, None)
                session.close()
                raise

    def close(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            session.close()
            return True

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "count": len(self._sessions),
                "sessions": {
                    key: {
                        "alive": value.alive,
                        "cwd": value.workspace.as_posix(),
                    }
                    for key, value in self._sessions.items()
                },
            }


bash_sessions = BashSessionManager()


class BashExecArgs(BaseModel):
    command: str = Field(
        min_length=1,
        max_length=50_000,
        description="在同一个 Git Bash 会话中执行的完整命令或脚本。",
    )
    cwd: str = Field(
        default="",
        description="相对 workspace 的工作目录；留空保持当前目录。",
    )
    parallel: bool = Field(
        default=False,
        description=(
            "仅在命令只读且与其他调用互不依赖时设为 true。"
            "并行调用使用独立 Shell。"
        ),
    )
    todo_id: str = Field(
        default="",
        description="本次命令对应的 todo 步骤 ID；并行时需要提供。",
    )
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=600,
        description="本次 Bash 调用超时时间。",
    )


def _resolve_cwd(raw_cwd: str, workspace: Path) -> Path:
    if not raw_cwd.strip():
        return workspace
    candidate = (workspace / raw_cwd).resolve()
    if not candidate.is_relative_to(workspace):
        raise ValueError("cwd 不能位于 workspace 之外")
    if not candidate.is_dir():
        raise ValueError(f"cwd 不存在: {raw_cwd}")
    return candidate


def _result_json(result: BashResult) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False)


async def _approve_if_needed(assessment: CommandAssessment) -> tuple[bool, str]:
    if assessment.blocked:
        return False, "blocked"
    if not assessment.needs_approval:
        return True, "automatic"

    record_approval_pending(
        approval_id=assessment.approval_id,
        tool_name="bash_exec",
        action_hash=assessment.approval_id,
        risk_level=assessment.risk_level,
    )
    response = interrupt(assessment.as_approval_payload())
    decision = response.get("action") if isinstance(response, dict) else "reject"
    elapsed = approval_pending_seconds(assessment.approval_id)
    record_approval_decision(
        approval_id=assessment.approval_id,
        tool_name="bash_exec",
        action_hash=assessment.approval_id,
        risk_level=assessment.risk_level,
        decision="approved" if decision == "approve" else "rejected",
        wait_seconds=elapsed,
    )
    return decision == "approve", "approved" if decision == "approve" else "rejected"


@tool(
    BashExecArgs,
    "使用持久 Git Bash 会话执行代码修改、搜索、构建和测试。一次调用可执行多行脚本；"
    "删除、Git 破坏性操作、代码执行、网络和非零风险命令会触发审批。"
    "无依赖且只读的多条调用可在同一轮设置 parallel=true 并行执行。",
    timeout=610,
)
async def bash_exec(
    command: str,
    cwd: str = "",
    parallel: bool = False,
    todo_id: str = "",
    timeout_seconds: int = 60,
) -> str:
    workspace = get_workspace_dir().resolve()
    task_cwd = _resolve_cwd(cwd, workspace)
    assessment = assess_bash_command(command, task_cwd, workspace)

    if assessment.blocked:
        record_audit(
            action="bash_exec",
            resource=task_cwd.as_posix(),
            decision="blocked",
            detail={"risk_level": assessment.risk_level, "approval_id": assessment.approval_id},
        )
        return json.dumps(
            {
                "status": "permission_denied",
                "cwd": str(task_cwd),
                "risk_level": assessment.risk_level,
                "findings": [finding.__dict__ for finding in assessment.findings],
            },
            ensure_ascii=False,
        )

    allowed, decision = await _approve_if_needed(assessment)
    if not allowed:
        return json.dumps(
            {
                "status": "rejected",
                "cwd": str(task_cwd),
                "risk_level": assessment.risk_level,
                "message": "用户拒绝或安全策略阻止了本次 Bash 执行。",
            },
            ensure_ascii=False,
        )

    effective_parallel = bool(
        parallel
        and not assessment.blocked
        and not assessment.needs_approval
        and assessment.risk_level in {"low", "medium"}
        and (assessment.parallel_safe or bool(todo_id))
    )
    effective_command = command
    if cwd.strip():
        quoted = shlex_quote_git_bash(str(task_cwd))
        effective_command = f"cd -- {quoted} || exit 125\n{command}"
    session_id = get_current_session() or "unknown"
    try:
        result = await asyncio.to_thread(
            bash_sessions.execute,
            session_id=session_id,
            command=effective_command,
            workspace=workspace,
            parallel=effective_parallel,
            timeout=float(min(timeout_seconds, settings.BASH_TIMEOUT)),
        )
    except BashUnavailableError as exc:
        return json.dumps(
            {"status": "bash_unavailable", "message": str(exc)},
            ensure_ascii=False,
        )
    except BashTimeoutError as exc:
        return json.dumps(
            {"status": "timeout", "message": str(exc)},
            ensure_ascii=False,
        )
    except BashSessionLostError as exc:
        return json.dumps(
            {"status": "session_lost", "message": str(exc)},
            ensure_ascii=False,
        )

    record_audit(
        action="bash_exec",
        resource=str(task_cwd),
        decision=decision,
        detail={
            "approval_id": assessment.approval_id,
            "risk_level": assessment.risk_level,
            "parallel": effective_parallel,
            "todo_id": todo_id or None,
        },
    )
    payload = result.as_dict()
    payload["risk_level"] = assessment.risk_level
    payload["parallel"] = effective_parallel
    return json.dumps(payload, ensure_ascii=False)


def shlex_quote_git_bash(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


class BashResetArgs(BaseModel):
    pass


@tool(BashResetArgs, "关闭当前会话的持久 Bash Shell，下一次调用会自动重建。")
def bash_reset() -> str:
    session_id = get_current_session() or "unknown"
    closed = bash_sessions.close(session_id)
    return json.dumps(
        {"status": "closed" if closed else "not_running", "session_id": session_id},
        ensure_ascii=False,
    )
