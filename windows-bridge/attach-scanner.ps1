# attach-scanner.ps1 - Keep the Avision AV210C2 (USB 0638:0A3A) attached to WSL2.
#
# Designed to run as a logon Scheduled Task (registered by install.ps1), but can
# also be run manually. It:
#   - finds the scanner's BUSID via 'usbipd list' (falls back to the BUSID cached
#     in %ProgramData%\ScannerBridge\config.json when the device is unplugged),
#   - runs 'usbipd attach --wsl <distro> --busid <id> --auto-attach --unplugged'.
#     That command BLOCKS while it babysits the attachment: --auto-attach
#     re-attaches automatically after unplug/replug or device reset, so we simply
#     restart it in a loop if it ever exits.
#   - logs to %ProgramData%\ScannerBridge\attach.log (rotated at ~1 MB).
#
# 'usbipd attach' does not need Administrator once the device is bound (install.ps1
# does the one-time 'usbipd bind' as admin).
#
# PowerShell 5.1 compatible.

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$DistroName = "Ubuntu-24.04",
    [string]$VidPid = "0638:0a3a",
    [int]$RetrySeconds = 20,
    [int]$MaxLogBytes = 1048576
)

$ErrorActionPreference = "Continue"

$BridgeDir = Join-Path $env:ProgramData "ScannerBridge"
$LogFile = Join-Path $BridgeDir "attach.log"
$ConfigFile = Join-Path $BridgeDir "config.json"

if (-not (Test-Path $BridgeDir)) {
    New-Item -ItemType Directory -Path $BridgeDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = $stamp + "  " + $Message
    try {
        if (Test-Path $LogFile) {
            $size = (Get-Item $LogFile).Length
            if ($size -gt $MaxLogBytes) {
                $old = $LogFile + ".1"
                if (Test-Path $old) {
                    Remove-Item $old -Force
                }
                Move-Item $LogFile $old -Force
            }
        }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch {
        # Never let logging kill the attach loop.
    }
    Write-Host $line
}

function Get-UsbipdPath {
    $cmd = Get-Command "usbipd" -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        return $cmd.Source
    }
    $default = Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"
    if (Test-Path $default) {
        return $default
    }
    return $null
}

function Get-ScannerBusId {
    param([string]$Usbipd)
    $out = & $Usbipd list 2>&1
    if ($null -eq $out) {
        return $null
    }
    foreach ($line in @($out)) {
        $text = "" + $line
        if ($text -match ('^\s*(\d+-[\d\.]+)\s+' + [regex]::Escape($VidPid) + '\s')) {
            return $Matches[1]
        }
    }
    return $null
}

function Get-CachedBusId {
    if (Test-Path $ConfigFile) {
        try {
            $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
            if ($null -ne $cfg -and $null -ne $cfg.BusId -and ("" + $cfg.BusId) -ne "") {
                return "" + $cfg.BusId
            }
        } catch {
            Write-Log ("Could not read config.json: " + $_.Exception.Message)
        }
    }
    return $null
}

# ------------------------------------------------------------------- main
Write-Log ("attach-scanner starting (VidPid=" + $VidPid + ", Distro=" + $DistroName + ")")

$usbipd = Get-UsbipdPath
if ($null -eq $usbipd) {
    Write-Log "FATAL: usbipd.exe not found. Run install.ps1 first. Exiting."
    exit 1
}

# Make sure the WSL distro is up before the first attach (attach starts a helper
# process inside WSL; a cold distro can make the first attempt slow/flaky).
$env:WSL_UTF8 = "1"
& wsl.exe -d $DistroName -u root -e true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Log ("WARNING: could not start WSL distro '" + $DistroName + "' (exit " + $LASTEXITCODE + "). Will try to attach anyway.")
}

function Ensure-DistroKeepAlive {
    # WSL terminates the distro (and airsaned with it) shortly after the last
    # client process exits. Pin the distro with a hidden 'sleep infinity'
    # process and make sure airsaned is running. Idempotent: skips if the
    # keep-alive marker process already exists inside the distro.
    & wsl.exe -d $DistroName -u root -e pgrep -f "av210-keepalive" *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }
    Write-Log "Starting WSL keep-alive process (pins the distro so airsaned stays up)."
    Start-Process -FilePath "wsl.exe" -WindowStyle Hidden -ArgumentList @(
        "-d", $DistroName, "-u", "root", "--",
        "bash", "-c", "systemctl start airsaned 2>/dev/null; exec -a av210-keepalive sleep infinity"
    )
}

Ensure-DistroKeepAlive

while ($true) {
    Ensure-DistroKeepAlive
    $busid = Get-ScannerBusId -Usbipd $usbipd
    if ($null -eq $busid) {
        $busid = Get-CachedBusId
        if ($null -ne $busid) {
            Write-Log ("Scanner not currently listed by usbipd (unplugged or powered off). Using cached BUSID " + $busid + " with --unplugged.")
        }
    }
    if ($null -eq $busid) {
        Write-Log ("Scanner " + $VidPid + " not found and no cached BUSID. Plug in / power on the scanner. Retrying in " + $RetrySeconds + "s.")
        Start-Sleep -Seconds $RetrySeconds
        continue
    }

    # Clear any stale attachment first: after a WSL restart usbipd can still
    # report the device as 'Attached' while the VM no longer has it, and a new
    # attach then fails forever. Detaching an unattached device is harmless.
    & $usbipd detach --busid $busid 2>$null | Out-Null

    Write-Log ("Attaching BUSID " + $busid + " to WSL (" + $DistroName + ") with --auto-attach --unplugged. This call blocks while it keeps the device attached.")
    # NOTE: --auto-attach works by keeping this client process running; it is not
    # a fire-and-forget setting. If the process exits (error, wsl shutdown, ...),
    # the loop below restarts it.
    & $usbipd attach --wsl $DistroName --busid $busid --auto-attach --unplugged 2>&1 | ForEach-Object {
        Write-Log ("[usbipd] " + ("" + $_))
    }
    $code = $LASTEXITCODE
    Write-Log ("usbipd attach exited with code " + $code + ". Common causes: device not bound (run install.ps1 as admin again), WSL shut down, or usbipd upgraded. Retrying in " + $RetrySeconds + "s.")
    Start-Sleep -Seconds $RetrySeconds
}
