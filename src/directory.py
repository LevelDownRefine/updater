"""Transactional installation of plain ZIP files and a local ownership inventory."""

import json
import logging
import stat
import uuid
from pathlib import Path

from updater.utils import (
    UpdateError,
    _replace,
    file_digest,
    linked_path,
    remove_directory,
    target_path,
    unlink_file,
    update_directory,
    version_number,
    write_json,
)

logger = logging.getLogger(__name__)


def _cleanup_snapshot(work: Path) -> None:
    if work.exists():
        try:
            remove_directory(work)
        except OSError:
            logger.warning(
                "Transaction finished; unable to remove snapshots: %s",
                work,
                exc_info=True,
            )


def _record(path: Path) -> dict:
    if linked_path(path):
        raise UpdateError("Update record cannot be a link")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise UpdateError("Invalid update record")
    return data


def _files(root: Path, files: object) -> dict:
    if not isinstance(files, dict) or not files:
        raise UpdateError("Invalid installed file inventory")
    folded = set()
    for name, digest in files.items():
        target_path(root, name)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise UpdateError("Invalid file checksum")
        if name.casefold() in folded:
            raise UpdateError("Duplicate inventory path")
        folded.add(name.casefold())
    if any(
        parent.as_posix() in folded for name in folded for parent in Path(name).parents
    ):
        raise UpdateError("Inventory file/directory conflict")
    return files


def recover_directory(root: Path) -> bool:
    """Restore snapshots before retrying an interrupted plain ZIP installation."""
    directory = update_directory(root)
    journal = directory / "github-transaction.json"
    if not journal.exists():
        return False
    data = _record(journal)
    required = {"phase", "transaction", "originals", "inventory"}
    if not required <= data.keys() or data["phase"] not in {
        "installing",
        "committed",
        "rolled_back",
    }:
        raise UpdateError("Invalid installation journal")
    identity = data["transaction"]
    if (
        not isinstance(identity, str)
        or len(identity) != 32
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise UpdateError("Invalid transaction directory")
    work = directory / identity
    if linked_path(work) or linked_path(work / "previous"):
        raise UpdateError("Snapshot directory cannot be a link")
    if data["phase"] != "installing":
        _cleanup_snapshot(work)
        return False
    originals = data["originals"]
    if not isinstance(originals, dict) or not originals:
        raise UpdateError("Invalid transaction snapshot")
    if data["inventory"] is not None and not isinstance(data["inventory"], dict):
        raise UpdateError("Invalid previous inventory")
    for name, digest in originals.items():
        target_path(root, name)
        source = target_path(work / "previous", name)
        if digest is not None and (
            not source.is_file() or file_digest(source) != digest
        ):
            raise UpdateError(f"Damaged snapshot: {name}")
    inventory = directory / "github-installed.json"
    if linked_path(inventory):
        raise UpdateError("Inventory cannot be a link")
    for name, digest in originals.items():
        target = target_path(root, name)
        if digest is None:
            unlink_file(target)
        elif target.is_file() and file_digest(target) == digest:
            source = target_path(work / "previous", name)
            mode = stat.S_IMODE(source.stat().st_mode)
            if stat.S_IMODE(target.stat().st_mode) != mode:
                target.chmod(mode)
        else:
            _replace(target_path(work / "previous", name), target, work / "swap.tmp")
    if data["inventory"] is None:
        inventory.unlink(missing_ok=True)
    elif isinstance(data["inventory"], dict):
        write_json(inventory, data["inventory"])
    data["phase"] = "rolled_back"
    write_json(journal, data)
    logger.info("Restored interrupted installation")
    _cleanup_snapshot(work)
    return True


def install_directory(root, package, data, repository, asset_name, current_version):
    """Replace listed files; never remove unrelated local files."""
    directory = update_directory(root)
    recover_directory(root)
    files = _files(package, data["files"])
    for name, digest in files.items():
        path = target_path(package, name)
        if not path.is_file() or file_digest(path) != digest:
            raise UpdateError(f"Prepared file checksum mismatch: {name}")
    inventory = directory / "github-installed.json"
    if linked_path(inventory):
        raise UpdateError("Inventory cannot be a link")
    old = None
    old_files = {}
    if inventory.exists():
        old = _record(inventory)
        if (
            not {"repository", "asset_name", "version", "files"} <= old.keys()
            or old["repository"] != repository
            or old["asset_name"] != asset_name
        ):
            raise UpdateError("Installation belongs to another release source")
        if version_number(old["version"]) != version_number(current_version):
            raise UpdateError("Caller version does not match installed inventory")
        old_files = _files(root, old["files"])
    names = set(files) | set(old_files)
    folded = {name.casefold() for name in names}
    if len(folded) != len(names) or any(
        parent.as_posix() in folded for name in folded for parent in Path(name).parents
    ):
        raise UpdateError("Update changes file/directory or path casing")
    for name in names:
        target = target_path(root, name)
        if target.exists() and not target.is_file():
            raise UpdateError(f"Update conflicts with a local directory: {name}")
        if (
            old is not None
            and name in files
            and name not in old_files
            and target.is_file()
        ):
            raise UpdateError(f"Update conflicts with an unowned local file: {name}")
        if (
            name in old_files
            and name not in files
            and target.is_file()
            and file_digest(target) != old_files[name]
        ):
            raise UpdateError(f"Refusing to remove a locally modified file: {name}")
    work = directory / uuid.uuid4().hex
    work.mkdir()
    originals = {}
    for name in sorted(names):
        target = target_path(root, name)
        digest = None
        if target.is_file():
            snapshot = target_path(work / "previous", name)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _replace(target, snapshot, work / "swap.tmp")
            digest = file_digest(snapshot)
        originals[name] = digest
    journal = {
        "phase": "installing",
        "transaction": work.name,
        "originals": originals,
        "inventory": old,
    }
    write_json(directory / "github-transaction.json", journal)
    try:
        for name in sorted(names):
            target = target_path(root, name)
            if name in files:
                _replace(target_path(package, name), target, work / "swap.tmp")
            else:
                unlink_file(target)
        write_json(
            inventory,
            {
                "repository": repository,
                "asset_name": asset_name,
                "version": data["version"],
                "files": files,
            },
        )
        journal["phase"] = "committed"
        write_json(directory / "github-transaction.json", journal)
    except (OSError, UpdateError):
        logger.exception("Installation failed; restoring previous files")
        recover_directory(root)
        raise
    _cleanup_snapshot(work)
