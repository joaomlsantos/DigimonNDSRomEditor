"""QUndoCommand subclasses for editor mutations.

All edits to the parsed model graph should go through a Command so that the
QUndoStack can drive Ctrl+Z / Ctrl+Y. `SetAttrCommand` covers the common case
of changing a single scalar field on a model object; more specialized commands
(list insert/delete/reorder) will be added as the UI grows.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

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
