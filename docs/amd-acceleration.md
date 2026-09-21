# Insta360 MediaSDK — AMD Hardware HEVC Encoding (SDK adaptation and shims)

## What this solution does

Processes Insta360 panoramic videos (`.insv`) through `MediaSDKTest` on
**native Linux** with:

- **Vulkan**-accelerated blending and FlowState (works around a Vulkan
  initialization bug);
- **hardware** HEVC encoding via **AMF** (`hevc_amf`) on AMD RX 7800 XT
  (`/dev/dri/renderD128`);
- a target bitrate of **200 Mbps** (`-bitrate 200000000`), sustained ~204 Mbps.

Result: ~14 s instead of ~21 s (CPU x265), ~130–150 fps encoding, roughly 2x
the bitrate at high quality (SSIM 0.945 vs x265) on a single file; in batch
mode the GPU pipeline is ~3.9x faster than CPU x265 (41.1 vs 10.5 fps).

The MediaSDK is NVIDIA/CUDA-oriented (NVENC/CUVID). On AMD it cannot use the
hardware encoder out of the box, so a compatibility layer and `LD_PRELOAD`
shims are required.

## Shims (LD_PRELOAD)

### `shim/vkfix16.so` (source: `shim/vkfix16.c`)

Fixes Vulkan initialization on RADV:

1. **`vk*` symbol interposition** — `libMediaSDK.so` contains uninitialized
   BSS variables named `vk*` that intercept `dlsym(RTLD_DEFAULT, ...)` and
   return garbage instead of the real libvulkan functions. The shim redirects
   `vkGetInstanceProcAddr` and `vkCreateDevice` to the real functions.
2. **`VK_EXT_global_priority`** — MediaSDK requests this extension, which RADV
   does not support (`VK_ERROR_NOT_PERMITTED`). The shim strips the
   `global_priority` pNext from `VkDeviceCreateInfo` and
   `VkDeviceQueueCreateInfo` and removes the extension from the enabled list.
   For AMF device creation the pNext chain is passed through unchanged.

### `shim/amfshim3.so` (source: `shim/amfshim3.c`)

Forces the maximum-bitrate options on the AMF encoder. MediaSDK calls
`avcodec_open2` via direct calls (not PLT), so ordinary symbol interposition
does not work — the shim performs an **inline hook** (mprotect + trampoline) on
`avcodec_open2`. After the `hevc_amf` codec opens successfully, it sets on
`codec_ctx->priv_data` via `av_opt_set_int`:

- `rc=3` — CBR;
- `quality=0` — maximum quality (default is 10 = speed);
- `filler_data=0` — do not pad to the exact bitrate with filler packets
  (avoids filler-packet sync stalls / mid-run load drops);
- `max_au_size=0` — no AU size limit;
- `enforce_hrd=0`.

All five options are read **once at load time** from environment variables
(defaults in parentheses): `AMF_RC` (3), `AMF_QUALITY` (0), `AMF_FILLER` (0),
`AMF_MAX_AU` (0), `AMF_HRD` (0). `convert_tui.py` exposes them as CLI flags
`--amf-rc`, `--amf-quality`, `--amf-filler`, `--amf-max-au`, `--amf-hrd` (see
[usage.md](usage.md)), which are passed through as the corresponding `AMF_*`
env vars; any `AMF_*` already set in the shell is inherited automatically. This
lets you tune the encoder without rebuilding the shim — e.g.
`--amf-filler 0` (or `AMF_FILLER=0`) keeps the encoder running continuously
instead of idling between waves of input frames.

The path to `libMediaSDK.so` is resolved portably: `$MEDIASDK_LIB` env →
sibling `../patched/lib/libMediaSDK.so` relative to the shim (via
`dladdr`) → plain `"libMediaSDK.so"`.

## SDK adaptation details

