"""QUndoCommand subclasses for editor mutations.

All edits to the parsed model graph should go through a Command so that the
QUndoStack can drive Ctrl+Z / Ctrl+Y. `SetAttrCommand` covers the common case
of changing a single scalar field on a model object; more specialized commands
(list insert/delete/reorder) will be added as the UI grows.
"""
from __future__ import annotations

import struct
from typing import Any, Callable, List, Optional, Tuple

from PySide6.QtGui import QUndoCommand

from digimon_core import btchr, btchrspr, overlay5 as overlay5_mod
from digimon_core.sound.swap import BgmSwap


# All SetAttrCommands share one id so the QUndoStack will *attempt* to merge
# consecutive pushes; mergeWith() then rejects the merge unless (target, attr)
# match. Effect: rapid edits to the same field collapse into one undo step,
# while switching to a different field starts a fresh entry.
SET_ATTR_COMMAND_ID = 0x5E7A  # "SETA"
SET_MCHR_OW_PAL_COMMAND_ID = 0x5E7B  # consecutive spinner ticks merge to one


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


class SetMchrOwPalCommand(QUndoCommand):
    """Reassign an overworld sprite's palette — stages the ARM9 ow-sprite table
    ``f2`` override in the session (patched into the ROM on save), undoable.
    Consecutive edits of the same ow-id merge so dragging the palette spinner
    collapses to a single undo entry."""

    def __init__(
        self,
        session: Any,
        ow_id: int,
        new_pal: int,
        description: Optional[str] = None,
        on_change: Optional[Callable[[], None]] = None,
        mergeable: bool = True,
    ):
        super().__init__(
            description or f"Set overworld palette for ow 0x{ow_id:04x}"
        )
        self._session = session
        self._ow_id = int(ow_id)
        self._new = int(new_pal)
        self._had_prev = self._ow_id in session.mchr_ow_pal_overrides
        self._prev = session.mchr_ow_pal_overrides.get(self._ow_id)
        self._on_change = on_change
        # Revert is a deliberate distinct step (id -1 → QUndoStack never merges
        # it), so it doesn't collapse into a preceding spinner assignment.
        self._mergeable = mergeable

    def id(self) -> int:  # noqa: A003 — required Qt override name
        return SET_MCHR_OW_PAL_COMMAND_ID if self._mergeable else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetMchrOwPalCommand):
            return False
        if other._session is not self._session or other._ow_id != self._ow_id:
            return False
        self._new = other._new  # keep our original prev, absorb newer target
        return True

    def redo(self) -> None:
        self._session.set_mchr_ow_pal(self._ow_id, self._new)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        if self._had_prev:
            self._session.mchr_ow_pal_overrides[self._ow_id] = self._prev
        else:
            self._session.mchr_ow_pal_overrides.pop(self._ow_id, None)
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


def _rehydrate_record(rec: Any, data_bytes: bytes) -> None:
    """Re-parse ``rec`` in place from ``data_bytes`` (full record image),
    preserving its ROM ``offset``. Mutating the same instance keeps every
    bound editor widget's target reference valid. Works for BaseDataDigimon
    and EnemyDataDigimon (both parse via ``__init__(bytearray, offset)`` and
    round-trip through ``getByteArray()``)."""
    rec.__init__(bytearray(data_bytes), rec.offset)


class SwapDigimonRecordCommand(QUndoCommand):
    """Swap the ENTIRE data of two same-type digimon records, each keeping its
    own internal id (the u16 at bytes ``[0:2]``). Base↔base or enemy↔enemy only
    — the two records must be the same class / SIZE. Snapshots both byte images
    in ``__init__`` (before push() triggers redo) so a single Ctrl+Z reverts."""

    def __init__(self, rec_a: Any, rec_b: Any, description: str,
                 on_change: Optional[Callable[[], None]] = None):
        super().__init__(description)
        self._a = rec_a
        self._b = rec_b
        self._on_change = on_change
        self._a_bytes = bytes(rec_a.getByteArray())
        self._b_bytes = bytes(rec_b.getByteArray())

    def redo(self) -> None:
        # a gets b's data but keeps a's id; b gets a's data but keeps b's id.
        _rehydrate_record(self._a, self._a_bytes[:2] + self._b_bytes[2:])
        _rehydrate_record(self._b, self._b_bytes[:2] + self._a_bytes[2:])
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        _rehydrate_record(self._a, self._a_bytes)
        _rehydrate_record(self._b, self._b_bytes)
        if self._on_change is not None:
            self._on_change()


