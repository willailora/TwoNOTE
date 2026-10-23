"""Statically check named imports between bundled PE files, without loading them."""
import ast
import json
import sys
from pathlib import Path

import pefile


def entries(value):
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and isinstance(value[2], str) and value[2] in ("BINARY", "EXTENSION"):
            yield value
        else:
            for child in value:
                yield from entries(child)


def check(path):
    archive = None
    if path.suffix.lower() == '.exe':
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(path))
        files = {Path(name).name.lower(): name for name in archive.toc
                 if Path(name).suffix.lower() in ('.dll', '.pyd')}
    else:
        files = {Path(name).name.lower(): Path(source) for name, source, kind in
                 entries(ast.literal_eval(path.read_text(encoding="utf-8")))}
    parsed = {}
    for name, source in files.items():
        if Path(source).suffix.lower() not in (".dll", ".pyd"):
            continue
        kwargs = {'data': archive.extract(source)} if archive else {'name': str(source)}
        # Qt exports more than pefile's default 8192 named-symbol safety cap.
        pe = pefile.PE(**kwargs, fast_load=True, max_symbol_exports=100000)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                              pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        exports = {symbol.name for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []) if symbol.name}
        parsed[name] = (pe, exports)
    failures = []
    for name, (pe, exports) in parsed.items():
        for dependency in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            target = dependency.dll.decode("ascii").lower()
            if target not in parsed:
                continue  # System/API-set DLLs are outside this package audit.
            for item in dependency.imports:
                if item.name and item.name not in parsed[target][1]:
                    failures.append({"importer": name, "dependency": target,
                                     "symbol": item.name.decode("ascii", errors="replace")})
    for pe, exports in parsed.values():
        pe.close()
    from collections import Counter
    print(json.dumps({"checked_binaries": len(parsed), "missing_count": len(failures),
        "by_dependency": dict(Counter(item['dependency'] for item in failures)),
        "missing_exports_sample": failures[:20]}, indent=2))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1] if len(sys.argv) > 1 else "build/TwoNote/PKG-00.toc")))
