from app.config import load_settings

settings = load_settings()
if settings is None:
    print("配置加载失败")
    exit(1)

print(f"LLM Provider: {settings.LLM_PROVIDER}")
print(f"LLM Model: {settings.LLM_MODEL}")
print(f"LLM Temperature: {settings.LLM_TEMPERATURE} ({type(settings.LLM_TEMPERATURE).__name__})")
print(f"LLM Max Tokens: {settings.LLM_MAX_TOKENS} ({type(settings.LLM_MAX_TOKENS).__name__})")
print(f"Embedding Model: {settings.EMBEDDING_MODEL}")
print(f"Embedding Dim: {settings.EMBEDDING_DIM} ({type(settings.EMBEDDING_DIM).__name__})")
print(f"DB Path: {settings.DB_PATH}")
print(f"Max Tool Rounds: {settings.MAX_TOOL_ROUNDS}")
print(f"\nLLM API Key: {settings.LLM_API_KEY[:8]}...")
print(f"Embedding API Key: {settings.EMBEDDING_API_KEY[:8]}...")