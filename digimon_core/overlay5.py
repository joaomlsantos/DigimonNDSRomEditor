"""Overlay9_5 script codec — entry pointer table + per-map field scripts.

Overlay 9_5 is the ARM9 overlay that ships every field map's event /
cutscene / dialog script. The file lives outside the FAT-listed
``DAT/`` tree: it's referenced by ROM-header ``0x50`` (ARM9 overlay
table). Each 32-byte overlay table entry stores a u32 file id whose
``(start, end)`` FAT slot holds the overlay bytes.

File layout (verified via ``research_docs/claude_notes/overlay5_*``):

* ``u32`` ``hdr_size`` at offset 0.
* ``(hdr_size - 4) // 4`` ``u32`` pointers, indexed 0..N-1. Pointer N
  is the offset into the overlay file at which entry N's script body
  begins.
* Entry bodies are packed back-to-back in pointer-sorted order; the
  per-entry length is the next-sorted-pointer minus this one (or
  ``len(overlay) - ptr`` for the final entry).

For the Dusk US ROM there are 505 entries. Entries 235..499 map 1-to-1
to field maps 0..264 (verified — see :func:`map_id_for`); 0..234 and
500..504 are non-map (cutscene / system / global) scripts.

Some entries share the same target offset (the heuristic detects
sub-blocks by sorted pointer order, so duplicates collapse). Vanilla
duplicates include the 17 farm-island maps (entries 288..304 → entry
235's blob = map 0 placeholder). Mutations follow the FIRST owner of
each shared blob; later duplicates surface the same bytes unchanged.

Script opcodes are 2-byte little-endian markers; opcode 0x0150 is the
``OVERWORLD_SPRITE`` placement we care about for the Events tab. Its
full 26-byte block is:

.. code-block::

   offset 0  u16  opcode (always 0x0150)
   offset 2  u16  overworld_sprite_id  (MCHR sprite index — the engine
                  resolves this to a base sprite_map id by reverse-
                  lookup of sprite_map[base_id].unknown_0x4)
   offset 4  u16  x  (pixel-space, top-left = 0,0)
   offset 6  u16  y  (pixel-space)
   offset 8  u16  slot (per-entry id, monotonic within an entry)
   offset 10 u16  facing (=0x0100 across all vanilla observations)
   offset 12 u16  reserved (=0)
   offset 14 u16  slot_again (mirrors offset 8)
   offset 16 u32  string_ptr (offset into the same entry's script,
                  0 if the sprite has no dialog)
   offset 20 u16  radius (=0x0064 across all vanilla observations)
   offset 22 u16  slot_again2 (mirrors offset 8)
   offset 24 u16  behavior (0x0003 or 0x0004 — likely
                  "interact" vs "always-on" but unverified)

X/Y verified against the Dark Market render (entry 0264, map 29) in
``research_docs/claude_notes/_overlay5_annotated/0264_sprite_overlay.py``:
all 7 sprites land on visible NPC / shop-counter spots.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import fnt


# ---- entry index <-> field map id mapping --------------------------------

# Matches research_docs/claude_notes/overlay5_annotate.py constants —
# entry index N -> destination_id N+1, and entries 235..499 line up
# 1-to-1 with field-map ids 0..264.
MAP_INDEX_OFFSET = 235
MAP_INDEX_MAX = 500      # exclusive upper bound (entries 235..499 = maps 0..264)
MAX_MAP_ID = MAP_INDEX_MAX - 1 - MAP_INDEX_OFFSET  # 264

# Field-map id 264's entry (0499) overshoots the next sorted pointer by
# 117 KB — that span is shared cutscene content, not the per-map script.
# The map's own prologue ends at END_SCRIPT @0x00c6 (the two HITBOX dst
# scripts at +0x015a / +0x0176 terminate cleanly without looping back),
# so we trim entry 499 to its prologue when feeding the Events scanners.
ENTRY_499_PROLOGUE_END = 0x00C8

# Opcode + total block size for the OVERWORLD_SPRITE placement.
OVERWORLD_SPRITE_OPCODE = 0x0150
OVERWORLD_SPRITE_BLOCK_SIZE = 26

# Exit zones / spawn points. Every field-map entry starts (just past the
# 4-byte map header) with a contiguous run of 18-byte 0x001b blocks:
# rectangular trigger zones with a u32 ``dst_file_off`` pointing at a
# handler script. Zones with ``dst_file_off == 0`` are spawn-arrival
# markers (the player lands at ``(x1, y1)`` when entering this map);
# the box extent on those is usually degenerate or single-axis-zero
# but the engine appears to ignore (x2, y2) for spawns.
#
# Zones with non-zero ``dst`` split into two kinds, distinguished by
# whether the handler decodes as the stereotyped fade+call prefix
# (:class:`ExitHandler`):
#  - fade+call → standard map exit (loads another map; the ``flag``
#    u16 varies per destination and isn't load-bearing for the
#    "is this an exit" test — confirmed by entry 0259 carrying three
#    different flag values 0x03/0x06/0x09 across three real map exits).
#  - anything else → bespoke interaction script (sign, NPC trigger,
#    locked gate). Editor surfaces these as read-only "hitboxes".
EXIT_ZONE_OPCODE = 0x001b
EXIT_ZONE_BLOCK_SIZE = 18

# Exit handler script prefix. The handler each non-spawn 0x001b block
# jumps to begins with a stereotyped 12-byte pair:
#   u16 op 0x0002 + u32 spawn_arg + u16 op 0x0030 + u32 dest_file_off
# ``dest_file_off`` is the absolute overlay5 file offset of the
# destination entry's start (CALL_SCRIPT_AT_OFFSET; see project memory
# ``project_op_0x0030_call_script``). ``spawn_arg``'s meaning is
# currently unknown — likely a spawn-side selector in the destination,
# surfaced as a raw editable u32 so the user can tweak it without us
# pretending we've decoded it.
EXIT_HANDLER_FADE_OPCODE = 0x0002
EXIT_HANDLER_CALL_OPCODE = 0x0030
EXIT_HANDLER_PREFIX_SIZE = 12


def map_id_for(entry_ix: int) -> Optional[int]:
    """Field-map id this entry corresponds to, or None when it's a
    non-map (system / cutscene / global) script. Entry 0499's prologue
    is included (map 264) — its 117 KB body is shared cutscene content
    and is trimmed by :func:`script_prologue_bytes` before scanning."""
    if MAP_INDEX_OFFSET <= entry_ix < MAP_INDEX_MAX:
        return entry_ix - MAP_INDEX_OFFSET
    return None


def entry_ix_for_map(map_id: int) -> Optional[int]:
    """Inverse of :func:`map_id_for`. Returns the entry index that owns
    ``map_id``'s script, or None if the map id is outside the editable
    range (maps 0..264)."""
    ix = map_id + MAP_INDEX_OFFSET
    if MAP_INDEX_OFFSET <= ix < MAP_INDEX_MAX:
        return ix
    return None


def script_prologue_bytes(entry_ix: int, entry_bytes: bytes) -> bytes:
    """Bytes the Events scanners should walk for ``entry_ix``.

    For most entries this is ``entry_bytes`` unchanged. Entry 0499 is
    the exception: its 117 KB blob contains cutscene handler scripts
    registered by other maps (via REGISTER_HANDLER), so a byte-by-byte
    OWS / exit-zone scan would surface dozens of cutscene-only
    placements as if they were standing NPCs. The map-264 prologue
    ends at END_SCRIPT @0x00C6; trim there so the Events tab only sees
    the two real OVERWORLD_SPRITE blocks + the SPAWN/EXIT/HITBOX runs.
    """
    if entry_ix == MAP_INDEX_MAX - 1:  # entry 499
        return entry_bytes[:ENTRY_499_PROLOGUE_END]
    return entry_bytes


# ---- placement record ----------------------------------------------------


@dataclass
class OverworldSpritePlacement:
    """One OVERWORLD_SPRITE block decoded from an overlay5 entry.

    ``block_offset`` is the offset of the 26-byte block inside the
    entry's body (NOT inside the overlay file). The Events tab uses
    this as the splice address when the user drags the sprite.
    """
    block_offset: int
    overworld_sprite_id: int
    x: int
    y: int
    slot: int
    facing: int
    reserved: int
    slot_again: int
    string_ptr: int
    radius: int
    slot_again2: int
    behavior: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "OverworldSpritePlacement":
        opcode, overworld_sprite_id, x, y, slot, facing, reserved, slot_again, \
            string_ptr, radius, slot_again2, behavior = struct.unpack_from(
                "<HHHHHHHHIHHH", entry, off,
            )
        assert opcode == OVERWORLD_SPRITE_OPCODE, (
            f"expected 0x{OVERWORLD_SPRITE_OPCODE:04x} at 0x{off:04x}, "
            f"got 0x{opcode:04x}"
        )
        return cls(
            block_offset=off,
            overworld_sprite_id=overworld_sprite_id,
            x=x,
            y=y,
            slot=slot,
            facing=facing,
            reserved=reserved,
            slot_again=slot_again,
            string_ptr=string_ptr,
            radius=radius,
            slot_again2=slot_again2,
            behavior=behavior,
        )


# ---- per-entry parsing ---------------------------------------------------


def iter_overworld_sprites(entry: bytes) -> List[OverworldSpritePlacement]:
    """Scan ``entry`` for OVERWORLD_SPRITE blocks.

    Walks byte-by-byte looking for the opcode; bumps past the 26-byte
    block on a hit so neighboring opcodes inside the payload (e.g. a
    string_ptr that happens to encode 0x0150 in its bytes) don't
    re-trigger. This matches the engine's forward-only script execution
    — there's no nesting to worry about.

    Skips a hit if the block would overrun the entry buffer.
    """
    placements: List[OverworldSpritePlacement] = []
    p = 0
    n = len(entry)
    while p + OVERWORLD_SPRITE_BLOCK_SIZE <= n:
        opcode = struct.unpack_from("<H", entry, p)[0]
        if opcode == OVERWORLD_SPRITE_OPCODE:
            placements.append(OverworldSpritePlacement.from_bytes(entry, p))
            p += OVERWORLD_SPRITE_BLOCK_SIZE
            continue
        p += 1
    return placements


def first_per_sprite_id(
    placements: List[OverworldSpritePlacement],
) -> List[OverworldSpritePlacement]:
    """Filter so each ``overworld_sprite_id`` only appears once (first occurrence).

    The same id can repeat within an entry (e.g. two duplicates of the
    same shopkeeper for cutscene reasons). The Events tab only surfaces
    the first unique placement per project memory
    ``project_users_prefer_romproj``-adjacent UX preference.
    """
    seen: set[int] = set()
    out: List[OverworldSpritePlacement] = []
    for p in placements:
        if p.overworld_sprite_id in seen:
            continue
        seen.add(p.overworld_sprite_id)
        out.append(p)
    return out


def replace_sprite_xy(
    entry: bytes, block_offset: int, x: int, y: int,
) -> bytes:
    """Return a copy of ``entry`` with the OVERWORLD_SPRITE at
    ``block_offset`` rewritten to use ``(x, y)``.

    Validates that ``block_offset`` actually starts on an OVERWORLD_SPRITE
    opcode — silent corruption would be very hard to track down.
    """
    if block_offset + OVERWORLD_SPRITE_BLOCK_SIZE > len(entry):
        raise ValueError(
            f"block_offset 0x{block_offset:04x} overruns entry "
            f"(len={len(entry)})"
        )
    opcode = struct.unpack_from("<H", entry, block_offset)[0]
    if opcode != OVERWORLD_SPRITE_OPCODE:
        raise ValueError(
            f"no OVERWORLD_SPRITE at offset 0x{block_offset:04x} "
            f"(found opcode 0x{opcode:04x})"
        )
    buf = bytearray(entry)
    struct.pack_into("<HH", buf, block_offset + 4, x & 0xFFFF, y & 0xFFFF)
    return bytes(buf)


def replace_sprite_id(
    entry: bytes, block_offset: int, sprite_id: int,
) -> bytes:
    """Return a copy of ``entry`` with the OVERWORLD_SPRITE at
    ``block_offset`` rewritten to use ``sprite_id`` (MCHR index).

    Same opcode-guard as :func:`replace_sprite_xy`; only the u16 at
    offset 2 of the block is touched.
    """
    if block_offset + OVERWORLD_SPRITE_BLOCK_SIZE > len(entry):
        raise ValueError(
            f"block_offset 0x{block_offset:04x} overruns entry "
            f"(len={len(entry)})"
        )
    opcode = struct.unpack_from("<H", entry, block_offset)[0]
    if opcode != OVERWORLD_SPRITE_OPCODE:
        raise ValueError(
            f"no OVERWORLD_SPRITE at offset 0x{block_offset:04x} "
            f"(found opcode 0x{opcode:04x})"
        )
    buf = bytearray(entry)
    struct.pack_into("<H", buf, block_offset + 2, sprite_id & 0xFFFF)
    return bytes(buf)


def replace_sprite_behavior(
    entry: bytes, block_offset: int, behavior: int,
) -> bytes:
    """Return a copy of ``entry`` with the OVERWORLD_SPRITE's behavior
    u16 at ``block_offset + 24`` rewritten to ``behavior``.

    Observed values cluster on 0x0003 / 0x0004 — small ints that line up
    with the MCHR frame index the in-game placement renders with (the
    canonical front-facing pose lives at frame 3 for most entries). The
    Events sidebar surfaces this as the sprite's display frame.
    """
    if block_offset + OVERWORLD_SPRITE_BLOCK_SIZE > len(entry):
        raise ValueError(
            f"block_offset 0x{block_offset:04x} overruns entry "
            f"(len={len(entry)})"
        )
    opcode = struct.unpack_from("<H", entry, block_offset)[0]
    if opcode != OVERWORLD_SPRITE_OPCODE:
        raise ValueError(
            f"no OVERWORLD_SPRITE at offset 0x{block_offset:04x} "
            f"(found opcode 0x{opcode:04x})"
        )
    buf = bytearray(entry)
    struct.pack_into("<H", buf, block_offset + 24, behavior & 0xFFFF)
    return bytes(buf)


# ---- exit zones / spawn points -------------------------------------------


@dataclass
class ExitZone:
    """One 0x001b block decoded from the head of an overlay5 entry.

    ``block_offset`` is the offset of the 18-byte block inside the
    entry's body — used as the splice address when editing the box.
    ``dst_file_off`` is an absolute overlay5 file offset pointing at a
    handler script (see :class:`ExitHandler`); it's zero for spawn
    points (``is_spawn``), which are arrival markers for where the
    player lands when a different map's exit lands here. Spawn box
    extents are usually degenerate (point boxes or single-axis 1-tile
    offsets); the engine appears to read only ``(x1, y1)`` for spawns.
    """
    block_offset: int
    idx: int
    x1: int
    y1: int
    x2: int
    y2: int
    flag: int
    dst_file_off: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "ExitZone":
        opcode, idx, x1, y1, x2, y2, flag, dst = struct.unpack_from(
            "<HHHHHHHI", entry, off,
        )
        assert opcode == EXIT_ZONE_OPCODE, (
            f"expected 0x{EXIT_ZONE_OPCODE:04x} at 0x{off:04x}, "
            f"got 0x{opcode:04x}"
        )
        return cls(
            block_offset=off,
            idx=idx,
            x1=x1, y1=y1, x2=x2, y2=y2,
            flag=flag,
            dst_file_off=dst,
        )

    @property
    def is_spawn(self) -> bool:
        # No-handler = spawn-arrival marker. Earlier versions also
        # required x1==x2 and y1==y2, which missed degenerate boxes
        # like entry 0259 idx=11 (x1==x2, y1=y2+1) that the in-game
        # arrival behaviour confirms as a spawn. The engine ignores
        # the box extent when there's no handler — only the upper-
        # left corner is used.
        return self.dst_file_off == 0


@dataclass
class ExitHandler:
    """Decoded 12-byte prefix of an exit handler script.

    Located by subtracting the entry's file offset from an
    :class:`ExitZone`'s ``dst_file_off``. ``spawn_arg_offset`` and
    ``dest_arg_offset`` give the in-entry addresses of the two
    editable u32 fields so the splice helpers can rewrite them in
    place without re-walking the prefix.
    """
    rel_offset: int
    spawn_arg: int
    dest_file_off: int
    spawn_arg_offset: int
    dest_arg_offset: int

    @classmethod
    def from_bytes(
        cls, entry: bytes, rel_offset: int,
    ) -> Optional["ExitHandler"]:
        if rel_offset < 0 or rel_offset + EXIT_HANDLER_PREFIX_SIZE > len(entry):
            return None
        op_fade, spawn_arg, op_call, dest = struct.unpack_from(
            "<HIHI", entry, rel_offset,
        )
        if (op_fade != EXIT_HANDLER_FADE_OPCODE
                or op_call != EXIT_HANDLER_CALL_OPCODE):
            return None
        return cls(
            rel_offset=rel_offset,
            spawn_arg=spawn_arg,
            dest_file_off=dest,
            spawn_arg_offset=rel_offset + 2,
            dest_arg_offset=rel_offset + 8,
        )


# Opcodes that can be interleaved with 0x001b in the prologue region of
# a field-map entry. Sized by inspection across all 264 vanilla entries
# (see scripts in research_docs/claude_notes/_overlay5_split): every
# walker stop after expanding this set fell on a rare opcode in <3
# entries or on a clear misalignment, so the coverage is 2257/2257 exit
# zones across 262/264 entries — the two skipped entries (farm island
# placeholders) have no zones to find.
_PROLOGUE_OPCODE_SIZES: Dict[int, int] = {
    EXIT_ZONE_OPCODE: EXIT_ZONE_BLOCK_SIZE,
    0x0004: 6,
    0x0006: 6,
    0x0009: 4,
    0x00a6: 10,
    0x00a7: 10,
    0x014d: 10,
}


def iter_exit_zones(entry: bytes) -> List[ExitZone]:
    """Walk the prologue region of ``entry`` and collect 0x001b blocks.

    Exit zones aren't always a single contiguous run — entries can
    interleave setup opcodes (0x0004, 0x0006, 0x0009, 0x00a6, 0x00a7,
    0x014d) between groups of zones. The walker hops past each known
    prologue opcode using :data:`_PROLOGUE_OPCODE_SIZES` and stops at
    the first opcode it doesn't recognize (typically the first
    OVERWORLD_SPRITE 0x0150). Anything past that lives in the script
    body proper and isn't an exit-zone definition.
    """
    zones: List[ExitZone] = []
    off = 4  # past the 4-byte map header (opcode 0x014c + map_id u16)
    n = len(entry)
    while off + 2 <= n:
        opcode = struct.unpack_from("<H", entry, off)[0]
        size = _PROLOGUE_OPCODE_SIZES.get(opcode)
        if size is None or off + size > n:
            break
        if opcode == EXIT_ZONE_OPCODE:
            zones.append(ExitZone.from_bytes(entry, off))
        off += size
    return zones


def replace_exit_box(
    entry: bytes, block_offset: int,
    x1: int, y1: int, x2: int, y2: int,
) -> bytes:
    """Rewrite the four box u16s of the EXIT_ZONE at ``block_offset``.

    Idx / flag / dst u32 stay intact; only the four corners are touched.
    """
    if block_offset + EXIT_ZONE_BLOCK_SIZE > len(entry):
        raise ValueError(
            f"block_offset 0x{block_offset:04x} overruns entry "
            f"(len={len(entry)})"
        )
    opcode = struct.unpack_from("<H", entry, block_offset)[0]
    if opcode != EXIT_ZONE_OPCODE:
        raise ValueError(
            f"no EXIT_ZONE at offset 0x{block_offset:04x} "
            f"(found opcode 0x{opcode:04x})"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<HHHH", buf, block_offset + 4,
        x1 & 0xFFFF, y1 & 0xFFFF, x2 & 0xFFFF, y2 & 0xFFFF,
    )
    return bytes(buf)


def replace_exit_handler_dest(
    entry: bytes, handler_rel_offset: int, new_dest_file_off: int,
) -> bytes:
    """Rewrite the handler's op 0x0030 u32 to ``new_dest_file_off``.

    Validates the full op 0x0002 + u32 + op 0x0030 prefix before
    touching anything — silent corruption of the script stream would
    be impossible to track down.
    """
    handler = ExitHandler.from_bytes(entry, handler_rel_offset)
    if handler is None:
        raise ValueError(
            f"no exit handler at offset 0x{handler_rel_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<I", buf, handler.dest_arg_offset,
        new_dest_file_off & 0xFFFFFFFF,
    )
    return bytes(buf)


def replace_exit_handler_spawn_arg(
    entry: bytes, handler_rel_offset: int, new_spawn_arg: int,
) -> bytes:
    """Rewrite the handler's op 0x0002 u32 to ``new_spawn_arg``.

    Same prefix-shape guard as :func:`replace_exit_handler_dest`.
    """
    handler = ExitHandler.from_bytes(entry, handler_rel_offset)
    if handler is None:
        raise ValueError(
            f"no exit handler at offset 0x{handler_rel_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<I", buf, handler.spawn_arg_offset,
        new_spawn_arg & 0xFFFFFFFF,
    )
    return bytes(buf)


# ---- dialog blocks -------------------------------------------------------

# Dialog block layout — 12 bytes, three sentinel opcodes wrapping the
# three editable u16s. Verified against the standalone annotator
# (research_docs/claude_notes/overlay5_annotate.py:try_dialog) and the
# project memory ``project_dialog_target_is_sprite_slot``.
#   off 0  u16  op 0x009A  (DIALOG_BEGIN — ``target`` follows)
#   off 2  u16  target     (OVERWORLD_SPRITE.slot of the speaker)
#   off 4  u16  op 0x009E  (set msg+portrait)
#   off 6  u16  msg_id     (MSG.PAK string id)
#   off 8  u16  portrait   (portrait_ids.txt entry — usually a digimon)
#   off 10 u16  op 0x009C  (DIALOG_END)
DIALOG_BEGIN_OPCODE = 0x009A
DIALOG_SETMSG_OPCODE = 0x009E
DIALOG_END_OPCODE = 0x009C
DIALOG_BLOCK_SIZE = 12


@dataclass
class DialogBlock:
    """One 12-byte dialog block parsed out of an entry's script body.

    ``block_offset`` is the in-entry offset (suitable for splice helpers).
    ``target`` matches an OVERWORLD_SPRITE.slot — i.e. *who is speaking*.
    """
    block_offset: int
    target: int
    msg_id: int
    portrait: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "DialogBlock":
        op_begin, target, op_setmsg, msg_id, portrait, op_end = (
            struct.unpack_from("<HHHHHH", entry, off)
        )
        if (op_begin != DIALOG_BEGIN_OPCODE
                or op_setmsg != DIALOG_SETMSG_OPCODE
                or op_end != DIALOG_END_OPCODE):
            raise ValueError(
                f"not a dialog block at 0x{off:04x} "
                f"(opcodes {op_begin:04x}/{op_setmsg:04x}/{op_end:04x})"
            )
        return cls(
            block_offset=off,
            target=target,
            msg_id=msg_id,
            portrait=portrait,
        )


def _is_dialog_at(entry: bytes, off: int) -> bool:
    """Cheap shape check — true when the 12 bytes at ``off`` line up as
    a dialog block's three sentinel opcodes."""
    if off < 0 or off + DIALOG_BLOCK_SIZE > len(entry):
        return False
    op_begin, op_setmsg, op_end = struct.unpack_from(
        "<H2xH4xH", entry, off,
    )
    return (
        op_begin == DIALOG_BEGIN_OPCODE
        and op_setmsg == DIALOG_SETMSG_OPCODE
        and op_end == DIALOG_END_OPCODE
    )


