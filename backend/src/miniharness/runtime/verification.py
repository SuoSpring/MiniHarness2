import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select

from miniharness.runtime.limits import ProjectRunSettings, RunLimits
from miniharness.storage.models import AcceptanceSuite, Project, Run, Verification, new_id, utcnow
from miniharness.verification.contracts import gate_passed, policy_hash
from miniharness.verification.runner import environment_hash, run_checks

from .acceptance import bundle_hash
from .errors import NotFound

if TYPE_CHECKING:
    from .manager import Runtime


def _case_report(path: Path, descriptions: dict[str, str]) -> list[dict] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    phases = defaultdict(dict)
    for item in report.get("reports", []):
        phases[item["id"]][item["when"]] = "skipped" if item.get("xfail") else item["outcome"]
    failures = {item["id"]: item["message"] for item in report.get("failures", [])}
    cases = []
    for name in report.get("collected", []):
        outcomes = phases[name]
        if "failed" in outcomes.values():
            status = "failed"
        elif "skipped" in outcomes.values():
            status = "skipped"
        elif all(outcomes.get(when) == "passed" for when in ("setup", "call", "teardown")):
            status = "passed"
        else:
            status = "incomplete"
        cases.append(
            {
                "id": name,
                "description": descriptions.get(name.replace("\\", "/").rsplit("/", 1)[-1])
                or report.get("descriptions", {}).get(name, ""),
                "status": status,
                "error": failures.get(name),
            }
        )
    return cases


def verification_result(row: Verification) -> dict:
    return {
        "id": row.id,
        "snapshot": row.snapshot,
        "policy_hash": row.policy_hash,
        "task_revision": row.task_revision,
        "environment_hash": row.environment_hash,
        "status": row.status,
        "checks": row.checks,
        "error": row.error,
        "suite_ids": row.suite_ids,
        "suite_hash": row.suite_hash,
        "scope": row.scope,
    }


