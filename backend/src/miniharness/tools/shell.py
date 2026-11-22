"""显式选择 Bash、CMD 或 PowerShell 执行命令；工作目录不等于系统沙箱。"""

import base64
import os
import shutil
from pathlib import Path
from typing import Literal

from ..environments import PythonEnvironment
from .process import PREVIEW_BYTES, ProcessRunResult, run_process

ShellName = Literal["bash", "cmd", "powershell"]


class CommandRunResult(ProcessRunResult):
    shell: ShellName


def resolve_shell(shell: ShellName) -> str:
    if shell not in {"bash", "cmd", "powershell"}:
        raise ValueError("shell 必须是 bash、cmd 或 powershell")
    if shell == "cmd" and os.name != "nt":
        raise FileNotFoundError("CMD 仅支持 Windows；请选择已安装的 bash 或 powershell")
    if shell == "bash" and os.name == "nt":
        executable = shutil.which("bash")
        # Windows 自带的 bash.exe 是 WSL 启动器，无法沿用本机进程树和路径语义。
        if executable and Path(executable).parent.name.casefold() not in {"system32", "sysnative"}:
            return executable
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for candidate in (root / "bin/bash.exe", root / "usr/bin/bash.exe"):
                if candidate.is_file():
                    return str(candidate)
        raise FileNotFoundError(
            "未找到本机 Bash；请安装 Git for Windows 并将 Git 或 Bash 加入 PATH（不使用 WSL 启动器）"
        )
    names = ("pwsh", "powershell") if shell == "powershell" else (shell,)
    for name in names:
        if executable := shutil.which(name):
            return executable
    raise FileNotFoundError(f"未找到 {shell}；请安装对应命令解释器并加入 PATH")


async def run_command(
    workdir: Path,
    command: str,
    shell: ShellName,
    logdir: Path,
    timeout: float = 30,
    *,
    preview_bytes: int = PREVIEW_BYTES,
    python_environment: PythonEnvironment | None = None,
) -> CommandRunResult:
    """每次启动独立非交互进程，返回真实退出状态、UTF-8 日志和有限正文预览。"""
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("command 必须是非空命令，且不能包含空字符")
    if preview_bytes < 2:
        raise ValueError("输出预览至少需要 2 字节")
    executable = resolve_shell(shell)
    if shell == "bash":
        arguments = [executable, "--noprofile", "--norc", "-c", command]
    elif shell == "cmd":
        # 通过批处理文件传递原文，避免 Windows 参数转义破坏嵌套引号与多行命令。
        logdir.mkdir(parents=True, exist_ok=True)
        script = logdir.resolve() / "command.cmd"
        script.write_text(
            "@echo off\nchcp 65001 >nul\n" + command.replace("\r\n", "\n") + "\n",
            encoding="utf-8",
            newline="\r\n",
        )
        arguments = [executable, "/d", "/q", "/c", str(script)]
    else:
        source = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            "$LASTEXITCODE = 0\n" + command + "\nexit $LASTEXITCODE\n"
        )
        encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
        arguments = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ]
    # 仅补充命令行程序需要的系统目录变量，模型密钥及任意自定义变量不继承。
    allowed = {
        "COMSPEC",
        "PATHEXT",
        "SYSTEMDRIVE",
        "USERPROFILE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if python_environment:
        environment.update(python_environment.variables())
    result = await run_process(
        workdir, arguments, logdir, timeout, environment, preview_bytes=preview_bytes
    )
    return {**result, "shell": shell}
