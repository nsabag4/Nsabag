# share-lan.ps1 - expose the scanner's web/eSCL interface to the office LAN.
#
# Run this ON THE PC THE SCANNER IS PLUGGED INTO, after install.ps1 succeeded.
# It forwards the AirSane port (8090, and optionally scanservjs on 8080) from
# this machine to the network and opens the Windows Firewall for it, so any
# office computer can scan from a browser at  http://<this-pc>:8090
#
#   .\share-lan.ps1                    enable sharing (AirSane, port 8090)
#   .\share-lan.ps1 -Ports 8090,8080   also share scanservjs (opt-in)
#   .\share-lan.ps1 -Disable           undo everything this script added
#
# Notes:
#  * The firewall rules are limited to the Domain and Private profiles -
#    nothing is exposed on networks marked Public.
#  * The web UIs have no authentication; share only on a trusted office LAN.
#  * Uses "netsh interface portproxy" pointed at 127.0.0.1, which reaches the
#    WSL2 localhost relay, so it keeps working when the WSL address changes.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Disable,
    # Only the AirSane port by default; pass -Ports 8090,8080 to also expose
    # scanservjs. 8080 is opt-in so a missing scanservjs never turns into an
    # accidental LAN exposure of some unrelated local service on that port.
    [ValidateScript({ $_ -ge 1 -and $_ -le 65535 })]
    [int[]]$Ports = @(8090)
)

$ErrorActionPreference = "Stop"
$RulePrefix = "ScannerBridge-LAN"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This script must run as Administrator. Right-click PowerShell -> 'Run as administrator'." -ForegroundColor Red
    Write-Host "יש להריץ כמנהל: קליק ימני על PowerShell ובחרו 'הפעל כמנהל'." -ForegroundColor Red
    exit 1
}

function Remove-Sharing {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([int[]]$PortList)
    foreach ($port in $PortList) {
        if ($PSCmdlet.ShouldProcess("port $port", "Remove portproxy forwarding and firewall rule")) {
            & netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0 2>$null | Out-Null
            Get-NetFirewallRule -DisplayName ("{0}-{1}" -f $RulePrefix, $port) -ErrorAction SilentlyContinue |
                Remove-NetFirewallRule
            Write-Host ("  [OK] Sharing removed for port " + $port)
        }
    }
}

if ($Disable) {
    Write-Host "Removing LAN sharing... / מסיר את השיתוף ברשת..."
    # Plain -Disable must clean up every port this script ever shared, not
    # just the current default: discover them from our own firewall rules.
    $portsToRemove = $Ports
    if (-not $PSBoundParameters.ContainsKey("Ports")) {
        $rulePattern = "^{0}-(\d+)$" -f ([regex]::Escape($RulePrefix))
        $managedPorts = @(Get-NetFirewallRule -DisplayName ($RulePrefix + "-*") -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.DisplayName -match $rulePattern) {
                    [int]$Matches[1]
                }
            })
        $portsToRemove = @($Ports + $managedPorts) | Sort-Object -Unique
    }
    Remove-Sharing -PortList $portsToRemove
    Write-Host "Done. The scanner is now reachable from this PC only (http://localhost:8090)."
    Write-Host "הסתיים. הסורק זמין כעת רק מהמחשב הזה."
    exit 0
}

Write-Host "Enabling LAN sharing for the scanner web interface..."
Write-Host "מפעיל שיתוף של ממשק הסריקה לרשת המשרדית..."
Write-Host ""

foreach ($port in $Ports) {
    if (-not $PSCmdlet.ShouldProcess("port $port", "Add portproxy forwarding and firewall rule")) {
        continue
    }

    # Re-create idempotently: delete any stale entry first.
    & netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0 2>$null | Out-Null
    & netsh interface portproxy add v4tov4 listenport=$port listenaddress=0.0.0.0 connectport=$port connectaddress=127.0.0.1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  [FAIL] Could not add port forwarding for " + $port) -ForegroundColor Red
        exit 1
    }

    # Always drop and recreate the rule: a stale rule with the same name but
    # wrong profile/port/action must never survive under this name.
    $ruleName = "{0}-{1}" -f $RulePrefix, $port
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $ruleName `
        -Description "Office scanner (Avision AV210C2) web/eSCL access from the LAN" `
        -Direction Inbound -Protocol TCP -LocalPort $port `
        -Action Allow -Profile Domain, Private | Out-Null
    Write-Host ("  [OK] Port " + $port + " is now shared (Domain/Private networks only)")
}

Write-Host ""
Write-Host "Shared! Coworkers can now scan from any office computer at:"
Write-Host "בוצע! עכשיו אפשר לסרוק מכל מחשב במשרד בכתובות:"
Write-Host ""
$hostName = [System.Net.Dns]::GetHostName()
foreach ($port in $Ports) {
    $label = ""
    if ($port -eq 8090) { $label = "        (AirSane web UI + eSCL)" }
    if ($port -eq 8080) { $label = "        (scanservjs UI)" }
    Write-Host ("    http://" + $hostName + ":" + $port + $label) -ForegroundColor Green
}
$addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "*WSL*" -and $_.InterfaceAlias -notlike "*vEthernet*" }
foreach ($addr in $addresses) {
    foreach ($port in $Ports) {
        Write-Host ("    http://" + $addr.IPAddress + ":" + $port) -ForegroundColor Green
    }
}
Write-Host ""
Write-Host "NAPS2 on other PCs: add an eSCL scanner with Manual IP = " -NoNewline
Write-Host ($hostName + ":8090") -ForegroundColor Green
Write-Host "Keep this PC on with the scanner connected. Undo anytime with: .\share-lan.ps1 -Disable"
Write-Host "השאירו את המחשב הזה דולק עם הסורק מחובר. לביטול: .\share-lan.ps1 -Disable"
