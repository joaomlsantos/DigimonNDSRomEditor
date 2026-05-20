"""RomSession — owns a loaded ROM and the parsed model graph.

A session is created via `RomSession.from_file(path)`. It parses every known
data table into in-memory model objects (using `digimon_core.loaders`). The UI
mutates those model objects directly (typically through QUndoCommand
subclasses); `serialize_all()` writes every model back into a fresh copy of the
original ROM bytes and `save()` persists that to disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from digimon_core import loaders, model, rom


@dataclass
class RomSession:
    source_path: str
    version: str
    original_rom_data: bytes  # immutable snapshot used as serialization base
    dirty: bool = False

    # parsed model collections
    base_digimon: Dict[int, model.BaseDataDigimon] = field(default_factory=dict)
    enemy_digimon: Dict[int, model.EnemyDataDigimon] = field(default_factory=dict)
    moves: List[model.MoveData] = field(default_factory=list)
    quests: List[model.QuestData] = field(default_factory=list)
    encounter_rewards: List[model.EncounterRewardTable] = field(default_factory=list)
    standard_digivolutions: Dict[int, model.StandardDigivolution] = field(default_factory=dict)
    armor_digivolutions: List[model.ArmorDigivolution] = field(default_factory=list)
    dna_digivolutions: List[model.DNADigivolution] = field(default_factory=list)
    sprite_map: List[model.SpriteMapEntry] = field(default_factory=list)
    battle_strings: List[model.BattleStringEntry] = field(default_factory=list)
    habitats_worldmap: List[model.HabitatWorldmap] = field(default_factory=list)
    farm_terrains: List[model.FarmTerrain] = field(default_factory=list)
    starters: List[model.StarterEntry] = field(default_factory=list)
    wild_encounter_areas: List[model.WildEncounterArea] = field(default_factory=list)
    equipment: Dict[int, model.Equipment] = field(default_factory=dict)
    consumables: List[model.Consumable] = field(default_factory=list)
    farm_items: List[model.FarmItem] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str) -> "RomSession":
        rom_data = rom.loadRom(path)
        version = rom.detectVersion(rom_data, path)

        session = cls(
            source_path=path,
            version=version,
            original_rom_data=bytes(rom_data),
        )
        session.base_digimon = loaders.loadBaseDigimonInfo(version, rom_data)
        session.enemy_digimon = loaders.loadEnemyDigimonInfo(version, rom_data)
        session.moves = loaders.loadMoveData(version, rom_data)
        session.quests = loaders.loadQuestData(version, rom_data)
        session.encounter_rewards = loaders.loadEncounterRewardData(version, rom_data)
        session.standard_digivolutions = loaders.loadStandardDigivolutions(version, rom_data)
        session.armor_digivolutions = loaders.loadArmorDigivolutions(version, rom_data)
        session.dna_digivolutions, _ = loaders.loadDnaDigivolutions(version, rom_data)
        session.sprite_map = loaders.loadSpriteMapTable(version, rom_data)
        session.battle_strings = loaders.loadBattleStringTable(version, rom_data)
        session.habitats_worldmap = loaders.loadHabitatsWorldmap(version, rom_data)
        session.farm_terrains = loaders.loadFarmTerrains(version, rom_data)
        session.starters = loaders.loadStarters(version, rom_data)
        session.wild_encounter_areas = loaders.loadWildEncounterAreas(version, rom_data)
        session.equipment = loaders.loadEquipment(version, rom_data)
        session.consumables = loaders.loadConsumables(version, rom_data)
        session.farm_items = loaders.loadFarmItems(version, rom_data)
        return session

    def serialize_all(self) -> bytearray:
        """Write every model back onto a copy of the original ROM bytes."""
        out = bytearray(self.original_rom_data)
        for obj in self.base_digimon.values():
            obj.writeToRom(out)
        for obj in self.enemy_digimon.values():
            obj.writeToRom(out)
        for obj in self.moves:
            obj.writeToRom(out)
        for obj in self.quests:
            obj.writeToRom(out)
        for obj in self.encounter_rewards:
            obj.writeToRom(out)
        for obj in self.standard_digivolutions.values():
            obj.writeToRom(out)
        for obj in self.armor_digivolutions:
            obj.writeToRom(out)
        for obj in self.dna_digivolutions:
            obj.writeToRom(out)
        for obj in self.sprite_map:
            obj.writeToRom(out)
        for obj in self.battle_strings:
            obj.writeToRom(out)
        for obj in self.habitats_worldmap:
            obj.writeToRom(out)
        for obj in self.farm_terrains:
            obj.writeToRom(out)
        for obj in self.starters:
            obj.writeToRom(out)
        for area in self.wild_encounter_areas:
            area.writeToRom(out)
        for obj in self.equipment.values():
            obj.writeToRom(out)
        for obj in self.consumables:
            obj.writeToRom(out)
        for obj in self.farm_items:
            obj.writeToRom(out)
        return out

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.source_path
        rom.writeRom(self.serialize_all(), target)
        self.source_path = target
        self.dirty = False
        return target
