"""在项目工作副本中运行 Python，复用共享子进程执行器。"""

import sys
from pathlib import Path

from ..environments import PythonEnvironment
from ..workspace.paths import resolve_path
from .process import PREVIEW_BYTES, run_process
from .process import ProcessRunResult as PythonRunResult


async def run_python(
    workdir: Path,
    path: str,
    args: list[str],
    logdir: Path,
    timeout: float = 30,
    *,
    preview_bytes: int = PREVIEW_BYTES,
    python_environment: PythonEnvironment | None = None,
) -> PythonRunResult:
    """执行已有 Python 脚本，返回输出预览、完整日志和退出状态。"""
    script = resolve_path(workdir, path)
    if script.suffix.lower() != ".py" or not script.is_file():
        raise ValueError("只能运行明确指定的已有 Python 文件")
    return await run_process(
        workdir,
        [
            python_environment.python if python_environment else sys.executable,
            "-u",
            "-B",
            str(script),
            *args,
        ],
        logdir,
        timeout,
        python_environment.variables() if python_environment else None,
        preview_bytes=preview_bytes,
    )


async def run_python_command(
    workdir: Path,
    arguments: list[str],
    logdir: Path,
    timeout: float,
    environment: dict[str, str] | None = None,
    *,
    python_environment: PythonEnvironment | None = None,
) -> PythonRunResult:
    """供框架验证器使用；不作为模型工具暴露，不经过 shell。"""
    return await run_process(
        workdir,
        [
            python_environment.python if python_environment else sys.executable,
            "-X",
            "utf8",
            "-u",
            "-B",
            *arguments,
        ],
        logdir,
        timeout,
        {**(python_environment.variables() if python_environment else {}), **(environment or {})},
    )
