"""在一次性文件副本中执行所有必需检查，不修改待交付版本。"""

import asyncio
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from miniharness.environments import PythonEnvironment
from miniharness.tools.python import run_python_command
from miniharness.workspace.git_store import GitWorkspace
from miniharness.workspace.paths import project_files, safe_path

from .acceptance import INDEPENDENT_CHECK, validate_source
from .contracts import VerificationPolicy


def environment_hash(python_environment: PythonEnvironment | None = None) -> str:
    if python_environment:
        return python_environment.fingerprint()
    packages = sorted(
        (dist.metadata.get("Name", ""), dist.version) for dist in importlib.metadata.distributions()
    )
    return hashlib.sha256(
        json.dumps([sys.executable, sys.version, platform.platform(), packages]).encode()
    ).hexdigest()


def source_digests(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project_files(directory)
    }


def unchanged(directory: Path, before: dict[str, str]) -> bool:
    # 允许测试产生新的输出文件，但原始代码、测试及输入都不能被检查过程改写。
    for name, digest in before.items():
        try:
            path = safe_path(directory, name)
            if path.is_symlink() or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        except (ValueError, OSError):
            return False
    return True


def passed_case_ids(report: dict) -> list[str]:
    completed: dict[str, list[str]] = {}
    for item in report.get("reports", []):
        phase = (
            item.get("when", "")
            if item.get("outcome") == "passed" and not item.get("xfail")
            else ""
        )
        completed.setdefault(item.get("id", ""), []).append(phase)
    return [
        name
        for name in report.get("collected", [])
        if completed.get(name) == ["setup", "call", "teardown"]
    ]


def tests_passed(report: dict, minimum: int) -> bool:
    collected = report.get("collected", [])
    reports = report.get("reports", [])
    if (
        report.get("exit_code") != 0
        or len(collected) < minimum
        or len(set(collected)) != len(collected)
        or report.get("collection_errors")
        or report.get("deselected")
        or any(item.get("outcome") != "passed" or item.get("xfail") for item in reports)
    ):
        return False
    # 每条收集到的测试必须完成 setup/call/teardown；跳过、xfail、缺失结果均不放行。
    expected = {
        (identifier, phase) for identifier in collected for phase in ("setup", "call", "teardown")
    }
    actual = [(item.get("id"), item.get("when")) for item in reports]
    return len(actual) == len(expected) and set(actual) == expected


