# install.ps1 - One-time setup: Avision AV210C2 scanner bridge (Windows -> WSL2 -> SANE -> eSCL)
#
# Run from an elevated (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# What it does:
#   1. Checks Windows version and virtualization support.
#   2. Installs WSL2 + Ubuntu-24.04 if missing (may require a reboot; the script
#      then prints resume instructions and exits - just run it again after reboot).
#   3. Installs usbipd-win via winget.
#   4. Binds (shares) the scanner USB device 0638:0A3A with usbipd.
#   5. Copies setup-wsl.sh into the distro (CRLF-safe, via base64) and runs it:
#      SANE + avision backend + AirSane eSCL server (+ scanservjs web UI).
#   6. Attaches the scanner to WSL now and registers a logon Scheduled Task
#      (attach-scanner.ps1) so it re-attaches automatically after every reboot.
#   7. Verifies with 'scanimage -L' and prints the scan URLs + NAPS2 instructions.
#
# PowerShell 5.1 compatible. Idempotent - safe to re-run at any point.

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$DistroName = "Ubuntu-24.04",
    [string]$VidPid = "0638:0a3a",
    [switch]$SkipScanservjs
)

$ErrorActionPreference = "Continue"
Set-StrictMode -Off

# Make wsl.exe emit UTF-8 instead of UTF-16 so PowerShell can parse its output.
$env:WSL_UTF8 = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeDir = Join-Path $env:ProgramData "ScannerBridge"
$TaskName = "ScannerBridge-AttachAV210C2"
$script:UsbipdExe = $null

# ----------------------------------------------------------------- messages
function Write-Step {
    param([string]$English, [string]$Hebrew)
    Write-Host ""
    Write-Host ("==> " + $English) -ForegroundColor Cyan
    if ($Hebrew -ne "") {
        Write-Host ("    " + $Hebrew) -ForegroundColor Cyan
    }
}

function Write-Ok {
    param([string]$English, [string]$Hebrew)
    Write-Host ("    OK: " + $English) -ForegroundColor Green
    if ($Hebrew -ne "") {
        Write-Host ("        " + $Hebrew) -ForegroundColor Green
    }
}

function Write-Warn2 {
    param([string]$English, [string]$Hebrew)
    Write-Host ("    WARNING: " + $English) -ForegroundColor Yellow
    if ($Hebrew -ne "") {
        Write-Host ("             " + $Hebrew) -ForegroundColor Yellow
    }
}

function Fail-Step {
    param([string]$English, [string]$Hebrew, [string]$Detail)
    Write-Host ""
    Write-Host ("ERROR: " + $English) -ForegroundColor Red
    if ($Hebrew -ne "") {
        Write-Host ("שגיאה: " + $Hebrew) -ForegroundColor Red
    }
    if ($Detail -ne "" -and $null -ne $Detail) {
        Write-Host ("Detail: " + $Detail) -ForegroundColor Red
    }
    exit 1
}

