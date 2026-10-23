$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.tools'
$jdk = Get-ChildItem -LiteralPath $runtimeRoot -Directory | Where-Object { $_.Name -like 'jdk-21*' } | Select-Object -First 1
$env:JAVA_HOME = $jdk.FullName
$env:ANDROID_HOME = Join-Path $runtimeRoot 'android-sdk'
$env:ANDROID_USER_HOME = Join-Path $runtimeRoot 'android-user'
$env:GRADLE_USER_HOME = Join-Path $runtimeRoot 'gradle-cache'
& (Join-Path $runtimeRoot 'gradle-8.9\bin\gradle.bat') --no-daemon -p (Join-Path $projectRoot 'android') :app:testDebugUnitTest
if ($LASTEXITCODE -ne 0) { throw 'Android lifecycle tests failed' }
