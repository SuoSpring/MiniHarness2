"""Git 只保存不可变产物；哪些版本已提交，由 PostgreSQL 指针决定。"""

import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypeAlias, TypedDict, cast

from .paths import project_files, safe_path


class FileContentResolution(TypedDict):
    content: str | None


FileResolutions: TypeAlias = dict[str, Literal["ours", "theirs"] | FileContentResolution | None]


class FileConflict(TypedDict):
    path: str
    binary: bool
    base: str | None
    ours: str | None
    theirs: str | None


class MergeResult(TypedDict):
    snapshot: str | None
    conflicts: list[FileConflict]
    diff: str


class GitWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _repository(self, project_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
            raise ValueError("无效的项目标识")
        return self.root / f"{project_id}.git"

    def _git(
        self,
        repository: Path,
        arguments: list[str],
        *,
        data: bytes | None = None,
        index: Path | None = None,
        workdir: Path | None = None,
    ) -> bytes:
        # 隔离宿主仓库配置、换行转换和外部 diff，不修改用户自己的 Git 仓库。
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
            GIT_LITERAL_PATHSPECS="1",
            GIT_AUTHOR_NAME="MiniHarness",
            GIT_AUTHOR_EMAIL="local@miniharness",
            GIT_COMMITTER_NAME="MiniHarness",
            GIT_COMMITTER_EMAIL="local@miniharness",
            GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
            GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
        )
        if index:
            environment["GIT_INDEX_FILE"] = str(index)
        command = [
            "git",
            "--git-dir",
            str(repository),
            "-c",
            "core.autocrlf=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "gc.auto=0",
        ]
        if workdir:
            command.extend(["--work-tree", str(workdir.resolve())])
        result = subprocess.run(
            [*command, *arguments],
            input=data,
            capture_output=True,
            env=environment,
            cwd=repository.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        return result.stdout

    @staticmethod
    def _version(snapshot: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40,64}", snapshot):
            raise ValueError("无效的文件版本")
        return snapshot

    def ensure_project(self, project_id: str) -> str:
        """建立内部仓库，返回可用作首轮起点的空快照。"""
        repository = self._repository(project_id)
        repository.parent.mkdir(parents=True, exist_ok=True)
        if not repository.exists():
            self._git(repository, ["init", "--bare", "--quiet", str(repository)])
            self._git(repository, ["config", "gc.auto", "0"])
            # 快照保留执行目录的原始字节，不运行项目 .gitattributes 中的转换规则。
            (repository / "info" / "attributes").write_text(
                "* -text -filter -ident -working-tree-encoding\n",
                encoding="utf-8",
            )
            tree = self._git(repository, ["mktree"], data=b"").decode().strip()
            commit = (
                self._git(repository, ["commit-tree", tree], data=b"Candidate snapshot\n")
                .decode()
                .strip()
            )
            self._git(repository, ["update-ref", "refs/harness/empty", commit])
        return self._git(repository, ["rev-parse", "refs/harness/empty"]).decode().strip()

    def snapshot(self, project_id: str, directory: Path) -> str:
        """保存候选目录并保留不可变引用；数据库未提交时它只是孤立产物。"""
        self.ensure_project(project_id)
        repository = self._repository(project_id)
        directory = directory.resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"工作副本不存在：{directory}")
        with TemporaryDirectory(prefix="harness-index-") as temporary:
            index = Path(temporary) / "index"
            self._git(repository, ["read-tree", "--empty"], index=index)
            names = [file.relative_to(directory).as_posix() for file in project_files(directory)]
            if names:
                self._git(
                    repository,
                    ["add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul"],
                    data=b"\0".join(name.encode("utf-8") for name in names) + b"\0",
                    index=index,
                    workdir=directory,
                )
            tree = self._git(repository, ["write-tree"], index=index).decode().strip()
        commit = (
            self._git(repository, ["commit-tree", tree], data=b"Candidate snapshot\n")
            .decode()
            .strip()
        )
        self._git(repository, ["update-ref", f"refs/harness/snapshots/{commit}", commit])
        return commit

    def materialize(self, project_id: str, snapshot: str, directory: Path) -> None:
        """把快照展开到空目录；目录是可重建缓存，不是提交状态。"""
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError("物化目标目录必须为空")
        repository = self._repository(project_id)
        with TemporaryDirectory(prefix="harness-index-") as temporary:
            index = Path(temporary) / "index"
            self._git(repository, ["read-tree", self._version(snapshot)], index=index)
            self._git(
                repository,
                ["checkout-index", "--all", f"--prefix={directory.as_posix()}/"],
                index=index,
                workdir=directory,
            )

    def create_worktree(self, project_id: str, snapshot: str, directory: Path) -> None:
        """会话首次使用时创建真实的独立 Git 工作树，后续调用复用该目录。"""
        directory = directory.resolve()
        directory.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            self._repository(project_id),
            ["worktree", "add", "--detach", str(directory), self._version(snapshot)],
        )

    def _worktree_gitdir(self, project_id: str, directory: Path) -> Path:
        repository = self._repository(project_id).resolve()
        directory = directory.resolve()
        gitdir = Path(
            self._git(repository, ["rev-parse", "--resolve-git-dir", str(directory / ".git")])
            .decode()
            .strip()
        ).resolve()
        if not gitdir.is_relative_to(repository / "worktrees"):
            raise ValueError("工作树不属于当前项目的内部仓库")
        backlink = Path((gitdir / "gitdir").read_text().strip()).resolve()
        if backlink != directory / ".git":
            raise ValueError("工作树登记路径与待操作目录不一致")
        return gitdir

    def restore_worktree(self, project_id: str, snapshot: str, directory: Path) -> None:
        """只在回退/异常恢复时重置会话文件，保留不进入快照的依赖与缓存。"""
        directory = directory.resolve()
        gitdir = self._worktree_gitdir(project_id, directory)
        wanted = set(self.files(project_id, snapshot))
        self._git(gitdir, ["reset", "--hard", self._version(snapshot)], workdir=directory)
        for path in project_files(directory):
            name = path.relative_to(directory).as_posix()
            if name not in wanted:
                safe_path(directory, name).unlink()

    def remove_worktree(self, project_id: str, directory: Path) -> None:
        """仅删除经内部仓库反向登记确认的工作树。"""
        directory = directory.resolve()
        if not directory.exists():
            return
        self._worktree_gitdir(project_id, directory)
        self._git(self._repository(project_id), ["worktree", "remove", "--force", str(directory)])

    def diff(self, project_id: str, before: str, after: str) -> str:
        """返回完整 Git 补丁；下载用 UTF-8 + surrogateescape 编码以保留原字节。"""
        return self._git(
            self._repository(project_id),
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                self._version(before),
                self._version(after),
                "--",
            ],
        ).decode("utf-8", errors="surrogateescape")

    def changed_paths(self, project_id: str, before: str, after: str) -> list[str]:
        names = self._git(
            self._repository(project_id),
            [
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                self._version(before),
                self._version(after),
            ],
        )
        return [name.decode("utf-8") for name in names.split(b"\0") if name]

    def files(self, project_id: str, snapshot: str) -> list[str]:
        output = self._git(
            self._repository(project_id),
            ["ls-tree", "-rz", "--name-only", self._version(snapshot)],
        )
        return [name.decode("utf-8") for name in output.split(b"\0") if name]

    def read(self, project_id: str, snapshot: str, path: str) -> bytes:
        repository = self._repository(project_id)
        relative = safe_path(repository, path).relative_to(repository).as_posix()
        if relative not in self.files(project_id, snapshot):
            raise FileNotFoundError(path)
        return self._git(repository, ["show", f"{self._version(snapshot)}:{relative}"])

    def merge(
        self,
        project_id: str,
        base: str,
        ours: str,
        theirs: str,
        resolutions: FileResolutions | None = None,
    ) -> MergeResult:
        """三方合并；冲突时不发布版本。返回差异以 ours 为起点。

        resolutions[path] 可为 ours、theirs 或 {content: 文本或 None}，None 表示删除。
        反向撤销：base=原合并后，theirs=原合并前，ours=当前公共版本。
        """
        versions = [
            {path: self.read(project_id, version, path) for path in self.files(project_id, version)}
            for version in (base, ours, theirs)
        ]
        ancestor, local, incoming = versions
        merged: dict[str, bytes] = {}
        conflicts: dict[str, FileConflict] = {}
        resolutions = resolutions or {}
        for path in sorted(set(ancestor) | set(local) | set(incoming)):
            contents = [mapping.get(path) for mapping in versions]
            previous, current, proposed = contents
            resolution = resolutions.get(path)
            conflict = False
            if resolution == "ours":
                content = current
            elif resolution == "theirs":
                content = proposed
            elif isinstance(resolution, dict) and "content" in resolution:
                content = resolution["content"]
                content = content.encode("utf-8") if content is not None else None
            elif resolution is not None:
                raise ValueError(f"{path} 的冲突解决方式无效")
            elif current == previous:
                content = proposed
            elif proposed == previous or current == proposed:
                content = current
            elif None in contents or any(self._binary(item) for item in contents):
                content, conflict = current, True
            else:
                content, conflict = self._merge_text(*contents)
            if content is not None:
                merged[path] = content
            if conflict:
                conflicts[path] = self._conflict(path, contents)
        # 文件 a 与文件 a/b 不能共存；逐路径选择版本可消除这种目录结构冲突。
        for path in merged:
            parent = Path(path).parent
            while parent != Path("."):
                if parent.as_posix() in merged:
                    for name in (path, parent.as_posix()):
                        conflicts[name] = self._conflict(
                            name, [mapping.get(name) for mapping in versions]
                        )
                parent = parent.parent
        if conflicts:
            return {"snapshot": None, "conflicts": list(conflicts.values()), "diff": ""}
        with TemporaryDirectory(prefix="harness-merge-") as temporary:
            directory = Path(temporary)
            for path, content in merged.items():
                target = safe_path(directory, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            snapshot = self.snapshot(project_id, directory)
        return {
            "snapshot": snapshot,
            "conflicts": [],
            "diff": self.diff(project_id, ours, snapshot),
        }

    @staticmethod
    def _binary(content: bytes | None) -> bool:
        if content is None:
            return False
        try:
            content.decode("utf-8")
            return b"\0" in content
        except UnicodeDecodeError:
            return True

    @classmethod
    def _conflict(cls, path: str, contents: list[bytes | None]) -> FileConflict:
        binary = any(cls._binary(content) for content in contents)
        return cast(
            FileConflict,
            {
                "path": path,
                "binary": binary,
                **{
                    name: content.decode("utf-8") if content is not None and not binary else None
                    for name, content in zip(("base", "ours", "theirs"), contents, strict=True)
                },
            },
        )

    @staticmethod
    def _merge_text(base: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        with TemporaryDirectory(prefix="harness-text-") as temporary:
            paths = [Path(temporary) / name for name in ("ours", "base", "theirs")]
            for path, content in zip(paths, (ours, base, theirs), strict=True):
                path.write_bytes(content)
            result = subprocess.run(
                ["git", "merge-file", "--stdout", *(str(path) for path in paths)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        if not 0 <= result.returncode <= 127:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout, result.returncode != 0
