"""Loaders that walk a ROM bytearray and return the parsed model graph.

Ported from DWDDRandomizer/src/utils.py. Randomization-only helpers
(generateConditions, generateLvlupStats, etc.) are not included — those belong
in the randomizer, not in shared core.

Loaders for FAT-listed content (`DAT/dm/`, `DAT/en/`, `DAT/ec/`, `DAT/eq/`,
`DAT/sk/`, `DAT/MSG.PAK`) take an optional ``file_table`` to share a single
FNT parse across the session; if omitted, each call rebuilds one from the ROM
bytes. Within-file structure (page-offset header for /dm,en,eq,sk/, flat
fixed-stride for /ec/I0XX.BIN, per-area record for /ec/E0XX.BIN) is parsed by
``digimon_core.pagination``. ARM9 / Overlay loaders (moves, quests, traits,
habitats, ...) keep using hardcoded ROM offsets from ``constants.py`` because
that content lives in arm9.bin / overlays, not in FAT-listed files.
"""
from typing import Dict, Iterator, List, Optional, Tuple

from . import constants
from . import fnt
from . import model
from . import pagination


def writeRomBytes(rom_data: bytearray, new_value: int, offset: int, byte_size: int):
    rom_data[offset:offset + byte_size] = new_value.to_bytes(byte_size, byteorder="little")


# ---- FNT helpers -------------------------------------------------------------

def _table(rom_data: bytes, file_table: Optional[fnt.FileTable]) -> fnt.FileTable:
    """Use the caller's FileTable if supplied; otherwise parse a fresh one.

    Each loader call would otherwise re-walk the FNT/FAT. RomSession parses
    once at session start and threads the result through; standalone callers
    (tests, ad-hoc scripts) get the implicit per-call build for convenience.
    """
    return file_table if file_table is not None else fnt.FileTable.from_rom(bytes(rom_data))


def _iter_dir_files(table: fnt.FileTable, prefix: str) -> List[str]:
    """All FAT-listed paths under ``prefix``, sorted by trailing file index.

    DWDD names per-directory files numerically (``DAT/DM/0``, ``DAT/DM/1``, ...
    or ``DAT/EC/E000.BIN``, ``E001.BIN``, ...). Lexical sort matches numeric
    order because the digits are zero-padded for the .BIN naming and bare-int
    for /dm,/en,/eq,/sk/. Both schemes preserve ascending file-id order, which
    is the order vanilla FAT entries were laid out — important so the iteration
    matches the legacy hardcoded-offset walks byte-for-byte.
    """
    matches = [p for p in table.paths() if p.startswith(prefix)]
    # /dm/, /en/, /eq/, /sk/ use bare integer suffixes (0..97). Sort numerically
    # so "10" doesn't come before "2"; for /ec/'s E0XX.BIN / I0XX.BIN the
    # zero-padded names sort lexically the same as numerically anyway.
    def _key(path: str) -> Tuple[int, str]:
        tail = path[len(prefix):]
        # strip an optional .BIN extension before the int parse
        stem = tail.rsplit(".", 1)[0] if "." in tail else tail
        # E000 / I000 → strip leading non-digit prefix
        digits = "".join(ch for ch in stem if ch.isdigit())
        return (int(digits) if digits else 0, path)
    matches.sort(key=_key)
    return matches


def _iter_paged_records(
    rom_data: bytes,
    table: fnt.FileTable,
    dir_prefix: str,
    page_width: int,
) -> Iterator[Tuple[int, bytes]]:
    """Yield (rom_offset, page_bytes) for every page in every file under
    ``dir_prefix``. ``page_bytes`` is exactly ``page_width`` bytes sliced from
    the ROM at ``rom_offset``; callers decide whether to skip 0xFFFF
    sentinels."""
    for path in _iter_dir_files(table, dir_prefix):
        file_start, file_end = table.resolve(path)
        file_bytes = bytes(rom_data[file_start:file_end])
        for _idx, page_off, _page_len in pagination.iter_pages(file_bytes):
            rom_offset = file_start + page_off
            yield rom_offset, bytes(rom_data[rom_offset:rom_offset + page_width])


