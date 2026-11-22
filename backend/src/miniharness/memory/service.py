from __future__ import annotations

from typing import Literal, Protocol, TypedDict, cast

from sqlalchemy import delete, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from miniharness.runtime.errors import Conflict, NotFound
from miniharness.storage import Database
from miniharness.storage.models import (
    Memory,
    MemoryCandidate,
    MemoryDraft,
    Project,
    Run,
    new_id,
    utcnow,
)

from .keywords import bm25_rank, normalize_keywords, search_terms


class EmbeddingGateway(Protocol):
    async def embed(self, text: str, /) -> list[float]: ...


class MemoryRecord(TypedDict):
    id: str
    key: str
    content: str
    keywords: list[str]
    session_id: str | None
    revision: int
    source_run_id: str | None
    updated_at: str | None


class RecalledMemory(MemoryRecord, total=False):
    staged: bool


class StagedMemory(TypedDict):
    id: str
    key: str
    revision: int
    staged: Literal[True]


class MemoryConflict(TypedDict):
    id: str
    key: str
    expected_revision: int
    current_revision: int


MemoryResolutions = dict[str, Literal["keep", "restore"]]


def memory_dict(row: Memory) -> MemoryRecord:
    return {
        "id": row.id,
        "key": row.key,
        "content": row.content,
        "keywords": row.keywords,
        "session_id": row.session_id,
        "revision": row.revision,
        "source_run_id": row.source_run_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class MemoryService:
    def __init__(self, database: Database, gateway: EmbeddingGateway) -> None:
        self.database, self.gateway = database, gateway

    async def embed(self, text: str) -> list[float] | None:
        # 缺少配置时明确使用关键词模式；已配置服务的网络/鉴权错误仍正常上报。
        if not getattr(self.gateway, "embedding_enabled", True):
            return None
        return await self.gateway.embed(text)

    async def list(self, project_id: str, session_id: str | None = None) -> list[MemoryRecord]:
        async with self.database.session() as db:
            query = select(Memory).where(
                Memory.project_id == project_id,
                or_(Memory.session_id.is_(None), Memory.session_id == session_id),
            )
            return [memory_dict(row) for row in await db.scalars(query.order_by(Memory.key))]

    async def recall(
        self,
        project_id: str,
        session_id: str | None,
        query: str,
        run_id: str | None = None,
        limit: int = 5,
        *,
        keywords: list[str] | None = None,
        exact_scope: bool = False,
    ) -> list[RecalledMemory]:
        limit = min(max(limit, 0), 20)
        if not limit:
            return []
        async with self.database.session() as db:
            published = select(
                Memory.id,
                Memory.key,
                Memory.content,
                Memory.keywords,
                Memory.session_id,
                Memory.revision,
                Memory.source_run_id,
                Memory.updated_at,
                Memory.embedding,
                literal(False).label("staged"),
            ).where(
                Memory.project_id == project_id,
                Memory.session_id == session_id
                if exact_scope
                else or_(Memory.session_id.is_(None), Memory.session_id == session_id),
            )
            if run_id:
                # 先取每条记忆的最新草稿，再覆盖旧向量；最后统一排名，不能先截取已发布 Top-K。
                draft_versions = (
                    select(
                        MemoryDraft,
                        func.row_number()
                        .over(
                            partition_by=MemoryDraft.memory_id,
                            order_by=MemoryDraft.id.desc(),
                        )
                        .label("version_rank"),
                    )
                    .join(Run, Run.id == MemoryDraft.run_id)
                    .where(
                        Run.id == run_id,
                        Run.project_id == project_id,
                        Run.session_id == session_id,
                        Run.finished_at.is_(None),
                        or_(MemoryDraft.session_id.is_(None), MemoryDraft.session_id == session_id),
                    )
                    .cte("scoped_memory_drafts")
                )
                latest = (
                    select(draft_versions)
                    .where(
                        draft_versions.c.version_rank == 1,
                    )
                    .cte("latest_memory_drafts")
                )
                published = published.where(~Memory.id.in_(select(latest.c.memory_id)))
                staged = select(
                    latest.c.memory_id.label("id"),
                    latest.c.key,
                    latest.c.content,
                    latest.c.keywords,
                    latest.c.session_id,
                    latest.c.revision,
                    latest.c.run_id.label("source_run_id"),
                    literal(None).label("updated_at"),
                    latest.c.embedding,
                    literal(True).label("staged"),
                )
                candidates = union_all(published, staged).subquery()
            else:
                candidates = published.subquery()
            columns = [column for column in candidates.c if column.key != "embedding"]
            if exact_scope and not await db.scalar(
                select(literal(True)).select_from(candidates).limit(1)
            ):
                return []
        # 嵌入服务还会独立记账。等待它时先释放检索连接。
        vector = await self.embed(query)
        async with self.database.session() as db:
            corpus = [dict(row) for row in (await db.execute(select(*columns))).mappings()]
            # 两路各自取候选，再用倒数排名融合，避免不同分数尺度相互压倒。
            pool_size = max(20, limit * 4)
            semantic = []
            if vector is not None:
                ranked = (
                    select(*columns)
                    .where(func.vector_dims(candidates.c.embedding) == len(vector))
                    .order_by(candidates.c.embedding.cosine_distance(vector), candidates.c.id)
                    .limit(pool_size)
                )
                semantic = [dict(row) for row in (await db.execute(ranked)).mappings()]
            terms = search_terms(query, keywords)
            lexical = bm25_rank(corpus, terms, pool_size)
            records, scores, semantic_order = {}, {}, {}
            for ranking in (semantic, lexical):
                for rank, row in enumerate(ranking, 1):
                    identifier = row["id"]
                    records[identifier] = row
                    scores[identifier] = scores.get(identifier, 0) + 1 / (60 + rank)
            semantic_order = {row["id"]: rank for rank, row in enumerate(semantic)}
            identifiers = sorted(
                records,
                key=lambda identifier: (
                    -scores[identifier],
                    semantic_order.get(identifier, pool_size),
                    identifier,
                ),
            )[:limit]
            results = [cast(RecalledMemory, records[identifier]) for identifier in identifiers]
            for result in results:
                timestamp = records[result["id"]]["updated_at"]
                result["updated_at"] = timestamp.isoformat() if timestamp else None
                if not result["staged"]:
                    del result["staged"]
            return results

    async def stage(
        self,
        run_id: str,
        key: str,
        content: str,
        session_id: str | None = None,
        *,
        keywords: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        """只保存候选文本，不调用嵌入或判重模型，也不修改正式记忆。"""
        key, content = key.strip(), content.strip()
        if not key or len(key) > 200 or not content or len(content) > 12000:
            raise ValueError("记忆名称须为 1–200 字符，正文须为 1–12000 字符。")
        words = normalize_keywords(keywords) or normalize_keywords(search_terms(key))
        async with self.database.session.begin() as db:
            run = await db.get(Run, run_id, with_for_update=True)
            if run is None or run.finished_at or run.status in {"crashed", "rolled_back"}:
                raise Conflict("调用已结束，不能追加记忆候选。")
            if session_id not in (None, run.session_id):
                raise ValueError("只能准备项目共享记忆或当前会话记忆。")
            if run.memory_preparation:
                raise Conflict("记忆已进入结束整理，不能继续追加。")
            if expected_revision is not None:
                current = await db.scalar(
                    select(Memory).where(
                        Memory.project_id == run.project_id,
                        Memory.session_id == session_id,
                        Memory.key == key,
                    )
                )
                if (current.revision if current else 0) != expected_revision:
                    raise Conflict("记忆版本已改变，请重新召回。")
            row = MemoryCandidate(
                run_id=run_id, session_id=session_id, key=key, content=content, keywords=words
            )
            db.add(row)
            await db.flush()
            return {
                "candidate_id": row.id,
                "key": key,
                "staged": True,
                "notice": "已登记候选；调用结束后统一判断更新或新增，尚未发布。",
            }

    async def write(
        self,
        project_id: str,
        key: str,
        content: str,
        session_id: str | None = None,
        expected_revision: int | None = None,
        run_id: str | None = None,
        keywords: list[str] | None = None,
    ) -> MemoryRecord | StagedMemory:
        vector = await self.embed(content)
        async with self.database.session.begin() as db:
            if run_id is None:
                await db.get(Project, project_id, with_for_update=True)
            current = await db.scalar(
                select(Memory)
                .where(
                    Memory.project_id == project_id,
                    Memory.session_id == session_id,
                    Memory.key == key,
                )
                .with_for_update()
            )
            draft = None
            if run_id:
                draft = await db.scalar(
                    select(MemoryDraft)
                    .where(
                        MemoryDraft.run_id == run_id,
                        MemoryDraft.session_id == session_id,
                        MemoryDraft.key == key,
                    )
                    .order_by(MemoryDraft.id.desc())
                    .limit(1)
                )
            actual_revision = draft.revision if draft else current.revision if current else 0
            words = (
                normalize_keywords(keywords)
                if keywords is not None
                else (draft.keywords if draft else current.keywords if current else [])
            )
            if actual_revision and expected_revision != actual_revision:
                raise Conflict(f"记忆 {key} 版本已改变，请重新读取；当前版本 {actual_revision}。")
            if not actual_revision and expected_revision not in (None, 0):
                raise Conflict("该记忆已不存在，请重新读取。")
            if run_id:
                memory_id = draft.memory_id if draft else current.id if current else new_id()
                row = MemoryDraft(
                    run_id=run_id,
                    memory_id=memory_id,
                    session_id=session_id,
                    key=key,
                    content=content,
                    keywords=words,
                    embedding=vector,
                    expected_revision=draft.expected_revision if draft else actual_revision,
                    revision=actual_revision + 1,
                )
                db.add(row)
                return {"id": memory_id, "key": key, "revision": row.revision, "staged": True}
            if current is None:
                current = Memory(
                    id=new_id(),
                    project_id=project_id,
                    session_id=session_id,
                    key=key,
                    content=content,
                    keywords=words,
                    embedding=vector,
                    revision=1,
                    updated_at=utcnow(),
                )
                db.add(current)
            else:
                current.content, current.embedding = content, vector
                current.keywords = words
                current.revision += 1
                current.source_run_id = None
                current.updated_at = utcnow()
            return memory_dict(current)

    async def delete(self, memory_id: str, expected_revision: int) -> None:
        async with self.database.session.begin() as db:
            row = await db.get(Memory, memory_id, with_for_update=True)
            if row is None:
                raise NotFound("记忆不存在。")
            if row.revision != expected_revision:
                raise Conflict("记忆已被其他调用修改，请刷新。")
            await db.delete(row)

    @staticmethod
    async def publish(db: AsyncSession, run: Run) -> None:
        drafts = list(
            await db.scalars(
                select(MemoryDraft).where(MemoryDraft.run_id == run.id).order_by(MemoryDraft.id)
            )
        )
        for draft in {d.memory_id: d for d in drafts}.values():
            current = await db.scalar(
                select(Memory)
                .where(
                    Memory.project_id == run.project_id,
                    Memory.session_id == draft.session_id,
                    Memory.key == draft.key,
                )
                .with_for_update()
            )
            actual = current.revision if current else 0
            if actual != draft.expected_revision or (current and current.id != draft.memory_id):
                raise Conflict(
                    f"记忆 {draft.key} 发布冲突：预期版本 {draft.expected_revision}，实际 {actual}；条目也可能已被删除重建。"
                )
            if current:
                draft.before = {
                    **memory_dict(current),
                    "embedding": list(current.embedding) if current.embedding is not None else None,
                }
                current.content, current.embedding = draft.content, draft.embedding
                current.keywords = draft.keywords
                current.revision, current.source_run_id = draft.revision, run.id
                current.updated_at = utcnow()
            else:
                db.add(
                    Memory(
                        id=draft.memory_id,
                        project_id=run.project_id,
                        session_id=draft.session_id,
                        key=draft.key,
                        content=draft.content,
                        keywords=draft.keywords,
                        embedding=draft.embedding,
                        revision=draft.revision,
                        source_run_id=run.id,
                    )
                )
            draft.published = True

    @staticmethod
    async def rollback_conflicts(db: AsyncSession, run_id: str) -> list[MemoryConflict]:
        result: list[MemoryConflict] = []
        for draft in await db.scalars(
            select(MemoryDraft).where(MemoryDraft.run_id == run_id, MemoryDraft.published.is_(True))
        ):
            current = await db.get(Memory, draft.memory_id)
            if (
                current is None
                or current.revision != draft.revision
                or current.source_run_id != run_id
            ):
                result.append(
                    {
                        "id": draft.memory_id,
                        "key": draft.key,
                        "expected_revision": draft.revision,
                        "current_revision": current.revision if current else 0,
                    }
                )
        return result

    @staticmethod
    async def rollback(db: AsyncSession, run_id: str, resolutions: MemoryResolutions) -> None:
        for draft in await db.scalars(
            select(MemoryDraft).where(MemoryDraft.run_id == run_id, MemoryDraft.published.is_(True))
        ):
            current = await db.get(Memory, draft.memory_id, with_for_update=True)
            conflict = (
                current is None
                or current.revision != draft.revision
                or current.source_run_id != run_id
            )
            if conflict and draft.memory_id not in resolutions:
                raise Conflict(f"记忆 {draft.key} 有撤回冲突。")
            if conflict and resolutions[draft.memory_id] == "keep":
                continue
            if draft.before:
                previous = draft.before
                if current is None:
                    run = await db.get(Run, run_id)
                    current = Memory(id=draft.memory_id, project_id=run.project_id, revision=0)
                    db.add(current)
                current.key, current.session_id = previous["key"], previous["session_id"]
                current.content, current.embedding = previous["content"], previous["embedding"]
                current.keywords = previous.get("keywords", [])
                current.revision = max(current.revision, draft.revision) + 1
                current.source_run_id = previous["source_run_id"]
                current.updated_at = utcnow()
            elif current:
                await db.delete(current)
        await db.execute(
            delete(MemoryDraft).where(
                MemoryDraft.run_id == run_id, MemoryDraft.published.is_(False)
            )
        )
        await db.execute(delete(MemoryCandidate).where(MemoryCandidate.run_id == run_id))
