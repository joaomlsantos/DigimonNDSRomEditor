"""One-shot repair tool for BTCHR appended-sidecar corruption.

Background
----------
Pre-fix (commit "memory_unbound"), ``RomSession.current_chrsize_word`` /
``current_btchrsize_value`` did not consult ``_btchr_appended_chrsize`` /
``_btchr_appended_btchrsize`` for groups past the vanilla 415. They fell
through to ``original_rom_data`` and ``struct.unpack_from``'d past the
end of the 1660-byte sidecar file into NDS 0x200-aligned 0xFF padding,
returning 0xFFFFFFFF.

``AppendBtchrGroupCommand`` snapshots a source group's sidecar values
in ``__init__`` and writes them back as the new group's sidecar pair.
So any "Duplicate sprite entry" with an already-appended group selected
wrote ``chrsize=0xFFFFFFFF, btchrsize=0xFFFFFFFF``. btchrsize=0xFFFFFFFF
asks the engine for a ~4 GB load buffer → instant crash whenever the
game tries to render that sprite.

This script repairs an already-saved ``.romproj`` that has one or more
of these poisoned sidecars. The editor fix prevents future corruption;
this fixes the past.

Subcommands
-----------
``recompute`` — Walk every appended sidecar, find the ones equal to
0xFFFFFFFF/0xFFFFFFFF, and recompute their values from the matching
PAK entries stored in ``sprite_edits``:

  - ``btchrsize`` = sum of uncompressed lengths of entries 1..4 (NCGR,
    NCLR, NCER, NANR — the mini-header at entry 0 does NOT count).
  - ``chrsize`` = ``(digimon_id | (tpf << 16))`` where ``tpf`` is
    ``ncgr_tile_count // 5`` (the ``tile_count_div5`` semantic that
    every vanilla group obeys). ``digimon_id`` defaults to 0x000 unless
    ``--digimon-id`` is given; the engine doesn't use this field as a
    lookup key on any code path the project has reverse-engineered, so
    the default is harmless but you can pick a meaningful id if you want
    the BTCHR browser's left-list label to read something specific.

``drop`` — Remove a broken group entirely. Pops sidecar K, drops the 5
PAK entries at indices ``(415+K)*5 .. (415+K)*5+4`` from sprite_edits,
renumbers every higher-indexed BTCHR entry down by 5, and rewrites the
sprite_map diffs:

  - any slot whose main_sprite pointed at the dropped group has its
    diff deleted (slot reverts to vanilla main_sprite — pick a new
    target manually later);
  - any slot whose main_sprite pointed at a group above the dropped
    one is decremented by 1 to track the shift.

By default ``drop`` operates on the FIRST corrupt sidecar it finds.
Pass ``--group <N>`` to target a specific appended group index (e.g.
``--group 424``); this also works for healthy groups if you want to
remove an unused append.

Usage
-----
  python -m digimon_core.tests._repair_btchr_appended recompute IN.romproj OUT.romproj
  python -m digimon_core.tests._repair_btchr_appended recompute IN.romproj OUT.romproj --digimon-id 0x42
  python -m digimon_core.tests._repair_btchr_appended drop      IN.romproj OUT.romproj
  python -m digimon_core.tests._repair_btchr_appended drop      IN.romproj OUT.romproj --group 424
  python -m digimon_core.tests._repair_btchr_appended inspect   IN.romproj

The ``inspect`` subcommand prints the current state without writing.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, sprite  # noqa: E402


VANILLA_GROUP_COUNT = 415
BTCHR_PAK = "DAT/BTCHR.PAK"
# DUSK_US sprite_map offset; constants.py exposes per-region values but
# this repair tool only runs on a specific .romproj so hard-coding the
# region detection on the rom_version field below is enough.
SPRITE_MAP_OFFSETS = {
    "DUSK_US": 0xFCE04,
    "DAWN_US": None,  # filled lazily from constants if needed
}


def _load_sprite_map_bounds(rom_version: str) -> Tuple[int, int]:
    """Return ``(sm_start, sm_end)`` for ``rom_version``.

    Resolves via ``digimon_core.constants`` so this stays in sync with
    the editor without us copy-pasting numbers. We need both ends so the
    "is this diff inside the sprite_map?" filter doesn't accidentally
    rewrite unrelated diffs past the table.
    """
    from digimon_core import constants
    table = constants.SPRITE_MAPPING_TABLE_OFFSET
    if rom_version not in table:
        raise SystemExit(
            f"unknown rom_version {rom_version!r}; "
            f"known: {sorted(table.keys())}"
        )
    return table[rom_version]


# ---- helpers ---------------------------------------------------------------


def _b64_to_int_le(b64: str) -> int:
    return int.from_bytes(base64.b64decode(b64), "little")


def _int_to_b64_le(value: int, n_bytes: int) -> str:
    return base64.b64encode(
        (value & ((1 << (n_bytes * 8)) - 1)).to_bytes(n_bytes, "little")
    ).decode("ascii")


def _index_btchr_entries(
    sprite_edits: List[dict],
) -> Dict[int, Tuple[int, bytes]]:
    """Return ``{entry_idx: (list_position, raw_bytes)}`` for BTCHR edits."""
    out: Dict[int, Tuple[int, bytes]] = {}
    for i, e in enumerate(sprite_edits):
        if e.get("pak") != BTCHR_PAK:
            continue
        out[e["idx"]] = (i, base64.b64decode(e["bytes"]))
    return out


def _recompute_one(
    sidecar_idx: int,
    btchr_entries: Dict[int, Tuple[int, bytes]],
    digimon_id: int,
) -> Tuple[int, int]:
    """Recompute (chrsize_word, btchrsize_value) for appended sidecar K.

    ``btchr_entries`` is the dict from :func:`_index_btchr_entries`.
    Raises ``KeyError`` if any of the 5 PAK entries is missing.
    """
    group = VANILLA_GROUP_COUNT + sidecar_idx
    base = group * btchr.GROUP_SIZE
    raws = []
    for i in range(btchr.GROUP_SIZE):
        if base + i not in btchr_entries:
            raise KeyError(
                f"sprite_edits is missing PAK entry {base + i} "
                f"for appended group {group} — cannot recompute"
            )
        raws.append(btchr_entries[base + i][1])

    # btchrsize = sum of uncompressed lengths of entries 1..4.
    btchrsize = sum(len(sprite.decompress_rle30(r)) for r in raws[1:])

    # tpf = ncgr_tile_count // GROUP_SIZE (cells per digimon).
    ncgr_raw = sprite.decompress_rle30(raws[1])
    rahc = sprite.find_block(ncgr_raw, b"RAHC")
    tile_byte_count = struct.unpack_from("<I", ncgr_raw, rahc + 24)[0]
    n_tiles = tile_byte_count // btchr.BYTES_PER_TILE_8BPP
    tpf = n_tiles // btchr.GROUP_SIZE
    if tpf * btchr.GROUP_SIZE != n_tiles:
        # Not a hard error — the engine reads tpf as a u16 and doesn't
        # require it to evenly divide the tile bank — but it's worth
        # flagging because every vanilla group does evenly divide.
        print(
            f"  WARN: appended group {group} has {n_tiles} tiles which is "
            f"not a multiple of {btchr.GROUP_SIZE}; tpf rounded down to {tpf}",
            file=sys.stderr,
        )

    chrsize = (digimon_id & 0xFFFF) | ((tpf & 0xFFFF) << 16)
    return chrsize, btchrsize


# ---- inspect ---------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> None:
    proj = json.load(open(args.input, "r", encoding="utf-8"))
    sidecars = proj.get("btchr_appended_sidecars", [])
    sprite_edits = proj.get("sprite_edits", [])
    rom_version = proj.get("rom_version", "DUSK_US")
    sm_start, sm_end = _load_sprite_map_bounds(rom_version)

    print(f"input    : {args.input}")
    print(
        f"region   : {rom_version}  "
        f"(sprite_map 0x{sm_start:x}..0x{sm_end:x})"
    )
    print(f"sidecars : {len(sidecars)} appended")
    btchr_entries = _index_btchr_entries(sprite_edits)
    print(f"BTCHR PAK entries in sprite_edits: {len(btchr_entries)}")
    print()
    print(
        f"  {'idx':>3} {'group':>5}  {'chrsize':>10} {'btchrsize':>10}  "
        f"  status"
    )
    for k, sc in enumerate(sidecars):
        chr_v = sc["chrsize"]
        bsz_v = sc["btchrsize"]
        broken = chr_v == 0xFFFFFFFF or bsz_v == 0xFFFFFFFF
        flag = "  <-- BROKEN" if broken else ""
        print(
            f"  {k:>3} {VANILLA_GROUP_COUNT + k:>5}  "
            f"0x{chr_v:08x} 0x{bsz_v:08x}  {flag}"
        )
    print()

    # sprite_map repoints to appended groups
    diffs = proj.get("diffs", [])
    hits = []
    for d in diffs:
        off = d["offset"]
        if not (sm_start <= off < sm_end):
            continue
        rel = off - sm_start
        slot = rel // 16
        field_off = rel % 16
        if field_off != 8:  # main_sprite
            continue
        val = _b64_to_int_le(d["bytes"])
        if val >= VANILLA_GROUP_COUNT:
            hits.append((slot, val, val - VANILLA_GROUP_COUNT))
    if hits:
        print("sprite_map main_sprite repoints to appended groups:")
        for slot, val, sc_idx in sorted(hits):
            print(
                f"  slot 0x{slot:03x} -> group {val} "
                f"(sidecar idx {sc_idx})"
            )


# ---- recompute -------------------------------------------------------------


def cmd_recompute(args: argparse.Namespace) -> None:
    proj = json.load(open(args.input, "r", encoding="utf-8"))
    sidecars = proj.get("btchr_appended_sidecars", [])
    sprite_edits = proj.get("sprite_edits", [])
    btchr_entries = _index_btchr_entries(sprite_edits)

    broken = [
        k for k, sc in enumerate(sidecars)
        if sc["chrsize"] == 0xFFFFFFFF or sc["btchrsize"] == 0xFFFFFFFF
    ]
    if not broken:
        print("No 0xFFFFFFFF sidecars found — nothing to do.")
        return

    print(f"Found {len(broken)} broken sidecar(s): {broken}")
    for k in broken:
        try:
            new_chr, new_bsz = _recompute_one(
                k, btchr_entries, args.digimon_id,
            )
        except KeyError as exc:
            raise SystemExit(f"  ERROR: {exc}") from exc
        old = sidecars[k]
        print(
            f"  sidecar #{k} (group {VANILLA_GROUP_COUNT + k}): "
            f"chrsize 0x{old['chrsize']:08x} -> 0x{new_chr:08x}  "
            f"btchrsize 0x{old['btchrsize']:08x} -> 0x{new_bsz:08x}"
        )
        sidecars[k] = {"chrsize": new_chr, "btchrsize": new_bsz}

    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        json.dump(proj, fh, indent=2)
    print(f"Wrote {args.output}")


# ---- drop ------------------------------------------------------------------


def cmd_drop(args: argparse.Namespace) -> None:
    proj = json.load(open(args.input, "r", encoding="utf-8"))
    sidecars = proj.get("btchr_appended_sidecars", [])
    sprite_edits = proj.get("sprite_edits", [])
    diffs = proj.get("diffs", [])
    rom_version = proj.get("rom_version", "DUSK_US")
    sm_start, sm_end = _load_sprite_map_bounds(rom_version)

    if not sidecars:
        raise SystemExit("No appended sidecars in this project.")

    if args.group is None:
        target_k = next(
            (k for k, sc in enumerate(sidecars)
             if sc["chrsize"] == 0xFFFFFFFF or sc["btchrsize"] == 0xFFFFFFFF),
            None,
        )
        if target_k is None:
            raise SystemExit(
                "No broken sidecars found. Pass --group <N> to drop a "
                "specific appended group."
            )
    else:
        target_k = args.group - VANILLA_GROUP_COUNT
        if not (0 <= target_k < len(sidecars)):
            raise SystemExit(
                f"--group {args.group} is not an appended group "
                f"(valid range: {VANILLA_GROUP_COUNT}..{VANILLA_GROUP_COUNT + len(sidecars) - 1})"
            )

    dropped_group = VANILLA_GROUP_COUNT + target_k
    print(f"Dropping appended group {dropped_group} (sidecar idx {target_k}).")
    print(f"  sidecar before: {sidecars[target_k]}")

    sidecars.pop(target_k)

    # Drop the 5 PAK entries; renumber any higher BTCHR entries down by 5.
    drop_start = dropped_group * btchr.GROUP_SIZE
    drop_end = drop_start + btchr.GROUP_SIZE
    kept_sprite_edits: List[dict] = []
    n_dropped_entries = 0
    n_renumbered_entries = 0
    for e in sprite_edits:
        if e.get("pak") == BTCHR_PAK:
            idx = e["idx"]
            if drop_start <= idx < drop_end:
                n_dropped_entries += 1
                continue
            if idx >= drop_end:
                e = dict(e)
                e["idx"] = idx - btchr.GROUP_SIZE
                n_renumbered_entries += 1
        kept_sprite_edits.append(e)
    proj["sprite_edits"] = kept_sprite_edits
    print(
        f"  PAK entries dropped: {n_dropped_entries}; "
        f"renumbered down by {btchr.GROUP_SIZE}: {n_renumbered_entries}"
    )

    # sprite_map diffs: drop main_sprite==dropped_group, decrement main_sprite>dropped_group.
    kept_diffs: List[dict] = []
    n_diffs_dropped = 0
    n_diffs_decremented = 0
    for d in diffs:
        off = d["offset"]
        if not (sm_start <= off < sm_end):
            kept_diffs.append(d)
            continue
        rel = off - sm_start
        if rel % 16 != 8:  # only main_sprite field
            kept_diffs.append(d)
            continue
        n_bytes = len(base64.b64decode(d["bytes"]))
        val = _b64_to_int_le(d["bytes"])
        if val == dropped_group:
            n_diffs_dropped += 1
            continue  # drop this diff (slot reverts to vanilla)
        if val > dropped_group and val >= VANILLA_GROUP_COUNT:
            d = dict(d)
            d["bytes"] = _int_to_b64_le(val - 1, n_bytes)
            n_diffs_decremented += 1
        kept_diffs.append(d)
    proj["diffs"] = kept_diffs
    print(
        f"  sprite_map diffs dropped: {n_diffs_dropped}; "
        f"decremented: {n_diffs_decremented}"
    )

    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        json.dump(proj, fh, indent=2)
    print(f"Wrote {args.output}")


# ---- CLI -------------------------------------------------------------------


def main(argv: List[str] = None) -> None:
    p = argparse.ArgumentParser(
        prog="_repair_btchr_appended",
        description="Repair 0xFFFFFFFF BTCHR appended-sidecar corruption.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ins = sub.add_parser("inspect", help="show appended-sidecar state")
    p_ins.add_argument("input")
    p_ins.set_defaults(func=cmd_inspect)

    p_rec = sub.add_parser(
        "recompute",
        help="recompute every 0xFFFFFFFF sidecar from the PAK entries",
    )
    p_rec.add_argument("input")
    p_rec.add_argument("output")
    p_rec.add_argument(
        "--digimon-id", type=lambda x: int(x, 0), default=0x000,
        help="digimon_id to stamp into chrsize.lo (default 0x000)",
    )
    p_rec.set_defaults(func=cmd_recompute)

    p_drop = sub.add_parser("drop", help="remove an appended group entirely")
    p_drop.add_argument("input")
    p_drop.add_argument("output")
    p_drop.add_argument(
        "--group", type=lambda x: int(x, 0), default=None,
        help="absolute group index to drop (default: first broken sidecar)",
    )
    p_drop.set_defaults(func=cmd_drop)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
