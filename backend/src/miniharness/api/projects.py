from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from miniharness.runtime.environments import DefaultEnvironment, EnvironmentSelection
from miniharness.runtime.errors import Conflict, NotFound
from miniharness.runtime.limits import ProjectRunSettings
from miniharness.runtime.manager import Runtime, public_row
from miniharness.storage.models import AcceptanceSuite, Message, Project, Run, Session, Verification
from miniharness.verification.contracts import VerificationPolicy

from .dependencies import runtime
from .schemas import AcceptanceRetirement, ProjectCreate, ProjectPath, SessionCreate, SessionUpdate

router = APIRouter(tags=["项目与会话"])


@router.get("/runs/{run_id}/acceptance", response_model=None)
async def acceptance_suites(
    run_id: str, service: Runtime = Depends(runtime), verification_id: str | None = None
) -> list[dict]:
    run = await service.get_run(run_id)
    verification = (run.result or {}).get("verification") or run.state.get("verification") or {}
    used = verification.get("suite_ids")
    async with service.database.session() as db:
        record_id = verification_id or verification.get("id")
        if record_id:
            record = await db.get(Verification, record_id)
            if (
                record is not None
                and record.run_id == run_id
                and record.project_id == run.project_id
            ):
                used = record.suite_ids
            elif record is not None or record_id != verification.get("id"):
                raise NotFound("本次调用没有这份验收记录。")
            # 早期记录可能只在该调用的结果中保留了用例编号。
        rows = await db.execute(
            select(AcceptanceSuite, Session.id, Session.title)
            .outerjoin(Run, AcceptanceSuite.run_id == Run.id)
            .outerjoin(
                Session,
                (Session.id == Run.session_id)
                & (Session.project_id == run.project_id)
                & Session.deleted_at.is_(None),
            )
            .where(
                AcceptanceSuite.project_id == run.project_id,
                (AcceptanceSuite.run_id == run_id) | AcceptanceSuite.id.in_(used or []),
            )
            .order_by(AcceptanceSuite.created_at)
        )
        return [
            {
                **public_row(row),
                "used_in_verification": row.id in used if used is not None else None,
                "from_current_run": row.run_id == run_id,
                "source_session": {"id": session_id, "title": title} if session_id else None,
            }
            for row, session_id, title in rows
        ]


@router.get("/projects/{project_id}/verifications/{verification_id}/cases", response_model=None)
async def verification_cases(
    project_id: str, verification_id: str, service: Runtime = Depends(runtime)
) -> dict:
    return await service.verification.case_results(project_id, verification_id)


@router.post("/projects/{project_id}/acceptance/{suite_id}/retire", response_model=None)
async def retire_acceptance(
    project_id: str, suite_id: str, body: AcceptanceRetirement, service: Runtime = Depends(runtime)
) -> dict:
    async with service.database.session.begin() as db:
        project = await db.get(Project, project_id, with_for_update=True)
        suite = await db.get(AcceptanceSuite, suite_id, with_for_update=True)
        if project is None or suite is None or suite.project_id != project_id:
            raise NotFound("独立测试契约不存在。")
        if await db.scalar(
            select(Session.id)
            .where(Session.project_id == project_id, Session.active_run_id.is_not(None))
            .limit(1)
        ):
            raise Conflict("项目仍有活动调用，不能修改冻结测试；请先结束或撤回调用。")
        suite.active, suite.retirement_reason = False, body.reason
        project.acceptance_suite_ids = [
            identifier for identifier in project.acceptance_suite_ids if identifier != suite_id
        ]
        project.revision += 1
        return {"id": suite_id, "active": False, "reason": body.reason}


@router.get("/projects", response_model=None)
async def projects(service: Runtime = Depends(runtime)) -> list[dict[str, Any]]:
    async with service.database.session() as db:
        return [
            public_row(row)
            for row in await db.scalars(select(Project).order_by(Project.created_at.desc()))
        ]


