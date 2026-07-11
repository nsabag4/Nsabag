# uninstall.ps1 - Remove the Avision AV210C2 scanner bridge from Windows.
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 [-RemoveDistro]
#
# What it does:
#   1. Stops and unregisters the 'ScannerBridge-AttachAV210C2' scheduled task.
#   2. Detaches and unbinds the scanner from usbipd (usbipd detach/unbind).
#   3. Removes %ProgramData%\ScannerBridge (logs, cached config, attach script).
#   4. Leaves the WSL distro (Ubuntu-24.04) and usbipd-win INSTALLED, unless:
#        -RemoveDistro    also unregisters the WSL distro (DESTROYS everything
#                         inside it, including any scans saved there!)
#      usbipd-win itself is never removed here (other devices may use it);
#      remove it manually with: winget uninstall dorssel.usbipd-win
#
# PowerShell 5.1 compatible. Safe to re-run.

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$DistroName = "Ubuntu-24.04",
    [string]$VidPid = "0638:0a3a",
    [switch]$RemoveDistro
)

$ErrorActionPreference = "Continue"
$env:WSL_UTF8 = "1"

$BridgeDir = Join-Path $env:ProgramData "ScannerBridge"
$TaskName = "ScannerBridge-AttachAV210C2"

function Write-Info {
    param([string]$Message)
    Write-Host ("==> " + $Message) -ForegroundColor Cyan
}

# ------------------------------------------------------------ admin guard
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: run this script as Administrator (usbipd unbind requires it)." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------- 1. scheduled task
Write-Info "Removing scheduled task and stopping the attach loop..."
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "    Task '$TaskName' removed."
    } else {
        Write-Host "    Task '$TaskName' not found (already removed)."
    }
} catch {
    Write-Host ("    WARNING: could not remove task: " + $_.Exception.Message) -ForegroundColor Yellow
}

# Any 'usbipd attach' client spawned by the task keeps the device attached;
# kill lingering attach CLIENTS so unbind succeeds cleanly. Do NOT kill every
# process named usbipd: the usbipd-win server itself is a Windows service
# ('usbipd' / 'USBIP Device Host') hosted by the same usbipd.exe image, and
# killing it would break sharing for every other USB device on the machine.
try {
    $attachClients = Get-CimInstance -ClassName Win32_Process -Filter "Name='usbipd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { ("" + $_.CommandLine) -match '\battach\b' }
    foreach ($proc in @($attachClients)) {
        if ($null -ne $proc) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
} catch {
}

# If the usbipd server service is not running (e.g. a previous cleanup stopped
# it), start it so the detach/unbind below can actually talk to it.
try {
    $usbipdSvc = Get-Service -Name "usbipd" -ErrorAction SilentlyContinue
    if ($null -ne $usbipdSvc -and $usbipdSvc.Status -ne "Running") {
        Start-Service -Name "usbipd" -ErrorAction SilentlyContinue
    }
} catch {
}

# ------------------------------------------------- 2. usbipd detach/unbind
Write-Info ("Detaching and unbinding the scanner (" + $VidPid + ") from usbipd...")
$usbipd = $null
$cmd = Get-Command "usbipd" -ErrorAction SilentlyContinue
if ($null -ne $cmd) {
    $usbipd = $cmd.Source
} else {
    $default = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
    if (Test-Path $default) {
        $usbipd = $default
    }
}

if ($null -eq $usbipd) {
    Write-Host "    usbipd not installed - nothing to unbind."
} else {
    $busid = $null
    $out = & $usbipd list 2>&1
    if ($null -ne $out) {
        foreach ($line in @($out)) {
            $text = "" + $line
            if ($text -match ('^\s*(\d+-[\d\.]+)\s+' + [regex]::Escape($VidPid) + '\s')) {
                $busid = $Matches[1]
            }
        }
    }
    if ($null -eq $busid) {
        # Fall back to the BUSID cached at install time (device may be unplugged).
        $configFile = Join-Path $BridgeDir "config.json"
        if (Test-Path $configFile) {
            try {
                $cfg = Get-Content $configFile -Raw | ConvertFrom-Json
                if ($null -ne $cfg -and $null -ne $cfg.BusId) {
                    $busid = "" + $cfg.BusId
                }
            } catch {
            }
        }
    }
    if ($null -eq $busid) {
        Write-Host "    Scanner not found in 'usbipd list' and no cached BUSID." -ForegroundColor Yellow
        Write-Host "    If it is still bound, plug it in and run: usbipd unbind --busid <BUSID>" -ForegroundColor Yellow
    } else {
        & $usbipd detach --busid $busid 2>&1 | Out-Null
        & $usbipd unbind --busid $busid 2>&1 | Out-Null
        Write-Host ("    Scanner (BUSID " + $busid + ") detached and unbound. Windows now owns the USB device again.")
    }
}

# ------------------------------------------------- 3. ProgramData cleanup
Write-Info "Removing $BridgeDir ..."
if (Test-Path $BridgeDir) {
    Remove-Item -Path $BridgeDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "    Removed."
} else {
    Write-Host "    Not present."
}

# ------------------------------------------------- 4. optional distro removal
if ($RemoveDistro) {
    Write-Info ("Unregistering WSL distro '" + $DistroName + "' (ALL data inside it will be DESTROYED)...")
    $answer = Read-Host ("Type YES to confirm deleting the WSL distro '" + $DistroName + "'")
    if ($answer -ceq "YES") {
        & wsl.exe --unregister $DistroName
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Distro removed."
        } else {
            Write-Host "    wsl --unregister failed (exit $LASTEXITCODE)." -ForegroundColor Yellow
        }
    } else {
        Write-Host "    Skipped (confirmation not given)."
    }
} else {
    Write-Info ("NOTE: the WSL distro '" + $DistroName + "' was left intact (it may contain your scans/other work).")
    Write-Host "    To remove it too, re-run with:  .\uninstall.ps1 -RemoveDistro"
    Write-Host "    usbipd-win was also left installed; remove with: winget uninstall dorssel.usbipd-win"
}

Write-Host ""
Write-Host "Uninstall finished." -ForegroundColor Green
exit 0
