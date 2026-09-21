#!/usr/bin/env bash
#
# apply_patch.sh — fully automatic build of the patched MediaSDK run tree.
#
# What it does:
#   1. Auto-finds the SDK archive in ./sdk/ (a .zip such as
#      "Linux_CameraSDK-2.1.8_MediaSDK-3.1.5.zip", or an already-extracted
#      MediaSDK-*.tar_*.gz, or an already-installed sdk/install/opt/... tree).
#   2. Extracts it (zip -> MediaSDK-*-linux64.tar_*.gz -> MediaSDK-*.deb ->
#      data.tar.gz -> ./opt/MediaSDK-*-linux/{bin,lib,models}), caching work
#      under sdk/work/.
#   3. Finds the original libMediaSDK.so and checks its version signature
#      (ELF + size + algorithmic anchors) against the tested 3.1.5 build.
#   4. Applies the ALGORITHMIC adaptation via libmedia_adapt.py (self-contained,
#      no data file: hides vk* symbols in .dynstr, renames nvenc->amf strings,
#      redirects the encoder branch) to a copy ->
#      sdk/work/libMediaSDK_patched.so, verifies the result with a sanity check
#      and `libmedia_adapt.py check`.
#   5. Assembles the FULL patched/ run tree (bin/MediaSDKTest,
#      bin/RealTimeStitcherSDKTest, bin/models/*, lib/*) and overwrites
#      lib/libMediaSDK.so with the patched one.
#   6. Optionally runs `make` to build the LD_PRELOAD shims (shim/*.so).
#
# Modes:
#   (default)  full automatic build (extract -> patch -> assemble).
#   --check    verify presence/version recognition only (no writes).
#   --force    redo extraction/assembly even if the cache is valid.
#   --regenerate  obsolete (the adaptation is algorithmic and self-contained;
#              there is no data file to regenerate). Accepted with a notice,
#              then runs as a normal build.
#
# Environment overrides:
#   SRC          original libMediaSDK.so (if set, skips auto-location)
#
# Output messages are in English (this tool targets a naive user).
# Code comments are in English.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER="$SCRIPT_DIR/libmedia_adapt.py"
DST="$SCRIPT_DIR/sdk/work/libMediaSDK_patched.so"
SDK_DIR="$SCRIPT_DIR/sdk"
WORK_DIR="$SDK_DIR/work"
PATCHED_DIR="$SCRIPT_DIR/patched"

MODE="apply"
FORCE=""
REGEN_NOTICE=""
for a in "$@"; do
    case "$a" in
        --check) MODE="check" ;;
        --regenerate) REGEN_NOTICE="1" ;;
        --force) FORCE="1" ;;
    esac
done

err() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "==> $*"; }

# ---------- tool checks ----------
need() { command -v "$1" >/dev/null 2>&1 || err "Tool '$1' not found. Install it (Arch: sudo pacman -S $2; Ubuntu/Debian: sudo apt install $2; Fedora: sudo dnf install $2)."; }
need python3 python3
need tar tar
need ar binutils

