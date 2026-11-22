"""共享子进程执行器：输出日志、超时、取消和进程树清理。"""

import asyncio
import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TypedDict

from .asyncio_bridge import with_subprocess_loop

PREVIEW_BYTES = 2000


class ProcessRunResult(TypedDict):
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_log: str
    stderr_log: str
    timed_out: bool
    duration: float


class _WindowsJob:
    """关闭作业句柄时终止整棵进程树，包括父进程已经退出的子进程。"""

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io_counters", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = None
        try:
            if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _Capture:
    def __init__(self, path: Path, preview_bytes: int) -> None:
        self.path = path
        self.preview_bytes = preview_bytes
        self.tail_bytes = preview_bytes - preview_bytes // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        with self.path.open("wb") as log:
            while chunk := await stream.read(8192):
                log.write(chunk)
                log.flush()
                self.total += len(chunk)
                self.head.extend(chunk[: max(0, self.preview_bytes - len(self.head))])
                self.tail.extend(chunk)
                del self.tail[: -self.tail_bytes]

    def preview(self) -> str:
        if self.total <= self.preview_bytes:
            return self.head.decode("utf-8", errors="replace")
        return (
            self.head[: self.preview_bytes // 2].decode("utf-8", errors="replace")
            + "\n……输出已截断；完整内容请用 read_log 按日志路径读取……\n"
            + self.tail.decode("utf-8", errors="replace")
        )


async def _stop(process: asyncio.subprocess.Process, job: _WindowsJob | None) -> None:
    if job:
        job.close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def run_process(
    workdir: Path,
    arguments: list[str],
    logdir: Path,
    timeout: float = 30,
    environment: dict[str, str] | None = None,
    *,
    preview_bytes: int = PREVIEW_BYTES,
) -> ProcessRunResult:
    """执行明确指定的程序；Windows Selector 事件循环下桥接到子进程线程。"""
    if preview_bytes < 2:
        raise ValueError("输出预览至少需要 2 字节")
    return await with_subprocess_loop(
        lambda: _run_process(
            workdir, arguments, logdir, timeout, environment or {}, preview_bytes=preview_bytes
        )
    )


async def _run_process(
    workdir: Path,
    arguments: list[str],
    logdir: Path,
    timeout: float,
    extra_env: dict[str, str],
    *,
    preview_bytes: int = PREVIEW_BYTES,
) -> ProcessRunResult:
    logdir.mkdir(parents=True, exist_ok=True)
    output = _Capture(logdir / "stdout.log", preview_bytes)
    errors = _Capture(logdir / "stderr.log", preview_bytes)
    # 不把模型密钥直接继承给执行进程；仅由调用方明确选择命令解释器。
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    environment.update(extra_env)
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=workdir,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    job = None
    timed_out = False
    drains = None
    try:
        if os.name == "nt":
            job = _WindowsJob(process.pid)
        drains = asyncio.gather(output.drain(process.stdout), errors.drain(process.stderr))
        try:
            async with asyncio.timeout(timeout):
                await process.wait()
                if job:
                    job.close()
                await asyncio.shield(drains)
        except TimeoutError:
            timed_out = True
            await _stop(process, job)
    except BaseException:
        await asyncio.shield(_stop(process, job))
        raise
    finally:
        if job:
            job.close()
        if drains:
            await asyncio.shield(drains)
    return {
        "exit_code": process.returncode,
        "stdout": output.preview(),
        "stderr": errors.preview(),
        "stdout_log": str(output.path.resolve()),
        "stderr_log": str(errors.path.resolve()),
        "timed_out": timed_out,
        "duration": round(time.monotonic() - started, 3),
    }
