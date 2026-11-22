import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from heapq import nsmallest
from itertools import chain
from operator import itemgetter
from pathlib import Path
from typing import Any, TypeAlias, TypedDict, cast
from weakref import WeakValueDictionary

from langchain_core.messages import AIMessage, messages_to_dict
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, Command
from sqlalchemy import delete, func, select

from miniharness.agent import GraphConfig, GraphState, build_graph, initial_state
from miniharness.agent.contracts import GraphInstruction, GraphServices
from miniharness.agent.planning import planning_answer_text
from miniharness.config import Settings
from miniharness.context import close_tool_calls, finished_context
from miniharness.context.manager import review_turn_history
from miniharness.memory import MemoryService
from miniharness.memory.consolidation import RankedBatch, prepare_memories
from miniharness.permissions import PermissionPolicy
from miniharness.runtime.acceptance import AcceptanceService
from miniharness.runtime.budget import ModelGateway, model_request_deadline
from miniharness.runtime.deletion import DeletionService
from miniharness.runtime.environments import EnvironmentSelection, EnvironmentService
from miniharness.runtime.errors import BudgetExceeded, Conflict, NotFound
from miniharness.runtime.limits import ProjectRunSettings, RunLimits
from miniharness.runtime.project_files import ProjectFiles
from miniharness.runtime.services import RunServices
from miniharness.runtime.verification import VerificationService
from miniharness.runtime.workspaces import SessionWorkspaces
from miniharness.storage import Database
from miniharness.storage.models import (
    Base,
    Event,
    Memory,
    MemoryCandidate,
    MemoryDraft,
    Message,
    Project,
    Run,
    Session,
    Verification,
    new_id,
    utcnow,
)
from miniharness.tools.files import (
    DEFAULT_DIRECTORY_LIMIT,
    DIRECTORY_PAGE_BYTES,
    MAX_DIRECTORY_LIMIT,
)
from miniharness.verification.acceptance import milestone_scope
from miniharness.verification.contracts import default_policy, legacy_default_policy
from miniharness.workspace import GitWorkspace

logger = logging.getLogger(__name__)
TERMINAL = {
    "completed",
    "blocked",
    "budget_exceeded",
    "failed",
    "crashed",
    "rolled_back",
    "cancelled",
}
WAITING = {"paused", "awaiting_approval", "awaiting_tool_approval", "awaiting_input"}

GraphInput: TypeAlias = GraphState | Command[None] | None


class StatusResult(TypedDict):
    status: str


class LogSlice(TypedDict):
    content: str
    next_offset: int
    total_bytes: int


class LogEntry(TypedDict):
    path: str
    size: int


class LogPage(TypedDict):
    logs: list[LogEntry]
    total: int
    next_cursor: str | None


def public_row(row: Base) -> dict[str, Any]:
    from sqlalchemy import inspect

    return {column.key: getattr(row, column.key) for column in inspect(row).mapper.column_attrs}


DIFF_PREVIEW_LIMIT = 50_000


def preview_text(text: str, limit: int = DIFF_PREVIEW_LIMIT) -> str:
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")[:limit]


