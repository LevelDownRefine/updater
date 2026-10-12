"""Paths accepted by plain ZIP updates."""

import re
from pathlib import Path, PureWindowsPath

from updater.package import UpdateError, linked_path


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