# ---------- sanity / version helpers ----------
# sanity_check <file>: file exists, size > 1MB, ELF magic intact, and (if
# $EXPECTED_SIZE is set) size matches. Returns 0 = OK, 1 = broken.
EXPECTED_SIZE="${EXPECTED_SIZE:-163484088}"   # tested 3.1.5 lib size (bytes)
sanity_check() {
    local f="$1"
    [ -f "$f" ] || return 1
    local sz
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    [ "$sz" -gt 1048576 ] || return 1
    local magic
    magic=$(head -c 4 "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')
    [ "$magic" = "7f454c46" ] || return 1
    if [ -n "$EXPECTED_SIZE" ] && [ "$sz" != "$EXPECTED_SIZE" ]; then
        return 1
    fi
    return 0
}

# version_guard <file>: recognize a compatible SDK build algorithmically.
# Checks ELF magic, a tolerant size range (150-175 MB), and that the ORIGINAL
# (unpatched) binary contains the anchors libmedia_adapt.py relies on: the
# 22-byte branch signature, both nvenc encoder strings, and >=160 vk* symbols
# in .dynstr. Returns 0 = recognized, 1 = unrecognized (hard error for the
# caller to report).
version_guard() {
    local f="$1"
    [ -f "$f" ] || return 1
    local sz magic
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    magic=$(head -c 4 "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')
    [ "$magic" = "7f454c46" ] || return 1
    if [ "$sz" -lt 157286400 ] || [ "$sz" -gt 183500800 ]; then
        return 1
    fi
    # Anchor-presence check (reuses libmedia_adapt.py logic as a module).
    # Exits 0 if the original anchors are found, 1 otherwise.
    python3 - "$f" "$ADAPTER" <<'PYEOF' 2>/dev/null
import sys, importlib.util

bin_path, adapter_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("la", adapter_path)
la = importlib.util.module_from_spec(spec)
spec.loader.exec_module(la)

data = open(bin_path, "rb").read()

# Anchor 1: branch signature present (unpatched variant).
try:
    state, _pos = la.branch_state(data)
    branch_ok = (state == "unpatched")
except ValueError:
    branch_ok = False

# Anchor 2: both nvenc encoder strings present exactly once.
str_ok = all(data.count(old) == 1 for old, _new in la.ENCODER_RENAMES)

# Anchor 3: enough vk* symbols in .dynstr.
dynstr = la.find_dynstr(data)
vk_count = len(la.vk_hide_spots(data, dynstr))

ok = branch_ok and str_ok and vk_count >= 160
sys.exit(0 if ok else 1)
PYEOF
}

# patched_marker: file touched after a successful assembly (idempotency).
PATCHED_MARKER="$PATCHED_DIR/.built"

# ---------- --check mode: verify presence/version recognition only (no writes) ----------
if [ "$MODE" = "check" ]; then
    echo "==> --check mode (verification only, nothing is written)"
    SRC="${SRC:-}"
    if [ -z "$SRC" ]; then
        # auto-locate original for check purposes (inline, independent of
        # function definition order)
        SRC="$(find "$SDK_DIR/install" "$SDK_DIR/work" "$SDK_DIR/extracted" -name 'libMediaSDK.so' -type f 2>/dev/null | head -1 || true)"
    fi
    if [ -n "$SRC" ] && [ -f "$SRC" ]; then
        if sanity_check "$SRC"; then
            if version_guard "$SRC"; then
                echo "==> original libMediaSDK.so: $SRC  [version recognized (compatible with the tested 3.1.5)]"
            else
                echo "==> original libMediaSDK.so: $SRC  [version not recognized — expected signatures not found]" >&2
                exit 1
            fi
        else
            echo "==> original libMediaSDK.so: $SRC  [file corrupted or not an ELF]" >&2
            exit 1
        fi
    else
        echo "==> original libMediaSDK.so: (not found; put the SDK archive into ./sdk/)" >&2
        exit 1
    fi
    if [ -f "$PATCHED_MARKER" ] && [ -x "$PATCHED_DIR/bin/MediaSDKTest" ] && sanity_check "$PATCHED_DIR/lib/libMediaSDK.so"; then
        echo "==> patched/: built (marker + sanity OK, adaptation applied)"
    else
        echo "==> patched/: not built (run ./apply_patch.sh)"
    fi
    echo "==> --check OK"
    exit 0
fi

# ---------- --regenerate is obsolete: friendly notice, continue as normal ----------
if [ -n "$REGEN_NOTICE" ]; then
    info "--regenerate is no longer needed: the adaptation is algorithmic (libmedia_adapt.py scans your SDK on the fly). Continuing with a normal build."
fi

# ---------- idempotency: already built? ----------
already_built() {
    [ -f "$PATCHED_MARKER" ] || return 1
    [ -x "$PATCHED_DIR/bin/MediaSDKTest" ] || return 1
    [ -d "$PATCHED_DIR/bin/models" ] && [ -n "$(ls -A "$PATCHED_DIR/bin/models" 2>/dev/null)" ] || return 1
    sanity_check "$PATCHED_DIR/lib/libMediaSDK.so" || return 1
    return 0
}
if [ "$MODE" = "apply" ] && [ -z "$FORCE" ] && already_built; then
    echo "==> Already built: patched/ contains a patched MediaSDK (marker + sanity OK)."
    echo "    Use --force to rebuild."
    exit 0
fi

# ---------- auto-locate the original libMediaSDK.so ----------
# find_original: prints a path to the ORIGINAL libMediaSDK.so, or nothing.
find_original() {
    # 1) SRC env override (already handled by caller if set)
    # 2) already-installed tree: sdk/install/opt/.../lib/libMediaSDK.so
    local f
    f=$(find "$SDK_DIR/install" -name 'libMediaSDK.so' -type f 2>/dev/null | head -1)
    [ -n "$f" ] && { echo "$f"; return 0; }
    # 3) extracted tree under sdk/work or sdk/extracted
    f=$(find "$SDK_DIR/work" "$SDK_DIR/extracted" -name 'libMediaSDK.so' -type f 2>/dev/null | head -1)
    [ -n "$f" ] && { echo "$f"; return 0; }
    return 1
}

# ---------- archive detection ----------
detect_archive() {
    # Prefer a .zip (the vendor's "Linux CameraSDK + MediaSDK" bundle), then
    # a MediaSDK tarball. Return a single path or empty.
    local zips=() tars=()
    # shellcheck disable=SC2206
    zips=( "$SDK_DIR"/*.zip )
    # shellcheck disable=SC2206
    tars=( "$SDK_DIR"/*MediaSDK*.tar_*.gz "$SDK_DIR"/*MediaSDK*.tar.gz )
    local cand=()
    [ -e "${zips[0]:-}" ] && cand+=( "${zips[@]}" )
    for t in "${tars[@]}"; do
        [ -e "$t" ] && cand+=( "$t" )
    done
    # filter to files
    local files=()
    for c in "${cand[@]}"; do
        [ -f "$c" ] && files+=( "$c" )
    done
    [ "${#files[@]}" -eq 0 ] && return 1
    [ "${#files[@]}" -eq 1 ] && { echo "${files[0]}"; return 0; }
    # multiple: prefer one whose name contains "MediaSDK" and no "CameraSDK"
    local pref=""
    for c in "${files[@]}"; do
        case "$(basename "$c")" in
            *MediaSDK*|*mediasdk*) pref="$c"; break ;;
        esac
    done
    if [ -n "$pref" ]; then
        echo "$pref"
        return 0
    fi
    err "MULTIPLE ARCHIVES FOUND IN ./sdk/. Keep a single archive (or the one containing MediaSDK) and re-run."
}

# ---------- disk space check ----------
check_space() {
    local need_mb=6000
    local avail
    avail=$(df -Pk "$SCRIPT_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
    [ -n "$avail" ] && [ "$avail" -lt "$((need_mb * 1024))" ] && \
        err "NOT ENOUGH DISK SPACE: ~6 GB needed for the build (archive extraction + patched/). Free now: $((avail / 1024)) MB."
    return 0
}

# ---------- extraction ----------
# extract_media_tarball <tarball> <dest_dir>
#   tarball is a MediaSDK-*-linux64.tar_*.gz containing a .deb.
#   Extracts the .deb, then data.tar.gz -> dest_dir/opt/MediaSDK-*-linux/...
extract_media_tarball() {
    local tb="$1" dest="$2"
    local deb
    info "Extracting tarball: $(basename "$tb")"
    deb=$(tar tzf "$tb" 2>/dev/null | grep -E '\.deb$' | head -1 || true)
    if [ -z "$deb" ]; then
        # maybe the tarball is already the installed tree (no deb)
        info "  (no .deb inside tarball — trying as a ready tree)"
        tar xzf "$tb" -C "$dest" 2>/dev/null || return 1
        return 0
    fi
    tar xzf "$tb" -C "$dest" "$deb" 2>/dev/null || return 1
    local debpath="$dest/$deb"
    [ -f "$debpath" ] || return 1
    info "  found .deb: $(basename "$debpath")"
    # extract data.tar.gz from the deb
    if command -v ar >/dev/null 2>&1; then
        ( cd "$dest" && ar p "$debpath" data.tar.gz 2>/dev/null | tar xz 2>/dev/null ) || return 1
    else
        # fallback: dpkg-deb -x
        need dpkg-deb dpkg
        dpkg-deb -x "$debpath" "$dest" 2>/dev/null || return 1
    fi
    rm -f "$debpath"
    return 0
}

# extract_archive <archive> <dest_dir>
#   Handles .zip (vendor bundle) and .tar_*.gz / .tar.gz.
extract_archive() {
    local arc="$1" dest="$2"
    mkdir -p "$dest"
    case "$(basename "$arc")" in
        *.zip)
            need unzip unzip
            info "Extracting zip: $(basename "$arc")"
            unzip -q -o "$arc" -d "$dest" 2>/dev/null || return 1
            # find the MediaSDK tarball inside the zip
            local tb
            tb=$(find "$dest" -type f \( -name 'MediaSDK-*.tar_*.gz' -o -name 'MediaSDK-*.tar.gz' \) 2>/dev/null | head -1)
            if [ -z "$tb" ]; then
                err "FILE IS NOT AN INSTA360 SDK PACKAGE. No MediaSDK-*-linux64.tar_*.gz found inside the archive. Make sure it is the 'Linux CameraSDK + MediaSDK' archive from the Insta360 site."
            fi
            extract_media_tarball "$tb" "$dest" || return 1
            ;;
        *.tar.gz|*.tar_*.gz)
            extract_media_tarball "$arc" "$dest" || return 1
            ;;
        *)
            err "Unsupported archive type: $(basename "$arc")"
            ;;
    esac
    # strip junk
    find "$dest" -type d -name '__MACOSX' -exec rm -rf {} + 2>/dev/null || true
    find "$dest" -name '.DS_Store' -delete 2>/dev/null || true
    return 0
}

# ---------- main flow ----------
check_space

# If SRC is not set, try to auto-locate or auto-extract.
SRC="${SRC:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
    SRC="$(find_original || true)"
fi

if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
    # No extracted original -> need the archive.
    if [ ! -d "$SDK_DIR" ]; then
        err "SDK ARCHIVE NOT FOUND. Put the Linux_CameraSDK-*.zip (or an extracted MediaSDK-*.tar.gz) into ./sdk/ and re-run. The archive can be downloaded here: https://www.insta360.com/sdk/apply"
    fi
    ARC="$(detect_archive || true)"
    if [ -z "$ARC" ]; then
        err "SDK ARCHIVE NOT FOUND. Put the Linux_CameraSDK-*.zip (or an extracted MediaSDK-*.tar.gz) into ./sdk/ and re-run. The archive can be downloaded here: https://www.insta360.com/sdk/apply"
    fi
    info "Found archive: $ARC"
    mkdir -p "$WORK_DIR"
    if [ -z "$FORCE" ]; then
        # cached original?
        cached=$(find "$WORK_DIR" -name 'libMediaSDK.so' -type f 2>/dev/null | head -1 || true)
        if [ -n "$cached" ] && sanity_check "$cached"; then
            info "Using extraction cache: $cached (sanity OK)"
            SRC="$cached"
        fi
    fi
    if [ -z "$SRC" ]; then
        info "Extracting archive (may take several minutes)..."
        extract_archive "$ARC" "$WORK_DIR" || err "Failed to extract archive: $ARC"
        SRC="$(find "$WORK_DIR" -name 'libMediaSDK.so' -type f 2>/dev/null | head -1 || true)"
    fi
fi

if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
    err "libMediaSDK.so NOT FOUND INSIDE THE ARCHIVE. The archive seems corrupted or is not the Linux version of the SDK."
fi
SRC="$(realpath "$SRC")"
info "Original libMediaSDK.so: $SRC"

# ---------- version guard (apply mode) ----------
if ! sanity_check "$SRC"; then
    err "File libMediaSDK.so is corrupted or not an ELF: $SRC"
fi
if ! version_guard "$SRC"; then
    err "SDK VERSION NOT RECOGNIZED (expected signatures not found): $SRC. Expected Linux CameraSDK + MediaSDK 3.x (e.g. 3.1.5). If this is another version, the adaptation may not fit."
fi
info "Version/signature of libMediaSDK.so recognized (compatible with the tested 3.1.5)."

# ---------- apply the algorithmic adaptation (libmedia_adapt.py) ----------
mkdir -p "$(dirname "$DST")"
echo "==> Original:    $SRC"
echo "==> Output:      $DST"
python3 "$ADAPTER" apply --bin "$SRC" --out "$DST"

# ---------- verify result (sanity, no md5 pinning) ----------
if ! sanity_check "$DST"; then
    err "Patched result corrupted or not an ELF: $DST"
fi
if cmp -s "$SRC" "$DST"; then
    err "Patched result is identical to the original — the adaptation did not apply (possibly an incompatible SDK version)."
fi
if ! python3 "$ADAPTER" check --bin "$DST" >/dev/null 2>&1; then
    err "Patched result failed verification (adaptation not applied): $DST"
fi
info "Patched result: sanity OK, differs from the original, adaptation applied."

# ---------- assemble patched/ run tree ----------
# Source tree = the extracted opt/MediaSDK-*-linux dir that contains our SRC.
SDK_OPT="$(dirname "$(dirname "$SRC")")"   # .../opt/MediaSDK-*-linux (parent of lib/)
if [ ! -d "$SDK_OPT/bin" ] || [ ! -d "$SDK_OPT/lib" ]; then
    # fall back: search for the opt dir that has bin+lib
    SDK_OPT="$(find "$(dirname "$SRC")" -maxdepth 4 -type d -name 'MediaSDK-*-linux' 2>/dev/null | head -1 || true)"
fi
if [ -z "$SDK_OPT" ] || [ ! -d "$SDK_OPT/bin" ] || [ ! -d "$SDK_OPT/lib" ]; then
    err "SDK tree (opt/MediaSDK-*-linux with bin/ and lib/) not found next to the original. Extract the full archive."
fi
info "SDK tree: $SDK_OPT"

mkdir -p "$PATCHED_DIR/bin" "$PATCHED_DIR/lib"
info "Copying bin/ (MediaSDKTest, RealTimeStitcherSDKTest)..."
cp -a "$SDK_OPT/bin/MediaSDKTest" "$SDK_OPT/bin/RealTimeStitcherSDKTest" "$PATCHED_DIR/bin/" 2>/dev/null || \
    err "Failed to copy executables from bin/."
info "Copying models/..."
if [ -d "$SDK_OPT/bin/models" ]; then
    cp -a "$SDK_OPT/bin/models" "$PATCHED_DIR/bin/"
else
    # models may be at opt/MediaSDK-*-linux/models (some builds)
    if [ -d "$SDK_OPT/models" ]; then
        cp -a "$SDK_OPT/models" "$PATCHED_DIR/bin/"
    else
        warn "models/ directory not found — MediaSDKTest may not work without models."
    fi
fi
info "Copying lib/ (this takes a while, ~4 GB)..."
cp -a "$SDK_OPT"/lib/. "$PATCHED_DIR/lib/" 2>/dev/null || err "Failed to copy lib/."
info "Replacing lib/libMediaSDK.so with the patched one..."
cp -f "$DST" "$PATCHED_DIR/lib/libMediaSDK.so"

# ---------- final verification ----------
ok=1
if ! sanity_check "$PATCHED_DIR/lib/libMediaSDK.so"; then
    warn "patched/lib/libMediaSDK.so is corrupted or not an ELF"
    ok=0
fi
[ -x "$PATCHED_DIR/bin/MediaSDKTest" ] || { warn "patched/bin/MediaSDKTest missing or not executable"; ok=0; }
[ -d "$PATCHED_DIR/bin/models" ] && [ -n "$(ls -A "$PATCHED_DIR/bin/models" 2>/dev/null)" ] || { warn "patched/bin/models/ empty or missing"; ok=0; }
libcount=$(find "$PATCHED_DIR/lib" -maxdepth 1 -type f -o -type l 2>/dev/null | wc -l)
[ "$libcount" -gt 0 ] || { warn "patched/lib/ empty"; ok=0; }

# idempotency marker: touched only on a successful assembly
if [ "$ok" = "1" ]; then
    touch "$PATCHED_MARKER"
fi

# ---------- optional: build shims ----------
if command -v make >/dev/null 2>&1; then
    info "Building LD_PRELOAD shims (make)..."
    ( cd "$SCRIPT_DIR" && make >/dev/null 2>&1 ) || \
        warn "FAILED TO BUILD SHIMS (make). Install gcc and make (Arch: sudo pacman -S base-devel; Ubuntu: sudo apt install build-essential; Fedora: sudo dnf install gcc make)."
else
    warn "make not found — shims not built. Install gcc and make (Arch: sudo pacman -S base-devel; Ubuntu: sudo apt install build-essential; Fedora: sudo dnf install gcc make)."
fi

# ---------- summary ----------
echo
echo "======================================================================"
if [ "$ok" = "1" ]; then
    echo "DONE! Everything is assembled: patched/ contains the patched MediaSDK."
    echo "Run: ./convert_tui.py --src <folder> --dst <folder>"
else
    echo "BUILD FINISHED WITH WARNINGS — check the messages above."
fi
echo "  patched/bin/MediaSDKTest           : $([ -x "$PATCHED_DIR/bin/MediaSDKTest" ] && echo OK || echo PROBLEM)"
echo "  patched/bin/models/                : $([ -d "$PATCHED_DIR/bin/models" ] && [ -n "$(ls -A "$PATCHED_DIR/bin/models" 2>/dev/null)" ] && echo OK || echo PROBLEM)"
echo "  patched/lib/libMediaSDK.so         : $(sanity_check "$PATCHED_DIR/lib/libMediaSDK.so" && echo 'sanity OK (ELF)' || echo 'PROBLEM')"
echo "  files in patched/lib/              : $libcount"
echo "======================================================================"
[ "$ok" = "1" ] && exit 0 || exit 1
