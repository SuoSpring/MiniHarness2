"""不可变 Git 文件版本与真实执行目录。"""

from .git_store import GitWorkspace
from .paths import safe_path

__all__ = ["GitWorkspace", "safe_path"]
