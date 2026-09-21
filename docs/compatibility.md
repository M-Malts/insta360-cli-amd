# Tested compatibility matrix

The following table lists the exact software/hardware versions on which the
Insta360 MediaSDK AMD acceleration solution (SDK adaptation + shims) was developed
and verified end-to-end.

| Component | Tested version | Notes |
|---|---|---|
| MediaSDK | 3.1.5 (deb build 20260819) | the adaptation is signature-based (searches for unique strings/patterns, not fixed offsets); `apply_patch.sh` verifies the expected signatures are present and sanity-checks the result |
| amf-amdgpu | 26.10.1-2 | provides `/usr/lib/libamfrt64.so.1` — **required** |
| Mesa / RADV (vulkan-radeon) | 3:26.2.2-2 | Vulkan driver used for blending/FlowState |
| vulkan-icd-loader | 1.4.357.0-1.1 | system Vulkan loader |
| Kernel | 7.2.4-3-cachyos | CachyOS / Arch-based |
| GPU | AMD Radeon RX 7800 XT (Navi32) | uses `/dev/dri/renderD128` |
| Camera | Insta360 X5 | footage used in the benchmarks |
| GCC | 16.2.1 | used to build the shims |
| System ffmpeg | any recent | used only for the `ffprobe` summary in `convert_tui.py`; **optional** |

## Benchmark

Measured with 4 Insta360 X5 clips, **3000 frames each** (~100 s, dual
2880x2880 HEVC), stitched with **optical flow** (`optflow`) to 3840x1920 HEVC
at 50 Mbps with flowstate on. Hardware: AMD Ryzen 7 5700X3D (8 cores / 16
threads), AMD Radeon RX 7800 XT (Navi 32). Resource metrics are for **only the
`MediaSDKTest` processes** (per-process `/proc` jiffies / VmRSS), not the whole
system. The only difference between CPU and GPU runs is the encode/processing
path: software (`libx265` + CPU image processing) vs hardware (`hevc_amf` +
Vulkan).

| Mode | Wall (4 files) | Aggregate fps | CPU load (procs) | RAM (procs) | Encoder |
|---|---|---|---|---|---|
| GPU single (1 file) | 128.7 s/file | 23.3 fps | 931.6% (~9.3 cores) | 1901 MB | `hevc_amf` / hardware |
| GPU batch (4 parallel) | 292 s | 41.1 fps | 1512.6% (~15.1 cores) | 6159 MB | `hevc_amf` / hardware |
| CPU single (1 file) | 310.95 s/file | 9.65 fps | 1441.5% (~14.4 cores) | 2881 MB | `libx265` / software |
| CPU batch (4 parallel) | 1143 s | 10.5 fps | ~1600% (est) | ~10–11 GB (est) | `libx265` / software |

Derived:

- **GPU single vs CPU single: 2.42x faster** (310.95 / 128.7), ~35% lower CPU
  load (932% vs 1441%), ~34% less RAM (1901 MB vs 2881 MB).
- **GPU batch vs CPU batch: 3.9x faster** (1143 / 292), 41.1 fps vs 10.5 fps.
- **GPU scales with parallelism: +76%** throughput (41.1 vs 23.3 fps).
- **CPU does not scale: only +9%** (10.5 vs 9.65 fps) — CPU-bound, all 16
  threads already saturated by one process.
- **GPU batch vs CPU single: 4.3x** (41.1 vs 9.65 fps), 4 files in 292 s
  instead of 1244 s sequentially.

Fixed overhead (~0.5 s SDK init) is negligible at 3000 frames/clip.

Note on parallelism: measured throughput keeps rising from 1 to 2-3 parallel
jobs, but the gain flattens beyond that — the GPU/CPU resources are already
saturated (GPU: 23.3 fps single → 41.1 fps at 4 parallel, +76%; CPU: ~9.7 fps
with no real scaling). `jobs=4` is the default as a safe "just in case" value
with a little headroom — it does not give a proportional 4x speedup, and for
this hardware 2-3 parallel jobs capture almost all of the available
parallelism.

Reproducible launch commands (GPU/AMF and CPU): see
[docs/amd-acceleration.md](amd-acceleration.md#launch-parameters-reproducible).
