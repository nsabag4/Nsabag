# פרומפטים מלאים לסוכני Workflow/Agent — מילה במילה

זהו התוכן **המדויק, ללא שינוי אות אחת**, של כל הפרומפטים שנשלחו לכלי ה-Workflow
וה-Agent במהלך השיחה. אלה ההוראות בפועל שקיבלו תת-הסוכנים (research agents,
build agents, review agents, fix agents) שביצעו את רוב העבודה בפרויקט.

---

## 1. סקריפט ה-Workflow המלא: `av210c2-driver-build`

זהו סקריפט ה-JavaScript המלא שהורץ דרך כלי ה-Workflow (מנוע אורקסטרציה
מרובה-סוכנים) לבניית כל הפתרון. כולל את כל 12 קריאות ה-`agent()` (מחקר,
בנייה, ביקורת, תיקון) עם הפרומפטים המלאים שלהן, ואת לוגיקת הבקרה
(phase/parallel) שביניהן.

```javascript
export const meta = {
  name: 'av210c2-driver-build',
  description: 'Build a complete driver stack for the Avision AV210C2 scanner: userspace driver, Windows bridge, Hebrew docs, with adversarial review',
  phases: [
    { title: 'Research', detail: 'extract byte-level protocol from SANE avision backend + verify Windows integration facts' },
    { title: 'Build', detail: 'userspace Python driver, Windows WSL2 bridge, Hebrew documentation' },
    { title: 'Review', detail: 'adversarial verification of protocol correctness, scripts, and docs' },
    { title: 'Fix', detail: 'apply confirmed findings' },
  ],
}

const SCRATCH = args.scratch
const REPO = args.repo

const SPEC_SCHEMA = {
  type: 'object',
  properties: {
    spec: { type: 'string', description: 'The full detailed specification document in markdown' },
    openQuestions: { type: 'array', items: { type: 'string' } },
  },
  required: ['spec'],
}

const MANIFEST_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
    testCommand: { type: 'string', description: 'command to run automated tests/syntax checks for this component, empty if none' },
  },
  required: ['files', 'notes'],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          summary: { type: 'string' },
          evidence: { type: 'string', description: 'exact quote from avision.c or docs proving the defect' },
          suggestedFix: { type: 'string' },
        },
        required: ['file', 'severity', 'summary', 'suggestedFix'],
      },
    },
  },
  required: ['findings'],
}

phase('Research')
log('Extracting the Avision USB protocol from the proven SANE backend source...')

const [transportSpec, scanFlowSpec, windowsFacts] = await parallel([
  () => agent(`You are a senior driver engineer. Read the SANE avision backend source at ${SCRATCH}/avision.c and ${SCRATCH}/avision.h (already downloaded, ~9580 lines — read in targeted chunks using Grep to locate functions, then Read specific line ranges).

Produce a BYTE-LEVEL specification of the USB TRANSPORT layer used by Avision USB scanners, sufficient for someone to reimplement it in Python with libusb WITHOUT ever looking at the C source. Cover exactly:

1. avision_cmd() (around line 2510): the exact framing of commands over USB. How is the SCSI CDB sent (bulk out endpoint, raw 12/10/6-byte CDB or wrapped)? Quote the m_cmd construction. What are the exact write sizes and any padding?
2. How outgoing payload data (e.g. window descriptors) is written after the CDB — chunk sizes, any per-chunk protocol.
3. How incoming data is read (bulk in) — chunking, timeouts.
4. avision_usb_status() (around line 2380): how command completion status is read — bulk vs interrupt endpoint probing logic, AVISION_USB_UNTESTED_STATUS / BULK_STATUS / INT_STATUS state machine, the meaning of status byte values AVISION_USB_GOOD / AVISION_USB_REQUEST_SENSE / AVISION_USB_BUSY (quote their numeric values from the source).
5. REQUEST SENSE handling (around line 2680): exact CDB bytes, sense buffer size, how sense data is interpreted (quote sense-to-error mapping essentials, ASC/ASCQ values that matter e.g. 'no paper in ADF' / NO DOCS).
6. USB endpoints: which endpoints are used and how they are discovered (sanei_usb conventions: first bulk-in, first bulk-out, first interrupt-in of interface 0), USB configuration/interface/altsetting expectations, whether a kernel-detach / set-configuration is done.
7. Timeouts and retry behavior (busy-wait loops, sleep intervals), and the STD_TIMEOUT if defined.
8. Any USB reset / clear-halt behavior on errors.

Include numeric constants VERBATIM (hex) with the source line numbers you took them from. Output the full spec as markdown in the 'spec' field. List anything ambiguous in openQuestions.`, { label: 'research:usb-transport', phase: 'Research', schema: SPEC_SCHEMA, effort: 'high' }),

  () => agent(`You are a senior driver engineer. Read the SANE avision backend source at ${SCRATCH}/avision.c and ${SCRATCH}/avision.h (already downloaded, ~9580 lines — use Grep to locate functions, then Read targeted line ranges).