@router.post("/projects", status_code=201, response_model=None)
async def create_project(
    body: ProjectCreate, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    return await service.create_project(body.name, body.environment, body.workspace_path)


@router.get("/projects/{project_id}/run-settings", response_model=None)
async def project_run_settings(project_id: str, service: Runtime = Depends(runtime)) -> dict:
    async with service.database.session() as db:
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at:
            raise NotFound("项目不存在。")
        return ProjectRunSettings.resolve(service.settings, project.run_settings).model_dump()


@router.put("/projects/{project_id}/run-settings", response_model=None)
async def save_project_run_settings(
    project_id: str, body: ProjectRunSettings, service: Runtime = Depends(runtime)
) -> dict:
    async with service.database.session.begin() as db:
        project = await db.get(Project, project_id, with_for_update=True)
        if project is None or project.deleted_at:
            raise NotFound("项目不存在。")
        settings = ProjectRunSettings.resolve(
            service.settings, {**project.run_settings, **body.model_dump(exclude_unset=True)}
        )
        project.run_settings = settings.model_dump()
        return project.run_settings


@router.put("/projects/{project_id}/path", response_model=None)
async def project_path(
    project_id: str, body: ProjectPath, service: Runtime = Depends(runtime)
) -> dict:
    return await service.project_files.configure(project_id, body.workspace_path)


@router.get("/projects/{project_id}/deletion-preview", response_model=None)
async def project_deletion_preview(project_id: str, service: Runtime = Depends(runtime)) -> dict:
    return await service.deletions.preview(project_id)


@router.get("/settings/python-environment", response_model=None)
async def environment_settings(service: Runtime = Depends(runtime)) -> dict:
    return {
        "default": await service.environments.defaults(),
        "environments": await service.environments.choices(),
    }


@router.put("/settings/python-environment", response_model=None)
async def update_environment_settings(
    body: DefaultEnvironment, service: Runtime = Depends(runtime)
) -> dict:
    return await service.environments.save_default(body)


@router.get("/projects/{project_id}/environment", response_model=None)
async def project_environment(project_id: str, service: Runtime = Depends(runtime)) -> dict:
    return asdict(await service.environments.get(project_id))


@router.put("/projects/{project_id}/environment", response_model=None)
async def update_project_environment(
    project_id: str, body: EnvironmentSelection, service: Runtime = Depends(runtime)
) -> dict:
    return await service.environments.configure(project_id, body)


@router.get("/projects/{project_id}/verification", response_model=None)
async def verification_policy(project_id: str, service: Runtime = Depends(runtime)) -> dict:
    async with service.database.session() as db:
        project = await db.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在。")
        return project.verification_policy


@router.put("/projects/{project_id}/verification", response_model=None)
async def update_verification_policy(
    project_id: str, body: VerificationPolicy, service: Runtime = Depends(runtime)
) -> dict:
    async with service.database.session.begin() as db:
        project = await db.get(Project, project_id, with_for_update=True)
        if project is None:
            raise NotFound("项目不存在。")
        active = await db.scalar(
            select(Session.id)
            .where(Session.project_id == project_id, Session.active_run_id.is_not(None))
            .limit(1)
        )
        if active:
            raise Conflict("项目中有活动调用，请结束或撤回后再修改必需检查。")
        project.verification_policy = body.model_dump()
        project.revision += 1  # 使所有旧的合并预览和检查失效。
        return project.verification_policy


@router.get("/projects/{project_id}/sessions", response_model=None)
async def sessions(
    project_id: str, summary: bool = False, service: Runtime = Depends(runtime)
) -> list[dict[str, Any]]:
    async with service.database.session() as db:
        if summary:
            rows = await db.execute(
                select(
                    Session.id,
                    Session.project_id,
                    Session.title,
                    Session.workspace_mode,
                    Session.archived_at,
                    Session.deleted_at,
                    Session.active_run_id,
                    Session.revision,
                    Session.created_at,
                    Run.status.label("active_status"),
                )
                .outerjoin(Run, Run.id == Session.active_run_id)
                .where(Session.project_id == project_id)
                .order_by(Session.created_at.desc())
            )
            return [dict(row) for row in rows.mappings()]
        return [
            public_row(row)
            for row in await db.scalars(
                select(Session)
                .where(Session.project_id == project_id)
                .order_by(Session.created_at.desc())
            )
        ]


@router.post("/projects/{project_id}/sessions", status_code=201, response_model=None)
async def create_session(
    project_id: str, body: SessionCreate, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    return await service.create_session(project_id, body.title, body.workspace_mode)


@router.get("/sessions/{session_id}", response_model=None)
async def session(session_id: str, service: Runtime = Depends(runtime)) -> dict[str, Any]:
    async with service.database.session() as db:
        row = await db.get(Session, session_id)
        if row is None:
            raise NotFound("会话不存在。")
        active = await db.get(Run, row.active_run_id) if row.active_run_id else None
        return {
            **public_row(row),
            "summary": row.context.get("summary", ""),
            "active_status": active.status if active else None,
        }


@router.put("/sessions/{session_id}", response_model=None)
async def update_session(
    session_id: str, body: SessionUpdate, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    async with service.database.session.begin() as db:
        row = await db.get(Session, session_id, with_for_update=True)
        if row is None:
            raise NotFound("会话不存在。")
        if row.archived_at:
            raise Conflict("会话已归档，不能修改固定约束。")
        row.pinned = body.pinned
        row.revision += 1
        return public_row(row)


def _history_runs(session_id: str):
    return (Run.session_id == session_id, Run.status != "rolled_back")


def _history_messages(session_id: str):
    return (
        select(Message)
        .join(Run)
        .where(Message.session_id == session_id, *_history_runs(session_id))
    )


@router.get("/sessions/{session_id}/messages", response_model=None)
async def messages(
    session_id: str, after: int = 0, limit: int = 200, service: Runtime = Depends(runtime)
) -> list[dict[str, Any]]:
    async with service.database.session() as db:
        rows = await db.scalars(
            _history_messages(session_id)
            .where(Message.id > after)
            .order_by(Message.id)
            .limit(min(max(limit, 1), 1000))
        )
        return [public_row(row) for row in rows]


@router.get("/sessions/{session_id}/messages/page", response_model=None)
async def message_page(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    run_id: str | None = None,
    service: Runtime = Depends(runtime),
) -> dict:
    async with service.database.session() as db:
        history = _history_messages(session_id)
        if run_id:
            run = await db.get(Run, run_id)
            if run is None or run.session_id != session_id:
                raise NotFound("执行记录不属于当前会话。")
            history = history.where(Message.run_id == run_id)
        total = await db.scalar(history.with_only_columns(func.count(), maintain_column_froms=True))
        rows = await db.scalars(
            history.order_by(Message.id).offset((page - 1) * page_size).limit(page_size)
        )
        return {
            "items": [public_row(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, service: Runtime = Depends(runtime)) -> Response:
    await service.deletions.session(session_id)
    return Response(status_code=204)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, service: Runtime = Depends(runtime)) -> Response:
    await service.deletions.project(project_id)
    return Response(status_code=204)


@router.get("/sessions/{session_id}/runs", response_model=None)
async def runs(
    session_id: str, summary: bool = False, service: Runtime = Depends(runtime)
) -> list[dict[str, Any]]:
    async with service.database.session() as db:
        if summary:
            rows = await db.execute(
                select(
                    Run.id,
                    Run.session_id,
                    Run.task,
                    Run.status,
                    Run.mode,
                    Run.permission_policy,
                    Run.token_budget,
                    Run.created_at,
                    Run.result["summary"].as_string().label("reply"),
                    Run.state["user_updates"].label("user_updates"),
                    Run.state["applied_instruction_ids"].label("applied_ids"),
                    Run.instructions,
                )
                .where(*_history_runs(session_id))
                .order_by(Run.created_at.desc(), Run.id.desc())
            )
            turns = []
            for row in rows.mappings():
                turn = dict(row)
                applied = set(turn.pop("applied_ids") or [])
                updates = turn.pop("user_updates") or []
                instructions = turn.pop("instructions") or []
                turn["followups"] = updates + [
                    item["text"] for item in instructions if item["id"] not in applied
                ]
                turns.append(turn)
            return turns
        return [
            service.public_run(row)
            for row in await db.scalars(
                select(Run)
                .where(*_history_runs(session_id))
                .order_by(Run.created_at.desc(), Run.id.desc())
            )
        ]
