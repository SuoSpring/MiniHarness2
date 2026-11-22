from fastapi import APIRouter, Depends, Response

from miniharness.memory import MemoryService
from miniharness.memory.service import MemoryRecord, StagedMemory
from miniharness.runtime.errors import NotFound
from miniharness.runtime.manager import Runtime
from miniharness.storage.models import Project, Session

from .dependencies import runtime
from .schemas import MemoryWrite

router = APIRouter(tags=["向量记忆"])


async def validate_scope(service: Runtime, project_id: str, session_id: str | None) -> None:
    async with service.database.session() as db:
        if await db.get(Project, project_id) is None:
            raise NotFound("项目不存在。")
        if session_id:
            session = await db.get(Session, session_id)
            if session is None or session.deleted_at or session.project_id != project_id:
                raise NotFound("会话不属于该项目。")


@router.get("/projects/{project_id}/memories", response_model=None)
async def memories(
    project_id: str, session_id: str | None = None, service: Runtime = Depends(runtime)
) -> list[MemoryRecord]:
    await validate_scope(service, project_id, session_id)
    return await MemoryService(service.database, service.gateway_factory(None)).list(
        project_id, session_id
    )


@router.put("/projects/{project_id}/memories", response_model=None)
async def write(
    project_id: str, body: MemoryWrite, service: Runtime = Depends(runtime)
) -> MemoryRecord | StagedMemory:
    await validate_scope(service, project_id, body.session_id)
    return await MemoryService(service.database, service.gateway_factory(None)).write(
        project_id, **body.model_dump()
    )


@router.delete("/memories/{memory_id}", status_code=204)
async def delete(
    memory_id: str, expected_revision: int, service: Runtime = Depends(runtime)
) -> Response:
    await MemoryService(service.database, service.gateway_factory(None)).delete(
        memory_id, expected_revision
    )
    return Response(status_code=204)
