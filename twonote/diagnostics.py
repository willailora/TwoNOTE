"""Packaged-app smoke test, isolated from the user's data and desktop."""
import tempfile
import uuid
from pathlib import Path

from PySide6.QtCore import QSize, qVersion
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from .backup import export_backup, restore_backup
from .desktop import Desktop
from .vault import Vault, atomic_write, json_bytes


def self_test(report_path: Path) -> int:
    result = {"version": "2.0.0", "ui": "PySide6 / Qt", "qt": qVersion(), "status": "failed"}
    window = None
    try:
        with tempfile.TemporaryDirectory(prefix="twonote-self-test-") as directory:
            root = Path(directory)
            password = uuid.uuid4().hex
            vault = Vault.create(root / "vault-v2", password)
            note = vault.save(uuid.uuid4().hex, [], {"title": "自己診断ノート"})
            window = Desktop(root, sync_port=0)
            def fail(title, error):
                raise RuntimeError(f"{title}: {error}")
            window.error = fail
            window.accept_vault(vault, {}, window.session)
            window.show()
            QApplication.processEvents()
            window.load_revision(note["revision"])
            window.body.setPlainText("日本語の合成テストデータ")
            assert window.flush()
            picture = QImage(QSize(32, 32), QImage.Format.Format_RGB32)
            picture.fill(QColor("#6dd3cf"))
            window.paste_image(picture)
            assert len(window.current["attachments"]) == 1
            assert window.sync.server is None
            result["local_edit_without_listener"] = True
            export_backup(vault, root / "sample.twonote")
            restored = restore_backup(root / "sample.twonote", root / "restored", password)
            assert restored.heads()[0]["content"] == "日本語の合成テストデータ"
            restored.lock()
            result["encrypted_backup_roundtrip"] = True
            window.lock()
            assert window.vault is None and window.current is None
            window.close()
            window = None
            QApplication.processEvents()
            reopened = Vault.open(root / "vault-v2", password)
            assert len(reopened.heads()[0]["attachments"]) == 1
            reopened.lock()
            result["save_and_reopen"] = True
            result["status"] = "ok"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if window is not None:
            window.close()
    atomic_write(Path(report_path).resolve(), json_bytes(result))
    return 0 if result["status"] == "ok" else 1
