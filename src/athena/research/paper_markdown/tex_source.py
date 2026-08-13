"""读取上游提供的 TeX 源码包并安全展开 ``input/include``。"""

import gzip
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from athena.research.paper_markdown.schemas import ProcessingDiagnostic, TexSourceFormat

MAX_SOURCE_FILES = 5000
MAX_SOURCE_BYTES = 256 * 1024 * 1024
INCLUDE_PATTERN = re.compile(
    r"\\(?:input|include|subfile)(?![A-Za-z@])\s*(?:\{([^}]+)\}|([^\s%]+))"
)


class TexSourceError(ValueError):
    """TeX 源码包不安全、不完整或没有可解析入口。"""


@dataclass(frozen=True, slots=True)
class SourceLine:
    """展开后某一行对应的原始文件与行号。"""

    file: str
    line: int


@dataclass(slots=True)
class ExpandedTex:
    """TeX 包、入口文件、递归展开文本及逐行溯源。"""

    files: dict[str, bytes]
    entrypoint: str
    text: str
    line_map: list[SourceLine]
    diagnostics: list[ProcessingDiagnostic]


def normalize_member_path(name: str) -> str:
    """规范归档成员路径并拒绝绝对路径和目录穿越。"""
    if "\x00" in name:
        raise TexSourceError("Source package member contains a NUL byte.")
    normalized = PurePosixPath(name.replace("\\", "/"))
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or (normalized.parts and normalized.parts[0].endswith(":"))
    ):
        raise TexSourceError(f"Unsafe source package member: {name}")
    cleaned = str(normalized)
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned == ".":
        raise TexSourceError(f"Invalid source package member: {name}")
    return cleaned


def checked_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """执行文件数、总大小和路径安全限制。"""
    if not files:
        raise TexSourceError("TeX source package is empty.")
    if len(files) > MAX_SOURCE_FILES:
        raise TexSourceError(f"TeX source package exceeds {MAX_SOURCE_FILES} files.")
    if sum(len(value) for value in files.values()) > MAX_SOURCE_BYTES:
        raise TexSourceError("TeX source package exceeds the 256 MiB safety limit.")
    return {normalize_member_path(name): value for name, value in files.items()}


def read_tar(payload: bytes) -> dict[str, bytes]:
    """从 tar 系列包读取普通文件；符号链接和设备成员不会进入解析器。"""
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = archive.getmembers()
            if (
                len([member for member in members if not member.isdir()])
                > MAX_SOURCE_FILES
            ):
                raise TexSourceError(
                    f"TeX source package exceeds {MAX_SOURCE_FILES} files."
                )
            if (
                sum(member.size for member in members if member.isfile())
                > MAX_SOURCE_BYTES
            ):
                raise TexSourceError(
                    "TeX source package exceeds the 256 MiB safety limit."
                )
            for member in members:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise TexSourceError(f"Unsupported tar member type: {member.name}")
                handle = archive.extractfile(member)
                if handle is None:
                    raise TexSourceError(f"Cannot read tar member: {member.name}")
                normalized = normalize_member_path(member.name)
                if normalized in files:
                    raise TexSourceError(
                        f"Duplicate normalized tar member: {member.name}"
                    )
                files[normalized] = handle.read()
    except tarfile.ReadError as error:
        # tar 目录或压缩流损坏 → 拒绝整个 TeX 源码包
        raise TexSourceError("Invalid tar TeX source package.") from error
    return checked_files(files)


def read_zip(payload: bytes) -> dict[str, bytes]:
    """从 zip 包读取普通文件，不向文件系统解压。"""
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) > MAX_SOURCE_FILES:
                raise TexSourceError(
                    f"TeX source package exceeds {MAX_SOURCE_FILES} files."
                )
            if sum(member.file_size for member in members) > MAX_SOURCE_BYTES:
                raise TexSourceError(
                    "TeX source package exceeds the 256 MiB safety limit."
                )
            for member in members:
                if member.file_size > MAX_SOURCE_BYTES:
                    raise TexSourceError(f"Oversized zip member: {member.filename}")
                normalized = normalize_member_path(member.filename)
                if normalized in files:
                    raise TexSourceError(
                        f"Duplicate normalized zip member: {member.filename}"
                    )
                files[normalized] = archive.read(member)
    except zipfile.BadZipFile as error:
        # zip 目录或压缩流损坏 → 拒绝整个 TeX 源码包
        raise TexSourceError("Invalid zip TeX source package.") from error
    return checked_files(files)


