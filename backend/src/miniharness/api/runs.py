import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from miniharness.runtime.errors import Conflict
from miniharness.runtime.limits import RunLimits
from miniharness.runtime.manager import TERMINAL, WAITING, Runtime, StatusResult
from miniharness.runtime.revisions import RevisionPreview, Revisions
from miniharness.runtime.timeline import timeline_page, verification_payloads
from miniharness.storage.models import (
    Event,
    MemoryCandidate,
    MemoryDraft,
    Run,
    Session,
    UsageEntry,
    utcnow,
)

from .dependencies import runtime
from .schemas import (
    ApplyPreview,
    Approval,
    Instruction,
    PreviewInput,
    Replace,
    Resume,
    RunCreate,
    ToolApproval,
)

router = APIRouter(tags=["执行与控制"])


@router.get("/settings/run-limits", response_model=None)
async def run_limits(service: Runtime = Depends(runtime)) -> dict:
    return RunLimits.resolve(service.settings).model_dump()


@router.post("/sessions/{session_id}/runs", status_code=202, response_model=None)
async def start(
    session_id: str, body: RunCreate, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    values = body.model_dump()
    values["limits"] = body.limits.model_dump(exclude_unset=True) if body.limits else None
    return await service.start(session_id, **values)


@router.get("/runs/{run_id}", response_model=None)
async def run(run_id: str, service: Runtime = Depends(runtime)) -> dict[str, Any]:
    result = service.public_run(await service.get_run(run_id))
    # 规划与执行共享对话历史。独立验收、摘要和嵌入请求有各自的输入。
    async with service.database.session() as db:
        latest = (
            await db.execute(
                select(UsageEntry.input_tokens, UsageEntry.estimated)
                .where(UsageEntry.run_id == run_id, UsageEntry.role.in_(("planner", "executor")))
                .order_by(UsageEntry.id.desc())
                .limit(1)
            )
        ).one_or_none()
    if latest is not None:
        result["usage"] = {
            **(result.get("usage") or {}),
            "context_tokens": latest.input_tokens,
            "context_estimated": latest.estimated,
        }
    return result


@router.post("/runs/{run_id}/pause", response_model=None)
async def pause(run_id: str, service: Runtime = Depends(runtime)) -> StatusResult:
    return await service.pause(run_id)


@router.post("/runs/{run_id}/retry", status_code=202, response_model=None)
async def retry(run_id: str, service: Runtime = Depends(runtime)) -> dict[str, Any]:
    row = await service.get_run(run_id)
    if row.status not in {"failed", "crashed", "blocked", "budget_exceeded", "cancelled"}:
        raise Conflict("只有未完成或已停止的任务可以重试。")
    return await service.start(
        row.session_id,
        row.task,
        row.mode,
        permission_policy=row.permission_policy,
    )


@router.post("/runs/{run_id}/cancel", response_model=None)
async def cancel(run_id: str, service: Runtime = Depends(runtime)) -> StatusResult:
    from sqlalchemy import delete

    await service.stop(run_id)
    original = await service.get_run(run_id)
    async with service.database.session.begin() as db:
        session = await db.get(Session, original.session_id, with_for_update=True)
        run = await db.get(Run, run_id, with_for_update=True)
        if run.status in TERMINAL:
            return {"status": run.status}
        if session.active_run_id != run_id:
            raise Conflict("当前任务已改变，请刷新后重试。")
        run.status, run.finished_at, run.interrupt = "cancelled", utcnow(), None
        run.generation += 1
        run.result = {
            "status": "cancelled",
            "summary": "任务已停止，未提交的修改不会发布。",
            "verified": False,
        }
        session.active_run_id = session.undo_run_id = None
        session.revision += 1
        await db.execute(
            delete(MemoryDraft).where(
                MemoryDraft.run_id == run_id, MemoryDraft.published.is_(False)
            )
        )
        await db.execute(delete(MemoryCandidate).where(MemoryCandidate.run_id == run_id))
        db.add(Event(run_id=run_id, kind="finished", payload=run.result))
    return {"status": "cancelled"}


@router.get("/runs/{run_id}/timeline", response_model=None)
async def timeline(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    kind: str | None = None,
    through: int | None = Query(None, ge=0),
    view: Literal["activity", "raw"] = "raw",
    service: Runtime = Depends(runtime),
) -> dict:
    async with service.database.session() as db:
        return await timeline_page(db, run_id, page, page_size, kind, through, view)


@router.post("/runs/{run_id}/resume", status_code=202, response_model=None)
async def resume(run_id: str, body: Resume, service: Runtime = Depends(runtime)) -> dict[str, Any]:
    return await service.resume(
        run_id,
        body.answer,
        token_budget=body.token_budget,
        limits=body.limits.model_dump(exclude_unset=True) if body.limits is not None else None,
    )


@router.post("/runs/{run_id}/approve", status_code=202, response_model=None)
async def approve(
    run_id: str, body: Approval, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    row = await service.get_run(run_id)
    if row.status != "awaiting_approval":
        raise Conflict("当前没有等待审批的计划。")
    if row.state.get("pending_plan", {}).get("version") != body.plan_version:
        raise Conflict("计划版本已改变，请刷新后审批。")
    return await service.resume(
        run_id,
        {
            "action": {"changes": "revise"}.get(body.decision, body.decision),
            "feedback": body.feedback,
        },
    )


@router.post("/runs/{run_id}/steer", response_model=None)
async def steer(
    run_id: str, body: Instruction, service: Runtime = Depends(runtime)
) -> StatusResult:
    return await service.steer(
        run_id,
        body.text,
        body.spec_update.model_dump(exclude_none=True) if body.spec_update else None,
    )


@router.post("/runs/{run_id}/approve-tool", status_code=202, response_model=None)
async def approve_tool(
    run_id: str, body: ToolApproval, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    row = await service.get_run(run_id)
    if row.status != "awaiting_tool_approval":
        raise Conflict("当前没有等待审批的操作。")
    return await service.resume(run_id, body.model_dump())


@router.post("/runs/{run_id}/replace", status_code=202, response_model=None)
async def replace(
    run_id: str, body: Replace, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    return await Revisions(service).replace(run_id, **body.model_dump(exclude_unset=True))


@router.post("/runs/{run_id}/rollback/preview", response_model=None)
async def rollback_preview(
    run_id: str, body: PreviewInput, service: Runtime = Depends(runtime)
) -> RevisionPreview:
    return await Revisions(service).rollback_preview(run_id, body.resolutions)


@router.post("/runs/{run_id}/rollback", response_model=None)
async def rollback(
    run_id: str, body: ApplyPreview, service: Runtime = Depends(runtime)
) -> StatusResult:
    return await Revisions(service).rollback(run_id, **body.model_dump())


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    after: int = 0,
    live: bool = False,
    last_event_id: str | None = Header(default=None),
    service: Runtime = Depends(runtime),
) -> StreamingResponse:
    await service.get_run(run_id)
    cursor = max(after, int(last_event_id or 0))
    if live and cursor == 0:
        async with service.database.session() as db:
            # 历史走分页接口。仅推送最后一条及之后的事件，避免重连重放整段历史。
            cursor = max(
                0,
                (await db.scalar(select(func.max(Event.id)).where(Event.run_id == run_id)) or 0)
                - 1,
            )

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        while not await request.is_disconnected():
            async with service.database.session() as db:
                status = await db.scalar(select(Run.status).where(Run.id == run_id))
                if status is None:
                    return  # 订阅建立后，项目或会话可能已被删除。
                batch = list(
                    await db.scalars(
                        select(Event)
                        .where(Event.run_id == run_id, Event.id > cursor)
                        .order_by(Event.id)
                        .limit(100)
                    )
                )
                reports = await verification_payloads(db, run_id, batch)
            for event in batch:
                cursor = event.id
                payload = reports.get(event.id, event.payload)
                yield f"id: {cursor}\nevent: {event.kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if not batch and status in TERMINAL | WAITING:
                return
            if not batch:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
