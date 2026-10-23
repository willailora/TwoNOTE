"""Device-local display preferences. No note contents or credentials live here."""
import json
import base64
import math
from pathlib import Path

from .vault import atomic_write, json_bytes

DEFAULTS = {"font_family": "Yu Gothic UI", "font_size": 12, "zoom": 100}


def normalized(value) -> dict:
    if not isinstance(value, dict):
        return dict(DEFAULTS)
    result = dict(DEFAULTS)
    family = value.get("font_family")
    if isinstance(family, str) and 0 < len(family.strip()) <= 100 and family.isprintable():
        result["font_family"] = family.strip()
    for key, low, high in (("font_size", 6, 48), ("zoom", 50, 300)):
        number = value.get(key)
        if type(number) is int:
            result[key] = max(low, min(high, number))
    geometry = value.get("window_geometry")
    if isinstance(geometry, str) and len(geometry) <= 2800:
        try:
            raw = base64.b64decode(geometry, validate=True)
            if 4 <= len(raw) <= 2048:
                result["window_geometry"] = geometry
        except ValueError:
            pass
    return result


def load(data_dir: Path) -> dict:
    try:
        path = Path(data_dir) / "desktop-preferences.json"
        with path.open("rb") as source:
            raw = source.read(4097)
        return normalized(json.loads(raw)) if len(raw) <= 4096 else dict(DEFAULTS)
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(data_dir: Path, value: dict) -> None:
    atomic_write(Path(data_dir) / "desktop-preferences.json", json_bytes(normalized(value)))


def body_style(value: dict) -> str:
    value = normalized(value)
    family = value["font_family"].replace("\\", "\\\\").replace('"', '\\"')
    points = value["font_size"] * value["zoom"] / 100
    return f'QPlainTextEdit, QTextEdit {{ font-family:"{family}"; font-size:{points:g}pt; }}'


def ui_style(value: dict) -> str:
    """App-wide base typography; body-only zoom never resizes navigation."""
    value = normalized(value)
    family = value["font_family"].replace("\\", "\\\\").replace('"', '\\"')
    points = value["font_size"]
    return (f'QWidget, QToolTip {{ font-family:"{family}"; font-size:{points:g}pt; }}\n'
            f'QLabel#heading {{ font-size:{points * 1.5:g}pt; }}\n'
            f'QLabel#brand, QLabel#mark, QLineEdit#title {{ font-size:{points * 1.6:g}pt; }}')


def stepped_zoom(current: int, steps: int) -> int:
    return max(50, min(300, current + steps * 10))


class WheelZoom:
    """Accumulate high-resolution wheel/trackpad events without rendering."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.angle = self.pixels = 0

    def feed(self, angle: int, pixels: int = 0) -> int:
        if angle:
            self.angle += angle
            steps = math.trunc(self.angle / 120)
            self.angle -= steps * 120
            return steps
        self.pixels += pixels
        steps = math.trunc(self.pixels / 40)
        self.pixels -= steps * 40
        return steps