def getDigimonStage(digimon_id: int) -> str:
    if 0x41 <= digimon_id <= 0x57:
        return "IN-TRAINING"
    if 0x61 <= digimon_id <= 0x9D:
        return "ROOKIE"
    if 0xA8 <= digimon_id <= 0x115 and digimon_id != 0xBA:
        return "CHAMPION"
    if 0x120 <= digimon_id <= 0x188:
        return "ULTIMATE"
    if 0x191 <= digimon_id <= 0x1F4:
        return "MEGA"
    return ""


def getDigimonStageFromSpriteInfo(sprite_val: int) -> str:
    if 0x01 <= sprite_val <= 0x17:
        return "IN-TRAINING"
    if 0x18 <= sprite_val <= 0x54:
        return "ROOKIE"
    if 0x55 <= sprite_val <= 0xC1:
        return "CHAMPION"
    if 0xC2 <= sprite_val <= 0x12A:
        return "ULTIMATE"
    if 0x12B <= sprite_val <= 0x18E:
        return "MEGA"
    if sprite_val == 0x190:
        return "JOINT_SLOT_BOSS"
    if 0x18E <= sprite_val <= 0x19E:
        return "BATTLE_EXCLUSIVE"
    return ""


def getAllDigimonPairs() -> List[Tuple[str, int]]:
    pairs = []
    for stage in constants.DIGIMON_IDS:
        pairs += list(constants.DIGIMON_IDS[stage].items())
    return pairs


def get_digimon_names() -> List[str]:
    return [name for _, name in sorted(constants.DIGIMON_ID_TO_STR.items(), key=lambda x: x[0])]


def getCurrentLocation(current_offset: int, version: str) -> str:
    base_offset = constants.AREA_ENCOUNTER_OFFSETS[version][0]
    relative_offset = current_offset - base_offset
    location_offset = max(
        (off for off in constants.LOCATION_OFFSETS_TO_NAMES if off <= relative_offset),
        default=0x0000,
    )
    return constants.LOCATION_OFFSETS_TO_NAMES[location_offset]


def getLocationForAreaIndex(area_index: int) -> str:
    """Resolve the location name for an area by its position in the list.

    `LOCATION_OFFSETS_TO_NAMES` keys are relative offsets inside the
    legacy `AREA_ENCOUNTER_OFFSETS` region, assuming a fixed 0x200
    stride per area. Under FNT-driven loading each area is its own
    file and the absolute ROM file_start no longer lands on that stride
    (`getCurrentLocation` only worked when areas were laid out
    contiguously inside one region). FNT iteration still yields areas
    in legacy order, so the area's INDEX is the stable key.
    """
    relative_offset = area_index * model.WildEncounterArea.SIZE
    location_offset = max(
        (off for off in constants.LOCATION_OFFSETS_TO_NAMES if off <= relative_offset),
        default=0x0000,
    )
    return constants.LOCATION_OFFSETS_TO_NAMES[location_offset]


def buildWildAreaLabels(num_areas: int) -> List[str]:
    """Label each wild-encounter area as ``<Location> Area <n>``.

    ``n`` resets to 1 at each new location, so areas read as "Login
    Mountain Area 1", "Login Mountain Area 2", … The single source of
    truth shared by the Wild Encounters editor, the enemy editor's
    cross-references, and the footer validator so all three agree.
    """
    labels: List[str] = []
    last_location = ""
    counter = 0
    for ix in range(num_areas):
        location = getLocationForAreaIndex(ix)
        if location != last_location:
            counter = 1
            last_location = location
        else:
            counter += 1
        labels.append(f"{location} Area {counter}")
    return labels


# ---- model-table loaders -----------------------------------------------------

