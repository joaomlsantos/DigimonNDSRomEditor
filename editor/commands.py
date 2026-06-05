"""QUndoCommand subclasses for editor mutations.

All edits to the parsed model graph should go through a Command so that the
QUndoStack can drive Ctrl+Z / Ctrl+Y. `SetAttrCommand` covers the common case
of changing a single scalar field on a model object; more specialized commands
(list insert/delete/reorder) will be added as the UI grows.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from PySide6.QtGui import QUndoCommand


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


class ReskinFixedEnemyCommand(QUndoCommand):
    """Atomic "Displayed As" reskin of a fixed enemy.

    Copies main_sprite + upperscreen_sprites from a source SpriteMapEntry and
    the battle-string value from a source BattleStringEntry into the fixed
    enemy's slots. Undo restores the previous three values in one step so a
    single Ctrl+Z reverts the full reskin.
    """

    def __init__(
        self,
        sprite_entry: Any,
        str_entry: Any,
        new_main_sprite: int,
        new_upperscreen: int,
        new_str_value: int,
        description: Optional[str] = None,
    ):
        super().__init__(description or f"Reskin enemy 0x{sprite_entry.id:03x}")
        self._sprite_entry = sprite_entry
        self._str_entry = str_entry
        self._old_main = sprite_entry.main_sprite
        self._old_upper = sprite_entry.upperscreen_sprites
        self._old_str = str_entry.value
        self._new_main = new_main_sprite
        self._new_upper = new_upperscreen
        self._new_str = new_str_value

    def redo(self) -> None:
        self._sprite_entry.main_sprite = self._new_main
        self._sprite_entry.upperscreen_sprites = self._new_upper
        self._str_entry.value = self._new_str

    def undo(self) -> None:
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
