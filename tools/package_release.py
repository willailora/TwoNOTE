"""Allowlisted source + EXE + APK bundle; never walk note/key/cache directories."""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".gitignore", "README.md", "THIRD_PARTY_NOTICES.md", "desktop.py", "importer.py",
    "server.py", "tls.py", "requirements.txt", "requirements-onenote.txt", "requirements-server.txt",
    "android/README.md", "android/build_android.ps1", "android/build.gradle",
    "android/settings.gradle", "android/gradle.properties", "android/app/build.gradle",
)
SOURCE_TREES = {
    "twonote": {".py"}, "windows": {".py", ".ps1", ".spec", ".md"},
    "tools": {".py", ".ps1"}, "tests": {".py", ".java"}, "docs": {".md"},
    "android/app/src": {".java", ".xml"},
}
BINARIES = ("dist/TwoNote.exe", "dist/TwoNote.apk")
EXCLUDED_DIRS = {
    "data", "onenoteexportdata", ".tools", "build", "archive", ".gradle", ".git",
    "__pycache__", "attachments", "vault-v2", "output",
}
FORBIDDEN_SUFFIXES = {
    ".one", ".onepkg", ".onetoc", ".onetoc2", ".mht", ".mhtml", ".pdf", ".xps",
    ".twonote", ".enc", ".dpapi", ".jks", ".keystore", ".p12", ".pfx", ".pem",
    ".key", ".db", ".sqlite", ".sqlite3", ".log", ".pyc",
}


def safe_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return (not path.is_absolute() and ".." not in path.parts
            and not any(part.lower() in EXCLUDED_DIRS or part.lower().startswith("vault-v2-recovered-") for part in path.parts)
            and path.suffix.lower() not in FORBIDDEN_SUFFIXES
            and path.name.lower() not in {"local.properties", ".env", "active-vault.json", "desktop-preferences.json"})


def digest_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def source_files() -> list[Path]:
    chosen = {ROOT / name for name in ROOT_FILES}
    for folder, extensions in SOURCE_TREES.items():
        base = ROOT / folder
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if not safe_name(path.relative_to(ROOT).as_posix()):
                raise RuntimeError("Unexpected file inside a source tree: " + str(path.relative_to(ROOT)))
            if not path.resolve().is_relative_to(base.resolve()):
                raise RuntimeError("Source link points outside its allowed tree")
            chosen.add(path)
    for path in chosen:
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise RuntimeError("Missing or redirected source file: " + str(path.relative_to(ROOT)))
        if not safe_name(path.relative_to(ROOT).as_posix()):
            raise RuntimeError("Disallowed source path")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("Unexpectedly large source file")
        text = path.read_text(encoding="utf-8-sig")
        if "\0" in text:
            raise RuntimeError("Binary data masquerading as source")
        # References to key formats in test code are fine; actual PEM blocks are not.
        if any(line.startswith("-----BEGIN ") and "PRIVATE KEY-----" in line for line in text.splitlines()):
            raise RuntimeError("Private key block in source")
    return sorted(chosen)


def audit_binaries() -> None:
    # Read package directories only. Never execute the EXE/APK or decrypt a vault.
    from PyInstaller.archive.readers import CArchiveReader
    exe = CArchiveReader(str(ROOT / BINARIES[0]))
    with zipfile.ZipFile(ROOT / BINARIES[1]) as apk:
        names = list(exe.toc) + apk.namelist()
        # AGP's public version metadata has a literal "build" path component;
        # it is not a workspace build/cache directory or user data.
        metadata_name = "META-INF/com/android/build/gradle/app-metadata.properties"
        if metadata_name in apk.namelist():
            for line in apk.read(metadata_name).decode("utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    key, separator, value = line.partition("=")
                    if not separator or key not in {"appMetadataVersion", "androidGradlePluginVersion"}:
                        raise RuntimeError("Unexpected APK build metadata")
    bad = [name for name in names if name != metadata_name and not safe_name(name)]
    if bad:
        raise RuntimeError("Disallowed file embedded in an executable: " + repr(bad))


def main():
    sources = source_files()
    audit_binaries()
    paths = sources + [ROOT / name for name in BINARIES]
    for path in paths:
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise RuntimeError("Missing or redirected package file")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output = ROOT / "dist" / f"TwoNote-source-exe-apk-{stamp}.zip"
    partial = output.with_suffix(".zip.incomplete")
    if output.exists() or partial.exists():
        raise FileExistsError(output)
    prefix = "TwoNote/"
    manifest = []
    for path in paths:
        with path.open("rb") as stream:
            digest = digest_stream(stream)
        manifest.append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    notes = (
        "TwoNote: source + Windows EXE + Android APK\n\n"
        "Windows: dist/TwoNote.exe\nAndroid: dist/TwoNote.apk\n"
        "Source, build instructions, tests and third-party notices are included.\n"
        "OneNote originals/exports, imported notes, attachments, vaults, backups,\n"
        "passwords, authentication keys, signing keys, SDKs and build caches are NOT included.\n"
        "Test code contains synthetic fixture strings only, not real notes.\n\n"
        "The Android signing key stays outside this archive. A build signed with\n"
        "a different key cannot update the existing release installation.\n"
        "See README.md, windows/README.md and android/README.md for build/use instructions.\n"
        "PACKAGE-CONTENTS.json records the included files and their SHA256 hashes.\n"
    )
    with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in paths:
            archive.write(path, prefix + path.relative_to(ROOT).as_posix())
        archive.writestr(prefix + "PACKAGE-NOTES.txt", notes)
        archive.writestr(prefix + "PACKAGE-CONTENTS.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    expected = {prefix + item["path"] for item in manifest} | {prefix + "PACKAGE-NOTES.txt", prefix + "PACKAGE-CONTENTS.json"}
    with zipfile.ZipFile(partial) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected or not all(safe_name(n) for n in names):
            raise RuntimeError("ZIP contents differ from the allowlist")
        for item in manifest:
            with archive.open(prefix + item["path"]) as stream:
                if digest_stream(stream) != item["sha256"]:
                    raise RuntimeError("ZIP content changed during packaging: " + item["path"])
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC check failed")
    partial.rename(output)
    with output.open("rb") as stream:
        package_hash = digest_stream(stream)
    print(json.dumps({"zip": str(output), "bytes": output.stat().st_size, "source_files": len(sources),
        "binaries": list(BINARIES), "zip_entries": len(expected), "sha256": package_hash,
        "excluded_note_and_key_files": True, "contents_and_crc_verified": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
