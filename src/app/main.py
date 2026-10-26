import asyncio
import sys
from langchain_core.messages import HumanMessage

from app.config import ENV_PATH, load_settings
from app.template import ENV_TEMPLATE


def ensure_env_file() -> bool:
    """确保 .env 存在。不存在则生成模板。"""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_PATH.exists():
        ENV_PATH.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"\n[初始化] 未找到 .env，已自动生成模板：{ENV_PATH}")
        print("[初始化] 请填入 LLM_API_KEY 和 EMBEDDING_API_KEY 后重启程序。\n")
        return False
    return True


def init_database():
    """初始化数据库表"""
    try:
        from app.db.connection import db_session
        from app.db.schema import create_all_tables
        with db_session() as db:
            create_all_tables(db)
    except Exception as e:
        print(f"\n[❌ 数据库初始化失败] {e}")
        sys.exit(1)


async def main():
    print("=" * 50)
    print("🤖 Coding Agent 启动中...")
    print(f"配置文件路径：{ENV_PATH}\n")

    # 第 1 步：检查配置
    if not ensure_env_file():
        sys.exit(0)

    settings = load_settings()
    if settings is None:
        print("\n[❌ 启动失败] 请修正配置后重启。")
        sys.exit(1)

    print(f"✅ 配置加载成功 (模型: {settings.LLM_MODEL})")

    # 第 2 步：初始化数据库
    init_database()
    print("✅ 数据库就绪")

    # 第 3 步：编译图
    try:
        from app.graph.build import build_graph
        from app.memory.store import MemoryStore

        graph = build_graph()
        store = MemoryStore()
        session_id = store.create_session("终端测试会话")
    except Exception as e:
        print(f"\n[❌ 图加载失败] {e}")
        sys.exit(1)

    print("=" * 50)
    print("输入 'exit' 退出对话\n")

    # 第 4 步：终端对话循环
    while True:
        try:
            user_input = input("你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            # 构造初始状态
            state = {
                "session_id": session_id,
                "user_input": user_input,
                "messages": [HumanMessage(content=user_input)],
                "tool_rounds": 0,
                "edit_batches": 0,
                "edit_operations": 0,
                "edited_files": [],
            }

            print("\nAgent 思考中...\n")

            # 运行图
            try:
                result = await graph.ainvoke(state)
            except Exception as e:
                print(f"\n[⚠️ 运行错误] {e}\n")
                continue

            # 从结果中提取最后一条 AI 消息
            final_msgs = result.get("messages", [])
            printed = False
            for m in reversed(final_msgs):
                # LangChain 的 AIMessage type 是 'ai'
                if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
                    print(f"Agent: {m.content}\n")
                    printed = True
                    break

            if not printed:
                print("Agent: (未生成文本回复)\n")

        except KeyboardInterrupt:
            print("\n[强制退出]")
            break
        except Exception as e:
            print(f"\n[⚠️ 系统错误] {e}\n")

    print("再见！")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
