"""本地工具和版本存储共用的项目路径规则。"""

import hashlib
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath

IGNORED_DIRECTORIES = {
    ".git",
    ".harness-skills",
    ".venv",
    "node_modules",
    "__pycache__",
    ".logs",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def resolve_path(workdir: Path, path: str) -> Path:
    """解析显式路径与链接；是否允许写入由调用方的权限策略判断。"""
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ValueError("路径必须是非空字符串且不能包含空字符")
    target = Path(path)
    if os.name == "nt" and (target.drive or target.root) and not target.is_absolute():
        raise ValueError("请使用完整绝对路径或项目相对路径，不能使用盘符相对路径")
    return (target if target.is_absolute() else workdir / target).resolve()


def safe_path(workdir: Path, path: str) -> Path:
    """返回项目内的绝对路径；拒绝绝对路径、上跳和越界符号链接。"""
    relative = PurePosixPath(path.replace("\\", "/"))
    if not path or relative.is_absolute() or ".." in relative.parts or ":" in path or "\0" in path:
        raise ValueError("路径必须是项目内的相对路径，不能包含 ..、盘符或空字符")
    root = workdir.resolve()
    target = (root / str(relative)).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("路径必须指向项目内的文件")
    return target


def project_files(
    directory: Path, *, onerror: Callable[[OSError], None] | None = None
) -> list[Path]:
    """列出源文件；依赖、缓存、Git 元数据和链接不进入调用快照。"""
    files = []
    for current, directories, names in os.walk(directory, followlinks=False, onerror=onerror):
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES
            and not (Path(current) / name).is_symlink()
            and (Path(current) / name).resolve() == (Path(current) / name).absolute()
        )
        files.extend(
            Path(current) / name
            for name in sorted(names)
            if name != ".git"
            and not (Path(current) / name).is_symlink()
            and (Path(current) / name).is_file()
        )
    return files


def workspace_fingerprint(directory: Path) -> str:
    """为审批及证据复用核对受管文件内容；无法遍历时不能断言内容相同。"""

    def unreadable(error: OSError) -> None:
        raise error

    result = hashlib.sha256()
    for path in project_files(directory, onerror=unreadable):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        result.update(len(relative).to_bytes(8, "big"))
        result.update(relative)
        result.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
        content = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                content.update(chunk)
        result.update(content.digest())
    return result.hexdigest()
