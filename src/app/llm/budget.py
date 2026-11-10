from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.db.connection import db_session


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetSnapshot:
    session_tokens: int
    session_cost_usd: float
    daily_cost_usd: float
    token_ratio: float
    daily_cost_ratio: float
    degraded: bool


def get_budget_snapshot(session_id: str | None) -> BudgetSnapshot:
    with db_session() as db:
        session_row = db.execute(
            """
            SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_usd), 0) AS cost
            FROM llm_calls
            WHERE session_id = ?
              AND created_at >= datetime('now', '-1 day')
            """,
            (session_id,),
        ).fetchone()
        daily_row = db.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS cost
            FROM llm_calls
            WHERE created_at >= date('now')
            """
        ).fetchone()

    session_tokens = int(session_row["tokens"] or 0)
    session_cost = float(session_row["cost"] or 0)
    daily_cost = float(daily_row["cost"] or 0)
    token_ratio = (
        session_tokens / settings.LLM_SESSION_TOKEN_BUDGET
        if settings.LLM_SESSION_TOKEN_BUDGET > 0
        else 0.0
    )
    daily_cost_ratio = (
        daily_cost / settings.LLM_DAILY_COST_BUDGET_USD
        if settings.LLM_DAILY_COST_BUDGET_USD > 0
        else 0.0
    )
    return BudgetSnapshot(
        session_tokens=session_tokens,
        session_cost_usd=session_cost,
        daily_cost_usd=daily_cost,
        token_ratio=token_ratio,
        daily_cost_ratio=daily_cost_ratio,
        degraded=max(token_ratio, daily_cost_ratio)
        >= settings.LLM_BUDGET_DEGRADE_RATIO,
    )


def enforce_budget(session_id: str | None) -> BudgetSnapshot:
    snapshot = get_budget_snapshot(session_id)
    if (
        settings.LLM_SESSION_TOKEN_BUDGET > 0
        and snapshot.session_tokens >= settings.LLM_SESSION_TOKEN_BUDGET
    ):
        raise BudgetExceededError(
            "当前会话 Token 预算已用尽，任务已安全暂停。"
        )
    if (
        settings.LLM_DAILY_COST_BUDGET_USD > 0
        and snapshot.daily_cost_usd >= settings.LLM_DAILY_COST_BUDGET_USD
    ):
        raise BudgetExceededError(
            "今日模型成本预算已用尽，任务已安全暂停。"
        )
    return snapshot


def choose_model(session_id: str | None, primary_model: str) -> str:
    snapshot = enforce_budget(session_id)
    if snapshot.degraded and settings.LLM_FALLBACK_MODEL.strip():
        return settings.LLM_FALLBACK_MODEL.strip()
    return primary_model
