#!/usr/bin/env python3
"""Self-contained Insta360 batch converter (merged from process.sh + TUI).
Launches MediaSDKTest directly with the same flags and CPU pinning.
Logs -> <dst>/logs/. Live TUI with REAL-TIME speed measurement:
per-file progress bar, elapsed/ETA, encoder write rate (Mbps), processing FPS,
output FPS, error panel. Ctrl-C stops everything.
"""
import argparse
import glob
import os
import shutil
import signal
import subprocess
import sys
import time

C_RESET = "\x1b[0m"
C_GREEN = "\x1b[32m"
C_YELLOW = "\x1b[33m"
C_RED = "\x1b[31m"
C_BOLD = "\x1b[1m"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MEDIABIN = os.path.join(SCRIPT_DIR, "patched/bin/MediaSDKTest")
PRELOAD = os.path.join(SCRIPT_DIR, "shim/vkfix16.so") + ":" + os.path.join(SCRIPT_DIR, "shim/amfshim3.so")

# Fallback target bitrate (bits/s) used when the source bitrate cannot be
# probed and --enc-bitrate was not explicitly set.
DEFAULT_BITRATE = 200000000

# Camera accessory / protection type passed to MediaSDKTest via
# `-camera_accessory_type <int>`. Default is "auto" (-1, kAutoDetect) so the
# lens-guard / camera-protection type is auto-detected from .insv metadata/ML
# instead of the SDK default kNormal=0 ("bare camera / no accessory"). A manual
# value can be chosen with --accessory (see ACCESSORY_CHOICES).
ACCESSORY_CHOICES = {
    "auto": -1,  # auto-detect accessory from file metadata
    "none": 0,  # no accessory (bare camera)
    "waterproof": 1,  # waterproof dive case (ONE / ONE X / ONE X2 / ONE R / ONE RS / ONE X3)
    "oner-guard": 2,  # adhesive lens guard (ONE R / ONE RS)
    "oner-guard-pro": 3,  # snap-on lens guard pro (ONE R / ONE RS)
    "onex2-guard": 4,  # adhesive lens guard (ONE R / ONE RS / ONE X2 / ONE X3)
    "onex2-guard-pro": 5,  # snap-on lens guard pro (ONE X2)
    "pano283-guard-pro": 6,  # snap-on lens guard pro for 283-degree pano lens (ONE R / ONE RS)
    "dive-air": 7,  # dive case above water (ONE X / ONE X2 / ONE R / ONE RS / ONE X3)
    "dive-water": 8,  # dive case under water (ONE X / ONE X2 / ONE R / ONE RS / ONE X3)
    "invisible-dive-air": 9,  # invisible dive case above water (X3 / X4 / X5 / X4 AIR)
    "invisible-dive-water": 10,  # invisible dive case under water (X3 / X4 / X5 / X4 AIR)
    "grade-a": 11,  # grade-A plastic lens guard (X3 / X4 / X5 / X4 AIR)
    "grade-s": 12,  # grade-S glass lens guard (X3 / X4 / X5 / X4 AIR)
    "grade-as": 13,  # auto-detect grade-A vs grade-S (X3 / X4)
    "nd16": 14,  # ND16 filter (X5)
    "nd32": 15,  # ND32 filter (X5)
    "nd64": 16,  # ND64 filter (X5)
    "oner283-guard-pro": 17,  # lens guard pro for 283-degree lens (ONE R / ONE RS)
    "oner-fpv-guard": 18,  # adhesive lens guard for FPV lens (ONE R / ONE RS)
    "x4air-dive-air": 19,  # dive case above water (X4 AIR)
    "x4air-dive-water": 20,  # dive case under water (X4 AIR)
}


