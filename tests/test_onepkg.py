"""Package classification regression tests; no Qt, screenshots or real notes."""
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from twonote.importing import ONE_SECTION_HEADER, ONE_TOC_HEADER, import_into, onenote_file_kind
from twonote.paths import active_vault_path
from twonote.recovery import rebuild_from_onenote
from twonote.vault import Vault, VaultError

PASSWORD = "synthetic-test-password"


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.package = self.root / "original.onepkg"
        self.package.write_bytes(b"synthetic package; expansion is stubbed")
        self.called = []
        self.entries = {"目次・onetoc2": ONE_TOC_HEADER + b"toc",
                        "本文・one": ONE_SECTION_HEADER + b"one",
                        "normal.ONE": ONE_SECTION_HEADER + b"two"}
        self.module = types.ModuleType("aspose.note")
        self.module.RichText = type("RichText", (), {})
        self.module.Image = type("Image", (), {})
        self.module.AttachedFile = type("AttachedFile", (), {})
        def document(path):
            file = Path(path)
            self.called.append(file.name)
            if onenote_file_kind(file) != "section":
                raise ValueError("toc reached section parser")
            def nodes(kind):
                if kind is self.module.RichText:
                    return [types.SimpleNamespace(Text="合成本文")]
                if kind is self.module.Image:
                    return [types.SimpleNamespace(FileName="fixture.png", Bytes=b"synthetic-image")]
                return []
            return [types.SimpleNamespace(Title=None, GetChildNodes=nodes)]
        self.module.Document = document
        def expand(command, **kwargs):
            destination = Path(command[-1])
            for name, raw in self.entries.items():
                (destination / name).write_bytes(raw)
            return subprocess.CompletedProcess(command, 0)
        self.patches = [patch.dict(sys.modules, {"aspose.note": self.module}),
                        patch("twonote.importing.shutil.which", return_value="expand.exe"),
                        patch("twonote.importing.subprocess.run", side_effect=expand)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_mangled_toc_is_skipped_and_extensionless_sections_are_imported(self):
        vault = Vault.create(self.root / "vault", PASSWORD)
        try:
            report = import_into(vault, self.package)
            self.assertEqual(report["warnings"], [])
            self.assertEqual(report["imported"], 2)
            self.assertEqual(report["attachments"], 2)
            self.assertCountEqual(self.called, ["本文・one", "normal.ONE"])
            self.assertEqual(len(vault.heads()), 2)
        finally:
            vault.lock()

    def test_content_type_wins_over_a_misleading_toc_suffix(self):
        self.entries = {"actually-a-section.onetoc2": ONE_SECTION_HEADER + b"one",
                        "actually-a-toc.one": ONE_TOC_HEADER + b"toc"}
        vault = Vault.create(self.root / "vault", PASSWORD)
        try:
            report = import_into(vault, self.package)
            self.assertEqual(report["imported"], 1)
            self.assertEqual(report["warnings"], [])
            self.assertEqual(self.called, ["actually-a-section.onetoc2"])
        finally:
            vault.lock()

    def test_recovery_completes_without_hiding_real_corruption(self):
        data = self.root / "data"
        restored, result = rebuild_from_onenote(data, self.package, PASSWORD)
        self.assertEqual(result["rebuilt"]["imported"], 2)
        selected = restored.root
        restored.lock()
        self.assertEqual(active_vault_path(data), selected)
        self.entries["damaged.one"] = b"truncated"
        with self.assertRaises(VaultError):
            rebuild_from_onenote(data, self.package, PASSWORD)
        self.assertEqual(active_vault_path(data), selected)


if __name__ == "__main__":
    unittest.main()
