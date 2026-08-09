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


def first_per_sprite_id_pos(
    placements: List[OverworldSpritePlacement],
) -> List[OverworldSpritePlacement]:
    """Filter so each ``(overworld_sprite_id, x, y)`` appears once.

    Unlike :func:`first_per_sprite_id` (id-only), this keeps the SAME sprite
    graphic reused for two *different* NPCs at different map positions — e.g.
    entry 262 (Dark Square) uses Tanemon (ow 0x0077) both as a digivolution
    stage at (673,137) AND as the separate L-Mushroom quest NPC at (144,144);
    id-only dedup dropped the quest NPC. Only exact same-position duplicates
    (story-state variants of one slot) still collapse."""
    seen: set = set()
    out: List[OverworldSpritePlacement] = []
    for p in placements:
        key = (p.overworld_sprite_id, p.x, p.y)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# Opcodes that gate a following OVERWORLD_SPRITE placement → the offset (from
# the opcode start) of the u16 "flag" discriminator that selects whether the
# sprite spawns:
#   0x06 IFEQ          — arg2 (offset 4)
#   0xA6 HANDLER_META  — the opaque hi word (offset 6); the DOMINANT gate,
#                        and its flag matches the sprite's dialogue flag
#                        (verified: GreenDigiEgg keys spawn AND dialog on 0x8bea)
#   0x2A CMP_BRANCH    — the var descriptor (offset 2)
_SPRITE_GATE_OPCODES = {0x0006: 4, 0x00A6: 6, 0x002A: 2}

# A gate must sit within this many bytes of the placement it controls, so a
# distant conditional (e.g. one gating a dialogue) doesn't leak onto a later
# sprite. A gate right before a 26-byte placement is <0x20 away.
_SPRITE_GATE_WINDOW = 0x20


def sprite_gate_conditions(entry: bytes) -> Dict[int, Tuple[int, int]]:
    """Map each OVERWORLD_SPRITE ``block_offset`` that is spawned conditionally
    to ``(gate_opcode, flag)``.

    A placement is conditional when a gate opcode (see
    :data:`_SPRITE_GATE_OPCODES`) appears since the previous placement and
    within :data:`_SPRITE_GATE_WINDOW` bytes; ``flag`` is the discriminator
    that opcode carries — the same value that gates the sprite's dialogue
    (e.g. GreenDigiEgg's spawn *and* dialog both key on ``0x8bea``). Sprites
    sharing a flag group together in the editor's Objects list. The gate is
    consumed by the placement it precedes, so a following placement needs its
    own gate to count. Structural walk stays aligned via the opcode-size
    table.

    NOTE: ``0xA6`` is the dominant gate and most base-region sprites carry
    one, so relatively few sprites are truly ungated ("always on map"). We
    surface the raw gating structure; whether a given flag is effectively
    always-true vs. story-dependent isn't decoded."""
    gates: Dict[int, Tuple[int, int]] = {}
    p = 0
    n = len(entry)
    last: Optional[Tuple[int, int, int]] = None  # (opcode, flag, pos)
    while p + 2 <= n:
        op = struct.unpack_from("<H", entry, p)[0]
        disc_off = _SPRITE_GATE_OPCODES.get(op)
        size = _DIALOG_SKIP_OPCODE_SIZES.get(op)
        if disc_off is not None and size and p + size <= n:
            flag = struct.unpack_from("<H", entry, p + disc_off)[0]
            last = (op, flag, p)
            p += size
            continue
        if op == OVERWORLD_SPRITE_OPCODE and p + OVERWORLD_SPRITE_BLOCK_SIZE <= n:
            if last is not None and p - last[2] <= _SPRITE_GATE_WINDOW:
                gates[p] = (last[0], last[1])
            last = None
            p += OVERWORLD_SPRITE_BLOCK_SIZE
            continue
        p += size if size else 2
    return gates


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


# ---- overworld chest give-item -------------------------------------------
#
# An overworld chest is an OVERWORLD_SPRITE (id 0x02fb) whose interaction
# script sets four SET_VAR args then calls the shared give-item routine:
#
#   15 00 04 00 <flag:u16>   SET_VAR ARG_0 = per-chest opened flag
#   15 00 05 00 <item:u16>   SET_VAR ARG_1 = item id given
#   15 00 06 00 <arg2:u16>   SET_VAR ARG_2 = (unconfirmed 0/1)
#   15 00 07 00 <arg3:u16>   SET_VAR ARG_3 = (unconfirmed small int)
#   02 00 <CHEST_GIVE_ROUTINE:u32>   CALL_SYS
#
# Verified across 163 chests (research_docs scan). The item id (ARG_1) is
# the one field it's safe to edit in place — the flag is also referenced by
# the 0x0006 load-time frame check, so editing it needs both sites rewritten
# together and is intentionally left alone here.
CHEST_GIVE_ROUTINE = 0x126A
CHEST_ITEM_SETVAR_PREFIX = (0x0015, 0x0005)  # op SET_VAR, var ARG_1