class CopyDigimonRecordCommand(QUndoCommand):
    """Copy the ENTIRE data of one digimon record onto another (same class),
    the destination keeping its own id. Snapshots the source image + the
    destination's prior image in ``__init__`` so undo restores the destination
    and a later source edit can't change what this command pastes."""

    def __init__(self, dest_rec: Any, source_rec: Any, description: str,
                 on_change: Optional[Callable[[], None]] = None):
        super().__init__(description)
        self._dest = dest_rec
        self._on_change = on_change
        src = bytes(source_rec.getByteArray())
        self._old_bytes = bytes(dest_rec.getByteArray())
        self._new_bytes = self._old_bytes[:2] + src[2:]  # dest id + source data

    def redo(self) -> None:
        _rehydrate_record(self._dest, self._new_bytes)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        _rehydrate_record(self._dest, self._old_bytes)
        if self._on_change is not None:
            self._on_change()


class SwapDisplayCommand(QUndoCommand):
    """Swap the display of two id slots — the sprite_map entry (overworld /
    battle / portrait / mini sprites) + the battle-string value — so a
    digimon-data swap can carry the sprites + name with it. Each slot keeps its
    own id; only the display fields move. Self-inverse, so redo == undo."""

    def __init__(self, sprite_a: Any, sprite_b: Any, str_a: Any, str_b: Any,
                 description: str = "", on_change: Optional[Callable[[], None]] = None):
        super().__init__(description)
        self._sa, self._sb = sprite_a, sprite_b
        self._ta, self._tb = str_a, str_b
        self._on_change = on_change

    def _swap(self) -> None:
        self._sa.unknown_0x4, self._sb.unknown_0x4 = self._sb.unknown_0x4, self._sa.unknown_0x4
        self._sa.main_sprite, self._sb.main_sprite = self._sb.main_sprite, self._sa.main_sprite
        self._sa.upperscreen_sprites, self._sb.upperscreen_sprites = (
            self._sb.upperscreen_sprites, self._sa.upperscreen_sprites)
        self._ta.value, self._tb.value = self._tb.value, self._ta.value
        if self._on_change is not None:
            self._on_change()

    def redo(self) -> None:
        self._swap()

    def undo(self) -> None:
        self._swap()


class CopyDisplayCommand(QUndoCommand):
    """Copy the display (sprite_map entry + battle-string value) of one id slot
    onto another; the destination keeps its id. Snapshots the destination's
    prior display so a single undo restores it."""

    def __init__(self, dest_sprite: Any, src_sprite: Any, dest_str: Any, src_str: Any,
                 description: str = "", on_change: Optional[Callable[[], None]] = None):
        super().__init__(description)
        self._ds, self._dt = dest_sprite, dest_str
        self._on_change = on_change
        self._old = (dest_sprite.unknown_0x4, dest_sprite.main_sprite,
                     dest_sprite.upperscreen_sprites, dest_str.value)
        self._new = (src_sprite.unknown_0x4, src_sprite.main_sprite,
                     src_sprite.upperscreen_sprites, src_str.value)

    def _apply(self, vals) -> None:
        (self._ds.unknown_0x4, self._ds.main_sprite,
         self._ds.upperscreen_sprites, self._dt.value) = vals
        if self._on_change is not None:
            self._on_change()

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


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


