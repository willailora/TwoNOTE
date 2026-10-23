"""Inspect only package entry types; never print notebook names or contents."""
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sources = list((root / "onenoteexportdata").glob("*.onepkg"))
if len(sources) != 1:
    raise SystemExit("Expected exactly one original .onepkg; no files changed")
original = sources[0]
if "--validate-import" in sys.argv:
    sys.path.insert(0, str(root))
    from twonote.importing import import_into

    class CountingSink:
        # Exercise the real parser, but keep no note bodies or image copies.
        records = {}

        def add_blob(self, filename, mime, raw):
            return {"id": "count-only", "filename": filename, "mime": mime, "size": len(raw)}

        def save(self, note_id, parents, values):
            return None

    report = import_into(CountingSink(), original)
    print(json.dumps({"imported": report["imported"], "attachments": report["attachments"],
                      "warning_count": len(report["warnings"]),
                      "warning_types": [item.rsplit(":", 1)[-1].strip() for item in report["warnings"]]}, ensure_ascii=True))
    raise SystemExit(1 if report["warnings"] or not report["imported"] else 0)
with tempfile.TemporaryDirectory(prefix="twonote-format-check-") as directory:
    result = subprocess.run(["C:/Windows/System32/expand.exe", "-F:*", str(original), directory],
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
    if result.returncode:
        raise SystemExit("Package expansion failed; no contents printed")
    entries = []
    for i, path in enumerate(sorted(Path(directory).rglob("*"))):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            head = stream.read(16)
        ending = next((token for token in ("onetoc2", "one") if path.name.lower().endswith(token)), "")
        separator = path.name[-len(ending) - 1:-len(ending)] if ending else ""
        separator_kind = "dot" if separator == "." else "middle-dot" if separator == "・" else "other"
        entries.append({"index": i, "suffix": path.suffix if path.suffix.lower() in (".one", ".onetoc2") else "unrecognized",
                        "extension_label": ending, "separator_kind": separator_kind,
                        "bytes": path.stat().st_size, "header_guid": str(uuid.UUID(bytes_le=head)) if len(head) == 16 else None})
    print(json.dumps({"entries": entries}, indent=2, ensure_ascii=True))
