"""Portable encrypted backups; restore never overwrites an existing vault."""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from .vault import MAX_BLOB, MAX_RECORDS, MAX_REVISION, Vault, VaultError, aad, atomic_write, json_bytes, seal, unlock_key

OBJECT = re.compile(r"(blobs|revisions)/[0-9a-f]{64}\Z")


def export_backup(vault: Vault, destination: Path) -> None:
    destination = Path(destination).resolve()
    if destination.is_relative_to(vault.root):
        raise VaultError("バックアップは保管庫の外へ保存してください")
    descriptor, temporary = tempfile.mkstemp(prefix=".twonote-backup-", dir=destination.parent)
    os.close(descriptor)
    try:
        with vault.mutex, zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
            inventory = vault.inventory()
            from .navigation import FILE_NAME, load_layout
            if (vault.root / FILE_NAME).exists():
                try:
                    load_layout(vault)
                except (ValueError, TypeError, KeyError, UnicodeError):
                    pass  # Optional display metadata must not prevent a note backup.
                else:
                    archive.write(vault.root / FILE_NAME, FILE_NAME)
            archive.writestr("config.json", json_bytes(vault.config))
            archive.writestr("index.enc", seal(vault._key(), json_bytes({"version": 2, "revisions": inventory["revisions"]}), aad(vault.vault_id, "index")))
            for kind in ("blobs", "revisions"):
                for oid in inventory[kind]:
                    archive.writestr(kind + "/" + oid, vault.read_object(kind, oid))
        with open(temporary, "r+b") as written:
            os.fsync(written.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_backup(source: Path, destination: Path, password: str) -> Vault:
    destination = Path(destination).resolve()
    if destination.exists():
        raise VaultError("復元先に保管庫が存在します。別の空フォルダを選んでください")
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or len(names) > MAX_RECORDS * 2 + 3:
            raise VaultError("バックアップの一覧が不正です")
        if not {"config.json", "index.enc"}.issubset(names):
            raise VaultError("TwoNoteのバックアップではありません")
        for item in entries:
            if item.filename == "config.json":
                limit = 4096
            elif item.filename == "index.enc":
                limit = 16 * 1024 * 1024
            elif item.filename == "navigation.enc":
                from .navigation import MAX_LAYOUT
                limit = MAX_LAYOUT + 28
            elif OBJECT.fullmatch(item.filename):
                limit = (MAX_BLOB if item.filename.startswith("blobs/") else MAX_REVISION) + 28
            else:
                raise VaultError("バックアップに想定外のファイルが含まれています")
            if item.file_size > limit or item.is_dir():
                raise VaultError("バックアップ内のファイルサイズが不正です")
        config = json.loads(archive.read("config.json"))
        # Authenticate before creating or copying any vault data.
        key = bytearray(unlock_key(config, password))
        for i in range(len(key)):
            key[i] = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
            scratch = Path(temporary).resolve()
            for item in entries:
                atomic_write(scratch / item.filename, archive.read(item))
            check = Vault.open(scratch, password)
            try:
                if check.recovered_index:
                    raise VaultError("バックアップの索引が破損しています")
                for oid in check.inventory()["blobs"]:
                    check.read_blob(oid)
                from .navigation import load_layout
                try:
                    load_layout(check)
                except (ValueError, TypeError, KeyError, UnicodeError):
                    pass  # Navigation quarantines/rebuilds it on the next UI open.
            finally:
                check.lock()
            # Settings, pairing credentials and TLS identity are deliberately not
            # restored. A restored PC needs a new explicit phone pairing.
            os.rename(scratch, destination)
    return Vault.open(destination, password)
