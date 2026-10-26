from app.tools.base import ToolRegistry
from app.tools.file import write_file, read_file, list_files, edit_file, grep_content
from app.tools.batch import read_files, apply_edit_batch, write_files
from app.tools.run import run_file
from app.tools.code import analyze_code, review_code

# 全局唯一的工具注册表
registry = ToolRegistry()

# 把所有工具注册进去
registry.register(write_file)
registry.register(read_file)
registry.register(list_files)
registry.register(edit_file)
registry.register(grep_content)
registry.register(read_files)
registry.register(apply_edit_batch)
registry.register(write_files)
registry.register(run_file)
registry.register(analyze_code)
registry.register(review_code)
