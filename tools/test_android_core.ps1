$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.tools'
$jdk = Get-ChildItem -LiteralPath $runtimeRoot -Directory | Where-Object { $_.Name -like 'jdk-21*' } | Select-Object -First 1
$jsonJar = Join-Path $runtimeRoot 'json-20240303.jar'
if (-not (Test-Path -LiteralPath $jsonJar)) {
    Invoke-WebRequest 'https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar' -OutFile $jsonJar
}
$classes = Join-Path $projectRoot 'build\java-tests'
New-Item -ItemType Directory -Force -Path $classes | Out-Null
$coreSources = (Get-ChildItem -LiteralPath (Join-Path $projectRoot 'android\app\src\main\java\com\twonote\core') -Filter '*.java' -File).FullName
$probeSources = (Get-ChildItem -LiteralPath (Join-Path $projectRoot 'tests\java') -Filter '*.java' -File).FullName
& (Join-Path $jdk.FullName 'bin\javac.exe') --release 17 -encoding UTF-8 -cp $jsonJar -d $classes @coreSources (Join-Path $projectRoot 'android\app\src\main\java\com\twonote\android\BiometricCipher.java') @probeSources
if ($LASTEXITCODE -ne 0) { throw 'Java contract test compile failed' }
$env:TWONOTE_JAVA = Join-Path $jdk.FullName 'bin\java.exe'
$env:TWONOTE_JAVA_CP = $classes + ';' + $jsonJar
Push-Location $projectRoot
try {
    python -B -m unittest discover -s tests -p test_android_core.py -v
    if ($LASTEXITCODE -ne 0) { throw 'Cross-platform tests failed' }
    $probeRoot = Join-Path $projectRoot ('build\android-probes-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $probeRoot | Out-Null
    & $env:TWONOTE_JAVA '-XX:ActiveProcessorCount=1' '-Xmx256m' -cp $env:TWONOTE_JAVA_CP ReviewRegressionProbe (Join-Path $probeRoot 'recovery')
    if ($LASTEXITCODE -ne 0) { throw 'Recovery regressions failed' }
    & $env:TWONOTE_JAVA '-XX:ActiveProcessorCount=1' '-Xmx128m' -cp $env:TWONOTE_JAVA_CP NavigationProbe (Join-Path $probeRoot 'navigation')
    if ($LASTEXITCODE -ne 0) { throw 'Tree regressions failed' }
    & $env:TWONOTE_JAVA '-XX:ActiveProcessorCount=1' '-Xmx128m' -cp $env:TWONOTE_JAVA_CP com.twonote.android.BiometricCipherProbe
    if ($LASTEXITCODE -ne 0) { throw 'Biometric cipher regressions failed' }
    & $env:TWONOTE_JAVA '-XX:ActiveProcessorCount=1' '-Xmx128m' -cp $env:TWONOTE_JAVA_CP PhotoAttachmentProbe (Join-Path $probeRoot 'photos')
    if ($LASTEXITCODE -ne 0) { throw 'Photo attachment regressions failed' }
    & $env:TWONOTE_JAVA '-XX:ActiveProcessorCount=1' '-Xmx128m' -cp $env:TWONOTE_JAVA_CP BlockDraftProbe (Join-Path $probeRoot 'block-drafts')
    if ($LASTEXITCODE -ne 0) { throw 'Block form draft regressions failed' }
} finally {
    Pop-Location
    Remove-Item Env:\TWONOTE_JAVA -ErrorAction SilentlyContinue
    Remove-Item Env:\TWONOTE_JAVA_CP -ErrorAction SilentlyContinue
}
