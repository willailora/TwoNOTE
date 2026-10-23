"""Rebuild from an original export without decrypting or replacing old files."""
from pathlib import Path
import uuid

from .importing import import_into
from .paths import ACTIVE_VAULT_FILE
from .vault import Vault, VaultError, atomic_write, json_bytes

ONENOTE_EXPORTS = {".onepkg", ".one", ".mht", ".mhtml", ".html", ".htm"}


def rebuild_from_onenote(data_dir: Path, source: Path, password: str) -> tuple[Vault, dict]:
    """Caller holds InstanceLock(data_dir). Publish only after a complete import.

    A failure leaves the old pointer and every previous vault unchanged. A fully
    encrypted candidate may remain for diagnosis/retry, but is never auto-opened.
    No user password is recovered, generated, logged or written as plaintext.
    """
    data_dir = Path(data_dir).resolve()
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() not in ONENOTE_EXPORTS:
        raise VaultError("OneNote原本（.onepkg / .one / .mht / .mhtml / .html）を選んでください")
    if len(password) < 12:
        raise VaultError("新しいパスワードは12文字以上にしてください")
    root = data_dir / ("vault-v2-recovered-" + uuid.uuid4().hex)
    if root.resolve().parent != data_dir:
        raise VaultError("復元先が不正です")
    candidate = Vault.create(root, password)
    try:
        report = import_into(candidate, source)
        if report["warnings"]:
            raise VaultError("原本の一部を読み取れないため、切り替えを中止しました。旧保管庫はそのままです。\n" + "\n".join(report["warnings"][:8]))
        if report["imported"] == 0 or len(candidate.heads()) != report["imported"]:
            raise VaultError("原本からノートを復元できなかったため、切り替えていません")
        candidate.set_settings({"onenote_recovery": {"source": str(source),
            "notes": report["imported"], "attachments": report["attachments"]}})
        # Reopen with the new password and authenticate every committed image.
        check = Vault.open(root, password)
        try:
            if check.recovered_index or check.inventory() != candidate.inventory():
                raise VaultError("復元した保管庫の検証に失敗しました")
            for object_id in check.inventory()["blobs"]:
                check.read_blob(object_id)
        finally:
            check.lock()
        atomic_write(data_dir / ACTIVE_VAULT_FILE, json_bytes({"version": 1, "directory": root.name}))
        return candidate, {"rebuilt": report}
    except BaseException:
        candidate.lock()
        raise
