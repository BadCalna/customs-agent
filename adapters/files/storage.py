"""受控文件存储：按版本隔离、内容寻址、原子写入、不可变（P05、D2）。

设计口径：

- **原件与图片分开目录**。``tasks/<task>/versions/<version>/original/`` 放版本原件，
  ``.../images/`` 放从工作簿提取出来的附图；新版本重新解析，不沿用旧版本目录。
- **内容寻址、可去重**。落盘文件名是内容 ``sha256`` + 归一化后缀，同一内容重复保存
  会命中同一路径（幂等），不同字节不会互相覆盖。
- **原子写入、不覆盖**。先写同目录临时文件并 ``fsync``，再用硬链接“独占发布”到目标名；
  目标已存在时只允许内容完全一致（幂等复用），内容不同即报错，绝不覆盖已有文件。
- **不可变**。本模块不提供修改/删除入口；删除显式拒绝（``delete``），清理只能由运维
  在仓库外按备份策略处理，避免业务代码误删历史证据。
- **路径即攻击面**。所有相对路径在读写前统一解析：拒绝绝对路径、``..``、反斜杠、
  控制字符、盘符冒号与符号链接逃逸，解析后必须落在根目录内。
- **文件名只作展示元数据**。展示名经过净化（去路径分隔符/控制字符/超长/危险扩展名），
  真实落盘名不用原文，避免下载头注入与路径穿越。

本模块不依赖 Django；存储根由调用方（``apps.reviews.services.files``）按 settings 传入。
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: 单次保存的默认上限；调用方可用 ``max_bytes`` 或 settings 覆盖。
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
#: 单张图片的默认上限；与 ``adapters.files.dispimg.DEFAULT_MAX_IMAGE_BYTES`` 保持一致。
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: 按相对路径读取时的默认上限，防止误读超大文件占满内存。
DEFAULT_MAX_READ_BYTES = 200 * 1024 * 1024

#: 版本原件只接受 Excel 工作簿（含启用宏的工作簿），其它类型由上传/解析任务拒绝。
ORIGINAL_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xltx"})
#: 附图允许的媒体后缀（与 OOXML 常见媒体类型一致）。
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf",
})
#: 危险扩展名：既不允许作为原件的展示名，也不会参与落盘命名。
DANGEROUS_SUFFIXES = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".msi", ".dll", ".so", ".dylib",
    ".sh", ".bash", ".zsh", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".jar", ".hta",
    ".py", ".rb", ".pl", ".php", ".html", ".htm", ".xhtml", ".svg", ".lnk", ".url", ".reg",
})
#: Windows 保留设备名，作为文件名会引发歧义。
_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
})
_SUFFIX_ALIASES = {".jpeg": ".jpg", ".tif": ".tiff"}
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MAX_RELATIVE_PATH = 400
_MAX_DISPLAY_NAME = 120


class StorageError(Exception):
    """受控存储的显式错误；``code`` 供服务层映射成 API 错误。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class StoredFile:
    """一次保存的结果；``relative_path`` 可落库，绝对路径不对外暴露。"""

    relative_path: str
    sha256: str
    size: int
    suffix: str
    display_name: str
    created: bool


# --------------------------------------------------------------------- 命名与净化
def sanitize_filename(
    filename: str,
    *,
    fallback: str = "file",
    allowed_suffixes: frozenset[str] | set[str] | None = None,
    max_length: int = _MAX_DISPLAY_NAME,
) -> str:
    """净化展示用文件名：去目录、控制字符、超长与危险扩展名。

    - 同时按 ``/`` 与 ``\\`` 取基名，杜绝“看起来像子路径”的展示名；
    - NFKC 归一化后再剔除控制字符（含 NUL），避免全角分隔符/不可见字符绕过；
    - 危险扩展名直接拒绝（``unsafe_filename``），不给“图标伪装成文档”的机会；
    - 给出 ``allowed_suffixes`` 时校验后缀，返回保留原后缀的净化名。
    """
    if not isinstance(filename, str):
        raise StorageError("unsafe_filename", f"文件名必须是字符串：{filename!r}")
    text = unicodedata.normalize("NFKC", filename)
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    if not text or text in (".", ".."):
        text = fallback
    suffix = PurePosixPath(text).suffix.lower()
    if suffix in DANGEROUS_SUFFIXES:
        raise StorageError("unsafe_filename", f"文件名带有危险扩展名，已拒绝：{filename!r}")
    if allowed_suffixes is not None and suffix not in {item.lower() for item in allowed_suffixes}:
        raise StorageError(
            "unsupported_file_type",
            f"不支持的文件类型：{suffix or '（无扩展名）'}；允许：{'、'.join(sorted(allowed_suffixes))}",
        )
    if len(text) > max_length:
        stem = text[: max_length - len(suffix)].rstrip() if suffix else text[:max_length]
        text = f"{stem}{suffix}"
    if PurePosixPath(text).stem.casefold() in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def _safe_segment(value: str, label: str) -> str:
    """校验 task/version 等标识只含安全字符；UUID 直接可用。"""
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise StorageError("unsafe_path", f"{label} 不是安全的标识：{value!r}")
    return value


