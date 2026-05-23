from enum import Enum
from typing import List, Tuple

from . import constants
from . import strings as _strings


class Species(Enum):
    HOLY = 0
    DARK = 1
    DRAGON = 2
    BEAST = 3
    BIRD = 4
    MACHINE = 5
    AQUAN = 6
    INSECTPLANT = 7
    UNKNOWN = 8


class Element(Enum):
    LIGHT = 0
    DARK = 1
    FIRE = 2
    EARTH = 3
    WIND = 4
    STEEL = 5
    WATER = 6
    THUNDER = 7


# kept as module constants so constants.py doesn't need to import model.py
ELEMENTAL_RESISTANCES = {
    Species.HOLY: Element.LIGHT,
    Species.DARK: Element.DARK,
    Species.DRAGON: Element.FIRE,
    Species.BEAST: Element.EARTH,
    Species.BIRD: Element.WIND,
    Species.MACHINE: Element.STEEL,
    Species.AQUAN: Element.WATER,
    Species.INSECTPLANT: Element.THUNDER,
}

ELEMENTAL_WEAKNESSES = {
    Species.HOLY: Element.DARK,
    Species.DARK: Element.LIGHT,
    Species.DRAGON: Element.EARTH,
    Species.BEAST: Element.FIRE,
    Species.BIRD: Element.THUNDER,
    Species.MACHINE: Element.WATER,
    Species.AQUAN: Element.STEEL,
    Species.INSECTPLANT: Element.WIND,
}


class DigimonType(Enum):
    BALANCE = 0
    ATTACKER = 1
    TANK = 2
    TECHNICAL = 3
    SPEED = 4
    HPTYPE = 5
    MPTYPE = 6


class LvlUpMode(Enum):
    RANDOM = 0
    FIXED_MIN = 1
    FIXED_AVG = 2
    FIXED_MAX = 3


class ItemType(Enum):
    NULL = 0
    FARM_ITEM = 1
    CONSUMABLE = 2
    EQUIPMENT = 3
    DIGIEGG = 4
    KEY_ITEM = 5


# Every model exposes:
#   __init__(data: bytearray, offset: int)   — parse a region at `offset`
#   getByteArray() -> bytearray              — serialize back to raw bytes
#   writeToRom(rom_data: bytearray)          — write self into rom_data at self.offset
#
# Round-trip invariant: for any region a vanilla ROM stores,
#   parse(data, offset).getByteArray() == data[offset:offset+SIZE]
# This is what makes the package usable for an editor.


class SpriteMapEntry:
    SIZE = 0x10

    offset: int
    id: int
    unknown_0x4: int
    main_sprite: int
    upperscreen_sprites: int

    def __init__(self, sprite_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(sprite_data[0:4], byteorder="little")
        self.unknown_0x4 = int.from_bytes(sprite_data[4:8], byteorder="little")
        self.main_sprite = int.from_bytes(sprite_data[8:0xc], byteorder="little")
        self.upperscreen_sprites = int.from_bytes(sprite_data[0xc:0x10], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:4] = self.id.to_bytes(4, byteorder="little")
        out[4:8] = self.unknown_0x4.to_bytes(4, byteorder="little")
        out[8:0xc] = self.main_sprite.to_bytes(4, byteorder="little")
        out[0xc:0x10] = self.upperscreen_sprites.to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class BattleStringEntry:
    SIZE = 4

    offset: int
    value: int

    def __init__(self, offset: int, value: int):
        self.offset = offset
        self.value = value

    def getByteArray(self) -> bytearray:
        return bytearray(self.value.to_bytes(4, byteorder="little"))

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class GameString:
    """A single in-game string at a fixed ROM offset.

    Strings end at FE FF ([END]) or FF FF. The terminator is owned by the
    string (its bytes are counted in `original_byte_length`) but kept out of
    `text` to avoid confusing the editor. Pointers to this offset live
    elsewhere in the ROM and aren't repointed by the editor, so the encoded
    bytes must fit within `original_byte_length`; shorter encodings are
    padded with NUL bytes.

    When an edit shortens the string, the terminator is rewritten as [END]
    (FE FF) regardless of what was originally there — that's the unified
    in-engine sentinel that stops rendering cleanly.
    """

    offset: int
    text: str
    original_byte_length: int  # bytes from offset through terminator, inclusive
    original_terminator: int   # END_MARKER or TERMINATOR
    region_id: str

    def __init__(
        self,
        offset: int,
        text: str,
        original_byte_length: int,
        original_terminator: int,
        region_id: str = "",
    ):
        self.offset = offset
        self.text = text
        self.original_byte_length = original_byte_length
        self.original_terminator = original_terminator
        self.region_id = region_id
        # Snapshot of `text` at parse time. The validation collector compares
        # the live `text` against this to skip the (expensive) encode pass for
        # unmodified strings — by construction, a parsed string always fits
        # its budget. Identity check first (the common case after assignment
        # diverges) falls back to value equality so undoing back to vanilla
        # also takes the fast path.
        self._initial_text = text

    def _resolved_terminator(self) -> int:
        """Pick the terminator to write: original on exact-fit, [END] if shortened."""
        # +2 for the terminator itself; if there's room left over, the user
        # truncated the string and per the spec we write [END] so the engine
        # stops cleanly. Otherwise the original terminator is preserved.
        char_bytes = _strings.byte_length(self.text, terminator=None)
        if char_bytes + 2 < self.original_byte_length:
            return _strings.END_MARKER
        return self.original_terminator

    def encoded_length(self) -> int:
        """Byte length of `text` re-encoded with its trailing terminator."""
        return _strings.byte_length(self.text, terminator=self._resolved_terminator())

    def fits(self) -> bool:
        return self.encoded_length() <= self.original_byte_length

    def getByteArray(self) -> bytearray:
        encoded = _strings.encode_string(
            self.text, terminator=self._resolved_terminator()
        )
        if len(encoded) > self.original_byte_length:
            raise _strings.StringTooLongError(
                f"string at 0x{self.offset:08x} ({self.region_id}): "
                f"encoded {len(encoded)} bytes exceeds budget {self.original_byte_length}"
            )
        out = bytearray(self.original_byte_length)
        out[:len(encoded)] = encoded
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.original_byte_length] = self.getByteArray()


