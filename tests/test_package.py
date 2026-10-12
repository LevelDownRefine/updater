"""下载包的路径、内容与版本边界。"""

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.policy import POLICY
from tests.support.update_package import archive_package, make_package
from updater import package
from updater.package import load_manifest, parse_manifest, safe_target, unpack_package
from updater.utils import UpdateError
from updater.utils import write_json as save_data


class TestUpdatePackage(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.package = make_package(self.directory / "source", "1.2.0")

    def test_zip_round_trip_verifies_program_bytes(self):
        archive = archive_package(self.package, self.directory / "package.zip")
        for borrowed in (False, True):
            with self.subTest(borrowed=borrowed):
                source = (
                    self.enterContext(zipfile.ZipFile(archive)) if borrowed else archive
                )
                target = self.directory / f"unpacked-{borrowed}"
                self.assertEqual(
                    unpack_package(source, target, "1.2.0", policy=POLICY)["version"],
                    "1.2.0",
                )
                self.assertEqual(
                    (target / "_internal/python.dll").read_bytes(), b"runtime"
                )
                if borrowed:
                    self.assertIsNotNone(source.fp)

    def test_reused_file_corrupted_during_copy_is_rejected(self):
        archive = archive_package(self.package, self.directory / "package.zip")
        copy = package.shutil.copy2

        def corrupt_copy(source, destination):
            copy(source, destination)
            destination.write_bytes(b"corrupted during copy")

        with (
            patch.object(package.shutil, "copy2", side_effect=corrupt_copy),
            self.assertRaisesRegex(UpdateError, "程序文件校验失败"),
        ):
            unpack_package(
                archive,
                self.directory / "unpacked",
                "1.2.0",
                reuse_root=self.package,
                policy=POLICY,
            )
        load_manifest(self.package, verify=True, policy=POLICY)

    def test_verified_resume_does_not_read_installed_files(self):
        installed = make_package(self.directory / "installed", "1.0.0")
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        progress = Mock()
        unpack_package(
            archive,
            target,
            "1.2.0",
            reuse_root=installed,
            progress=progress,
            policy=POLICY,
        )
        total = progress.call_args.args[1]
        self.assertGreater(total, 0)
        digest = package.file_digest

        def deny_installed_read(path):
            if path.is_relative_to(installed):
                self.fail(f"已校验缓存不应再次读取安装源: {path}")
            return digest(path)

        progress.reset_mock()
        with patch.object(package, "file_digest", side_effect=deny_installed_read):
            unpack_package(
                archive,
                target,
                "1.2.0",
                reuse_root=installed,
                progress=progress,
                policy=POLICY,
            )
        progress.assert_called_once_with(total, total)
        load_manifest(target, verify=True, policy=POLICY)

    def test_legacy_verified_cache_survives_unreadable_installed_file(self):
        installed = make_package(self.directory / "installed", "1.0.0")
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        shutil.copytree(self.package, target)
        digest = package.file_digest

        def deny_runtime_read(path):
            if path == installed / "_internal/python.dll":
                raise PermissionError("安装源临时不可读")
            return digest(path)

        progress = Mock()
        with (
            patch.object(package, "file_digest", side_effect=deny_runtime_read),
            self.assertLogs(package.__name__, level="WARNING") as logs,
        ):
            unpack_package(
                archive,
                target,
                "1.2.0",
                reuse_root=installed,
                progress=progress,
                policy=POLICY,
            )
        self.assertIn("PermissionError", "\n".join(logs.output))
        received, total = progress.call_args.args
        self.assertEqual(received, total)
        load_manifest(target, verify=True, policy=POLICY)

    def test_missing_invalid_or_stale_progress_record_is_rebuilt(self):
        installed = make_package(self.directory / "installed", "1.0.0")
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        progress = Mock()
        unpack_package(
            archive,
            target,
            "1.2.0",
            reuse_root=installed,
            progress=progress,
            policy=POLICY,
        )
        total = progress.call_args.args[1]
        state = target.with_name(target.name + ".progress.json")
        self.assertTrue(state.is_file())

        for kind in ("missing", "invalid_json", "invalid_fields", "stale_manifest"):
            with self.subTest(kind=kind):
                if kind == "missing":
                    state.unlink()
                elif kind == "invalid_json":
                    state.write_text("{", encoding="utf-8")
                elif kind == "invalid_fields":
                    save_data(state, {"copied": True})
                else:
                    save_data(
                        state,
                        {
                            "manifest": "0" * 64,
                            "copied": list(
                                load_manifest(self.package, policy=POLICY)["files"]
                            ),
                        },
                    )
                progress.reset_mock()
                unpack_package(
                    archive,
                    target,
                    "1.2.0",
                    reuse_root=installed,
                    progress=progress,
                    policy=POLICY,
                )
                progress.assert_called_once_with(total, total)
                load_manifest(target, verify=True, policy=POLICY)

    def test_resume_revalidates_missing_or_corrupt_cached_files(self):
        installed = self.directory / "installed"
        shutil.copytree(self.package, installed)
        archive = archive_package(self.package, self.directory / "package.zip")
        expected = (self.package / "_internal/python.dll").read_bytes()

        for kind in ("missing", "corrupt"):
            with self.subTest(kind=kind):
                (installed / "_internal/python.dll").write_bytes(expected)
                target = self.directory / f"unpacked-{kind}"
                unpack_package(
                    archive, target, "1.2.0", reuse_root=installed, policy=POLICY
                )
                (installed / "_internal/python.dll").write_bytes(b"modified source")
                cached = target / "_internal/python.dll"
                if kind == "missing":
                    cached.unlink()
                else:
                    cached.write_bytes(b"x" * len(expected))

                progress = Mock()
                unpack_package(
                    archive,
                    target,
                    "1.2.0",
                    reuse_root=installed,
                    progress=progress,
                    policy=POLICY,
                )
                self.assertEqual(progress.call_args_list[0].args, (0, len(expected)))
                self.assertEqual(
                    progress.call_args.args, (len(expected), len(expected))
                )
                self.assertEqual(cached.read_bytes(), expected)
                load_manifest(target, verify=True, policy=POLICY)

    def test_corrupt_cache_does_not_hide_unreadable_installed_file(self):
        installed = make_package(self.directory / "installed", "1.0.0")
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        unpack_package(archive, target, "1.2.0", reuse_root=installed, policy=POLICY)
        (target / "_internal/python.dll").write_bytes(b"corrupt")
        digest = package.file_digest

        def deny_runtime_read(path):
            if path == installed / "_internal/python.dll":
                raise PermissionError("安装源临时不可读")
            return digest(path)

        with (
            patch.object(package, "file_digest", side_effect=deny_runtime_read),
            self.assertRaises(PermissionError),
        ):
            unpack_package(
                archive, target, "1.2.0", reuse_root=installed, policy=POLICY
            )

    def test_linked_destination_is_rejected(self):
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        with (
            patch.object(package, "linked_path", return_value=True),
            self.assertRaisesRegex(UpdateError, "链接"),
        ):
            unpack_package(archive, target, "1.2.0", policy=POLICY)
        self.assertFalse(target.exists())

    def test_manifest_rejects_unsafe_and_user_file_paths(self):
        for name in (
            "../app.exe",
            "/app.exe",
            "assets/../../config/config.yml",
            "assets\\file",
            "assets/NUL.txt",
            "assets/a:stream",
            "assets/a.",
            "assets/a ",
            "config/config.yml",
            "config/SCHEDULE.YML",
            "config/weekly.yml",
            "config/wallpaper.json",
            "config/backups/a.zip",
            "assets/a.bak2",
            "assets/BANNER.JPG",
            "logs/a.log",
            ".update/intent.lock",
        ):
            with self.subTest(name=name):
                data = load_manifest(self.package, policy=POLICY)
                data["files"][name] = "0" * 64
                with self.assertRaises(UpdateError):
                    parse_manifest(data, policy=POLICY)

    def test_existing_symlink_cannot_redirect_program_writes(self):
        outside = self.directory / "personal"
        outside.mkdir()
        (self.package / "assets").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UpdateError, "链接"):
            safe_target(self.package, "assets/example.txt", policy=POLICY)
        self.assertFalse((outside / "example.txt").exists())

    def test_existing_directory_in_destination_is_rejected(self):
        archive = archive_package(self.package, self.directory / "package.zip")
        target = self.directory / "unpacked"
        (target / "_internal/python.dll").mkdir(parents=True)
        with self.assertRaisesRegex(UpdateError, "同名目录"):
            unpack_package(archive, target, "1.2.0", policy=POLICY)

    def test_manifest_rejects_case_duplicates_and_file_directory_conflicts(self):
        for names in (("assets/a", "assets/A"), ("assets/a", "assets/a/b")):
            with self.subTest(names=names):
                data = load_manifest(self.package, policy=POLICY)
                data["files"].update(dict.fromkeys(names, "0" * 64))
                with self.assertRaises(UpdateError):
                    parse_manifest(data, policy=POLICY)

    def test_corrupt_file_is_rejected(self):
        (self.package / "_internal/python.dll").write_bytes(b"corrupt")
        archive = archive_package(self.package, self.directory / "corrupt.zip")
        with self.assertRaisesRegex(UpdateError, "校验失败"):
            unpack_package(archive, self.directory / "unpacked", "1.2.0", policy=POLICY)

    def test_wrong_version_and_extra_user_file_are_rejected_before_unpack(self):
        for extra in (False, True):
            with self.subTest(extra=extra):
                archive = archive_package(self.package, self.directory / "bad.zip")
                if extra:
                    with zipfile.ZipFile(archive, "a") as output:
                        output.writestr("Example/config/config.yml", "secret")
                destination = self.directory / "unpacked"
                with self.assertRaises(UpdateError):
                    unpack_package(
                        archive,
                        destination,
                        "1.2.0" if extra else "1.3.0",
                        policy=POLICY,
                    )
                self.assertFalse(destination.exists())

    def test_zip_duplicate_symlink_and_path_traversal_are_rejected(self):
        for name, link in (
            ("Example/assets/../secret", False),
            ("Example/assets/link", True),
            ("outside.txt", False),
            ("Example/VERSION.JSON", False),
        ):
            with self.subTest(name=name):
                archive = archive_package(self.package, self.directory / "bad.zip")
                with zipfile.ZipFile(archive, "a") as output:
                    info = zipfile.ZipInfo(name)
                    if link:
                        info.external_attr = 0o120777 << 16
                    output.writestr(info, "bad")
                with self.assertRaises(UpdateError):
                    unpack_package(
                        archive, self.directory / "unpacked", "1.2.0", policy=POLICY
                    )
