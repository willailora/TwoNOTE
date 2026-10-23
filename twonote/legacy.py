"""One-way migration with a verified encrypted recovery archive.

Never encrypt legacy attachments in place. The new vault is built separately;
only after validation and publication may the archived legacy files be removed.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path

from .vault import Vault, VaultError, aad, atomic_write, derive, digest, seal, unseal


def legacy_key(source: Path, password: str) -> bytes | None:
    config_path = source / "vault.json"
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_bytes())
    if config.get("iterations") != 600_000:
        raise VaultError("旧保管庫の鍵設定に対応していません")
    key = derive(password, base64.urlsafe_b64decode(config["salt"]))
    verifier = unseal(key, base64.urlsafe_b64decode(config["verifier"]), b"verifier")
    if verifier != b"TwoNote vault verifier v1":
        raise VaultError("旧保管庫のパスワードが違います")
    return key


def _decode_row(row, key: bytes | None, fields: tuple[str, ...]) -> dict:
    if "payload" in row.keys() and row["payload"]:
        if key is None:
            raise VaultError("旧暗号化設定がありません。元のvault.jsonが必要です")
        return json.loads(unseal(key, row["payload"], row["id"].encode("utf-8")))
    return {field: row[field] for field in fields}


def _attachment_bytes(raw: bytes, attachment_id: str, key: bytes | None, encrypted: bool) -> bytes:
    if key is None:
        if encrypted:
            raise VaultError("旧保管庫の鍵がありません")
        return raw
    # Old interrupted migrations could encrypt a file without committing metadata,
    # or encrypt it twice. Each successful GCM tag verifies a real encryption layer.
    decoded = raw
    layers = 0
    for _ in range(8):
        try:
            candidate = unseal(key, decoded, attachment_id.encode("utf-8"))
        except VaultError:
            break
        decoded = candidate
        layers += 1
    if encrypted and layers == 0:
        raise VaultError("旧添付ファイルを復号できません。移行を中止しました")
    return decoded


def _contained(source: Path, relative: str) -> Path:
    target = (source / relative).resolve()
    if not target.is_relative_to(source.resolve()) or target == source.resolve():
        raise VaultError("旧データのパスが保存先の外を指しています")
    return target


def cleanup_legacy(vault: Vault, source: Path) -> list[str]:
    """Resume cleanup only for files verified in the encrypted backup manifest."""
    settings = vault.settings()
    migration = settings.get("legacy_migration")
    if not migration or migration.get("cleaned"):
        return []
    archive = unseal(vault._key(), (vault.root / "legacy-backup.enc").read_bytes(), aad(vault.vault_id, "legacy-backup"))
    with zipfile.ZipFile(io.BytesIO(archive)) as recovery:
        if recovery.testzip() is not None:
            raise VaultError("暗号化バックアップの検証に失敗しました")
        for name, expected in migration["files"].items():
            if digest(recovery.read(name)) != expected:
                raise VaultError("暗号化バックアップの内容が一致しません")
        snapshot = recovery.read("recovery/notes.sqlite3")

    def same_archived_file(relative, raw, expected):
        if digest(raw) == expected:
            return True
        if relative != "notes.sqlite3" or len(raw) != len(snapshot):
            return False
        # Closing the last SQLite WAL reader can checkpoint committed pages and
        # update these non-content header counters. Compare every other byte to
        # the fully serialized snapshot that is already in the verified archive.
        def content_bytes(database):
            return database[:24] + database[28:92] + database[100:]
        return raw.startswith(b"SQLite format 3\x00") and content_bytes(raw) == content_bytes(snapshot)

    # Validate the entire legacy set before removing any member. A changed WAL
    # must not leave a newly edited old database without its matching files.
    changed = []
    for relative, expected in migration["files"].items():
        target = _contained(source, relative)
        if target.exists():
            try:
                if not same_archived_file(relative, target.read_bytes(), expected):
                    changed.append(relative)
            except OSError:
                changed.append(relative)
    if changed:
        return changed
    remaining = []
    for relative, expected in migration["files"].items():
        target = _contained(source, relative)
        if not target.exists():
            continue
        try:
            if not same_archived_file(relative, target.read_bytes(), expected):
                remaining.append(relative)
                continue
            target.unlink()
        except OSError:
            remaining.append(relative)
    migration["cleaned"] = not remaining
    vault.set_settings(settings)
    return remaining


def migrate_legacy(source: Path, password: str) -> tuple[Vault, dict]:
    source = Path(source).resolve()
    destination = source / "vault-v2"
    if (destination / "config.json").exists():
        vault = Vault.open(destination, password)
        return vault, {"existing": True, "remaining": cleanup_legacy(vault, source)}
    key = legacy_key(source, password)
    database = source / "notes.sqlite3"
    if not database.is_file():
        raise VaultError("旧ノートDBがありません")
    scratch = Path(tempfile.mkdtemp(prefix=".migration-", dir=source))
    vault = Vault.create(scratch, password)
    old_files: dict[str, bytes] = {}
    db = sqlite3.connect(database, timeout=3, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        # Prevent simultaneous legacy writers during the snapshot. The old app
        # must be stopped; failure to acquire the writer lock aborts migration.
        db.execute("BEGIN IMMEDIATE")
        notes = db.execute("SELECT * FROM notes").fetchall()
        attachments = db.execute("SELECT * FROM attachments").fetchall()
        for candidate in (database, source / "notes.sqlite3-wal", source / "vault.json"):
            if candidate.is_file():
                old_files[candidate.relative_to(source).as_posix()] = candidate.read_bytes()
        attachment_dir = source / "attachments"
        if attachment_dir.exists():
            for candidate in attachment_dir.rglob("*"):
                if candidate.is_file():
                    if not candidate.resolve().is_relative_to(attachment_dir.resolve()):
                        raise VaultError("旧添付ファイルに外部へのリンクがあります")
                    old_files[candidate.relative_to(source).as_posix()] = candidate.read_bytes()
        converted: dict[str, list[dict]] = {}
        for attachment in attachments:
            metadata = _decode_row(attachment, key, ("filename", "mime", "size"))
            relative = "attachments/" + attachment["stored_name"]
            path = _contained(source, relative)
            stored = old_files[path.relative_to(source).as_posix()]
            plain = _attachment_bytes(stored, attachment["id"], key, bool(attachment["payload"]) if "payload" in attachment.keys() else False)
            item = vault.add_blob(metadata.get("filename") or "attachment", metadata.get("mime") or "application/octet-stream", plain)
            if vault.read_blob(item["id"]) != plain:
                raise VaultError("移行した添付を検証できません")
            converted.setdefault(attachment["note_id"], []).append(item)
        for note in notes:
            values = _decode_row(note, key, ("title", "content", "notebook", "section", "tags", "source_path"))
            values["attachments"] = converted.get(note["id"], [])
            note_id = uuid.UUID(note["id"]).hex if len(note["id"]) == 32 else uuid.uuid5(uuid.NAMESPACE_OID, note["id"]).hex
            vault.save(note_id, [], values)
        if len(vault.heads()) != len(notes):
            raise VaultError("移行前後のノート件数が一致しません")
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in old_files.items():
                archive.writestr(name, raw)
            # This SQLite snapshot can be recovered without WAL / SHM files.
            archive.writestr("recovery/notes.sqlite3", db.serialize())
        encrypted = seal(vault._key(), archive_buffer.getvalue(), aad(vault.vault_id, "legacy-backup"))
        atomic_write(scratch / "legacy-backup.enc", encrypted)
        if unseal(vault._key(), encrypted, aad(vault.vault_id, "legacy-backup")) != archive_buffer.getvalue():
            raise VaultError("暗号化バックアップの検証に失敗しました")
        vault.set_settings({"legacy_migration": {"files": {p: digest(raw) for p, raw in old_files.items()}, "cleaned": False}})
        # A final reopen validates the complete graph before publishing it.
        check = Vault.open(scratch, password)
        check.lock()
        os.rename(scratch, destination)
        vault.root = destination
    except BaseException:
        vault.lock()
        # The scratch directory contains only ciphertext. Preserve it for recovery
        # rather than deleting anything after an unexpected migration failure.
        raise
    finally:
        db.close()
    remaining = cleanup_legacy(vault, source)
    # SHM contains SQLite coordination data, not pages; only delete after DB/WAL
    # cleanup, and only inside the explicitly migrated legacy directory.
    if not remaining:
        (source / "notes.sqlite3-shm").unlink(missing_ok=True)
    return vault, {"notes": len(notes), "attachments": len(attachments), "remaining": remaining}
