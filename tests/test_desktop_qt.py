"""In-process Qt checks; the offscreen platform never controls the user's desktop."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from twonote.desktop import Desktop
from twonote.vault import LockedError, Vault

PASSWORD = "test-only-日本語-password"


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        # The offscreen plugin has no Windows system-font discovery.
        for filename in ("segoeui.ttf", "YuGothM.ttc", "YuGothB.ttc"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.vault = Vault.create(self.root / "vault-v2", PASSWORD)
        self.first = self.vault.save(uuid.uuid4().hex, [], {
            "title": "週末のアイデア", "content": "テスト用ノートです。\n日本語の編集内容を暗号化して保存します。",
            "notebook": "個人のノート", "section": "アイデア", "tags": "メモ, テスト"})
        self.other = self.vault.save(uuid.uuid4().hex, [], {"title": "買い物リスト", "content": "牛乳\nパン"})
        self.window = Desktop(self.root, sync_port=0)
        self.window.error = lambda title, error: self.fail(f"{title}: {error}")
        self.window.accept_vault(self.vault, {}, self.window.session)
        self.window.resize(1120, 760)
        self.window.show()
        self.app.processEvents()
        self.window.load_revision(self.first["revision"])

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def test_edit_autosave_switch_and_reopen_preserve_the_right_note(self):
        self.window.body.setPlainText("保存前に素早く切り替えます")
        other_item = self.window.tree_items[self.other["revision"]]
        self.window.select_note(other_item)
        self.assertEqual(self.window.current["note_id"], self.other["note_id"])
        self.assertEqual(self.window.body.toPlainText(), "牛乳\nパン")
        self.window.body.setPlainText("日本語の追加入力を自動保存")
        QTest.qWait(850)
        self.assertFalse(self.window.dirty)
        reopened = Vault.open(self.vault.root, PASSWORD)
        notes = {n["note_id"]: n for n in reopened.heads()}
        self.assertEqual(notes[self.first["note_id"]]["content"], "保存前に素早く切り替えます")
        self.assertEqual(notes[self.other["note_id"]]["content"], "日本語の追加入力を自動保存")
        reopened.lock()

    def paste_fixture_image(self):
        fixture = QImage(QSize(320, 180), QImage.Format.Format_RGB32)
        fixture.fill(QColor("#518fa3"))
        mime = QMimeData()
        mime.setImageData(fixture)
        self.window.body.insertFromMimeData(mime)
        self.app.processEvents()
        return self.window.current["attachments"][0]

    def test_images_are_local_cached_and_never_refetched_while_idle(self):
        item = self.paste_fixture_image()
        self.assertEqual(item["mime"], "image/png")
        self.assertIn(item["id"], self.window.thumbnail_cache)
        with patch.object(self.vault, "read_blob", side_effect=AssertionError("idle image reload")), \
             patch.object(self.window.sync, "start", side_effect=AssertionError("local editing opened a listener")):
            with patch.object(self.vault, "get", side_effect=AssertionError("idle text reload")):
                QTest.qWait(1200)
            self.window.body.insertPlainText("\n画像を再取得せず編集")
            self.assertTrue(self.window.flush())
            self.window.render_attachments()
        self.assertIsNone(self.window.sync.server)

    def test_lock_stops_sync_and_clears_editor_and_key(self):
        self.window.body.setPlainText("ロック直前の編集")
        self.window.sync.start("127.0.0.1", 0)
        host = self.window.sync
        self.window.lock()
        self.app.processEvents()
        self.assertFalse(host.active)
        self.assertIsNone(self.window.current)
        self.assertEqual(self.window.fields, {})
        self.assertEqual(self.window.thumbnail_cache, {})
        self.assertEqual(self.window.password.text(), "")
        with self.assertRaises(LockedError):
            self.vault.heads()
        reopened = Vault.open(self.root / "vault-v2", PASSWORD)
        self.assertIn("ロック直前の編集", [n["content"] for n in reopened.heads()])
        reopened.lock()

    def test_remote_change_keeps_unsaved_local_branch(self):
        self.window.body.setPlainText("未保存のローカル編集")
        self.vault.save(self.first["note_id"], [self.first["revision"]], {**self.first, "content": "スマホでの編集"})
        self.window.sync_event("changed", self.window.session)
        self.assertEqual(self.window.body.toPlainText(), "未保存のローカル編集")
        self.assertTrue(self.window.flush())
        branches = [n["content"] for n in self.vault.heads() if n["note_id"] == self.first["note_id"]]
        self.assertCountEqual(branches, ["未保存のローカル編集", "スマホでの編集"])

    def test_small_window_controls_remain_inside_and_render_preview(self):
        self.paste_fixture_image()
        for width, height in ((900, 650), (1120, 760)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertLessEqual(self.window.height(), height)
            self.assertGreaterEqual(self.window.body.height(), 60)
            for control in self.window.attachment_scroll.findChildren(QPushButton):
                bottom = control.mapTo(self.window, control.rect().bottomRight())
                self.assertLess(bottom.y(), self.window.height() - 20)
            self.assertLess(self.window.status.mapTo(self.window, QPoint(0, self.window.status.height())).y(), height)
        output = Path(__file__).resolve().parents[1] / "build" / "qa"
        output.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.window.grab().save(str(output / "qt-editor.png")))
        self.window.lock()
        self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(output / "qt-lock.png")))

    def test_new_vault_setup_transitions_to_editable_notes(self):
        self.window.close()
        self.window = Desktop(self.root / "new-data", sync_port=0)
        self.window.resize(900, 650)
        self.window.show()
        self.app.processEvents()
        self.assertIsNotNone(self.window.confirm)
        self.window.password.setText(PASSWORD)
        self.window.confirm.setText(PASSWORD)
        self.window.unlock()
        for _ in range(100):
            if self.window.vault is not None:
                break
            QTest.qWait(50)
        self.assertIsNotNone(self.window.vault)
        self.window.new_note()
        self.window.body.setPlainText("新規作成後も編集できます")
        self.assertTrue(self.window.flush())
        self.assertEqual(self.window.vault.heads()[0]["content"], "新規作成後も編集できます")

    def test_lock_is_available_during_import_and_rejects_late_completion(self):
        old_session = self.window.session
        self.window.busy = True
        self.window.lock()
        self.assertIsNone(self.window.vault)
        self.assertFalse(self.window.busy)
        self.window.import_finished({}, old_session)
        self.window.work_failed("old import error", old_session)
        self.assertIsNone(self.window.vault)
        self.assertEqual(self.window.login_status.text(), "")


if __name__ == "__main__":
    unittest.main()
