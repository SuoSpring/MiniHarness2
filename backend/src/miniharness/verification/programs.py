"""Independent acceptance drivers. The child entry point uses only the standard library."""

import ast
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def contains_number(value) -> bool:
    if isinstance(value, dict):
        return any(contains_number(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_number(v) for v in value)
    return type(value) in (int, float) or (
        isinstance(value, str) and bool(re.search(r"(?<!\w)[+-]?\d+(?:\.\d+)?(?!\w)", value))
    )


def _entry_function(tree, name):
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in functions:
        if fn.name == name:
            return fn
    # A regular pytest-style test or a standalone reference function can keep its own name.
    if len(functions) == 1 and all(
        isinstance(
            n,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.AnnAssign,
            ),
        )
        or (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
        for n in tree.body
    ):
        return functions[0]
    return None


def function_source(source: str, name: str) -> str:
    """Accept a function, a function body, or a single expression."""
    source = textwrap.dedent(source).strip()
    if source.startswith(("```python\n", "```py\n", "```\n")) and source.endswith("\n```"):
        source = source.split("\n", 1)[1].rsplit("\n", 1)[0]
    if not source:
        raise ValueError(f"{name} 需要可执行的代码")
    try:
        tree = ast.parse(source)
        if _entry_function(tree, name) is None:
            if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
                source = "return " + ast.unparse(tree.body[0].value)
            body = textwrap.indent(source, "    ")
            source = f"def {name}(inputs):\n{body}"
            try:
                compile(source, f"<{name}>", "exec")
            except SyntaxError:
                source = "async " + source
        compile(source, f"<{name}>", "exec")
    except SyntaxError as exc:
        raise ValueError(f"{name} 第 {exc.lineno} 行：{exc.msg}") from exc
    return source


def _bindings(tree):
    # A reassigned or incremented variable can be the result of a real calculation.
    bindings = {}
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assignments[node.id] = assignments.get(node.id, 0) + 1
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
    return {key: value for key, value in bindings.items() if assignments.get(key) == 1}


def _literal(node, bindings):
    if isinstance(node, ast.Name):
        node = bindings.get(node.id, node)
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return None


def _precomputed_answer(node, bindings):
    if isinstance(node, ast.Name):
        node = bindings.get(node.id, node)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "float", "str"}
        and len(node.args) == 1
    ):
        return contains_number(_literal(node.args[0], bindings))
    if isinstance(node, ast.Subscript):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "inputs"
            and isinstance(node.slice, ast.Constant)
        ):
            if node.slice.value in {"expected", "answer", "expected_output"}:
                return True
        table = _literal(node.value, bindings)
        if isinstance(table, (list, tuple, dict)) and contains_number(table):
            return True
    return contains_number(_literal(node, bindings))


def literal_comparisons(tree, *, include_returns=False) -> set[str]:
    bindings = _bindings(tree)
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            verdict = node.test
        elif include_returns and isinstance(node, ast.Return) and node.value:
            verdict = node.value
        else:
            continue
        if isinstance(verdict, ast.Name):
            verdict = bindings.get(verdict.id, verdict)
        for expr in ast.walk(verdict):
            if not isinstance(expr, ast.Compare):
                continue
            for left, op, right in zip(
                [expr.left, *expr.comparators[:-1]], expr.ops, expr.comparators, strict=True
            ):
                if not isinstance(op, ast.Eq):
                    continue
                if any(
                    isinstance(v, ast.Attribute) and v.attr == "returncode" for v in (left, right)
                ):
                    continue
                for side in (left, right):
                    value = _literal(side, bindings)
                    if contains_number(value):
                        result.add(f"{ast.dump(expr)} = {value!r}")
    return result


