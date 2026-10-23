"""Window persistence handler tests without importing Qt or creating windows."""
import ast
import base64
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


class WindowGeometryTests(unittest.TestCase):
    def make_window(self):
        path = Path(__file__).resolve().parents[1] / "twonote" / "desktop.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        desktop = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Desktop")
        function = next(n for n in desktop.body if isinstance(n, ast.FunctionDef) and n.name == "capture_window_geometry")
        namespace = {"base64": base64}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), namespace)
        state = types.SimpleNamespace(geometry_ready=True, display_preferences={}, preferences_pending=False,
            preference_timer=Mock(), isVisible=lambda: True, isMinimized=lambda: False,
            saveGeometry=Mock(return_value=b"Qt geometry with normal size and maximized state"))
        return state, namespace["capture_window_geometry"]

    def test_move_and_resize_save_geometry_once_when_changed(self):
        window, capture = self.make_window()
        capture(window)
        self.assertEqual(base64.b64decode(window.display_preferences["window_geometry"]), window.saveGeometry.return_value)
        window.preference_timer.start.assert_called_once()
        capture(window)
        window.preference_timer.start.assert_called_once()

    def test_startup_and_minimizing_cannot_overwrite_last_visible_geometry(self):
        window, capture = self.make_window()
        capture(window)
        saved = window.display_preferences.copy()
        window.isMinimized = lambda: True
        window.saveGeometry.return_value = b"minimized state"
        capture(window)
        self.assertEqual(window.display_preferences, saved)
        window.isMinimized = lambda: False
        window.geometry_ready = False
        capture(window)
        self.assertEqual(window.display_preferences, saved)


if __name__ == "__main__":
    unittest.main()
