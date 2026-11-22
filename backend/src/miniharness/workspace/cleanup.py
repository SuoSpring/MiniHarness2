"""只操作应用拥有的明确目录；不跟随目录链接和 Windows 连接点。"""

import os
import re
import shutil
import stat
from pathlib import Path


def owned_path(root: Path, *parts: str) -> Path:
    root = root.resolve()
    if not parts or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts
    ):
        raise ValueError("无效的清理路径")
    literal = root.joinpath(*parts).absolute()
    if literal.resolve() != literal or not literal.is_relative_to(root) or literal == root:
        raise ValueError(f"清理路径越界或包含目录链接：{literal}")
    return literal


def remove_owned(root: Path, *parts: str) -> None:
    target = owned_path(root, *parts)
    if not target.exists():
        return

    def readonly(function, filename, info):
        path = Path(filename)
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(target):
            raise ValueError("不能修改清理目录以外的文件权限")
        if not isinstance(info[1], PermissionError):
            raise info[1]
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(filename)

    # Python 3.11 的 rmtree 不递归进入符号链接/Windows junction。
    # 入口仍独立检查，避免把被替换的顶层目录当成应用自己的目录。
    if target.is_dir():
        shutil.rmtree(target, onerror=readonly)
    else:
        target.unlink()
