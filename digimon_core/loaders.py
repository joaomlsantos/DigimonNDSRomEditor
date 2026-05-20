"""Loaders that walk a ROM bytearray and return the parsed model graph.

Ported from DWDDRandomizer/src/utils.py. Randomization-only helpers
(generateConditions, generateLvlupStats, etc.) are not included — those belong
in the randomizer, not in shared core.
"""
from typing import Dict, List, Tuple

from . import constants
from . import model


def writeRomBytes(rom_data: bytearray, new_value: int, offset: int, byte_size: int):
    rom_data[offset:offset + byte_size] = new_value.to_bytes(byte_size, byteorder="little")


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


# ---- model-table loaders -----------------------------------------------------

def loadBaseDigimonInfo(version: str, rom_data: bytearray) -> Dict[int, model.BaseDataDigimon]:
    offset_start, offset_end = constants.BASE_DIGIMON_OFFSETS[version]
    seek_offset = offset_start
    out: Dict[int, model.BaseDataDigimon] = {}

    while seek_offset <= offset_end:
        cur_offset = seek_offset
        header_skip = int.from_bytes(rom_data[cur_offset:cur_offset + 4], byteorder="little")
        cur_offset += header_skip

        cur_data = rom_data[cur_offset:cur_offset + 0x44]
        cur_id = int.from_bytes(cur_data[0:2], byteorder="little")

        while cur_id != 0xffff and cur_offset < seek_offset + 0x400:
            out[cur_id] = model.BaseDataDigimon(cur_data, cur_offset)
            cur_offset += 0x44
            cur_data = rom_data[cur_offset:cur_offset + 0x44]
            cur_id = int.from_bytes(cur_data[0:2], byteorder="little")

        seek_offset += 0x400
    return out


def loadEnemyDigimonInfo(version: str, rom_data: bytearray) -> Dict[int, model.EnemyDataDigimon]:
    offset_start, offset_end = constants.ENEMY_DIGIMON_OFFSETS[version]
    seek_offset = offset_start
    out: Dict[int, model.EnemyDataDigimon] = {}

    while seek_offset <= offset_end:
        cur_offset = seek_offset
        header_skip = int.from_bytes(rom_data[cur_offset:cur_offset + 4], byteorder="little")
        cur_offset += header_skip

        cur_data = rom_data[cur_offset:cur_offset + 0x6c]
        cur_id = int.from_bytes(cur_data[0:2], byteorder="little")

        while cur_id != 0xffff and cur_offset < seek_offset + 0x400:
            out[cur_id] = model.EnemyDataDigimon(cur_data, cur_offset)
            cur_offset += 0x6c
            cur_data = rom_data[cur_offset:cur_offset + 0x6c]
            cur_id = int.from_bytes(cur_data[0:2], byteorder="little")

        seek_offset += 0x400
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


def loadEncounterRewardData(version: str, rom_data: bytearray) -> List[model.EncounterRewardTable]:
    offset_start, offset_end = constants.ENCOUNTER_REWARD_OFFSETS[version]
    page_offset = offset_start
    seek = offset_start
    out: List[model.EncounterRewardTable] = []

    while seek < offset_end:
        cur_data = rom_data[seek:seek + 0x20]
        # 0xffffffff sentinel ends a page; advance to next 0x400 page
        if hex(int.from_bytes(cur_data[0:4], byteorder="little")) == "0xffffffff":
            page_offset += 0x400
            seek = page_offset
            continue
        out.append(model.EncounterRewardTable(cur_data, seek))
        seek += 0x20
    return out


def loadStandardDigivolutions(version: str, rom_data: bytearray) -> Dict[int, model.StandardDigivolution]:
    out: Dict[int, model.StandardDigivolution] = {}
    for digimon_id, addr in constants.DIGIVOLUTION_ADDRESSES[version].items():
        out[digimon_id] = model.StandardDigivolution(rom_data[addr:addr + 0x70], addr, digimon_id)
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


def loadWildEncounterAreas(version: str, rom_data: bytearray) -> List[model.WildEncounterArea]:
    """0x200-byte wild-encounter regions covering AREA_ENCOUNTER_OFFSETS[version].

    The endpoint in AREA_ENCOUNTER_OFFSETS is the *start* of the last area, so
    `<= offset_end` matches the iteration pattern in DWDDRandomizer's
    randomizeAreaEncounters.
    """
    offset_start, offset_end = constants.AREA_ENCOUNTER_OFFSETS[version]
    out: List[model.WildEncounterArea] = []
    seek = offset_start
    while seek <= offset_end:
        out.append(model.WildEncounterArea(rom_data[seek:seek + model.WildEncounterArea.SIZE], seek))
        seek += model.WildEncounterArea.SIZE
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


def loadEquipment(version: str, rom_data: bytearray) -> Dict[int, model.Equipment]:
    """Brute-force id-scan the equipment region.

    Pages 0-9 of the /eq/ region have a uniform header + 8-record layout but
    pages 10-18 have irregular pointer tables; rather than decoding each
    header, walk every 4-byte-aligned slot and accept ones whose first 4 bytes
    look like a valid equipment record (id in 0x8F..0x120, level 1-99, species
    enum value). This matches all 146 vanilla records on both DUSK and DAWN.
    """
    offset_start, offset_end = constants.EQUIPMENT_OFFSETS[version]
    out: Dict[int, model.Equipment] = {}
    i = offset_start
    while i <= offset_end - model.Equipment.SIZE:
        rec_id = int.from_bytes(rom_data[i:i + 2], byteorder="little")
        if 0x8F <= rec_id <= 0x120 and rec_id not in out:
            level = rom_data[i + 2]
            species = rom_data[i + 3]
            if 1 <= level <= 99 and species <= 0x0C:
                out[rec_id] = model.Equipment(rom_data[i:i + model.Equipment.SIZE], i)
                i += model.Equipment.SIZE
                continue
        i += 4
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
