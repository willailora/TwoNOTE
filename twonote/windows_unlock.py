"""Opt-in current-user DPAPI protection of a vault key, never the password.

This is convenience under an already signed-in Windows account, NOT Windows
Hello or proof of user presence. Other processes under that account can decrypt
it too. No machine-wide protection flag or authentication UI is used.
"""
import base64
import ctypes
import json
import os
from pathlib import Path

from .vault import Vault, VaultError, aad, atomic_write, digest, json_bytes

KEY_FILE = "windows-unlock.dpapi"
UI_FORBIDDEN = 1


def supported() -> bool:
    return os.name == "nt"


def available(root: Path) -> bool:
    return supported() and (Path(root) / KEY_FILE).is_file()


def _crypt(raw: bytes, entropy: bytes, *, decrypt: bool) -> bytes:
    if not supported():
        raise VaultError("Windowsの自動解除はこの端末では使えません")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    blob_pointer = ctypes.POINTER(Blob)
    crypt32.CryptProtectData.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
    crypt32.CryptUnprotectData.argtypes = [blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    entropy_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
    source = Blob(len(raw), input_buffer)
    extra = Blob(len(entropy), entropy_buffer)
    output = Blob()
    try:
        if decrypt:
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(extra),
                None, None, UI_FORBIDDEN, ctypes.byref(output))
        else:
            ok = crypt32.CryptProtectData(ctypes.byref(source), "TwoNote vault key", ctypes.byref(extra),
                None, None, UI_FORBIDDEN, ctypes.byref(output))
        if not ok:
            raise VaultError(f"Windowsの鍵保護を利用できません（エラー {ctypes.get_last_error()}）。パスワードで開いてください")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(input_buffer, 0, len(raw))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel32.LocalFree(output.data)


def remember(vault: Vault) -> None:
    with vault.mutex:
        payload = json_bytes({"version": 1, "vault_id": vault.vault_id,
            "config_hash": digest(json_bytes(vault.config)),
            "key": base64.b64encode(vault._key()).decode("ascii")})
        protected = _crypt(payload, aad(vault.vault_id, "windows-unlock"), decrypt=False)
        atomic_write(vault.root / KEY_FILE, protected)


def forget(root: Path) -> None:
    (Path(root) / KEY_FILE).unlink(missing_ok=True)


def open_remembered(root: Path) -> Vault:
    root = Path(root)
    config = json.loads((root / "config.json").read_bytes())
    # Validate the public config before using its identifier as DPAPI entropy.
    from .vault import validate_config
    validate_config(config)
    with (root / KEY_FILE).open("rb") as saved:
        protected = saved.read(65537)
    if not protected or len(protected) > 65536:
        raise VaultError("保存済みのWindows鍵が不正です。パスワードで開いてください")
    payload = json.loads(_crypt(protected, aad(config["vault_id"], "windows-unlock"), decrypt=True))
    if (payload.get("version") != 1 or payload.get("vault_id") != config["vault_id"]
            or payload.get("config_hash") != digest(json_bytes(config))):
        raise VaultError("保管庫の設定が変わっています。パスワードで開き直してください")
    key = bytearray(base64.b64decode(payload["key"], validate=True))
    try:
        if len(key) != 32:
            raise VaultError("保存済みのWindows鍵が不正です")
        return Vault(root, config, bytes(key))
    finally:
        key[:] = b"\0" * len(key)
