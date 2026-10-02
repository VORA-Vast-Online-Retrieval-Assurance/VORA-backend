# Install the VORA browser from its zip (download it from the repository's
# GitHub Releases page) and point .env at it.
#
#   powershell -ExecutionPolicy Bypass -File install-browser.ps1 "$HOME\Downloads\voraBrowser.zip"
#
# Safe to re-run: the browser folder is replaced and only the
# VORA_BROWSER_BINARY line of .env changes.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Zip
)
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path -LiteralPath $Zip -PathType Leaf)) {
    Write-Error "Zip not found: $Zip"
    exit 1
}

$runtime = Join-Path $PSScriptRoot '.runtime'
$target = Join-Path $runtime 'voraBrowser'
New-Item -ItemType Directory -Force $runtime | Out-Null
if (Test-Path $target) {
    Write-Host 'Replacing the existing .runtime\voraBrowser'
    Remove-Item -Recurse -Force $target
}
Write-Host 'Unpacking the browser (about 540 MB)...'
# Unpack beside the target, then move the folder that holds chrome.exe into place, whatever the zip
# calls it (older releases name it differently).
$staging = Join-Path $runtime '.unpacking'
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
Expand-Archive -LiteralPath $Zip -DestinationPath $staging -Force
$found = Get-ChildItem -Recurse -File -Filter 'chrome.exe' $staging | Select-Object -First 1
if (-not $found) {
    Remove-Item -Recurse -Force $staging
    Write-Error 'The zip does not contain a browser (no chrome.exe).'
    exit 1
}
$top = Get-ChildItem -Directory $staging | Select-Object -First 1
$source = if ($top -and (Get-ChildItem -Directory $staging).Count -eq 1) { $top.FullName } else { $staging }
Move-Item -LiteralPath $source -Destination $target
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
# Files from a downloaded zip carry Windows' "from the internet" mark.
Get-ChildItem -Recurse -File $target | Unblock-File

$exe = Get-ChildItem -Recurse -File -Filter 'chrome.exe' $target | Select-Object -First 1
if (-not $exe) {
    Write-Error 'No chrome.exe found inside voraBrowser.'
    exit 1
}
$path = $exe.FullName -replace '\\', '/'

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example'
}
$lines = @(Get-Content '.env')
if ($lines -match '^VORA_BROWSER_BINARY=') {
    $lines = $lines -replace '^VORA_BROWSER_BINARY=.*', "VORA_BROWSER_BINARY=$path"
} else {
    $lines += "VORA_BROWSER_BINARY=$path"
}
# UTF-8 without a byte-order mark (a BOM would break the first .env setting).
[IO.File]::WriteAllLines((Resolve-Path '.env').Path, [string[]]$lines)

$version = $exe.VersionInfo.ProductVersion
Write-Host ''
Write-Host "Browser installed: $path (version $version)"
Write-Host 'Restart VORA (python app.py) to use it.'
