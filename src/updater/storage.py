"""Atomic, durable JSON records for update state."""

import json
import os
from pathlib import Path


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
