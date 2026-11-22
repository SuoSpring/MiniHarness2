import asyncio
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response

from miniharness.runtime.errors import NotFound
from miniharness.runtime.manager import LogEntry, LogPage, LogSlice, Runtime
from miniharness.runtime.revisions import MergeCommitResult, RevisionPreview, Revisions
from miniharness.storage.models import Project, Run, Session
from miniharness.tools.files import DEFAULT_DIRECTORY_LIMIT, MAX_DIRECTORY_LIMIT
from miniharness.workspace.paths import project_files, safe_path

from .dependencies import runtime
from .schemas import ApplyPreview, PreviewInput

router = APIRouter(tags=["文件与交付"])


class FileEntry(TypedDict):
    path: str


class FilePreview(FileEntry):
    content: str
    binary: bool
    truncated: NotRequired[bool]


async def source_for(
    service: Runtime, project_id: str, session_id: str | None
) -> tuple[str, Path | None]:
    async with service.database.session() as db:
        project = await db.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在。")
        if session_id:
            session = await db.get(Session, session_id)
            if session is None or session.project_id != project_id:
                raise NotFound("会话不属于该项目。")
            if session.active_run_id:
                run = await db.get(Run, session.active_run_id)
                return "", Path(run.working_dir)
            return session.head_snapshot, None
        return project.public_snapshot, None


def read_working_file(directory: Path, path: str) -> bytes:
    target = safe_path(directory, path)
    if target not in project_files(directory):
        raise FileNotFoundError(path)
    return target.read_bytes()


@router.get("/projects/{project_id}/files", response_model=None)
async def files(
    project_id: str, session_id: str | None = None, service: Runtime = Depends(runtime)
) -> list[FileEntry]:
    snapshot, directory = await source_for(service, project_id, session_id)
    if directory is not None:
        return [
            {"path": path.relative_to(directory).as_posix()}
            for path in await asyncio.to_thread(project_files, directory)
        ]
    return [
        {"path": path}
        for path in await asyncio.to_thread(service.workspace.files, project_id, snapshot)
    ]


@router.get("/projects/{project_id}/file", response_model=None)
async def file(
    project_id: str, path: str, session_id: str | None = None, service: Runtime = Depends(runtime)
) -> FilePreview:
    snapshot, directory = await source_for(service, project_id, session_id)
    content = (
        await asyncio.to_thread(read_working_file, directory, path)
        if directory is not None
        else await asyncio.to_thread(service.workspace.read, project_id, snapshot, path)
    )
    try:
        text = content.decode("utf-8")
        return {
            "path": path,
            "content": text[:100000],
            "binary": False,
            "truncated": len(text) > 100000,
        }
    except UnicodeDecodeError:
        return {"path": path, "content": "二进制文件，请下载查看。", "binary": True}


@router.get("/projects/{project_id}/download")
async def download(
    project_id: str, path: str, session_id: str | None = None, service: Runtime = Depends(runtime)
) -> Response:
    snapshot, directory = await source_for(service, project_id, session_id)
    return Response(
        await asyncio.to_thread(read_working_file, directory, path)
        if directory is not None
        else await asyncio.to_thread(service.workspace.read, project_id, snapshot, path),
        media_type="application/octet-stream",
    )


@router.get("/runs/{run_id}/patch")
async def patch(run_id: str, service: Runtime = Depends(runtime), inline: bool = False) -> Response:
    run = await service.get_run(run_id)
    target = service.run_dir(run_id) / "changes.patch"
    if target.is_file():
        return FileResponse(
            target,
            media_type="text/x-diff",
            filename=f"{run_id}.patch",
            content_disposition_type="inline" if inline else "attachment",
        )
    if inline and run.start_snapshot and run.end_snapshot:
        try:
            diff = await asyncio.to_thread(
                service.workspace.diff, run.project_id, run.start_snapshot, run.end_snapshot
            )
        except (ValueError, RuntimeError) as exc:
            raise NotFound("这次调用的文件版本已不可用。") from exc
        return Response(diff.encode("utf-8", "surrogateescape"), media_type="text/x-diff")
    raise NotFound("本次调用的完整补丁尚未生成，或已按旧调用保留策略清理。")


@router.get("/runs/{run_id}/logs", response_model=None)
async def logs(
    run_id: str, path: str, offset: int = Query(0, ge=0), service: Runtime = Depends(runtime)
) -> LogSlice:
    await service.get_run(run_id)
    return service.read_log(run_id, path, offset)


@router.get("/runs/{run_id}/logs/index", response_model=None)
async def log_index(
    run_id: str,
    service: Runtime = Depends(runtime),
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_DIRECTORY_LIMIT)] = None,
) -> list[LogEntry] | LogPage:
    await service.get_run(run_id)
    if cursor is not None or limit is not None:
        return await asyncio.to_thread(
            service.list_log_page,
            run_id,
            cursor=cursor or "",
            limit=DEFAULT_DIRECTORY_LIMIT if limit is None else limit,
        )
    return await asyncio.to_thread(service.list_logs, run_id)


@router.get("/runs/{run_id}/logs/download")
async def download_log(run_id: str, path: str, service: Runtime = Depends(runtime)) -> FileResponse:
    from miniharness.tools import safe_path

    await service.get_run(run_id)
    service.read_log(run_id, path, limit=0)
    return FileResponse(
        safe_path(service.run_dir(run_id), path), media_type="application/octet-stream"
    )


@router.post("/sessions/{session_id}/merge/preview", response_model=None)
async def merge_preview(
    session_id: str, body: PreviewInput, service: Runtime = Depends(runtime)
) -> RevisionPreview:
    return await Revisions(service).merge_preview(session_id, body.resolutions)


@router.post("/sessions/{session_id}/merge", response_model=None)
async def merge(
    session_id: str, body: ApplyPreview, service: Runtime = Depends(runtime)
) -> MergeCommitResult:
    return await Revisions(service).merge(session_id, body.token, body.resolutions)


@router.post("/sessions/{session_id}/archive/preview", response_model=None)
async def archive_preview(
    session_id: str, body: PreviewInput, service: Runtime = Depends(runtime)
) -> RevisionPreview:
    return await Revisions(service).merge_preview(session_id, body.resolutions, archive=True)


@router.post("/sessions/{session_id}/archive", response_model=None)
async def archive(
    session_id: str, body: ApplyPreview, service: Runtime = Depends(runtime)
) -> MergeCommitResult:
    return await Revisions(service).merge(session_id, body.token, body.resolutions, archive=True)
