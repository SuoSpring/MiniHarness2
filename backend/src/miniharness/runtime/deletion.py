"""可重试的物理删除。先锁定并登记意图，清理成功后才移除数据库记录。"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from miniharness.runtime.errors import Conflict, NotFound
from miniharness.storage.models import (
    AcceptanceSuite,
    ApplicationSetting,
    Boundary,
    Contribution,
    Event,
    MCPServer,
    Memory,
    MemoryCandidate,
    MemoryDraft,
    Message,
    Preview,
    Project,
    Run,
    Session,
    UsageEntry,
    Verification,
    utcnow,
)
from miniharness.workspace.cleanup import owned_path, remove_owned
from miniharness.workspace.paths import safe_path

if TYPE_CHECKING:
    from .manager import Runtime

logger = logging.getLogger(__name__)


def referenced_ids(value, identifiers: set[str]) -> set[str]:
    if isinstance(value, str):
        return {value} & identifiers
    if isinstance(value, dict):
        return (
            set().union(*(referenced_ids(item, identifiers) for item in value.values()))
            if value
            else set()
        )
    if isinstance(value, list):
        return (
            set().union(*(referenced_ids(item, identifiers) for item in value)) if value else set()
        )
    return set()


class DeletionService:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.root = runtime.settings.data_dir

    async def _rows(self, db, project_id: str, session_id: str | None):
        project = await db.get(Project, project_id, with_for_update=True)
        if not project:
            raise NotFound("项目不存在。")
        query = select(Session).where(Session.project_id == project_id)
        if session_id:
            query = query.where(Session.id == session_id)
        sessions = list(await db.scalars(query.with_for_update()))
        if session_id and not sessions:
            raise NotFound("会话不存在。")
        if any(row.active_run_id for row in sessions):
            raise Conflict("请先停止活动任务，再执行删除。")
        runs = list(
            await db.scalars(select(Run).where(Run.session_id.in_([row.id for row in sessions])))
        )
        if any(
            row.id in self.runtime.tasks and not self.runtime.tasks[row.id].done() for row in runs
        ):
            raise Conflict("任务正在完成收尾，请稍后重试删除。")
        return project, sessions, runs

    async def preview(self, project_id: str) -> dict:
        async with self.runtime.database.session() as db:
            project = await db.get(Project, project_id)
            if not project:
                raise NotFound("项目不存在。")
            saved_path = project.workspace_path
            if (
                not saved_path
                and (self.root / "projects" / (project_id + ".git")).exists()
                and await asyncio.to_thread(
                    self.runtime.workspace.files, project_id, project.public_snapshot
                )
            ):
                saved_path = str(owned_path(self.root, "retained", project_id))
            return {
                "preserved_path": saved_path,
                "deletes": ["会话工作区", "执行记录与检查点", "日志与补丁", "项目内部版本库"],
                "preserves": "已合并的项目代码，以及仍被其他项目使用的 Python 环境",
            }

    async def project(self, project_id: str) -> None:
        await self._delete(project_id, None)

    async def session(self, session_id: str) -> None:
        async with self.runtime.database.session() as db:
            row = await db.get(Session, session_id)
            if not row:
                raise NotFound("会话不存在。")
            project_id = row.project_id
        await self._delete(project_id, session_id)

    async def _delete(self, project_id: str, session_id: str | None) -> None:
        # 同时排除正在使用项目环境的验收；目录不能在验收进程下面消失。
        async with self.runtime.environments.lock("project:" + project_id):
            async with self.runtime.database.session.begin() as db:
                project, sessions, runs = await self._rows(db, project_id, session_id)
                session_ids, run_ids = [row.id for row in sessions], [row.id for row in runs]
                previews = list(
                    await db.scalars(select(Preview).where(Preview.session_id.in_(session_ids)))
                )
                merge_ids = [row.payload.get("verification", {}).get("id") for row in previews]
                verification_query = select(Verification).where(
                    Verification.project_id == project_id
                )
                if session_id:
                    verification_query = verification_query.where(
                        (Verification.run_id.in_(run_ids)) | Verification.id.in_(merge_ids)
                    )
                checks = list(await db.scalars(verification_query))
                paths = [("runs", row.id) for row in runs] + [
                    ("sessions", row.id) for row in sessions
                ]
                paths += [("verifications", row.id) for row in checks if row.run_id is None]
                # 先检查工作树归属，再用不跟随链接的目录清理。登记目录只匹配这些会话。
                work_paths = {self.runtime.session_workspaces.path(row) for row in sessions}
                registry = owned_path(self.root, "projects", project_id + ".git", "worktrees")
                for work in work_paths:
                    if work.exists() and (work / ".git").exists() and registry.exists():
                        await asyncio.to_thread(
                            self.runtime.workspace._worktree_gitdir, project_id, work
                        )
                if registry.exists():
                    for entry in registry.iterdir():
                        registered = owned_path(registry, entry.name)
                        backlink = registered / "gitdir"
                        if (
                            backlink.is_file()
                            and Path(backlink.read_text().strip()).resolve().parent in work_paths
                        ):
                            paths.append(("projects", project_id + ".git", "worktrees", entry.name))
                if not session_id:
                    paths += [
                        ("projects", project_id),
                        ("projects", project_id + ".git"),
                        ("environment-logs", project_id),
                    ]
                    environment_root = owned_path(self.root, "environments", project_id)
                    others = list(await db.scalars(select(Project).where(Project.id != project_id)))
                    default = await db.get(ApplicationSetting, "python_environment")
                    environments = [row.python_environment for row in others] + (
                        [default.value] if default else []
                    )
                    environments += [
                        {"python": sys.executable},
                        {"python": self.runtime.settings.default_python_environment},
                    ]
                    if not any(
                        value.get("python")
                        and Path(value["python"]).resolve().is_relative_to(environment_root)
                        for value in environments
                    ):
                        paths.append(("environments", project_id))
                # 全部路径在任何删除前检查，拒绝跨项目路径、目录链接和连接点。
                for parts in paths:
                    owned_path(self.root, *parts)
                for row in sessions:
                    if row.working_dir:
                        self.runtime.session_workspaces.path(row)
                if not session_id and not project.workspace_path:
                    names = (
                        await asyncio.to_thread(
                            self.runtime.workspace.files, project_id, project.public_snapshot
                        )
                        if (self.root / "projects" / (project_id + ".git")).exists()
                        else []
                    )
                    if names:
                        destination = owned_path(self.root, "retained", project_id)
                        for name in names:
                            target = safe_path(destination, name)
                            content = await asyncio.to_thread(
                                self.runtime.workspace.read,
                                project_id,
                                project.public_snapshot,
                                name,
                            )
                            if target.exists() and target.read_bytes() != content:
                                raise Conflict("保留目录已有不同内容，尚未删除项目。")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(content)
                        project.workspace_path = str(destination)
                if not session_id:
                    project.deleted_at = project.deleted_at or utcnow()
                for row in sessions:
                    row.deleted_at = row.deleted_at or utcnow()

            # 有失败就保留删除意图及记录，返回错误；重试/重启从确定的目录继续清理。
            try:
                for parts in paths:
                    await asyncio.to_thread(remove_owned, self.root, *parts)
                for identifier in run_ids:
                    await self.runtime.checkpointer.adelete_thread(identifier)
                async with self.runtime.database.session.begin() as db:
                    project, sessions, runs = await self._rows(db, project_id, session_id)
                    if session_id:
                        await self._retain_shared_suites(
                            db, project, session_ids, run_ids, [row.id for row in checks]
                        )
                    else:
                        await db.execute(
                            delete(AcceptanceSuite).where(AcceptanceSuite.project_id == project_id)
                        )
                    await db.execute(
                        delete(Verification).where(Verification.id.in_([row.id for row in checks]))
                    )
                    await db.execute(delete(Preview).where(Preview.session_id.in_(session_ids)))
                    for model in (
                        Message,
                        Event,
                        UsageEntry,
                        Boundary,
                        MemoryDraft,
                        MemoryCandidate,
                        Contribution,
                    ):
                        await db.execute(delete(model).where(model.run_id.in_(run_ids)))
                    await db.execute(delete(Memory).where(Memory.session_id.in_(session_ids)))
                    await db.execute(
                        update(Memory)
                        .where(Memory.source_run_id.in_(run_ids))
                        .values(source_run_id=None)
                    )
                    await db.execute(delete(Run).where(Run.id.in_(run_ids)))
                    await db.execute(delete(Session).where(Session.id.in_(session_ids)))
                    if not session_id:
                        await db.execute(delete(Memory).where(Memory.project_id == project_id))
                        await db.execute(
                            delete(MCPServer).where(MCPServer.project_id == project_id)
                        )
                        await db.execute(delete(Project).where(Project.id == project_id))
            except (OSError, RuntimeError, ValueError) as error:
                raise Conflict(f"清理未完成，可以重试删除。{error}") from error

    async def _retain_shared_suites(self, db, project, session_ids, run_ids, removed_checks):
        suites = list(
            await db.scalars(select(AcceptanceSuite).where(AcceptanceSuite.run_id.in_(run_ids)))
        )
        identifiers = {row.id for row in suites}
        references = referenced_ids(project.acceptance_suite_ids, identifiers)
        for row in await db.scalars(
            select(Session).where(Session.project_id == project.id, Session.id.not_in(session_ids))
        ):
            references |= referenced_ids(row.context, identifiers)
        for row in await db.scalars(
            select(Run).where(Run.project_id == project.id, Run.id.not_in(run_ids))
        ):
            references |= referenced_ids([row.state, row.start_context, row.result], identifiers)
        for row in await db.scalars(
            select(Preview).where(
                Preview.project_id == project.id, Preview.session_id.not_in(session_ids)
            )
        ):
            references |= referenced_ids(row.payload, identifiers)
        for row in await db.scalars(
            select(Verification).where(
                Verification.project_id == project.id, Verification.id.not_in(removed_checks)
            )
        ):
            references |= referenced_ids(row.suite_ids, identifiers)
        for row in suites:
            if row.id in references:
                row.run_id = None
            else:
                await db.delete(row)
        await db.flush()

    async def recover(self) -> None:
        async with self.runtime.database.session() as db:
            projects = list(
                await db.scalars(select(Project.id).where(Project.deleted_at.is_not(None)))
            )
            sessions = list(
                await db.scalars(
                    select(Session.id).where(
                        Session.deleted_at.is_not(None), Session.project_id.not_in(projects)
                    )
                )
            )
        for kind, identifiers in (("project", projects), ("session", sessions)):
            for identifier in identifiers:
                try:
                    await getattr(self, kind)(identifier)
                except Exception:
                    logger.exception("继续清理 %s %s 失败，保留删除请求供重试", kind, identifier)