Target device: Avision AV210C2 sheetfed scanner, USB 0x0638:0x0A3A, model table flags exactly: AV_INT_BUTTON | AV_GRAY_MODES (see ~line 260-265). It has NO AV_FIRMWARE flag, NO other quirk flags.

Produce a BYTE-LEVEL specification of the complete SCAN FLOW for THIS model, sufficient to reimplement in Python without the C source. For each SCSI command give: opcode byte, full CDB layout (every byte), data payload layout (every field with offset, size, endianness), and expected response. Cover exactly:

1. INQUIRY (opcode, CDB, the full inquiry result layout from avision.h and its parsing in avision.c): which fields matter for AV210C2 — max resolution, optical dpi fields, max x/y ranges (inquiry bytes for max scan width/length and their units), color/gray capability bits, ADF presence bit, 'sheetfed' detection, inquiry_asic_type, inquiry_buttons, channels-per-pixel, bits-per-channel, line-difference/color-pack fields, background raster, needs-calibration/software-calibration bits. Give byte offsets verbatim from the source.
2. TEST UNIT READY + wait_ready logic (sleep/retry counts).
3. How the backend decides calibration is needed or skipped for this model (get_calib_format, sheetfed handling — note the AV210C2 has no AV_NO_CALIB flag; trace what actually happens: is calibration attempted for sheetfed scanners? quote the sheetfed/calibration decision logic). If calibration is required, spec the full calibration sequence — but clearly mark which parts are skippable.
4. send_gamma — gamma table format, whether required for this model, the exact table encoding (size, default gamma curve computation).
5. SET WINDOW (opcode 0x24): the EXACT window descriptor layout used by avision (avision.h command_set_window + command_set_window_window) with every byte offset: window ID, x/y resolution, upper-left x/y, width, length, brightness, threshold, contrast, image composition (mode byte values for lineart/gray/color), bit-per-channel, paper length, ADF/duplex/transparency bytes, line-difference, bitset1, bitset2/3 for ADF mode on sheetfed, quality/speed bits, exact values the backend writes for a color 300dpi A4 scan on an AV210-class sheetfed device. Note BASE_RESOLUTION and how coordinates are converted.
6. START SCAN (opcode 0x1B): CDB layout, bitset for quality scan / preview bits.
7. READ IMAGE DATA (opcode 0x28): CDB layout (read type 0x00 image data, units), chunk size strategy, how end-of-page / end-of-document is detected (EOP sense, read returning short, ADF paper-end sense ASC/ASCQ), line-by-line data format for color (RGB packing, line difference / deinterlacing for this ASIC family if any — check inquiry_line_difference and inquiry_color_pack handling in reader_process), padding bytes per line, how bytes_per_line is computed.
8. OBJECT POSITION / media handling for ADF: load/unload paper, how multi-page ADF scanning loops, detecting 'feeder empty' to end a batch.
9. RELEASE/stop: send_cancel or scan end, and any end-of-scan cleanup.
10. Button reading for AV_INT_BUTTON models (see ~line 3939-3960 for AV210C2-specific button handling) — spec it briefly as optional feature.
11. The exact default color mode names and their image composition byte values, plus how gray (AV_GRAY_MODES flag) affects available modes.

Include numeric constants VERBATIM (hex) with source line numbers. Output full spec as markdown in 'spec'. List ambiguities in openQuestions.`, { label: 'research:scan-flow', phase: 'Research', schema: SPEC_SCHEMA, effort: 'high' }),

  () => agent(`You are a Windows/Linux integration engineer. Research CURRENT (2025-2026) facts for bridging a legacy USB scanner (Avision AV210C2, USB 0x0638:0x0A3A, supported as 'complete' by the SANE avision backend) to a Windows 10/11 office PC. Load WebSearch and WebFetch via ToolSearch and verify each fact from real sources (project docs/GitHub). Note: direct fetches to some domains may be blocked by proxy — prefer github.com, learn.microsoft.com, gitlab.com.

Produce a verified facts document covering:

