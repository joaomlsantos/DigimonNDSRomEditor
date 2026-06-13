"""QUndoCommand subclasses for editor mutations.

All edits to the parsed model graph should go through a Command so that the
QUndoStack can drive Ctrl+Z / Ctrl+Y. `SetAttrCommand` covers the common case
of changing a single scalar field on a model object; more specialized commands
(list insert/delete/reorder) will be added as the UI grows.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from PySide6.QtGui import QUndoCommand

from digimon_core import btchr, btchrspr


# All SetAttrCommands share one id so the QUndoStack will *attempt* to merge
# consecutive pushes; mergeWith() then rejects the merge unless (target, attr)
# match. Effect: rapid edits to the same field collapse into one undo step,
# while switching to a different field starts a fresh entry.
SET_ATTR_COMMAND_ID = 0x5E7A  # "SETA"


class SetAttrCommand(QUndoCommand):
    """Set `target.attr = new_value`, remembering the old value for undo."""

    def __init__(
        self,
        target: Any,
        attr: str,
        new_value: Any,
        description: Optional[str] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description or f"Edit {type(target).__name__}.{attr}")
        self._target = target
        self._attr = attr
        self._new_value = new_value
        self._old_value = getattr(target, attr)
        self._on_change = on_change

    def id(self) -> int:  # noqa: A003 — required Qt override name
        return SET_ATTR_COMMAND_ID

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetAttrCommand):
            return False
        if other._target is not self._target or other._attr != self._attr:
            return False
        # Keep our original old_value; absorb the newer new_value.
        self._new_value = other._new_value
        return True

    def redo(self) -> None:
        setattr(self._target, self._attr, self._new_value)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        setattr(self._target, self._attr, self._old_value)
        if self._on_change is not None:
            self._on_change()


class ReskinSlotCommand(QUndoCommand):
    """Atomic "Displayed As" reskin of a sprite_map slot.

    Copies unknown_0x4 (party-follower overworld) + main_sprite +
    upperscreen_sprites from a source SpriteMapEntry and the battle-string
    value from a source BattleStringEntry into the target slot. Undo
    restores the previous four values in one step so a single Ctrl+Z
    reverts the full reskin.

    Used by both base and enemy digimon editors — `sprite_map` is one flat
    table keyed by digimon_id, so the same operation applies in either
    context (the blast radius is encoded in the id itself, not the editor).
    """

    def __init__(
        self,
        sprite_entry: Any,
        str_entry: Any,
        new_overworld: int,
        new_main_sprite: int,
        new_upperscreen: int,
        new_str_value: int,
        description: Optional[str] = None,
    ):
        super().__init__(description or f"Reskin slot 0x{sprite_entry.id:03x}")
        self._sprite_entry = sprite_entry
        self._str_entry = str_entry
        self._old_overworld = sprite_entry.unknown_0x4
        self._old_main = sprite_entry.main_sprite
        self._old_upper = sprite_entry.upperscreen_sprites
        self._old_str = str_entry.value
        self._new_overworld = new_overworld
        self._new_main = new_main_sprite
        self._new_upper = new_upperscreen
        self._new_str = new_str_value

    def redo(self) -> None:
        self._sprite_entry.unknown_0x4 = self._new_overworld
        self._sprite_entry.main_sprite = self._new_main
        self._sprite_entry.upperscreen_sprites = self._new_upper
        self._str_entry.value = self._new_str

    def undo(self) -> None:
        self._sprite_entry.unknown_0x4 = self._old_overworld
        self._sprite_entry.main_sprite = self._old_main
        self._sprite_entry.upperscreen_sprites = self._old_upper
        self._str_entry.value = self._old_str


class ReplaceSpriteCommand(QUndoCommand):
    """Atomic replace of one or more SPR_*.PAK entries.

    A sprite import touches one (CHR-only PNG path) or two (CHR + PAL
    NCGR+NCLR path) pak entries that must succeed or roll back together —
    a half-applied CHR with a stale palette would render garbage. Each
    ``(pak_name, entry_idx, new_bytes)`` tuple captures the pre-mutation
    bytes from the live PakFile so redo/undo flip them all atomically.

    Marks each touched pak dirty on redo so :meth:`RomSession.serialize_all`
    knows to splice it back onto the ROM at save time. ``on_change`` is
    invoked after every flip so the browser can re-render its preview.
    """

    def __init__(
        self,
        session: Any,
        replacements: List[Tuple[str, int, bytes]],
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._on_change = on_change
        # Snapshot in __init__ (before push() triggers redo) so old_bytes
        # reflects the live state — could be vanilla or a prior edit.
        self._ops: List[Tuple[str, int, bytes, bytes]] = []
        for pak_name, idx, new_bytes in replacements:
            pak_obj = session.sprite_pak(pak_name)
            old_bytes = pak_obj.entries[idx]
            self._ops.append((pak_name, idx, old_bytes, bytes(new_bytes)))

    def redo(self) -> None:
        for pak_name, idx, _old, new in self._ops:
            self._session.sprite_pak(pak_name).replace_entry(idx, new)
            self._session.mark_sprite_pak_dirty(pak_name)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        for pak_name, idx, old, _new in self._ops:
            self._session.sprite_pak(pak_name).replace_entry(idx, old)
            # Keep dirty flag set — undo to vanilla still means the pak's
            # serialization may differ from a fresh PakFile, and the splice
            # is cheap (a no-op when bytes match).
            self._session.mark_sprite_pak_dirty(pak_name)
        if self._on_change is not None:
            self._on_change()


class ReplaceBtmapFileCommand(QUndoCommand):
    """Atomic swap of one ``DAT/btmap/*`` FAT file's bytes.

    Records the bytes that were live when ``__init__`` ran so redo/undo
    flip between the new content and whatever existed before — vanilla
    FAT bytes or a prior edit. ``on_change`` is invoked after each flip
    so the browser can drop its parsed NaXn cache and re-render.
    """

    def __init__(
        self,
        session: Any,
        path: str,
        new_bytes: bytes,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._path = path
        self._on_change = on_change
        # Snapshot before push() so old_bytes reflects the live state.
        self._old_bytes = bytes(session.btmap_file_bytes(path))
        self._new_bytes = bytes(new_bytes)

    def redo(self) -> None:
        self._session.replace_btmap_file_bytes(self._path, self._new_bytes)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._session.replace_btmap_file_bytes(self._path, self._old_bytes)
        if self._on_change is not None:
            self._on_change()


class ReplaceMapFileCommand(QUndoCommand):
    """Atomic swap of one ``DAT/map/*`` FAT file's bytes.

    Same shape as :class:`ReplaceBtmapFileCommand` — used by the field-
    map paint tools (``.0t`` walkability in Phase C, the tilemap
    painter in Phase D). ``on_change`` lets the browser re-render after
    each redo/undo flip.
    """

    def __init__(
        self,
        session: Any,
        path: str,
        new_bytes: bytes,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._path = path
        self._on_change = on_change
        self._old_bytes = bytes(session.map_file_bytes(path))
        self._new_bytes = bytes(new_bytes)

    def redo(self) -> None:
        self._session.replace_map_file_bytes(self._path, self._new_bytes)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._session.replace_map_file_bytes(self._path, self._old_bytes)
        if self._on_change is not None:
            self._on_change()


# FAT path for BTCHR.PAK — duplicated here (also defined in btchr_browser)
# so commands.py doesn't reach back into the UI layer.
_BTCHR_PAK = "DAT/BTCHR.PAK"


class PortBtchrSpriteCommand(QUndoCommand):
    """Atomic port of one digimon's BTCHR sprite kit into another's slot.

    A port touches three FAT files at once:

    - **BTCHR.PAK** — 5 entries at ``target_group * 5`` get the source's
      (header, NCGR, NCLR, NCER, NANR). Rides the sprite splice path.
    - **BTCHR/CHRSIZE.BIN** — high u16 of the target's slot becomes the
      source's tpf so the engine's VRAM budget matches the new sprite's
      tile count. Low u16 (the slot's secondary digimon id) is preserved.
    - **BTCHR/BTCHRSIZE.BIN** — target's u32 is replaced with the source's
      uncompressed entry-sum so load-time allocation matches.

    All five entries plus both sidecar slots get snapshotted in
    ``__init__`` (before push() triggers redo), so a single Ctrl+Z
    reverts the whole port — including the case where the target slot
    was already an earlier port (the prior port's bytes are restored,
    not vanilla).
    """

    def __init__(
        self,
        session: Any,
        target_group: int,
        spr: btchrspr.BtchrSprite,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._target_group = target_group
        self._on_change = on_change

        base = target_group * btchr.GROUP_SIZE
        pak_obj = session.sprite_pak(_BTCHR_PAK)
        self._entry_ops: List[Tuple[int, bytes, bytes]] = []
        for i in range(btchrspr.ENTRY_COUNT):
            idx = base + i
            old_bytes = pak_obj.entries[idx]
            self._entry_ops.append((idx, old_bytes, bytes(spr.entries[i])))

        # Preserve the target slot's secondary digimon id (low u16) — the
        # slot keeps its identity in the other systems (sprite map, etc.).
        # Only the tpf (high u16) changes to match the imported sprite.
        old_chrsize_word = session.current_chrsize_word(target_group)
        tgt_id = old_chrsize_word & 0xFFFF
        new_chrsize_word = (tgt_id & 0xFFFF) | ((spr.source_tpf & 0xFFFF) << 16)
        old_btchrsize = session.current_btchrsize_value(target_group)
        self._old_chrsize = old_chrsize_word
        self._new_chrsize = new_chrsize_word
        self._old_btchrsize = old_btchrsize
        self._new_btchrsize = spr.btchrsize_value

    def redo(self) -> None:
        pak_obj = self._session.sprite_pak(_BTCHR_PAK)
        for idx, _old, new in self._entry_ops:
            pak_obj.replace_entry(idx, new)
        self._session.mark_sprite_pak_dirty(_BTCHR_PAK)
        self._session.set_chrsize_word(self._target_group, self._new_chrsize)
        self._session.set_btchrsize_value(self._target_group, self._new_btchrsize)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        pak_obj = self._session.sprite_pak(_BTCHR_PAK)
        for idx, old, _new in self._entry_ops:
            pak_obj.replace_entry(idx, old)
        self._session.mark_sprite_pak_dirty(_BTCHR_PAK)
        self._session.set_chrsize_word(self._target_group, self._old_chrsize)
        self._session.set_btchrsize_value(self._target_group, self._old_btchrsize)
        if self._on_change is not None:
            self._on_change()


class AppendBtchrGroupCommand(QUndoCommand):
    """Atomic append of a new BTCHR group cloned from an existing one.

    Extends three FAT files in lockstep — the same triple a vanilla
    group occupies (project memory ``project_btchr_extensible``):

    - **BTCHR.PAK** — five new entries (header, NCGR, NCLR, NCER, NANR)
      appended past the current count. Flag word matches vanilla
      (``0x80000000``) for every entry.
    - **BTCHR/CHRSIZE.BIN** — one u32 appended carrying the source
      group's ``(id | tpf << 16)``. The id remains the source's; the
      user can edit it later via the header-field editor once we expose
      it. (No engine path is known to read it as a lookup key — see
      project_btchr_extensible.)
    - **BTCHR/BTCHRSIZE.BIN** — one u32 appended carrying the source
      group's uncompressed body sum.

    Bytes are snapshotted in ``__init__`` (before push() triggers redo)
    so a single Ctrl+Z drops the whole append, including the case where
    the source was itself an earlier-edited slot.
    """

    def __init__(
        self,
        session: Any,
        source_group: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._on_change = on_change

        pak_obj = session.sprite_pak(_BTCHR_PAK)
        base = source_group * btchr.GROUP_SIZE
        self._entry_bytes: List[bytes] = [
            bytes(pak_obj.entries[base + k]) for k in range(btchr.GROUP_SIZE)
        ]
        self._entry_flags: List[int] = [
            pak_obj.flags[base + k] for k in range(btchr.GROUP_SIZE)
        ]
        self._chrsize_word = session.current_chrsize_word(source_group)
        self._btchrsize_value = session.current_btchrsize_value(source_group)
        # Captured at construction so undo restores the exact pre-append
        # count even if a later edit changed something past that point.
        self._pre_count = pak_obj.count
        self._new_group_index = self._pre_count // btchr.GROUP_SIZE

    @property
    def new_group_index(self) -> int:
        """0-based BTCHR group index the append produces. Useful for the
        caller to select the new entry in the list after push()."""
        return self._new_group_index

    def redo(self) -> None:
        pak_obj = self._session.sprite_pak(_BTCHR_PAK)
        for data, flag in zip(self._entry_bytes, self._entry_flags):
            pak_obj.entries.append(data)
            pak_obj.flags.append(flag)
        pak_obj.count += btchr.GROUP_SIZE
        self._session.mark_sprite_pak_dirty(_BTCHR_PAK)
        self._session.append_btchr_group_sidecars(
            self._chrsize_word, self._btchrsize_value,
        )
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        pak_obj = self._session.sprite_pak(_BTCHR_PAK)
        for _ in range(btchr.GROUP_SIZE):
            pak_obj.entries.pop()
            pak_obj.flags.pop()
        pak_obj.count -= btchr.GROUP_SIZE
        self._session.mark_sprite_pak_dirty(_BTCHR_PAK)
        self._session.pop_btchr_group_sidecars()
        if self._on_change is not None:
            self._on_change()


class AppendPakEntriesCommand(QUndoCommand):
    """Atomic append of one entry to each of several strict-parallel paks.

    Use for groups of FAT files that share an index — e.g. the SPR_*
    trio/quad (``SPR_CHR``/``SPR_PAL``/``SPR_CEL``/``SPR_ANM``), all
    1627 entries paired by index. "Duplicate entry N" means appending
    a copy of entry N from every named pak in lockstep so the new
    entry has its own bytes in each parallel pak.

    Source bytes + flags are snapshotted in ``__init__`` (before push()
    triggers redo) so a single Ctrl+Z drops the whole append, even when
    the source slot was itself an earlier-edited entry.

    No sidecar coordination — assumes the involved paks are pure
    parallel arrays with no external lookup table keyed by count. BTCHR
    is NOT one of these (use :class:`AppendBtchrGroupCommand` for that).
    """

    def __init__(
        self,
        session: Any,
        pak_names: List[str],
        source_idx: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._pak_names = list(pak_names)
        self._on_change = on_change

        # Snapshot bytes + flag for each parallel pak. Captured before push()
        # so undo can restore the exact source values even if the source slot
        # later changes via another edit.
        self._snapshots: List[Tuple[str, bytes, int]] = []
        for pak_name in self._pak_names:
            pak_obj = session.sprite_pak(pak_name)
            self._snapshots.append((
                pak_name,
                bytes(pak_obj.entries[source_idx]),
                pak_obj.flags[source_idx],
            ))
        # New index is the same across every parallel pak (precondition: they
        # all have equal counts at construction time).
        self._new_entry_index = session.sprite_pak(self._pak_names[0]).count

    @property
    def new_entry_index(self) -> int:
        """0-based index the append lands at — useful for the caller to
        select the new row after push()."""
        return self._new_entry_index

    def redo(self) -> None:
        for pak_name, data, flag in self._snapshots:
            pak_obj = self._session.sprite_pak(pak_name)
            pak_obj.entries.append(data)
            pak_obj.flags.append(flag)
            pak_obj.count += 1
            self._session.mark_sprite_pak_dirty(pak_name)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        for pak_name, _data, _flag in self._snapshots:
            pak_obj = self._session.sprite_pak(pak_name)
            pak_obj.entries.pop()
            pak_obj.flags.pop()
            pak_obj.count -= 1
            self._session.mark_sprite_pak_dirty(pak_name)
        if self._on_change is not None:
            self._on_change()
