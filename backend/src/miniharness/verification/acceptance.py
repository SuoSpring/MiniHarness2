"""独立验收契约、三等分检查点，以及不允许改写断言的测试编译器。"""

import ast
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from miniharness.milestones import active_plan, planned_checkpoints, review_criteria
from miniharness.workspace.paths import project_files

from .programs import contains_number, validate_program
from .references import Reference, at_path

ACCEPTANCE_RUNTIME_VERSION = "acceptance-v7"
INDEPENDENT_CHECK = "@independent"
MAX_PLAN_ENTRIES = 8


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in value or "\0" in value:
        raise ValueError("测试路径必须在项目内")
    return path.as_posix()


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: list[str | int] = Field(min_length=1, max_length=12)
    operator: Literal[
        "equals",
        "not_equals",
        "text_equals",
        "contains",
        "approx",
        "lt",
        "le",
        "gt",
        "ge",
        "sorted_permutation",
        "descending_permutation",
        "length_equals",
    ] = "equals"
    expected: JsonValue = None
    tolerance: float = Field(default=1e-8, gt=0, allow_inf_nan=False)
    reference: Reference | None = Field(
        default=None, description="需推导精确值时优先选用。框架独立使用原始输入计算预期。"
    )
    input_path: list[str | int] | None = None
    compare_to: list[str | int] | None = Field(
        default=None,
        min_length=1,
        max_length=12,
        description="验证两项观测的关系时填写另一条观测路径，例如两次调用的返回值。",
    )

    def require_computed_number(self) -> None:
        if (
            self.reference is None
            and self.compare_to is None
            and self.operator in {"equals", "text_equals", "approx", "length_equals"}
            and self.path[-1] != "exit_code"
            and contains_number(self.expected)
        ):
            raise ValueError("精确数值预期须使用独立 reference 计算")

    @model_serializer(mode="wrap")
    def frozen_fields(self, serialize):
        # 兼容旧版冻结源码的序列化和 AST。
        data = serialize(self)
        for key in ("reference", "input_path", "compare_to"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def numeric_tolerance(self) -> Self:
        if (
            self.operator == "length_equals"
            and not self.reference
            and (type(self.expected) is not int or self.expected < 0)
        ):
            raise ValueError("length_equals 的 expected 必须是非负整数")
        if self.compare_to is not None:
            if (
                self.compare_to == self.path
                or self.reference
                or self.input_path
                or self.expected is not None
            ):
                raise ValueError("compare_to 需要另一项独立观测，不能同时指定其他预期来源")
            if self.operator not in {"equals", "not_equals", "approx", "lt", "le", "gt", "ge"}:
                raise ValueError("compare_to 仅用于两项观测的等值、近似或大小关系")
        elif self.operator in {"sorted_permutation", "descending_permutation"}:
            if (
                not self.input_path
                or self.input_path[0] not in {"args", "kwargs"}
                or self.reference
            ):
                raise ValueError('排序性质需要引用原始列表的 input_path，例如 ["args",0]')
        elif self.input_path is not None:
            raise ValueError("只有性质断言使用 input_path")
        elif not self.reference and "expected" not in self.model_fields_set:
            raise ValueError("缺少 expected 或可信 reference")
        if (
            self.operator in {"approx", "lt", "le", "gt", "ge"}
            and not self.reference
            and not self.compare_to
            and (isinstance(self.expected, bool) or not isinstance(self.expected, (int, float)))
        ):
            raise ValueError("数值比较需要数值预期")
        return self


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["call", "script", "file"]
    target: str = Field(
        min_length=1,
        max_length=300,
        description="项目内相对路径。call/script 选择已交付的产品脚本，可从公开接口目录或检查入口定位；file 填写要观测的项目文件。fixtures 用作产品的输入数据。",
    )
    symbol: str = ""
    args: list[JsonValue] = Field(default_factory=list, max_length=30)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    stdin: str = ""
    fixtures: dict[str, str] = Field(default_factory=dict)
    read_files: list[str] = Field(default_factory=list, max_length=10)
    json_stdout: bool = False

    @model_validator(mode="after")
    def safe_inputs(self) -> Self:
        self.target = relative(self.target)
        if self.target.split("/")[0].casefold() == "__harness_inputs__":
            raise ValueError("测试入口必须来自项目；临时输入只能作为参数或数据")
        if self.kind == "call" and not self.target.endswith(".py"):
            raise ValueError("导入公开函数需要 Python 模块入口")
        if self.kind == "call" and (
            not self.symbol
            or any(
                not part.isidentifier() or part.startswith("_") for part in self.symbol.split(".")
            )
        ):
            raise ValueError("只允许调用公开的函数或类方法")
        if self.kind == "script" and any(not isinstance(arg, str) for arg in self.args):
            raise ValueError("脚本参数必须是字符串")
        if len(self.fixtures) > 10:
            raise ValueError("每个用例最多十个输入文件")
        for name in self.fixtures:
            clean = relative(name)
            suffix = Path(clean).suffix.lower()
            if clean.casefold() == self.target.casefold() or suffix in {".pyi", ".pyd", ".so"}:
                raise ValueError("测试输入不能覆盖产品代码")
            if suffix == ".py" and not clean.startswith("__harness_inputs__/"):
                raise ValueError("临时 .py 数据必须放在 __harness_inputs__/ 下，不能覆盖产品代码")
        for name in self.read_files:
            relative(name)
        return self


class SequenceProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["sequence"]
    steps: list[Probe] = Field(min_length=2, max_length=8)


class AcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
    criterion_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=500)
    probe: Probe | SequenceProbe = Field(discriminator="kind")
    assertions: list[Assertion] = Field(min_length=1, max_length=8)

    def observation(self, path: list) -> tuple[Probe, list]:
        if isinstance(self.probe, SequenceProbe):
            if (
                len(path) < 3
                or path[0] != "steps"
                or type(path[1]) is not int
                or not 0 <= path[1] < len(self.probe.steps)
            ):
                raise ValueError(
                    f'用例 {self.id} 多步断言须指向实际步骤，例如 ["steps",0,"stdout"]'
                )
            return self.probe.steps[path[1]], path[2:]
        return self.probe, path

    @model_validator(mode="after")
    def observable_paths(self, info: ValidationInfo) -> Self:
        probes = self.probe.steps if isinstance(self.probe, SequenceProbe) else [self.probe]
        fixtures = {
            relative(name).casefold()
            for step in probes
            for name in step.fixtures
            if isinstance(self.probe, SequenceProbe)
            or Path(name).suffix.casefold() == ".py"
            or (info.context or {}).get("new_design")
        }
        for step in probes:
            if any(
                relative(name).casefold() in fixtures for name in [step.target, *step.read_files]
            ):
                raise ValueError("独立测试不能以刚注入的输入文件作为产品输出或入口")
        for index, assertion in enumerate(self.assertions):
            try:
                self._check_observation(assertion, info, assertion.path)
                if assertion.compare_to is not None:
                    self._check_observation(assertion, info, assertion.compare_to)
            except ValueError as exc:
                raise ValueError(f"assertions[{index}] — {exc}") from exc
        return self

    def _check_observation(
        self, assertion: Assertion, info: ValidationInfo, location: list
    ) -> None:
        fields = {
            "file": {"exists", "text", "size", "binary"},
            "script": {"exit_code", "stdout", "stderr"},
            "call": {"return", "raised", "exception", "message", "stdout", "stderr"},
        }
        probe, path = self.observation(location)
        root = path[0]
        if assertion.input_path and not isinstance(
            at_path(probe.model_dump(), assertion.input_path), list
        ):
            raise ValueError(f"用例 {self.id} 排序性质须引用列表输入")
        if root == "files":
            if len(path) == 1:
                return
            legacy = (
                (info.context or {}).get("allow_legacy_file_alias")
                and probe.kind == "file"
                and path[1] == probe.target
            )
            if path[1] not in probe.read_files and not legacy:
                raise ValueError(
                    f"用例 {self.id} 的 {path} 未在 probe.read_files 声明文件。"
                    'kind=file 的目标结果直接使用 ["exists"] 或 ["text"]；'
                    "files 仅包含 read_files 声明的额外文件。"
                )
            if len(path) > 3 or (
                len(path) == 3 and path[2] not in {"exists", "text", "size", "binary"}
            ):
                raise ValueError(f"用例 {self.id} 的文件观测路径无效：{path}")
        elif root == "json":
            if not probe.json_stdout or probe.kind == "file":
                raise ValueError(f"用例 {self.id} 使用 json 观测前必须启用 json_stdout")
        elif root not in fields[probe.kind]:
            raise ValueError(
                f"用例 {self.id} 的 {probe.kind} 观测不支持字段 {root}；可用字段：{sorted(fields[probe.kind])}"
            )
        elif root != "return" and len(path) != 1:
            raise ValueError(f"用例 {self.id} 的标量观测不能继续取下级路径：{path}")
        if any(isinstance(part, int) and part < 0 for part in path):
            raise ValueError("观测数组下标不能为负数")


