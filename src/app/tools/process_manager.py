from __future__ import annotations

import atexit
import os
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_current_session: ContextVar[str] = ContextVar(
    "coding_agent_tool_session",
    default="",
)


@contextmanager
def tool_session(session_id: str) -> Iterator[None]:
    """让工具知道当前正在为哪个会话执行任务。"""
    token = _current_session.set(session_id)
    try:
        yield
    finally:
        _current_session.reset(token)


def get_current_session() -> str:
    return _current_session.get()


def create_log_paths(filename: str) -> tuple[Path, Path]:
    log_dir = Path(tempfile.gettempdir()) / "coding-agent-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:10]
    safe_name = Path(filename).name.replace(" ", "_")
    return (
        log_dir / f"{run_id}_{safe_name}.stdout.log",
        log_dir / f"{run_id}_{safe_name}.stderr.log",
    )


@dataclass
class ManagedProcess:
    pid: int
    session_id: str
    filename: str
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    started_at: float


class ProcessRegistry:
    """管理 GUI 等启动后不会自行退出的子进程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, ManagedProcess] = {}

    def register(
        self,
        process: subprocess.Popen,
        *,
        session_id: str,
        filename: str,
        stdout_path: Path,
        stderr_path: Path,
    ) -> ManagedProcess:
        managed = ManagedProcess(
            pid=process.pid,
            session_id=session_id,
            filename=filename,
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=time.time(),
        )
        with self._lock:
            self._processes[process.pid] = managed
        return managed

    def stop_pid(self, pid: int) -> bool:
        with self._lock:
            managed = self._processes.pop(pid, None)
        if managed is None:
            return False
        _terminate_process_tree(managed.process)
        _remove_logs(managed)
        return True

    def stop_session(self, session_id: str) -> list[int]:
        with self._lock:
            matches = [
                pid for pid, managed in self._processes.items()
                if managed.session_id == session_id
            ]
        return [pid for pid in matches if self.stop_pid(pid)]

    def stop_all(self) -> list[int]:
        with self._lock:
            pids = list(self._processes)
        return [pid for pid in pids if self.stop_pid(pid)]


def _remove_logs(managed: ManagedProcess) -> None:
    for path in (managed.stdout_path, managed.stderr_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass

    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


process_registry = ProcessRegistry()
atexit.register(process_registry.stop_all)
