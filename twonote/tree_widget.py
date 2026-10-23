"""Tree drag/drop transports identities only; the controller commits changes."""
import uuid

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QAbstractItemView, QFrame, QTreeWidget

from .navigation import drop_plan

MIME = "application/x-twonote-tree-node"


class NoteTree(QTreeWidget):
    dropped = Signal(object, object, str)
    drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(18)
        self.setSortingEnabled(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAutoExpandDelay(650)
        self.setAutoScroll(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.drag_active = False
        self.drag_source = None
        self.token = ""
        self.pending_drop = None
        self.marker = QFrame(self.viewport())
        self.marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.marker.hide()

    def mimeTypes(self):
        return [MIME]

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        source = dict(item.data(0, Qt.ItemDataRole.UserRole) or {})
        if source.get("blocked") or not source.get("kind"):
            return
        self.drag_source = source
        self.pending_drop = None
        self.token = uuid.uuid4().hex
        self.drag_active = True
        mime = QMimeData()
        mime.setData(MIME, self.token.encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            pending = self.pending_drop
            self.pending_drop = None
            self.drag_active = False
            self.drag_source = None
            self.token = ""
            self.marker.hide()
            drag.deleteLater()
            def finish():
                if pending is not None:
                    self.dropped.emit(*pending)
                self.drag_finished.emit()
            QTimer.singleShot(0, finish)

    def _ours(self, event):
        return (event.source() is self and self.drag_source is not None
                and bytes(event.mimeData().data(MIME)) == self.token.encode("ascii"))

    def _target(self, event):
        point = event.position().toPoint()
        item = self.itemAt(point)
        if item is None:
            return None, "viewport", None
        target = dict(item.data(0, Qt.ItemDataRole.UserRole) or {})
        rect = self.visualItemRect(item)
        if self.drag_source["kind"] == "note" and target.get("kind") in ("book", "section"):
            position = "on"
        else:
            position = "before" if point.y() < rect.center().y() else "after"
        return target, position, rect

    def dragEnterEvent(self, event):
        if self._ours(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._ours(event):
            self.marker.hide()
            event.ignore()
            return
        super().dragMoveEvent(event)  # Native hover expansion and auto-scroll.
        target, position, rect = self._target(event)
        try:
            drop_plan(self.drag_source, target, position)
        except ValueError:
            self.marker.hide()
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        if rect is not None:
            if position == "on":
                self.marker.setStyleSheet("background:transparent; border:2px solid #6dd3cf;")
                self.marker.setGeometry(rect)
            else:
                self.marker.setStyleSheet("background:#6dd3cf; border:0;")
                self.marker.setGeometry(rect.x(), rect.top() if position == "before" else rect.bottom(), rect.width(), 2)
            self.marker.show()
            self.marker.raise_()
        else:
            self.marker.hide()

    def dragLeaveEvent(self, event):
        self.marker.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.marker.hide()
        if not self._ours(event):
            event.ignore()
            return
        source = dict(self.drag_source)
        target, position, rect = self._target(event)
        try:
            drop_plan(source, target, position)
        except ValueError:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        # Never mutate QTreeWidget's model ahead of the encrypted disk commit.
        # Copies remain valid even if saving/rebuilding deletes the Qt items.
        self.pending_drop = (source, target, position)
