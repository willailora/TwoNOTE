"""Encrypted Windows tree layout; note classification uses ordinary revisions."""
import copy
import json
import uuid

from .vault import FIELDS, NOTE_ID, VaultError, aad, atomic_write, json_bytes, seal, unseal

FILE_NAME = "navigation.enc"
MAX_LAYOUT = 8 * 1024 * 1024


def empty_layout():
    return {"version": 1, "books": [], "sections": {}, "notes": [], "collapsed": []}


def group_key(kind, book, section=""):
    return json.dumps([kind, book, section], ensure_ascii=False, separators=(",", ":"))


def validate_layout(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise VaultError("ツリーの表示設定が不正です")
    for field in ("books", "notes", "collapsed"):
        items = value.get(field)
        if (not isinstance(items, list) or len(items) > 100000
                or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items)):
            raise VaultError("ツリーの表示設定が不正です")
    if any(not NOTE_ID.fullmatch(item) for item in value["notes"]):
        raise VaultError("ツリーの記事IDが不正です")
    sections = value.get("sections")
    if not isinstance(sections, dict) or set(sections) - set(value["books"]):
        raise VaultError("ツリーの分類が不正です")
    for items in sections.values():
        if (not isinstance(items, list) or len(items) > 100000
                or any(not isinstance(item, str) for item in items) or len(set(items)) != len(items)):
            raise VaultError("ツリーの分類が不正です")
    return copy.deepcopy(value)


def load_layout(vault):
    path = vault.root / FILE_NAME
    if not path.exists():
        return empty_layout()
    with path.open("rb") as source:
        raw = source.read(MAX_LAYOUT + 29)
    if len(raw) > MAX_LAYOUT + 28:
        raise VaultError("ツリーの表示設定が大きすぎます")
    return validate_layout(json.loads(unseal(vault._key(), raw, aad(vault.vault_id, "navigation"))))


def reordered(values, source, anchor=None, after=False):
    if anchor == source:
        return list(values)
    if source not in values or (anchor is not None and anchor not in values):
        raise VaultError("移動先が更新されています。もう一度選んでください")
    result = [item for item in values if item != source]
    position = len(result) if anchor is None else result.index(anchor) + int(after)
    result.insert(position, source)
    return result


def drop_plan(source, target, position):
    """Pure DnD semantics, shared with drag highlighting and headless tests."""
    if not source or source.get("kind") not in ("book", "section", "note"):
        raise VaultError("移動する項目を選んでください")
    if source.get("blocked"):
        raise VaultError("削除済み・競合中の記事は移動できません")
    after = position != "before"
    kind = source["kind"]
    plan = {"kind": kind, "book": source["book"], "section": source.get("section", ""), "anchor": None, "after": after}
    if kind == "note":
        plan["note_id"] = source["note_id"]
        if target:
            if target.get("blocked"):
                raise VaultError("削除済み・競合中の記事は移動先にできません")
            plan.update(book=target["book"], section=target.get("section", ""))
            if target["kind"] == "note":
                plan["anchor"] = target["note_id"]
        return plan
    if kind == "book":
        if target:
            plan["anchor"] = target["book"]
        return plan
    if target:
        if target["book"] != source["book"]:
            raise VaultError("セクションの並べ替えは同じノートブック内です。別の分類へは記事を移動してください")
        if target["kind"] != "book":
            plan["anchor"] = target.get("section", "")
    return plan


