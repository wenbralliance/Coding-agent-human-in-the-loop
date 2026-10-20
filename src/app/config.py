import sys
from pathlib import Path
from pydantic import field_validator, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict



# 开发时读项目根，打包后读用户目录
if getattr(sys, "frozen", False):
    ENV_PATH = Path.home() / ".coding-agent" / ".env"
else:
    ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ========== LLM ==========
    LLM_PROVIDER: str
    LLM_API_KEY: str
    LLM_BASE_URL: str
    LLM_MODEL: str
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 4096
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_BACKOFF: float = 1.0
    LLM_TIMEOUT: int = 60

    # ========== Embedding ==========
    EMBEDDING_API_KEY: str
    EMBEDDING_BASE_URL: str
    EMBEDDING_MODEL: str
    EMBEDDING_DIM: int = 1024
    EMBEDDING_MAX_RETRIES: int = 3
    EMBEDDING_TIMEOUT: int = 30

    # ========== 其他配置（都有默认值） ==========
    DB_PATH: str = "data/coding_agent.db"
    WORKSPACE_PATH: str = "workspace"
    SANDBOX_MODE: str = "subprocess"
    SANDBOX_TIMEOUT: int = 10
    TOOL_MAX_RETRIES: int = 2
    TOOL_TIMEOUT: int = 10
    MAX_TOOL_ROUNDS: int = 5
    MAX_TOTAL_RETRIES: int = 10
    CIRCUIT_BREAKER_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN: int = 60
    MEMORY_CONFIDENCE_THRESHOLD: float = 0.6
    MEMORY_TOP_K: int = 5
    MEMORY_DECAY_EVENT: int = 3
    MEMORY_DECAY_PREFERENCE: int = 180
    MAX_HISTORY_MESSAGES: int = 10
    COMPRESS_THRESHOLD: float = 0.6

    # ========== 校验：必填非空 ==========
    @field_validator(
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "EMBEDDING_API_KEY", "EMBEDDING_MODEL","EMBEDDING_BASE_URL",
    )
    @classmethod
    def check_not_empty(cls, v, info):
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} 不能为空")
        return v

settings = Settings()

def load_settings() -> Settings | None:
    """加载配置。失败返回 None，并打印友好提示。"""
    try:
        return Settings()
    except ValidationError as e:
        print(f"配置文件有误：{ENV_PATH}\n")
        for err in e.errors():
            field = err["loc"][0] if err["loc"] else "未知"
            print(f"  - {field}: {err['msg']}")
        print("\n请修改配置文件后重启程序。")
        return None
