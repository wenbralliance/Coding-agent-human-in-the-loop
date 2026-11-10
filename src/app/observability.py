from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import APP_HOME, settings
from app.db.connection import db_session


logger = logging.getLogger(__name__)
_event_log_lock = threading.Lock()
_event_log_path = APP_HOME / "logs" / "agent-events.jsonl"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    session_id: str
    workflow_version: str
    started_at: float


_current_run: ContextVar[RunContext | None] = ContextVar(
    "coding_agent_current_run",
    default=None,
)


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


@contextmanager
def run_context(
    run_id: str,
    session_id: str,
    workflow_version: str,
) -> Iterator[RunContext]:
    context = RunContext(
        run_id=run_id,
        session_id=session_id,
        workflow_version=workflow_version,
        started_at=time.monotonic(),
    )
    token = _current_run.set(context)
    try:
        yield context
    finally:
        _current_run.reset(token)


def get_run_context() -> RunContext | None:
    return _current_run.get()


def _json_log(event: str, **fields) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    logger.info(line)
    try:
        _event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with _event_log_lock:
            with _event_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        logger.debug("无法写入结构化事件日志", exc_info=True)


def hash_arguments(arguments: str) -> str:
    return hashlib.sha256(arguments.encode("utf-8")).hexdigest()


def start_task_run(
    run_id: str,
    session_id: str,
    workflow_version: str,
    user_input: str,
) -> None:
    with db_session() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO task_runs
                (id, session_id, workflow_version, status, user_input_chars)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (run_id, session_id, workflow_version, len(user_input)),
        )
    _json_log(
        "task.started",
        run_id=run_id,
        session_id=session_id,
        workflow_version=workflow_version,
    )


def finish_task_run(
    run_id: str,
    *,
    status: str,
    termination_reason: str = "",
    tool_rounds: int = 0,
    todo_total: int = 0,
    todo_completed: int = 0,
    loop_detected: bool = False,
    duration_ms: int = 0,
) -> None:
    with db_session() as db:
        db.execute(
            """
            UPDATE task_runs
            SET status = ?,
                termination_reason = ?,
                tool_rounds = ?,
                todo_total = ?,
                todo_completed = ?,
                loop_detected = ?,
                duration_ms = ?,
                input_tokens = COALESCE((
                    SELECT SUM(input_tokens) FROM llm_calls
                    WHERE run_id = task_runs.id
                ), 0),
                output_tokens = COALESCE((
                    SELECT SUM(output_tokens) FROM llm_calls
                    WHERE run_id = task_runs.id
                ), 0),
                cost_usd = COALESCE((
                    SELECT SUM(cost_usd) FROM llm_calls
                    WHERE run_id = task_runs.id
                ), 0),
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                termination_reason,
                tool_rounds,
                todo_total,
                todo_completed,
                int(loop_detected),
                duration_ms,
                run_id,
            ),
        )
    _json_log(
        "task.finished",
        run_id=run_id,
        status=status,
        termination_reason=termination_reason,
        tool_rounds=tool_rounds,
        todo_total=todo_total,
        todo_completed=todo_completed,
        loop_detected=loop_detected,
        duration_ms=duration_ms,
    )


def record_tool_call(
    tool_name: str,
    arguments: str,
    *,
    status: str,
    error_code: str = "",
    attempt: int = 1,
    duration_ms: int = 0,
) -> None:
    context = get_run_context()
    args_hash = hash_arguments(arguments)
    with db_session() as db:
        db.execute(
            """
            INSERT INTO tool_call_events
                (run_id, session_id, tool_name, args_hash, attempt, status,
                 error_code, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.run_id if context else None,
                context.session_id if context else None,
                tool_name,
                args_hash,
                attempt,
                status,
                error_code or None,
                duration_ms,
            ),
        )
    _json_log(
        "tool.call",
        run_id=context.run_id if context else None,
        session_id=context.session_id if context else None,
        tool_name=tool_name,
        args_hash=args_hash,
        status=status,
        error_code=error_code or None,
        attempt=attempt,
        duration_ms=duration_ms,
    )
    if status != "ok":
        evaluate_tool_alert(status, error_code)


def record_approval_pending(
    *,
    approval_id: str,
    tool_name: str,
    action_hash: str,
    risk_level: str,
) -> None:
    context = get_run_context()
    with db_session() as db:
        existing = db.execute(
            """
            SELECT id FROM approval_events
            WHERE approval_id = ? AND decision = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (approval_id,),
        ).fetchone()
        if existing:
            return
        db.execute(
            """
            INSERT INTO approval_events
                (approval_id, session_id, run_id, tool_name, action_hash,
                 risk_level, decision)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                approval_id,
                context.session_id if context else None,
                context.run_id if context else None,
                tool_name,
                action_hash,
                risk_level,
            ),
        )
    _json_log(
        "approval.pending",
        approval_id=approval_id,
        tool_name=tool_name,
        action_hash=action_hash,
        risk_level=risk_level,
    )


