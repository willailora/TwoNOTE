"""Deleted branch visibility, explicit winner selection, and headless UI regressions."""
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
from twonote import inline, vault as vault_module
from twonote.vault import Vault, VaultError, FIELDS
from twonote.navigation import Navigation
from test_desktop_controls_logic import handlers

PASSWORD = "synthetic-conflict-password"


class ConflictResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / "vault", PASSWORD)
        image = self.vault.add_blob("fixture.png", "image/png", b"synthetic-image")
        self.base = self.vault.save(uuid.uuid4().hex, [], {"title":"Fixture", "notebook":"Book", "section":"Ideas", "content":"base", "attachments":[image]})
        self.keep = self.vault.save(self.base["note_id"], [self.base["revision"]], {**self.base, "content":inline.encode("前" + inline.token(image["id"]) + "後"), "tags":"keep-tag"})
        unwanted = self.vault.add_blob("unwanted.png", "image/png", b"UNWANTED-IMAGE")
        self.other = self.vault.save(self.base["note_id"], [self.base["revision"]], {**self.base, "content":"UNWANTED-BRANCH", "attachments":[unwanted]})
    def tearDown(self):
        self.vault.lock(); self.temp.cleanup()
    def snapshot(self): return [n["revision"] for n in self.vault.heads(True)]
    def delete_other(self): return self.vault.set_deleted(self.other["revision"], True)

    def test_deleted_branch_hidden_from_normal_heads_and_not_counted_as_conflict(self):
        dead = self.delete_other()
        self.assertEqual([(n["revision"], n["conflicts"]) for n in self.vault.heads()], [(self.keep["revision"], 1)])
        self.assertEqual(len(self.vault.heads(True)), 2)
        self.assertTrue(all(n["conflicts"] == 1 for n in self.vault.heads(True)))
        self.assertEqual(self.vault.get(dead["revision"])["content"], "UNWANTED-BRANCH")

    def test_old_tombstones_need_no_migration_and_stay_hidden_after_restart(self):
        self.delete_other(); before = self.vault.inventory(); self.vault.lock()
        self.vault = Vault.open(self.root / "vault", PASSWORD)
        self.assertEqual(len(self.vault.heads()), 1)
        self.assertEqual(before, self.vault.inventory())
        tree = Navigation(self.vault)
        shown = lambda deleted: [n for b in tree.tree(include_deleted=deleted) for s in b["sections"] for n in s["notes"]]
        self.assertEqual(len(shown(False)), 1); self.assertEqual(len(shown(True)), 2)

    def test_hidden_tombstone_does_not_block_moving_remaining_note(self):
        self.delete_other(); tree = Navigation(self.vault); tree.tree(); tree.add_folder("Book", "Other")
        moved = tree.apply_drop({"kind":"note", "book":"Book", "section":"Ideas", "note_id":self.keep["note_id"]}, {"kind":"section", "book":"Book", "section":"Other"}, "on")
        self.assertEqual(moved["section"], "Other"); self.assertEqual(moved["content"], self.keep["content"])
        self.assertEqual(self.vault.heads()[0]["conflicts"], 1)

    def test_choose_keeps_exact_body_attachments_metadata_and_all_history(self):
        dead = self.delete_other(); expected = self.snapshot()
        saved = self.vault.resolve_versions(self.keep["revision"], expected)
        for field in FIELDS + ("attachments", "deleted"):
            self.assertEqual(saved[field], self.keep[field], field)
        self.assertEqual(set(saved["parents"]), set(expected)); self.assertEqual(len(self.vault.heads(True)), 1)
        self.assertEqual(self.vault.get(dead["revision"])["content"], "UNWANTED-BRANCH")
        self.assertEqual(self.vault.get(self.other["revision"])["attachments"], self.other["attachments"])

    def test_merge_does_not_resurrect_deleted_body_or_images(self):
        self.delete_other()
        third = self.vault.save(self.base["note_id"], [self.base["revision"]], {**self.base, "content":"THIRD-LIVE-BRANCH"})
        self.assertEqual({n["conflicts"] for n in self.vault.heads()}, {2})
        saved = self.vault.resolve_versions(self.keep["revision"], self.snapshot(), merge=True)
        self.assertIn("THIRD-LIVE-BRANCH", saved["content"]); self.assertNotIn("UNWANTED-BRANCH", saved["content"])
        self.assertNotIn(self.other["attachments"][0]["id"], {a["id"] for a in saved["attachments"]})
        self.assertIn(third["revision"], saved["parents"])

    def test_all_deleted_are_hidden_but_can_be_restored(self):
        self.delete_other(); deleted = self.vault.set_deleted(self.keep["revision"], True)
        self.assertEqual(self.vault.heads(), []); self.assertEqual(len(self.vault.heads(True)), 2)
        self.vault.set_deleted(deleted["revision"], False)
        self.assertEqual(len(self.vault.heads()), 1); self.assertEqual(self.vault.heads()[0]["conflicts"], 1)

    def test_restoring_other_branch_reintroduces_a_real_live_conflict(self):
        dead = self.delete_other(); self.vault.set_deleted(dead["revision"], False)
        self.assertEqual(len(self.vault.heads()), 2); self.assertEqual({n["conflicts"] for n in self.vault.heads()}, {2})

    def test_unseen_sync_update_stale_target_and_deleted_choice_are_rejected(self):
        snapshot = self.snapshot()
        newer = self.vault.save(self.other["note_id"], [self.other["revision"]], {**self.other, "content":"NEW-REMOTE-EDIT"})
        before = self.vault.inventory()
        with self.assertRaises(VaultError): self.vault.resolve_versions(self.keep["revision"], snapshot)
        with self.assertRaises(VaultError): self.vault.set_deleted(self.other["revision"], True)
        self.assertEqual(before, self.vault.inventory())
        dead = self.vault.set_deleted(newer["revision"], True)
        with self.assertRaises(VaultError): self.vault.resolve_versions(dead["revision"], self.snapshot())

    def test_failed_resolution_does_not_publish_or_lose_branches(self):
        expected = self.snapshot(); before = self.vault.inventory(); write = vault_module.atomic_write
        def fail_index(path, data):
            if path == self.vault.root / "index.enc": raise OSError("synthetic full disk")
            return write(path, data)
        with patch("twonote.vault.atomic_write", side_effect=fail_index), self.assertRaises(OSError):
            self.vault.resolve_versions(self.keep["revision"], expected)
        self.assertEqual(self.snapshot(), expected); self.assertEqual(self.vault.inventory(), before)


