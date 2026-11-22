"""独立设计只读用户规格与接口目录；编译阶段不接触普通执行上下文。"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    messages_to_dict,
)
from sqlalchemy import select

from miniharness.agent import prompts
from miniharness.agent.contracts import json_response_text, validation_feedback
from miniharness.context import ContextConfig, prepare_context
from miniharness.context.manager import message_tokens, token_count
from miniharness.milestones import active_plan, review_criteria
from miniharness.storage.models import AcceptanceSuite, Event, Run, new_id
from miniharness.verification.acceptance import (
    ACCEPTANCE_RUNTIME_VERSION,
    ProgramDesign,
    TestDesign,
    compile_tests,
    digest,
    interface_catalog,
    repair_legacy_file_paths,
    validate_source,
)
from miniharness.verification.corrections import (
    IndependentTestFix,
    amend_contract,
    failed_targets,
)

if TYPE_CHECKING:
    from .manager import Runtime
    from .services import RunServices


def suite_result(row: AcceptanceSuite) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "task_revision": row.task_revision,
        "scope": row.scope,
        "contract": row.contract,
        "contract_hash": row.contract_hash,
        "source": row.source,
        "status": row.status,
        "active": row.active,
    }


def bundle_hash(suites: list[dict]) -> str:
    return digest(
        [
            ACCEPTANCE_RUNTIME_VERSION,
            sorted((suite["id"], suite["contract_hash"]) for suite in suites),
        ]
    )


class AcceptanceService:
    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    async def load(self, db, project_id: str, identifiers: list[str]) -> list[dict]:
        if not identifiers:
            return []
        rows = list(
            await db.scalars(
                select(AcceptanceSuite)
                .where(
                    AcceptanceSuite.project_id == project_id,
                    AcceptanceSuite.id.in_(identifiers),
                )
                .order_by(AcceptanceSuite.created_at, AcceptanceSuite.id)
            )
        )
        if {row.id for row in rows} != set(identifiers):
            raise ValueError("独立测试契约缺失或不属于当前项目")
        replaced = {old for row in rows if row.active for old in row.scope.get("replaces", [])}
        suites = []
        for row in rows:
            if not row.active or row.id in replaced:
                continue
            if row.status != "ready" or digest(row.contract) != row.contract_hash:
                raise ValueError("独立测试尚未编译或契约已被改写")
            validate_source(row.contract, row.source)
            suites.append(suite_result(row))
        return suites

    async def required_ids(self, db, state: dict, *, phase: int = 3) -> list[str]:
        own = list(
            await db.scalars(
                select(AcceptanceSuite)
                .where(
                    AcceptanceSuite.run_id == state["run_id"],
                    AcceptanceSuite.active.is_(True),
                    AcceptanceSuite.status == "ready",
                    AcceptanceSuite.task_revision >= state.get("test_contract_floor", 1),
                    AcceptanceSuite.task_revision <= state["task_revision"],
                )
                .order_by(AcceptanceSuite.created_at)
            )
        )
        replaced = {old for row in own for old in row.scope.get("replaces", [])}
        if phase < 3:
            scope = state.get("test_scope", {})
            return [
                row.id
                for row in own
                if row.task_revision == state["task_revision"]
                and row.scope_key == scope.get("key")
                and row.id not in replaced
            ]
        return list(
            dict.fromkeys(
                [
                    *(key for key in state.get("baseline_suite_ids", []) if key not in replaced),
                    *(row.id for row in own if row.id not in replaced),
                ]
            )
        )

    async def has_final(self, db, state: dict) -> bool:
        from miniharness.verification.acceptance import milestone_scope

        scope = milestone_scope(state, complete=True)
        return bool(
            await db.scalar(
                select(AcceptanceSuite.id)
                .where(
                    AcceptanceSuite.run_id == state["run_id"],
                    AcceptanceSuite.task_revision == state["task_revision"],
                    AcceptanceSuite.scope_key == scope["key"],
                    AcceptanceSuite.status == "ready",
                    AcceptanceSuite.active.is_(True),
                )
                .limit(1)
            )
        )

    async def prepare(self, services: "RunServices", state: dict, scope: dict) -> dict:
        criteria = review_criteria(state, final=scope["final"])
        if {item["id"] for item in criteria} != set(scope["criterion_ids"]):
            raise ValueError("独立验收范围已经改变，请重新计算本阶段的条目")
        direct = not active_plan(state).get("steps")
        same_scope = (
            select(AcceptanceSuite)
            .where(
                AcceptanceSuite.run_id == state["run_id"],
                AcceptanceSuite.task_revision == state["task_revision"],
                AcceptanceSuite.scope_key == scope["key"],
                AcceptanceSuite.active.is_(True),
            )
            .order_by(AcceptanceSuite.created_at, AcceptanceSuite.id)
        )
        async with self.runtime.database.session() as db:
            existing = await db.scalar(same_scope)
            if existing:
                return {"id": existing.id, "status": existing.status, "model_calls": 0}
        run = await services.current()
        catalog = await asyncio.to_thread(interface_catalog, Path(run.working_dir))
        # 独立请求：禁止把 messages/summary/report/working_memory、实际输出或失败账本拼入这里。
        information = {
            "需求版本": state["task_revision"],
            "验收条目": criteria,
            "阶段范围": {**scope, "steps": [{"id": step["id"]} for step in scope["steps"]]},
            "公开接口目录": catalog,
            "项目检查入口": state["verification_policy"]["checks"],
        }
        if direct:
            information.update(
                {
                    "当前有效目标": state["task"],
                    "用户补充": state.get("user_updates", []),
                    "固定约束": state.get("pinned", []),
                }
            )
        history = [HumanMessage(content=json.dumps(information, ensure_ascii=False))]
        system = prompts.TEST_DESIGN
        model_calls = 0
        limits = await services.gateway.limits()
        context_config = ContextConfig(
            max_tokens=limits.max_context_tokens,
            output_tokens=limits.max_output_tokens,
            reserved_tokens=token_count(ProgramDesign.model_json_schema()) + 128,
            compression_threshold=limits.compression_threshold,
            keep_groups=1,
        )

        async def summarize(messages):
            nonlocal model_calls
            model_calls += 1
            response = await services.model("test_designer_summary", state, messages)
            return (
                str(response.content)
                if isinstance(response, AIMessage)
                else str(response.get("summary", ""))
            )

        limit = (
            context_config.max_tokens
            - context_config.output_tokens
            - context_config.reserved_tokens
            - token_count([])
        )
        if (
            context_config.max_tokens
            and message_tokens([SystemMessage(content=system), *history]) > limit
        ):
            # 有效需求、约束、阶段及入口固定；历史补充与接口目录才参与有损压缩。
            core = {
                key: value
                for key, value in information.items()
                if key not in {"用户补充", "公开接口目录"}
            }
            system += "\n当前有效验收依据（不能被历史摘要覆盖）：\n" + json.dumps(
                core, ensure_ascii=False
            )
            history[0] = HumanMessage(
                content=json.dumps(
                    {
                        key: information[key]
                        for key in ("用户补充", "公开接口目录")
                        if key in information
                    },
                    ensure_ascii=False,
                )
            )
        prepared = await prepare_context(
            messages_to_dict(history),
            system=system,
            summary="",
            protected_ids=set(),
            summarize=summarize,
            config=context_config,
        )
        messages = prepared.messages
        for attempt in (1, 2):
            model_calls += 1
            response = await services.model("test_designer", state, messages, schema=ProgramDesign)
            payload = response
            try:
                if isinstance(response, AIMessage):
                    payload = response.text
                    if response.response_metadata.get("finish_reason") == "length":
                        raise ValueError("输出被截断，请提交最少且完整的测试用例。")
                    payload = (
                        response.tool_calls[0]["args"]
                        if len(response.tool_calls) == 1
                        and response.tool_calls[0]["name"] in {"ProgramDesign", "TestDesign"}
                        else json_response_text(response.text)
                    )
                data = json.loads(payload) if isinstance(payload, str) else payload
                contract = TestDesign.model_validate(
                    data, context={"new_design": True}
                ).frozen_contract(scope.get("entry_checks"))
                break
            except ValueError as exc:
                error = validation_feedback(exc)
                await services.event("test_design_rejected", {"attempt": attempt, "error": error})
                if attempt == 2:
                    return {
                        "status": "error",
                        "error": f"独立用例无法编译：{error}",
                        "model_calls": model_calls,
                    }
                feedback = f"请根据下列校验错误修正上一份测试设计，返回完整用例。这是本轮唯一一次格式纠正。\n{error[:2000]}"
                draft = AIMessage(
                    content=payload
                    if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False)
                )
                repair = HumanMessage(content=feedback)
                messages = [*prepared.messages, draft, repair]
                if context_config.max_tokens and message_tokens(messages) > limit:
                    compressed = await prepare_context(
                        messages_to_dict([*history, draft, repair]),
                        system=system,
                        summary="",
                        protected_ids=set(),
                        summarize=summarize,
                        config=context_config,
                    )
                    messages = compressed.messages
        identifier = new_id()
        async with self.runtime.database.session.begin() as db:
            # 同一范围并发返回的设计使用首份冻结结果，各自的模型请求仍正常计量。
            await db.execute(select(Run.id).where(Run.id == state["run_id"]).with_for_update())
            existing = await db.scalar(same_scope)
            if existing:
                return {"id": existing.id, "status": existing.status, "model_calls": model_calls}
            db.add(
                AcceptanceSuite(
                    id=identifier,
                    project_id=state["project_id"],
                    run_id=state["run_id"],
                    task_revision=state["task_revision"],
                    scope=scope,
                    scope_key=scope["key"],
                    contract=contract,
                    contract_hash=digest(contract),
                    status="draft",
                )
            )
        await services.event(
            "test_design", {"id": identifier, "scope": scope, "cases": len(contract["cases"])}
        )
        return {"id": identifier, "status": "draft", "model_calls": model_calls}

    async def failed_sources(self, state: dict) -> dict:
        gate = state["verification"]
        targets, files = failed_targets(gate)
        cases = {}
        async with self.runtime.database.session() as db:
            for suite in await self.load(db, state["project_id"], list(targets)):
                cases[suite["id"]] = [
                    case
                    for case in suite["contract"]["cases"]
                    if case["id"] in targets[suite["id"]]
                ]
        sources = {}
        for name in sorted({path for paths in files.values() for path in paths}):
            try:
                content = await asyncio.to_thread(
                    self.runtime.workspace.read, state["project_id"], gate["snapshot"], name
                )
            except FileNotFoundError:
                sources[name] = "该文件只出现在运行时的测试副本中，请查看收集日志。"
                continue
            source = content.decode("utf-8", errors="replace")
            sources[name] = (
                source
                if len(source) <= 8000
                else source[:4000] + "\n…可按路径继续 read_file…\n" + source[-4000:]
            )
        return {
            "失败的独立用例": cases,
            "项目检查与文件": {k: sorted(v) for k, v in files.items()},
            "项目测试源码": sources,
        }

    async def correct_contract(
        self, state: dict, fix: IndependentTestFix, generation: int
    ) -> list[str]:
        gate = state["verification"]
        targets, _ = failed_targets(gate)
        if fix.suite_id not in targets:
            raise ValueError("待修正套件没有出现在本次失败记录中")
        key = digest([state["run_id"], gate["id"], fix.model_dump()])[:32]
        async with self.runtime.database.session.begin() as db:
            run = await db.get(Run, state["run_id"], with_for_update=True)
            if run.status != "running" or run.generation != generation:
                raise asyncio.CancelledError
            loaded = await self.load(db, state["project_id"], [fix.suite_id])
            if not loaded:
                raise ValueError("待修正套件已停用")
            original = loaded[0]
            existing = await db.get(AcceptanceSuite, key)
            if not existing:
                contract = amend_contract(
                    original["contract"], original["scope"], fix, targets[fix.suite_id]
                )
                scope = original["scope"]
                row = AcceptanceSuite(
                    id=key,
                    project_id=run.project_id,
                    run_id=run.id,
                    task_revision=state["task_revision"],
                    scope={**scope, "replaces": [*scope.get("replaces", []), original["id"]]},
                    scope_key=scope["key"],
                    contract=contract,
                    contract_hash=digest(contract),
                    status="draft",
                )
                db.add(row)
        await self.compile(state, key)
        async with self.runtime.database.session() as db:
            return await self.required_ids(db, state, phase=state["test_scope"]["phase"])

    async def repair_legacy_file_contracts(self, run_id: str) -> list[dict]:
        """维护操作：暂停时修复可唯一确定的旧协议错误，并保存原始契约与来源。"""
        repaired = []
        async with self.runtime.database.session.begin() as db:
            run = await db.get(Run, run_id, with_for_update=True)
            if run is None or run.status != "paused":
                raise ValueError("必须先暂停任务，再修复旧验收契约")
            suites = list(
                await db.scalars(
                    select(AcceptanceSuite)
                    .where(AcceptanceSuite.run_id == run_id, AcceptanceSuite.active.is_(True))
                    .with_for_update()
                )
            )
            for row in suites:
                if digest(row.contract) != row.contract_hash:
                    raise ValueError("冻结测试契约已被改写，不能自动修复")
                contract, changes = repair_legacy_file_paths(row.contract, row.source)
                if not changes:
                    continue
                before = {"contract": row.contract, "source": row.source, "hash": row.contract_hash}
                row.contract, row.contract_hash = contract, digest(contract)
                row.source = compile_tests(contract)
                record = {
                    "suite_id": row.id,
                    "changes": changes,
                    "before": before,
                    "hash": row.contract_hash,
                }
                db.add(Event(run_id=run_id, kind="acceptance_contract_repaired", payload=record))
                repaired.append(record)
            if repaired:
                run.state = {
                    **run.state,
                    "review_refresh_notice": (
                        "独立验收契约中单文件目标的错误读取路径已修复，所有输入、预期和用例保持不变。"
                        "之前的观测协议错误不能视为产品实现失败。请依据当前文件、实际调用方式与修订号匹配的证据重新审查，并在具备条件时提出真实验收申请。"
                    ),
                }
        # 这些是可重建的导出文件；执行时仍以校验过的数据库契约为准。
        for record in repaired:
            async with self.runtime.database.session() as db:
                row = await db.get(AcceptanceSuite, record["suite_id"])
                directory = self.runtime.run_dir(run_id) / "acceptance" / row.id
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "contract.json").write_text(
                    json.dumps(row.contract, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (directory / "test_acceptance.py").write_text(row.source, encoding="utf-8")
        # 不恢复任务、不执行模型或工具。原生暂停位置及待执行工具批次都继续保留。
        run = await self.runtime.get_run(run_id)
        if notice := run.state.get("review_refresh_notice"):
            from .services import RunServices

            async with self.runtime.database.session.begin() as db:
                saved = await db.get(Run, run_id, with_for_update=True)
                if saved.status != "paused":
                    raise ValueError("任务已恢复，停止更新暂停现场")
                graph = self.runtime.graph(RunServices(self.runtime, saved))
                config = {"configurable": {"thread_id": run_id}}
                checkpoint = await graph.aget_state(config)
                update = {
                    "review_refresh_notice": notice,
                    "feedback": notice,
                    "review": {},
                    "verification": {},
                    "pending_review": {},
                    "repair_watch": {},
                    "review_watch": {},
                    "stall": {},
                    "model_retry": {},
                    "message_role": "reflection",
                }
                if checkpoint.values:
                    # 清空停滞计数不能让 control 的路由绕过原生暂停节点。
                    if "pause" in checkpoint.next:
                        update["pause_requested"] = True
                    await graph.aupdate_state(config, update)
                    saved.state = (await graph.aget_state(config)).values
                else:
                    saved.state = {**saved.state, **update}
        return repaired

    async def compile(self, state: dict, identifier: str) -> dict:
        async with self.runtime.database.session.begin() as db:
            row = await db.get(AcceptanceSuite, identifier, with_for_update=True)
            if (
                row is None
                or row.run_id != state["run_id"]
                or row.task_revision != state["task_revision"]
                or not row.active
            ):
                raise ValueError("测试编写任务已过期")
            if digest(row.contract) != row.contract_hash:
                raise ValueError("冻结测试契约已被改写")
            source = compile_tests(row.contract)
            if row.source and row.source != source:
                raise ValueError("既有测试断言已被改写，禁止继续验收")
            directory = self.runtime.run_dir(row.run_id) / "acceptance" / row.id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "contract.json").write_text(
                json.dumps(row.contract, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (directory / "test_acceptance.py").write_text(source, encoding="utf-8")
            row.source, row.status = source, "ready"
            return {"id": row.id, "status": "ready", "cases": len(row.contract["cases"])}
