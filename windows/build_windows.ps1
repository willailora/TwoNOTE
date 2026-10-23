param([string]$Python = 'python', [switch]$InstallDependencies, [switch]$CleanBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousPath = $env:PATH
$previousPythonPath = $env:PYTHONPATH
$previousNoUserSite = $env:PYTHONNOUSERSITE
$pythonExecutable = (& $Python -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the Python interpreter' }
$pythonDirectory = Split-Path -Parent $pythonExecutable
Push-Location $projectRoot
try {
    # Foreign runtimes (e.g. Poppler/ICU on a developer's PATH) must not be
    # collected under Windows system DLL names by PyInstaller.
    $env:PATH = (@($pythonDirectory, (Join-Path $pythonDirectory 'DLLs'),
        (Join-Path $projectRoot '.tools\python-packages\PySide6'),
        (Join-Path $projectRoot '.tools\python-packages\shiboken6'),
        (Join-Path $env:WINDIR 'System32'), $env:WINDIR) -join ';')
    $env:PYTHONNOUSERSITE = '1'
    if ($InstallDependencies) {
        & $pythonExecutable -m pip install --target .tools/python-packages -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    }
    $env:PYTHONPATH = (Join-Path $projectRoot '.tools\python-packages') + ';' + $projectRoot
    $licenseDir = Join-Path $projectRoot '.tools\licenses'
    New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
    foreach ($license in @('gpl-3.0', 'lgpl-3.0')) {
        $target = Join-Path $licenseDir ($license + '.txt')
        if (-not (Test-Path -LiteralPath $target)) {
            Invoke-WebRequest ('https://www.gnu.org/licenses/' + $license + '.txt') -OutFile $target
        }
    }
    & $pythonExecutable -B tools/build_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }
    $packageArguments = @('--noconfirm')
    if ($CleanBuild) { $packageArguments += '--clean' }
    & $pythonExecutable -m PyInstaller @packageArguments windows/TwoNote.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    & $pythonExecutable -B tools/check_windows_dlls.py dist/TwoNote.exe
    if ($LASTEXITCODE -ne 0) { throw 'Packaged DLL import/export verification failed' }
    # Existing shortcuts now launch the same native app; they cannot reopen v1.
    Copy-Item -LiteralPath dist/TwoNote.exe -Destination dist/TwoNoteServer.exe
    Copy-Item -LiteralPath THIRD_PARTY_NOTICES.md -Destination dist/THIRD_PARTY_NOTICES.md
    $packageFiles = @('dist/TwoNote.exe', 'THIRD_PARTY_NOTICES.md', 'README.md',
        'LICENSE.txt', 'LICENSE.ja.txt', 'INSTALL-NOTICE.en.txt', 'INSTALL-NOTICE.ja.txt')
    Compress-Archive -LiteralPath $packageFiles -DestinationPath dist/TwoNote-Windows.zip -Force
    Write-Host 'Built dist\TwoNote.exe (native desktop)'
} finally {
    $env:PATH = $previousPath
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONNOUSERSITE = $previousNoUserSite
    Pop-Location
}
