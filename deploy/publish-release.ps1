# Build and upload GitHub release assets for MidiPult.
#
#   powershell -ExecutionPolicy Bypass -File deploy/publish-release.ps1 -Version 0.5.66
#
# Requires a release with tag v<version> to exist. Uploads:
#   midipult-<version>.tar.gz
#   midipult-<version>.tar.gz.sha256
#
# Token is taken from GITHUB_TOKEN/GH_TOKEN or from the Windows git credential
# manager (the same account that can push). Existing assets with the same names
# are replaced, so the command is safe to re-run.

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Token = '',
    [switch]$NoBuild,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Die([string]$message) {
    Write-Host "[x] $message" -ForegroundColor Red
    exit 1
}

$versionFile = Join-Path $root 'version.json'
if (-not (Test-Path $versionFile)) { Die "нет $versionFile" }
$actual = (Get-Content $versionFile -Raw | ConvertFrom-Json).version
if ($actual -ne $Version) { Die "version.json = $actual, запрошена $Version" }

if (-not $NoBuild) {
    Write-Host "==> собираю архив" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot 'build-release.ps1') -Version $Version | Out-Host
    if ($LASTEXITCODE -ne 0) { Die 'build-release.ps1 завершился с ошибкой' }
}

$archive = Join-Path $root ".release\midipult-$Version.tar.gz"
$shaFile = "$archive.sha256"
foreach ($path in @($archive, $shaFile)) {
    if (-not (Test-Path $path)) { Die "нет $path — сначала соберите архив" }
}

if (-not $Token) { $Token = $env:GITHUB_TOKEN }
if (-not $Token) { $Token = $env:GH_TOKEN }
if (-not $Token) {
    try {
        $lines = ("protocol=https`nhost=github.com`n`n" | git credential fill) 2>$null
        $Token = ($lines | Where-Object { $_ -match '^password=' } |
            ForEach-Object { ($_ -split '=', 2)[1] })
    } catch { }
}
if (-not $Token) {
    Die 'нет GitHub-токена: задайте GITHUB_TOKEN или войдите через git credential manager'
}

$remote = (git -C $root config --get remote.origin.url) 2>$null
if ($remote -notmatch 'github\.com[:/]([^/]+)/([^/\.]+)') {
    Die "не удалось определить owner/repo из remote: $remote"
}
$owner = $matches[1]
$repo = $matches[2]
$tag = "v$Version"
$headers = @{ Authorization = "token $Token"; 'User-Agent' = 'MidiPult-publish' }

try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo/releases/tags/$tag" `
        -Headers $headers -TimeoutSec 30
} catch {
    Die "релиз $tag не найден: создайте его на GitHub и повторите"
}
if (-not $release -or -not $release.id) { Die "релиз $tag не найден" }

foreach ($path in @($archive, $shaFile)) {
    $name = Split-Path $path -Leaf
    $size = (Get-Item $path).Length
    $existing = @($release.assets | Where-Object { $_.name -eq $name })
    if ($DryRun) {
        Write-Host "==> [dry-run] $name ($size байт) -> $tag"
        continue
    }
    foreach ($asset in $existing) {
        Write-Host "==> заменяю $name" -ForegroundColor Yellow
        Invoke-RestMethod -Method Delete -Uri $asset.url -Headers $headers -TimeoutSec 30 | Out-Null
    }
    $contentType = if ($name -like '*.sha256') { 'text/plain' } else { 'application/gzip' }
    $response = Join-Path $env:TEMP ("mypult-upload-" + [guid]::NewGuid().ToString('N') + ".json")
    $curlArgs = @(
        '-sS', '-o', $response, '-w', '%{http_code}', '-X', 'POST',
        '-H', "Authorization: token $Token",
        '-H', "Content-Type: $contentType",
        '--data-binary', "@$path",
        "https://uploads.github.com/repos/$owner/$repo/releases/$($release.id)/assets?name=$name"
    )
    $code = & curl.exe @curlArgs
    if ($code -ne '201') {
        $detail = if (Test-Path $response) { Get-Content $response -Raw } else { '' }
        Die "загрузка $name -> HTTP $code $detail"
    }
    Remove-Item $response -Force -ErrorAction SilentlyContinue
    Write-Host "==> загружено $name ($size байт)" -ForegroundColor Green
}

if (-not $DryRun) {
    $check = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo/releases/$($release.id)" `
        -Headers $headers -TimeoutSec 30
    foreach ($path in @($archive, $shaFile)) {
        $name = Split-Path $path -Leaf
        if (-not (@($check.assets) | Where-Object { $_.name -eq $name })) {
            Die "после загрузки в релизе нет $name"
        }
    }
    Write-Host "==> ${tag}: $($check.assets.Count) файлов, архив и SHA на месте" -ForegroundColor Green
}
