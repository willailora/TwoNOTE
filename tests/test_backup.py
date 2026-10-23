import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from twonote.backup import export_backup, restore_backup
from twonote.vault import Vault, VaultError


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.password = 'test-only-password-1234'
        self.vault = Vault.create(self.root / 'original', self.password)
        blob = self.vault.add_blob('private-filename.png', 'image/png', b'PRIVATE-IMAGE')
        note = self.vault.save(uuid.uuid4().hex, [], {'title': 'PRIVATE-TITLE', 'attachments': [blob]})
        self.vault.save(note['note_id'], [note['revision']], {**note, 'content': 'PRIVATE-TEXT'})
        self.archive = self.root / 'backup.twonote'
        export_backup(self.vault, self.archive)

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def test_backup_roundtrip_preserves_history_images_and_has_no_plaintext(self):
        self.assertNotIn(b'PRIVATE-', self.archive.read_bytes())
        restored = restore_backup(self.archive, self.root / 'restored', self.password)
        self.assertEqual(restored.inventory(), self.vault.inventory())
        note = restored.heads()[0]
        self.assertEqual(note['content'], 'PRIVATE-TEXT')
        self.assertEqual(restored.read_blob(note['attachments'][0]['id']), b'PRIVATE-IMAGE')
        self.assertEqual(restored.settings(), {})
        restored.lock()

    def test_wrong_password_and_existing_destination_leave_files_untouched(self):
        with self.assertRaises(VaultError):
            restore_backup(self.archive, self.root / 'restored', 'incorrect')
        self.assertFalse((self.root / 'restored').exists())
        before = (self.vault.root / 'index.enc').read_bytes()
        with self.assertRaises(VaultError):
            restore_backup(self.archive, self.vault.root, self.password)
        self.assertEqual((self.vault.root / 'index.enc').read_bytes(), before)

    def test_traversal_entry_is_rejected_before_any_extraction(self):
        with zipfile.ZipFile(self.archive, 'a') as archive:
            archive.writestr('../outside', b'bad')
        with self.assertRaises(VaultError):
            restore_backup(self.archive, self.root / 'restored', self.password)
        self.assertFalse((self.root / 'outside').exists())
        self.assertFalse((self.root / 'restored').exists())

    def test_tampered_ciphertext_cannot_publish_a_restored_vault(self):
        bad = self.root / 'bad.twonote'
        with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(bad, 'w') as changed:
            for info in original.infolist():
                raw = original.read(info.filename)
                if info.filename.startswith('blobs/'):
                    raw = raw[:-1] + bytes([raw[-1] ^ 1])
                changed.writestr(info.filename, raw)
        with self.assertRaises(VaultError):
            restore_backup(bad, self.root / 'restored', self.password)
        self.assertFalse((self.root / 'restored').exists())
