from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


RiskLevel = Literal["low", "medium", "high", "critical", "blocked"]


@dataclass(frozen=True)
class RiskFinding:
    code: str
    message: str
    level: RiskLevel


@dataclass(frozen=True)
class CommandAssessment:
    command: str
    cwd: str
    risk_level: RiskLevel
    needs_approval: bool
    blocked: bool
    parallel_safe: bool
    approval_id: str
    findings: tuple[RiskFinding, ...] = field(default_factory=tuple)

    def as_approval_payload(self) -> dict:
        return {
            "type": "bash",
            "tool": "bash_exec",
            "command": self.command,
            "cwd": self.cwd,
            "risk_level": self.risk_level,
            "approval_id": self.approval_id,
            "findings": [
                {
                    "code": finding.code,
                    "message": finding.message,
                    "level": finding.level,
                }
                for finding in self.findings
            ],
        }


def _contains_command(command: str, names: set[str]) -> bool:
    lowered = command.lower()
    for name in names:
        escaped = re.escape(name.lower())
        if re.search(rf"(^|[\s;&|(){{}}]){escaped}(?:\s|$|[;&|(){{}}])", lowered):
            return True
    return False


def _first_words(command: str) -> set[str]:
    words: set[str] = set()
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.strip().split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if tokens:
            words.add(Path(tokens[0]).name.lower())
    return words


def _outside_workspace_reference(command: str, workspace: Path) -> bool:
    lowered = command.lower().replace("\\", "/")
    workspace_text = str(workspace).lower().replace("\\", "/")
    patterns = (
        r"(?<![a-z0-9])[a-z]:/[^\s\"']+",
        r"(?<![a-z0-9])/(?:etc|usr|bin|sbin|opt|root|home)/[^\s\"']*",
        r"(?<![a-z0-9])/(?:mnt|media)/[^\s\"']+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            value = match.group(0).replace("/c/", "c:/")
            if not value.startswith(workspace_text):
                return True
    return bool(re.search(r"(?:^|[\s;&|()])cd\s+\.\.(?:/|\s|$)", lowered))


def command_approval_id(command: str, cwd: Path, workspace: Path) -> str:
    material = "\0".join(
        (
            command.replace("\r\n", "\n").strip(),
            str(cwd.resolve()).lower(),
            str(workspace.resolve()).lower(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def assess_bash_command(command: str, cwd: Path, workspace: Path) -> CommandAssessment:
    findings: list[RiskFinding] = []

    blocked_commands = {"sudo", "su", "runas", "diskpart", "format", "shutdown", "reboot"}
    if _contains_command(command, blocked_commands):
        findings.append(
            RiskFinding(
                "privilege_or_system_control",
                "命令包含管理员提权、磁盘或系统控制操作，当前策略默认拒绝。",
                "blocked",
            )
        )

    destructive_commands = {
        "rm",
        "rmdir",
        "unlink",
        "del",
        "erase",
        "remove-item",
        "rd",
        "taskkill",
        "kill",
        "pkill",
        "dd",
        "truncate",
        "shred",
    }
    if _contains_command(command, destructive_commands):
        findings.append(
            RiskFinding(
                "destructive",
                "检测到删除、覆盖或进程终止操作。",
                "critical",
            )
        )

    lowered = command.lower()
    if (
        re.search(r"\bfind\b[^\n]*\s-delete\b", lowered)
        or re.search(r"\bgit\s+(?:clean|reset\s+--hard|checkout\s+--|restore)\b", lowered)
        or re.search(r"\b(?:sed|perl)\b[^\n]*\s-[a-z]*i[a-z]*\b", lowered)
    ):
        findings.append(
            RiskFinding(
                "destructive_git_or_rewrite",
                "检测到 Git 破坏性操作或原地批量重写。",
                "critical",
            )
        )

    execution_commands = {
        "python",
        "python3",
        "py",
        "pytest",
        "unittest",
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "uv",
        "pip",
        "pip3",
        "bash",
        "sh",
        "zsh",
        "powershell",
        "pwsh",
        "cmd",
        "java",
        "go",
        "cargo",
        "dotnet",
    }
    if _contains_command(command, execution_commands):
        findings.append(
            RiskFinding(
                "code_execution",
                "命令会启动解释器、测试框架、构建工具或子 Shell。",
                "high",
            )
        )

    network_commands = {
        "curl",
        "wget",
        "ssh",
        "scp",
        "ftp",
        "nc",
        "netcat",
        "telnet",
        "git",
    }
    if _contains_command(command, network_commands) and any(
        token in lowered
        for token in ("clone", "pull ", "push ", "fetch", "install", "http://", "https://")
    ):
        findings.append(
            RiskFinding(
                "network_or_dependency_install",
                "命令包含网络访问、仓库同步或依赖安装。",
                "high",
            )
        )

    if _outside_workspace_reference(command, workspace):
        findings.append(
            RiskFinding(
                "outside_workspace",
                "命令可能访问 workspace 之外的绝对路径或上跳目录。",
                "high",
            )
        )

    if re.search(r"(?<![0-9&])>{1,2}\s*(?!/dev/null\b)", command):
        findings.append(
            RiskFinding(
                "redirection_write",
                "命令包含输出重定向，可能写入或覆盖文件。",
                "medium",
            )
        )

    if _contains_command(
        command,
        {"touch", "mkdir", "cp", "mv", "tee", "patch", "install"},
    ):
        findings.append(
            RiskFinding(
                "workspace_write",
                "命令包含文件创建、复制或替换操作。",
                "medium",
            )
        )

    safe_commands = {
        "pwd",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "stat",
        "file",
        "rg",
        "grep",
        "find",
        "git",
        "printf",
        "echo",
        "test",
    }
    words = _first_words(command)
    recognized = safe_commands | blocked_commands | destructive_commands | execution_commands | network_commands
    unknown = {word for word in words if word and word not in recognized}
    if unknown:
        findings.append(
            RiskFinding(
                "unrecognized_command",
                f"检测到未在安全允许列表中的命令: {', '.join(sorted(unknown))}",
                "high",
            )
        )

    level_order = {"low": 0, "medium": 1, "high": 2, "critical": 3, "blocked": 4}
    risk_level: RiskLevel = "low"
    if findings:
        risk_level = max(findings, key=lambda item: level_order[item.level]).level

    blocked = risk_level == "blocked"
    needs_approval = not blocked and risk_level in {"high", "critical"}
    has_mutation = any(
        finding.code
        in {
            "destructive",
            "destructive_git_or_rewrite",
            "outside_workspace",
            "redirection_write",
            "workspace_write",
            "network_or_dependency_install",
            "code_execution",
        }
        for finding in findings
    )
    parallel_safe = risk_level == "low" and not has_mutation

    return CommandAssessment(
        command=command,
        cwd=str(cwd),
        risk_level=risk_level,
        needs_approval=needs_approval,
        blocked=blocked,
        parallel_safe=parallel_safe,
        approval_id=command_approval_id(command, cwd, workspace),
        findings=tuple(findings),
    )
