#!/usr/bin/env python3
"""Windows OCR discovery/validation tests using temporary volumes and mocked executables.

Runs on macOS/Linux too; this is not a claim of native Windows integration coverage.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_pipeline
import resolve_env


class WindowsOCRTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drives = tuple(self.root / name for name in ("C", "D", "I"))
        for root in self.drives:
            root.mkdir()
        self.tesseract = self.file(self.drives[1] / "Program Files/Tesseract-OCR/tesseract.exe")
        self.renderer = self.file(self.drives[2] / "tools/poppler-26.07/Library/bin/pdftoppm.exe")
        resolve_env.windows_ocr_search.cache_clear()
        resolve_env._windows_ocr_probe.cache_clear()
        self.addCleanup(resolve_env.windows_ocr_search.cache_clear)
        self.addCleanup(resolve_env._windows_ocr_probe.cache_clear)

    def file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test fixture, not executable")
        return path

    def runner(self, command, **kwargs):
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 5)
        if command[1] == "--list-langs":
            return subprocess.CompletedProcess(command, 0, "List of available languages (2):\neng\nchi_sim\n", "")
        name = "tesseract" if command[1] == "--version" else "pdftoppm"
        # Poppler normally writes version information on stderr.
        return subprocess.CompletedProcess(command, 0, "", f"{name} version 5.0\n")

    def environment(self):
        self.stack.enter_context(patch.object(resolve_env.sys, "platform", "win32"))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(resolve_env, "load_local_config", return_value={}))
        self.stack.enter_context(patch.object(resolve_env, "_which", return_value=None))
        self.stack.enter_context(patch.object(resolve_env, "windows_fixed_drives", return_value=self.drives))
        self.stack.enter_context(patch.object(resolve_env.subprocess, "run", side_effect=self.runner))

    def test_drive_api_excludes_network_and_removable_volumes(self):
        import ctypes
        kernel = SimpleNamespace(
            GetLogicalDrives=Mock(return_value=sum(1 << i for i in (2, 3, 4, 8))),
            GetDriveTypeW=Mock(side_effect=lambda root: {"C:\\": 3, "D:\\": 4, "E:\\": 2, "I:\\": 3}[root]),
        )
        with patch.object(resolve_env.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            self.assertEqual(resolve_env.windows_fixed_drives(), (Path("C:/"), Path("I:/")))

    def test_permission_failure_does_not_hide_other_drives(self):
        scan = os.scandir
        def guarded(path):
            if Path(path).is_relative_to(self.drives[0]):
                raise PermissionError("test access denied")
            return scan(path)
        with patch.object(resolve_env.os, "scandir", side_effect=guarded):
            paths, scope = resolve_env.search_windows_ocr("pdftoppm", self.drives)
        self.assertIn(self.renderer, paths)
        self.assertIn("inaccessible", scope)

    def test_search_other_drives_spaces_and_versioned_poppler(self):
        self.environment()
        tess = resolve_env.resolve_ocr_program("tesseract")
        poppler = resolve_env.resolve_ocr_program("pdftoppm")
        self.assertEqual(tess.value, self.tesseract)
        self.assertEqual(poppler.value, self.renderer)
        self.assertEqual(tess.source, "probe:local-drives")
        self.assertIn(str(self.drives[0]), tess.detail)
        self.assertIn(str(self.drives[2]), tess.detail)
        self.assertEqual(resolve_env.ocr_helper_candidates("pdftoppm"), [self.renderer])

    def test_no_recursive_search_of_documents_or_links(self):
        self.file(self.drives[0] / "Users/student/Documents/secret/tesseract.exe")
        outside = self.file(self.root / "outside/pdftoppm.exe")
        (self.drives[0] / "apps").mkdir()
        try:
            (self.drives[0] / "apps/linked").symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            # Windows may disallow symlink creation without developer mode/admin privileges.
            # The remaining assertions still cover the unrelated-document-directory boundary.
            pass
        paths, _ = resolve_env.search_windows_ocr("tesseract", self.drives)
        self.assertEqual(paths, [self.tesseract])
        paths, _ = resolve_env.search_windows_ocr("pdftoppm", self.drives)
        self.assertEqual(paths, [self.renderer])

    def test_portable_root_and_unicode_installation(self):
        target = self.file(self.drives[0] / "poppler-25/Library/bin/pdftoppm.exe")
        chinese = self.file(self.drives[0] / "软件/Tesseract-OCR/tesseract.exe")
        self.assertIn(target, resolve_env.search_windows_ocr("pdftoppm", self.drives)[0])
        self.assertIn(chinese, resolve_env.search_windows_ocr("tesseract", self.drives)[0])

    def test_limits_are_reported_and_each_drive_gets_a_budget(self):
        for index in range(15):
            self.file(self.drives[0] / f"apps/a{index}/irrelevant.txt")
        paths, scope = resolve_env.search_windows_ocr("tesseract", self.drives, max_entries=8)
        self.assertIn(self.tesseract, paths)
        self.assertIn("budget reached", scope)
        self.assertIn(str(self.drives[2]), scope)
        self.assertIn("budget reached", resolve_env.search_windows_ocr(
            "pdftoppm", self.drives, seconds_per_drive=0)[1])

    def test_config_and_path_precede_search_and_stale_config_falls_back(self):
        self.environment()
        with patch.object(resolve_env, "windows_ocr_search", side_effect=AssertionError("unneeded scan")):
            result = resolve_env.resolve_ocr_program("tesseract", config={"ocr_executable": str(self.tesseract)})
            self.assertEqual(result.source, "config:local.json")
        with patch.object(resolve_env, "_which", return_value=self.renderer):
            result = resolve_env.resolve_ocr_program("pdftoppm", config={"pdftoppm_executable": "missing.exe"})
            self.assertEqual(result.value, self.renderer)
            self.assertIn("config:local.json", result.detail)

    def test_invalid_explicit_flag_and_environment_do_not_fall_back(self):
        self.environment()
        with patch.object(resolve_env, "windows_ocr_search", side_effect=AssertionError("must not scan")):
            self.assertFalse(resolve_env.resolve_ocr_program("tesseract", "missing.exe").ok)
            with patch.dict(os.environ, {"EXPERIMENT_REPORT_GRADER_OCR_EXECUTABLE": "bad.exe"}):
                result = resolve_env.resolve_ocr_program("tesseract")
                self.assertFalse(result.ok)
                self.assertTrue(result.source.startswith("env:"))

    def test_missing_language_broken_binary_and_timeout(self):
        self.environment()
        def languages(command, **kwargs):
            if command[1] == "--list-langs":
                return subprocess.CompletedProcess(command, 0, "eng\n", "")
            return self.runner(command, **kwargs)
        with patch.object(resolve_env.subprocess, "run", side_effect=languages):
            result = resolve_env.resolve_ocr_program("tesseract", str(self.tesseract))
            self.assertFalse(result.ok)
            self.assertIn("missing language data chi_sim", result.detail)
        for error in (OSError("missing DLL"), subprocess.TimeoutExpired("helper", 5)):
            resolve_env._windows_ocr_probe.cache_clear()
            with patch.object(resolve_env.subprocess, "run", side_effect=error):
                result = resolve_env.resolve_ocr_program("pdftoppm", str(self.renderer))
                self.assertFalse(result.ok)
                self.assertIn("cannot run", result.detail)

    def test_missing_eng_wrong_executable_and_exit_failure(self):
        self.environment()
        for output, code, expected in (("unexpected binary", 0, "does not identify"),
                                       ("pdftoppm DLL failure", 1, "exit 1")):
            resolve_env._windows_ocr_probe.cache_clear()
            with patch.object(resolve_env.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], code, output, "")):
                self.assertIn(expected, resolve_env.resolve_ocr_program("pdftoppm", str(self.renderer)).detail)
        resolve_env._windows_ocr_probe.cache_clear()
        def language_only(command, **kwargs):
            if command[1] == "--list-langs":
                return subprocess.CompletedProcess(command, 0, "chi_sim\n", "")
            return self.runner(command, **kwargs)
        with patch.object(resolve_env.subprocess, "run", side_effect=language_only):
            result = resolve_env.resolve_ocr_program("tesseract", str(self.tesseract))
            self.assertIn("missing language data eng", result.detail)

    def test_automatic_probe_skips_unusable_install(self):
        self.environment()
        broken = self.file(self.drives[0] / "Tesseract-OCR/tesseract.exe")
        def run(command, **kwargs):
            if command[0] == str(broken):
                return subprocess.CompletedProcess(command, 1, "", "DLL missing")
            return self.runner(command, **kwargs)
        with patch.object(resolve_env.subprocess, "run", side_effect=run):
            result = resolve_env.resolve_ocr_program("tesseract")
            self.assertEqual(result.value, self.tesseract)
            self.assertIn("DLL missing", result.detail)

    def test_search_miss_is_not_reported_as_not_installed(self):
        self.environment()
        with patch.object(resolve_env, "windows_fixed_drives", return_value=()):
            result = resolve_env.resolve_ocr_program("pdftoppm")
            self.assertFalse(result.ok)
            self.assertIn("not proof of no installation", result.detail)
            self.assertIn("provide the full executable path", result.detail)

    def test_preflight_requires_renderer_and_pins_validated_paths(self):
        self.environment()
        args = SimpleNamespace(ocr_mode="auto", ocr_executable=None, pdftoppm_executable=None)
        batch_pipeline.require_ocr_on_windows(args)
        self.assertEqual(args.ocr_executable, str(self.tesseract))
        self.assertEqual(args.pdftoppm_executable, str(self.renderer))
        self.renderer.unlink()
        with self.assertRaisesRegex(RuntimeError, "pdftoppm"):
            batch_pipeline.require_ocr_on_windows(args)

    def test_off_bypasses_checks_and_non_windows_is_unchanged(self):
        with patch.object(resolve_env, "resolve_ocr_program", side_effect=AssertionError("should skip")):
            with patch.object(resolve_env.sys, "platform", "win32"):
                batch_pipeline.require_ocr_on_windows(SimpleNamespace(ocr_mode="off"))
            with patch.object(resolve_env.sys, "platform", "darwin"):
                batch_pipeline.require_ocr_on_windows(SimpleNamespace(ocr_mode="auto"))

    def test_failed_prepare_does_not_create_batch_outputs(self):
        self.environment()
        report_dir = self.root / "reports"
        self.file(report_dir / "student.pdf")
        args = batch_pipeline.build_parser().parse_args([
            "prepare", "--report-dir", str(report_dir), "--batch-name", "test",
            "--ocr-executable", str(self.tesseract), "--pdftoppm-executable", "missing.exe",
        ])
        with self.assertRaisesRegex(RuntimeError, "no batch work was started"):
            batch_pipeline.prepare_batch(args)
        self.assertEqual([path.name for path in report_dir.iterdir()], ["student.pdf"])


if __name__ == "__main__":
    unittest.main()
