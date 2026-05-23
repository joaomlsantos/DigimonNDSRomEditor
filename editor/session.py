"""RomSession — owns a loaded ROM and the parsed model graph.

A session is created via `RomSession.from_file(path)`. It parses every known
data table into in-memory model objects (using `digimon_core.loaders`). The UI
mutates those model objects directly (typically through QUndoCommand
subclasses); `serialize_all()` writes every model back into a fresh copy of the
original ROM bytes and `save()` persists that to disk.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from digimon_core import constants, loaders, model, qol as qol_module, rom


@dataclass
class RomSession:
    source_path: Optional[str]
    version: str
    original_rom_data: bytes  # immutable snapshot used as serialization base
    dirty: bool = False
    # Path to the .romproj this session was loaded from, if any. None when
    # the user opened a plain .nds. Tracked separately from source_path so
    # "Save ROM" and "Save Project" are independently routable.
    project_path: Optional[str] = None

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
    # In-game text. Keyed by region_id (see constants.STRING_REGIONS); each
    # entry is a list of GameString in offset order.
    string_regions: Dict[str, List[model.GameString]] = field(default_factory=dict)

    # QoL byte-patches applied at save time, *after* serialize_all(). Defaults
    # to all-off / vanilla parameters — the editor is data-editing-first; QoL
    # is opt-in.
    qol: qol_module.QolSettings = field(default_factory=qol_module.QolSettings)

    @classmethod
    def from_file(cls, path: str) -> "RomSession":
        rom_data = rom.loadRom(path)
        version = rom.detectVersion(rom_data, path)
        return cls._build(
            source_path=path,
            version=version,
            vanilla_data=bytes(rom_data),
            parse_data=rom_data,
        )

    @classmethod
    def from_project(
        cls,
        project_path: str,
        vanilla_path: str,
        vanilla_data: bytes,
        version: str,
        patched_data: bytes,
        qol_settings: qol_module.QolSettings,
    ) -> "RomSession":
        """Build a session from a project file's vanilla + diff payload.

        `vanilla_data` is the immutable serialization base (so future
        serialize_all() writes a fresh diff against the same vanilla). The
        model objects are parsed from `patched_data` so the editor reflects
        the project's edits. `source_path` is left as None — the user must
        explicitly Save As when first exporting the patched ROM.
        """
        session = cls._build(
            source_path=None,
            version=version,
            vanilla_data=vanilla_data,
            parse_data=patched_data,
        )
        session.project_path = project_path
        session.qol = qol_settings
        # vanilla_path isn't stored on the session — the caller (main_window)
        # owns the QSettings cache so projects sharing a vanilla ROM don't
        # each re-prompt the user.
        del vanilla_path
        return session

    @classmethod
    def _build(
        cls,
        source_path: Optional[str],
        version: str,
        vanilla_data: bytes,
        parse_data: bytes,
    ) -> "RomSession":
        """Shared session-population path used by both from_file and
        from_project. `vanilla_data` becomes the serialization baseline;
        `parse_data` is what the loaders read to build the model graph."""
        session = cls(
            source_path=source_path,
            version=version,
            original_rom_data=vanilla_data,
        )
        session.base_digimon = loaders.loadBaseDigimonInfo(version, parse_data)
        session.enemy_digimon = loaders.loadEnemyDigimonInfo(version, parse_data)
        session.moves = loaders.loadMoveData(version, parse_data)
        session.quests = loaders.loadQuestData(version, parse_data)
        session.encounter_rewards = loaders.loadEncounterRewardData(version, parse_data)
        session.standard_digivolutions = loaders.loadStandardDigivolutions(version, parse_data)
        session.armor_digivolutions = loaders.loadArmorDigivolutions(version, parse_data)
        session.dna_digivolutions, _ = loaders.loadDnaDigivolutions(version, parse_data)
        session.sprite_map = loaders.loadSpriteMapTable(version, parse_data)
        session.battle_strings = loaders.loadBattleStringTable(version, parse_data)
        session.habitats_worldmap = loaders.loadHabitatsWorldmap(version, parse_data)
        session.farm_terrains = loaders.loadFarmTerrains(version, parse_data)
        session.starters = loaders.loadStarters(version, parse_data)
        session.wild_encounter_areas = loaders.loadWildEncounterAreas(version, parse_data)
        session.equipment = loaders.loadEquipment(version, parse_data)
        session.consumables = loaders.loadConsumables(version, parse_data)
        session.farm_items = loaders.loadFarmItems(version, parse_data)
        session.string_regions = loaders.loadAllStringRegions(version, parse_data)
        # Seed QoL parameter defaults from the actual bytes at their ARM-imm
        # offsets so the editor displays the current value (vanilla on a fresh
        # ROM, the user's previously-patched value otherwise). from_project()
        # then replaces the whole `qol` field with the project's saved state.
        session.qol.movement_speed = parse_data[constants.MOVEMENT_SPEED_OFFSET[version]]
        session.qol.scan_rate = parse_data[constants.BASE_SCAN_RATE_OFFSET[version]]
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
        for region_strings in self.string_regions.values():
            for s in region_strings:
                s.writeToRom(out)
        return out

    def serialize_all_with_qol(self) -> bytearray:
        """`serialize_all()` plus enabled QoL byte-patches applied on top.

        QoL is applied last so it sits over any model edits — never the other
        way around. Multiplier-style patches read from the post-serialize_all
        bytes, so they scale the user-edited values (not vanilla).
        """
        out = self.serialize_all()
        qol_module.apply_qol_patches(out, self.version, self.qol)
        return out

    def over_budget_strings(self) -> List[model.GameString]:
        """Strings whose encoded length exceeds their original byte budget.

        Pointers to each string's offset are baked into the ROM and aren't
        repointed by the editor, so writing an over-budget encoded string
        would clobber whatever follows it. Save paths gate on this list.
        """
        bad: List[model.GameString] = []
        for region_strings in self.string_regions.values():
            for s in region_strings:
                if not s.fits():
                    bad.append(s)
        return bad

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.source_path
        if target is None:
            raise ValueError("save() called with no path and no source_path set")
        # Single rolling .bak so a botched write (or a save the user regrets)
        # doesn't destroy the previous on-disk copy. Only meaningful when
        # `target` already exists — first-ever Save As to a new path skips it.
        # copy2 preserves metadata; failure to back up is logged but doesn't
        # block the save itself (the user explicitly asked to write).
        if os.path.exists(target):
            try:
                shutil.copy2(target, target + ".bak")
            except OSError:
                pass
        rom.writeRom(self.serialize_all_with_qol(), target)
        self.source_path = target
        self.dirty = False
        return target
