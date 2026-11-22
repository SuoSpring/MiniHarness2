import asyncio
import sys

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from miniharness.config import Settings
from miniharness.storage.models import Base


def migrate(connection: Connection) -> None:
    # LangGraph 自己迁移检查点表，业务迁移不能尝试删除它们。
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_object=lambda obj, name, kind, reflected, compare_to: (
            not (kind == "table" and name.startswith("checkpoint"))
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(online())
