param([switch]$DebugBuild, [switch]$SkipLint)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.tools'
$jdkDir = Get-ChildItem -LiteralPath $runtimeRoot -Directory | Where-Object { $_.Name -like 'jdk-21*' } | Select-Object -First 1
if (-not $jdkDir) { throw 'Run tools/bootstrap_android.ps1 first' }
$env:JAVA_HOME = $jdkDir.FullName
$env:ANDROID_HOME = Join-Path $runtimeRoot 'android-sdk'
$env:ANDROID_USER_HOME = Join-Path $runtimeRoot 'android-user'
$env:GRADLE_USER_HOME = Join-Path $runtimeRoot 'gradle-cache'
$gradle = Join-Path $runtimeRoot 'gradle-8.9\bin\gradle.bat'
$distDir = Join-Path $projectRoot 'dist'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

if ($DebugBuild) {
    & $gradle --no-daemon --max-workers=1 --no-parallel -p $PSScriptRoot :app:assembleDebug
    if ($LASTEXITCODE -ne 0) { throw 'Android build failed' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'app\build\outputs\apk\debug\app-debug.apk') -Destination (Join-Path $distDir 'TwoNote-debug.apk')
} else {
    $signingDir = Join-Path $runtimeRoot 'signing'
    New-Item -ItemType Directory -Force -Path $signingDir | Out-Null
    $passFile = Join-Path $signingDir 'password.dpapi'
    if (-not (Test-Path -LiteralPath $passFile)) {
        $random = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $generator.GetBytes($random)
        $generator.Dispose()
        $secure = ConvertTo-SecureString ([Convert]::ToBase64String($random)) -AsPlainText -Force
        [IO.File]::WriteAllText($passFile, (ConvertFrom-SecureString $secure))
    }
    $secure = ConvertTo-SecureString ([IO.File]::ReadAllText($passFile))
    $credential = [System.Management.Automation.PSCredential]::new('signing',$secure)
    $env:TWONOTE_STOREPASS = $credential.GetNetworkCredential().Password
    $env:TWONOTE_KEYSTORE = Join-Path $signingDir 'twonote-release.jks'
    try {
        if (-not (Test-Path -LiteralPath $env:TWONOTE_KEYSTORE)) {
            & (Join-Path $env:JAVA_HOME 'bin\keytool.exe') -genkeypair -alias twonote -keystore $env:TWONOTE_KEYSTORE -storepass:env TWONOTE_STOREPASS -keypass:env TWONOTE_STOREPASS -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=TwoNote Local, O=Personal, C=JP'
            if ($LASTEXITCODE -ne 0) { throw 'Signing key creation failed' }
        }
        $tasks = @(':app:assembleRelease')
        if (-not $SkipLint) { $tasks += ':app:lintRelease' }
        & $gradle --no-daemon --max-workers=1 --no-parallel -p $PSScriptRoot @tasks
        if ($LASTEXITCODE -ne 0) { throw 'Android build or lint failed' }
        $apk = Join-Path $PSScriptRoot 'app\build\outputs\apk\release\app-release.apk'
        & (Join-Path $env:ANDROID_HOME 'build-tools\35.0.0\apksigner.bat') verify $apk
        if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed' }
        Copy-Item -LiteralPath $apk -Destination (Join-Path $distDir 'TwoNote.apk')
        $published = Get-Item -LiteralPath (Join-Path $distDir 'TwoNote.apk')
        $apkHash = (Get-FileHash -LiteralPath $published.FullName -Algorithm SHA256).Hash
        Write-Host ('APK: ' + $published.FullName)
        Write-Host ('APK timestamp: ' + $published.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
        Write-Host ('APK SHA256: ' + $apkHash)
    } finally {
        Remove-Item Env:\TWONOTE_STOREPASS -ErrorAction SilentlyContinue
        Remove-Item Env:\TWONOTE_KEYSTORE -ErrorAction SilentlyContinue
    }
}
