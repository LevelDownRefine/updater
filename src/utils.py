"""Shared paths, validation, locks and durable filesystem operations."""

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
from concurrent.futures import CancelledError
from pathlib import Path, PureWindowsPath
from threading import Event

import portalocker
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

MAX_PACKAGE_BYTES = 4 * 1024**3
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF_SECONDS = 0.2
TRANSIENT_WINERRORS = frozenset({5, 32, 33})
TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY, errno.ETXTBSY})


class UpdateError(ValueError):
    """可恢复的更新输入或状态错误。"""


class UpdateCancelled(UpdateError, CancelledError):
    """用户中断下载；已下载的部分保留，供下次续传。"""


def check_cancelled(cancelled: Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise UpdateCancelled("下载已取消")


def version_number(value: str) -> Version:
    """按版本语义比较，不按字符串比较。"""
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise UpdateError(f"无法识别版本号: {value}") from exc


def linked_path(path: Path) -> bool:
    """符号链接或 Windows junction；更新路径不允许落在链接后面。"""
    return path.is_symlink() or bool(
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
    )


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def relative_path(name: str) -> bool:
    """Reject traversal, Windows aliases and updater state paths."""
    return (
        isinstance(name, str)
        and bool(name)
        and all(
            part
            and part not in {".", ".."}
            and not part.endswith((".", " "))
            and not PureWindowsPath(part).is_reserved()
            and not any(char in part for char in '\\:*?"<>|')
            and not any(ord(char) < 32 for char in part)
            and re.search(r"\.bak\d*$", part, re.IGNORECASE) is None
            and part.casefold() != ".update"
            for part in name.split("/")
        )
    )


def target_path(root: Path, name: str) -> Path:
    """Resolve a validated relative file without traversing links."""
    if not relative_path(name):
        raise UpdateError(f"Unsafe update path: {name}")
    target = root / name
    for item in (target, *target.parents):
        if item == root:
            break
        if linked_path(item):
            raise UpdateError(f"Update path traverses a link: {name}")
    return target


def write_json(path: Path, data: dict) -> None:
    """临时文件与目标同目录；先 flush/fsync 再原子替换。

    名字带进程号：启动闸门在共享锁下也会补写记录，固定名会让并发启动互相踩。
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class UpdateBusyError(UpdateError):
    """更新或脚本运行尚未结束。"""


class FileLease:
    """共享运行锁和独占更新锁，由 portalocker 管理跨平台加锁与释放。"""

    def __init__(self, path: Path, *, shared: bool = False, timeout: float = 0):
        self.path = path
        self.lock = portalocker.Lock(
            path,
            mode="a+b",
            timeout=timeout,
            check_interval=0.1,
            flags=(portalocker.LOCK_SH if shared else portalocker.LOCK_EX)
            | portalocker.LOCK_NB,
        )

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.lock.acquire()
        except portalocker.AlreadyLocked as exc:
            raise UpdateBusyError("应用正在运行任务或更新，请稍后重试") from exc
        except portalocker.LockException as exc:
            raise UpdateError(f"无法锁定安装目录: {exc}") from exc
        return self

    def __exit__(self, *args):
        self.lock.__exit__(*args)


def update_directory(root: Path) -> Path:
    directory = root / ".update"
    if linked_path(directory):
        raise UpdateError("更新工作目录不能是链接")
    directory.mkdir(exist_ok=True)
    return directory


def _transient_occupation(exc: OSError) -> bool:
    """瞬时占用可退避重试；其余失败立即上抛，不掩盖确定性错误。"""
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in TRANSIENT_WINERRORS
    return exc.errno in TRANSIENT_ERRNOS


def _replace(source: Path, target: Path, temporary: Path) -> None:
    """copy/fsync/原子替换；占用退避重试，不让偶发失败中断整次安装或回滚。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            shutil.copy2(source, temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return
        except OSError as exc:
            if attempt + 1 == REPLACE_ATTEMPTS or not _transient_occupation(exc):
                # 清掉半成品；清理本身失败不能盖住真正的异常。
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
                raise
            delay = REPLACE_BACKOFF_SECONDS * 2**attempt
            logger.warning(
                "文件被占用，%.1f 秒后重试: %s (%s: %s)",
                delay,
                target.name,
                type(exc).__name__,
                exc,
            )
            time.sleep(delay)