class Runtime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        checkpointer: Checkpointer,
        gateway_factory: Callable[[str | None], ModelGateway] | None = None,
    ) -> None:
        self.settings, self.database, self.checkpointer = settings, database, checkpointer
        self.settings.data_dir = self.settings.data_dir.resolve()
        self.workspace = GitWorkspace(self.settings.data_dir / "projects")
        self.session_workspaces = SessionWorkspaces(self)
        self.environments = EnvironmentService(self)
        self.project_files = ProjectFiles(self)
        self.deletions = DeletionService(self)
        self.verification = VerificationService(self)
        self.acceptance = AcceptanceService(self)
        self.gateway_factory = gateway_factory or (
            lambda run_id: ModelGateway(settings, database, run_id)
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.memory_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def run_dir(self, run_id: str) -> Path:
        return self.settings.data_dir / "runs" / run_id

    def public_run(self, run: Run) -> dict[str, Any]:
        return {
            **public_row(run),
            "limits": run.limits or None,  # 旧记录未保存限制，不用当前配置冒充历史配置。
            "patch_available": (self.run_dir(run.id) / "changes.patch").is_file(),
        }

    async def get_run(self, run_id: str) -> Run:
        async with self.database.session() as db:
            run = await db.get(Run, run_id)
            if run is None:
                raise NotFound("调用不存在。")
            return run

    async def create_project(
        self,
        name: str,
        environment: EnvironmentSelection | None = None,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        project_id = new_id()
        configured = await self.environments.prepare(
            project_id,
            environment or EnvironmentSelection(),
            self.settings.data_dir / "environment-logs" / project_id / new_id(),
        )
        snapshot = await asyncio.to_thread(self.workspace.ensure_project, project_id)
        async with self.database.session.begin() as db:
            row = Project(
                id=project_id,
                name=name,
                public_snapshot=snapshot,
                revision=0,
                python_environment=configured,
                run_settings=ProjectRunSettings.resolve(self.settings).model_dump(),
            )
            db.add(row)
        try:
            await self.project_files.configure(project_id, workspace_path)
        except Exception:
            await self.deletions.project(project_id)
            raise
        async with self.database.session() as db:
            row = await db.get(Project, project_id)
        return public_row(row)

    async def create_session(
        self, project_id: str, title: str, workspace_mode: str = "isolated"
    ) -> dict[str, Any]:
        if workspace_mode not in {"isolated", "worktree"}:
            raise ValueError("未知的工作区模式")
        async with self.database.session.begin() as db:
            project = await db.get(Project, project_id, with_for_update=True)
            if project is None or project.deleted_at:
                raise NotFound("项目不存在。")
            await self.project_files.refresh(project)
            row = Session(
                id=new_id(),
                project_id=project_id,
                title=title,
                workspace_mode=workspace_mode,
                head_snapshot=project.public_snapshot,
                base_snapshot=project.public_snapshot,
                context={"acceptance_suite_ids": project.acceptance_suite_ids}
                if project.acceptance_suite_ids
                else {},
                pinned=[],
                crash_notes=[],
                revision=0,
            )
            db.add(row)
        return public_row(row)

    async def start(
        self,
        session_id: str,
        task: str,
        mode: str = "react",
        token_budget: int | None = None,
        permission_policy: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        continued_from: str | None = None,
    ) -> dict[str, Any]:
        permissions = PermissionPolicy.model_validate(permission_policy or {}).model_dump()
        async with self.database.session.begin() as db:
            session = await db.get(Session, session_id)
            if session is None or session.deleted_at:
                raise NotFound("会话不存在。")
            project = await db.get(Project, session.project_id, with_for_update=True)
            session = await db.get(
                Session, session_id, with_for_update=True, populate_existing=True
            )
            if project is None or project.deleted_at or session.deleted_at:
                raise NotFound("项目或会话已删除。")
            project_settings = ProjectRunSettings.resolve(self.settings, project.run_settings)
            selected_limits = RunLimits.resolve(
                self.settings, {**project_settings.run_limits(), **(limits or {})}
            )
            if session.active_run_id:
                raise Conflict("本会话已有活动调用，请继续、追加指令或撤回。")
            if session.archived_at:
                raise Conflict("会话已归档，不能启动新的调用。请创建新会话。")
            continuation = {}
            if continued_from:
                previous = await db.get(Run, continued_from)
                if (
                    previous is None
                    or previous.session_id != session_id
                    or previous.status != "budget_exceeded"
                    or previous.finished_at is None
                ):
                    raise Conflict("只能接续本会话已完成收尾的预算耗尽任务。")
                continuation = {
                    "source_run_id": previous.id,
                    "files_changed_since_source": session.head_snapshot != previous.end_snapshot,
                    "summary": (previous.result or {}).get("summary", ""),
                    "plan": previous.state.get("plan", {}),
                    "goal": previous.state.get("task", previous.task),
                    "criteria": previous.state.get("criteria", []),
                    "step_index": previous.state.get("step_index", 0),
                    "report": previous.state.get("report", ""),
                    "last_tool": previous.last_operation.get("name", ""),
                    "result_unknown": previous.last_operation.get("result") == "pending",
                    "instruction": "从会话当前文件继续源任务未完成部分，保留之后的有效修改。先检查文件和来源运行的日志，旧报告可能已过时；结果未知的工具不得盲目重放。结合用户后续要求确认当前验收目标。",
                }
            run_id = new_id()
            if session.workspace_mode == "worktree":
                target = await self.session_workspaces.prepare(session)
                # 活动调用尚未提交；异常退出后的下一次启动必须恢复 head_snapshot。
                session.workspace_snapshot = None
            else:
                target = self.run_dir(run_id) / "work"
                await asyncio.to_thread(
                    self.workspace.materialize, session.project_id, session.head_snapshot, target
                )
            self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
            state = initial_state(
                project_id=session.project_id,
                session_id=session_id,
                run_id=run_id,
                task=task,
                mode=mode,
                permission_policy=permissions,
                messages=session.context.get("messages", []),
                summary=session.context.get("summary", ""),
                pinned=session.pinned,
                crash_notes=session.crash_notes,
                verification_policy=project.verification_policy,
                baseline_suite_ids=session.context.get("acceptance_suite_ids", []),
            )
            state["snapshot_id"] = session.head_snapshot
            if continuation:
                state["continuation"] = continuation
            run = Run(
                id=run_id,
                project_id=session.project_id,
                session_id=session_id,
                task=task,
                mode=mode,
                permission_policy=permissions,
                status="running",
                start_snapshot=session.head_snapshot,
                start_context=session.context,
                state=state,
                working_dir=str(target),
                token_budget=token_budget
                if token_budget is not None
                else project_settings.token_budget,
                limits=selected_limits.model_dump(),
                usage={},
                generation=0,
            )
            db.add(run)
            await db.flush()
            if session.title == "新会话" and not await db.scalar(
                select(Run.id).where(Run.session_id == session_id, Run.id != run_id).limit(1)
            ):
                session.title = " ".join(task.split())[:60]
            session.active_run_id = run_id
            session.undo_run_id = run_id
            session.revision += 1
            db.add(
                Message(session_id=session_id, run_id=run_id, role="user", content={"text": task})
            )
        self.launch(run_id, state)
        return public_row(run)

    def launch(self, run_id: str, graph_input: GraphInput) -> None:
        if run_id in self.tasks and not self.tasks[run_id].done():
            raise Conflict("该调用正在执行。")
        task = asyncio.create_task(self.drive(run_id, graph_input), name=f"run:{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(
            lambda finished: (
                self.tasks.pop(run_id, None) if self.tasks.get(run_id) is finished else None
            )
        )

    def graph(
        self, services: GraphServices
    ) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
        limits = RunLimits.resolve(self.settings, getattr(services, "limits", None))
        return build_graph(
            services,
            self.checkpointer,
            legacy_intent=bool(getattr(services, "legacy_intent", False)),
            config=GraphConfig(
                context_tokens=limits.max_context_tokens,
                output_tokens=limits.max_output_tokens,
                max_tool_calls=limits.max_tool_calls,
                max_model_rounds=limits.max_rounds,
                compression_threshold=limits.compression_threshold,
                memory_context_tokens=self.settings.memory_context_tokens,
                memory_auto_share=self.settings.memory_auto_share,
            ),
        )

    async def drive(self, run_id: str, graph_input: GraphInput) -> None:
        run = await self.get_run(run_id)
        services = RunServices(self, run)
        if isinstance(graph_input, Command) and isinstance(graph_input.resume, dict):
            services.resumed_approval_operation = graph_input.resume.get("_approval_operation")
            services.legacy_approval_operation = graph_input.resume.get(
                "_legacy_approval_operation"
            )
        started = time.monotonic()
        latest = cast(GraphState, run.state)
        limits = RunLimits.resolve(self.settings, run.limits)
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 2147483647}
        remaining = (
            max(0.01, limits.run_timeout - run.usage.get("active_seconds", 0))
            if limits.run_timeout
            else None
        )
        deadline_token = model_request_deadline.set(
            asyncio.get_running_loop().time() + remaining if remaining is not None else None
        )
        try:
            async with asyncio.timeout(remaining):
                # 启动时的向量记忆请求及其网络重试也必须受任务总时限约束。
                if isinstance(graph_input, dict) and "task" in graph_input:
                    if self.settings.memory_recommendations:
                        memories = await services.memory.recall(
                            run.project_id,
                            run.session_id,
                            run.task,
                            run.id,
                            limit=self.settings.memory_recommendations,
                        )
                        graph_input["memory_context"] = [
                            json.dumps(item, ensure_ascii=False) for item in memories
                        ]
                    catalog = (await services.skills()).catalog()
                    graph_input["memory_context"].extend(
                        "可用 Skill，按需 load_skill："
                        + json.dumps(
                            {"name": item["name"], "description": item["description"]},
                            ensure_ascii=False,
                        )
                        for item in catalog
                        if item["enabled"]
                    )
                graph = self.graph(services)
                async for state in graph.astream(
                    graph_input, config, stream_mode="values", durability="sync"
                ):
                    latest = cast(
                        GraphState,
                        {key: value for key, value in state.items() if key != "__interrupt__"},
                    )
                    async with self.database.session.begin() as db:
                        row = await db.get(Run, run_id, with_for_update=True)
                        if row.generation != services.generation:
                            raise asyncio.CancelledError
                        row.state = latest
                    await services.event(
                        "state",
                        {
                            "status": latest.get("status"),
                            "next_node": latest.get("next_node"),
                            "step_index": latest.get("step_index"),
                        },
                    )
            saved = await graph.aget_state(config)
            if saved.interrupts:
                payload = saved.interrupts[0].value
                if not isinstance(payload, dict):
                    payload = {"status": "awaiting_input", "reason": str(payload)}
                async with self.database.session.begin() as db:
                    row = await db.get(Run, run_id, with_for_update=True)
                    row.status = payload.get("status", "awaiting_input")
                    row.interrupt, row.state = payload, latest
                await services.event("interrupted", payload)
            else:
                await self.finish(run_id, latest)
        except asyncio.CancelledError:
            raise
        except (BudgetExceeded, TimeoutError) as error:
            latest = {
                **latest,
                "status": "budget_exceeded",
                "summary_output": str(error) or "达到执行时间上限。",
            }
            await self.finish(run_id, latest)
        except Conflict as error:
            async with self.database.session.begin() as db:
                row = await db.get(Run, run_id, with_for_update=True)
                row.status = "awaiting_input"
                row.interrupt = {
                    "status": "awaiting_input",
                    "kind": "memory_conflict",
                    "reason": str(error),
                    "questions": [
                        "共享记忆有版本冲突：输入 keep 保留当前共享记忆，或 restore 使用本次候选记忆。"
                    ],
                }
            await services.event("interrupted", {"reason": str(error), "kind": "memory_conflict"})
        except Exception as error:
            logger.exception("调用 %s 执行异常", run_id)
            latest = {
                **latest,
                "status": "failed",
                "summary_output": f"{type(error).__name__}: {error}",
            }
            try:
                await self.finish(run_id, latest)
            except Conflict as conflict:
                async with self.database.session.begin() as db:
                    row = await db.get(Run, run_id, with_for_update=True)
                    row.state, row.status = latest, "awaiting_input"
                    row.interrupt = {
                        "status": "awaiting_input",
                        "kind": "memory_conflict",
                        "reason": str(conflict),
                        "questions": ["输入 keep 保留当前共享记忆，或 restore 使用本次候选记忆。"],
                    }
            except Exception as commit_error:
                logger.exception("调用 %s 无法完整提交，撤销候选状态", run_id)
                await self.abort(run_id, f"提交失败：{type(commit_error).__name__}: {commit_error}")
            await services.event("error", {"error_type": type(error).__name__, "error": str(error)})
        finally:
            model_request_deadline.reset(deadline_token)
            async with self.database.session.begin() as db:
                row = await db.get(Run, run_id, with_for_update=True)
                usage = dict(row.usage)
                usage["active_seconds"] = (
                    usage.get("active_seconds", 0) + time.monotonic() - started
                )
                row.usage = usage

    async def finish(self, run_id: str, state: GraphState) -> None:
        run = await self.get_run(run_id)
        if run.finished_at:
            return
        snapshot = await asyncio.to_thread(
            self.workspace.snapshot, run.project_id, Path(run.working_dir)
        )
        state = {
            **state,
            "snapshot_id": snapshot,
            "snapshot_revision": state.get("file_revision", 0),
        }
        patch = await asyncio.to_thread(
            self.workspace.diff, run.project_id, run.start_snapshot, snapshot
        )
        patch_path = self.run_dir(run_id) / "changes.patch"
        patch_path.write_bytes(patch.encode("utf-8", "surrogateescape"))
        services = RunServices(self, run)

        async def consolidate(messages):
            return await services.model("memory_consolidation", state, messages, schema=RankedBatch)

        memory_lock = self.memory_locks.setdefault(run_id, asyncio.Lock())
        async with memory_lock:
            memory_outcome = await prepare_memories(
                services.memory,
                run_id,
                consolidate,
                match_limit=self.settings.memory_match_limit,
                state=state,
            )
        from .revisions import Revisions

        revisions = Revisions(self)
        async with revisions.finishing(run, state, snapshot) as (db, project, candidate, journals):
            session = await db.get(Session, run.session_id, with_for_update=True)
            row = await db.get(Run, run_id, with_for_update=True)
            if row.finished_at or row.status == "rolled_back":
                return
            if session.active_run_id != run_id:
                raise Conflict("调用已被替代，不能发布迟到结果。")
            suite_ids = await self.acceptance.required_ids(db, state)
            if state.get("status") == "completed" and state.get("task_kind") not in {
                "answer",
                "coding",
            }:
                state = {
                    **state,
                    "status": "blocked",
                    "summary_output": "审查尚未明确验收类型，不能标记任务完成。",
                }
            if state.get("status") == "completed" and state.get("pending_requirement_updates"):
                state = {
                    **state,
                    "status": "blocked",
                    "review": {},
                    "summary_output": "最新用户要求尚未完成需求整理与验收，候选成果已保留但未标记完成。",
                }
            if (
                state.get("status") == "completed"
                and (state.get("task_kind") == "coding" or snapshot != row.start_snapshot)
                and (
                    not await self.acceptance.has_final(db, state)
                    or not await self.verification.accepted(
                        db,
                        state.get("verification", {}),
                        row.project_id,
                        snapshot,
                        project.verification_policy,
                        state.get("task_revision", 1),
                        row.id,
                        suite_ids=suite_ids,
                        scope=milestone_scope(state, complete=True),
                    )
                )
            ):
                state = {
                    **state,
                    "status": "blocked",
                    "review": {},
                    "summary_output": "最终文件版本没有通过全部必需检查，候选成果已保留但未标记完成。",
                    "verification": {
                        **state.get("verification", {}),
                        "status": "stale",
                        "error": "验证记录缺失或与最终文件、需求、检查配置不一致，需要重新验收。",
                        "checks": state.get("verification", {}).get("checks", []),
                    },
                }
            publication = None
            if state.get("status") == "completed" and candidate:
                publication = await revisions.publish_finished(
                    db, project, row, candidate, journals
                )
                if publication["status"] == "pending":
                    state = {
                        **state,
                        "status": "blocked",
                        "summary_output": f"会话版本已通过验收，项目文件待写回。{publication['reason']}\n\n{state.get('summary_output', '')}",
                    }
            await MemoryService.publish(db, row)
            written = await db.scalar(
                select(func.count(MemoryDraft.id)).where(
                    MemoryDraft.run_id == run_id, MemoryDraft.published.is_(True)
                )
            )
            memory_result = {
                "status": "published"
                if memory_outcome["status"] == "ready" or written
                else memory_outcome["status"],
                "written": written,
                **({"error": memory_outcome["error"]} if memory_outcome.get("error") else {}),
            }
            row.memory_preparation = memory_result
            if memory_outcome["status"] != "failed":
                await db.execute(delete(MemoryCandidate).where(MemoryCandidate.run_id == run_id))
            row.status = state.get("status", "failed")
            final = AIMessage(content=state.get("summary_output", ""), id=f"{run_id}:final")
            final_message = messages_to_dict([final])
            effective_messages = close_tool_calls(
                state.get("messages", []), state.get("summary_output", "调用已结束")
            )
            state = {**state, "messages": [*effective_messages, *final_message]}
            row.state, row.end_snapshot, row.finished_at = state, snapshot, utcnow()
            row.interrupt = None
            patch_preview = preview_text(patch, DIFF_PREVIEW_LIMIT + 1)
            row.result = {
                "status": row.status,
                **({"publication": publication} if publication else {}),
                "summary": state.get("summary_output", ""),
                "verified": row.status == "completed",
                "diff": patch_preview[:DIFF_PREVIEW_LIMIT],
                "diff_truncated": len(patch_preview) > DIFF_PREVIEW_LIMIT,
                "has_changes": run.start_snapshot != snapshot,
                "snapshot": snapshot,
                "review": state.get("review", {}),
                "memory": memory_result,
                "verification": state.get("verification", {}),
            }
            session.head_snapshot = snapshot
            if session.workspace_mode == "worktree":
                session.workspace_snapshot = snapshot
            session.context = finished_context(
                row.start_context,
                run_id=run_id,
                request=row.task,
                user_updates=state.get("user_updates", []),
                final_message=final,
            )
            if suite_ids:
                session.context = {**session.context, "acceptance_suite_ids": suite_ids}
            if publication and publication["status"] != "pending":
                session.head_snapshot = session.base_snapshot = project.public_snapshot
                session.context = {
                    **session.context,
                    "acceptance_suite_ids": project.acceptance_suite_ids,
                }
            session.active_run_id = None
            session.undo_run_id = run_id
            session.revision += 1
            db.add(
                Message(
                    session_id=session.id, run_id=run_id, role="assistant", content=final_message[0]
                )
            )
            db.add(Event(run_id=run_id, kind="result", payload=row.result))

        if state.get("status") == "completed":
            await self.session_workspaces.prune_patches(run.session_id)

    async def pause(self, run_id: str) -> StatusResult:
        async with self.database.session.begin() as db:
            run = await db.get(Run, run_id, with_for_update=True)
            if run is None:
                raise NotFound("调用不存在。")
            if run.status != "running":
                raise Conflict("只有执行中的调用可以请求暂停。")
            run.pause_requested = True
        return {"status": "pause_requested"}

    async def resume(
        self,
        run_id: str,
        answer: object = True,
        *,
        token_budget: int | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = await self.get_run(run_id)
        if run.status == "budget_exceeded":
            # 已提交的运行不重开；新运行继承成果和有效上下文，旧工具与账本不重放。
            return await self.start(
                run.session_id,
                run.state.get("task", run.task),
                run.mode,
                token_budget,
                permission_policy=run.permission_policy,
                limits=limits,
                continued_from=run.id,
            )
        if run.status not in WAITING:
            raise Conflict("只有暂停或等待输入的调用可以继续。")
        if token_budget is not None or limits is not None:
            raise Conflict("预算调整仅用于已结束的预算耗尽任务。")
        if self.tasks.get(run_id) and not self.tasks[run_id].done():
            previous_task = self.tasks[run_id]
            try:
                await asyncio.shield(previous_task)
            except asyncio.CancelledError:
                if not previous_task.cancelled():
                    raise
        if run.interrupt and run.interrupt.get("kind") == "memory_conflict":
            choice = (
                answer
                if isinstance(answer, str)
                else answer.get("text")
                if isinstance(answer, dict)
                else ""
            )
            if choice not in {"keep", "restore"}:
                raise Conflict("请输入 keep 或 restore 解决共享记忆发布冲突。")
            async with self.database.session.begin() as db:
                drafts = list(
                    await db.scalars(select(MemoryDraft).where(MemoryDraft.run_id == run_id))
                )
                for draft in drafts:
                    if choice == "keep":
                        await db.delete(draft)
                    else:
                        current = await db.scalar(
                            select(Memory).where(
                                Memory.project_id == run.project_id,
                                Memory.session_id == draft.session_id,
                                Memory.key == draft.key,
                            )
                        )
                        if current:
                            draft.memory_id = current.id
                        draft.expected_revision = current.revision if current else 0
                        draft.revision = draft.expected_revision + 1
            await self.finish(run_id, cast(GraphState, run.state))
            return public_row(await self.get_run(run_id))
        async with self.database.session.begin() as db:
            session = await db.get(Session, run.session_id, with_for_update=True)
            row = await db.get(Run, run_id, with_for_update=True)
            if row.status not in WAITING:
                raise Conflict("调用已经继续，请勿重复恢复。")
            reply_text = answer if isinstance(answer, str) else ""
            if (row.interrupt or {}).get("kind") == "plan_clarification":
                try:
                    reply_text = planning_answer_text(row.interrupt, answer)
                except ValueError as exc:
                    raise Conflict(str(exc)) from exc
            if row.status == "awaiting_tool_approval":
                if (
                    not isinstance(answer, dict)
                    or answer.get("request_id") != (row.interrupt or {}).get("request_id")
                    or answer.get("decision") not in {"approve", "approve_similar", "reject"}
                ):
                    raise Conflict("请针对当前操作明确批准或拒绝，普通继续不能代替权限审批。")
                if answer["decision"] == "approve_similar" and not row.interrupt.get(
                    "similar_approval"
                ):
                    raise Conflict("本次操作只支持单次审批。")
                answer = {"decision": answer["decision"], "request_id": answer["request_id"]}
                answer["_approval_operation"] = row.interrupt.get("operation_id")
                if not row.interrupt.get("approval_version"):
                    answer["_legacy_approval_operation"] = row.interrupt.get("operation_id")
            cancelled_inflight = bool(
                row.interrupt and row.interrupt.get("kind") == "inflight_cancelled"
            )
            row.status, row.pause_requested, row.interrupt = "running", False, None
            session.revision += 1
            pinned = session.pinned
            project = await db.get(Project, run.project_id)
            verification_policy = project.verification_policy
            db.add(
                Message(
                    session_id=run.session_id,
                    run_id=run_id,
                    role="user",
                    content={
                        "text": (reply_text if not cancelled_inflight else "") or "继续执行",
                        "kind": "resume",
                        "answer": answer,
                    },
                )
            )
        graph = self.graph(RunServices(self, run))
        graph_config = {"configurable": {"thread_id": run_id}}
        checkpoint = await graph.aget_state(graph_config)
        compatibility: GraphState = {"pinned": pinned}
        previous = checkpoint.values or run.state
        turns = review_turn_history(run.start_context.get("messages", []))
        if previous.get("prior_review_turns") != turns:
            compatibility["prior_review_turns"] = turns
            if "prior_review_turns" not in previous and (
                previous.get("review_messages") or previous.get("review_summary")
            ):
                compatibility["review_messages"] = [*turns, *previous.get("review_messages", [])]
        if not previous.get("verification_policy") or (
            previous["verification_policy"] == legacy_default_policy()
            and verification_policy == default_policy()
        ):
            compatibility.update(
                verification_policy=verification_policy,
                verification={},
                verification_requested=False,
                seen_verification_id="",
            )
        if "test_contract_floor" not in previous:
            compatibility.update(
                test_contract_floor=1,
                test_scope={},
                test_job={},
                pending_review={},
                baseline_suite_ids=run.start_context.get("acceptance_suite_ids", []),
                acceptance_suite_ids=[],
            )
        if checkpoint.values:
            await graph.aupdate_state(graph_config, compatibility)
            graph_input = (
                None if cancelled_inflight and not checkpoint.interrupts else Command(resume=answer)
            )
        else:
            # 可能在首次模型/记忆请求期间取消，还没有图检查点；此时没有图工具可重放。
            graph_input = cast(GraphState, {**run.state, **compatibility})
        if cancelled_inflight and isinstance(answer, (str, dict)):
            text = answer if isinstance(answer, str) else answer.get("text", "")
            spec_update = answer.get("spec_update") if isinstance(answer, dict) else None
            if text or spec_update:
                await self.steer(run_id, text, spec_update)
        self.launch(run_id, graph_input)
        return public_row(await self.get_run(run_id))

    async def steer(
        self, run_id: str, text: str, spec_update: dict[str, object] | None = None
    ) -> StatusResult:
        initial = await self.get_run(run_id)
        async with self.database.session.begin() as db:
            session = await db.get(Session, initial.session_id, with_for_update=True)
            run = await db.get(Run, run_id, with_for_update=True)
            if run is None:
                raise NotFound("调用不存在。")
            if run.status in TERMINAL:
                raise Conflict("调用已结束，请提交新请求或替换最新调用。")
            if run.state.get("next_node") == "finish" and run.state.get("status") in TERMINAL:
                raise Conflict("当前任务正在保存最终结果，请在完成后发送这条消息。")
            instruction: GraphInstruction = {
                "id": new_id(),
                "text": text,
                "received_at": time.time(),
            }
            if spec_update:
                instruction["spec_update"] = spec_update
            run.instructions = [*run.instructions, instruction]
            session.revision += 1
            db.add(
                Message(
                    session_id=run.session_id,
                    run_id=run_id,
                    role="user",
                    content={"text": text, "kind": "instruction", "spec_update": spec_update},
                )
            )
        return {"status": "queued"}

    async def stop(self, run_id: str) -> None:
        initial = await self.get_run(run_id)
        task = self.tasks.get(run_id)
        async with self.database.session.begin() as db:
            session = await db.get(Session, initial.session_id, with_for_update=True)
            run = await db.get(Run, run_id, with_for_update=True)
            if run.status != "running":
                if not (
                    run.status == "paused"
                    and run.interrupt
                    and run.interrupt.get("kind") == "inflight_cancelled"
                ):
                    return
            else:
                if session.active_run_id != run_id:
                    raise Conflict("调用已被替代，不能停止其他活动调用。")
                run.generation += 1
                run.status, run.pause_requested = "paused", False
                run.interrupt = {
                    "status": "paused",
                    "kind": "inflight_cancelled",
                    "reason": "已取消正在执行的请求并冻结现场；可撤回，也可放弃撤回后继续。",
                }
                session.revision += 1
                db.add(Event(run_id=run_id, kind="interrupted", payload=run.interrupt))
                # 在会话锁内发出取消，防止旧任务抢先发布结束结果；退出事务后等待清理。
                if task and not task.done():
                    task.cancel()
        if task and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise

    async def recover(self) -> None:
        await self.project_files.recover()
        async with self.database.session.begin() as db:
            for check in await db.scalars(
                select(Verification).where(Verification.status == "running")
            ):
                check.status, check.finished_at = "error", utcnow()
                check.error = "进程退出导致验证结果未知，需要重新运行。"
            for run in await db.scalars(select(Run).where(Run.status == "running")):
                session = await db.get(Session, run.session_id, with_for_update=True)
                note = f"调用 {run.id} 异常退出，未发布其文件与上下文。最后操作：{json.dumps(run.last_operation, ensure_ascii=False)[:3000]}。没有已记录异常堆栈时根因未知，请先检查导致故障的操作。"
                run.status, run.finished_at = "crashed", utcnow()
                run.result = {"status": "crashed", "summary": note, "verified": False}
                run.generation += 1
                session.active_run_id = None
                session.undo_run_id = None
                session.crash_notes = [*session.crash_notes, note]
                session.revision += 1
                await db.execute(
                    delete(MemoryDraft).where(
                        MemoryDraft.run_id == run.id, MemoryDraft.published.is_(False)
                    )
                )
                await db.execute(delete(MemoryCandidate).where(MemoryCandidate.run_id == run.id))
                db.add(Event(run_id=run.id, kind="error", payload=run.result))

        async with self.database.session() as db:
            archived = list(
                await db.scalars(
                    select(Session.id).where(
                        Session.archived_at.is_not(None), Session.working_dir.is_not(None)
                    )
                )
            )
        for session_id in archived:
            await self.session_workspaces.cleanup_archived(session_id)
        await self.deletions.recover()

    async def abort(self, run_id: str, reason: str) -> None:
        """执行仍存活但提交失败时也结束调用，不能遗留没有任务的 running 状态。"""
        initial = await self.get_run(run_id)
        async with self.database.session.begin() as db:
            session = await db.get(Session, initial.session_id, with_for_update=True)
            run = await db.get(Run, run_id, with_for_update=True)
            if run.finished_at:
                return
            note = f"调用 {run_id} 已撤销未提交成果。{reason}。最后操作：{json.dumps(run.last_operation, ensure_ascii=False)[:2000]}"
            run.status, run.finished_at = "crashed", utcnow()
            run.generation += 1
            run.result = {"status": "crashed", "summary": note, "verified": False}
            if session.active_run_id == run_id:
                session.active_run_id = session.undo_run_id = None
            session.crash_notes = [*session.crash_notes, note]
            session.revision += 1
            await db.execute(
                delete(MemoryDraft).where(
                    MemoryDraft.run_id == run_id, MemoryDraft.published.is_(False)
                )
            )
            await db.execute(delete(MemoryCandidate).where(MemoryCandidate.run_id == run_id))
            db.add(Event(run_id=run_id, kind="error", payload=run.result))

    async def shutdown(self) -> None:
        # 正常退出优先走安全暂停；超过当前节点时限则保留 running 标记供重启回退。
        ids = list(self.tasks)
        for run_id in ids:
            run = await self.get_run(run_id)
            if run.status == "running":
                await self.pause(run_id)
        tasks = [self.tasks[run_id] for run_id in ids if run_id in self.tasks]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.settings.command_timeout + 2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _log_entries(self, run_id: str) -> Iterator[LogEntry]:
        root = self.run_dir(run_id)
        # 验收按固定布局写日志，工作副本及依赖目录无需递归遍历。
        candidates = chain(root.glob("logs/**/*.log"), root.glob("verification/*/*/logs/*.log"))
        for target in candidates:
            path = target.relative_to(root).as_posix()
            try:
                info = self.read_log(run_id, path, limit=0)
            except (ValueError, OSError):
                continue
            yield {"path": path, "size": info["total_bytes"]}

    def list_logs(self, run_id: str) -> list[LogEntry]:
        """工作台日志选择器的完整索引。"""
        return sorted(self._log_entries(run_id), key=itemgetter("path"))

    def list_log_page(
        self, run_id: str, cursor: str = "", limit: int = DEFAULT_DIRECTORY_LIMIT
    ) -> LogPage:
        """供模型按日志路径续页，只保留一页候选及一个前瞻项。"""
        if not isinstance(cursor, str):
            raise ValueError("cursor 必须是上页返回的字符串，第一页请省略")
        if type(limit) is not int or not 1 <= limit <= MAX_DIRECTORY_LIMIT:
            raise ValueError(f"limit 必须是 1 到 {MAX_DIRECTORY_LIMIT} 之间的整数")
        total = 0

        def after_cursor() -> Iterator[LogEntry]:
            nonlocal total
            for entry in self._log_entries(run_id):
                total += 1
                if entry["path"] > cursor:
                    yield entry

        candidates = nsmallest(limit + 1, after_cursor(), key=itemgetter("path"))
        logs = []
        page_bytes = 2
        for entry in candidates[:limit]:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + (2 if logs else 0)
            if logs and page_bytes + size > DIRECTORY_PAGE_BYTES:
                break
            logs.append(entry)
            page_bytes += size
        return {
            "logs": logs,
            "total": total,
            "next_cursor": logs[-1]["path"] if len(logs) < len(candidates) else None,
        }

    def read_log(self, run_id: str, path: str, offset: int = 0, limit: int = 16000) -> LogSlice:
        from miniharness.tools import safe_path

        if path in {"stdout_log", "stderr_log"}:
            raise ValueError(
                f"{path} 是返回字段名，不是日志路径。请使用该字段的实际值，"
                "例如 logs/调用编号/stdout.log；没有引用时先调用 list_logs(run_id=来源运行编号)。"
            )
        target = safe_path(self.run_dir(run_id), path)
        parts = target.relative_to(self.run_dir(run_id).resolve()).parts
        verification_log = (
            len(parts) == 5
            and parts[0] == "verification"
            and parts[3] == "logs"
            and parts[4] in {"stdout.log", "stderr.log"}
        )
        if not (parts and parts[0] == "logs") and not verification_log:
            raise ValueError(
                "只能读取该调用的日志；先用 list_logs 获取真实路径，项目源码请用 read_file。"
            )
        if not target.is_file():
            raise FileNotFoundError(
                f"该运行不存在日志 {path}；请用 list_logs 核对路径和来源 run_id。"
            )
        with target.open("rb") as stream:
            stream.seek(max(0, offset))
            raw = stream.read(min(limit, 65536))
            end = stream.tell()
        # 页尾不拆开 UTF-8 字符，下一页从完整字符起点读取。
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        content = decoder.decode(raw, final=end >= target.stat().st_size)
        remaining = decoder.getstate()[0]
        return {
            "content": content,
            "next_offset": end - len(remaining),
            "total_bytes": target.stat().st_size,
        }
