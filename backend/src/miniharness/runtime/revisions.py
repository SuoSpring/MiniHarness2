import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from sqlalchemy import select

from miniharness.memory.service import MemoryConflict, MemoryResolutions, MemoryService
from miniharness.runtime.errors import Conflict, NotFound
from miniharness.runtime.manager import Runtime, StatusResult, preview_text
from miniharness.storage.models import (
    AcceptanceSuite,
    Contribution,
    Event,
    Memory,
    MemoryDraft,
    Preview,
    Project,
    Run,
    Session,
    new_id,
    utcnow,
)
from miniharness.workspace.git_store import FileConflict, FileResolutions


class RevisionConflict(FileConflict):
    id: str


class RunContribution(TypedDict):
    run_id: str
    before: str
    after: str


class RevisionPreviewPayload(TypedDict):
    snapshot: str
    conflicts: list[RevisionConflict]
    memory_conflicts: list[MemoryConflict]
    diff: str
    resolutions: FileResolutions
    contributions: NotRequired[list[RunContribution]]
    run_id: NotRequired[str]
    merged: NotRequired[bool]
    memory_versions: NotRequired[dict[str, int]]
    generation: NotRequired[int]
    verification: NotRequired[dict[str, Any]]
    suite_ids: NotRequired[list[str]]
    contracts_ready: NotRequired[bool]
    no_file_changes: NotRequired[bool]
    archive_only: NotRequired[bool]


class RevisionPreview(RevisionPreviewPayload):
    token: str


class MergeCommitResult(TypedDict):
    status: Literal["merged", "archived"]
    snapshot: str
    cleanup_pending: NotRequired[bool]


@dataclass
class RunPublication:
    revision: int
    snapshot: str = ""
    suite_ids: list[str] = field(default_factory=list)
    contributions: list[RunContribution] = field(default_factory=list)
    error: str = ""
    conflict_paths: list[str] = field(default_factory=list)
    verification: dict | None = None


