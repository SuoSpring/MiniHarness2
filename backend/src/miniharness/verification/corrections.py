"""定位失败用例并校验测试修正，保留原版本和未涉及的断言。"""

import ast
import re
from collections import Counter
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .acceptance import (
    INDEPENDENT_CHECK,
    AcceptanceCase,
    Assertion,
    PlanEntryCheck,
    Probe,
    SequenceProbe,
    TestDesign,
    TestProgram,
)
from .programs import literal_comparisons


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    check_id: str
    cause: Literal["test", "implementation", "mixed", "uncertain", "environment"]
    requirement: str = Field(min_length=1, description="从用户规格推出的正确行为")
    evidence: str = Field(min_length=1, description="结合失败输入、断言和实现解释依据")


class AssertionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    index: int = Field(ge=0, description="原断言的从零开始的索引")
    replacements: list[Assertion] = Field(min_length=1)


class ProbeEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    probe: Probe | SequenceProbe


class ProgramEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    program: TestProgram


class IndependentTestFix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["independent"]
    suite_id: str
    edits: list[AssertionEdit | ProbeEdit | ProgramEdit] = Field(min_length=1)


class ProjectTestFix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["project"]
    check_id: str
    path: str
    old: str = Field(min_length=1, description="在失败测试文件中唯一匹配的原文")
    new: str = Field(min_length=1)


def failed_targets(gate: dict) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """将 pytest 节点名映射到当时实际使用的套件及项目文件。"""
    suites: dict[str, set[str]] = {}
    files: dict[str, set[str]] = {}
    for check in gate.get("checks", []):
        if check["status"] == "passed" or check.get("origin") == "独立生成的验收测试":
            continue
        if check.get("kind") == "python":
            files[check["id"]] = {check["path"]}
            continue
        for failure in [*check.get("case_failures", []), *check.get("collection_failures", [])]:
            node = failure.get("id", "").replace("\\", "/")
            path, _, name = node.partition("::")
            if check["id"] == INDEPENDENT_CHECK:
                match = re.fullmatch(r"test_suite_(\d+)\.py", PurePosixPath(path).name)
                if match and name.startswith("test_") and int(match[1]) < len(gate["suite_ids"]):
                    suites.setdefault(gate["suite_ids"][int(match[1])], set()).add(name[5:])
            elif check.get("kind") == "pytest":
                relative = path.removeprefix(f"{check['id']}/work/")
                target = PurePosixPath(check["path"])
                candidates = [relative]
                # 失败的断言可能定义在测试目录的共享 helper 中。
                if failure.get("source_path") and target.suffix != ".py":
                    candidates.append(failure["source_path"])
                for name in candidates:
                    file = PurePosixPath(name)
                    if (
                        (file == target or target in file.parents)
                        and file.suffix == ".py"
                        and not (file.is_absolute() or ".." in file.parts or ":" in name)
                    ):
                        files.setdefault(check["id"], set()).add(file.as_posix())
    return suites, files


