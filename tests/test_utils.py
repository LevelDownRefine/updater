"""Shared filesystem helpers operate without a release policy or installer."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from updater import utils


class TestFilesystemUtils(unittest.TestCase):
    def test_shared_leases_prevent_exclusive_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "update.lock"
            with (
                utils.FileLease(path, shared=True),
                utils.FileLease(path, shared=True),
                self.assertRaises(utils.UpdateBusyError),
                utils.FileLease(path),
            ):
                self.fail("Exclusive lease must not acquire a shared lock")
            with utils.FileLease(path):
                self.assertTrue(path.is_file())

    def test_lease_blocks_another_process_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "update.lock"
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from updater.utils import FileLease; "
                "with_lock = FileLease(Path(sys.argv[1])); "
                "with_lock.__enter__(); with_lock.__exit__(None, None, None)",
                str(path),
            ]
            with utils.FileLease(path):
                blocked = subprocess.run(
                    command, capture_output=True, text=True, timeout=10
                )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("UpdateBusyError", blocked.stderr)
            available = subprocess.run(
                command, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(available.returncode, 0, available.stderr)

    def test_transient_occupation_retries_without_losing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, temporary = root / "new", root / "old", root / "swap"
            source.write_bytes(b"new program")
            target.write_bytes(b"old program")
            real_replace = utils.os.replace
            attempts = []

            def occupied(source_path, target_path):
                attempts.append(target_path)
                if len(attempts) <= 2:
                    raise PermissionError(13, "temporarily occupied")
                return real_replace(source_path, target_path)

            with (
                patch.object(utils.os, "replace", side_effect=occupied),
                patch.object(utils.time, "sleep") as sleep,
                self.assertLogs(utils.__name__, level="WARNING"),
            ):
                utils._replace(source, target, temporary)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(target.read_bytes(), b"new program")
            self.assertEqual(source.read_bytes(), b"new program")
            self.assertFalse(temporary.exists())

    def test_failed_replace_preserves_source_and_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, temporary = root / "new", root / "old", root / "swap"
            source.write_bytes(b"new program")
            target.write_bytes(b"old program")
            with (
                patch.object(
                    utils.os,
                    "replace",
                    side_effect=FileNotFoundError("missing destination"),
                ),
                patch.object(utils.time, "sleep") as sleep,
                self.assertRaises(FileNotFoundError),
            ):
                utils._replace(source, target, temporary)
            sleep.assert_not_called()
            self.assertEqual(source.read_bytes(), b"new program")
            self.assertEqual(target.read_bytes(), b"old program")
            self.assertFalse(temporary.exists())