# Known fixed-size opcodes that can appear interleaved with DIALOG blocks
# in a sprite/hitbox script body or a cutscene handler chain. Sizes are
# shared with the cutscene_index decoder
# (``overlay5_cutscenes._BODY_FIXED_OPCODE_SIZES``) which walks every
# owner entry in overlay5 and produces a verified-consistent slice, so
# borrowing its size table here is safe.
_DIALOG_SKIP_OPCODE_SIZES: Dict[int, int] = {
    0x0002: 6,    # CALL_SYS
    0x0004: 6,    # REGISTER_HANDLER (prologue, but may appear in body)
    0x0005: 6,    # SET_VAR_ALT
    0x0006: 6,
    0x0009: 4,
    0x0015: 6,    # SET_VAR
    0x0030: 6,    # CALL_SCRIPT_AT_OFFSET (mid-region call; the tail-call
                  # variant terminates the region before we reach it)
    0x003C: 4,    # MOVE_BEGIN
    0x003E: 4,    # MOVE_END
    0x0076: 4,    # MOVE_OPEN
    0x00a6: 10,
    0x00a7: 10,
    0x00BB: 4,    # OPEN_SHOP (re-identified by the cutscene decoder; was
                  # previously misread as a script-end marker, which made
                  # the walker bail just before shop NPCs' dialog tails)
    0x00C0: 6,    # REACTION_BALLOON
    0x014b: 4,    # LOAD_TOP_SCREEN_MAP (DS dual-screen cutscenes)
    0x014c: 4,    # LOAD_BOTTOM_SCREEN_MAP (also the prologue header at
                  # offset 0 of every entry; body occurrences appear in
                  # mid-script map swaps)
    0x014d: 10,
    0x0150: 26,   # OVERWORLD_SPRITE — appears inside handler regions
                  # that spawn cutscene NPCs (e.g. entry 0499 chains)
    0x01a6: 2,
}
# Opcodes that terminate dialog collection. 0x0003 is END_SCRIPT — within
# a sliced cutscene region it's the boundary, and inside a sprite/hitbox
# script body it ends the script. Either way the walker should stop
# rather than bleed into the next region's bytes.
_DIALOG_TERMINATOR_OPCODES: frozenset = frozenset({0x0003})

