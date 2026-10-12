"""独立更新器入口，不依赖 Qt；从安装目录之外的副本运行。"""

import argparse
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil

from updater.installer import install_package, recover_installation
from updater.policy import UpdatePolicy
from updater.runtime import application_processes, child_environment
from updater.utils import FileLease, UpdateBusyError, update_directory, write_json

logger = logging.getLogger(__name__)


def run_update(
    root: Path,
    package: Path | None,
    *,
    parent_pid: int = 0,
    parent_created: float = 0,
    frontend_pid: int = 0,
    frontend_created: float = 0,
    ready: Path | None = None,
    cancel: Path | None = None,
    recover: bool = False,
    policy: UpdatePolicy,
) -> None:
    """先关闭启动闸门，再等待调用窗口释放运行锁；不终止任何已有任务。"""
    directory = update_directory(root)
    with FileLease(directory / "intent.lock"):
        waiting = []
        for pid, created, executable in (
            (
                parent_pid,
                parent_created,
                tuple({name for names in policy.cli_exes.values() for name in names})
                if frontend_pid
                else None,
            ),
            (frontend_pid, frontend_created, (policy.app_exe,)),
        ):
            if not pid:
                continue
            try:
                process = psutil.Process(pid)
                if process.create_time() != created:
                    raise UpdateBusyError("调用进程已变化，取消更新")
                if executable is not None and Path(process.exe()).resolve() not in {
                    root / name for name in executable
                }:
                    raise UpdateBusyError("调用进程不属于当前安装")
                if (
                    pid == parent_pid
                    and frontend_pid
                    and process.ppid() != frontend_pid
                ):
                    raise UpdateBusyError("CLI 与应用窗口的父子关系已变化")
                waiting.append(process)
            except psutil.NoSuchProcess:
                logger.info("调用进程已经退出: pid=%s", pid)
        occupied = application_processes(
            root, {os.getpid(), parent_pid, frontend_pid}, policy=policy
        )
        if occupied:
            raise UpdateBusyError(f"当前安装仍有运行中的进程: {occupied}")
        if ready is not None:
            write_json(ready, {"status": "ready"})
        deadline = time.monotonic() + 30
        for process in waiting:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except psutil.TimeoutExpired as exc:
                raise UpdateBusyError("窗口未退出，已取消安装") from exc
        with FileLease(directory / "runtime.lock", timeout=10):
            if cancel is not None and cancel.exists():
                raise UpdateBusyError("调用方已取消更新")
            occupied = application_processes(root, {os.getpid()}, policy=policy)
            if occupied:
                raise UpdateBusyError(f"当前安装仍有运行中的进程: {occupied}")
            if recover:
                recover_installation(root, policy=policy)
            else:
                assert package is not None
                install_package(root, package, policy=policy)


def main(policy: UpdatePolicy) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--package", type=Path)
    action.add_argument("--recover", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--parent-created", type=float, default=0)
    parser.add_argument("--frontend-pid", type=int, default=0)
    parser.add_argument("--frontend-created", type=float, default=0)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--cancel", type=Path)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    directory = update_directory(root)
    logging.basicConfig(
        filename=directory / "update.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    result = args.result or directory / "result.json"

    def record_result(data):
        write_json(directory / "result.json", data)
        if result != directory / "result.json":
            write_json(result, data)

    try:
        run_update(
            root,
            args.package,
            parent_pid=args.parent_pid,
            parent_created=args.parent_created,
            frontend_pid=args.frontend_pid,
            frontend_created=args.frontend_created,
            ready=args.ready,
            cancel=args.cancel,
            recover=args.recover,
            policy=policy,
        )
    except (OSError, ValueError, psutil.Error) as exc:
        logger.exception("更新失败")
        record_result({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    record_result({"status": "recovered" if args.recover else "installed"})
    if args.restart:
        try:
            subprocess.Popen(
                [str(root / policy.app_exe), *policy.restart_args],
                cwd=root,
                env=child_environment(),
            )
        except OSError as exc:
            logger.exception("更新完成但重启失败")
            record_result(
                {"status": "restart_failed", "error": f"{type(exc).__name__}: {exc}"},
            )
            return 1
    return 0