class DeleteControllerTests(unittest.TestCase):
    def controller(self, remaining):
        box = types.SimpleNamespace(StandardButton=types.SimpleNamespace(Yes=1), question=Mock(return_value=1))
        kind = handlers("Desktop", {"delete_note"}, {"QMessageBox":box})
        desktop = kind(); desktop.current = {"note_id":"note", "revision":"unwanted", "deleted":False}
        desktop.flush = Mock(return_value=True); desktop.trash = Mock(); desktop.trash.isChecked.return_value = False
        desktop.vault = Mock(); desktop.vault.set_deleted.return_value = {"note_id":"note", "revision":"deleted", "deleted":True}; desktop.vault.heads.return_value = remaining
        desktop.refresh_list = Mock(); desktop.load_revision = Mock(); desktop.clear_current = Mock(); desktop.error = Mock()
        return desktop, box
    def test_delete_does_not_enable_trash_and_selects_remaining_live_branch(self):
        desktop, _ = self.controller([{"note_id":"note", "revision":"keep"}]); desktop.delete_note()
        desktop.trash.setChecked.assert_not_called(); desktop.load_revision.assert_called_once_with("keep"); desktop.error.assert_not_called()
    def test_last_deleted_note_clears_editor_instead_of_leaving_a_ghost(self):
        desktop, _ = self.controller([]); desktop.delete_note()
        desktop.clear_current.assert_called_once(); desktop.load_revision.assert_not_called()
    def test_confirmation_never_deletes_a_different_note_selected_by_sync(self):
        desktop, box = self.controller([])
        def sync_during_dialog(*args): desktop.current = {"note_id":"other", "revision":"new-remote", "deleted":False}; return 1
        box.question.side_effect = sync_during_dialog; desktop.delete_note()
        desktop.vault.set_deleted.assert_called_once_with("unwanted", True)

    def test_cleared_editor_cannot_accept_edits_to_the_deleted_note(self):
        kind = handlers("Desktop", {"clear_current"}, {"inline":inline})
        desktop = kind(); desktop.current = {"revision":"deleted"}; desktop.dirty = True
        desktop.fields = {"title":Mock(), "section":Mock()}; desktop.body = Mock(); desktop.vault = Mock()
        desktop.thumbnail_cache = {"old":"cached-image"}; desktop.attachment_scroll = Mock()
        desktop.save_timer = Mock(); desktop.save_status = Mock(); desktop.delete_button = Mock()
        desktop.clear_current()
        self.assertIsNone(desktop.current); self.assertFalse(desktop.dirty); self.assertFalse(desktop.loading)
        self.assertEqual(desktop.thumbnail_cache, {})
        desktop.body.setEnabled.assert_called_once_with(False)
        desktop.body.load_body.assert_called_once_with(inline.encode(""), [], desktop.vault)
        for entry in desktop.fields.values(): entry.clear.assert_called_once(); entry.setEnabled.assert_called_once_with(False)

    def test_selecting_another_note_reenables_the_cleared_editor(self):
        kind = handlers("Desktop", {"load_revision"}, {})
        desktop = kind(); desktop.vault = Mock(); desktop.vault.get.return_value = {"revision":"keep", "title":"Keep", "content":"text", "attachments":[], "notebook":"Book", "section":"Ideas", "deleted":False}
        desktop.fields = {"title":Mock()}; desktop.body = Mock(); desktop.save_timer = Mock()
        desktop.save_status = Mock(); desktop.delete_button = Mock(); desktop.navigation = Mock()
        desktop.note_list = Mock(); desktop.tree_items = {}; desktop.render_attachments = Mock()
        desktop.load_revision("keep")
        desktop.body.setEnabled.assert_called_once_with(True)
        desktop.fields["title"].setEnabled.assert_called_once_with(True)


if __name__ == "__main__": unittest.main()