1. usbipd-win: current install method (winget package id), exact CLI syntax of current v4/v5: 'usbipd list', 'usbipd bind --busid', 'usbipd attach --wsl --busid', auto-attach option ('--auto-attach'), whether attach must re-run after replug and workarounds (scheduled task / usbipd policies). Which Windows versions supported. Note: 'usbipd wsl' subcommand was removed in v4.
2. WSL2: enabling on an office PC ('wsl --install -d Ubuntu-24.04'), systemd enablement in /etc/wsl.conf (default on recent WSL), mirrored networking mode (.wslconfig networkingMode=mirrored) — which Windows 11 builds support it and does mDNS from WSL reach the host; fallback: NAT mode with localhostForwarding (default true) so http://localhost:PORT works from Windows.
3. SANE in Ubuntu 24.04: package names (sane-utils, libsane1), whether the avision backend is enabled by default in /etc/sane.d/dll.conf, scanimage -L usage, and udev/group permissions needed when the device arrives via usbip (scanner group, udev rule for 0638:0a3a MODE 0666).
4. AirSane (github.com/SimulPiscator/AirSane): Ubuntu 24.04 package availability or build from source (cmake deps: libsane-dev, libjpeg-dev, libpng-dev, libavahi-client-dev, libusb-1.0-0-dev), web UI port (8090?), eSCL endpoint URL scheme, systemd service installation. Also check 'scanservjs' as a friendlier web UI alternative (default port 8080, install method).
5. NAPS2 on Windows: does current NAPS2 support adding an eSCL scanner manually by URL/host (not just autodiscovered)? Exact steps. NAPS2 download URL.
6. Windows built-in eSCL/AirScan class driver: can Windows 11 add a network scanner by IP for eSCL, or does it require mDNS discovery? Will mirrored networking make an airsane instance in WSL2 discoverable to the Windows host?
7. pyusb on Windows: libusb backend options — the 'libusb-package' pip wheel bundling libusb-1.0.dll vs manual DLL; Zadig (zadig.akeo.ie) to install WinUSB on a specific device; caveat that WinUSB replaces any existing driver; revert via Device Manager.
8. VueScan: confirm it lists AV210C2 support and runs on Windows 10/11 x64 (use search result snippets if the site is blocked).
9. Ubuntu WSL provisioning gotchas: apt noninteractive, building from git honoring HTTPS_PROXY, avahi-daemon inside WSL2 (works with systemd; in mirrored mode host may conflict on port 5353 — note limitation and fallback to direct URL entry in NAPS2/browser).

For every fact give the source URL and quote the key line. Mark anything you could NOT verify as UNVERIFIED with your best-known answer. Output as markdown in 'spec'.`, { label: 'research:windows-bridge', phase: 'Research', schema: SPEC_SCHEMA, effort: 'high' }),
])

if (!transportSpec || !scanFlowSpec) {
  log('Protocol research failed — cannot proceed to build the userspace driver safely.')
  return { error: 'protocol research failed', transportSpec: !!transportSpec, scanFlowSpec: !!scanFlowSpec }
}

log('Research complete. Building the three components in parallel...')
phase('Build')

const windowsFactsSpec = windowsFacts ? windowsFacts.spec : 'UNAVAILABLE — research agent failed; rely on well-known stable knowledge and mark uncertain items clearly in docs.'

const [driverManifest, bridgeManifest, docsManifest] = await parallel([
  () => agent(`You are a senior driver engineer building a production-quality USERSPACE DRIVER in Python for the Avision AV210C2 sheetfed scanner (USB 0x0638:0x0A3A). Create it under ${REPO}/userspace-driver/ . The git repo working tree is at ${REPO} — write files only under userspace-driver/.

You have two authoritative specs extracted from the proven SANE backend (20 years of field testing). FOLLOW THEM EXACTLY — every opcode, offset, and constant. When in doubt, consult the original C source yourself at ${SCRATCH}/avision.c and ${SCRATCH}/avision.h.

=== USB TRANSPORT SPEC ===
${transportSpec.spec}

=== SCAN FLOW SPEC ===
${scanFlowSpec.spec}

Build this structure:
- userspace-driver/av210/__init__.py — version, exports
- userspace-driver/av210/transport.py — libusb transport via pyusb: device discovery by VID/PID (accept the whole AV210 family: 0x0a24, 0x0a25, 0x0a2f, 0x0a3a, 0x1a35), endpoint discovery (first bulk-in/bulk-out/interrupt-in), the avision_cmd framing, status read state machine (bulk-vs-interrupt probing), request-sense on CHECK CONDITION, busy retry loop, timeouts, clear-halt on error. Clean class AvisionTransport with send_cmd(cdb, data_out=None, data_in_len=0) -> bytes.
- userspace-driver/av210/protocol.py — dataclasses + builders for INQUIRY parsing (all fields the spec says matter), SET WINDOW descriptor builder (exact byte layout, taking resolution/mode/geometry), START SCAN, READ IMAGE DATA, TEST UNIT READY + wait_ready, media/object position for ADF, gamma table builder if the spec says the model needs it, sense-data decoder incl. 'feeder empty' detection. All multi-byte fields with correct endianness per spec (Avision set_double/set_triple/set_quad are BIG-endian). Include the calibration path ONLY as far as the spec says this sheetfed model needs it — do not invent.
- userspace-driver/av210/scanner.py — high-level AV210Scanner class: open(), inquiry() -> ScannerInfo, scan_page(resolution, mode) -> assembled PIL Image (handle RGB line format / line-difference deinterlace exactly per spec; gray and lineart too), scan_adf_batch() looping pages until feeder empty, close(). On sense 'no paper' first page -> raise NoPaperError with clear message.
- userspace-driver/av210/cli.py — argparse CLI: 'av210 probe' (find device, print endpoints + full decoded INQUIRY), 'av210 scan -o out.png|out.pdf --resolution 150|200|300|600 --mode color|gray|lineart [--all-pages]' (multi-page -> single PDF via PIL). Friendly errors: device not found -> explain WinUSB/Zadig on Windows, udev on Linux.
- userspace-driver/av210/__main__.py — python -m av210
- userspace-driver/pyproject.toml — package 'av210-scanner-driver', deps: pyusb, pillow, libusb-package (marker: platform_system=="Windows"); entry point av210=av210.cli:main.
- userspace-driver/99-av210.rules — udev rule file for Linux.
- userspace-driver/tests/test_protocol.py — pytest unit tests WITHOUT hardware: CDB builders produce exact expected bytes (assert literal hex derived from the spec), window descriptor golden bytes for 300dpi color A4, inquiry parser against a synthetic inquiry blob, sense decoder cases (good/busy/no-paper), transport framing against a MockUSB device object (simulate bulk write/read + status byte sequences incl. busy retry and request-sense path).
- userspace-driver/README.md — English technical readme: what this is (port of the SANE avision protocol to a portable userspace driver), Windows setup (Zadig -> WinUSB on 0638:0A3A, pip install, av210 probe), Linux setup (udev rule), limitations (no TWAIN/WIA integration — standalone tool; office users may prefer windows-bridge/).