class PlanEntryCheck(BaseModel):
    """用户审批的脚本调用，随最终独立用例冻结。"""

    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1, description="此调用验证的验收条目 ID。")
    path: str = Field(
        min_length=1, max_length=300, description="用项目 Python 解释器执行的实际文件相对路径。"
    )
    args: list[str] = Field(
        default_factory=list, max_length=30, description="传给脚本的参数，每项一个。"
    )
    stdin: str = ""
    exit_code: int | Literal["nonzero"] = Field(description="预期退出码；只约定失败时写 nonzero。")
    stdout: str | None = Field(
        default=None,
        description="用户明确约定的输出原文。需计算的样例优先用 stdout_reference。忽略一个末尾换行。",
    )
    stdout_reference: Reference | None = Field(
        default=None, description="独立计算预期输出，可引用 args 或 stdin；批准前展示计算结果。"
    )

    def for_approval(self) -> dict:
        entry = self.model_dump(exclude_none=True)
        if self.stdout_reference is not None:
            value = self.stdout_reference.calculate({"args": self.args, "stdin": self.stdin})
            if type(value) not in (str, int, float):
                raise ValueError("标准输出参考计算须得到文本或数值")
            entry["stdout"] = str(value)
        return entry

    @field_validator("path")
    @classmethod
    def script_path(cls, value: str) -> str:
        return Probe(kind="script", target=value).target

    @field_validator("args")
    @classmethod
    def valid_args(cls, args: list[str]) -> list[str]:
        if any("\0" in arg for arg in args):
            raise ValueError("入口参数不能包含空字符")
        return args

    def as_case(self, identifier: str) -> AcceptanceCase:
        if self.stdout_reference and self.stdout is None:
            raise ValueError("入口样例缺少已审批的参考输出")
        assertions = [
            Assertion(path=["exit_code"], operator="not_equals", expected=0)
            if self.exit_code == "nonzero"
            else Assertion(path=["exit_code"], expected=self.exit_code)
        ]
        if self.stdout is not None:
            assertions.append(
                Assertion(
                    path=["stdout"],
                    operator="text_equals",
                    expected=self.stdout,
                    reference=self.stdout_reference,
                )
            )
        return AcceptanceCase(
            id=identifier,
            criterion_ids=[self.criterion_id],
            description=f"计划入口 · {self.path}",
            probe=Probe(kind="script", target=self.path, args=self.args, stdin=self.stdin),
            assertions=assertions,
        )


