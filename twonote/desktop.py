"""Qt native UI. Local viewing and editing never start a network listener."""
from __future__ import annotations

import argparse
import base64
import html
import io
import mimetypes
import os
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QMimeData, QObject, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QImageReader, QKeySequence, QPainter, QPixmap, QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QFontComboBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QTreeWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .legacy import cleanup_legacy, migrate_legacy
from .backup import export_backup, restore_backup
from .paths import ACTIVE_VAULT_FILE, active_vault_path, default_data_dir
from .recovery import rebuild_from_onenote
from .sync import SyncHost, lan_addresses
from .vault import FIELDS, InstanceLock, Vault
from . import preferences, windows_unlock
from .navigation import Navigation, group_key
from .tree_widget import NoteTree
from . import inline, blocks
from .block_render import render as render_block

STYLE = """
QWidget { background:#0c1524; color:#e5edf7; }
QFrame#panel { background:#122136; border:1px solid #243449; border-radius:12px; }
QFrame#login { background:#122136; border:1px solid #2c4058; border-radius:18px; }
QLabel { background:transparent; }
QLabel#muted { color:#99abc1; }
QLabel#heading { font-weight:700; }
QLabel#brand { font-weight:700; }
QLabel#mark { background:#6dd3cf; color:#0c2431; border-radius:10px; padding:6px; font-weight:800; }
QPushButton { background:#20364d; border:1px solid #2c425b; border-radius:7px; padding:8px 14px; }
QPushButton:hover { background:#2b4762; border-color:#53738f; }
QPushButton:pressed { background:#172d42; }
QPushButton:disabled { color:#62778c; background:#18293c; }
QPushButton#primary { background:#6dd3cf; color:#0b2630; border-color:#6dd3cf; font-weight:700; }
QPushButton#primary:hover { background:#96e3df; }
QPushButton#danger { color:#f5b2aa; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox { background:#0e1b2c; color:#e5edf7; border:1px solid #293f57; border-radius:8px; padding:9px; selection-background-color:#2e607a; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color:#6dd3cf; }
QLineEdit#title { background:transparent; border:0; border-radius:0; padding:3px 0; font-weight:700; }
QTreeWidget { background:transparent; border:0; outline:0; }
QTreeWidget::item { padding:7px 4px; border-radius:5px; }
QTreeWidget::item:selected { background:#254b64; color:#f3f8ff; }
QTreeWidget::item:hover:!selected { background:#1b344d; }
QTreeWidget::branch { background:transparent; }
QSplitter::handle { background:#0c1524; }
QScrollArea { border:0; background:transparent; }
QFrame#attachment { background:#172b42; border:1px solid #2e435b; border-radius:9px; }
QMenu { background:#172a40; border:1px solid #3b536d; padding:6px; }
QMenu::item { padding:8px 22px; }
QMenu::item:selected { background:#2c526b; border-radius:5px; }
QComboBox { background:#20364d; border:1px solid #3c536b; padding:7px; border-radius:6px; }
QComboBox QAbstractItemView { background:#182d43; selection-background-color:#2c526b; }
QCheckBox { background:transparent; color:#a9bad0; spacing:8px; }
QScrollBar:vertical { background:transparent; width:11px; margin:0; }
QScrollBar:horizontal { background:transparent; height:11px; margin:0; }
QScrollBar::handle { background:#3c536e; border-radius:5px; min-height:25px; min-width:25px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
"""


class Events(QObject):
    opened = Signal(object, object, int)
    failed = Signal(str, int)
    imported = Signal(object, int)
    sync = Signal(str, int)


class NoteText(QTextEdit):
    image_pasted = Signal(QImage)
    zoom_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wheel_zoom = preferences.WheelZoom()
        self.inline_files, self.inline_cache = {}, {}
        self.inline_mode, self.inline_bytes, self.inline_vault = False, 0, None

    def load_body(self, content, attachments, vault):
        self.inline_mode = content.startswith(inline.HEADER) or any(inline.supported(a) for a in attachments)
        self.inline_files = {a["id"]: a for a in attachments}
        self.inline_vault = vault
        self.inline_cache = {}
        self.inline_bytes = 0
        old = self.document()
        old.setParent(None)
        self.setDocument(QTextDocument(self))
        self.document().setDefaultFont(self.font())
        old.deleteLater()
        self.insert_portable(inline.body(content, attachments))
        self.document().clearUndoRedoStacks()
        cursor = self.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.Start); self.setTextCursor(cursor)

    def loadResource(self, kind, name):
        # QTextEdit must never resolve file:/http(s): resources from pasted text.
        return getattr(self, "inline_cache", {}).get(name.toString().removeprefix("twonote-embed://"), QImage())

    def insert_portable(self, text):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for kind, value in inline.parts(text, self.inline_files.values()):
            if kind == "text": cursor.insertText(value, QTextCharFormat())
            else:
                item = self.inline_files[value]
                if value not in self.inline_cache:
                    image = QImage()
                    try:
                        if self.inline_bytes < 32 * 1024 * 1024:
                            raw = self.inline_vault.read_blob(value)
                            image = render_block(blocks.load(raw)) if item["mime"] == inline.BLOCK_MIME else image_preview(raw, QSize(600, 700))
                            if image.width() > 600: image = image.scaledToWidth(600, Qt.TransformationMode.SmoothTransformation)
                    except Exception:
                        pass
                    if image.isNull():
                        image = QImage(340, 40, QImage.Format.Format_ARGB32_Premultiplied); image.fill(QColor("#20364d"))
                        painter = QPainter(image); painter.setPen(QColor("#e5edf7")); painter.drawText(8, 26, "画像・表（下の一覧から開けます）"); painter.end()
                    self.inline_cache[value] = image
                    self.inline_bytes += image.sizeInBytes()
                image = self.inline_cache[value]
                name = "twonote-embed://" + value
                self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
                fmt = QTextImageFormat(); fmt.setName(name); fmt.setWidth(image.width()); fmt.setHeight(image.height())
                cursor.insertImage(fmt)
        cursor.endEditBlock()
        cursor.setCharFormat(QTextCharFormat())
        self.setTextCursor(cursor)

    def portable_text(self, start=0, end=None):
        end = self.document().characterCount() - 1 if end is None else end
        result = []
        block = self.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                a, b = max(start, fragment.position()), min(end, fragment.position() + fragment.length())
                if a < b:
                    fmt = fragment.charFormat()
                    if fmt.isImageFormat() and fmt.toImageFormat().name().startswith("twonote-embed://"):
                        result.append(inline.token(fmt.toImageFormat().name().split("://", 1)[1]))
                    else:
                        raw = fragment.text().encode("utf-16-le")
                        result.append(raw[(a-fragment.position())*2:(b-fragment.position())*2].decode("utf-16-le", errors="replace"))
                iterator += 1
            newline = block.position() + block.length() - 1
            if block.next().isValid() and start <= newline < end: result.append("\n")
            block = block.next()
        return "".join(result)

    def saved_content(self):
        text = self.portable_text()
        return inline.encode(text) if getattr(self, "inline_mode", False) else text

    def createMimeDataFromSelection(self):
        cursor = self.textCursor()
        result = QMimeData()
        result.setText(self.portable_text(cursor.selectionStart(), cursor.selectionEnd()))
        return result

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = self.wheel_zoom.feed(event.angleDelta().y(), event.pixelDelta().y())
            if steps:
                self.zoom_requested.emit(steps)
            event.accept()
            return
        self.wheel_zoom.reset()
        super().wheelEvent(event)

    def insertFromMimeData(self, source):
        if source.hasText():
            self.insert_portable(source.text())
            return
        if source.hasImage() and not source.hasText():
            image = QImage(source.imageData())
            if not image.isNull():
                self.image_pasted.emit(image)
                return
        # No rich HTML pasting: only local attachment references and plain text.


def label(text, name="", word_wrap=False):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        item.setObjectName(name)
    item.setWordWrap(word_wrap)
    return item


def button(text, callback, name=""):
    result = QPushButton(text)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        result.setObjectName(name)
    result.clicked.connect(callback)
    return result


