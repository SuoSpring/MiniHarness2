"""会话独占工作树；数据库快照决定已提交状态，目录可在异常后恢复。"""

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from miniharness.storage.models import Run, Session

if TYPE_CHECKING:
    from .manager import Runtime

logger = logging.getLogger(__name__)


class SessionWorkspaces:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    def path(self, session: Session) -> Path:
        root = (self.runtime.settings.data_dir / "sessions").resolve()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session.id):
            raise ValueError("无效的会话标识")
        literal = root / session.id / "work"
        expected = literal.resolve()
        if expected != literal.absolute() or not expected.is_relative_to(root):
            raise ValueError("会话工作树目录越界")
        if session.working_dir and Path(session.working_dir).resolve() != expected:
            raise ValueError("会话工作树路径与登记目录不一致")
        return expected

    async def prepare(self, session: Session) -> Path:
        target = self.path(session)
        if not target.exists():
            await asyncio.to_thread(
                self.runtime.workspace.create_worktree,
                session.project_id,
                session.head_snapshot,
                target,
            )
        elif session.workspace_snapshot != session.head_snapshot:
            await asyncio.to_thread(
                self.runtime.workspace.restore_worktree,
                session.project_id,
                session.head_snapshot,
                target,
            )
        session.working_dir = str(target)
        session.workspace_snapshot = session.head_snapshot
        return target

    async def restore(self, session: Session, snapshot: str, *, committed: bool) -> Path:
        target = self.path(session)
        operation = (
            self.runtime.workspace.restore_worktree
            if target.exists()
            else self.runtime.workspace.create_worktree
        )
        await asyncio.to_thread(operation, session.project_id, snapshot, target)
        session.working_dir = str(target)
        session.workspace_snapshot = snapshot if committed else None
        return target

    async def cleanup_archived(self, session_id: str) -> bool:
        try:
            async with self.runtime.database.session.begin() as db:
                session = await db.get(Session, session_id, with_for_update=True)
                if not session or not session.archived_at or session.active_run_id:
                    return False
                if session.working_dir:
                    await asyncio.to_thread(
                        self.runtime.workspace.remove_worktree,
                        session.project_id,
                        self.path(session),
                    )
                    session.working_dir = session.workspace_snapshot = None
            return True
        except (OSError, RuntimeError, ValueError):
            # 会话已经归档，清理失败时保留重试记录。
            logger.exception("归档会话 %s 的工作树清理失败，将在恢复时重试", session_id)
            return False

    async def prune_patches(self, session_id: str) -> None:
        async with self.runtime.database.session() as db:
            session = await db.get(Session, session_id)
            latest_success = await db.scalar(
                select(Run.id)
                .where(Run.session_id == session_id, Run.status == "completed")
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            keep = {latest_success, session.undo_run_id, session.active_run_id}
            old = list(await db.scalars(select(Run.id).where(Run.session_id == session_id)))
        for identifier in old:
            if identifier not in keep:
                try:
                    (self.runtime.run_dir(identifier) / "changes.patch").unlink(missing_ok=True)
                except OSError:
                    logger.exception("旧调用 %s 的补丁清理失败", identifier)
