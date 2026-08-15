"""Shop inventories — the buy lists inside ARM9 overlay 9_9 (the shop UI).

Overlay 9_9 (the buy/sell UI for items / equipment / farm goods) reaches each
shop's stock through a **24-entry pointer table** (three contiguous 8-entry
sub-arrays: consumable, equipment, farm) followed by the ``FF FF``-terminated
u16 item-id lists themselves. Field scripts open a shop with the ``OPEN_SHOP``
opcode (``bb 00 <id:u16>``); the u16 ``id`` selects a slot in that table:

    id  0..1   remodel      (farm redecoration — no stock list, ignored here)
    id  2..9   farm goods   -> farm sub-array slot (id - 2)
    id 10..17  equipment    -> equipment sub-array slot (id - 10)
    id 18..25  consumable   -> consumable sub-array slot (id - 18)

There are 24 lists — 8 of each kind, one per shop town. Shops are labeled by
opener id (:func:`opener_label` → ``"Item Shop 3"``) rather than by location:
the same opener id gets reused by NPCs in several areas (e.g. the farm-remodel
service), so no single town owns a given list.

Parsing follows the pointer table rather than scanning a byte range: that finds
all 24 lists (four farm lists sit *before* the main block, which a range scan
misses) and yields each list's exact start and its opener id.

The lists are *single-type* (an item shop stocks only consumables, etc.) and
are displayed + priced by type, so putting a wrong-type id into a list makes
the shop render garbage (a farm item in an equipment shop shows a bogus price).
The editor therefore only ever swaps an id for another of the **same type**.

Item ids share one space classified by :data:`constants.ITEM_TYPE_IDS`
(FARM_ITEM ``0x00-0x4D`` · CONSUMABLE ``0x56-0x84`` · EQUIPMENT ``0x8F-0x116`` ·
DIGIEGG ``0x121-0x128`` · KEY_ITEM ``0x129-0x199``); names come from
:data:`constants.ITEM_ID_TO_STR`.

Edits are **in place, same length** (swap ids, don't add/remove — the lists are
contiguous and pointer-indexed, so resizing would shift every later list). That
keeps shop edits inside the ROM byte-diff (``.romproj``) with no channel.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import constants

# Per version: (base file offset of the 24-entry opener pointer table in
# overlay 9_9, ROM-file -> RAM delta to turn each pointer into a file offset).
# The table is laid out CONSUMABLE[8], EQUIPMENT[8], FARM_ITEM[8]
# (:data:`TABLE_KIND_ORDER`). Both traced + verified to recover 24 shops.
SHOP_POINTER_TABLE = {
    "DUSK_US": (0x002658B0, 0x0202B1C0),
    "DAWN_US": (0x002656B0, 0x0202B1E0),
}

# 8 towns; each has one consumable, one equipment and one farm shop.
SHOP_SLOTS = 8

# Physical order of the three 8-entry sub-arrays inside the pointer table.
TABLE_KIND_ORDER = ("CONSUMABLE", "EQUIPMENT", "FARM_ITEM")

# OPEN_SHOP opener-id base per kind (the opcode carries one u16 id; kind is
# derived from its range). remodel (id 0..1) has no stock list -> excluded.
OPENER_ID_BASE = {"FARM_ITEM": 2, "EQUIPMENT": 10, "CONSUMABLE": 18}

# Farm-remodel service openers (OPEN_SHOP ids 0-1) — no stock list of their
# own, so they aren't parsed as shops, but the OPEN_SHOP editor still offers
# them as reassignment targets.
REMODEL_OPENER_IDS = (0, 1)

# Per-kind display name; shops are numbered 1-based per kind in opener-id order
# (Item Shop 1..8, …). Labeled by id, not location: one opener id is reused by
# NPCs across areas, so no single town owns a shop.
_SHOP_KIND_NAME = {
    "CONSUMABLE": "Item Shop",
    "EQUIPMENT": "Equipment Shop",
    "FARM_ITEM": "Farm Shop",
    "REMODEL": "Farm Remodel",
}

# Only these item types are ever stocked by a shop; keep the editor's pickers
# (and the parse) to them so DIGIEGG/KEY_ITEM ids can't sneak in.
SHOP_ITEM_TYPES = ("CONSUMABLE", "EQUIPMENT", "FARM_ITEM")

# A stock list longer than this is almost certainly a bad pointer, not a shop.
_MAX_LIST = 64


def item_type(item_id: int) -> Optional[str]:
    """Classify an item id into its :data:`constants.ITEM_TYPE_IDS` bucket."""
    for name, (lo, hi) in constants.ITEM_TYPE_IDS.items():
        if lo <= item_id <= hi:
            return name
    return None


def opener_kind(opener_id: int) -> Optional[str]:
    """Shop kind an OPEN_SHOP id selects: a SHOP_ITEM_TYPES value, ``"REMODEL"``
    for the farm-remodel service (ids 0-1), or ``None`` if out of range."""
    if opener_id in REMODEL_OPENER_IDS:
        return "REMODEL"
    for kind, base in OPENER_ID_BASE.items():
        if base <= opener_id < base + SHOP_SLOTS:
            return kind
    return None


def opener_label(opener_id: int) -> str:
    """Stable, location-free name for an OPEN_SHOP target — e.g. ``Item Shop 3``.

    Numbered 1-based per kind in opener-id order; the remodel ids become
    ``Farm Remodel 1``/``2``. Shops are named by id, not town, because one
    opener id is reused by NPCs in several areas."""
    kind = opener_kind(opener_id)
    if kind is None:
        return f"Shop 0x{opener_id & 0xFFFF:x}"
    base = OPENER_ID_BASE.get(kind, 0)  # REMODEL isn't in the dict -> base 0
    return f"{_SHOP_KIND_NAME[kind]} {opener_id - base + 1}"


def opener_ids() -> List[int]:
    """Every valid OPEN_SHOP id — 0-1 remodel + the 24 town shops (2-25)."""
    return list(range(SHOP_SLOTS * 3 + 2))


def items_of_type(type_name: str) -> List[Tuple[int, str]]:
    """``[(item_id, name), …]`` for every known item of ``type_name`` — the
    valid choices for a shop of that type."""
    rng = constants.ITEM_TYPE_IDS.get(type_name)
    if rng is None:
        return []
    lo, hi = rng
    return [
        (i, constants.ITEM_ID_TO_STR.get(i, f"#{i:#x}"))
        for i in range(lo, hi + 1)
        if i in constants.ITEM_ID_TO_STR
    ]


@dataclass
class ShopInventory:
    """One town shop's stock: a same-length-editable list of item ids."""
    index: int            # ordinal among the parsed shops (stable label)
    offset: int           # ROM file offset of item_ids[0] (u16 each)
    item_type: str        # one of SHOP_ITEM_TYPES
    item_ids: List[int]   # mutable; the editor swaps ids in place (same count)
    opener_id: int        # OPEN_SHOP field-script id that reaches this list
    original_count: int = field(default=0)

    @property
    def label(self) -> str:
        return opener_label(self.opener_id)

    def __post_init__(self):
        if not self.original_count:
            self.original_count = len(self.item_ids)

    @property
    def type_range(self) -> Tuple[int, int]:
        return constants.ITEM_TYPE_IDS[self.item_type]

    def is_valid_id(self, item_id: int) -> bool:
        lo, hi = self.type_range
        return lo <= item_id <= hi


