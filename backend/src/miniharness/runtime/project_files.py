"""用户项目目录与内部版本库之间的导入和带恢复记录的发布。"""

import asyncio
import json
import logging
import os
import secrets
import string
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from miniharness.runtime.errors import Conflict, NotFound
from miniharness.storage.models import Project, Session, new_id
from miniharness.workspace.cleanup import owned_path, remove_owned
from miniharness.workspace.paths import safe_path

if TYPE_CHECKING:
    from .manager import Runtime

logger = logging.getLogger(__name__)


class ProjectFiles:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    async def validate(self, db, value: str, project_id: str) -> Path:
        # 路径登记串行化，避免两个项目同时绑定同一目录或嵌套目录。
        await db.execute(text("SELECT pg_advisory_xact_lock(742016)"))
        path = Path(value.strip()).expanduser()
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError("项目路径必须是完整的目录路径，不能使用磁盘根目录。")
        absolute = Path(os.path.abspath(path))
        if path.resolve() != absolute:
            raise ValueError("项目路径不能经过符号链接或目录连接点。")
        root = self.runtime.settings.data_dir
        if absolute.is_relative_to(root) or root.is_relative_to(absolute):
            raise ValueError("项目目录不能与工作台内部数据目录重叠。")
        if absolute.exists() and not absolute.is_dir():
            raise ValueError("项目路径必须指向目录。")
        for other in await db.scalars(
            select(Project).where(Project.id != project_id, Project.workspace_path.is_not(None))
        ):
            other_path = Path(other.workspace_path).resolve()
            if absolute.is_relative_to(other_path) or other_path.is_relative_to(absolute):
                raise Conflict("该路径与另一个项目目录重叠，请选择独立目录。")
        return absolute

    async def new_directory(self, db, project_id: str) -> Path:
        root = self.runtime.settings.workspace_dir.absolute()
        while True:
            name = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
            if (root / name).exists():
                continue
            path = await self.validate(db, str(root / name), project_id)
            try:
                path.mkdir(parents=True, exist_ok=False)
                return path
            except FileExistsError:
                continue

    @asynccontextmanager
    async def publications(self):
        journals: list[str] = []
        try:
            yield journals
        except BaseException:
            for identifier in reversed(journals):
                await asyncio.to_thread(self.rollback, identifier)
            raise
        for identifier in journals:
            try:
                await asyncio.to_thread(self.complete, identifier)
            except OSError:
                logger.exception("项目写回已提交，发布日志清理将在恢复时重试：%s", identifier)

    async def configure(self, project_id: str, value: str | None) -> dict:
        async with self.runtime.database.session.begin() as db:
            project = await db.get(Project, project_id, with_for_update=True)
            if not project or project.deleted_at:
                raise NotFound("项目不存在。")
            if await db.scalar(
                select(Session.id)
                .where(Session.project_id == project_id, Session.active_run_id.is_not(None))
                .limit(1)
            ):
                raise Conflict("请先结束项目中的活动任务，再设置项目目录。")
            path = (
                await self.validate(db, value, project_id)
                if value and value.strip()
                else await self.new_directory(db, project_id)
            )
            before = project.public_snapshot
            path.mkdir(parents=True, exist_ok=True)
            incoming = await asyncio.to_thread(self.runtime.workspace.snapshot, project_id, path)
            existing = await asyncio.to_thread(self.runtime.workspace.files, project_id, before)
            incoming_files = await asyncio.to_thread(
                self.runtime.workspace.files, project_id, incoming
            )
            if existing and incoming_files and before != incoming:
                raise Conflict(
                    "所选目录与项目文件不同。请选择空目录以导出项目，或为已有代码新建项目。"
                )
            if existing and not incoming_files:
                # .git、依赖等非纳管内容可能存在；只发布纳管文件。
                await asyncio.to_thread(self._export_empty, project_id, before, path)
            else:
                project.public_snapshot = incoming
            project.workspace_path = str(path)
            project.revision += 1
            # 尚无任务的空会话随首次导入同步；已有会话继续保留自己的版本。
            for session in await db.scalars(
                select(Session).where(Session.project_id == project_id)
            ):
                if session.revision == 0 and session.head_snapshot == before:
                    session.head_snapshot = session.base_snapshot = project.public_snapshot
            return {"workspace_path": str(path), "public_snapshot": project.public_snapshot}

    def _export_empty(self, project_id: str, snapshot: str, path: Path) -> None:
        for name in self.runtime.workspace.files(project_id, snapshot):
            target = safe_path(path, name)
            if target != (path / name).absolute():
                raise Conflict("目标路径包含目录链接，未导出文件。")
            if target.exists():
                raise Conflict(f"目标目录已有同名文件：{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.runtime.workspace.read(project_id, snapshot, name))

    async def refresh(self, project: Project) -> None:
        if not project.workspace_path:
            return
        path = Path(project.workspace_path)
        if path.resolve() != path.absolute() or not path.is_dir():
            raise Conflict("项目目录已移动或变为链接，请检查项目路径。")
        snapshot = await asyncio.to_thread(self.runtime.workspace.snapshot, project.id, path)
        if snapshot != project.public_snapshot:
            project.public_snapshot = snapshot
            project.revision += 1

    def publish(
        self, project: Project, snapshot: str, *, initial_directory: bool = False
    ) -> str | None:
        if not project.workspace_path:
            return None
        path = Path(project.workspace_path)
        if path.resolve() != path.absolute() or not path.is_dir():
            raise Conflict("项目目录已移动或变为链接。")
        current = self.runtime.workspace.snapshot(project.id, path)
        expected = (
            self.runtime.workspace.ensure_project(project.id)
            if initial_directory
            else project.public_snapshot
        )
        if current != expected:
            raise Conflict("项目目录在预览后被修改，请重新预览合并。")
        if snapshot == current and not initial_directory:
            return None
        identifier = new_id()
        journal = owned_path(self.runtime.settings.data_dir, "publications", identifier)
        journal.mkdir(parents=True)
        before = set(self.runtime.workspace.files(project.id, current))
        after = set(self.runtime.workspace.files(project.id, snapshot))
        records = []
        for name in sorted(before | after):
            old = self.runtime.workspace.read(project.id, current, name) if name in before else None
            new = self.runtime.workspace.read(project.id, snapshot, name) if name in after else None
            if old == new:
                continue
            index = str(len(records))
            if old is not None:
                (journal / (index + ".before")).write_bytes(old)
            if new is not None:
                (journal / (index + ".after")).write_bytes(new)
            records.append({"path": name, "before": old is not None, "after": new is not None})
        manifest = {
            "project_id": project.id,
            "path": str(path),
            "snapshot": snapshot,
            "initial_directory": initial_directory,
            "records": records,
        }
        (journal / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        try:
            self._apply(journal, manifest, restoring=False)
        except BaseException:
            self.rollback(identifier)
            raise
        return identifier

    @staticmethod
    def _apply(journal: Path, manifest: dict, *, restoring: bool) -> None:
        root = Path(manifest["path"])
        if root.resolve() != root.absolute():
            raise Conflict("项目目录已变为链接，停止写入。")
        sequence = list(enumerate(manifest["records"]))
        for index, record in reversed(sequence) if restoring else sequence:
            try:
                target = safe_path(root, record["path"])
            except ValueError:
                if restoring:
                    continue
                raise
            if target.is_symlink() or (root / record["path"]).absolute() != target:
                if restoring:
                    continue
                raise Conflict("目标文件变为链接，停止写入。")
            old = (journal / f"{index}.before").read_bytes() if record["before"] else None
            new = (journal / f"{index}.after").read_bytes() if record["after"] else None
            wanted, expected = (old, new) if restoring else (new, old)
            observed = target.read_bytes() if target.is_file() else None
            if observed == wanted:
                continue
            if observed != expected or (target.exists() and not target.is_file()):
                if restoring:
                    continue  # 仅撤销本次已写入的字节，保留外部编辑并继续恢复其他文件。
                raise Conflict(f"文件被其他程序修改，未覆盖：{record['path']}")
            if wanted is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".miniharness-" + new_id())
                try:
                    temporary.write_bytes(wanted)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def remove_empty(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass  # 保留同时由其他程序创建的文件。

    def rollback(self, identifier: str) -> None:
        journal = owned_path(self.runtime.settings.data_dir, "publications", identifier)
        manifest = json.loads((journal / "manifest.json").read_text(encoding="utf-8"))
        self._apply(journal, manifest, restoring=True)
        if manifest.get("initial_directory"):
            root = Path(manifest["path"])
            parents = {root}
            for record in manifest["records"]:
                parent = (root / record["path"]).parent
                while parent.is_relative_to(root) and parent not in parents:
                    parents.add(parent)
                    parent = parent.parent
            for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
                if parent.absolute() == parent.resolve():
                    self.remove_empty(parent)
        self.complete(identifier)

    def complete(self, identifier: str) -> None:
        remove_owned(self.runtime.settings.data_dir, "publications", identifier)

    async def recover(self) -> None:
        root = owned_path(self.runtime.settings.data_dir, "publications")
        if not root.exists():
            return
        for entry in root.iterdir():
            try:
                journal = owned_path(root, entry.name)
                manifest_file = journal / "manifest.json"
                if not manifest_file.exists():
                    self.complete(entry.name)  # 尚未开始写入目录。
                    continue
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                async with self.runtime.database.session() as db:
                    project = await db.get(Project, manifest["project_id"])
                if project and not project.workspace_path and manifest.get("initial_directory"):
                    self.rollback(entry.name)
                    continue
                if not project or project.workspace_path != manifest["path"]:
                    raise Conflict("发布记录与项目目录不一致，保留记录供检查。")
                if project.public_snapshot == manifest["snapshot"]:
                    self.complete(entry.name)
                else:
                    self.rollback(entry.name)
            except (OSError, ValueError, Conflict):
                logger.exception("项目目录发布恢复失败：%s", entry.name)
