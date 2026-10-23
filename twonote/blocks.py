"""Editable, bounded table/chart data. Renderers never evaluate HTML or formulas."""
import csv
import io
import json
import math
import re

MAX_BYTES = 2 * 1024 * 1024
# CSV doubles quotation marks; allow editing any table accepted by the schema.
MAX_INPUT_UNITS = 1000000
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_HEX = re.compile(r"[+-]?0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)[pP][+-]?[0-9]+\Z")
_FULLWIDTH = str.maketrans("０１２３４５６７８９＋－．Ｅｅ", "0123456789+-.Ee")


def number_text(text, legacy=False):
    value = text.translate(_FULLWIDTH).strip(" \t\r\n\f")
    value = re.sub(r"(?<=[0-9])_(?=[0-9])", "", value)
    if legacy and value.endswith(("d", "D", "f", "F")):
        value = value[:-1]
    if legacy and _HEX.fullmatch(value):
        try:
            number = float.fromhex(value)
        except OverflowError as exc:
            raise ValueError("グラフの数値が大きすぎます") from exc
        value = str(int(number)) if math.isfinite(number) and number.is_integer() else repr(number)
    elif _DECIMAL.fullmatch(value):
        number = float(value)
    else:
        raise ValueError("グラフの2列目は10進数を入力してください")
    if not math.isfinite(number) or abs(number) > 1e15:
        raise ValueError("グラフの数値は有限の±10^15以内です")
    return value


def validate(value, *, legacy=False):
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("kind") not in ("table", "bar", "line"):
        raise ValueError("表・グラフの形式が不正です")
    rows = value.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("表は1〜50行、グラフは1〜30項目です")
    columns = len(rows[0]) if isinstance(rows[0], list) else 0
    if not 1 <= columns <= 12:
        raise ValueError("表は1〜12列です")
    for row in rows:
        if not isinstance(row, list) or len(row) != columns or any(not isinstance(c, str) or len(c.encode("utf-16-le")) // 2 > 500 for c in row):
            raise ValueError("各行の列数を揃え、セルは500文字以内にしてください")
    title = value.get("title")
    if not isinstance(title, str) or len(title.encode("utf-16-le")) // 2 > 100:
        raise ValueError("見出しは100文字以内です")
    if value["kind"] != "table":
        if columns != 2 or len(rows) > 30:
            raise ValueError("グラフは項目名,数値の2列、30項目までです（見出し行は不要）")
        value = {**value, "rows": [[row[0], number_text(row[1], legacy)] for row in rows]}
    return value


def parse(kind, title, data):
    if len(data.encode("utf-16-le")) // 2 > MAX_INPUT_UNITS:
        raise ValueError("データが大きすぎます")
    quoted = False
    delimiter = ","
    for character in data:
        if character == '"': quoted = not quoted
        elif character == "\t" and not quoted: delimiter = "\t"; break
    try:
        rows = list(csv.reader(io.StringIO(data), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise ValueError("CSVの引用符が不正です") from exc
    value = validate({"version": 1, "kind": kind, "title": title, "rows": rows})
    dump(value)  # Enforce exactly the same encoded-size limit as the reader.
    return value


def load(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("データが大きすぎます")
    return validate(json.loads(raw), legacy=True)


def dump(value):
    raw = json.dumps(validate(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("表・グラフの保存データは2MBまでです")
    return raw


def csv_text(value):
    stream = io.StringIO()
    csv.writer(stream, lineterminator="\n", quoting=csv.QUOTE_ALL).writerows(validate(value)["rows"])
    return stream.getvalue()


def chart_range(value):
    numbers = [float(r[1]) for r in validate(value)["rows"]]
    low, high = min(0, min(numbers)), max(0, max(numbers))
    return numbers, low, high if high > low else low + 1


def save_edit(vault, base_revision, value, item_id=None, offset=-1, end=None, snapshot_text=None):
    """Commit against the version at dialog-open, not a mutable current selection.

    A remote replacement remains a separate head rather than consuming the
    local edit or attaching its data to a different document revision.
    """
    from . import inline
    raw = dump(value)
    with vault.mutex:
        base = vault.get(base_revision)
        if base["deleted"]:
            raise ValueError("削除済みの記事は先に復元してください")
        files = base["attachments"]
        if item_id is not None and not any(a["id"] == item_id and a["mime"] == inline.BLOCK_MIME for a in files):
            raise ValueError("編集元の表・グラフを確認できません")
        text = inline.body(base["content"], files) if snapshot_text is None else snapshot_text
        added = vault.add_blob((value.get("title", "").strip() or "表・グラフ") + ".json", inline.BLOCK_MIME, raw)
        if item_id is None:
            at = len(text) if offset < 0 else min(offset, len(text))
            stop = at if end is None else max(at, min(end, len(text)))
            text = text[:at] + inline.token(added["id"]) + text[stop:]
        else:
            text = text.replace(inline.token(item_id), inline.token(added["id"]))
        files = [a for a in files if a["id"] != item_id] + [added]
        return vault.save(base["note_id"], [base_revision], {**base, "content": inline.encode(text), "attachments": files})