async def run_checks(
    workspace: GitWorkspace,
    project_id: str,
    snapshot: str,
    policy: dict,
    directory: Path,
    *,
    independent_suites: list[dict] | None = None,
    scope: dict | None = None,
    python_environment: PythonEnvironment | None = None,
) -> list[dict]:
    specification = VerificationPolicy.model_validate(policy)
    directory.mkdir(parents=True, exist_ok=True)
    configuration = directory / "pytest.ini"
    configuration.write_text("[pytest]\n", encoding="utf-8")
    results = []
    independent = None
    if independent_suites is not None:
        independent = await run_independent(
            workspace,
            project_id,
            snapshot,
            independent_suites,
            directory,
            specification.timeout_seconds,
            python_environment=python_environment,
        )
        results.append(independent)
    for check in specification.checks if not scope or scope.get("final") else []:
        check_dir = directory / check.id
        work = check_dir / "work"
        # 每项从同一版本重新物化，避免前一项生成的文件影响后一项。
        await asyncio.to_thread(workspace.materialize, project_id, snapshot, work)
        before = await asyncio.to_thread(source_digests, work)
        result = {"id": check.id, "kind": check.kind, "path": check.path, "status": "failed"}
        if check.args:
            result["args"] = check.args
        try:
            target = safe_path(work, check.path)
            if not target.exists():
                if check.kind == "pytest" and check.path == "tests" and independent_suites:
                    # 默认 tests 入口由隔离编译器提供，不再要求正常执行上下文编写占位测试。
                    result.update(
                        {
                            key: value
                            for key, value in independent.items()
                            if key not in {"id", "path", "kind"}
                        }
                    )
                    result["origin"] = "独立生成的验收测试"
                    if result.get("tests_collected", 0) < check.min_tests:
                        result.update(status="failed", error="独立测试数量少于该入口的最少测试数")
                    results.append(result)
                    continue
                raise ValueError(f"必需检查入口不存在：{check.path}")
            report_path = check_dir / "pytest-results.json"
            arguments = (
                [str(target), *check.args]
                if check.kind == "python"
                else [
                    "-I",
                    str(Path(__file__).with_name("pytest_runner.py")),
                    str(target),
                    str(report_path.resolve()),
                    str(configuration.resolve()),
                ]
            )
            output = await run_python_command(
                work,
                arguments,
                check_dir / "logs",
                specification.timeout_seconds,
                {
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    **({"MINIHARNESS_VERIFY": "1"} if check.kind == "python" else {}),
                },
                python_environment=python_environment,
            )
            result.update(output)
            passed = output["exit_code"] == 0 and not output["timed_out"]
            if check.kind == "pytest":
                report = (
                    json.loads(report_path.read_text(encoding="utf-8"))
                    if report_path.exists()
                    else {}
                )
                result["tests_collected"] = len(report.get("collected", []))
                result["tests_passed"] = sum(
                    item.get("when") == "call"
                    and item.get("outcome") == "passed"
                    and not item.get("xfail")
                    for item in report.get("reports", [])
                )
                passed = passed and tests_passed(report, check.min_tests)
                if not passed:
                    result["case_failures"] = report.get("failures", [])
                    result["collection_failures"] = report.get("collection_failures", [])
                    result["collection_errors"] = [
                        str(error)[-1500:] for error in report.get("collection_errors", [])
                    ]
                    result["error"] = (
                        "必需测试未全部通过：检查失败、跳过、预期失败、零测试或结果缺失。"
                    )
            if not await asyncio.to_thread(unchanged, work, before):
                passed = False
                result["error"] = "验证过程改写或删除了原始项目文件，不能用于验收该版本。"
            elif check.kind == "pytest" and not passed and not output["timed_out"]:
                result["passed_case_ids"] = passed_case_ids(report)
            result["status"] = "passed" if passed else "failed"
        except (ValueError, OSError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


async def run_independent(
    workspace,
    project_id,
    snapshot,
    suites,
    directory,
    timeout,
    *,
    python_environment: PythonEnvironment | None = None,
) -> dict:
    result = {
        "id": INDEPENDENT_CHECK,
        "kind": "independent",
        "path": "独立验收契约",
        "status": "failed",
    }
    if not suites:
        return {**result, "error": "缺少反思独立设计并编译的验收测试"}
    base = directory / "independent"
    work, tests = base / "work", base / "tests"
    await asyncio.to_thread(workspace.materialize, project_id, snapshot, work)
    tests.mkdir(parents=True, exist_ok=True)
    expected = set()
    for index, suite in enumerate(suites):
        validate_source(suite["contract"], suite["source"])
        name = f"test_suite_{index}.py"
        (tests / name).write_text(suite["source"], encoding="utf-8")
        expected.update((name, f"test_{case['id']}") for case in suite["contract"]["cases"])
    before = await asyncio.to_thread(source_digests, tests)
    report_path, settings_path = base / "results.json", base / "settings.json"
    settings_path.write_text(
        json.dumps({"project_root": str(work.resolve()), "timeout": timeout}), encoding="utf-8"
    )
    output = await run_python_command(
        work,
        [
            "-I",
            str(Path(__file__).with_name("pytest_runner.py")),
            str(tests.resolve()),
            str(report_path.resolve()),
            str((directory / "pytest.ini").resolve()),
            str(settings_path.resolve()),
        ],
        base / "logs",
        timeout,
        {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        python_environment=python_environment,
    )
    result.update(output)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    collected = {
        (Path(name.split("::")[0]).name, name.split("::")[-1])
        for name in report.get("collected", [])
    }
    intact = await asyncio.to_thread(unchanged, tests, before)
    passed = (
        output["exit_code"] == 0
        and not output["timed_out"]
        and collected == expected
        and tests_passed(report, len(expected))
        and intact
    )
    result.update(
        status="passed" if passed else "failed",
        tests_collected=len(collected),
        case_names=[case for _, case in sorted(collected)[:8]],
        tests_passed=sum(
            item.get("when") == "call" and item.get("outcome") == "passed"
            for item in report.get("reports", [])
        ),
    )
    result["case_failures"] = report.get("failures", [])
    if not passed:
        result["collection_failures"] = report.get("collection_failures", [])
        result["collection_errors"] = [
            str(error)[-1500:] for error in report.get("collection_errors", [])
        ]
        if intact and not output["timed_out"] and collected == expected:
            result["passed_case_ids"] = passed_case_ids(report)
        result["error"] = "独立契约测试未全部通过，缺失/跳过/改写测试不能放行；预期结果保持冻结。"
    return result
