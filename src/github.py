"""GitHub Release ZIP updates without application-specific packaging rules."""

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from urllib.parse import quote

from updater.directory import install_directory, recover_directory
from updater.utils import (
    MAX_PACKAGE_BYTES,
    FileLease,
    UpdateError,
    check_cancelled,
    file_digest,
    linked_path,
    relative_path,
    target_path,
    update_directory,
    version_number,
    write_json,
)


@dataclass(frozen=True)
class GitHubRelease:
    """One selected ZIP asset from a stable GitHub release."""

    tag: str
    version: str
    notes: str
    asset_id: int
    archive_url: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True)
class PreparedUpdate:
    """A complete, extracted and hashed update owned by this installation."""

    directory: Path
    version: str


class GitHubUpdater:
    """Check releases, resume ZIP downloads and install published files."""

    def __init__(
        self,
        repository: str,
        asset_name: str,
        install_dir: Path,
        current_version: str,
        archive_prefix: str = "",
    ):
        if re.fullmatch(r"[\w.-]+/[\w.-]+", repository) is None:
            raise UpdateError("Expected repository owner/name")
        if not relative_path(asset_name) or "/" in asset_name:
            raise UpdateError("Expected an asset file name")
        if archive_prefix and (
            not archive_prefix.endswith("/") or not relative_path(archive_prefix[:-1])
        ):
            raise UpdateError("Expected a relative archive directory ending in /")
        version_number(current_version)
        self.repository = repository
        self.asset_name = asset_name
        self.install_dir = Path(install_dir).resolve()
        self.current_version = current_version
        self.archive_prefix = archive_prefix

    def _url(self, tag: str) -> str:
        return (
            f"https://github.com/{self.repository}/releases/download/"
            f"{quote(tag, safe='')}/{quote(self.asset_name, safe='')}"
        )

    def check(self) -> GitHubRelease | None:
        """Check the latest stable release; does not read or write installation files."""
        import requests

        with requests.get(
            f"https://api.github.com/repos/{self.repository}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=(10, 30),
        ) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
        required = {"tag_name", "draft", "prerelease", "assets", "body"}
        if not isinstance(data, dict) or not required <= data.keys():
            raise UpdateError("Incomplete GitHub release")
        tag = data["tag_name"]
        if not isinstance(tag, str) or data["draft"] or data["prerelease"]:
            raise UpdateError("Expected a stable GitHub release")
        version = version_number(tag)
        if version.is_prerelease or version.is_devrelease:
            raise UpdateError("Expected a stable version tag")
        if version <= version_number(self.current_version):
            return None
        if not isinstance(data["assets"], list):
            raise UpdateError("Invalid GitHub assets")
        matches = []
        for asset in data["assets"]:
            if not isinstance(asset, dict) or "name" not in asset:
                raise UpdateError("Invalid GitHub asset")
            if asset["name"] == self.asset_name:
                matches.append(asset)
        if len(matches) != 1:
            raise UpdateError(f"Expected exactly one asset: {self.asset_name}")
        asset = matches[0]
        if not {"id", "browser_download_url", "size"} <= asset.keys():
            raise UpdateError("Incomplete GitHub asset")
        notes = data["body"]
        if notes is not None and not isinstance(notes, str):
            raise UpdateError("Invalid release notes")
        digest = None
        if "digest" in asset and asset["digest"] is not None:
            if (
                not isinstance(asset["digest"], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"]) is None
            ):
                raise UpdateError("Invalid asset SHA-256 digest")
            digest = asset["digest"][7:]
        release = GitHubRelease(
            tag,
            str(version),
            notes or "",
            asset["id"],
            asset["browser_download_url"],
            asset["size"],
            digest,
        )
        self._validate_release(release)
        return release

    def _validate_release(self, release: GitHubRelease) -> None:
        if (
            not isinstance(release.tag, str)
            or version_number(release.tag) != version_number(release.version)
            or version_number(release.version) <= version_number(self.current_version)
            or release.archive_url != self._url(release.tag)
            or type(release.asset_id) is not int
            or release.asset_id <= 0
            or type(release.size) is not int
            or not 0 < release.size <= MAX_PACKAGE_BYTES
            or (
                release.sha256 is not None
                and re.fullmatch(r"[0-9a-f]{64}", release.sha256) is None
            )
        ):
            raise UpdateError("Invalid release asset")

    def _work(self, release: GitHubRelease) -> Path:
        asset = asdict(release)
        del asset["notes"]
        identity = json.dumps(
            [self.repository, self.asset_name, self.archive_prefix, asset],
            sort_keys=True,
        ).encode()
        return update_directory(self.install_dir) / (
            "github-" + hashlib.sha256(identity).hexdigest()
        )

    def download(
        self,
        release: GitHubRelease,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Event | None = None,
    ) -> PreparedUpdate:
        """Resume a download; interruption/cancellation preserves downloaded bytes."""
        self._validate_release(release)
        check_cancelled(cancelled)
        directory = update_directory(self.install_dir)
        with FileLease(directory / "github.lock"):
            work = self._work(release)
            if linked_path(work):
                raise UpdateError("Download workspace cannot be a link")
            work.mkdir(exist_ok=True)
            archive = work / "archive.zip"
            partial = work / "archive.partial"
            for path in (archive, partial, work / "http.json", work / "package"):
                if linked_path(path):
                    raise UpdateError("Download cache cannot contain links")
            if not archive.is_file():
                self._download(
                    release, partial, work / "http.json", progress, cancelled
                )
                self._verify_archive(partial, release)
                os.replace(partial, archive)
            else:
                self._verify_archive(archive, release)
                if progress is not None:
                    progress(release.size, release.size)
            self._extract(archive, work, release, cancelled)
            return PreparedUpdate(work, release.version)

    def _verify_archive(self, archive: Path, release: GitHubRelease) -> None:
        if archive.stat().st_size != release.size or (
            release.sha256 is not None and file_digest(archive) != release.sha256
        ):
            archive.unlink()
            raise UpdateError("Downloaded asset failed size/SHA-256 verification")

    def _download(self, release, partial, metadata, progress, cancelled):
        import requests

        offset = partial.stat().st_size if partial.is_file() else 0
        validator = ""
        if metadata.is_file():
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or "validator" not in data
                or not isinstance(data["validator"], str)
            ):
                raise UpdateError("Invalid download metadata")
            validator = data["validator"]
        if offset > release.size or (offset and not validator and not release.sha256):
            offset = 0
        if offset == release.size:
            check_cancelled(cancelled)
            return
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if validator:
                headers["If-Range"] = validator
        with requests.get(
            release.archive_url, headers=headers, stream=True, timeout=(10, 30)
        ) as response:
            response.raise_for_status()
            if (
                "Content-Encoding" in response.headers
                and response.headers["Content-Encoding"] != "identity"
            ):
                raise UpdateError("Encoded response cannot be resumed by byte offset")
            if response.status_code == 206:
                if (
                    "Content-Range" not in response.headers
                    or response.headers["Content-Range"]
                    != f"bytes {offset}-{release.size - 1}/{release.size}"
                ):
                    raise UpdateError("Invalid range response")
            elif response.status_code == 200:
                offset = 0
            else:
                raise UpdateError("Unexpected download response")
            remote_validator = ""
            if "ETag" in response.headers and not response.headers["ETag"].startswith(
                "W/"
            ):
                remote_validator = response.headers["ETag"]
            elif "Last-Modified" in response.headers:
                remote_validator = response.headers["Last-Modified"]
            if offset and validator and remote_validator != validator:
                partial.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise UpdateError("Remote asset changed; retry download")
            if "Content-Length" in response.headers and response.headers[
                "Content-Length"
            ] != str(release.size - offset):
                raise UpdateError("Download response length does not match the asset")
            write_json(metadata, {"validator": remote_validator})
            with partial.open("ab" if offset else "wb") as output:
                try:
                    if progress is not None:
                        progress(offset, release.size)
                    for chunk in response.iter_content(65536):
                        check_cancelled(cancelled)
                        if offset + len(chunk) > release.size:
                            raise UpdateError("Download exceeds asset size")
                        output.write(chunk)
                        offset += len(chunk)
                        if progress is not None:
                            progress(offset, release.size)
                    check_cancelled(cancelled)
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            if offset != release.size:
                raise UpdateError("Download interrupted before asset was complete")

    def _extract(self, archive, work, release, cancelled):
        package = work / "package"
        # Rebuild staging from the verified ZIP; a cancelled extraction never becomes installable.
        marker = work / "prepared.json"
        marker.unlink(missing_ok=True)
        if package.exists():
            shutil.rmtree(package)
        package.mkdir()
        files = {}
        try:
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                if (
                    len(entries) > 20000
                    or sum(item.file_size for item in entries) > MAX_PACKAGE_BYTES
                ):
                    raise UpdateError("ZIP expands beyond package limits")
                names = set()
                members = []
                for item in entries:
                    name = item.filename
                    if (
                        not name.startswith(self.archive_prefix)
                        or item.flag_bits & 1
                        or stat.S_ISLNK(item.external_attr >> 16)
                    ):
                        raise UpdateError(f"Invalid ZIP entry: {name}")
                    name = name[len(self.archive_prefix) :]
                    if item.is_dir():
                        if name and not relative_path(name.rstrip("/")):
                            raise UpdateError(f"Unsafe ZIP directory: {name}")
                        continue
                    if not relative_path(name) or name.casefold() in names:
                        raise UpdateError(f"Unsafe or duplicate ZIP entry: {name}")
                    names.add(name.casefold())
                    members.append((item, name))
                if not names or any(
                    parent.as_posix() in names
                    for name in names
                    for parent in Path(name).parents
                ):
                    raise UpdateError("ZIP contains empty or conflicting file paths")
                for item, name in members:
                    check_cancelled(cancelled)
                    target = target_path(package, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(item) as input_file, target.open("wb") as output:
                        while chunk := input_file.read(65536):
                            check_cancelled(cancelled)
                            output.write(chunk)
                    mode = stat.S_IMODE(item.external_attr >> 16) & 0o777
                    if mode:
                        target.chmod(mode)
                    files[name] = file_digest(target)
        except zipfile.BadZipFile as exc:
            archive.unlink(missing_ok=True)
            raise UpdateError("Invalid ZIP asset; retry download") from exc
        write_json(
            marker,
            {
                "repository": self.repository,
                "asset_name": self.asset_name,
                "install_dir": str(self.install_dir),
                "version": release.version,
                "files": files,
            },
        )

    def install(self, prepared: PreparedUpdate) -> None:
        """Install from an independent process after the application has exited."""
        directory = update_directory(self.install_dir)
        work = prepared.directory.resolve()
        if (
            work.parent != directory
            or not re.fullmatch(r"github-[0-9a-f]{64}", work.name)
            or linked_path(prepared.directory)
        ):
            raise UpdateError("Prepared update belongs to another workspace")
        with FileLease(directory / "github.lock"):
            marker = work / "prepared.json"
            if (
                not marker.is_file()
                or linked_path(marker)
                or linked_path(work / "package")
            ):
                raise UpdateError("Update has not finished downloading")
            data = json.loads(marker.read_text(encoding="utf-8"))
            required = {"repository", "asset_name", "install_dir", "version", "files"}
            if (
                not isinstance(data, dict)
                or not required <= data.keys()
                or data["repository"] != self.repository
                or data["asset_name"] != self.asset_name
                or data["install_dir"] != str(self.install_dir)
                or data["version"] != prepared.version
            ):
                raise UpdateError("Prepared update identity mismatch")
            if version_number(prepared.version) <= version_number(self.current_version):
                raise UpdateError("Update version must be newer")
            install_directory(
                self.install_dir,
                work / "package",
                data,
                self.repository,
                self.asset_name,
                self.current_version,
            )
            self.current_version = prepared.version

    def recover(self) -> bool:
        """Restore an interrupted installation; no release or frozen executable required."""
        directory = update_directory(self.install_dir)
        with FileLease(directory / "github.lock"):
            return recover_directory(self.install_dir)

    def discard(self, release: GitHubRelease) -> None:
        """Explicitly remove this asset's download cache, including saved partial bytes."""
        self._validate_release(release)
        directory = update_directory(self.install_dir)
        with FileLease(directory / "github.lock"):
            work = self._work(release)
            if linked_path(work):
                raise UpdateError("Download workspace cannot be a link")
            if work.exists():
                shutil.rmtree(work)