# ---------------------------------------------------------------- helpers
function Invoke-Wsl {
    # Runs wsl.exe with the given arguments, returns output lines (null-stripped).
    param([string[]]$Arguments)
    $out = & wsl.exe @Arguments 2>&1
    $script:WslExit = $LASTEXITCODE
    $lines = @()
    if ($null -ne $out) {
        foreach ($line in @($out)) {
            $text = ("" + $line) -replace "`0", ""
            $lines += $text.TrimEnd()
        }
    }
    return $lines
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

function Get-ScannerLine {
    # Returns the 'usbipd list' line for our VID:PID, or $null.
    $out = & $script:UsbipdExe list 2>&1
    if ($null -eq $out) {
        return $null
    }
    foreach ($line in @($out)) {
        $text = "" + $line
        if ($text -match ('^\s*(\d+-[\d\.]+)\s+' + [regex]::Escape($VidPid) + '\s')) {
            return $text
        }
    }
    return $null
}

function Get-ScannerBusId {
    $line = Get-ScannerLine
    if ($null -eq $line) {
        return $null
    }
    if ($line -match '^\s*(\d+-[\d\.]+)\s') {
        return $Matches[1]
    }
    return $null
}

# =========================================================================
Write-Host "=========================================================" -ForegroundColor White
Write-Host " Avision AV210C2 scanner bridge installer (WSL2 + SANE) " -ForegroundColor White
Write-Host " מתקין גשר סורק Avision AV210C2 (WSL2 + SANE)           " -ForegroundColor White
Write-Host "=========================================================" -ForegroundColor White

# ------------------------------------------------------------ admin guard
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail-Step "This script must run as Administrator. Right-click PowerShell, choose 'Run as administrator', then run install.ps1 again." `
              "יש להריץ סקריפט זה כמנהל מערכת. לחצו קליק ימני על PowerShell, בחרו 'הפעל כמנהל', והריצו שוב את install.ps1." ""
}

# ------------------------------------------- Step 1: Windows version + virt
Write-Step "Step 1/7: Checking Windows version and virtualization support" `
           "שלב 1/7: בודק גרסת Windows ותמיכה בווירטואליזציה"
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -lt 19041) {
        Fail-Step ("Windows build " + $build + " is too old. WSL2 ('wsl --install') requires Windows 10 2004 (build 19041) or newer / Windows 11. Update Windows first.") `
                  ("גרסת Windows (build " + $build + ") ישנה מדי. נדרש Windows 10 2004 ומעלה או Windows 11. עדכנו את Windows ונסו שוב.") ""
    }
    Write-Ok ("Windows build " + $build + " (" + $os.Caption + ")") ""

    $virtOk = $false
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    if ($cs.HypervisorPresent) {
        $virtOk = $true
    }
    if (-not $virtOk) {
        $cpu = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1
        if ($null -ne $cpu -and $cpu.VirtualizationFirmwareEnabled) {
            $virtOk = $true
        }
    }
    if ($virtOk) {
        Write-Ok "Virtualization is available" "וירטואליזציה זמינה"
    } else {
        Write-Warn2 "Could not confirm virtualization (VT-x/AMD-V). If WSL fails to start, enable virtualization in the BIOS/UEFI." `
                    "לא ניתן לאמת שווירטואליזציה מופעלת. אם WSL לא עולה - הפעילו וירטואליזציה ב-BIOS/UEFI."
    }
} catch {
    Fail-Step "Failed to query Windows version/virtualization." "בדיקת גרסת Windows נכשלה." $_.Exception.Message
}

