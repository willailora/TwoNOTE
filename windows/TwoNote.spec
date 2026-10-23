from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
license_files = []
for metadata in (ROOT / '.tools' / 'python-packages').glob('*.dist-info'):
    for item in metadata.rglob('*'):
        if item.is_file() and any(word in item.name.lower() for word in ('license', 'copying', 'notice')):
            license_files.append((str(item), 'licenses/' + metadata.name))
for item in (ROOT / '.tools' / 'licenses').glob('*.txt'):
    license_files.append((str(item), 'licenses/Qt'))
analysis = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT), str(ROOT / ".tools" / "python-packages")],
    binaries=[], datas=license_files + [(str(ROOT / 'THIRD_PARTY_NOTICES.md'), '.')],
    hiddenimports=collect_submodules("aspose.note") + ["qrcode"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['tkinter', 'PySide6.QtWebEngineCore'], noarchive=False,
)
# Widgets only: omit unrelated PDF, SVG, QML and virtual-keyboard plugins and
# their transitive libraries. Japanese IME is provided by qwindows.dll itself.
unneeded = ('qt6pdf', 'qpdf', 'qt6virtualkeyboard', 'qtvirtualkeyboardplugin',
            'qt6qml', 'qt6quick', 'qt6svg', 'qsvg', 'qtuiotouchplugin',
            'icuuc.', 'icuin.', 'icudt', 'api-ms-win-', 'ext-ms-win-')
analysis.binaries = [entry for entry in analysis.binaries
                     if not Path(entry[0]).name.lower().startswith(unneeded)]
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
          name="TwoNote", debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False, icon=str(ROOT / 'build' / 'twonote.ico'))
