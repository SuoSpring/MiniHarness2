"""在产品进程之外执行冻结断言；每个用例使用独立的输入/文件副本。"""

import hashlib
import json
import math
import operator
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from miniharness.workspace.paths import project_files, safe_path

from .programs import check_program

_project: Path | None = None
_timeout: float = 30
NUMERIC_RELATIONS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}


def configure(project: Path, timeout: float) -> None:
    global _project, _timeout
    _project, _timeout = project.resolve(), timeout


def _file(path: Path) -> dict:
    result = {"exists": path.is_file(), "text": None}
    if path.is_file():
        result["size"] = path.stat().st_size
        try:
            result["text"] = path.read_text(encoding="utf-8")
        except UnicodeError:
            result["binary"] = True
    return result


def _observe_step(probe: dict, work: Path, location: Path, timeout: float) -> dict:
    if probe["kind"] == "file":
        result = _file(safe_path(work, probe["target"]))
    else:
        request_path, response_path = location / "request.json", location / "response.json"
        response_path.unlink(missing_ok=True)
        request_path.write_text(json.dumps(probe, ensure_ascii=False), encoding="utf-8")
        if probe["kind"] == "call":
            arguments = [
                "-I",
                str(Path(__file__).with_name("probe.py")),
                str(request_path),
                str(response_path),
            ]
        else:
            target = safe_path(work, probe["target"])
            if not target.is_file():
                raise AssertionError(f"脚本入口不存在：{probe['target']}")
            arguments = [str(target), *probe["args"]]
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-u", "-B", *arguments],
            cwd=work,
            env={**os.environ, "MINIHARNESS_VERIFY": "1"},
            input=probe["stdin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if probe["kind"] == "call":
            if process.returncode != 0 or not response_path.is_file():
                raise AssertionError(f"产品观测进程没有提供有效结果：{process.stderr[-1500:]}")
            result = json.loads(response_path.read_text(encoding="utf-8"))
        else:
            result = {
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
        if probe["json_stdout"]:
            result["json"] = json.loads(result["stdout"])
    result["files"] = {name: _file(safe_path(work, name)) for name in probe["read_files"]}
    return result


def observe(probe: dict) -> dict:
    if _project is None:
        raise RuntimeError("独立测试必须由框架验证器启动")
    with TemporaryDirectory(prefix="harness-case-") as temporary:
        location = Path(temporary)
        work = location / "project"
        shutil.copytree(
            _project, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
        )
        originals = {
            path: hashlib.sha256(path.read_bytes()).digest() for path in project_files(work)
        }
        sequence = probe["kind"] == "sequence"
        deadline = time.monotonic() + _timeout
        results = []
        for step in probe["steps"] if sequence else [probe]:
            for name, content in step["fixtures"].items():
                target = safe_path(work, name)
                if target.suffix.casefold() == ".py" and (
                    target.exists() or not target.is_relative_to(work / "__harness_inputs__")
                ):
                    raise ValueError(f"临时 Python 输入须使用专用目录中的新文件：{name}")
                if target in originals and (sequence or target.suffix.casefold() == ".py"):
                    raise ValueError(f"多步测试输入不能覆盖原文件：{name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                originals[target] = hashlib.sha256(target.read_bytes()).digest()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("多步用例达到超时上限")
            results.append(_observe_step(step, work, location, remaining))
            for path, fingerprint in originals.items():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).digest() != fingerprint
                ):
                    raise AssertionError(
                        f"独立测试期间改写或删除了原始项目文件：{path.relative_to(work)}"
                    )
        return {"steps": results} if sequence else results[0]


def at_path(value, path):
    for part in path:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise ValueError(f"路径 {path} 中没有 {part!r}")
    return value


def sorted_permutation(actual, original, *, descending=False) -> bool:
    if (
        not isinstance(actual, list)
        or not isinstance(original, list)
        or len(actual) != len(original)
    ):
        return False

    def counts(values):
        return Counter(json.dumps(value, sort_keys=True, allow_nan=False) for value in values)

    if counts(actual) != counts(original):
        return False
    compare = operator.ge if descending else operator.le
    try:
        return all(compare(a, b) for a, b in zip(actual, actual[1:]))
    except TypeError:
        return False


def check_case(case: dict) -> None:
    if "driver" in case:
        if _project is None:
            raise RuntimeError("独立测试必须由框架验证器启动")
        check_program(case, _project, _timeout)
        return
    # 预期值只留在判定进程，observe/probe 不接收 assertions。
    observed = observe(case["probe"])
    for assertion in case["assertions"]:
        actual = observed
        for part in assertion["path"]:
            try:
                actual = actual[part]
            except (KeyError, IndexError, TypeError) as exc:
                raise AssertionError(
                    f"{case['id']} 缺少观测 {assertion['path']}：{observed!r}"
                ) from exc
        expected, operator = assertion["expected"], assertion["operator"]
        if assertion.get("compare_to"):
            expected = at_path(observed, assertion["compare_to"])
        if operator in {"sorted_permutation", "descending_permutation"}:
            probe = case["probe"]
            if probe["kind"] == "sequence":
                probe = probe["steps"][assertion["path"][1]]
            expected = at_path(probe, assertion["input_path"])
            passed = sorted_permutation(
                actual, expected, descending=operator == "descending_permutation"
            )
        elif operator == "length_equals":
            passed = isinstance(actual, (str, list, dict)) and len(actual) == expected
        elif operator == "equals":
            passed = actual == expected
        elif operator == "not_equals":
            passed = actual != expected
        elif operator == "text_equals":
            passed = (
                isinstance(actual, str)
                and isinstance(expected, str)
                and (
                    actual.replace("\r\n", "\n").removesuffix("\n")
                    == expected.replace("\r\n", "\n").removesuffix("\n")
                )
            )
        elif operator == "contains":
            passed = isinstance(actual, (str, list, dict)) and expected in actual
        elif operator in NUMERIC_RELATIONS:
            passed = (
                type(actual) in (int, float)
                and type(expected) in (int, float)
                and NUMERIC_RELATIONS[operator](actual, expected)
            )
        else:
            passed = (
                type(actual) in (int, float)
                and type(expected) in (int, float)
                and math.isclose(actual, expected, rel_tol=0, abs_tol=assertion["tolerance"])
            )
        if not passed:
            raise AssertionError(
                f"{case['id']} {case['description']}：{assertion['path']} 预期 {operator} {expected!r}，实际 {actual!r}"
            )
