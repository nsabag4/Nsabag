@echo off
rem =====================================================================
rem  One-click installer for the Avision AV210C2 scanner bridge.
rem  Double-click this file and approve the elevation prompt - that's it.
rem  It downloads the latest bridge, extracts it, and runs install.ps1.
rem =====================================================================

rem --- Self-elevate to Administrator -----------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator permissions...
    set "AV210_INSTALLER=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:AV210_INSTALLER -Verb RunAs"
    exit /b
)

echo.
echo  ==============================================================
echo   Avision AV210C2 scanner bridge - one-click installer
echo   Downloading and starting the installation. Keep the scanner
echo   plugged in (24V power + USB). This takes 10-20 minutes.
echo  ==============================================================
echo.

rem Trust note: the archive is fetched over TLS from this project's own
rem GitHub repository - the same trust level as downloading the ZIP by hand.
rem After the branch is merged/tagged, replace the URL with the release tag
rem archive (archive/refs/tags/vX.Y.zip) for a fully pinned artifact.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$zip=Join-Path $env:TEMP 'av210-bridge.zip';" ^
  "$dst=Join-Path $env:TEMP 'av210-bridge';" ^
  "Write-Host 'Downloading the scanner bridge from GitHub...';" ^
  "Invoke-WebRequest 'https://github.com/nsabag4/Nsabag/archive/refs/heads/claude/scanner-compatibility-7pah21.zip' -OutFile $zip -UseBasicParsing;" ^
  "if (Test-Path $dst) { Remove-Item $dst -Recurse -Force };" ^
  "Write-Host 'Extracting...';" ^
  "Expand-Archive -Path $zip -DestinationPath $dst -Force;" ^
  "$installer = Join-Path $dst 'Nsabag-claude-scanner-compatibility-7pah21\windows-bridge\install.ps1';" ^
  "if (-not (Test-Path $installer)) { throw ('install.ps1 not found at ' + $installer) };" ^
  "Set-Location (Split-Path $installer);" ^
  "& $installer"

echo.
if %errorlevel% neq 0 (
    echo  The installer reported an error - scroll up for the red lines,
    echo  photograph or copy them, and send them to Claude for diagnosis.
) else (
    echo  Done! Try scanning now: open your browser at  http://localhost:8090
)
echo.
pause
