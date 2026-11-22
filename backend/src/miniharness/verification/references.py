"""只用出题时的输入计算预期。注册项都是框架代码，不加载产品模块。"""

import json
import math
import operator
import statistics
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .oracle import at_path


def _add_days(value: str, days: int) -> str:
    return (date.fromisoformat(value) + timedelta(days=days)).isoformat()


def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _hours(value: int | str) -> str:
    hours, minutes = divmod(int(value), 60)
    return f"{hours:02}:{minutes:02}"


def _inversions(values: list) -> int:
    if len(values) > 256:
        raise ValueError("穷举逆序对的输入上限是 256 个元素")
    return sum(a > b for i, a in enumerate(values) for b in values[i + 1 :])


FUNCTIONS = {
    "median": statistics.median,
    "mean": statistics.mean,
    "sum": sum,
    "length": len,
    "add": operator.add,
    "subtract": operator.sub,
    "multiply": operator.mul,
    "divide": operator.truediv,
    "sqrt": math.sqrt,
    "gcd": math.gcd,
    "sorted": sorted,
    "sorted_desc": lambda values: sorted(values, reverse=True),
    "json_loads": json.loads,
    "add_days": _add_days,
    "days_between": _days_between,
    "minutes_hhmm": _hours,
    "inversion_count": _inversions,
    "input": lambda value: value,
}


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    function: str = Field(json_schema_extra={"enum": list(FUNCTIONS)})
    inputs: list[list[str | int]] = Field(min_length=1, max_length=8)
    decode: list[Literal["value", "json"]] = Field(default_factory=list, max_length=8)
    result_path: list[str | int] = Field(default_factory=list, max_length=12)
    render: Literal["value", "text", "line"] = "value"

    @field_validator("function")
    @classmethod
    def registered(cls, value: str) -> str:
        if value not in FUNCTIONS:
            raise ValueError(f"未注册可信参考实现 {value}，可选项为 {', '.join(FUNCTIONS)}")
        return value

    def calculate(self, probe: dict):
        values = []
        decoders = self.decode or ["value"] * len(self.inputs)
        if len(decoders) != len(self.inputs):
            raise ValueError("每个输入路径须对应一个 decode 选项")
        for path, decoder in zip(self.inputs, decoders, strict=True):
            if not path or path[0] not in {"args", "kwargs", "stdin", "fixtures"}:
                raise ValueError("参考实现的操作数须引用测试输入 args、kwargs、stdin 或 fixtures")
            value = at_path(probe, path)
            if decoder == "json":
                if not isinstance(value, str):
                    raise ValueError(f"{path} 的 JSON 解码需要字符串输入")
                value = json.loads(value)
            values.append(value)
        try:
            if self.function in {"add", "subtract", "multiply", "divide"}:
                if not all(type(value) in (int, float) for value in values):
                    raise ValueError("算术操作数必须是数值")
            value = at_path(FUNCTIONS[self.function](*values), self.result_path)
            json.dumps(value, allow_nan=False)
            if self.render != "value":
                return str(value) + ("\n" if self.render == "line" else "")
            return value
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError(f"参考实现 {self.function} 无法计算这些输入：{exc}") from exc