class VerificationService:
    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime

    async def run(self, project_id: str, snapshot: str, policy: dict, **kwargs) -> dict:
        async with self.runtime.environments.use(project_id) as environment:
            return await self._run(project_id, snapshot, policy, environment, **kwargs)

    async def _run(
        self,
        project_id: str,
        snapshot: str,
        policy: dict,
        python_environment,
        *,
        run_id: str | None = None,
        task_revision: int = 0,
        suite_ids: list[str] | None = None,
        scope: dict | None = None,
    ) -> dict:
        scope = scope or {"phase": 3, "final": True, "key": "final"}
        async with self.runtime.database.session() as db:
            try:
                suites = await self.runtime.acceptance.load(db, project_id, suite_ids or [])
            except ValidationError as exc:
                return {
                    "id": "",
                    "snapshot": snapshot,
                    "status": "error",
                    "checks": [],
                    "error": f"独立验收契约不符合观测协议，未执行产品测试：{exc}",
                }
            if run_id:
                run = await db.get(Run, run_id)
                limits = RunLimits.resolve(self.runtime.settings, run.limits)
            else:
                project = await db.get(Project, project_id)
                limits = ProjectRunSettings.resolve(self.runtime.settings, project.run_settings)
        identifiers = [suite["id"] for suite in suites]
        suite_fingerprint = bundle_hash(suites)
        fingerprint = policy_hash(policy)
        try:
            environment = await asyncio.to_thread(environment_hash, python_environment)
        except (ValueError, OSError) as error:
            return {
                "snapshot": snapshot,
                "status": "error",
                "checks": [],
                "error": f"Python 环境不可用：{error}",
            }
        # 仅同一调用内重用终态结果；每次合并始终重新执行。
        if run_id:
            async with self.runtime.database.session() as db:
                cached = await db.scalar(
                    select(Verification)
                    .where(
                        Verification.run_id == run_id,
                        Verification.snapshot == snapshot,
                        Verification.policy_hash == fingerprint,
                        Verification.task_revision == task_revision,
                        Verification.environment_hash == environment,
                        Verification.suite_hash == suite_fingerprint,
                        Verification.scope == scope,
                        Verification.status.in_(["passed", "failed"]),
                    )
                    .order_by(Verification.created_at.desc())
                )
                if cached:
                    return verification_result(cached)
        identifier = new_id()
        directory = (
            self.runtime.run_dir(run_id) / "verification" / identifier
            if run_id
            else self.runtime.settings.data_dir / "verifications" / identifier
        ).resolve()
        async with self.runtime.database.session.begin() as db:
            db.add(
                Verification(
                    id=identifier,
                    project_id=project_id,
                    run_id=run_id,
                    purpose="run" if run_id else "merge",
                    snapshot=snapshot,
                    policy_hash=fingerprint,
                    task_revision=task_revision,
                    environment_hash=environment,
                    status="running",
                    suite_ids=identifiers,
                    suite_hash=suite_fingerprint,
                    scope=scope,
                )
            )
        try:
            async with asyncio.timeout(limits.run_timeout or None):
                checks = await run_checks(
                    self.runtime.workspace,
                    project_id,
                    snapshot,
                    policy,
                    directory,
                    independent_suites=suites,
                    scope=scope,
                    python_environment=python_environment,
                )
            if environment != await asyncio.to_thread(environment_hash, python_environment):
                checks.append(
                    {
                        "id": "environment",
                        "status": "failed",
                        "error": "验收期间 Python 环境发生变化，请重新验收。",
                    }
                )
            if run_id:
                for check in checks:
                    for name in ("stdout_log", "stderr_log"):
                        if name in check:
                            check[name] = (
                                Path(check[name])
                                .relative_to(self.runtime.run_dir(run_id).resolve())
                                .as_posix()
                            )
            async with self.runtime.database.session.begin() as db:
                row = await db.get(Verification, identifier)
                row.checks = checks
                row.status = (
                    "passed"
                    if checks and all(c["status"] == "passed" for c in checks)
                    else "failed"
                )
                row.finished_at = utcnow()
                return verification_result(row)
        except BaseException as exc:

            async def record_error(error=exc):
                async with self.runtime.database.session.begin() as db:
                    row = await db.get(Verification, identifier)
                    row.status = (
                        "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                    )
                    row.error = f"{type(error).__name__}: {error}"
                    row.finished_at = utcnow()
                    return verification_result(row)

            result = await asyncio.shield(record_error())
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result

    async def accepted(
        self,
        db,
        result: dict,
        project_id: str,
        snapshot: str,
        policy: dict,
        task_revision: int,
        run_id: str | None = None,
        suite_ids: list[str] | None = None,
        scope: dict | None = None,
    ) -> bool:
        """最终提交只信任数据库中的宿主结果，不信任模型或请求体的 passed 字段。"""
        row = await db.get(Verification, result.get("id", ""))
        suites = await self.runtime.acceptance.load(db, project_id, suite_ids or [])
        project = await db.get(Project, project_id)
        try:
            environment = (
                await asyncio.to_thread(
                    environment_hash, self.runtime.environments.from_project(project)
                )
                if project
                else ""
            )
        except (ValueError, OSError):
            return False
        return bool(
            row
            and row.project_id == project_id
            and row.run_id == run_id
            and row.environment_hash == environment
            and suites
            and set(row.suite_ids) == {suite["id"] for suite in suites}
            and row.suite_hash == bundle_hash(suites)
            and gate_passed(verification_result(row), snapshot, policy, task_revision, scope)
        )

    async def case_results(self, project_id: str, verification_id: str) -> dict:
        async with self.runtime.database.session() as db:
            row = await db.get(Verification, verification_id)
            if row is None or row.project_id != project_id:
                raise NotFound("验收记录不存在。")
            suites = {
                suite.id: suite
                for suite in await db.scalars(
                    select(AcceptanceSuite).where(
                        AcceptanceSuite.project_id == project_id,
                        AcceptanceSuite.id.in_(row.suite_ids),
                    )
                )
            }
            descriptions = {}
            for index, suite_id in enumerate(row.suite_ids):
                if suite := suites.get(suite_id):
                    for case in suite.contract["cases"]:
                        descriptions[f"test_suite_{index}.py::test_{case['id']}"] = case[
                            "description"
                        ]
        folder = (
            self.runtime.run_dir(row.run_id) / "verification"
            if row.run_id
            else self.runtime.settings.data_dir / "verifications"
        ) / row.id
        result = {}
        for check in row.checks:
            if check.get("kind") not in {"pytest", "independent"}:
                continue
            independent = (
                check["kind"] == "independent" or check.get("origin") == "独立生成的验收测试"
            )
            path = (
                folder / "independent" / "results.json"
                if independent
                else folder / check["id"] / "pytest-results.json"
            )
            result[check["id"]] = await asyncio.to_thread(
                _case_report, path, descriptions if independent else {}
            )
        return result
