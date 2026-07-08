# windows-bridge — Avision AV210C2 on Windows 10/11 via WSL2 + SANE + eSCL

One-time setup that makes an **Avision AV210C2** sheetfed scanner (USB `0638:0A3A`)
work on a modern Windows office PC — without any Windows driver from Avision.
The mature Linux **SANE `avision` backend** (which supports this model with status
*complete*) runs inside **WSL2**, and **AirSane** re-exposes the scanner to Windows
as a standard **eSCL / AirScan** network scanner plus a browser UI.

Note: the protocol/stack here wraps the field-proven SANE `avision` backend, but
this repo was developed without the physical scanner attached — see
"Verification status" in [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
before rollout.

## Architecture

```text
 +----------------------------- Windows 10/11 PC ------------------------------+
 |                                                                             |
 |  Avision AV210C2                                                            |
 |  (USB 0638:0A3A)                                                            |
 |        |                                                                    |
 |        | USB                                                                |
 |        v                                                                    |
 |  usbipd-win  ---- USB/IP (bind once, auto-attach at logon) ----+            |
 |  (Windows service)                                             |            |
 |                                                                v            |
 |  +----------------------- WSL2: Ubuntu-24.04 -----------------------+       |
 |  |                                                                  |       |
 |  |   /dev/bus/usb/...  -->  SANE (avision backend, status:complete) |       |
 |  |                            |                                     |       |
 |  |                            +--> AirSane   : eSCL server + web UI |       |
 |  |                            |    port 8090   (systemd: airsaned)  |       |
 |  |                            +--> scanservjs: browser scan UI      |       |
 |  |                                 port 8080   (optional extra)     |       |
 |  +------------------------------------------------------------------+       |
 |        ^                    ^                                               |
 |        | localhost:8090     | localhost:8080                                |
 |        | (WSL NAT localhost forwarding - zero network config needed)        |
 |        |                    |                                               |
 |   NAPS2 (eSCL driver,   Browser (Edge/Chrome):                              |
 |   "Manual IP" =         scan straight to PDF/JPG                            |
 |   localhost:8090)                                                           |
 +-----------------------------------------------------------------------------+
```

With WSL2's default NAT networking, `localhostForwarding` is `true` out of the
box, so `http://localhost:8090` and `http://localhost:8080` work from the
Windows side with **zero network configuration**.

## Prerequisites

| Requirement | Why |
|---|---|
| Windows 10 2004+ (build 19041) or Windows 11 | `wsl --install` needs it; usbipd-win needs 1809+ |
| Administrator account | One-time: enable WSL, install usbipd-win, `usbipd bind` |
| CPU virtualization (VT-x / AMD-V) enabled in BIOS/UEFI | WSL2 is a lightweight VM |
| Internet access | Downloads Ubuntu, usbipd-win, apt packages, AirSane source |
| The scanner plugged in and powered on | `install.ps1` finds it by VID:PID in `usbipd list` |
| ~10 minutes | AirSane is compiled from source inside WSL |

## Files

| File | Runs on | What it does |
|---|---|---|
| `install.ps1` | Windows (admin) | Orchestrates everything: WSL + Ubuntu-24.04, usbipd-win, `usbipd bind`, runs `setup-wsl.sh` inside the distro (CRLF-safe transfer via base64), attaches the scanner, registers the logon scheduled task, verifies with `scanimage -L`, prints scan URLs. Bilingual (English + Hebrew) progress messages. Idempotent — safe to re-run, including after the WSL-install reboot. |
| `setup-wsl.sh` | Ubuntu in WSL (root) | apt-installs SANE + build deps (noninteractive), ensures `avision` is enabled in `/etc/sane.d/dll.conf`, writes udev rules for the AV210 family (PIDs 0a24/0a25/0a2f/0a3a/1a35, MODE 0666) plus the `setfacl` workaround for the saned-user Debian bug, builds + installs AirSane with its `airsaned` systemd service (port 8090), optionally installs scanservjs (port 8080). Exits 42 if it had to enable systemd (installer restarts WSL and re-runs). Idempotent. |
| `attach-scanner.ps1` | Windows (scheduled task) | Finds the scanner's BUSID (cached fallback when unplugged) and keeps `usbipd attach --wsl --auto-attach --unplugged` running in a retry loop. Logs to `%ProgramData%\ScannerBridge\attach.log`. |
| `uninstall.ps1` | Windows (admin) | Removes the scheduled task, detaches/unbinds the scanner from usbipd, deletes `%ProgramData%\ScannerBridge`. Leaves the WSL distro intact unless `-RemoveDistro` is passed. |

## Install

```powershell
# Elevated (Administrator) PowerShell, from this folder, scanner plugged in + on:
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

If WSL was not previously installed, the script installs it, tells you to
**reboot and run `install.ps1` again** — it continues where it left off.

**Run `install.ps1` from the account that will use the scanner** (elevated via
UAC, not from a separate admin account): WSL distributions are registered
per-user, and the auto-attach scheduled task is pinned to the installing user's
logon. Installing from a different admin account means the daily user's logon
never starts the attach loop and cannot see the distro.

Options: `-DistroName Ubuntu-24.04` (default), `-SkipScanservjs`.

## Scanning

- **Browser**: `http://localhost:8090` (AirSane) or `http://localhost:8080` (scanservjs).
- **NAPS2** (free, recommended for multi-page PDF workflows), version **7.5+**:
  Profiles → New Profile → Choose Device → driver **ESCL Driver** → **Manual IP**
  → `localhost:8090` (the Manual IP field accepts a `host:port` value — keep
  port 8090). If you truly need AirSane on port 80, note that `airsaned` runs as
  the unprivileged `saned` user and cannot bind ports below 1024 by default —
  simply setting `LISTEN_PORT=80` takes the whole service down. Inside WSL run
  `sudo systemctl edit airsaned`, add `[Service]` + `AmbientCapabilities=CAP_NET_BIND_SERVICE`,
  set `LISTEN_PORT=80` in `/etc/default/airsane`, then `sudo systemctl restart airsaned`.
- **Do not** rely on Windows Settings → "Printers & scanners" → *Add device*:
  that flow needs mDNS discovery, which is unreliable between the Windows host
  and a service published from inside its own WSL guest (see table below).

## Why usbipd bind/attach?

- `usbipd bind` (done once by `install.ps1`, as admin) **is persistent** across
  reboots — it marks the device as shareable.
- `usbipd attach` is **not** persistent: it must be redone after every reboot,
  device reset, or unplug/replug. `--auto-attach` handles replug but only while
  the attach process stays alive — that is exactly what the scheduled task
  running `attach-scanner.ps1` provides.
- While attached, the scanner is **invisible to Windows** applications (VueScan
  etc.); run `uninstall.ps1` (or `usbipd detach`) to give it back to Windows.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Scanner missing from `usbipd list` | Unplugged, powered off, bad cable/port, or USB hub issue | Power the scanner on, use a rear USB port, avoid hubs, re-run `usbipd list`. Then re-run `install.ps1`. |
| `usbipd bind` fails | Not elevated, or device held by another driver | Run in an Administrator PowerShell. If VueScan/other software grabbed the device, close it. |
| Attach fails right after replug / reboot | The `--auto-attach` client process died; attach is never persistent by itself | Check the task: `Get-ScheduledTask ScannerBridge-AttachAV210C2`; log at `%ProgramData%\ScannerBridge\attach.log`; start manually: `Start-ScheduledTask ScannerBridge-AttachAV210C2`. |
| Attach says device is not shared | `usbipd` was upgraded or bind lost; BUSID changed (different USB port) | Re-run `install.ps1` (re-binds and re-caches the BUSID). Keep the scanner on the same physical port. |
| `wsl -d Ubuntu-24.04 -u root -- scanimage -L` shows nothing | Device not attached into WSL, or permissions | 1) `lsusb` inside WSL must show `0638:0a3a` — if not, it is an attach problem (see above). 2) If `sudo scanimage -L` works but plain/`saned` does not, the udev/ACL rules did not apply: re-run `install.ps1` (which re-copies and re-runs `setup-wsl.sh` inside the distro), then unplug/replug (udev rules run on the *next* attach event). |
| `sudo -u saned scanimage -L` empty but root sees it | Debian bug #918358 (ACLs not applied for daemon users) | `setup-wsl.sh` installs the documented `65-libsane.rules` setfacl workaround — re-run `install.ps1` (which re-copies and re-runs `setup-wsl.sh` inside the distro), then detach/attach the scanner once. |
| AirSane web page dead (`http://localhost:8090` refused) | `airsaned` not running, or WSL VM not started | Inside WSL: `systemctl status airsaned`, `journalctl -u airsaned -n 50`. Starting any WSL command boots the VM; the attach task does this at logon. |
| Windows "Add device" never finds the scanner (avahi/mDNS) | In NAT mode, mDNS announcements never leave the WSL subnet; in mirrored mode, Windows' own mDNS responder shares UDP 5353 with the guest and discovery is flaky (WSL issues #11852, #12354) | Expected — don't use that flow. Use NAPS2 → ESCL Driver → **Manual IP** → `localhost:8090`, or the browser UIs. |
| Other PCs on the LAN can't reach the scanner | Default NAT mode only forwards to the *local* host | Either enable mirrored networking (Windows 11 22H2+: `.wslconfig` → `[wsl2] networkingMode=mirrored`, then `wsl --shutdown`) **and** open inbound TCP 8090/8080 in Windows Defender Firewall, or add `netsh interface portproxy` rules + firewall openings. Same-PC scanning needs none of this. |
| Firewall prompt / corporate policy blocks 8090 | Windows Defender Firewall or corp endpoint agent | Same-PC access via `localhost` is normally exempt; for LAN access add explicit inbound allow rules for TCP 8090 (AirSane) / 8080 (scanservjs). |
| `winget` not found | Older Windows 10 / LTSC without App Installer | Install "App Installer" from the Microsoft Store, or install usbipd-win manually from https://github.com/dorssel/usbipd-win/releases. |
| `wsl --install` stuck at 0.0% | Store CDN issue | `wsl --install --web-download -d Ubuntu-24.04`. |
| Corporate proxy breaks apt/git inside WSL | Proxy env not propagated | Recent WSL propagates the Windows proxy (`autoProxy`). Otherwise, inside WSL: `export https_proxy=http://proxy:port` before re-running `setup-wsl.sh` (git/apt/curl honor it). |
| Everything is too much trouble | — | Commercial fallback: **VueScan** (hamrick.com) ships its own reverse-engineered AV210C2 driver for Windows — no WSL needed. Run `uninstall.ps1` first so Windows owns the USB device again. |

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1                # keep Ubuntu
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveDistro  # also delete Ubuntu (destroys its data)
```

usbipd-win itself is left installed (`winget uninstall dorssel.usbipd-win` to remove).
