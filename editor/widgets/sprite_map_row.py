"""Shared Display / Reskin widget for base and enemy digimon editors.

`sprite_map` is one flat table keyed by digimon_id, used by both base
species and fixed enemies. So the same widget works in both editors —
it edits `sprite_map[target.id]` and `battle_strings[target.id]`, and
the blast radius depends on the id (species-wide vs per-encounter), not
on which editor opened the slot.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from ..commands import ReskinSlotCommand, SetAttrCommand
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundIdCombo,
    BoundSpinBox,
    NoWheelComboBox,
    silenced,
)


class _SpriteListPicker(NoWheelComboBox):
    """Editable, name-filterable combo over an arbitrary sprite list.

    The picker is label-source agnostic: the caller supplies a
    ``labels_provider`` callable that returns ``List[str]`` for the
    current state of the underlying pak. The position of each label in
    the returned list IS the user-data of the corresponding combo item.

    Used by the sprite-map row to expose BTCHR groups (main sprite),
    SPR_* entries (icon portrait, battle mini) without each field
    needing its own picker class. Labels rebuild only when the provider
    returns a different list object (identity check) so warm selection
    switches don't pay the 1627-item add cost.
    """

    def __init__(self, labels_provider, undo_stack: QUndoStack):
        super().__init__()
        self._labels_provider = labels_provider
        self._undo_stack = undo_stack
        self._target = None
        self._attr: str = ""
        # Identity-key for the labels list we last populated with. The
        # SPR list is 1627 items — re-adding on every selection switch
        # costs ~130ms. The provider returns the SAME list object while
        # its cache is valid, so identity is a cheap "needs repopulate?"
        # check that lets the common case (selection switch, no pak
        # change) skip the full rebuild.
        self._last_labels_id: int = -1

        self.setMaximumWidth(360)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(completer)
        self._completer = completer

        self.currentIndexChanged.connect(self._on_index_changed)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.editingFinished.connect(self._snap_text_to_selection)

    def bind(self, target, attr: str) -> None:
        """Snap to ``target.attr``, rebuilding choices only when needed.

        Wraps the whole rebuild in ``silenced``: :meth:`QComboBox.clear`
        and the first :meth:`addItem` both fire ``currentIndexChanged`,
        which would otherwise push a spurious ``SetAttrCommand`` for
        index 0 on every selection switch.

        Skips the rebuild when the provider returns the same list
        object as last time (identity check). With 1627-item SPR
        lists, this drops selection-switch cost from ~130ms to
        negligible.
        """
        self._target = target
        self._attr = attr
        labels = self._labels_provider()
        with silenced(self):
            if id(labels) != self._last_labels_id:
                self._populate(labels)
            self.setCurrentIndex(self._ensure_index_for(getattr(target, attr)))

    def refresh(self) -> None:
        if self._target is None:
            return
        with silenced(self):
            self.setCurrentIndex(self._ensure_index_for(getattr(self._target, self._attr)))

    def _populate(self, labels: List[str]) -> None:
        self.clear()
        for ix, label in enumerate(labels):
            self.addItem(label, userData=ix)
        if self._completer is not None:
            self._completer.setModel(self.model())
        self._last_labels_id = id(labels)

    def _ensure_index_for(self, value: int) -> int:
        for i in range(self.count()):
            if self.itemData(i) == value:
                return i
        # Value beyond known range — show it as a fallback row so the
        # picker faithfully reflects whatever the model carries.
        self.addItem(f"(undefined 0x{value:x})", userData=value)
        return self.count() - 1

    def _on_index_changed(self, _index: int) -> None:
        if self._target is None:
            return
        value = self.currentData(Qt.UserRole)
        if value is None:
            return
        if getattr(self._target, self._attr) == value:
            return
        self._undo_stack.push(SetAttrCommand(self._target, self._attr, value))

    def _snap_text_to_selection(self) -> None:
        line_edit = self.lineEdit()
        if line_edit is None:
            return
        ix = self.currentIndex()
        if ix < 0:
            return
        expected = self.itemText(ix)
        if line_edit.text() != expected:
            with silenced(self):
                line_edit.setText(expected)


class SpriteMapRow:
    """Sprite-map / battle-string editor with two complementary modes.

    Shared by base and enemy digimon editors — both index the same
    ``sprite_map`` / ``battle_strings`` tables by ``target.id``.

    *Top* — "Appears as" combo: a quick-pick reskin that copies
    ``main_sprite``, ``upperscreen_sprites`` and the battle-string
    value from another sprite_map slot into the current digimon's slot
    in one atomic undo step.

    *Bottom* — checkbox-gated manual editors, one per
    :class:`SpriteMapEntry` field plus the :class:`BattleStringEntry`
    value. Disabled by default; the user opts in via the checkbox when
    they need to dial in specific values (e.g. pointing at an appended
    BTCHR group not yet wired into any other slot).

    When the digimon id has no sprite-map / battle-string slot at all,
    both panels are disabled and the status label explains why.
    """

    def __init__(
        self,
        sprite_map: List[model.SpriteMapEntry],
        battle_strings: List[model.BattleStringEntry],
        undo_stack: QUndoStack,
        session,
    ):
        self._sprite_map = sprite_map
        self._battle_strings = battle_strings
        self._undo_stack = undo_stack
        self._session = session
        self._target_id: int = -1
        # Computed lazily on first picker bind (compute_spr_labels parses
        # 1627 NCGR+NCER entries — too slow to do at editor instantiation).
        # Caching also lets _SpriteListPicker.bind skip its 1627-item
        # rebuild via identity check on warm selection switches.
        self._spr_labels_cache: Optional[List[str]] = None
        self._btchr_labels_cache: Optional[List[str]] = None
        self._mchr_labels_cache: Optional[List[str]] = None

        # Reverse lookup: main_sprite value -> first slot index that uses it.
        # Public so the host editor can decorate list / title labels with a
        # "displayed as" suffix when the slot has been reskinned.
        # Pickable list covers every sprite-map slot.
        self.sprite_to_base: Dict[int, int] = {}
        self._pickable: List[Tuple[int, str]] = []
        for base_id in range(len(sprite_map)):
            self._pickable.append((base_id, session.digimon_display_name(base_id)))
            self.sprite_to_base.setdefault(sprite_map[base_id].main_sprite, base_id)

        self.group = QGroupBox("Display / Reskin")
        outer = QVBoxLayout(self.group)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(4)

        # --- top: Appears-as picker (existing reskin convenience) ---
        top_form = QFormLayout()
        top_form.setContentsMargins(0, 0, 0, 0)
        top_form.setSpacing(4)
        self._combo = NoWheelComboBox()
        self._combo.setMaximumWidth(280)
        # Editable + NoInsert + substring completer mirrors BoundIdCombo: user
        # can type any name fragment to filter, free-typed text never inserts.
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(self._combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._combo.setCompleter(completer)
        for base_id, name in self._pickable:
            self._combo.addItem(f"0x{base_id:03x}  {name}", userData=base_id)
        completer.setModel(self._combo.model())
        self._combo.currentIndexChanged.connect(self._on_reskin_changed)
        line_edit = self._combo.lineEdit()
        if line_edit is not None:
            line_edit.editingFinished.connect(self._snap_reskin_text)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid);")
        self._status.setWordWrap(True)

        top_form.addRow("Appears as", self._combo)
        top_form.addRow(self._status)
        outer.addLayout(top_form)

        # --- divider checkbox ---
        self._customize_checkbox = QCheckBox("Customize sprite/string data manually")
        self._customize_checkbox.setChecked(False)
        self._customize_checkbox.toggled.connect(self._on_customize_toggled)
        outer.addWidget(self._customize_checkbox)

        # --- bottom: manual editors (one per SpriteMapEntry field + battle string) ---
        first_slot = sprite_map[0]
        first_str = battle_strings[0]
        self._id_spin = BoundSpinBox(first_slot, "id", 4, undo_stack, hex_display=True)
        # Party-follower overworld sprite — full MCHR_CHR list, name-filtered
        # via the shared picker (mirrors the BTCHR / SPR pickers below so all
        # three sprite fields read the same way).
        self._overworld_combo = _SpriteListPicker(
            self._mchr_labels_provider, undo_stack,
        )
        self._overworld_combo.bind(first_slot, "unknown_0x4")
        # All three sprite pickers are full searchable lists over the
        # corresponding pak — not just the digimon-mapped subset. Labels
        # come from the shared browser helpers so the formatting stays
        # consistent across the app (e.g. `[ICON] Koromon`).
        self._main_sprite_combo = _SpriteListPicker(
            self._btchr_labels_provider, undo_stack,
        )
        self._main_sprite_combo.bind(first_slot, "main_sprite")
        self._upper_sprite_low_combo = _SpriteListPicker(
            self._spr_labels_provider, undo_stack,
        )
        self._upper_sprite_low_combo.bind(first_slot, "upperscreen_low")
        self._upper_sprite_high_combo = _SpriteListPicker(
            self._spr_labels_provider, undo_stack,
        )
        self._upper_sprite_high_combo.bind(first_slot, "upperscreen_high")
        # Battle string `value` is a relative offset into the ARM9 text
        # region: `STRING_BATTLE_TABLE_OFFSET[version][0] + value` lands on
        # a string inside `arm9_digiegg_enemy_names`. Showing the resolved
        # text instead of the raw hex lets the user pick "Greymon" by name
        # instead of memorizing 0x1be2.
        self._battle_str_base = constants.STRING_BATTLE_TABLE_OFFSET[session.version][0]
        bs_strings = session.string_regions.get("arm9_digiegg_enemy_names", [])
        bs_choices: List[Tuple[int, str]] = [
            (g.offset - self._battle_str_base, g.text) for g in bs_strings
        ]
        self._battle_str_combo = BoundIdCombo(
            first_str, "value", bs_choices, undo_stack,
        )

        manual_form = QFormLayout()
        manual_form.setContentsMargins(0, 0, 0, 0)
        manual_form.setSpacing(4)
        manual_form.addRow("ID", self._id_spin)
        manual_form.addRow("Overworld (party)", self._overworld_combo)
        manual_form.addRow("Main sprite", self._main_sprite_combo)
        manual_form.addRow("Icon Portrait", self._upper_sprite_low_combo)
        manual_form.addRow("Battle Mini", self._upper_sprite_high_combo)
        manual_form.addRow("Battle string", self._battle_str_combo)
        outer.addLayout(manual_form)

        self._manual_widgets: List[QWidget] = [
            self._id_spin, self._overworld_combo, self._main_sprite_combo,
            self._upper_sprite_low_combo, self._upper_sprite_high_combo,
            self._battle_str_combo,
        ]
        self._on_customize_toggled(False)

    def _btchr_labels_provider(self) -> List[str]:
        # Lazy import keeps editor.widgets.sprite_map_row importable
        # without the heavier btchr_browser module on its bare-import path
        # (and avoids a circular import via the parent package).
        from .btchr_browser import compute_btchr_group_labels
        # Same caching reasoning as _spr_labels_provider: returning the
        # same list object lets _SpriteListPicker skip its rebuild on
        # warm selection switches.
        if self._btchr_labels_cache is None:
            self._btchr_labels_cache = compute_btchr_group_labels(self._session)
        return self._btchr_labels_cache

    def _mchr_labels_provider(self) -> List[str]:
        from .mchr_browser import compute_mchr_labels
        # Same caching reasoning as _spr_labels_provider.
        if self._mchr_labels_cache is None:
            self._mchr_labels_cache = compute_mchr_labels(self._session)
        return self._mchr_labels_cache

    def _spr_labels_provider(self) -> List[str]:
        from .sprite_browser import compute_spr_labels
        # Cache the SPR labels until something invalidates them: 1627
        # NCGR+NCER parses takes a few hundred ms; recomputing on every
        # bind() would stutter selection switches. Caller invalidates
        # via _invalidate_label_caches() when sprite_map cross-refs or
        # pak length might have changed.
        if self._spr_labels_cache is None:
            self._spr_labels_cache = compute_spr_labels(self._session)
        return self._spr_labels_cache

    def _invalidate_label_caches(self) -> None:
        self._spr_labels_cache = None
        self._btchr_labels_cache = None
        self._mchr_labels_cache = None

    def _on_customize_toggled(self, enabled: bool) -> None:
        # Manual widgets stay disabled when there's no slot to bind against
        # (see `_apply_state`); guard against re-enabling them in that case.
        if not self._has_slot_for_target():
            enabled = False
        for w in self._manual_widgets:
            w.setEnabled(enabled)

    def _has_slot_for_target(self) -> bool:
        tid = self._target_id
        if tid < 0:
            return False
        return tid < len(self._sprite_map) and tid < len(self._battle_strings)

    @property
    def widget(self) -> QWidget:
        return self.group

    def rebind(self, target: Any) -> None:
        self._target_id = target.id
        if self._has_slot_for_target():
            slot = self._sprite_map[target.id]
            self._id_spin.rebind(slot)
            self._overworld_combo.bind(slot, "unknown_0x4")
            self._main_sprite_combo.bind(slot, "main_sprite")
            self._upper_sprite_low_combo.bind(slot, "upperscreen_low")
            self._upper_sprite_high_combo.bind(slot, "upperscreen_high")
            self._battle_str_combo.rebind(self._battle_strings[target.id])
        self._apply_state()

    def refresh(self) -> None:
        if self._target_id < 0:
            return
        # refresh() runs on external state changes (undo/redo, tab switch).
        # SPR / BTCHR labels embed sprite_map cross-refs and depend on pak
        # length, so drop the caches here — the next bind() recomputes.
        # Cheap in-form changes (selection switches) go through rebind()
        # and keep using the caches.
        self._invalidate_label_caches()
        self._apply_state()
        if self._has_slot_for_target():
            self._id_spin.refresh()
            self._overworld_combo.refresh()
            self._main_sprite_combo.refresh()
            self._upper_sprite_low_combo.refresh()
            self._upper_sprite_high_combo.refresh()
            self._battle_str_combo.refresh()

    def _apply_state(self) -> None:
        target_id = self._target_id
        self.group.setVisible(True)

        if target_id < 0:
            self._combo.setEnabled(False)
            self._customize_checkbox.setEnabled(False)
            self._status.setText("")
            with silenced(self._combo):
                self._combo.setCurrentIndex(-1)
            self._on_customize_toggled(self._customize_checkbox.isChecked())
            return

        if not self._has_slot_for_target():
            self._combo.setEnabled(False)
            self._customize_checkbox.setEnabled(False)
            self._status.setText("(no sprite-map / battle-string slot for this digimon id)")
            with silenced(self._combo):
                self._combo.setCurrentIndex(-1)
            self._on_customize_toggled(self._customize_checkbox.isChecked())
            return

        self._combo.setEnabled(True)
        self._customize_checkbox.setEnabled(True)
        # Re-apply checkbox state now that there's a valid slot.
        self._on_customize_toggled(self._customize_checkbox.isChecked())

        current_sprite = self._sprite_map[target_id].main_sprite
        current_base = self.sprite_to_base.get(current_sprite)

        if current_base is None:
            self._status.setText(
                f"(sprite 0x{current_sprite:x} not present in the sprite-map table)"
            )
            with silenced(self._combo):
                self._combo.setCurrentIndex(-1)
            return

        self._status.setText("")
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == current_base:
                with silenced(self._combo):
                    self._combo.setCurrentIndex(i)
                break

    def _snap_reskin_text(self) -> None:
        """Revert free-typed text to the current item's label on focus-out."""
        line_edit = self._combo.lineEdit()
        if line_edit is None:
            return
        ix = self._combo.currentIndex()
        if ix < 0:
            return
        expected = self._combo.itemText(ix)
        if line_edit.text() != expected:
            with silenced(self._combo):
                line_edit.setText(expected)

    def _on_reskin_changed(self, _index: int) -> None:
        target_id = self._target_id
        if target_id < 0:
            return
        if not self._has_slot_for_target():
            return
        new_base_id = self._combo.currentData(Qt.UserRole)
        if new_base_id is None:
            return
        if new_base_id >= len(self._sprite_map) or new_base_id >= len(self._battle_strings):
            return

        sprite_entry = self._sprite_map[target_id]
        str_entry = self._battle_strings[target_id]
        source_sprite = self._sprite_map[new_base_id]
        source_str = self._battle_strings[new_base_id]

        if (sprite_entry.main_sprite == source_sprite.main_sprite
                and sprite_entry.upperscreen_sprites == source_sprite.upperscreen_sprites
                and str_entry.value == source_str.value):
            return

        self._undo_stack.push(
            ReskinSlotCommand(
                sprite_entry,
                str_entry,
                source_sprite.main_sprite,
                source_sprite.upperscreen_sprites,
                source_str.value,
            )
        )


def displayed_as_suffix(
    sprite_map: List[model.SpriteMapEntry],
    sprite_to_base: Dict[int, int],
    digimon_id: int,
    own_name: str,
    name_resolver: Optional[Callable[[int], str]] = None,
) -> str:
    """Return a "  [DisplayName]" suffix when the slot has been reskinned.

    Used by both editors' list/title labels to surface reskinned slots
    at a glance. Empty string when the slot resolves to the same name
    (the common, unreskinned case). ``name_resolver`` falls back to
    ``DIGIMON_ID_TO_STR`` when omitted; callers should pass
    ``session.digimon_display_name`` so the reskin target gets its
    battle-string fallback name too.
    """
    if digimon_id < 0 or digimon_id >= len(sprite_map):
        return ""
    if not sprite_to_base:
        return ""
    sprite = sprite_map[digimon_id].main_sprite
    base = sprite_to_base.get(sprite)
    if base is None:
        return "  [???]"
    if name_resolver is not None:
        display_name = name_resolver(base)
    else:
        display_name = constants.DIGIMON_ID_TO_STR.get(base, f"0x{base:03x}")
    if display_name == own_name:
        return ""
    return f"  [{display_name}]"
