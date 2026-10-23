"""Tree operations with real encrypted storage, no Qt or graphics."""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from twonote import vault as vault_module
from twonote.backup import export_backup, restore_backup
from twonote.navigation import FILE_NAME, Navigation, drop_plan, group_key
from twonote.vault import Vault, VaultError

PASSWORD = "synthetic-tree-password"


def payload(note):
    return {"kind": "note", "book": note["notebook"], "section": note["section"], "note_id": note["note_id"], "revision": note["revision"]}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", PASSWORD)
        attachment = self.vault.add_blob("fixture.png", "image/png", b"synthetic image")
        self.a = self.vault.save(uuid.uuid4().hex, [], {"title": "Article A", "content": "unsynced text A", "notebook": "PRIVATE BOOK", "section": "Ideas", "attachments": [attachment]})
        self.b = self.vault.save(uuid.uuid4().hex, [], {"title": "Article B", "content": "text B", "notebook": "PRIVATE BOOK", "section": "Ideas"})
        self.c = self.vault.save(uuid.uuid4().hex, [], {"title": "Article C", "content": "text C", "notebook": "Work", "section": "Plans"})
        self.navigation = Navigation(self.vault)
        self.navigation.tree()

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def section_notes(self, book, section, navigation=None):
        return next(group["notes"] for node in (navigation or self.navigation).tree()
                    if node["name"] == book for group in node["sections"] if group["name"] == section)

    def test_move_between_trees_keeps_body_images_and_history_without_reading_image(self):
        before = self.vault.inventory()["blobs"]
        with patch.object(self.vault, "read_blob", side_effect=AssertionError("image reloaded")):
            moved = self.navigation.apply_drop(payload(self.a), {"kind": "section", "book": "Work", "section": "Plans"}, "on")
        self.assertEqual((moved["notebook"], moved["section"]), ("Work", "Plans"))
        self.assertEqual(moved["content"], self.a["content"])
        self.assertEqual(moved["attachments"], self.a["attachments"])
        self.assertEqual(before, self.vault.inventory()["blobs"])
        self.assertEqual(self.vault.get(self.a["revision"])["notebook"], "PRIVATE BOOK")
        self.assertEqual([n["note_id"] for n in self.section_notes("Work", "Plans")], [self.c["note_id"], self.a["note_id"]])
        replica = Vault.create(self.root / "replica", PASSWORD, self.vault.config)
        try:
            inventory = self.vault.inventory()
            for kind in ("blobs", "revisions"):
                for identity in inventory[kind]:
                    replica.stage_object(kind, identity, self.vault.read_object(kind, identity))
            replica.commit(inventory["revisions"])
            received = next(note for note in replica.heads() if note["note_id"] == moved["note_id"])
            self.assertEqual((received["notebook"], received["section"], received["content"]), ("Work", "Plans", self.a["content"]))
        finally:
            replica.lock()

    def test_order_survives_edits_restart_and_filtering(self):
        inventory = self.vault.inventory()
        self.navigation.apply_drop(payload(self.b), payload(self.a), "before")
        self.assertEqual(self.vault.inventory(), inventory)  # Ordering is not a content edit.
        self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "new text"})
        reopened = Vault.open(self.vault.root, PASSWORD)
        try:
            navigation = Navigation(reopened)
            self.assertEqual([n["note_id"] for n in self.section_notes("PRIVATE BOOK", "Ideas", navigation)], [self.b["note_id"], self.a["note_id"]])
            filtered = navigation.tree("Article A")
            self.assertEqual(sum(len(s["notes"]) for b in filtered for s in b["sections"]), 1)
            self.assertEqual([n["note_id"] for n in self.section_notes("PRIVATE BOOK", "Ideas", navigation)], [self.b["note_id"], self.a["note_id"]])
        finally:
            reopened.lock()

    def test_empty_groups_order_and_collapsed_state_are_encrypted_and_persistent(self):
        self.navigation.add_folder("PRIVATE EMPTY")
        self.navigation.add_folder("Work", "New section")
        self.navigation.apply_drop({"kind": "book", "book": "Work"}, {"kind": "book", "book": "PRIVATE BOOK"}, "before")
        self.navigation.apply_drop({"kind": "section", "book": "Work", "section": "New section"}, {"kind": "section", "book": "Work", "section": "Plans"}, "before")
        self.navigation.collapse("book", "Work", "", True)
        other = Navigation(self.vault)
        self.assertEqual(other.state["books"][0], "Work")
        self.assertEqual(other.state["sections"]["Work"][0], "New section")
        self.assertIn(group_key("book", "Work"), other.state["collapsed"])
        self.assertNotIn(b"PRIVATE", (self.vault.root / FILE_NAME).read_bytes())
        other.remove_empty("book", "PRIVATE EMPTY")
        with self.assertRaises(VaultError):
            other.remove_empty("book", "PRIVATE BOOK")

    def test_failed_move_rolls_back_order_and_preserves_note(self):
        before = (self.vault.root / FILE_NAME).read_bytes()
        old_state = self.navigation.state.copy()
        with patch.object(self.vault, "save", side_effect=OSError("injected full disk")):
            with self.assertRaises(OSError):
                self.navigation.apply_drop(payload(self.a), payload(self.c), "before")
        self.assertEqual(Navigation(self.vault).state, old_state)
        self.assertEqual(self.vault.get(self.a["revision"])["notebook"], "PRIVATE BOOK")
        self.assertTrue(before)

    def test_conflict_or_stale_target_cannot_overwrite_a_note(self):
        self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "edit 1"})
        self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "edit 2"})
        with self.assertRaises(VaultError):
            self.navigation.apply_drop(payload(self.a), payload(self.c), "after")
        self.assertEqual(len([n for n in self.vault.heads() if n["note_id"] == self.a["note_id"]]), 2)
        self.vault.save(self.c["note_id"], [self.c["revision"]], {**self.c, "section": "Changed remotely"})
        with self.assertRaises(VaultError):
            self.navigation.apply_drop(payload(self.b), payload(self.c), "after")

    def test_move_uses_latest_saved_text_and_book_drop_uses_unassigned_section(self):
        self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "typing saved before drop"})
        moved = self.navigation.apply_drop(payload(self.a), {"kind": "book", "book": "Work"}, "on")
        self.assertEqual(moved["content"], "typing saved before drop")
        self.assertEqual(moved["section"], "")

    def test_backup_includes_order_but_not_credentials(self):
        self.navigation.apply_drop(payload(self.b), payload(self.a), "before")
        self.navigation.collapse("section", "PRIVATE BOOK", "Ideas", True)
        path = self.root / "backup.twonote"
        export_backup(self.vault, path)
        restored = restore_backup(path, self.root / "restored", PASSWORD)
        try:
            self.assertEqual(Navigation(restored).state, self.navigation.state)
            self.assertEqual(restored.settings(), {})
        finally:
            restored.lock()

    def test_invalid_drop_is_rejected(self):
        with self.assertRaises(VaultError):
            drop_plan({"kind": "section", "book": "A", "section": "X"}, {"kind": "book", "book": "B"}, "on")
        with self.assertRaises(VaultError):
            drop_plan({**payload(self.a), "blocked": True}, payload(self.b), "before")

    def test_rename_preserves_articles_order_collapse_history_and_syncs_new_name(self):
        self.navigation.add_folder("PRIVATE BOOK", "Later")
        self.navigation.apply_drop(payload(self.b), payload(self.a), "before")
        self.navigation.collapse("section", "PRIVATE BOOK", "Ideas", True)
        before_blobs = self.vault.inventory()["blobs"]
        with patch.object(self.vault, "_publish", wraps=self.vault._publish) as publish:
            with patch.object(self.vault, "read_blob", side_effect=AssertionError("image reloaded")):
                changes = self.navigation.rename_section("PRIVATE BOOK", "Ideas", "  日本語の資料  ")
            self.assertEqual(publish.call_count, 1)
        self.assertEqual(set(changes), {self.a["revision"], self.b["revision"]})
        for old in (self.a, self.b):
            renamed = changes[old["revision"]]
            self.assertEqual(renamed["section"], "日本語の資料")
            self.assertEqual(renamed["parents"], [old["revision"]])
            for field in ("note_id", "content", "attachments", "title", "notebook", "deleted", "tags", "source_path"):
                self.assertEqual(renamed[field], old[field])
            self.assertEqual(self.vault.get(old["revision"])["section"], "Ideas")
        self.assertEqual(before_blobs, self.vault.inventory()["blobs"])
        reopened = Vault.open(self.vault.root, PASSWORD)
        try:
            layout = Navigation(reopened)
            self.assertEqual(layout.state["sections"]["PRIVATE BOOK"], ["日本語の資料", "Later"])
            self.assertIn(group_key("section", "PRIVATE BOOK", "日本語の資料"), layout.state["collapsed"])
            self.assertNotIn(group_key("section", "PRIVATE BOOK", "Ideas"), layout.state["collapsed"])
            self.assertEqual([n["note_id"] for n in self.section_notes("PRIVATE BOOK", "日本語の資料", layout)], [self.b["note_id"], self.a["note_id"]])
        finally:
            reopened.lock()
        self.assertNotIn("日本語の資料".encode(), (self.vault.root / FILE_NAME).read_bytes())
        replica = Vault.create(self.root / "rename-replica", PASSWORD, self.vault.config)
        try:
            inventory = self.vault.inventory()
            for kind in ("blobs", "revisions"):
                for identity in inventory[kind]:
                    replica.stage_object(kind, identity, self.vault.read_object(kind, identity))
            replica.commit(inventory["revisions"])
            received = [n for n in replica.heads() if n["notebook"] == "PRIVATE BOOK"]
            self.assertEqual(len(received), 2)
            self.assertTrue(all(n["section"] == "日本語の資料" for n in received))
        finally:
            replica.lock()

    def test_rename_empty_or_unnamed_section_stays_in_same_position(self):
        self.navigation.add_folder("Work", "")
        self.navigation.add_folder("Work", "Last")
        inventory = self.vault.inventory()
        self.assertEqual(self.navigation.rename_section("Work", "", "Ideas"), {})
        self.assertEqual(self.vault.inventory(), inventory)
        self.assertEqual(Navigation(self.vault).state["sections"]["Work"], ["Plans", "Ideas", "Last"])
        # Identical section names in different notebooks remain independent.
        self.assertEqual(len(self.section_notes("PRIVATE BOOK", "Ideas")), 2)

    def test_rename_keeps_deleted_status_and_all_conflict_branches(self):
        deleted = self.vault.save(self.b["note_id"], [self.b["revision"]], {**self.b, "deleted": True})
        first = self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "branch one"})
        second = self.vault.save(self.a["note_id"], [self.a["revision"]], {**self.a, "content": "branch two"})
        changes = self.navigation.rename_section("PRIVATE BOOK", "Ideas", "修正済み")
        self.assertEqual(set(changes), {deleted["revision"], first["revision"], second["revision"]})
        heads = [n for n in self.vault.heads(True) if n["notebook"] == "PRIVATE BOOK"]
        self.assertEqual(len(heads), 3)
        self.assertTrue(all(n["section"] == "修正済み" for n in heads))
        self.assertTrue(next(n for n in heads if n["note_id"] == self.b["note_id"])["deleted"])
        branches = [n for n in heads if n["note_id"] == self.a["note_id"]]
        self.assertEqual({n["content"] for n in branches}, {"branch one", "branch two"})
        self.assertTrue(all(n["conflicts"] == 2 for n in branches))
        self.navigation.tree()
        self.assertNotIn("Ideas", self.navigation.state["sections"]["PRIVATE BOOK"])

    def test_rename_invalid_duplicate_or_missing_section_is_non_destructive(self):
        self.navigation.add_folder("PRIVATE BOOK", "Taken")
        inventory = self.vault.inventory()
        layout = Navigation(self.vault).state
        for name in ("", "   ", "x" * 257, "Taken"):
            with self.subTest(name=name):
                with self.assertRaises(VaultError):
                    self.navigation.rename_section("PRIVATE BOOK", "Ideas", name)
        with self.assertRaises(VaultError):
            self.navigation.rename_section("PRIVATE BOOK", "Missing", "New")
        self.assertEqual(self.navigation.rename_section("PRIVATE BOOK", "Ideas", "Ideas"), {})
        self.assertEqual(self.vault.inventory(), inventory)
        self.assertEqual(Navigation(self.vault).state, layout)

    def test_rename_failure_after_first_staged_article_publishes_nothing(self):
        inventory = self.vault.inventory()
        layout = Navigation(self.vault).state
        stage = self.vault.stage_object
        calls = []
        def fail_second(*args):
            calls.append(args[1])
            if len(calls) == 2:
                raise OSError("injected full disk during second article")
            return stage(*args)
        with patch.object(self.vault, "stage_object", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.navigation.rename_section("PRIVATE BOOK", "Ideas", "New")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.vault.inventory(), inventory)
        self.assertEqual(Navigation(self.vault).state, layout)
        reopened = Vault.open(self.vault.root, PASSWORD)
        try:
            self.assertEqual(reopened.inventory(), inventory)
        finally:
            reopened.lock()

    def test_rename_failed_index_commit_rolls_back_layout_and_all_articles(self):
        inventory = self.vault.inventory()
        layout = Navigation(self.vault).state
        atomic = vault_module.atomic_write
        def fail_index(path, data):
            if path == self.vault.root / "index.enc":
                raise OSError("injected index write failure")
            return atomic(path, data)
        with patch.object(vault_module, "atomic_write", side_effect=fail_index):
            with self.assertRaises(OSError):
                self.navigation.rename_section("PRIVATE BOOK", "Ideas", "New")
        self.assertEqual(self.vault.inventory(), inventory)
        self.assertEqual(Navigation(self.vault).state, layout)
        reopened = Vault.open(self.vault.root, PASSWORD)
        try:
            self.assertEqual(reopened.inventory(), inventory)
        finally:
            reopened.lock()


if __name__ == "__main__":
    unittest.main()
