"""Exercise current-user DPAPI on synthetic data, with OS UI forbidden."""
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from twonote.backup import export_backup
from twonote.vault import Vault, VaultError
from twonote import windows_unlock

PASSWORD = "synthetic-only-password"


@unittest.skipUnless(windows_unlock.supported(), "Windows DPAPI required")
class WindowsUnlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", PASSWORD)
        self.vault.save(uuid.uuid4().hex, [], {"title": "fixture", "content": "private synthetic text"})

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def test_opt_in_roundtrip_without_plaintext_key_or_password(self):
        self.assertFalse(windows_unlock.available(self.vault.root))
        key = self.vault._key()
        windows_unlock.remember(self.vault)
        raw = (self.vault.root / windows_unlock.KEY_FILE).read_bytes()
        self.assertNotIn(key, raw)
        self.assertNotIn(PASSWORD.encode(), raw)
        self.vault.lock()
        reopened = windows_unlock.open_remembered(self.vault.root)
        self.assertEqual(reopened.heads()[0]["content"], "private synthetic text")
        reopened.lock()

    def test_disable_keeps_password_access_and_notes(self):
        windows_unlock.remember(self.vault)
        windows_unlock.forget(self.vault.root)
        self.assertFalse(windows_unlock.available(self.vault.root))
        self.vault.lock()
        reopened = Vault.open(self.vault.root, PASSWORD)
        self.assertEqual(len(reopened.heads()), 1)
        reopened.lock()

    def test_saved_key_survives_a_fresh_process_without_password(self):
        windows_unlock.remember(self.vault)
        self.vault.lock()
        program = ("import sys; from pathlib import Path; from twonote.windows_unlock import open_remembered; "
                   "vault = open_remembered(Path(sys.argv[1])); "
                   "assert len(vault.heads()) == 1; vault.lock(); print('auto-unlock-ok')")
        # No GUI, real notes, user credentials, or password arguments in the child.
        result = subprocess.run([sys.executable, "-B", "-c", program, str(self.vault.root)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "auto-unlock-ok")

    def test_tampered_key_fails_but_password_still_works(self):
        windows_unlock.remember(self.vault)
        path = self.vault.root / windows_unlock.KEY_FILE
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(VaultError):
            windows_unlock.open_remembered(self.vault.root)
        reopened = Vault.open(self.vault.root, PASSWORD)
        reopened.lock()

    def test_saved_key_is_bound_to_vault_and_config_and_excluded_from_backup(self):
        windows_unlock.remember(self.vault)
        other = Vault.create(self.root / "other", PASSWORD)
        try:
            (other.root / windows_unlock.KEY_FILE).write_bytes((self.vault.root / windows_unlock.KEY_FILE).read_bytes())
            with self.assertRaises(VaultError):
                windows_unlock.open_remembered(other.root)
        finally:
            other.lock()
        output = self.root / "backup.twonote"
        export_backup(self.vault, output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn(windows_unlock.KEY_FILE, archive.namelist())
        config = dict(self.vault.config)
        config["unexpected_change"] = True
        (self.vault.root / "config.json").write_text(json.dumps(config))
        with self.assertRaises(VaultError):
            windows_unlock.open_remembered(self.vault.root)


if __name__ == "__main__":
    unittest.main()
