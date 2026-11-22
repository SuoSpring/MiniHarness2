"""文件路径显式传入；运行服务必须在写入前完成权限检查。"""

import json
import os
import re
from collections.abc import Iterator
from hashlib import blake2s
from heapq import nsmallest
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO, TypedDict

from ..workspace.paths import IGNORED_DIRECTORIES, resolve_path

DEFAULT_DIRECTORY_LIMIT = 50
MAX_DIRECTORY_LIMIT = 100
DIRECTORY_PAGE_BYTES = 8192
READ_PAGE_CHARS = 16_000
READ_PAGE_LINES = 400
SEARCH_PREVIEW_CHARS = 160
_LINE_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class DirectoryPage(TypedDict):
    result: list[str]
    total: int
    next_cursor: str | None


class ReadFileResult(TypedDict):
    path: str
    content: str
    start_line: int
    start_column: int
    total_lines: int | None
    truncated: bool
    next_cursor: str | None


class FileMatch(TypedDict):
    term: str
    term_index: int
    length: int
    line: int
    column: int
    byte_offset: int
    preview: str
    read_cursor: str


class FileSearchResult(TypedDict):
    path: str
    match: FileMatch | None
    next_cursor: str | None


class WriteFileResult(TypedDict):
    path: str
    bytes: int
    changed: bool


class DeleteFileResult(TypedDict):
    path: str
    deleted: bool


class EditRejected(ValueError):
    """写入之前的参数检查失败，目标内容仍与调用前一致。"""


def _directory_path(workdir: Path, path: str) -> Path:
    # 列目录允许省略目标；模型给出的空字符串也指当前工作副本。
    return resolve_path(workdir, "." if path == "" else path)


def list_files(
    workdir: Path, path: str = ".", cursor: str = "", limit: int = DEFAULT_DIRECTORY_LIMIT
) -> DirectoryPage:
    """按名称分页列出直接子项。目录名带 /，游标是上一页最后一个名字。"""
    if not isinstance(cursor, str):
        raise ValueError("cursor 必须是上页返回的字符串，第一页请省略")
    if type(limit) is not int or not 1 <= limit <= MAX_DIRECTORY_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_DIRECTORY_LIMIT} 之间的整数")
    root = _directory_path(workdir, path)
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{path}。请先用 list_files('.') 查看当前工作副本。")
    total = 0

    def names_after_cursor() -> Iterator[str]:
        nonlocal total
        with os.scandir(root) as children:
            for child in children:
                name = child.name
                if name == ".git" or child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    directory = Path(child.path)
                    if name in IGNORED_DIRECTORIES or directory.resolve() != directory:
                        continue
                    name += "/"
                elif not child.is_file(follow_symlinks=False):
                    continue
                total += 1
                if name > cursor:
                    yield name

    # 只保留下一页候选。即使同层有大量文件，也无需在内存里排序整个目录。
    candidates = nsmallest(limit + 1, names_after_cursor())
    page = []
    page_bytes = 2  # JSON 数组的方括号
    for name in candidates[:limit]:
        size = len(json.dumps(name, ensure_ascii=False).encode("utf-8")) + (2 if page else 0)
        if page and page_bytes + size > DIRECTORY_PAGE_BYTES:
            break
        page.append(name)
        page_bytes += size
    return {
        "result": page,
        "total": total,
        "next_cursor": page[-1] if len(page) < len(candidates) else None,
    }


def _line_fragments(stream: TextIO) -> Iterator[str]:
    """保留原始行尾以计算字节位置，超长行也只占用一个固定大小的块。"""
    carry = ""
    while block := stream.read(8192):
        block = carry + block
        carry = "\r" if block.endswith("\r") else ""
        if carry:
            block = block[:-1]  # 下一块的首字符可能是 CRLF 中的 LF。
        yield from block.splitlines(keepends=True)
    if carry:
        yield carry


def _file_version(target: Path, info: os.stat_result) -> bytes:
    stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return os.fsencode(os.path.normcase(target)) + repr(stamp).encode("ascii")


def read_source(workdir: Path, name: str, arguments: dict) -> dict:
    """用实际工具的路径解析和文件版本识别读请求，只查询元数据。"""
    if name not in {"read_file", "search_file", "list_files"}:
        return {}
    path = arguments.get("path", "." if name == "list_files" else None)
    try:
        target = (
            _directory_path(workdir, path) if name == "list_files" else resolve_path(workdir, path)
        )
        root = workdir.resolve()
        display = target.relative_to(root) if target.is_relative_to(root) else target
        version = _file_version(target, target.stat())
        return {"path": display.as_posix(), "stamp": blake2s(version, digest_size=16).hexdigest()}
    except (OSError, ValueError, RuntimeError):
        return {}