class TestProgram(BaseModel):
    """The driver can use any operation supported by the project's environment."""

    __test__ = False
    description: str = ""
    inputs: JsonValue = None
    driver: str = Field(
        description="Python 源码。observe(inputs) 操作工作副本并返回性质布尔值，或返回要与独立参考值比较的实际数据。可填写函数体。"
    )
    reference: str | None = Field(
        default=None,
        description="仅需要精确结果时填写 Python 计算程序 expected(inputs)。在空目录独立执行，只接收 inputs。",
    )

    @model_validator(mode="before")
    @classmethod
    def no_literal_answer(cls, raw):
        if isinstance(raw, dict) and contains_number(raw.get("expected")):
            raise ValueError("请使用 reference 从 inputs 计算精确预期")
        return raw

    @model_validator(mode="after")
    def executable(self) -> Self:
        validate_program(self.driver, "observe", property_test=self.reference is None)
        if self.reference is not None:
            validate_program(self.reference, "expected")
        return self


class ProgramDesign(BaseModel):
    """Design the smallest set of independent behavioral checks, normally one."""

    cases: list[TestProgram] = Field(min_length=1)


class ProgramCase(TestProgram):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _case_format(value):
    if isinstance(value, dict):
        return "structured" if "probe" in value and "driver" not in value else "driver"
    return "structured" if isinstance(value, AcceptanceCase) else "driver"


