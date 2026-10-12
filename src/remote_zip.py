"""Seekable remote ZIP backed by resumable, validated HTTP ranges."""

import io
import json
import os
from pathlib import Path

from updater.utils import (
    UpdateError,
    check_cancelled,
    linked_path,
    remove_directory,
    write_json,
)

BLOCK = 65536


class RangeUnavailable(UpdateError):
    """Server cannot provide a stable range-addressable archive."""


class RangeInterrupted(UpdateError):
    """Network interruption that ZipFile must not mistake for an invalid ZIP."""


class RemoteArchive(io.RawIOBase):
    """Read only requested ZIP blocks; keep partial blocks across retries."""

    def __init__(self, release, cache: Path, cancelled=None, progress=None):
        import requests

        super().__init__()
        self.release = release
        self.cache = cache
        self.cancelled = cancelled
        self.progress = progress
        self.position = 0
        self.received = 0
        self.session = requests.Session()
        self.requests = requests
        self.session.headers["Accept-Encoding"] = "identity"
        try:
            self._identify()
        except BaseException:
            self.session.close()
            raise

    def _identify(self):
        check_cancelled(self.cancelled)
        with self.session.head(
            self.release.archive_url, allow_redirects=True, timeout=(10, 30)
        ) as response:
            if response.status_code in {405, 501}:
                raise RangeUnavailable("Server does not support archive HEAD")
            response.raise_for_status()
            # HTTP metadata is optional; missing values disable partial transfers.
            if response.headers.get("Content-Length", "") != str(self.release.size):
                raise RangeUnavailable("Archive size unavailable or changed")
            etag = response.headers.get("ETag", "")
            self.validator = (
                etag
                if etag and not etag.startswith("W/")
                else response.headers.get("Last-Modified", "")
            )
            if not self.validator:
                raise RangeUnavailable("Archive has no stable range validator")
            self.download_url = response.url
        if linked_path(self.cache):
            raise UpdateError("Range cache cannot be a link")
        identity = {"size": self.release.size, "validator": self.validator}
        state = self.cache / "state.json"
        if linked_path(state):
            raise UpdateError("Range cache metadata cannot be a link")
        stored = (
            json.loads(state.read_text(encoding="utf-8")) if state.is_file() else None
        )
        if self.cache.exists() and stored != identity:
            remove_directory(self.cache)
        self.cache.mkdir(exist_ok=True)
        write_json(state, identity)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset
        if whence == 1:
            position += self.position
        elif whence == 2:
            position += self.release.size
        elif whence != 0:
            raise ValueError("Invalid seek mode")
        if position < 0 or position > self.release.size:
            raise UpdateError("ZIP seek outside archive")
        self.position = position
        return position

    def _response(self, start, end):
        try:
            return self.session.get(
                self.download_url,
                headers={"Range": f"bytes={start}-{end}", "If-Range": self.validator},
                stream=True,
                timeout=(10, 30),
            )
        except self.requests.RequestException as exc:
            raise RangeInterrupted(f"ZIP range request interrupted: {exc}") from exc

    def _block(self, start):
        end = min(start + BLOCK, self.release.size) - 1
        path = self.cache / f"{start}.bin"
        if linked_path(path):
            raise UpdateError("Range cache block cannot be a link")
        length = end - start + 1
        have = path.stat().st_size if path.is_file() else 0
        if have > length:
            path.unlink()
            have = 0
        if have == length:
            return path.read_bytes()
        check_cancelled(self.cancelled)
        with self._response(start + have, end) as response:
            response.raise_for_status()
            if response.status_code == 200:
                raise RangeUnavailable("Server ignored the range or asset changed")
            if (
                response.status_code != 206
                or response.headers.get("Content-Range", "")
                != f"bytes {start + have}-{end}/{self.release.size}"
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or response.headers.get("Content-Length", str(length - have))
                != str(length - have)
            ):
                raise UpdateError("Invalid ZIP range response")
            returned = response.headers.get("ETag", "")
            if not returned or returned.startswith("W/"):
                returned = response.headers.get("Last-Modified", "")
            if returned != self.validator:
                raise UpdateError("Archive changed during partial download")
            with path.open("ab" if have else "wb") as output:
                try:
                    for chunk in response.iter_content(8192):
                        check_cancelled(self.cancelled)
                        if have + len(chunk) > length:
                            raise UpdateError("Range exceeds requested block")
                        output.write(chunk)
                        have += len(chunk)
                        self.received += len(chunk)
                        if self.progress is not None:
                            self.progress(self.received, self.release.size)
                    check_cancelled(self.cancelled)
                except self.requests.RequestException as exc:
                    raise RangeInterrupted(
                        f"ZIP range transfer interrupted: {exc}"
                    ) from exc
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            if have != length:
                raise UpdateError("Range download ended before block was complete")
        return path.read_bytes()

    def read(self, size=-1):
        if size < 0:
            size = self.release.size - self.position
        size = min(size, self.release.size - self.position)
        if size > 8 * 1024**2:
            raise UpdateError("ZIP metadata exceeds read limit")
        result = bytearray()
        while len(result) < size:
            check_cancelled(self.cancelled)
            start = self.position // BLOCK * BLOCK
            try:
                block = self._block(start)
            except OSError as exc:
                # ZipFile catches OSError while finding its directory; retain the actual cause.
                raise RangeInterrupted(f"ZIP range I/O interrupted: {exc}") from exc
            offset = self.position - start
            count = min(size - len(result), len(block) - offset)
            if count <= 0:
                raise UpdateError("Cached range block changed while reading")
            result.extend(block[offset : offset + count])
            self.position += count
        return bytes(result)

    def close(self):
        self.session.close()
        super().close()


def manifest_files(data, version, manifest_name, root):
    """Validate optional schema-1 per-file SHA-256 manifests."""
    from updater.directory import _files
    from updater.utils import version_number

    if (
        not isinstance(data, dict)
        or not {"schema", "version", "files"} <= data.keys()
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or not isinstance(data["version"], str)
        or version_number(data["version"]) != version_number(version)
    ):
        raise UpdateError("Invalid partial-update manifest")
    files = _files(root, data["files"])
    if any(name.casefold() == manifest_name.casefold() for name in files):
        raise UpdateError("Manifest cannot hash itself")
    return files