def read_plain(payload: bytes, entrypoint: str | None) -> dict[str, bytes]:
    """把单个 TeX 文本包装成源码包。"""
    name = normalize_member_path(entrypoint or "main.tex")
    return checked_files({name: payload})


def is_tar_payload(payload: bytes) -> bool:
    """只判断字节是否为可读 tar，不吞掉后续成员安全错误。"""
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
            return True
    except tarfile.ReadError:
        # 格式自动识别发现内容不是 tar → 继续尝试其他格式
        return False


def decompress_gzip(payload: bytes) -> bytes:
    """有界解压 gzip，避免在格式识别阶段接受压缩炸弹。"""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            decompressed = archive.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        # gzip 头或压缩流无效 → 拒绝该源码输入
        raise TexSourceError("Invalid gzip TeX source.") from error
    if len(decompressed) > MAX_SOURCE_BYTES:
        raise TexSourceError("Gzip TeX source exceeds the 256 MiB safety limit.")
    return decompressed


def unpack_source(
    payload: bytes, source_format: TexSourceFormat, entrypoint: str | None
) -> dict[str, bytes]:
    """根据显式格式或魔数读取常见 arXiv 源码包，不执行网络操作。"""
    if source_format == "plain":
        return read_plain(payload, entrypoint)
    if source_format == "zip":
        return read_zip(payload)
    if source_format in {"tar", "tar.gz"}:
        return read_tar(payload)
    if source_format == "gzip":
        decompressed = decompress_gzip(payload)
        return (
            read_tar(decompressed)
            if is_tar_payload(decompressed)
            else read_plain(decompressed, entrypoint)
        )

    if zipfile.is_zipfile(io.BytesIO(payload)):
        return read_zip(payload)
    if is_tar_payload(payload):
        return read_tar(payload)
    if payload.startswith(b"\x1f\x8b"):
        decompressed = decompress_gzip(payload)
        return (
            read_tar(decompressed)
            if is_tar_payload(decompressed)
            else read_plain(decompressed, entrypoint)
        )
    return read_plain(payload, entrypoint)


def decode_tex(
    path: str, payload: bytes, diagnostics: list[ProcessingDiagnostic]
) -> str:
    """优先 UTF-8 解码，旧论文回退 Latin-1 并留下诊断。"""
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        # TeX 不是 UTF-8 → 使用可逆的 Latin-1 回退并记录诊断
        diagnostics.append(
            ProcessingDiagnostic(
                level="warning",
                code="tex_latin1_fallback",
                message=f"Decoded {path} as Latin-1 after UTF-8 failed.",
            )
        )
        return payload.decode("latin-1")


def choose_entrypoint(
    files: dict[str, bytes],
    requested: str | None,
    diagnostics: list[ProcessingDiagnostic],
) -> str:
    """选择显式入口，或从含 documentclass/document 环境的 TeX 文件中推断。"""
    if requested:
        normalized = normalize_member_path(requested)
        if normalized not in files and not normalized.endswith(".tex"):
            normalized += ".tex"
        if normalized not in files:
            raise TexSourceError(f"Requested TeX entrypoint not found: {requested}")
        return normalized

    candidates: list[tuple[int, int, str]] = []
    for path, payload in files.items():
        if not path.lower().endswith(".tex"):
            continue
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            # 入口候选不是 UTF-8 → 仅为入口识别使用 Latin-1 解码
            text = payload.decode("latin-1")
        score = (
            int("\\documentclass" in text) * 4 + int("\\begin{document}" in text) * 2
        )
        score += int(
            PurePosixPath(path).name.lower()
            in {"main.tex", "paper.tex", "manuscript.tex"}
        )
        candidates.append((-score, len(PurePosixPath(path).parts), path))
    if not candidates:
        raise TexSourceError("TeX source package contains no .tex files.")
    candidates.sort()
    entrypoint = candidates[0][2]
    if -candidates[0][0] < 2:
        diagnostics.append(
            ProcessingDiagnostic(
                level="warning",
                code="tex_entrypoint_weak_inference",
                message=f"Selected {entrypoint} without a complete document marker.",
            )
        )
    return entrypoint


def strip_comment(line: str) -> str:
    """移除第一个未转义 ``%`` 后的内容，供 include 识别使用。"""
    for index, char in enumerate(line):
        if char != "%":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return line[:index]
    return line


