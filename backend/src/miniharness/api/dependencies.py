from typing import cast

from fastapi import Request

from miniharness.runtime.errors import NotFound
from miniharness.runtime.manager import Runtime
from miniharness.storage.models import MCPServer, Memory, Project, Run, Session


def runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


async def visible_resource(request: Request) -> None:
    """删除标记对所有入口生效，包括历史链接、文件与工具配置。"""
    service = runtime(request)
    params = request.path_params
    project_id = params.get("project_id")
    session_id = params.get("session_id") or request.query_params.get("session_id")
    deleting = (
        request.method == "DELETE" and ("session_id" in params or "project_id" in params)
    ) or request.url.path.endswith("/deletion-preview")
    async with service.database.session() as db:
        for key, model in (("run_id", Run), ("server_id", MCPServer), ("memory_id", Memory)):
            if key in params:
                row = await db.get(model, params[key])
                if row is None:
                    raise NotFound("记录不存在或已删除。")
                project_id = row.project_id
                session_id = getattr(row, "session_id", None)
        if session_id:
            session = await db.get(Session, session_id)
            if (
                session is None
                or (session.deleted_at and not deleting)
                or (project_id and session.project_id != project_id)
            ):
                raise NotFound("会话不存在或已删除。")
            project_id = session.project_id
        if project_id:
            project = await db.get(Project, project_id)
            if project is None or (project.deleted_at and not deleting):
                raise NotFound("项目不存在或已删除。")