# Inline-scan size of a BATTLE block (variable length): the cutscene
# decoder matches ``DA 00 [5×u16] D7 00 ... D8 00 [bg:2] D9 00 [music:2]
# BA 00``. We don't need the contents — just the consumed length so the
# dialog walker can step past it cleanly.
_BATTLE_HEADER_OPCODE = 0xDA
_BATTLE_MAX_D7_PAYLOAD = 64


def _try_skip_battle(entry: bytes, p: int) -> Optional[int]:
    """If ``entry[p:]`` begins with a BATTLE block, return its length.

    Mirrors ``_EntryDecoder._try_battle`` in
    :mod:`digimon_core.overlay5_cutscenes` — we only need the length to
    skip past it without misalignment.
    """
    n = len(entry)
    if p + 12 > n or entry[p] != _BATTLE_HEADER_OPCODE or entry[p + 1] != 0x00:
        return None
    cursor = p + 12  # past DA 00 + 5 enemy u16s
    if cursor + 2 > n or entry[cursor] != 0xD7 or entry[cursor + 1] != 0x00:
        return None
    cursor += 2
    d7_start = cursor
    while cursor + 1 < n and not (
        entry[cursor] == 0xD8 and entry[cursor + 1] == 0x00
    ):
        cursor += 2
        if cursor - d7_start > _BATTLE_MAX_D7_PAYLOAD:
            return None
    if cursor + 8 > n:
        return None
    cursor += 4  # past D8 00 [bg]
    if entry[cursor] != 0xD9 or entry[cursor + 1] != 0x00:
        return None
    cursor += 4  # past D9 00 [music]
    if entry[cursor] != 0xBA or entry[cursor + 1] != 0x00:
        return None
    return cursor + 2 - p