def _read_list(rom_data, off: int, lo: int, hi: int) -> Optional[List[int]]:
    """Read a ``FF FF``-terminated u16 list of ``[lo, hi]`` ids at ``off``.

    Returns ``None`` (rather than a partial list) if the pointer doesn't land
    on a well-formed same-type list — that's the signal that the pointer table
    base is wrong for this ROM, so the whole parse should bail gracefully.
    """
    if off < 0 or off + 2 > len(rom_data):
        return None
    ids: List[int] = []
    p = off
    for _ in range(_MAX_LIST + 1):
        if p + 2 > len(rom_data):
            return None
        v = struct.unpack_from("<H", rom_data, p)[0]
        if v == 0xFFFF:
            return ids or None
        if not (lo <= v <= hi):
            return None
        ids.append(v)
        p += 2
    return None  # no terminator within a sane length


def parse_shops(version: str, rom_data) -> List[ShopInventory]:
    """Resolve the 24 town shops via overlay 9_9's opener pointer table.

    Returns ``[]`` for versions we haven't mapped, or if the pointer table
    doesn't validate against this ROM (any pointer missing its typed list).
    """
    tbl = SHOP_POINTER_TABLE.get(version)
    if tbl is None:
        return []
    base, ram_delta = tbl

    # (opener_id, kind, offset, item_ids) per pointer-table slot.
    parsed = []
    for kix, kind in enumerate(TABLE_KIND_ORDER):
        lo, hi = constants.ITEM_TYPE_IDS[kind]
        for slot in range(SHOP_SLOTS):
            ptr_off = base + (kix * SHOP_SLOTS + slot) * 4
            if ptr_off + 4 > len(rom_data):
                return []
            ptr = struct.unpack_from("<I", rom_data, ptr_off)[0]
            ids = _read_list(rom_data, ptr - ram_delta, lo, hi)
            if ids is None:
                return []
            parsed.append((OPENER_ID_BASE[kind] + slot, kind,
                           ptr - ram_delta, ids))

    parsed.sort(key=lambda e: e[0])  # by opener id
    return [
        ShopInventory(i, off, kind, ids, opener_id)
        for i, (opener_id, kind, off, ids) in enumerate(parsed)
    ]


def write_shops(out: bytearray, shops: List[ShopInventory]) -> None:
    """Write each shop's ``item_ids`` back in place (u16 LE). Same-length only —
    a length change would desync the FF FF terminator + later lists, so it's a
    hard error (the editor never changes counts)."""
    for s in shops:
        if len(s.item_ids) != s.original_count:
            raise ValueError(
                f"shop {s.index} item count changed "
                f"({s.original_count} -> {len(s.item_ids)}); in-place edits only")
        for i, item_id in enumerate(s.item_ids):
            struct.pack_into("<H", out, s.offset + i * 2, item_id & 0xFFFF)
