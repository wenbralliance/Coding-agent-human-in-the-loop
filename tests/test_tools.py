import unittest

from app.tools import registry


class ToolRegistrySmokeTest(unittest.TestCase):
    def test_write_tools_are_not_exposed(self) -> None:
        names = set(registry.tools)
        self.assertIn("todo_write", names)
        self.assertIn("bash_exec", names)
        self.assertNotIn("write_file", names)
        self.assertNotIn("write_files", names)
        self.assertNotIn("apply_edit_batch", names)
        self.assertNotIn("edit_file", names)


if __name__ == "__main__":
    unittest.main()