def iter_dialogs_from_with_meta(
    entry: bytes,
    start_off: int,
    end_off: Optional[int] = None,
) -> Tuple[List[DialogBlock], List[Tuple[int, int]]]:
    """Forgiving dialog walker that also reports unmapped opcodes.

    Behaves like :func:`iter_dialogs_from` (skipping known fixed-size
    body opcodes and BATTLE blocks, stopping on END_SCRIPT), but when
    it meets an opcode that isn't in :data:`_DIALOG_SKIP_OPCODE_SIZES`,
    it records ``(offset, opcode)`` and advances 2 bytes instead of
    bailing — so a single unmapped opcode doesn't hide every DIALOG
    block that follows it.

    Recovery is heuristic: the unmapped opcode might really be 4 / 6 /
    10 bytes long, so the 2-byte step may briefly desync. The walker
    re-syncs the moment it lands on either a recognized opcode or the
    ``9A 00 .. 9E 00 .. 9C 00`` DIALOG signature, which is robust
    enough that real-region dialogs (e.g. map 87 hitbox #12) surface
    even when their region begins with mystery opcodes like ``0x01a9``.

    The unknown-opcode list is surfaced verbatim in the cutscene detail
    panel so users can correlate unmapped ids with in-game behavior.

    ``end_off`` (exclusive) bounds the walk. When supplied (the
    cutscene-detail call-site), it is the authoritative region
    boundary from :class:`overlay5_cutscenes.CutsceneRegion` — END_SCRIPT
    inside the region is downgraded to a section break (still recorded
    as ``(off, 0x0003)`` in ``unknowns``) so multi-script regions like
    map 87 hitbox #12 — which begins with one script then transitions
    into the dialog-bearing one — surface all their dialogs. When
    ``end_off`` is omitted (legacy sprite/hitbox sidebar in
    :mod:`map_browser`), END_SCRIPT stays a hard stop because we have
    no other notion of where the script ends.
    """
    dialogs: List[DialogBlock] = []
    unknowns: List[Tuple[int, int]] = []
    if start_off <= 0 or start_off >= len(entry):
        return dialogs, unknowns
    bounded = end_off is not None
    n = len(entry) if not bounded else min(end_off, len(entry))
    seen_offsets: set = set()
    p = start_off
    while p + 2 <= n:
        if _is_dialog_at(entry, p):
            dialogs.append(DialogBlock.from_bytes(entry, p))
            seen_offsets.add(p)
            p += DIALOG_BLOCK_SIZE
            continue
        opcode = struct.unpack_from("<H", entry, p)[0]
        if opcode in _DIALOG_TERMINATOR_OPCODES:
            if not bounded:
                break
            unknowns.append((p, opcode))
            p += 2
            continue
        battle_size = _try_skip_battle(entry, p)
        if battle_size is not None:
            p += battle_size
            continue
        size = _DIALOG_SKIP_OPCODE_SIZES.get(opcode)
        if size is None:
            unknowns.append((p, opcode))
            p += 2
            continue
        if p + size > n:
            break
        p += size
    # Bounded fallback: raw DIALOG-signature sweep over the region.
    # When an unmapped opcode has the wrong size in our table (or when
    # the structured walk drifts), the walker can step right past a
    # real DIALOG block. The signature ``9A 00 .. 9E 00 .. 9C 00`` is
    # specific enough (3 anchored u16 markers across 12 bytes) that a
    # raw scan won't false-positive in script bodies — confirmed
    # against entry 0322 (map 87 hitbox #12) where the structured walk
    # missed the first of 4 dialogs by one offset. We only run this in
    # bounded mode because outside a region we have no end of script.
    if bounded:
        sweep_p = start_off
        sweep_end = n - DIALOG_BLOCK_SIZE + 1
        while sweep_p < sweep_end:
            if sweep_p not in seen_offsets and _is_dialog_at(entry, sweep_p):
                dialogs.append(DialogBlock.from_bytes(entry, sweep_p))
                seen_offsets.add(sweep_p)
            sweep_p += 2
        dialogs.sort(key=lambda d: d.block_offset)
    return dialogs, unknowns


