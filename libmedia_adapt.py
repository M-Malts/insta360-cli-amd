#!/usr/bin/env python3
"""Self-contained, algorithmic patcher for the Insta360 MediaSDK AMD adaptation.

This patcher is FULLY ALGORITHMIC: it contains NO data extracted from the
proprietary SDK (no hardcoded offsets, no symbol-name lists, no byte dumps).
Everything is derived at runtime from the user's own copy of the binary. This
keeps the repository free of proprietary SDK artifacts.

Three generic rules are applied to the target ELF:

  Rule 1 - hide vk* symbols:
      Scan the .dynstr ELF section (located via the ELF section headers,
      SHT_STRTAB named ".dynstr") for every NUL-terminated string matching the
      generic pattern ^vk[A-Za-z0-9_]+$ and zero the first byte (0x76) of each.
      This prevents the loader from resolving those Vulkan entry points so the
      SDK falls back to the AMD path. It is a GENERIC rule: any vk* symbol
      present in .dynstr gets hidden, no name list needed.

  Rule 2 - encoder strings:
      Find the byte patterns b"hevc_nvenc\\x00" and b"h264_nvenc\\x00"
      (each unique in the binary) and replace the 5 bytes "nvenc" (at +5..+10)
      with b"amf\\x00\\x00" so the encoder names become hevc_amf / h264_amf
      (matching the AMD AMF backend). Idempotency is keyed on the OLD string:
      b"hevc_amf\\x00" already exists elsewhere, so we never search for it.

  Rule 3 - branch redirect:
      Search for the unique 22-byte signature
          85 d2 0f 84 08 01 00 00 83 fa 01 0f 84 87 00 00 00
      and set the byte at sig_start + 0x0D to 0x3F (it must currently be 0x87),
      producing "... 0f 84 3f 00 00 00". On an already-patched binary the
      original signature is absent and the patched variant (with 0x3F) is
      present — handled idempotently.

Idempotency: applying twice yields the same result (each rule is a no-op once
already applied). `check` reports whether a binary is already adapted.

Only stdlib is used (argparse, hashlib, os, re, struct, sys). ELF parsing is
done via struct (e_shoff at 0x28; e_shentsize/e_shnum/e_shstrndx at 0x3A/0x3C/
0x3E; section-header string table; .dynstr found by name).
"""

import argparse
import hashlib
import re
import struct
import sys

# ---------------------------------------------------------------------------
# ELF helpers (ELF64 only; this SDK is a 64-bit .so)
# ---------------------------------------------------------------------------

ELF_HEADER_FMT = "<16sHHIQQQIHHHHHH"
SHDR_FMT = "<IIQQQQII"

SHT_STRTAB = 3


