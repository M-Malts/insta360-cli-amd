# Portability

This document describes which components are **auto-detected**, which are
**required** (documented as requirements), and how to adapt the solution to
other SDK versions or machines.

## Auto-detected (no configuration needed)

| Component | How it is detected |
|---|---|
| `libtiff.so.5` directory | **Required system dep, reported at startup.** `convert_tui.py` checks the `TIFF_LIB` env var first (must point at a directory containing a **real** `libtiff.so.5`), then `ldconfig -p`, then common dirs `/usr/lib/x86_64-linux-gnu`, `/usr/lib64`, `/usr/lib`. A symlink to `libtiff.so.6` is **rejected** (incompatible with the patched MediaSDK — `undefined symbol: jpeg12_write_raw_data`). Missing → `MISSING:` + per-distro hint, exit 2. |
| Startup dependency check | `convert_tui.py` verifies repo-delivered artifacts (`patched/bin/MediaSDKTest`, `shim/vkfix16.so`, `shim/amfshim3.so`, `patched/lib/libMediaSDK.so`) and system libs (`libtiff.so.5`, `libamfrt64.so.1`, `libvulkan.so.1` + Vulkan ICD in `/usr/share/vulkan/icd.d/`). Optional tools (`ffprobe`, `spatialmedia`, `taskset`) only warn. `--check-deps` runs only the check (exit 0/2). |
| Shims / MediaSDKTest paths | Fixed at repo root relative to `SCRIPT_DIR`: `patched/bin/MediaSDKTest`, `shim/vkfix16.so`, `shim/amfshim3.so`. These are hardcoded constants in `convert_tui.py` (no CLI/env override). |
| `libMediaSDK.so` for the AMF hook | `amfshim3.c` resolves it as: `$MEDIASDK_LIB` env → sibling `../patched/lib/libMediaSDK.so` relative to the shim (via `dladdr`) → plain `"libMediaSDK.so"`. |
| CPU core ranges for `--jobs` | `convert_tui.py` uses `os.cpu_count()` and splits cores into `N` ranges for `taskset` pinning. |
| Output resolution | Default `native` = no scaling (source resolution, 1:1); `-output_size` is only passed when an explicit `--resolution` is given. |

## Required (documented requirements)

| Requirement | Notes |
|---|---|
| AMD GPU with Mesa RADV Vulkan driver | Tested on RX 7800 XT (Navi 32). Requires `/dev/dri/renderD128` and the `vulkan-radeon` ICD. |
| `amf-amdgpu` (AMF runtime) | Provides `/usr/lib/libamfrt64.so.1`. Required for hardware HEVC encoding. |
| Insta360 MediaSDK 3.x (original `libMediaSDK.so`) | Proprietary — **not shipped** in this repo. Request the `Linux CameraSDK + MediaSDK` archive from Insta360 via the official SDK request form at <https://www.insta360.com/sdk/apply> and put it in `./sdk/`. `apply_patch.sh` auto-extracts and locates `libMediaSDK.so` (no path knowledge needed). The adaptation is algorithmic and self-contained (no SDK data in the repo) and tolerates small SDK updates (see [compatibility.md](compatibility.md)); the SDK name/version may differ from the vendor. |
| `gcc`, `make`, `python3` | Build/verification toolchain. |
| `ffprobe` (optional) | Only used for the per-file output summary. |
| `taskset` (optional) | Only used when `--jobs > 1`. Falls back to sequential without it. |
| `spatialmedia` (optional) | Only used with `--spatialmedia` to rewrite the spherical metadata block after stitching. |

## Dependency check

`convert_tui.py` verifies its dependencies automatically at startup and prints
exactly what to install (per distro, from `/etc/os-release`) for anything
missing. If a **required** dependency is absent it prints `MISSING:` lines and
exits with code 2; the startup config line ends with `deps:OK` or
`deps:N missing`. Run `./convert_tui.py --check-deps` to only run the check
(exit 0 = OK, 2 = missing) without converting anything.

Required (repo-delivered): `patched/bin/MediaSDKTest`, `shim/vkfix16.so`,
`shim/amfshim3.so`, `patched/lib/libMediaSDK.so` (the last from
`./apply_patch.sh`).

Required (system): `libtiff.so.5` (real), `libamfrt64.so.1` (AMF runtime),
`libvulkan.so.1` + a Mesa/RADV ICD in `/usr/share/vulkan/icd.d/`.

Optional (warn only): `ffprobe` (TUI speed/FPS summary), `spatialmedia` (only
with `--spatialmedia`), `taskset` (CPU pinning).

### `libtiff.so.5` caveat

Modern distros ship only `libtiff.so.6`, but a plain `libtiff.so.6 →
libtiff.so.5` symlink is **NOT compatible** with the patched MediaSDK — it
fails at runtime with `undefined symbol: jpeg12_write_raw_data, version
LIBJPEG_8.0`. A **real** `libtiff.so.5` is required:

- Ubuntu 22.04 / Debian bookworm: `sudo apt install libtiff5`.
- Other distros (Arch / CachyOS / Fedora / openSUSE / Ubuntu 24.04 / Debian
  trixie): install a genuine `libtiff.so.5` (e.g. from a Steam runtime or a
  compatible package), or set `TIFF_LIB=<dir with a real libtiff.so.5>` (for
  example `/opt/insta360-libs`).

## Version-sensitivity of the adaptation

The adaptation is **algorithmic and self-contained** (`libmedia_adapt.py` in
the repo root): it scans **your** SDK copy at runtime — hides `vk*` symbols in
`.dynstr`, renames the `nvenc` encoder strings to `amf`, and redirects the
encoder branch. The repository contains **no data extracted from the
proprietary SDK** (no offsets, no symbol lists, no byte dumps).

`apply_patch.sh` checks that the expected anchors are present in the SDK build
(the branch signature, the `hevc_nvenc` / `h264_nvenc` strings, and the `vk*`
symbols) and aborts safely if they are not — the original file is left
untouched. The result is then verified with a sanity check (ELF + size +
differs from the original) plus an algorithmic check (all changes applied).

### Adapting to another SDK version

`--regenerate` is **not needed**: the adaptation is algorithmic and scans the
SDK on the fly, so there is no data file to regenerate. If the anchors are
present in a different SDK build, `apply_patch.sh` will recognize it and adapt
it directly. Always run a test encode after adapting to a different version:

## models/ directory

`MediaSDKTest` loads AI models from `<exe>/models/` by default (the `.deb`
installs them to `/opt/MediaSDK-3.1.5-linux/bin/models/` — the exact path may
differ by SDK version). `apply_patch.sh` copies `bin/models/` into `patched/bin/`
automatically, so
no extra configuration is needed. The old `MODELS_DIR`/`-model_root_dir`
override is **not** used by `convert_tui.py` anymore.

## Machine-specific assumptions (documented, not auto-detected)

- OS: CachyOS/Arch (package names like `amf-amdgpu` are AUR). On other distros
  the AMF runtime may be packaged differently.
- `--src` / `--dst` are **required** arguments — there are no machine-specific
  defaults baked into the script.
- The Steam-runtime `libtiff.so.5` path is **not** assumed anymore: `convert_tui.py`
  checks the `TIFF_LIB` env var first, then `ldconfig -p` and standard lib dirs.