class ReplaceBgFileCommand(QUndoCommand):
    """Atomic swap of one ``DAT/bg/*`` menu-background FAT file's bytes.

    Same shape as :class:`ReplaceMapFileCommand` — used by the menu-background
    browser's PNG import (three files: NCGR/NSCR/NCLR, wrapped in a macro).
    ``on_change`` lets the browser re-render after each redo/undo flip.
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
        self._old_bytes = bytes(session.bg_file_bytes(path))
        self._new_bytes = bytes(new_bytes)

    def redo(self) -> None:
        self._session.replace_bg_file_bytes(self._path, self._new_bytes)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._session.replace_bg_file_bytes(self._path, self._old_bytes)
        if self._on_change is not None:
            self._on_change()


class MoveOverworldSpriteCommand(QUndoCommand):
    """Atomic (x, y) flip of one OVERWORLD_SPRITE block in an overlay5 entry.

    The block-level splice goes through ``overlay5.replace_sprite_xy``,
    which only touches the 4-byte x/y window — every other byte of the
    entry is preserved. The full entry bytes round-trip through
    ``session.replace_overlay5_entry_bytes`` so undo restores the exact
    same payload the user was looking at before the drag.

    ``on_change(new_x, new_y)`` is called on both redo and undo so the
    Events canvas can re-sync the marker position without rebuilding
    every marker on the map.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_x: int,
        new_y: int,
        description: str,
        on_change: Optional[Callable[[int, int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        # Snapshot the current placement so undo restores the exact x/y
        # the user dragged away from. Sample the live block bytes at
        # init time — pushing the command triggers redo, so this is the
        # last moment we can see the pre-drag value.
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.OverworldSpritePlacement.from_bytes(
            entry, block_offset,
        )
        self._old_xy = (prev.x, prev.y)
        self._new_xy = (int(new_x), int(new_y))

    def _apply(self, x: int, y: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_sprite_xy(
            entry, self._block_offset, x, y,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(x, y)

    def redo(self) -> None:
        self._apply(*self._new_xy)

    def undo(self) -> None:
        self._apply(*self._old_xy)


class EditOverworldSpriteIdCommand(QUndoCommand):
    """Atomic sprite-id swap of one OVERWORLD_SPRITE block.

    Only the u16 at block_offset+2 changes; ``overlay5.replace_sprite_id``
    enforces the opcode guard. ``on_change(new_id)`` fires on redo/undo
    so the Events sidebar + canvas can re-pull label/pixmap without
    rebuilding the whole layer.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_sprite_id: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.OverworldSpritePlacement.from_bytes(
            entry, block_offset,
        )
        self._old_id = prev.overworld_sprite_id
        self._new_id = int(new_sprite_id) & 0xFFFF

    def _apply(self, sprite_id: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_sprite_id(
            entry, self._block_offset, sprite_id,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(sprite_id)

    def redo(self) -> None:
        self._apply(self._new_id)

    def undo(self) -> None:
        self._apply(self._old_id)


class EditOverworldSpriteBehaviorCommand(QUndoCommand):
    """Atomic behavior (= sprite frame) swap on one OVERWORLD_SPRITE block.

    Mirrors :class:`EditOverworldSpriteIdCommand` for the u16 at
    ``block_offset + 24``. ``on_change(new_behavior)`` fires on
    redo/undo so the Events sidebar + canvas can re-render the marker
    with the new frame without rebuilding the layer.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_behavior: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.OverworldSpritePlacement.from_bytes(
            entry, block_offset,
        )
        self._old = prev.behavior
        self._new = int(new_behavior) & 0xFFFF

    def _apply(self, behavior: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_sprite_behavior(
            entry, self._block_offset, behavior,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(behavior)

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


class EditExitBoxCommand(QUndoCommand):
    """Atomic bounding-box edit of one 0x001b exit-zone block.

    Only the four u16 corners change; idx / flag / dst u32 are preserved
    by ``overlay5.replace_exit_box``. ``on_change(x1, y1, x2, y2)`` fires
    on redo/undo so the Events canvas can repaint the rectangle without
    rebuilding the prologue.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_x1: int,
        new_y1: int,
        new_x2: int,
        new_y2: int,
        description: str,
        on_change: Optional[Callable[[int, int, int, int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.ExitZone.from_bytes(entry, block_offset)
        self._old = (prev.x1, prev.y1, prev.x2, prev.y2)
        self._new = (
            int(new_x1) & 0xFFFF, int(new_y1) & 0xFFFF,
            int(new_x2) & 0xFFFF, int(new_y2) & 0xFFFF,
        )

    def _apply(self, box: tuple) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_exit_box(
            entry, self._block_offset, *box,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(*box)

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


class EditExitDestinationCommand(QUndoCommand):
    """Atomic edit of an exit handler's CALL_SCRIPT_AT_OFFSET u32.

    Repoints which destination entry the handler jumps to. Shared
    handlers (multiple 0x001b blocks with the same ``dst_file_off``)
    all observe the change — by design, since the editor can't grow
    overlay5 to allocate a fresh handler. ``on_change(new_dest)`` fires
    so the sidebar can re-resolve the destination map_id label.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        handler_rel_offset: int,
        new_dest_file_off: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._handler_rel_offset = handler_rel_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.ExitHandler.from_bytes(entry, handler_rel_offset)
        if prev is None:
            raise ValueError(
                f"no exit handler at rel 0x{handler_rel_offset:04x} "
                f"in entry {entry_ix}"
            )
        self._old_dest = prev.dest_file_off
        self._new_dest = int(new_dest_file_off) & 0xFFFFFFFF

    def _apply(self, dest: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_exit_handler_dest(
            entry, self._handler_rel_offset, dest,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(dest)

    def redo(self) -> None:
        self._apply(self._new_dest)

    def undo(self) -> None:
        self._apply(self._old_dest)


class EditExitSpawnArgCommand(QUndoCommand):
    """Atomic edit of an exit handler's op 0x0002 u32 arg.

    The arg's meaning is currently unknown (presumed spawn-side
    selector in the destination map); the editor surfaces it as a raw
    u32 so the user can tweak it manually. Same shared-handler caveat
    as :class:`EditExitDestinationCommand`.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        handler_rel_offset: int,
        new_spawn_arg: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._handler_rel_offset = handler_rel_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.ExitHandler.from_bytes(entry, handler_rel_offset)
        if prev is None:
            raise ValueError(
                f"no exit handler at rel 0x{handler_rel_offset:04x} "
                f"in entry {entry_ix}"
            )
        self._old_arg = prev.spawn_arg
        self._new_arg = int(new_spawn_arg) & 0xFFFFFFFF

    def _apply(self, arg: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_exit_handler_spawn_arg(
            entry, self._handler_rel_offset, arg,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(arg)

    def redo(self) -> None:
        self._apply(self._new_arg)

    def undo(self) -> None:
        self._apply(self._old_arg)


class EditChestItemCommand(QUndoCommand):
    """Atomic edit of the item an overworld chest gives.

    The chest's interaction script sets ARG_1 (SET_VAR var 0x0005) to the
    item id just before ``CALL_SYS 0x126a``; this rewrites that one u16 in
    place. ``overlay5.replace_chest_item`` validates the ``15 00 05 00``
    prefix on every apply so a stale offset can't corrupt the script.
    ``on_change(item_id)`` fires on redo/undo so the card can re-resolve
    the item name.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        value_offset: int,
        new_item_id: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = int(entry_ix)
        self._value_offset = int(value_offset)
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        if value_offset < 4 or value_offset + 2 > len(entry):
            raise ValueError(
                f"chest item offset 0x{value_offset:04x} out of range "
                f"in entry {entry_ix}"
            )
        self._old = struct.unpack_from("<H", entry, value_offset)[0]
        self._new = int(new_item_id) & 0xFFFF

    def _apply(self, item_id: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_chest_item(
            entry, self._value_offset, item_id,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(item_id)

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


class EditScriptFieldCommand(QUndoCommand):
    """In-place u16 edit of one field inside a fixed-size overlay5 opcode.

    Backs the Tier-1 Cutscenes cards (wait / sprite-anim / camera / item):
    each rewrites a single u16 at ``block_offset + field_offset``,
    validated against the opcode byte at ``block_offset`` via
    ``overlay5.replace_scalar_field`` so a stale offset is a hard error,
    not silent corruption. ``on_change(value)`` fires on redo/undo.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        field_offset: int,
        new_value: int,
        expected_opcode: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = int(entry_ix)
        self._block_offset = int(block_offset)
        self._field_offset = int(field_offset)
        self._expected_opcode = int(expected_opcode)
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        self._old = struct.unpack_from(
            "<H", entry, self._block_offset + self._field_offset,
        )[0]
        self._new = int(new_value) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_scalar_field(
            entry, self._block_offset, self._field_offset,
            value, self._expected_opcode,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


class EditDialogFieldCommand(QUndoCommand):
    """Atomic edit of one u16 field (target / msg_id / portrait) inside
    a 12-byte DIALOG block.

    Same-length splice so the entry's byte budget is untouched. Dialog
    blocks can be reached from multiple objects (a sprite's string_ptr
    and a nearby hitbox's dst can land on the same offset); changes are
    observed by every caller — by design, since we can't allocate a
    fresh block. ``on_change(new_value)`` fires after redo/undo so the
    sidebar can refresh its label.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        field: str,
        new_value: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._field = field
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.DialogBlock.from_bytes(entry, block_offset)
        self._old_value = getattr(prev, field)
        self._new_value = int(new_value) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_dialog_field(
            entry, self._block_offset, self._field, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


class SetMusicIdCommand(QUndoCommand):
    """Atomic edit of the ``music_id`` u16 in a SET_MUSIC (0e 00 XX XX)
    block.

    Same-length splice (4 bytes in, 4 bytes out). The block may be the
    map's boot-time BGM (right after REGISTER_HANDLER in the prologue-
    adjacent region) or a mid-cutscene retune; the codec treats them
    identically. ``on_change`` fires post-redo/undo so the card can
    refresh its combo without re-decoding the entry.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_music_id: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.SetMusicBlock.from_bytes(entry, block_offset)
        self._old_value = prev.music_id
        self._new_value = int(new_music_id) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_set_music_id(
            entry, self._block_offset, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


class EditReactionFieldCommand(QUndoCommand):
    """Atomic edit of ``reaction`` or ``target`` u16 in a REACTION_BALLOON
    (C0 00 [reaction] [target]) block.

    Same-length splice. ``target`` is a sprite slot the balloon anchors
    over; ``reaction`` is the balloon icon id (0 = "!", 1 = "…", etc).
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        field: str,
        new_value: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._field = field
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod.ReactionBlock.from_bytes(entry, block_offset)
        self._old_value = getattr(prev, field)
        self._new_value = int(new_value) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_reaction_field(
            entry, self._block_offset, self._field, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


class EditBattleEnemyCommand(QUndoCommand):
    """Atomic edit of one enemy u16 slot (0..4) inside a BATTLE block.

    The 5-slot roster uses ``0xFFFF`` for "empty"; the UI passes it
    through as any other value so users can add / clear specific slots
    without a special "remove" affordance.
    """

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        slot_ix: int,
        new_enemy_id: int,
        description: str,
        on_change: Optional[Callable[[int, int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._slot_ix = int(slot_ix)
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod._parse_battle_at(entry, block_offset)
        if prev is None:
            raise ValueError(
                f"no BATTLE block at entry {entry_ix:04d} +0x{block_offset:04x}"
            )
        self._old_value = prev.enemies[self._slot_ix]
        self._new_value = int(new_enemy_id) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_battle_enemy(
            entry, self._block_offset, self._slot_ix, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(self._slot_ix, value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


class EditBattleBgCommand(QUndoCommand):
    """Atomic edit of the ``D8 00 [bg]`` u16 inside a BATTLE block."""

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_bg_id: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod._parse_battle_at(entry, block_offset)
        if prev is None:
            raise ValueError(
                f"no BATTLE block at entry {entry_ix:04d} +0x{block_offset:04x}"
            )
        self._old_value = prev.bg_id
        self._new_value = int(new_bg_id) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_battle_bg(
            entry, self._block_offset, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


class EditBattleMusicCommand(QUndoCommand):
    """Atomic edit of the ``D9 00 [music]`` u16 inside a BATTLE block."""

    def __init__(
        self,
        session: Any,
        entry_ix: int,
        block_offset: int,
        new_music_id: int,
        description: str,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._entry_ix = entry_ix
        self._block_offset = block_offset
        self._on_change = on_change
        entry = session.overlay5_entry_bytes(entry_ix)
        prev = overlay5_mod._parse_battle_at(entry, block_offset)
        if prev is None:
            raise ValueError(
                f"no BATTLE block at entry {entry_ix:04d} +0x{block_offset:04x}"
            )
        self._old_value = prev.music_id
        self._new_value = int(new_music_id) & 0xFFFF

    def _apply(self, value: int) -> None:
        entry = self._session.overlay5_entry_bytes(self._entry_ix)
        edited = overlay5_mod.replace_battle_music(
            entry, self._block_offset, value,
        )
        self._session.replace_overlay5_entry_bytes(self._entry_ix, edited)
        if self._on_change is not None:
            self._on_change(value)

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)


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


class BatchCompressBtchrCommand(QUndoCommand):
    """Apply many occupied-only OAM re-covers as a single undoable step.

    Each ``(group, spr)`` port is delegated to a child
    :class:`PortBtchrSpriteCommand`. The children are built up-front, before
    any ``redo()``, against the pre-batch bytes — compressing group A never
    touches group B's entries or sidecar words, so the snapshots don't
    interfere. Only the batch fires ``on_change`` (once, after the whole run),
    so the browser redecodes a single time instead of once per group.
    """

    def __init__(
        self,
        session: Any,
        ports: List[Tuple[int, btchrspr.BtchrSprite]],
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._on_change = on_change
        self._children = [
            PortBtchrSpriteCommand(session, group, spr, description="", on_change=None)
            for group, spr in ports
        ]

    def redo(self) -> None:
        for child in self._children:
            child.redo()
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        for child in reversed(self._children):
            child.undo()
        if self._on_change is not None:
            self._on_change()


class SyncChrsizeFootprintCommand(QUndoCommand):
    """Rewrite one CHRSIZE.BIN slot's fs (hi u16) to match a displayed sprite.

    The wild-encounter roll budgets each enemy against
    ``CHRSIZE.BIN[lo==id].hi`` (Σ fs ≤ 1440), but the sprite that renders
    is the id's ``main_sprite`` group. A reskin changes what renders
    without touching that word, so the roll under-counts and can spawn a
    big sprite past the VRAM pool — crashing even a natural encounter
    (project memory ``project_wild_spawn_size_gate``). This syncs the fs
    to the displayed sprite's real footprint, preserving the lo (digimon
    id) half. Single-slot, fully reversible.
    """

    def __init__(
        self,
        session: Any,
        entry_group: int,
        new_fs: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._group = entry_group
        self._on_change = on_change
        self._old_word = session.current_chrsize_word(entry_group)
        self._new_word = (self._old_word & 0xFFFF) | ((new_fs & 0xFFFF) << 16)

    def redo(self) -> None:
        self._session.set_chrsize_word(self._group, self._new_word)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._session.set_chrsize_word(self._group, self._old_word)
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


class AddWildEncounterCommand(QUndoCommand):
    """Add a new encounter slot to a wild-encounter area.

    The record count grows past the area's vanilla FAT slot, so
    ``serialize_all`` routes it through the wild-encounter splice. On
    redo-after-undo the *same* WildEncounter instance is re-inserted, so any
    per-field edits the user layers on top (SetAttrCommands bound to that
    instance) survive the round-trip. Undo removes the slot. Capped at the
    16-encounter engine limit (``WildEncounterArea.MAX_ENCOUNTERS``).
    """

    def __init__(
        self,
        session: Any,
        area: Any,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._area = area
        self._on_change = on_change
        self._enc: Any = None
        self._index: Optional[int] = None

    def redo(self) -> None:
        if self._enc is None:
            self._enc = self._area.add_encounter()
            self._index = self._area.encounters.index(self._enc)
        else:
            self._area.insert_encounter(self._index, self._enc)
        self._session.invalidate_wild_area_index()
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._area.remove_encounter(self._index)
        self._session.invalidate_wild_area_index()
        if self._on_change is not None:
            self._on_change()


class RemoveWildEncounterCommand(QUndoCommand):
    """Remove one encounter slot from a wild-encounter area.

    Snapshots the removed WildEncounter instance so undo re-inserts the exact
    same object at its original index — keeping any field-edit commands that
    reference it valid across the round-trip.
    """

    def __init__(
        self,
        session: Any,
        area: Any,
        index: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._area = area
        self._index = int(index)
        self._on_change = on_change
        self._enc: Any = None

    def redo(self) -> None:
        self._enc = self._area.remove_encounter(self._index)
        self._session.invalidate_wild_area_index()
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        self._area.insert_encounter(self._index, self._enc)
        self._session.invalidate_wild_area_index()
        if self._on_change is not None:
            self._on_change()


class StageBgmSwapCommand(QUndoCommand):
    """Stage a donor ``BgmSwap`` against its ``target_bgm_id`` slot.

    Captures whatever swap was staged on that slot before (or ``None``
    for vanilla) so undo flips back to the prior state — chained
    Replace + Replace + Undo round-trips back to the first donor, not
    to vanilla. ``on_change`` lets the SoundEditor refresh the ROM list
    marker and footer after each flip.
    """

    def __init__(
        self,
        session: Any,
        swap: BgmSwap,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._target_id = swap.target_bgm_id
        self._new_swap = swap
        self._prev_swap: Optional[BgmSwap] = session.staged_bgm_swap(
            swap.target_bgm_id
        )
        self._on_change = on_change

    def redo(self) -> None:
        self._session.stage_bgm_swap(self._new_swap)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        if self._prev_swap is None:
            self._session.revert_bgm_swap(self._target_id)
        else:
            self._session.stage_bgm_swap(self._prev_swap)
        if self._on_change is not None:
            self._on_change()


class RevertBgmSwapCommand(QUndoCommand):
    """Drop the staged swap on ``target_bgm_id``; undo restages it.

    No-op if the slot is already vanilla — the caller should gate the
    Revert button on ``session.staged_bgm_swap(id) is not None`` so we
    don't push empty undo entries onto the stack.
    """

    def __init__(
        self,
        session: Any,
        target_bgm_id: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._target_id = target_bgm_id
        self._prev_swap: Optional[BgmSwap] = session.staged_bgm_swap(target_bgm_id)
        self._on_change = on_change

    def redo(self) -> None:
        self._session.revert_bgm_swap(self._target_id)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        if self._prev_swap is not None:
            self._session.stage_bgm_swap(self._prev_swap)
        if self._on_change is not None:
            self._on_change()


class AddBgmCommand(QUndoCommand):
    """Append a donor ``BgmSwap`` as a brand-new BGM slot ("Add As New Entry").

    Each addition gets bgm_id ``vanilla_seq_count + position`` at save time.
    Undo pops it from the staged-additions list at the position the redo
    placed it; redo re-inserts at that same position so undo-redo round-
    trips are stable even when the user has staged other additions in
    between.
    """

    def __init__(
        self,
        session: Any,
        swap: BgmSwap,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._new_swap = swap
        self._index: Optional[int] = None
        self._on_change = on_change

    def redo(self) -> None:
        self._index = self._session.stage_bgm_addition(
            self._new_swap, index=self._index,
        )
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        if self._index is not None:
            self._session.revert_bgm_addition(self._index)
        if self._on_change is not None:
            self._on_change()


class RevertBgmAdditionCommand(QUndoCommand):
    """Drop a staged addition at ``index``; undo restages it at the same index.

    Caller should gate the button on the index being valid so we don't
    push empty undo entries.
    """

    def __init__(
        self,
        session: Any,
        index: int,
        description: str,
        on_change: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self._session = session
        self._index = index
        self._removed: Optional[BgmSwap] = None
        self._on_change = on_change

    def redo(self) -> None:
        self._removed = self._session.revert_bgm_addition(self._index)
        if self._on_change is not None:
            self._on_change()

    def undo(self) -> None:
        if self._removed is not None:
            self._session.stage_bgm_addition(self._removed, index=self._index)
        if self._on_change is not None:
            self._on_change()