def _file_cursor(version: bytes, offset: int, line: int, column: int, stop: int) -> str:
    position = f"{offset}:{line}:{column}:{stop}"
    digest = blake2s(version + position.encode("ascii"), digest_size=12).hexdigest()
    return f"{position}:{digest}"


def _resume_file(
    cursor: str, version: bytes, error: str = "read_file 游标无效或文件已变化，请按行重新读取"
) -> tuple[int, int, int, int]:
    if isinstance(cursor, str) and len(cursor) <= 160:
        fields = cursor.split(":")
        if len(fields) == 5 and all(part.isascii() and part.isdecimal() for part in fields[:4]):
            offset, line, column, stop = map(int, fields[:4])
            if line >= 1 and cursor == _file_cursor(version, offset, line, column, stop):
                return offset, line, column, stop
    raise ValueError(error)


def _text_file_path(workdir: Path, path: str) -> Path:
    target = resolve_path(workdir, path)
    if not target.exists():
        raise FileNotFoundError(
            f"文件不存在：{path}。请用 list_files 核对实际文件；计划中提到的文件不代表已经创建。"
            "若为新项目，请在执行阶段用 write_file 创建，勿重复读取同一路径。"
        )
    if target.is_dir():
        raise IsADirectoryError(f"{path} 是目录，请用 list_files 列出其中的文件。")
    return target


def read_file(
    workdir: Path,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    cursor: str | None = None,
) -> ReadFileResult:
    """按行开始，按文件版本及字节位置续读。只有实际读到末尾时总行数才已知。"""
    if cursor is not None and (start_line is not None or end_line is not None):
        raise ValueError("续读时只需传入原 path 和 cursor，游标已包含行范围")
    first = 1 if start_line is None else start_line
    if (
        type(first) is not int
        or first < 1
        or (
            end_line is not None
            and (type(end_line) is not int or not first <= end_line < first + READ_PAGE_LINES)
        )
    ):
        raise ValueError(f"行号从 1 开始，每次最多读取 {READ_PAGE_LINES} 行")
    target = _text_file_path(workdir, path)
    with target.open("rb") as raw:
        info = os.fstat(raw.fileno())
        version = _file_version(target, info)
        offset, line, column, stop = 0, 1, 0, end_line or 0
        if cursor is not None:
            offset, line, column, stop = _resume_file(cursor, version)
            raw.seek(offset)
            first = line
        first_column = column
        last = stop or first + READ_PAGE_LINES - 1
        parts = []
        remaining = READ_PAGE_CHARS
        more = False
        with TextIOWrapper(raw, encoding="utf-8", newline="") as stream:
            fragments = _line_fragments(stream)
            for fragment in fragments:
                if line >= first:
                    text = fragment.replace("\r\n", "\n").replace("\r", "\n")
                    if len(text) > remaining:
                        # 切点在行尾之前，原始字符前缀与换行归一化后的前缀相同。
                        fragment = text = text[:remaining]
                        more = True
                    parts.append(text)
                    remaining -= len(text)
                offset += len(fragment.encode("utf-8"))
                if fragment[-1] in _LINE_ENDINGS:
                    line += 1
                    column = 0
                else:
                    column += len(fragment)
                if not remaining or line > last:
                    more = more or next(fragments, None) is not None
                    break
            if _file_version(target, os.fstat(raw.fileno())) != version:
                raise ValueError("文件在读取期间已变化，请按行重新读取")
    return {
        "path": path,
        "content": "".join(parts),
        "start_line": first,
        "start_column": first_column,
        "total_lines": None if more else (line if column else line - 1),
        "truncated": more,
        "next_cursor": _file_cursor(version, offset, line, column, stop)
        if more and (not stop or line <= stop)
        else None,
    }


def _search_terms(query: str) -> list[str]:
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    terms = []
    current: list[str] = []
    # 仅对分隔符和反斜杠转义，空格和正则元字符也都按字面查找。
    for part in re.split(r"(\\[\\|]|\|)", query.replace("\r\n", "\n").replace("\r", "\n")):
        if part == "|":
            terms.append("".join(current))
            current.clear()
        else:
            current.append(part[1:] if part in {"\\\\", "\\|"} else part)
    terms.append("".join(current))
    if not all(terms):
        raise ValueError(r"搜索词不能为空。用 | 分隔多个词，查找竖线本身用 \|。")
    return terms


def _position_after(text: str, offset: int, line: int, column: int) -> tuple[int, int, int]:
    """同步推进原文的字节位置和字符行列。调用方保证不切开 CRLF。"""
    lines = text.splitlines(keepends=True)
    offset += len(text.encode("utf-8"))
    if not lines:
        return offset, line, column
    if lines[-1][-1] in _LINE_ENDINGS:
        return offset, line + len(lines), 0
    return offset, line + len(lines) - 1, len(lines[-1]) + (column if len(lines) == 1 else 0)


