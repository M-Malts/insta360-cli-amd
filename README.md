# Insta360 MediaSDK — Native Linux AMD Acceleration

> **Legal note.** The Insta360 MediaSDK is proprietary software distributed by
> Insta360 under its own license terms. This project is an independent
> third-party tool: it does not bundle or redistribute the SDK. You are
> responsible for obtaining the SDK from Insta360 and for complying with your
> SDK license — including any terms regarding redistribution or modification.
> Use this project only if your license permits such use, and at your own risk.

Offline stitching of Insta360 360-degree videos (`.insv`) on **native Linux**
using the Insta360 MediaSDK with AMD hardware acceleration. The project
consists of:

- `convert_tui.py` — a self-contained batch converter with a live TUI;
- two `LD_PRELOAD` shims — `shim/vkfix16.so` (Vulkan blend/FlowState fix) and
  `shim/amfshim3.so` (AMF encoder bitrate hook);
- a **binary compatibility layer** for `libMediaSDK.so` that lets the SDK use
  AMD's AMF HEVC encoder.

Tested on an AMD Radeon RX 7800 XT (Navi 32) with MediaSDK 3.1.5 and footage
from an **Insta360 X5**.

## Features

- **Hardware AMF HEVC encoding** — `hevc_amf` on the AMD VCN block
  (`/dev/dri/renderD128`); in batch mode the GPU pipeline is **~3.9x faster
  than CPU-only x265** (41.1 fps vs 10.5 fps), and ~1.9x on a single file at
  ~50 Mbps (see [Benchmark](#benchmark)).
- **Vulkan GPU blending / FlowState** — GPU-accelerated blending and FlowState
  stitching on AMD Vulkan.
- **Automatic camera & protection detection** — camera model is always read from
  the `.insv` metadata; the camera protection (lens-guard) type is auto-detected
  by default (`-camera_accessory_type -1`, kAutoDetect). Override with
  `--accessory`.
- **Automatic output bitrate** — output bitrate is set by `max(video1, video2)` from insv file.
- **TUI progress** — per-file progress bar, elapsed/ETA, encoder write rate,
  processing/output FPS, error panel; graceful Ctrl-C.
- **Tunable encoder** — rate control mode, quality level,
  speed/quality preset and target bitrate are configurable per run
  (`--enc-mode`, `--enc-quality`, `--enc-preset`, `--enc-bitrate`); useful for
  balancing quality, file size and load.

## Benchmark

In **batch mode** the GPU/AMF pipeline is **~3.9x faster than CPU-only x265**
(41.1 fps vs 10.5 fps) at roughly the same CPU load (GPU batch 1512.6% vs
CPU batch ~1600%). On a single file the GPU is ~2.4x faster and ~35% lighter
on CPU (23.3 fps/931.6% vs 9.65 fps/1441.5%). Measured on 4 Insta360 X5 clips
(3000 frames each, 3840x1920, 50 Mbps, optical flow).

Parallelism: `--jobs 3` (the default) gave the best aggregate throughput on
the test machine — 45.5 fps vs 43.1 fps at `jobs=4` and 31.4 fps at `jobs=2`,
with ~43% average GPU load (peaks to 100%).

Full 4-mode CPU-vs-GPU/AMF table and parallelism notes:
see [docs/compatibility.md](docs/compatibility.md#benchmark).

## Requirements

What you need and where to get it:

- **Python 3.8+** — already installed on most Linux systems. `convert_tui.py`
  is stdlib-only, no pip packages required to run it.
- **git, make, gcc** — build tools for the shims.
- **The Insta360 SDK archive** — download the `Linux CameraSDK + MediaSDK`
  bundle from <https://www.insta360.com/sdk/apply> and put the `.zip` into
  `./sdk/`. That's it — `./apply_patch.sh` extracts, adapts and assembles
  everything automatically (you don't need to know the internal paths).
- **An AMD GPU** with AMF + Vulkan support (Mesa RADV driver) and
  `/dev/dri/renderD128`.

The SDK is **proprietary and is NOT included in this repository** — you must
obtain it yourself via the official SDK request form at
<https://www.insta360.com/sdk/apply>.

Per-distro package names, driver details, optional extras and the dependency
check breakdown: see [docs/portability.md](docs/portability.md).

## Setup

```bash
git clone git@github.com:M-Malts/insta360-cli-amd.git && cd insta360-cli-amd
mkdir -p sdk
# put Linux_CameraSDK-*.zip into ./sdk/
./apply_patch.sh                 # auto: extract SDK, verify SDK version, apply compatibility layer,
                                 # assemble patched/ (bin + models + lib), build shims
./convert_tui.py --check-deps    # verify everything is ready (exit 0 = OK)
```

That's the whole setup. `./apply_patch.sh` is fully automatic and prints
status messages; it skips re-extraction if the work is already cached
under `sdk/work/` and says "Already built" if `patched/` is already correct.

`apply_patch.sh` modes (`--check`, `--force`), the `SRC` environment override
and the repository layout: see [docs/amd-acceleration.md](docs/amd-acceleration.md).

## Quick usage

```bash
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR>                # --src/--dst are required
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --jobs 3
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --bitrate 200000000 --codec h265
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --stitch-type optflow
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --codec h264 --resolution 1920x960 --no-flowstate
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --spatialmedia  # optional: rewrite 360 metadata
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --amf-rc 2 --amf-filler 0   # tune AMF encoder (rate control, filler)
./convert_tui.py --check-deps                                    # dependency check only (exit 0/2)
```

Output: `<basename>_stitched.mp4` in `--dst`, per-file log —
`<dst>/logs/<basename>_stitched.mp4.log`. Already-converted inputs are skipped.

**Output resolution.** By default (`--resolution native`) the tool computes the
full stitched resolution from the source via ffprobe — dual-fisheye eyes of
`WxH` become an equirectangular frame of `2WxH` (e.g. `2880x2880` eyes →
`5760x2880` for an Insta360 X5). If the source cannot be probed, `-output_size`
is omitted and the SDK falls back to its silent default of **1920x960** — which
is only ~1/3 of the true native width and not even full HD. Pass an explicit
`--resolution 3840x1920` (or any `WxH`) to downscale deliberately.

The default stitching type is `optflow` (recommended); `dynamicstitch` is the
alternative for fast-moving action. The former `aistitch` option was removed
(it does not work). Full guidance: see
[docs/usage.md](docs/usage.md#choosing-a-stitching-type).

The full options reference (all flags, defaults, accessory mapping): see
[docs/usage.md](docs/usage.md) or `./convert_tui.py --help`.

## License

This project is MIT-licensed — see [LICENSE](LICENSE). The Insta360 MediaSDK
itself is proprietary and is **not** included in this repository; it must be
obtained from Insta360 via the official SDK request form at
<https://www.insta360.com/sdk/apply>.

## Documentation

- [docs/usage.md](docs/usage.md) — full `convert_tui.py` CLI reference
- [docs/amd-acceleration.md](docs/amd-acceleration.md) — how it works, SDK adaptation, shims, launch commands, rollback
- [docs/portability.md](docs/portability.md) — auto-detected vs required components, dependency check, adapting to other SDK versions
- [docs/compatibility.md](docs/compatibility.md) — tested-version matrix