def amend_contract(original: dict, scope: dict, fix: IndependentTestFix, failed: set[str]) -> dict:
    contract = deepcopy(original)
    cases = {case["id"]: case for case in contract["cases"]}
    edits: dict[str, dict[int, list[dict]]] = {}
    probes = set()
    programs = set()
    for edit in fix.edits:
        if edit.case_id not in failed or edit.case_id not in cases:
            raise ValueError("只能修正本次失败的独立用例")
        case = cases[edit.case_id]
        if any(
            PlanEntryCheck.model_validate(entry).as_case(edit.case_id).model_dump() == case
            for entry in scope.get("entry_checks", [])
        ):
            raise ValueError("计划确认的入口行为须按原规格验证")
        if isinstance(edit, ProgramEdit):
            if "driver" not in case or edit.case_id in programs:
                raise ValueError("程序修正须指向本次失败的独立驱动，每个用例只能替换一次")
            programs.add(edit.case_id)
            replacement = edit.program.model_dump()
            replacement["description"] = edit.program.description or case.get("description", "")
            case.update(replacement)
        elif "driver" in case:
            raise ValueError("请用 program 提交修正后的独立测试驱动")
        elif isinstance(edit, ProbeEdit):
            if edit.case_id in probes:
                raise ValueError("同一测试输入出现多次修正")
            probes.add(edit.case_id)
            case["probe"] = edit.probe.model_dump()
        else:
            changes = edits.setdefault(edit.case_id, {})
            if edit.index >= len(case["assertions"]) or edit.index in changes:
                raise ValueError("断言索引超出原用例范围或重复")
            for assertion in edit.replacements:
                assertion.require_computed_number()
            changes[edit.index] = [item.model_dump() for item in edit.replacements]
    for case_id in probes | edits.keys():
        case = cases[case_id]
        for index, replacements in sorted(edits.get(case_id, {}).items(), reverse=True):
            case["assertions"][index : index + 1] = replacements
        checked = AcceptanceCase.model_validate(case, context={"new_design": True})
        # Replacing a failed assertion leaves unrelated historical literals intact.
        frozen = TestDesign(rationale=contract["rationale"], cases=[checked]).frozen_contract(
            preserve_legacy_numbers=case_id not in probes
        )
        case.update(frozen["cases"][0])
    if contract == original:
        raise ValueError("修正没有改变测试契约")
    return contract


def corrected_test_text(
    original: str,
    fix: ProjectTestFix,
    *,
    product_entry: bool = False,
    failed_functions: set[str] | None = None,
    standalone_assertions: bool = False,
) -> str:
    if original.count(fix.old) != 1 or fix.old == fix.new:
        raise ValueError("请重新读取失败测试并给出唯一匹配、确有变化的替换")
    updated = original.replace(fix.old, fix.new, 1)
    try:
        after = ast.parse(updated)
    except SyntaxError as exc:
        raise ValueError(f"修正后的测试无法解析：{exc}") from exc
    try:
        before = ast.parse(original)
    except SyntaxError:
        before = None  # pytest 收集错误本身也可能来自测试文件的语法错误。

    previous_literals = literal_comparisons(before) if before else set()
    if literal_comparisons(after) - previous_literals:
        raise ValueError("修正的精确数值断言须用独立计算获得预期，不能把模型给出的数字写入测试")

    def tests(tree):
        return {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        }

    if before:
        existing, revised = tests(before), tests(after)
        if not existing.keys() <= revised.keys():
            raise ValueError("测试修正不能移除已有测试函数")
        for name, function in existing.items():
            if (
                failed_functions
                and name not in failed_functions
                and ast.dump(function) != ast.dump(revised[name])
            ):
                raise ValueError(f"测试函数 {name} 没有出现在本次失败记录中")
            if any(isinstance(n, ast.Assert) for n in ast.walk(function)) and not any(
                isinstance(n, (ast.Assert, ast.Call)) for n in ast.walk(revised[name])
            ):
                raise ValueError(f"测试修正不能清空 {name} 的检查")

    def shortcuts(tree):
        found = Counter()
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in {
                "skip",
                "skipif",
                "xfail",
                "skipTest",
                "expectedFailure",
            }:
                found[n.attr] += 1
            if isinstance(n, ast.Assert) and isinstance(n.test, ast.Constant) and n.test.value:
                found["恒真断言"] += 1
        return found

    if shortcuts(after) - (shortcuts(before) if before else Counter()):
        raise ValueError("测试修正不能以跳过用例或恒真断言替代检查")
    # pytest 也允许验收一个含产品函数的脚本；这类文件中仅可改测试函数。
    path = PurePosixPath(fix.path)
    if product_entry or not (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or "tests" in path.parts[:-1]
    ):

        def non_tests(tree):
            return [
                "测试断言" if standalone_assertions and isinstance(n, ast.Assert) else ast.dump(n)
                for n in tree.body
                if not (
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name.startswith("test")
                )
            ]

        if before is None or non_tests(before) != non_tests(after):
            raise ValueError("混合脚本中的产品代码必须保持原版本")
    return updated