# ------------------------------------------------- Step 2: WSL + Ubuntu-24.04
Write-Step ("Step 2/7: Checking WSL2 and the " + $DistroName + " distribution") `
           ("שלב 2/7: בודק התקנת WSL2 והפצת " + $DistroName)
try {
    $wslCmd = Get-Command "wsl.exe" -ErrorAction SilentlyContinue
    if ($null -eq $wslCmd) {
        Fail-Step "wsl.exe not found. Your Windows build should include it (19041+). Install 'Windows Subsystem for Linux' from the Microsoft Store, reboot, and re-run install.ps1." `
                  "הקובץ wsl.exe לא נמצא. התקינו את 'Windows Subsystem for Linux' מחנות Microsoft, הפעילו מחדש את המחשב והריצו שוב." ""
    }

    $null = Invoke-Wsl -Arguments @("--status")
    $wslInstalled = ($script:WslExit -eq 0)

    $distroPresent = $false
    if ($wslInstalled) {
        $distros = Invoke-Wsl -Arguments @("--list", "--quiet")
        foreach ($d in $distros) {
            if ($d.Trim() -ieq $DistroName) {
                $distroPresent = $true
            }
        }
    }

    if (-not $wslInstalled) {
        Write-Host "    WSL is not installed yet. Installing WSL2 + $DistroName now (this downloads several hundred MB)..." -ForegroundColor Yellow
        Write-Host "    WSL אינו מותקן. מתקין כעת WSL2 עם $DistroName (הורדה של כמה מאות MB)..." -ForegroundColor Yellow
        & wsl.exe --install -d $DistroName
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 ("'wsl --install' failed (exit " + $LASTEXITCODE + "). Retrying with --web-download (bypasses the Microsoft Store CDN)...") `
                        "התקנת WSL נכשלה; מנסה שוב עם הורדה ישירה (--web-download)..."
            & wsl.exe --install -d $DistroName --web-download
            if ($LASTEXITCODE -ne 0) {
                Fail-Step ("'wsl --install -d " + $DistroName + "' failed even with --web-download (exit " + $LASTEXITCODE + "). Check internet access, proxy settings and Microsoft Store availability, then run install.ps1 again.") `
                          "התקנת WSL נכשלה גם עם --web-download. בדקו חיבור לאינטרנט/פרוקסי והריצו שוב את install.ps1." ""
            }
        }
        Write-Host ""
        Write-Host "IMPORTANT: A reboot is most likely required now." -ForegroundColor Yellow
        Write-Host "1. Restart the computer." -ForegroundColor Yellow
        Write-Host "2. If an Ubuntu window opens asking for a username/password - create one and close it." -ForegroundColor Yellow
        Write-Host "3. Run install.ps1 again (as Administrator). It will continue from this point." -ForegroundColor Yellow
        Write-Host "חשוב: כנראה נדרשת הפעלה מחדש של המחשב." -ForegroundColor Yellow
        Write-Host "1. הפעילו מחדש את המחשב." -ForegroundColor Yellow
        Write-Host "2. אם נפתח חלון אובונטו המבקש שם משתמש וסיסמה - צרו משתמש וסגרו את החלון." -ForegroundColor Yellow
        Write-Host "3. הריצו שוב את install.ps1 (כמנהל). ההתקנה תמשיך מנקודה זו." -ForegroundColor Yellow
        exit 0
    }

    if (-not $distroPresent) {
        Write-Host "    Installing distribution $DistroName (no reboot expected since WSL itself is present)..." -ForegroundColor Yellow
        Write-Host "    מתקין את ההפצה $DistroName..." -ForegroundColor Yellow
        # Ensure the new distro is created as WSL 2 even if the machine's default
        # was previously set to 1 (systemd-as-PID-1 and 'usbipd attach --wsl'
        # both require WSL 2).
        & wsl.exe --set-default-version 2 | Out-Null
        & wsl.exe --install -d $DistroName --no-launch
        if ($LASTEXITCODE -ne 0) {
            # --no-launch is not supported on very old wsl.exe versions; retry plain.
            & wsl.exe --install -d $DistroName
        }
    }

    # Verify the distro actually runs (as root, so first-run user creation is not needed).
    $null = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "-e", "true")
    if ($script:WslExit -ne 0) {
        Fail-Step ("The distribution '" + $DistroName + "' is installed but did not start. Open a normal terminal, run: wsl -d " + $DistroName + " , finish the first-run user setup (or reboot if Windows asked to), then run install.ps1 again.") `
                  ("ההפצה '" + $DistroName + "' מותקנת אך לא עלתה. פתחו טרמינל, הריצו: wsl -d " + $DistroName + " , השלימו יצירת משתמש (או הפעילו מחדש את המחשב אם נדרש), והריצו שוב את install.ps1.") ""
    }
    # Verify the distro actually runs as WSL 2. Under WSL 1 systemd can never be
    # PID 1 (setup-wsl.sh would exit 42 forever) and usbipd attach --wsl does not
    # work, so convert - or fail clearly - instead of dying later with a cryptic error.
    $distroVersion = $null
    $verLines = Invoke-Wsl -Arguments @("--list", "--verbose")
    foreach ($vline in $verLines) {
        $clean = ($vline -replace '^\s*\*', ' ').Trim()
        if ($clean -match ('^' + [regex]::Escape($DistroName) + '\s+\S+\s+(\d+)\s*$')) {
            $distroVersion = [int]$Matches[1]
        }
    }
    if ($distroVersion -eq 1) {
        Write-Host "    $DistroName is currently WSL 1; converting to WSL 2 (required for systemd and usbipd). This can take a few minutes..." -ForegroundColor Yellow
        Write-Host "    ההפצה רצה כ-WSL 1; ממיר ל-WSL 2 (נדרש עבור systemd ו-usbipd). זה עשוי לקחת מספר דקות..." -ForegroundColor Yellow
        & wsl.exe --set-version $DistroName 2
        if ($LASTEXITCODE -ne 0) {
            Fail-Step ("'wsl --set-version " + $DistroName + " 2' failed (exit " + $LASTEXITCODE + "). The scanner bridge requires WSL 2. Ensure the 'Virtual Machine Platform' Windows feature is enabled and virtualization is on in the BIOS/UEFI, then run install.ps1 again.") `
                      ("המרת ההפצה ל-WSL 2 נכשלה. ודאו שרכיב 'Virtual Machine Platform' מופעל ושווירטואליזציה פעילה ב-BIOS/UEFI, והריצו שוב.") ""
        }
        Write-Ok ($DistroName + " converted to WSL 2") ($DistroName + " הומרה ל-WSL 2")
    } elseif ($null -eq $distroVersion) {
        Write-Warn2 ("Could not read the WSL version of " + $DistroName + " from 'wsl --list --verbose'; continuing. If setup later fails with exit code 42 twice, check 'wsl -l -v' for WSL 1.") ""
    }
    Write-Ok ($DistroName + " is installed and running") ($DistroName + " מותקנת ופעילה")
} catch {
    Fail-Step "WSL setup failed." "התקנת WSL נכשלה." $_.Exception.Message
}

