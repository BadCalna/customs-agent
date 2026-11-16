"""受控文件存储：按版本隔离、内容寻址、原子写入、不可变（P05、D2）。

设计口径：

- **原件、图片、导出副本分开目录**。``tasks/<task>/versions/<version>/original/`` 放版本原件，
  ``.../images/`` 放从工作簿提取出来的附图，``.../exports/<kind>/`` 放导出副本；
  新版本重新解析，不沿用旧版本目录。
- **内容寻址、可去重**。落盘文件名是内容 ``sha256`` + 归一化后缀，同一内容重复保存
  会命中同一路径（幂等），不同字节不会互相覆盖。
- **原子写入、不覆盖**。先写同目录临时文件并 ``fsync``，再用硬链接“独占发布”到目标名；
  目标已存在时只允许内容完全一致（幂等复用），内容不同即报错，绝不覆盖已有文件。
- **不可变**。正式文件不提供修改/删除入口；删除显式拒绝（``delete``），清理只能由运维
  在仓库外按备份策略处理，避免业务代码误删历史证据。唯一可删的是 ``incoming/`` 暂存区，
  供 P06 实现“先落暂存 → 事务提交 → 原子发布”，失败时丢弃暂存，不留孤件。
- **路径即攻击面**。所有相对路径在读写前统一解析：拒绝绝对路径、``..``、反斜杠、
  控制字符、盘符冒号与符号链接逃逸，解析后必须落在根目录内。
- **文件名只作展示元数据**。展示名经过净化（去路径分隔符/控制字符/超长/危险扩展名），
  真实落盘名不用原文，避免下载头注入与路径穿越。

本模块不依赖 Django；存储根由调用方（``apps.reviews.services.files``）按 settings 传入。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
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
#: 导出副本允许的后缀；导出业务本身由 P20 负责，本模块只做受控存取。
EXPORT_SUFFIXES = frozenset({".xlsx", ".xlsm", ".csv", ".pdf", ".zip"})
#: 暂存区目录；只允许 stage/publish/discard 触碰，不属于正式版本目录。
STAGE_DIR = "incoming"
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


@dataclass(frozen=True)
class StagedFile:
    """暂存结果；P06 在事务提交后再用 token 发布到正式版本目录。"""

    token: str
    relative_path: str
    sha256: str
    size: int
    suffix: str


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


def version_export_relative_path(
    task_id: str, version_id: str, kind: str, sha256: str, suffix: str,
) -> str:
    """导出副本路径：``tasks/<task>/versions/<version>/exports/<kind>/<sha256>.<ext>``。"""
    safe_kind = _safe_segment(kind, "导出类型")
    safe_suffix = _content_suffix(suffix, EXPORT_SUFFIXES, "导出副本")
    return (
        f"tasks/{_safe_segment(task_id, 'task_id')}"
        f"/versions/{_safe_segment(version_id, 'version_id')}"
        f"/exports/{safe_kind}/{_safe_segment(sha256, 'sha256')}{safe_suffix}"
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


def save_export(
    root: str | Path,
    *,
    task_id: str,
    version_id: str,
    kind: str,
    filename: str,
    data: bytes,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StoredFile:
    """保存一份导出副本；业务内容由 P20 生成，这里只做不可覆盖的受控落盘。"""
    display_name = sanitize_filename(filename, fallback="导出.xlsx", allowed_suffixes=EXPORT_SUFFIXES)
    suffix = _content_suffix(PurePosixPath(display_name).suffix, EXPORT_SUFFIXES, "导出副本")
    relative_path = version_export_relative_path(
        task_id, version_id, kind, sha256(data), suffix,
    )
    return _store(data, root, relative_path=relative_path, suffix=suffix,
                  display_name=display_name, max_bytes=max_bytes)


def read_export(
    root: str | Path, relative_path: str, *, max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> bytes:
    """读取导出副本；只接受 ``.../exports/<kind>/<sha256>.<ext>`` 路径。"""
    _export_path_parts(relative_path)
    return read_bytes(root, relative_path, max_bytes=max_bytes)


def export_exists(root: str | Path, relative_path: str) -> bool:
    """导出副本存在性检查；路径必须是导出目录，不能借此探测原件/图片。"""
    _export_path_parts(relative_path)
    return exists(root, relative_path)


def _export_path_parts(relative_path: str) -> tuple[str, ...]:
    pure = PurePosixPath(relative_path or "")
    parts = pure.parts
    if (len(parts) != 7 or parts[0] != "tasks" or parts[2] != "versions"
            or parts[4] != "exports"):
        raise StorageError(
            "unsafe_path", f"只能按导出副本路径读取：{relative_path!r}",
        )
    _safe_segment(parts[1], "task_id")
    _safe_segment(parts[3], "version_id")
    _safe_segment(parts[5], "导出类型")
    if PurePosixPath(parts[6]).suffix.lower() not in EXPORT_SUFFIXES:
        raise StorageError("unsafe_path", f"导出副本后缀不受支持：{parts[6]!r}")
    return parts


# --------------------------------------------------------------------- 暂存与发布
def stage_bytes(
    root: str | Path,
    data: bytes,
    *,
    suffix: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> StagedFile:
    """把上传字节写入 ``incoming/<uuid>.<ext>`` 暂存区。

    P06 推荐顺序：先暂存 → 创建 Task/FileVersion 并在事务内提交 → 用返回的 token
    调用 :func:`publish_staged_original` 原子发布；事务失败则 :func:`discard_staged`。
    暂存同样执行大小上限、扩展名白名单与路径安全校验。
    """
    payload = _validated_payload(data, max_bytes)
    safe_suffix = _content_suffix(suffix, ORIGINAL_SUFFIXES, "暂存原件")
    digest = sha256(payload)
    for _ in range(3):
        token = str(uuid.uuid4())
        relative_path = f"{STAGE_DIR}/{token}{safe_suffix}"
        created = _write_exclusive(root, relative_path, payload, safe_suffix)
        if created:
            return StagedFile(
                token=token, relative_path=relative_path, sha256=digest,
                size=len(payload), suffix=safe_suffix,
            )
    raise StorageError("storage_token_collision", "暂存 token 连续冲突，请重试")


def publish_staged_original(
    root: str | Path,
    token: str,
    *,
    task_id: str,
    version_id: str,
    filename: str,
) -> StoredFile:
    """把暂存原件原子发布到版本原件路径；成功后清理暂存，重复发布幂等。"""
    canonical = _canonical_stage_token(token)
    receipt_relative = f"{STAGE_DIR}/{canonical}.json"
    receipt = _read_receipt(root, receipt_relative)
    if receipt is not None:
        return _stored_from_receipt(
            root, receipt, task_id=task_id, version_id=version_id,
        )

    staged_relative = _staged_relative_path(root, canonical)
    payload = read_bytes(root, staged_relative, max_bytes=DEFAULT_MAX_READ_BYTES)
    suffix = PurePosixPath(staged_relative).suffix.lower()
    suffix = _content_suffix(suffix, ORIGINAL_SUFFIXES, "暂存原件")
    stored = _store(
        payload, root,
        relative_path=version_original_relative_path(
            task_id, version_id, sha256(payload), suffix,
        ),
        suffix=suffix, display_name=filename, max_bytes=DEFAULT_MAX_BYTES,
    )
    _write_receipt(root, receipt_relative, {
        "token": canonical,
        "task_id": str(task_id),
        "version_id": str(version_id),
        "relative_path": stored.relative_path,
        "sha256": stored.sha256,
        "size": stored.size,
        "suffix": stored.suffix,
        "display_name": stored.display_name,
    })
    try:
        resolve_path(root, staged_relative).unlink()
    except OSError as exc:
        raise StorageError("storage_unwritable", f"清理暂存文件失败：{exc}") from exc
    return stored


def discard_staged(root: str | Path, token: str) -> None:
    """丢弃暂存文件（只允许 ``incoming/`` 内的合法 UUID），不触碰任何正式文件。"""
    canonical = _canonical_stage_token(token)
    removed = False
    try:
        staged = _staged_relative_path(root, canonical)
    except StorageError as exc:
        if exc.code != "stage_not_found":
            raise
        staged = ""
    if staged:
        try:
            resolve_path(root, staged).unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError("storage_unwritable", f"清理暂存文件失败：{exc}") from exc

    receipt_relative = f"{STAGE_DIR}/{canonical}.json"
    try:
        receipt = resolve_path(root, receipt_relative)
        if receipt.is_file() and not receipt.is_symlink():
            receipt.unlink()
            removed = True
    except FileNotFoundError:
        pass
    if not removed:
        raise StorageError("stage_not_found", "暂存记录不存在或已发布并清理")


def _canonical_stage_token(token: str) -> str:
    try:
        return str(uuid.UUID(str(token)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StorageError("invalid_stage_token", f"暂存 token 不是合法 UUID：{token!r}") from exc


def _staged_relative_path(root: str | Path, canonical: str) -> str:
    incoming = resolve_path(root, STAGE_DIR)
    if not incoming.is_dir():
        raise StorageError("stage_not_found", "暂存区还没有内容")
    candidates = [
        path for path in sorted(incoming.iterdir())
        if path.is_file() and not path.is_symlink()
        and path.name.startswith(f"{canonical}.") and not path.name.endswith(".json")
    ]
    if not candidates:
        raise StorageError("stage_not_found", "暂存文件不存在或已发布并清理")
    if len(candidates) > 1:
        raise StorageError("stage_ambiguous", "同一 token 有多个暂存文件，需人工核对")
    return f"{STAGE_DIR}/{candidates[0].name}"


def _read_receipt(root: str | Path, relative_path: str) -> dict | None:
    try:
        payload = read_bytes(root, relative_path)
    except StorageError as exc:
        if exc.code == "file_not_found":
            return None
        raise
    try:
        receipt = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError("stage_receipt_invalid", f"暂存发布回执无法解析：{exc}") from exc
    if not isinstance(receipt, dict):
        raise StorageError("stage_receipt_invalid", "暂存发布回执格式不正确")
    return receipt


def _stored_from_receipt(root, receipt: dict, *, task_id: str, version_id: str) -> StoredFile:
    for key in ("relative_path", "sha256", "suffix", "size", "display_name"):
        if key not in receipt:
            raise StorageError("stage_receipt_invalid", f"暂存发布回执缺少字段：{key}")
    if str(receipt.get("task_id")) != str(task_id) or str(receipt.get("version_id")) != str(version_id):
        raise StorageError(
            "stage_publish_conflict",
            "该暂存 token 已发布到其它任务/版本，不能跨版本重放",
        )
    expected = version_original_relative_path(
        task_id, version_id, str(receipt["sha256"]), str(receipt["suffix"]),
    )
    if receipt["relative_path"] != expected:
        raise StorageError("stage_receipt_invalid", "暂存发布回执路径与内容寻址不一致")
    if not exists(root, expected) or file_sha256(root, expected) != str(receipt["sha256"]):
        raise StorageError("file_not_found", "已发布的正式原件丢失或内容不一致，需运维核查")
    return StoredFile(
        relative_path=expected, sha256=str(receipt["sha256"]), size=int(receipt["size"]),
        suffix=str(receipt["suffix"]), display_name=str(receipt["display_name"]),
        created=False,
    )


def _write_receipt(root: str | Path, relative_path: str, payload: dict) -> None:
    target = resolve_path(root, relative_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError("storage_unwritable", f"创建暂存目录失败：{exc}") from exc
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=".tmp-", suffix=".json", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _validated_payload(data: bytes, max_bytes: int) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise StorageError("invalid_content", "保存内容必须是字节")
    payload = bytes(data)
    if not payload:
        raise StorageError("empty_file", "空文件不能入库")
    if len(payload) > max_bytes:
        raise StorageError(
            "file_too_large", f"文件体积 {len(payload)} 字节超过上限 {max_bytes} 字节",
        )
    return payload


def _store(
    data: bytes,
    root: str | Path,
    *,
    relative_path: str,
    suffix: str,
    display_name: str,
    max_bytes: int,
) -> StoredFile:
    payload = _validated_payload(data, max_bytes)
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

    created = _write_exclusive(root, relative_path, payload, suffix)

    if not created:
        # 并发保存同一内容：另一个进程已发布，校验后按幂等复用处理。
        if file_sha256(root, relative_path) != digest:
            raise StorageError(
                "storage_content_conflict",
                f"目标路径已有不同内容的文件，拒绝覆盖：{relative_path}",
            )
    return StoredFile(relative_path=relative_path, sha256=digest, size=len(payload),
                      suffix=suffix, display_name=display_name, created=created)


def _write_exclusive(
    root: str | Path, relative_path: str, payload: bytes, suffix: str,
) -> bool:
    """把字节写进受控根内的目标名：同目录临时文件 + 原子独占发布，不覆盖已有文件。"""
    target = resolve_path(root, relative_path)
    if target.is_file():
        return False
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
        return _publish_exclusive(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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
