"""Plain ZIP updates and resumable transfers against a real local HTTP server."""

import hashlib
import http.server
import io
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from updater import (
    GitHubRelease,
    GitHubUpdater,
    UpdateCancelled,
    UpdateError,
    directory,
)
from updater.remote_zip import RangeInterrupted
from updater.utils import file_digest


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("App/", b"")
        for name, content in files.items():
            entry = zipfile.ZipInfo("placeholder")
            entry.filename = "App/" + name
            entry.orig_filename = entry.filename
            bundle.writestr(entry, content)
    return output.getvalue()


class TestGitHubUpdater(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.client = GitHubUpdater(
            "example/application", "App.zip", self.root, "1.0.0", "App/"
        )
        self.content = archive(
            {"app.exe": os.urandom(200000), "config/app.example.yml": b"new template"}
        )
        self.etag = '"one"'
        self.supports_range = True
        self.ignore_if_range = False
        self.disconnect_once = False
        self.send_validator = True
        self.partial_head = False
        self.sent = 0
        self.requests = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                if not fixture.partial_head:
                    self.send_error(501)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(fixture.content)))
                if fixture.send_validator:
                    self.send_header("ETag", fixture.etag)
                self.end_headers()

            def do_GET(self):
                # Range is optional on the initial request.
                requested = self.headers.get("Range", None)
                fixture.requests.append(requested)
                start = 0
                end = len(fixture.content) - 1
                if (
                    requested
                    and fixture.supports_range
                    and (
                        fixture.ignore_if_range
                        or self.headers["If-Range"] == fixture.etag
                    )
                ):
                    first, last = requested.removeprefix("bytes=").split("-", 1)
                    start = int(first)
                    if last:
                        end = int(last)
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{end}/{len(fixture.content)}",
                    )
                else:
                    self.send_response(200)
                if fixture.send_validator:
                    self.send_header("ETag", fixture.etag)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if fixture.disconnect_once:
                    fixture.disconnect_once = False
                    count = min(100000, end - start + 1)
                    if requested and requested.split("-", 1)[1]:
                        count = max(1, count // 2)
                    self.wfile.write(fixture.content[start : start + count])
                    fixture.sent += count
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(fixture.content[start : end + 1])
                except (BrokenPipeError, ConnectionResetError) as exc:
                    logging.getLogger(__name__).debug(
                        "Client rejected range response: %s", exc
                    )
                else:
                    fixture.sent += end - start + 1

            def log_message(self, format, *args):
                # HTTP requests are recorded above for assertions.
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/App.zip"
        self.enterContext(patch.object(self.client, "_url", return_value=self.url))
        self.release = self.make_release()

    def make_release(self, version="1.1.0", asset_id=10):
        return GitHubRelease(
            "v" + version,
            version,
            "notes",
            asset_id,
            self.url,
            len(self.content),
            hashlib.sha256(self.content).hexdigest(),
        )

    def partial_fixture(self):
        self.partial_head = True
        self.client.manifest_name = "update-manifest.json"
        files = {
            "unchanged.bin": os.urandom(1024 * 1024),
            "changed.bin": os.urandom(300000),
        }
        (self.root / "unchanged.bin").write_bytes(files["unchanged.bin"])
        (self.root / "unchanged.bin").chmod(0o600)
        manifest = {
            "schema": 1,
            "version": "1.1.0",
            "files": {
                name: hashlib.sha256(body).hexdigest() for name, body in files.items()
            },
        }
        self.content = archive(
            {**files, "update-manifest.json": json.dumps(manifest).encode()}
        )
        self.release = self.make_release()
        return files

    def test_partial_download_reuses_local_bytes_and_installs_without_replacing_them(
        self,
    ):
        files = self.partial_fixture()
        inode = (self.root / "unchanged.bin").stat().st_ino
        prepared = self.client.download(self.release)
        self.assertLess(self.sent, len(self.content) // 2)
        self.assertTrue(all(request is not None for request in self.requests))
        self.client.install(prepared)
        self.assertEqual((self.root / "changed.bin").read_bytes(), files["changed.bin"])
        self.assertEqual((self.root / "unchanged.bin").stat().st_ino, inode)

    def test_partial_cancel_resumes_cached_blocks_and_staging(self):
        self.partial_fixture()
        cancelled = Event()

        def progress(received, total):
            if received >= 100000:
                cancelled.set()

        with self.assertRaises(UpdateCancelled):
            self.client.download(self.release, progress=progress, cancelled=cancelled)
        before = self.sent
        requested_before = list(self.requests)
        self.client.download(self.release)
        self.assertLess(self.sent, len(self.content) // 2)
        self.assertGreater(self.sent, before)
        self.assertTrue(
            set(requested_before).isdisjoint(self.requests[len(requested_before) :])
        )

    def test_partial_falls_back_when_manifest_or_range_support_is_missing(self):
        self.partial_head = True
        self.client.manifest_name = "update-manifest.json"
        prepared = self.client.download(self.release)
        self.assertTrue((prepared.directory / "archive.zip").is_file())
        self.client.discard(self.release)
        self.partial_fixture()
        self.supports_range = False
        prepared = self.client.download(self.release)
        self.assertTrue((prepared.directory / "archive.zip").is_file())

    def test_partial_rejects_false_file_hash_without_installing(self):
        files = self.partial_fixture()
        files["changed.bin"] = b"wrong"
        self.content = archive(
            {
                **files,
                "update-manifest.json": json.dumps(
                    {
                        "schema": 1,
                        "version": "1.1.0",
                        "files": {
                            name: hashlib.sha256(body).hexdigest()
                            if name != "changed.bin"
                            else "0" * 64
                            for name, body in files.items()
                        },
                    }
                ).encode(),
            }
        )
        self.release = self.make_release()
        with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
            self.client.download(self.release)
        self.assertFalse((self.client._work(self.release) / "prepared.json").exists())
        self.assertFalse((self.root / "changed.bin").exists())

    def test_partial_network_disconnect_keeps_ranges_for_retry(self):
        self.partial_fixture()
        self.disconnect_once = True
        with self.assertRaises(RangeInterrupted):
            self.client.download(self.release)
        self.assertTrue((self.client._work(self.release) / "ranges").is_dir())
        prepared = self.client.download(self.release)
        self.assertTrue((prepared.directory / "prepared.json").is_file())
        self.assertLess(self.sent, len(self.content) // 2)

    def test_partial_reloads_corrupt_cached_range(self):
        self.partial_fixture()
        prepared = self.client.download(self.release)
        cached = list((prepared.directory / "ranges").glob("*.bin"))
        for path in cached:
            path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises((UpdateError, zipfile.BadZipFile)):
            self.client.download(self.release)
        self.assertFalse((prepared.directory / "ranges").exists())
        self.client.download(self.release)

    def test_full_fallback_still_rejects_bad_manifest_hash(self):
        self.partial_fixture()
        self.supports_range = False
        files = {"changed.bin": b"wrong"}
        manifest = {"schema": 1, "version": "1.1.0", "files": {"changed.bin": "0" * 64}}
        self.content = archive(
            {**files, "update-manifest.json": json.dumps(manifest).encode()}
        )
        self.release = self.make_release()
        with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
            self.client.download(self.release)
        self.assertFalse((self.client._work(self.release) / "prepared.json").exists())

    def pause(self):
        cancelled = Event()

        def progress(received, total):
            if received >= 65536:
                cancelled.set()

        with self.assertRaises(UpdateCancelled):
            self.client.download(self.release, progress=progress, cancelled=cancelled)
        work = self.client._work(self.release)
        self.assertEqual((work / "archive.partial").stat().st_size, 65536)
        self.assertFalse((work / "prepared.json").exists())
        return work

    def test_check_is_independent_of_installation_and_frozen_state(self):
        asset = {
            "id": 10,
            "name": "App.zip",
            "size": len(self.content),
            "browser_download_url": self.url,
            "digest": "sha256:" + self.release.sha256,
        }
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status_code = 200
        response.json.return_value = {
            "tag_name": "1.1.0",
            "draft": False,
            "prerelease": False,
            "body": None,
            "assets": [asset],
        }
        with patch("requests.get", return_value=response):
            result = self.client.check()
        self.assertEqual(result.version, "1.1.0")
        self.assertEqual(result.sha256, self.release.sha256)
        self.assertEqual(result.notes, "")
        self.assertFalse((self.root / ".update").exists())

    def test_no_release_returns_none(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status_code = 404
        with patch("requests.get", return_value=response):
            self.assertIsNone(self.client.check())

    def test_new_client_resumes_cancelled_download(self):
        self.pause()
        client = GitHubUpdater(
            "example/application", "App.zip", self.root, "1.0.0", "App/"
        )
        with patch.object(client, "_url", return_value=self.url):
            prepared = client.download(self.release)
        self.assertEqual(self.requests, [None, "bytes=65536-"])
        self.assertEqual(
            (prepared.directory / "package/config/app.example.yml").read_bytes(),
            b"new template",
        )

    def test_network_disconnect_preserves_received_bytes(self):
        self.disconnect_once = True
        with self.assertRaises(requests.RequestException):
            self.client.download(self.release)
        work = self.client._work(self.release)
        self.assertEqual((work / "archive.partial").stat().st_size, 65536)
        self.client.download(self.release)
        self.assertEqual(self.requests, [None, "bytes=65536-"])

    def test_missing_validator_and_digest_restart_safely(self):
        self.send_validator = False
        self.release = replace(self.release, sha256=None)
        self.pause()
        self.client.download(self.release)
        self.assertEqual(self.requests, [None, None])

    def test_changing_notes_does_not_discard_partial_download(self):
        self.pause()
        self.client.download(replace(self.release, notes="edited notes"))
        self.assertEqual(self.requests, [None, "bytes=65536-"])

    def test_explicit_discard_removes_partial_workspace(self):
        work = self.pause()
        self.client.discard(self.release)
        self.assertFalse(work.exists())
        self.client.download(self.release)
        self.assertEqual(self.requests, [None, None])

    def test_discard_can_clean_installed_and_older_releases(self):
        prepared = self.client.download(self.release)
        self.client.install(prepared)
        for current_version in ("1.1.0", "1.2.0"):
            with self.subTest(current_version=current_version):
                prepared.directory.mkdir(exist_ok=True)
                (prepared.directory / "archive.partial").write_bytes(b"saved bytes")
                self.client.current_version = current_version
                self.client.discard(self.release)
                self.assertFalse(prepared.directory.exists())
        self.assertTrue((self.root / "app.exe").is_file())
        self.assertTrue((self.root / ".update/github-installed.json").is_file())

    def test_discard_still_rejects_foreign_assets(self):
        work = self.pause()
        foreign = replace(
            self.release, archive_url="https://github.com/other/repo/App.zip"
        )
        with self.assertRaisesRegex(UpdateError, "Invalid release asset"):
            self.client.discard(foreign)
        self.assertTrue(work.is_dir())

    def test_download_still_rejects_installed_and_older_releases(self):
        for current_version in ("1.1.0", "1.2.0"):
            with self.subTest(current_version=current_version):
                self.client.current_version = current_version
                with self.assertRaisesRegex(UpdateError, "newer"):
                    self.client.download(self.release)
        self.assertEqual(self.requests, [])

    def test_full_response_restarts_partial_download(self):
        self.pause()
        self.supports_range = False
        prepared = self.client.download(self.release)
        self.assertEqual(
            file_digest(prepared.directory / "archive.zip"), self.release.sha256
        )
        self.assertEqual(self.requests, [None, "bytes=65536-"])

    def test_changed_validator_cannot_mix_bytes(self):
        work = self.pause()
        self.etag = '"two"'
        self.ignore_if_range = True
        with self.assertRaisesRegex(UpdateError, "changed"):
            self.client.download(self.release)
        self.assertFalse((work / "archive.partial").exists())
        self.client.download(self.release)
        self.assertEqual(self.requests[-1], None)

    def test_digest_mismatch_discards_download(self):
        release = replace(self.release, sha256="0" * 64)
        with self.assertRaisesRegex(UpdateError, "SHA-256"):
            self.client.download(release)
        work = self.client._work(release)
        self.assertFalse((work / "archive.partial").exists())
        self.assertFalse((work / "prepared.json").exists())

    def test_asset_without_digest_uses_zip_crc(self):
        prepared = self.client.download(replace(self.release, sha256=None))
        self.assertTrue((prepared.directory / "prepared.json").is_file())

    def test_completed_download_is_reused(self):
        first = self.client.download(self.release)
        second = self.client.download(self.release)
        self.assertEqual(first, second)
        self.assertEqual(self.requests, [None])

    def test_plain_zip_install_keeps_local_configuration(self):
        (self.root / "app.exe").write_bytes(b"old program")
        (self.root / "config").mkdir()
        (self.root / "config/app.yml").write_bytes(b"user settings")
        (self.root / "logs").mkdir()
        (self.root / "logs/run.log").write_bytes(b"old log")
        prepared = self.client.download(self.release)
        self.client.install(prepared)
        self.assertEqual(
            (self.root / "config/app.example.yml").read_bytes(), b"new template"
        )
        self.assertEqual((self.root / "config/app.yml").read_bytes(), b"user settings")
        self.assertEqual((self.root / "logs/run.log").read_bytes(), b"old log")
        self.assertFalse((self.root / "version.json").exists())
        self.assertFalse((self.root / "update-manifest.json").exists())
        self.assertEqual(self.client.current_version, "1.1.0")

    def test_inventory_removes_only_owned_stale_files(self):
        first = self.client.download(self.release)
        self.client.install(first)
        (self.root / "unrelated.txt").write_bytes(b"keep")
        self.content = archive({"config/app.example.yml": b"second template"})
        self.release = self.make_release("1.2.0", 11)
        self.client.install(self.client.download(self.release))
        self.assertFalse((self.root / "app.exe").exists())
        self.assertEqual((self.root / "unrelated.txt").read_bytes(), b"keep")

    def test_new_published_path_cannot_overwrite_unowned_user_file(self):
        self.client.install(self.client.download(self.release))
        old_program = (self.root / "app.exe").read_bytes()
        old_inventory = (self.root / ".update/github-installed.json").read_bytes()
        (self.root / "notes.txt").write_bytes(b"USER NOTES")
        self.content = archive(
            {"app.exe": b"new program", "notes.txt": b"RELEASE NOTES"}
        )
        self.release = self.make_release("1.2.0", 11)
        prepared = self.client.download(self.release)
        with self.assertRaisesRegex(UpdateError, "unowned local file"):
            self.client.install(prepared)
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"USER NOTES")
        self.assertEqual((self.root / "app.exe").read_bytes(), old_program)
        self.assertEqual(
            (self.root / ".update/github-installed.json").read_bytes(), old_inventory
        )
        self.assertEqual(self.client.current_version, "1.1.0")

    def test_modified_obsolete_file_is_not_deleted(self):
        self.client.install(self.client.download(self.release))
        (self.root / "app.exe").write_bytes(b"local replacement")
        self.content = archive({"config/app.example.yml": b"second"})
        self.release = self.make_release("1.2.0", 11)
        with self.assertRaisesRegex(UpdateError, "locally modified"):
            self.client.install(self.client.download(self.release))
        self.assertEqual((self.root / "app.exe").read_bytes(), b"local replacement")

    def test_install_failure_rolls_back_files_and_inventory(self):
        (self.root / "app.exe").write_bytes(b"old program")
        prepared = self.client.download(self.release)
        original = directory._replace

        def fail_once(source, target, temporary):
            if "package" in source.parts and target.name.endswith(".yml"):
                raise PermissionError("locked file")
            return original(source, target, temporary)

        with (
            patch.object(directory, "_replace", side_effect=fail_once),
            self.assertRaises(PermissionError),
        ):
            self.client.install(prepared)
        self.assertEqual((self.root / "app.exe").read_bytes(), b"old program")
        self.assertFalse((self.root / "config/app.example.yml").exists())
        self.assertFalse((self.root / ".update/github-installed.json").exists())
        self.assertFalse(self.client.recover())
        journal = json.loads(
            (self.root / ".update/github-transaction.json").read_text()
        )
        self.assertFalse((self.root / ".update" / journal["transaction"]).exists())

    def test_rollback_skips_unchanged_locked_original(self):
        self.content = archive(
            {"a.exe": b"new a", "b.dll": b"old b", "c.exe": b"new c"}
        )
        self.release = self.make_release()
        (self.root / "a.exe").write_bytes(b"old a")
        (self.root / "b.dll").write_bytes(b"old b")
        (self.root / "c.exe").write_bytes(b"old c")
        prepared = self.client.download(self.release)
        (prepared.directory / "package/b.dll").chmod(
            (self.root / "b.dll").stat().st_mode
        )
        real_replace = directory._replace
        locked_attempts = []

        def occupied(source, target, temporary):
            if target == self.root / "b.dll":
                locked_attempts.append(source)
                raise PermissionError("DLL locked")
            if source == prepared.directory / "package/c.exe":
                raise PermissionError("changed executable locked")
            return real_replace(source, target, temporary)

        with patch.object(directory, "_replace", side_effect=occupied):
            with (
                self.assertLogs(directory.__name__, level="ERROR"),
                self.assertRaises(PermissionError),
            ):
                self.client.install(prepared)
            self.assertFalse(self.client.recover())
        self.assertEqual(len(locked_attempts), 0)
        self.assertEqual((self.root / "a.exe").read_bytes(), b"old a")
        self.assertEqual((self.root / "b.dll").read_bytes(), b"old b")
        self.assertEqual((self.root / "c.exe").read_bytes(), b"old c")
        journal = json.loads(
            (self.root / ".update/github-transaction.json").read_text()
        )
        self.assertEqual(journal["phase"], "rolled_back")
        self.assertFalse((self.root / ".update/github-installed.json").exists())

    def test_rollback_restores_permissions_when_content_is_unchanged(self):
        (self.root / "a.sh").write_bytes(b"same content")
        (self.root / "a.sh").chmod(0o755)
        (self.root / "b.txt").write_bytes(b"old b")
        original_mode = stat.S_IMODE((self.root / "a.sh").stat().st_mode)
        self.content = archive({"a.sh": b"same content", "b.txt": b"new b"})
        self.release = self.make_release()
        prepared = self.client.download(self.release)
        (prepared.directory / "package/a.sh").chmod(0o444)
        real_replace = directory._replace

        def failure(source, target, temporary):
            if source == prepared.directory / "package/b.txt":
                raise PermissionError("second file is locked")
            return real_replace(source, target, temporary)

        with (
            patch.object(directory, "_replace", side_effect=failure),
            self.assertLogs(directory.__name__, level="ERROR"),
            self.assertRaises(PermissionError),
        ):
            self.client.install(prepared)
        self.assertEqual((self.root / "a.sh").read_bytes(), b"same content")
        self.assertEqual(
            stat.S_IMODE((self.root / "a.sh").stat().st_mode), original_mode
        )
        self.assertFalse(self.client.recover())

    def test_prepared_tampering_is_rejected_before_writes(self):
        prepared = self.client.download(self.release)
        (prepared.directory / "package/app.exe").write_bytes(b"tampered")
        with self.assertRaisesRegex(UpdateError, "checksum"):
            self.client.install(prepared)
        self.assertFalse((self.root / "app.exe").exists())

    def test_unsafe_zip_paths_are_rejected(self):
        for name in (
            "../escape",
            "../.update/file",
            "x\\y",
            "file.bak",
            "CON",
            "trailing.",
        ):
            with self.subTest(name=name):
                self.content = archive({name: b"unsafe"})
                self.release = self.make_release(asset_id=self.release.asset_id + 1)
                with self.assertRaises(UpdateError):
                    self.client.download(self.release)
                self.assertFalse(
                    (self.client._work(self.release) / "prepared.json").exists()
                )

    def test_bad_source_and_prefix_are_rejected(self):
        for repository, asset, prefix in (
            ("bad", "App.zip", ""),
            ("a/b", "../App.zip", ""),
            ("a/b", "App.zip", "../App/"),
        ):
            with (
                self.subTest(repository=repository, asset=asset, prefix=prefix),
                self.assertRaises(UpdateError),
            ):
                GitHubUpdater(repository, asset, self.root, "1.0.0", prefix)

    def test_stale_caller_cannot_install_over_newer_inventory(self):
        self.client.install(self.client.download(self.release))
        stale = GitHubUpdater(
            "example/application", "App.zip", self.root, "1.0.0", "App/"
        )
        self.content = archive({"app.exe": b"v2"})
        self.release = self.make_release("1.2.0", 11)
        prepared = self.client.download(self.release)
        with self.assertRaisesRegex(UpdateError, "Caller version"):
            stale.install(prepared)

    def test_recover_replays_durable_snapshot(self):
        self.client.install(self.client.download(self.release))
        original = (self.root / "app.exe").read_bytes()
        work = self.root / ".update" / ("a" * 32)
        (work / "previous").mkdir(parents=True)
        (work / "previous/app.exe").write_bytes(original)
        inventory = json.loads(
            (self.root / ".update/github-installed.json").read_text()
        )
        (self.root / "app.exe").write_bytes(b"interrupted update")
        (self.root / ".update/github-transaction.json").write_text(
            json.dumps(
                {
                    "phase": "installing",
                    "transaction": work.name,
                    "originals": {"app.exe": hashlib.sha256(original).hexdigest()},
                    "inventory": inventory,
                }
            )
        )
        self.assertTrue(self.client.recover())
        self.assertEqual((self.root / "app.exe").read_bytes(), original)
        self.assertFalse(self.client.recover())
