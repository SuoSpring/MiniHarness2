import re
import shutil
from pathlib import Path
from typing import TypedDict

import yaml

from miniharness.workspace.paths import safe_path


class SkillEntry(TypedDict):
    name: str
    description: str
    path: str
    enabled: bool


class LoadedSkill(TypedDict):
    name: str
    content: str
    directory: str
    note: str


class Skills:
    def __init__(self, directories: list[Path], disabled: set[str] | None = None) -> None:
        self.directories = directories
        self.disabled = disabled or set()

    def catalog(self) -> list[SkillEntry]:
        found: dict[str, SkillEntry] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for source in sorted(directory.glob("*/SKILL.md")):
                if source.is_symlink() or source.parent.is_symlink():
                    continue
                text = source.read_text(encoding="utf-8")
                sections = text.split("---", 2)
                if not text.startswith("---") or len(sections) != 3:
                    raise ValueError(f"Skill 缺少 YAML 元数据：{source}")
                metadata = yaml.safe_load(sections[1])
                name, description = metadata["name"], metadata["description"]
                if (
                    not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or name != source.parent.name
                ):
                    raise ValueError(f"Skill 名称与目录不一致：{source}")
                found[name] = {
                    "name": name,
                    "description": description,
                    "path": str(source.parent.resolve()),
                    "enabled": name not in self.disabled,
                }
        return list(found.values())

    def load(self, name: str, workdir: Path, resource: str = "SKILL.md") -> LoadedSkill:
        selected = next(
            (item for item in self.catalog() if item["name"] == name and item["enabled"]), None
        )
        if selected is None:
            raise ValueError(f"Skill 不存在或已停用：{name}")
        root = Path(selected["path"])
        source = safe_path(root, resource)
        if not source.is_file():
            raise FileNotFoundError(f"Skill 资源不存在：{resource}")
        destination = safe_path(workdir, f".harness-skills/{name}")
        if not destination.exists():
            if any(
                path.is_symlink() or path.resolve() != path.absolute() for path in root.rglob("*")
            ):
                raise ValueError("Skill 目录不能包含符号链接。")
            shutil.copytree(root, destination)
        # 一次调用固定使用首次加载的包副本，正文、参考资料和脚本保持同一版本。
        return {
            "name": name,
            "content": safe_path(destination, resource).read_text(encoding="utf-8"),
            "directory": f".harness-skills/{name}",
            "note": "按需读取 references；只可使用 run_python 执行其中 Python 脚本。",
        }