def iter_dialogs_from(entry: bytes, start_off: int) -> List[DialogBlock]:
    """Walk ``entry`` from ``start_off``, collecting DIALOG blocks.

    Thin wrapper around :func:`iter_dialogs_from_with_meta` that drops
    the unknown-opcode list — most callers (chip labels, the legacy
    sprite/hitbox sidebar) only need the dialogs themselves. UI code
    that wants to expose the unknown opcodes (cutscenes detail panel)
    should call the meta variant directly.
    """
    dialogs, _ = iter_dialogs_from_with_meta(entry, start_off)
    return dialogs


def replace_dialog_field(
    entry: bytes, block_offset: int, field: str, new_value: int,
) -> bytes:
    """Rewrite one u16 inside the dialog block at ``block_offset``.

    ``field`` is ``"target"``, ``"msg_id"``, or ``"portrait"`` — anything
    else is a programming error and raises. Validates the opcode triplet
    before touching anything so a stale offset can't corrupt a non-dialog
    span.
    """
    if not _is_dialog_at(entry, block_offset):
        raise ValueError(
            f"no dialog block at offset 0x{block_offset:04x}"
        )
    field_offsets = {"target": 2, "msg_id": 6, "portrait": 8}
    if field not in field_offsets:
        raise ValueError(f"unknown dialog field {field!r}")
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block_offset + field_offsets[field],
        int(new_value) & 0xFFFF,
    )
    return bytes(buf)


