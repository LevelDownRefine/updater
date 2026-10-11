"""Applications have independent layouts; path safety remains unconditional."""

import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from tests.support.policy import POLICY
from tests.support.update_package import archive_package, make_package
from updater.package import UpdateError, load_manifest, managed_path, unpack_package
from updater.service import UpdateService


class PolicyTests(unittest.TestCase):
    def test_two_applications_do_not_share_repository_layout_or_variant(self):
        alternate = replace(
            POLICY,
            repository="other/tool",
            archive_prefix="Tool/",
            archives={"desktop": "Tool.zip"},
            required_files={"desktop": POLICY.required_files["qt"]},
            default_frontend="desktop",
            cli_exes={"desktop": ()},
            parent_required=frozenset(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = make_package(root / "source")
            archive = archive_package(original, root / "original.zip")
            with (
                zipfile.ZipFile(archive) as source,
                zipfile.ZipFile(root / "tool.zip", "w") as output,
            ):
                for item in source.infolist():
                    output.writestr(
                        "Tool/" + item.filename.removeprefix(POLICY.archive_prefix),
                        source.read(item),
                    )
            unpack_package(root / "tool.zip", root / "tool", "1.0.0", policy=alternate)
            load_manifest(original, verify=True, policy=POLICY)
            with self.assertRaisesRegex(UpdateError, "路径"):
                unpack_package(archive, root / "wrong", "1.0.0", policy=alternate)
            self.assertEqual(UpdateService(root, policy=alternate).zip_name, "Tool.zip")
            self.assertEqual(
                UpdateService(root, policy=POLICY).policy.releases_url,
                "https://github.com/example/application/releases",
            )
            # A missing frontend follows this policy, not a hard-coded Qt default.
            self.assertNotIn(
                "frontend", json.loads((root / "tool/version.json").read_text())
            )

    def test_application_allowlist_cannot_override_path_safety(self):
        allow_all = replace(POLICY, program_path=lambda name: True)
        for name in ("../file", "/file", "NUL.txt", "a:stream", ".update/intent.lock"):
            with self.subTest(name=name):
                self.assertFalse(managed_path(name, policy=allow_all))
        self.assertTrue(managed_path("assets/banner.jpg", policy=allow_all))
        self.assertFalse(managed_path("assets/banner.jpg", policy=POLICY))