# --------------------------------------------------- Step 3: usbipd-win
Write-Step "Step 3/7: Installing usbipd-win (USB-over-IP bridge for WSL)" `
           "שלב 3/7: מתקין usbipd-win (גשר USB עבור WSL)"
try {
    $script:UsbipdExe = Get-UsbipdPath
    if ($null -eq $script:UsbipdExe) {
        $winget = Get-Command "winget" -ErrorAction SilentlyContinue
        if ($null -eq $winget) {
            Fail-Step "winget is not available. Install usbipd-win manually from https://github.com/dorssel/usbipd-win/releases (the .msi), then run install.ps1 again." `
                      "הכלי winget אינו זמין. התקינו ידנית את usbipd-win מהקישור https://github.com/dorssel/usbipd-win/releases והריצו שוב." ""
        }
        & winget install --exact --id dorssel.usbipd-win --accept-source-agreements --accept-package-agreements
        $script:UsbipdExe = Get-UsbipdPath
        if ($null -eq $script:UsbipdExe) {
            Fail-Step "usbipd was installed but usbipd.exe was not found. Close this window, open a NEW Administrator PowerShell, and run install.ps1 again (PATH refresh)." `
                      "usbipd הותקן אך לא נמצא ב-PATH. סגרו את החלון, פתחו PowerShell חדש כמנהל והריצו שוב את install.ps1." ""
        }
    }
    Write-Ok ("usbipd-win present at " + $script:UsbipdExe) "usbipd-win מותקן"
} catch {
    Fail-Step "usbipd-win installation failed." "התקנת usbipd-win נכשלה." $_.Exception.Message
}

# --------------------------------------- Step 4: find + bind the scanner
Write-Step ("Step 4/7: Locating and sharing the scanner (USB " + $VidPid + ")") `
           ("שלב 4/7: מאתר ומשתף את הסורק (USB " + $VidPid + ")")
try {
    $busid = Get-ScannerBusId
    if ($null -eq $busid) {
        Fail-Step ("Scanner " + $VidPid + " not found in 'usbipd list'. Make sure the Avision AV210C2 is connected via USB and powered ON, then run install.ps1 again. (Run 'usbipd list' yourself to see all devices.)") `
                  ("הסורק " + $VidPid + " לא נמצא. ודאו שהסורק Avision AV210C2 מחובר בכבל USB ודלוק, והריצו שוב את install.ps1.") ""
    }
    Write-Ok ("Scanner found at BUSID " + $busid) ("הסורק נמצא בכתובת " + $busid)

    $line = Get-ScannerLine
    if ($line -cmatch 'Not shared') {
        & $script:UsbipdExe bind --busid $busid
        if ($LASTEXITCODE -ne 0) {
            Fail-Step ("'usbipd bind --busid " + $busid + "' failed. Ensure you are running as Administrator and no other tool holds the device, then re-run.") `
                      "פקודת usbipd bind נכשלה. ודאו הרצה כמנהל ונסו שוב." ""
        }
        Write-Ok "Scanner is now shared with usbipd (persistent across reboots)" "הסורק משותף כעת עם usbipd (נשמר גם אחרי אתחול)"
    } else {
        Write-Ok "Scanner already shared (or attached)" "הסורק כבר משותף"
    }

    # Persist config for attach-scanner.ps1 / uninstall.ps1.
    if (-not (Test-Path $BridgeDir)) {
        New-Item -ItemType Directory -Path $BridgeDir -Force | Out-Null
    }
    $config = New-Object PSObject -Property @{
        VidPid = $VidPid
        BusId = $busid
        DistroName = $DistroName
    }
    $config | ConvertTo-Json | Set-Content -Path (Join-Path $BridgeDir "config.json") -Encoding UTF8
} catch {
    Fail-Step "Sharing the scanner via usbipd failed." "שיתוף הסורק דרך usbipd נכשל." $_.Exception.Message
}

