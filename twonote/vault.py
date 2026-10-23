"""Encrypted, crash-consistent local storage shared by both native clients.

Immutable revisions preserve concurrent edits. Only an atomic encrypted index
publishes changes, so incomplete writes / syncs never replace the visible state.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
MAX_BLOB = 64 * 1024 * 1024
MAX_REVISION = 4 * 1024 * 1024
MAX_RECORDS = 100_000
HASH = re.compile(r"[0-9a-f]{64}\Z")
NOTE_ID = re.compile(r"[0-9a-f]{32}\Z")
FIELDS = ("title", "content", "notebook", "section", "tags", "source_path")


class VaultError(ValueError):
    pass


class LockedError(VaultError):
    pass


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def seal(key: bytes, raw: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, raw, aad)


def unseal(key: bytes, raw: bytes, aad: bytes) -> bytes:
    if len(raw) < 28:
        raise VaultError("暗号化ファイルが壊れています")
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag as exc:
        raise VaultError("パスワードが違うか、暗号化ファイルが破損しています") from exc


def aad(vault_id: str, purpose: str) -> bytes:
    return f"twonote:2:{vault_id}:{purpose}".encode("ascii")


def validate_config(config: dict) -> dict:
    try:
        if config["version"] != 2 or not NOTE_ID.fullmatch(config["vault_id"]):
            raise ValueError()
        if config["iterations"] != ITERATIONS:
            raise ValueError()
        if len(base64.b64decode(config["salt"], validate=True)) != 16:
            raise ValueError()
        if len(base64.b64decode(config["wrapped_key"], validate=True)) != 60:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError("対応していない保管庫設定です") from exc
    return config


def derive(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERATIONS).derive(password.encode("utf-8"))


def unlock_key(config: dict, password: str) -> bytes:
    validate_config(config)
    wrapping = derive(password, base64.b64decode(config["salt"]))
    return unseal(wrapping, base64.b64decode(config["wrapped_key"]), aad(config["vault_id"], "key"))


def new_config(password: str) -> tuple[dict, bytes]:
    if len(password) < 12:
        raise VaultError("パスワードは12文字以上にしてください")
    key, salt, vault_id = os.urandom(32), os.urandom(16), uuid.uuid4().hex
    config = {"version": 2, "vault_id": vault_id, "iterations": ITERATIONS, "salt": b64(salt),
              "wrapped_key": b64(seal(derive(password, salt), key, aad(vault_id, "key")))}
    return config, key


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Vault:
    def __init__(self, root: Path, config: dict, key: bytes):
        self.root = Path(root).resolve()
        self.config = copy.deepcopy(validate_config(config))
        self.vault_id = config["vault_id"]
        self._secret: bytearray | None = bytearray(key)
        self.mutex = threading.RLock()
        self.records: dict[str, dict] = {}
        self.recovered_index = False
        self.generation = 0
        self._load()

    @classmethod
    def create(cls, root: Path, password: str, config: dict | None = None) -> "Vault":
        root = Path(root).resolve()
        if root.exists() and any(root.iterdir()):
            raise VaultError("保管庫の作成先は空のフォルダにしてください。既存ファイルは変更していません")
        if config is None:
            config, key = new_config(password)
        else:
            key = unlock_key(config, password)
        root.mkdir(parents=True, exist_ok=True)
        # Publish config last. No plaintext note data is written, even temporarily.
        atomic_write(root / "index.enc", seal(key, json_bytes({"version": 2, "revisions": []}),
                                             aad(config["vault_id"], "index")))
        with (root / "config.json").open("xb") as output:
            output.write(json_bytes(config))
            output.flush()
            os.fsync(output.fileno())
        return cls(root, config, key)

    @classmethod
    def open(cls, root: Path, password: str) -> "Vault":
        root = Path(root)
        config = json.loads((root / "config.json").read_bytes())
        return cls(root, config, unlock_key(config, password))

    def _key(self) -> bytes:
        if self._secret is None:
            raise LockedError("保管庫はロックされています")
        return bytes(self._secret)

    def lock(self) -> None:
        with self.mutex:
            if self._secret is not None:
                self._secret[:] = b"\0" * len(self._secret)
            self._secret = None
            self.records.clear()
            self.generation += 1

    def _index_ids(self, path: Path) -> list[str]:
        value = json.loads(unseal(self._key(), path.read_bytes(), aad(self.vault_id, "index")))
        ids = value.get("revisions")
        if value.get("version") != 2 or not isinstance(ids, list) or len(ids) > MAX_RECORDS:
            raise VaultError("保管庫の索引が壊れています")
        if any(not isinstance(i, str) or not HASH.fullmatch(i) for i in ids) or len(ids) != len(set(ids)):
            raise VaultError("保管庫の索引が壊れています")
        return ids

    def _load(self) -> None:
        try:
            ids = self._index_ids(self.root / "index.enc")
        except (OSError, ValueError):
            backup = self.root / "index.backup.enc"
            if not backup.exists():
                raise
            ids = self._index_ids(backup)
            self.recovered_index = True
        records = {i: self._read_revision(i) for i in ids}
        self._validate_graph(records)
        self.records = records

    def object_path(self, kind: str, object_id: str) -> Path:
        if kind not in ("blobs", "revisions") or not isinstance(object_id, str) or not HASH.fullmatch(object_id):
            raise VaultError("オブジェクトIDが不正です")
        return self.root / kind / object_id

    def read_object(self, kind: str, object_id: str) -> bytes:
        self._key()
        raw = self.object_path(kind, object_id).read_bytes()
        if digest(raw) != object_id:
            raise VaultError("保存ファイルの整合性を確認できません")
        return raw

    def _validate_revision(self, record: dict) -> dict:
        if not isinstance(record, dict) or not NOTE_ID.fullmatch(str(record.get("note_id", ""))):
            raise VaultError("ノートIDが不正です")
        parents = record.get("parents")
        if not isinstance(parents, list) or len(parents) > 512:
            raise VaultError("変更履歴が不正です")
        if any(not isinstance(p, str) or not HASH.fullmatch(p) for p in parents):
            raise VaultError("変更履歴が不正です")
        if len(set(parents)) != len(parents):
            raise VaultError("変更履歴が重複しています")
        for field in FIELDS + ("modified",):
            if not isinstance(record.get(field), str):
                raise VaultError("ノートの形式が不正です")
        if not isinstance(record.get("deleted"), bool) or not isinstance(record.get("attachments"), list):
            raise VaultError("ノートの形式が不正です")
        if len(record["attachments"]) > 1000:
            raise VaultError("添付ファイルが多すぎます")
        for item in record["attachments"]:
            if not isinstance(item, dict) or not HASH.fullmatch(str(item.get("id", ""))):
                raise VaultError("添付IDが不正です")
            if not isinstance(item.get("filename"), str) or not isinstance(item.get("mime"), str):
                raise VaultError("添付ファイル名が不正です")
            if type(item.get("size")) is not int or not 0 <= item["size"] <= MAX_BLOB:
                raise VaultError("添付サイズが不正です")
        return record

    def _read_revision(self, revision_id: str) -> dict:
        raw = self.read_object("revisions", revision_id)
        if len(raw) > MAX_REVISION + 28:
            raise VaultError("ノートが大きすぎます")
        record = json.loads(unseal(self._key(), raw, aad(self.vault_id, "revision")))
        return self._validate_revision(record)

    def _validate_graph(self, records: dict[str, dict], new_ids=None) -> None:
        if len(records) > MAX_RECORDS:
            raise VaultError("変更履歴が多すぎます")
        # Existing records were validated on open/commit and are immutable.
        # Only new records can introduce missing parents, blobs or graph cycles.
        checking = records if new_ids is None else {rid: records[rid] for rid in new_ids}
        blobs = set()
        for record in checking.values():
            for parent in record["parents"]:
                if parent not in records or records[parent]["note_id"] != record["note_id"]:
                    raise VaultError("変更履歴の一部が不足しています。同期を再実行してください")
            for attachment in record["attachments"]:
                blobs.add(attachment["id"])
        for blob in blobs:
            if not self.object_path("blobs", blob).is_file():
                raise VaultError("添付ファイルの一部が不足しています。同期を再実行してください")
        # Kahn traversal also rejects cyclic imported graphs, without recursion limits.
        degrees = {rid: sum(parent in checking for parent in record["parents"]) for rid, record in checking.items()}
        children: dict[str, list[str]] = {}
        for rid, record in checking.items():
            for parent in record["parents"]:
                if parent in checking:
                    children.setdefault(parent, []).append(rid)
        queue = [rid for rid, count in degrees.items() if count == 0]
        visited = 0
        while queue:
            visited += 1
            for child in children.get(queue.pop(), []):
                degrees[child] -= 1
                if degrees[child] == 0:
                    queue.append(child)
        if visited != len(checking):
            raise VaultError("変更履歴が循環しています")

    def inventory(self) -> dict:
        with self.mutex:
            self._key()
            blobs = {a["id"] for r in self.records.values() for a in r["attachments"]}
            return {"vault_id": self.vault_id, "revisions": sorted(self.records), "blobs": sorted(blobs)}

    def stage_object(self, kind: str, object_id: str, raw: bytes) -> None:
        with self.mutex:
            limit = MAX_BLOB if kind == "blobs" else MAX_REVISION
            target = self.object_path(kind, object_id)
            if len(raw) > limit + 28 or digest(raw) != object_id:
                raise VaultError("同期データのサイズまたはハッシュが不正です")
            plain = unseal(self._key(), raw, aad(self.vault_id, "blob" if kind == "blobs" else "revision"))
            if kind == "revisions":
                self._validate_revision(json.loads(plain))
            if target.exists():
                if digest(target.read_bytes()) != object_id:
                    raise VaultError("既存ファイルが破損しています")
                return
            atomic_write(target, raw)

    def commit(self, revisions: list[str]) -> int:
        with self.mutex:
            self._key()
            if not isinstance(revisions, list) or len(revisions) > MAX_RECORDS:
                raise VaultError("同期の変更数が不正です")
            new = set(revisions) - self.records.keys()
            if not new:
                return 0
            candidate = dict(self.records)
            for rid in new:
                candidate[rid] = self._read_revision(rid)
            self._validate_graph(candidate, new)
            self._publish(candidate)
            return len(new)

    def _publish(self, records: dict[str, dict]) -> None:
        raw = seal(self._key(), json_bytes({"version": 2, "revisions": sorted(records)}), aad(self.vault_id, "index"))
        index = self.root / "index.enc"
        if not self.recovered_index:
            atomic_write(self.root / "index.backup.enc", index.read_bytes())
        atomic_write(index, raw)
        self.records = records
        self.recovered_index = False
        self.generation += 1

    def heads(self, include_deleted: bool = False) -> list[dict]:
        with self.mutex:
            self._key()
            parents = {p for r in self.records.values() for p in r["parents"]}
            all_heads = {i: r for i, r in self.records.items() if i not in parents}
            counts: dict[str, int] = {}
            for record in all_heads.values():
                if not record["deleted"]:
                    counts[record["note_id"]] = counts.get(record["note_id"], 0) + 1
            result = []
            for rid, record in all_heads.items():
                if not include_deleted and record["deleted"]:
                    continue
                result.append({**copy.deepcopy(record), "revision": rid,
                               "conflicts": 1 if record["deleted"] else counts[record["note_id"]]})
            return sorted(result, key=lambda r: (r["modified"], r["revision"]), reverse=True)

    def get(self, revision: str) -> dict:
        with self.mutex:
            self._key()
            return {**copy.deepcopy(self.records[revision]), "revision": revision}

    def save(self, note_id: str, parents: list[str], values: dict) -> dict:
        with self.mutex:
            self._key()
            rid = self._stage_revision(note_id, parents, values)
            self.commit([rid])
            return self.get(rid)

    def resolve_versions(self, revision: str, expected: list[str], merge=False) -> dict:
        """Close exactly the reviewed branches, retaining all immutable history.

        Deleted heads participate as parents, never as content to resurrect.
        Checking the snapshot under the commit mutex protects unseen sync edits.
        """
        with self.mutex:
            chosen = self.get(revision)
            heads = [n for n in self.heads(True) if n["note_id"] == chosen["note_id"]]
            actual = {n["revision"] for n in heads}
            if revision not in actual or actual != set(expected):
                raise VaultError("別の端末で編集版が更新されました。表示を確認してからやり直してください")
            if chosen["deleted"]:
                raise VaultError("削除済みの版は採用できません。必要な場合は先に復元してください")
            values = chosen
            if merge:
                from . import inline
                live = [n for n in heads if not n["deleted"]]
                values = {**chosen, "content": inline.merge(live),
                          "attachments": list({a["id"]: a for n in live for a in n["attachments"]}.values())}
            return self.save(chosen["note_id"], sorted(actual), values)

    def set_deleted(self, revision: str, deleted: bool) -> dict:
        with self.mutex:
            chosen = self.get(revision)
            if revision not in {n["revision"] for n in self.heads(True)}:
                raise VaultError("選択した編集版は更新されています。表示を確認してからやり直してください")
            return self.save(chosen["note_id"], [revision], {**chosen, "deleted": deleted})

    def _stage_revision(self, note_id: str, parents: list[str], values: dict) -> str:
        """Prepare an immutable revision; caller holds mutex and publishes later."""
        record = {f: str(values.get(f, "")) for f in FIELDS}
        record["title"] = record["title"].strip() or "無題"
        record.update(note_id=note_id, parents=list(parents),
                      modified=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                      deleted=bool(values.get("deleted", False)), attachments=copy.deepcopy(values.get("attachments", [])))
        self._validate_revision(record)
        if any(p not in self.records or self.records[p]["note_id"] != note_id for p in parents):
            raise VaultError("保存元のノートが違います")
        if len(parents) == 1 and parents[0] in self.records:
            previous = self.records[parents[0]]
            if all(previous.get(f) == record.get(f) for f in FIELDS + ("attachments", "deleted")):
                return parents[0]
        raw = json_bytes(record)
        if len(raw) > MAX_REVISION:
            raise VaultError("ノート本文が大きすぎます")
        encrypted = seal(self._key(), raw, aad(self.vault_id, "revision"))
        rid = digest(encrypted)
        self.stage_object("revisions", rid, encrypted)
        return rid

    def rename_section(self, notebook: str, section: str, name: str) -> dict[str, dict]:
        """Rename every current branch in one index commit, including tombstones."""
        with self.mutex:
            self._key()
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 256:
                raise VaultError("セクション名は1〜256文字で入力してください")
            name = name.strip()
            if name == section:
                return {}
            heads = self.heads(True)
            if any(n["notebook"] == notebook and n["section"] == name for n in heads):
                raise VaultError("同じ名前のセクションが既にあります。別の名前を入力してください")
            changes = {}
            for note in heads:
                if note["notebook"] == notebook and note["section"] == section:
                    # Keep each conflicting branch separate; do not resolve edits.
                    changes[note["revision"]] = self._stage_revision(
                        note["note_id"], [note["revision"]], {**note, "section": name})
            self.commit(list(changes.values()))
            return {previous: self.get(current) for previous, current in changes.items()}

    def add_blob(self, filename: str, mime: str, raw: bytes) -> dict:
        with self.mutex:
            if len(raw) > MAX_BLOB:
                raise VaultError("添付は64MB以下にしてください")
            # OneNote commonly names extracted pictures .bin without a MIME type.
            # Identify only well-known raster headers; never interpret active SVG.
            if not mime or mime == "application/octet-stream":
                for signature, raster_mime in ((b"\x89PNG\r\n\x1a\n", "image/png"),
                                               (b"\xff\xd8\xff", "image/jpeg"),
                                               (b"GIF87a", "image/gif"),
                                               (b"GIF89a", "image/gif"),
                                               (b"BM", "image/bmp")):
                    if raw.startswith(signature):
                        mime = raster_mime
                        break
                if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
                    mime = "image/webp"
            encrypted = seal(self._key(), raw, aad(self.vault_id, "blob"))
            bid = digest(encrypted)
            self.stage_object("blobs", bid, encrypted)
            return {"id": bid, "filename": Path(filename).name, "mime": mime or "application/octet-stream", "size": len(raw)}

    def read_blob(self, blob_id: str) -> bytes:
        with self.mutex:
            return unseal(self._key(), self.read_object("blobs", blob_id), aad(self.vault_id, "blob"))

    def settings(self) -> dict:
        with self.mutex:
            self._key()
            path = self.root / "settings.enc"
            return json.loads(unseal(self._key(), path.read_bytes(), aad(self.vault_id, "settings"))) if path.exists() else {}

    def set_settings(self, settings: dict) -> None:
        with self.mutex:
            atomic_write(self.root / "settings.enc", seal(self._key(), json_bytes(settings), aad(self.vault_id, "settings")))


class InstanceLock:
    """A second desktop instance cannot independently replace the same index."""
    def __init__(self, root: Path):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.file = (Path(root) / ".instance.lock").open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise VaultError("この保管庫は別のTwoNoteで開かれています") from exc

    def close(self):
        self.file.close()
