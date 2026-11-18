"""P15 历史导入输出的原子发布（generation snapshot + current 指针）。

旧实现把 ``history_records.json``/报告逐个 ``os.replace`` 到输出目录：普通异常
还能回滚，但进程被 ``SIGKILL`` 时可能停在"记录已换、报告未换"的中间态，留下
撕裂输出。本模块用一次 generation 快照切换来消除该窗口：

布局::

    out_dir/
      history_records.json      -> .current/history_records.json   （稳定 symlink）
      import_report.json        -> .current/import_report.json
      import_report.md          -> .current/import_report.md
      query_validation.json     -> .current/query_validation.json （本次有验证时）
      images/ID.<sha8>.png      普通文件，内容地址，永久保留
      .current                  -> .snapshots/<generation-id>      （提交点）
      .snapshots/
        <generation-id>/history_records.json, *.json, *.md ...
        .publish.lock

一次发布：

1. 图片先按内容地址写入 ``images/``：同名同内容幂等，同名异内容拒绝，绝不
   覆盖旧图片（新增图片对旧快照无影响）。
2. 全部快照文件先复制进隐藏的 ``.snapshots/.tmp-<id>/``，完成后原子改名为
   ``.snapshots/<id>/``；旧 generation 的其它文件（例如上一轮的
   ``query_validation.json``）会被带到新 generation，保持与旧平面输出一致的
   可见性。
3. 顶层文件名如果还不存在，先建好指向 ``.current/<name>`` 的稳定 symlink。
4. 提交：创建临时 ``.current`` symlink，然后一次 ``os.replace`` 成
   ``.current``。这是唯一提交点：

   - 提交前被杀：``.current`` 仍指向旧 generation，旧输出完整；
   - 提交后被杀：``.current`` 指向新 generation，新输出完整；
   - 任何时刻都不存在新旧文件混搭的可见快照。

首次发布前已有的普通平面输出（顶层普通文件、没有 ``.current``）会平滑迁移：
先把旧文件原样复制进一个 legacy generation 并设成 current，再逐个把顶层普通
文件替换成指向 ``.current/<name>`` 的 symlink（替换前后内容完全相同），最后
才把 ``.current`` 切到新 generation。迁移的任何阶段中断，顶层文件要么是旧普通
文件、要么是等内容的稳定 symlink，旧记录始终可读。

不清理旧 generation（旧读者已打开的路径/文件描述符继续有效）；只有本进程创建
但尚未提交的临时 generation/temp symlink 会在失败回滚时删除，崩溃遗留的
``.tmp-*`` 会在下一次发布取到锁后清扫。

文件系统/权限不支持 symlink 时抛 :class:`PublishError` 并保留旧输出，不会退化成
逐个 ``os.replace`` 的非原子发布。发布期间用 ``fcntl``/``msvcrt`` 文件锁串行化
并发发布；提交本身不依赖锁，读者无需加锁。

集成方式（调用方 ``adapters/sources/history_import.py`` 的薄包装）::

    from adapters.sources.history_publish import PublishError, publish_staging

    def _publish_staging(staging, out_dir, files):
        try:
            publish_staging(staging, out_dir, files)
        except PublishError as exc:
            raise ImportIncompleteError(f"发布输出失败，旧输出未改动：{exc}") from exc

限制与约定：

- 需要 symlink 支持（``.current`` 与顶层稳定链接都是 symlink）。不支持时明确
  报错并保留旧输出，不会降级成逐文件覆盖。云同步目录（如 OneDrive）可能不
  同步 symlink，正式输出建议放本地文件系统。
- 一个读者如果先 open ``history_records.json``、等提交发生后再 open
  ``import_report.json``，两次 open 会分别解析 ``.current``，可能跨 generation。
  需要多文件一致性的读者应先解析一次 ``(out_dir / ".current").resolve()``，
  再从该 generation 目录读取；已经打开的文件描述符不受切换影响。
- 测试故障注入：设置模块级 ``_FAULT_INJECTION_HOOK = fn``，发布过程会在
  ``images-before``/``images-after``/``legacy-ready``/``legacy-current-set``/
  ``legacy-replace-<i>``/``generation-ready``/``link-created-<i>``/
  ``before-commit``/``after-commit`` 调用 ``fn(stage)``，可在子进程里
  ``os.kill(os.getpid(), signal.SIGKILL)`` 做真实中断测试。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Sequence

__all__ = ["PublishError", "publish_staging"]

# 顶层稳定链接名与隐藏目录名。这些名字不允许出现在待发布文件相对路径中。
CURRENT_LINK_NAME = ".current"
SNAPSHOTS_DIR_NAME = ".snapshots"
IMAGES_DIR_NAME = "images"
LOCK_FILE_NAME = ".publish.lock"

# 顶层快照文件名：用于识别/迁移旧的平面输出，并保持 P15 现有路径不变。
KNOWN_SNAPSHOT_NAMES = (
    "history_records.json",
    "import_report.json",
    "import_report.md",
    "query_validation.json",
    "query_validation.md",
)

_COMPARE_CHUNK = 1024 * 1024


class PublishError(ValueError):
    """发布失败；旧输出保持完整可用，未降级为逐文件覆盖。"""


# 仅供测试的故障注入钩子：在指定阶段调用（例如 kill -9 当前子进程）。
# 生产代码恒为 None；设置后异常会照常向上传播，便于测试重复触发中断。
_FAULT_INJECTION_HOOK: Callable[[str], None] | None = None


def _checkpoint(stage: str) -> None:
    """发布阶段标记；默认无操作，测试可替换 ``_FAULT_INJECTION_HOOK``。"""

    hook = _FAULT_INJECTION_HOOK
    if hook is not None:
        hook(stage)


# --------------------------------------------------------------------------- 平台锁
if os.name == "nt":  # pragma: no cover - Windows 分支在 macOS/Linux 上不执行
    import msvcrt

    _LOCK_AVAILABLE = True

    def _acquire_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _release_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - 理论上的非 POSIX 平台
        fcntl = None  # type: ignore[assignment]
        _LOCK_AVAILABLE = False
    else:
        _LOCK_AVAILABLE = True

    def _acquire_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- 基础工具
def _absolute(path: Path | str) -> Path:
    """规范化成绝对路径，但保留调用方给定的 symlink 语义（不 realpath）。"""

    return Path(os.path.abspath(os.fspath(path)))


def _new_token() -> str:
    return uuid.uuid4().hex[:12]


def _timestamp_id() -> str:
    return f"{time.time_ns():020d}"


def _sorted_rels(rels: Sequence[Path] | set[Path]) -> list[Path]:
    return sorted(rels, key=lambda item: item.as_posix())


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    """尽力 fsync；不支持/失败不影响原子性（SIGKILL 不会丢 page cache）。"""

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if os.stat(first).st_size != os.stat(second).st_size:
            return False
        with open(first, "rb") as left, open(second, "rb") as right:
            while True:
                left_chunk = left.read(_COMPARE_CHUNK)
                right_chunk = right.read(_COMPARE_CHUNK)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _is_within(child: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.fspath(child), os.fspath(root)]) == os.fspath(root)
    except ValueError:
        return False


def _stable_link_target(rel: Path) -> str:
    """顶层文件应指向的稳定目标文本，例如 ``.current/history_records.json``。"""

    return os.path.join(CURRENT_LINK_NAME, *rel.parts)


def _validated_rel(rel: Path, *, origin: str) -> Path:
    """校验暂存相对路径；拒绝绝对路径、``..``、隐藏名与内部目录名。"""

    if rel.is_absolute():
        raise PublishError(f"待发布文件路径不能是绝对路径：{origin}")
    parts = rel.parts
    if not parts:
        raise PublishError(f"待发布文件路径为空：{origin}")
    for part in parts:
        if part in {"", ".", ".."}:
            raise PublishError(f"待发布文件路径包含非法片段 {part!r}：{origin}")
        if part.startswith("."):
            raise PublishError(f"待发布文件路径不能使用隐藏名 {part!r}：{origin}")
        if part in {CURRENT_LINK_NAME, SNAPSHOTS_DIR_NAME}:
            raise PublishError(f"待发布文件路径不能占用内部名 {part!r}：{origin}")
        if "\x00" in part:
            raise PublishError(f"待发布文件路径包含 NUL 字节：{origin}")
        if os.sep in part or (os.altsep and os.altsep in part):
            raise PublishError(f"待发布文件路径片段包含分隔符：{origin}")
    return Path(*parts)


def _collect_files(
    staging: Path, files: Sequence[Path]
) -> tuple[dict[Path, Path], dict[Path, Path]]:
    """把待发布文件分成快照文件与图片，并校验路径不逃出暂存目录。"""

    staging_abs = _absolute(staging)
    snapshots: dict[Path, Path] = {}
    images: dict[Path, Path] = {}
    for item in files:
        staged = _absolute(Path(item))
        try:
            info = os.lstat(staged)
        except FileNotFoundError:
            # 与旧实现一致：不存在的条目忽略（图片目录 rglob 会带出目录）。
            continue
        except OSError as exc:
            raise PublishError(f"无法读取待发布文件 {staged}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PublishError(f"待发布文件是符号链接，拒绝跟随：{staged}")
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            rel = staged.relative_to(staging_abs)
        except ValueError as exc:
            raise PublishError(
                f"待发布文件不在暂存目录 {staging_abs} 内：{staged}"
            ) from exc
        rel = _validated_rel(rel, origin=str(staged))
        if rel.parts[0] == IMAGES_DIR_NAME:
            if len(rel.parts) < 2:
                raise PublishError(f"图片路径缺少文件名：{rel}")
            bucket = images
        else:
            bucket = snapshots
        if rel in bucket:
            raise PublishError(f"待发布文件重复：{rel.as_posix()}")
        bucket[rel] = staged
    return snapshots, images


# --------------------------------------------------------------------------- 目录安全
def _ensure_real_dir(out_dir: Path, parts: Sequence[str]) -> Path:
    """在输出目录内确保逐级是真实目录（不跟随 symlink），拒绝写出输出目录。"""

    root_real = Path(os.path.realpath(out_dir))
    current = out_dir
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise PublishError(f"无法创建输出目录 {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PublishError(
                f"输出路径 {current} 是符号链接，拒绝跟随（避免写出输出目录）"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise PublishError(f"输出路径 {current} 不是目录")
        if not _is_within(Path(os.path.realpath(current)), root_real):
            raise PublishError(
                f"输出路径 {current} 解析后位于输出目录之外，拒绝写入"
            )
    return current


def _ensure_snapshots_dir(out_dir: Path) -> Path:
    return _ensure_real_dir(out_dir, (SNAPSHOTS_DIR_NAME,))


def _open_lock_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PublishError(f"无法打开发布锁 {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"发布锁 {path} 不是普通文件")
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _output_lock(out_dir: Path) -> Iterator[None]:
    """串行化同一输出目录的并发发布；进程被杀时操作系统自动释放锁。"""

    snapshots_dir = _ensure_snapshots_dir(out_dir)
    fd = _open_lock_file(snapshots_dir / LOCK_FILE_NAME)
    try:
        if _LOCK_AVAILABLE:
            try:
                _acquire_lock(fd)
            except OSError as exc:
                raise PublishError(f"获取发布锁失败：{exc}") from exc
        try:
            yield
        finally:
            if _LOCK_AVAILABLE:
                try:
                    _release_lock(fd)
                except OSError:
                    pass
    finally:
        os.close(fd)


def _scandir_entries(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as iterator:
            return list(iterator)
    except OSError:
        return []


def _sweep_stale_temps(out_dir: Path) -> None:
    """清扫上次崩溃遗留的临时 generation/temp symlink（不碰已提交 generation）。"""

    # 顶层稳定链接的临时名（.{name}.link-{token}）可能因 SIGKILL 残留。
    for entry in _scandir_entries(out_dir):
        if entry.name.startswith(".") and ".link-" in entry.name:
            _unlink_quietly(Path(entry.path))
    snapshots_dir = out_dir / SNAPSHOTS_DIR_NAME
    for entry in _scandir_entries(snapshots_dir):
        name = entry.name
        if name == LOCK_FILE_NAME:
            continue
        if name.startswith(".tmp-"):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                _unlink_quietly(Path(entry.path))
        elif name.startswith(".current-") or name.startswith(".probe-"):
            _unlink_quietly(Path(entry.path))
    images_dir = out_dir / IMAGES_DIR_NAME
    try:
        if images_dir.is_dir() and not images_dir.is_symlink():
            for entry in _scandir_entries(images_dir):
                if entry.name.startswith(".") and ".tmp-" in entry.name:
                    _unlink_quietly(Path(entry.path))
    except OSError:
        pass


def _require_symlink_support(out_dir: Path) -> None:
    """在改动任何输出前探测 symlink 能力；不支持则明确报错，不降级。"""

    snapshots_dir = out_dir / SNAPSHOTS_DIR_NAME
    probe = snapshots_dir / f".probe-{_new_token()}"
    try:
        os.symlink("symlink-probe-target", probe)
    except (OSError, NotImplementedError) as exc:
        raise PublishError(
            "输出目录所在文件系统/权限不支持符号链接，无法原子发布；旧输出未改动。"
            "请把 --out-dir 指向支持 symlink 的本地文件系统，或使用具备 symlink "
            "权限的账号（Windows 需开发者模式或管理员权限）。"
        ) from exc
    finally:
        _unlink_quietly(probe)


# --------------------------------------------------------------------------- 图片发布
def _publish_image_file(staged: Path, target: Path) -> None:
    """内容地址图片：同名同内容跳过，同名异内容拒绝，绝不覆盖旧图片。"""

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise PublishError(
                f"输出图片 {target} 是符号链接，拒绝跟随/覆盖；旧输出未改动"
            )
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"输出图片路径 {target} 不是普通文件；旧输出未改动")
        if not _files_equal(staged, target):
            raise PublishError(
                f"输出图片同名但内容不同，拒绝覆盖：{target.name}；旧输出未改动"
            )
        return

    tmp = target.with_name(f".{target.name}.tmp-{_new_token()}")
    try:
        shutil.copyfile(staged, tmp)
        _fsync_file(tmp)
        try:
            # 硬链接带 O_EXCL 语义：目标已存在时失败，绝不覆盖。
            os.link(tmp, target)
        except FileExistsError:
            if not _files_equal(staged, target):
                raise PublishError(
                    f"输出图片同名但内容不同，拒绝覆盖：{target.name}；旧输出未改动"
                )
        except OSError as exc:
            # 个别文件系统不支持硬链接；锁内检查后 rename，仍不覆盖已有图片。
            if os.path.lexists(target):
                if not _files_equal(staged, target):
                    raise PublishError(
                        f"输出图片同名但内容不同，拒绝覆盖：{target.name}；旧输出未改动"
                    ) from exc
            else:
                try:
                    os.replace(tmp, target)
                except OSError as rename_exc:
                    raise PublishError(
                        f"写入输出图片 {target.name} 失败：{rename_exc}；旧输出未改动"
                    ) from rename_exc
                tmp = None
    finally:
        if tmp is not None:
            _unlink_quietly(tmp)


def _publish_images(out_dir: Path, images: dict[Path, Path]) -> None:
    if not images:
        return
    image_root = _ensure_real_dir(out_dir, (IMAGES_DIR_NAME,))
    _checkpoint("images-before")
    for rel in _sorted_rels(images):
        staged = images[rel]
        parts = rel.parts[1:]
        parent = _ensure_real_dir(image_root, parts[:-1])
        _publish_image_file(staged, parent / parts[-1])
    _checkpoint("images-after")


# --------------------------------------------------------------------------- generation
def _read_current(out_dir: Path) -> Path | None:
    """读取并校验 ``.current``；返回对应 generation 目录。"""

    link = out_dir / CURRENT_LINK_NAME
    try:
        info = os.lstat(link)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise PublishError(
            f"输出目录中的 {link} 存在但不是符号链接，状态不一致；"
            "请先人工确认/清理该路径（旧记录未被本模块改动）"
        )
    target = os.readlink(link)
    normalized = os.path.normpath(target)
    parts = Path(normalized).parts
    if os.path.isabs(target) or len(parts) != 2 or parts[0] != SNAPSHOTS_DIR_NAME:
        raise PublishError(
            f"输出目录中的 .current 指向非法目标 {target!r}；拒绝跟随"
        )
    generation_id = parts[1]
    if (
        not generation_id
        or generation_id.startswith(".")
        or generation_id in {".", ".."}
    ):
        raise PublishError(f"输出目录中的 .current 指向非法 generation {generation_id!r}")
    generation_dir = out_dir / SNAPSHOTS_DIR_NAME / generation_id
    try:
        gen_info = os.lstat(generation_dir)
    except FileNotFoundError as exc:
        raise PublishError(
            f".current 指向的 generation 不存在：{generation_dir}；输出状态不一致"
        ) from exc
    if stat.S_ISLNK(gen_info.st_mode) or not stat.S_ISDIR(gen_info.st_mode):
        raise PublishError(f"generation {generation_dir} 不是真实目录")
    return generation_dir


def _install_legacy_generation(out_dir: Path, rels: Sequence[Path]) -> str:
    """把旧的顶层普通文件原样复制进一个新 generation（尚未切换 current）。"""

    snapshots_dir = out_dir / SNAPSHOTS_DIR_NAME
    generation_id = f"{_timestamp_id()}-legacy-{_new_token()}"
    tmp_dir = snapshots_dir / f".tmp-{generation_id}"
    try:
        tmp_dir.mkdir()
        for rel in _sorted_rels(rels):
            source = out_dir / rel
            destination = tmp_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            _fsync_file(destination)
        os.replace(tmp_dir, snapshots_dir / generation_id)
    except OSError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise PublishError(f"迁移旧输出到 generation 失败：{exc}；旧输出未改动") from exc
    return generation_id


def _ensure_generation_copy(generation_dir: Path, source: Path, rel: Path) -> None:
    """确保旧 generation 中已有与顶层旧文件一致的副本（中断迁移的自愈）。"""

    destination = generation_dir / rel
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"旧 generation 中的 {rel.as_posix()} 不是普通文件")
        if not _files_equal(source, destination):
            raise PublishError(
                f"旧 generation 中的 {rel.as_posix()} 与顶层文件内容不一致，拒绝切换"
                "（可能被手工修改，请先人工确认）"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        _fsync_file(destination)
    except OSError as exc:
        raise PublishError(
            f"补齐旧 generation 中的 {rel.as_posix()} 失败：{exc}；旧输出未改动"
        ) from exc


def _replace_regular_with_stable_link(out_dir: Path, rel: Path) -> None:
    """把顶层普通文件原子替换为指向 ``.current/<name>`` 的稳定 symlink。"""

    parent = _ensure_real_dir(out_dir, rel.parts[:-1])
    target = parent / rel.name
    wanted = _stable_link_target(rel)
    tmp_link = parent / f".{rel.name}.link-{_new_token()}"
    try:
        os.symlink(wanted, tmp_link)
        os.replace(tmp_link, target)
    except OSError as exc:
        _unlink_quietly(tmp_link)
        raise PublishError(
            f"无法把 {target} 替换为稳定符号链接：{exc}；旧输出仍可读"
        ) from exc


def _migrate_legacy_output(out_dir: Path, candidate_names: set[Path]) -> Path | None:
    """迁移旧的平面输出；返回可作为新 generation 回填来源的旧 generation。

    任何阶段中断都保持顶层内容不变（普通文件或等内容 symlink 的混合）。
    """

    current = _read_current(out_dir)
    legacy_rels: list[Path] = []
    for rel in _sorted_rels(candidate_names):
        target = out_dir / rel
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            actual = os.readlink(target)
            if actual != _stable_link_target(rel):
                raise PublishError(
                    f"输出中的 {target} 是指向 {actual!r} 的符号链接，不是本模块生成的"
                    "稳定链接；拒绝跟随或覆盖（旧输出未改动）"
                )
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"输出中的 {target} 既不是普通文件也不是稳定链接，拒绝发布")
        legacy_rels.append(rel)
    if not legacy_rels:
        return current

    if current is None:
        generation_id = _install_legacy_generation(out_dir, legacy_rels)
        _checkpoint("legacy-ready")
        _commit_current(out_dir, generation_id)
        _checkpoint("legacy-current-set")
        current = out_dir / SNAPSHOTS_DIR_NAME / generation_id

    for index, rel in enumerate(legacy_rels):
        _ensure_generation_copy(current, out_dir / rel, rel)
        _replace_regular_with_stable_link(out_dir, rel)
        _checkpoint(f"legacy-replace-{index}")
    return current


def _iter_generation_files(generation_dir: Path) -> Iterator[tuple[Path, Path]]:
    """遍历 generation 内的普通文件（不跟随 symlink）。"""

    for root, dirnames, filenames in os.walk(generation_dir, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(root) / name
            if full.is_symlink():
                continue
            yield full.relative_to(generation_dir), full


def _build_generation(
    out_dir: Path,
    snapshots: dict[Path, Path],
    carry_from: Path | None,
) -> tuple[str, Path]:
    """在隐藏临时目录组装新 generation，再原子改名；旧 generation 不动。"""

    snapshots_dir = out_dir / SNAPSHOTS_DIR_NAME
    generation_id = f"{_timestamp_id()}-{_new_token()}"
    tmp_dir = snapshots_dir / f".tmp-{generation_id}"
    final_dir = snapshots_dir / generation_id
    try:
        tmp_dir.mkdir()
        if carry_from is not None:
            for rel, source in _iter_generation_files(carry_from):
                if any(part.startswith(".") for part in rel.parts):
                    # 忽略文件系统/编辑器留下的隐藏文件，它们不属于快照。
                    continue
                rel = _validated_rel(rel, origin=str(source))
                if rel in snapshots:
                    continue
                destination = tmp_dir / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                _fsync_file(destination)
        for rel in _sorted_rels(snapshots):
            source = snapshots[rel]
            destination = tmp_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            _fsync_file(destination)
        os.replace(tmp_dir, final_dir)
    except PublishError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise PublishError(f"写入新 generation 失败：{exc}；旧输出未改动") from exc
    return generation_id, final_dir


def _ensure_top_level_links(out_dir: Path, rels: set[Path]) -> list[Path]:
    """为新 generation 的每个文件名准备稳定 symlink；返回本次新建的链接。"""

    created: list[Path] = []
    for rel in _sorted_rels(rels):
        parent = _ensure_real_dir(out_dir, rel.parts[:-1])
        target = parent / rel.name
        wanted = _stable_link_target(rel)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                actual = os.readlink(target)
                if actual == wanted:
                    continue
                raise PublishError(
                    f"输出 {target} 已存在且指向 {actual!r}，不是稳定链接；拒绝覆盖"
                )
            raise PublishError(
                f"输出 {target} 已存在且不是稳定链接（旧输出迁移未完成？）；拒绝覆盖"
            )
        tmp_link = parent / f".{rel.name}.link-{_new_token()}"
        try:
            os.symlink(wanted, tmp_link)
            os.replace(tmp_link, target)
        except OSError as exc:
            _unlink_quietly(tmp_link)
            raise PublishError(
                f"无法创建稳定符号链接 {target}：{exc}；旧输出未改动"
            ) from exc
        created.append(target)
        _checkpoint(f"link-created-{len(created) - 1}")
    return created


def _commit_current(out_dir: Path, generation_id: str) -> None:
    """唯一提交点：一次 ``os.replace`` 把临时 symlink 换成为 ``.current``。"""

    snapshots_dir = out_dir / SNAPSHOTS_DIR_NAME
    tmp_link = snapshots_dir / f".current-{_new_token()}.tmp"
    relative_target = os.path.join(SNAPSHOTS_DIR_NAME, generation_id)
    try:
        os.symlink(relative_target, tmp_link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        _unlink_quietly(tmp_link)
        raise PublishError(
            f"无法创建 .current 临时符号链接：{exc}；旧输出未改动"
        ) from exc
    try:
        os.replace(tmp_link, out_dir / CURRENT_LINK_NAME)
    except OSError as exc:
        _unlink_quietly(tmp_link)
        raise PublishError(
            f"切换 .current 指针失败：{exc}；旧输出未改动。"
            "若在 Windows 上，替换目录符号链接可能需要更底层的支持；"
            "不会退化为非原子发布。"
        ) from exc


def _rollback_publish(created_links: Sequence[Path], generation_dir: Path | None) -> None:
    """提交前失败的回滚：只删除本次新建且未提交的链接/generation。"""

    for link in reversed(created_links):
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
    if generation_dir is not None:
        shutil.rmtree(generation_dir, ignore_errors=True)


def _check_records_baseline(out_dir: Path, expected: str | None) -> None:
    """持发布锁比较记录基线，拒绝在锁外 merge 后覆盖并发发布的新记录。"""

    if expected is None:
        return
    records = out_dir / "history_records.json"
    try:
        info = os.lstat(records)
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        if os.readlink(records) != _stable_link_target(Path("history_records.json")):
            raise PublishError("记录基线是外来符号链接，拒绝跟随；旧输出未改动")
        generation = _read_current(out_dir)
        # 首次发布在建顶层链接后被杀，允许不存在 current 的悬空稳定链接。
        records = generation / "history_records.json" if generation else None
        try:
            info = os.lstat(records) if records is not None else None
        except FileNotFoundError:
            info = None
    actual = ""
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise PublishError("记录基线不是普通文件，拒绝读取；旧输出未改动")
        digest = hashlib.sha256()
        try:
            with open(records, "rb") as handle:
                for chunk in iter(lambda: handle.read(_COMPARE_CHUNK), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise PublishError(f"无法读取记录基线：{exc}；旧输出未改动") from exc
        actual = digest.hexdigest()
    if actual != expected:
        raise PublishError(
            "并发发布冲突：history_records.json 已在读取/合并后发生变化；"
            "本次未发布，旧输出未改动。请重新读取最新记录、合并后重试。"
        )


# --------------------------------------------------------------------------- 公共入口
def publish_staging(
    staging: Path,
    out_dir: Path,
    files: Sequence[Path],
    *,
    expected_records_sha256: str | None = None,
) -> None:
    """把暂存文件原子发布到 ``out_dir``；失败时旧输出保持完整可用。

    参数与旧 ``history_import._publish_staging`` 完全一致：``files`` 中位于
    ``staging`` 下的普通文件按相对路径发布；``images/`` 下的文件按内容地址
    永久写入输出 ``images/`` 目录，其余文件进入 generation 快照。不存在的条目
    会被忽略（图片目录 ``rglob`` 会带出目录）。

    ``expected_records_sha256`` 为锁外读取/合并所用的记录原始字节 SHA256。
    默认 ``None`` 不检查；``""`` 表示期待记录不存在。持发布锁后、任何输出改动前
    比较当前记录哈希，不一致抛 ``PublishError``，调用方必须重新读取/合并并重试，
    不能直接重复发布旧 staging。调用方应对同一次 read_bytes() 的字节同时计算哈希
    和解析，不能在读取旧记录之后再独立重读计算哈希（否则仍可能丢更新）。

    成功返回 ``None``。失败抛 :class:`PublishError`（``ValueError`` 子类）；
    旧输出不受影响，不会降级成逐文件非原子替换。
    """

    staging_abs = _absolute(Path(staging))
    out_abs = _absolute(Path(out_dir))
    if not staging_abs.is_dir():
        raise PublishError(f"暂存目录不存在：{staging_abs}")
    snapshots, images = _collect_files(staging_abs, files)
    try:
        out_abs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PublishError(f"无法创建输出目录 {out_abs}: {exc}") from exc
    if not out_abs.is_dir():
        raise PublishError(f"输出路径不是目录：{out_abs}")

    created_links: list[Path] = []
    generation_dir: Path | None = None
    with _output_lock(out_abs):
        _check_records_baseline(out_abs, expected_records_sha256)
        _sweep_stale_temps(out_abs)
        _require_symlink_support(out_abs)
        candidate_names = {Path(name) for name in KNOWN_SNAPSHOT_NAMES} | set(snapshots)
        try:
            _publish_images(out_abs, images)
            old_generation = _migrate_legacy_output(out_abs, candidate_names)
            generation_id, generation_dir = _build_generation(
                out_abs, snapshots, old_generation
            )
            _checkpoint("generation-ready")
            new_generation_rels = {
                rel for rel, _ in _iter_generation_files(generation_dir)
            }
            created_links = _ensure_top_level_links(out_abs, new_generation_rels)
            _checkpoint("before-commit")
            _commit_current(out_abs, generation_id)
        except PublishError:
            _rollback_publish(created_links, generation_dir)
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转为发布错误并回滚
            _rollback_publish(created_links, generation_dir)
            raise PublishError(
                "发布输出失败，旧输出保持可用（未降级为逐文件覆盖）："
                f"{type(exc).__name__}: {exc}"
            ) from exc
    _checkpoint("after-commit")