Code quality bar: full type hints, docstrings citing avision.c line numbers for every protocol constant, no dead code, handles partial reads, explicit timeouts. THE TESTS MUST PASS: run 'cd ${REPO}/userspace-driver && python -m pytest tests/ -q' yourself (pip install pyusb pillow pytest first; libusb device won't be present, so modules must import cleanly without hardware and tests must not require real USB). Iterate until green. Return the file list, notes on any spec deviations, and the test command.`, { label: 'build:userspace-driver', phase: 'Build', schema: MANIFEST_SCHEMA, effort: 'high' }),

  () => agent(`You are a Windows/Linux integration engineer. Build the 'windows-bridge' component under ${REPO}/windows-bridge/ — a one-time-setup bridge that makes the Avision AV210C2 (USB 0x0638:0x0A3A) work on a Windows 10/11 office PC using the mature SANE avision driver inside WSL2, exposed to Windows as an eSCL/AirScan network scanner + browser UI.

Verified research facts (trust these; where marked UNVERIFIED, code defensively and comment):
=== FACTS ===
${windowsFactsSpec}

Architecture: usbipd-win attaches the scanner USB device into WSL2 -> Ubuntu in WSL2 runs SANE (avision backend, supports this model as 'complete') -> AirSane exposes it as eSCL + web UI (include scanservjs as optional extra if install is easy) -> user scans from browser at http://localhost:PORT or adds it in NAPS2 as eSCL device; with WSL2 NAT localhost-forwarding this needs zero network config.

Deliverables:
- windows-bridge/install.ps1 — idempotent, run-as-admin guard, clear Hebrew-and-English progress messages (Write-Host both languages). Steps: (1) check Windows version + virtualization; (2) wsl --install Ubuntu-24.04 if missing (handle reboot-required case gracefully: print resume instructions and exit); (3) winget install usbipd; (4) usbipd bind for 0638:0a3a found via 'usbipd list' parsed by VID:PID (fail with clear message if scanner not plugged in); (5) copy setup-wsl.sh into the distro and run it via wsl.exe bash (convert CRLF->LF when transferring!); (6) usbipd attach --wsl --busid <id> --auto-attach as detached process AND register scheduled task attach-on-logon (attach-scanner.ps1); (7) verify: wsl -e scanimage -L shows avision, print final URLs + NAPS2 instructions. Every step try/catch with actionable error text.
- windows-bridge/setup-wsl.sh — runs inside Ubuntu (assume systemd on; enable via /etc/wsl.conf if not and instruct restart): apt-installs SANE + build deps (noninteractive); ensure 'avision' enabled in /etc/sane.d/dll.conf; udev rule 0638:0a3a MODE=0666 (cover PIDs 0a24 0a25 0a3a 0a2f 1a35); build+install AirSane from github (honor proxy env), install its systemd service; final self-test hints. Idempotent. LF line endings.
- windows-bridge/attach-scanner.ps1 — finds busid for 0638:0a3a and attaches to WSL, loops/retries, suitable for scheduled task; logs to %ProgramData%\\ScannerBridge\\attach.log.
- windows-bridge/uninstall.ps1 — unbind usbipd, remove scheduled task, note WSL distro left intact unless -RemoveDistro.
- windows-bridge/README.md — English: ascii architecture diagram, prerequisites, what each script does, troubleshooting table (scanner not in usbipd list / attach fails after replug / scanimage -L empty / avahi vs mirrored networking / firewall).

Constraints: PowerShell 5.1-compatible syntax ONLY (no ternary, no && || chains, no ??), no emoji in .ps1. Validate: bash -n on the .sh; check line endings with 'file'; if pwsh exists run [System.Management.Automation.Language.Parser]::ParseFile on each .ps1, else re-read carefully for balanced braces/quotes. shellcheck setup-wsl.sh if available. Return file list + notes + test command used.`, { label: 'build:windows-bridge', phase: 'Build', schema: MANIFEST_SCHEMA, effort: 'high' }),

  () => agent(`You are a bilingual (Hebrew/English) technical writer for IT solutions. Create user-facing documentation for a project that makes an old Avision AV210C2 sheetfed scanner (USB IDs 0638:0A3A) work on a modern office Windows 10/11 PC. Write under the git repo ${REPO}: the root README.md plus a docs/ directory. Audience: an Israeli office worker (Hebrew native) with basic computer skills, plus an IT person (reads English).

Verified integration facts to draw from:
=== FACTS ===
${windowsFactsSpec}

The repo will contain (being built in parallel — describe them accurately by contract):
1. windows-bridge/ — RECOMMENDED solution: PowerShell installer (install.ps1, run as Administrator) that sets up WSL2 + usbipd-win + SANE avision driver + AirSane web scanning; after install the user scans from the browser (http://localhost:8090) or via NAPS2 (add eSCL scanner). Uses the mature open-source Avision driver trusted for 20 years.
2. userspace-driver/ — ADVANCED: standalone portable userspace driver (Python, libusb/WinUSB via Zadig) with CLI: 'av210 probe', 'av210 scan -o file.pdf --resolution 300 --mode color --all-pages'. For power users / diagnostics / Linux & Mac.
3. Zero-install commercial fallback: VueScan (hamrick.com) supports this exact model on Win 10/11 with its own built-in driver — free trial with watermark to validate hardware health, one-time license.

Deliverables:
- ${REPO}/README.md — HEBREW-FIRST (RTL-friendly markdown: keep code blocks/paths LTR), with an English summary section at the bottom. Structure: מה הבעיה (הסורק ישן, אין דרייבר רשמי ל-Windows 10/11) / שלושת הפתרונות בטבלת השוואה (קלות התקנה, עלות, למי מתאים) / התחלה מהירה לכל פתרון / קישורים ל-docs. Honest framing: הפתרון המומלץ עוטף את הדרייבר הפתוח המוכח; לא בוצעה בדיקה מול חומרה אמיתית בסביבת הפיתוח — יש כלי אבחון ('av210 probe') ותהליך אימות מסודר.
- docs/QUICKSTART.he.md — מדריך צעד-אחר-צעד בעברית להתקנת windows-bridge: דרישות קדם (Windows 10 2004+/11, הרשאות אדמין, חיבור הסורק ישירות ל-USB וספק 24V), הרצת install.ps1 (איך פותחים PowerShell כמנהל, Set-ExecutionPolicy Bypass -Scope Process), מה רואים בכל שלב, איך סורקים מהדפדפן, איך מוסיפים ב-NAPS2 (עם ה-URL המדויק), ואיך סורקים מ'סריקה' של Windows אם mDNS זמין.
- docs/TROUBLESHOOTING.he.md — טבלת תקלות בעברית: הסורק לא מופיע ב-usbipd list (כבל/יציאה/ספק 24V) / attach נכשל אחרי ניתוק-חיבור / scanimage -L ריק / הדפדפן לא נפתח / NAPS2 לא מוצא / פתרון ביניים עם VueScan / איך לאסוף לוגים.
- docs/NAPS2.he.md — מדריך קצר: התקנת NAPS2, הוספת סורק eSCL ידנית, פרופיל סריקה מומלץ (300dpi צבע, PDF), OCR בעברית ב-NAPS2.
- docs/OFFICE-ONEPAGER.he.md — דף אחד להדפסה ליד הסורק: 'איך סורקים' ב-5 שורות + 'אם לא עובד' ב-3 שורות.
- docs/ARCHITECTURE.md — English, for IT: data flow USB -> usbipd -> WSL2 -> SANE avision backend -> AirSane eSCL -> browser/NAPS2; security notes (services bound to localhost; what install.ps1 changes and how to uninstall).

Style: warm, confident, zero fluff, numbered steps, expected-output snippets after each command. NEVER invent UI strings you are unsure of — describe generically ('אשר את חלון בקרת חשבון המשתמש'). Correct Hebrew tech terminology. Return file list + notes.`, { label: 'build:docs-hebrew', phase: 'Build', schema: MANIFEST_SCHEMA, effort: 'high' }),
])

