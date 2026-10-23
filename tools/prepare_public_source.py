"""Copy the explicit, reviewed source allowlist into a new independent folder."""
from pathlib import Path
import argparse
import json
from package_release import ROOT, source_files
from audit_public_source import findings, safe_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.absolute()
    if destination.exists(): raise FileExistsError("Use a new destination; existing files are never overwritten")
    if destination.is_relative_to(ROOT / "data") or destination.is_relative_to(ROOT / "onenoteexportdata"):
        raise ValueError("Publication output cannot be a data directory")
    sources = source_files()
    issues = []
    for source in sources:
        name = source.relative_to(ROOT).as_posix()
        if not safe_path(name): issues.append({"path":name, "rule":"forbidden-path"})
        issues.extend(findings(name, source.read_bytes()))
    if issues:
        print(json.dumps({"ok":False, "findings":issues}, ensure_ascii=True, indent=2))
        raise SystemExit(1)
    destination.mkdir(parents=True)
    for source in sources:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stable Git hashes across Windows/Linux; this is a mechanical LF copy.
        target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    print(json.dumps({"ok":True, "copied_source_files":len(sources), "binaries_included":False, "history_copied":False}, indent=2))


if __name__ == "__main__": main()
