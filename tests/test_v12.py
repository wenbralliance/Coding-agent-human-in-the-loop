from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_HOME = Path(tempfile.mkdtemp(prefix="coding-agent-v12-tests-"))
os.environ["CODING_AGENT_HOME"] = str(_TEST_HOME)
os.environ["WORKSPACE_PATH"] = str(_TEST_HOME / "workspace")
os.environ["DB_PATH"] = str(_TEST_HOME / "coding_agent.db")
os.environ["LANGSMITH_TRACING"] = "false"


def _cleanup_test_home() -> None:
    if _TEST_HOME.exists():
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


atexit.register(_cleanup_test_home)


from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.api.main import app
from app.config import settings
from app.db.connection import db_session
from app.db.schema import create_all_tables
from app.graph.nodes import tools_node
from app.graph.todo import (
    TodoItem,
    TodoPlanError,
    build_execution_waves,
    todo_registry,
    validate_todo_items,
)
from app.security import assess_bash_command
from app.tools import registry
from app.tools.base import ToolRegistry, tool
from app.tools.bash import (
    PersistentBashSession,
    bash_sessions,
    find_bash_executable,
)
from app.tools.file import get_workspace_dir
from app.tools.process_manager import tool_session


class _NoArgs(BaseModel):
    pass


class V12IntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with db_session() as db:
            create_all_tables(db)

    def setUp(self) -> None:
        workspace = get_workspace_dir()
        workspace.mkdir(parents=True, exist_ok=True)
        for path in workspace.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def test_registry_uses_todo_and_bash_not_write_tools(self) -> None:
        names = set(registry.tools)
        self.assertIn("todo_write", names)
        self.assertIn("bash_exec", names)
        self.assertNotIn("write_file", names)
        self.assertNotIn("write_files", names)
        self.assertNotIn("apply_edit_batch", names)
        self.assertNotIn("edit_file", names)

    def test_todo_dependency_waves_and_cycle_detection(self) -> None:
        items = [
            TodoItem(id="a", title="A"),
            TodoItem(id="b", title="B"),
            TodoItem(id="c", title="C", depends_on=["a", "b"]),
        ]
        self.assertEqual(build_execution_waves(items), [["a", "b"], ["c"]])
        cyclic = [
            TodoItem(id="a", title="A", depends_on=["b"]),
            TodoItem(id="b", title="B", depends_on=["a"]),
        ]
        with self.assertRaises(TodoPlanError):
            validate_todo_items(cyclic)

    def test_risk_analysis_matches_destructive_and_blocked_commands(self) -> None:
        workspace = get_workspace_dir()
        safe = assess_bash_command("pwd && ls", workspace, workspace)
        self.assertFalse(safe.needs_approval)
        destructive = assess_bash_command("rm -rf build", workspace, workspace)
        self.assertTrue(destructive.needs_approval)
        self.assertEqual(destructive.risk_level, "critical")
        blocked = assess_bash_command("sudo rm -rf build", workspace, workspace)
        self.assertTrue(blocked.blocked)

    async def test_rejected_bash_approval_does_not_execute(self) -> None:
        marker = get_workspace_dir() / "must-not-exist.txt"
        marker.write_text("protected", encoding="utf-8")
        command = f"rm -rf '{marker.as_posix()}'"
        with patch(
            "app.tools.bash.interrupt",
            return_value={"action": "reject"},
        ):
            with tool_session("approval-reject"):
                result = await registry.execute(
                    "bash_exec",
                    json.dumps(
                        {
                            "command": command,
                            "todo_id": "modify",
                        }
                    ),
                )
        self.assertEqual(json.loads(result)["status"], "rejected")
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "protected")

    def test_persistent_bash_keeps_directory_and_environment(self) -> None:
        workspace = get_workspace_dir()
        session = PersistentBashSession(workspace)
        try:
            first = session.execute(
                "mkdir -p nested && cd nested && export CA_TEST_VALUE=kept",
                timeout=10,
            )
            second = session.execute(
                'printf "%s|%s" "$CA_TEST_VALUE" "$(basename "$PWD")"',
                timeout=10,
            )
        finally:
            session.close()
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.stdout, "kept|nested")

    def test_retryable_tool_recovers_transient_exception(self) -> None:
        attempts = {"count": 0}

        @tool(_NoArgs, "flaky test tool", retryable=True)
        def flaky_tool() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OSError("temporary filesystem failure")
            return json.dumps({"status": "completed", "attempts": attempts["count"]})

        local_registry = ToolRegistry()
        local_registry.register(flaky_tool)
        result = asyncio_run(
            local_registry.execute("flaky_tool", "{}")
        )
        self.assertEqual(json.loads(result)["status"], "completed")
        self.assertEqual(attempts["count"], 3)

    async def test_todo_is_required_before_other_tools(self) -> None:
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "bash_exec",
                    "args": {"command": "pwd", "todo_id": "inspect"},
                    "id": "call_plan",
                    "type": "tool_call",
                }
            ],
        )
        state = {
            "session_id": "todo-required",
            "messages": [message],
            "tool_rounds": 0,
            "todos": [],
            "recent_tool_signatures": [],
            "loop_detected": False,
        }
        update = await tools_node(state)
        self.assertEqual(update["termination_reason"], "todo_required")
        self.assertIn("TODO_REQUIRED", update["messages"][0].content)

    async def test_repeated_tool_signature_stops_at_threshold(self) -> None:
        session_id = "loop-test"
        todo_registry.set(
            session_id,
            [TodoItem(id="inspect", title="检查目录")],
        )
        previous: list[str] = []
        update: dict = {}
        recovery_count = 0
        for index in range(settings.LOOP_REPEAT_THRESHOLD + 1):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "bash_exec",
                        "args": {
                            "command": "pwd",
                            "todo_id": "inspect",
                        },
                        "id": f"call_loop_{index}",
                        "type": "tool_call",
                    }
                ],
            )
            update = await tools_node(
                {
                    "session_id": session_id,
                    "messages": [message],
                    "tool_rounds": index,
                    "todos": [{"id": "inspect", "status": "pending", "depends_on": [], "parallelizable": True}],
                    "recent_tool_signatures": previous,
                    "loop_detected": False,
                    "loop_recovery_count": recovery_count,
                }
            )
            previous = update["recent_tool_signatures"]
            recovery_count = update.get("loop_recovery_count", recovery_count)
        self.assertTrue(update["loop_detected"])
        self.assertEqual(update["termination_reason"], "loop_detected")
        self.assertIn("LOOP_DETECTED", update["messages"][0].content)

    def test_api_health_and_metrics(self) -> None:
        with TestClient(app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            payload = health.json()
            self.assertEqual(payload["version"], settings.WORKFLOW_VERSION)
            self.assertTrue(payload["bash"]["available"])
            metrics = client.get("/api/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("tasks", metrics.json())


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
