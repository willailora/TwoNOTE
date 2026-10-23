param([switch]$AcceptSdkLicense)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.tools'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

function Download-Archive([string]$Url, [string]$Target, [string]$Checksum = '') {
    if (-not (Test-Path -LiteralPath $Target)) {
        Invoke-WebRequest -Uri $Url -OutFile ($Target + '.download') -TimeoutSec 300
        Move-Item -LiteralPath ($Target + '.download') -Destination $Target
    }
    if ($Checksum -and (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash -ne $Checksum) {
        throw "Archive checksum failed: $Target"
    }
}

$jdkDir = Get-ChildItem -LiteralPath $runtimeRoot -Directory | Where-Object { $_.Name -like 'jdk-21*' } | Select-Object -First 1
if (-not $jdkDir) {
    $release = Invoke-RestMethod 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
    $package = $release[0].binary.package
    Download-Archive $package.link (Join-Path $runtimeRoot 'jdk21.zip') $package.checksum
    Expand-Archive -LiteralPath (Join-Path $runtimeRoot 'jdk21.zip') -DestinationPath $runtimeRoot
    $jdkDir = Get-ChildItem -LiteralPath $runtimeRoot -Directory | Where-Object { $_.Name -like 'jdk-21*' } | Select-Object -First 1
}
$env:JAVA_HOME = $jdkDir.FullName
$env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $env:PATH
$env:GRADLE_USER_HOME = Join-Path $runtimeRoot 'gradle-cache'
$env:ANDROID_USER_HOME = Join-Path $runtimeRoot 'android-user'
$env:ANDROID_HOME = Join-Path $runtimeRoot 'android-sdk'

$gradlePath = Join-Path $runtimeRoot 'gradle-8.9\bin\gradle.bat'
if (-not (Test-Path -LiteralPath $gradlePath)) {
    $checksumResponse = (Invoke-WebRequest 'https://services.gradle.org/distributions/gradle-8.9-bin.zip.sha256').Content
    $checksum = if ($checksumResponse -is [byte[]]) { [Text.Encoding]::ASCII.GetString($checksumResponse).Trim() } else { $checksumResponse.Trim() }
    Download-Archive 'https://services.gradle.org/distributions/gradle-8.9-bin.zip' (Join-Path $runtimeRoot 'gradle89.zip') $checksum
    Expand-Archive -LiteralPath (Join-Path $runtimeRoot 'gradle89.zip') -DestinationPath $runtimeRoot
}
$sdkManager = Join-Path $env:ANDROID_HOME 'cmdline-tools\cmdline-tools\bin\sdkmanager.bat'
if (-not (Test-Path -LiteralPath $sdkManager)) {
    Download-Archive 'https://dl.google.com/android/repository/commandlinetools-win-13114758_latest.zip' (Join-Path $runtimeRoot 'android-commandline.zip')
    Expand-Archive -LiteralPath (Join-Path $runtimeRoot 'android-commandline.zip') -DestinationPath (Join-Path $env:ANDROID_HOME 'cmdline-tools')
}
if ($AcceptSdkLicense) {
    1..20 | ForEach-Object { 'y' } | & $sdkManager "--sdk_root=$env:ANDROID_HOME" --licenses
    if ($LASTEXITCODE -ne 0) { throw 'Android SDK license step failed' }
    & $sdkManager "--sdk_root=$env:ANDROID_HOME" 'platforms;android-35' 'build-tools;35.0.0' 'platform-tools'
    if ($LASTEXITCODE -ne 0) { throw 'Android SDK installation failed' }
}
Write-Output "JDK: $env:JAVA_HOME"
Write-Output "SDK: $env:ANDROID_HOME"
Write-Output "Gradle: $gradlePath"
