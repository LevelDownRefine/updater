"""同一安装目录的运行共享锁和更新独占锁。"""

import contextlib
import logging
import os
from pathlib import Path

import psutil

from updater.policy import UpdatePolicy
from updater.utils import FileLease, UpdateError, update_directory

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def application_lease(root: Path, *, policy: UpdatePolicy):
    """启动先通过更新闸门，再持有运行共享锁，覆盖 GUI 与所有 CLI 入口。"""
    directory = update_directory(root)
    with contextlib.ExitStack() as stack:
        with FileLease(directory / "intent.lock", shared=True):
            stack.enter_context(FileLease(directory / "runtime.lock", shared=True))
            # 安装状态检查按需加载，避免循环依赖。
            from updater.installer import settle_transaction

            if not settle_transaction(root, policy=policy):
                raise UpdateError("上次更新未完成，请运行更新器 --recover 恢复后再启动")
        yield


def application_processes(
    root: Path, excluded: set[int] | None = None, *, policy: UpdatePolicy
) -> list[int]:
    """按 EXE 完整路径识别当前安装的 GUI、调度和 Runner，不按名称误杀其他安装。"""
    excluded = excluded or set()
    targets = {
        os.path.normcase(str((root / name).resolve())) for name in policy.process_exes
    }
    found = []
    for process in psutil.process_iter():
        if process.pid in excluded:
            continue
        try:
            executable = process.exe()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            # 无权限的系统进程不是当前管理员用户启动的应用。
            logger.debug("无法读取进程路径: pid=%s", process.pid)
            continue
        if executable and os.path.normcase(str(Path(executable).resolve())) in targets:
            found.append(process.pid)
    return found


def child_environment() -> dict[str, str]:
    """启动独立冻结程序，不复用父进程的 PyInstaller 解压目录。"""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