def validate_program(source: str, name: str, *, property_test: bool = False) -> None:
    tree = ast.parse(function_source(source, name))
    entry = _entry_function(tree, name)
    positional = [*entry.args.posonlyargs, *entry.args.args]
    if len(positional) - len(entry.args.defaults) > 1 or any(
        d is None for d in entry.args.kw_defaults
    ):
        raise ValueError(f"{name} 入口接受一个 inputs 参数，也可以使用无参函数")
    bindings = _bindings(entry)
    returns = [n for n in ast.walk(entry) if isinstance(n, ast.Return) and n.value]
    assertions = [n for n in ast.walk(entry) if isinstance(n, ast.Assert)]
    if not returns and not (property_test and assertions):
        raise ValueError(f"{name} 需要返回可观测的结果")
    if name == "expected":
        if all(_precomputed_answer(n.value, bindings) for n in returns):
            raise ValueError("精确数值须由 expected 根据输入实际计算，不能直接返回写死的答案")
    if property_test:
        if all(_literal(r.value, bindings) is not None for r in returns) and not any(
            _literal(a.test, bindings) is None for a in assertions
        ):
            raise ValueError("性质检查需要观测产品行为；固定值不能验证需求")
        if literal_comparisons(tree, include_returns=True):
            raise ValueError("性质应比较观测或输入；精确数值请返回实际值并用独立 expected 计算")


class _CountAssertions(ast.NodeTransformer):
    def visit_Assert(self, node):
        record = ast.Expr(ast.Call(ast.Name("__mh_record_check__", ast.Load()), [], []))
        return [node, ast.copy_location(record, node)]


def _invoke(name, source, inputs, directory, work, deadline, *, property_test=False):
    request, response = directory / "request.json", directory / "response.json"
    request.write_text(
        json.dumps(
            {"source": source, "inputs": inputs, "property": property_test}, ensure_ascii=False
        ),
        "utf-8",
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("独立用例执行超时")
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", __file__, name, str(request), str(response)],
        cwd=work,
        env={**os.environ, "MINIHARNESS_VERIFY": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=remaining,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if run.returncode or not response.is_file():
        raise AssertionError(f"{name} 执行失败：\n{(run.stderr or run.stdout)[-3000:]}")
    return json.loads(response.read_text(encoding="utf-8"))


def check_program(case, project, timeout):
    deadline = time.monotonic() + timeout
    reference = case.get("reference")
    expected = True
    if reference is not None:
        with TemporaryDirectory(prefix="harness-reference-") as temporary:
            calc = Path(temporary)
            expected = _invoke("expected", reference, case["inputs"], calc, calc, deadline)
    with TemporaryDirectory(prefix="harness-driver-") as temp:
        root = Path(temp)
        work = root / "project"
        shutil.copytree(
            project, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
        )
        actual = _invoke(
            "observe",
            case["driver"],
            case["inputs"],
            root,
            work,
            deadline,
            property_test=reference is None,
        )
        if reference is None:
            if actual is not True:
                raise AssertionError(f"{case['id']} 性质检查未通过，实际返回 {actual!r}")
        elif actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            raise AssertionError(f"{case['id']} 参考程序计算出 {expected!r}，实际得到 {actual!r}")


def _main():
    name, request, destination = sys.argv[1:]
    cfg = json.loads(Path(request).read_text(encoding="utf-8"))
    if name == "observe":
        sys.path.insert(0, str(Path.cwd()))
    source = function_source(cfg["source"], name)
    source_path = Path(request).with_suffix(".py")
    source_path.write_text(source, encoding="utf-8")
    namespace = {"__name__": "__acceptance__", "__file__": str(source_path)}
    tree = ast.parse(source)
    entry_name = _entry_function(tree, name).name
    checked = False

    def record_check():
        nonlocal checked
        checked = True

    if cfg["property"]:
        namespace["__mh_record_check__"] = record_check
        tree = ast.fix_missing_locations(_CountAssertions().visit(tree))
    exec(compile(tree, str(source_path), "exec"), namespace)
    fn = namespace[entry_name]
    signature = inspect.signature(fn)
    try:
        signature.bind(cfg["inputs"])
    except TypeError:
        value = fn()
    else:
        value = fn(cfg["inputs"])
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    if cfg["property"] and value is None:
        value = checked
    Path(destination).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), "utf-8")


if __name__ == "__main__":
    _main()
