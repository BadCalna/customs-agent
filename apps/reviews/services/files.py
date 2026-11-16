"""P05 受控文件与附图服务：版本原件落盘、DISPIMG 图片入库、受控读取。

职责边界（P05 交接给 P06 的编排入口）：

1. P06 在自己的事务里创建 ``Task`` / ``FileVersion`` 与商品行 ``Item``；
2. 调用 :func:`store_version_original` 把上传字节落到该版本目录并写 ``FileVersion.storage_path``；
3. 商品行建立后调用 :func:`extract_version_images_for_version`，按 ``(页签, 行号集合, 图片列)``
   解析原件并按 ``(item, source_cell)`` upsert 该版本、该批商品行的 ``ItemImage`` 记录；
4. 页面/端点需要下载时调用 :func:`read_version_original` / :func:`read_item_image`
   （或 :func:`read_item_image_path` 取相对路径），不要自行拼存储路径。

P06 负责上传端点、事务、幂等键与事件；本模块不依赖 HTTP 视图，只复用
``apps.accounts.permissions.require_action`` 的权限语义，异常交给 API 层统一转 JSON。
图片提取只提供系统入口，**不开放任何角色手动重跑**；未来若开放需同时校验版本与任务阶段。
存储根取 ``settings.CUSTOMS_FILE_ROOT``（默认 ``BASE_DIR/.local/files``，见 settings）；
测试用 ``override_settings`` 指向临时目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from adapters.files import dispimg, storage
from adapters.files.ooxml import OoxmlError, Workbook
from apps.accounts.errors import ApiError
from apps.accounts.permissions import READ, UPLOAD_VERSION, require_action

from ..models import FileVersion, Item, ItemImage

#: 下载时按后缀给出 Content-Type；不在表内的后缀一律按二进制流返回。
_MIME_BY_SUFFIX = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
}

#: 存储错误 → API 错误的映射（D3 第 1 节：404 不泄露存在性、409 冲突、422 语义校验）。
_STORAGE_ERRORS = {
    "unsafe_filename": (422, "unsafe_filename"),
    "unsupported_file_type": (422, "unsupported_file_type"),
    "unsupported_image_type": (422, "unsupported_image_type"),
    "file_too_large": (422, "file_too_large"),
    "empty_file": (422, "empty_file"),
    "invalid_content": (422, "invalid_content"),
    "storage_content_conflict": (409, "storage_conflict"),
    "immutable_storage": (409, "immutable_storage"),
    "storage_path_mismatch": (409, "storage_conflict"),
    "unsafe_path": (404, "file_content_missing"),
    "path_outside_root": (404, "file_content_missing"),
    "file_not_found": (404, "file_content_missing"),
    "storage_unreadable": (404, "file_content_missing"),
    "storage_unwritable": (500, "storage_unavailable"),
}


@dataclass(frozen=True)
class FileDownload:
    """受控下载结果：只含相对路径，不含服务器绝对路径。"""

    content: bytes
    filename: str
    content_type: str
    sha256: str
    size: int
    relative_path: str


# --------------------------------------------------------------------- 配置
def storage_root() -> Path:
    """受控存储根目录；测试通过 ``override_settings(CUSTOMS_FILE_ROOT=...)`` 覆盖。"""
    return Path(getattr(settings, "CUSTOMS_FILE_ROOT", Path(settings.BASE_DIR) / ".local" / "files"))


def max_original_bytes() -> int:
    return int(getattr(settings, "CUSTOMS_FILE_MAX_BYTES", storage.DEFAULT_MAX_BYTES))


def max_image_bytes() -> int:
    return int(getattr(settings, "CUSTOMS_IMAGE_MAX_BYTES", storage.DEFAULT_MAX_IMAGE_BYTES))


def max_read_bytes() -> int:
    return int(getattr(settings, "CUSTOMS_FILE_READ_MAX_BYTES", storage.DEFAULT_MAX_READ_BYTES))


# --------------------------------------------------------------------- 取对象
def _get_version(version_id) -> FileVersion:
    """按 ID 取版本；非法 ID 与不存在同样按 404 处理，不泄露存在性。"""
    try:
        return FileVersion.objects.select_related("task").get(pk=version_id)
    except (FileVersion.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("文件版本不存在或不在可见范围") from exc


def _get_image(image_id) -> ItemImage:
    try:
        return ItemImage.objects.select_related("item__version__task").get(pk=image_id)
    except (ItemImage.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("附图不存在或不在可见范围") from exc


def _is_latest_version(version: FileVersion) -> bool:
    return not version.task.versions.filter(number__gt=version.number).exists()


def _raise_storage_error(exc: storage.StorageError) -> NoReturn:
    status, code = _STORAGE_ERRORS.get(exc.code, (422, exc.code or "storage_error"))
    raise ApiError(status, code, str(exc) or code) from exc


def _display_filename(original: str, *, fallback: str) -> str:
    """把数据库里的原文件名净化为下载头文件名；净化失败时用兜底名。"""
    try:
        return storage.sanitize_filename(
            original or "", fallback=fallback, allowed_suffixes=storage.ORIGINAL_SUFFIXES,
        )
    except storage.StorageError:
        return fallback


def _content_type(suffix: str) -> str:
    return _MIME_BY_SUFFIX.get((suffix or "").lower(), "application/octet-stream")


# --------------------------------------------------------------------- 保存原件
def store_version_original(user, version_id, *, filename: str, data: bytes) -> FileVersion:
    """为文件版本落原件并写 ``storage_path``；版本原件不可覆盖。

    - 只允许 ``version.create`` 动作（供应商本人任务，D3 第 5 节）；
    - 只能是任务下的最新版本，旧版本返回 409 ``stale_version``；
    - 同一内容重复保存是幂等复用，直接返回原记录；不同内容返回 409，不覆盖；
    - ``filename`` 只作展示元数据，落盘名是内容 sha256。
    """
    version = _get_version(version_id)
    require_action(user, UPLOAD_VERSION, version)
    if not _is_latest_version(version):
        raise ApiError(
            409, "stale_version",
            "只能为任务的最新版本入库原件；补料需要新建版本，旧版本原件不可覆盖",
            {"version_number": version.number},
        )

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ApiError(422, "invalid_content", "上传内容必须是文件字节，不能是文本或空值")
    incoming_sha = storage.sha256(bytes(data or b""))
    root = storage_root()
    if version.storage_path:
        try:
            existing_sha = storage.file_sha256(root, version.storage_path)
        except storage.StorageError as exc:
            if exc.code in ("file_not_found", "storage_unreadable"):
                raise ApiError(
                    409, "original_already_stored",
                    "该版本已登记原件但受控存储中找不到文件；请运维核查，不能用另一次上传覆盖登记",
                    {"storage_path": version.storage_path},
                ) from exc
            _raise_storage_error(exc)
        if existing_sha == incoming_sha:
            # 幂等重放：同一份字节重复提交，不重复写盘、不改记录。
            return version
        raise ApiError(
            409, "original_already_stored",
            "该版本原件已入库且不可覆盖；补料请创建新版本",
            {"storage_path": version.storage_path},
        )

    try:
        stored = storage.save_version_original(
            root, task_id=str(version.task_id), version_id=str(version.pk),
            filename=filename, data=data, max_bytes=max_original_bytes(),
        )
    except storage.StorageError as exc:
        _raise_storage_error(exc)

    updated = FileVersion.objects.filter(pk=version.pk, storage_path="").update(
        storage_path=stored.relative_path,
    )
    if not updated:
        # 并发上传：另一个请求先写成功。内容一致算幂等，不一致报冲突，绝不覆盖。
        version.refresh_from_db()
        if version.storage_path != stored.relative_path:
            raise ApiError(
                409, "storage_conflict",
                "该版本原件已被另一个请求登记为不同内容，请刷新后重试；原件不可覆盖",
            )
    version.refresh_from_db()
    return version


# --------------------------------------------------------------------- 受控读取
def read_version_original(user, version_id) -> FileDownload:
    """读取版本原件字节；先做对象权限校验，再读受控存储。"""
    version = _get_version(version_id)
    require_action(user, READ, version)
    if not version.storage_path:
        raise ApiError(404, "file_not_stored", "该版本原件尚未入库，无法下载")
    try:
        content = storage.read_bytes(storage_root(), version.storage_path, max_bytes=max_read_bytes())
    except storage.StorageError as exc:
        _raise_storage_error(exc)
    suffix = Path(version.storage_path).suffix
    return FileDownload(
        content=content,
        filename=_display_filename(version.original_filename, fallback=f"V{version.number}.xlsx"),
        content_type=_content_type(suffix),
        sha256=storage.sha256(content),
        size=len(content),
        relative_path=version.storage_path,
    )


def read_item_image_path(user, image_id) -> str:
    """返回附图的受控相对路径（不返回绝对路径），供 API 层拼下载路由。"""
    image = _get_image(image_id)
    require_action(user, READ, image)
    if image.status != ItemImage.Status.AVAILABLE or not image.storage_path:
        raise ApiError(404, "image_not_available", "该商品附图没有可用文件（状态不是已提取）")
    try:
        found = storage.exists(storage_root(), image.storage_path)
    except storage.StorageError as exc:
        _raise_storage_error(exc)
    if not found:
        raise ApiError(404, "file_content_missing", "附图记录存在但受控存储中找不到文件，请联系管理员")
    return image.storage_path


def read_item_image(user, image_id) -> FileDownload:
    """读取附图字节；越权与不存在同样 404，不泄露对象是否存在。"""
    image = _get_image(image_id)
    require_action(user, READ, image)
    if image.status != ItemImage.Status.AVAILABLE or not image.storage_path:
        raise ApiError(404, "image_not_available", "该商品附图没有可用文件（状态不是已提取）")
    try:
        content = storage.read_bytes(storage_root(), image.storage_path, max_bytes=max_read_bytes())
    except storage.StorageError as exc:
        _raise_storage_error(exc)
    suffix = Path(image.storage_path).suffix
    return FileDownload(
        content=content,
        filename=f"image{suffix}",
        content_type=_content_type(suffix),
        sha256=storage.sha256(content),
        size=len(content),
        relative_path=image.storage_path,
    )


# --------------------------------------------------------------------- 图片入库
def extract_version_images_for_version(
    version,
    *,
    sheet_name: str,
    rows=None,
    column: str | None = None,
    header_row: int = 1,
    keywords=dispimg.DEFAULT_IMAGE_HEADER_KEYWORDS,
    extracted_at=None,
) -> dict:
    """系统派生入口：解析版本原件里的附图并 upsert ``ItemImage``。

    **不接收 user，也不做权限校验，且不对外提供用户重跑入口**：调用方必须已在上传/解析
    流程中完成权限校验，并在自己的事务里完成 Task/FileVersion/Item 的归属校验。允许传
    ``FileVersion`` 实例或版本 ID。同一 ``(item, source_cell)`` 重跑只更新字段、保留主键；
    本次解析未出现的旧单元格记录会被删除（仍限定在本版本内）。

    参数：
    - ``sheet_name``：申报要素页签名（与 ``Item.source_sheet`` 对应）；
    - ``rows``：申报行号集合；缺省取该版本在该页签下已有的商品行；
    - ``column``：图片列；缺省按表头关键词识别，识别不出直接报错，不写“无图片”；
    - ``header_row``：表头行号，缺省 1。

    返回可 JSON 序列化的报告（计数、逐行状态、跳过的行）；**不**创建 ``Item``，
    商品行由 P06 的解析流程负责。可用图片按 sha256 落到该版本的 ``images/`` 目录。
    """
    version = _get_version(version.pk if isinstance(version, FileVersion) else version)
    if not version.storage_path:
        raise ApiError(404, "file_not_stored", "该版本原件尚未入库，无法提取附图")

    root = storage_root()
    try:
        data = storage.read_bytes(root, version.storage_path, max_bytes=max_read_bytes())
    except storage.StorageError as exc:
        _raise_storage_error(exc)
    try:
        workbook = Workbook(data, source_name=version.original_filename)
    except OoxmlError as exc:
        raise ApiError(
            422, f"workbook_{exc.code}", f"原件无法按 OOXML 读取：{exc}",
            {key: value for key, value in
             {"sheet": exc.sheet, "cell": exc.cell}.items() if value},
        ) from exc

    items = {
        item.source_row: item
        for item in Item.objects.filter(version=version, source_sheet=sheet_name)
    }
    if rows is None:
        rows = sorted(items)
        if not rows:
            raise ApiError(
                422, "no_items",
                "该版本在该页签下没有商品行，无法确定要提取哪些申报行的图片",
                {"sheet": sheet_name},
            )

    try:
        extraction = dispimg.extract_images(
            workbook, sheet_name=sheet_name, rows=rows, column=column,
            header_row=header_row, keywords=keywords, max_image_bytes=max_image_bytes(),
        )
    except dispimg.DispimgError as exc:
        raise ApiError(
            422, exc.code, str(exc),
            {key: value for key, value in
             {"sheet": exc.sheet, "column": exc.column}.items() if value},
        ) from exc
    except OoxmlError as exc:
        # 图片定义部件本身损坏（如 cellimages XML/关系非法）时同样按可解释的 422 返回。
        raise ApiError(
            422, f"workbook_{exc.code}", f"图片资源部件无法解析：{exc}",
            {key: value for key, value in
             {"sheet": exc.sheet, "cell": exc.cell}.items() if value},
        ) from exc

    if extraction.column_status != "found" and extraction.column_status != "provided":
        # 列没识别出来 ≠ 没有图片：直接报错，不写 absent 记录冒充结论。
        raise ApiError(
            422, f"image_column_{extraction.column_status}",
            extraction.column_reason or "图片列识别失败，需人工核对",
            {"sheet": sheet_name, "candidates": list(extraction.column_candidates)},
        )

    when = extracted_at or timezone.now()
    records: list[ItemImage] = []
    report_rows: list[dict] = []
    skipped_rows: list[dict] = []
    written_items: list = []
    for row_image in extraction.rows:
        item = items.get(row_image.row)
        if item is None:
            skipped_rows.append({
                "source_row": row_image.row,
                "reason": "该行还没有商品行记录（商品行由上传解析任务创建）",
            })
            continue
        image_path = ""
        if row_image.available:
            try:
                stored = storage.save_image(
                    root, task_id=str(version.task_id), version_id=str(version.pk),
                    media_part=row_image.media_part, data=row_image.data,
                    max_bytes=max_image_bytes(),
                )
            except storage.StorageError as exc:
                _raise_storage_error(exc)
            image_path = stored.relative_path
        records.append(ItemImage(
            item=item,
            source_sheet=sheet_name,
            source_cell=row_image.cell,
            dispimg_id=(row_image.dispimg_id or "")[:128],
            storage_path=image_path,
            status=row_image.status,
            error_message=row_image.message if row_image.status != dispimg.STATUS_AVAILABLE else "",
            extracted_at=when,
        ))
        written_items.append(item)
        report_rows.append({
            "item_id": str(item.pk),
            "source_row": row_image.row,
            "source_cell": row_image.cell,
            "status": row_image.status,
            "code": row_image.code,
            "dispimg_id": row_image.dispimg_id,
            "storage_path": image_path,
            "sha256": row_image.sha256,
            "media_part": row_image.media_part,
            "error_message": row_image.message,
            "id_source": row_image.id_source,
            "notes": list(row_image.notes),
        })

    # 按 (item, source_cell) upsert：重跑保留 ItemImage 主键，旧图片 ID 仍能读到同一张图。
    # 过滤条件带版本 ID，绝不串到旧版本；本次未出现的旧单元格记录删除。
    with transaction.atomic():
        touched = [item.pk for item in written_items]
        existing = {
            (image.item_id, image.source_cell): image
            for image in ItemImage.objects.filter(
                item__version_id=version.pk, item_id__in=touched,
            )
        }
        kept: set[tuple] = set()
        for record in records:
            key = (record.item_id, record.source_cell)
            current = existing.get(key)
            if current is None:
                record.save()
            else:
                current.status = record.status
                current.dispimg_id = record.dispimg_id
                current.storage_path = record.storage_path
                current.error_message = record.error_message
                current.extracted_at = record.extracted_at
                current.save(update_fields=[
                    "status", "dispimg_id", "storage_path", "error_message", "extracted_at",
                ])
            kept.add(key)
        stale = [image.pk for key, image in existing.items() if key not in kept]
        if stale:
            ItemImage.objects.filter(pk__in=stale, item__version_id=version.pk).delete()

    counts = {dispimg.STATUS_AVAILABLE: 0, dispimg.STATUS_ABSENT: 0, dispimg.STATUS_READ_FAILED: 0}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return {
        "task_id": str(version.task_id),
        "version_id": str(version.pk),
        "version_number": version.number,
        "sheet": sheet_name,
        "column": extraction.column,
        "column_status": extraction.column_status,
        "header": extraction.column_header,
        "image_column_provided": column is not None,
        "max_image_bytes": extraction.max_image_bytes,
        "extracted_at": when.isoformat(),
        "counts": {**counts, "skipped": len(skipped_rows)},
        "rows": report_rows,
        "skipped_rows": skipped_rows,
    }


__all__ = [
    "FileDownload",
    "extract_version_images_for_version",
    "max_image_bytes",
    "max_original_bytes",
    "max_read_bytes",
    "read_item_image",
    "read_item_image_path",
    "read_version_original",
    "storage_root",
    "store_version_original",
]
