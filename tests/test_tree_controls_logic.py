"""Execute actual drag/selection handlers on stand-ins, without Qt or graphics."""
import ast
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from twonote.navigation import drop_plan, group_key

QT = types.SimpleNamespace(ItemDataRole=types.SimpleNamespace(UserRole=256), DropAction=types.SimpleNamespace(MoveAction=2))


def controller(filename, name, methods, namespace):
    path = Path(__file__).resolve().parents[1] / "twonote" / filename
    source = ast.parse(path.read_text(encoding="utf-8"))
    original = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == name)
    body = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    target = ast.ClassDef(name=name, bases=[], keywords=[], body=body, decorator_list=[], type_params=[])
    tree = ast.fix_missing_locations(ast.Module(body=[target], type_ignores=[]))
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace[name]()


class TreeControlsTests(unittest.TestCase):
    def tree_and_event(self, source_matches=True, y=2):
        tree = controller("tree_widget.py", "NoteTree", {"_ours", "_target", "dropEvent"}, {"Qt": QT, "MIME": "fixture", "drop_plan": drop_plan})
        tree.token = "nonce"
        tree.drag_source = {"kind": "note", "note_id": "a", "book": "Old", "section": "A"}
        target = {"kind": "note", "note_id": "b", "book": "New", "section": "B"}
        tree.itemAt = lambda point: types.SimpleNamespace(data=lambda column, role: target)
        tree.visualItemRect = lambda item: types.SimpleNamespace(center=lambda: types.SimpleNamespace(y=lambda: 10))
        tree.marker = Mock()
        tree.pending_drop = None
        event = types.SimpleNamespace(source=lambda: tree if source_matches else object(),
            mimeData=lambda: types.SimpleNamespace(data=lambda fmt: b"nonce"),
            position=lambda: types.SimpleNamespace(toPoint=lambda: types.SimpleNamespace(y=lambda: y)),
            setDropAction=Mock(), accept=Mock(), ignore=Mock())
        return tree, event, target

    def test_drop_defers_mutation_and_copies_identity_before_items_are_rebuilt(self):
        tree, event, target = self.tree_and_event()
        tree.dropEvent(event)
        event.accept.assert_called_once()
        self.assertEqual(tree.pending_drop[2], "before")
        target["book"] = "changed"
        tree.drag_source["book"] = "changed"
        self.assertEqual(tree.pending_drop[0]["book"], "Old")
        self.assertEqual(tree.pending_drop[1]["book"], "New")

    def test_external_drop_is_rejected(self):
        tree, event, target = self.tree_and_event(False)
        tree.dropEvent(event)
        event.ignore.assert_called_once()
        self.assertIsNone(tree.pending_drop)

    def test_row_lower_half_inserts_after_target(self):
        tree, event, target = self.tree_and_event(y=18)
        tree.dropEvent(event)
        self.assertEqual(tree.pending_drop[2], "after")

    def test_selection_never_rebuilds_items_inside_native_selection_callback(self):
        timers = []
        desktop = controller("desktop.py", "Desktop", {"select_note"},
            {"Qt": QT, "QTimer": types.SimpleNamespace(singleShot=lambda delay, action: timers.append(action))})
        desktop.loading = False
        desktop.current = {"revision": "old"}
        desktop.selecting_tree = False
        desktop.tree_refresh_pending = False
        def save():
            self.assertTrue(desktop.selecting_tree)
            desktop.tree_refresh_pending = True
            return True
        desktop.flush = save
        desktop.load_revision = Mock()
        desktop.refresh_list = Mock()
        item = types.SimpleNamespace(data=lambda column, role: {"kind": "note", "revision": "next"})
        desktop.select_note(item)
        desktop.load_revision.assert_called_once_with("next")
        desktop.refresh_list.assert_not_called()
        self.assertFalse(desktop.selecting_tree)
        self.assertEqual(timers, [desktop.refresh_list])

    def test_moving_current_note_updates_metadata_not_text_or_image_widgets(self):
        desktop = controller("desktop.py", "Desktop", {"tree_drop"}, {})
        desktop.vault = object()
        desktop.busy = False
        desktop.flush = Mock(return_value=True)
        desktop.current = {"note_id": "a", "revision": "old", "content": "keep caret and undo"}
        moved = {**desktop.current, "revision": "new", "notebook": "B", "section": "S"}
        desktop.navigation = types.SimpleNamespace(apply_drop=Mock(return_value=moved), reveal=Mock())
        desktop.fields = {"notebook": Mock(), "section": Mock()}
        desktop.body = Mock()
        desktop.save_status = Mock()
        desktop.refresh_list = Mock()
        desktop.error = Mock()
        desktop.tree_drop({"kind": "note"}, {"kind": "book"}, "on")
        self.assertEqual(desktop.current, moved)
        self.assertEqual(desktop.body.mock_calls, [])
        desktop.fields["notebook"].setText.assert_called_once_with("B")
        desktop.error.assert_not_called()

    def rename_controller(self, response=("直した名前", True)):
        dialog = types.SimpleNamespace(getText=Mock(return_value=response))
        desktop = controller("desktop.py", "Desktop", {"rename_section"},
                             {"QInputDialog": dialog, "group_key": group_key})
        desktop.busy = False
        desktop.loading = False
        desktop.vault = object()
        desktop.current = {"note_id": "id", "revision": "old", "section": "文字化け"}
        desktop.flush = Mock(return_value=True)
        renamed = {**desktop.current, "revision": "new", "section": "直した名前"}
        desktop.navigation = types.SimpleNamespace(rename_section=Mock(return_value={"old": renamed}))
        desktop.fields = {"section": Mock()}
        desktop.body = Mock()
        desktop.note_list = Mock()
        desktop.group_items = {group_key("section", "Book", "直した名前"): object()}
        desktop.save_status = Mock()
        desktop.refresh_list = Mock()
        desktop.error = Mock()
        return desktop, dialog

    def test_rename_flushes_draft_and_updates_current_revision_without_reloading_body(self):
        desktop, dialog = self.rename_controller()
        def rename(*args):
            desktop.flush.assert_called_once()
            return {"old": {**desktop.current, "revision": "new", "section": "直した名前"}}
        desktop.navigation.rename_section.side_effect = rename
        desktop.rename_section({"kind": "section", "book": "Book", "section": "文字化け"})
        self.assertEqual(dialog.getText.call_args.kwargs["text"], "文字化け")
        desktop.navigation.rename_section.assert_called_once_with("Book", "文字化け", "直した名前")
        self.assertEqual(desktop.current["revision"], "new")
        desktop.fields["section"].setText.assert_called_once_with("直した名前")
        self.assertEqual(desktop.body.mock_calls, [])
        self.assertFalse(desktop.loading)
        desktop.note_list.setCurrentItem.assert_called_once()
        desktop.error.assert_not_called()

    def test_rename_cancel_invalid_name_or_failed_flush_does_not_rename(self):
        for response in (("", False), (" ", True), ("x" * 257, True), ("文字化け", True)):
            with self.subTest(response=response):
                desktop, dialog = self.rename_controller(response)
                desktop.rename_section({"kind": "section", "book": "Book", "section": "文字化け"})
                desktop.navigation.rename_section.assert_not_called()
        desktop, dialog = self.rename_controller()
        desktop.flush.return_value = False
        desktop.rename_section({"kind": "section", "book": "Book", "section": "文字化け"})
        desktop.navigation.rename_section.assert_not_called()

    def test_rename_failure_keeps_current_revision_and_reports_error(self):
        desktop, dialog = self.rename_controller()
        desktop.navigation.rename_section.side_effect = OSError("disk full")
        desktop.rename_section({"kind": "section", "book": "Book", "section": "文字化け"})
        self.assertEqual(desktop.current["revision"], "old")
        desktop.fields["section"].setText.assert_not_called()
        desktop.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
