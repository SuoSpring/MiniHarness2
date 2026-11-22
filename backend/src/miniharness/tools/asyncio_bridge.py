"""Windows 的数据库用 Selector，子进程用独立线程中的 Proactor。"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import TypeVar

T = TypeVar("T")


async def with_subprocess_loop(operation: Callable[[], Awaitable[T]]) -> T:
    """仅在 Windows Selector 下桥接；取消必须等工作线程清理完成再返回。"""
    if os.name != "nt" or isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
        return await operation()
    lock = Lock()
    state = {"loop": None, "task": None, "cancelled": False}

    async def invoke() -> T:
        with lock:
            state["loop"], state["task"] = asyncio.get_running_loop(), asyncio.current_task()
            if state["cancelled"]:
                state["task"].cancel()
        try:
            return await operation()
        finally:
            with lock:
                state["loop"], state["task"] = None, None

    def worker() -> T:
        with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
            return runner.run(invoke())

    # to_thread 同时继承调用方上下文变量；不在工作线程访问异步数据库连接。
    future = asyncio.create_task(asyncio.to_thread(worker))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        with lock:
            state["cancelled"] = True
            if state["loop"]:
                state["loop"].call_soon_threadsafe(state["task"].cancel)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            pass
        raise
