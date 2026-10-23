import os
import json
import re
import sys
from pathlib import Path


ACTIVE_VAULT_FILE = "active-vault.json"
RECOVERED_VAULT = re.compile(r"vault-v2-recovered-[0-9a-f]{32}\Z")


def active_vault_path(data_dir: Path) -> Path:
    """An atomic, non-secret pointer lets recovery keep the old vault untouched."""
    data_dir = Path(data_dir).resolve()
    marker = data_dir / ACTIVE_VAULT_FILE
    if not marker.exists():
        return data_dir / "vault-v2"
    try:
        value = json.loads(marker.read_bytes())
        name = value["directory"]
        if value["version"] != 1 or not isinstance(name, str) or not RECOVERED_VAULT.fullmatch(name):
            raise ValueError()
        target = (data_dir / name).resolve()
        if target.parent != data_dir or not (target / "config.json").is_file():
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError("保管庫の切替設定を読み取れません。データは削除していません。保存先を確認するか、OneNote原本から作り直してください") from exc
    return target


def default_data_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    for candidate in (base / "data", base.parent / "data"):
        if (candidate / ACTIVE_VAULT_FILE).is_file() or (candidate / "vault-v2" / "config.json").is_file() or (candidate / "notes.sqlite3").is_file():
            return candidate
    return Path(os.environ.get("LOCALAPPDATA", str(base))) / "TwoNote"