def resolve_include(
    current_file: str, target: str, files: dict[str, bytes]
) -> tuple[str | None, bool]:
    """按当前文件目录、包根目录和省略的 ``.tex`` 后缀解析 include。

    返回 ``(路径, 是否大小写完全匹配)``。找不到时返回 ``(None, True)``。

    精确匹配失败后会做一次不区分大小写的回退。作者多在 macOS/Windows 这类大小写不敏感
    的文件系统上编译，源码里 ``\\input{prompts/ALFWorld}`` 而磁盘上是 ``alfworld.tex``
    在 arXiv 包里很常见——ReAct 那篇就因此丢了 6789 字符的附录提示词。回退命中时返回
    ``False``，让调用方留下诊断：这类不一致值得记录，不该静默吞掉。
    """
    target = target.strip().replace("\\", "/")
    choices: list[str] = []
    for base in (PurePosixPath(current_file).parent, PurePosixPath(".")):
        candidate = base / target
        for option in (candidate, candidate.with_suffix(".tex")):
            if option is candidate or not candidate.suffix:
                normalized = str(option)
                choices.append(
                    normalized[2:] if normalized.startswith("./") else normalized
                )
    for choice in choices:
        if choice in files:
            return choice, True

    folded: dict[str, list[str]] = {}
    for name in files:
        folded.setdefault(name.lower(), []).append(name)
    for choice in choices:
        matches = folded.get(choice.lower())
        if matches:
            # 同一目录下同时存在 A.tex 与 a.tex 时按名称排序取定，保证可复现
            return sorted(matches)[0], False
    return None, True


def expand_file(
    path: str,
    files: dict[str, bytes],
    decoded: dict[str, str],
    diagnostics: list[ProcessingDiagnostic],
    stack: tuple[str, ...],
) -> tuple[list[str], list[SourceLine]]:
    """递归展开一个 TeX 文件，同时保持每行的原始来源。"""
    if path in stack:
        chain = " -> ".join((*stack, path))
        raise TexSourceError(f"Cyclic TeX include detected: {chain}")
    text = decoded[path]
    output: list[str] = []
    line_map: list[SourceLine] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        searchable = strip_comment(line)
        cursor = 0
        matches = list(INCLUDE_PATTERN.finditer(searchable))
        if not matches:
            output.append(line)
            line_map.append(SourceLine(file=path, line=line_number))
            continue
        for match in matches:
            prefix = line[cursor : match.start()]
            if prefix.strip():
                output.append(prefix)
                line_map.append(SourceLine(file=path, line=line_number))
            target = match.group(1) or match.group(2) or ""
            included, exact_case = resolve_include(path, target, files)
            if included is not None and not exact_case:
                diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="tex_include_case_mismatch",
                        message=(
                            f"Include '{target}' from {path}:{line_number} resolved to "
                            f"{included} only by ignoring case."
                        ),
                    )
                )
            if included is None:
                diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="tex_include_missing",
                        message=f"Could not resolve include '{target}' from {path}:{line_number}.",
                    )
                )
                output.append(match.group(0))
                line_map.append(SourceLine(file=path, line=line_number))
            else:
                if included not in decoded:
                    diagnostics.append(
                        ProcessingDiagnostic(
                            level="warning",
                            code="tex_include_non_text",
                            message=f"Include '{target}' resolved to unsupported text type {included}.",
                        )
                    )
                    output.append(match.group(0))
                    line_map.append(SourceLine(file=path, line=line_number))
                else:
                    child_lines, child_map = expand_file(
                        included, files, decoded, diagnostics, (*stack, path)
                    )
                    output.extend(child_lines)
                    line_map.extend(child_map)
            cursor = match.end()
        suffix = line[cursor:]
        if suffix.strip():
            output.append(suffix)
            line_map.append(SourceLine(file=path, line=line_number))
    return output, line_map


def load_tex_source(
    payload: bytes,
    source_format: TexSourceFormat = "auto",
    entrypoint: str | None = None,
) -> ExpandedTex:
    """读取并展开上游 TeX artifact；例如 tar 包会返回可定位到原文件的完整文本。"""
    diagnostics: list[ProcessingDiagnostic] = []
    files = unpack_source(payload, source_format, entrypoint)
    root = choose_entrypoint(files, entrypoint, diagnostics)
    decoded = {
        path: decode_tex(path, content, diagnostics)
        for path, content in files.items()
        if PurePosixPath(path).suffix.lower() in {".tex", ".sty", ".cls"}
    }
    lines, line_map = expand_file(root, files, decoded, diagnostics, ())
    return ExpandedTex(
        files=files,
        entrypoint=root,
        text="\n".join(lines),
        line_map=line_map,
        diagnostics=diagnostics,
    )
