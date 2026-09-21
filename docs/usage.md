# Insta360 CLI — `convert_tui.py` usage

Full command-line reference for the batch converter.

## Examples

```bash
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR>                # --src/--dst are required
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --jobs 2
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --bitrate 200000000 --codec h265
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --stitch-type optflow
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --codec h264 --resolution 1920x960 --no-flowstate
./convert_tui.py --src <SRC_DIR> --dst <DST_DIR> --spatialmedia  # optional: rewrite 360 metadata
./convert_tui.py --check-deps                                    # dependency check only (exit 0/2)
```

## Options

Options (`./convert_tui.py --help`):

- `--src DIR` — **required**; source directory with `*.insv` / `*.mp4` inputs.
- `--dst DIR` — **required**; output directory; per-file logs go to
  `<dst>/logs/`.
- `--jobs N` — number of parallel conversion slots (default `4`).
- `--bitrate BITS` — target video bitrate in bits/sec, CBR (default
  `200000000` = 200 Mbps).
- `--codec {h265,h264}` — output codec (h265 = HEVC/AMF, h264 = AVC/AMF;
  default `h265`).
- `--stitch-type {optflow,dynamicstitch}` — stitching template
  (default `optflow`). See "Choosing a stitching type" below.
- `--resolution WxH` — output resolution; default `native` = no scaling (source
  resolution, 1:1); pass e.g. `3840x1920`, `5760x2880`, `3840x2160`, `1920x960`
  to scale.
- `--accessory TYPE` — camera accessory / protection type; default `auto` =
  auto-detect from file metadata (maps to `-camera_accessory_type`). Choices and
  int mapping: `auto`(-1), `none`(0), `waterproof`(1), `oner-guard`(2),
  `oner-guard-pro`(3), `onex2-guard`(4), `onex2-guard-pro`(5),
  `pano283-guard-pro`(6), `dive-air`(7), `dive-water`(8),
  `invisible-dive-air`(9), `invisible-dive-water`(10), `grade-a`(11),
  `grade-s`(12), `grade-as`(13), `nd16`(14), `nd32`(15), `nd64`(16),
  `oner283-guard-pro`(17), `oner-fpv-guard`(18), `x4air-dive-air`(19),
  `x4air-dive-water`(20).
- `--directionlock` — lock output viewing direction (keeps horizon level,
  reduces FOV); requires flowstate, ignored when flowstate is off.
- `--no-flowstate` — disable flowstate (image stabilization); flowstate is on by
  default.
- `--spatialmedia` — rewrite 360 metadata with the `spatialmedia` tool after
  each file (optional, default off).
- `--amf-rc N` — AMF rate control: `0`=CQP, `1`=VBR, `2`=QVBR, `3`=CBR
  (default `3`).
- `--amf-quality N` — AMF quality preset: `0`=best quality, `10`=fastest speed
  (default `0`).
- `--amf-filler N` — AMF filler packets to pad exact bitrate: `0`=off, `1`=on
  (default `0`).
- `--amf-max-au N` — AMF max access-unit size in bits: `0`=no limit
  (default `0`); e.g. `10000000` for 1080p VBV.
- `--amf-hrd N` — AMF enforce HRD conformance: `0`=off, `1`=on (default `0`).
- `--check-deps` — run only the dependency check and exit (0 = OK, 2 = missing).

The same `--amf-*` values can also be set via `AMF_*` environment variables
(`AMF_RC=2 AMF_FILLER=0 ./convert_tui.py ...`), which are inherited
automatically. These tune the AMF HEVC encoder (e.g. `--amf-filler 0` avoids
filler-packet sync stalls).

### Choosing a stitching type

`optflow` (optical flow) is the default and is recommended. It actively forces
mismatched edges to line up perfectly, so it is the best choice when:

- You have prominent vertical or horizontal lines in your shot (like buildings
  or tables), or
- a person is standing relatively close to the camera.

`dynamicstitch` (dynamic stitching) is the alternative for fast-moving action
sports (like biking, skiing, or running) where the environment changes rapidly
and you need fast rendering speeds without strange pixel-warping glitches.

The former `aistitch` option was removed: in comparison it is usually worse
because it produces artifacts. Use `optflow` (default) or `dynamicstitch`.

## Startup dependency check

`convert_tui.py` verifies the repo-delivered artifacts and system libraries
(see [portability.md](portability.md)), prints `MISSING:` lines with per-distro
install hints, and exits 2 if a required dependency is absent. Optional tools
only warn. The startup config line reports the effective parameters, e.g.:

```
config: src=... dst=... jobs=4 bitrate=200M codec=h265 resolution=native accessory=auto flowstate=on directionlock=off spatialmedia=off deps:OK
```

## Output

Output: `<basename>_stitched.mp4` in `--dst`, per-file log —
`<dst>/logs/<basename>_stitched.mp4.log`. Already-converted inputs are skipped.

The optional `--spatialmedia` flag runs Google's `spatialmedia` tool after
stitching to rewrite the spherical metadata block (requires `spatialmedia` on
PATH). The base pipeline already writes spherical metadata inherited from the
source `.insv` (`StitchingSoftware=Insta360`), so `--spatialmedia` only
replaces that block with Google-tool-standard metadata (dropping
`StereoMode`/`SourceCount`) — the output is already 360-playable without it.

## Benchmark

Benchmark numbers (CPU vs GPU/AMF, 4-mode table) live in
[docs/compatibility.md](compatibility.md#benchmark).