# ------------------------------------ Step 5: provision Ubuntu (setup-wsl.sh)
Write-Step "Step 5/7: Setting up Ubuntu: SANE + avision driver + AirSane eSCL server. This can take several minutes (compiles AirSane)..." `
           "שלב 5/7: מגדיר את אובונטו: SANE + דרייבר avision + שרת AirSane. עשוי לקחת מספר דקות..."
try {
    $srcScript = Join-Path $ScriptDir "setup-wsl.sh"
    if (-not (Test-Path $srcScript)) {
        Fail-Step ("setup-wsl.sh not found next to install.ps1 (expected at " + $srcScript + "). Keep both files in the same folder.") `
                  "הקובץ setup-wsl.sh לא נמצא ליד install.ps1. שמרו את שני הקבצים באותה תיקייה." ""
    }

    # Read the script, normalize CRLF -> LF (bash chokes on CRLF), transfer via
    # base64 chunks (avoids all quoting/encoding issues between PowerShell and WSL).
    # Staged under /root, NOT /tmp: the exit-42 path below restarts WSL into its
    # first systemd boot, and systemd-tmpfiles empties /tmp on every boot on
    # Ubuntu 24.04 - a /tmp-staged script would be gone for the re-run.
    $remoteB64 = "/root/setup-wsl.sh.b64"
    $remoteScript = "/root/setup-wsl.sh"
    $content = [System.IO.File]::ReadAllText($srcScript)
    $content = $content -replace "`r`n", "`n"
    $content = $content -replace "`r", "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($content)
    $b64 = [System.Convert]::ToBase64String($bytes)

    $null = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "--exec", "bash", "-c", ("rm -f " + $remoteB64 + " " + $remoteScript))
    $offset = 0
    $chunkSize = 6000
    while ($offset -lt $b64.Length) {
        $len = [Math]::Min($chunkSize, $b64.Length - $offset)
        $chunk = $b64.Substring($offset, $len)
        $cmd = "printf %s " + $chunk + " >> " + $remoteB64
        $null = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "--exec", "bash", "-c", $cmd)
        if ($script:WslExit -ne 0) {
            Fail-Step "Failed to copy setup-wsl.sh into the WSL distro." "העתקת setup-wsl.sh אל תוך WSL נכשלה." ""
        }
        $offset = $offset + $len
    }
    $null = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "--exec", "bash", "-c", ("base64 -d " + $remoteB64 + " > " + $remoteScript))
    if ($script:WslExit -ne 0) {
        Fail-Step "Failed to decode setup-wsl.sh inside WSL." "פענוח setup-wsl.sh בתוך WSL נכשל." ""
    }

    $envPrefix = "SKIP_SCANSERVJS=0"
    if ($SkipScanservjs) {
        $envPrefix = "SKIP_SCANSERVJS=1"
    }

    $runSetup = {
        param($prefix)
        $setupCmd = $prefix + " bash " + $remoteScript
        & wsl.exe -d $DistroName -u root --exec bash -c $setupCmd 2>&1 | ForEach-Object {
            $text = ("" + $_) -replace "`0", ""
            Write-Host ("    [wsl] " + $text)
        }
        return $LASTEXITCODE
    }

    $setupExit = & $runSetup $envPrefix
    if ($setupExit -eq 42) {
        # setup-wsl.sh just enabled systemd; restart WSL and run it once more.
        # ($remoteScript lives under /root, so it survives the restart.)
        Write-Host "    systemd was enabled inside the distro; restarting WSL and re-running setup..." -ForegroundColor Yellow
        Write-Host "    systemd הופעל בהפצה; מאתחל את WSL ומריץ שוב את ההגדרה..." -ForegroundColor Yellow
        & wsl.exe --shutdown
        Start-Sleep -Seconds 8
        $setupExit = & $runSetup $envPrefix
        if ($setupExit -eq 42) {
            Fail-Step ("setup-wsl.sh still reports that systemd is not PID 1 even after restarting WSL. This usually means the distro runs as WSL 1 (systemd requires WSL 2). Check with: wsl -l -v  and convert with: wsl --set-version " + $DistroName + " 2 , then run install.ps1 again.") `
                      ("systemd עדיין אינו פעיל גם אחרי אתחול WSL. ככל הנראה ההפצה רצה כ-WSL 1. בדקו עם wsl -l -v, המירו עם wsl --set-version " + $DistroName + " 2 והריצו שוב.") ""
        }
    }
    if ($setupExit -ne 0) {
        Fail-Step ("setup-wsl.sh failed with exit code " + $setupExit + ". Read the [wsl] lines above for the exact error; fix and run install.ps1 again (it is safe to re-run).") `
                  ("הגדרת אובונטו נכשלה (קוד " + $setupExit + "). קראו את שורות [wsl] למעלה, תקנו והריצו שוב.") ""
    }
    Write-Ok "Ubuntu is provisioned (SANE + AirSane installed)" "אובונטו מוגדרת (SANE + AirSane מותקנים)"
} catch {
    Fail-Step "Provisioning Ubuntu failed." "הגדרת אובונטו נכשלה." $_.Exception.Message
}

