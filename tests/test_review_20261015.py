"""Regressions from the October review. Synthetic data only; no Qt/Android GUI."""
import json
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
from twonote import blocks, inline
from twonote.importing import import_into, local_image_path
from twonote.vault import Vault
from test_desktop_controls_logic import handlers


class BlockValidationTests(unittest.TestCase):
    def test_android_form_checkpoints_before_lock_and_keeps_plaintext_out_of_bundle(self):
        source = (Path(__file__).resolve().parents[1] / "android/app/src/main/java/com/twonote/android/MainActivity.java").read_text(encoding="utf-8")
        lock = source.split("private void lockMemory()", 1)[1].split("protected void onResume()", 1)[0]
        self.assertLess(lock.index("checkpointBlockDraft();"), lock.index("vault.lock();"))
        self.assertLess(lock.index("checkpointBlockDraft();"), lock.index("dialog.dismiss();"))
        state = source.split("protected void onSaveInstanceState", 1)[1].split("private void lockMemory", 1)[0]
        self.assertIn("checkpointBlockDraft();", state)
        for secret in ("blockHeading", "blockData", "activeBlockDraft"):
            self.assertNotIn(secret, state)
        self.assertIn("resumeBlockDraft(block);", source)
    def test_japanese_table_that_used_to_be_unreadable_roundtrips(self):
        text = "\n".join([",".join(["あ" * 500] * 10)] * 28)
        value = blocks.parse("table", "日本語", text)
        raw = blocks.dump(value)
        self.assertGreater(len(raw), 400000)
        self.assertEqual(blocks.load(raw), value)
        # Also accepts an already-saved old version of the same object.
        self.assertEqual(blocks.load(json.dumps(value, ensure_ascii=False).encode()), value)

    def test_encoded_size_is_checked_before_saving(self):
        value = blocks.parse("table", "", "A,B")
        value["extra"] = "x" * blocks.MAX_BYTES
        with self.assertRaises(ValueError): blocks.dump(value)
        with self.assertRaises(ValueError): blocks.load(b" " * (blocks.MAX_BYTES + 1))

    def test_maximum_valid_quoted_table_can_be_reedited_as_csv(self):
        value = {"version":1, "kind":"table", "title":"", "rows":[['"' * 500] * 12 for _ in range(50)]}
        csv = blocks.csv_text(value)
        self.assertGreater(len(csv), 400000)
        self.assertEqual(blocks.parse("table", "", csv), value)

    def test_numbers_are_shared_decimal_strings_and_legacy_values_recover(self):
        for source, normalized in [("１２", "12"), ("1_000", "1000"), ("－１．５", "-1.5"), ("1e2", "1e2")]:
            self.assertEqual(blocks.parse("bar", "", "A," + source)["rows"][0][1], normalized)
        for value in ("1d", "1f", "0x1.0p0", "1__0", "NaN", "Infinity", "1e16"):
            with self.subTest(value=value), self.assertRaises(ValueError): blocks.parse("bar", "", "A," + value)
        for source in ("1d", "1f", "0x1.0p0"):
            raw = json.dumps({"version":1, "kind":"line", "title":"", "rows":[["A", source]]}).encode()
            loaded = blocks.load(raw)
            self.assertEqual(float(loaded["rows"][0][1]), 1)
            self.assertEqual(blocks.load(blocks.dump(loaded)), loaded)


class ImportAndEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", "synthetic-october-regression")
    def tearDown(self): self.vault.lock(); self.temp.cleanup()

    def test_external_paths_rejected_before_any_filesystem_probe(self):
        for path in [r"\\review.invalid\share\x.png", "%5c%5creview.invalid%5cshare%5cx.png", "//review.invalid/x", "/root/x", "C:/x.png", "C:x.png", "file:///x", "https://example.invalid/a", "..%5c..%5cx.png", "name:stream", "%00bad"]:
            with self.subTest(path=path), patch.object(Path, "resolve", side_effect=AssertionError("resolve called")), patch.object(Path, "is_symlink", side_effect=AssertionError("stat called")), self.assertRaises(ValueError):
                local_image_path(self.root, path)

    def test_relative_local_path_is_supported_and_links_are_not_followed(self):
        self.assertEqual(local_image_path(self.root, "folder/a%20b.png"), self.root / "folder" / "a b.png")
        self.assertEqual(local_image_path(self.root, "folder/../x.png"), self.root / "x.png")
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(ValueError):
            local_image_path(self.root, "link/x.png")

    def test_unsafe_image_reference_warns_without_touching_remote_path(self):
        source = self.root / "unc.html"; source.write_text('<img src="%5C%5Creview.invalid%5Cshare%5Cx.png">')
        original = Path.resolve
        def guarded(path, *args, **kwargs):
            if path.drive.startswith("\\\\"): self.fail("UNC reached resolution")
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", guarded): report = import_into(self.vault, source)
        self.assertTrue(report["warnings"]); self.assertEqual(report["attachments"], 0)

    def test_caption_is_retained_in_short_long_and_complex_tables(self):
        for index, caption in enumerate(["表の説明", "長い説明" * 40]):
            for complex_table in (False, True):
                source = self.root / f"caption-{index}-{complex_table}.html"
                attribute = ' colspan="2"' if complex_table else ""
                source.write_text(f'<p>Before</p><table><caption>{caption}</caption><tr><td{attribute}>A</td><td>B</td></tr></table><p>After</p>', encoding="utf-8")
                report = import_into(self.vault, source); self.assertFalse(report["warnings"])
                note = next(n for n in self.vault.heads() if n["title"] == source.stem)
                payload = note["content"] + "".join(self.vault.read_blob(a["id"]).decode() for a in note["attachments"])
                self.assertIn(caption, payload)

    def controller(self, note):
        kind = handlers("Desktop", {"import_finished"}, {"Slot":lambda *a:lambda f:f, "QMessageBox":types.SimpleNamespace(information=lambda *a:None)})
        obj = kind(); obj.current = note; obj.vault = self.vault; obj.session = 1; obj.dirty = False
        obj.refresh_list = Mock(); obj.update_status = Mock(); obj.save_status = Mock()
        obj.load_revision = Mock(side_effect=lambda rev:setattr(obj, "current", self.vault.get(rev)))
        return obj

    def test_reimport_reloads_unedited_body_but_preserves_dirty_body(self):
        note = self.vault.save(uuid.uuid4().hex, [], {"content":"old"})
        obj = self.controller(note)
        latest = self.vault.save(note["note_id"], [note["revision"]], {**note, "content":"new layout"})
        report = {"imported":0, "attachments":0, "skipped":1, "positioned":1, "warnings":[]}
        obj.import_finished(report, 1); self.assertEqual(obj.current["revision"], latest["revision"])
        self.vault.save(obj.current["note_id"], [obj.current["revision"]], {**obj.current, "content":"typed after import"})
        self.assertEqual(len(self.vault.heads()), 1)
        obj = self.controller(note); obj.dirty = True; obj.import_finished(report, 1)
        obj.load_revision.assert_not_called(); self.assertEqual(obj.current["revision"], note["revision"])

    def test_table_edit_from_pinned_base_keeps_both_concurrent_versions(self):
        original = blocks.parse("table", "", "A,old")
        old = self.vault.add_blob("old.json", inline.BLOCK_MIME, blocks.dump(original))
        base = self.vault.save(uuid.uuid4().hex, [], {"title":"Pinned", "content":inline.encode("before" + inline.token(old["id"]) + "after"), "attachments":[old]})
        remote = blocks.save_edit(self.vault, base["revision"], blocks.parse("table", "", "A,remote"), old["id"])
        local = blocks.save_edit(self.vault, base["revision"], blocks.parse("table", "", "A,local"), old["id"])
        self.assertEqual(len(self.vault.heads()), 2)
        self.assertIn(remote["revision"], {n["revision"] for n in self.vault.heads()})
        item = local["attachments"][0]
        self.assertIn(inline.token(item["id"]), local["content"])
        self.assertEqual(blocks.load(self.vault.read_blob(item["id"]))["rows"], [["A","local"]])
        self.assertEqual(local["parents"], [base["revision"]])

    def test_insert_preserves_captured_selection_and_rejects_wrong_target(self):
        base = self.vault.save(uuid.uuid4().hex, [], {"content":"A😀B"})
        value = blocks.parse("bar", "", "A,１")
        saved = blocks.save_edit(self.vault, base["revision"], value, offset=1, end=2)
        self.assertEqual(inline.body(saved["content"]), "A" + inline.token(saved["attachments"][0]["id"]) + "B")
        with self.assertRaises(ValueError): blocks.save_edit(self.vault, saved["revision"], value, "a" * 64)


if __name__ == "__main__": unittest.main()
