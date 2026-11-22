"""Python 环境的解析、检查和子进程环境；不依赖目标环境安装 MiniHarness。"""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def python_path(reference: str) -> Path:
    path = Path(reference).expanduser()
    if not path.is_absolute():
        raise ValueError("请指定 Python 解释器或环境目录的绝对路径，或选择已发现的环境名称。")
    if path.is_dir():
        candidates = (path / "Scripts/python.exe", path / "bin/python", path / "python.exe")
        path = next((candidate for candidate in candidates if candidate.is_file()), path)
    if not path.is_file():
        raise ValueError(f"Python 解释器不存在：{path}")
    # 不 resolve 符号链接：POSIX venv 的 python 通常链接到系统解释器。
    return Path(os.path.abspath(path))


def inspect_python(reference: str, *, packages: bool = False) -> dict:
    executable = python_path(reference)
    source = (
        "import sys,json,platform,importlib.metadata; "
        "print(json.dumps(dict(python=sys.executable,prefix=sys.prefix,"
        "version=platform.python_version(),base_prefix=sys.base_prefix,"
        "platform=platform.platform(),packages="
        + (
            "sorted((d.metadata.get('Name',''),d.version) for d in importlib.metadata.distributions())"
            if packages
            else "[]"
        )
        + ")))"
    )
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    environment = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", source],
            cwd=executable.parent,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise ValueError(result.stderr[-1500:])
        info = json.loads(result.stdout)
        if not all(isinstance(info.get(key), str) for key in ("python", "prefix", "version")):
            raise ValueError("解释器未返回有效环境信息")
        if tuple(int(part) for part in info["version"].split(".")[:2]) < (3, 11):
            raise ValueError("项目运行与自动验收需要 Python 3.11 或更新版本")
        # 保留用户选择的 venv 入口。
        info["python"] = str(executable)
        return info
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError(f"无法使用 Python 环境 {executable}：{exc}") from exc


@dataclass(frozen=True)
class PythonEnvironment:
    python: str
    prefix: str
    name: str = ""
    managed: bool = False
    version: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "PythonEnvironment":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})

    def variables(self) -> dict[str, str]:
        executable = python_path(self.python)
        prefix = Path(self.prefix)
        paths = [executable.parent, prefix / "Scripts", prefix / "bin"]
        if (prefix / "conda-meta").is_dir():
            paths += [prefix, prefix / "Library/bin", prefix / "Library/usr/bin"]
        result = {
            "PATH": os.pathsep.join([*(str(path) for path in paths), os.environ.get("PATH", "")]),
            "PYTHONNOUSERSITE": "1",
        }
        if (prefix / "conda-meta").is_dir():
            result.update(CONDA_PREFIX=str(prefix), CONDA_DEFAULT_ENV=self.name or prefix.name)
        else:
            result["VIRTUAL_ENV"] = str(prefix)
        return result

    def fingerprint(self) -> str:
        details = inspect_python(self.python, packages=True)
        return hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()


def backend_environment() -> PythonEnvironment:
    return PythonEnvironment(
        sys.executable, sys.prefix, "MiniHarness 后端", version=sys.version.split()[0]
    )
