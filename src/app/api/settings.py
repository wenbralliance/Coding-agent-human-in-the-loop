from fastapi import APIRouter
from pydantic import BaseModel
from pathlib import Path
import os

from app.config import ENV_PATH

router = APIRouter()


class SettingsUpdate(BaseModel):
    llm_api_key: str | None = None
    embedding_api_key: str | None = None
    workspace_path: str | None = None


@router.get("/api/settings")
def get_settings():
    """读取当前配置（返回给前端展示）"""
    # 简单读取 .env 文件（不暴露完整的 Key）
    if not ENV_PATH.exists():
        return {}

    config = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if "API_KEY" in k and v:
                v = v[:4] + "***" + v[-4:] if len(v) > 8 else "***"
            config[k] = v
    return config


@router.post("/api/settings")
def update_settings(data: SettingsUpdate):
    """修改配置并写入 .env，重启后生效"""
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    updates = {}
    if data.llm_api_key:
        updates["LLM_API_KEY"] = data.llm_api_key
    if data.embedding_api_key:
        updates["EMBEDDING_API_KEY"] = data.embedding_api_key
    if data.workspace_path:
        updates["WORKSPACE_PATH"] = data.workspace_path

    if not updates:
        return {"status": "no_change"}

    # 简单的行替换
    new_lines = []
    for line in lines:
        matched = False
        for k, v in updates.items():
            if line.startswith(f"{k}="):
                new_lines.append(f"{k}={v}")
                matched = True
                break
        if not matched:
            new_lines.append(line)

    # 补充新增的项
    for k, v in updates.items():
        if not any(l.startswith(f"{k}=") for l in new_lines):
            new_lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return {"status": "ok"}