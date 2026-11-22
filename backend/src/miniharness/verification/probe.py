"""在独立产品进程中采集实际观测；此进程不接收预期值或测试断言。"""

import asyncio
import contextlib
import importlib
import inspect
import io
import json
import sys
from decimal import Decimal
from pathlib import Path


def decimal_number(value):
    if not isinstance(value, Decimal):
        raise TypeError(f"{type(value).__name__} 不能表示为 JSON")
    if value.is_finite():
        if value == value.to_integral_value():
            limit = sys.get_int_max_str_digits()
            if not limit or value.adjusted() < limit:
                return int(value)
        number = float(value)
        if Decimal(str(number)) == value:
            return number
    raise ValueError("Decimal 超出 JSON 数值的无损观测范围")


def main():
    request_path, response_path = sys.argv[1:]
    probe = json.loads(Path(request_path).read_text(encoding="utf-8"))
    root = Path.cwd()
    target = Path(probe["target"])
    parts = list(target.with_suffix("").parts)
    sys.path.insert(0, str(root))
    if parts[0] == "src":
        sys.path.insert(0, str(root / "src"))
        parts.pop(0)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    output, errors = io.StringIO(), io.StringIO()
    result = {}
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        function = importlib.import_module(".".join(parts))
        for part in probe["symbol"].split("."):
            function = getattr(function, part)
        if not callable(function):
            raise ValueError("指定的公开接口不可调用")
        try:
            value = function(*probe["args"], **probe["kwargs"])
            if inspect.isawaitable(value):
                value = asyncio.run(value)
        except BaseException as exc:
            result.update(raised=True, exception=type(exc).__name__, message=str(exc))
        else:
            result.update(raised=False)
            result["return"] = value
    result.update(stdout=output.getvalue(), stderr=errors.getvalue())
    Path(response_path).write_text(
        json.dumps(result, ensure_ascii=False, allow_nan=False, default=decimal_number),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