log('Build complete. Running adversarial reviews...')
phase('Review')

const reviews = await parallel([
  () => agent(`You are an adversarial protocol reviewer. The Python userspace driver in ${REPO}/userspace-driver/ claims to implement the Avision AV210C2 scanner protocol EXACTLY as the proven SANE backend does. Your job: try to REFUTE that claim. Compare the Python code byte-by-byte against the ORIGINAL C source at ${SCRATCH}/avision.c and ${SCRATCH}/avision.h (the ground truth — read the actual C code, do not trust comments in the Python).

Hunt specifically for: wrong opcodes; wrong CDB lengths/field offsets; endianness mistakes (set_double/set_triple/set_quad in C are BIG-endian — verify each use); wrong window descriptor size/offsets/values; missing or extra framing bytes in the USB command wrapper; status byte values wrong; request-sense CDB or buffer size wrong; sense key/ASC/ASCQ misinterpretation (especially feeder-empty detection); wrong image composition byte values for color/gray/lineart; bytes_per_line computation errors incl. padding; line-difference/color deinterlacing wrongly applied or wrongly skipped for THIS ASIC family (check what inquiry fields drive it in the C reader_process and whether the Python honors them); calibration steps this sheetfed model actually requires in C but Python skips (or vice versa — trace the C decision path for a device with flags AV_INT_BUTTON|AV_GRAY_MODES only); gamma table required-or-not mismatch; wait_ready/busy retry semantics; ADF object-position/multi-page loop mismatches; endpoint discovery differences vs sanei_usb.

Also run the test suite: cd ${REPO}/userspace-driver && python -m pytest tests/ -q (pip install pyusb pillow pytest if needed) and report failures as critical findings.

For EVERY finding provide: file, line, severity (critical = would definitely break scanning on real hardware; major = likely breaks some mode/path; minor = robustness/clarity), a VERBATIM evidence quote from avision.c with its line number, and a concrete suggestedFix. Verify each candidate finding against the C source before reporting — no speculation. Report only findings that survive your own verification.`, { label: 'review:protocol-vs-C', phase: 'Review', schema: FINDINGS_SCHEMA, effort: 'high' }),

  () => agent(`You are an adversarial reviewer of Windows/Linux ops scripts. Review ${REPO}/windows-bridge/ (install.ps1, setup-wsl.sh, attach-scanner.ps1, uninstall.ps1, README.md). Try to find ways these scripts FAIL on a real Windows 10/11 office PC.

Hunt for: PowerShell syntax errors or PS7-only syntax that breaks PS 5.1 (ternary, &&/|| chains, ?? operator); wrong usbipd CLI syntax for current v4/v5 (verify via web if needed — 'usbipd wsl' subcommand is REMOVED in v4+, must be 'usbipd bind/attach --wsl'); fragile parsing of 'usbipd list' output; missing admin/elevation checks; wsl.exe invocation quoting bugs; CRLF line endings breaking setup-wsl.sh inside Linux (check the actual file with 'file' command AND check how install.ps1 transfers it); apt packages that don't exist in Ubuntu 24.04; AirSane build steps missing a dependency; udev rule syntax errors; systemd service assumptions when systemd is off in WSL; install flow deadlocking when wsl --install requires reboot; scheduled task registration syntax; idempotency violations; uninstall leaving the system broken. Run 'bash -n' on the .sh; if pwsh exists use its Parser to validate .ps1 files; check line endings with 'file'.

For each finding: file, line, severity (critical = install fails or system left broken; major = a documented flow doesn't work; minor = UX/robustness), evidence (quote the broken line + why, with authoritative source if CLI-syntax related), suggestedFix. Verify before reporting.`, { label: 'review:bridge-scripts', phase: 'Review', schema: FINDINGS_SCHEMA, effort: 'high' }),

  () => agent(`You are a completeness-and-accuracy critic reviewing documentation for a scanner-compatibility project. Review ${REPO}/README.md and ${REPO}/docs/*.md against the ACTUAL code in ${REPO}/windows-bridge/ and ${REPO}/userspace-driver/ (read the scripts/CLI source to verify every claim).

Hunt for: commands or flags in docs that don't match the actual scripts (e.g. docs say port 8090 but airsane config in setup-wsl.sh uses another; docs say 'av210 scan --all-pages' but the CLI flag is named differently; install.ps1 parameter names); steps in wrong order; missing prerequisite warnings (reboot-required path, ExecutionPolicy, 24V power supply); Hebrew errors or awkward machine-translation phrasing (you are a native-level Hebrew reviewer — flag anything a native would not write, wrong gender agreement, anglicisms); RTL/LTR markdown issues (code spans inside Hebrew lines that render broken); broken internal links; claims that overpromise (anything implying it was tested on real hardware — honest framing: protocol ported from the proven open-source driver + diagnostics provided, hardware validation pending); missing uninstall documentation; the one-pager longer than one page.

For each finding: file, line, severity, evidence, suggestedFix (for Hebrew fixes give the corrected Hebrew sentence verbatim). Verify each against the actual code before reporting.`, { label: 'review:docs-consistency', phase: 'Review', schema: FINDINGS_SCHEMA, effort: 'high' }),
])