def _search_matches(
    stream: TextIO, terms: list[str], version: bytes, offset: int, line: int, column: int
) -> Iterator[FileMatch]:
    overlap = max(2 * SEARCH_PREVIEW_CHARS, max(len(t) + t.count("\n") for t in terms))
    tail = ""
    while True:
        block = stream.read(max(32_768, overlap))
        window = tail + block
        # 页尾尚不足以比较所有候选词。在下一块中处理这些起点，保留同位置的查询顺序。
        cut = max(0, len(window) - overlap) if block else len(window)
        if cut and window[cut - 1 : cut + 1] == "\r\n":
            cut -= 1
        text = window.replace("\r\n", "\n").replace("\r", "\n")
        searchable = cut - window.count("\r\n", 0, cut)
        start = 0
        while start < searchable:
            at = searchable
            term_index = None
            for index, term in enumerate(terms):
                candidate = text.find(term, start, at + len(term) - 1)
                if 0 <= candidate < at:
                    at, term_index = candidate, index
                    if at == start:
                        break
            if term_index is None:
                break
            # 每个已归一化的 CRLF 在磁盘上多占一个字符，只在实际命中时换算位置。
            raw_at = at
            for crlf in re.finditer("\r\n", window):
                if crlf.start() >= raw_at:
                    break
                raw_at += 1
            byte_offset, hit_line, hit_column = _position_after(
                window[:raw_at], offset, line, column
            )
            term = terms[term_index]
            yield {
                "term": term[:SEARCH_PREVIEW_CHARS],
                "term_index": term_index,
                "length": len(term),
                "line": hit_line,
                "column": hit_column,
                "byte_offset": byte_offset,
                "preview": text[at : at + SEARCH_PREVIEW_CHARS],
                "read_cursor": _file_cursor(version, byte_offset, hit_line, hit_column, 0),
            }
            start = at + 1  # 重叠命中也按不同起点返回。
        if not block:
            return
        offset, line, column = _position_after(window[:cut], offset, line, column)
        tail = window[cut:]


def search_file(
    workdir: Path, path: str, query: str, cursor: str | None = None
) -> FileSearchResult:
    """返回最靠前的一处字面命中。续查游标直接指向下一处真实命中。"""
    terms = _search_terms(query)
    target = _text_file_path(workdir, path)
    with target.open("rb") as raw:
        version = _file_version(target, os.fstat(raw.fileno()))
        query_digest = blake2s(query.encode("utf-8"), digest_size=16).digest()
        search_version = version + b"\0search_file\0" + query_digest
        offset, line, column = 0, 1, 0
        if cursor is not None:
            offset, line, column, _ = _resume_file(
                cursor, search_version, "search_file 游标无效，或文件、查询已变化，请从头搜索"
            )
            raw.seek(offset)
        with TextIOWrapper(raw, encoding="utf-8", newline="") as stream:
            matches = _search_matches(stream, terms, version, offset, line, column)
            first = next(matches, None)
            following = next(matches, None) if first is not None else None
            if _file_version(target, os.fstat(raw.fileno())) != version:
                raise ValueError("文件在搜索期间已变化，请从头搜索")
    return {
        "path": path,
        "match": first,
        "next_cursor": _file_cursor(
            search_version, following["byte_offset"], following["line"], following["column"], 0
        )
        if following is not None
        else None,
    }


def write_file(workdir: Path, path: str, content: str, *, append: bool = False) -> WriteFileResult:
    """写入 UTF-8 文本，返回 {path, bytes}；文件版本由调用提交统一记录。"""
    if not isinstance(append, bool):
        raise EditRejected("append 必须为布尔值")
    target = resolve_path(workdir, path)
    data = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if append:
        changed = bool(data) or not target.exists()
        with target.open("ab") as destination:
            destination.write(data)
        return {"path": path, "bytes": len(data), "changed": changed}
    try:
        same = target.stat().st_size == len(data) and target.read_bytes() == data
    except (FileNotFoundError, PermissionError):
        same = False
    target.write_bytes(data)
    return {"path": path, "bytes": len(data), "changed": not same}


def replace_file(workdir: Path, path: str, old: str, new: str) -> WriteFileResult:
    """精确替换唯一匹配，返回 {path, bytes}；多处匹配需先读取并缩小片段。"""
    content = resolve_path(workdir, path).read_text(encoding="utf-8")
    if not old or content.count(old) != 1:
        raise EditRejected("old 必须非空且恰好匹配一次，请重新读取文件")
    return write_file(workdir, path, content.replace(old, new, 1))


def delete_file(workdir: Path, path: str) -> DeleteFileResult:
    """删除单个现有文件，返回 {path, deleted: True}。"""
    resolve_path(workdir, path).unlink()
    return {"path": path, "deleted": True}
