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
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from digimon_core import constants, fat, fnt, loaders, model, msgpak, overlay5 as overlay5_mod, overlay5_cutscenes as overlay5_cutscenes_mod, pak, qol as qol_module, rom


# Every sprite pak the editor knows how to splice on save. Used by
# from_project to pre-load any pak that diverges from vanilla (so a
# project's sprite edits survive a re-save even if the user never opens
# the sprite browser) and by _apply_sprite_pak_splice to iterate.
#
# SPR_*: battle/UI sprites (NCGR/NCLR/NCER triplets, project memory
#   ``project_sprite_pak_pair_heuristic`` — pair by index).
# MCHR_*: overworld sprites (custom multi-frame format, no NCGR wrapper;
#   see :mod:`digimon_core.mchr`). Save/load rides the exact same channel.
SPRITE_PAK_PATHS = (
    "DAT/SPR_CHR.PAK", "DAT/SPR_PAL.PAK", "DAT/SPR_CEL.PAK",
    "DAT/MCHR_CHR.PAK", "DAT/MCHR_PAL.PAK",
    "DAT/BTCHR.PAK",
)

# SPR_CHR.PAK entry indices for the 8 elemental icons used by the in-game
# move HUD. Order in the pak is fixed:
#   0xa1 fire · 0xa2 thunder · 0xa3 wind · 0xa4 water
#   0xa5 steel · 0xa6 light · 0xa7 dark · 0xa8 earth
# Keyed by ``model.Element`` enum value so the move editor and the
# base/enemy move-row inline summaries can resolve a sprite without
# touching the icons/portraits/ui directory structure directly.
ELEMENT_SPR_INDEX: Dict[int, int] = {
    2: 0xa1,  # FIRE
    7: 0xa2,  # THUNDER
    4: 0xa3,  # WIND
    6: 0xa4,  # WATER
    5: 0xa5,  # STEEL
    0: 0xa6,  # LIGHT
    1: 0xa7,  # DARK
    3: 0xa8,  # EARTH
}