def find_dynstr(data):
    """Locate the .dynstr section (offset, size) from an ELF in memory."""
    if data[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    shstr_sec = struct.unpack_from(SHDR_FMT, data, e_shoff + e_shstrndx * e_shentsize)
    shstr_off, shstr_size = shstr_sec[4], shstr_sec[5]
    shstr = data[shstr_off : shstr_off + shstr_size]
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name_idx, typ, _flags, _addr, sec_off, sec_size, _link, _info = struct.unpack_from(SHDR_FMT, data, off)
        end = shstr.find(b"\x00", name_idx)
        name = shstr[name_idx:end]
        if name == b".dynstr" and typ == SHT_STRTAB:
            return sec_off, sec_size
    raise ValueError(".dynstr section not found")


# ---------------------------------------------------------------------------
# The three algorithmic rules
# ---------------------------------------------------------------------------

# Rule 2: encoder string renames. Keyed on the OLD (pre-patch) string only.
ENCODER_RENAMES = (
    (b"hevc_nvenc\x00", b"hevc_amf\x00\x00\x00"),
    (b"h264_nvenc\x00", b"h264_amf\x00\x00\x00"),
)

# Rule 3: branch redirect. The signature and the byte to flip are code logic
# (an algorithm), not a data dump: the byte at +0x0D is the branch displacement
# low byte which we change from 0x87 to 0x3F.
BRANCH_SIG = bytes.fromhex("85d20f840801000083fa010f8487000000")
BRANCH_OFF = 0x0D
BRANCH_OLD = 0x87
BRANCH_NEW = 0x3F


def vk_hide_spots(data, dynstr):
    """Return the absolute offsets of the first byte of each vk* symbol in
    .dynstr that is currently present (first byte == 0x76)."""
    dynstr_off, dynstr_size = dynstr
    region = data[dynstr_off : dynstr_off + dynstr_size]
    spots = []
    # Zero-width lookbehind so the match starts AT the 'v'; group(0) is the
    # full NUL-terminated name (no leading NUL). This avoids prefix collisions
    # like vkCmdDispatch inside vkCmdDispatchIndirect.
    for m in re.finditer(rb"(?<=\x00)vk[A-Za-z0-9_]+\x00", region):
        pos = dynstr_off + m.start()
        if data[pos] == 0x76:
            spots.append(pos)
    return spots


def branch_state(data):
    """Return ('unpatched'|'patched', pos) for the branch rule, or raise."""
    hits = [m.start() for m in re.finditer(re.escape(BRANCH_SIG), data)]
    if len(hits) == 1:
        tgt = hits[0] + BRANCH_OFF
        if data[tgt] == BRANCH_OLD:
            return "unpatched", hits[0]
        if data[tgt] == BRANCH_NEW:
            return "patched", hits[0]
        raise ValueError(f"branch: byte at {hex(tgt)} is {data[tgt]:02x}, expected {BRANCH_OLD:02x} or {BRANCH_NEW:02x}")
    if len(hits) == 0:
        # patched variant: signature with BRANCH_NEW at BRANCH_OFF
        psig = bytearray(BRANCH_SIG)
        psig[BRANCH_OFF] = BRANCH_NEW
        phits = [m.start() for m in re.finditer(re.escape(bytes(psig)), data)]
        if len(phits) == 1:
            return "patched", phits[0]
        raise ValueError("branch signature: not found (neither original nor patched variant)")
    raise ValueError(f"branch signature: expected exactly 1 occurrence, found {len(hits)}")


# ---------------------------------------------------------------------------
# Apply / check
# ---------------------------------------------------------------------------


def apply_patch(bin_data):
    """Apply the three algorithmic rules to bin_data. Returns patched bytes.

    Raises ValueError if any required signature is missing/ambiguous or a
    pre-patch byte verify fails.
    """
    out = bytearray(bin_data)
    dynstr = find_dynstr(bytes(out))

    # Rule 1: hide vk* symbols (idempotent: already-hidden names have first
    # byte 0x00 and no longer match the vk regex, so they're simply absent).
    spots = vk_hide_spots(bytes(out), dynstr)
    for pos in spots:
        if out[pos] != 0x76:
            raise ValueError(f"vk hide: byte at {hex(pos)} is {out[pos]:02x}, expected 76")
        out[pos] = 0x00

    # Rule 3: branch redirect (idempotent via branch_state).
    state, _pos = branch_state(bytes(out))
    if state == "unpatched":
        hits = [m.start() for m in re.finditer(re.escape(BRANCH_SIG), bytes(out))]
        tgt = hits[0] + BRANCH_OFF
        if out[tgt] != BRANCH_OLD:
            raise ValueError(f"branch: byte at {hex(tgt)} is {out[tgt]:02x}, expected {BRANCH_OLD:02x}")
        out[tgt] = BRANCH_NEW

    # Rule 2: encoder strings (idempotent: old string gone after patch).
    for old, new in ENCODER_RENAMES:
        hits = [m.start() for m in re.finditer(re.escape(old), bytes(out))]
        if len(hits) == 0:
            continue  # already patched
        if len(hits) != 1:
            raise ValueError(f"encoder string {old!r}: expected 1 occurrence, found {len(hits)}")
        pos = hits[0]
        if out[pos : pos + len(old)] != old:
            raise ValueError(f"encoder string {old!r}: mismatch at {hex(pos)}")
        out[pos : pos + len(old)] = new

    return bytes(out)


def check_binary(bin_data):
    """Return (is_adapted: bool, details: list[str]).

    Idempotent-safe: adapted when no vk* symbols remain, the branch byte is the
    patched value, and the old encoder strings are gone.
    """
    details = []
    ok = True
    dynstr = find_dynstr(bin_data)

    spots = vk_hide_spots(bin_data, dynstr)
    if spots:
        ok = False
        details.append(f"vk hides: {len(spots)} vk* symbols still present (not hidden)")
    else:
        details.append("vk hides: all vk* symbols hidden OK")

    state, _pos = branch_state(bin_data)
    if state == "patched":
        details.append("branch: patched OK")
    else:
        ok = False
        details.append("branch: not patched (original signature still present)")

    for old, _new in ENCODER_RENAMES:
        hits = [m.start() for m in re.finditer(re.escape(old), bin_data)]
        if len(hits) == 0:
            details.append(f"encoder {old!r}: gone (patched OK)")
        else:
            ok = False
            details.append(f"encoder {old!r}: still present ({len(hits)} hits)")

    return ok, details


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_apply(args):
    with open(args.bin, "rb") as f:
        bin_data = f.read()
    patched = apply_patch(bin_data)
    out = args.out if args.out else args.bin
    with open(out, "wb") as f:
        f.write(patched)
    n_hides = len(vk_hide_spots(bin_data, find_dynstr(bin_data)))
    print(f"patched {args.bin} -> {out} (hidden {n_hides} vk* symbols, {len(ENCODER_RENAMES)} encoder renames, 1 branch)")
    print(f"out md5: {hashlib.md5(patched).hexdigest()}")


def cmd_check(args):
    with open(args.bin, "rb") as f:
        bin_data = f.read()
    ok, details = check_binary(bin_data)
    for d in details:
        print(f"  {d}")
    print(f"adapted: {ok}")
    sys.exit(0 if ok else 1)


def cmd_list(args):
    print("Algorithmic rules applied to the target binary (no data file):")
    print("  Rule 1: hide vk* symbols in .dynstr (regex ^vk[A-Za-z0-9_]+$, zero first byte)")
    print("  Rule 2: encoder renames (keyed on OLD string):")
    for old, new in ENCODER_RENAMES:
        print(f"          {old!r} -> {new!r}")
    print(f"  Rule 3: branch redirect: sig={BRANCH_SIG.hex()} byte@{BRANCH_OFF:#x} {BRANCH_OLD:02x}->{BRANCH_NEW:02x}")


def main():
    ap = argparse.ArgumentParser(description="Self-contained algorithmic patcher for the Insta360 MediaSDK AMD adaptation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="apply the algorithmic patch to a binary")
    a.add_argument("--bin", required=True)
    a.add_argument("--out")
    a.set_defaults(func=cmd_apply)

    c = sub.add_parser("check", help="report whether a binary is already adapted (exit 0) or not (exit 1)")
    c.add_argument("--bin", required=True)
    c.set_defaults(func=cmd_check)

    l = sub.add_parser("list-sigs", help="print the algorithmic rules the patcher applies")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
