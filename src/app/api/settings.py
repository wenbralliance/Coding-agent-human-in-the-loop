from fastapi import APIRouter
from pydantic import BaseModel
from pathlib import Path
from app.config import ENV_PATH, settings
from app.llm.tracing import refresh_langsmith

router = APIRouter()


class SettingsUpdate(BaseModel):
    llm_api_key: str | None = None
    embedding_api_key: str | None = None
    workspace_path: str | None = None
    langsmith_tracing: bool | None = None
    langsmith_api_key: str | None = None
    langsmith_project: str | None = None


@router.get("/api/settings")
def get_settings():
    if not ENV_PATH.exists():
        return {}
    config = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip();
            v = v.strip()
            if "API_KEY" in k and v:
                v = v[:4] + "***" + v[-4:] if len(v) > 8 else "***"
            config[k] = v
    return config


@router.post("/api/settings")
def update_settings(data: SettingsUpdate):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    updates = {}
    if data.llm_api_key: updates["LLM_API_KEY"] = data.llm_api_key
    if data.embedding_api_key: updates["EMBEDDING_API_KEY"] = data.embedding_api_key
    if data.workspace_path: updates["WORKSPACE_PATH"] = data.workspace_path
    if data.langsmith_tracing is not None:
        updates["LANGSMITH_TRACING"] = "true" if data.langsmith_tracing else "false"
    if data.langsmith_api_key: updates["LANGSMITH_API_KEY"] = data.langsmith_api_key
    if data.langsmith_project: updates["LANGSMITH_PROJECT"] = data.langsmith_project

    if not updates: return {"status": "no_change"}

    # 1. 写入 .env 文件持久化
    new_lines = []
    for line in lines:
        matched = False
        for k, v in updates.items():
            if line.startswith(f"{k}="):
                new_lines.append(f"{k}={v}")
                matched = True
                break
        if not matched: new_lines.append(line)

    for k, v in updates.items():
        if not any(l.startswith(f"{k}=") for l in new_lines):
            new_lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # 2. 👇 核心：热更新内存里的配置对象！
    if data.llm_api_key:
        settings.LLM_API_KEY = data.llm_api_key
    if data.embedding_api_key:
        settings.EMBEDDING_API_KEY = data.embedding_api_key
    if data.workspace_path:
        settings.WORKSPACE_PATH = data.workspace_path
    if data.langsmith_tracing is not None:
        settings.LANGSMITH_TRACING = data.langsmith_tracing
    if data.langsmith_api_key:
        settings.LANGSMITH_API_KEY = data.langsmith_api_key
    if data.langsmith_project:
        settings.LANGSMITH_PROJECT = data.langsmith_project
    refresh_langsmith()

    return {"status": "ok"}
