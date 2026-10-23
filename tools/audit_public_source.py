"""Offline audit of the source-only publication checkout. Never prints matched secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

MANIFEST = "PUBLIC_SOURCE_MANIFEST.json"
ROOT_NAMES = {".gitignore", ".gitattributes", "README.md", "PUBLICATION.md", MANIFEST, "THIRD_PARTY_NOTICES.md", "LICENSE", "LICENSE.txt", "LICENSE.ja.txt", "INSTALL-NOTICE.en.txt", "INSTALL-NOTICE.ja.txt", "desktop.py", "importer.py", "server.py", "tls.py", "requirements.txt", "requirements-onenote.txt", "requirements-server.txt"}
CONFIG_NAMES = {"android/README.md", "android/build_android.ps1", "android/build.gradle", "android/settings.gradle", "android/gradle.properties", "android/app/build.gradle"}
SOURCE_TREES = {"twonote": {".py"}, "windows": {".py", ".ps1", ".spec", ".md"}, "tools": {".py", ".ps1"}, "tests": {".py", ".java"}, "docs": {".md"}, "android/app/src": {".java", ".xml"}}
EXCLUDED_COMPONENTS = {".tools", "build", "dist", "data", "onenoteexportdata", "archive", "web", "__pycache__", ".gradle", ".aws", ".ssh", "attachments", "output"}
FORBIDDEN_SUFFIXES = {".one", ".onepkg", ".onetoc", ".onetoc2", ".mht", ".mhtml", ".twonote", ".enc", ".dpapi", ".key", ".pem", ".crt", ".pub", ".p12", ".pfx", ".jks", ".keystore", ".sqlite", ".sqlite3", ".db", ".log", ".exe", ".dll", ".apk", ".zip", ".png", ".jpg", ".jpeg", ".pdf", ".xps", ".pyc"}
RULES = {
    "private-key": re.compile(r"^-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", re.M),
    "ssh-key": re.compile(r"\bssh-(?:rsa|ed25519) [A-Za-z0-9+/]{40,}"),
    "credential-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})"),
    "home-path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>]+|/(?:Users|home)/[^/\s\"'<>]+)", re.I),
    "url-credentials": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I),
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
LITERAL_SECRET = re.compile(r"\b(?:password|passwd|token|secret|api_key|apikey|client_secret)[ \t]*=[ \t]*['\"]([^'\"\r\n]{8,})['\"]", re.I)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(name: str) -> bool:
    parts = Path(name).parts
    allowed = name in ROOT_NAMES or name in CONFIG_NAMES or any(name.startswith(tree + "/") and Path(name).suffix.lower() in extensions for tree, extensions in SOURCE_TREES.items())
    return (allowed and not Path(name).is_absolute() and ".." not in parts
            and not any(p.lower() in EXCLUDED_COMPONENTS or p.lower().startswith("vault-v2") for p in parts)
            and Path(name).suffix.lower() not in FORBIDDEN_SUFFIXES
            and Path(name).name.lower() not in {"local.properties", ".env", "active-vault.json", "desktop-preferences.json"}
            and not Path(name).name.lower().startswith(".env."))


def findings(name: str, data: bytes) -> list[dict]:
    result = []
    if len(data) > 2 * 1024 * 1024 or b"\0" in data:
        return [{"path": name, "rule": "binary-or-oversize"}]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        return [{"path": name, "rule": "not-utf8"}]
    for rule, pattern in RULES.items():
        for match in pattern.finditer(text):
            result.append({"path": name, "line": text.count("\n", 0, match.start()) + 1, "rule": rule})
    for match in EMAIL.finditer(text):
        domain = match[1].lower()
        if domain not in {"example.com", "example.net", "example.org"} and not domain.endswith((".invalid", ".test", ".example", ".localhost")):
            result.append({"path": name, "line": text.count("\n", 0, match.start()) + 1, "rule": "non-example-email"})
    fixture = name.startswith("tests/") or name.startswith("android/app/src/test/")
    for match in LITERAL_SECRET.finditer(text):
        if not (fixture and any(word in match[1].lower() for word in ("test", "synthetic", "invalid", "fixture", "nonce", "テスト"))):
            result.append({"path": name, "line": text.count("\n", 0, match.start()) + 1, "rule": "literal-credential"})
    local_user = os.environ.get("USERNAME", "")
    if len(local_user) >= 5 and local_user.lower() not in {"administrator", "default", "public", "system", "runner", "github", "codex"}:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(local_user) + r"(?![A-Za-z0-9_])", text, re.I):
            result.append({"path": name, "rule": "local-user-name"})
    return result


def public_files(root: Path) -> list[Path]:
    paths = []
    def visit(folder):
        for path in sorted(folder.iterdir()):
            if path == root / ".git":
                if not path.is_dir() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise ValueError("Unexpected Git directory")
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("A publication path is a link: " + path.relative_to(root).as_posix())
            if path.is_dir():
                if path.name.lower() in EXCLUDED_COMPONENTS or path.name.lower().startswith("vault-v2"):
                    raise ValueError("Private/generated directory in publication folder: " + path.relative_to(root).as_posix())
                visit(path)
            elif path.is_file(): paths.append(path)
            else: raise ValueError("Unsupported publication entry")
    visit(root)
    return paths


def inspect(root: Path, *, refresh=False) -> dict:
    root = root.resolve()
    if not (root / "PUBLICATION.md").is_file():
        raise ValueError("Run this audit only in the source-only publication folder")
    paths = public_files(root)
    issues = []
    entries = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        if not safe_path(name):
            issues.append({"path": name, "rule": "forbidden-path"})
            continue  # Do not read an accidentally copied private file.
        data = path.read_bytes()
        issues.extend(findings(name, data))
        if name != MANIFEST:
            entries.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    manifest_path = root / MANIFEST
    if not refresh:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("files") != entries: issues.append({"rule": "manifest-mismatch"})
    if issues:
        return {"ok": False, "files": len(paths), "findings": issues}
    if refresh:
        # Generated metadata contains relative source paths and hashes only.
        manifest_path.write_text(json.dumps({"format":1, "kind":"source-only", "files":entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"ok": True, "source_files": len(entries), "manifest": MANIFEST, "findings": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--refresh-manifest", action="store_true", help="Review file changes before deliberately refreshing the inventory")
    args = parser.parse_args()
    result = inspect(args.root, refresh=args.refresh_manifest)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__": main()
