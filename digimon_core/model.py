from enum import Enum
from typing import List, Optional, Tuple

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

    # The 4-byte ``upperscreen_sprites`` field packs two 16-bit sprite refs
    # (low half at bytes 0xc–0xd, high half at bytes 0xe–0xf). Editors can
    # bind directly to either half via these proxy properties — getattr/
    # setattr drive ``SetAttrCommand`` undo correctly, and serialization
    # still reads the 32-bit composite.
    @property
    def upperscreen_low(self) -> int:
        return self.upperscreen_sprites & 0xFFFF

    @upperscreen_low.setter
    def upperscreen_low(self, value: int) -> None:
        self.upperscreen_sprites = (self.upperscreen_sprites & 0xFFFF0000) | (int(value) & 0xFFFF)

    @property
    def upperscreen_high(self) -> int:
        return (self.upperscreen_sprites >> 16) & 0xFFFF

    @upperscreen_high.setter
    def upperscreen_high(self, value: int) -> None:
        self.upperscreen_sprites = (self.upperscreen_sprites & 0x0000FFFF) | ((int(value) & 0xFFFF) << 16)


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


# Empirical per-string cap inside MSG.PAK: the DWDD textbox renderer
# corrupts when a single dialogue entry exceeds 1024 bytes (encoded text +
# its FE FF / FF FF terminator), regardless of content. Vanilla strings
# top out at ~1004 bytes — never crossed. Applied as the budget ceiling
# for any MSG.PAK string in the editor's save guard and budget meter.
MSGPAK_STRING_CAP: int = 1024


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

    def encoded_bytes_for_grow(self) -> bytes:
        """Encoded text + original terminator, no NUL padding.

        For use by paths that resize the string's container (e.g. the
        MSG.PAK grow path in §12) — those don't need or want budget
        padding, and they must preserve the original group terminator
        (FF FF / FE FF) so the engine still sees correct group boundaries
        after a rebuild. ``_resolved_terminator`` is intentionally bypassed:
        its FE-on-shrink rule is meant for the fixed-slot writer, where
        the trailing gap inside the slot needs an in-engine stop.
        """
        return _strings.encode_string(self.text, terminator=self.original_terminator)

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
    # Verified in the ov2 battle executor FUN_0017cdbc — the old u16 "unknown"
    # pair at 0x14/0x16 is really four independent bytes. (The actual damage
    # power is primary_value at +8; +0x15 is the to-hit term, not damage.)
    status_strength: int  # +0x14 u8 — base status-infliction value (trait-modified)
    accuracy: int         # +0x15 u8 — to-hit contributor: rand(1000) < (this+offense)*(1000-def)/1000
    crit_rate: int        # +0x16 u8 — +50%-damage chance (rand(100) < attacker_mod + this)
    flinch_chance: int    # +0x17 u8 — flinch/turn-skip chance (rand(100) < this - target_resist)
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
        self.status_strength = move_data[0x14]
        self.accuracy = move_data[0x15]
        self.crit_rate = move_data[0x16]
        self.flinch_chance = move_data[0x17]
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
        out[0x14] = self.status_strength & 0xFF
        out[0x15] = self.accuracy & 0xFF
        out[0x16] = self.crit_rate & 0xFF
        out[0x17] = self.flinch_chance & 0xFF
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
    element_affinity: int      # +0x26 element/STAB affinity bitmask (bit0 Light .. bit7 Thunder)
    unknown_0x27: int          # +0x27 second element-shaped mask; battle code reads only +0x26
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
        # +0x26 is the element-affinity mask read by the STAB check in the
        # battle damage code (ov2 FUN_0017cdbc → species accessor FUN_00036bb8:
        # `mask & (1 << move_element)` grants ×1.15). +0x27 is a separate
        # element-shaped mask whose consumer is not yet identified.
        self.element_affinity = digimon_data[0x26]
        self.unknown_0x27 = digimon_data[0x27]
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
        out[0x26] = self.element_affinity
        out[0x27] = self.unknown_0x27
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
    element_affinity: int      # +0x24 element affinity, SAME elements as base +0x26 but a
    unknown_0x25: int          # different bit order (see __init__); +0x25 always 0 in vanilla
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
        # +0x24 is the enemy table's copy of the element-affinity mask. It encodes
        # the SAME elements as the base record's +0x26 mask, but in a DIFFERENT bit
        # order — base is Light,Dark,Fire,Earth,Wind,Steel,Water,Thunder while the
        # enemy table is Light,Fire,Water,Wind,Dark,Earth,Steel,Thunder (verified:
        # enemy == permute(base) for all 398 named digimon). Combat STAB reads the
        # base copy via FUN_00036bb8(species_id), not this field. Traced the enemy
        # accessor FUN_00036c4c + all 11 call sites (battler build + scene setup):
        # none read +0x24, so this copy appears vestigial. +0x25 is always 0.
        self.element_affinity = digimon_data[0x24]
        self.unknown_0x25 = digimon_data[0x25]
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
        out[0x24] = self.element_affinity
        out[0x25] = self.unknown_0x25
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
    """One 0x5c-byte farm-terrain record (17 total). Layout verified against the
    arm9 accessors + overlay-6 consumers — see
    ``research_docs/claude_notes/arm9_data_table_readers.md``. Every byte is
    accounted for:

    - 0x00 id (u16)
    - 0x02 ``secondary_id`` (s16, == id-370 in vanilla) — a sequential
      per-terrain resource index cached into the farm-scene object at init;
      the exact table it indexes (name / background / layout) is unconfirmed.
    - 0x04 ``farm_digimon_limit`` — how many digimon this terrain holds
      (`FUN_000d10e4`; the "can add digimon?" gate).
    - 0x06..0x25 ``digimon{0..7}_x/_y`` — 8 on-screen placement coords for the
      farm digimon (`FUN_000d10fc`; only the first ``farm_digimon_limit`` used).
    - 0x26 ``farm_item_limit`` — how many decoration items this terrain holds.
    - 0x28..0x47 ``item{0..7}_x/_y`` — 8 placement coords for farm decorations.
    - 0x48/0x4A ``anchor_x/anchor_y`` — a single {x,y} home point (cursor/avatar).
    - 0x4C..0x5B — 8 per-attribute EXP values.
    """
    SIZE = 0x5c
    POSITION_COUNT = 8

    # Named 2-byte fields for offsets 0x02..0x4B (id / limit / positions /
    # anchor). EXP block (0x4C..) is handled explicitly below.
    _FIELDS = (
        [(0x02, "secondary_id"), (0x04, "farm_digimon_limit")]
        + [pair for i in range(POSITION_COUNT)
           for pair in ((0x06 + i * 4, f"digimon{i}_x"),
                        (0x08 + i * 4, f"digimon{i}_y"))]
        + [(0x26, "farm_item_limit")]
        + [pair for i in range(POSITION_COUNT)
           for pair in ((0x28 + i * 4, f"item{i}_x"),
                        (0x2A + i * 4, f"item{i}_y"))]
        + [(0x48, "anchor_x"), (0x4A, "anchor_y")]
    )

    def __init__(self, digimon_data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(digimon_data[0:2], byteorder="little")
        for field_offset, attr in self._FIELDS:
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
        for field_offset, attr in self._FIELDS:
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

    Field layout (verified against arm9 ``FUN_00058500``, the spawn selector —
    see ``research_docs/claude_notes/wild_encounter_format.md``):
    - 0x00 u16  digimon_id
    - 0x02..0x10  placement coords (``0x0C80`` == wildcard / random position;
      a non-``0x0C80`` value pins a fixed on-map roaming encounter). Preserved
      raw — not yet split into named x/y pairs.
    - 0x12 u16  ``spawn_chance`` — per-slot appearance chance, rolled each
      battle against ``rand(100)`` (higher = more likely). Vanilla values are
      multiples of 10 in 10..100.
    - 0x14 u16  ``reward_slot`` — selects which encounter-reward table to roll.
    - 0x16 u16  extra per-record param (0..0xFFFF; ``0xFFFF`` == none). Purpose
      not yet pinned; preserved raw.

    Coordinate slots and the 0x16 param stay in ``_raw`` and round-trip
    untouched; only the named fields are overlaid on serialize.
    """
    SIZE = 0x18

    offset: int
    digimon_id: int
    spawn_chance: int  # offset 0x12 — appearance chance %, rolled vs rand(100)
    reward_slot: int  # offset 0x14 — selects which encounter-reward table to roll
    _raw: bytearray

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self._raw = bytearray(data[:self.SIZE])
        self.digimon_id = int.from_bytes(self._raw[0:2], byteorder="little")
        self.spawn_chance = int.from_bytes(self._raw[0x12:0x14], byteorder="little")
        self.reward_slot = int.from_bytes(self._raw[0x14:0x16], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self._raw)
        out[0:2] = self.digimon_id.to_bytes(2, byteorder="little")
        out[0x12:0x14] = self.spawn_chance.to_bytes(2, byteorder="little")
        out[0x14:0x16] = self.reward_slot.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class WildEncounterArea:
    """A wild-encounter region — one per area.

    Layout: 16-byte header (num_encounters, rate_lower, rate_upper, 10 bytes of
    filler) followed by up to ~20 WildEncounter records terminated by a record
    whose digimon_id == 0. Bytes after the terminator are filler / unused but
    preserved verbatim for round-trip equality.

    `SIZE` is the legacy 0x200-padded slab size used by the hardcoded-offset
    loader (`AREA_ENCOUNTER_OFFSETS`-driven walk). Under FNT-driven loading
    each area is its own self-contained file with no padding, so the instance
    width comes from `len(_raw)` set at construction. The legacy slab and the
    FNT-trimmed file produce equivalent parsed state — the trimmed file
    excludes only 0xFFFF padding bytes the loader/writer never touched.
    """
    SIZE = 0x200
    HEADER_SIZE = 0x10
    # Engine cap: arm9 FUN_00058500 gathers candidates into a fixed 16-entry
    # stack buffer (frame 0x1bc, array at sp+0xbc stride 0x10). num_encounters
    # >= 17 overruns the frame -> stack smash -> crash. Vanilla max is 14.
    MAX_ENCOUNTERS = 16
    # FNT area files terminate the record list with a 4-byte `00 00 00 00`
    # (digimon_id == 0). No trailing 0xFFFF padding in the trimmed file.
    TERMINATOR = b"\x00\x00\x00\x00"

    offset: int
    original_size: int
    num_encounters: int
    rate_lower: int
    rate_upper: int
    encounters: List["WildEncounter"]
    _raw: bytearray

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self._raw = bytearray(data)
        # Vanilla FNT file size, used to detect an add/remove-slot resize that
        # can no longer ride the in-place ``writeToRom`` path (the session's
        # wild-encounter FAT splice handles those instead).
        self.original_size = len(self._raw)
        self._reparse()

    def _reparse(self) -> None:
        """Rebuild the header fields + encounter list from ``_raw``."""
        self.num_encounters = int.from_bytes(self._raw[0:2], byteorder="little")
        self.rate_lower = int.from_bytes(self._raw[2:4], byteorder="little")
        self.rate_upper = int.from_bytes(self._raw[4:6], byteorder="little")

        self.encounters = []
        cur = self.HEADER_SIZE
        while cur + WildEncounter.SIZE <= len(self._raw):
            dig_id = int.from_bytes(self._raw[cur:cur + 2], byteorder="little")
            if dig_id == 0:
                break
            self.encounters.append(
                WildEncounter(self._raw[cur:cur + WildEncounter.SIZE], self.offset + cur)
            )
            cur += WildEncounter.SIZE

    @property
    def is_resized(self) -> bool:
        """True once an add/remove-slot changed the file's byte length."""
        return len(self._raw) != self.original_size

    def can_add_encounter(self) -> bool:
        return len(self.encounters) < self.MAX_ENCOUNTERS

    def _default_record(self) -> bytes:
        """A plain random encounter (all-wildcard coords). Seeds digimon_id +
        reward_slot from the last existing record so the new slot lands in the
        area's level/reward range; the user retargets it in the editor."""
        rec = bytearray(WildEncounter.SIZE)
        for off in range(0x02, 0x12, 2):  # coord slots -> 0x0C80 wildcard
            rec[off:off + 2] = (0x0C80).to_bytes(2, byteorder="little")
        seed = self.encounters[-1] if self.encounters else None
        digimon_id = seed.digimon_id if seed else 1
        reward_slot = seed.reward_slot if seed else 0
        rec[0:2] = (digimon_id & 0xFFFF).to_bytes(2, byteorder="little")
        rec[0x12:0x14] = (30).to_bytes(2, byteorder="little")  # spawn_chance %
        rec[0x14:0x16] = (reward_slot & 0xFFFF).to_bytes(2, byteorder="little")
        rec[0x16:0x18] = (0xFFFF).to_bytes(2, byteorder="little")
        return bytes(rec)

    def _insert(self, index: int, enc: "WildEncounter") -> None:
        """Splice ``enc`` into the record list at ``index``, preserving the
        identity of every other encounter object (their ``offset`` shifts but
        the instances are the same — undo commands may hold references)."""
        index = max(0, min(index, len(self.encounters)))
        # Bake any pending field/rate edits into _raw before mutating length.
        self._raw = self.getByteArray()
        pos = self.HEADER_SIZE + index * WildEncounter.SIZE
        self._raw[pos:pos] = enc.getByteArray()
        for existing in self.encounters[index:]:
            existing.offset += WildEncounter.SIZE
        enc.offset = self.offset + pos
        self.encounters.insert(index, enc)
        self.num_encounters = len(self.encounters)
        self._raw[0:2] = self.num_encounters.to_bytes(2, byteorder="little")

    def _remove(self, index: int) -> "WildEncounter":
        enc = self.encounters[index]
        self._raw = self.getByteArray()
        pos = self.HEADER_SIZE + index * WildEncounter.SIZE
        del self._raw[pos:pos + WildEncounter.SIZE]
        del self.encounters[index]
        for existing in self.encounters[index:]:
            existing.offset -= WildEncounter.SIZE
        self.num_encounters = len(self.encounters)
        self._raw[0:2] = self.num_encounters.to_bytes(2, byteorder="little")
        return enc

    def add_encounter(
        self, record_bytes: bytes = None, index: int = None,
    ) -> "WildEncounter":
        """Create + insert a new encounter and return the object. Raises
        ValueError at the engine cap."""
        if not self.can_add_encounter():
            raise ValueError(
                f"area is at the {self.MAX_ENCOUNTERS}-encounter engine cap"
            )
        rec = bytes(record_bytes) if record_bytes is not None else self._default_record()
        if len(rec) != WildEncounter.SIZE:
            raise ValueError(f"encounter record must be {WildEncounter.SIZE} bytes")
        if index is None:
            index = len(self.encounters)
        enc = WildEncounter(rec, self.offset)  # real offset set in _insert
        self._insert(index, enc)
        return enc

    def insert_encounter(self, index: int, enc: "WildEncounter") -> None:
        """Re-insert an existing encounter object (undo/redo path — keeps the
        same instance so its bound undo commands stay valid)."""
        if not self.can_add_encounter():
            raise ValueError(
                f"area is at the {self.MAX_ENCOUNTERS}-encounter engine cap"
            )
        self._insert(index, enc)

    def remove_encounter(self, index: int) -> "WildEncounter":
        """Remove the encounter at ``index`` and return the object (so an undo
        command can re-insert the same instance)."""
        if not (0 <= index < len(self.encounters)):
            raise IndexError(f"encounter index {index} out of range")
        return self._remove(index)

    def replace_raw(self, new_bytes: bytes) -> None:
        """Install ``new_bytes`` as the file body and reparse (keeps
        ``original_size`` so ``is_resized`` still reflects the vanilla delta).
        Used by the .romproj wild-encounter-area edit channel on load."""
        self._raw = bytearray(new_bytes)
        self._reparse()

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
        data = self.getByteArray()
        rom_data[self.offset:self.offset + len(data)] = data


class MapEncounterEntry:
    """One 8-byte entry in ``DAT/ec/ENCTBL.BIN`` — the per-field-map
    encounter assignment table.

    The table has 265 entries (field maps 0..264, same id space as the
    overlay5 map entries) plus a trailing all-``FFFF`` terminator; entry
    index == field-map id. Full decode + verification in
    ``research_docs/claude_notes/map_encounter_table.md``.

    Layout (4 × u16 LE):
    - 0x00 ``area_index`` — wild-encounter area (index into the
      ``WildEncounterArea`` list; which digimon spawn). ``0`` / ``1`` are
      Shine/Dark-side dummies used by towns; ``0xFFFF`` = no area.
    - 0x02 ``battle_bg`` — battle background id (``DAT/btmap/<id>``);
      ``0xFFFF`` = none.
    - 0x04 ``unknown_0x4`` — per-map category (values 1..8, 10; no 9). NOT
      the encounter rate — that lives in the ``WildEncounterArea`` header's
      rate bounds, read by arm9 ``FUN_0013f6e4``. Clusters by map
      role/region: 3 = boss/special battle rooms (every btmap-49 boss
      arena), 1/2 = Shine/Dark hubs, 4/5 = Shine/Dark routes. Exact runtime
      use unconfirmed. Kept raw + editable.
    - 0x06 ``wild_battle_bgm`` — BGM played during a wild encounter on this
      map (music id, same id space as the overlay5 SET_MUSIC opcode / the
      Sound editor's BGM list). Vanilla uses only 0x10 Normal Battle Theme,
      0x11 Alt Battle Theme (the "Sunken Tunnel"-family maps), 0x12 Chaos
      Brain Battle Theme.
    """
    SIZE = 8
    NONE = 0xFFFF

    offset: int
    map_id: int
    area_index: int
    battle_bg: int
    unknown_0x4: int
    wild_battle_bgm: int

    def __init__(self, data: bytearray, offset: int, map_id: int = -1):
        self.offset = offset
        self.map_id = map_id
        self.area_index = int.from_bytes(data[0:2], byteorder="little")
        self.battle_bg = int.from_bytes(data[2:4], byteorder="little")
        self.unknown_0x4 = int.from_bytes(data[4:6], byteorder="little")
        self.wild_battle_bgm = int.from_bytes(data[6:8], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = (self.area_index & 0xFFFF).to_bytes(2, byteorder="little")
        out[2:4] = (self.battle_bg & 0xFFFF).to_bytes(2, byteorder="little")
        out[4:6] = (self.unknown_0x4 & 0xFFFF).to_bytes(2, byteorder="little")
        out[6:8] = (self.wild_battle_bgm & 0xFFFF).to_bytes(2, byteorder="little")
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


class TraitData:
    """One 8-byte trait record: ``[index:u16, kind:u16, effect_type:u8,
    value_mode:u8, magnitude:u16]``.

    Indexed 1:1 with ``constants.TRAIT_ARRAY_STR``. ``index`` is the trait's own
    slot (== position in the table). ``kind`` is the stacking mode read by the
    battle aggregator (``FUN_00058f0c``): 0 = take the max among matching traits,
    nonzero = sum them (every vanilla trait is 1 = additive). ``effect_type``
    (byte ``+4``) selects the effect (see ``constants.TRAIT_EFFECT_TYPE_NAMES``).
    ``value_mode`` (byte ``+5``, read by ``FUN_00058e24``) picks how ``magnitude``
    applies: 0 = flat, 1 = percent of the base stat/damage (``base*mag/100``).
    Earlier code read ``+4..+5`` as one u16, so percent traits surfaced as
    ``0x1xx`` (e.g. Flame Aura ``0x127`` = type ``0x27`` + percent flag).
    """
    SIZE = 0x8
    FLAT, PERCENT = 0, 1

    offset: int
    index: int
    kind: int
    effect_type: int
    value_mode: int
    magnitude: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.index = int.from_bytes(data[0:2], byteorder="little")
        self.kind = int.from_bytes(data[2:4], byteorder="little")
        self.effect_type = data[4]
        self.value_mode = data[5]
        self.magnitude = int.from_bytes(data[6:8], byteorder="little")

    @property
    def is_percent(self) -> bool:
        return self.value_mode != self.FLAT

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.index.to_bytes(2, byteorder="little")
        out[2:4] = self.kind.to_bytes(2, byteorder="little")
        out[4] = self.effect_type & 0xFF
        out[5] = self.value_mode & 0xFF
        out[6:8] = self.magnitude.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class FarmItem:
    """One 0x30-byte farm-item record.

    Layout mirrors `FarmTrainingPen` but shifted: a farm good points at a
    training pen (0x04) and carries its own outcome value block (0x08-0x0E)
    plus the overworld placement (sprite 0x18, x/y relative to the pen
    centre at 0x28/0x2A).

    Documented offsets:
    - 0x00 u16  id
    - 0x02 u8   data_size (per-item byte, correlates loosely with rank)
    - 0x03 u8   rank ordinal (0=S, 1=A, 2=B, 3=C, 4=D)
    - 0x04 u16  training_pen_id (index into the 48 FarmTrainingPen records)
    - 0x06 u8   stat_id (which stat the good raises)
    - 0x07 u8   unknown_0x07 (secondary byte alongside stat_id; purpose TBD)
    - 0x08 s16  great_failure_value  (vanilla 0xFFFF = -1)
    - 0x0A s16  failure_value
    - 0x0C s16  success_value
    - 0x0E s16  great_success_value
    - 0x10 u16  max_points
    - 0x14 u8   great_failure_chance   } four outcome odds; no stored total,
    - 0x15 u8   failure_chance         } they just sum to 100 (0x64) in vanilla
    - 0x16 u8   success_chance         }
    - 0x17 u8   great_success_chance   }
    - 0x18 u16  sprite_id (overworld sprite; id↔sprite mapping still TBD)
    - 0x1C u32  bit_cost
    - 0x28 s16  x_position (relative to the pen centre)
    - 0x2A s16  y_position (relative to the pen centre)

    Unlike FarmTrainingPen there's no `total_odds` slot — the four chances are
    assumed to sum to 100. The remaining still-undecoded bytes stay in
    `_UNKNOWN_FIELDS` so they round-trip untouched.
    """
    SIZE = 0x30

    _UNKNOWN_FIELDS = [
        (0x12, "unknown_0x12"),
        (0x1A, "unknown_0x1a"),
        (0x20, "unknown_0x20"),
        (0x22, "unknown_0x22"),
        (0x24, "unknown_0x24"),
        (0x26, "unknown_0x26"),
        (0x2C, "unknown_0x2c"),
        (0x2E, "unknown_0x2e"),
    ]

    offset: int
    id: int
    data_size: int
    rank: int
    training_pen_id: int
    stat_id: int               # u8
    unknown_0x07: int          # u8
    great_failure_value: int   # s16
    failure_value: int         # s16
    success_value: int         # s16
    great_success_value: int   # s16
    max_points: int
    great_failure_chance: int  # u8
    failure_chance: int        # u8
    success_chance: int        # u8
    great_success_chance: int  # u8
    sprite_id: int
    bit_cost: int
    x_position: int            # s16
    y_position: int            # s16

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.id = int.from_bytes(data[0:2], byteorder="little")
        self.data_size = data[2]
        self.rank = data[3]
        self.training_pen_id     = int.from_bytes(data[0x04:0x06], byteorder="little")
        self.stat_id             = data[0x06]
        self.unknown_0x07        = data[0x07]
        # Stored as raw u16 even though these are semantically s16 (0xFFFF ==
        # -1): the editor's signed spinboxes handle the two's-complement
        # display, and raw storage keeps serialisation overflow-free — a
        # negative edit round-trips through ``value & 0xFFFF`` == 0..65535,
        # which ``to_bytes(2, signed=True)`` would reject. Mirrors
        # QuestData.unlock_condition_tamerpoints.
        self.great_failure_value = int.from_bytes(data[0x08:0x0A], byteorder="little")
        self.failure_value       = int.from_bytes(data[0x0A:0x0C], byteorder="little")
        self.success_value       = int.from_bytes(data[0x0C:0x0E], byteorder="little")
        self.great_success_value = int.from_bytes(data[0x0E:0x10], byteorder="little")
        self.max_points = int.from_bytes(data[0x10:0x12], byteorder="little")
        self.great_failure_chance = data[0x14]
        self.failure_chance       = data[0x15]
        self.success_chance       = data[0x16]
        self.great_success_chance = data[0x17]
        self.sprite_id           = int.from_bytes(data[0x18:0x1A], byteorder="little")
        self.bit_cost = int.from_bytes(data[0x1C:0x20], byteorder="little")
        self.x_position          = int.from_bytes(data[0x28:0x2A], byteorder="little")
        self.y_position          = int.from_bytes(data[0x2A:0x2C], byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            setattr(self, attr, int.from_bytes(data[field_offset:field_offset + 2], byteorder="little"))

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0:2] = self.id.to_bytes(2, byteorder="little")
        out[2] = self.data_size & 0xFF
        out[3] = self.rank & 0xFF
        out[0x04:0x06] = self.training_pen_id.to_bytes(2, byteorder="little")
        out[0x06] = self.stat_id & 0xFF
        out[0x07] = self.unknown_0x07 & 0xFF
        # `& 0xFFFF` normalises both representations these fields can hold —
        # a signed value straight off a fresh parse, or the raw u16 the
        # signed spinbox writes back on edit — to the same unsigned bytes.
        out[0x08:0x0A] = (self.great_failure_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0A:0x0C] = (self.failure_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0C:0x0E] = (self.success_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0E:0x10] = (self.great_success_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.max_points.to_bytes(2, byteorder="little")
        out[0x14] = self.great_failure_chance & 0xFF
        out[0x15] = self.failure_chance & 0xFF
        out[0x16] = self.success_chance & 0xFF
        out[0x17] = self.great_success_chance & 0xFF
        out[0x18:0x1A] = self.sprite_id.to_bytes(2, byteorder="little")
        out[0x1C:0x20] = self.bit_cost.to_bytes(4, byteorder="little")
        out[0x28:0x2A] = (self.x_position & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x2A:0x2C] = (self.y_position & 0xFFFF).to_bytes(2, byteorder="little")
        for field_offset, attr in self._UNKNOWN_FIELDS:
            out[field_offset:field_offset + 2] = getattr(self, attr).to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()


class FarmTrainingPen:
    """One 0x1C-byte farm-training-pen record.

    Each training pen produces an outcome (great failure / failure / success /
    great success) when a farm good is used; the four chance bytes at 0x12-0x15
    sum to `total_odds` (typically 0x64 = 100). The four stat-delta values
    (great_failure_value..great_success_value) are signed s16 — vanilla weapons
    use 0xFFFF (-1) for `great_failure_value` to subtract a point on a miss.
    """
    SIZE = 0x1C

    offset: int
    string_id: int
    sprite_id: int
    stat_op_id: int
    great_failure_value: int   # s16
    failure_value: int         # s16
    success_value: int         # s16
    great_success_value: int   # s16
    stat_cap: int
    total_odds: int
    great_failure_chance: int  # u8
    failure_chance: int        # u8
    success_chance: int        # u8
    great_success_chance: int  # u8
    animation_id: int
    sound_id: int
    vertical_position: int

    def __init__(self, data: bytearray, offset: int):
        self.offset = offset
        self.string_id           = int.from_bytes(data[0x00:0x02], byteorder="little")
        self.sprite_id           = int.from_bytes(data[0x02:0x04], byteorder="little")
        self.stat_op_id          = int.from_bytes(data[0x04:0x06], byteorder="little")
        # Raw u16 storage for these s16 fields — see FarmItem for the
        # rationale (signed spinbox display + overflow-free serialisation).
        self.great_failure_value = int.from_bytes(data[0x06:0x08], byteorder="little")
        self.failure_value       = int.from_bytes(data[0x08:0x0A], byteorder="little")
        self.success_value       = int.from_bytes(data[0x0A:0x0C], byteorder="little")
        self.great_success_value = int.from_bytes(data[0x0C:0x0E], byteorder="little")
        self.stat_cap            = int.from_bytes(data[0x0E:0x10], byteorder="little")
        self.total_odds          = int.from_bytes(data[0x10:0x12], byteorder="little")
        self.great_failure_chance = data[0x12]
        self.failure_chance       = data[0x13]
        self.success_chance       = data[0x14]
        self.great_success_chance = data[0x15]
        self.animation_id         = int.from_bytes(data[0x16:0x18], byteorder="little")
        self.sound_id             = int.from_bytes(data[0x18:0x1A], byteorder="little")
        self.vertical_position    = int.from_bytes(data[0x1A:0x1C], byteorder="little")

    def getByteArray(self) -> bytearray:
        out = bytearray(self.SIZE)
        out[0x00:0x02] = self.string_id.to_bytes(2, byteorder="little")
        out[0x02:0x04] = self.sprite_id.to_bytes(2, byteorder="little")
        out[0x04:0x06] = self.stat_op_id.to_bytes(2, byteorder="little")
        out[0x06:0x08] = (self.great_failure_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x08:0x0A] = (self.failure_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0A:0x0C] = (self.success_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0C:0x0E] = (self.great_success_value & 0xFFFF).to_bytes(2, byteorder="little")
        out[0x0E:0x10] = self.stat_cap.to_bytes(2, byteorder="little")
        out[0x10:0x12] = self.total_odds.to_bytes(2, byteorder="little")
        out[0x12] = self.great_failure_chance & 0xFF
        out[0x13] = self.failure_chance & 0xFF
        out[0x14] = self.success_chance & 0xFF
        out[0x15] = self.great_success_chance & 0xFF
        out[0x16:0x18] = self.animation_id.to_bytes(2, byteorder="little")
        out[0x18:0x1A] = self.sound_id.to_bytes(2, byteorder="little")
        out[0x1A:0x1C] = self.vertical_position.to_bytes(2, byteorder="little")
        return out

    def writeToRom(self, rom_data: bytearray):
        rom_data[self.offset:self.offset + self.SIZE] = self.getByteArray()
