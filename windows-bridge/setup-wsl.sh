#!/usr/bin/env bash
#
# setup-wsl.sh -- provision Ubuntu 24.04 (inside WSL2) as a scan server for the
# Avision AV210C2 sheetfed scanner (USB 0638:0a3a).
#
# What it does (all steps are idempotent, safe to re-run):
#   1. Verifies systemd is PID 1 (enables it in /etc/wsl.conf and exits 42 if not,
#      so the Windows-side installer knows to restart WSL and re-run).
#   2. Installs SANE (avision backend supports the AV210C2 with status "complete")
#      plus the build dependencies for AirSane.
#   3. Ensures the 'avision' backend is enabled in /etc/sane.d/dll.conf
#      (it is enabled by default upstream, but we make sure).
#   4. Installs udev rules so non-root users / the saned service user can open
#      the USB device that usbipd-win attaches into WSL.
#   5. Builds and installs AirSane (eSCL/AirScan server + web UI, port 8090)
#      from source -- there is no Ubuntu package for it.
#   6. Optionally installs scanservjs (friendlier browser scanning UI, port 8080).
#      Skip with SKIP_SCANSERVJS=1.
#   7. Runs a self-test and prints hints.
#
# Exit codes: 0 = ok, 42 = systemd was just enabled, WSL restart + re-run needed,
#             anything else = failure (message printed).
#
# Must run as root (re-execs itself with sudo if not).
# Proxy note: git/curl/apt honor the standard http_proxy/https_proxy env vars;
# run with 'sudo -E' (we do) so a corporate proxy configured in the environment
# is passed through. On recent WSL, autoProxy propagates the Windows proxy.

set -u
set -o pipefail

SCANNER_VID="0638"
# Cover the AV210 family PIDs, not just the AV210C2 (0a3a).
SCANNER_PIDS="0a24 0a25 0a2f 0a3a 1a35"
AIRSANE_REPO="https://github.com/SimulPiscator/AirSane.git"
AIRSANE_SRC="/usr/local/src/AirSane"
AIRSANE_BUILD="/usr/local/src/AirSane-build"
SCANSERVJS_BOOTSTRAP="https://raw.githubusercontent.com/sbs20/scanservjs/master/bootstrap.sh"
SKIP_SCANSERVJS="${SKIP_SCANSERVJS:-0}"
# scanservjs release passed to the bootstrap's -v flag; override with a
# pinned tag (e.g. SCANSERVJS_VERSION=v3.0.3) for a deterministic install.
SCANSERVJS_VERSION="${SCANSERVJS_VERSION:-latest}"
FORCE_REBUILD_AIRSANE="${FORCE_REBUILD_AIRSANE:-0}"

