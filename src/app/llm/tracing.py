from __future__ import annotations

import os

from app.config import settings


def configure_langsmith() -> bool:
    """Mirror application settings into the environment used by LangSmith."""
    enabled = bool(settings.LANGSMITH_TRACING and settings.LANGSMITH_API_KEY.strip())
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    os.environ["LANGSMITH_TRACING_V2"] = "true" if enabled else "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if enabled else "false"
    os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
    os.environ["LANGCHAIN_PROJECT"] = settings.LANGSMITH_PROJECT
    os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT

    if settings.LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
        os.environ["LANGCHAIN_API_KEY"] = settings.LANGSMITH_API_KEY

    # LangSmith caches environment lookups through lru_cache.
    from langsmith.utils import get_env_var

    get_env_var.cache_clear()
    try:
        import langsmith

        langsmith.configure(
            client=None,
            enabled=enabled,
            project_name=settings.LANGSMITH_PROJECT,
        )
    except Exception:
        # Configuration is best-effort; normal application startup must continue.
        pass
    return enabled


LANGSMITH_ENABLED = configure_langsmith()
_tracer = None


def refresh_langsmith() -> bool:
    global LANGSMITH_ENABLED, _tracer

    LANGSMITH_ENABLED = configure_langsmith()
    _tracer = None
    return LANGSMITH_ENABLED


def tracing_enabled() -> bool:
    return LANGSMITH_ENABLED


def langgraph_trace_config(session_id: str) -> dict:
    global _tracer

    config = {
        "run_name": f"coding-agent:{session_id}",
        "tags": ["coding-agent"],
        "metadata": {
            "session_id": session_id,
            "llm_provider": settings.LLM_PROVIDER,
            "llm_model": settings.LLM_MODEL,
        },
    }
    if LANGSMITH_ENABLED:
        if _tracer is None:
            from langchain_core.tracers import LangChainTracer

            _tracer = LangChainTracer(project_name=settings.LANGSMITH_PROJECT)
        config["callbacks"] = [_tracer]
    return config