def unlock_choice(registered=False):
    choice = QComboBox()
    choice.addItem("毎回パスワードを入力", False)
    choice.addItem("このWindowsユーザーで自動解除", True)
    choice.setCurrentIndex(1 if registered and windows_unlock.supported() else 0)
    choice.setEnabled(windows_unlock.supported())
    choice.setAccessibleName("次回起動時のロック解除方法")
    return choice


def app_icon():
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#6dd3cf"))
    painter.drawRoundedRect(2, 2, 60, 60, 15, 15)
    painter.setPen(QColor("#102638"))
    font = QFont("Segoe UI", 35, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "2")
    painter.end()
    return QIcon(image)


def image_preview(raw: bytes, maximum: QSize) -> QImage:
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    original = reader.size()
    if original.isValid():
        reader.setScaledSize(original.scaled(maximum, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    buffer.close()
    return image


class Desktop(QMainWindow):
    def __init__(self, data_dir: Path, sync_port=8001):
        super().__init__()
        self.geometry_ready = False
        self.data_dir = Path(data_dir).resolve()
        self.instance_lock = InstanceLock(self.data_dir)
        self.sync_port = sync_port
        self.vault: Vault | None = None
        self.sync: SyncHost | None = None
        self.current: dict | None = None
        self.dirty = self.loading = self.busy = False
        self.session = 0
        self.viewers = []
        self.thumbnail_cache = {}
        self.sync_dialog = None
        self.backup_source = None
        self.onenote_source = None
        self.fields = {}
        self.navigation = None
        self.tree_items = {}
        self.group_items = {}
        self.tree_refresh_pending = False
        self.selecting_tree = False
        self.display_preferences = preferences.load(self.data_dir)
        self.preferences_pending = False
        self.preference_timer = QTimer(self)
        self.preference_timer.setSingleShot(True)
        self.preference_timer.setInterval(400)
        self.preference_timer.timeout.connect(self.persist_preferences)
        self.startup_unlock_attempted = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(700)
        self.save_timer.timeout.connect(self.flush)
        self.events = Events(self)
        self.events.opened.connect(self.accept_vault)
        self.events.failed.connect(self.work_failed)
        self.events.imported.connect(self.import_finished)
        self.events.sync.connect(self.sync_event)
        self.setWindowTitle("TwoNote")
        self.setWindowIcon(app_icon())
        self.apply_ui_preferences()
        self.setMinimumSize(900, 610)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1260, available.width() - 60), min(840, available.height() - 50))
        self.move(available.center() - self.rect().center())
        QImageReader.setAllocationLimit(128)
        save = QAction(self)
        save.setShortcut(QKeySequence.StandardKey.Save)
        save.triggered.connect(self.flush)
        self.addAction(save)
        for shortcuts, callback in ((["Ctrl++", "Ctrl+="], lambda: self.change_zoom(1)),
                                    (["Ctrl+-"], lambda: self.change_zoom(-1)),
                                    (["Ctrl+0"], self.reset_zoom)):
            action = QAction(self)
            action.setShortcuts([QKeySequence(value) for value in shortcuts])
            action.triggered.connect(callback)
            self.addAction(action)
        self.show_login()
        saved_geometry = self.display_preferences.get("window_geometry")
        if saved_geometry:
            self.restoreGeometry(QByteArray(base64.b64decode(saved_geometry)))
        self.geometry_ready = True
        # Startup only. Manual lock never schedules an automatic unlock.
        QTimer.singleShot(0, self.try_startup_unlock)

    def capture_window_geometry(self):
        if not getattr(self, "geometry_ready", False) or not self.isVisible() or self.isMinimized():
            return
        geometry = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        if self.display_preferences.get("window_geometry") != geometry:
            self.display_preferences["window_geometry"] = geometry
            self.preferences_pending = True
            self.preference_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.capture_window_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.capture_window_geometry()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.capture_window_geometry()

    def dismiss_viewers(self):
        for viewer in self.viewers:
            try:
                viewer.close()
                viewer.deleteLater()
            except RuntimeError:
                pass
        self.viewers.clear()
        self.sync_dialog = None
        self.thumbnail_cache.clear()

    def show_login(self):
        self.apply_ui_preferences()
        self.dismiss_viewers()
        self.current = None
        self.dirty = False
        configured = (self.data_dir / ACTIVE_VAULT_FILE).exists() or (self.data_dir / "vault-v2" / "config.json").is_file()
        restoring = self.backup_source is not None
        rebuilding = self.onenote_source is not None
        legacy = (self.data_dir / "notes.sqlite3").is_file() and not configured and not restoring and not rebuilding
        new = rebuilding or (not configured and not (self.data_dir / "vault.json").is_file() and not restoring)
        self.login_new = new
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.addStretch()
        card = QFrame()
        card.setObjectName("login")
        card.setMinimumWidth(500)
        card.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        form = QVBoxLayout(card)
        form.setContentsMargins(36, 32, 36, 32)
        form.setSpacing(14)
        form.addWidget(label("2   TwoNote", "brand"))
        form.addSpacing(10)
        form.addWidget(label("OneNoteから作り直す" if rebuilding else "バックアップを復元" if restoring else "パスワードを設定" if new else "保管庫をロック解除", "heading"))
        message = "ノートはこのPCに暗号化して保存されます。"
        if legacy:
            message = "旧TwoNoteServerを終了してから進んでください。既存ノートを新形式へ移行し、旧データは復元できる暗号化バックアップに置き換えます。"
        if restoring:
            message = "バックアップ作成時のパスワードで復元します。スマホは復元後に再ペアリングしてください。"
        if rebuilding:
            message = ("古いパスワードは不要です。原本と旧保管庫は残したまま、新しいパスワードでノートを作り直します。TwoNote上だけの変更は引き継がれません。\n原本：" + self.onenote_source.name)
        form.addWidget(label(message, "muted", True))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("新しいパスワード（12文字以上）" if rebuilding else "パスワード（12文字以上）" if new else "保管庫のパスワード")
        self.password.returnPressed.connect(self.unlock)
        form.addWidget(self.password)
        self.confirm = None
        if new:
            self.confirm = QLineEdit()
            self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
            self.confirm.setPlaceholderText("パスワードをもう一度")
            self.confirm.returnPressed.connect(self.unlock)
            form.addWidget(self.confirm)
        self.auto_unlock_registered = False
        if not rebuilding and not restoring:
            try:
                self.auto_unlock_registered = windows_unlock.available(active_vault_path(self.data_dir))
            except ValueError:
                pass
        form.addWidget(label("次回起動時のロック解除"))
        self.remember_choice = unlock_choice(self.auto_unlock_registered)
        form.addWidget(self.remember_choice)
        self.unlock_hint = label("", "muted", True)
        form.addWidget(self.unlock_hint)
        form.addWidget(label("自動解除はWindowsの鍵保護を使用します。パスワードは保存しません。\n同じWindowsユーザーの人・アプリも解除できるため、共有アカウントでは無効にしてください。", "muted", True))
        self.login_caption = "新しいパスワードで復元" if rebuilding else "復元して開く" if restoring else "移行して開く" if legacy else "開く"
        self.login_button = button(self.login_caption, self.unlock, "primary")
        form.addWidget(self.login_button)
        self.login_status = label("", "muted", True)
        form.addWidget(self.login_status)
        self.remember_choice.currentIndexChanged.connect(self.update_unlock_hint)
        self.update_unlock_hint()
        form.addWidget(label("パスワードは復元に必要です。忘れない場所に保管してください。", "muted", True))
        if rebuilding:
            form.addWidget(button("原本を選び直す", self.choose_onenote_recovery))
            form.addWidget(button("キャンセル", self.cancel_onenote_recovery))
        else:
            locations = QHBoxLayout()
            locations.addWidget(button("保存先を選ぶ", self.choose_data_dir))
            locations.addWidget(button("バックアップから復元", self.choose_backup))
            form.addLayout(locations)
            form.addWidget(button("パスワードを忘れた：OneNoteから作り直す", self.choose_onenote_recovery))
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(outer)
        self.setCentralWidget(scroll)
        self.password.setFocus()

    def update_unlock_hint(self, *args):
        selected = bool(self.remember_choice.currentData())
        if not windows_unlock.supported():
            message = "自動解除はWindows専用です。パスワードで開いてください。"
        elif selected:
            message = ("Windows鍵は登録済みです。アプリの再起動・PC再起動後も自動解除します。"
                       if self.auto_unlock_registered else
                       "今回パスワードで開くと自動解除を登録します。次回はアプリ・PC再起動後も入力不要です。")
        else:
            message = ("今回パスワードで開くと自動解除を無効にします。以後は毎回入力します。"
                       if self.auto_unlock_registered else
                       "自動解除は未登録です。アプリを閉じると、次回は再びパスワードが必要です。")
        self.unlock_hint.setText(message)
        caption = "自動解除を登録して開く" if selected and not self.auto_unlock_registered and self.login_caption == "開く" else self.login_caption
        self.login_button.setText(caption)

    def try_startup_unlock(self):
        if self.startup_unlock_attempted:
            return
        self.startup_unlock_attempted = True
        if self.busy or self.vault is not None or self.backup_source or self.onenote_source:
            return
        try:
            root = active_vault_path(self.data_dir)
            if not windows_unlock.available(root):
                return
        except Exception:
            return
        self.busy = True
        self.login_button.setEnabled(False)
        self.remember_choice.setEnabled(False)
        self.login_status.setText("Windowsユーザーの鍵で開いています…")
        epoch = self.session
        def run():
            try:
                vault = windows_unlock.open_remembered(root)
                self.events.opened.emit(vault, {}, epoch)
            except Exception:
                self.events.failed.emit("このWindows鍵では自動解除できません。パスワードで開き直し、設定から再登録してください。", epoch)
        threading.Thread(target=run, daemon=True).start()

    def choose_data_dir(self):
        if self.busy:
            return
        name = QFileDialog.getExistingDirectory(self, "TwoNoteのデータフォルダ", str(self.data_dir))
        if not name or Path(name).resolve() == self.data_dir:
            return
        try:
            lock = InstanceLock(Path(name))
        except Exception as exc:
            self.error("開けません", exc)
            return
        self.instance_lock.close()
        self.instance_lock = lock
        self.data_dir = Path(name).resolve()
        self.display_preferences = preferences.load(self.data_dir)
        self.backup_source = None
        self.onenote_source = None
        self.show_login()

    def choose_onenote_recovery(self):
        if self.busy:
            return
        app_base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        folders = (self.data_dir.parent / "onenoteexportdata", app_base / "onenoteexportdata", app_base.parent / "onenoteexportdata")
        initial = next((str(path) for path in folders if path.is_dir()), "")
        name, _ = QFileDialog.getOpenFileName(self, "元のOneNoteエクスポートを選ぶ", initial,
            "OneNote exports (*.onepkg *.one *.mht *.mhtml *.html *.htm)")
        if not name:
            return
        self.backup_source = None
        self.onenote_source = Path(name)
        self.show_login()

    def cancel_onenote_recovery(self):
        if not self.busy:
            self.onenote_source = None
            self.show_login()

    def choose_backup(self):
        if self.busy:
            return
        name, _ = QFileDialog.getOpenFileName(self, "TwoNoteバックアップを選ぶ", "", "TwoNote (*.twonote)")
        if not name:
            return
        folder = QFileDialog.getExistingDirectory(self, "復元先の空フォルダを選ぶ（新規作成できます）")
        if not folder:
            return
        destination = Path(folder).resolve()
        if any(p.name != ".instance.lock" for p in destination.iterdir()):
            self.error("復元先を変更してください", "既存データのない空フォルダを選んでください")
            return
        try:
            if destination != self.data_dir:
                lock = InstanceLock(destination)
                self.instance_lock.close()
                self.instance_lock = lock
            self.data_dir = destination
            self.display_preferences = preferences.load(self.data_dir)
            self.backup_source = Path(name)
            self.onenote_source = None
            self.show_login()
        except Exception as exc:
            self.error("復元を開始できません", exc)

    def error(self, title, error):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(error))
        box.setIcon(QMessageBox.Icon.Warning)
        box.exec()

    def unlock(self):
        if self.busy:
            return
        password = self.password.text()
        if self.login_new and (len(password) < 12 or password != self.confirm.text()):
            self.login_status.setText("12文字以上で、確認欄と同じパスワードを入力してください")
            return
        self.password.clear()
        if self.confirm:
            self.confirm.clear()
        self.busy = True
        self.login_button.setEnabled(False)
        self.login_status.setText("原本から復元しています… 完了するまで使用する保管庫は変更しません" if self.onenote_source else "保管庫を開いています…")
        epoch, source, backup, original = self.session, self.data_dir, self.backup_source, self.onenote_source
        remember = bool(self.remember_choice.currentData())
        self.remember_choice.setEnabled(False)
        def run():
            vault = None
            try:
                if original is not None:
                    vault, result = rebuild_from_onenote(source, original, password)
                else:
                    root = active_vault_path(source)
                    if backup is not None:
                        vault, result = restore_backup(backup, root, password), {}
                    elif (root / "config.json").is_file():
                        vault = Vault.open(root, password)
                        result = {"remaining": cleanup_legacy(vault, source)}
                    elif (source / "notes.sqlite3").is_file():
                        vault, result = migrate_legacy(source, password)
                    else:
                        vault, result = Vault.create(root, password), {}
                if windows_unlock.supported():
                    try:
                        if remember:
                            windows_unlock.remember(vault)
                        else:
                            windows_unlock.forget(vault.root)
                    except Exception:
                        result["remember_error"] = True
                self.events.opened.emit(vault, result, epoch)
            except Exception as exc:
                if vault is not None:
                    vault.lock()
                self.events.failed.emit(str(exc), epoch)
        threading.Thread(target=run, daemon=True).start()

    @Slot(object, object, int)
    def accept_vault(self, vault, result, epoch):
        if epoch != self.session:
            vault.lock()
            return
        try:
            self.navigation = Navigation(vault)
        except Exception as exc:
            vault.lock()
            self.work_failed(str(exc), epoch)
            return
        self.busy = False
        self.vault = vault
        self.backup_source = None
        self.onenote_source = None
        self.sync = SyncHost(vault, lambda event: self.events.sync.emit(event, epoch))
        self.show_editor()
        if self.navigation.recovered_from:
            self.error("ツリー表示を復旧しました", "表示設定が破損していたため、記事からツリーを再作成しました。本文・画像は変更していません。\n破損した暗号化設定は " + self.navigation.recovered_from + " に退避しています。表示順・開閉状態は再設定してください。")
        if result.get("remember_error"):
            self.error("ノートは開けました", "自動解除の設定を変更できませんでした。以前の設定が残っている可能性があります。パスワードは引き続き使用できます。自動解除は設定から変更し直してください。")
        if result.get("rebuilt"):
            report = result["rebuilt"]
            QMessageBox.information(self, "OneNote原本から復元しました",
                f"ノート {report['imported']}件、添付 {report['attachments']}件を新しいパスワードで保存しました。\n原本と旧保管庫は変更していません。次回も同じ保存先で新しい保管庫を開けます。\n旧スマホ保管庫とは別の保管庫になるため、Androidを利用する場合は新しい保管庫として設定してください。")
        if result.get("remaining"):
            self.error("旧データの整理が未完了", "新しい保管庫は作成できましたが、旧ファイルが使用中または変更されています。旧TwoNoteServerを終了してから再度開いてください。旧ファイルはまだ残っています。")
        if vault.recovered_index:
            self.error("前回の索引から復元", "索引の破損を検出し、前回保存した索引から復元しました。変更履歴の暗号化ファイルは保管庫内に残っています。")

    @Slot(str, int)
    def work_failed(self, message, epoch):
        if epoch != self.session:
            return
        self.busy = False
        if self.vault is None:
            self.login_status.setText(message)
            self.login_button.setEnabled(True)
            self.remember_choice.setEnabled(windows_unlock.supported())
        else:
            self.error("処理できません", message)
            self.update_status()

    def show_editor(self):
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(12)
        header = QHBoxLayout()
        mark = label("2", "mark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setMinimumSize(40, 40)
        mark.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        header.addWidget(mark)
        header.addSpacing(4)
        header.addWidget(label("TwoNote", "brand"))
        header.addSpacing(8)
        header.addWidget(label("ローカルノート", "muted"))
        header.addStretch()
        self.menu_button = button("メニュー", self.open_menu)
        header.addWidget(self.menu_button)
        header.addWidget(button("設定", self.show_settings))
        self.sync_button = button("スマホと同期", self.toggle_sync, "primary")
        header.addWidget(self.sync_button)
        header.addWidget(button("ロック", self.lock))
        layout.addLayout(header)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        sidebar = QFrame()
        sidebar.setObjectName("panel")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 12)
        heading = QHBoxLayout()
        heading.addWidget(label("ノート", "heading"))
        self.count = label("0件", "muted")
        heading.addStretch()
        heading.addWidget(self.count)
        side.addLayout(heading)
        create = QHBoxLayout()
        create.addWidget(button("＋ 記事", self.new_note), 1)
        create.addWidget(button("＋ 分類", self.folder_menu))
        side.addLayout(create)
        self.search = QLineEdit()
        self.search.setPlaceholderText("タイトル・本文を検索")
        self.search.textChanged.connect(self.refresh_list)
        side.addWidget(self.search)
        self.trash = QCheckBox("削除したノートも表示")
        self.trash.toggled.connect(self.refresh_list)
        side.addWidget(self.trash)
        self.note_list = NoteTree()
        self.note_list.currentItemChanged.connect(self.select_note)
        self.note_list.dropped.connect(self.tree_drop)
        self.note_list.drag_finished.connect(self.finish_tree_drag)
        self.note_list.itemExpanded.connect(lambda item: self.tree_expansion(item, False))
        self.note_list.itemCollapsed.connect(lambda item: self.tree_expansion(item, True))
        self.note_list.customContextMenuRequested.connect(self.tree_context_menu)
        side.addWidget(self.note_list, 1)
        side.addWidget(label("記事を分類へドラッグして移動。\n同じ階層では上下へ並べ替え。", "muted", True))
        side.addWidget(label("通常の閲覧・編集に通信は不要です。", "muted", True))
        splitter.addWidget(sidebar)
        editor = QFrame()
        editor.setObjectName("panel")
        right = QVBoxLayout(editor)
        right.setContentsMargins(20, 14, 20, 12)
        right.setSpacing(8)
        actions = QHBoxLayout()
        self.save_status = label("ノートを選んでください", "muted")
        self.save_status.setMinimumWidth(0)
        self.save_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        actions.addWidget(self.save_status, 1)
        actions.addWidget(button("編集版を統合", lambda: self.resolve(merge=True)))
        actions.addWidget(button("この版を採用", lambda: self.resolve(merge=False)))
        self.delete_button = button("削除", self.delete_note, "danger")
        actions.addWidget(self.delete_button)
        actions.addWidget(button("保存", self.flush))
        right.addLayout(actions)
        self.fields = {}
        title = QLineEdit()
        title.setObjectName("title")
        title.setPlaceholderText("ノートのタイトル")
        title.textChanged.connect(self.mark_dirty)
        self.fields["title"] = title
        right.addWidget(title)
        metadata = QGridLayout()
        metadata.setHorizontalSpacing(12)
        for column, (field, caption) in enumerate((("notebook", "ノートブック"), ("section", "セクション"), ("tags", "タグ"))):
            metadata.addWidget(label(caption, "muted"), 0, column)
            entry = QLineEdit()
            entry.setPlaceholderText(caption)
            entry.textChanged.connect(self.mark_dirty)
            self.fields[field] = entry
            metadata.addWidget(entry, 1, column)
        right.addLayout(metadata)
        self.body = NoteText()
        self.body.setPlaceholderText("ここに書き始める…")
        self.body.setTabChangesFocus(False)
        self.body.setMinimumHeight(60)
        self.body.textChanged.connect(self.mark_dirty)
        self.body.image_pasted.connect(self.paste_image)
        self.body.zoom_requested.connect(self.change_zoom)
        right.addWidget(self.body, 1)
        attachment_top = QHBoxLayout()
        attachment_top.addWidget(label("画像・添付ファイル"))
        attachment_top.addStretch()
        attachment_top.addWidget(button("追加", self.attach))
        attachment_top.addWidget(button("表", lambda: self.edit_block("table")))
        attachment_top.addWidget(button("グラフ", lambda: self.edit_block("bar")))
        right.addLayout(attachment_top)
        self.attachment_scroll = QScrollArea()
        self.attachment_scroll.setWidgetResizable(True)
        self.attachment_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.resize_attachment_strip()
        right.addWidget(self.attachment_scroll)
        splitter.addWidget(editor)
        splitter.setSizes([285, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = label("", "muted")
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(button("－", lambda: self.change_zoom(-1)))
        self.zoom_label = button("本文 100%", self.reset_zoom)
        self.zoom_label.setToolTip("Ctrl＋ホイールで拡大・縮小 / Ctrl＋0で100%に戻す")
        footer.addWidget(self.zoom_label)
        footer.addWidget(button("＋", lambda: self.change_zoom(1)))
        layout.addLayout(footer)
        # Large accessibility fonts must not make controls unreachable. The
        # editor keeps its own text scroll; this outer scroll is only for layout.
        screen = QScrollArea()
        screen.setWidgetResizable(True)
        screen.setWidget(outer)
        self.setCentralWidget(screen)
        self.apply_body_preferences()
        self.refresh_list()
        first = next((revision for revision, item in self.tree_items.items()
                      if item.parent().isExpanded() and item.parent().parent().isExpanded()), None)
        if first:
            self.load_revision(first)
        self.update_status()

    def open_menu(self):
        menu = QMenu(self)
        for title, callback in (("設定", self.show_settings),
                                ("OneNote・テキストを取り込み", self.import_notes),
                                ("暗号化バックアップを書き出す", self.backup),
                                ("同期端末をすべて解除", self.revoke_peers),
                                ("保管庫の場所", lambda: QMessageBox.information(self, "保管庫", str(self.vault.root)))):
            action = menu.addAction(title)
            action.triggered.connect(callback)
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))

    def apply_ui_preferences(self):
        style = STYLE + preferences.ui_style(self.display_preferences)
        if getattr(self, "applied_ui_style", None) == style:
            return
        body = getattr(self, "body", None) if self.vault is not None else None
        # A stylesheet change is not a document edit; never rebuild the tree,
        # replace the text document or recreate attachment previews here.
        with QSignalBlocker(body) if body is not None else nullcontext():
            self.setStyleSheet(style)
        self.applied_ui_style = style
        if self.vault is not None and hasattr(self, "attachment_scroll"):
            self.resize_attachment_strip()

    def resize_attachment_strip(self):
        line = self.fontMetrics().height()
        self.attachment_scroll.setFixedHeight(max(148, max(70, line + 20) + 2 * line + 44))

    def apply_body_preferences(self):
        if self.vault is None:
            return
        # Only presentation changes: keep the document, cursor, undo history and
        # attachment cache. Font/zoom changes must never save a note revision.
        with QSignalBlocker(self.body):
            self.body.setStyleSheet(preferences.body_style(self.display_preferences))
        self.zoom_label.setText(f"本文 {self.display_preferences['zoom']}%")

    def change_zoom(self, steps):
        if self.vault is None:
            return
        zoom = preferences.stepped_zoom(self.display_preferences["zoom"], steps)
        if zoom == self.display_preferences["zoom"]:
            return
        self.display_preferences["zoom"] = zoom
        self.apply_body_preferences()
        self.preferences_pending = True
        self.preference_timer.start()

    def reset_zoom(self):
        if self.vault is None or self.display_preferences["zoom"] == 100:
            return
        self.display_preferences["zoom"] = 100
        self.apply_body_preferences()
        self.preferences_pending = True
        self.preference_timer.start()

    def persist_preferences(self):
        self.preference_timer.stop()
        if not self.preferences_pending:
            return
        try:
            preferences.save(self.data_dir, self.display_preferences)
            self.preferences_pending = False
        except OSError:
            if self.vault is not None:
                self.status.setText("表示設定を保存できませんでした。ノートの保存とは別のエラーです")

    def show_settings(self):
        if self.busy or self.vault is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("TwoNoteの設定")
        dialog.setMinimumWidth(560)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("表示", "heading"))
        form = QGridLayout()
        family = QFontComboBox()
        family.setCurrentFont(QFont(self.display_preferences["font_family"]))
        size = QSpinBox()
        size.setRange(6, 48)
        size.setSuffix(" pt")
        size.setValue(self.display_preferences["font_size"])
        zoom = QSpinBox()
        zoom.setRange(50, 300)
        zoom.setSingleStep(10)
        zoom.setSuffix(" %")
        zoom.setValue(self.display_preferences["zoom"])
        for row, (caption, entry) in enumerate((("画面全体のフォント", family), ("画面全体の文字サイズ", size), ("本文のみの表示倍率", zoom))):
            form.addWidget(label(caption), row, 0)
            form.addWidget(entry, row, 1)
        layout.addLayout(form)
        layout.addWidget(label("フォント・文字サイズはツリー、メニュー、ボタン、本文、設定画面に反映します。\n本文上のCtrl＋ホイール / Ctrl＋＋・－は本文のみ拡大縮小、Ctrl＋0で100%に戻します。\n表示設定は次回も保持します。ノートの内容や画像は変更しません。", "muted", True))
        layout.addWidget(label("ロック解除", "heading"))
        remember = unlock_choice(windows_unlock.available(self.vault.root))
        layout.addWidget(remember)
        layout.addWidget(label("パスワードは保存せず、Windows（DPAPI）で保護した保管庫の鍵をこのPCに保存します。\n同じWindowsアカウントを使える人・アプリも解除できるため、共有アカウントでは無効にしてください。Windows Hello認証ではありません。", "muted", True))
        layout.addWidget(label("自動解除は、アプリの終了・PCの再起動後も有効です。変更は下の「保存」で反映します。\n手動でロックした直後は自動解除しません。PCから離れるときはWindowsもロックしてください。\nバックアップ復元や別端末ではパスワードが必要です。この設定は同期・バックアップに含めません。", "muted", True))
        layout.addWidget(label("保存先", "heading"))
        layout.addWidget(label(str(self.vault.root), "muted", True))
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("キャンセル", dialog.reject))
        def apply():
            values = preferences.normalized({**self.display_preferences, "font_family": family.currentFont().family(),
                "font_size": size.value(), "zoom": zoom.value()})
            try:
                if remember.currentData():
                    windows_unlock.remember(self.vault)
                else:
                    windows_unlock.forget(self.vault.root)
            except Exception as exc:
                self.error("自動解除の設定を変更できません", exc)
                return
            # Display preferences contain no authentication state or secrets.
            self.display_preferences = values
            self.apply_ui_preferences()
            self.apply_body_preferences()
            self.preferences_pending = True
            self.persist_preferences()
            if self.preferences_pending:
                self.error("表示設定を保存できません", "この画面には反映しましたが、次回起動用に保存できませんでした。自動解除の設定は変更済みです。")
                return
            dialog.accept()
        actions.addWidget(button("保存", apply, "primary"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        shell = QVBoxLayout(dialog)
        shell.addWidget(scroll, 1)
        shell.addLayout(actions)
        available = QApplication.primaryScreen().availableGeometry()
        dialog.resize(600, min(700, available.height() - 80))
        self.viewers.append(dialog)
        dialog.exec()
        if dialog in self.viewers:
            self.viewers.remove(dialog)
        dialog.deleteLater()

    def update_status(self):
        if self.vault is None:
            return
        active = self.sync is not None and self.sync.active
        self.status.setText("スマホとの同期待受中 · 完了後に通信を終了します" if active else "このPCに暗号化して保存 · 同期停止中")
        self.sync_button.setText("同期待受を終了" if active else "スマホと同期")

    def refresh_list(self, *args):
        if self.vault is None or self.navigation is None:
            return
        if self.note_list.drag_active or self.selecting_tree:
            self.tree_refresh_pending = True
            return
        self.tree_refresh_pending = False
        previous = self.selected_tree_data()
        old_loading = self.loading
        self.loading = True
        self.note_list.blockSignals(True)
        try:
            groups = self.navigation.tree(self.search.text(), self.trash.isChecked())
            collapsed = set(self.navigation.state["collapsed"])
            scroll = self.note_list.verticalScrollBar().value()
            self.note_list.clear()
            self.tree_items = {}
            self.group_items = {}
            def make(parent, caption, data):
                item = QTreeWidgetItem(parent, [caption])
                item.setData(0, Qt.ItemDataRole.UserRole, data)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled
                if not data.get("blocked"):
                    flags |= Qt.ItemFlag.ItemIsDragEnabled
                item.setFlags(flags)
                item.setToolTip(0, "<qt>" + html.escape(caption) + "</qt>")
                return item
            for book in groups:
                name = book["name"]
                total = sum(len(section["notes"]) for section in book["sections"])
                parent = make(self.note_list, f"{name or '（未分類ノートブック）'}  ({total})", {"kind": "book", "book": name})
                self.group_items[group_key("book", name)] = parent
                for section in book["sections"]:
                    section_name = section["name"]
                    branch = make(parent, f"{section_name or '（セクション未指定）'}  ({len(section['notes'])})",
                                  {"kind": "section", "book": name, "section": section_name})
                    self.group_items[group_key("section", name, section_name)] = branch
                    for note in section["notes"]:
                        prefix = "[削除] " if note["deleted"] else ""
                        if note["conflicts"] > 1:
                            prefix += "[競合] "
                        caption = prefix + note["title"]
                        if note["conflicts"] > 1:
                            caption += " · " + note["revision"][:6]
                        item = make(branch, caption, {"kind": "note", "book": name, "section": section_name,
                            "note_id": note["note_id"], "revision": note["revision"],
                            "blocked": note["deleted"] or note["conflicts"] > 1})
                        self.tree_items[note["revision"]] = item
            selected = None
            if previous and previous["kind"] in ("book", "section"):
                selected = self.group_items.get(group_key(previous["kind"], previous["book"], previous.get("section", "")))
            elif self.current:
                selected = self.tree_items.get(self.current["revision"])
            if selected:
                self.note_list.setCurrentItem(selected)
            for key, item in self.group_items.items():
                item.setExpanded(key not in collapsed)
            self.note_list.verticalScrollBar().setValue(scroll)
            self.count.setText(f"{len(self.tree_items)}件")
        except Exception as exc:
            self.error("ツリーを表示できません", exc)
        finally:
            self.note_list.blockSignals(False)
            self.loading = old_loading

    def selected_tree_data(self):
        item = self.note_list.currentItem()
        return dict(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None

    def selected_route(self):
        data = self.selected_tree_data()
        if data:
            if data["kind"] == "note" and self.current and data["note_id"] == self.current["note_id"]:
                return self.fields["notebook"].text(), self.fields["section"].text()
            return data["book"], data.get("section", "")
        if self.current:
            return self.current["notebook"], self.current["section"]
        return "", ""

    def finish_tree_drag(self):
        if self.tree_refresh_pending:
            self.refresh_list()

    def tree_expansion(self, item, collapsed):
        if self.loading or self.navigation is None:
            return
        data = dict(item.data(0, Qt.ItemDataRole.UserRole) or {})
        if data.get("kind") in ("book", "section"):
            try:
                self.navigation.collapse(data["kind"], data["book"], data.get("section", ""), collapsed)
            except Exception as exc:
                self.error("開閉状態を保存できません", exc)

    def tree_drop(self, source, target, position):
        if self.vault is None or self.busy or not self.flush():
            return
        try:
            moved = self.navigation.apply_drop(source, target, position)
            if moved is not None:
                self.navigation.reveal(moved["notebook"], moved["section"])
                if self.current and self.current["note_id"] == moved["note_id"]:
                    self.current = moved
                    self.loading = True
                    try:
                        self.fields["notebook"].setText(moved["notebook"])
                        self.fields["section"].setText(moved["section"])
                    finally:
                        self.loading = False
                    self.save_status.setText("移動・表示順を保存しました")
            self.refresh_list()
        except Exception as exc:
            self.refresh_list()
            self.error("移動できません", exc)

    def reorder_selected(self, direction):
        item = self.note_list.currentItem()
        if item is None:
            return
        parent = item.parent()
        index = parent.indexOfChild(item) if parent else self.note_list.indexOfTopLevelItem(item)
        count = parent.childCount() if parent else self.note_list.topLevelItemCount()
        if not 0 <= index + direction < count:
            return
        target = parent.child(index + direction) if parent else self.note_list.topLevelItem(index + direction)
        self.tree_drop(dict(item.data(0, Qt.ItemDataRole.UserRole)),
                       dict(target.data(0, Qt.ItemDataRole.UserRole)), "before" if direction < 0 else "after")

    def folder_menu(self):
        menu = QMenu(self)
        menu.addAction("新しいノートブック", lambda: self.new_folder("book"))
        menu.addAction("選択した場所に新しいセクション", lambda: self.new_folder("section"))
        menu.exec(self.note_list.mapToGlobal(self.note_list.rect().topLeft()))

    def new_folder(self, kind):
        if self.busy or self.vault is None:
            return
        book, section = self.selected_route()
        caption = "ノートブック" if kind == "book" else "セクション"
        name, accepted = QInputDialog.getText(self, "新しい" + caption, caption + "名")
        if not accepted or not name.strip():
            return
        name = name.strip()
        if len(name) > 256:
            self.error("名前が長すぎます", "256文字以内で入力してください")
            return
        if not self.flush():
            return
        try:
            if kind == "book":
                book, section = name, ""
                self.navigation.add_folder(book)
            else:
                section = name
                self.navigation.add_folder(book, section)
            self.navigation.reveal(book, section)
            self.refresh_list()
            item = self.group_items[group_key(kind, book, section)]
            self.note_list.setCurrentItem(item)
            self.note_list.scrollToItem(item)
        except Exception as exc:
            self.error("分類を作成できません", exc)

    def tree_context_menu(self, position):
        item = self.note_list.itemAt(position)
        if item is not None:
            self.note_list.setCurrentItem(item)
        data = self.selected_tree_data()
        menu = QMenu(self)
        menu.addAction("この場所に新しい記事", self.new_note)
        menu.addAction("新しいノートブック", lambda: self.new_folder("book"))
        menu.addAction("新しいセクション", lambda: self.new_folder("section"))
        if data:
            menu.addSeparator()
            if data["kind"] == "section":
                menu.addAction("セクション名を変更…", lambda: self.rename_section(data))
            menu.addAction("上へ移動", lambda: self.reorder_selected(-1))
            menu.addAction("下へ移動", lambda: self.reorder_selected(1))
            if data["kind"] != "note":
                menu.addAction("空の分類を削除", lambda: self.remove_folder(data))
        menu.exec(self.note_list.viewport().mapToGlobal(position))

    def rename_section(self, data):
        if self.busy or self.vault is None or data.get("kind") != "section":
            return
        book, section = data["book"], data["section"]
        name, accepted = QInputDialog.getText(self, "セクション名を変更", "新しいセクション名", text=section)
        if not accepted:
            return
        name = name.strip()
        if not name or len(name) > 256:
            self.error("名前を変更できません", "セクション名は1〜256文字で入力してください")
            return
        if name == section or not self.flush():
            return
        try:
            updates = self.navigation.rename_section(book, section, name)
            if self.current and self.current["revision"] in updates:
                self.current = updates[self.current["revision"]]
                old_loading = self.loading
                self.loading = True
                try:
                    self.fields["section"].setText(self.current["section"])
                finally:
                    self.loading = old_loading
            self.refresh_list()
            item = self.group_items.get(group_key("section", book, name))
            if item is not None:
                self.note_list.setCurrentItem(item)
                self.note_list.scrollToItem(item)
            self.save_status.setText("セクション名を変更しました")
        except Exception as exc:
            self.refresh_list()
            self.error("名前を変更できません", exc)

    def remove_folder(self, data):
        if self.busy or not self.flush():
            return
        if QMessageBox.question(self, "空の分類を削除", "記事がない分類だけを削除しますか？") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.navigation.remove_empty(data["kind"], data["book"], data.get("section", ""))
            self.refresh_list()
        except Exception as exc:
            self.error("分類を削除できません", exc)

    def select_note(self, item, previous=None):
        if self.loading or item is None:
            return
        # Copy identity before flush refreshes and destroys old list items.
        data = dict(item.data(0, Qt.ItemDataRole.UserRole))
        self.selecting_tree = True
        try:
            if data["kind"] != "note":
                self.flush()
                return
            revision = data["revision"]
            if self.current and revision == self.current["revision"]:
                return
            if self.flush():
                self.load_revision(revision)
            elif self.current:
                current_item = self.tree_items.get(self.current["revision"])
                if current_item is not None:
                    with QSignalBlocker(self.note_list):
                        self.note_list.setCurrentItem(current_item)
        finally:
            self.selecting_tree = False
            if self.tree_refresh_pending:
                QTimer.singleShot(0, self.refresh_list)

    def load_revision(self, revision):
        note = self.vault.get(revision)
        self.loading = True
        try:
            self.current = note
            for field, entry in self.fields.items():
                entry.setEnabled(True)
                entry.setText(note[field])
            self.body.setEnabled(True)
            self.body.load_body(note["content"], note["attachments"], self.vault)
            self.dirty = False
            self.save_timer.stop()
            self.save_status.setText("削除済み" if note["deleted"] else "保存済み")
            self.delete_button.setText("復元" if note["deleted"] else "削除")
            self.navigation.reveal(note["notebook"], note["section"])
            self.note_list.blockSignals(True)
            item = self.tree_items.get(revision)
            if item is not None:
                self.note_list.setCurrentItem(item)
                item.parent().setExpanded(True)
                item.parent().parent().setExpanded(True)
                self.note_list.scrollToItem(item)
            self.note_list.blockSignals(False)
            self.render_attachments()
        finally:
            self.loading = False

    def mark_dirty(self, *args):
        if self.loading or self.vault is None or self.current is None:
            return
        self.dirty = True
        self.save_status.setText("未保存")
        self.save_timer.start()

    def flush(self):
        self.save_timer.stop()
        if self.vault is None or self.current is None or not self.dirty:
            return True
        try:
            # Synchronous local commit on the UI thread: no response can race with
            # newer keystrokes or a different selected note. Sync retains branches.
            values = {field: entry.text() for field, entry in self.fields.items()}
            values.update(content=self.body.saved_content(), source_path=self.current.get("source_path", ""),
                          attachments=self.current["attachments"], deleted=False)
            self.current = self.vault.save(self.current["note_id"], [self.current["revision"]], values)
            self.dirty = False
            self.save_status.setText("保存済み")
            self.delete_button.setText("削除")
            self.refresh_list()
            return True
        except Exception as exc:
            self.save_status.setText("保存できません · 編集内容を保持中")
            self.error("保存できません", exc)
            return False

    def new_note(self):
        book, section = self.selected_route()
        if not self.flush():
            return
        try:
            note = self.vault.save(uuid.uuid4().hex, [], {"title": "無題", "notebook": book, "section": section})
            self.refresh_list()
            self.load_revision(note["revision"])
            self.fields["title"].setFocus()
            self.fields["title"].selectAll()
        except Exception as exc:
            self.error("作成できません", exc)

    def delete_note(self):
        if self.current is None or not self.flush():
            return
        selected = self.current["revision"]
        deleted = not self.current["deleted"]
        action = "削除" if deleted else "復元"
        if QMessageBox.question(self, f"編集版を{action}", f"選択中の編集版を{action}しますか？他の編集版は変更しません。削除した版は「削除したノートも表示」から復元できます。") != QMessageBox.StandardButton.Yes:
            return
        try:
            note = self.vault.set_deleted(selected, deleted)
            self.current = note
            self.refresh_list()
            if not deleted or self.trash.isChecked():
                self.load_revision(note["revision"])
            else:
                remaining = [n for n in self.vault.heads() if n["note_id"] == note["note_id"]]
                if remaining: self.load_revision(remaining[0]["revision"])
                else: self.clear_current()
        except Exception as exc:
            self.error(f"{action}できません", exc)

    def clear_current(self):
        self.loading = True
        try:
            self.current = None
            self.dirty = False
            self.save_timer.stop()
            for entry in self.fields.values():
                entry.clear()
                entry.setEnabled(False)
            self.body.load_body(inline.encode(""), [], self.vault)
            self.body.setEnabled(False)
            self.thumbnail_cache.clear()
            old = self.attachment_scroll.takeWidget()
            if old: old.deleteLater()
            self.save_status.setText("ノートを選んでください")
            self.delete_button.setText("削除")
        finally:
            self.loading = False

    def resolve(self, merge=True):
        if self.current is None or not self.flush():
            return
        heads = [n for n in self.vault.heads(True) if n["note_id"] == self.current["note_id"]]
        if len(heads) < 2:
            QMessageBox.information(self, "編集版", "このノートに競合する編集版はありません")
            return
        if self.current["deleted"]:
            QMessageBox.information(self, "編集版", "残す編集版を選んでください。削除済みの版が必要な場合は先に復元してください。")
            return
        title = "編集版を統合" if merge else "この版を採用"
        message = "削除されていない版の本文と添付をまとめます。削除済みの内容は復活させません。" if merge else "表示中の本文・画像・表・グラフだけを残します。他の版は変更履歴に残りますが、一覧からは消えます。"
        expected = [n["revision"] for n in heads]
        selected = self.current["revision"]
        if QMessageBox.question(self, title, message) != QMessageBox.StandardButton.Yes:
            return
        try:
            note = self.vault.resolve_versions(selected, expected, merge=merge)
            self.refresh_list()
            self.load_revision(note["revision"])
        except Exception as exc:
            self.error("編集版を確定できません", exc)

    def render_attachments(self):
        active = {a["id"] for a in self.current["attachments"]}
        self.thumbnail_cache = {key: value for key, value in self.thumbnail_cache.items() if key in active}
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 3)
        layout.setSpacing(10)
        for item in self.current["attachments"]:
            tile = QFrame()
            tile.setObjectName("attachment")
            tile.setMinimumWidth(155)
            box = QVBoxLayout(tile)
            box.setContentsMargins(7, 6, 7, 5)
            box.setSpacing(4)
            picture = button("ファイル", lambda checked=False, a=item: self.open_attachment(a))
            picture.setMinimumHeight(70)
            try:
                if item["mime"] != "image/svg+xml":
                    if item["id"] not in self.thumbnail_cache:
                        preview = self.body.inline_cache.get(item["id"])
                        self.thumbnail_cache[item["id"]] = preview.scaled(QSize(130, 64), Qt.AspectRatioMode.KeepAspectRatio) if preview is not None else image_preview(self.vault.read_blob(item["id"]), QSize(130, 64))
                    image = self.thumbnail_cache[item["id"]]
                    if not image.isNull():
                        picture.setText("")
                        picture.setIcon(QIcon(QPixmap.fromImage(image)))
                        picture.setIconSize(image.size())
            except Exception:
                picture.setText("プレビューなし")
            box.addWidget(picture)
            name = label(item["filename"])
            name.setToolTip("<qt>" + html.escape(item["filename"]) + "</qt>")
            name.setMaximumWidth(140)
            box.addWidget(name)
            controls = QHBoxLayout()
            open_button = button("データ編集", lambda checked=False, a=item: self.edit_block(item=a)) if item["mime"] == inline.BLOCK_MIME else button("開く", lambda checked=False, a=item: self.open_attachment(a))
            remove_button = button("外す", lambda checked=False, a=item: self.detach(a))
            for control in (open_button, remove_button):
                control.setStyleSheet("padding:3px 9px;")
                controls.addWidget(control)
            if inline.supported(item):
                controls.addWidget(button("本文へ", lambda checked=False, a=item: self.insert_attachment(a)))
            box.addLayout(controls)
            layout.addWidget(tile)
        if not self.current["attachments"]:
            layout.addWidget(label("画像は「追加」または本文欄への貼り付けで保存できます。", "muted"))
        layout.addStretch()
        old = self.attachment_scroll.takeWidget()
        if old:
            old.deleteLater()
        self.attachment_scroll.setWidget(container)

    def insert_attachment(self, item, save=True):
        if self.current is None or not inline.supported(item): return
        self.body.inline_files[item["id"]] = item
        self.body.inline_mode = True
        self.body.insert_portable(inline.token(item["id"]))
        self.body.setFocus()
        self.dirty = True
        if save: self.flush()

    def edit_block(self, kind="table", item=None):
        if self.current is None or not self.flush(): return
        try:
            target_vault, epoch = self.vault, self.session
            base_revision = self.current["revision"]
            snapshot_text = self.body.portable_text()
            cursor = self.body.textCursor()
            at = len(self.body.portable_text(0, cursor.selectionStart()))
            stop = len(self.body.portable_text(0, cursor.selectionEnd()))
            value = blocks.load(self.vault.read_blob(item["id"])) if item else {"kind": kind, "title": "", "rows": [["項目", "内容"], ["例", "値"]] if kind == "table" else [["A", "10"], ["B", "20"], ["C", "15"]], "version": 1}
            dialog = QDialog(self); dialog.setWindowTitle("表・グラフのデータ")
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("カンマ区切り、またはExcel等からタブ区切りで貼り付け。表は50行×12列、グラフは項目名,数値の30行まで。"))
            heading = QLineEdit(value.get("title", "")); heading.setPlaceholderText("見出し（任意）"); layout.addWidget(heading)
            choice = QComboBox()
            for caption, code in (("表", "table"), ("棒グラフ", "bar"), ("折れ線グラフ", "line")): choice.addItem(caption, code)
            choice.setCurrentIndex(choice.findData(value["kind"])); layout.addWidget(choice)
            data = QPlainTextEdit(); data.setPlainText(blocks.csv_text(value)); layout.addWidget(data)
            def save():
                try:
                    if self.vault is not target_vault or self.session != epoch:
                        raise ValueError("保管庫が切り替わったため保存できません")
                    value = blocks.parse(choice.currentData(), heading.text(), data.toPlainText())
                    saved = blocks.save_edit(target_vault, base_revision, value, item["id"] if item else None, at, stop, snapshot_text)
                    self.load_revision(saved["revision"])
                    self.refresh_list(); dialog.accept()
                    if len([n for n in target_vault.heads() if n["note_id"] == saved["note_id"]]) > 1:
                        self.save_status.setText("編集中に別の変更がありました · 両方の版を保持しています")
                except Exception as exc: self.error("表・グラフを保存できません", exc)
            buttons = QHBoxLayout(); buttons.addWidget(button("保存", save)); buttons.addWidget(button("キャンセル", dialog.reject)); layout.addLayout(buttons)
            dialog.resize(620, 460); dialog.exec()
        except Exception as exc: self.error("表・グラフを開けません", exc)

    def attach(self):
        if self.current is None or not self.flush():
            return
        names, _ = QFileDialog.getOpenFileNames(self, "画像・ファイルを追加")
        if not names:
            return
        try:
            for name in names:
                path = Path(name)
                item = self.vault.add_blob(path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.read_bytes())
                self.current["attachments"].append(item)
                if inline.supported(item): self.insert_attachment(item, save=False)
            self.dirty = True
            self.flush()
            self.render_attachments()
        except Exception as exc:
            self.dirty = True
            self.error("追加できません", exc)

    def paste_image(self, image):
        if self.current is None or not self.flush():
            return
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            item = self.vault.add_blob("貼り付け画像.png", "image/png", bytes(buffer.data()))
            self.current["attachments"].append(item)
            self.insert_attachment(item, save=False)
            self.dirty = True
            self.flush()
            self.render_attachments()
        except Exception as exc:
            self.error("貼り付けできません", exc)

    def detach(self, item):
        # Keep existing image objects as undoable text tokens rather than erasing
        # any surrounding text. The attachment itself is removed only explicitly.
        text = self.body.portable_text().replace(inline.token(item["id"]), "")
        self.current["attachments"] = [a for a in self.current["attachments"] if a["id"] != item["id"]]
        self.body.load_body(inline.encode(text), self.current["attachments"], self.vault)
        self.dirty = True
        if self.flush():
            self.render_attachments()

    def open_attachment(self, item):
        try:
            raw = self.vault.read_blob(item["id"])
            image = render_block(blocks.load(raw)) if item["mime"] == inline.BLOCK_MIME else QImage() if item["mime"] == "image/svg+xml" else image_preview(raw, QSize(1400, 1000))
            if not image.isNull():
                viewer = QDialog(self)
                viewer.setWindowTitle(item["filename"])
                layout = QVBoxLayout(viewer)
                scroll = QScrollArea()
                picture = QLabel()
                picture.setPixmap(QPixmap.fromImage(image))
                scroll.setWidget(picture)
                layout.addWidget(scroll)
                available = QApplication.primaryScreen().availableGeometry()
                viewer.resize(min(image.width() + 45, available.width() - 80), min(image.height() + 45, available.height() - 80))
                self.viewers.append(viewer)
                viewer.show()
            else:
                name, _ = QFileDialog.getSaveFileName(self, "添付を書き出す（書き出し先は暗号化されません）", item["filename"])
                if name:
                    Path(name).write_bytes(raw)
        except Exception as exc:
            self.error("添付を開けません", exc)

    def toggle_sync(self):
        if not self.flush():
            return
        if self.sync.active:
            self.sync.stop()
            return
        try:
            self.sync.start(port=self.sync_port)
            dialog = QDialog(self)
            self.sync_dialog = dialog
            dialog.setWindowTitle("スマホと同期")
            dialog.setMinimumWidth(460)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(24, 22, 24, 22)
            layout.setSpacing(12)
            layout.addWidget(label("初回はAndroidでQRを読み取ります", "heading", True))
            layout.addWidget(label("登録済みのスマホは「今すぐ同期」を押してください。同期完了後に通信を終了します。", "muted", True))
            address = QComboBox()
            address.addItems(lan_addresses())
            layout.addWidget(address)
            picture = QLabel()
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(picture)
            code = QPlainTextEdit()
            code.setReadOnly(True)
            code.setMinimumHeight(76)
            layout.addWidget(code)
            def update():
                import qrcode
                uri = self.sync.pairing_uri(address.currentText())
                code.setPlainText(uri)
                output = io.BytesIO()
                qrcode.make(uri, box_size=4, border=4).save(output, format="PNG")
                image = QImage.fromData(output.getvalue())
                picture.setPixmap(QPixmap.fromImage(image))
            address.currentTextChanged.connect(update)
            update()
            layout.addWidget(button("ペアリングコードをコピー", lambda: QApplication.clipboard().setText(code.toPlainText())))
            countdown = label("", "muted")
            layout.addWidget(countdown)
            timer = QTimer(dialog)
            timer.setInterval(1000)
            timer.timeout.connect(lambda: countdown.setText(f"同期待受は最大 {max(0, int(self.sync.expires - time.monotonic()))} 秒で終了します") if self.sync else None)
            timer.start()
            host = self.sync
            server = host.server
            layout.addWidget(button("同期待受を終了", lambda: host.stop(server)))
            dialog.finished.connect(lambda _: host.stop(server))
            self.viewers.append(dialog)
            dialog.show()
            self.update_status()
        except Exception as exc:
            self.sync.stop()
            self.error("同期を開始できません", exc)

    @Slot(str, int)
    def sync_event(self, event, epoch):
        if epoch != self.session or self.vault is None:
            return
        self.update_status()
        if event == "changed":
            self.refresh_list()
            if self.current and not self.dirty:
                heads = [n for n in self.vault.heads(self.trash.isChecked() and self.current["deleted"]) if n["note_id"] == self.current["note_id"]]
                if len(heads) == 1 and heads[0]["revision"] != self.current["revision"]:
                    self.load_revision(heads[0]["revision"])
                elif sum(not n["deleted"] for n in heads) > 1:
                    self.save_status.setText("別の編集版があります · 両方を保持しています")
                elif not heads:
                    self.clear_current()
        if event == "stopped" and not self.sync.active and self.sync_dialog:
            self.sync_dialog.close()
            self.sync_dialog = None

    def revoke_peers(self):
        if QMessageBox.question(self, "同期端末を解除", "登録した全端末の同期資格を失効させます。再接続には新しいQRの読み取りが必要です。") == QMessageBox.StandardButton.Yes:
            self.sync.revoke_all()
            QMessageBox.information(self, "解除しました", "端末登録を解除しました")

    def backup(self):
        if not self.flush():
            return
        name, _ = QFileDialog.getSaveFileName(self, "暗号化バックアップ", "TwoNote-backup.twonote", "TwoNote (*.twonote)")
        if not name:
            return
        try:
            export_backup(self.vault, Path(name))
            QMessageBox.information(self, "バックアップ", "暗号化バックアップを保存しました。復元には保管庫のパスワードが必要です。")
        except Exception as exc:
            self.error("バックアップできません", exc)

    def import_notes(self):
        if self.busy or not self.flush():
            return
        name, _ = QFileDialog.getOpenFileName(self, "OneNote・テキストを取り込み", "", "Notes (*.onepkg *.one *.mht *.mhtml *.html *.htm *.txt *.md);;All files (*)")
        if not name:
            return
        self.busy = True
        self.status.setText("取り込み中…")
        epoch, vault = self.session, self.vault
        def run():
            try:
                from .importing import import_into
                self.events.imported.emit(import_into(vault, Path(name)), epoch)
            except Exception as exc:
                self.events.failed.emit(str(exc), epoch)
        threading.Thread(target=run, daemon=True).start()

    @Slot(object, int)
    def import_finished(self, report, epoch):
        if epoch != self.session:
            return
        self.busy = False
        self.refresh_list()
        if self.current:
            heads = [n for n in self.vault.heads(True) if n["note_id"] == self.current["note_id"]]
            if len(heads) == 1 and heads[0]["revision"] != self.current["revision"]:
                if not self.dirty:
                    self.load_revision(heads[0]["revision"])
                else:
                    self.save_status.setText("原本から更新されました · 未保存の編集は別の版として保持します")
        self.update_status()
        text = f"ノート {report['imported']}件、添付 {report['attachments']}件を取り込みました。既存 {report['skipped']}件のうち、未編集の本文 {report.get('positioned', 0)}件に画像・表の位置を補いました。"
        if report["warnings"]:
            text += "\n一部を読み取れませんでした:\n" + "\n".join(report["warnings"][:12])
        QMessageBox.information(self, "取り込み結果", text)

    def lock(self):
        if self.vault is None or not self.flush():
            return
        # Import runs in the background. Locking invalidates its session and key;
        # a late callback cannot unlock the screen or continue publishing notes.
        self.busy = False
        self.persist_preferences()
        self.sync.stop()
        self.session += 1
        self.loading = True
        self.body.load_body(inline.encode(""), [], None)
        for entry in self.fields.values():
            entry.clear()
        self.fields.clear()
        self.tree_items.clear()
        self.group_items.clear()
        self.navigation = None
        self.loading = False
        self.vault.lock()
        self.vault = None
        self.sync = None
        self.show_login()

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "処理中", "保管庫の処理が完了してから閉じてください")
            event.ignore()
            return
        if not self.flush():
            event.ignore()
            return
        self.capture_window_geometry()
        self.persist_preferences()
        if self.sync:
            self.sync.stop()
        if self.vault:
            self.vault.lock()
            self.vault = None
        self.save_timer.stop()
        self.dismiss_viewers()
        self.instance_lock.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="TwoNote Qt desktop")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--sync-port", type=int, default=8001)
    parser.add_argument("--self-test-report", type=Path, help="人工データだけで自己診断し、JSONを出力して終了")
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--host", help=argparse.SUPPRESS)
    parser.add_argument("--https", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test_report:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setApplicationName("TwoNote")
    app.setWindowIcon(app_icon())
    if args.self_test_report:
        from .diagnostics import self_test
        return self_test(args.self_test_report)
    try:
        window = Desktop(args.data_dir, args.sync_port)
        window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(None, "TwoNote", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
