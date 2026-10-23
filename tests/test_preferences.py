"""Display behavior without importing Qt or drawing any windows."""
import tempfile
import unittest
from pathlib import Path

from twonote import preferences


class PreferencesTests(unittest.TestCase):
    def test_preferences_roundtrip_and_unknown_keys_are_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferences.save(root, {"font_family": "メイリオ", "font_size": 18, "zoom": 80,
                                    "password": "do-not-store", "key": "do-not-store"})
            self.assertEqual(preferences.load(root), {"font_family": "メイリオ", "font_size": 18, "zoom": 80})
            self.assertNotIn(b"do-not-store", (root / "desktop-preferences.json").read_bytes())

    def test_limits_and_corrupt_file_fallback(self):
        self.assertEqual(preferences.normalized({"font_size": 1000, "zoom": -1})["font_size"], 48)
        self.assertEqual(preferences.normalized({"zoom": -1})["zoom"], 50)
        self.assertEqual(preferences.normalized({"zoom": True})["zoom"], 100)
        self.assertEqual(preferences.normalized({"font_family": "bad\nfont"})["font_family"], "Yu Gothic UI")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "desktop-preferences.json").write_text("not json")
            self.assertEqual(preferences.load(root), preferences.DEFAULTS)

    def test_wheel_directions_high_resolution_and_trackpad(self):
        wheel = preferences.WheelZoom()
        self.assertEqual(wheel.feed(-120), -1)
        self.assertEqual(wheel.feed(240), 2)
        self.assertEqual(wheel.feed(-60), 0)
        self.assertEqual(wheel.feed(-60), -1)
        self.assertEqual(wheel.feed(0, 20), 0)
        self.assertEqual(wheel.feed(0, 20), 1)
        wheel.feed(60)
        wheel.reset()
        self.assertEqual(wheel.feed(60), 0)
        self.assertEqual(preferences.stepped_zoom(100, -1), 90)
        self.assertEqual(preferences.stepped_zoom(50, -1), 50)
        self.assertEqual(preferences.stepped_zoom(300, 1), 300)

    def test_font_size_and_zoom_affect_presentation_only(self):
        style = preferences.body_style({"font_family": 'A"B', "font_size": 20, "zoom": 75})
        self.assertIn("font-size:15pt", style)
        self.assertIn('font-family:"A\\"B"', style)
        self.assertIn("font-size:12pt", preferences.body_style({}))

    def test_ui_font_applies_to_all_widgets_and_headings_scale_with_it(self):
        style = preferences.ui_style({"font_family": "メイリオ", "font_size": 20})
        self.assertIn('QWidget, QToolTip { font-family:"メイリオ"; font-size:20pt; }', style)
        self.assertIn('QLabel#heading { font-size:30pt; }', style)
        self.assertIn('QLineEdit#title { font-size:32pt; }', style)
        self.assertNotIn('px', style)
        self.assertIn('font-size:48pt', preferences.ui_style({"font_size": 1000}))
        self.assertIn('font-family:"A\\"B"', preferences.ui_style({"font_family": 'A"B'}))

    def test_body_zoom_does_not_change_tree_menu_or_other_ui_sizes(self):
        small = {"font_size": 18, "zoom": 50}
        large = {"font_size": 18, "zoom": 300}
        self.assertEqual(preferences.ui_style(small), preferences.ui_style(large))
        self.assertIn("font-size:9pt", preferences.body_style(small))
        self.assertIn("font-size:54pt", preferences.body_style(large))

    def test_window_geometry_survives_display_changes_and_invalid_data_is_ignored(self):
        import base64
        geometry = base64.b64encode(b"synthetic geometry bytes").decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preferences.save(root, {"window_geometry": geometry})
            stored = preferences.load(root)
            self.assertEqual(stored["window_geometry"], geometry)
            preferences.save(root, {**stored, "font_size": 20, "zoom": 90})
            self.assertEqual(preferences.load(root)["window_geometry"], geometry)
        self.assertNotIn("window_geometry", preferences.normalized({"window_geometry": "not base64!!"}))
        self.assertNotIn("window_geometry", preferences.normalized({"window_geometry": "x" * 5000}))


if __name__ == "__main__":
    unittest.main()
