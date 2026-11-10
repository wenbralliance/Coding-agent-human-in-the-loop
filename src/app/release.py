from __future__ import annotations

import hashlib

from app.config import settings
from app.db.connection import db_session


def _recent_version_metrics(version: str) -> dict | None:
    with db_session() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(loop_detected) AS loops
            FROM task_runs
            WHERE workflow_version = ?
              AND started_at >= datetime('now', '-1 day')
            """,
            (version,),
        ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return None
    return {
        "total": total,
        "success_rate": float(row["success"] or 0) / total,
        "failure_rate": float(row["failed"] or 0) / total,
        "loop_rate": float(row["loops"] or 0) / total,
    }


def should_rollback_canary(canary: str, stable: str) -> bool:
    canary_metrics = _recent_version_metrics(canary)
    stable_metrics = _recent_version_metrics(stable)
    if not canary_metrics or canary_metrics["total"] < 20 or not stable_metrics:
        return False
    return (
        canary_metrics["success_rate"]
        < stable_metrics["success_rate"] - 0.05
        or canary_metrics["failure_rate"]
        > stable_metrics["failure_rate"] + 0.02
        or canary_metrics["loop_rate"] > stable_metrics["loop_rate"] + 0.01
    )


def workflow_version_for_session(session_id: str) -> str:
    """Stable percentage rollout without a session flipping versions per turn."""
    canary = settings.CANARY_VERSION.strip()
    if not canary or settings.CANARY_PERCENT <= 0:
        return settings.WORKFLOW_VERSION
    if should_rollback_canary(canary, settings.WORKFLOW_VERSION):
        return settings.WORKFLOW_VERSION
    bucket = int.from_bytes(
        hashlib.sha256(session_id.encode("utf-8")).digest()[:2],
        "big",
    ) % 100
    return canary if bucket < min(100, settings.CANARY_PERCENT) else settings.WORKFLOW_VERSION
