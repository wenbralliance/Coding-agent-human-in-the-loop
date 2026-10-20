ENV_TEMPLATE = """# ========== LLM ==========
# 可选：deepseek / openai / dashscope / ollama
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=4096
LLM_MAX_RETRIES=3
LLM_RETRY_BACKOFF=1.0
LLM_TIMEOUT=60

# ========== Embedding ==========
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=
EMBEDDING_MODEL=
EMBEDDING_DIM=1024
EMBEDDING_MAX_RETRIES=3
EMBEDDING_TIMEOUT=30

# ========== 数据库 ==========
DB_PATH=data/coding_agent.db

# ========== 工作区 ==========
WORKSPACE_PATH=workspace

# ========== 工具 ==========
SANDBOX_MODE=subprocess
SANDBOX_TIMEOUT=10
TOOL_MAX_RETRIES=2
TOOL_TIMEOUT=10

# ========== 图循环 ==========
MAX_TOOL_ROUNDS=5
MAX_TOTAL_RETRIES=10

# ========== 熔断 ==========
CIRCUIT_BREAKER_THRESHOLD=5
CIRCUIT_BREAKER_COOLDOWN=60

# ========== 记忆 ==========
MEMORY_CONFIDENCE_THRESHOLD=0.6
MEMORY_TOP_K=5
MEMORY_DECAY_EVENT=3
MEMORY_DECAY_PREFERENCE=180

# ========== 上下文 ==========
MAX_HISTORY_MESSAGES=10
COMPRESS_THRESHOLD=0.6
"""