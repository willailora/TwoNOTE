"""Command-line import into a v2 encrypted local vault."""
import argparse
import getpass
from pathlib import Path

from twonote.paths import active_vault_path, default_data_dir
from twonote.importing import import_into
from twonote.legacy import migrate_legacy
from twonote.vault import InstanceLock, Vault


def main():
    parser = argparse.ArgumentParser(description="TwoNote import")
    parser.add_argument("source", type=Path)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args()
    lock = InstanceLock(args.data_dir)
    vault = None
    try:
        password = getpass.getpass("保管庫のパスワード: ")
        root = active_vault_path(args.data_dir)
        if (root / "config.json").exists():
            vault = Vault.open(root, password)
        elif (args.data_dir / "notes.sqlite3").exists():
            vault, migration = migrate_legacy(args.data_dir, password)
            if migration.get("remaining"):
                print("旧データの整理が未完了です。旧TwoNoteを終了して再度開いてください。")
        else:
            if password != getpass.getpass("確認用パスワード: "):
                parser.error("パスワードが一致しません")
            vault = Vault.create(root, password)
        result = import_into(vault, args.source)
        print(f"取り込み {result['imported']}件 / 既存 {result['skipped']}件 / 添付 {result['attachments']}件")
        for warning in result["warnings"]:
            print("一部未対応:", warning)
    finally:
        if vault:
            vault.lock()
        lock.close()


if __name__ == "__main__":
    main()
