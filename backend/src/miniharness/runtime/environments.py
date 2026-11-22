"""项目环境配置、默认设置、虚拟环境创建与依赖安装。"""

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from miniharness.environments import (
    PythonEnvironment,
    backend_environment,
    inspect_python,
    python_path,
)
from miniharness.storage.models import ApplicationSetting, Project, Session, new_id
from miniharness.tools.python import run_python_command

from .errors import Conflict, NotFound

if TYPE_CHECKING:
    from .manager import Runtime


class EnvironmentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["default", "existing", "managed"] = "default"
    python: str = ""
    name: str = Field(default="", max_length=100)


class DefaultEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    python: str = Field(min_length=1)
    name: str = Field(default="", max_length=100)


class DependencyInstall(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    packages: list[str] = Field(default_factory=list, max_length=100)
    requirements: str = ""
    upgrade: bool = False


class EnvironmentService:
    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    async def defaults(self) -> dict:
        async with self.runtime.database.session() as db:
            row = await db.get(ApplicationSetting, "python_environment")
            if row:
                return dict(row.value)
        reference = self.runtime.settings.default_python_environment
        if reference:
            return await self.locate(reference)
        return asdict(backend_environment())

    async def choices(self) -> list[dict]:
        choices = [asdict(backend_environment())]
        async with self.runtime.database.session() as db:
            row = await db.get(ApplicationSetting, "python_environment")
            if row:
                choices.append(row.value)
            choices.extend(
                value for value in await db.scalars(select(Project.python_environment)) if value
            )
        candidates: list[Path] = []
        registry = Path.home() / ".conda/environments.txt"
        if registry.is_file():
            candidates.extend(
                Path(line.strip())
                for line in registry.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        backend = backend_environment()
        for root in {Path(backend.prefix), Path(sys.base_prefix)}:
            for folder in (
                root / "envs",
                root.parent if root.parent.name == "envs" else root / "envs",
            ):
                if folder.is_dir():
                    candidates.extend(path for path in folder.iterdir() if path.is_dir())
        for candidate in candidates:
            try:
                choices.append(
                    {
                        "name": candidate.name,
                        "python": str(python_path(str(candidate))),
                        "prefix": str(candidate),
                        "managed": False,
                    }
                )
            except ValueError:
                continue
        # 已切换走的项目独立环境仍可重新选择；不依赖当前项目配置保留其名称。
        managed_root = self.runtime.settings.data_dir / "environments"
        if managed_root.is_dir():
            for project_root in managed_root.iterdir():
                if not project_root.is_dir() or project_root.is_symlink():
                    continue
                for candidate in project_root.iterdir():
                    if candidate.is_symlink() or not (candidate / "pyvenv.cfg").is_file():
                        continue
                    try:
                        choices.append(
                            {
                                "name": candidate.name,
                                "python": str(python_path(str(candidate))),
                                "prefix": str(candidate),
                                "managed": True,
                            }
                        )
                    except ValueError:
                        continue
        unique = {}
        for choice in choices:
            unique.setdefault((os.path.normcase(choice["python"]), choice["name"]), choice)
        return list(unique.values())

    async def locate(
        self, reference: str, name: str = "", *, project_id: str | None = None
    ) -> dict:
        """仅定位文件；审批前不启动模型选择的解释器。"""
        if not reference:
            raise ValueError("请选择已有环境或填写 Python 的绝对路径。")
        if not Path(reference).expanduser().is_absolute():
            matches = [item for item in await self.choices() if item["name"] == reference]
            if project_id:
                project_root = self.runtime.settings.data_dir / "environments" / project_id
                own = [
                    item for item in matches if Path(item["prefix"]).is_relative_to(project_root)
                ]
                if own:
                    matches = own
            if len(matches) != 1:
                raise ValueError(f"环境名称 {reference!r} 不存在或有重名，请使用解释器绝对路径。")
            name = name or matches[0]["name"]
            reference = matches[0]["python"]
        executable = python_path(reference)
        prefix = (
            executable.parent.parent
            if executable.parent.name.lower() in {"scripts", "bin"}
            else executable.parent
        )
        return asdict(PythonEnvironment(str(executable), str(prefix), name or prefix.name))

    async def resolve(
        self, reference: str, name: str = "", *, project_id: str | None = None
    ) -> dict:
        located = await self.locate(reference, name, project_id=project_id)
        reference, name = located["python"], located["name"]
        info = await asyncio.to_thread(inspect_python, reference)
        return asdict(
            PythonEnvironment(
                info["python"],
                info["prefix"],
                name or Path(info["prefix"]).name,
                version=info["version"],
            )
        )

    async def save_default(self, selection: DefaultEnvironment) -> dict:
        value = await self.resolve(selection.python, selection.name)
        async with self.runtime.database.session.begin() as db:
            await db.execute(
                insert(ApplicationSetting)
                .values(key="python_environment", value=value)
                .on_conflict_do_update(
                    index_elements=[ApplicationSetting.key], set_={"value": value}
                )
            )
        return value

    async def get(self, project_id: str) -> PythonEnvironment:
        async with self.runtime.database.session() as db:
            project = await db.get(Project, project_id)
            if project is None:
                raise NotFound("项目不存在。")
            # 旧项目保持升级前的后端环境；新建项目会固定保存当时的默认设置。
            return self.from_project(project)

    def from_project(self, project: Project) -> PythonEnvironment:
        return (
            PythonEnvironment.from_dict(project.python_environment)
            if project.python_environment
            else backend_environment()
        )

    @asynccontextmanager
    async def use(self, project_id: str):
        async with self.lock("project:" + project_id):
            environment = await self.get(project_id)
            async with self.lock("python:" + os.path.normcase(environment.python)):
                yield environment

    async def assert_idle(self, db, project_id: str, run_id: str | None) -> None:
        query = select(Session.id).where(
            Session.project_id == project_id, Session.active_run_id.is_not(None)
        )
        if run_id:
            query = query.where(Session.active_run_id != run_id)
        if await db.scalar(query.limit(1)):
            raise Conflict(
                "项目还有其他活动调用，请结束或撤回后再修改环境或安装依赖。当前调用可通过环境工具自行配置。"
            )

    async def prepare(self, project_id: str, selection: EnvironmentSelection, logdir: Path) -> dict:
        if selection.mode == "default":
            configured = await self.defaults()
            return await self.resolve(configured["python"], configured["name"])
        if selection.mode == "existing":
            value = await self.resolve(selection.python, selection.name, project_id=project_id)
            root = self.runtime.settings.data_dir / "environments" / project_id
            value["managed"] = Path(value["prefix"]).is_relative_to(root)
            return value
        name = selection.name or "venv"
        if not re.fullmatch(r"[\w-][\w.-]{0,79}", name):
            raise ValueError("独立环境名称只能包含字母、数字、下划线、连字符和点，且不能以点开头。")
        root = (self.runtime.settings.data_dir / "environments" / project_id).resolve()
        destination = root / name
        if not destination.resolve().is_relative_to(root):
            raise ValueError("环境目录超出项目环境存储范围。")
        if not destination.exists():
            base = (
                await self.resolve(selection.python, project_id=project_id)
                if selection.python
                else await self.defaults()
            )
            base_environment = PythonEnvironment.from_dict(base)
            root.mkdir(parents=True, exist_ok=True)
            result = await run_python_command(
                root,
                ["-m", "venv", str(destination)],
                logdir,
                self.runtime.settings.environment_timeout,
                python_environment=base_environment,
            )
            if result["exit_code"] != 0 or result["timed_out"]:
                raise ValueError(
                    f"创建虚拟环境失败：{result['stderr']}；日志：{result['stderr_log']}"
                )
        if not (destination / "pyvenv.cfg").is_file():
            raise ValueError("环境目录已存在但不是完整虚拟环境，请检查创建日志或使用另一个名称。")
        value = await self.resolve(str(destination), name)
        return {**value, "managed": True}

    async def configure(
        self,
        project_id: str,
        selection: EnvironmentSelection,
        *,
        run_id: str | None = None,
        expected: dict | None = None,
    ) -> dict:
        async with self.lock("project:" + project_id):
            async with self.runtime.database.session.begin() as db:
                project = await db.get(Project, project_id, with_for_update=True)
                if project is None:
                    raise NotFound("项目不存在。")
                await self.assert_idle(db, project_id, run_id)
                if expected is not None and asdict(self.from_project(project)) != expected:
                    raise Conflict("项目环境已变化，请重新读取环境并提交操作。")
                logdir = self.runtime.settings.data_dir / "environment-logs" / project_id / new_id()
                value = await self.prepare(project_id, selection, logdir)
                project.python_environment = value
                project.revision += 1
                return value

    async def install(
        self,
        project_id: str,
        selection: DependencyInstall,
        workdir: Path,
        logdir: Path,
        *,
        run_id: str,
        expected: dict | None = None,
    ) -> dict:
        from miniharness.workspace.paths import resolve_path

        if not selection.packages and not selection.requirements:
            raise ValueError("请提供 packages 或 requirements。")
        if any(
            not item.strip() or item.startswith("-") or "\0" in item for item in selection.packages
        ):
            raise ValueError("packages 只能包含依赖说明，不能传入 pip 选项；升级请使用 upgrade。")
        async with self.use(project_id) as environment:
            if expected is not None and asdict(environment) != expected:
                raise Conflict("项目环境已变化，请重新提交依赖安装操作。")
            async with self.runtime.database.session.begin() as db:
                project = await db.get(Project, project_id, with_for_update=True)
                await self.assert_idle(db, project_id, run_id)
                project.revision += 1  # 安装失败也可能已经改变部分包。
                arguments = [
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                ]
                if selection.upgrade:
                    arguments.append("--upgrade")
                if selection.requirements:
                    requirements = resolve_path(workdir, selection.requirements)
                    if not requirements.is_file():
                        raise ValueError(f"依赖清单不存在：{requirements}")
                    arguments.extend(["-r", str(requirements)])
                arguments.extend(["--", *selection.packages])
                result = await run_python_command(
                    workdir,
                    arguments,
                    logdir,
                    self.runtime.settings.environment_timeout,
                    python_environment=environment,
                )
            return {**result, "environment": asdict(environment)}