def _content_suffix(suffix: str, allowed: frozenset[str], label: str) -> str:
    normalized = (suffix or "").lower()
    normalized = _SUFFIX_ALIASES.get(normalized, normalized)
    if normalized not in allowed:
        raise StorageError("unsupported_file_type", f"{label} 类型不受支持：{suffix!r}")
    return normalized


def _sniff_image_suffix(data: bytes) -> str:
    """按文件头识别图片类型；用于媒体部件名缺失/扩展名不可信时。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise StorageError("unsupported_image_type", "无法识别图片类型，且媒体部件名没有可用后缀")


# --------------------------------------------------------------------- 路径
def version_original_relative_path(task_id: str, version_id: str, sha256: str, suffix: str) -> str:
    return (
        f"tasks/{_safe_segment(task_id, 'task_id')}"
        f"/versions/{_safe_segment(version_id, 'version_id')}"
        f"/original/{_safe_segment(sha256, 'sha256')}{suffix}"
    )


def version_image_relative_path(task_id: str, version_id: str, sha256: str, suffix: str) -> str:
    return (
        f"tasks/{_safe_segment(task_id, 'task_id')}"
        f"/versions/{_safe_segment(version_id, 'version_id')}"
        f"/images/{_safe_segment(sha256, 'sha256')}{suffix}"
    )


def resolve_path(root: str | Path, relative_path: str, *, label: str = "存储路径") -> Path:
    """把相对路径解析为根目录内的绝对路径；任何越界/符号链接逃逸都拒绝。"""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise StorageError("unsafe_path", f"{label}为空")
    if len(relative_path) > _MAX_RELATIVE_PATH:
        raise StorageError("unsafe_path", f"{label}过长：{len(relative_path)} 字符")
    if "\\" in relative_path:
        raise StorageError("unsafe_path", f"{label}不允许反斜杠：{relative_path!r}")
    if any(unicodedata.category(char).startswith("C") for char in relative_path):
        raise StorageError("unsafe_path", f"{label}含控制字符：{relative_path!r}")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or relative_path.startswith("/"):
        raise StorageError("unsafe_path", f"{label}不允许绝对路径：{relative_path!r}")
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise StorageError("unsafe_path", f"{label}含上跳或空路径段：{relative_path!r}")
    if any(":" in part for part in parts):
        raise StorageError("unsafe_path", f"{label}含盘符或冒号：{relative_path!r}")

    root_resolved = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_resolved, *parts))
    if candidate == root_resolved or not candidate.startswith(f"{root_resolved}{os.sep}"):
        raise StorageError("path_outside_root", f"{label}越出受控存储根目录：{relative_path!r}")
    return Path(candidate)


def exists(root: str | Path, relative_path: str) -> bool:
    """相对路径是否存在（只认真实文件，不跟随到根目录外的符号链接）。"""
    return resolve_path(root, relative_path).is_file()


def file_size(root: str | Path, relative_path: str) -> int:
    return resolve_path(root, relative_path).stat().st_size


def read_bytes(
    root: str | Path, relative_path: str, *, max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> bytes:
    """按相对路径读取字节；不返回绝对路径给调用方。"""
    path = resolve_path(root, relative_path)
    if not path.is_file():
        raise StorageError("file_not_found", f"受控存储中找不到文件：{relative_path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise StorageError("file_too_large", f"文件体积 {size} 字节超过读取上限 {max_bytes} 字节")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StorageError("storage_unreadable", f"读取受控文件失败：{exc}") from exc


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(root: str | Path, relative_path: str) -> str:
    path = resolve_path(root, relative_path)
    if not path.is_file():
        raise StorageError("file_not_found", f"受控存储中找不到文件：{relative_path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StorageError("storage_unreadable", f"读取受控文件失败：{exc}") from exc
    return digest.hexdigest()


# --------------------------------------------------------------------- 保存
def save_version_original(
    root: str | Path,
    *,
    task_id: str,
    version_id: str,
    filename: str,
    data: bytes,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StoredFile:
    """保存某个文件版本的原件；同一内容重复保存是幂等复用，不覆盖已有文件。"""
    display_name = sanitize_filename(filename, fallback="原件.xlsx", allowed_suffixes=ORIGINAL_SUFFIXES)
    suffix = _content_suffix(PurePosixPath(display_name).suffix, ORIGINAL_SUFFIXES, "版本原件")
    relative_path = version_original_relative_path(
        task_id, version_id, sha256(data), suffix,
    )
    return _store(data, root, relative_path=relative_path, suffix=suffix,
                  display_name=display_name, max_bytes=max_bytes)


def save_image(
    root: str | Path,
    *,
    task_id: str,
    version_id: str,
    media_part: str,
    data: bytes,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> StoredFile:
    """保存一张附图；按 sha256 + 归一化后缀落盘，同内容在不同行/版本内自动去重。"""
    media_name = (media_part or "").replace("\\", "/").split("/")[-1]
    suffix = PurePosixPath(media_name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = _sniff_image_suffix(data)
    else:
        suffix = _content_suffix(suffix, IMAGE_SUFFIXES, "附图")
    display_name = sanitize_filename(media_name, fallback=f"image{suffix}", max_length=80)
    relative_path = version_image_relative_path(task_id, version_id, sha256(data), suffix)
    return _store(data, root, relative_path=relative_path, suffix=suffix,
                  display_name=display_name, max_bytes=max_bytes)


def _store(
    data: bytes,
    root: str | Path,
    *,
    relative_path: str,
    suffix: str,
    display_name: str,
    max_bytes: int,
) -> StoredFile:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise StorageError("invalid_content", "保存内容必须是字节")
    payload = bytes(data)
    if not payload:
        raise StorageError("empty_file", "空文件不能入库")
    if len(payload) > max_bytes:
        raise StorageError(
            "file_too_large", f"文件体积 {len(payload)} 字节超过上限 {max_bytes} 字节",
        )
    digest = sha256(payload)
    expected = relative_path.rsplit("/", 1)[-1]
    if not expected.startswith(digest):
        # 防御内部拼接错误：路径必须与内容指纹一致，否则幂等判断失效。
        raise StorageError("storage_path_mismatch", "存储路径与内容指纹不一致")

    target = resolve_path(root, relative_path)
    existed = target.is_file()
    if existed:
        if file_sha256(root, relative_path) != digest:
            raise StorageError(
                "storage_content_conflict",
                f"目标路径已有不同内容的文件，拒绝覆盖：{relative_path}",
            )
        return StoredFile(relative_path=relative_path, sha256=digest, size=len(payload),
                          suffix=suffix, display_name=display_name, created=False)

    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError("storage_unwritable", f"创建存储目录失败：{exc}") from exc
    # mkdir 之后重新解析一次，缩小“解析后被换成符号链接”的窗口。
    resolved_parent = os.path.realpath(parent)
    root_resolved = os.path.realpath(root)
    if not resolved_parent.startswith(f"{root_resolved}{os.sep}"):
        raise StorageError("path_outside_root", f"存储目录越出受控根目录：{relative_path}")

    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=parent, prefix=".tmp-", suffix=suffix, delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        created = _publish_exclusive(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    if not created:
        # 并发保存同一内容：另一个进程已发布，校验后按幂等复用处理。
        if file_sha256(root, relative_path) != digest:
            raise StorageError(
                "storage_content_conflict",
                f"目标路径已有不同内容的文件，拒绝覆盖：{relative_path}",
            )
    return StoredFile(relative_path=relative_path, sha256=digest, size=len(payload),
                      suffix=suffix, display_name=display_name, created=created)


def _publish_exclusive(temp_path: Path, target: Path) -> bool:
    """把临时文件原子地发布到目标名；目标已存在则返回 False（不覆盖）。

    优先用 ``os.link``：硬链接是原子的“仅当不存在才成功”的发布方式。少数文件系统
    不支持硬链接时退回 ``O_EXCL`` 独占创建 + 复制，仍然不覆盖已有文件。
    """
    try:
        os.link(temp_path, target)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP,
                             errno.EXDEV, errno.EMLINK, errno.ENOSYS):
            raise StorageError("storage_unwritable", f"发布受控文件失败：{exc}") from exc
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as destination, temp_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            try:
                target.unlink()
            finally:
                raise
        return True


# --------------------------------------------------------------------- 不可变
def delete(*_args, **_kwargs):
    """受控存储不可删除；保留函数是为了给误用一处明确的拒绝，而不是静默忽略。"""
    raise StorageError(
        "immutable_storage",
        "受控存储按版本不可变，业务代码不得删除原件或图片；清理只能走仓库外的备份策略",
    )