def replace_chest_item(entry: bytes, value_offset: int, item_id: int) -> bytes:
    """Rewrite the u16 item id at ``value_offset`` (a chest's ARG_1
    SET_VAR value).

    Guards on the ``15 00 05 00`` SET_VAR-ARG_1 prefix 4 bytes before the
    value so a stale offset can't silently corrupt the script stream —
    same defensive posture as the exit-handler splices above.
    """
    if value_offset < 4 or value_offset + 2 > len(entry):
        raise ValueError(
            f"value_offset 0x{value_offset:04x} out of range (len={len(entry)})"
        )
    op, var = struct.unpack_from("<HH", entry, value_offset - 4)
    if (op, var) != CHEST_ITEM_SETVAR_PREFIX:
        raise ValueError(
            f"no SET_VAR ARG_1 (item) at rel 0x{value_offset - 4:04x} "
            f"(found op 0x{op:04x} var 0x{var:04x})"
        )
    buf = bytearray(entry)
    struct.pack_into("<H", buf, value_offset, item_id & 0xFFFF)
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
# Strides = confirmed TOTAL byte length (opcode + args), from the ARM9
# handlers (research_docs/claude_notes/overlay5_opcodes.md). Keeping this
# accurate lets the walker step cleanly between the events it emits. The
# events it *does* surface (dialog / battle / reaction, and the Tier-1
# wait / sprite-anim / camera / item below) are handled by their own
# branches before this table is consulted; the rest are skipped by size.
# NB the compare/branch + task opcodes (0x2A=6, 0x156=10) read args inline
# / after a branch, so their length is hand-set, not reader-derived.
_DIALOG_SKIP_OPCODE_SIZES: Dict[int, int] = {
    0x0002: 6,    # CALL_SYS
    0x0004: 6,    # REGISTER_HANDLER (prologue, but may appear in body)
    0x0005: 4,    # WAIT_FRAMES (emitted; kept for stride safety)
    0x0006: 6,    # IFEQ_LIT_BRANCH
    0x0008: 4, 0x0009: 4, 0x000a: 4, 0x000b: 4, 0x000e: 4, 0x000f: 4,
    0x0010: 4, 0x0011: 4,
    0x0015: 6,    # SET_VAR
    0x001e: 16, 0x0023: 6, 0x0025: 2, 0x002a: 6,   # CMP_BRANCH 6, not 2
    0x0030: 6,    # CALL_SCRIPT_AT_OFFSET (mid-region call; the tail-call
                  # variant terminates the region before we reach it)
    0x003C: 12,   # MOVE_BEGIN (was mis-sized 4)
    0x003d: 4, 0x003E: 4,
    0x0040: 6, 0x0042: 4, 0x004c: 10,
    0x0060: 4, 0x0061: 4, 0x0062: 4, 0x0063: 6, 0x0064: 6,
    0x0076: 4,    # MOVE_OPEN
    0x0077: 12, 0x0080: 6, 0x0082: 6, 0x0084: 6, 0x008a: 8,
    0x008e: 2, 0x008f: 2, 0x0090: 2, 0x0092: 2, 0x0094: 10,
    0x009f: 4,
    0x00a6: 10,
    0x00a7: 10,
    0x00ae: 6,
    0x00b4: 4,    # GIVE_ITEM (emitted)
    0x00BB: 4,    # OPEN_SHOP (re-identified by the cutscene decoder; was
                  # previously misread as a script-end marker, which made
                  # the walker bail just before shop NPCs' dialog tails)
    0x00C0: 6,    # REACTION_BALLOON
    0x00c2: 8, 0x00c6: 2, 0x00c7: 4, 0x00dc: 2,
    0x00e1: 8, 0x00e2: 6, 0x00e5: 2, 0x00e6: 6,
    0x00f1: 4,    # REMOVE_ITEM (emitted)
    0x0100: 4, 0x0117: 4,
    0x014b: 4,    # LOAD_TOP_SCREEN_MAP (DS dual-screen cutscenes)
    0x014c: 4,    # LOAD_BOTTOM_SCREEN_MAP (also the prologue header at
                  # offset 0 of every entry; body occurrences appear in
                  # mid-script map swaps)
    0x014d: 10,
    0x0150: 26,   # OVERWORLD_SPRITE — appears inside handler regions
                  # that spawn cutscene NPCs (e.g. entry 0499 chains)
    0x0156: 10,   # WARP_TRANSITION (reads 4 args after a branch)
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


# ---- unified events walker (dialog + set_music + reaction + battle) ------

EVENT_KIND_DIALOG = "dialog"
EVENT_KIND_SET_MUSIC = "set_music"
EVENT_KIND_REACTION = "reaction"
EVENT_KIND_BATTLE = "battle"
EVENT_KIND_WAIT = "wait"            # WAIT_FRAMES 0x05
EVENT_KIND_SPRITE_ANIM = "sprite_anim"  # SET_SPRITE_ANIM 0x64
EVENT_KIND_CAMERA = "camera"        # CAMERA_PAN 0x40 / CAMERA_PAN_TO_XY 0xC2
EVENT_KIND_ITEM = "item"            # GIVE_ITEM 0xB4 / REMOVE_ITEM 0xF1
EVENT_KIND_MOVE = "move"            # MOVE_BEGIN 0x3C
EVENT_KIND_CONTROL = "control"      # read-only flag/branch gating (0x2A/0x15/0x0F/…)

# `0e 00` is NOT reliably a SET_MUSIC opcode. The linear walk mis-syncs and
# lands on stray `0e 00` byte pairs sitting mid-instruction (slot ids,
# y-coords, dialog trailers), so surfacing them as editable BGM cards
# produced mostly-wrong edits — see entry 0322, where every SET_MUSIC but
# the last is a false positive threaded through Barone's dialog. Until
# `0e 00` can be distinguished from mid-instruction data, the walker
# consumes the block to stay in sync but does NOT emit an editable event.
# Flip this to re-enable the SET_MUSIC cards (`_MusicCard` in the Cutscenes
# tab) once that detection exists.
SURFACE_SET_MUSIC_EVENTS = False


@dataclass
class RegionEvent:
    """One decoded editable event inside a cutscene region.

    ``kind`` selects the ``payload`` type — one of :class:`DialogBlock`,
    :class:`SetMusicBlock`, :class:`ReactionBlock`, :class:`BattleBlock`.
    ``rel`` is the in-entry offset the walker landed on (same as
    ``payload.block_offset``, kept up top for cheap sort keys).
    """
    rel: int
    kind: str
    payload: object


def iter_region_events_with_meta(
    entry: bytes,
    start_off: int,
    end_off: Optional[int] = None,
) -> Tuple[List[RegionEvent], List[Tuple[int, int]]]:
    """Forgiving walker that emits every editable structural event
    inside a cutscene region (or a sprite/hitbox script body).

    Same fixed-size opcode table + END_SCRIPT / bounded-region semantics
    as :func:`iter_dialogs_from_with_meta`, but returns a mixed event
    stream so the cutscene detail panel can render dialog / music /
    reaction / battle cards in offset order rather than emitting each
    kind in a separate pass.

    Unmapped opcodes are still surfaced through ``unknowns`` for the
    debug section.
    """
    events: List[RegionEvent] = []
    unknowns: List[Tuple[int, int]] = []
    if start_off <= 0 or start_off >= len(entry):
        return events, unknowns
    bounded = end_off is not None
    n = len(entry) if not bounded else min(end_off, len(entry))
    seen_offsets: set = set()
    p = start_off
    while p + 2 <= n:
        if _is_dialog_at(entry, p):
            events.append(RegionEvent(
                rel=p,
                kind=EVENT_KIND_DIALOG,
                payload=DialogBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += DIALOG_BLOCK_SIZE
            continue
        # BATTLE has variable length + its own D7 payload sink; try it
        # before the generic opcode table so ``0xDA`` isn't confused
        # with a SET_VAR value.
        battle = _parse_battle_at(entry, p)
        if battle is not None:
            events.append(RegionEvent(
                rel=p,
                kind=EVENT_KIND_BATTLE,
                payload=battle,
            ))
            seen_offsets.add(p)
            p += battle.total_size
            continue
        opcode = struct.unpack_from("<H", entry, p)[0]
        if opcode == SET_MUSIC_OPCODE and p + SET_MUSIC_BLOCK_SIZE <= n:
            # Consume the block to keep the walk in sync with prior
            # behaviour, but only surface an editable SET_MUSIC event when
            # explicitly enabled — see SURFACE_SET_MUSIC_EVENTS.
            if SURFACE_SET_MUSIC_EVENTS:
                events.append(RegionEvent(
                    rel=p,
                    kind=EVENT_KIND_SET_MUSIC,
                    payload=SetMusicBlock.from_bytes(entry, p),
                ))
                seen_offsets.add(p)
            p += SET_MUSIC_BLOCK_SIZE
            continue
        if opcode == REACTION_OPCODE and p + REACTION_BLOCK_SIZE <= n:
            events.append(RegionEvent(
                rel=p,
                kind=EVENT_KIND_REACTION,
                payload=ReactionBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += REACTION_BLOCK_SIZE
            continue
        # Tier-1 editable scene opcodes (Cutscenes tab cards).
        if opcode == WAIT_FRAMES_OPCODE and p + WAIT_FRAMES_BLOCK_SIZE <= n:
            events.append(RegionEvent(
                rel=p, kind=EVENT_KIND_WAIT,
                payload=WaitFramesBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += WAIT_FRAMES_BLOCK_SIZE
            continue
        if opcode == SPRITE_ANIM_OPCODE and p + SPRITE_ANIM_BLOCK_SIZE <= n:
            events.append(RegionEvent(
                rel=p, kind=EVENT_KIND_SPRITE_ANIM,
                payload=SpriteAnimBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += SPRITE_ANIM_BLOCK_SIZE
            continue
        if opcode in (CAMERA_TARGET_OPCODE, CAMERA_XY_OPCODE):
            cam = CameraBlock.from_bytes(entry, p)
            if p + cam.size <= n:
                events.append(RegionEvent(
                    rel=p, kind=EVENT_KIND_CAMERA, payload=cam,
                ))
                seen_offsets.add(p)
                p += cam.size
                continue
        if (opcode in (GIVE_ITEM_OPCODE, REMOVE_ITEM_OPCODE)
                and p + ITEM_BLOCK_SIZE <= n):
            events.append(RegionEvent(
                rel=p, kind=EVENT_KIND_ITEM,
                payload=ItemBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += ITEM_BLOCK_SIZE
            continue
        if opcode == MOVE_BEGIN_OPCODE and p + MOVE_BEGIN_BLOCK_SIZE <= n:
            events.append(RegionEvent(
                rel=p, kind=EVENT_KIND_MOVE,
                payload=MoveBeginBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += MOVE_BEGIN_BLOCK_SIZE
            continue
        ctrl_size = CONTROL_OPCODE_SIZES.get(opcode)
        if ctrl_size is not None and p + ctrl_size <= n:
            events.append(RegionEvent(
                rel=p, kind=EVENT_KIND_CONTROL,
                payload=ControlBlock.from_bytes(entry, p),
            ))
            seen_offsets.add(p)
            p += ctrl_size
            continue
        if opcode in _DIALOG_TERMINATOR_OPCODES:
            if not bounded:
                break
            unknowns.append((p, opcode))
            p += 2
            continue
        size = _DIALOG_SKIP_OPCODE_SIZES.get(opcode)
        if size is None:
            unknowns.append((p, opcode))
            p += 2
            continue
        if p + size > n:
            break
        p += size
    # Same signature-sweep safety net as the dialog walker — catches
    # DIALOG blocks the structured walk drifted past inside a mis-sized
    # opcode. We only sweep for DIALOG (not the shorter opcodes) because
    # its 3-marker signature is specific enough to avoid false positives.
    if bounded:
        sweep_p = start_off
        sweep_end = n - DIALOG_BLOCK_SIZE + 1
        while sweep_p < sweep_end:
            if sweep_p not in seen_offsets and _is_dialog_at(entry, sweep_p):
                events.append(RegionEvent(
                    rel=sweep_p,
                    kind=EVENT_KIND_DIALOG,
                    payload=DialogBlock.from_bytes(entry, sweep_p),
                ))
                seen_offsets.add(sweep_p)
            sweep_p += 2
        events.sort(key=lambda e: e.rel)
    return events, unknowns


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


# ---- SET_MUSIC (0e 00 [music_id:2]) -------------------------------------

SET_MUSIC_OPCODE = 0x000E
SET_MUSIC_BLOCK_SIZE = 4


@dataclass
class SetMusicBlock:
    """Fixed 4-byte ``0e 00 [music_id:u16]`` BGM-select opcode.

    Same opcode drives both the mid-map SET_MUSIC and the first-instruction
    of every registered handler script — the byte layout is identical, so
    the codec doesn't distinguish them.
    """
    block_offset: int
    music_id: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "SetMusicBlock":
        opcode, music_id = struct.unpack_from("<HH", entry, off)
        if opcode != SET_MUSIC_OPCODE:
            raise ValueError(
                f"not a SET_MUSIC block at 0x{off:04x} (opcode 0x{opcode:04x})"
            )
        return cls(block_offset=off, music_id=music_id)


def _is_set_music_at(entry: bytes, off: int) -> bool:
    if off < 0 or off + SET_MUSIC_BLOCK_SIZE > len(entry):
        return False
    return entry[off] == 0x0E and entry[off + 1] == 0x00


def replace_set_music_id(
    entry: bytes, block_offset: int, new_music_id: int,
) -> bytes:
    """Rewrite the music id u16 at ``block_offset + 2``.

    Same length in/out — annotated as SET_MUSIC only when the id
    resolved to a known BGM in the research docs, but at splice-time
    we trust the caller located the block via :func:`_is_set_music_at`
    or the events walker below.
    """
    if not _is_set_music_at(entry, block_offset):
        raise ValueError(
            f"no SET_MUSIC block at offset 0x{block_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block_offset + 2, int(new_music_id) & 0xFFFF,
    )
    return bytes(buf)


# ---- REACTION_BALLOON (C0 00 [reaction:2] [target:2]) --------------------

REACTION_OPCODE = 0x00C0
REACTION_BLOCK_SIZE = 6

# Named reactions we've pinned. Ids outside this set stay editable
# (spinbox) — the annotator uses the same short list.
REACTION_NAMES: Dict[int, str] = {
    0: "!",
    1: "...",
    2: "waterdrop",
    3: "anger",
}


@dataclass
class ReactionBlock:
    """6-byte ``C0 00 [reaction:u16] [target:u16]`` — over-head balloon.

    ``target`` is a sprite slot (same convention as DIALOG.target), so
    the reaction floats above the matching OVERWORLD_SPRITE placement.
    """
    block_offset: int
    reaction: int
    target: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "ReactionBlock":
        opcode, reaction, target = struct.unpack_from("<HHH", entry, off)
        if opcode != REACTION_OPCODE:
            raise ValueError(
                f"not a REACTION_BALLOON block at 0x{off:04x} "
                f"(opcode 0x{opcode:04x})"
            )
        return cls(block_offset=off, reaction=reaction, target=target)


def _is_reaction_at(entry: bytes, off: int) -> bool:
    if off < 0 or off + REACTION_BLOCK_SIZE > len(entry):
        return False
    return entry[off] == 0xC0 and entry[off + 1] == 0x00


def replace_reaction_field(
    entry: bytes, block_offset: int, field: str, new_value: int,
) -> bytes:
    """Rewrite ``reaction`` or ``target`` u16 inside a REACTION_BALLOON block."""
    if not _is_reaction_at(entry, block_offset):
        raise ValueError(
            f"no REACTION_BALLOON block at offset 0x{block_offset:04x}"
        )
    field_offsets = {"reaction": 2, "target": 4}
    if field not in field_offsets:
        raise ValueError(f"unknown reaction field {field!r}")
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block_offset + field_offsets[field],
        int(new_value) & 0xFFFF,
    )
    return bytes(buf)


# ---- Tier-1 editable scene opcodes (Cutscenes tab) -----------------------
#
# Small fixed-size action opcodes surfaced as editable event cards:
#   05 00 [frames:u16]                 WAIT_FRAMES       (pacing pause)
#   64 00 [sprite:u16] [anim:u16]      SET_SPRITE_ANIM   (pose / facing)
#   40 00 [target:u16] [speed:u16]     CAMERA_PAN_TO_TARGET
#   c2 00 [x:u16] [y:u16] [speed:u16]  CAMERA_PAN_TO_XY
#   b4 00 [item:u16]                   GIVE_ITEM
#   f1 00 [item:u16]                   REMOVE_ITEM
# Behaviour decoded from the ARM9 handlers — see
# research_docs/claude_notes/overlay5_opcodes.md. All edits are in-place
# u16 rewrites guarded by the opcode byte (:func:`replace_scalar_field`).

WAIT_FRAMES_OPCODE = 0x0005
WAIT_FRAMES_BLOCK_SIZE = 4
SPRITE_ANIM_OPCODE = 0x0064
SPRITE_ANIM_BLOCK_SIZE = 6
CAMERA_TARGET_OPCODE = 0x0040
CAMERA_TARGET_BLOCK_SIZE = 6
CAMERA_XY_OPCODE = 0x00C2
CAMERA_XY_BLOCK_SIZE = 8
GIVE_ITEM_OPCODE = 0x00B4
REMOVE_ITEM_OPCODE = 0x00F1
ITEM_BLOCK_SIZE = 4


@dataclass
class WaitFramesBlock:
    """4-byte ``05 00 [frames:u16]`` — pauses the running actor N frames."""
    block_offset: int
    frames: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "WaitFramesBlock":
        opcode, frames = struct.unpack_from("<HH", entry, off)
        if opcode != WAIT_FRAMES_OPCODE:
            raise ValueError(f"not a WAIT_FRAMES block at 0x{off:04x}")
        return cls(block_offset=off, frames=frames)


@dataclass
class SpriteAnimBlock:
    """6-byte ``64 00 [sprite:u16] [anim:u16]`` — set a sprite's pose/anim.

    ``sprite`` is a slot id (same space as DIALOG.target); ``anim`` is a
    pose/animation id (low values = 8-way facing, higher = named anims
    like 0x18 head-nod).
    """
    block_offset: int
    sprite: int
    anim: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "SpriteAnimBlock":
        opcode, sprite, anim = struct.unpack_from("<HHH", entry, off)
        if opcode != SPRITE_ANIM_OPCODE:
            raise ValueError(f"not a SET_SPRITE_ANIM block at 0x{off:04x}")
        return cls(block_offset=off, sprite=sprite, anim=anim)


@dataclass
class CameraBlock:
    """A camera pan — ``40 00 [target] [speed]`` (pan to a sprite) or
    ``c2 00 [x] [y] [speed]`` (pan to a point). ``is_xy`` selects the
    form; for the target form ``b`` is unused (0)."""
    block_offset: int
    opcode: int
    a: int
    b: int
    speed: int

    @property
    def is_xy(self) -> bool:
        return self.opcode == CAMERA_XY_OPCODE

    @property
    def size(self) -> int:
        return CAMERA_XY_BLOCK_SIZE if self.is_xy else CAMERA_TARGET_BLOCK_SIZE

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "CameraBlock":
        opcode = struct.unpack_from("<H", entry, off)[0]
        if opcode == CAMERA_TARGET_OPCODE:
            _, a, speed = struct.unpack_from("<HHH", entry, off)
            return cls(block_offset=off, opcode=opcode, a=a, b=0, speed=speed)
        if opcode == CAMERA_XY_OPCODE:
            _, a, b, speed = struct.unpack_from("<HHHH", entry, off)
            return cls(block_offset=off, opcode=opcode, a=a, b=b, speed=speed)
        raise ValueError(f"not a CAMERA block at 0x{off:04x}")


@dataclass
class ItemBlock:
    """4-byte ``b4 00 [item]`` (give) or ``f1 00 [item]`` (remove)."""
    block_offset: int
    opcode: int
    item: int

    @property
    def is_remove(self) -> bool:
        return self.opcode == REMOVE_ITEM_OPCODE

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "ItemBlock":
        opcode, item = struct.unpack_from("<HH", entry, off)
        if opcode not in (GIVE_ITEM_OPCODE, REMOVE_ITEM_OPCODE):
            raise ValueError(f"not a GIVE/REMOVE_ITEM block at 0x{off:04x}")
        return cls(block_offset=off, opcode=opcode, item=item)


MOVE_BEGIN_OPCODE = 0x003C
MOVE_BEGIN_BLOCK_SIZE = 12


@dataclass
class MoveBeginBlock:
    """12-byte ``3C 00 [tgt] [type] [x] [y] [speed]`` — start moving a sprite.

    ``tgt`` is a scene slot (same space as DIALOG.target / SET_SPRITE_ANIM).
    For the common walk type (``move_type == 1``) ``x``/``y`` are the
    **destination position** — data-confirmed: they carry map-pixel
    coordinates matching the sprite's OVERWORLD_SPRITE x/y, and ``0xFFFF``
    means "leave that axis unchanged". ``speed`` ``0xFFFF`` snaps the sprite
    there instantly. ``move_type`` is a small mode enum (0/1/8/0xc/0xe seen);
    its non-1 meanings aren't decoded, so it's exposed raw rather than named.
    """
    block_offset: int
    tgt: int
    move_type: int
    x: int
    y: int
    speed: int

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "MoveBeginBlock":
        opcode, tgt, move_type, x, y, speed = struct.unpack_from("<6H", entry, off)
        if opcode != MOVE_BEGIN_OPCODE:
            raise ValueError(f"not a MOVE_BEGIN block at 0x{off:04x}")
        return cls(
            block_offset=off, tgt=tgt, move_type=move_type,
            x=x, y=y, speed=speed,
        )


# ---- read-only flag / branch "control" opcodes ---------------------------
#
# These decide WHEN a scene runs and what story state it sets — the gating the
# cutscene editor surfaces read-only (not editable yet). Sizes match
# _DIALOG_SKIP_OPCODE_SIZES so emitting them keeps the walk in sync.
#
# 0x2A CMP_BRANCH and 0x06 IFEQ_LIT_BRANCH are BOTH conditionals — different
# NPCs use different ones (0x2A resolves a script var via 0x20497C8; 0x06 is
# the dominant one, 1545× vs 202×, resolving a value via the gate at
# 0x0202EB04 and comparing it against arg1's literal). 0x06's arg1 is the
# compared literal; its 2nd u16 (arg2) is control-flow data whose exact role
# isn't reversed, so it's not shown. 0x0E CALL_IF_EQ is still excluded: a
# structural walk finds ZERO aligned occurrences (the ``0e 00`` in linear
# dumps is mid-instruction misalignment, same class as the old SET_MUSIC
# mislabel).
CONTROL_OPCODE_SIZES: Dict[int, int] = {
    0x002A: 6,   # CMP_BRANCH        — IF (script var vs global-table value)
    0x0006: 6,   # IFEQ_LIT_BRANCH   — IF (resolved value == arg1 literal)
    0x0015: 6,   # SET_VAR
    0x000F: 4,   # FLAG_SET_OR_CLEAR
}

# CMP_BRANCH operator from the descriptor's high nibble. Data check: every
# vanilla descriptor uses op 0 (==); the rest are decoded for completeness.
_CMP_OPS = {0: "==", 1: ">", 2: "<", 3: ">=", 4: "<=", 5: "!="}


@dataclass
class ControlBlock:
    """A flag/branch control opcode, decoded for read-only display.

    ``a``/``b`` are the raw u16 args (``b`` is 0 for the 4-byte ops). The
    :meth:`describe` string is what the cutscene panel shows; the RHS of a
    CMP_BRANCH comes from a global table the script indexes, so we surface the
    variable + jump target rather than inventing the compared value."""
    block_offset: int
    opcode: int
    a: int
    b: int

    @property
    def category(self) -> str:
        if self.opcode in (0x002A, 0x0006):
            return "branch"
        if self.opcode == 0x0015:
            return "set_var"
        return "flag"

    @property
    def gate_len(self) -> int:
        """For a CMP_BRANCH, the relative forward skip (``b``) the FALSE case
        takes — i.e. the byte length of the gated block. Data-verified:
        ``block_offset + gate_len`` lands at the next branch. 0 for non-branch
        ops. Used to indent the events a branch gates."""
        return int(self.b) if self.opcode == 0x002A else 0

    def row_summary(self) -> Tuple[str, str]:
        """``(ACTION LABEL, detail)`` for a compact events-browser row — the
        label carries the action word so the detail needn't repeat it
        (``IF`` / ``var 0x9 == value``)."""
        op = self.opcode
        if op == 0x002A:
            operator = _CMP_OPS.get(self.a >> 12, "?")
            return ("IF", f"var 0x{self.a & 0x0FFF:x} {operator} value")
        if op == 0x0006:
            return ("IF", f"input == 0x{self.a:x} · 0x{self.b:04x}")
        if op == 0x0015:
            return ("SET VAR", f"0x{self.a:x} = 0x{self.b:x}")
        if self.a >= 0x8000:
            return ("FLAG", f"clear 0x{(~self.a) & 0xFFFF:x}")
        return ("FLAG", f"set 0x{self.a:x}")

    def describe(self) -> str:
        op = self.opcode
        if op == 0x002A:
            operator = _CMP_OPS.get(self.a >> 12, "?")
            var = self.a & 0x0FFF
            # ``b`` is a relative skip length (see gate_len), not an address,
            # and the compared RHS comes from a global table we don't resolve
            # — so surface only the variable + operator.
            return f"if var 0x{var:x} {operator} value"
        if op == 0x0006:
            # arg1 is the compared literal (per the 0x0203EEF8 handler); arg2's
            # exact role isn't reversed but it's the per-line discriminator, so
            # it's shown raw after the middot so sibling rows stay distinct.
            return f"if input == 0x{self.a:x} · 0x{self.b:04x}"
        if op == 0x0015:
            return f"set var 0x{self.a:x} = 0x{self.b:x}"
        if op == 0x000F:
            if self.a >= 0x8000:
                return f"clear flag 0x{(~self.a) & 0xFFFF:x}"
            return f"set flag 0x{self.a:x}"
        return f"op 0x{op:02x}"

    @classmethod
    def from_bytes(cls, entry: bytes, off: int) -> "ControlBlock":
        opcode = struct.unpack_from("<H", entry, off)[0]
        size = CONTROL_OPCODE_SIZES.get(opcode)
        if size is None:
            raise ValueError(f"not a control opcode at 0x{off:04x}")
        if size == 6:
            _, a, b = struct.unpack_from("<HHH", entry, off)
        else:
            _, a = struct.unpack_from("<HH", entry, off)
            b = 0
        return cls(block_offset=off, opcode=opcode, a=a, b=b)


def replace_scalar_field(
    entry: bytes, block_offset: int, field_offset: int,
    value: int, expected_opcode: int,
) -> bytes:
    """In-place u16 rewrite at ``block_offset + field_offset``, guarded by
    the opcode at ``block_offset``.

    Shared by the Tier-1 editable scene opcodes (wait / sprite-anim /
    camera / item). Validating the leading opcode makes a stale offset a
    hard error instead of silent script corruption — same posture as the
    dialog / chest splices.
    """
    if block_offset < 0 or block_offset + field_offset + 2 > len(entry):
        raise ValueError(
            f"field at 0x{block_offset:04x}+{field_offset} out of range"
        )
    op = struct.unpack_from("<H", entry, block_offset)[0]
    if op != expected_opcode:
        raise ValueError(
            f"expected opcode 0x{expected_opcode:04x} at 0x{block_offset:04x}, "
            f"found 0x{op:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into("<H", buf, block_offset + field_offset, int(value) & 0xFFFF)
    return bytes(buf)


# ---- BATTLE (DA 00 [5×enemy:2] D7 00 [payload] D8 00 [bg:2] D9 00 [music:2] BA 00)

BATTLE_HEADER_OPCODE = 0x00DA
BATTLE_D7_OPCODE = 0x00D7
BATTLE_D8_OPCODE = 0x00D8
BATTLE_D9_OPCODE = 0x00D9
BATTLE_TERMINATOR_OPCODE = 0x00BA
BATTLE_ENEMY_SLOTS = 5
BATTLE_ENEMY_EMPTY = 0xFFFF


@dataclass
class BattleBlock:
    """Variable-length battle setup found in overlay5 cutscenes.

    Byte layout::

        DA 00 [e0:u16][e1:u16][e2:u16][e3:u16][e4:u16]    # 12 bytes
        D7 00 <payload — variable u16 stream>              # 2 + N bytes
        D8 00 [bg_id:u16]                                  # 4 bytes
        D9 00 [music_id:u16]                               # 4 bytes
        BA 00                                              # 2 bytes

    The five enemy u16s use ``0xFFFF`` as "empty slot"; ``bg_id`` /
    ``music_id`` index the same BG map / SDAT-BGM tables the rest of
    the codec uses. The ``D7`` payload's semantics aren't fully pinned,
    so the codec surfaces it as opaque bytes; the editor only touches
    the enemy / bg / music u16s.

    Offsets stored on the block make in-place field replacement cheap
    for the ``replace_battle_*`` helpers — no re-walk needed.
    """
    block_offset: int
    total_size: int
    enemies: Tuple[int, ...]         # length BATTLE_ENEMY_SLOTS
    d7_payload: bytes                # opaque
    bg_id: int
    music_id: int
    # Absolute in-entry offsets of each editable u16 field.
    bg_field_offset: int
    music_field_offset: int

    def enemy_field_offset(self, slot_ix: int) -> int:
        if not 0 <= slot_ix < BATTLE_ENEMY_SLOTS:
            raise ValueError(
                f"BATTLE enemy slot {slot_ix} out of range 0..{BATTLE_ENEMY_SLOTS - 1}"
            )
        return self.block_offset + 2 + 2 * slot_ix


def _parse_battle_at(entry: bytes, off: int) -> Optional[BattleBlock]:
    """Try to decode a BATTLE block at ``off``. Returns ``None`` when the
    bytes don't fit — used by both the events walker and the splice
    helpers so field replacement re-validates the block shape.
    """
    n = len(entry)
    if off < 0 or off + 12 > n:
        return None
    if entry[off] != 0xDA or entry[off + 1] != 0x00:
        return None
    enemies = tuple(
        struct.unpack_from("<H", entry, off + 2 + 2 * i)[0]
        for i in range(BATTLE_ENEMY_SLOTS)
    )
    cursor = off + 12
    if cursor + 2 > n or entry[cursor] != 0xD7 or entry[cursor + 1] != 0x00:
        return None
    cursor += 2
    d7_start = cursor
    # Same 64-byte payload ceiling as the annotator / chain decoder.
    while cursor + 1 < n and not (
        entry[cursor] == 0xD8 and entry[cursor + 1] == 0x00
    ):
        cursor += 2
        if cursor - d7_start > 64:
            return None
    if cursor + 8 > n:
        return None
    d7_payload = bytes(entry[d7_start:cursor])
    cursor += 2  # past D8 00
    bg_field_offset = cursor
    bg_id = struct.unpack_from("<H", entry, cursor)[0]
    cursor += 2
    if entry[cursor] != 0xD9 or entry[cursor + 1] != 0x00:
        return None
    cursor += 2  # past D9 00
    music_field_offset = cursor
    music_id = struct.unpack_from("<H", entry, cursor)[0]
    cursor += 2
    if entry[cursor] != 0xBA or entry[cursor + 1] != 0x00:
        return None
    cursor += 2
    return BattleBlock(
        block_offset=off,
        total_size=cursor - off,
        enemies=enemies,
        d7_payload=d7_payload,
        bg_id=bg_id,
        music_id=music_id,
        bg_field_offset=bg_field_offset,
        music_field_offset=music_field_offset,
    )


def replace_battle_enemy(
    entry: bytes, block_offset: int, slot_ix: int, new_enemy_id: int,
) -> bytes:
    """Rewrite one enemy u16 (0..4) inside the BATTLE block at ``block_offset``."""
    block = _parse_battle_at(entry, block_offset)
    if block is None:
        raise ValueError(
            f"no BATTLE block at offset 0x{block_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block.enemy_field_offset(slot_ix),
        int(new_enemy_id) & 0xFFFF,
    )
    return bytes(buf)


def replace_battle_bg(
    entry: bytes, block_offset: int, new_bg_id: int,
) -> bytes:
    """Rewrite the ``D8 00 [bg]`` u16 inside a BATTLE block."""
    block = _parse_battle_at(entry, block_offset)
    if block is None:
        raise ValueError(
            f"no BATTLE block at offset 0x{block_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block.bg_field_offset, int(new_bg_id) & 0xFFFF,
    )
    return bytes(buf)


def replace_battle_music(
    entry: bytes, block_offset: int, new_music_id: int,
) -> bytes:
    """Rewrite the ``D9 00 [music]`` u16 inside a BATTLE block."""
    block = _parse_battle_at(entry, block_offset)
    if block is None:
        raise ValueError(
            f"no BATTLE block at offset 0x{block_offset:04x}"
        )
    buf = bytearray(entry)
    struct.pack_into(
        "<H", buf, block.music_field_offset, int(new_music_id) & 0xFFFF,
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
