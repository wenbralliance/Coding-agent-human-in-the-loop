from app.tools.base import ToolRegistry
from app.tools.file import read_file, list_files, grep_content
from app.tools.batch import read_files
from app.tools.bash import bash_exec, bash_reset
from app.tools.todo import todo_read, todo_write
from app.tools.run import run_file
from app.tools.code import analyze_code, review_code

# 全局唯一的工具注册表
registry = ToolRegistry()

# 把所有工具注册进去
registry.register(read_file)
registry.register(list_files)
registry.register(grep_content)
registry.register(read_files)
registry.register(todo_write)
registry.register(todo_read)
registry.register(bash_exec)
registry.register(bash_reset)
registry.register(run_file)
registry.register(analyze_code)
registry.register(review_code)
