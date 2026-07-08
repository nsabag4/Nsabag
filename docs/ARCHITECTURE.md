# Architecture & Security Notes (for IT)

Audience: the IT person who installs and owns `windows-bridge/`. End-user docs are in Hebrew under `docs/*.he.md`.

## Why this design

The Avision AV210C2 (USB `0638:0A3A`) has no official Windows 10/11 driver. The SANE project's `avision` backend supports this exact model with status **complete** (see `doc/descriptions/avision.desc` in sane-backends) and has ~20 years of field history. Rather than reimplement a driver, `windows-bridge` runs that backend inside WSL2 and re-exposes the scanner to Windows over **eSCL** (the Mopria/AirScan standard that Windows 10/11 and NAPS2 speak natively).

## Data flow

```
Avision AV210C2 -- USB --> Windows host
                              |
                              | usbipd-win (USB/IP; Windows service)
                              |   - `usbipd bind`   : one-time, persistent share (admin)
                              |   - `usbipd attach --wsl --auto-attach --unplugged`
                              |     kept alive by scheduled task "ScannerBridge-AttachAV210C2"
                              v
                    WSL2 guest (Ubuntu-24.04, systemd enabled)
                              |
                              | /dev/bus/usb/* + udev rules (0638:0a3a, MODE 0666,
                              | plus the setfacl workaround for Debian bug #918358)
                              v
                    SANE `avision` backend (libsane / sane-utils)
                              |
              +---------------+----------------+
              v                                v
   AirSane (systemd unit `airsaned`)   scanservjs (optional)
   eSCL server + minimal web UI        full-featured browser scan UI
   TCP 8090                            TCP 8080
              |                                |
              +----------- WSL NAT localhostForwarding (default true) ----+
              v                                v
   Windows clients on the SAME machine:
   - Browser: http://localhost:8090 / http://localhost:8080
   - NAPS2 >= 7.5: ESCL Driver -> Manual IP -> localhost:8090
```

## Persistence model

| Piece | Persistent? | Mechanism |
|---|---|---|
| `usbipd bind` (device shared) | Yes, survives reboots | usbipd-win stores it; done once by `install.ps1` |
| `usbipd attach` (device inside WSL) | **No** — lost on reboot, device reset, or replug | Scheduled task `ScannerBridge-AttachAV210C2` (at logon) runs `attach-scanner.ps1`, which keeps `usbipd attach --wsl --auto-attach --unplugged` alive in a retry loop. `--auto-attach` handles replug only while that process runs |
| BUSID | Changes if the scanner moves to another USB port | Cached in `%ProgramData%\ScannerBridge\config.json`; re-run `install.ps1` after changing ports |
| `airsaned` / scanservjs | systemd units inside the distro | Start when the WSL VM boots; the attach task's `wsl` invocation at logon boots the VM |

## What `install.ps1` changes on the machine

All of it is inspectable in `windows-bridge/install.ps1` (idempotent, PowerShell 5.1, bilingual output):

