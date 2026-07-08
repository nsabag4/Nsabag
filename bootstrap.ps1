# Bootstrap for the Avision AV210C2 scanner bridge.
# Run from any PowerShell window with:
#   irm https://raw.githubusercontent.com/nsabag4/Nsabag/claude/scanner-compatibility-7pah21/bootstrap.ps1 | iex
# Downloads the bridge to %TEMP%, extracts it, and runs install.ps1.
# Self-elevates if not started as Administrator.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Requesting administrator permissions (approve the popup)..."
    $cmd = 'irm https://raw.githubusercontent.com/nsabag4/Nsabag/claude/scanner-compatibility-7pah21/bootstrap.ps1 | iex'
    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-Command', $cmd
    return
}

Write-Host ""
Write-Host "=============================================================="
Write-Host " Avision AV210C2 scanner bridge - bootstrap installer"
Write-Host " Keep the scanner plugged in (24V power + USB)."
Write-Host " First install takes 10-20 minutes."
Write-Host "=============================================================="
Write-Host ""

$zip = Join-Path $env:TEMP 'av210-bridge.zip'
$dst = Join-Path $env:TEMP 'av210-bridge'

Write-Host "[1/3] Downloading the scanner bridge from GitHub..."
Invoke-WebRequest 'https://github.com/nsabag4/Nsabag/archive/refs/heads/claude/scanner-compatibility-7pah21.zip' -OutFile $zip -UseBasicParsing

Write-Host "[2/3] Extracting..."
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $dst -Force

Write-Host "[3/3] Starting the installer..."
$installer = Get-ChildItem -Path $dst -Recurse -Filter install.ps1 | Select-Object -First 1
if (-not $installer) { throw 'install.ps1 not found in the downloaded archive' }
Set-Location $installer.DirectoryName
& $installer.FullName