const [protoReview, bridgeReview, docsReview] = reviews.map(r => r || { findings: [] })
const totalFindings = protoReview.findings.length + bridgeReview.findings.length + docsReview.findings.length
log(`Reviews done: ${protoReview.findings.length} protocol, ${bridgeReview.findings.length} scripts, ${docsReview.findings.length} docs findings. Applying fixes...`)

phase('Fix')

const fixJobs = []
if (protoReview.findings.length) {
  fixJobs.push(() => agent(`You are the maintainer of the Python userspace driver at ${REPO}/userspace-driver/. An adversarial review against the original C source (${SCRATCH}/avision.c, ${SCRATCH}/avision.h) produced these verified findings. Apply ALL critical and major fixes, and minor ones unless they conflict. For each fix, verify against the C source yourself before editing. After edits, run: cd ${REPO}/userspace-driver && python -m pytest tests/ -q — update golden test bytes only when the fix legitimately changes them (justify each golden change against the C source). Iterate until tests pass.

FINDINGS:
${JSON.stringify(protoReview.findings, null, 2)}

Return files changed, notes on any finding you rejected (with C-source evidence), and the final test result.`, { label: 'fix:driver', phase: 'Fix', schema: MANIFEST_SCHEMA, effort: 'high' }))
}
if (bridgeReview.findings.length) {
  fixJobs.push(() => agent(`You maintain ${REPO}/windows-bridge/. Apply ALL the verified review findings below (critical+major mandatory, minor unless conflicting). Re-validate after editing: bash -n on .sh, check line endings ('file'), keep .ps1 PS5.1-compatible (pwsh Parser if available).

FINDINGS:
${JSON.stringify(bridgeReview.findings, null, 2)}

Return files changed + notes on any rejected finding with evidence.`, { label: 'fix:bridge', phase: 'Fix', schema: MANIFEST_SCHEMA, effort: 'high' }))
}
if (docsReview.findings.length) {
  fixJobs.push(() => agent(`You maintain the docs: ${REPO}/README.md and ${REPO}/docs/. Apply ALL the verified review findings below. Where a doc-vs-code mismatch exists, FIX THE DOC to match the code (do not edit code). Keep Hebrew natural and technically precise.

FINDINGS:
${JSON.stringify(docsReview.findings, null, 2)}

Return files changed + notes on any rejected finding.`, { label: 'fix:docs', phase: 'Fix', schema: MANIFEST_SCHEMA, effort: 'high' }))
}