def loadBaseDigimonInfo(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> Dict[int, model.BaseDataDigimon]:
    """Walk ``DAT/dm/N`` files (98 on Dusk/Dawn). Each file has 8 0x44-wide
    pages; pages whose first u16 is ``0xFFFF`` are sentinel slots and skipped
    to match the legacy hardcoded-offset loader's record set.

    ``version`` is unused but kept in the signature for call-site uniformity
    with the ARM9 / Overlay loaders that still need it.
    """
    del version
    table = _table(rom_data, file_table)
    out: Dict[int, model.BaseDataDigimon] = {}
    for rom_offset, page_data in _iter_paged_records(rom_data, table, "DAT/DM/", 0x44):
        cur_id = int.from_bytes(page_data[0:2], byteorder="little")
        if cur_id == 0xffff:
            continue
        out[cur_id] = model.BaseDataDigimon(page_data, rom_offset)
    return out


def loadEnemyDigimonInfo(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> Dict[int, model.EnemyDataDigimon]:
    """Walk ``DAT/en/N`` files. 8 0x6C-wide pages each; 0xFFFF id pages skipped."""
    del version
    table = _table(rom_data, file_table)
    out: Dict[int, model.EnemyDataDigimon] = {}
    for rom_offset, page_data in _iter_paged_records(rom_data, table, "DAT/EN/", 0x6c):
        cur_id = int.from_bytes(page_data[0:2], byteorder="little")
        if cur_id == 0xffff:
            continue
        out[cur_id] = model.EnemyDataDigimon(page_data, rom_offset)
    return out


def loadQuestData(version: str, rom_data: bytearray) -> List[model.QuestData]:
    offset_start, offset_end = constants.QUEST_DATA_OFFSETS[version]
    out = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.QuestData(rom_data[seek:seek + 0x44], seek))
        seek += 0x44
    return out


def loadMoveData(version: str, rom_data: bytearray) -> List[model.MoveData]:
    offset_start, offset_end = constants.MOVE_DATA_OFFSETS[version]
    out = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.MoveData(rom_data[seek:seek + 0x1c], seek))
        seek += 0x1c
    return out