# --------------------- Step 6: attach now + scheduled task for every logon
Write-Step "Step 6/7: Attaching the scanner to WSL and registering auto-attach at logon" `
           "שלב 6/7: מחבר את הסורק ל-WSL ורושם חיבור אוטומטי בכניסה למחשב"
try {
    $srcAttach = Join-Path $ScriptDir "attach-scanner.ps1"
    if (-not (Test-Path $srcAttach)) {
        Fail-Step "attach-scanner.ps1 not found next to install.ps1." "הקובץ attach-scanner.ps1 לא נמצא ליד install.ps1." ""
    }
    $destAttach = Join-Path $BridgeDir "attach-scanner.ps1"
    Copy-Item -Path $srcAttach -Destination $destAttach -Force

    $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $taskArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $destAttach + '" -DistroName "' + $DistroName + '" -VidPid "' + $VidPid + '"'
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $taskArgs
    # Pin the trigger to the installing user: the task runs with this user's
    # interactive token, and the WSL distro is registered per-user anyway, so an
    # any-user trigger would silently do nothing for other accounts. install.ps1
    # must therefore be run from the account that will use the scanner (see README).
    $taskUser = $env:USERDOMAIN + "\" + $env:USERNAME
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Ok ("Scheduled task '" + $TaskName + "' registered (runs when " + $taskUser + " logs on)") ("משימה מתוזמנת נרשמה (רצה בכניסת המשתמש " + $env:USERNAME + " למחשב)")

    # Start it right now; the task keeps 'usbipd attach --auto-attach' alive, which
    # also survives unplug/replug of the scanner while it is running.
    Start-ScheduledTask -TaskName $TaskName
    Write-Ok "Attach task started (usbipd attach --wsl --auto-attach --unplugged)" "משימת החיבור הופעלה"
} catch {
    Fail-Step "Registering/starting the attach task failed." "רישום משימת החיבור נכשל." $_.Exception.Message
}

# ----------------------------------------------------- Step 7: verification
Write-Step "Step 7/7: Verifying the scanner is visible inside WSL" `
           "שלב 7/7: מאמת שהסורק נראה בתוך WSL"