def record_approval_decision(
    *,
    approval_id: str,
    tool_name: str,
    action_hash: str,
    risk_level: str,
    decision: str,
    wait_seconds: float,
) -> None:
    context = get_run_context()
    with db_session() as db:
        updated = db.execute(
            """
            UPDATE approval_events
            SET decision = ?, wait_seconds = ?, decided_at = CURRENT_TIMESTAMP
            WHERE approval_id = ? AND decision = 'pending'
            """,
            (decision, wait_seconds, approval_id),
        ).rowcount
        if not updated:
            db.execute(
                """
                INSERT INTO approval_events
                    (approval_id, session_id, run_id, tool_name, action_hash,
                     risk_level, decision, wait_seconds, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    approval_id,
                    context.session_id if context else None,
                    context.run_id if context else None,
                    tool_name,
                    action_hash,
                    risk_level,
                    decision,
                    wait_seconds,
                ),
            )
        db.execute(
            """
            INSERT INTO audit_events
                (run_id, session_id, actor, action, resource, decision, detail_json)
            VALUES (?, ?, 'local-user', 'approval', ?, ?, ?)
            """,
            (
                context.run_id if context else None,
                context.session_id if context else None,
                tool_name,
                decision,
                json.dumps(
                    {"approval_id": approval_id, "risk_level": risk_level},
                    ensure_ascii=False,
                ),
            ),
        )
    _json_log(
        "approval.decided",
        approval_id=approval_id,
        tool_name=tool_name,
        risk_level=risk_level,
        decision=decision,
        wait_seconds=round(wait_seconds, 3),
    )
    if wait_seconds > settings.ALERT_APPROVAL_WAIT_SECONDS:
        record_alert(
            "APPROVAL_WAIT_EXCEEDED",
            "warning",
            "审批等待时间超过配置阈值。",
            wait_seconds,
            float(settings.ALERT_APPROVAL_WAIT_SECONDS),
        )


def approval_pending_seconds(approval_id: str) -> float:
    with db_session() as db:
        row = db.execute(
            """
            SELECT (julianday('now') - julianday(created_at)) * 86400.0 AS age
            FROM approval_events
            WHERE approval_id = ? AND decision = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (approval_id,),
        ).fetchone()
    return max(0.0, float(row["age"] or 0)) if row else 0.0


def record_audit(
    *,
    action: str,
    resource: str,
    decision: str,
    detail: dict | None = None,
) -> None:
    context = get_run_context()
    with db_session() as db:
        db.execute(
            """
            INSERT INTO audit_events
                (run_id, session_id, actor, action, resource, decision, detail_json)
            VALUES (?, ?, 'agent', ?, ?, ?, ?)
            """,
            (
                context.run_id if context else None,
                context.session_id if context else None,
                action,
                resource,
                decision,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )


def record_alert(
    code: str,
    severity: str,
    message: str,
    value: float | None = None,
    threshold: float | None = None,
) -> None:
    context = get_run_context()
    with db_session() as db:
        db.execute(
            """
            INSERT INTO alert_events
                (code, severity, message, value, threshold, session_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                severity,
                message,
                value,
                threshold,
                context.session_id if context else None,
            ),
        )
    _json_log(
        "alert.created",
        code=code,
        severity=severity,
        message=message,
        value=value,
        threshold=threshold,
    )


def evaluate_tool_alert(status: str, error_code: str) -> None:
    with db_session() as db:
        rows = db.execute(
            """
            SELECT status FROM tool_call_events
            ORDER BY id DESC LIMIT ?
            """,
            (settings.ALERT_TOOL_WINDOW_SIZE,),
        ).fetchall()
    if not rows:
        return
    failures = sum(row["status"] != "ok" for row in rows)
    rate = failures / len(rows)
    if rate >= settings.ALERT_TOOL_FAILURE_RATE:
        record_alert(
            "TOOL_FAILURE_RATE_HIGH",
            "warning",
            "最近工具调用失败率达到阈值。",
            rate,
            settings.ALERT_TOOL_FAILURE_RATE,
        )
    if error_code == "LOOP_DETECTED":
        record_alert(
            "AGENT_LOOP_DETECTED",
            "critical",
            "重复工具调用指纹触发死循环保护。",
        )


def get_metrics_summary() -> dict:
    with db_session() as db:
        task_rows = db.execute(
            """
            SELECT status, termination_reason, loop_detected, duration_ms
            FROM task_runs
            WHERE started_at >= datetime('now', '-30 days')
            """
        ).fetchall()
        tool_rows = db.execute(
            """
            SELECT status, error_code, duration_ms
            FROM tool_call_events
            WHERE created_at >= datetime('now', '-30 days')
            """
        ).fetchall()
        alerts = db.execute(
            """
            SELECT code, severity, message, created_at
            FROM alert_events
            WHERE acknowledged = 0
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()

    tasks = len(task_rows)
    successes = sum(row["status"] == "completed" for row in task_rows)
    crashes = sum(
        row["status"] == "failed"
        or row["termination_reason"] in {"tool_error", "unhandled_error"}
        for row in task_rows
    )
    loops = sum(bool(row["loop_detected"]) for row in task_rows)
    durations = sorted(row["duration_ms"] or 0 for row in task_rows)
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else 0
    tool_total = len(tool_rows)
    tool_failures = sum(row["status"] != "ok" for row in tool_rows)

    return {
        "window_days": 30,
        "tasks": {
            "total": tasks,
            "success_rate": round(successes / tasks, 4) if tasks else 0,
            "crash_rate": round(crashes / tasks, 4) if tasks else 0,
            "loop_rate": round(loops / tasks, 4) if tasks else 0,
            "p95_duration_ms": p95,
        },
        "tools": {
            "total": tool_total,
            "failure_rate": round(tool_failures / tool_total, 4) if tool_total else 0,
        },
        "alerts": [dict(row) for row in alerts],
    }