SuiteCase = Annotated[
    Annotated[AcceptanceCase, Tag("structured")] | Annotated[ProgramCase, Tag("driver")],
    Discriminator(_case_format),
]


class FrozenSuite(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid")
    rationale: str = ""
    cases: list[SuiteCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        seen = set()
        for index, case in enumerate(self.cases):
            if case.id in seen:
                raise ValueError(f"cases[{index}].id — 用例编号必须唯一")
            seen.add(case.id)
        return self


class TestDesign(FrozenSuite):
    @model_validator(mode="before")
    @classmethod
    def number_programs(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
            return value
        used = {
            c.get("id")
            for c in value["cases"]
            if isinstance(c, dict) and isinstance(c.get("id"), str)
        }
        cases = []
        index = 0
        for case in value["cases"]:
            if isinstance(case, dict) and _case_format(case) == "driver" and not case.get("id"):
                index += 1
                while f"case_{index}" in used:
                    index += 1
                case = {**case, "id": f"case_{index}"}
            cases.append(case)
        return {**value, "cases": cases}

    def frozen_contract(
        self, entry_checks: list[dict] | None = None, *, preserve_legacy_numbers: bool = False
    ) -> dict:
        """保留设计者给出的预期；选择参考函数的断言在冻结时由输入求值一次。"""
        for case in self.cases:
            if isinstance(case, ProgramCase):
                continue
            for assertion in case.assertions:
                if assertion.reference:
                    probe, _ = case.observation(assertion.path)
                    assertion.expected = assertion.reference.calculate(probe.model_dump())
                    if assertion.operator == "length_equals" and (
                        type(assertion.expected) is not int or assertion.expected < 0
                    ):
                        raise ValueError(f"用例 {case.id} 的长度预期须算出非负整数")
                    if assertion.operator in {"approx", "lt", "le", "gt", "ge"} and type(
                        assertion.expected
                    ) not in (
                        int,
                        float,
                    ):
                        raise ValueError(f"用例 {case.id} 的数值比较需要数值预期")
                elif not preserve_legacy_numbers:
                    assertion.require_computed_number()
        contract = self.model_dump()
        used = {case.id for case in self.cases}
        number = 0
        for spec in entry_checks or []:
            number += 1
            while f"plan_entry_{number}" in used:
                number += 1
            case = PlanEntryCheck.model_validate(spec).as_case(f"plan_entry_{number}")
            contract["cases"].append(case.model_dump())
        return contract


def milestone_scope(state: dict, *, complete: bool = False) -> dict | None:
    plan = active_plan(state)
    steps = plan.get("steps", [])
    count = len(steps)
    through = state.get("step_index", 0) + 1
    if complete or not steps:
        phase, final, start, selected = 3, True, 0, steps
    else:
        points = {math.ceil(count * part / 3): part for part in (1, 2, 3)}
        if through not in planned_checkpoints(state):
            return None
        phase, final = points[through], through == count
        start = 0 if final else min(state.get("verified_step", 0), through)
        selected = steps if final else steps[start:through]
    criteria = [item["id"] for item in review_criteria(state, final=final)]
    if not criteria and not final:
        return None
    # 保留旧计划的范围摘要以复用已经冻结的最终契约。设计请求只传步骤编号。
    legacy = any("criterion_ids" in step for step in steps)
    keys = ("id", "done_when", "criterion_ids") if legacy else ("id",)
    scope = {
        "phase": phase,
        "final": final,
        "from_step": start,
        "through_step": count if final else through,
        "plan_version": plan.get("version", 0),
        "criterion_ids": criteria if final else sorted(criteria),
        "steps": [{key: step[key] for key in keys if key in step} for step in selected],
    }
    if final and plan.get("entry_checks"):
        scope["entry_checks"] = plan["entry_checks"]
    scope["key"] = digest(scope)
    return scope


def compile_tests(contract: dict, *, allow_legacy_file_alias: bool = False) -> str:
    """反思节点用确定性模板编译冻结契约中的输入与预期。"""
    design = FrozenSuite.model_validate(
        contract, context={"allow_legacy_file_alias": allow_legacy_file_alias}
    )
    lines = ["from miniharness.verification.oracle import check_case", ""]
    for case in design.cases:
        lines.extend([f"def test_{case.id}():", f"    check_case({case.model_dump()!r})", ""])
    return "\n".join(lines)


def validate_source(contract: dict, source: str, *, allow_legacy_file_alias: bool = False) -> None:
    if ast.dump(ast.parse(source)) != ast.dump(
        ast.parse(compile_tests(contract, allow_legacy_file_alias=allow_legacy_file_alias))
    ):
        raise ValueError("独立测试代码与冻结契约不一致，禁止放宽或删除断言")


def repair_legacy_file_paths(contract: dict, source: str) -> tuple[dict, list[dict]]:
    """仅迁移旧版误用的单文件目标路径；输入、断言值与运算符全部保留。"""
    validate_source(contract, source, allow_legacy_file_alias=True)
    repaired = FrozenSuite.model_validate(
        contract, context={"allow_legacy_file_alias": True}
    ).model_dump()
    changes = []
    for case in repaired["cases"]:
        probe = case["probe"]
        if probe["kind"] != "file":
            continue
        for assertion in case["assertions"]:
            path = assertion["path"]
            if (
                len(path) == 3
                and path[:2] == ["files", probe["target"]]
                and probe["target"] not in probe["read_files"]
            ):
                assertion["path"] = path[2:]
                changes.append({"case_id": case["id"], "from": path, "to": path[2:]})
    # 不能推断的其他错误仍然拒绝，不将其转换成更宽松的测试。
    FrozenSuite.model_validate(repaired)
    return repaired, changes


def interface_catalog(directory: Path) -> list[dict]:
    """仅暴露文件名和公开调用签名，不向测试设计模型提供实现正文、输出或执行推理。"""
    result = []
    for path in project_files(directory):
        name = path.relative_to(directory).as_posix()
        if len(result) >= 60:
            break
        if "tests" in path.relative_to(directory).parts or path.name.startswith("test_"):
            continue
        entry = {"path": name, "callables": []}
        if path.suffix == ".py" and path.stat().st_size <= 200000:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (SyntaxError, UnicodeError):
                tree = None
            for node in tree.body if tree else []:
                members = [(node, "")]
                if isinstance(node, ast.ClassDef):
                    # call 探针调用类本身的属性，不能构造 GUI 或传入 self 实例。
                    members = [
                        (child, node.name + ".")
                        for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and any(
                            isinstance(d, ast.Name) and d.id in {"staticmethod", "classmethod"}
                            for d in child.decorator_list
                        )
                    ]
                for function, prefix in members:
                    if isinstance(
                        function, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and not function.name.startswith(("_", "test_")):
                        positional = [*function.args.posonlyargs, *function.args.args]
                        if prefix and any(
                            isinstance(d, ast.Name) and d.id == "classmethod"
                            for d in function.decorator_list
                        ):
                            positional = positional[1:]
                        parameters = [*positional, *function.args.kwonlyargs]
                        annotations = {
                            arg.arg: ast.unparse(arg.annotation)
                            for arg in parameters
                            if arg.annotation is not None
                        }
                        entry["callables"].append(
                            {
                                "name": prefix + function.name,
                                "parameters": [arg.arg for arg in parameters],
                                **({"parameter_types": annotations} if annotations else {}),
                                **(
                                    {"return_type": ast.unparse(function.returns)}
                                    if function.returns is not None
                                    else {}
                                ),
                            }
                        )
        result.append(entry)
    return result
