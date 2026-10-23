"""Native bounded table/chart previews; no browser, network or plotting runtime."""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from . import blocks


def render(value):
    value = blocks.validate(value)
    rows = value["rows"]
    table = value["kind"] == "table"
    width, height = (max(360, min(1200, len(rows[0]) * 190)), 56 + len(rows) * 42) if table else (900, 490)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#122136"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont("sans-serif", 14))
        painter.setPen(QColor("#e5edf7"))
        painter.drawText(QRectF(12, 6, width - 24, 40), Qt.AlignmentFlag.AlignVCenter, value.get("title", ""))
        if table:
            cell = width / len(rows[0])
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    rect = QRectF(c * cell, 52 + r * 42, cell, 42)
                    painter.setPen(QColor("#53738f")); painter.drawRect(rect)
                    painter.setPen(QColor("#e5edf7"))
                    text = painter.fontMetrics().elidedText(text.replace("\n", " "), Qt.TextElideMode.ElideRight, int(cell - 16))
                    painter.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, text)
        else:
            values, low, high = blocks.chart_range(value)
            left, top, right, bottom = 110, 65, 875, 380
            y = lambda n: bottom - (n - low) / (high - low) * (bottom - top)
            for i in range(5):
                n = low + (high - low) * i / 4
                painter.setPen(QColor("#365069")); painter.drawLine(left, int(y(n)), right, int(y(n)))
                painter.setPen(QColor("#d5e1ef")); painter.drawText(QRectF(4, y(n) - 15, 100, 30), Qt.AlignmentFlag.AlignRight, f"{n:.3g}")
            slot = (right - left) / len(values)
            previous = None
            for i, n in enumerate(values):
                x = left + slot * (i + .5)
                painter.setPen(QPen(QColor("#6dd3cf"), 3))
                if table is False and value["kind"] == "bar":
                    painter.fillRect(QRectF(x - slot * .3, min(y(n), y(0)), slot * .6, max(1, abs(y(n) - y(0)))), QColor("#6dd3cf"))
                else:
                    if previous is not None: painter.drawLine(int(previous[0]), int(previous[1]), int(x), int(y(n)))
                    painter.drawEllipse(QRectF(x - 3, y(n) - 3, 6, 6)); previous = (x, y(n))
                painter.setPen(QColor("#e5edf7"))
                label = painter.fontMetrics().elidedText(rows[i][0], Qt.TextElideMode.ElideRight, int(slot - 2))
                painter.drawText(QRectF(x - slot / 2, bottom + 8, slot, 38), Qt.AlignmentFlag.AlignCenter, label)
    finally:
        painter.end()
    return image