log()  { printf '[setup-wsl] %s\n' "$*"; }
warn() { printf '[setup-wsl] WARNING: %s\n' "$*" >&2; }
die()  { printf '[setup-wsl] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- root check
if [ "$(id -u)" -ne 0 ]; then
    log "Not running as root, re-executing with sudo..."
    exec sudo -E bash "$0" "$@"
fi

# ------------------------------------------------------------- systemd check
# Ubuntu-24.04 WSL images enable systemd by default, but an imported or older
# rootfs may not. AirSane's service (airsaned) and avahi need systemd.
if [ "$(ps -o comm= -p 1 2>/dev/null | tr -d ' ')" != "systemd" ]; then
    log "systemd is not PID 1 in this distro; enabling it via /etc/wsl.conf."
    touch /etc/wsl.conf
    if grep -Eq '^\s*systemd\s*=\s*true' /etc/wsl.conf; then
        log "systemd=true already present in /etc/wsl.conf."
    elif grep -Eq '^\s*\[boot\]' /etc/wsl.conf; then
        # [boot] section exists without systemd=true: insert after the header.
        sed -i '/^\s*\[boot\]/a systemd=true' /etc/wsl.conf
    else
        printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
    fi
    log "systemd enabled in /etc/wsl.conf. The WSL distro must be restarted:"
    log "  (from Windows)  wsl --shutdown"
    log "then re-run this script. Exiting with code 42 to signal the installer."
    exit 42
fi
log "systemd is PID 1 - OK."

# ------------------------------------------------------------------ packages
export DEBIAN_FRONTEND=noninteractive
log "Installing SANE, AirSane build dependencies and helpers (apt, noninteractive)..."
apt-get update -y || die "apt-get update failed. Check network/proxy inside WSL (env http_proxy/https_proxy)."
# sane-utils sets up the 'saned' user with proper permissions (per AirSane README);
# libsane1 ships the udev rules/hwdb that tag scanners; libsane-common ships
# /etc/sane.d/dll.conf and avision.conf.
# libusb-1.0-0-dev is the concrete noble package for AirSane's "libusb-1.*-dev" dep.
apt-get install -y \
    sane-utils libsane1 libsane-common libsane-dev \
    libjpeg-dev libpng-dev libavahi-client-dev libusb-1.0-0-dev \
    git cmake g++ make pkg-config \
    avahi-daemon acl usbutils ca-certificates curl \
    || die "apt-get install failed. Re-run after fixing the apt error above."
log "Packages installed."

# ------------------------------------------------------- enable avision in dll.conf
DLL_CONF="/etc/sane.d/dll.conf"
if [ ! -f "$DLL_CONF" ]; then
    warn "$DLL_CONF missing (unexpected: shipped by libsane-common). Creating it."
    printf 'avision\n' > "$DLL_CONF"
elif grep -Eq '^[[:space:]]*avision[[:space:]]*$' "$DLL_CONF"; then
    log "avision backend already enabled in dll.conf."
elif grep -Eq '^[[:space:]]*#[[:space:]]*avision[[:space:]]*$' "$DLL_CONF"; then
    sed -i 's/^[[:space:]]*#[[:space:]]*avision[[:space:]]*$/avision/' "$DLL_CONF"
    log "Uncommented avision backend in dll.conf."
else
    printf 'avision\n' >> "$DLL_CONF"
    log "Appended avision backend to dll.conf."
fi

# ----------------------------------------------------------------- udev rules
# Devices arriving via usbip/vhci still generate udev events, so rules apply.
# Rule 1: belt-and-braces rule pinned to the Avision AV210 family: world-rw so
#         permission problems can never block first light. (0666 is deliberate
#         for a single-user office PC; tighten to 0664 + group later if wanted.)
RULES_AVISION="/etc/udev/rules.d/65-avision-av210.rules"
{
    echo "# Avision AV210 family sheetfed scanners - generated by setup-wsl.sh"
    for pid in $SCANNER_PIDS; do
        printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="%s", ATTR{idProduct}=="%s", MODE="0666", GROUP="scanner", ENV{libsane_matched}="yes"\n' "$SCANNER_VID" "$pid"
    done
} > "$RULES_AVISION"
log "Wrote $RULES_AVISION."

# Rule 2: documented workaround for Debian bug #918358 (root sees scanner,
# daemon user 'saned' does not, because ACLs are not applied for daemon users).
# Exact rule from the AirSane README.
RULES_SETFACL="/etc/udev/rules.d/65-libsane.rules"
if [ ! -f "$RULES_SETFACL" ]; then
    cat > "$RULES_SETFACL" <<'EOF'
# Workaround for Debian bug #918358: grant the scanner group rw on matched
# SANE devices so daemon users (saned) can access them. From the AirSane README.
ENV{libsane_matched}=="yes", RUN+="/usr/bin/setfacl -m g:scanner:rw $env{DEVNAME}"
EOF
    log "Wrote $RULES_SETFACL (saned/scanner-group ACL workaround)."
else
    log "$RULES_SETFACL already exists - leaving as is."
fi

# Reload udev; in WSL udev may be partially functional, so never fail here.
udevadm control --reload 2>/dev/null || warn "udevadm control --reload failed (non-fatal in WSL)."
udevadm trigger 2>/dev/null || warn "udevadm trigger failed (non-fatal in WSL)."

# ------------------------------------------------------------------- groups
getent group scanner >/dev/null || groupadd scanner
if id saned >/dev/null 2>&1; then
    usermod -aG scanner saned
    log "User 'saned' is in group 'scanner'."
else
    warn "User 'saned' does not exist (sane-utils normally creates it). AirSane's service runs as saned; re-check after install."
fi
DEFAULT_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
if [ -n "$DEFAULT_USER" ]; then
    usermod -aG scanner "$DEFAULT_USER"
    log "User '$DEFAULT_USER' added to group 'scanner'."
fi

# ------------------------------------------------------------ build AirSane
# No Ubuntu/Debian package exists for AirSane; source build is the documented path.
need_build=1
if [ "$FORCE_REBUILD_AIRSANE" != "1" ]; then
    if command -v airsaned >/dev/null 2>&1 || [ -x /usr/local/bin/airsaned ]; then
        if systemctl list-unit-files 2>/dev/null | grep -q '^airsaned\.service'; then
            log "AirSane already installed (binary + airsaned.service present) - skipping build. Set FORCE_REBUILD_AIRSANE=1 to rebuild."
            need_build=0
        fi
    fi
fi

if [ "$need_build" = "1" ]; then
    log "Cloning/updating AirSane sources (git honors http_proxy/https_proxy)..."
    if [ -d "$AIRSANE_SRC/.git" ]; then
        git -C "$AIRSANE_SRC" pull --ff-only || warn "git pull failed; building the already-checked-out revision."
    else
        git clone "$AIRSANE_REPO" "$AIRSANE_SRC" || die "git clone of AirSane failed. Check network/proxy (export https_proxy=... and re-run)."
    fi
    mkdir -p "$AIRSANE_BUILD"
    log "Building AirSane (cmake + make)..."
    ( cd "$AIRSANE_BUILD" \
      && cmake "$AIRSANE_SRC" \
      && make -j"$(nproc)" \
      && make install ) || die "AirSane build failed. Scroll up for the compiler/cmake error."
    log "AirSane built and installed."
fi

# Default config: AirSane's 'make install' ships a complete /etc/default/airsane
# whose defaults (LISTEN_PORT=8090, compatible /eSCL path) already suit us, so
# normally there is nothing to do. If the file is somehow absent, restore the
# FULL upstream template from the source tree: the shipped airsaned.service
# passes every ${VAR} from this file unconditionally on its ExecStart line, so a
# partial hand-written file would expand ~15 unset variables into empty
# '--flag=' arguments and break the service.
if [ ! -f /etc/default/airsane ]; then
    if [ -f "$AIRSANE_SRC/systemd/airsaned.default" ]; then
        cp "$AIRSANE_SRC/systemd/airsaned.default" /etc/default/airsane
        log "Restored /etc/default/airsane from the AirSane source template."
    else
        warn "/etc/default/airsane is missing and no template found at $AIRSANE_SRC/systemd/airsaned.default; airsaned will likely fail to start. Re-run with FORCE_REBUILD_AIRSANE=1 to reinstall AirSane."
    fi
else
    log "/etc/default/airsane already exists - leaving as is."
fi

systemctl daemon-reload
systemctl enable --now avahi-daemon >/dev/null 2>&1 || warn "avahi-daemon enable/start failed (mDNS discovery is best-effort under WSL anyway)."
if systemctl list-unit-files 2>/dev/null | grep -q '^airsaned\.service'; then
    systemctl enable airsaned  >/dev/null 2>&1 || warn "systemctl enable airsaned failed."
    systemctl restart airsaned || warn "airsaned failed to start; check: journalctl -u airsaned -n 50"
    log "airsaned service enabled and (re)started on port 8090."
else
    warn "airsaned.service not found after install - check the 'make install' output above."
fi

# ----------------------------------------------------- optional: scanservjs
if [ "$SKIP_SCANSERVJS" = "1" ]; then
    log "Skipping scanservjs (SKIP_SCANSERVJS=1)."
elif systemctl list-unit-files 2>/dev/null | grep -q '^scanservjs\.service'; then
    log "scanservjs already installed - skipping."
else
    log "Installing scanservjs (optional browser scan UI on port 8080)..."
    # Official Debian/Ubuntu bootstrap from the scanservjs README, but
    # downloaded to a local file first (never piped straight into a root
    # shell) and gated on a sanity check. Trust assumption: the script is
    # fetched over TLS from the scanservjs GitHub repository; skip this
    # optional component entirely with SKIP_SCANSERVJS=1. Pin a release
    # with SCANSERVJS_VERSION=vX.Y.Z for a deterministic artifact.
    # Non-fatal either way: AirSane alone fully covers eSCL + browser use.
    bootstrap_tmp="$(mktemp /tmp/scanservjs-bootstrap.XXXXXX.sh)"
    if curl -fsSL "$SCANSERVJS_BOOTSTRAP" -o "$bootstrap_tmp" \
        && [ -s "$bootstrap_tmp" ] \
        && head -c2 "$bootstrap_tmp" | grep -q '#!' \
        && bash "$bootstrap_tmp" -v "$SCANSERVJS_VERSION"; then
        log "scanservjs installed (http://localhost:8080)."
    else
        warn "scanservjs install failed - continuing, AirSane on :8090 still provides a web UI."
    fi
    rm -f "$bootstrap_tmp"
fi

# -------------------------------------------------------------- self-test
log "----------------------------------------------------------------------"
log "Self-test (it is OK if no scanner is listed yet - the USB device is"
log "attached from Windows AFTER this script, by 'usbipd attach'):"
log "----------------------------------------------------------------------"
if lsusb 2>/dev/null | grep -qi "0638:"; then
    log "USB: Avision device is visible to WSL (lsusb)."
else
    log "USB: no Avision device attached yet (expected at this stage)."
fi
scanimage -L 2>&1 | sed 's/^/[scanimage -L] /' || true
if id saned >/dev/null 2>&1; then
    sudo -u saned scanimage -L 2>&1 | sed 's/^/[saned scanimage -L] /' || true
fi
if systemctl is-active airsaned >/dev/null 2>&1; then
    log "airsaned: active (http://localhost:8090)"
else
    warn "airsaned not active."
fi

log "Done. After the scanner is attached (usbipd attach --wsl), verify with:"
log "  scanimage -L                      -> should list 'avision:libusb:...'"
log "  sudo -u saned scanimage -L        -> must also list it (AirSane runs as saned)"
log "  curl http://localhost:8090/       -> AirSane web UI"
exit 0