# BTCHR sidecars: fixed-size 1660B each (415 × u32). Edits ride the byte
# diff channel — no FAT resize needed because per-group writes are u32
# in-place. Resolution happens against the post-sprite-splice ROM in
# :meth:`RomSession._apply_btchr_size_edits` so BTCHR.PAK growth still
# leaves these files findable.
CHRSIZE_PATH = "DAT/BTCHR/CHRSIZE.BIN"
BTCHRSIZE_PATH = "DAT/BTCHR/BTCHRSIZE.BIN"


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

    # Lazy PakFile cache for sprite directories. Parsing all 1627-entry sprite
    # paks at session open would cost ~50ms+ per pak with no payoff for users
    # who never open the sprite browser; populated on first sprite_pak() call.
    _sprite_pak_cache: Dict[str, pak.PakFile] = field(default_factory=dict)
    # Sprite paks with at least one ReplaceSpriteCommand applied. Serialize
    # iterates this on save to splice the rebuilt pak over its FAT slot.
    _dirty_sprite_paks: Set[str] = field(default_factory=set)

    # Lazy file_table built from original_rom_data — shared across read-only
    # browsers that don't need to follow the post-edit FAT layout (btmap
    # Phase B/C reads vanilla bytes only; the FAT-splice path lands in
    # Phase E with its own write-side resolver).
    _file_table_cache: Optional[fnt.FileTable] = field(default=None)

    # Per-FAT-path overrides for btmap edits (Phase D). Path → uncompressed
    # bytes that supersede the vanilla FAT slot. Cleared on session reset;
    # serialize_all (Phase E) will splice these back over the ROM.
    _dirty_btmap_files: Dict[str, bytes] = field(default_factory=dict)

    # Same shape as ``_dirty_btmap_files``, but for ``DAT/map/*`` field-map
    # edits (PLAN.md §14.5 Phase C onwards). The FAT splice lands in
    # Phase F; reads route through :meth:`map_file_bytes` so paint tools
    # see their own edits immediately.
    _dirty_map_files: Dict[str, bytes] = field(default_factory=dict)

    # Per-entry overrides for overlay5 (script overlay) — keyed by
    # entry index, value is the full replacement entry payload. The
    # Events tab on field maps (PLAN.md §14.9) populates this when the
    # user drags an OVERWORLD_SPRITE marker; ``_apply_overlay5_splice``
    # writes each edit back into the overlay file bytes on save.
    # Length is invariant per edit (drag changes x/y only, never the
    # body length), so no FAT resize is needed and the entries below
    # this one in the overlay don't shift.
    _dirty_overlay5_entries: Dict[int, bytes] = field(default_factory=dict)

    # Lazy ``Overlay5Index`` over ``original_rom_data``. Built on first
    # access; shared across the Events tab read path and the splice
    # path so a save sees the same pointer table that the UI dragged
    # against. Cleared via ``invalidate_overlay5_index`` only if the
    # overlay layout ever changes (it doesn't, today).
    _overlay5_index_cache: Optional[overlay5_mod.Overlay5Index] = field(default=None)

    # Lazy cutscene chain index built atop the Overlay5Index. The Cutscenes
    # tab takes O(1) ``chains_by_map[map_id]`` lookups off this; build is
    # eager (single ~1s walk on first access, cached for session lifetime).
    # No invalidation today — chain topology is structural, not editable.
    _cutscene_index_cache: Optional[overlay5_cutscenes_mod.CutsceneIndex] = field(default=None)

    # Per-group edits to BTCHR/CHRSIZE.BIN and BTCHR/BTCHRSIZE.BIN. Both
    # files are fixed-size (1660B = 415 × u32) and never resize, so edits
    # ride the byte diff channel instead of a sprite-style splice: writes
    # happen in-place into ``out`` during serialize_all and the
    # vanilla-vs-edited diff captures them automatically. Keys are group
    # indices; values are the full u32 to store at ``group * 4``.
    _chrsize_edits: Dict[int, int] = field(default_factory=dict)
    _btchrsize_edits: Dict[int, int] = field(default_factory=dict)

    # Sidecar entries for BTCHR groups appended past vanilla 415. Parallel
    # arrays: position k describes vanilla_count + k. The PAK growth itself
    # rides ``_sprite_pak_cache[BTCHR_PAK]`` (count/entries bumped, splice
    # path resizes the FAT slot); these two arrays carry the u32s that
    # have to ride alongside in the chrsize/btchrsize sidecars so the
    # loader sees a consistent triple. Engine extensibility confirmed by
    # in-game test 2026-06-07 (project memory project_btchr_extensible).
    _btchr_appended_chrsize: List[int] = field(default_factory=list)
    _btchr_appended_btchrsize: List[int] = field(default_factory=list)

    # Lazy display-name cache. Built on first access from battle_strings +
    # string_regions["arm9_digiegg_enemy_names"]; invalidated explicitly via
    # invalidate_name_caches() when a battle-string entry or that region's
    # strings change. Editors that surface these labels (base/enemy digimon,
    # MCHR browser, sprite browser) call the resolver on every list-row
    # build, so the cache avoids the per-call dict rebuild.
    _digimon_name_cache: Dict[int, str] = field(default_factory=dict)
    _digimon_name_cache_valid: bool = False

    # msgpak_all index of dialog msg_id 0. Lazy: ``dialog_msg_text``
    # anchor-searches once and caches; ``None`` means uncomputed, ``-1``
    # means anchor missing.
    _dialog_msgpak_base: Optional[int] = None

    # Lazy QIcon cache for the digimon portrait sprite (SPR_CHR entry pointed
    # at by SpriteMapEntry.upperscreen_low). Populated on demand by
    # ``digimon_portrait_icon``; misses (out-of-range id, parse failure,
    # empty render) cache as ``None`` so the parse work isn't redone for
    # every popup repaint. Form-helper combos read through the registry in
    # ``form_helpers.set_details_providers``.
    _digimon_portrait_icon_cache: Dict[int, object] = field(default_factory=dict)

    # Lazy QPixmap cache for the BTCHR battle-sprite preview (cell 0 of the
    # group pointed at by SpriteMapEntry.main_sprite). Built on demand by
    # ``battle_sprite_pixmap``; misses cache as ``None``. Invalidated alongside
    # the sprite label caches whenever BTCHR.PAK is mutated.
    _battle_sprite_pixmap_cache: Dict[int, object] = field(default_factory=dict)

    # Lazy QPixmap caches for the SPR_* and MCHR sprite-map row previews
    # (portrait, battle-mini, party-follower overworld). Keyed by
    # ``(entry_idx, max_size)`` so editors that render at different sizes
    # don't trample each other. Misses cache as ``None`` and are dropped
    # by ``invalidate_sprite_label_caches`` when the underlying pak changes.
    _spr_pixmap_cache: Dict[Tuple[int, int], object] = field(default_factory=dict)
    _mchr_pixmap_cache: Dict[Tuple[int, int], object] = field(default_factory=dict)

    # Lazy label caches for the SPR / MCHR / BTCHR sprite pickers used by
    # SpriteMapRow (Display/Reskin section of base + enemy editors).
    # compute_spr_labels parses 1627 NCGR+NCER entries (~370ms),
    # compute_mchr_labels parses every MCHR group (~220ms), and
    # compute_btchr_group_labels resolves 415+ groups (~50ms). Shared at
    # the session so opening multiple editors only pays the cost once.
    # Invalidated by ``invalidate_sprite_label_caches`` after sprite_map
    # edits (cross-refs change) or sprite-pak splices (pak length / bytes
    # change).
    _spr_labels_cache: Optional[List[str]] = None
    _mchr_labels_cache: Optional[List[str]] = None
    _btchr_group_labels_cache: Optional[List[str]] = None

    # Frozen sprite-idx → owning digimon_id snapshot. Captured from the
    # sprite_map at first access (during the eager label pre-warm in
    # `_build`), then never re-derived — so reassigning a digimon's
    # sprite later doesn't steal the original sprite's label. Editor
    # labels behave as if sourced from a fixed string array. Four maps,
    # one per sprite_map field that addresses a sprite pak.
    _sprite_attribution_snapshot: Optional[Dict[str, Dict[int, int]]] = None

    # Lazy shared QStandardItemModel registry for combo pickers. Each
    # "kind" (moves, traits, spr, mchr, btchr, battle_strings) has one
    # model that every combo of that kind sets via ``setModel``, so the
    # ~50-130ms per-editor addItem cost collapses to a one-shot build at
    # ROM load. QComboBox.currentIndex is per-combo view state, not part
    # of the model, so combos sharing a model can show different
    # selections. Built by ``picker_model``; invalidated via
    # ``invalidate_picker_model`` after edits that change row count or
    # label text. Object-typed to keep the headless ``RomSession``
    # import path Qt-free.
    _picker_models: Dict[str, object] = field(default_factory=dict)

    # Cursor for the idle-tick portrait-icon prewarm chain. Walks the
    # shared "digimon" / "digimon_evo" picker models in ~50-item chunks
    # so the ~1s of total icon decode work spreads across the event loop
    # without blocking the UI. See ``prewarm_digimon_icons``.
    _icon_prewarm_cursor: int = 0

    # Pooled sprite-picker widgets — built once at ROM load, reparented
    # into the active SpriteMapRow on editor open, and detached back to
    # the ``_picker_pool_holder`` on editor teardown. Constructing a
    # _SpriteListPicker pays ~46ms (setEditable creates a QLineEdit,
    # QCompleter wraps a 1627-item model); pooling collapses 4×46ms per
    # editor open to a one-shot ROM-load cost. Only one editor is open
    # at a time, so 4 instances (mchr, btchr, spr, spr) suffice. The
    # holder is a permanently-hidden parent widget so pool members never
    # become top-level windows (which would pop up as stray frames) and
    # never get the "explicitly hidden" flag that setParent(None) sets.
    # Object-typed to keep the headless ``RomSession`` import path Qt-free.
    _pooled_sprite_pickers: Optional[List[object]] = None
    _picker_pool_holder: Optional[object] = None
    # Monotonic ownership token. _build_editor_for constructs the NEW
    # editor *before* set_content tears down the OLD one, so when both
    # editors use SpriteMapRow the old editor's release would otherwise
    # steal the pickers away from the new editor. Each acquire bumps the
    # token; release only reparks to the holder when its captured token
    # still matches — the old editor's stale token no-ops.
    _picker_pool_generation: int = 0

    # Pooled BoundIdCombo / BoundIdComboRow widgets, keyed by pool name
    # ("moves" today; traits / battle_str candidates for later). Same
    # rationale as the sprite-picker pool: each widget pays ~9ms for
    # ``setEditable`` + ``QCompleter`` setup at construction; pooling
    # collapses that to a one-shot ROM-load cost. Per-kind generation
    # tokens so unrelated pools don't interfere with each other's
    # acquire/release races.
    _combo_pools: Dict[str, List[object]] = field(default_factory=dict)
    _combo_pool_generations: Dict[str, int] = field(default_factory=dict)

    # Per-editor last-selected row, keyed by the dispatch key in
    # ``main_window._build_editor_for`` (e.g. "base_digimon", "wild"). Lets
    # editors restore the user's previous cursor when navigated back to
    # within the same session. Values are the editor's natural id (digimon
    # id, area index, equipment id, ...) — each editor knows how to map its
    # own key back to the list row. Not persisted to .romproj; resets on
    # ROM close, by design.
    last_selections: Dict[str, int] = field(default_factory=dict)

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
        # Sprite paks aren't parsed during _build (they're lazy). If the
        # project's byte diff touched any of them, eagerly pre-load from
        # patched_data so sprite_pak() returns the project's state (not
        # vanilla) and the splice path captures them into a re-save's diff
        # even when the user never opens the sprite browser.
        # Each side's FAT is queried separately — an msgpak grow inside
        # the project would have shifted every later file's FAT entry, so
        # vanilla and patched can map this pak to different ROM offsets.
        ft_vanilla = fnt.FileTable.from_rom(vanilla_data)
        ft_patched = fnt.FileTable.from_rom(patched_data)
        for pak_name in SPRITE_PAK_PATHS:
            try:
                v_start, v_end = ft_vanilla.resolve(pak_name)
                p_start, p_end = ft_patched.resolve(pak_name)
            except KeyError:
                continue
            if vanilla_data[v_start:v_end] != patched_data[p_start:p_end]:
                session._sprite_pak_cache[pak_name] = pak.PakFile(patched_data[p_start:p_end])
                session._dirty_sprite_paks.add(pak_name)
        # BTCHR sidecars: scan per-group u32 slots in patched vs vanilla.
        # The byte diff already landed these in patched_data; mirror them
        # into the per-group edit dicts so a re-save writes them back even
        # after we strip the sprite splice from the next diff.
        for path, target in (
            (CHRSIZE_PATH, session._chrsize_edits),
            (BTCHRSIZE_PATH, session._btchrsize_edits),
        ):
            try:
                v_start, v_end = ft_vanilla.resolve(path)
                p_start, p_end = ft_patched.resolve(path)
            except KeyError:
                continue
            v_buf = vanilla_data[v_start:v_end]
            p_buf = patched_data[p_start:p_end]
            n = min(len(v_buf), len(p_buf)) // 4
            for g in range(n):
                v_word = struct.unpack_from("<I", v_buf, g * 4)[0]
                p_word = struct.unpack_from("<I", p_buf, g * 4)[0]
                if v_word != p_word:
                    target[g] = p_word
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
        # Parse FNT/FAT once per session and pass through to every FAT-listed
        # loader (dm/, en/, ec/, eq/, sk/, MSG.PAK). Saves ~6 redundant FAT
        # walks. ARM9 / overlay loaders ignore it.
        file_table = fnt.FileTable.from_rom(bytes(parse_data))
        session.base_digimon = loaders.loadBaseDigimonInfo(version, parse_data, file_table=file_table)
        session.enemy_digimon = loaders.loadEnemyDigimonInfo(version, parse_data, file_table=file_table)
        session.moves = loaders.loadMoveData(version, parse_data)
        session.quests = loaders.loadQuestData(version, parse_data)
        session.encounter_rewards = loaders.loadEncounterRewardData(version, parse_data, file_table=file_table)
        session.standard_digivolutions = loaders.loadStandardDigivolutions(version, parse_data, file_table=file_table)
        session.armor_digivolutions = loaders.loadArmorDigivolutions(version, parse_data)
        session.dna_digivolutions, _ = loaders.loadDnaDigivolutions(version, parse_data)
        session.sprite_map = loaders.loadSpriteMapTable(version, parse_data)
        session.battle_strings = loaders.loadBattleStringTable(version, parse_data)
        session.habitats_worldmap = loaders.loadHabitatsWorldmap(version, parse_data)
        session.farm_terrains = loaders.loadFarmTerrains(version, parse_data)
        session.starters = loaders.loadStarters(version, parse_data)
        session.wild_encounter_areas = loaders.loadWildEncounterAreas(version, parse_data, file_table=file_table)
        session.equipment = loaders.loadEquipment(version, parse_data, file_table=file_table)
        session.consumables = loaders.loadConsumables(version, parse_data)
        session.farm_items = loaders.loadFarmItems(version, parse_data)
        session.string_regions = loaders.loadAllStringRegions(version, parse_data, file_table=file_table)
        # Seed QoL parameter defaults from the actual bytes at their ARM-imm
        # offsets so the editor displays the current value (vanilla on a fresh
        # ROM, the user's previously-patched value otherwise). from_project()
        # then replaces the whole `qol` field with the project's saved state.
        session.qol.movement_speed = parse_data[constants.MOVEMENT_SPEED_OFFSET[version]]
        session.qol.scan_rate = parse_data[constants.BASE_SCAN_RATE_OFFSET[version]]
        # Pre-warm the SPR / MCHR / BTCHR picker labels so the very first
        # base/enemy editor open already shows labeled sprite values
        # instead of bare hex (~700ms one-shot, paid here so editor opens
        # stay snappy and users can read the existing values without
        # toggling Customize).
        session.get_spr_labels()
        session.get_mchr_labels()
        session.get_btchr_group_labels()
        # Pre-warm shared picker models. Front-loading the QStandardItem
        # creation here (one-shot ~150-200ms) saves ~80-130ms per
        # subsequent editor open since each combo just does
        # ``setModel(model)`` instead of N ``addItem`` calls.
        for kind in (
            "moves", "traits_byte", "traits_word",
            "spr", "mchr", "btchr", "battle_strings",
            "digimon", "digimon_evo", "item_reward",
        ):
            session.picker_model(kind)
        # Spread the ~1s of portrait-icon decode work across idle ticks
        # so dropdowns show icons immediately on first open instead of
        # waiting for the showPopup fill loop. No-op in headless tests.
        session.prewarm_digimon_icons()
        # Pre-build the 4 pooled sprite pickers (~185ms one-shot). Front-
        # loaded here so SpriteMapRow.__init__ just reparents instead of
        # paying setEditable + QCompleter setup on every editor open.
        session._build_sprite_picker_pool()
        # Same trick for the moves picker rows (~47ms saved per editor
        # open). Depends on the sprite-picker pool's holder, so order
        # matters — holder is created in _build_sprite_picker_pool.
        session._build_combo_pools()
        return session

    def serialize_all(
        self,
        *,
        skip_sprite_splice: bool = False,
        skip_btmap_splice: bool = False,
        skip_map_splice: bool = False,
        skip_overlay5_splice: bool = False,
    ) -> bytearray:
        """Write every model back onto a copy of the original ROM bytes.

        ``skip_sprite_splice`` leaves sprite paks at their vanilla bytes
        instead of rebuilding them. Used by project save so the byte diff
        captures only equal-length model edits — sprite edits ride the
        :meth:`sprite_pak_edits` channel and are replayed at load time
        (mirrors how MSG.PAK over-budget strings use ``string_edits``).
        Without this flag, a single grown sprite would balloon the byte
        diff to span every downstream-shifted file.
        """
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
        for region_id, region_strings in self.string_regions.items():
            is_msgpak = region_id.startswith("msgpak_")
            for s in region_strings:
                # MSG.PAK strings never go through in-place writeToRom: that
                # path pads the slot with `\x00` to fill `original_byte_length`,
                # and `00 00` decodes to `@SP@`. Within a MSG.PAK group there's
                # no per-string offset, so the engine reads the padding as
                # leading spaces on the *next* string. The §12 resize path
                # rebuilds affected entries below using
                # ``encoded_bytes_for_grow`` (no padding, original terminator)
                # so the bytes flow cleanly into the next string. Unedited
                # MSG.PAK strings keep their vanilla bytes from the initial
                # ``bytearray(original_rom_data)`` copy.
                if is_msgpak:
                    continue
                s.writeToRom(out)
        # Sprite pak splice runs inside serialize_all (not just _with_qol)
        # so direct ROM saves include sprite edits. Project save passes
        # ``skip_sprite_splice=True`` and snapshots per-entry edits into
        # the sprite_edits channel instead — otherwise a single grown
        # sprite would shift every downstream file and bloat the byte diff.
        if not skip_sprite_splice:
            self._apply_sprite_pak_splice(out)
        # Sidecar growth piggybacks on the sprite splice — without the PAK
        # growing too, longer chrsize/btchrsize files would describe groups
        # that don't exist. Project save skips both; the next ROM export
        # writes the consistent triple.
        self._apply_btchr_size_edits(out, skip_sidecar_resize=skip_sprite_splice)
        # Btmap edits ride the same channel pattern as sprite edits —
        # ROM save splices, project save (Phase F) skips and persists
        # per-path bytes so a single grown btmap doesn't shift every
        # downstream file in the byte diff.
        if not skip_btmap_splice:
            self._apply_btmap_splice(out)
        # Field-map edits (PLAN.md §14.5 Phase F) ride the same channel
        # pattern: ROM save splices, project save (handled by callers
        # passing ``skip_map_splice=True``) persists per-path bytes via
        # ``map_file_edits`` so a single grown ``.s`` or ``.c`` doesn't
        # shift every downstream file in the byte diff.
        if not skip_map_splice:
            self._apply_map_splice(out)
        # Overlay5 (script overlay) edits — same channel pattern as
        # btmap/map: ROM save splices in place, project save (caller
        # passes ``skip_overlay5_splice=True``) routes per-entry bytes
        # through the ``overlay5_entry_edits`` channel instead.
        if not skip_overlay5_splice:
            self._apply_overlay5_splice(out)
        return out

    def serialize_all_with_qol(self) -> bytearray:
        """`serialize_all()` plus enabled QoL byte-patches applied on top.

        QoL is applied last so it sits over any model edits — never the other
        way around. Multiplier-style patches read from the post-serialize_all
        bytes, so they scale the user-edited values (not vanilla).
        """
        out = self.serialize_all()
        qol_module.apply_qol_patches(out, self.version, self.qol)
        self._apply_msgpak_resize(out)
        self._trim_trailing_padding(out)
        return out

    def vanilla_file_table(self) -> fnt.FileTable:
        """Lazy ``FileTable`` over ``original_rom_data``.

        Used by read-only browsers (currently the btmap browser per
        PLAN.md §14.4 Phase B) that need path → FAT range lookups without
        following the post-edit splice layout. Cached for the session
        lifetime since vanilla bytes don't change.
        """
        if self._file_table_cache is None:
            self._file_table_cache = fnt.FileTable.from_rom(self.original_rom_data)
        return self._file_table_cache

    def btmap_file_bytes(self, path: str) -> bytes:
        """Resolve ``path`` (e.g. ``"DAT/btmap/0ac"``) to bytes.

        Checks the per-session dirty cache first (Phase D import edits);
        falls back to the vanilla FAT slot if no override is recorded.
        Codec call sites pass the result through ``maybe_decompress`` so
        the cache may store either uncompressed or RLE-30 bytes — the
        import path currently emits uncompressed, which is what the
        read path consumes regardless of compression.
        """
        cached = self._dirty_btmap_files.get(path)
        if cached is not None:
            return cached
        return self.vanilla_file_table().slice(self.original_rom_data, path)

    def replace_btmap_file_bytes(self, path: str, new_bytes: bytes) -> bytes:
        """Install ``new_bytes`` as the override for ``path`` and return
        whatever was there before — vanilla FAT bytes or a prior edit.

        Used by :class:`commands.ReplaceBtmapFileCommand` to make the
        flip undoable. Mutates only the dirty cache; ``original_rom_data``
        and the cached vanilla file table are not touched.
        """
        previous = self.btmap_file_bytes(path)
        self._dirty_btmap_files[path] = bytes(new_bytes)
        return previous

    def map_file_bytes(self, path: str) -> bytes:
        """Resolve ``path`` (e.g. ``"DAT/map/100.0t"``) to bytes.

        Mirrors :meth:`btmap_file_bytes`: dirty cache first, vanilla FAT
        fallback. The dirty cache holds *uncompressed* bytes; the FAT
        splice (Phase F) re-encodes via the shared RLE-30 wrapper on
        save.
        """
        cached = self._dirty_map_files.get(path)
        if cached is not None:
            return cached
        return self.vanilla_file_table().slice(self.original_rom_data, path)

    def replace_map_file_bytes(self, path: str, new_bytes: bytes) -> bytes:
        """Install ``new_bytes`` as the override for ``path`` and return
        the prior bytes — vanilla or a previous edit. Mutates only the
        dirty cache."""
        previous = self.map_file_bytes(path)
        self._dirty_map_files[path] = bytes(new_bytes)
        return previous

    # ---- overlay5 (script overlay) -----------------------------------------

    def overlay5_index(self) -> overlay5_mod.Overlay5Index:
        """Lazy ``Overlay5Index`` over vanilla bytes.

        Used by the Events tab to enumerate OVERWORLD_SPRITE placements
        per map; also reused by :meth:`_apply_overlay5_splice` so the
        save path sees the same pointer table the UI dragged against.
        """
        if self._overlay5_index_cache is None:
            ft = self.vanilla_file_table()
            self._overlay5_index_cache = overlay5_mod.Overlay5Index.from_file_table(
                ft, self.original_rom_data,
            )
        return self._overlay5_index_cache

    def cutscene_index(self) -> overlay5_cutscenes_mod.CutsceneIndex:
        """Lazy :class:`CutsceneIndex` over the overlay5 chain graph.

        Built once on first access and cached; subsequent calls are O(1).
        Drives the Cutscenes tab's per-map browsing — `chains_for_map(id)`
        returns every chain whose source entry maps to that field map.
        """
        if self._cutscene_index_cache is None:
            self._cutscene_index_cache = overlay5_cutscenes_mod.build_cutscene_index(
                self.overlay5_index(),
            )
        return self._cutscene_index_cache

    def overlay5_entry_bytes(self, entry_ix: int) -> bytes:
        """Bytes for overlay5 entry ``entry_ix`` — dirty cache first,
        vanilla overlay payload otherwise. Mirrors the btmap / map
        dirty-cache pattern."""
        cached = self._dirty_overlay5_entries.get(entry_ix)
        if cached is not None:
            return cached
        return self.overlay5_index().read_entry(entry_ix)

    def replace_overlay5_entry_bytes(
        self, entry_ix: int, new_bytes: bytes,
    ) -> bytes:
        """Install ``new_bytes`` as the override for entry ``entry_ix``
        and return the prior bytes — vanilla or a previous edit.

        Enforces the same-length invariant the codec also checks; the
        splice path depends on it (length-shifting an entry would
        re-flow every later entry's offset in the pointer table).
        Raises ``ValueError`` on a length mismatch.
        """
        previous = self.overlay5_entry_bytes(entry_ix)
        if len(new_bytes) != len(previous):
            raise ValueError(
                f"overlay5 entry {entry_ix:04d} length mismatch: "
                f"{len(new_bytes)} vs {len(previous)}"
            )
        self._dirty_overlay5_entries[entry_ix] = bytes(new_bytes)
        return previous

    def sprite_pak(self, pak_name: str) -> pak.PakFile:
        """Lazy-load and cache one of the sprite pak directories.

        ``pak_name`` is the FAT path (e.g. ``"DAT/SPR_CHR.PAK"``). The
        PakFile is parsed from ``original_rom_data`` for fresh ROM sessions;
        project sessions whose byte diff touched this pak pre-populate the
        cache from ``patched_data`` in :meth:`from_project` so the live
        ``pak.entries`` start at the project's edited state.
        """
        cached = self._sprite_pak_cache.get(pak_name)
        if cached is not None:
            return cached
        ft = fnt.FileTable.from_rom(self.original_rom_data)
        start, end = ft.resolve(pak_name)
        cached = pak.PakFile(self.original_rom_data[start:end])
        self._sprite_pak_cache[pak_name] = cached
        return cached

    def mark_sprite_pak_dirty(self, pak_name: str) -> None:
        """Flag ``pak_name`` for re-splice on the next ``serialize_all``.

        Called by :class:`commands.ReplaceSpriteCommand` on every redo/undo.
        Idempotent — the splice only runs once per save regardless of how
        many entries inside the pak were touched.
        """
        self._dirty_sprite_paks.add(pak_name)

    def _apply_sprite_pak_splice(self, out: bytearray) -> None:
        """Rebuild every dirty sprite pak and splice it into the ROM.

        Uses the same ``fat.splice_range`` + ``fat.resize_fat_entry``
        machinery MSG.PAK rides on, so a grown pak shifts every downstream
        FAT entry + NDS header offset by an 0x200-aligned step (DS file
        loader requires that alignment). Shrunk paks reclaim whole 0x200
        blocks; sub-block leftover stays as 0xFF padding inside the
        container so the next file still starts on a boundary.

        Splice order matters when multiple paks are dirty: each splice
        shifts every byte past the container's old end, invalidating any
        cached offsets for files at higher ROM addresses. Iterating in
        **descending pak_start order** keeps the lower-offset paks'
        pre-resolved offsets valid (those files don't move when a
        higher-offset file is spliced first).
        """
        if not self._dirty_sprite_paks:
            return
        file_table = fnt.FileTable.from_rom(bytes(out))
        # Snapshot offsets BEFORE any splice — they're valid for every pak
        # as long as we process highest-offset-first (each splice only
        # shifts content past its own container_end).
        dirty: List[Tuple[int, int, str]] = []
        for pak_name in self._dirty_sprite_paks:
            try:
                pak_start, pak_end = file_table.resolve(pak_name)
            except KeyError:
                continue
            if pak_name in self._sprite_pak_cache:
                dirty.append((pak_start, pak_end, pak_name))
        for pak_start, pak_end, pak_name in sorted(dirty, reverse=True):
            pak_obj = self._sprite_pak_cache[pak_name]
            new_bytes = pak_obj.to_bytes()
            idx, _cs, ce = fat.find_container(out, pak_start, pak_end)
            content_delta = len(new_bytes) - (pak_end - pak_start)
            aligned_shift = fat.splice_range(
                out, pak_start, pak_end, ce, new_bytes
            )
            fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)

    def _apply_btmap_splice(self, out: bytearray) -> None:
        """Splice every dirty btmap FAT file back into the ROM.

        Re-compresses each entry with RLE-30 — vanilla btmap files are
        RLE-30 wrapped and the engine calls SWI 0x14 on them, so the
        replacement must use the same framing or the BIOS decompressor
        will read garbage. The dirty cache may hold uncompressed bytes
        (the typical import-path output) or already-compressed bytes;
        we normalize via ``maybe_decompress`` then re-encode so the ROM
        sees consistent framing regardless of caller.

        Same descending-offset ordering as the sprite splice: each
        splice shifts everything past its container's old end, so
        higher-offset files are processed first to keep lower-offset
        pre-resolved bounds valid.
        """
        if not self._dirty_btmap_files:
            return
        from digimon_core.sprite import compress_rle30, maybe_decompress

        file_table = fnt.FileTable.from_rom(bytes(out))
        dirty: List[Tuple[int, int, str, bytes]] = []
        for path, new_bytes in self._dirty_btmap_files.items():
            try:
                file_start, file_end = file_table.resolve(path)
            except KeyError:
                continue
            raw = maybe_decompress(new_bytes)
            compressed = compress_rle30(raw)
            dirty.append((file_start, file_end, path, compressed))
        for file_start, file_end, _path, compressed in sorted(
            dirty, key=lambda x: x[0], reverse=True,
        ):
            idx, _cs, ce = fat.find_container(out, file_start, file_end)
            content_delta = len(compressed) - (file_end - file_start)
            aligned_shift = fat.splice_range(
                out, file_start, file_end, ce, compressed,
            )
            fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)

    def _apply_map_splice(self, out: bytearray) -> None:
        """Splice every dirty field-map FAT file back into the ROM.

        Mirrors :meth:`_apply_btmap_splice`. Field-map files (``DAT/map/
        <id>{a,b}.{c,p,s}`` and ``<id>.{d,0t,a}``) are RLE-30 wrapped on
        disk and the engine decompresses them via SWI 0x14, so the
        replacement re-runs compress_rle30 on the (decompressed) dirty
        bytes. The dirty cache may hold either form — paint tools store
        the uncompressed payload, but external callers (project load)
        can pass already-compressed bytes; ``maybe_decompress`` normalizes
        both.
        """
        if not self._dirty_map_files:
            return
        from digimon_core.sprite import compress_rle30, maybe_decompress

        file_table = fnt.FileTable.from_rom(bytes(out))
        dirty: List[Tuple[int, int, str, bytes]] = []
        for path, new_bytes in self._dirty_map_files.items():
            try:
                file_start, file_end = file_table.resolve(path)
            except KeyError:
                continue
            raw = maybe_decompress(new_bytes)
            compressed = compress_rle30(raw)
            dirty.append((file_start, file_end, path, compressed))
        for file_start, file_end, _path, compressed in sorted(
            dirty, key=lambda x: x[0], reverse=True,
        ):
            idx, _cs, ce = fat.find_container(out, file_start, file_end)
            content_delta = len(compressed) - (file_end - file_start)
            aligned_shift = fat.splice_range(
                out, file_start, file_end, ce, compressed,
            )
            fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)

    def _apply_overlay5_splice(self, out: bytearray) -> None:
        """Stamp every dirty overlay5 entry back into the overlay file.

        Same-length only (enforced by ``replace_overlay5_entry_bytes``)
        so no FAT resize is needed — each entry's byte range stays
        fixed and downstream entries don't shift. Resolves the overlay
        file's location through :func:`overlay5.find_overlay_fat_range`
        against the *current* ROM image so any earlier splice
        (sprite / btmap / map) that shifted the overlay forward is
        followed automatically.
        """
        if not self._dirty_overlay5_entries:
            return
        ovl_start, _ovl_end = overlay5_mod.find_overlay_fat_range(
            bytes(out), overlay_id=5,
        )
        index = self.overlay5_index()
        for entry_ix, new_entry in self._dirty_overlay5_entries.items():
            entry_off = index.entry_starts[entry_ix]
            # In-place write — length already verified at register time.
            out[ovl_start + entry_off:
                ovl_start + entry_off + len(new_entry)] = new_entry

    # ---- BTCHR sidecar edits ----------------------------------------------

    def current_chrsize_word(self, group: int) -> int:
        """Return the live u32 for ``group`` in BTCHR/CHRSIZE.BIN.

        Prefers an in-memory edit if one exists; otherwise reads the
        vanilla word from ``original_rom_data``. Used by undo commands
        to snapshot the pre-edit state before they overwrite it.
        """
        if group in self._chrsize_edits:
            return self._chrsize_edits[group]
        ft = fnt.FileTable.from_rom(self.original_rom_data)
        start, _end = ft.resolve(CHRSIZE_PATH)
        return struct.unpack_from("<I", self.original_rom_data, start + group * 4)[0]

    def current_btchrsize_value(self, group: int) -> int:
        """Return the live u32 for ``group`` in BTCHR/BTCHRSIZE.BIN."""
        if group in self._btchrsize_edits:
            return self._btchrsize_edits[group]
        ft = fnt.FileTable.from_rom(self.original_rom_data)
        start, _end = ft.resolve(BTCHRSIZE_PATH)
        return struct.unpack_from("<I", self.original_rom_data, start + group * 4)[0]

    def set_chrsize_word(self, group: int, word: int) -> None:
        """Record a u32 edit for ``group`` in BTCHR/CHRSIZE.BIN.

        Idempotent — writing the vanilla word leaves the edit in the dict
        but produces a no-op diff at save time. Undo commands rely on
        this so they can restore an exact pre-edit value without having
        to special-case "no edit" vs "edit equal to vanilla".
        """
        self._chrsize_edits[group] = word & 0xFFFFFFFF

    def set_btchrsize_value(self, group: int, value: int) -> None:
        """Record a u32 edit for ``group`` in BTCHR/BTCHRSIZE.BIN."""
        self._btchrsize_edits[group] = value & 0xFFFFFFFF

    def vanilla_btchr_group_count(self) -> int:
        """Number of BTCHR groups in ``original_rom_data`` — 415 on vanilla
        US Dusk/Dawn. Derived from CHRSIZE.BIN size since the PAK count
        already reflects any in-memory appends."""
        ft = fnt.FileTable.from_rom(self.original_rom_data)
        start, end = ft.resolve(CHRSIZE_PATH)
        return (end - start) // 4

    def append_btchr_group_sidecars(
        self, chrsize_word: int, btchrsize_value: int,
    ) -> None:
        """Record a new BTCHR group's sidecar words (chrsize + btchrsize).

        The PAK growth is the caller's responsibility (bump
        ``sprite_pak(BTCHR_PAK).count`` and append 5 entries). These two
        u32s ride the sidecar resize on serialize so the engine sees a
        consistent (PAK count, chrsize length, btchrsize length) triple.
        """
        self._btchr_appended_chrsize.append(chrsize_word & 0xFFFFFFFF)
        self._btchr_appended_btchrsize.append(btchrsize_value & 0xFFFFFFFF)

    def pop_btchr_group_sidecars(self) -> Tuple[int, int]:
        """Drop the last appended sidecar pair. Used by undo paths to
        rewind an append. Returns the popped ``(chrsize_word,
        btchrsize_value)``. Raises ``IndexError`` if no appends are
        pending."""
        return (
            self._btchr_appended_chrsize.pop(),
            self._btchr_appended_btchrsize.pop(),
        )

    def _apply_btchr_size_edits(
        self, out: bytearray, *, skip_sidecar_resize: bool = False,
    ) -> None:
        """Stamp every recorded chrsize/btchrsize edit into ``out``.

        Resolved against the current ROM image's FAT so this works
        whether or not :meth:`_apply_sprite_pak_splice` already ran —
        if a sprite splice shifted BTCHR.PAK and pushed the sidecars to
        new offsets, the freshly-walked FAT finds them at their new home.

        Sidecar growth (``_btchr_appended_chrsize`` non-empty) takes the
        splice path instead: each file is rebuilt as ``vanilla_bytes +
        appended u32s`` with in-place edits stamped on top, then run
        through ``fat.splice_range`` so the FAT/header track the new
        length. Walking a fresh FNT before each splice keeps offsets
        valid through interleaved file shifts.

        ``skip_sidecar_resize=True`` suppresses the splice path —
        project save uses it so the byte diff stays small. The appended
        groups are dropped from the project save; they only survive a
        direct ROM export.
        """
        no_edits = not self._chrsize_edits and not self._btchrsize_edits
        no_appended = not self._btchr_appended_chrsize or skip_sidecar_resize
        if no_edits and no_appended:
            return
        if no_appended:
            ft = fnt.FileTable.from_rom(bytes(out))
            if self._chrsize_edits:
                start, _end = ft.resolve(CHRSIZE_PATH)
                for g, word in self._chrsize_edits.items():
                    struct.pack_into("<I", out, start + g * 4, word)
            if self._btchrsize_edits:
                start, _end = ft.resolve(BTCHRSIZE_PATH)
                for g, value in self._btchrsize_edits.items():
                    struct.pack_into("<I", out, start + g * 4, value)
            return
        for path, edits, appended in (
            (CHRSIZE_PATH, self._chrsize_edits, self._btchr_appended_chrsize),
            (BTCHRSIZE_PATH, self._btchrsize_edits, self._btchr_appended_btchrsize),
        ):
            ft = fnt.FileTable.from_rom(bytes(out))
            start, end = ft.resolve(path)
            content = bytearray(out[start:end])
            for word in appended:
                content += struct.pack("<I", word)
            for g, word in edits.items():
                struct.pack_into("<I", content, g * 4, word)
            idx, _cs, ce = fat.find_container(out, start, end)
            content_delta = len(content) - (end - start)
            aligned_shift = fat.splice_range(out, start, end, ce, bytes(content))
            fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)

    # ---- display-name resolvers --------------------------------------------

    def digimon_display_name(self, digimon_id: int) -> str:
        """Best available display name for a sprite_map slot id.

        Prefers ``DIGIMON_ID_TO_STR``; falls back to the battle-string
        text resolved via ``STRING_BATTLE_TABLE_OFFSET[version][0] +
        BattleStringEntry.value``. The fallback covers ~380 digimon
        slots (digieggs, in-trainings, fixed-enemy bosses) plus the
        NPC slots in 0x30e..0x363 (Glare..Sayo and assorted aliased
        recolors) — every sprite_map slot whose battle_string points
        at a string in ``arm9_digiegg_enemy_names`` resolves through
        the same path. Unknown ids return ``<unnamed 0x...>``.
        """
        if not self._digimon_name_cache_valid:
            self._build_digimon_name_cache()
        name = self._digimon_name_cache.get(digimon_id)
        if name is not None:
            return name
        return f"<unnamed 0x{digimon_id:03x}>"

    def invalidate_name_caches(self) -> None:
        """Force rebuild of the display-name caches on next access.

        Call after editing a ``BattleStringEntry.value`` or a string
        inside ``arm9_digiegg_enemy_names`` — the cache is otherwise
        held for the lifetime of the session.
        """
        self._digimon_name_cache_valid = False

    def dialog_msg_text(self, msg_id: int) -> Optional[model.GameString]:
        """Resolve a DIALOG-block ``msg_id`` to its MSG.PAK GameString.

        The dialog group lives at a fixed run of msgpak_all entries: the
        first dialog (``msg_id == 0``) is the string ending with
        ``"Are you okay?[BR]...You were quite restless."``. Anchor-search
        for it once and cache the base index so callers can compute
        ``msgpak_all[base + msg_id]``. Returns ``None`` for out-of-range
        ids or if the anchor isn't present (e.g. a heavily-modified
        MSG.PAK that dropped the system messages).
        """
        if self._dialog_msgpak_base is None:
            strings = self.string_regions.get("msgpak_all", [])
            anchor = -1
            for i, s in enumerate(strings):
                if s.text.endswith("You were quite restless."):
                    anchor = i
                    break
            self._dialog_msgpak_base = anchor
        base = self._dialog_msgpak_base
        if base < 0:
            return None
        strings = self.string_regions.get("msgpak_all", [])
        idx = base + int(msg_id)
        if 0 <= idx < len(strings):
            return strings[idx]
        return None

    def remember_selection(self, editor_key: str, selection_id: int) -> None:
        """Stash the active row id for ``editor_key`` so a later visit can
        restore it. ``editor_key`` matches the dispatch key in
        ``main_window._build_editor_for``."""
        self.last_selections[editor_key] = int(selection_id)

    def recall_selection(self, editor_key: str) -> Optional[int]:
        """Return the last id remembered for ``editor_key``, or None if the
        editor hasn't been visited in this session yet."""
        return self.last_selections.get(editor_key)

    def invalidate_portrait_icon_cache(self) -> None:
        """Drop the cached portrait QIcons.

        Call after a sprite_map edit (changes which SPR entry a digimon
        points at) or after a SPR_*.PAK splice (changes the entry's
        bytes). Misses are recomputed lazily on the next combo popup.
        """
        self._digimon_portrait_icon_cache.clear()

    def invalidate_battle_sprite_pixmap_cache(self) -> None:
        """Drop the cached BTCHR preview pixmaps.

        Call after a BTCHR.PAK edit (entry bytes change) or after a group
        append (count changes). Misses recomputed lazily on next preview.
        """
        self._battle_sprite_pixmap_cache.clear()

    def invalidate_spr_pixmap_cache(self) -> None:
        """Drop the cached SPR_* preview pixmaps (portrait + battle-mini)."""
        self._spr_pixmap_cache.clear()

    def invalidate_mchr_pixmap_cache(self) -> None:
        """Drop the cached MCHR preview pixmaps (overworld follower)."""
        self._mchr_pixmap_cache.clear()

    def battle_sprite_pixmap(self, group_idx: int, max_size: int = 96):
        """Lazy QPixmap of BTCHR group ``group_idx``'s idle stance, or ``None``.

        Decodes the 5 BTCHR entries (header/NCGR/NCLR/NCER), renders the
        first non-empty cell into RGBA, and scales to fit ``max_size``
        (keeping aspect ratio). Returns ``None`` for out-of-range ids,
        parse failures, or groups with no renderable cell — those negative
        results cache too so preview repaints don't redo the work.
        """
        cache_key = (group_idx, max_size)
        cached = self._battle_sprite_pixmap_cache.get(cache_key)
        if cache_key in self._battle_sprite_pixmap_cache:
            return cached
        pixmap = self._build_battle_sprite_pixmap(group_idx, max_size)
        self._battle_sprite_pixmap_cache[cache_key] = pixmap
        return pixmap

    def _build_battle_sprite_pixmap(self, group_idx: int, max_size: int):
        from digimon_core import btchr
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPixmap

        try:
            pak_file = self.sprite_pak("DAT/BTCHR.PAK")
        except KeyError:
            return None
        if not (0 <= group_idx * btchr.GROUP_SIZE + btchr.GROUP_SIZE <= pak_file.count):
            return None
        try:
            d = btchr.decode_digimon(pak_file, group_idx)
        except (ValueError, IndexError):
            return None
        # Pick the first cell whose OAM bbox is non-trivial — cell 0 is the
        # idle stance for most digimon but a handful use it for an empty /
        # transition frame, in which case render_cell_rgba returns an 8x8
        # placeholder we don't want to show.
        chosen = None
        for cell in d.ncer.cells:
            if cell.oams:
                chosen = cell
                break
        if chosen is None:
            return None
        try:
            rgba, w, h = btchr.render_cell_rgba(
                chosen, d.tile_bytes, d.palette,
                boundary_bytes=d.ncer.boundary_bytes,
            )
        except (ValueError, IndexError):
            return None
        if w == 0 or h == 0:
            return None
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pix = QPixmap.fromImage(img)
        if pix.width() > max_size or pix.height() > max_size:
            pix = pix.scaled(
                max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        return pix

    def spr_sprite_pixmap(self, spr_idx: int, max_size: int = 80):
        """Lazy QPixmap of SPR_*[spr_idx], or ``None`` on miss / parse failure.

        Used by the sprite-map row's portrait + battle-mini previews.
        Caches the negative result so empty / unparseable entries don't
        redo the parse on every refresh.
        """
        cache_key = (spr_idx, max_size)
        if cache_key in self._spr_pixmap_cache:
            return self._spr_pixmap_cache[cache_key]
        pix = self._build_spr_pixmap(spr_idx, max_size)
        self._spr_pixmap_cache[cache_key] = pix
        return pix

    def _build_spr_pixmap(self, spr_idx: int, max_size: int):
        from digimon_core import ncer as ncer_mod, sprite
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPixmap

        try:
            chr_pak = self.sprite_pak("DAT/SPR_CHR.PAK")
            pal_pak = self.sprite_pak("DAT/SPR_PAL.PAK")
            cel_pak = self.sprite_pak("DAT/SPR_CEL.PAK")
        except KeyError:
            return None
        if not (0 <= spr_idx < min(chr_pak.count, pal_pak.count, cel_pak.count)):
            return None
        try:
            chr_dec = sprite.maybe_decompress(chr_pak.entries[spr_idx])
            tile_bytes, bit_depth, hint_w, _hint_h, is_bitmap = sprite.parse_ncgr(chr_dec)
            palettes, _ = sprite.parse_nclr(sprite.maybe_decompress(pal_pak.entries[spr_idx]))
            parsed_ncer = ncer_mod.parse_ncer(cel_pak.entries[spr_idx])
        except (ValueError, IndexError):
            return None
        if not palettes:
            return None
        # Width heuristic mirrors _build_portrait_icon / SpriteBrowser.
        if hint_w:
            width_tiles = hint_w
        else:
            bbox_w, _ = ncer_mod.sprite_bbox(parsed_ncer)
            if bbox_w <= 0:
                width_tiles = 4
            elif bbox_w <= 16:
                width_tiles = 2
            elif bbox_w < 64:
                width_tiles = 4
            else:
                width_tiles = 8
        if bit_depth == 4 and len(palettes[0]) == 16:
            palette = [c for bank in palettes for c in bank]
        else:
            palette = palettes[0]
        rgba, w, h = sprite.render_rgba(
            tile_bytes, bit_depth, palette, width_tiles, is_bitmap,
        )
        if w == 0 or h == 0:
            return None
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pix = QPixmap.fromImage(img)
        if pix.width() > max_size or pix.height() > max_size:
            pix = pix.scaled(
                max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        return pix

    def element_icon_pixmap(self, element_value: int, max_size: int = 24):
        """Lazy QPixmap of the SPR_CHR element icon, or ``None`` on miss.

        Shares ``_spr_pixmap_cache`` with ``spr_sprite_pixmap`` so a
        SPR_*.PAK edit invalidates both via the existing
        ``invalidate_spr_pixmap_cache`` hook.
        """
        spr_idx = ELEMENT_SPR_INDEX.get(element_value)
        if spr_idx is None:
            return None
        return self.spr_sprite_pixmap(spr_idx, max_size=max_size)

    def mchr_sprite_pixmap(
        self, mchr_idx: int, max_size: int = 80, frame: Optional[int] = None,
    ):
        """Lazy QPixmap of MCHR[mchr_idx], or ``None`` on miss.

        ``frame=None`` picks the canonical front-facing pose (frame 3 when
        available, frame 0 otherwise) — matches the digivolution-menu
        render and the sprite-map row's party-follower preview. Passing
        an explicit ``frame`` is used by the Events sidebar's Sprite
        Frame editor so the marker shows the in-game placement's pose
        instead of the canonical one.
        """
        cache_key = (mchr_idx, max_size, frame)
        if cache_key in self._mchr_pixmap_cache:
            return self._mchr_pixmap_cache[cache_key]
        pix = self._build_mchr_pixmap(mchr_idx, max_size, frame)
        self._mchr_pixmap_cache[cache_key] = pix
        return pix

    def _build_mchr_pixmap(
        self, mchr_idx: int, max_size: int, frame: Optional[int] = None,
    ):
        from digimon_core import mchr as mchr_mod, sprite
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPixmap

        try:
            chr_pak = self.sprite_pak("DAT/MCHR_CHR.PAK")
            pal_pak = self.sprite_pak("DAT/MCHR_PAL.PAK")
        except KeyError:
            return None
        if not (0 <= mchr_idx < chr_pak.count):
            return None
        try:
            entry = mchr_mod.parse_mchr_chr_entry(sprite.decompress_rle30(chr_pak.entries[mchr_idx]))
        except (ValueError, IndexError):
            return None
        if entry.frame_count == 0:
            return None
        pal_idx = min(mchr_idx, pal_pak.count - 1)
        if pal_idx < 0:
            return None
        try:
            palette = mchr_mod.parse_palette_bgr555(sprite.decompress_rle30(pal_pak.entries[pal_idx]))
        except (ValueError, IndexError):
            return None
        # Frame 3 is the canonical front-facing pose for most MCHR entries
        # (matches the in-game digivolution menu); fall back to frame 0 for
        # short animations that don't have a frame 3. Explicit ``frame``
        # overrides that default — Events sidebar uses it to render the
        # in-game placement's pose. Out-of-range frame indexes clamp to
        # the last available frame instead of returning None: the user
        # may have typed a frame that exists on a different MCHR, and
        # clamping is friendlier than going blank.
        if frame is None:
            frame_idx = 3 if entry.frame_count > 3 else 0
        else:
            frame_idx = max(0, min(int(frame), entry.frame_count - 1))
        try:
            rgba, w, h = mchr_mod.render_frame_rgba(entry.frames[frame_idx], palette)
        except (ValueError, IndexError):
            return None
        if w == 0 or h == 0:
            return None
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pix = QPixmap.fromImage(img)
        if pix.width() > max_size or pix.height() > max_size:
            pix = pix.scaled(
                max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        return pix

    def prewarm_digimon_icons(self) -> None:
        """Idle-tick background population of digimon portrait icons on
        the shared "digimon" / "digimon_evo" picker models.

        Each portrait costs ~2-3ms to decode (SPR_CHR/PAL/CEL parse +
        RGBA composite + QIcon wrap), so doing ~400 entries up-front
        would stall the UI for ~1s. Spread across ~50-item ticks the
        wall-time cost is invisible and icons are typically in place by
        the time the user opens an editor that uses these dropdowns.

        After completion, marks each model's ``icons_loaded`` property
        True so the form-helper ``showPopup`` icon-fill loop becomes a
        no-op.
        """
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer, Qt
        if QApplication.instance() is None:
            return

        targets: List[Tuple[object, int]] = []
        for kind in ("digimon", "digimon_evo"):
            model = self._picker_models.get(kind)
            if model is None:
                continue
            for i in range(model.rowCount()):
                item = model.item(i)
                if item is None:
                    continue
                did = item.data(Qt.UserRole)
                if not isinstance(did, int) or did == 0xFFFFFFFF:
                    continue
                targets.append((item, did))

        if not targets:
            return

        chunk_size = 50
        self._icon_prewarm_cursor = 0

        def process_chunk() -> None:
            end = min(self._icon_prewarm_cursor + chunk_size, len(targets))
            for ix in range(self._icon_prewarm_cursor, end):
                item, did = targets[ix]
                icon = self.digimon_portrait_icon(did)
                if icon is not None:
                    item.setIcon(icon)
            self._icon_prewarm_cursor = end
            if end < len(targets):
                QTimer.singleShot(0, process_chunk)
            else:
                for kind in ("digimon", "digimon_evo"):
                    m = self._picker_models.get(kind)
                    if m is not None:
                        m.setProperty("icons_loaded", True)

        QTimer.singleShot(0, process_chunk)

    def digimon_portrait_icon(self, digimon_id: int):
        """Lazy QIcon for the digimon's portrait sprite, or ``None``.

        Resolves ``sprite_map[digimon_id].upperscreen_low`` to an SPR_CHR
        entry, parses CHR/PAL/CEL, and renders a small RGBA bitmap wrapped
        in a QIcon. Returns ``None`` for ids without a sprite_map slot,
        parse failures, or empty renders — those negative results cache
        too so popup repaints don't redo the work.

        Imports Qt lazily so headless code paths that touch RomSession
        (CLI loaders, tests) don't pull in PySide6.
        """
        cached = self._digimon_portrait_icon_cache.get(digimon_id)
        if digimon_id in self._digimon_portrait_icon_cache:
            return cached
        icon = self._build_portrait_icon(digimon_id)
        self._digimon_portrait_icon_cache[digimon_id] = icon
        return icon

    def _build_portrait_icon(self, digimon_id: int):
        from digimon_core import ncer as ncer_mod, sprite
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon, QImage, QPixmap

        if not (0 <= digimon_id < len(self.sprite_map)):
            return None
        spr_idx = self.sprite_map[digimon_id].upperscreen_low
        try:
            chr_pak = self.sprite_pak("DAT/SPR_CHR.PAK")
            pal_pak = self.sprite_pak("DAT/SPR_PAL.PAK")
            cel_pak = self.sprite_pak("DAT/SPR_CEL.PAK")
        except KeyError:
            return None
        if not (0 <= spr_idx < min(chr_pak.count, pal_pak.count, cel_pak.count)):
            return None
        try:
            chr_dec = sprite.maybe_decompress(chr_pak.entries[spr_idx])
            tile_bytes, bit_depth, hint_w, _hint_h, is_bitmap = sprite.parse_ncgr(chr_dec)
            palettes, _ = sprite.parse_nclr(sprite.maybe_decompress(pal_pak.entries[spr_idx]))
            parsed_ncer = ncer_mod.parse_ncer(cel_pak.entries[spr_idx])
        except (ValueError, IndexError):
            return None
        if not palettes:
            return None
        # Width heuristic mirrors SpriteBrowser._default_width_tiles_for_bbox.
        if hint_w:
            width_tiles = hint_w
        else:
            bbox_w, _ = ncer_mod.sprite_bbox(parsed_ncer)
            if bbox_w <= 0:
                width_tiles = 4
            elif bbox_w <= 16:
                width_tiles = 2
            elif bbox_w < 64:
                width_tiles = 4
            else:
                width_tiles = 8
        # Engine concatenates 4bpp banks for an 8bpp NCGR; match it so
        # portraits don't render with a wrong-bank palette.
        if bit_depth == 4 and len(palettes[0]) == 16:
            palette = [c for bank in palettes for c in bank]
        else:
            palette = palettes[0]
        rgba, w, h = sprite.render_rgba(
            tile_bytes, bit_depth, palette, width_tiles, is_bitmap,
        )
        if w == 0 or h == 0:
            return None
        img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pix = QPixmap.fromImage(img).scaled(
            32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        return QIcon(pix)

    def sprite_attribution(self) -> Dict[str, Dict[int, int]]:
        """Frozen sprite-idx → owning digimon_id snapshot.

        Captured on first call from the current ``sprite_map`` state,
        then served as-is forever. Used by every ``compute_*_labels``
        helper so reassigning a digimon's sprite later in the session
        does NOT relabel the original sprite — labels behave as if
        sourced from a fixed string array. NPC slots (sprite_map
        0x30e..0x363) get attributed the same way as digimon since
        ``digimon_display_name`` already covers both.

        Returns a dict with four sub-maps, one per sprite_map field:
        ``main_sprite`` (BTCHR), ``unknown_0x4`` (MCHR overworld),
        ``upperscreen_low`` (SPR portrait), ``upperscreen_high`` (SPR
        battle preview).
        """
        if self._sprite_attribution_snapshot is None:
            snap: Dict[str, Dict[int, int]] = {
                "main_sprite": {},
                "unknown_0x4": {},
                "upperscreen_low": {},
                "upperscreen_high": {},
            }
            for base_id, entry in enumerate(getattr(self, "sprite_map", [])):
                snap["main_sprite"].setdefault(entry.main_sprite, base_id)
                snap["unknown_0x4"].setdefault(entry.unknown_0x4, base_id)
                snap["upperscreen_low"].setdefault(entry.upperscreen_low, base_id)
                snap["upperscreen_high"].setdefault(entry.upperscreen_high, base_id)
            self._sprite_attribution_snapshot = snap
        return self._sprite_attribution_snapshot

    def get_spr_labels(self) -> List[str]:
        """Shared SPR_* picker labels. Computed lazily, then cached.

        Returns the same list object until ``invalidate_sprite_label_caches``
        is called, so identity-keyed combo populators (see
        ``_SpriteListPicker._populate``) can skip the 1627-item rebuild on
        warm selection switches.
        """
        if self._spr_labels_cache is None:
            from .widgets.sprite_browser import compute_spr_labels
            self._spr_labels_cache = compute_spr_labels(self)
        return self._spr_labels_cache

    def get_mchr_labels(self) -> List[str]:
        """Shared MCHR_* picker labels (overworld sprites)."""
        if self._mchr_labels_cache is None:
            from .widgets.mchr_browser import compute_mchr_labels
            self._mchr_labels_cache = compute_mchr_labels(self)
        return self._mchr_labels_cache

    def get_btchr_group_labels(self) -> List[str]:
        """Shared BTCHR group picker labels (main battle sprite)."""
        if self._btchr_group_labels_cache is None:
            from .widgets.btchr_browser import compute_btchr_group_labels
            self._btchr_group_labels_cache = compute_btchr_group_labels(self)
        return self._btchr_group_labels_cache

    def invalidate_sprite_label_caches(self) -> None:
        """Drop SPR / MCHR / BTCHR label caches and their picker models.

        Call after a sprite_map edit (changes cross-ref names embedded in
        SPR labels) or after a sprite-pak splice / BTCHR append (changes
        pak length). Next ``get_*_labels`` / ``picker_model`` rebuilds
        from scratch. Pooled sprite pickers get re-linked to the fresh
        models so they don't show stale labels on next acquire.
        """
        self._spr_labels_cache = None
        self._mchr_labels_cache = None
        self._btchr_group_labels_cache = None
        for kind in ("spr", "mchr", "btchr"):
            self.invalidate_picker_model(kind)
        self._relink_pooled_picker_models()
        self.invalidate_battle_sprite_pixmap_cache()
        self.invalidate_spr_pixmap_cache()
        self.invalidate_mchr_pixmap_cache()

    def _relink_pooled_picker_models(self) -> None:
        """Re-attach pooled pickers to the current SPR/MCHR/BTCHR models.

        Called after ``invalidate_sprite_label_caches`` so the long-lived
        pool reflects the post-edit labels. Quietly no-ops when the pool
        wasn't built (e.g. headless test sessions).
        """
        if self._pooled_sprite_pickers is None:
            return
        # Same slot order as _build_sprite_picker_pool: mchr, btchr, spr, spr.
        kinds = ("mchr", "btchr", "spr", "spr")
        for ix, kind in enumerate(kinds):
            picker = self._pooled_sprite_pickers[ix]
            model = self.picker_model(kind)
            if model is None:
                continue
            # Silence currentIndexChanged: setModel emits when the new
            # model's currentIndex differs, which would cascade into a
            # spurious SetAttrCommand(slot, attr, 0) for every bound row.
            # The picker's real index is restored by picker.refresh() in
            # sprite_map_row.refresh() immediately after.
            picker.blockSignals(True)
            try:
                picker.setModel(model)
                completer = picker.completer()
                if completer is not None:
                    completer.setModel(model)
            finally:
                picker.blockSignals(False)
            picker._shared_model = model

    def picker_model(self, kind: str):
        """Shared QStandardItemModel for combos of ``kind``, or ``None``.

        Lazy: built on first request and cached. Every combo opting in
        via ``setModel`` reuses the same model so the per-combo
        ``addItem`` loop collapses to a single bulk build. Each row
        carries ``UserRole = int_value`` so existing value-lookup paths
        (``_ensure_index_for``) keep working unchanged.

        Kinds with a ``(none)`` sentinel bake it into row 0 — callers
        still pass ``none_value`` so they can recognize / skip the
        sentinel, but they don't need to add the row themselves.
        Imports Qt lazily so the headless ``RomSession`` import stays
        Qt-free.
        """
        cached = self._picker_models.get(kind)
        if cached is not None:
            return cached
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QStandardItem, QStandardItemModel

        rows: List[Tuple[int, str]] = []
        if kind == "moves":
            rows.append((0xFFFF, "(none)"))
            rows.extend(
                (i, f"0x{i:03x}  {name}")
                for i, name in enumerate(constants.MOVE_ARRAY_STR)
            )
        elif kind == "traits_byte":
            # Base digimon trait fields are 1-byte → sentinel 0xFF.
            rows.append((0xFF, "(none)"))
            rows.extend(
                (i, f"0x{i:03x}  {name}")
                for i, name in enumerate(constants.TRAIT_ARRAY_STR)
            )
        elif kind == "traits_word":
            # Enemy digimon trait fields are 2-byte → sentinel 0xFFFF.
            rows.append((0xFFFF, "(none)"))
            rows.extend(
                (i, f"0x{i:03x}  {name}")
                for i, name in enumerate(constants.TRAIT_ARRAY_STR)
            )
        elif kind == "battle_strings":
            base = constants.STRING_BATTLE_TABLE_OFFSET[self.version][0]
            for g in self.string_regions.get("arm9_digiegg_enemy_names", []):
                rel = g.offset - base
                rows.append((rel, f"0x{rel:03x}  {g.text}"))
        elif kind == "spr":
            rows.extend(enumerate(self.get_spr_labels()))
        elif kind == "mchr":
            rows.extend(enumerate(self.get_mchr_labels()))
        elif kind == "btchr":
            rows.extend(enumerate(self.get_btchr_group_labels()))
        elif kind == "digimon":
            rows.extend(
                (did, f"0x{did:03x}  {name}")
                for did, name in sorted(
                    constants.DIGIMON_ID_TO_STR.items(), key=lambda kv: kv[0]
                )
            )
        elif kind == "sprite_map":
            # Every sprite_map slot (digimon + digieggs + bosses + NPCs),
            # resolved through ``digimon_display_name`` which already
            # covers all four categories uniformly. Used by editors that
            # need to pick a sprite_map entry by id without distinguishing
            # the slot's category — e.g. dialog portrait pickers (the
            # portrait u16 indexes this same table).
            rows.extend(
                (base_id, f"0x{base_id:03x}  {self.digimon_display_name(base_id)}")
                for base_id in range(len(getattr(self, "sprite_map", [])))
            )
        elif kind == "digimon_evo":
            # Standard-digivolution evo/degen target picker: NO_EVO_SENTINEL
            # = 0xFFFFFFFF marks an empty slot, rendered as a "(none)" row at
            # the top of the dropdown. Same digimon list otherwise.
            rows.append((0xFFFFFFFF, "(none)"))
            rows.extend(
                (did, f"0x{did:03x}  {name}")
                for did, name in sorted(
                    constants.DIGIMON_ID_TO_STR.items(), key=lambda kv: kv[0]
                )
            )
        elif kind == "item_reward":
            # Encounter-reward slots: item id 0 is filtered out because the
            # slot encoding can't represent it (raw=0 means "empty slot"),
            # so a Scale reward would silently re-encode as empty.
            rows.extend(
                (iid, f"0x{iid:03x}  {name}")
                for iid, name in sorted(
                    constants.ITEM_ID_TO_STR.items(), key=lambda kv: kv[0]
                )
                if iid != 0
            )
        else:
            return None

        model = QStandardItemModel()
        for value, label in rows:
            item = QStandardItem(label)
            item.setData(value, _Qt.UserRole)
            item.setEditable(False)
            model.appendRow(item)
        self._picker_models[kind] = model
        return model

    def invalidate_picker_model(self, kind: str) -> None:
        """Drop the cached shared model for ``kind``.

        Next ``picker_model(kind)`` rebuilds. Combos currently bound to
        the old model continue showing its rows until they're
        re-constructed or explicitly rebound to the new model — fine
        for our usage since editors are torn down on every navigation.
        """
        self._picker_models.pop(kind, None)

    def _build_sprite_picker_pool(self) -> None:
        """Build the 4 pooled _SpriteListPicker widgets (mchr/btchr/spr/spr).

        Called at the end of ``_build`` so the ~185ms construction cost
        rides the ROM-load tick instead of the first editor open. Pickers
        are parented to a permanently-hidden holder widget until
        acquired; this keeps Qt's visibility/window machinery quiet and
        avoids the setParent(None)-induced top-level pop-up that would
        otherwise flash every editor swap. Silently skips when no
        QApplication is running (headless save tests).
        """
        from PySide6.QtWidgets import QApplication, QWidget
        if QApplication.instance() is None:
            return
        from .widgets.sprite_map_row import _SpriteListPicker
        self._picker_pool_holder = QWidget()
        # Holder is a top-level widget that we never show — anything
        # parented to it inherits the hidden state without acquiring
        # the "explicitly hidden" flag, so re-parenting into a visible
        # layout later just works.
        holder = self._picker_pool_holder
        self._pooled_sprite_pickers = [
            _SpriteListPicker(self.get_mchr_labels, shared_kind="mchr"),
            _SpriteListPicker(self.get_btchr_group_labels, shared_kind="btchr"),
            _SpriteListPicker(self.get_spr_labels, shared_kind="spr"),
            _SpriteListPicker(self.get_spr_labels, shared_kind="spr"),
        ]
        for picker in self._pooled_sprite_pickers:
            picker.setParent(holder)

    def acquire_sprite_pickers(self) -> Tuple[List[object], int]:
        """Hand the pooled pickers to the active SpriteMapRow.

        Returns ``(pickers, generation)``: the 4 pickers in fixed order
        ([mchr, btchr, spr_low, spr_high]) and an ownership token. Caller
        must pass that token back to ``release_sprite_pickers`` from its
        teardown hook — only the *current* owner's release reparks the
        pickers; stale releases (from an editor that's already been
        succeeded by a new SpriteMapRow) no-op.
        """
        if self._pooled_sprite_pickers is None:
            # Defensive: pool wasn't built (e.g. session built outside _build).
            self._build_sprite_picker_pool()
        self._picker_pool_generation += 1
        return list(self._pooled_sprite_pickers or []), self._picker_pool_generation

    def _build_combo_pools(self) -> None:
        """Build the session-shared BoundIdCombo / BoundIdComboRow pools.

        Currently just "moves" — 5 ``BoundIdComboRow`` widgets shared
        between base + enemy digimon editors. Each row pays ~9ms in
        ``setEditable`` + ``QCompleter`` setup; pooling collapses
        5 × 9ms per editor open to a one-shot ROM-load cost. Seeds
        them against ``base_digimon``'s first entry (any object with
        ``move_signature, move_1..move_4`` works; the host editor
        overrides via ``rebind`` on acquire).
        """
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            return
        if not self.base_digimon:
            # Defensive: no entries to seed with (partial/test sessions).
            return
        # BoundIdCombo's shared-model path reads from form_helpers'
        # _SESSION_DATA registry, but main_window doesn't call
        # set_details_providers until *after* _build returns. Register
        # the picker_model lookup eagerly so the pool widgets pick up
        # the shared "moves" model (skipping per-instance addItem +
        # _apply_item_tooltips O(rows) work). main_window's later
        # set_details_providers call is idempotent — clears and
        # re-registers the same provider.
        from .widgets import form_helpers
        form_helpers._SESSION_DATA["picker_model"] = self.picker_model
        from .widgets.form_helpers import BoundIdComboRow, move_choices
        seed = next(iter(self.base_digimon.values()))
        move_attrs = ["move_signature", "move_1", "move_2", "move_3", "move_4"]
        holder = self._picker_pool_holder
        rows: List[object] = []
        for attr in move_attrs:
            row = BoundIdComboRow(
                seed, attr, move_choices(), undo_stack=None,
                details_kind="move",
                none_value=0xFFFF, none_label="(none)",
                shared_kind="moves",
            )
            row.setParent(holder)
            rows.append(row)
        self._combo_pools["moves"] = rows
        self._combo_pool_generations["moves"] = 0

    def acquire_combo_pool(self, kind: str) -> Tuple[List[object], int]:
        """Hand the named combo pool to the active editor.

        Same ownership-token contract as ``acquire_sprite_pickers``: each
        acquire bumps the generation for ``kind``; the host editor passes
        that token back to ``release_combo_pool`` from its
        ``aboutToTeardown`` so a stale release (when a newer editor has
        already taken the pool) no-ops.
        """
        pool = self._combo_pools.get(kind)
        if pool is None:
            return [], 0
        self._combo_pool_generations[kind] = self._combo_pool_generations.get(kind, 0) + 1
        return list(pool), self._combo_pool_generations[kind]

    def release_combo_pool(self, kind: str, generation: int) -> None:
        """Park the named combo pool back on the hidden holder."""
        if generation != self._combo_pool_generations.get(kind, 0):
            return
        pool = self._combo_pools.get(kind)
        if pool is None or self._picker_pool_holder is None:
            return
        holder = self._picker_pool_holder
        for widget in pool:
            try:
                widget.setParent(holder)
            except RuntimeError:
                pass

    def release_sprite_pickers(self, generation: int) -> None:
        """Park pooled pickers back on the hidden holder before teardown.

        Reparenting to the holder (rather than ``None``) keeps the
        widgets out of top-level state — setParent(None) would make
        each picker a hidden top-level window that flashes briefly on
        screen during the swap. No-ops when ``generation`` is stale: a
        newer SpriteMapRow has already acquired the pool, so the calling
        editor's teardown must not steal the pickers back.
        """
        if generation != self._picker_pool_generation:
            return
        if self._pooled_sprite_pickers is None or self._picker_pool_holder is None:
            return
        holder = self._picker_pool_holder
        for picker in self._pooled_sprite_pickers:
            try:
                picker.setParent(holder)
            except RuntimeError:
                # Widget already destroyed (e.g. session being torn down).
                pass

    def _build_digimon_name_cache(self) -> None:
        cache: Dict[int, str] = {}
        base = constants.STRING_BATTLE_TABLE_OFFSET[self.version][0]
        region = self.string_regions.get("arm9_digiegg_enemy_names", [])
        addr_to_text = {g.offset: g.text for g in region}
        for i, entry in enumerate(self.battle_strings):
            named = constants.DIGIMON_ID_TO_STR.get(i)
            if named is not None:
                cache[i] = named
                continue
            text = addr_to_text.get(base + entry.value)
            if text:
                cache[i] = text
        # Cover ids past the battle-string table too (rare, but cheap).
        for did, named in constants.DIGIMON_ID_TO_STR.items():
            cache.setdefault(did, named)
        self._digimon_name_cache = cache
        self._digimon_name_cache_valid = True

    def over_budget_strings(self) -> List[model.GameString]:
        """Non-MSG.PAK strings whose encoded length exceeds their byte budget.

        ARM9 / overlay string regions have pointers hardcoded in code; an
        over-budget write would clobber the next field on disk. MSG.PAK
        strings, by contrast, sit in a pak whose entries can be resized
        in-place (§12), so they're excluded here — the grow path handles
        them at save time without a user-visible gate.
        """
        bad: List[model.GameString] = []
        for region_id, region_strings in self.string_regions.items():
            if region_id.startswith("msgpak_"):
                continue
            for s in region_strings:
                if not s.fits():
                    bad.append(s)
        return bad

    def over_cap_msgpak_strings(self) -> List[model.GameString]:
        """MSG.PAK strings whose encoded size exceeds the empirical 1024-byte
        engine cap.

        Vanilla strings top out at ~1004 bytes; past 1024 the DWDD textbox
        renderer corrupts the box regardless of content. The cap is per
        *string* (per dialogue textbox), not per pak entry — vanilla entries
        routinely run tens of KB across many strings. The save gate refuses
        to write while any string crosses 1024.
        """
        bad: List[model.GameString] = []
        for region_id, region_strings in self.string_regions.items():
            if not region_id.startswith("msgpak_"):
                continue
            for s in region_strings:
                if len(s.encoded_bytes_for_grow()) > model.MSGPAK_STRING_CAP:
                    bad.append(s)
        return bad

    def msgpak_string_edits(self) -> List[Tuple[str, int, str]]:
        """Snapshot every edited MSG.PAK string for the .romproj string_edits
        channel.

        ``serialize_all`` no longer writes any MSG.PAK string in-place — the
        in-place writer pads slots with `\x00` and that padding leaks into
        the next string in the group as `@SP@`. The §12 resize path rebuilds
        affected entries on ROM save, but project save returns the
        vanilla-MSG.PAK pre-resize buffer, so MSG.PAK edits don't appear in
        the byte diff at all. The string_edits channel carries them as
        logical edits for Open Project to replay after reparse.
        """
        out: List[Tuple[str, int, str]] = []
        for region_id, region_strings in self.string_regions.items():
            if not region_id.startswith("msgpak_"):
                continue
            for s in region_strings:
                if s.text != s._initial_text:
                    out.append((region_id, s.offset, s.text))
        return out

    def sprite_pak_edits(self) -> List[Tuple[str, int, bytes]]:
        """Snapshot per-entry sprite edits for the .romproj sprite_edits channel.

        For each dirty pak, compares every entry against a freshly-parsed
        vanilla PakFile (from ``original_rom_data``) and emits one tuple
        per differing slot. Comparing against the in-memory pak's own
        ``original_entry`` would be wrong for project-loaded sessions
        because the cached PakFile was parsed from patched_data (project
        state), not vanilla.

        Entries past ``vanilla.count`` are emitted in order so the load
        side can append them sequentially (each one extends the pak by
        one entry). For BTCHR.PAK these ride alongside the
        ``btchr_appended_sidecars`` channel (chrsize + btchrsize words).
        """
        out: List[Tuple[str, int, bytes]] = []
        if not self._dirty_sprite_paks:
            return out
        file_table = fnt.FileTable.from_rom(self.original_rom_data)
        for pak_name in sorted(self._dirty_sprite_paks):
            edited = self._sprite_pak_cache.get(pak_name)
            if edited is None:
                continue
            try:
                v_start, v_end = file_table.resolve(pak_name)
            except KeyError:
                continue
            vanilla = pak.PakFile(self.original_rom_data[v_start:v_end])
            n = min(edited.count, vanilla.count)
            for i in range(n):
                v_bytes = vanilla.original_entry(i)
                if edited.entries[i] != v_bytes:
                    out.append((pak_name, i, bytes(edited.entries[i])))
            for i in range(vanilla.count, edited.count):
                out.append((pak_name, i, bytes(edited.entries[i])))
        return out

    def btchr_appended_sidecars(self) -> List[Tuple[int, int]]:
        """Snapshot per-group sidecar values for appended BTCHR groups.

        Each tuple is ``(chrsize_word, btchrsize_value)`` parallel to
        the appended PAK entries. Used by .romproj save so the project
        load can restore the full (PAK count, chrsize length, btchrsize
        length) triple without depending on the byte diff (which is
        skipped on save by ``skip_sprite_splice=True``).
        """
        return list(zip(
            self._btchr_appended_chrsize,
            self._btchr_appended_btchrsize,
        ))

    def apply_btchr_appended_sidecars(
        self, sidecars: List[Tuple[int, int]],
    ) -> None:
        """Replay ``btchr_appended_sidecars`` snapshots onto the session.

        Pairs are stored verbatim; the caller (project load) must also
        replay the corresponding 5-entry PAK appends so the triple stays
        consistent. Idempotent: replays into an empty list (project
        load runs once on a fresh session)."""
        for chrsize_word, btchrsize_value in sidecars:
            self.append_btchr_group_sidecars(chrsize_word, btchrsize_value)

    def apply_sprite_pak_edits(
        self, edits: List[Tuple[str, int, bytes]],
    ) -> None:
        """Replay sprite-entry edits from a .romproj onto the in-memory paks.

        Each tuple is ``(pak_name, entry_idx, new_compressed_bytes)``.
        Marks each touched pak dirty so the next ``serialize_all`` runs
        the splice + FAT-shift path. Raises ``KeyError`` on unknown pak
        name (signals project/ROM version drift) and ``IndexError`` on
        out-of-range ``entry_idx``.

        ``entry_idx == pak_obj.count`` triggers an append (extending
        the pak by one entry). Higher gaps raise — appended entries
        must arrive in order so each ``count`` value the previous
        append produced matches the next ``entry_idx``.
        """
        for pak_name, idx, new_bytes in edits:
            pak_obj = self.sprite_pak(pak_name)
            if idx == pak_obj.count:
                pak_obj.entries.append(bytes(new_bytes))
                pak_obj.flags.append(0x80000000)
                pak_obj.count += 1
            elif 0 <= idx < pak_obj.count:
                pak_obj.replace_entry(idx, new_bytes)
            else:
                raise IndexError(
                    f"sprite_edits: entry {idx} out of range for "
                    f"{pak_name} (count={pak_obj.count})"
                )
            self.mark_sprite_pak_dirty(pak_name)

    def btmap_file_edits(self) -> List[Tuple[str, bytes]]:
        """Snapshot per-path btmap overrides for the .romproj btmap_edits channel.

        One tuple per dirty FAT path; the bytes are whatever the dirty
        cache currently holds (uncompressed from the import path, possibly
        already-compressed from external callers — _apply_btmap_splice
        normalizes either form on save).

        Mirrors :meth:`sprite_pak_edits`: project save skips the FAT
        splice and routes the actual file bytes through this channel
        so a grown btmap doesn't shift every later file into the byte
        diff. ``apply_btmap_file_edits`` replays them at load.
        """
        return [
            (path, bytes(data))
            for path, data in sorted(self._dirty_btmap_files.items())
        ]

    def apply_btmap_file_edits(self, edits: List[Tuple[str, bytes]]) -> None:
        """Replay btmap file overrides from a .romproj onto the dirty cache.

        Each tuple is ``(fat_path, file_bytes)``. The bytes go straight
        into ``_dirty_btmap_files`` so the next ``serialize_all`` runs
        them through ``_apply_btmap_splice``. Idempotent — project load
        runs once on a fresh session.
        """
        for path, data in edits:
            self._dirty_btmap_files[path] = bytes(data)

    def map_file_edits(self) -> List[Tuple[str, bytes]]:
        """Snapshot per-path field-map overrides for the .romproj map_edits
        channel.

        Mirrors :meth:`btmap_file_edits`. Project save skips the field-map
        FAT splice (``skip_map_splice=True``) and routes the dirty bytes
        through this channel so a grown ``.s`` / ``.0t`` / ``.d`` doesn't
        shift every later file into the byte diff.
        """
        return [
            (path, bytes(data))
            for path, data in sorted(self._dirty_map_files.items())
        ]

    def apply_map_file_edits(self, edits: List[Tuple[str, bytes]]) -> None:
        """Replay field-map file overrides from a .romproj onto the dirty cache.

        Each tuple is ``(fat_path, file_bytes)``. The bytes go straight
        into ``_dirty_map_files`` so the next ``serialize_all`` runs them
        through ``_apply_map_splice``. Idempotent.
        """
        for path, data in edits:
            self._dirty_map_files[path] = bytes(data)

    def overlay5_entry_edits(self) -> List[Tuple[int, bytes]]:
        """Snapshot per-entry overlay5 overrides for the .romproj
        ``overlay5_edits`` channel.

        Mirrors :meth:`map_file_edits` shape: one tuple per dirty
        entry, sorted by entry index for stable on-disk output.
        Project save runs with ``skip_overlay5_splice=True`` and routes
        the dirty bytes through this channel; on load
        :meth:`apply_overlay5_entry_edits` re-populates the dirty cache
        and the next ``serialize_all`` splices them back in.
        """
        return [
            (entry_ix, bytes(data))
            for entry_ix, data in sorted(self._dirty_overlay5_entries.items())
        ]

    def apply_overlay5_entry_edits(
        self, edits: List[Tuple[int, bytes]],
    ) -> None:
        """Replay overlay5 entry overrides from a .romproj onto the
        dirty cache. Same-length invariant is enforced — a project
        carrying a length-mismatched edit would corrupt the overlay
        on save, so fail loudly during load instead."""
        for entry_ix, data in edits:
            vanilla = self.overlay5_index().read_entry(entry_ix)
            if len(data) != len(vanilla):
                raise ValueError(
                    f"overlay5 entry {entry_ix:04d} project edit length "
                    f"mismatch: {len(data)} vs {len(vanilla)}"
                )
            self._dirty_overlay5_entries[entry_ix] = bytes(data)

    def apply_string_edits(self, edits: List[Tuple[str, int, str]]) -> None:
        """Replay logical string edits from a .romproj onto the parsed model.

        Each tuple is ``(region_id, vanilla_offset, new_text)``. The byte
        diff has already restored in-budget edits via reparse; this fills in
        the over-budget MSG.PAK strings the byte diff couldn't carry. Raises
        ``KeyError`` if a region is gone or no string sits at ``offset``
        (signals project/ROM version drift, not a recoverable state).
        """
        for region_id, off, text in edits:
            region = self.string_regions.get(region_id)
            if region is None:
                raise KeyError(f"region {region_id!r} not present in this ROM")
            match = next((s for s in region if s.offset == off), None)
            if match is None:
                raise KeyError(
                    f"no string at offset 0x{off:x} in region {region_id!r}"
                )
            match.text = text

    # --- §12 save-path helpers ---------------------------------------------

    def _apply_msgpak_resize(self, out: bytearray) -> None:
        """Rebuild MSG.PAK entries that contain *edited* strings (grow or
        shrink), then splice the new MSG.PAK over its original FAT range.

        Triggered for every edit, not just over-budget ones. In-place
        ``writeToRom`` would pad shrunk slots with `\x00`, and `00 00`
        decodes to `@SP@` — within a MSG.PAK group there's no per-string
        offset, so the engine reads that padding as leading spaces on the
        next string. Rebuilding the affected entry via
        ``encoded_bytes_for_grow`` (no padding, original terminator) keeps
        the next string's bytes flush against the previous string's
        terminator. ``fat.resize_fat_entry`` then ripples the net size
        delta through the FAT, which may be zero (pure rename), positive
        (grow), or negative (shrink).
        """
        msgpak_strings: List[model.GameString] = []
        for rid, ss in self.string_regions.items():
            if rid.startswith("msgpak_"):
                msgpak_strings.extend(ss)
        if not msgpak_strings:
            return
        if all(s.text == s._initial_text for s in msgpak_strings):
            return

        file_table = fnt.FileTable.from_rom(bytes(out))
        pak_start, pak_end = file_table.resolve("DAT/MSG.PAK")
        msgpak_bytes = bytes(out[pak_start:pak_end])
        p = pak.PakFile(msgpak_bytes)

        # entry_idx -> set of group_idx that need a fresh rebuild
        affected: Dict[int, set] = {}
        # For each edited string, locate (entry, group) inside MSG.PAK.
        # msgpak_all spans the whole file, so a small number of "strings"
        # land in pak entry sub-headers or in the dev-tail past the last
        # pak entry — those have no group to rebuild and edits to them
        # can't be persisted via the resize path; skip silently.
        for s in msgpak_strings:
            if s.text == s._initial_text:
                continue
            file_off = s.offset - pak_start
            try:
                entry_idx = p.entry_index_at(file_off)
            except ValueError:
                continue
            entry_start_in_pak, _ = p.original_entry_range(entry_idx)
            off_in_entry = file_off - entry_start_in_pak
            entry_bytes = p.original_entry(entry_idx)
            groups = msgpak.parse_entry_groups(entry_bytes)
            g_idx = next(
                (i for i, (gs, ge) in enumerate(groups)
                 if gs <= off_in_entry < ge),
                None,
            )
            if g_idx is None:
                continue
            affected.setdefault(entry_idx, set()).add(g_idx)

        # Build a fast lookup: absolute ROM offset -> GameString.
        by_offset = {s.offset: s for s in msgpak_strings}

        for entry_idx, group_idxs in affected.items():
            entry_bytes = p.original_entry(entry_idx)
            entry_start_in_pak, _ = p.original_entry_range(entry_idx)
            groups = msgpak.parse_entry_groups(entry_bytes)
            payloads: List[bytes] = []
            for g_idx, (gs, ge) in enumerate(groups):
                if g_idx not in group_idxs:
                    payloads.append(entry_bytes[gs:ge])
                    continue
                # Re-encode every string in this group from its model.
                group_start_abs = pak_start + entry_start_in_pak + gs
                group_end_abs = pak_start + entry_start_in_pak + ge
                new_payload = bytearray()
                for off in range(group_start_abs, group_end_abs):
                    s = by_offset.get(off)
                    if s is None:
                        continue
                    new_payload.extend(s.encoded_bytes_for_grow())
                payloads.append(bytes(new_payload))
            p.replace_entry(entry_idx, msgpak.rebuild_entry(payloads))

        new_pak_bytes = p.to_bytes()
        idx, _cs, ce = fat.find_container(out, pak_start, pak_end)
        content_delta = len(new_pak_bytes) - (pak_end - pak_start)
        aligned_shift = fat.splice_range(
            out, pak_start, pak_end, ce, new_pak_bytes
        )
        fat.resize_fat_entry(out, idx, ce, content_delta, aligned_shift)

    # Vanilla DWDD cart size. Saves pad/trim back to this whenever content
    # genuinely fits, so the ROM image matches the physical cart layout that
    # flashcarts and emulators expect.
    _VANILLA_ROM_SIZE = 0x4000000

    def _trim_trailing_padding(self, out: bytearray) -> None:
        """Restore the ROM to its vanilla 0x4000000-byte cart size.

        Vanilla DWDD ships as a 64 MiB cart with ~6.5 MB of trailing 0xFF
        padding between the last used byte (``header[0x80]``) and EOF.
        After model edits + the §12 resize the buffer may be shorter (pure
        shrink dropped trailing padding) or longer (a grow pushed bytes
        past 0x4000000). Either way we want the on-disk image to look like
        the vanilla cart whenever content fits — pad up with 0xFF when
        short, trim back when long, and only let the file genuinely exceed
        0x4000000 when ``header[0x80]`` itself crosses that line.

        Safety: any byte we'd trim away or write 0xFF over must already be
        0xFF — otherwise some writer extended the ROM past its FAT bound
        with real data and we'd silently drop it. Bail loudly in that case.
        """
        import struct as _struct
        used = _struct.unpack_from("<I", out, 0x80)[0]
        target = max(self._VANILLA_ROM_SIZE, used)
        if len(out) > target:
            tail = out[target:]
            if any(b != 0xFF for b in tail):
                raise RuntimeError(
                    f"refusing to trim: non-0xFF bytes between target=0x{target:08x} "
                    f"and EOF=0x{len(out):08x} — a writer extended the ROM past max(FAT.end)"
                )
            del out[target:]
        elif len(out) < target:
            out.extend(b"\xff" * (target - len(out)))
        # Region between header[0x80] and target must be 0xFF (it's the
        # cart-padding the engine expects). Sanity-check rather than
        # blindly overwrite — anything else here is a writer bug.
        if used < target:
            gap = out[used:target]
            if any(b != 0xFF for b in gap):
                raise RuntimeError(
                    f"refusing to pad: non-0xFF bytes between header[0x80]=0x{used:08x} "
                    f"and target=0x{target:08x} — a writer extended the ROM past max(FAT.end)"
                )

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