1. **WSL2 + Ubuntu-24.04** installed if missing (`wsl --install -d Ubuntu-24.04`; may require one reboot — the script resumes on re-run).
2. **usbipd-win** installed via `winget install --exact dorssel.usbipd-win` (adds a Windows service and the `usbipd` CLI).
3. `usbipd bind --busid <BUSID>` for the device matching `0638:0a3a`.
4. Inside the distro (`setup-wsl.sh`, run as root): apt packages (sane-utils, build deps, avahi-daemon), udev rules for the AV210 family + setfacl workaround, AirSane compiled from source and installed with its `airsaned` systemd unit (port 8090), scanservjs (port 8080, skippable with `-SkipScanservjs`).
5. **Scheduled task** `ScannerBridge-AttachAV210C2` (at logon, hidden window) running `%ProgramData%\ScannerBridge\attach-scanner.ps1`; logs to `%ProgramData%\ScannerBridge\attach.log`.
6. State directory `%ProgramData%\ScannerBridge\` (config.json, attach script, log).

No changes to: Windows firewall rules, PATH, execution policy (only `-Scope Process` at run time), networking mode (`.wslconfig` untouched — default NAT).

## Security posture

- **Network exposure**: with WSL's default NAT networking, AirSane (8090) and scanservjs (8080) live on the WSL-internal subnet. They are reachable from the Windows host itself via `localhost` (WSL `localhostForwarding`, default `true`) and **not reachable from the LAN**. No inbound firewall rules are added. Nothing leaves the machine; no cloud services involved.
- **Authentication**: the web UIs have no auth — acceptable because only local processes can reach them in the default configuration. If you deliberately expose them to the LAN (see below), treat them as unauthenticated services and scope firewall rules accordingly.
- **Device exclusivity**: while attached to WSL, the scanner is invisible to Windows applications (WIA/TWAIN/VueScan). `usbipd detach` or `uninstall.ps1` returns it to Windows.
- **Privilege**: admin rights are needed only for install/uninstall (WSL enablement, usbipd install, `usbipd bind`, scheduled task registration). Day-to-day scanning requires no elevation. `usbipd attach` itself does not need admin once the device is bound.
- **udev rule breadth**: `setup-wsl.sh` grants `MODE 0666` on the AV210-family VID:PIDs *inside the WSL guest only*; this is world-writable within the guest, which is single-purpose here. Tighten to group-based ACLs if the distro is shared for other work.
- **Supply chain**: AirSane is built from source from `github.com/SimulPiscator/AirSane` (no Ubuntu package exists); scanservjs installs via its upstream bootstrap script. Pin/vendor these if your policy requires it. Everything else comes from Ubuntu archives and winget.

## Optional: exposing the scanner to other LAN machines

Not enabled by default. Two routes, both requiring explicit inbound firewall allows for TCP 8090/8080:

- **Mirrored networking** (Windows 11 22H2+): `.wslconfig` → `[wsl2] networkingMode=mirrored`, then `wsl --shutdown`. LAN peers can then reach the services on the host's IP, and mDNS discovery *may* work for LAN clients.
- **NAT + portproxy**: `netsh interface portproxy` rules from a host port to the WSL address.

## Known limitation: mDNS discovery on the host itself

AirSane advertises `_uscan._tcp` via avahi, and the Windows Settings "Add device" flow depends on that discovery. Between a WSL guest and **its own Windows host** this is unreliable: in NAT mode the multicast never leaves the WSL subnet; in mirrored mode the guest shares UDP 5353 with Windows' own mDNS responder and discovery is flaky (microsoft/WSL#11852, #12354). Windows offers no manual-IP flow for eSCL *scanners* (the add-by-IP wizard covers printing only). Hence the supported clients are: the browser UIs, and NAPS2 (>= 7.5) with its eSCL **Manual IP** option pointed at `localhost:8090`. Treat host-side discovery as best-effort bonus, never as the plan.

## Verification status (read this)

This repository was developed **without the physical scanner attached**. Every command, package name, port, flag, and protocol claim was verified against the upstream documentation/source of the respective component (sane-backends, usbipd-win, MicrosoftDocs/WSL, AirSane, scanservjs, NAPS2), but no end-to-end run against real hardware has occurred. Verification tooling shipped for you:

- `install.ps1` self-verifies each stage and ends with a `scanimage -L` + `airsaned` health check.
- The diagnostic chain in [TROUBLESHOOTING.he.md](TROUBLESHOOTING.he.md) (`usbipd list` → `lsusb` → `scanimage -L` → `systemctl is-active airsaned`) isolates a failure to one link.
- `userspace-driver/`'s `av210 probe` talks to the device over raw USB (WinUSB via Zadig), independent of the whole WSL stack — if `probe` succeeds, the hardware and cabling are good.
- VueScan's free trial (hamrick.com has a dedicated AV210C2 page) is an independent hardware sanity check with zero setup.

## Uninstall

```powershell
# From windows-bridge\, elevated PowerShell:
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1                 # remove task + detach/unbind + delete %ProgramData%\ScannerBridge
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveDistro   # additionally delete the Ubuntu-24.04 distro (destroys its data)

winget uninstall dorssel.usbipd-win   # optional: remove usbipd-win itself
```

After `uninstall.ps1`, Windows owns the USB device again (e.g. for VueScan). WSL itself is never removed by these scripts.
