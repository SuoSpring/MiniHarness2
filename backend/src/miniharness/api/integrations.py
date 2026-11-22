import asyncio
from typing import Any, TypedDict, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from miniharness.integrations.mcp import MCPServerConfig, MCPServerDescription, describe_server
from miniharness.integrations.skills import SkillEntry, Skills
from miniharness.runtime.errors import Conflict, NotFound
from miniharness.runtime.manager import Runtime
from miniharness.storage.models import MCPServer, Project, new_id

from .dependencies import runtime
from .schemas import Enabled, MCPConfig

router = APIRouter(tags=["MCP 与 Skill"])


class SkillSetting(TypedDict):
    name: str
    enabled: bool


def server_dict(row: MCPServer) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "enabled": row.enabled, **row.config}


def validate_server(body: MCPConfig) -> None:
    if body.transport == "stdio" and not body.command:
        raise ValueError("stdio 连接需要 command。")
    if body.transport == "http" and (
        not body.url or not body.url.startswith(("http://", "https://"))
    ):
        raise ValueError("HTTP 连接需要有效 http(s) URL。")


@router.get("/projects/{project_id}/mcp", response_model=None)
async def servers(project_id: str, service: Runtime = Depends(runtime)) -> list[dict[str, Any]]:
    async with service.database.session() as db:
        return [
            server_dict(row)
            for row in await db.scalars(select(MCPServer).where(MCPServer.project_id == project_id))
        ]


@router.post("/projects/{project_id}/mcp", status_code=201, response_model=None)
async def create(
    project_id: str, body: MCPConfig, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    validate_server(body)
    async with service.database.session.begin() as db:
        if await db.get(Project, project_id, with_for_update=True) is None:
            raise NotFound("项目不存在。")
        if await db.scalar(
            select(MCPServer.id).where(
                MCPServer.project_id == project_id, MCPServer.name == body.name
            )
        ):
            raise Conflict("同一项目的 MCP 服务器名称必须唯一。")
        row = MCPServer(
            id=new_id(),
            project_id=project_id,
            name=body.name,
            enabled=body.enabled,
            config=body.model_dump(exclude={"name", "enabled"}),
        )
        db.add(row)
        return server_dict(row)


@router.put("/mcp/{server_id}", response_model=None)
async def update(
    server_id: str, body: MCPConfig | Enabled, service: Runtime = Depends(runtime)
) -> dict[str, Any]:
    async with service.database.session.begin() as db:
        row = await db.get(MCPServer, server_id)
        if row is None:
            raise NotFound("MCP 服务器不存在。")
        row.enabled = body.enabled
        if isinstance(body, MCPConfig):
            validate_server(body)
            await db.get(Project, row.project_id, with_for_update=True)
            if await db.scalar(
                select(MCPServer.id).where(
                    MCPServer.project_id == row.project_id,
                    MCPServer.name == body.name,
                    MCPServer.id != row.id,
                )
            ):
                raise Conflict("同一项目的 MCP 服务器名称必须唯一。")
            row.name, row.config = body.name, body.model_dump(exclude={"name", "enabled"})
        return server_dict(row)


@router.delete("/mcp/{server_id}", status_code=204)
async def delete(server_id: str, service: Runtime = Depends(runtime)) -> Response:
    async with service.database.session.begin() as db:
        row = await db.get(MCPServer, server_id)
        if row is None:
            raise NotFound("MCP 服务器不存在。")
        await db.delete(row)
    return Response(status_code=204)


@router.post("/mcp/{server_id}/test", response_model=None)
async def test(server_id: str, service: Runtime = Depends(runtime)) -> MCPServerDescription:
    async with service.database.session() as db:
        row = await db.get(MCPServer, server_id)
        if row is None:
            raise NotFound("MCP 服务器不存在。")
    try:
        async with asyncio.timeout(service.settings.command_timeout):
            return await describe_server(row.name, cast(MCPServerConfig, row.config))
    except TimeoutError as error:
        raise HTTPException(504, "MCP 连接测试超时。") from error
    except Exception as error:
        raise HTTPException(502, f"MCP 连接失败：{type(error).__name__}: {error}") from error


@router.get("/projects/{project_id}/skills", response_model=None)
async def skills(project_id: str, service: Runtime = Depends(runtime)) -> list[SkillEntry]:
    async with service.database.session() as db:
        project = await db.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在。")
    directories = [
        *service.settings.skill_dirs,
        service.settings.data_dir / "projects" / project_id / "skills",
    ]
    return Skills(
        directories, {key for key, enabled in project.skill_settings.items() if not enabled}
    ).catalog()


@router.put("/projects/{project_id}/skills/{name}", response_model=None)
async def skill_enabled(
    project_id: str, name: str, body: Enabled, service: Runtime = Depends(runtime)
) -> SkillSetting:
    async with service.database.session.begin() as db:
        project = await db.get(Project, project_id, with_for_update=True)
        if project is None:
            raise NotFound("项目不存在。")
        project.skill_settings = {**project.skill_settings, name: body.enabled}
    return {"name": name, "enabled": body.enabled}
