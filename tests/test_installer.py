"""Installation contracts and local browser probes without contacting a portal."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("notasync_install_check", ROOT / "installer" / "verify_install.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_manifest(self):
        (self.root / "python.exe").write_bytes(b"synthetic-runtime")
        (self.root / "library.py").write_bytes(b"value = 1\n")
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        (self.root / "runtime_integrity.json").write_text(json.dumps({"algorithm": "sha256", "files": files}))

    def test_hash_verification_rejects_corrupt_or_missing_component(self):
        self.make_manifest()
        with patch.object(check, "emit"):
            self.assertEqual(check.verify_integrity(self.root), 2)
            (self.root / "library.py").write_bytes(b"changed")
            with self.assertRaisesRegex(check.InstallError, "alterado"):
                check.verify_integrity(self.root)
            (self.root / "library.py").unlink()
            with self.assertRaisesRegex(check.InstallError, "extraído"):
                check.verify_integrity(self.root)

    def test_manifest_cannot_read_outside_runtime(self):
        outside = self.root.parent / "should-not-be-read"
        manifest = {"algorithm": "sha256", "files": {"../should-not-be-read": "0" * 64}}
        (self.root / "runtime_integrity.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(check.InstallError, "fora"):
            check.verify_integrity(self.root)

    def test_runtime_rejects_wrong_python_before_hashing(self):
        with patch.object(check.sys, "version_info", (3, 12, 1)), patch.object(check, "verify_integrity") as integrity:
            with self.assertRaisesRegex(check.InstallError, "versão"):
                check.verify_runtime(self.root)
            integrity.assert_not_called()

    def test_runtime_validates_hashes_before_loading_dependencies(self):
        calls = []
        with patch.object(check.sys, "version_info", (3, 13, 17)), \
             patch.object(check.struct, "calcsize", return_value=8), \
             patch.object(check, "verify_integrity", side_effect=lambda p: calls.append("hash")), \
             patch.object(check, "verify_imports", side_effect=lambda: calls.append("imports")), \
             patch.object(check, "emit"):
            check.verify_runtime(self.root)
        self.assertEqual(calls, ["hash", "imports"])

    def test_missing_interface_is_not_marked_ready(self):
        module = SimpleNamespace(__file__=str(self.root / "app.py"))
        with patch.object(check.importlib, "import_module", return_value=module):
            with self.assertRaisesRegex(check.InstallError, "interface"):
                check.verify_imports()

    def test_edge_usable_avoids_chromium_requirement(self):
        context = MagicMock()
        context.__enter__.return_value = context
        with patch("playwright.sync_api.sync_playwright", return_value=context), \
             patch.object(check, "edge_paths", return_value=[Path("edge.exe")]), \
             patch.object(check, "_test_browser") as probe, patch.object(check, "emit"):
            self.assertEqual(check.find_usable_browser(), "msedge")
        probe.assert_called_once_with(context.chromium, channel="msedge")

    def test_edge_failure_falls_back_to_existing_chromium(self):
        context = MagicMock()
        context.__enter__.return_value = context
        with patch("playwright.sync_api.sync_playwright", return_value=context), \
             patch.object(check, "edge_paths", return_value=[Path("edge.exe")]), \
             patch.object(check, "_test_browser", side_effect=[RuntimeError("policy"), None]) as probe, \
             patch.object(check, "emit"):
            self.assertEqual(check.find_usable_browser(), "chromium")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[1].kwargs, {})

    def test_no_install_option_never_starts_download(self):
        with patch.object(check, "find_usable_browser", side_effect=check.BrowserUnavailable("absent")), \
             patch.object(check, "install_browser") as install:
            with self.assertRaises(check.BrowserUnavailable):
                check.verify_browser(False)
        install.assert_not_called()

    def test_download_requires_actual_browser_probe_afterwards(self):
        with patch.object(check, "find_usable_browser", side_effect=[check.BrowserUnavailable("absent"), "chromium"]) as probe, \
             patch.object(check, "install_browser") as install:
            self.assertEqual(check.verify_browser(True), "chromium")
        install.assert_called_once_with()
        self.assertEqual(probe.call_count, 2)

    def test_failed_browser_download_cannot_report_success(self):
        with patch.object(check, "find_usable_browser", side_effect=check.BrowserUnavailable("absent")), \
             patch.object(check, "install_browser", side_effect=check.InstallError("download failed")):
            with self.assertRaisesRegex(check.InstallError, "download failed"):
                check.verify_browser(True)

    def test_browser_probe_closes_browser_on_page_failure(self):
        kind = MagicMock()
        browser = kind.launch.return_value
        browser.new_page.return_value.title.return_value = "wrong"
        with self.assertRaises(check.InstallError):
            check._test_browser(kind, channel="msedge")
        browser.close.assert_called_once_with()

    def test_main_missing_browser_returns_two_and_records_failure(self):
        with patch.object(check, "setup_environment", return_value=self.root / "log"), \
             patch.object(check, "verify_browser", side_effect=check.BrowserUnavailable("absent")), \
             patch.object(check, "emit") as output:
            self.assertEqual(check.main(["--stage", "browser"]), 2)
        self.assertEqual(output.call_args.kwargs["status"], "error")

    def test_native_installer_does_not_use_powershell_or_inline_python(self):
        source = (ROOT / "installer" / "NotaSync.nsi").read_text(encoding="utf-8")
        self.assertNotIn("powershell", source.casefold())
        self.assertNotIn("-ExecutionPolicy", source)
        self.assertNotIn("'-c'", source)
        for stage in ("runtime", "browser", "verify"):
            self.assertIn('verify_install.py" --stage ' + stage, source)
        self.assertIn('--stage browser --install', source)
        self.assertIn('File /r', source)
        self.assertIn('vendor/runtime/*', source)


if __name__ == "__main__":
    unittest.main()