class Revisions:
    """预览只创建不可变候选；最终事务校验版本后一次切换有效指针。"""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.database = runtime.database

    async def _refresh_directory(self, project_id: str) -> None:
        async with self.database.session.begin() as db:
            project = await db.get(Project, project_id, with_for_update=True)
            if project is None or project.deleted_at:
                raise NotFound("项目不存在。")
            await self.runtime.project_files.refresh(project)

    async def _publish_transaction(self, operation, *args, **kwargs):
        async with self.runtime.project_files.publications() as publications:
            return await operation(*args, publications=publications, **kwargs)

    async def _prepare_run(self, run: Run, state: dict, snapshot: str) -> RunPublication | None:
        if (
            state.get("status") != "completed"
            or (state.get("task_kind") != "coding" and snapshot == run.start_snapshot)
            or state.get("pending_requirement_updates")
            or state.get("verification", {}).get("status") != "passed"
        ):
            return None
        async with self.database.session.begin() as db:
            project = await db.get(Project, run.project_id, with_for_update=True)
            try:
                await self.runtime.project_files.refresh(project)
            except (Conflict, OSError, ValueError) as exc:
                return RunPublication(project.revision, error=str(exc))
            suite_ids = await self.runtime.acceptance.required_ids(db, state)
            suites = await self.runtime.acceptance.load(
                db, project.id, list(dict.fromkeys([*project.acceptance_suite_ids, *suite_ids]))
            )
            prior = await db.scalars(
                select(Run)
                .where(
                    Run.session_id == run.session_id,
                    Run.merged.is_(False),
                    Run.status != "rolled_back",
                    Run.end_snapshot.is_not(None),
                    Run.start_snapshot != Run.end_snapshot,
                )
                .order_by(Run.created_at, Run.id)
            )
            changes = [
                (row.id, row.start_snapshot, row.end_snapshot) for row in prior if row.id != run.id
            ]
            changes.append((run.id, run.start_snapshot, snapshot))
        result = RunPublication(project.revision, suite_ids=[suite["id"] for suite in suites])
        result.snapshot, result.contributions, conflicts = await self.merge_changes(
            project.id, project.public_snapshot, changes, {}
        )
        if conflicts and len(changes) > 1:
            # 后续调用可能已经修正了之前的冲突。以最终通过验收的文件重新做三方合并。
            result.snapshot, net, conflicts = await self.merge_changes(
                project.id, project.public_snapshot, [(run.id, changes[0][1], snapshot)], {}
            )
            if not conflicts:
                result.contributions = [
                    {
                        "run_id": key,
                        "before": project.public_snapshot,
                        "after": project.public_snapshot,
                    }
                    for key, _, _ in changes[:-1]
                ] + net
        if conflicts:
            result.error = "项目与会话修改了相同内容，请在变更审阅中解决冲突。"
            result.conflict_paths = [item["path"] for item in conflicts]
        elif result.snapshot != snapshot or set(result.suite_ids) != set(suite_ids):
            # 其他会话的改动或新增用例必须在实际要写回的版本上通过。
            result.verification = await self.runtime.verification.run(
                project.id,
                result.snapshot,
                project.verification_policy,
                suite_ids=result.suite_ids,
            )
            if result.verification.get("status") != "passed":
                result.error = "合并后的项目版本未通过验收，已保留会话中的文件。"
        return result

    @asynccontextmanager
    async def finishing(self, run: Run, state: dict, snapshot: str):
        # 合并检查在事务外运行。若此时另一会话已写回，则根据新的公共版本重新准备。
        while True:
            candidate = await self._prepare_run(run, state, snapshot)
            async with (
                self.runtime.project_files.publications() as journals,
                self.database.session.begin() as db,
            ):
                project = await db.get(Project, run.project_id, with_for_update=True)
                if candidate and not candidate.error and project.revision == candidate.revision:
                    try:
                        await self.runtime.project_files.refresh(project)
                    except (Conflict, OSError, ValueError) as exc:
                        candidate.error = str(exc)
                if candidate and project.revision != candidate.revision:
                    continue
                yield db, project, candidate, journals
                return

    async def publish_finished(
        self, db, project: Project, run: Run, candidate: RunPublication, journals: list[str]
    ) -> dict:
        outcome = {"status": "pending", "path": project.workspace_path}
        if candidate.verification:
            outcome["verification_id"] = candidate.verification.get("id", "")
        if candidate.error:
            return {**outcome, "reason": candidate.error, "conflicts": candidate.conflict_paths}
        if candidate.verification and not await self.runtime.verification.accepted(
            db,
            candidate.verification,
            project.id,
            candidate.snapshot,
            project.verification_policy,
            0,
            suite_ids=candidate.suite_ids,
        ):
            return {**outcome, "reason": "合并验收结果已失效，请重新预览写回。"}
        new_directory = None
        try:
            if not project.workspace_path:
                new_directory = await self.runtime.project_files.new_directory(db, project.id)
                project.workspace_path = str(new_directory)
            journal = await asyncio.to_thread(
                self.runtime.project_files.publish,
                project,
                candidate.snapshot,
                initial_directory=new_directory is not None,
            )
            if journal:
                journals.append(journal)
        except (Conflict, OSError, ValueError) as exc:
            if new_directory is not None:
                project.workspace_path = None
                self.runtime.project_files.remove_empty(new_directory)
            return {**outcome, "reason": str(exc)}
        changed = project.public_snapshot != candidate.snapshot
        for part in candidate.contributions:
            row = (
                run
                if part["run_id"] == run.id
                else await db.get(Run, part["run_id"], with_for_update=True)
            )
            row.merged = True
            db.add(
                Contribution(
                    run_id=row.id, before_snapshot=part["before"], after_snapshot=part["after"]
                )
            )
        if (
            changed
            or new_directory is not None
            or project.acceptance_suite_ids != candidate.suite_ids
        ):
            project.revision += 1
        project.public_snapshot = candidate.snapshot
        project.acceptance_suite_ids = candidate.suite_ids
        return {"status": "written" if journal else "unchanged", "path": project.workspace_path}

    async def merge(
        self,
        session_id: str,
        token: str,
        resolutions: FileResolutions | None = None,
        *,
        archive: bool = False,
    ) -> MergeCommitResult:
        if archive:
            if resolutions:
                raise Conflict("归档只关闭对话，文件冲突请在预览合并中处理。")
            return await self._archive(session_id, token)
        return await self._publish_transaction(self._merge, session_id, token, resolutions)

    async def rollback(
        self,
        run_id: str,
        token: str,
        resolutions: FileResolutions | None = None,
        memory_resolutions: MemoryResolutions | None = None,
    ) -> StatusResult:
        return await self._publish_transaction(
            self._rollback, run_id, token, resolutions, memory_resolutions
        )

    async def merge_preview(
        self, session_id: str, resolutions: FileResolutions | None = None, *, archive: bool = False
    ) -> RevisionPreview:
        resolutions = resolutions or {}
        async with self.database.session() as db:
            selected = await db.get(Session, session_id)
            if selected is None:
                raise NotFound("会话不存在。")
            if archive:
                if resolutions:
                    raise Conflict("归档只关闭对话，文件冲突请在预览合并中处理。")
                if selected.archived_at or selected.active_run_id:
                    raise Conflict("只有已结束当前调用的未归档会话可以归档。")
                project = await db.get(Project, selected.project_id)
                return await self._save_preview(
                    project,
                    selected,
                    "archive",
                    {
                        "snapshot": selected.head_snapshot,
                        "archive_only": True,
                        "no_file_changes": selected.head_snapshot == selected.base_snapshot,
                        "diff": "",
                        "conflicts": [],
                        "memory_conflicts": [],
                        "resolutions": {},
                    },
                )
        await self._refresh_directory(selected.project_id)
        async with self.database.session() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise NotFound("会话不存在。")
            self._check_merge_mode(session)
            if session.active_run_id:
                raise Conflict("请先结束或撤回当前调用，再合并。")
            project = await db.get(Project, session.project_id)
            runs = list(
                await db.scalars(
                    select(Run)
                    .where(
                        Run.session_id == session_id,
                        Run.merged.is_(False),
                        Run.end_snapshot.is_not(None),
                        Run.status != "rolled_back",
                    )
                    .order_by(Run.created_at)
                )
            )
            changed_runs = [run for run in runs if run.start_snapshot != run.end_snapshot]
            contracts_ready = not changed_runs or await self.runtime.acceptance.has_final(
                db, changed_runs[-1].state
            )
            suites = await self.runtime.acceptance.load(
                db,
                project.id,
                list(
                    dict.fromkeys(
                        [
                            *project.acceptance_suite_ids,
                            *session.context.get("acceptance_suite_ids", []),
                        ]
                    )
                ),
            )
            suite_ids = [suite["id"] for suite in suites]
        current, contributions, conflicts = await self.merge_changes(
            project.id,
            project.public_snapshot,
            [(run.id, run.start_snapshot, cast(str, run.end_snapshot)) for run in runs],
            resolutions,
        )
        diff = await asyncio.to_thread(
            self.runtime.workspace.diff, project.id, project.public_snapshot, current
        )
        payload: RevisionPreviewPayload = {
            "snapshot": current,
            "contributions": contributions,
            "conflicts": conflicts,
            "memory_conflicts": [],
            "diff": preview_text(diff),
            "resolutions": resolutions,
            "suite_ids": suite_ids,
            "contracts_ready": contracts_ready,
            "no_file_changes": not changed_runs,
        }
        if not conflicts and (changed_runs or suite_ids):
            payload["verification"] = await self.runtime.verification.run(
                project.id, current, project.verification_policy, suite_ids=suite_ids
            )
            if not contracts_ready:
                payload["verification"] = {
                    **payload["verification"],
                    "status": "failed",
                    "error": "最近的代码修改尚未建立最终独立测试契约，请先在会话中完成验收。",
                }
        return await self._save_preview(project, session, "merge", payload)

    async def merge_changes(
        self,
        project_id: str,
        current: str,
        changes: list[tuple[str, str, str]],
        resolutions: FileResolutions,
    ) -> tuple[str, list[RunContribution], list[RevisionConflict]]:
        contributions: list[RunContribution] = []
        conflicts: list[RevisionConflict] = []
        for run_id, start, end in changes:
            chosen = {
                key.split(":", 1)[1]: value
                for key, value in resolutions.items()
                if key.startswith(run_id + ":")
            }
            if current == start and not chosen:
                result = {"snapshot": end, "conflicts": []}
            else:
                result = await asyncio.to_thread(
                    self.runtime.workspace.merge, project_id, start, current, end, chosen
                )
            if result["conflicts"]:
                conflicts.extend(
                    {**item, "id": f"{run_id}:{item['path']}"} for item in result["conflicts"]
                )
                break  # 后一段必须基于已解决的前段，避免错误地提前套用。
            snapshot = cast(str, result["snapshot"])  # 无冲突的合并一定有快照。
            contributions.append({"run_id": run_id, "before": current, "after": snapshot})
            current = snapshot
        return current, contributions, conflicts

    async def _merge(
        self,
        session_id: str,
        token: str,
        resolutions: FileResolutions | None = None,
        *,
        publications: list[str],
    ) -> MergeCommitResult:
        async with self.database.session.begin() as db:
            preview = await db.get(Preview, token)
            if preview is None or preview.session_id != session_id or preview.kind != "merge":
                raise Conflict("合并预览不存在，请重新预览。")
            project = await db.get(Project, preview.project_id, with_for_update=True)
            session = await db.get(Session, session_id, with_for_update=True)
            self._check_merge_mode(session)
            await self.runtime.project_files.refresh(project)
            self._check_preview(preview, project, session, resolutions)
            if session.active_run_id:
                raise Conflict("会话已有新的活动调用，请重新预览。")
            if (
                not preview.payload.get("no_file_changes")
                or preview.payload.get("verification") is not None
            ) and (
                not preview.payload.get("contracts_ready")
                or not await self.runtime.verification.accepted(
                    db,
                    preview.payload.get("verification", {}),
                    project.id,
                    preview.payload["snapshot"],
                    project.verification_policy,
                    0,
                    suite_ids=preview.payload.get("suite_ids", []),
                )
            ):
                raise Conflict("合并候选版本的必需检查未全部通过，请查看验收结果、修复后重新预览。")
            for part in preview.payload["contributions"]:
                run = await db.get(Run, part["run_id"], with_for_update=True)
                run.merged = True
                db.add(
                    Contribution(
                        run_id=run.id, before_snapshot=part["before"], after_snapshot=part["after"]
                    )
                )
            publication = await asyncio.to_thread(
                self.runtime.project_files.publish, project, preview.payload["snapshot"]
            )
            if publication:
                publications.append(publication)
            project.public_snapshot = preview.payload["snapshot"]
            if not preview.payload.get("no_file_changes"):
                project.acceptance_suite_ids = preview.payload["suite_ids"]
                project.revision += 1
            session.head_snapshot = session.base_snapshot = project.public_snapshot
            session.context = self._sync_context(
                session.context, "已合并到项目公共版本，请按实际文件继续。"
            )
            session.context = {
                **session.context,
                "acceptance_suite_ids": project.acceptance_suite_ids,
            }
            session.revision += 1
        return {"status": "merged", "snapshot": project.public_snapshot}

    async def _archive(self, session_id: str, token: str) -> MergeCommitResult:
        async with self.database.session.begin() as db:
            preview = await db.get(Preview, token)
            session = await db.get(Session, session_id, with_for_update=True)
            if (
                session is None
                or not preview
                or preview.kind != "archive"
                or preview.session_id != session_id
            ):
                raise Conflict("归档预览不存在，请重新预览。")
            if not preview.payload.get("archive_only"):
                raise Conflict("归档方式已更新，请重新预览关闭会话。")
            if (
                session.active_run_id
                or session.archived_at
                or session.revision != preview.session_revision
            ):
                raise Conflict("归档预览已过期，请重新预览。")
            session.archived_at = utcnow()
            session.undo_run_id = None
            session.revision += 1
        cleanup = (
            session.workspace_mode == "worktree"
            and not await self.runtime.session_workspaces.cleanup_archived(session_id)
        )
        return {"status": "archived", "snapshot": session.head_snapshot, "cleanup_pending": cleanup}

    @staticmethod
    def _check_merge_mode(session: Session) -> None:
        if session.archived_at:
            raise Conflict("会话已归档。")

    async def rollback_preview(
        self, run_id: str, resolutions: FileResolutions | None = None
    ) -> RevisionPreview:
        run = await self.runtime.get_run(run_id)
        await self._refresh_directory(run.project_id)
        if run.status == "running" or (
            run.status == "paused"
            and run.interrupt
            and run.interrupt.get("kind") == "inflight_cancelled"
        ):
            async with self.database.session() as db:
                session = await db.get(Session, run.session_id)
                if session.undo_run_id != run_id:
                    raise Conflict("只能撤回每个会话最新一次调用，不能连续回退历史。")
            await self.runtime.stop(run_id)
            run = await self.runtime.get_run(run_id)
        async with self.database.session() as db:
            session = await db.get(Session, run.session_id)
            if session.undo_run_id != run_id or run.status == "rolled_back":
                raise Conflict("只能撤回每个会话最新一次调用，不能连续回退历史。")
            project = await db.get(Project, run.project_id)
            parts = list(
                await db.scalars(
                    select(Contribution).where(
                        Contribution.run_id == run_id, Contribution.reverted.is_(False)
                    )
                )
            )
            memory_conflicts = await MemoryService.rollback_conflicts(db, run_id)
            memory_versions = {}
            for draft in await db.scalars(
                select(MemoryDraft).where(
                    MemoryDraft.run_id == run_id, MemoryDraft.published.is_(True)
                )
            ):
                memory = await db.get(Memory, draft.memory_id)
                memory_versions[draft.memory_id] = memory.revision if memory else 0
        current = project.public_snapshot
        conflicts: list[RevisionConflict] = []
        resolutions = resolutions or {}
        for part in reversed(parts):
            chosen = {
                key.split(":", 1)[1]: value
                for key, value in resolutions.items()
                if key.startswith(run.id + ":")
            }
            result = await asyncio.to_thread(
                self.runtime.workspace.merge,
                project.id,
                part.after_snapshot,
                current,
                part.before_snapshot,
                chosen,
            )
            if result["conflicts"]:
                conflicts.extend(
                    {**item, "id": f"{run.id}:{item['path']}"} for item in result["conflicts"]
                )
                break
            current = cast(str, result["snapshot"])  # 无冲突的合并一定有快照。
        if parts:
            diff = await asyncio.to_thread(
                self.runtime.workspace.diff, project.id, project.public_snapshot, current
            )
        else:
            present = run.end_snapshot or await asyncio.to_thread(
                self.runtime.workspace.snapshot, project.id, Path(run.working_dir)
            )
            diff = await asyncio.to_thread(
                self.runtime.workspace.diff, project.id, present, run.start_snapshot
            )
        payload: RevisionPreviewPayload = {
            "run_id": run_id,
            "snapshot": current,
            "merged": bool(parts),
            "conflicts": conflicts,
            "memory_conflicts": memory_conflicts,
            "memory_versions": memory_versions,
            "diff": preview_text(diff),
            "resolutions": resolutions,
            "generation": run.generation,
        }
        return await self._save_preview(project, session, "rollback", payload)

    async def _rollback(
        self,
        run_id: str,
        token: str,
        resolutions: FileResolutions | None = None,
        memory_resolutions: MemoryResolutions | None = None,
        *,
        publications: list[str],
    ) -> StatusResult:
        run = await self.runtime.get_run(run_id)
        async with self.database.session.begin() as db:
            preview = await db.get(Preview, token)
            if preview is None or preview.kind != "rollback" or preview.payload["run_id"] != run_id:
                raise Conflict("撤回预览不存在，请重新预览。")
            project = await db.get(Project, run.project_id, with_for_update=True)
            session = await db.get(Session, run.session_id, with_for_update=True)
            row = await db.get(Run, run_id, with_for_update=True)
            self._check_preview(preview, project, session, resolutions)
            if row.status == "running" or row.generation != preview.payload["generation"]:
                raise Conflict("调用已继续或执行状态已改变，请重新暂停并预览。")
            if session.undo_run_id != run_id or row.status == "rolled_back":
                raise Conflict("最新调用已改变，请重新预览。")
            for memory_id, revision in preview.payload["memory_versions"].items():
                memory = await db.get(Memory, memory_id, with_for_update=True)
                if (memory.revision if memory else 0) != revision:
                    raise Conflict("预览之后共享记忆又发生变化，请重新预览。")
            await MemoryService.rollback(db, run_id, memory_resolutions or {})
            if preview.payload["merged"]:
                publication = await asyncio.to_thread(
                    self.runtime.project_files.publish, project, preview.payload["snapshot"]
                )
                if publication:
                    publications.append(publication)
                project.public_snapshot = preview.payload["snapshot"]
                owned = list(
                    await db.scalars(
                        select(AcceptanceSuite).where(AcceptanceSuite.run_id == run_id)
                    )
                )
                removed = {suite.id for suite in owned}
                remaining = [
                    identifier
                    for identifier in project.acceptance_suite_ids
                    if identifier not in removed
                ]
                # 撤回本次发布的测试纠正时，恢复调用开始时继承的契约；其他分支的后继版本仍优先。
                inherited = set(row.start_context.get("acceptance_suite_ids", []))
                restore = {
                    old
                    for suite in owned
                    if suite.id in project.acceptance_suite_ids
                    for old in suite.scope.get("replaces", [])
                    if old in inherited
                }
                if restore:
                    available = await db.scalars(
                        select(AcceptanceSuite.id).where(
                            AcceptanceSuite.project_id == project.id,
                            AcceptanceSuite.id.in_(restore),
                            AcceptanceSuite.active.is_(True),
                        )
                    )
                    combined = list(dict.fromkeys([*remaining, *available]))
                    remaining = [
                        s["id"]
                        for s in await self.runtime.acceptance.load(db, project.id, combined)
                    ]
                project.acceptance_suite_ids = remaining
                project.revision += 1
                session.head_snapshot = session.base_snapshot = project.public_snapshot
                session.context = dict(row.start_context)
                if project.public_snapshot != row.start_snapshot:
                    session.context = self._sync_context(
                        session.context, "最新调用已撤回，会话已同步到撤销后的公共文件版本。"
                    )
                if project.acceptance_suite_ids or "acceptance_suite_ids" in session.context:
                    session.context = {
                        **session.context,
                        "acceptance_suite_ids": project.acceptance_suite_ids,
                    }
                if session.workspace_mode == "worktree":
                    await self.runtime.session_workspaces.restore(
                        session, project.public_snapshot, committed=True
                    )
            else:
                session.head_snapshot, session.context = row.start_snapshot, row.start_context
                if session.workspace_mode == "worktree":
                    await self.runtime.session_workspaces.restore(
                        session, row.start_snapshot, committed=True
                    )
            session.active_run_id = session.undo_run_id = None
            session.revision += 1
            row.status, row.generation, row.interrupt = "rolled_back", row.generation + 1, None
            for part in await db.scalars(select(Contribution).where(Contribution.run_id == run_id)):
                part.reverted = True
            db.add(
                Event(
                    run_id=run_id,
                    kind="result",
                    payload={
                        "status": "rolled_back",
                        "summary": "已恢复文件、有效上下文与记忆，并从会话历史移除本次调用。",
                    },
                )
            )
        return {"status": "rolled_back"}

    async def replace(
        self,
        run_id: str,
        task: str,
        mode: str | None = None,
        token_budget: int | None = None,
        token: str | None = None,
        resolutions: FileResolutions | None = None,
        memory_resolutions: MemoryResolutions | None = None,
        permission_policy: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = await self.runtime.get_run(run_id)
        if token is None:
            preview = await self.rollback_preview(run_id, resolutions)
            if preview["conflicts"] or preview["memory_conflicts"]:
                raise Conflict("替换前需要先解决撤回冲突，请打开撤回预览。")
            token = preview["token"]
        await self.rollback(run_id, token, resolutions, memory_resolutions)
        return await self.runtime.start(
            run.session_id,
            task,
            mode or run.mode,
            token_budget if token_budget is not None else run.token_budget,
            permission_policy=permission_policy
            if permission_policy is not None
            else run.permission_policy,
            limits=limits if limits is not None else run.limits,
        )

    async def _save_preview(
        self,
        project: Project,
        session: Session,
        kind: Literal["merge", "archive", "rollback"],
        payload: RevisionPreviewPayload,
    ) -> RevisionPreview:
        row = Preview(
            id=new_id(),
            project_id=project.id,
            session_id=session.id,
            kind=kind,
            project_revision=project.revision,
            session_revision=session.revision,
            payload=payload,
        )
        async with self.database.session.begin() as db:
            db.add(row)
        return {"token": row.id, **payload}

    @staticmethod
    def _check_preview(
        preview: Preview, project: Project, session: Session, resolutions: FileResolutions | None
    ) -> None:
        if (
            preview.project_revision != project.revision
            or preview.session_revision != session.revision
        ):
            raise Conflict("预览已过期，请根据最新文件重新预览。")
        if preview.payload["conflicts"]:
            raise Conflict("请解决冲突并重新生成预览后提交。")
        if (resolutions or {}) != preview.payload["resolutions"]:
            raise Conflict("冲突选择发生变化，请重新预览。")

    @staticmethod
    def _sync_context(context: dict[str, Any], note: str) -> dict[str, Any]:
        return {
            **context,
            "summary": (context.get("summary", "") + "\n文件同步说明：" + note).strip(),
        }