try {
    $found = $false
    for ($i = 0; $i -lt 6; $i++) {
        Start-Sleep -Seconds 5
        $scanOut = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "--exec", "scanimage", "-L")
        foreach ($line in $scanOut) {
            if ($line -match 'avision') {
                $found = $true
                Write-Ok ("SANE sees the scanner: " + $line.Trim()) "SANE מזהה את הסורק"
            }
        }
        if ($found) {
            break
        }
    }
    if (-not $found) {
        Write-Warn2 "scanimage -L did not list the avision scanner yet. The attach may still be in progress. Wait ~30s and check with: wsl -d $DistroName -u root -- scanimage -L . If still empty, see the Troubleshooting table in README.md." `
                    "הסורק עדיין לא זוהה בתוך WSL. המתינו כ-30 שניות ובדקו שוב. אם עדיין ריק - ראו את טבלת פתרון התקלות ב-README.md."
    }

    $airsane = Invoke-Wsl -Arguments @("-d", $DistroName, "-u", "root", "--exec", "systemctl", "is-active", "airsaned")
    if ($script:WslExit -eq 0) {
        Write-Ok "AirSane eSCL server is running on port 8090" "שרת AirSane פעיל על פורט 8090"
    } else {
        Write-Warn2 "airsaned service is not active. Check inside WSL: journalctl -u airsaned -n 50" `
                    "שירות airsaned אינו פעיל. בדקו בתוך WSL עם journalctl."
    }
} catch {
    Write-Warn2 ("Verification step hit an error: " + $_.Exception.Message) "שלב האימות נתקל בשגיאה."
}

# ------------------------------------------------------------------ summary
Write-Host ""
Write-Host "=========================================================" -ForegroundColor Green
Write-Host " Installation finished! / ההתקנה הסתיימה!" -ForegroundColor Green
Write-Host "=========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "How to scan / איך סורקים:" -ForegroundColor White
Write-Host ""
Write-Host "  A) Browser / דפדפן:" -ForegroundColor White
Write-Host "     AirSane web UI:    http://localhost:8090" -ForegroundColor White
if (-not $SkipScanservjs) {
    Write-Host "     scanservjs web UI: http://localhost:8080  (nicer UI, if installed)" -ForegroundColor White
}
Write-Host ""
Write-Host "  B) NAPS2 (recommended desktop app, free):" -ForegroundColor White
Write-Host "     1. Download NAPS2 7.5 or newer: https://www.naps2.com/download" -ForegroundColor White
Write-Host "     2. Profiles -> New Profile -> Choose Device" -ForegroundColor White
Write-Host "     3. Driver: ESCL Driver -> Manual IP -> enter: localhost:8090" -ForegroundColor White
Write-Host "     (Do NOT rely on Windows Settings 'Add device' discovery - a WSL-hosted" -ForegroundColor White
Write-Host "      scanner is not reliably discoverable by the Windows host itself.)" -ForegroundColor White
Write-Host ""
Write-Host "  1. הורידו NAPS2 גרסה 7.5 ומעלה" -ForegroundColor White
Write-Host "  2. Profiles -> New Profile -> Choose Device" -ForegroundColor White
Write-Host "  3. בחרו ESCL Driver -> Manual IP -> הקלידו localhost:8090" -ForegroundColor White
Write-Host ""
Write-Host "The scanner re-attaches automatically at every logon (scheduled task '$TaskName')." -ForegroundColor White
Write-Host "הסורק מתחבר אוטומטית בכל כניסה למחשב." -ForegroundColor White
exit 0