const fixResults = fixJobs.length ? await parallel(fixJobs) : []

return {
  research: {
    transportOpenQuestions: transportSpec.openQuestions || [],
    scanFlowOpenQuestions: scanFlowSpec.openQuestions || [],
    windowsFactsAvailable: !!windowsFacts,
  },
  build: {
    driver: driverManifest ? { files: driverManifest.files, notes: driverManifest.notes, testCommand: driverManifest.testCommand } : null,
    bridge: bridgeManifest ? { files: bridgeManifest.files, notes: bridgeManifest.notes } : null,
    docs: docsManifest ? { files: docsManifest.files, notes: docsManifest.notes } : null,
  },
  review: {
    protocolFindings: protoReview.findings.length,
    bridgeFindings: bridgeReview.findings.length,
    docsFindings: docsReview.findings.length,
    totalFindings,
    criticals: [...protoReview.findings, ...bridgeReview.findings, ...docsReview.findings].filter(f => f.severity === 'critical').map(f => f.file + ': ' + f.summary),
  },
  fixes: fixResults.filter(Boolean).map(r => ({ notes: r.notes, files: r.files })),
}
```

**args שהועברו לסקריפט:**
```json
{
  "scratch": "/tmp/claude-0/-home-user-Nsabag/95824325-0a8d-5ca6-b0be-1cb750721c6d/scratchpad",
  "repo": "/home/user/Nsabag"
}
```

**תוצאה מסוכם:** 12 סוכנים רצו (4 שלבים), ~1.3 מיליון טוקנים, 417 קריאות כלים,
28 ממצאי ביקורת (9 בפרוטוקול, 7 בסקריפטים, 12 בתיעוד) — כולם תוקנו. ממצא
קריטי אחד בפרוטוקול: `send_cmd` בתעבורה השמיט את מטען הנתונים המלא כשבייט
הסטטוס הסופי מיפה ל-sense של EOF/no-docs, מה שגרם לדרייבר Python להשמיט
בשקט רצועת תמונה שלמה (עד 32 שורות, ~2.7 מ"מ ב-300dpi) מתחתית כל דף שנסרק
מהמזין.

---

## 2. פרומפט סוכן מחקר: "Verify AirSane options.conf syntax"

נשלח (ברקע, `run_in_background: true`) לאחר שהתגלה כשל
`libjpeg error: Maximum supported image dimension is 65500 pixels, aborting`
בזמן סריקה מול חומרה אמיתית, ותיקון ראשוני ב-`options.conf` לא עזר.

```
Research task (use ToolSearch to load WebSearch/WebFetch; prefer github.com raw files — other domains may be blocked by proxy).

Context: AirSane (github.com/SimulPiscator/AirSane) commit ~129cc3b rev 328 serving a SANE 'avision' backend sheetfed scanner (AV210C2). Every eSCL scan job fails at encode time with "libjpeg error: Maximum supported image dimension is 65500 pixels, aborting" because the avision backend reports unbounded page height for the ADF. We appended to /etc/airsane/options.conf:

