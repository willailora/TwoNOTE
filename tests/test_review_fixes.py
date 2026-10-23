import copy
import socket
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from twonote.backup import export_backup, restore_backup
from twonote.navigation import Navigation
from twonote.sync import SyncHost
from twonote.vault import LockedError, Vault, VaultError

PASSWORD = "synthetic-review-fixes"


class ReviewStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", PASSWORD)
        self.blobs = [self.vault.add_blob(f"fixture{i}.bin", "application/octet-stream", bytes([i])) for i in range(2)]
        self.note = self.vault.save(uuid.uuid4().hex, [], {"title": "Fixture", "attachments": self.blobs})

    def tearDown(self):
        self.vault.lock(); self.temp.cleanup()

    def test_corrupt_navigation_is_quarantined_without_changing_notes(self):
        path = self.vault.root / "navigation.enc"; path.write_bytes(b"damaged display state")
        before = self.vault.inventory()
        tree = Navigation(self.vault); tree.tree()
        self.assertTrue(tree.recovered_from)
        self.assertEqual((self.vault.root / tree.recovered_from).read_bytes(), b"damaged display state")
        self.assertEqual(self.vault.inventory(), before)
        self.assertFalse(Navigation(self.vault).recovered_from)

    def test_backup_and_restore_still_work_with_bad_optional_navigation(self):
        (self.vault.root / "navigation.enc").write_bytes(b"damaged display state")
        output = self.root / "notes.twonote"; export_backup(self.vault, output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("navigation.enc", archive.namelist())
        # An older backup may itself contain the damaged optional metadata.
        with zipfile.ZipFile(output, "a") as archive: archive.writestr("navigation.enc", b"damaged backup metadata")
        restored = restore_backup(output, self.root / "restored", PASSWORD)
        try:
            self.assertEqual(restored.inventory(), self.vault.inventory())
            self.assertTrue(Navigation(restored).recovered_from)
        finally: restored.lock()

    def test_lock_does_not_quarantine_valid_metadata(self):
        Navigation(self.vault).tree()
        original = (self.vault.root / "navigation.enc").read_bytes()
        self.vault.lock()
        with self.assertRaises(LockedError): Navigation(self.vault)
        self.assertEqual((self.vault.root / "navigation.enc").read_bytes(), original)

    def test_one_save_checks_only_unique_attachments_of_new_revisions(self):
        note = self.note
        for i in range(25): note = self.vault.save(note["note_id"], [note["revision"]], {**note, "content": str(i)})
        seen = []; original = Path.is_file
        def checked(path):
            if path.parent.name == "blobs": seen.append(path.name)
            return original(path)
        with patch.object(Path, "is_file", checked):
            self.vault.save(note["note_id"], [note["revision"]], {**note, "content": "next"})
        self.assertCountEqual(seen, [b["id"] for b in self.blobs])

    def test_incremental_validation_keeps_missing_blob_and_parent_checks(self):
        before = self.vault.inventory()
        missing = {**self.blobs[0], "id": "f" * 64}
        with self.assertRaises(VaultError):
            self.vault.save(self.note["note_id"], [self.note["revision"]], {**self.note, "attachments": [missing]})
        with self.assertRaises(VaultError):
            self.vault.save(uuid.uuid4().hex, [self.note["revision"]], {"title": "wrong parent"})
        self.assertEqual(self.vault.inventory(), before)

    def test_batch_cycles_and_missing_parents_are_rejected(self):
        a, b = "a" * 64, "b" * 64
        records = dict(self.vault.records)
        records[a] = {**self.note, "parents": [b]}
        records[b] = {**self.note, "parents": [a]}
        with self.assertRaises(VaultError): self.vault._validate_graph(records, {a, b})
        records[a]["parents"] = ["c" * 64]
        with self.assertRaises(VaultError): self.vault._validate_graph(records, {a})
        records[a]["parents"] = [self.note["revision"]]
        records[b]["parents"] = [a]
        self.vault._validate_graph(records, {a, b})


class SyncCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Vault.create(Path(self.temp.name) / "vault", PASSWORD)
        self.host = SyncHost(self.vault)
        self.port = self.host.start("127.0.0.1", 0, seconds=10)
        self.server = self.host.server

    def tearDown(self):
        self.host.stop(); self.vault.lock(); self.temp.cleanup()

    def test_failed_handshakes_close_ssl_sockets_and_release_slots(self):
        captured = []; replace = self.server.replace_connection
        def capture(raw, encrypted):
            captured.append(encrypted); return replace(raw, encrypted)
        with patch.object(self.server, "replace_connection", side_effect=capture):
            for _ in range(4):
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
                    client.sendall(b"not a TLS handshake")
                    try: client.recv(1)
                    except ConnectionResetError: pass
        deadline = time.monotonic() + 2
        while self.server.connections and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(len(captured), 4)
        self.assertFalse(self.server.connections)
        self.assertTrue(all(sock.fileno() == -1 for sock in captured))

    def test_closing_listener_rejects_a_late_ssl_registration(self):
        self.host.stop()
        raw, encrypted = Mock(), Mock()
        with self.assertRaises(OSError): self.server.replace_connection(raw, encrypted)
        encrypted.close.assert_called_once()
        self.assertFalse(self.server.connections)


if __name__ == "__main__": unittest.main()