# ---- overlay file resolver -----------------------------------------------


# NDS header offsets that point at the ARM9 overlay table.
_HDR_ARM9_OVERLAY_OFF = 0x50
_HDR_ARM9_OVERLAY_SIZE = 0x54

# 32-byte overlay table entry layout — only ovl_id (offset 0) and
# file_id (offset 24) matter for resolving where the overlay bytes live.
_OVERLAY_ENTRY_SIZE = 32
_OVERLAY_ENTRY_FILE_ID_OFF = 24


def find_overlay_file_id(rom: bytes, overlay_id: int) -> int:
    """Look up the FAT file id that backs ``overlay_id`` in the ARM9
    overlay table. Raises ``ValueError`` if no entry matches."""
    table_off = struct.unpack_from("<I", rom, _HDR_ARM9_OVERLAY_OFF)[0]
    table_size = struct.unpack_from("<I", rom, _HDR_ARM9_OVERLAY_SIZE)[0]
    n_entries = table_size // _OVERLAY_ENTRY_SIZE
    for i in range(n_entries):
        eoff = table_off + i * _OVERLAY_ENTRY_SIZE
        eid = struct.unpack_from("<I", rom, eoff)[0]
        if eid == overlay_id:
            file_id = struct.unpack_from(
                "<I", rom, eoff + _OVERLAY_ENTRY_FILE_ID_OFF,
            )[0]
            return file_id
    raise ValueError(f"overlay_id {overlay_id} not in ARM9 overlay table")


