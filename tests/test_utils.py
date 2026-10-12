"""Shared filesystem helpers operate without a release policy or installer."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from updater import utils


class TestFilesystemUtils(unittest.TestCase):
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
                self.assertRaises(FileNotFoundError),
            ):
                utils._replace(source, target, temporary)
            self.assertEqual(source.read_bytes(), b"new program")
            self.assertEqual(target.read_bytes(), b"old program")
            self.assertFalse(temporary.exists())
