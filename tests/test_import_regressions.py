import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from twonote.importing import import_into
from twonote.recovery import rebuild_from_onenote
from twonote.vault import Vault, VaultError


class ImportRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", "synthetic-review-password")

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def test_same_markup_different_images_keeps_both_pages_and_retry_is_safe(self):
        for name in ("a", "b"):
            folder = self.root / "exports" / name
            folder.mkdir(parents=True)
            (folder / "page.html").write_text('<title>Same</title><img src="image.png">')
            (folder / "image.png").write_bytes(name.encode())
        report = import_into(self.vault, self.root / "exports")
        self.assertEqual((report["imported"], report["attachments"]), (2, 2))
        self.assertEqual(len(self.vault.heads()), 2)
        self.assertEqual(import_into(self.vault, self.root / "exports")["skipped"], 2)

    def test_identical_text_in_distinct_named_files_is_not_dropped(self):
        folder = self.root / "exports"; folder.mkdir()
        for name in ("first", "second"):
            (folder / (name + ".txt")).write_text("same text")
        self.assertEqual(import_into(self.vault, folder)["imported"], 2)
        self.assertEqual({n["title"] for n in self.vault.heads()}, {"first", "second"})

    def test_replaced_image_is_imported_without_overwriting_edited_note(self):
        page = self.root / "page.html"; image = self.root / "image.png"
        page.write_text('<title>Same</title><img src="image.png">'); image.write_bytes(b"first")
        import_into(self.vault, page)
        note = self.vault.heads()[0]
        self.vault.save(note["note_id"], [note["revision"]], {**note, "content": "local edit"})
        image.write_bytes(b"second")
        self.assertEqual(import_into(self.vault, page)["imported"], 1)
        self.assertIn("local edit", [n["content"] for n in self.vault.heads()])

    def test_missing_image_warns_and_recovery_does_not_publish_pointer(self):
        page = self.root / "page.html"
        page.write_text('<p>Keep text</p><img src="absent.png">')
        report = import_into(self.vault, page)
        self.assertTrue(report["warnings"])
        data = self.root / "recovery"
        with self.assertRaises(VaultError):
            rebuild_from_onenote(data, page, "synthetic-review-password")
        self.assertFalse((data / "active-vault.json").exists())

    def test_missing_cid_and_remote_images_warn(self):
        message = EmailMessage(); message.set_content('<img src="cid:missing"><img src="https://example.invalid/photo.png">', subtype="html")
        page = self.root / "page.mht"; page.write_bytes(message.as_bytes())
        self.assertEqual(len(import_into(self.vault, page)["warnings"]), 2)

    def test_data_uri_image_is_retained(self):
        page = self.root / "page.html"
        page.write_text('<img src="data:image/png;base64,c3ludGhldGljLWltYWdl">')
        report = import_into(self.vault, page)
        self.assertEqual(report["warnings"], [])
        attachment = self.vault.heads()[0]["attachments"][0]
        self.assertEqual(self.vault.read_blob(attachment["id"]), b"synthetic-image")


if __name__ == "__main__": unittest.main()
