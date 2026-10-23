import sys
from pathlib import Path

packages = Path(__file__).resolve().parent / ".tools" / "python-packages"
if packages.is_dir():
    sys.path.insert(0, str(packages))

def main():
    try:
        from twonote.desktop import main as desktop_main
        return desktop_main()
    except Exception as exc:
        # Test mode must remain headless even when Qt itself cannot import.
        # Letting this exception reach a windowed PyInstaller bootloader would
        # show a native traceback dialog before Qt's offscreen setup can run.
        if "--self-test-report" not in sys.argv:
            raise
        import json
        import traceback
        index = sys.argv.index("--self-test-report")
        report = Path(sys.argv[index + 1]).resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"status": "failed", "stage": "bootstrap",
            "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}, ensure_ascii=False), encoding="utf-8")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