`apply_patch.sh` is now **fully automatic**: drop the SDK archive
(`Linux_CameraSDK-*.zip`) into `./sdk/` and run it — it extracts the SDK
(zip → tarball → deb → `opt/MediaSDK-*-linux/`), locates the original
`libMediaSDK.so` (you don't need to know its path), verifies the SDK version
signature, applies the patch and assembles `patched/`.

Original (auto-located from the SDK archive, tested version):
`opt/MediaSDK-3.1.5-linux/lib/libMediaSDK.so`

Patched: `patched/lib/libMediaSDK.so`

The adaptation changes, at a high level:

1. **Encoder selection** — the SDK's hardware-encoder selection is redirected
   so that on AMD systems it uses the AMF encoder (`hevc_amf`) instead of the
   NVIDIA/CUDA path.
2. **Vulkan initialization compatibility** — a fix for the SDK's Vulkan
   initialization so it works correctly with the Mesa RADV driver (the
   `vkfix16` shim also covers this at runtime).
3. **Encoder name handling** — the encoder name checks in the SDK are made
   consistent with the AMF encoder.

The adaptation is **algorithmic and self-contained** (`libmedia_adapt.py` in
the repo root): it scans **your** SDK copy at runtime — hides `vk*` symbols in
`.dynstr`, renames the `nvenc` encoder strings to `amf`, and redirects the
encoder branch. The repository contains **no data extracted from the
proprietary SDK** (no offsets, no symbol lists, no byte dumps).

> The adaptation is tied to a specific SDK build. For reproducibility use
> `apply_patch.sh`, which applies the algorithmic adaptation and verifies the
> result.

## Reproducibility

```bash
./apply_patch.sh
# auto: extracts the SDK from ./sdk/, verifies the SDK version signature,
# applies the algorithmic adaptation (libmedia_adapt.py) ->
# sdk/work/libMediaSDK_patched.so, assembles patched/, builds the shims (make),
# and sanity-checks the result (ELF + size)
```

`./apply_patch.sh --check` only verifies presence/version without writing
anything.

### `apply_patch.sh` modes

- `--check` — verify only (read-only): checks that the original SDK is present
  and the version is recognized, and that `patched/` is built.
  Exits non-zero if the original is missing or unrecognized.
- `--force` — redo extraction/assembly even if the cache is valid.
- `--regenerate` — obsolete (the adaptation is algorithmic and self-contained;
  there is no data file to regenerate). Accepted with a notice, then runs as a
  normal build.

### Environment overrides

- `SRC` — path to the original `libMediaSDK.so` (default: auto-located from the
  SDK archive under `./sdk/`).

### Repository layout

- `sdk/` — your SDK content, all gitignored:
  - `sdk/` — drop the downloaded `Linux_CameraSDK-*.zip` here;
  - `sdk/work/` — extraction cache and the intermediate adapted
    `libMediaSDK_patched.so` (created by `apply_patch.sh`, gitignored).
- `patched/` — the patched run tree used by `convert_tui.py`
  (`patched/bin/MediaSDKTest`, `patched/lib/libMediaSDK.so`), built by
  `./apply_patch.sh`.
- `shim/` — shim sources and built `.so` files (`shim/vkfix16.c` +
  `shim/amfshim3.c`, built by `make`, run automatically by
  `./apply_patch.sh`).
- `libmedia_adapt.py` — the self-contained algorithmic patcher (repo root).

## Rollback

To return to the original behavior (CPU x265 encoding):

1. Copy the original over the patched library (the original is the one
   `apply_patch.sh` auto-extracted, e.g. under `sdk/work/opt/MediaSDK-*-linux/lib/`):
   ```bash
   cp sdk/work/opt/MediaSDK-3.1.5-linux/lib/libMediaSDK.so \
      patched/lib/libMediaSDK.so
   ```
2. Run `MediaSDKTest` **without** the shims and with software encoding
   (here `$TIFF_LIB` is a manually-set shell variable pointing at a directory
   that contains `libtiff.so.5` — this is a manual run, not `convert_tui.py`):
   ```bash
   cd patched
   LD_LIBRARY_PATH="$TIFF_LIB" ./bin/MediaSDKTest -inputs <in.insv> -output <out.mp4> \
      -stitch_type optflow -output_size 3840x1920 -enable_flowstate \
     -enable_h265_encoder -enable_soft_encode -enable_soft_decode \
     -image_processing_accel cpu -disable_cuda --log_level info
   ```

## Dependencies

- **amf-amdgpu** (AUR) — provides `/usr/lib/libamfrt64.so.1` (AMF runtime).
  Required for hardware encoding.
- A **real** `libtiff.so.5` is required (a `libtiff.so.6 → libtiff.so.5`
  symlink is **NOT compatible** — `undefined symbol: jpeg12_write_raw_data`).
  See the [`libtiff.so.5` caveat](portability.md#libtiffso5-caveat) in
  [portability.md](portability.md).

## Run

`convert_tui.py` is the self-contained batch converter + TUI. It launches
`MediaSDKTest` directly with the fixed run tree
(`patched/bin/MediaSDKTest`,
`LD_PRELOAD=shim/vkfix16.so:shim/amfshim3.so`) and auto-detects the
`libtiff.so.5` directory at startup (override with the `TIFF_LIB` env var).

At startup it also runs a built-in **dependency check** and prints per-distro
install hints for anything missing (exit 2 if a required dependency is absent);
`./convert_tui.py --check-deps` runs only the check. See the "Dependency
check" section in the root [README](../README.md) for the required/optional
split and per-distro packages.

Camera settings are auto-detected from the `.insv` metadata: the camera model
is always read from the source (there is no model flag), and the camera
protection (lens-guard) type is auto-detected by default — `convert_tui.py`
passes `-camera_accessory_type -1` (kAutoDetect) to `MediaSDKTest` so the
lens-guard correction is always applied when needed, instead of being left at
the SDK default of "no accessory" (kNormal). Use `--accessory` to override the
protection type manually.

```bash
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR>                 # --src/--dst are required
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --jobs 2
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --bitrate 200000000 --codec h265
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --codec h264 --resolution 1920x960 --no-flowstate
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --spatialmedia  # optional: rewrite 360 metadata
```

The full options list with defaults is in [usage.md](usage.md) (or
`./convert_tui.py --help`).

Output: `<basename>_stitched.mp4` in `--dst`, per-file log —
`<dst>/logs/<basename>_stitched.mp4.log`.

### Launch parameters (reproducible)

Direct `MediaSDKTest` invocations, for reproducing the benchmarks or running
the SDK without the `convert_tui.py` wrapper. These match the benchmark setup
(3000-frame clips, optical flow, 3840x1920, 50 Mbps, flowstate):

GPU (AMF + Vulkan):

```bash
cd patched/bin
LD_LIBRARY_PATH=/opt/insta360-libs ./MediaSDKTest -inputs <file>.insv -output out.mp4 \
  -stitch_type optflow -enable_flowstate -enable_h265_encoder -camera_accessory_type -1 \
  -enable_soft_decode -bitrate 50000000 -image_processing_accel auto \
  -output_size 3840x1920 --log_level info
```

CPU (software): the same command but with `-enable_soft_encode
-image_processing_accel cpu` instead of `-image_processing_accel auto` (no AMF).

The optional `--spatialmedia` flag runs Google's `spatialmedia` tool after
stitching to rewrite the spherical metadata block (requires `spatialmedia` on
PATH). The base pipeline already writes spherical metadata inherited from the
source `.insv` (`StitchingSoftware=Insta360`), so `--spatialmedia` only
replaces that block with Google-tool-standard metadata (dropping
`StereoMode`/`SourceCount`) — the output is already 360-playable without it.

## License / redistribution

We ship **no** MediaSDK binaries. The original `libMediaSDK.so` must be
obtained from Insta360 under their SDK terms. The compatibility layer and shims
in this repository are applied to a copy of your own original file and are
intended for personal use. You are responsible for complying with your SDK
license; use the adaptation only if your license permits it.