def fmt_dur(s):
    s = max(0, int(s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_size(n):
    if n is None:
        n = 0
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024.0
    return f"{n:.1f}PB"


def bar(pct, w=22):
    pct = max(0.0, min(100.0, pct))
    f = round(w * pct / 100.0)
    return f"[{'#' * f}{'-' * (w - f)}] {pct:5.1f}%"


def short(n, w=30):
    return n if len(n) <= w else n[: w - 3] + "..."


def re_error(line):
    return any(k in line.lower() for k in ("error", "fail", "exception", "segmentation", "cannot", "abort"))


def parse_rat(s):
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            n = float(n)
            d = float(d)
            return n / d if d > 0 else None
        return float(s)
    except (ValueError, TypeError):
        return None


TIFF_LIB_NAMES = ("libtiff.so.5", "libtiff.so.5.7.0")


def tiff_lib_file_in_dir(d):
    """Return the path of a real libtiff.so.5 in `d`, or None.

    Rejects a broken symlink whose realpath resolves to libtiff.so.6* — that is
    INCOMPATIBLE with the patched MediaSDK (undefined symbol
    jpeg12_write_raw_data). Only a genuine libtiff.so.5 / .5.7.0 is accepted.
    """
    for name in TIFF_LIB_NAMES:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        real = os.path.realpath(p)
        if "libtiff.so.6" in real:
            continue
        return p
    return None


def find_tiff_lib_dir():
    # Escape hatch: TIFF_LIB env var pointing at a directory containing a REAL
    # libtiff.so.5 (a symlink to libtiff.so.6 is rejected).
    env_dir = os.environ.get("TIFF_LIB")
    if env_dir and tiff_lib_file_in_dir(env_dir):
        return env_dir
    cands = []
    try:
        out = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        for ln in out.splitlines():
            if "libtiff.so.5" in ln and "=>" in ln:
                p = ln.split("=>", 1)[1].strip()
                d = os.path.dirname(p)
                if d:
                    cands.append(d)
    except (OSError, subprocess.SubprocessError):
        pass
    # Known-good real-libtiff.so.5 location (verified working with AMF +
    # 3840x1920); machine-specific fallback, checked before the generic dirs.
    cands.append("/opt/insta360-libs")
    cands += [
        "/usr/lib/x86_64-linux-gnu",
        "/usr/lib64",
        "/usr/lib",
    ]
    for d in cands:
        if tiff_lib_file_in_dir(d):
            return d
    return None


# Distro-specific install hints keyed by family (detected from /etc/os-release).
DISTRO_HINTS = {
    "arch": {
        "libtiff": "install a real libtiff.so.5 (e.g. from the AUR: libtiff5), NOT a symlink to libtiff.so.6",
        "amf": "yay -S amf-amdgpu",
        "vulkan": "sudo pacman -S vulkan-radeon vulkan-icd-loader",
        "ffmpeg": "sudo pacman -S ffmpeg",
    },
    "debian": {
        "libtiff": "sudo apt install libtiff5",
        "amf": "install amf-amdgpu-pro_*.deb from https://repo.radeon.com/amf/",
        "vulkan": "sudo apt install mesa-vulkan-drivers libvulkan1",
        "ffmpeg": "sudo apt install ffmpeg",
    },
    "fedora": {
        "libtiff": "install a real libtiff.so.5 package (NOT a symlink to libtiff.so.6)",
        "amf": "install amf-amdgpu-pro_*.rpm from https://repo.radeon.com/amf/",
        "vulkan": "sudo dnf install mesa-vulkan-drivers vulkan-loader",
        "ffmpeg": "sudo dnf install ffmpeg-free",
    },
    "generic": {
        "libtiff": "install a real libtiff.so.5 package, or set TIFF_LIB=<dir with a real libtiff.so.5>, e.g. /opt/insta360-libs",
        "amf": "install the AMF runtime (libamfrt64.so.1) from your distro or https://repo.radeon.com/amf/",
        "vulkan": "install a Vulkan loader + mesa/radeon ICD (vulkan-radeon / mesa-vulkan-drivers)",
        "ffmpeg": "install ffmpeg from your distro package manager",
    },
}


def distro_family():
    """Return 'arch', 'debian', 'fedora' or 'generic' from /etc/os-release."""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return "generic"
    for line in data.splitlines():
        if line.startswith(("ID_LIKE=", "ID=")):
            val = line.split("=", 1)[1].strip().strip('"').lower()
            if "arch" in val:
                return "arch"
            if "debian" in val or "ubuntu" in val:
                return "debian"
            if "fedora" in val:
                return "fedora"
    return "generic"


def ldconfig_has(lib):
    """Return True if `lib` appears in `ldconfig -p` output."""
    try:
        out = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(lib in ln for ln in out.splitlines())


def check_dependencies(a):
    """Check required + optional dependencies.

    Returns (missing_required: list[str], warnings: list[str]).
    """
    fam = distro_family()
    hints = DISTRO_HINTS[fam]
    missing = []
    warns = []

    # --- required: repo-delivered artifacts ---
    if not os.path.isfile(MEDIABIN):
        missing.append(f"patched run tree missing: {MEDIABIN} (repo-delivered, not a package; restore patched/)")
    for shim in PRELOAD.split(":"):
        if not os.path.isfile(shim):
            missing.append(f"shim missing: {shim} (build with 'make' in repo root)")
    media_sdk = os.path.join(SCRIPT_DIR, "patched", "lib", "libMediaSDK.so")
    if not os.path.isfile(media_sdk):
        missing.append("patched libMediaSDK.so missing (run ./apply_patch.sh, then copy to patched/lib/)")

    # --- required: system libraries ---
    tiff_dir = find_tiff_lib_dir()
    if tiff_dir is None:
        missing.append(
            "libtiff.so.5 not found: a real libtiff.so.5 is required (a symlink to libtiff.so.6 is NOT "
            "sufficient — the patched MediaSDK crashes with undefined symbol jpeg12_write_raw_data). "
            f"{hints['libtiff']}; or set TIFF_LIB=<dir with a real libtiff.so.5>, e.g. /opt/insta360-libs"
        )
    if not ldconfig_has("libamfrt64.so.1"):
        missing.append(f"libamfrt64.so.1 (AMF runtime) not found ({hints['amf']})")
    if not ldconfig_has("libvulkan.so.1"):
        missing.append(f"libvulkan.so.1 not found ({hints['vulkan']})")
    else:
        icd_dir = "/usr/share/vulkan/icd.d"
        if not any(fname.endswith(".json") for fname in os.listdir(icd_dir) if os.path.isfile(os.path.join(icd_dir, fname))):
            missing.append(f"no Vulkan ICD json in {icd_dir} ({hints['vulkan']})")

    # --- optional (warn only) ---
    if shutil.which("ffprobe") is None:
        warns.append(f"ffprobe not found: TUI speed/FPS summary disabled (install ffmpeg: {hints['ffmpeg']})")
    if a.spatialmedia and shutil.which("spatialmedia") is None:
        warns.append("spatialmedia not found: --spatialmedia will be skipped (pip install spatialmedia)")
    if shutil.which("taskset") is None:
        warns.append("taskset not found: CPU pinning disabled")

    return missing, warns


def cores_for_slot(slot, jobs):
    nc = os.cpu_count() or 8
    if jobs <= 1:
        return f"0-{nc - 1}"
    band = nc // jobs
    if band < 1:
        return None
    s = slot * band
    e = s + band - 1
    return f"{s}-{e}"


def enc_quality_type(s):
    v = int(s)
    if not 0 <= v <= 51:
        raise argparse.ArgumentTypeError("must be 0-51 (lower = better)")
    return v


def enc_bitrate_type(s):
    v = int(s)
    if not 1 <= v <= 1_000_000_000:
        raise argparse.ArgumentTypeError("must be 1..1000000000 bits/s (1 bps .. 1 Gbps)")
    return v


def check_enc_ranges(a):
    amf_limits = {
        "amf_rc": (0, 3, "0=CQP,1=VBR,2=QVBR,3=CBR"),
        "amf_quality": (0, 10, "0=best,10=fastest"),
        "amf_filler": (0, 1, "0=off,1=on"),
        "amf_max_au": (0, None, "0=no limit"),
        "amf_hrd": (0, 1, "0=off,1=on"),
    }
    for attr, (lo, hi, desc) in amf_limits.items():
        v = getattr(a, attr, None)
        if v is None:
            continue
        if v < lo or (hi is not None and v > hi):
            print(f"ERROR: --{attr.replace('_', '-')} {v} out of range ({desc})", file=sys.stderr)
            sys.exit(2)
    if a.enc_mode not in ("cqp", "vbr", "qvbr", "cbr"):
        print(f"WARNING: unknown --enc-mode '{a.enc_mode}', passing through to AMF_RC", file=sys.stderr)


def parse_args():
    ap = argparse.ArgumentParser(description="Self-contained Insta360 convert with TUI (merged process.sh)")
    ap.add_argument("--src", default=None, help="source directory with *.insv / *.mp4 inputs (required)")
    ap.add_argument("--dst", default=None, help="output directory; logs go to <dst>/logs/ (required)")
    ap.add_argument("--jobs", type=int, default=4, help="number of parallel conversion slots (default: 4)")
    ap.add_argument(
        "--stitch-type",
        choices=["optflow", "dynamicstitch"],
        default="optflow",
        help="stitching template: optflow (optical-flow, default and recommended), "
        "dynamicstitch (dynamic stitching, alternative) (default: optflow); "
        "aistitch was removed (unreliable / produces artifacts)",
    )
    ap.add_argument(
        "--resolution",
        default="native",
        help="output resolution; default 'native' = no scaling (source native resolution, 1:1); "
        "examples: 3840x1920, 5760x2880, 3840x2160, 1920x960",
    )
    ap.add_argument(
        "--accessory",
        choices=list(ACCESSORY_CHOICES),
        default="auto",
        help="camera accessory / protection type; default 'auto' = auto-detect from file metadata. "
        "Values: auto(-1)=auto-detect accessory from file metadata; none(0)=no accessory (bare camera); "
        "waterproof(1)=waterproof dive case (ONE/ONE X/ONE X2/ONE R/ONE RS/ONE X3); "
        "oner-guard(2)=adhesive lens guard (ONE R/ONE RS); "
        "oner-guard-pro(3)=snap-on lens guard pro (ONE R/ONE RS); "
        "onex2-guard(4)=adhesive lens guard (ONE R/ONE RS/ONE X2/ONE X3); "
        "onex2-guard-pro(5)=snap-on lens guard pro (ONE X2); "
        "pano283-guard-pro(6)=snap-on lens guard pro for 283-degree pano lens (ONE R/ONE RS); "
        "dive-air(7)=dive case above water (ONE X/ONE X2/ONE R/ONE RS/ONE X3); "
        "dive-water(8)=dive case under water (same cams); "
        "invisible-dive-air(9)=invisible dive case above water (X3/X4/X5/X4 AIR); "
        "invisible-dive-water(10)=invisible dive case under water (same); "
        "grade-a(11)=grade-A plastic lens guard (X3/X4/X5/X4 AIR); "
        "grade-s(12)=grade-S glass lens guard (same); "
        "grade-as(13)=auto-detect grade-A vs grade-S (X3/X4); "
        "nd16(14)=ND16 filter (X5); nd32(15)=ND32 filter (X5); nd64(16)=ND64 filter (X5); "
        "oner283-guard-pro(17)=lens guard pro 283-deg (ONE R/ONE RS); "
        "oner-fpv-guard(18)=adhesive lens guard FPV lens (ONE R/ONE RS); "
        "x4air-dive-air(19)=dive case above water (X4 AIR); "
        "x4air-dive-water(20)=dive case under water (X4 AIR)",
    )
    ap.add_argument(
        "--no-flowstate",
        action="store_true",
        help="disable flowstate (image stabilization); flowstate on by default",
    )
    ap.add_argument(
        "--directionlock",
        action="store_true",
        help="lock output viewing direction (requires flowstate; keeps horizon level, reduces FOV); "
        "ignored if flowstate is off",
    )
    ap.add_argument(
        "--spatialmedia",
        action="store_true",
        help="rewrite 360 metadata with spatialmedia tool after each file (optional, default: off)",
    )
    ap.add_argument(
        "--check-deps",
        action="store_true",
        help="check dependencies and exit (no conversion)",
    )

    # --- encoder options (one logical group) ---
    enc = ap.add_argument_group("encoder options")
    enc.add_argument(
        "--enc-mode",
        default="qvbr",
        help="encoder rate-control mode: qvbr, cbr, vbr, cqp, or custom (default: qvbr)",
    )
    enc.add_argument(
        "--enc-quality",
        type=enc_quality_type,
        default=None,
        help="quality level for qvbr/cqp, 0-51, lower is better (default: not set)",
    )
    enc.add_argument(
        "--enc-preset",
        choices=["quality", "balanced", "speed"],
        default="quality",
        help="encoder speed/quality preset: quality, balanced, speed (default: quality)",
    )
    enc.add_argument(
        "--enc-bitrate",
        type=enc_bitrate_type,
        default=None,
        help="target video bitrate in bits/s (default: source file bitrate)",
    )
    enc.add_argument(
        "--codec",
        choices=["h265", "h264"],
        default="h265",
        help="output video codec, h265 = HEVC/AMF, h264 = AVC/AMF (default: h265)",
    )
    # Hidden advanced options (still functional, not shown in --help via
    # SUPPRESS). Canonical help kept consistent with the visible options:
    #   --amf-rc      "AMF rate control (default: 3)"
    #   --amf-quality "AMF quality (default: 0)"
    #   --amf-filler  "AMF filler packets (default: 0)"
    #   --amf-max-au  "AMF max access-unit size in bits (default: 0)"
    #   --amf-hrd     "AMF enforce HRD (default: 0)"
    enc.add_argument(
        "--amf-rc",
        type=int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    enc.add_argument(
        "--amf-quality",
        type=int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    enc.add_argument(
        "--amf-filler",
        type=int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    enc.add_argument(
        "--amf-max-au",
        type=int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    enc.add_argument(
        "--amf-hrd",
        type=int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    return ap.parse_args()


class Job:
    def __init__(self, path, dst, slot):
        self.src = path
        self.slot = slot
        base = os.path.splitext(os.path.basename(path))[0]
        self.out = os.path.join(dst, base + "_stitched.mp4")
        self.logfile = os.path.join(dst, "logs", base + "_stitched.mp4.log")
        self.in_size = os.path.getsize(path)
        self.start = None
        self.end = None
        self.rc = None
        self.err_tail = ""
        self.in_fps = None
        self.in_dur = None
        self.in_bitrate = None  # max source video-stream bit_rate in bits/s (from ffprobe)
        self.out_fps = None
        self.out_dur = None
        self.proc_fps = None
        self.out_prev = 0.0
        self.sample_t = None
        self.inst_rate = 0.0
        self.enc_mbps = None
        self.rt_pct = 0.0
        self.rt_fps = None
        # async finalization (spatialmedia rewrite) state
        self.final_p = None  # background spatialmedia Popen, or None
        self.finalizing = False  # True while spatialmedia rewrite is in flight


def probe(path, timeout=20):
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate,avg_frame_rate,duration",
                "-of",
                "default=noprint_wrappers=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        fps = None
        dur = None
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            if ln.startswith("r_frame_rate="):
                v = parse_rat(ln.split("=", 1)[1])
                if v:
                    fps = v
            elif ln.startswith("avg_frame_rate="):
                v = parse_rat(ln.split("=", 1)[1])
                if v and fps is None:
                    fps = v
            elif ln.startswith("duration="):
                try:
                    dur = float(ln.split("=", 1)[1])
                except ValueError:
                    pass
        return fps, dur
    except (OSError, subprocess.SubprocessError):
        return None, None


def main():
    a = parse_args()
    check_enc_ranges(a)
    if a.check_deps:
        missing, warns = check_dependencies(a)
        for w in warns:
            print("WARNING:", w, file=sys.stderr)
        if missing:
            for m in missing:
                print("MISSING:", m, file=sys.stderr)
            print(f"ERROR: {len(missing)} missing required dependencies. Fix and re-run.", file=sys.stderr)
            sys.exit(2)
        print("DEPS_OK: all required dependencies present")
        sys.exit(0)
    if a.src is None or a.dst is None:
        print("ERROR: --src and --dst are required", file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(a.src):
        print("SRC dir not found:", a.src)
        sys.exit(2)
    if not os.path.isfile(MEDIABIN):
        print("MediaSDKTest not found:", MEDIABIN)
        sys.exit(2)
    tiff_dir = find_tiff_lib_dir()
    # dependency check at startup (before config line / TUI loop); exits if a
    # required dep (incl. a real libtiff.so.5) is missing
    missing, warns = check_dependencies(a)
    for w in warns:
        print("WARNING:", w, file=sys.stderr)
    if missing:
        for m in missing:
            print("MISSING:", m, file=sys.stderr)
        print(f"ERROR: {len(missing)} missing required dependencies. Fix and re-run.", file=sys.stderr)
        sys.exit(2)
    os.makedirs(a.dst, exist_ok=True)
    os.makedirs(os.path.join(a.dst, "logs"), exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.src, "*.insv")) + glob.glob(os.path.join(a.src, "*.mp4")))
    if not files:
        print("No input files in", a.src)
        sys.exit(2)

    # startup: print the effective (resolved) configuration
    flowstate = "off" if a.no_flowstate else "on"
    directionlock = "on" if (a.directionlock and not a.no_flowstate) else "off"
    spatialmedia = "on" if a.spatialmedia else "off"
    deps_summary = "OK" if not missing else f"{len(missing)} missing"
    human_parts = [f"mode={a.enc_mode}"]
    if a.enc_quality is not None:
        human_parts.append(f"quality={a.enc_quality}")
    human_parts.append(f"preset={a.enc_preset}")
    human_str = " ".join(human_parts)
    print(
        "config: src={} dst={} jobs={} bitrate={} codec={} stitch={} resolution={} "
        "accessory={} flowstate={} directionlock={} spatialmedia={} deps:{}".format(
            a.src,
            a.dst,
            a.jobs,
            f"{(a.enc_bitrate if a.enc_bitrate is not None else DEFAULT_BITRATE) // 1000000}M",
            a.codec,
            a.stitch_type,
            a.resolution,
            a.accessory,
            flowstate,
            directionlock,
            spatialmedia,
            deps_summary,
        )
    )
    if human_str:
        print(f"  encode: {human_str}")

    def out_exists(p):
        base = os.path.splitext(os.path.basename(p))[0]
        o = os.path.join(a.dst, base + "_stitched.mp4")
        return os.path.exists(o) and os.path.getsize(o) > 0

    pending = [f for f in files if not out_exists(f)]
    already = [f for f in files if out_exists(f)]
    if not pending:
        print("All inputs already converted. Nothing to do.")
        sys.exit(0)
    total_bytes = sum(os.path.getsize(f) for f in pending)
    done_bytes = sum(os.path.getsize(f) for f in already)
    grand_total = done_bytes + total_bytes  # fixed: all input bytes for this batch
    jobs = [None] * a.jobs
    queue = list(pending)
    finished = []
    failed = []
    finalizing = []  # jobs whose spatialmedia rewrite is still in flight
    slot_rates = [6_000_000.0] * a.jobs
    start_all = time.time()
    env = dict(os.environ)
    env["LD_PRELOAD"] = PRELOAD
    env["LD_LIBRARY_PATH"] = tiff_dir
    # AMF encoder params -> AMF_* env vars consumed by shim/amfshim3.c.
    # Precedence (highest wins): user env (already in env via os.environ) >
    # Python human defaults (setdefault) > explicit human flags > hidden raw flags.
    mode_to_rc = {"cqp": 0, "vbr": 1, "qvbr": 2, "cbr": 3}
    if a.enc_mode in mode_to_rc:
        env.setdefault("AMF_RC", str(mode_to_rc[a.enc_mode]))  # default qvbr -> 2
    else:
        # unknown mode: pass through verbatim (flexible / future modes)
        env.setdefault("AMF_RC", str(a.enc_mode))
    if a.enc_quality is not None:
        env["AMF_QVBR_QUALITY_LEVEL"] = str(a.enc_quality)
    preset_to_quality = {"quality": 0, "balanced": 5, "speed": 10}
    if a.enc_preset is not None:
        env.setdefault("AMF_QUALITY", str(preset_to_quality[a.enc_preset]))
    # Hidden raw --amf-* overrides (explicitly set -> they win over everything).
    amf_env_map = {
        "amf_rc": "AMF_RC",
        "amf_quality": "AMF_QUALITY",
        "amf_filler": "AMF_FILLER",
        "amf_max_au": "AMF_MAX_AU",
        "amf_hrd": "AMF_HRD",
    }
    for attr, varname in amf_env_map.items():
        val = getattr(a, attr, None)
        if val is not None:
            env[varname] = str(val)

    def build_cmd(jb):
        cmd = [
            MEDIABIN,
            "-inputs",
            jb.src,
            "-output",
            jb.out,
            "-stitch_type",
            a.stitch_type,
        ]
        # native resolution = no scaling: omit -output_size so MediaSDKTest uses
        # the source's native resolution (1:1). Only pass it when explicit.
        if a.resolution.lower() != "native":
            cmd += ["-output_size", a.resolution]
        if not a.no_flowstate:
            cmd += ["-enable_flowstate"]
            # directionlock is a flowstate modifier; ignored when flowstate is off
            if a.directionlock:
                cmd += ["-enable_directionlock"]
        if a.codec == "h265":
            # MediaSDKTest only exposes -enable_h265_encoder; its default value
            # is "h264", so h264 output means simply omitting this flag.
            cmd += ["-enable_h265_encoder"]
        cmd += [
            "-camera_accessory_type",
            str(ACCESSORY_CHOICES[a.accessory]),
            "-enable_soft_decode",
            "-bitrate",
            str(eff_bitrate(jb)),
            "-image_processing_accel",
            "auto",
            "--log_level",
            "info",
        ]
        cores = cores_for_slot(jb.slot, a.jobs)
        if cores and shutil.which("taskset"):
            cmd = ["taskset", "-c", cores] + cmd
        return cmd

    def eff_bitrate(jb):
        """Effective target bitrate for a job: explicit --enc-bitrate wins,
        else the max source video-stream bitrate, else DEFAULT_BITRATE."""
        if a.enc_bitrate is not None:
            return a.enc_bitrate
        if jb.in_bitrate:
            return jb.in_bitrate
        return DEFAULT_BITRATE

    def parse_probe_output(text, jb):
        """Parse ffprobe -show_entries output into jb.in_fps/in_dur/in_bitrate.

        bit_rate takes the MAX across all video streams (the dominant eye
        stream; -select_streams v excludes audio). fps/dur: first wins.
        """
        for ln in text.splitlines():
            if ln.startswith(("r_frame_rate=", "avg_frame_rate=")):
                v = parse_rat(ln.split("=", 1)[1])
                if v and jb.in_fps is None:
                    jb.in_fps = v
            elif ln.startswith("duration="):
                try:
                    jb.in_dur = float(ln.split("=", 1)[1])
                except ValueError:
                    pass
            elif ln.startswith("bit_rate="):
                try:
                    v = int(ln.split("=", 1)[1])
                    if v and (jb.in_bitrate is None or v > jb.in_bitrate):
                        jb.in_bitrate = v
                except ValueError:
                    pass

    def spawn():
        if not queue:
            return
        slot = next((i for i, j in enumerate(jobs) if j is None), None)
        if slot is None:
            return
        path = queue.pop(0)
        jb = Job(path, a.dst, slot)
        jobs[slot] = jb
        jb.probe_p = subprocess.Popen(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=index,r_frame_rate,avg_frame_rate,duration,bit_rate",
                "-of",
                "default=noprint_wrappers=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the probe so jb.in_bitrate is known BEFORE building the
        # encode command (otherwise eff_bitrate falls back to DEFAULT_BITRATE).
        try:
            out, _ = jb.probe_p.communicate(timeout=15)
            parse_probe_output(out.decode("utf-8", "replace"), jb)
        except (OSError, subprocess.SubprocessError):
            pass
        jb.probed = True
        with open(jb.logfile, "w") as logf:
            jb.start = time.time()
            jb.proc = subprocess.Popen(
                build_cmd(jb), stdout=logf, stderr=subprocess.STDOUT, env=env, start_new_session=True
            )

    def poll_probe(jb):
        if getattr(jb, "probed", False):
            return
        if jb.probe_p.poll() is None:
            return
        try:
            out, _ = jb.probe_p.communicate(timeout=2)
            parse_probe_output(out.decode("utf-8", "replace"), jb)
        except (OSError, subprocess.SubprocessError):
            pass
        jb.probed = True

    def rt_measure(jb):
        """Real-time measurement of encoder rate and progress from output growth."""
        now = time.time()
        try:
            sz = os.path.getsize(jb.out)
        except OSError:
            sz = 0
        if jb.sample_t is None:
            jb.sample_t = now
            jb.out_prev = sz
            return
        dt = now - jb.sample_t
        if dt >= 0.8:
            inst = (sz - jb.out_prev) / dt
            if inst >= 0:
                jb.inst_rate = inst
                jb.enc_mbps = inst * 8.0 / 1e6
            jb.out_prev = sz
            jb.sample_t = now
        # real progress: current out size vs predicted final size (CBR)
        pred = None
        br = eff_bitrate(jb)
        if jb.in_dur and br > 0:
            pred = br * jb.in_dur / 8.0
        if pred and pred > 0:
            jb.rt_pct = min(100.0, sz / pred * 100.0)
        else:
            el = now - jb.start
            exp = jb.in_size / slot_rates[jb.slot] if slot_rates[jb.slot] > 0 else 1
            jb.rt_pct = min(100.0, el / exp * 100.0) if exp > 0 else 0.0
        el = now - jb.start
        if jb.in_fps and jb.in_dur and el > 0:
            jb.rt_fps = jb.in_fps * jb.in_dur * (jb.rt_pct / 100.0) / el
        else:
            jb.rt_fps = None
        # encoder write throughput in real time: target CBR scaled by proc rate
        if jb.rt_fps and jb.in_fps and jb.in_fps > 0:
            jb.enc_mbps = (br / 1e6) * (jb.rt_fps / jb.in_fps)
        else:
            jb.enc_mbps = None

    def do_spatialmedia(jb):
        """Start spatialmedia metadata rewrite in the BACKGROUND (non-blocking).

        Returns the Popen handle (or None if not applicable). The caller polls
        jb.final_p in the main loop and only counts the job as fully finished
        once the rewrite completes. The encode slot is freed immediately so a
        new encode job can start while the metadata rewrite runs on the same
        output file.
        """
        if not a.spatialmedia:
            return None
        sm = shutil.which("spatialmedia")
        if not sm:
            return None
        tmp = jb.out + ".spatial.mp4"
        try:
            p = subprocess.Popen(
                [sm, "-i", jb.out, tmp], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            jb.final_p = p
            jb.finalizing = True
            return p
        except (OSError, subprocess.SubprocessError):
            return None

    def update_finished(jb):
        """Finish bookkeeping for an encode job that just exited.

        Called AFTER the slot has been freed and refilled (refill-first), so
        the blocking ffprobe here does not delay starting the next job. Any
        spatialmedia rewrite was already started as a background process; the
        job is moved to `finalizing` and only appended to `finished` once that
        rewrite completes (see poll_finalizing).
        """
        nonlocal done_bytes, slot_rates
        jb.end = time.time()
        wall = jb.end - jb.start
        done_bytes += jb.in_size
        fps, dur = probe(jb.out, timeout=20)
        jb.out_fps = fps
        jb.out_dur = dur
        nb = (fps * dur) if (fps and dur) else 0.0
        jb.proc_fps = (nb / wall) if wall > 0 and nb > 0 else None
        fs = [x for x in finished[-max(1, a.jobs) * 3 :]]
        tot_in = sum(x.in_size for x in fs)
        tot_w = sum((x.end - x.start) for x in fs)
        if tot_w > 0:
            slot_rates[jb.slot] = max(1e6, tot_in / tot_w)
        # start async spatialmedia rewrite (if enabled); job becomes finalizing
        do_spatialmedia(jb)
        if jb.finalizing:
            finalizing.append(jb)
        else:
            # no background rewrite -> job is fully finished right away
            finish_job(jb)

    def finish_job(jb):
        """Append a job to the finished/failed lists (TUI bookkeeping)."""
        finished.append(jb)
        if jb.rc != 0:
            failed.append(jb)
            try:
                with open(jb.logfile, "rb") as f:
                    data = f.read()[-4000:]
                jb.err_tail = "\n".join(l for l in data.decode("utf-8", "replace").splitlines() if re_error(l))[-1200:]
            except OSError:
                jb.err_tail = "<no log>"

    def poll_finalizing(jb):
        """Poll a background spatialmedia rewrite; finish the job when done."""
        if not jb.finalizing:
            return
        if jb.final_p is None:
            # background proc failed to start; finish as-is
            jb.finalizing = False
            if jb in finalizing:
                finalizing.remove(jb)
            finish_job(jb)
            return
        rc = jb.final_p.poll()
        if rc is None:
            return  # still running
        jb.finalizing = False
        if jb in finalizing:
            finalizing.remove(jb)
        if rc == 0:
            tmp = jb.out + ".spatial.mp4"
            try:
                if os.path.exists(tmp):
                    os.replace(tmp, jb.out)
            except OSError:
                pass
        finish_job(jb)

    def stop_all():
        print("\nStopping children gracefully...")
        for jb in jobs:
            if jb is None:
                continue
            if jb.proc.poll() is None:
                try:
                    os.killpg(jb.proc.pid, signal.SIGINT)
                except (OSError, ProcessLookupError):
                    pass
        time.sleep(1.0)
        for jb in jobs:
            if jb is None:
                continue
            if jb.proc.poll() is None:
                try:
                    os.killpg(jb.proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        time.sleep(3.0)
        for jb in jobs:
            if jb is None:
                continue
            if jb.proc.poll() is None:
                try:
                    os.killpg(jb.proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        time.sleep(1.0)
        for jb in jobs:
            if jb is None:
                continue
            if jb.proc.poll() != 0:
                for p in (jb.out, jb.out + ".spatial.mp4"):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass

    for _ in range(min(a.jobs, len(queue))):
        spawn()
    try:
        while any(jobs) or queue or finalizing:
            time.sleep(0.5)
            for i, jb in enumerate(jobs):
                if jb is None:
                    continue
                poll_probe(jb)
                rc = jb.proc.poll()
                if rc is not None:
                    jb.rc = rc
                    update_finished(jb)
                    jobs[i] = None
                    spawn()
                else:
                    rt_measure(jb)
            # poll background spatialmedia rewrites (finalizing jobs)
            for jb in list(finalizing):
                poll_finalizing(jb)
            # global real-time: input-bytes-equivalent done
            now = time.time()
            equiv = done_bytes
            for jb in jobs:
                if jb is not None:
                    equiv += jb.in_size * (jb.rt_pct / 100.0)
            gwall = now - start_all
            # whole-run average throughput (input bytes done per wall-second).
            # Smooth and never dips to 0 during slot-refill/probe gaps.
            g_rate = equiv / gwall if gwall > 1e-6 else 0.0
            # gdone_pct and remaining are both in input-bytes terms so they stay
            # consistent: processed = done_bytes + active in_size*pct/100.
            gdone_pct = equiv / (done_bytes + total_bytes) * 100 if (done_bytes + total_bytes) else 0
            rem = max(0.0, (done_bytes + total_bytes) - equiv)
            # guard: no meaningful ETA when there is nothing left or no measured rate
            geta = rem / g_rate if g_rate > 1e-6 and rem > 0 else None
            if geta is not None and geta > 24 * 3600:
                geta = None  # cap absurd ETAs -> render as --:--:--
            # overall processing fps: active slots + recent finished
            g_fps = 0.0
            for jb in jobs:
                if jb is not None and jb.rt_fps:
                    g_fps += jb.rt_fps
            for jb in finished[-8:]:
                if jb.proc_fps:
                    g_fps += jb.proc_fps
            out = []
            out.append(
                C_BOLD
                + "Insta360 batch convert TUI"
                + C_RESET
                + f"   jobs={a.jobs} total={fmt_size(total_bytes)} already={len(already)} to-do={len(pending)}"
            )
            out.append("")
            for slot, jb in enumerate(jobs):
                if jb is None:
                    out.append(f"slot {slot}:  (idle)")
                    continue
                if jb.start is None:
                    # queued: spawned but not started yet
                    out.append(f"slot {slot}:  (queued) {short(os.path.basename(jb.src))}")
                    continue
                el = now - jb.start
                # ETA guard: no meaningful ETA until real progress exists
                if not jb.rt_pct or jb.rt_pct <= 0.5:
                    eta_s = "--:--:--"
                else:
                    eta = el * (100 - jb.rt_pct) / jb.rt_pct
                    eta_s = fmt_dur(eta)
                pf_s = f"{jb.rt_fps:.1f} fps" if jb.rt_fps else "n/a fps"
                tgt_s = f"{int(eff_bitrate(jb) / 1e6):>5}M"
                br_s = f"{int(eff_bitrate(jb) / 1e6):>4}"
                if jb.inst_rate and jb.inst_rate > 0:
                    enc_s = f"disk {int(jb.inst_rate * 8.0 / 1e6):>4} Mbps"
                elif jb.enc_mbps:
                    enc_s = f"enc {int(jb.enc_mbps):>4}/{br_s} Mbps"
                else:
                    enc_s = f"enc {'':>4}/{br_s} Mbps"
                enc_s = enc_s.ljust(18)
                fps_s = f"out {jb.in_fps:.2f} fps" if jb.in_fps else "out n/a fps"
                out.append(
                    f"slot {slot:>2} {bar(jb.rt_pct)} {short(os.path.basename(jb.src), 28):<28} "
                    f"el {fmt_dur(el)} eta {eta_s:<8} "
                    f"in {fmt_size(jb.in_size):>8} | {tgt_s} | {enc_s} | proc {pf_s:<9} | {fps_s:<13}"
                )
            out.append("")
            out.append(
                "GLOBAL "
                + bar(gdone_pct, 40)
                + f" done={len(finished) + len(already)}/{len(files)} fail={len(failed)} "
                f"run={sum(1 for j in jobs if j)}"
            )
            out.append(
                f"       elapsed {fmt_dur(gwall)}   ETA {fmt_dur(geta) if geta is not None else '--:--:--':<8}   "
                f"rate {fmt_size(g_rate):>10}/s | proc {g_fps:.1f} fps"
            )
            if finished:
                out.append("")
                out.append(C_GREEN + (f"FINISHED (last {min(6, len(finished))}):") + C_RESET)
                for jb in finished[-6:]:
                    pf = f"{jb.proc_fps:.1f}" if jb.proc_fps else "n/a"
                    of = f"{jb.out_fps:.2f}" if jb.out_fps else "n/a"
                    outsz = fmt_size(os.path.getsize(jb.out)) if os.path.exists(jb.out) else "0"
                    out.append(
                        f"  {short(os.path.basename(jb.src)):<34} out {of} fps | proc {pf} fps | "
                        f"wall {fmt_dur(jb.end - jb.start)} | {outsz}"
                    )
            if failed:
                out.append("")
                out.append(C_RED + C_BOLD + "ERRORS:" + C_RESET)
                for jb in failed[-5:]:
                    out.append(C_RED + "FAILED " + os.path.basename(jb.src) + " rc=" + str(jb.rc) + C_RESET)
                    if jb.err_tail:
                        for l in jb.err_tail.splitlines()[-4:]:
                            out.append(C_RED + "   " + l[:150] + C_RESET)
            sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(out) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        stop_all()
        print(f"Interrupted by user. done={len(finished)} fail={len(failed)}")
        sys.exit(130)
    print(f"\nALL DONE. success={len(finished) - len(failed)} failed={len(failed)}")
    if failed:
        print("Failed files:")
        for jb in failed:
            print("  ", jb.src, "rc=", jb.rc)


if __name__ == "__main__":
    main()
