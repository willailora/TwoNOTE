"""Portable inline content and import tests, no GUI or personal data."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from twonote import inline, blocks
from twonote.importing import import_into
from twonote.vault import Vault

ID = "a" * 64
IMAGE = {"id": ID, "mime": "image/png", "filename": "fixture.png"}


class InlineTests(unittest.TestCase):
    def test_legacy_tail_is_added_once_and_explicit_removal_stays_removed(self):
        text = inline.body("前😀後", [IMAGE])
        self.assertEqual(text, "前😀後\n\n" + inline.token(ID))
        self.assertEqual(inline.body(inline.encode(text), [IMAGE]), text)
        self.assertEqual(inline.body(inline.encode("前😀後"), [IMAGE]), "前😀後")

    def test_only_local_declared_references_render(self):
        self.assertEqual(list(inline.parts("before" + inline.token(ID) + "after", [IMAGE])), [("text", "before"), ("embed", ID), ("text", "after")])
        unknown = "<img src='file:///secret'>https://example.invalid" + inline.token("b" * 64)
        self.assertEqual(list(inline.parts(unknown, [IMAGE])), [("text", unknown)])

    def test_repeated_images_and_conflicts_preserve_order(self):
        text = inline.token(ID) + "日本語" + inline.token(ID)
        self.assertEqual([p[0] for p in inline.parts(text, [IMAGE])], ["embed", "text", "embed"])
        notes = [{"content": inline.encode(text), "attachments": [IMAGE]}, {"content": "other", "attachments": [IMAGE]}]
        self.assertEqual(inline.merge(notes).count(inline.HEADER), 1)
        self.assertEqual(inline.merge(notes).count(inline.token(ID)), 3)

    def test_table_csv_quotes_newlines_and_tabs(self):
        table = blocks.parse("table", "日本語", '"a,b","line\n2"\n"引用""符",😀\n')
        self.assertEqual(blocks.parse("table", "日本語", blocks.csv_text(table)), table)
        self.assertEqual(blocks.load(blocks.dump(table)), table)
        self.assertEqual(blocks.parse("table", "", "A\tB\n1\t2")["rows"], [["A", "B"], ["1", "2"]])
        tabbed = blocks.parse("table", "", '"a\tb",c')
        self.assertEqual(tabbed["rows"], [["a\tb", "c"]])
        self.assertEqual(blocks.parse("table", "", blocks.csv_text(tabbed)), tabbed)

    def test_charts_negative_and_all_zero_ranges(self):
        self.assertEqual(blocks.chart_range(blocks.parse("bar", "", "A,-2\nB,3")), ([-2, 3], -2, 3))
        self.assertEqual(blocks.chart_range(blocks.parse("line", "", "A,0\nB,0")), ([0, 0], 0, 1))

    def test_invalid_and_oversize_data_rejected(self):
        for kind, text in [("bar", "A,NaN"), ("line", "A,Infinity"), ("line", "A,1e16"), ("table", "A,B\nC"), ("table", "x\n" * 51), ("table", "x" * 501), ("line", "a,1\n" * 31), ("table", '"unterminated')]:
            with self.subTest(kind=kind, text=text[:20]), self.assertRaises(ValueError): blocks.parse(kind, "", text)


class InlineImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", "synthetic-inline-password")
        (self.root / "image.png").write_bytes(b"synthetic-image")
        self.page = self.root / "page.html"
    def tearDown(self):
        self.vault.lock(); self.temp.cleanup()
    def test_html_image_between_text_and_repeat(self):
        self.page.write_text('<p>前</p><img src="image.png"><p>中</p><img src="image.png"><p>後</p>', encoding="utf-8")
        report = import_into(self.vault, self.page); note = self.vault.heads()[0]
        self.assertEqual(report["attachments"], 1)
        parts = list(inline.parts(inline.body(note["content"]), note["attachments"]))
        self.assertEqual([k for k, v in parts], ["text", "embed", "text", "embed", "text"])
        self.assertEqual(import_into(self.vault, self.page)["positioned"], 0)
    def test_html_table_is_editable_block_at_original_position(self):
        self.page.write_text('<p>前</p><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table><p>後</p>', encoding="utf-8")
        import_into(self.vault, self.page); note = self.vault.heads()[0]
        block = note["attachments"][0]
        self.assertEqual(block["mime"], inline.BLOCK_MIME)
        self.assertEqual(blocks.load(self.vault.read_blob(block["id"]))["rows"], [["A", "B"], ["1", "2"]])
        self.assertLess(note["content"].index("前"), note["content"].index(inline.token(block["id"])))
        self.assertLess(note["content"].index(inline.token(block["id"])), note["content"].index("後"))
    def test_reimport_only_repositions_exact_unedited_body_and_keeps_classification(self):
        self.page.write_text('<p>前</p><img src="image.png"><p>後</p>', encoding="utf-8")
        import_into(self.vault, self.page); note = self.vault.heads()[0]
        old = self.vault.save(note["note_id"], [note["revision"]], {**note, "content": "前\n\n後", "section": "改名済み"})
        report = import_into(self.vault, self.page); restored = self.vault.heads()[0]
        self.assertEqual(report["positioned"], 1)
        self.assertEqual(restored["section"], "改名済み")
        self.assertEqual(restored["attachments"], old["attachments"])
        changed = self.vault.save(restored["note_id"], [restored["revision"]], {**restored, "content": "編集中の本文"})
        self.assertEqual(import_into(self.vault, self.page)["positioned"], 0)
        self.assertEqual(self.vault.heads()[0]["revision"], changed["revision"])

    def test_native_onenote_node_order_and_table_without_opening_real_files(self):
        from aspose.note import Page, Outline, OutlineElement, RichText, Image, Table, TableRow, TableCell
        page = Page(); outline = Outline(); page.AppendChildLast(outline)
        element = OutlineElement(); outline.AppendChildLast(element)
        element.AppendChildLast(RichText(Text="前"))
        element.AppendChildLast(Image(FileName="fixture.png", Bytes=b"synthetic-image"))
        element.AppendChildLast(RichText(Text="中"))
        table = Table(); row = TableRow(); table.AppendChildLast(row)
        for text in ("A", "B"):
            cell = TableCell(); cell.AppendChildLast(RichText(Text=text)); row.AppendChildLast(cell)
        element.AppendChildLast(table); element.AppendChildLast(RichText(Text="後"))
        source = self.root / "synthetic.one"; source.write_bytes(b"synthetic-source")
        with patch("aspose.note.Document", return_value=[page]): import_into(self.vault, source)
        note = self.vault.heads()[0]
        sequence = list(inline.parts(inline.body(note["content"]), note["attachments"]))
        self.assertEqual([k for k, v in sequence], ["text", "embed", "text", "embed", "text"])
        self.assertIn("前", sequence[0][1]); self.assertIn("中", sequence[2][1]); self.assertIn("後", sequence[4][1])


if __name__ == "__main__": unittest.main()
