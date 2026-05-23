"""digimon_core — shared parsing/serialization for Digimon World Dawn & Dusk ROMs.

Used by both DWDDRandomizer (the randomizer/QoL patcher) and DigimonNDSRomEditor
(the ROM editor). All model classes round-trip via __init__ ↔ getByteArray().
"""

from . import constants
from . import loaders
from . import model
from . import rom
from . import strings

from .model import (
    ArmorDigivolution,
    BaseDataDigimon,
    BattleStringEntry,
    DNADigivolution,
    DigimonType,
    Element,
    ELEMENTAL_RESISTANCES,
    ELEMENTAL_WEAKNESSES,
    EncounterRewardTable,
    EnemyDataDigimon,
    FarmTerrain,
    GameString,
    HabitatWorldmap,
    ItemType,
    LvlUpMode,
    MoveData,
    QuestData,
    Species,
    SpriteMapEntry,
    StandardDigivolution,
    StarterEntry,
    WildEncounter,
    WildEncounterArea,
)
from .rom import (
    RomFile,
    UnknownRomError,
    UnsupportedRomError,
    detectVersion,
    loadRom,
    writeRom,
)

__all__ = [
    "constants",
    "loaders",
    "model",
    "rom",
    "strings",
    "ArmorDigivolution",
    "BaseDataDigimon",
    "BattleStringEntry",
    "DNADigivolution",
    "DigimonType",
    "Element",
    "ELEMENTAL_RESISTANCES",
    "ELEMENTAL_WEAKNESSES",
    "EncounterRewardTable",
    "EnemyDataDigimon",
    "FarmTerrain",
    "GameString",
    "HabitatWorldmap",
    "ItemType",
    "LvlUpMode",
    "MoveData",
    "QuestData",
    "Species",
    "SpriteMapEntry",
    "StandardDigivolution",
    "StarterEntry",
    "WildEncounter",
    "WildEncounterArea",
    "RomFile",
    "UnknownRomError",
    "UnsupportedRomError",
    "detectVersion",
    "loadRom",
    "writeRom",
]