class Navigation:
    def __init__(self, vault):
        self.vault = vault
        self.recovered_from = None
        with vault.mutex:
            vault._key()  # Never quarantine metadata merely because the vault is locked.
            try:
                self.state = load_layout(vault)
            except (ValueError, TypeError, KeyError, UnicodeError):
                path = vault.root / FILE_NAME
                saved = vault.root / ("navigation.invalid-" + uuid.uuid4().hex + ".enc")
                path.rename(saved)  # Preserve ciphertext; never replace the note index.
                self.recovered_from = saved.name
                self.state = empty_layout()

    def _publish(self, state):
        state = validate_layout(state)
        if state == self.state and (self.vault.root / FILE_NAME).exists():
            return
        raw = json_bytes(state)
        if len(raw) > MAX_LAYOUT:
            raise VaultError("ツリーの表示設定が大きすぎます")
        atomic_write(self.vault.root / FILE_NAME, seal(self.vault._key(), raw, aad(self.vault.vault_id, "navigation")))
        self.state = state

    @staticmethod
    def _folder(state, book, section=None):
        if book not in state["books"]:
            state["books"].append(book)
        sections = state["sections"].setdefault(book, [])
        if section is not None and section not in sections:
            sections.append(section)

    def ensure(self, heads):
        state = copy.deepcopy(self.state)
        known = set(state["notes"])
        for note in sorted(heads, key=lambda n: (n["notebook"].casefold(), n["section"].casefold(), n["title"].casefold(), n["note_id"])):
            self._folder(state, note["notebook"], note["section"])
            if note["note_id"] not in known:
                state["notes"].append(note["note_id"])
                known.add(note["note_id"])
        self._publish(state)

    def tree(self, query="", include_deleted=False):
        with self.vault.mutex:
            heads = self.vault.heads(True)
            self.ensure(heads)
            query = query.strip().casefold()
            ranks = {identity: i for i, identity in enumerate(self.state["notes"])}
            groups = {}
            for note in heads:
                if note["deleted"] and not include_deleted:
                    continue
                if query and query not in " ".join(note.get(field, "") for field in FIELDS).casefold():
                    continue
                groups.setdefault((note["notebook"], note["section"]), []).append(note)
            result = []
            for book in self.state["books"]:
                sections = []
                for section in self.state["sections"].get(book, []):
                    notes = sorted(groups.get((book, section), []), key=lambda n: (ranks[n["note_id"]], n["revision"]))
                    if notes or not query:
                        sections.append({"name": section, "notes": notes})
                if sections or not query:
                    result.append({"name": book, "sections": sections})
            return result

    def add_folder(self, book, section=None):
        with self.vault.mutex:
            state = copy.deepcopy(self.state)
            self._folder(state, book, section)
            self._publish(state)

    def rename_section(self, book, section, name):
        with self.vault.mutex:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 256:
                raise VaultError("セクション名は1〜256文字で入力してください")
            name = name.strip()
            self.ensure(self.vault.heads(True))
            sections = self.state["sections"].get(book, [])
            if section not in sections:
                raise VaultError("変更するセクションがありません。もう一度選んでください")
            if name == section:
                return {}
            if name in sections:
                raise VaultError("同じ名前のセクションが既にあります。別の名前を入力してください")
            state = copy.deepcopy(self.state)
            state["sections"][book] = [name if item == section else item for item in sections]
            old_key, new_key = group_key("section", book, section), group_key("section", book, name)
            state["collapsed"] = [new_key if item == old_key else item
                                  for item in state["collapsed"] if item != new_key]
            old_state = self.state
            self._publish(state)
            try:
                return self.vault.rename_section(book, section, name)
            except Exception:
                try:
                    self._publish(old_state)
                except OSError as failure:
                    raise VaultError("名前の保存に失敗し、表示設定を戻せませんでした。記事は変更されていません") from failure
                raise

    def remove_empty(self, kind, book, section=""):
        with self.vault.mutex:
            if any(note["notebook"] == book and (kind == "book" or note["section"] == section)
                   for note in self.vault.heads(True)):
                raise VaultError("記事がある分類は削除できません（削除済み記事も含みます）")
            state = copy.deepcopy(self.state)
            if kind == "book":
                state["books"].remove(book)
                state["sections"].pop(book, None)
            elif kind == "section":
                state["sections"][book].remove(section)
            else:
                raise VaultError("空の分類だけを削除できます")
            self._publish(state)

    def collapse(self, kind, book, section, collapsed):
        with self.vault.mutex:
            state = copy.deepcopy(self.state)
            key = group_key(kind, book, section)
            state["collapsed"] = [entry for entry in state["collapsed"] if entry != key]
            if collapsed:
                state["collapsed"].append(key)
            self._publish(state)

    def reveal(self, book, section):
        with self.vault.mutex:
            state = copy.deepcopy(self.state)
            keys = {group_key("book", book), group_key("section", book, section)}
            state["collapsed"] = [entry for entry in state["collapsed"] if entry not in keys]
            self._publish(state)

    def apply_drop(self, source, target, position):
        with self.vault.mutex:
            heads = self.vault.heads(True)
            self.ensure(heads)
            plan = drop_plan(source, target, position)
            state = copy.deepcopy(self.state)
            result = None
            if plan["kind"] == "note":
                versions = [note for note in heads if note["note_id"] == plan["note_id"] and not note["deleted"]]
                if len(versions) != 1 or versions[0]["deleted"]:
                    raise VaultError("記事が更新・削除されたか競合しています。先に内容を確認してください")
                note = versions[0]
                if target is None:
                    plan.update(book=note["notebook"], section=note["section"])
                book, section = plan["book"], plan["section"]
                if book not in state["books"] or (target and target["kind"] != "book" and section not in state["sections"].get(book, [])):
                    raise VaultError("移動先の分類がありません")
                if plan["anchor"] is not None:
                    anchors = [n for n in heads if n["note_id"] == plan["anchor"] and not n["deleted"]]
                    if len(anchors) != 1 or anchors[0]["deleted"] or (anchors[0]["notebook"], anchors[0]["section"]) != (book, section):
                        raise VaultError("移動先の記事が更新されています。もう一度選んでください")
                self._folder(state, book, section)
                state["notes"] = reordered(state["notes"], note["note_id"], plan["anchor"], plan["after"])
                old = copy.deepcopy(self.state)
                self._publish(state)
                try:
                    result = self.vault.save(note["note_id"], [note["revision"]], {**note, "notebook": book, "section": section})
                except Exception:
                    # A failed note commit cannot leave its displayed order changed.
                    try:
                        self._publish(old)
                    except OSError as failure:
                        raise VaultError("記事の保存に失敗し、表示順の巻き戻しもできませんでした。記事の内容は保持されています") from failure
                    raise
            elif plan["kind"] == "book":
                state["books"] = reordered(state["books"], plan["book"], plan["anchor"], plan["after"])
                self._publish(state)
            else:
                book = plan["book"]
                state["sections"][book] = reordered(state["sections"][book], plan["section"], plan["anchor"], plan["after"])
                self._publish(state)
            return result
