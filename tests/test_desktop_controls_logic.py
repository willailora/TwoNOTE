"""Run actual event-handler bodies with plain-Python stand-ins, never Qt/GUI."""
import ast
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from twonote import preferences


class Signal:
    def __init__(self, *args):
        self.values = []

    def emit(self, *args):
        self.values.append(args)


class NoRenderingText:
    def __init__(self, parent=None):
        self.normal_scroll = 0
        self.text = "unchanged note"
        self.styles = []

    def wheelEvent(self, event):
        self.normal_scroll += 1

    def setStyleSheet(self, value):
        self.styles.append(value)


class Blocker:
    def __init__(self, widget):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def handlers(class_name, methods, namespace, bases=None, extra=None):
    source = Path(__file__).resolve().parents[1] / "twonote" / "desktop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    selected = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    target = ast.ClassDef(name=class_name, bases=bases or [], keywords=[], body=(extra or []) + selected, decorator_list=[], type_params=[])
    module = ast.fix_missing_locations(ast.Module(body=[target], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    return namespace[class_name]


class ControlsTests(unittest.TestCase):
    def test_ctrl_wheel_is_zoom_and_normal_wheel_remains_scroll(self):
        namespace = {"QPlainTextEdit": NoRenderingText, "preferences": preferences,
                     "Qt": types.SimpleNamespace(KeyboardModifier=types.SimpleNamespace(ControlModifier=1)), "Signal": Signal}
        note_type = handlers("NoteText", {"__init__", "wheelEvent"}, namespace,
            bases=[ast.Name(id="QPlainTextEdit", ctx=ast.Load())], extra=ast.parse("zoom_requested = Signal(int)").body)
        note = note_type()
        event = types.SimpleNamespace(modifiers=lambda: 1, angleDelta=lambda: types.SimpleNamespace(y=lambda: -120),
                                      pixelDelta=lambda: types.SimpleNamespace(y=lambda: 0), accept=Mock())
        note.wheelEvent(event)
        self.assertEqual(note.zoom_requested.values, [(-1,)])
        self.assertEqual(note.normal_scroll, 0)
        event.accept.assert_called_once()
        event.modifiers = lambda: 0
        note.wheelEvent(event)
        self.assertEqual(note.normal_scroll, 1)
        self.assertEqual(note.text, "unchanged note")

    def test_zoom_does_not_reload_save_notes_or_touch_images(self):
        namespace = {"preferences": preferences, "QSignalBlocker": Blocker}
        desktop_type = handlers("Desktop", {"apply_body_preferences", "change_zoom", "reset_zoom"}, namespace)
        desktop = desktop_type()
        desktop.vault = Mock()
        desktop.body = NoRenderingText()
        desktop.zoom_label = Mock()
        desktop.preference_timer = Mock()
        desktop.display_preferences = dict(preferences.DEFAULTS)
        desktop.preferences_pending = False
        desktop.change_zoom(-1)
        self.assertEqual(desktop.display_preferences["zoom"], 90)
        self.assertIn("10.8pt", desktop.body.styles[-1])
        self.assertEqual(desktop.body.text, "unchanged note")
        self.assertTrue(desktop.preferences_pending)
        self.assertEqual(desktop.vault.mock_calls, [])
        desktop.reset_zoom()
        self.assertEqual(desktop.display_preferences["zoom"], 100)

    def test_ui_font_changes_do_not_rebuild_tree_reload_images_or_save_notes(self):
        namespace = {"preferences": preferences, "STYLE": "dark-theme;", "QSignalBlocker": Blocker,
                     "nullcontext": nullcontext}
        desktop_type = handlers("Desktop", {"apply_ui_preferences", "resize_attachment_strip"}, namespace)
        desktop = desktop_type()
        desktop.vault = Mock()
        desktop.body = Mock()
        desktop.note_list = Mock()
        desktop.render_attachments = Mock()
        desktop.refresh_list = Mock()
        desktop.setStyleSheet = Mock()
        desktop.attachment_scroll = Mock()
        desktop.fontMetrics = lambda: types.SimpleNamespace(height=lambda: 28)
        desktop.display_preferences = {"font_size": 18, "font_family": "メイリオ", "zoom": 100}
        desktop.apply_ui_preferences()
        desktop.setStyleSheet.assert_called_once_with("dark-theme;" + preferences.ui_style(desktop.display_preferences))
        desktop.attachment_scroll.setFixedHeight.assert_called_once_with(170)
        for target in (desktop.vault, desktop.body, desktop.note_list, desktop.render_attachments, desktop.refresh_list):
            self.assertEqual(target.mock_calls, [])
        desktop.apply_ui_preferences()
        desktop.display_preferences["zoom"] = 200
        desktop.apply_ui_preferences()
        self.assertEqual(desktop.setStyleSheet.call_count, 1)
        desktop.display_preferences["font_size"] = 24
        desktop.apply_ui_preferences()
        self.assertEqual(desktop.setStyleSheet.call_count, 2)

    def test_saved_ui_font_is_applied_on_locked_screen_without_a_body_widget(self):
        namespace = {"preferences": preferences, "STYLE": "", "nullcontext": nullcontext}
        desktop_type = handlers("Desktop", {"apply_ui_preferences"}, namespace)
        desktop = desktop_type()
        desktop.vault = None
        desktop.display_preferences = {"font_size": 22, "font_family": "メイリオ"}
        desktop.setStyleSheet = Mock()
        desktop.apply_ui_preferences()
        self.assertIn('font-size:22pt', desktop.setStyleSheet.call_args.args[0])

    def test_attachment_strip_grows_for_larger_labels_without_touching_previews(self):
        desktop_type = handlers("Desktop", {"resize_attachment_strip"}, {})
        desktop = desktop_type()
        desktop.attachment_scroll = Mock()
        for line in (10, 28, 90):
            desktop.fontMetrics = lambda: types.SimpleNamespace(height=lambda: line)
            desktop.resize_attachment_strip()
            height = desktop.attachment_scroll.setFixedHeight.call_args.args[0]
            self.assertGreaterEqual(height, 148)
            self.assertGreaterEqual(height, max(70, line + 20) + 2 * line + 44)

    def test_theme_and_local_styles_have_no_fixed_pixel_font_sizes(self):
        source = Path(__file__).resolve().parents[1] / "twonote" / "desktop.py"
        self.assertNotRegex(source.read_text(encoding="utf-8"), r"font-size\s*:\s*\d+(?:\.\d+)?px")

    def test_automatic_unlock_is_one_shot_and_does_not_run_after_manual_lock(self):
        api = types.SimpleNamespace(available=Mock(return_value=True), open_remembered=Mock(return_value=object()))
        namespace = {"active_vault_path": lambda directory: Path(directory), "windows_unlock": api,
                     "threading": types.SimpleNamespace(Thread=lambda target, daemon: types.SimpleNamespace(start=target))}
        desktop_type = handlers("Desktop", {"try_startup_unlock"}, namespace)
        desktop = desktop_type()
        desktop.startup_unlock_attempted = False
        desktop.busy = False
        desktop.vault = desktop.backup_source = desktop.onenote_source = None
        desktop.data_dir = "synthetic-unused-path"
        desktop.login_button = Mock()
        desktop.remember_choice = Mock()
        desktop.login_status = Mock()
        desktop.session = 0
        desktop.events = types.SimpleNamespace(opened=Signal(), failed=Signal())
        desktop.try_startup_unlock()
        api.open_remembered.assert_called_once()
        self.assertEqual(len(desktop.events.opened.values), 1)
        desktop.busy = False
        desktop.vault = None
        desktop.try_startup_unlock()
        api.open_remembered.assert_called_once()

    def test_auto_unlock_failure_returns_to_password_without_retry_loop(self):
        api = types.SimpleNamespace(available=Mock(return_value=True), open_remembered=Mock(side_effect=ValueError("synthetic")))
        namespace = {"active_vault_path": lambda directory: Path(directory), "windows_unlock": api,
                     "threading": types.SimpleNamespace(Thread=lambda target, daemon: types.SimpleNamespace(start=target))}
        desktop_type = handlers("Desktop", {"try_startup_unlock"}, namespace)
        desktop = desktop_type()
        desktop.startup_unlock_attempted = desktop.busy = False
        desktop.vault = desktop.backup_source = desktop.onenote_source = None
        desktop.data_dir = "synthetic-unused-path"
        desktop.login_button = desktop.login_status = Mock()
        desktop.remember_choice = Mock()
        desktop.session = 3
        desktop.events = types.SimpleNamespace(opened=Signal(), failed=Signal())
        desktop.try_startup_unlock()
        self.assertEqual(desktop.events.opened.values, [])
        self.assertEqual(len(desktop.events.failed.values), 1)
        self.assertEqual(desktop.events.failed.values[0][1], 3)
        desktop.busy = False
        desktop.try_startup_unlock()
        api.open_remembered.assert_called_once()

    def test_unlock_choice_has_explicit_text_and_does_not_enable_itself(self):
        class Choice:
            def __init__(self):
                self.items = []
            def addItem(self, text, value):
                self.items.append((text, value))
            def setCurrentIndex(self, index):
                self.index = index
            def setEnabled(self, enabled):
                self.enabled = enabled
            def setAccessibleName(self, name):
                self.name = name
        path = Path(__file__).resolve().parents[1] / "twonote" / "desktop.py"
        source = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "unlock_choice")
        api = types.SimpleNamespace(supported=Mock(return_value=True))
        namespace = {"QComboBox": Choice, "windows_unlock": api}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        create = namespace["unlock_choice"]
        disabled = create()
        self.assertEqual(disabled.index, 0)
        self.assertEqual(disabled.items, [("毎回パスワードを入力", False), ("このWindowsユーザーで自動解除", True)])
        self.assertEqual(create(True).index, 1)
        api.supported.return_value = False
        self.assertEqual(create(True).index, 0)
        self.assertFalse(create(True).enabled)

    def test_unlock_hint_distinguishes_unregistered_pending_and_registered(self):
        api = types.SimpleNamespace(supported=lambda: True)
        desktop_type = handlers("Desktop", {"update_unlock_hint"}, {"windows_unlock": api})
        desktop = desktop_type()
        desktop.remember_choice = types.SimpleNamespace(currentData=Mock(return_value=False))
        desktop.unlock_hint = Mock()
        desktop.login_button = Mock()
        desktop.login_caption = "開く"
        desktop.auto_unlock_registered = False
        desktop.update_unlock_hint()
        self.assertIn("未登録", desktop.unlock_hint.setText.call_args.args[0])
        desktop.remember_choice.currentData.return_value = True
        desktop.update_unlock_hint()
        self.assertIn("今回パスワードで開くと", desktop.unlock_hint.setText.call_args.args[0])
        desktop.login_button.setText.assert_called_with("自動解除を登録して開く")
        desktop.auto_unlock_registered = True
        desktop.update_unlock_hint()
        self.assertIn("登録済み", desktop.unlock_hint.setText.call_args.args[0])
        desktop.remember_choice.currentData.return_value = False
        desktop.update_unlock_hint()
        self.assertIn("無効にします", desktop.unlock_hint.setText.call_args.args[0])

    def password_unlock_controller(self, directory, selected):
        api = types.SimpleNamespace(supported=lambda: True, remember=Mock(), forget=Mock())
        vault = Mock()
        storage = types.SimpleNamespace(create=Mock(return_value=vault))
        namespace = {"windows_unlock": api, "Vault": storage,
            "Slot": lambda *types: lambda method: method,
            "active_vault_path": lambda root: root / "vault-v2",
            "threading": types.SimpleNamespace(Thread=lambda target, daemon: types.SimpleNamespace(start=target))}
        desktop_type = handlers("Desktop", {"unlock", "work_failed"}, namespace)
        desktop = desktop_type()
        desktop.busy = desktop.login_new = False
        desktop.password = Mock()
        desktop.password.text.return_value = "synthetic-only-password"
        desktop.confirm = desktop.backup_source = desktop.onenote_source = desktop.vault = None
        desktop.session = 7
        desktop.data_dir = directory
        desktop.remember_choice = Mock()
        desktop.remember_choice.currentData.return_value = selected
        desktop.login_button = desktop.login_status = Mock()
        desktop.events = types.SimpleNamespace(opened=Signal(), failed=Signal())
        return desktop, api, storage, vault

    def test_password_unlock_registers_only_explicit_choice_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            for selected in (True, False):
                with self.subTest(selected=selected):
                    desktop, api, storage, vault = self.password_unlock_controller(Path(directory), selected)
                    desktop.unlock()
                    desktop.remember_choice.setEnabled.assert_called_once_with(False)
                    self.assertEqual(len(desktop.events.opened.values), 1)
                    if selected:
                        api.remember.assert_called_once_with(vault)
                        api.forget.assert_not_called()
                    else:
                        api.remember.assert_not_called()
                        api.forget.assert_called_once_with(vault.root)

    def test_wrong_password_does_not_change_registration_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop, api, storage, vault = self.password_unlock_controller(Path(directory), True)
            storage.create.side_effect = ValueError("wrong synthetic password")
            desktop.unlock()
            api.remember.assert_not_called()
            api.forget.assert_not_called()
            self.assertEqual(desktop.events.opened.values, [])
            desktop.work_failed(*desktop.events.failed.values[0])
            self.assertFalse(desktop.busy)
            desktop.remember_choice.setEnabled.assert_called_with(True)

    def test_registration_failure_is_reported_without_losing_password_access(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop, api, storage, vault = self.password_unlock_controller(Path(directory), True)
            api.remember.side_effect = OSError("injected key write failure")
            desktop.unlock()
            self.assertEqual(desktop.events.failed.values, [])
            opened, result, epoch = desktop.events.opened.values[0]
            self.assertIs(opened, vault)
            self.assertTrue(result["remember_error"])


if __name__ == "__main__":
    unittest.main()