def find_overlay_fat_range(rom: bytes, overlay_id: int) -> Tuple[int, int]:
    """Resolve ``overlay_id``'s ``(start, end)`` byte range in the ROM."""
    file_id = find_overlay_file_id(rom, overlay_id)
    fat_off = struct.unpack_from("<I", rom, 0x48)[0]
    start, end = struct.unpack_from("<II", rom, fat_off + file_id * 8)
    return start, end


# ---- index over an overlay's entries -------------------------------------


@dataclass
class Overlay5Index:
    """Parsed pointer table + (entry_ix -> entry bytes) view over overlay 5.

    The index keeps the full overlay payload around so callers can
    re-slice individual entries against the in-memory bytes without
    re-reading from ROM. ``entry_starts`` / ``entry_ends`` are the
    sorted boundaries used to bound each entry's body.
    """
    payload: bytes
    pointers: List[int]                      # raw pointer table (entry_ix order)
    entry_starts: Dict[int, int]             # entry_ix -> start offset in payload
    entry_ends: Dict[int, int]               # entry_ix -> exclusive end offset

    @classmethod
    def from_bytes(cls, payload: bytes) -> "Overlay5Index":
        hdr_size = struct.unpack_from("<I", payload, 0)[0]
        n_ptrs = (hdr_size - 4) // 4
        pointers = list(struct.unpack_from(f"<{n_ptrs}I", payload, 4))
        # Boundaries via sorted-unique pointer values: each entry runs
        # from its pointer to the next-sorted-unique pointer (or EOF
        # for the final one). Sharing-target entries (multiple
        # pointers landing on the same offset) all read the same blob.
        sorted_unique = sorted(set(pointers))
        next_boundary: Dict[int, int] = {}
        for ix, p in enumerate(sorted_unique):
            if ix + 1 < len(sorted_unique):
                next_boundary[p] = sorted_unique[ix + 1]
            else:
                next_boundary[p] = len(payload)
        entry_starts: Dict[int, int] = {}
        entry_ends: Dict[int, int] = {}
        for entry_ix, ptr in enumerate(pointers):
            entry_starts[entry_ix] = ptr
            entry_ends[entry_ix] = next_boundary[ptr]
        return cls(
            payload=payload,
            pointers=pointers,
            entry_starts=entry_starts,
            entry_ends=entry_ends,
        )

    @classmethod
    def from_file_table(
        cls, file_table: "fnt.FileTable", rom: bytes,
    ) -> "Overlay5Index":
        # FileTable currently has no entry for overlays (they live
        # outside the FNT tree), so resolve via the ARM9 overlay table
        # directly. ``file_table`` is kept on the signature for symmetry
        # with btmap/map call sites — useful when overlay-relocation
        # ever lands.
        del file_table  # unused; signature is symmetric with map/btmap
        start, end = find_overlay_fat_range(rom, overlay_id=5)
        return cls.from_bytes(bytes(rom[start:end]))

    def entry_count(self) -> int:
        return len(self.pointers)

    def read_entry(self, entry_ix: int) -> bytes:
        """Bytes for entry ``entry_ix`` sliced from the cached payload."""
        start = self.entry_starts[entry_ix]
        end = self.entry_ends[entry_ix]
        return bytes(self.payload[start:end])

    def replace_entry(self, entry_ix: int, new_entry: bytes) -> bytes:
        """Return a new full-overlay payload with ``entry_ix``'s body
        replaced by ``new_entry``.

        Only same-length replacements are supported — shifting later
        entries would require rewriting every pointer >= this one's
        start and is out of scope for the Events tab (drag only changes
        x/y, never the body length). Raises ``ValueError`` on a length
        mismatch so the caller fails fast instead of silently
        corrupting downstream entries.
        """
        original = self.read_entry(entry_ix)
        if len(new_entry) != len(original):
            raise ValueError(
                f"overlay5 entry {entry_ix:04d} length mismatch: "
                f"{len(new_entry)} vs {len(original)}"
            )
        start = self.entry_starts[entry_ix]
        end = self.entry_ends[entry_ix]
        return bytes(self.payload[:start]) + bytes(new_entry) + bytes(self.payload[end:])

    def find_entry_containing(
        self, file_off: int,
    ) -> Optional[Tuple[int, int]]:
        """Resolve an absolute overlay file offset to ``(entry_ix, rel)``.

        ``rel`` is the in-entry offset suitable for the splice helpers.
        Returns ``None`` when the offset doesn't fall inside any entry's
        body (e.g. dangling pointer or overlay-header region).

        Shared blobs: when multiple entry_ixs point at the same start,
        all of them have identical ``(entry_starts, entry_ends)`` ranges
        — the lowest matching entry_ix is returned for determinism.
        """
        if file_off < 0 or file_off >= len(self.payload):
            return None
        for entry_ix in range(self.entry_count()):
            start = self.entry_starts[entry_ix]
            end = self.entry_ends[entry_ix]
            if start <= file_off < end:
                return (entry_ix, file_off - start)
        return None

    def iter_map_entries(self) -> List[Tuple[int, int, bytes]]:
        """Yield ``(map_id, entry_ix, entry_bytes)`` for every entry that
        backs a field map. Skips entries 0..234 and 500..504 — the
        Events tab only cares about field maps."""
        out: List[Tuple[int, int, bytes]] = []
        for entry_ix in range(self.entry_count()):
            mid = map_id_for(entry_ix)
            if mid is None:
                continue
            out.append((mid, entry_ix, self.read_entry(entry_ix)))
        return out
