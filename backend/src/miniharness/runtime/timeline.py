"""面向用户的轨迹分页；原始事件仍保留供审计。"""

import json

from sqlalchemy import and_, case, exists, func, not_, or_, select
from sqlalchemy.orm import aliased

from miniharness.agent.evidence import EXECUTION_SOURCES
from miniharness.context.logs import execution_failed
from miniharness.storage.models import Event


async def execution_result_page(
    db, run_id: str, event_ids: list[int], revision: int, args: dict
) -> dict:
    """只读活动分支上执行节点取得的原始回执，按事件或正文分页。"""
    name = Event.payload["name"].astext
    where = (
        Event.run_id == run_id,
        Event.kind == "tool",
        Event.id.in_(event_ids),
        Event.payload["role"].astext == "executor",
        Event.payload["task_revision"].as_integer() == revision,
        Event.payload["denied"].as_boolean().is_not(True),
        or_(name.in_(EXECUTION_SOURCES), name.startswith("mcp_", autoescape=True)),
    )
    event_id, before, offset = args.get("event_id"), args.get("before"), args.get("offset", 0)
    if (
        any(type(i) is not int or i < 1 for i in (event_id, before) if i is not None)
        or type(offset) is not int
        or offset < 0
    ):
        raise ValueError("请使用目录中的正整数事件编号和非负字符偏移")
    if event_id is None:
        if offset:
            raise ValueError("读取正文需要 event_id")
        query = select(Event.id, name, Event.payload["arguments"]["path"].astext).where(*where)
        if before is not None:
            query = query.where(Event.id < before)
        rows = (await db.execute(query.order_by(Event.id.desc()).limit(21))).all()
        return {
            "run_id": run_id,
            "events": [
                {"event_id": i, "tool": tool, **({"path": path} if path else {})}
                for i, tool, path in rows[:20]
            ],
            "next_before": rows[19][0] if len(rows) > 20 else None,
        }
    payload = await db.scalar(select(Event.payload).where(*where, Event.id == event_id))
    if payload is None:
        raise ValueError("这次运行的有效执行记录中没有该事件")
    result = payload["result"]
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    end = min(len(text), offset + 4000)
    return {
        "run_id": run_id,
        "event_id": event_id,
        "tool": payload["name"],
        "file_revision": payload["file_revision"],
        "task_revision": payload["task_revision"],
        "source_ok": not execution_failed(result),
        "content": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
    }


async def verification_payloads(db, run_id: str, events) -> dict[int, dict]:
    """旧订阅接口展示完整验收报告，正文直接取自同一运行的正式工具事件。"""
    references = {
        event.id: event.payload
        for event in events
        if event.kind == "verification" and event.payload.get("source_event_id")
    }
    if not references:
        return {}
    source_ids = {payload["source_event_id"] for payload in references.values()}
    rows = await db.execute(
        select(Event.id, Event.payload["result"]).where(
            Event.run_id == run_id,
            Event.kind == "tool",
            Event.id.in_(source_ids),
            Event.payload["name"].astext == "verify_project",
        )
    )
    reports = dict(rows.all())
    return {
        key: {**reports[payload["source_event_id"]], **payload}
        for key, payload in references.items()
        if payload["source_event_id"] in reports
    }


async def timeline_page(
    db, run_id: str, page: int, page_size: int, kind: str | None, through: int | None, view: str
) -> dict:
    ceiling = (
        through
        if through is not None
        else (await db.scalar(select(func.max(Event.id)).where(Event.run_id == run_id)) or 0)
    )
    status = Event.payload["status"].astext
    phase = func.coalesce(Event.payload["next_node"].astext, Event.payload["phase"].astext)
    step = Event.payload["step_index"].astext
    metadata = (
        select(
            Event.id,
            Event.kind,
            status.label("status"),
            phase.label("phase"),
            step.label("step"),
            func.lag(status).over(order_by=Event.id).label("previous_status"),
            func.lag(phase).over(order_by=Event.id).label("previous_phase"),
            func.lag(step).over(order_by=Event.id).label("previous_step"),
            Event.payload["tool"].astext.label("tool"),
            Event.payload["operation_id"].astext.label("operation"),
            func.lag(Event.kind).over(order_by=Event.id).label("previous_kind"),
            func.lag(Event.payload["name"].astext).over(order_by=Event.id).label("previous_name"),
        )
        .where(Event.run_id == run_id, Event.id <= ceiling)
        .subquery()
    )
    conditions = []
    event_type = metadata.c.kind
    if view == "activity":
        recorded = aliased(Event)
        paired = exists(
            select(recorded.id).where(
                recorded.run_id == run_id,
                recorded.kind == "tool",
                recorded.id < metadata.c.id,
                recorded.payload["operation_id"].astext == metadata.c.operation,
            )
        )
        legacy_pair = and_(
            metadata.c.operation.is_(None),
            metadata.c.previous_kind == "tool",
            metadata.c.previous_name == metadata.c.tool,
        )
        duplicate = and_(
            metadata.c.kind == "tool_result",
            or_(and_(metadata.c.operation.is_not(None), paired), legacy_pair),
        )
        conditions.append(not_(func.coalesce(duplicate, False)))
        repeated_state = and_(
            metadata.c.kind == "state",
            metadata.c.previous_kind == "state",
            or_(metadata.c.status.is_not(None), metadata.c.phase.is_not(None)),
            metadata.c.status.is_not_distinct_from(metadata.c.previous_status),
            metadata.c.phase.is_not_distinct_from(metadata.c.previous_phase),
            metadata.c.step.is_not_distinct_from(metadata.c.previous_step),
        )
        conditions.append(not_(func.coalesce(repeated_state, False)))
        event_type = case((metadata.c.kind == "tool_result", "tool"), else_=metadata.c.kind)
    if kind:
        conditions.append(event_type == kind)
    total = await db.scalar(select(func.count()).select_from(metadata).where(*conditions))
    result = await db.execute(
        select(Event, event_type.label("type"))
        .join(metadata, Event.id == metadata.c.id)
        .where(*conditions)
        .order_by(Event.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = result.all()
    reports = await verification_payloads(db, run_id, (row for row, _ in rows))
    return {
        "items": [
            {
                "id": str(row.id),
                "type": event_type,
                "data": reports.get(row.id, row.payload),
                "created_at": row.created_at,
            }
            for row, event_type in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "through": str(ceiling),
    }
