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
from typing import Dict, List, Optional, Set, Tuple

from digimon_core import constants, fat, fnt, loaders, model, msgpak, pak, qol as qol_module, rom


# Every sprite trio the editor knows how to splice on save. Used by
# from_project to pre-load any pak that diverges from vanilla (so a
# project's sprite edits survive a re-save even if the user never opens
# the sprite browser) and by _apply_sprite_pak_splice to iterate.
SPRITE_PAK_PATHS = ("DAT/SPR_CHR.PAK", "DAT/SPR_PAL.PAK", "DAT/SPR_CEL.PAK")


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
        return session

    def serialize_all(self, *, skip_sprite_splice: bool = False) -> bytearray:
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
        return out

    def apply_sprite_pak_edits(
        self, edits: List[Tuple[str, int, bytes]],
    ) -> None:
        """Replay sprite-entry edits from a .romproj onto the in-memory paks.

        Each tuple is ``(pak_name, entry_idx, new_compressed_bytes)``.
        Marks each touched pak dirty so the next ``serialize_all`` runs
        the splice + FAT-shift path. Raises ``KeyError`` on unknown pak
        name (signals project/ROM version drift) and ``IndexError`` on
        out-of-range ``entry_idx``.
        """
        for pak_name, idx, new_bytes in edits:
            pak_obj = self.sprite_pak(pak_name)
            if not (0 <= idx < pak_obj.count):
                raise IndexError(
                    f"sprite_edits: entry {idx} out of range for "
                    f"{pak_name} (count={pak_obj.count})"
                )
            pak_obj.replace_entry(idx, new_bytes)
            self.mark_sprite_pak_dirty(pak_name)

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
