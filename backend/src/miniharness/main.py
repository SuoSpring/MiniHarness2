import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text

from miniharness.api import artifacts, integrations, memories, projects, runs
from miniharness.api.dependencies import visible_resource
from miniharness.config import Settings
from miniharness.runtime.errors import Conflict, HarnessError, NotFound
from miniharness.runtime.manager import Runtime
from miniharness.storage import Database


def create_app(
    settings: Settings | None = None, runtime_override: Runtime | None = None
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime_override is not None:
            app.state.runtime = runtime_override
            yield
            return
        database = Database(settings.database_url)
        # 单机服务只有一个调度器；持有连接锁，防止另一进程误把活动调用当崩溃恢复。
        try:
            async with database.engine.connect() as owner:
                acquired = await owner.scalar(text("SELECT pg_try_advisory_lock(742015)"))
                await owner.commit()
                if not acquired:
                    raise RuntimeError("此数据库已有 MiniHarness2 调度器，请使用单进程启动。")
                async with AsyncPostgresSaver.from_conn_string(settings.postgres_dsn) as saver:
                    await saver.setup()
                    service = Runtime(settings, database, saver)
                    app.state.runtime = service
                    await service.recover()
                    try:
                        yield
                    finally:
                        await service.shutdown()
                await owner.execute(text("SELECT pg_advisory_unlock(742015)"))
        finally:
            await database.close()

    app = FastAPI(title="MiniHarness2", version="0.1.0", lifespan=lifespan)
    if runtime_override is not None:
        app.state.runtime = runtime_override
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (projects, runs, artifacts, memories, integrations):
        app.include_router(
            module.router, prefix="/api/v1", dependencies=[Depends(visible_resource)]
        )

    @app.exception_handler(HarnessError)
    async def harness_error(request: Request, error: HarnessError) -> JSONResponse:
        status = 404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 400
        return JSONResponse({"detail": str(error)}, status_code=status)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def file_not_found(request: Request, error: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.get("/healthz", response_model=None)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    import uvicorn

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(create_app(), host="127.0.0.1", port=8001, loop="miniharness.main:loop_factory")


def loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


if __name__ == "__main__":
    main()