class HabitatWorldmap:
    SIZE = 0x18

    offset: int
    x_coordinate: int
    y_coordinate: int
    species_living: int
    map_preview_id: int
    location_text_id: int
    location_available_flag: int
    location_visited_flag: int
    unknown_0x0e: int
    unknown_0x10: int
    unknown_0x12: int
    location_destination_id: int
    spawn_position_flag: int

    def __init__(self, habitat_data: bytearray, offset: int):
        self.offset = offset
        self.x_coordinate = int.from_bytes(habitat_data[0:2], byteorder="little")
        self.y_coordinate = int.from_bytes(habitat_data[2:4], byteorder="little")
        self.species_living = int.from_bytes(habitat_data[4:6], byteorder="little")
        self.map_preview_id = int.from_bytes(habitat_data[6:8], byteorder="little")
        self.location_text_id = int.from_bytes(habitat_data[8:0xa], byteorder="little")
        self.location_available_flag = int.from_bytes(habitat_data[0xa:0xc], byteorder="little")
        self.location_visited_flag = int.from_bytes(habitat_data[0xc:0xe], byteorder="little")
        self.unknown_0x0e = int.from_bytes(habitat_data[0xe:0x10], byteorder="little")
        self.unknown_0x10 = int.from_bytes(habitat_data[0x10:0x12], byteorder="little")
        self.unknown_0x12 = int.from_bytes(habitat_data[0x12:0x14], byteorder="little")
        self.location_destination_id = int.from_bytes(habitat_data[0x14:0x16], byteorder="little")
        self.spawn_position_flag = int.from_bytes(habitat_data[0x16:0x18], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.x_coordinate.to_bytes(2, byteorder="little")
        out[2:4] = self.y_coordinate.to_bytes(2, byteorder="little")
        out[4:6] = self.species_living.to_bytes(2, byteorder="little")
        out[6:8] = self.map_preview_id.to_bytes(2, byteorder="little")
        out[8:0xa] = self.location_text_id.to_bytes(2, byteorder="little")
        out[0xa:0xc] = self.location_available_flag.to_bytes(2, byteorder="little")
        out[0xc:0xe] = self.location_visited_flag.to_bytes(2, byteorder="little")
        out[0xe:0x10] = self.unknown_0x0e.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.unknown_0x10.to_bytes(2, byteorder="little")
        out[0x12:0x14] = self.unknown_0x12.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.location_destination_id.to_bytes(2, byteorder="little")
        out[0x16:0x18] = self.spawn_position_flag.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class MoveData:
    SIZE = 0x1c

    offset: int
    id: int
    mp_cost: int
    element: Element
    special_identifier: int
    primary_effect: int
    primary_value: int
    secondary_effect: int
    secondary_value: int
    unknown_0xe: int
    is_consumable: int
    num_hits: int
    move_range: int
    unknown_0x14: int
    unknown_0x16: int
    level_learned: int
    eos_bytes: int

    def __init__(self, move_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(move_data[0:2], byteorder="little")
        self.mp_cost = int.from_bytes(move_data[2:4], byteorder="little")
        self.element = Element(move_data[4])
        self.special_identifier = move_data[5]
        self.primary_effect = int.from_bytes(move_data[6:8], byteorder="little")
        self.primary_value = int.from_bytes(move_data[8:0xa], byteorder="little")
        self.secondary_effect = int.from_bytes(move_data[0xa:0xc], byteorder="little")
        self.secondary_value = int.from_bytes(move_data[0xc:0xe], byteorder="little")
        self.unknown_0xe = int.from_bytes(move_data[0xe:0x10], byteorder="little")
        self.is_consumable = int.from_bytes(move_data[0x10:0x12], byteorder="little")
        self.num_hits = move_data[0x12]
        self.move_range = move_data[0x13]
        self.unknown_0x14 = int.from_bytes(move_data[0x14:0x16], byteorder="little")
        self.unknown_0x16 = int.from_bytes(move_data[0x16:0x18], byteorder="little")
        self.level_learned = int.from_bytes(move_data[0x18:0x1a], byteorder="little")
        self.eos_bytes = int.from_bytes(move_data[0x1a:0x1c], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2:4] = self.mp_cost.to_bytes(2, byteorder="little")
        out[4] = self.element.value
        out[5] = self.special_identifier
        out[6:8] = self.primary_effect.to_bytes(2, byteorder="little")
        out[8:0xa] = self.primary_value.to_bytes(2, byteorder="little")
        out[0xa:0xc] = self.secondary_effect.to_bytes(2, byteorder="little")
        out[0xc:0xe] = self.secondary_value.to_bytes(2, byteorder="little")
        out[0xe:0x10] = self.unknown_0xe.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.is_consumable.to_bytes(2, byteorder="little")
        out[0x12] = self.num_hits
        out[0x13] = self.move_range
        out[0x14:0x16] = self.unknown_0x14.to_bytes(2, byteorder="little")
        out[0x16:0x18] = self.unknown_0x16.to_bytes(2, byteorder="little")
        out[0x18:0x1a] = self.level_learned.to_bytes(2, byteorder="little")
        out[0x1a:0x1c] = self.eos_bytes.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class BaseDataDigimon:
    SIZE = 0x40

    offset: int
    id: int
    level: int
    species: Species
    hp: int
    # bytes 6:8 are padding in vanilla ROMs; preserved on round-trip
    _padding_0x6: int
    mp: int
    attack: int
    defense: int
    spirit: int
    speed: int
    evasion: int
    aptitude: int
    light_res: int
    dark_res: int
    fire_res: int
    earth_res: int
    wind_res: int
    steel_res: int
    water_res: int
    thunder_res: int
    unknown_0x26: int
    trait_1: int
    trait_2: int
    trait_3: int
    trait_4: int
    support_trait: int
    digimon_type: DigimonType
    move_signature: int
    move_1: int
    move_2: int
    move_3: int
    move_4: int
    unknown_0x38: int
    dex_habitat: int
    unknown_0x3A: int
    is_scannable: int
    exp_curve: int

    def __init__(self, digimon_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(digimon_data[0:2], byteorder="little")
        self.level = digimon_data[2]
        self.species = Species(digimon_data[3])
        self.hp = int.from_bytes(digimon_data[4:6], byteorder="little")
        self._padding_0x6 = int.from_bytes(digimon_data[6:8], byteorder="little")
        self.mp = int.from_bytes(digimon_data[8:0xa], byteorder="little")
        self.attack = int.from_bytes(digimon_data[0xa:0xc], byteorder="little")
        self.defense = int.from_bytes(digimon_data[0xc:0xe], byteorder="little")
        self.spirit = int.from_bytes(digimon_data[0xe:0x10], byteorder="little")
        self.speed = int.from_bytes(digimon_data[0x10:0x12], byteorder="little")
        self.evasion = int.from_bytes(digimon_data[0x12:0x14], byteorder="little")
        self.aptitude = int.from_bytes(digimon_data[0x14:0x16], byteorder="little")
        self.light_res = int.from_bytes(digimon_data[0x16:0x18], byteorder="little")
        self.dark_res = int.from_bytes(digimon_data[0x18:0x1a], byteorder="little")
        self.fire_res = int.from_bytes(digimon_data[0x1a:0x1c], byteorder="little")
        self.earth_res = int.from_bytes(digimon_data[0x1c:0x1e], byteorder="little")
        self.wind_res = int.from_bytes(digimon_data[0x1e:0x20], byteorder="little")
        self.steel_res = int.from_bytes(digimon_data[0x20:0x22], byteorder="little")
        self.water_res = int.from_bytes(digimon_data[0x22:0x24], byteorder="little")
        self.thunder_res = int.from_bytes(digimon_data[0x24:0x26], byteorder="little")
        self.unknown_0x26 = int.from_bytes(digimon_data[0x26:0x28], byteorder="little")
        self.trait_1 = digimon_data[0x28]
        self.trait_2 = digimon_data[0x29]
        self.trait_3 = digimon_data[0x2a]
        self.trait_4 = digimon_data[0x2b]
        self.support_trait = digimon_data[0x2c]
        self.digimon_type = DigimonType(digimon_data[0x2d])
        self.move_signature = int.from_bytes(digimon_data[0x2e:0x30], byteorder="little")
        self.move_1 = int.from_bytes(digimon_data[0x30:0x32], byteorder="little")
        self.move_2 = int.from_bytes(digimon_data[0x32:0x34], byteorder="little")
        self.move_3 = int.from_bytes(digimon_data[0x34:0x36], byteorder="little")
        self.move_4 = int.from_bytes(digimon_data[0x36:0x38], byteorder="little")
        self.unknown_0x38 = digimon_data[0x38]
        self.dex_habitat = digimon_data[0x39]
        self.unknown_0x3A = digimon_data[0x3a]
        self.is_scannable = digimon_data[0x3b]
        self.exp_curve = int.from_bytes(digimon_data[0x3c:0x40], byteorder="little")

    def getBaseStats(self) -> List[int]:
        return [self.hp, self.mp, self.attack, self.defense, self.spirit, self.speed, self.aptitude]

    def setBaseStats(self, stats_array: List[int]):
        if len(stats_array) != 7:
            raise ValueError(f"setBaseStats expects 7 values, got {len(stats_array)}")
        for attr, val in zip(
            ["hp", "mp", "attack", "defense", "spirit", "speed", "aptitude"], stats_array
        ):
            if val == -1:
                continue
            setattr(self, attr, val)

    def getRegularMoves(self) -> List[int]:
        return [self.move_1, self.move_2, self.move_3, self.move_4]

    def setRegularMoves(self, move_array: List[int]):
        for attr, val in zip(["move_1", "move_2", "move_3", "move_4"], move_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def getResistanceValues(self) -> List[int]:
        return [
            self.light_res, self.dark_res, self.fire_res, self.earth_res,
            self.wind_res, self.steel_res, self.water_res, self.thunder_res,
        ]

    def setResistanceValues(self, resistance_array: List[int]):
        if len(resistance_array) != 8:
            raise ValueError(f"setResistanceValues expects 8 values, got {len(resistance_array)}")
        attrs = ["light_res", "dark_res", "fire_res", "earth_res",
                 "wind_res", "steel_res", "water_res", "thunder_res"]
        for attr, val in zip(attrs, resistance_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def getRegularTraits(self) -> List[int]:
        return [self.trait_1, self.trait_2, self.trait_3, self.trait_4]

    def setRegularTraits(self, trait_array: List[int]):
        for attr, val in zip(["trait_1", "trait_2", "trait_3", "trait_4"], trait_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2] = self.level
        out[3] = self.species.value
        out[4:6] = self.hp.to_bytes(2, byteorder="little")
        out[6:8] = self._padding_0x6.to_bytes(2, byteorder="little")
        out[8:0xa] = self.mp.to_bytes(2, byteorder="little")
        out[0xa:0xc] = self.attack.to_bytes(2, byteorder="little")
        out[0xc:0xe] = self.defense.to_bytes(2, byteorder="little")
        out[0xe:0x10] = self.spirit.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.speed.to_bytes(2, byteorder="little")
        out[0x12:0x14] = self.evasion.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.aptitude.to_bytes(2, byteorder="little")
        out[0x16:0x18] = self.light_res.to_bytes(2, byteorder="little")
        out[0x18:0x1a] = self.dark_res.to_bytes(2, byteorder="little")
        out[0x1a:0x1c] = self.fire_res.to_bytes(2, byteorder="little")
        out[0x1c:0x1e] = self.earth_res.to_bytes(2, byteorder="little")
        out[0x1e:0x20] = self.wind_res.to_bytes(2, byteorder="little")
        out[0x20:0x22] = self.steel_res.to_bytes(2, byteorder="little")
        out[0x22:0x24] = self.water_res.to_bytes(2, byteorder="little")
        out[0x24:0x26] = self.thunder_res.to_bytes(2, byteorder="little")
        out[0x26:0x28] = self.unknown_0x26.to_bytes(2, byteorder="little")
        out[0x28] = self.trait_1
        out[0x29] = self.trait_2
        out[0x2a] = self.trait_3
        out[0x2b] = self.trait_4
        out[0x2c] = self.support_trait
        out[0x2d] = self.digimon_type.value
        out[0x2e:0x30] = self.move_signature.to_bytes(2, byteorder="little")
        out[0x30:0x32] = self.move_1.to_bytes(2, byteorder="little")
        out[0x32:0x34] = self.move_2.to_bytes(2, byteorder="little")
        out[0x34:0x36] = self.move_3.to_bytes(2, byteorder="little")
        out[0x36:0x38] = self.move_4.to_bytes(2, byteorder="little")
        out[0x38] = self.unknown_0x38
        out[0x39] = self.dex_habitat
        out[0x3a] = self.unknown_0x3A
        out[0x3b] = self.is_scannable
        out[0x3c:0x40] = self.exp_curve.to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class EnemyDataDigimon:
    SIZE = 0x6c

    offset: int
    id: int
    level: int
    species: Species
    hp: int
    _padding_0x6: int  # bytes 6:8 are padding in vanilla
    mp: int
    attack: int
    defense: int
    spirit: int
    speed: int
    evasion: int
    light_res: int
    dark_res: int
    fire_res: int
    earth_res: int
    wind_res: int
    steel_res: int
    water_res: int
    thunder_res: int
    unknown_0x24: int
    trait_1: int  # enemy traits are 2 bytes each (unlike base data which is 1 byte)
    trait_2: int
    trait_3: int
    trait_4: int
    move_signature: int
    move_1: int
    move_2: int
    move_3: int
    move_4: int
    usage_weight_signature: int
    usage_weight_move1: int
    usage_weight_move2: int
    usage_weight_move3: int
    holy_exp: int
    dark_exp: int
    dragon_exp: int
    beast_exp: int
    bird_exp: int
    machine_exp: int
    aquan_exp: int
    insectplant_exp: int
    unknown_0x5C: int
    unknown_0x60: int
    unknown_0x64: int
    unknown_0x68: int

    def __init__(self, digimon_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(digimon_data[0:2], byteorder="little")
        self.level = digimon_data[2]
        self.species = Species(digimon_data[3])
        self.hp = int.from_bytes(digimon_data[4:6], byteorder="little")
        self._padding_0x6 = int.from_bytes(digimon_data[6:8], byteorder="little")
        self.mp = int.from_bytes(digimon_data[8:0xa], byteorder="little")
        self.attack = int.from_bytes(digimon_data[0xa:0xc], byteorder="little")
        self.defense = int.from_bytes(digimon_data[0xc:0xe], byteorder="little")
        self.spirit = int.from_bytes(digimon_data[0xe:0x10], byteorder="little")
        self.speed = int.from_bytes(digimon_data[0x10:0x12], byteorder="little")
        self.evasion = int.from_bytes(digimon_data[0x12:0x14], byteorder="little")
        self.light_res = int.from_bytes(digimon_data[0x14:0x16], byteorder="little")
        self.dark_res = int.from_bytes(digimon_data[0x16:0x18], byteorder="little")
        self.fire_res = int.from_bytes(digimon_data[0x18:0x1a], byteorder="little")
        self.earth_res = int.from_bytes(digimon_data[0x1a:0x1c], byteorder="little")
        self.wind_res = int.from_bytes(digimon_data[0x1c:0x1e], byteorder="little")
        self.steel_res = int.from_bytes(digimon_data[0x1e:0x20], byteorder="little")
        self.water_res = int.from_bytes(digimon_data[0x20:0x22], byteorder="little")
        self.thunder_res = int.from_bytes(digimon_data[0x22:0x24], byteorder="little")
        self.unknown_0x24 = int.from_bytes(digimon_data[0x24:0x26], byteorder="little")
        self.trait_1 = int.from_bytes(digimon_data[0x26:0x28], byteorder="little")
        self.trait_2 = int.from_bytes(digimon_data[0x28:0x2a], byteorder="little")
        self.trait_3 = int.from_bytes(digimon_data[0x2a:0x2c], byteorder="little")
        self.trait_4 = int.from_bytes(digimon_data[0x2c:0x2e], byteorder="little")
        self.move_signature = int.from_bytes(digimon_data[0x2e:0x30], byteorder="little")
        self.move_1 = int.from_bytes(digimon_data[0x30:0x32], byteorder="little")
        self.move_2 = int.from_bytes(digimon_data[0x32:0x34], byteorder="little")
        self.move_3 = int.from_bytes(digimon_data[0x34:0x36], byteorder="little")
        self.move_4 = int.from_bytes(digimon_data[0x36:0x38], byteorder="little")
        self.usage_weight_signature = digimon_data[0x38]
        self.usage_weight_move1 = digimon_data[0x39]
        self.usage_weight_move2 = digimon_data[0x3a]
        self.usage_weight_move3 = digimon_data[0x3b]
        self.holy_exp = int.from_bytes(digimon_data[0x3c:0x40], byteorder="little")
        self.dark_exp = int.from_bytes(digimon_data[0x40:0x44], byteorder="little")
        self.dragon_exp = int.from_bytes(digimon_data[0x44:0x48], byteorder="little")
        self.beast_exp = int.from_bytes(digimon_data[0x48:0x4c], byteorder="little")
        self.bird_exp = int.from_bytes(digimon_data[0x4c:0x50], byteorder="little")
        self.machine_exp = int.from_bytes(digimon_data[0x50:0x54], byteorder="little")
        self.aquan_exp = int.from_bytes(digimon_data[0x54:0x58], byteorder="little")
        self.insectplant_exp = int.from_bytes(digimon_data[0x58:0x5c], byteorder="little")
        self.unknown_0x5C = int.from_bytes(digimon_data[0x5c:0x60], byteorder="little")
        self.unknown_0x60 = int.from_bytes(digimon_data[0x60:0x64], byteorder="little")
        self.unknown_0x64 = int.from_bytes(digimon_data[0x64:0x68], byteorder="little")
        self.unknown_0x68 = int.from_bytes(digimon_data[0x68:0x6c], byteorder="little")

    def getTotalExp(self) -> int:
        return (self.holy_exp + self.dark_exp + self.dragon_exp + self.beast_exp
                + self.bird_exp + self.machine_exp + self.aquan_exp + self.insectplant_exp)

    def updateExpYield(self, exp_yield: int):
        for attr in ["holy_exp", "dark_exp", "dragon_exp", "beast_exp",
                     "bird_exp", "machine_exp", "aquan_exp", "insectplant_exp"]:
            if getattr(self, attr) > 0:
                setattr(self, attr, exp_yield)

    def setResistanceValues(self, resistance_array: List[int]):
        if len(resistance_array) != 8:
            raise ValueError(f"setResistanceValues expects 8 values, got {len(resistance_array)}")
        attrs = ["light_res", "dark_res", "fire_res", "earth_res",
                 "wind_res", "steel_res", "water_res", "thunder_res"]
        for attr, val in zip(attrs, resistance_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def setRegularMoves(self, move_array: List[int]):
        for attr, val in zip(["move_1", "move_2", "move_3", "move_4"], move_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def setRegularTraits(self, trait_array: List[int]):
        for attr, val in zip(["trait_1", "trait_2", "trait_3", "trait_4"], trait_array):
            if val == -1:
                continue
            setattr(self, attr, val)

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2] = self.level
        out[3] = self.species.value
        out[4:6] = self.hp.to_bytes(2, byteorder="little")
        out[6:8] = self._padding_0x6.to_bytes(2, byteorder="little")
        out[8:0xa] = self.mp.to_bytes(2, byteorder="little")
        out[0xa:0xc] = self.attack.to_bytes(2, byteorder="little")
        out[0xc:0xe] = self.defense.to_bytes(2, byteorder="little")
        out[0xe:0x10] = self.spirit.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.speed.to_bytes(2, byteorder="little")
        out[0x12:0x14] = self.evasion.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.light_res.to_bytes(2, byteorder="little")
        out[0x16:0x18] = self.dark_res.to_bytes(2, byteorder="little")
        out[0x18:0x1a] = self.fire_res.to_bytes(2, byteorder="little")
        out[0x1a:0x1c] = self.earth_res.to_bytes(2, byteorder="little")
        out[0x1c:0x1e] = self.wind_res.to_bytes(2, byteorder="little")
        out[0x1e:0x20] = self.steel_res.to_bytes(2, byteorder="little")
        out[0x20:0x22] = self.water_res.to_bytes(2, byteorder="little")
        out[0x22:0x24] = self.thunder_res.to_bytes(2, byteorder="little")
        out[0x24:0x26] = self.unknown_0x24.to_bytes(2, byteorder="little")
        out[0x26:0x28] = self.trait_1.to_bytes(2, byteorder="little")
        out[0x28:0x2a] = self.trait_2.to_bytes(2, byteorder="little")
        out[0x2a:0x2c] = self.trait_3.to_bytes(2, byteorder="little")
        out[0x2c:0x2e] = self.trait_4.to_bytes(2, byteorder="little")
        out[0x2e:0x30] = self.move_signature.to_bytes(2, byteorder="little")
        out[0x30:0x32] = self.move_1.to_bytes(2, byteorder="little")
        out[0x32:0x34] = self.move_2.to_bytes(2, byteorder="little")
        out[0x34:0x36] = self.move_3.to_bytes(2, byteorder="little")
        out[0x36:0x38] = self.move_4.to_bytes(2, byteorder="little")
        out[0x38] = self.usage_weight_signature
        out[0x39] = self.usage_weight_move1
        out[0x3a] = self.usage_weight_move2
        out[0x3b] = self.usage_weight_move3
        out[0x3c:0x40] = self.holy_exp.to_bytes(4, byteorder="little")
        out[0x40:0x44] = self.dark_exp.to_bytes(4, byteorder="little")
        out[0x44:0x48] = self.dragon_exp.to_bytes(4, byteorder="little")
        out[0x48:0x4c] = self.beast_exp.to_bytes(4, byteorder="little")
        out[0x4c:0x50] = self.bird_exp.to_bytes(4, byteorder="little")
        out[0x50:0x54] = self.machine_exp.to_bytes(4, byteorder="little")
        out[0x54:0x58] = self.aquan_exp.to_bytes(4, byteorder="little")
        out[0x58:0x5c] = self.insectplant_exp.to_bytes(4, byteorder="little")
        out[0x5c:0x60] = self.unknown_0x5C.to_bytes(4, byteorder="little")
        out[0x60:0x64] = self.unknown_0x60.to_bytes(4, byteorder="little")
        out[0x64:0x68] = self.unknown_0x64.to_bytes(4, byteorder="little")
        out[0x68:0x6c] = self.unknown_0x68.to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class FarmTerrain:
    SIZE = 0x5c

    # field layout: most slots are still uncharacterized, kept as unknown_*
    _UNKNOWN_FIELDS = [
        (0x02, "unknown_0x2"),
        (0x06, "unknown_0x6"),
        (0x08, "unknown_0x8"),
        (0x0A, "unknown_0xA"),
        (0x0C, "unknown_0xC"),
        (0x0E, "unknown_0xE"),
        (0x10, "unknown_0x10"),
        (0x12, "unknown_0x12"),
        (0x14, "unknown_0x14"),
        (0x16, "unknown_0x16"),
        (0x18, "unknown_0x18"),
        (0x1A, "unknown_0x1A"),
        (0x1C, "unknown_0x1C"),
        (0x1E, "unknown_0x1E"),
        (0x20, "unknown_0x20"),
        (0x22, "unknown_0x22"),
        (0x24, "unknown_0x24"),
        (0x26, "unknown_0x26"),
        (0x28, "unknown_0x28"),
        (0x2A, "unknown_0x2A"),
        (0x2C, "unknown_0x2C"),
        (0x2E, "unknown_0x2E"),
        (0x30, "unknown_0x30"),
        (0x32, "unknown_0x32"),
        (0x34, "unknown_0x34"),
        (0x36, "unknown_0x36"),
        (0x38, "unknown_0x38"),
        (0x3A, "unknown_0x3A"),
        (0x3C, "unknown_0x3C"),
        (0x3E, "unknown_0x3E"),
        (0x40, "unknown_0x40"),
        (0x42, "unknown_0x42"),
        (0x44, "unknown_0x44"),
        (0x46, "unknown_0x46"),
        (0x48, "unknown_0x48"),
        (0x4A, "unknown_0x4A"),
    ]

    def __init__(self, digimon_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(digimon_data[0:2], byteorder="little")
        self.farm_digimon_limit = int.from_bytes(digimon_data[4:6], byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            setattr(self, attr, int.from_bytes(digimon_data[field_offset:field_offset + 2], byteorder="little"))
        self.holy_exp = int.from_bytes(digimon_data[0x4C:0x4E], byteorder="little")
        self.dark_exp = int.from_bytes(digimon_data[0x4E:0x50], byteorder="little")
        self.dragon_exp = int.from_bytes(digimon_data[0x50:0x52], byteorder="little")
        self.beast_exp = int.from_bytes(digimon_data[0x52:0x54], byteorder="little")
        self.bird_exp = int.from_bytes(digimon_data[0x54:0x56], byteorder="little")
        self.machine_exp = int.from_bytes(digimon_data[0x56:0x58], byteorder="little")
        self.aquan_exp = int.from_bytes(digimon_data[0x58:0x5A], byteorder="little")
        self.insectplant_exp = int.from_bytes(digimon_data[0x5A:0x5c], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[4:6] = self.farm_digimon_limit.to_bytes(2, byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            out[field_offset:field_offset + 2] = getattr(self, attr).to_bytes(2, byteorder="little")
        out[0x4C:0x4E] = self.holy_exp.to_bytes(2, byteorder="little")
        out[0x4E:0x50] = self.dark_exp.to_bytes(2, byteorder="little")
        out[0x50:0x52] = self.dragon_exp.to_bytes(2, byteorder="little")
        out[0x52:0x54] = self.beast_exp.to_bytes(2, byteorder="little")
        out[0x54:0x56] = self.bird_exp.to_bytes(2, byteorder="little")
        out[0x56:0x58] = self.machine_exp.to_bytes(2, byteorder="little")
        out[0x58:0x5A] = self.aquan_exp.to_bytes(2, byteorder="little")
        out[0x5A:0x5c] = self.insectplant_exp.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class QuestData:
    SIZE = 0x44

    offset: int
    unknown_0x0: int
    quest_stars: int
    species_center: int
    unknown_0x6: int
    unknown_0x8: int
    pointer_steps_1: int
    pointer_steps_2: int
    pointer_steps_3: int
    pointer_steps_4: int
    money_reward: int
    item_reward: int
    tamerpoints_reward: int
    questflag_1: int
    questflag_2: int
    questflag_3: int
    questflag_4: int
    questflag_5: int
    questgiver_name_pointer: int
    unknown_0x36: int
    unlock_condition_numquests: int
    unlock_condition_questflag: int
    unlock_condition_tamerpoints: int
    unlock_condition_online: int
    unlock_condition_personality: int
    unknown_0x42: int

    def __init__(self, quest_data: bytearray, offset: int):
        self.offset = offset
        self.unknown_0x0 = int.from_bytes(quest_data[0:4], byteorder="little")
        self.quest_stars = quest_data[4]
        self.species_center = quest_data[5]
        self.unknown_0x6 = int.from_bytes(quest_data[6:8], byteorder="little")
        self.unknown_0x8 = int.from_bytes(quest_data[8:0xc], byteorder="little")
        self.pointer_steps_1 = int.from_bytes(quest_data[0xc:0x10], byteorder="little")
        self.pointer_steps_2 = int.from_bytes(quest_data[0x10:0x14], byteorder="little")
        self.pointer_steps_3 = int.from_bytes(quest_data[0x14:0x18], byteorder="little")
        self.pointer_steps_4 = int.from_bytes(quest_data[0x18:0x1c], byteorder="little")
        self.money_reward = int.from_bytes(quest_data[0x1c:0x20], byteorder="little")
        self.item_reward = int.from_bytes(quest_data[0x20:0x24], byteorder="little")
        self.tamerpoints_reward = int.from_bytes(quest_data[0x24:0x28], byteorder="little")
        self.questflag_1 = int.from_bytes(quest_data[0x28:0x2a], byteorder="little")
        self.questflag_2 = int.from_bytes(quest_data[0x2a:0x2c], byteorder="little")
        self.questflag_3 = int.from_bytes(quest_data[0x2c:0x2e], byteorder="little")
        self.questflag_4 = int.from_bytes(quest_data[0x2e:0x30], byteorder="little")
        self.questflag_5 = int.from_bytes(quest_data[0x30:0x32], byteorder="little")
        self.questgiver_name_pointer = int.from_bytes(quest_data[0x32:0x36], byteorder="little")
        self.unknown_0x36 = int.from_bytes(quest_data[0x36:0x38], byteorder="little")
        self.unlock_condition_numquests = int.from_bytes(quest_data[0x38:0x3a], byteorder="little")
        self.unlock_condition_questflag = int.from_bytes(quest_data[0x3a:0x3c], byteorder="little")
        self.unlock_condition_tamerpoints = int.from_bytes(quest_data[0x3c:0x3e], byteorder="little")
        self.unlock_condition_online = int.from_bytes(quest_data[0x3e:0x40], byteorder="little")
        self.unlock_condition_personality = int.from_bytes(quest_data[0x40:0x42], byteorder="little")
        self.unknown_0x42 = int.from_bytes(quest_data[0x42:0x44], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:4] = self.unknown_0x0.to_bytes(4, byteorder="little")
        out[4] = self.quest_stars
        out[5] = self.species_center
        out[6:8] = self.unknown_0x6.to_bytes(2, byteorder="little")
        out[8:0xc] = self.unknown_0x8.to_bytes(4, byteorder="little")
        out[0xc:0x10] = self.pointer_steps_1.to_bytes(4, byteorder="little")
        out[0x10:0x14] = self.pointer_steps_2.to_bytes(4, byteorder="little")
        out[0x14:0x18] = self.pointer_steps_3.to_bytes(4, byteorder="little")
        out[0x18:0x1c] = self.pointer_steps_4.to_bytes(4, byteorder="little")
        out[0x1c:0x20] = self.money_reward.to_bytes(4, byteorder="little")
        out[0x20:0x24] = self.item_reward.to_bytes(4, byteorder="little")
        out[0x24:0x28] = self.tamerpoints_reward.to_bytes(4, byteorder="little")
        out[0x28:0x2a] = self.questflag_1.to_bytes(2, byteorder="little")
        out[0x2a:0x2c] = self.questflag_2.to_bytes(2, byteorder="little")
        out[0x2c:0x2e] = self.questflag_3.to_bytes(2, byteorder="little")
        out[0x2e:0x30] = self.questflag_4.to_bytes(2, byteorder="little")
        out[0x30:0x32] = self.questflag_5.to_bytes(2, byteorder="little")
        out[0x32:0x36] = self.questgiver_name_pointer.to_bytes(4, byteorder="little")
        out[0x36:0x38] = self.unknown_0x36.to_bytes(2, byteorder="little")
        out[0x38:0x3a] = self.unlock_condition_numquests.to_bytes(2, byteorder="little")
        out[0x3a:0x3c] = self.unlock_condition_questflag.to_bytes(2, byteorder="little")
        out[0x3c:0x3e] = self.unlock_condition_tamerpoints.to_bytes(2, byteorder="little")
        out[0x3e:0x40] = self.unlock_condition_online.to_bytes(2, byteorder="little")
        out[0x40:0x42] = self.unlock_condition_personality.to_bytes(2, byteorder="little")
        out[0x42:0x44] = self.unknown_0x42.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class EncounterRewardTable:
    # variable-length: 4 bytes per (probability, reward) pair
    offset: int
    probabilitiesArray: List[int]
    rewardsArray: List[int]

    def __init__(self, reward_data: bytearray, offset: int):
        self.offset = offset
        self.probabilitiesArray = []
        self.rewardsArray = []
        for byte_ix in range(0, len(reward_data), 4):
            self.probabilitiesArray.append(int.from_bytes(reward_data[byte_ix:byte_ix + 2], byteorder="little"))
            self.rewardsArray.append(int.from_bytes(reward_data[byte_ix + 2:byte_ix + 4], byteorder="little"))

    @property
    def size(self) -> int:
        return 4 * len(self.probabilitiesArray)

    def multiplyMoney(self, multiplier: int = 4):
        for ix, reward in enumerate(self.rewardsArray):
            # values > 0x8000 encode money (two's complement-style); items are below 0x8000
            if reward > 0x8000:
                calc_money = 0x10000 - reward
                calc_money *= multiplier
                self.rewardsArray[ix] = 0x10000 - calc_money

    def getByteArray(self) -> bytearray:
        out = bytearray()
        for prob, reward in zip(self.probabilitiesArray, self.rewardsArray):
            out.extend(prob.to_bytes(2, byteorder="little"))
            out.extend(reward.to_bytes(2, byteorder="little"))
        return out

    # kept for compatibility with DWDDRandomizer
    def getByteRepresentation(self) -> bytearray:
        return self.getByteArray()

    def writeToRom(self, rom_data: bytearray):
        data = self.getByteArray()
        rom_data[self.offset:self.offset + len(data)] = data

    def getRewardReprString(self) -> str:
        str_repr = []
        for reward_ix, reward_value in enumerate(self.rewardsArray):
            if self.probabilitiesArray[reward_ix] == 0:
                continue
            if reward_value > 0x8000:
                calc_money = min(0x10000 - reward_value, 0x8001)
                str_repr.append(f"{calc_money} bit")
            else:
                item_name = constants.ITEM_ID_TO_STR.get(reward_value, "Unknown Item")
                str_repr.append(item_name)
        return str(str_repr)


class StandardDigivolution:
    SIZE = 0x70

    offset: int
    digimon_id: int

    # ROM fields, declared in storage order
    degen_evo_id: int
    evolution_1_id: int
    evolution_2_id: int
    evolution_3_id: int
    degen_condition_id_1: int
    degen_condition_value_1: int
    degen_condition_id_2: int
    degen_condition_value_2: int
    degen_condition_id_3: int
    degen_condition_value_3: int
    evo_1_condition_id_1: int
    evo_1_condition_value_1: int
    evo_1_condition_id_2: int
    evo_1_condition_value_2: int
    evo_1_condition_id_3: int
    evo_1_condition_value_3: int
    evo_2_condition_id_1: int
    evo_2_condition_value_1: int
    evo_2_condition_id_2: int
    evo_2_condition_value_2: int
    evo_2_condition_id_3: int
    evo_2_condition_value_3: int
    evo_3_condition_id_1: int
    evo_3_condition_value_1: int
    evo_3_condition_id_2: int
    evo_3_condition_value_2: int
    evo_3_condition_id_3: int
    evo_3_condition_value_3: int

    _FIELD_ORDER = [
        "degen_evo_id", "evolution_1_id", "evolution_2_id", "evolution_3_id",
        "degen_condition_id_1", "degen_condition_value_1",
        "degen_condition_id_2", "degen_condition_value_2",
        "degen_condition_id_3", "degen_condition_value_3",
        "evo_1_condition_id_1", "evo_1_condition_value_1",
        "evo_1_condition_id_2", "evo_1_condition_value_2",
        "evo_1_condition_id_3", "evo_1_condition_value_3",
        "evo_2_condition_id_1", "evo_2_condition_value_1",
        "evo_2_condition_id_2", "evo_2_condition_value_2",
        "evo_2_condition_id_3", "evo_2_condition_value_3",
        "evo_3_condition_id_1", "evo_3_condition_value_1",
        "evo_3_condition_id_2", "evo_3_condition_value_2",
        "evo_3_condition_id_3", "evo_3_condition_value_3",
    ]

    def __init__(self, digivolution_data: bytearray, offset: int, digimon_id: int):
        self.offset = offset
        self.digimon_id = digimon_id
        for i, field in enumerate(self._FIELD_ORDER):
            setattr(self, field, int.from_bytes(digivolution_data[i * 4:(i + 1) * 4], byteorder="little"))

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        for i, field in enumerate(self._FIELD_ORDER):
            out[i * 4:(i + 1) * 4] = getattr(self, field).to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class ArmorDigivolution:
    SIZE = 0x2c

    offset: int
    digimon_id: int
    item_id: int
    evolution_id: int
    condition_id_1: int
    condition_value_1: int
    condition_id_2: int
    condition_value_2: int
    condition_id_3: int
    condition_value_3: int
    degen_condition_id: int
    degen_condition_value: int

    _FIELD_ORDER = [
        "digimon_id", "item_id", "evolution_id",
        "condition_id_1", "condition_value_1",
        "condition_id_2", "condition_value_2",
        "condition_id_3", "condition_value_3",
        "degen_condition_id", "degen_condition_value",
    ]

    def __init__(self, digivolution_data: bytearray, offset: int):
        self.offset = offset
        for i, field in enumerate(self._FIELD_ORDER):
            setattr(self, field, int.from_bytes(digivolution_data[i * 4:(i + 1) * 4], byteorder="little"))

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        for i, field in enumerate(self._FIELD_ORDER):
            out[i * 4:(i + 1) * 4] = getattr(self, field).to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class DNADigivolution:
    SIZE = 0x24

    offset: int
    digimon_1_id: int
    digimon_2_id: int
    dna_evolution_id: int
    condition_id_1: int
    condition_value_1: int
    condition_id_2: int
    condition_value_2: int
    condition_id_3: int
    condition_value_3: int

    _FIELD_ORDER = [
        "digimon_1_id", "digimon_2_id", "dna_evolution_id",
        "condition_id_1", "condition_value_1",
        "condition_id_2", "condition_value_2",
        "condition_id_3", "condition_value_3",
    ]

    def __init__(self, digivolution_data: bytearray, offset: int):
        self.offset = offset
        for i, field in enumerate(self._FIELD_ORDER):
            setattr(self, field, int.from_bytes(digivolution_data[i * 4:(i + 1) * 4], byteorder="little"))

    def removeRequirements(self):
        self.condition_id_1 = 0x1   # condition -> level
        self.condition_value_1 = 0x1
        self.condition_id_2 = 0x0
        self.condition_value_2 = 0x0
        self.condition_id_3 = 0x0
        self.condition_value_3 = 0x0

    def getConditionsArray(self) -> List[List[int]]:
        return [
            [self.condition_id_1, self.condition_value_1],
            [self.condition_id_2, self.condition_value_2],
            [self.condition_id_3, self.condition_value_3],
        ]

    def setConditionsFromArray(self, conditionsArray: List[Tuple[int, int]]):
        self.condition_id_1 = conditionsArray[0][0]
        self.condition_value_1 = conditionsArray[0][1]
        self.condition_id_2 = conditionsArray[1][0]
        self.condition_value_2 = conditionsArray[1][1]
        self.condition_id_3 = conditionsArray[2][0]
        self.condition_value_3 = conditionsArray[2][1]

    def getDnaDigivolutionLog(self) -> str:
        digimon_1_str = constants.DIGIMON_ID_TO_STR[self.digimon_1_id]
        digimon_2_str = constants.DIGIMON_ID_TO_STR[self.digimon_2_id]
        dna_digivolution_str = constants.DIGIMON_ID_TO_STR[self.dna_evolution_id]
        cur_str = f"{dna_digivolution_str} = {digimon_1_str} + {digimon_2_str} ["
        condition_strs = []
        for condition_pair in self.getConditionsArray():
            if condition_pair[0] != 0:
                cur_condition_str = constants.DIGIVOLUTION_CONDITIONS[condition_pair[0]]
                condition_strs.append(f"{cur_condition_str} {condition_pair[1]}")
        cur_str += ", ".join(condition_strs) + "]"
        return cur_str

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        for i, field in enumerate(self._FIELD_ORDER):
            out[i * 4:(i + 1) * 4] = getattr(self, field).to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()

    # kept for compatibility with DWDDRandomizer
    def writeDnaDigivolutionToRom(self, rom_data: bytearray):
        self.writeToRom(rom_data)


class StarterEntry:
    SIZE = 8

    offset: int
    digimon_id: int
    level: int
    screen_x: int
    screen_y: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.digimon_id = int.from_bytes(data[0:2], byteorder="little")
        self.level = int.from_bytes(data[2:4], byteorder="little")
        self.screen_x = int.from_bytes(data[4:6], byteorder="little")
        self.screen_y = int.from_bytes(data[6:8], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.digimon_id.to_bytes(2, byteorder="little")
        out[2:4] = self.level.to_bytes(2, byteorder="little")
        out[4:6] = self.screen_x.to_bytes(2, byteorder="little")
        out[6:8] = self.screen_y.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class WildEncounter:
    """A single 24-byte wild-encounter record inside a wild-encounter area.

    The interior bytes (filler 0x0C80 repetitions, the `unknown_0x12` field that
    crashes the game when set to wrong values, the trailing 0xFFFF terminator)
    aren't yet fully reverse-engineered, so we preserve the original 24 bytes
    raw and overlay only the editable fields on serialize.
    """
    SIZE = 0x18

    offset: int
    digimon_id: int
    reward_slot: int  # offset 0x14 — selects which encounter-reward table to roll
    _raw: bytearray

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self._raw = bytearray(data[:self.SIZE])
        self.digimon_id = int.from_bytes(self._raw[0:2], byteorder="little")
        self.reward_slot = int.from_bytes(self._raw[0x14:0x16], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self._raw)
        out[0:2] = self.digimon_id.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.reward_slot.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class WildEncounterArea:
    """A 0x200-byte wild-encounter region.

    Layout: 16-byte header (num_encounters, rate_lower, rate_upper, 10 bytes of
    filler) followed by up to ~20 WildEncounter records terminated by a record
    whose digimon_id == 0. Bytes after the terminator are filler / unused but
    preserved verbatim for round-trip equality.
    """
    SIZE = 0x200
    HEADER_SIZE = 0x10

    offset: int
    num_encounters: int
    rate_lower: int
    rate_upper: int
    encounters: List["WildEncounter"]
    _raw: bytearray

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self._raw = bytearray(data[:self.SIZE])
        self.num_encounters = int.from_bytes(self._raw[0:2], byteorder="little")
        self.rate_lower = int.from_bytes(self._raw[2:4], byteorder="little")
        self.rate_upper = int.from_bytes(self._raw[4:6], byteorder="little")

        self.encounters = []
        cur = self.HEADER_SIZE
        while cur + WildEncounter.SIZE <= self.SIZE:
            dig_id = int.from_bytes(self._raw[cur:cur + 2], byteorder="little")
            if dig_id == 0:
                break
            self.encounters.append(
                WildEncounter(self._raw[cur:cur + WildEncounter.SIZE], offset + cur)
            )
            cur += WildEncounter.SIZE

    def getByteArray(self) -> bytearray:
        out = bytearray(self._raw)
        out[0:2] = self.num_encounters.to_bytes(2, byteorder="little")
        out[2:4] = self.rate_lower.to_bytes(2, byteorder="little")
        out[4:6] = self.rate_upper.to_bytes(2, byteorder="little")
        for enc in self.encounters:
            local = enc.offset - self.offset
            out[local:local + WildEncounter.SIZE] = enc.getByteArray()
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class Equipment:
    """One 0x48-byte equipment record from /eq/.

    Field layout is the documented one from research_docs/eq_folder_hex.txt.
    Bytes 0x08-0x16 are zero on every vanilla equipment record but the field
    semantics are unknown (the research doc speculates they may be repurposed
    for consumables); they're preserved as named 2-byte unknowns so they
    round-trip exactly even if a hacked ROM populates them.
    """
    SIZE = 0x48

    offset: int
    id: int
    lvl_condition: int
    species_condition: int
    bit_cost: int
    unknown_0x08: int
    unknown_0x0a: int
    unknown_0x0c: int
    unknown_0x0e: int
    unknown_0x10: int
    unknown_0x12: int
    unknown_0x14: int
    atk_boost: int
    defense_boost: int
    spirit_boost: int
    speed_boost: int
    light_res_boost: int
    fire_res_boost: int
    water_res_boost: int
    wind_res_boost: int
    dark_res_boost: int
    earth_res_boost: int
    steel_res_boost: int
    thunder_res_boost: int
    poison_res: int
    confusion_res: int
    death_res: int
    paralysis_res: int
    sleep_res: int
    accuracy_boost: int
    dodge_boost: int
    critical_boost: int
    flee_rate_boost: int
    dmg_boost: int
    money_boost: int
    exp_boost: int
    eos_bytes: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(data[0:2], byteorder="little")
        self.lvl_condition = data[2]
        self.species_condition = data[3]
        self.bit_cost = int.from_bytes(data[4:8], byteorder="little")
        self.unknown_0x08 = int.from_bytes(data[0x08:0x0a], byteorder="little")
        self.unknown_0x0a = int.from_bytes(data[0x0a:0x0c], byteorder="little")
        self.unknown_0x0c = int.from_bytes(data[0x0c:0x0e], byteorder="little")
        self.unknown_0x0e = int.from_bytes(data[0x0e:0x10], byteorder="little")
        self.unknown_0x10 = int.from_bytes(data[0x10:0x12], byteorder="little")
        self.unknown_0x12 = int.from_bytes(data[0x12:0x14], byteorder="little")
        self.unknown_0x14 = int.from_bytes(data[0x14:0x16], byteorder="little")
        self.atk_boost = int.from_bytes(data[0x16:0x18], byteorder="little")
        self.defense_boost = int.from_bytes(data[0x18:0x1a], byteorder="little")
        self.spirit_boost = int.from_bytes(data[0x1a:0x1c], byteorder="little")
        self.speed_boost = int.from_bytes(data[0x1c:0x1e], byteorder="little")
        self.light_res_boost = int.from_bytes(data[0x1e:0x20], byteorder="little")
        self.fire_res_boost = int.from_bytes(data[0x20:0x22], byteorder="little")
        self.water_res_boost = int.from_bytes(data[0x22:0x24], byteorder="little")
        self.wind_res_boost = int.from_bytes(data[0x24:0x26], byteorder="little")
        self.dark_res_boost = int.from_bytes(data[0x26:0x28], byteorder="little")
        self.earth_res_boost = int.from_bytes(data[0x28:0x2a], byteorder="little")
        self.steel_res_boost = int.from_bytes(data[0x2a:0x2c], byteorder="little")
        self.thunder_res_boost = int.from_bytes(data[0x2c:0x2e], byteorder="little")
        self.poison_res = int.from_bytes(data[0x2e:0x30], byteorder="little")
        self.confusion_res = int.from_bytes(data[0x30:0x32], byteorder="little")
        self.death_res = int.from_bytes(data[0x32:0x34], byteorder="little")
        self.paralysis_res = int.from_bytes(data[0x34:0x36], byteorder="little")
        self.sleep_res = int.from_bytes(data[0x36:0x38], byteorder="little")
        self.accuracy_boost = int.from_bytes(data[0x38:0x3a], byteorder="little")
        self.dodge_boost = int.from_bytes(data[0x3a:0x3c], byteorder="little")
        self.critical_boost = int.from_bytes(data[0x3c:0x3e], byteorder="little")
        self.flee_rate_boost = int.from_bytes(data[0x3e:0x40], byteorder="little")
        self.dmg_boost = int.from_bytes(data[0x40:0x42], byteorder="little")
        self.money_boost = int.from_bytes(data[0x42:0x44], byteorder="little")
        self.exp_boost = int.from_bytes(data[0x44:0x46], byteorder="little")
        self.eos_bytes = int.from_bytes(data[0x46:0x48], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2] = self.lvl_condition
        out[3] = self.species_condition
        out[4:8] = self.bit_cost.to_bytes(4, byteorder="little")
        out[0x08:0x0a] = self.unknown_0x08.to_bytes(2, byteorder="little")
        out[0x0a:0x0c] = self.unknown_0x0a.to_bytes(2, byteorder="little")
        out[0x0c:0x0e] = self.unknown_0x0c.to_bytes(2, byteorder="little")
        out[0x0e:0x10] = self.unknown_0x0e.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.unknown_0x10.to_bytes(2, byteorder="little")
        out[0x12:0x14] = self.unknown_0x12.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.unknown_0x14.to_bytes(2, byteorder="little")
        out[0x16:0x18] = self.atk_boost.to_bytes(2, byteorder="little")
        out[0x18:0x1a] = self.defense_boost.to_bytes(2, byteorder="little")
        out[0x1a:0x1c] = self.spirit_boost.to_bytes(2, byteorder="little")
        out[0x1c:0x1e] = self.speed_boost.to_bytes(2, byteorder="little")
        out[0x1e:0x20] = self.light_res_boost.to_bytes(2, byteorder="little")
        out[0x20:0x22] = self.fire_res_boost.to_bytes(2, byteorder="little")
        out[0x22:0x24] = self.water_res_boost.to_bytes(2, byteorder="little")
        out[0x24:0x26] = self.wind_res_boost.to_bytes(2, byteorder="little")
        out[0x26:0x28] = self.dark_res_boost.to_bytes(2, byteorder="little")
        out[0x28:0x2a] = self.earth_res_boost.to_bytes(2, byteorder="little")
        out[0x2a:0x2c] = self.steel_res_boost.to_bytes(2, byteorder="little")
        out[0x2c:0x2e] = self.thunder_res_boost.to_bytes(2, byteorder="little")
        out[0x2e:0x30] = self.poison_res.to_bytes(2, byteorder="little")
        out[0x30:0x32] = self.confusion_res.to_bytes(2, byteorder="little")
        out[0x32:0x34] = self.death_res.to_bytes(2, byteorder="little")
        out[0x34:0x36] = self.paralysis_res.to_bytes(2, byteorder="little")
        out[0x36:0x38] = self.sleep_res.to_bytes(2, byteorder="little")
        out[0x38:0x3a] = self.accuracy_boost.to_bytes(2, byteorder="little")
        out[0x3a:0x3c] = self.dodge_boost.to_bytes(2, byteorder="little")
        out[0x3c:0x3e] = self.critical_boost.to_bytes(2, byteorder="little")
        out[0x3e:0x40] = self.flee_rate_boost.to_bytes(2, byteorder="little")
        out[0x40:0x42] = self.dmg_boost.to_bytes(2, byteorder="little")
        out[0x42:0x44] = self.money_boost.to_bytes(2, byteorder="little")
        out[0x44:0x46] = self.exp_boost.to_bytes(2, byteorder="little")
        out[0x46:0x48] = self.eos_bytes.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class Consumable:
    """One 0x14-byte consumable record.

    Layout inferred by cross-referencing item names with field values (e.g. the
    `effect_value` field literally encodes the 150/300/600 in the Digiar item
    names; bit_cost varies with potency). `flags` carries an end-of-sequence /
    targeting word whose semantics aren't fully reverse-engineered, so the
    field is preserved as a single 4-byte integer.
    """
    SIZE = 0x14

    offset: int
    id: int
    consumable_marker: int
    bit_cost: int  # u16 at 0x04..0x06
    unknown_0x06: int  # u16 at 0x06..0x08 — pending research
    primary_effect_id: int
    secondary_effect_id: int
    effect_value: int
    flags: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(data[0:2], byteorder="little")
        self.consumable_marker = int.from_bytes(data[2:4], byteorder="little")
        self.bit_cost = int.from_bytes(data[4:6], byteorder="little")
        self.unknown_0x06 = int.from_bytes(data[6:8], byteorder="little")
        self.primary_effect_id = int.from_bytes(data[8:0xa], byteorder="little")
        self.secondary_effect_id = int.from_bytes(data[0xa:0xc], byteorder="little")
        self.effect_value = int.from_bytes(data[0xc:0x10], byteorder="little")
        self.flags = int.from_bytes(data[0x10:0x14], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2:4] = self.consumable_marker.to_bytes(2, byteorder="little")
        out[4:6] = self.bit_cost.to_bytes(2, byteorder="little")
        out[6:8] = self.unknown_0x06.to_bytes(2, byteorder="little")
        out[8:0xa] = self.primary_effect_id.to_bytes(2, byteorder="little")
        out[0xa:0xc] = self.secondary_effect_id.to_bytes(2, byteorder="little")
        out[0xc:0x10] = self.effect_value.to_bytes(4, byteorder="little")
        out[0x10:0x14] = self.flags.to_bytes(4, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class FarmItem:
    """One 0x30-byte farm-item record.

    Only id (0x00), rank (0x02), max_points (0x10), and bit_cost (0x1C) are
    documented in research_docs/farm_items_research.txt. The remaining 2-byte
    slots are preserved through `_UNKNOWN_FIELDS` so the record round-trips
    even though those fields aren't exposed in the editor UI for editing
    individually (they're listed under an "advanced" group there).
    """
    SIZE = 0x30

    _UNKNOWN_FIELDS = [
        (0x04, "unknown_0x04"),
        (0x06, "unknown_0x06"),
        (0x08, "unknown_0x08"),
        (0x0A, "unknown_0x0a"),
        (0x0C, "unknown_0x0c"),
        (0x0E, "unknown_0x0e"),
        (0x12, "unknown_0x12"),
        (0x14, "unknown_0x14"),
        (0x16, "unknown_0x16"),
        (0x18, "unknown_0x18"),
        (0x1A, "unknown_0x1a"),
        (0x20, "unknown_0x20"),
        (0x22, "unknown_0x22"),
        (0x24, "unknown_0x24"),
        (0x26, "unknown_0x26"),
        (0x28, "unknown_0x28"),
        (0x2A, "unknown_0x2a"),
        (0x2C, "unknown_0x2c"),
        (0x2E, "unknown_0x2e"),
    ]

    offset: int
    id: int
    rank: int
    max_points: int
    bit_cost: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(data[0:2], byteorder="little")
        self.rank = int.from_bytes(data[2:4], byteorder="little")
        self.max_points = int.from_bytes(data[0x10:0x12], byteorder="little")
        self.bit_cost = int.from_bytes(data[0x1C:0x20], byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            setattr(self, attr, int.from_bytes(data[field_offset:field_offset + 2], byteorder="little"))

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2:4] = self.rank.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.max_points.to_bytes(2, byteorder="little")
        out[0x1C:0x20] = self.bit_cost.to_bytes(4, byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            out[field_offset:field_offset + 2] = getattr(self, attr).to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()