device avision.*
br-y 295

and restarted airsaned, but eSCL scans (from NAPS2, requesting A4 JPEG) still fail with empty response.

Verify from the ACTUAL AirSane source/README (fetch https://raw.githubusercontent.com/SimulPiscator/AirSane/master/README.md and relevant source files like server/scanner.cpp, sanecpp/*.cpp, or wherever options.conf is parsed):

1. The EXACT syntax of /etc/airsane/options.conf: how device sections are declared (keyword? regex against what string — SANE device name like 'avision:libusb:001:008', or make/model?), and how option lines are written (option name = SANE option name? values with units?).
2. Whether options.conf options are applied to eSCL-initiated scan jobs or only to web-UI scans.
3. How AirSane maps the eSCL job's region (A4 from the client) to SANE window options (tl-x/tl-y/br-x/br-y) — does it set br-y itself, and in what units? Why might a huge/unbounded image still be delivered for a sheetfed avision device (e.g., height quantization, 'page-height' option, backend returning lines until EOF regardless of br-y)?
4. Any known GitHub issues in SimulPiscator/AirSane mentioning 'Maximum supported image dimension', '65500', 'avision', or unbounded page height with sheetfed ADFs — and the recommended fixes/workarounds (search issues via WebSearch: site:github.com SimulPiscator/AirSane 65500 OR "Maximum supported image dimension").
5. Whether AirSane supports PNG or PDF output for eSCL jobs (DocumentFormat image/png, application/pdf) that would bypass the libjpeg 65500 limit, and whether NAPS2 can request PDF format from eSCL.

Return a concise factual report with: correct options.conf content for our case (exact lines), whether our 'device avision.*' section syntax is right or wrong (and the corrected version), any better workaround, and source URLs/quotes for each claim. If a claim cannot be verified, say UNVERIFIED.
```

---

## 3. פרומפט סוכן המשך: "Continue agent to build AirSane patch"

נשלח כהמשך לאותו סוכן (`SendMessage` לאותו agentId), לאחר שהמחקר אישר את
שורש הבעיה, כדי לכתוב וגם **לקמפל בפועל** את התיקון.

```
Follow-up to your AirSane research (you have the clone at /tmp/claude-0/-home-user-Nsabag/95824325-0a8d-5ca6-b0be-1cb750721c6d/scratchpad/AirSane at commit 129cc3b).

Produce the patch you proposed: make AirSane handle SANE backends that report lines = -1 (unknown page height, e.g. the avision sheetfed backend), so eSCL JPEG/PNG/PDF jobs succeed.

Requirements:
1. Edit server/scanjob.cpp (and only what's strictly needed):
   a. In finishTransfer where pEncoder->setHeight(p->lines) is called: when p->lines < 0, fall back to the requested region height in pixels (mHeight_px, already at scan resolution). Keep a local `int height` used consistently afterwards.
   b. Handle the end-of-page mismatch: when the SANE read loop hits EOF before `height` lines were delivered, pad the remaining lines with white (0xFF bytes for gray/RGB; think about what byte value is white for the actual formats AirSane feeds encoders) so the encoder receives exactly `height` lines and the 'incomplete or excess scan data' check at ~line 779 does not abort. If more lines than `height` arrive, stop feeding at `height` (drain and discard the rest).
   c. Guard so behavior for backends with known height (lines >= 0) is completely unchanged.
2. Read the surrounding code carefully (the read loop, buffer handling, bytesPerLine, encoder API) so the patch compiles and is logically airtight - do not guess field names.
3. Try to compile: check if cmake and the dev packages are available (cmake, g++, libsane-dev, libjpeg-dev, libpng-dev, libavahi-client-dev, libusb-1.0-0-dev). apt-get may work through the proxy (sudo apt-get update && apt-get install -y ...). If you can build, run cmake+make on the patched tree and fix any compile errors. If apt/network fails, do a rigorous manual review pass instead and say so.
4. Deliverable: write the final patch as a unified diff to /tmp/claude-0/-home-user-Nsabag/95824325-0a8d-5ca6-b0be-1cb750721c6d/scratchpad/airsane-unknown-height.patch , generated with git diff from the clone, and VERIFY it applies cleanly to a pristine checkout with `git -C <fresh-clone-or-stash-workflow> apply --check`. Keep the patch minimal (single file if possible) and include 3 lines of context (default).
5. Return: the full patch text, whether it compiled (or review-only), any risks, and the exact marker string I can grep for in the patched source to detect an already-patched tree (e.g. a distinctive comment you add, like /* airsane-unknown-height-patch v1 */).
```

**התוצאה שהוחזרה (מלאה, מילה במילה) מהסוכן הזה** נכללת בקובץ
`04-transcript-part3-installation-saga.md` בסעיף התגלית של הבאג, כולל הטלאי
המלא כ-unified diff.