def loadEncounterRewardData(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> List[model.EncounterRewardTable]:
    """Walk ``DAT/ec/I0XX.BIN`` files (5 on Dusk/Dawn). Each file is a flat
    array of 0x20-byte records with no header; the FNT-trimmed file size
    is exactly ``num_records * 0x20`` so a stride walk gives the same record
    set the legacy 0x400-page-aligned + 0xFFFFFFFF-sentinel walk produced."""
    del version
    table = _table(rom_data, file_table)
    out: List[model.EncounterRewardTable] = []
    for path in _iter_dir_files(table, "DAT/EC/I"):
        file_start, file_end = table.resolve(path)
        file_bytes = bytes(rom_data[file_start:file_end])
        for _idx, rec_off in pagination.iter_fixed_records(file_bytes, 0x20):
            rom_offset = file_start + rec_off
            cur_data = bytes(rom_data[rom_offset:rom_offset + 0x20])
            out.append(model.EncounterRewardTable(cur_data, rom_offset))
    return out


# DWDD's vanilla /sk/ contains 63 files (DAT/SK/0..62) but only 54 hold
# StandardDigivolution records — SK/0..7 and SK/20 store unrelated data and
# are skipped. The 54 used files are paged with 8 0x70-records each except
# SK/62, which is a smaller 6-page file (header 0x18 bytes, not 0x20) for
# digimon ids 0x1F0..0x1F5. Both DUSK and DAWN share this layout exactly —
# only the ROM addresses of the files differ — so a single shared table is
# enough.
#
# Each entry: (sk_file_idx, first_digimon_id, num_entries). Within a file the
# pages are laid out in digimon-id order, so page index = digimon_id -
# first_digimon_id. ROM offsets come from parsing the file's page header at
# load time, which makes the mapping layout-invariant across grown ROMs.
_SK_DIGIVOLUTION_LAYOUT: List[Tuple[int, int, int]] = [
    (8, 0x40, 8), (9, 0x48, 8),
    (10, 0x50, 8), (11, 0x58, 8), (12, 0x60, 8), (13, 0x68, 8),
    (14, 0x70, 8), (15, 0x78, 8), (16, 0x80, 8), (17, 0x88, 8),
    (18, 0x90, 8), (19, 0x98, 8),
    # SK/20 is skipped — vanilla has no digivolutions for 0xA0..0xA7.
    (21, 0xA8, 8), (22, 0xB0, 8), (23, 0xB8, 8), (24, 0xC0, 8),
    (25, 0xC8, 8), (26, 0xD0, 8), (27, 0xD8, 8), (28, 0xE0, 8),
    (29, 0xE8, 8), (30, 0xF0, 8), (31, 0xF8, 8), (32, 0x100, 8),
    (33, 0x108, 8), (34, 0x110, 8), (35, 0x118, 8), (36, 0x120, 8),
    (37, 0x128, 8), (38, 0x130, 8), (39, 0x138, 8), (40, 0x140, 8),
    (41, 0x148, 8), (42, 0x150, 8), (43, 0x158, 8), (44, 0x160, 8),
    (45, 0x168, 8), (46, 0x170, 8), (47, 0x178, 8), (48, 0x180, 8),
    (49, 0x188, 8), (50, 0x190, 8), (51, 0x198, 8), (52, 0x1A0, 8),
    (53, 0x1A8, 8), (54, 0x1B0, 8), (55, 0x1B8, 8), (56, 0x1C0, 8),
    (57, 0x1C8, 8), (58, 0x1D0, 8), (59, 0x1D8, 8), (60, 0x1E0, 8),
    (61, 0x1E8, 8),
    (62, 0x1F0, 6),
]


def loadStandardDigivolutions(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> Dict[int, model.StandardDigivolution]:
    """Resolve each digimon_id's StandardDigivolution record via
    ``DAT/SK/N`` + page index (see ``_SK_DIGIVOLUTION_LAYOUT``).

    Page offsets are read from each file's page header at load time, so the
    resolution honors current file boundaries on grown ROMs and naturally
    handles SK/62's 6-page header (0x18) instead of the usual 8-page (0x20).
    """
    del version
    table = _table(rom_data, file_table)
    sk_files = _iter_dir_files(table, "DAT/SK/")
    out: Dict[int, model.StandardDigivolution] = {}
    for sk_idx, first_id, num_entries in _SK_DIGIVOLUTION_LAYOUT:
        if sk_idx >= len(sk_files):
            raise ValueError(
                f"SK layout references DAT/SK/{sk_idx} but FNT only lists "
                f"{len(sk_files)} sk files"
            )
        file_start, file_end = table.resolve(sk_files[sk_idx])
        page_offsets = pagination.parse_page_header(bytes(rom_data[file_start:file_end]))
        if len(page_offsets) < num_entries:
            raise ValueError(
                f"DAT/SK/{sk_idx} has {len(page_offsets)} pages but layout "
                f"expects at least {num_entries}"
            )
        for page_idx in range(num_entries):
            rom_offset = file_start + page_offsets[page_idx]
            data = bytes(rom_data[rom_offset:rom_offset + 0x70])
            out[first_id + page_idx] = model.StandardDigivolution(
                data, rom_offset, first_id + page_idx
            )
    return out


def loadArmorDigivolutions(version: str, rom_data: bytearray) -> List[model.ArmorDigivolution]:
    # offset_end is the *exclusive* end of the armor region (= start of DNA),
    # so the stop condition is `<` to avoid pulling the first DNA record into
    # the armor list.
    offset_start, offset_end = constants.ARMOR_DIGIVOLUTIONS_OFFSETS[version]
    out: List[model.ArmorDigivolution] = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.ArmorDigivolution(rom_data[seek:seek + 0x2c], seek))
        seek += 0x2c
    return out


def loadDnaDigivolutions(version: str, rom_data: bytearray):
    offset_start, offset_end = constants.DNA_DIGIVOLUTIONS_OFFSETS[version]
    out: List[model.DNADigivolution] = []
    conditions_by_id: Dict[int, List[List[int]]] = {}
    seek = offset_start
    while seek < offset_end:
        cur = model.DNADigivolution(rom_data[seek:seek + 0x24], seek)
        out.append(cur)
        conditions_by_id[cur.dna_evolution_id] = cur.getConditionsArray()
        seek += 0x24
    return out, conditions_by_id


def loadSpriteMapTable(version: str, rom_data: bytearray) -> List[model.SpriteMapEntry]:
    offset_start, offset_end = constants.SPRITE_MAPPING_TABLE_OFFSET[version]
    out: List[model.SpriteMapEntry] = []
    seek = offset_start
    while seek <= offset_end:
        out.append(model.SpriteMapEntry(rom_data[seek:seek + 0x10], seek))
        seek += 0x10
    return out


def loadBattleStringTable(version: str, rom_data: bytearray) -> List[model.BattleStringEntry]:
    offset_start, offset_end = constants.STRING_BATTLE_TABLE_OFFSET[version]
    out: List[model.BattleStringEntry] = []
    seek = offset_start
    while seek <= offset_end:
        value = int.from_bytes(rom_data[seek:seek + 4], byteorder="little")
        out.append(model.BattleStringEntry(seek, value))
        seek += 4
    return out


def _resolve_string_region(
    version: str,
    region: Tuple[str, int, int, str],
    file_table: fnt.FileTable,
) -> Tuple[int, int]:
    """Translate one ``STRING_REGIONS`` entry to current-ROM ``(start, end)``.

    ARM9 / overlay regions return their hardcoded offsets unchanged. ``msgpak_*``
    regions are file-relative inside ``DAT/MSG.PAK``: subtract the vanilla
    ``MSGPAK_FILE_START[version]`` to recover the within-file offset, then add
    the current FNT-resolved MSG.PAK start. The translation is identity on
    vanilla and gracefully follows MSG.PAK shifts on grown ROMs.
    """
    region_id, vanilla_start, vanilla_end, _label = region
    if not region_id.startswith("msgpak_"):
        return vanilla_start, vanilla_end
    cur_msgpak_start, _ = file_table.resolve("DAT/MSG.PAK")
    vanilla_msgpak_start = constants.MSGPAK_FILE_START[version]
    within_start = vanilla_start - vanilla_msgpak_start
    within_end = vanilla_end - vanilla_msgpak_start
    return cur_msgpak_start + within_start, cur_msgpak_start + within_end


def loadStringRegion(
    version: str,
    rom_data: bytearray,
    region_id: str,
    file_table: Optional[fnt.FileTable] = None,
) -> List[model.GameString]:
    """Parse one named string region into GameString objects.

    ``msgpak_*`` regions resolve their ROM-absolute range from the current
    ``DAT/MSG.PAK`` FAT entry so the editor follows the file when other FNT
    files have been grown ahead of it. ARM9 / overlay regions still use the
    hardcoded version-keyed offsets in ``constants.STRING_REGIONS``.

    For ``msgpak_*`` regions every string is also tagged with the
    ``PakEntryGroup`` it shares with its pak-entry siblings, so the editor
    can surface the 1024-byte per-entry cap.
    """
    from . import strings as _strings
    regions = constants.STRING_REGIONS.get(version, [])
    match = next((r for r in regions if r[0] == region_id), None)
    if match is None:
        raise KeyError(f"string region {region_id!r} not defined for {version}")
    table = _table(rom_data, file_table)
    start, end = _resolve_string_region(version, match, table)
    out: List[model.GameString] = []
    for offset, text, byte_len, terminator in _strings.decode_block(rom_data, start, end):
        out.append(model.GameString(offset, text, byte_len, terminator, region_id))
    return out


def loadAllStringRegions(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> Dict[str, List[model.GameString]]:
    """Parse every defined string region for `version`."""
    table = _table(rom_data, file_table)
    out: Dict[str, List[model.GameString]] = {}
    for region in constants.STRING_REGIONS.get(version, []):
        out[region[0]] = loadStringRegion(version, rom_data, region[0], file_table=table)
    return out


def loadHabitatsWorldmap(version: str, rom_data: bytearray) -> List[model.HabitatWorldmap]:
    offset_start, offset_end = constants.HABITATS_WORLDMAP_OFFSET[version]
    out: List[model.HabitatWorldmap] = []
    seek = offset_start
    while seek <= offset_end:
        out.append(model.HabitatWorldmap(rom_data[seek:seek + 0x18], seek))
        seek += 0x18
    return out


def loadFarmTerrains(version: str, rom_data: bytearray, num_terrains: int = 17) -> List[model.FarmTerrain]:
    start = constants.FARM_TERRAINS_START_OFFSET[version]
    out: List[model.FarmTerrain] = []
    for i in range(num_terrains):
        offset = start + i * 0x5c
        out.append(model.FarmTerrain(rom_data[offset:offset + 0x5c], offset))
    return out


def loadLvlupTypeTable(version: str, rom_data: bytearray) -> List[List[List[int]]]:
    offset = constants.LVLUP_TYPE_TABLE_OFFSET[version]
    table: List[List[List[int]]] = []
    cur = offset
    for _ in range(7):  # 7 digimon types
        stats = [
            [rom_data[cur], rom_data[cur + 1]],       # hp
            [rom_data[cur + 2], rom_data[cur + 3]],   # mp
            [rom_data[cur + 4], rom_data[cur + 5]],   # atk
            [rom_data[cur + 6], rom_data[cur + 7]],   # def
            [rom_data[cur + 8], rom_data[cur + 9]],   # spirit
            [rom_data[cur + 0xa], rom_data[cur + 0xb]],  # speed
        ]
        table.append(stats)
        cur += 0xc
    return table


def loadStarters(version: str, rom_data: bytearray, num_starters: int = 12) -> List[model.StarterEntry]:
    """12 × 8-byte starter records starting at STARTER_PACK_OFFSET[version].

    DUSK pack ends where DAWN pack begins (0x60 = 96 bytes = 12 × 8B), so 12
    is the per-version count.
    """
    start = constants.STARTER_PACK_OFFSET[version]
    out: List[model.StarterEntry] = []
    for i in range(num_starters):
        offset = start + i * model.StarterEntry.SIZE
        out.append(model.StarterEntry(rom_data[offset:offset + model.StarterEntry.SIZE], offset))
    return out


def loadWildEncounterAreas(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> List[model.WildEncounterArea]:
    """One ``WildEncounterArea`` per ``DAT/ec/E0XX.BIN`` (80 files on Dusk/Dawn).

    Under FNT each area is a standalone file (sizes 0x44..0x14C) with no
    0x200 padding; the ``WildEncounterArea`` model accepts variable file size
    via ``len(_raw)``. The parsed record set matches the legacy 0x200-strided
    walk byte-for-byte because the trimmed file excludes only the 0xFFFF
    padding bytes the writer never touched."""
    del version
    table = _table(rom_data, file_table)
    out: List[model.WildEncounterArea] = []
    for path in _iter_dir_files(table, "DAT/EC/E"):
        # "DAT/EC/E" also matches ENCTBL.BIN (cross-table file, format TBD —
        # see PLAN §13.3.c). With sort key (0, path) it slots between E000
        # and E001, shifting every subsequent area's label down by one
        # location boundary. Skip it explicitly so the legacy-style
        # `getLocationForAreaIndex` mapping stays aligned.
        if path.endswith("/ENCTBL.BIN"):
            continue
        file_start, file_end = table.resolve(path)
        file_bytes = bytes(rom_data[file_start:file_end])
        out.append(model.WildEncounterArea(file_bytes, file_start))
    return out


# Field-map count in ENCTBL.BIN (maps 0..264); a trailing all-FFFF
# terminator entry follows and is not loaded.
MAP_ENCOUNTER_COUNT = 265


def loadMapEncounterTable(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> List[model.MapEncounterEntry]:
    """Parse ``DAT/ec/ENCTBL.BIN`` — the per-field-map encounter table.

    265 fixed 8-byte entries, entry ``i`` == field map ``i``. Each assigns
    the map a wild-encounter area + battle background (+ two still-undecoded
    params). See ``model.MapEncounterEntry`` /
    ``research_docs/claude_notes/map_encounter_table.md``. Returns an empty
    list when ENCTBL.BIN isn't present (non-DWDD ROM / stripped build)."""
    del version
    table = _table(rom_data, file_table)
    enctbl_path = None
    for path in _iter_dir_files(table, "DAT/EC/E"):
        if path.endswith("/ENCTBL.BIN"):
            enctbl_path = path
            break
    if enctbl_path is None:
        return []
    file_start, file_end = table.resolve(enctbl_path)
    out: List[model.MapEncounterEntry] = []
    size = model.MapEncounterEntry.SIZE
    for map_id in range(MAP_ENCOUNTER_COUNT):
        eoff = file_start + map_id * size
        if eoff + size > file_end:
            break
        out.append(model.MapEncounterEntry(
            rom_data[eoff:eoff + size], eoff, map_id=map_id,
        ))
    return out


def loadConsumables(version: str, rom_data: bytearray) -> List[model.Consumable]:
    offset_start, offset_end = constants.CONSUMABLE_OFFSETS[version]
    out: List[model.Consumable] = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.Consumable(rom_data[seek:seek + model.Consumable.SIZE], seek))
        seek += model.Consumable.SIZE
    return out


def loadFarmItems(version: str, rom_data: bytearray) -> List[model.FarmItem]:
    offset_start, offset_end = constants.FARM_ITEM_OFFSETS[version]
    out: List[model.FarmItem] = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.FarmItem(rom_data[seek:seek + model.FarmItem.SIZE], seek))
        seek += model.FarmItem.SIZE
    return out


def loadFarmTrainingPens(version: str, rom_data: bytearray) -> List[model.FarmTrainingPen]:
    rng = constants.FARM_TRAINING_PEN_OFFSETS.get(version)
    if rng is None:
        return []
    offset_start, offset_end = rng
    out: List[model.FarmTrainingPen] = []
    seek = offset_start
    while seek < offset_end:
        out.append(model.FarmTrainingPen(rom_data[seek:seek + model.FarmTrainingPen.SIZE], seek))
        seek += model.FarmTrainingPen.SIZE
    return out


def loadEquipment(
    version: str,
    rom_data: bytearray,
    file_table: Optional[fnt.FileTable] = None,
) -> Dict[int, model.Equipment]:
    """Walk ``DAT/eq/N`` files (19 on Dusk/Dawn). Each file has 8 0x48-wide
    pages; the same id/level/species sanity check the legacy ROM-absolute
    id-scan used keeps placeholder slots out of the parsed set, yielding the
    same 146 vanilla records on both regions."""
    del version
    table = _table(rom_data, file_table)
    out: Dict[int, model.Equipment] = {}
    for rom_offset, page_data in _iter_paged_records(rom_data, table, "DAT/EQ/", 0x48):
        rec_id = int.from_bytes(page_data[0:2], byteorder="little")
        if not (0x8F <= rec_id <= 0x120) or rec_id in out:
            continue
        level = page_data[2]
        species = page_data[3]
        if not (1 <= level <= 99 and species <= 0x0C):
            continue
        out[rec_id] = model.Equipment(page_data, rom_offset)
    return out


def loadDigivolutionInformation(rom_data: bytearray, offset: int) -> Dict[int, List[List[int]]]:
    """Standard digivolution at `offset` → {evo_id: [[cond_id, cond_value], ...]}."""
    info = rom_data[offset:offset + 0x70]
    out: Dict[int, List[List[int]]] = {}
    for n in range(4):  # up to 4 evolution targets (degen + 3 evos)
        evo_id = int.from_bytes(info[n * 4:(n * 4) + 4], byteorder="little")
        if evo_id == 0xffffffff:
            continue
        conditions = []
        for c in range(3):  # up to 3 conditions per target
            ptr = 16 + (24 * n) + (8 * c)
            cond_id = int.from_bytes(info[ptr:ptr + 4], byteorder="little")
            cond_val = int.from_bytes(info[ptr + 4:ptr + 8], byteorder="little")
            if cond_id == 0x0:
                continue
            conditions.append([cond_id, cond_val])
        if conditions:
            out[evo_id] = conditions
    return out
