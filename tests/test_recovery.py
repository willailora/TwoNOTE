"""Recovery checks with synthetic exports only; no Qt import or image rendering."""
import json
import tempfile
import unittest
import uuid
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from twonote.importing import import_into
from twonote.paths import ACTIVE_VAULT_FILE, active_vault_path, default_data_dir
from twonote.recovery import rebuild_from_onenote
from twonote.vault import Vault, VaultError

OLD_PASSWORD = "old-test-only-password"
NEW_PASSWORD = "new-test-only-password"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.previous = Vault.create(self.data / "vault-v2", OLD_PASSWORD)
        self.previous.save(uuid.uuid4().hex, [], {"title": "old fixture", "content": "OLD-PRIVATE-FIXTURE"})
        self.previous.set_settings({"peers": {"fixture": "old-test-token"}})
        self.snapshot = {p.relative_to(self.previous.root): p.read_bytes()
                         for p in self.previous.root.rglob("*") if p.is_file()}
        self.source = self.root / "original.mht"
        mail = EmailMessage()
        mail.set_type("multipart/related")
        body = EmailMessage()
        body.set_content('<title>原本ノート</title><p>原本の本文</p><img src="cid:photo">', subtype="html", charset="utf-8")
        mail.attach(body)
        attachment = EmailMessage()
        attachment.set_content(b"\x89PNG\r\n\x1a\nSYNTHETIC-IMAGE", maintype="image", subtype="png")
        attachment["Content-ID"] = "<photo>"
        mail.attach(attachment)
        self.original_bytes = mail.as_bytes()
        self.source.write_bytes(self.original_bytes)

    def tearDown(self):
        self.previous.lock()
        self.temporary.cleanup()

    def assert_originals_unchanged(self):
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual({p.relative_to(self.previous.root): p.read_bytes()
                          for p in self.previous.root.rglob("*") if p.is_file()}, self.snapshot)

    def test_new_password_recovers_editable_text_images_and_persists_selection(self):
        restored, result = rebuild_from_onenote(self.data, self.source, NEW_PASSWORD)
        self.assertEqual(result["rebuilt"]["imported"], 1)
        self.assertEqual(result["rebuilt"]["attachments"], 1)
        self.assertNotEqual(restored.vault_id, self.previous.vault_id)
        self.assertEqual(active_vault_path(self.data), restored.root)
        self.assertNotIn("peers", restored.settings())
        note = restored.heads()[0]
        from twonote import inline
        self.assertEqual(note["content"], inline.encode("原本の本文\n" + inline.token(note["attachments"][0]["id"])))
        self.assertEqual(restored.read_blob(note["attachments"][0]["id"]), b"\x89PNG\r\n\x1a\nSYNTHETIC-IMAGE")
        restored.save(note["note_id"], [note["revision"]], {**note, "content": "復元後の編集"})
        restored.lock()
        reopened = Vault.open(active_vault_path(self.data), NEW_PASSWORD)
        self.assertEqual(reopened.heads()[0]["content"], "復元後の編集")
        reopened.lock()
        with self.assertRaises(VaultError):
            Vault.open(active_vault_path(self.data), OLD_PASSWORD)
        self.assert_originals_unchanged()
        for file in active_vault_path(self.data).rglob("*"):
            if file.is_file():
                raw = file.read_bytes()
                self.assertNotIn(NEW_PASSWORD.encode(), raw)
                self.assertNotIn("復元後の編集".encode(), raw)
                self.assertNotIn(b"SYNTHETIC-IMAGE", raw)
        # An EXE beside data/ discovers the selected recovered vault next time.
        (self.root / "dist").mkdir()
        with patch("twonote.paths.sys.frozen", True, create=True), patch("twonote.paths.sys.executable", str(self.root / "dist" / "TwoNote.exe")):
            self.assertEqual(default_data_dir(), self.data)

    def test_invalid_password_or_source_does_not_create_or_select_new_vault(self):
        for source, password in ((self.source, "short"), (self.root / "missing.onepkg", NEW_PASSWORD)):
            with self.assertRaises(VaultError):
                rebuild_from_onenote(self.data, source, password)
        self.assertFalse((self.data / ACTIVE_VAULT_FILE).exists())
        self.assertFalse(list(self.data.glob("vault-v2-recovered-*")))
        self.assert_originals_unchanged()

    def test_incomplete_import_never_becomes_active(self):
        for report in ({"imported": 0, "attachments": 0, "warnings": []},
                       {"imported": 1, "attachments": 0, "warnings": ["unreadable section"]}):
            with patch("twonote.recovery.import_into", return_value=report):
                with self.assertRaises(VaultError):
                    rebuild_from_onenote(self.data, self.source, NEW_PASSWORD)
            self.assertEqual(active_vault_path(self.data), self.previous.root)
        self.assert_originals_unchanged()

    def test_pointer_write_failure_preserves_previously_recovered_vault(self):
        first, _ = rebuild_from_onenote(self.data, self.source, NEW_PASSWORD)
        first.lock()
        before = (self.data / ACTIVE_VAULT_FILE).read_bytes()
        with patch("twonote.recovery.atomic_write", side_effect=OSError("injected write error")):
            with self.assertRaises(OSError):
                rebuild_from_onenote(self.data, self.source, NEW_PASSWORD)
        self.assertEqual((self.data / ACTIVE_VAULT_FILE).read_bytes(), before)
        self.assertEqual(active_vault_path(self.data), first.root)
        self.assert_originals_unchanged()

    def test_corrupt_image_aborts_before_publishing(self):
        def corrupt(vault, source):
            report = import_into(vault, source)
            blob = vault.object_path("blobs", vault.inventory()["blobs"][0])
            raw = blob.read_bytes()
            blob.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            return report
        with patch("twonote.recovery.import_into", side_effect=corrupt):
            with self.assertRaises(VaultError):
                rebuild_from_onenote(self.data, self.source, NEW_PASSWORD)
        self.assertEqual(active_vault_path(self.data), self.previous.root)
        self.assert_originals_unchanged()

    def test_invalid_pointer_cannot_escape_data_directory_or_fall_back_silently(self):
        for directory in ("../outside", str(self.previous.root), "vault-v2-recovered-" + "0" * 32):
            (self.data / ACTIVE_VAULT_FILE).write_text(json.dumps({"version": 1, "directory": directory}), encoding="utf-8")
            with self.assertRaises(ValueError):
                active_vault_path(self.data)
        self.assert_originals_unchanged()


if __name__ == "__main__":
    unittest.main()
