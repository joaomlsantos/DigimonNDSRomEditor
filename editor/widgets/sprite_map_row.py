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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .._perf import span
from ..commands import ReskinSlotCommand, SetAttrCommand, SyncChrsizeFootprintCommand
from .flow_layout import FlowLayout, make_height_for_width
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

    def __init__(
        self,
        labels_provider,
        undo_stack: Optional[QUndoStack] = None,
        shared_kind: Optional[str] = None,
    ):
        super().__init__()
        self._labels_provider = labels_provider
        # Undo stack may be supplied later via set_undo_stack — the session
        # builds these pickers up-front (no main_window, no stack yet) and
        # the host editor injects its stack on acquire.
        self._undo_stack: Optional[QUndoStack] = undo_stack
        self._target = None
        self._attr: str = ""
        # Identity-key for the labels list we last populated with. The
        # SPR list is 1627 items — re-adding on every selection switch
        # costs ~130ms. The provider returns the SAME list object while
        # its cache is valid, so identity is a cheap "needs repopulate?"
        # check that lets the common case (selection switch, no pak
        # change) skip the full rebuild. Unused in shared-model mode.
        self._last_labels_id: int = -1
        # Shared model kind ("spr" / "mchr" / "btchr") — when registered,
        # bind() just setCurrentIndex's against the pre-built model and
        # skips both labels_provider() and the addItem loop. Falls back
        # to the labels_provider path if no session is registered.
        self._shared_kind = shared_kind
        self._shared_model = None
        if shared_kind is not None:
            from .form_helpers import get_picker_model
            model = get_picker_model(shared_kind)
            if model is not None:
                self._shared_model = model
                self.setModel(model)

        self.setMaximumWidth(360)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        completer = QCompleter(self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        if self._shared_model is not None:
            completer.setModel(self._shared_model)
        self.setCompleter(completer)
        self._completer = completer

        self.currentIndexChanged.connect(self._on_index_changed)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.editingFinished.connect(self._snap_text_to_selection)

    def set_undo_stack(self, undo_stack: QUndoStack) -> None:
        self._undo_stack = undo_stack

    def bind(self, target, attr: str) -> None:
        """Snap to ``target.attr``, rebuilding choices only when needed.

        Wraps the whole rebuild in ``silenced``: :meth:`QComboBox.clear`
        and the first :meth:`addItem` both fire ``currentIndexChanged`,
        which would otherwise push a spurious ``SetAttrCommand`` for
        index 0 on every selection switch.

        In shared-model mode the model is already populated at the
        session level — just setCurrentIndex.

        In labels-provider mode, skips the rebuild when the provider
        returns the same list object as last time (identity check).
        """
        self._target = target
        self._attr = attr
        if self._shared_model is not None:
            with silenced(self):
                self.setCurrentIndex(self._ensure_index_for(getattr(target, attr)))
            return
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
        if self._target is None or self._undo_stack is None:
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

        # Reverse lookup: main_sprite value -> first slot index that uses it.
        # Public so the host editor can decorate list / title labels with a
        # "displayed as" suffix when the slot has been reskinned.
        # Pickable list covers every sprite-map slot.
        with span(f"pickable_build×{len(sprite_map)}"):
            self.sprite_to_base: Dict[int, int] = {}
            self._pickable: List[Tuple[int, str]] = []
            for base_id in range(len(sprite_map)):
                self._pickable.append((base_id, session.digimon_display_name(base_id)))
                self.sprite_to_base.setdefault(sprite_map[base_id].main_sprite, base_id)

        self.group = QGroupBox("Display / Reskin")
        # Form column + preview column sit side by side when there's room; a
        # FlowLayout drops the previews below the form once the pane narrows,
        # so this box shrinks with the rest of the detail form instead of
        # pinning a wide floor.
        root = QVBoxLayout(self.group)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(4)
        row_wrap = QWidget()
        row_flow = FlowLayout(row_wrap, margin=0, h_spacing=8, v_spacing=6)
        root.addWidget(row_wrap)
        make_height_for_width(row_wrap)
        make_height_for_width(self.group)

        form_col = QWidget()
        outer = QVBoxLayout(form_col)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        row_flow.addWidget(form_col)

        # Right column: 2x2 grid of live sprite previews, one per pak the
        # sprite_map references. Pixmaps are pulled from session caches so
        # the first edit pays decode cost (~5-15ms for SPR, ~5ms for MCHR,
        # ~10-30ms for BTCHR) and subsequent paints are free.
        #   row 0: Overworld | Battle
        #   row 1: Portrait  | Mini
        self._preview_size = 80
        self._overworld_preview = self._make_preview_label()
        self._battle_preview = self._make_preview_label()
        self._portrait_preview = self._make_preview_label()
        self._mini_preview = self._make_preview_label()

        preview_col = QWidget()
        preview_grid = QGridLayout(preview_col)
        preview_grid.setContentsMargins(0, 0, 0, 0)
        preview_grid.setHorizontalSpacing(8)
        preview_grid.setVerticalSpacing(6)
        for col, (caption, label) in enumerate((
            ("Overworld", self._overworld_preview),
            ("Battle", self._battle_preview),
        )):
            preview_grid.addWidget(self._make_preview_caption(caption), 0, col, Qt.AlignHCenter)
            preview_grid.addWidget(label, 1, col, Qt.AlignHCenter)
        for col, (caption, label) in enumerate((
            ("Portrait", self._portrait_preview),
            ("Mini", self._mini_preview),
        )):
            preview_grid.addWidget(self._make_preview_caption(caption), 2, col, Qt.AlignHCenter)
            preview_grid.addWidget(label, 3, col, Qt.AlignHCenter)
        preview_grid.setAlignment(Qt.AlignTop)
        row_flow.addWidget(preview_col)

        # --- top: Appears-as picker (existing reskin convenience) ---
        top_form = QFormLayout()
        top_form.setContentsMargins(0, 0, 0, 0)
        top_form.setSpacing(4)
        with span("appears_as_combo"):
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
            with span(f"appears_as_addItem×{len(self._pickable)}"):
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

        # --- reskin-safety: spawn-budget (CHRSIZE.BIN) vs displayed sprite ---
        # The wild-encounter roll budgets each enemy against
        # CHRSIZE.BIN[lo==id].hi (Σ fs ≤ 1440), but renders main_sprite.
        # A reskin desyncs them → over-spawn → VRAM crash. Warn + offer a
        # one-click sync of the budget to the displayed sprite's real fs.
        self._pending_footprint_sync: Optional[Tuple[int, int]] = None
        self._footprint_warn = QLabel("")
        self._footprint_warn.setWordWrap(True)
        self._footprint_warn.setStyleSheet("color: #c0392b;")
        self._footprint_sync_btn = QPushButton("Sync")
        self._footprint_sync_btn.setMaximumWidth(64)
        self._footprint_sync_btn.clicked.connect(self._on_sync_footprint)
        fp_row = QHBoxLayout()
        fp_row.setContentsMargins(0, 0, 0, 0)
        fp_row.setSpacing(6)
        fp_row.addWidget(self._footprint_warn, 1)
        fp_row.addWidget(self._footprint_sync_btn, 0, Qt.AlignTop)
        self._footprint_widget = QWidget()
        self._footprint_widget.setLayout(fp_row)
        self._footprint_widget.setVisible(False)
        outer.addWidget(self._footprint_widget)

        # --- divider checkbox ---
        # Short label so it doesn't pin a wide floor on a narrow screen; the
        # full meaning is in the tooltip.
        self._customize_checkbox = QCheckBox("Customize fields")
        self._customize_checkbox.setToolTip(
            "Edit the sprite-map and battle-string fields directly."
        )
        self._customize_checkbox.setChecked(False)
        self._customize_checkbox.toggled.connect(self._on_customize_toggled)
        outer.addWidget(self._customize_checkbox)

        # --- bottom: manual editors (one per SpriteMapEntry field + battle string) ---
        first_slot = sprite_map[0]
        first_str = battle_strings[0]
        self._id_spin = BoundSpinBox(first_slot, "id", 4, undo_stack, hex_display=True)
        # Party-follower overworld sprite — full MCHR_CHR list, name-filtered
        # via the shared picker (mirrors the BTCHR / SPR pickers below so all
        # three sprite fields read the same way). All four pickers opt into
        # session-level shared QStandardItemModels — pre-built at ROM load
        # (RomSession.picker_model) so editor open just setModels instead of
        # paying ~280ms of addItem calls per open.
        # Pickers are pooled at the session level (see
        # RomSession.acquire_sprite_pickers) — constructed once at ROM
        # load and reparented into this row's layout on acquire. Skips
        # the ~46ms-per-picker setEditable + QCompleter setup that
        # dominated editor open before pooling.
        with span("sprite_pickers_acquire"):
            pickers, self._picker_pool_generation = session.acquire_sprite_pickers()
            self._overworld_combo = pickers[0]
            self._main_sprite_combo = pickers[1]
            self._upper_sprite_low_combo = pickers[2]
            self._upper_sprite_high_combo = pickers[3]
            # Pool builds pickers with no undo stack (session has none);
            # inject the host editor's stack now so currentIndexChanged
            # pushes commands onto the right history.
            for picker in pickers:
                picker.set_undo_stack(undo_stack)
        with span("sprite_pickers_bind"):
            for picker, attr in self._sprite_picker_bindings(first_slot):
                picker.bind(first_slot, attr)
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
        with span("battle_str_combo"):
            self._battle_str_combo = BoundIdCombo(
                first_str, "value", bs_choices, undo_stack,
                shared_kind="battle_strings",
            )

        # Manual editors live in their own container so the whole block
        # (labels included) can be hidden until the user opts in via the
        # checkbox — collapsed, it stops contributing its width to the form's
        # minimum, keeping the box narrow-screen friendly.
        self._manual_container = QWidget()
        manual_form = QFormLayout(self._manual_container)
        manual_form.setContentsMargins(0, 0, 0, 0)
        manual_form.setSpacing(4)
        manual_form.addRow("ID", self._id_spin)
        manual_form.addRow("Overworld (party)", self._overworld_combo)
        manual_form.addRow("Main sprite", self._main_sprite_combo)
        manual_form.addRow("Icon Portrait", self._upper_sprite_low_combo)
        manual_form.addRow("Battle Mini", self._upper_sprite_high_combo)
        manual_form.addRow("Battle string", self._battle_str_combo)
        outer.addWidget(self._manual_container)

        self._manual_widgets: List[QWidget] = [
            self._id_spin, self._overworld_combo, self._main_sprite_combo,
            self._upper_sprite_low_combo, self._upper_sprite_high_combo,
            self._battle_str_combo,
        ]
        self._on_customize_toggled(False)

        # Live preview hooks: re-render the matching preview tile whenever
        # the user picks a different sprite. Stored as bound methods so
        # release_pickers() can disconnect cleanly — pickers are pooled at
        # the session level, so leaving the connections live would leak
        # into the next editor that acquires them.
        self._main_sprite_combo.currentIndexChanged.connect(self._refresh_battle_preview)
        self._main_sprite_combo.currentIndexChanged.connect(self._refresh_footprint_check)
        self._overworld_combo.currentIndexChanged.connect(self._refresh_overworld_preview)
        self._upper_sprite_low_combo.currentIndexChanged.connect(self._refresh_portrait_preview)
        self._upper_sprite_high_combo.currentIndexChanged.connect(self._refresh_mini_preview)

    def _sprite_picker_bindings(self, slot) -> List[Tuple["_SpriteListPicker", str]]:
        """The four sprite pickers paired with the slot attr they bind to.

        Centralizes the picker/attr list so initial bind and rebind use
        the same iteration order without duplicating the mapping.
        """
        return [
            (self._overworld_combo, "unknown_0x4"),
            (self._main_sprite_combo, "main_sprite"),
            (self._upper_sprite_low_combo, "upperscreen_low"),
            (self._upper_sprite_high_combo, "upperscreen_high"),
        ]

    def _btchr_labels_provider(self) -> List[str]:
        # Delegate to the session-level cache so opening a second editor
        # (or toggling Customize on another digimon) doesn't recompute.
        return self._session.get_btchr_group_labels()

    def _mchr_labels_provider(self) -> List[str]:
        return self._session.get_mchr_labels()

    def _spr_labels_provider(self) -> List[str]:
        return self._session.get_spr_labels()

    def _on_customize_toggled(self, enabled: bool) -> None:
        # Manual widgets stay disabled when there's no slot to bind against
        # (see `_apply_state`); guard against re-enabling them in that case.
        if not self._has_slot_for_target():
            enabled = False
        # Fields stay visible at all times (user preference) — the checkbox
        # only gates whether they're editable.
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

    def release_pickers(self) -> None:
        """Detach the pooled sprite pickers before this row is destroyed.

        Host editors call this from their ``aboutToTeardown`` hook so
        Qt's parent-deletes-children rule doesn't drag the pool widgets
        down with the editor. Passes our generation token so the session
        no-ops when a newer SpriteMapRow has already taken the pool —
        otherwise this teardown would steal pickers back from the
        just-constructed editor.
        """
        for combo, slot in (
            (self._main_sprite_combo, self._refresh_battle_preview),
            (self._main_sprite_combo, self._refresh_footprint_check),
            (self._overworld_combo, self._refresh_overworld_preview),
            (self._upper_sprite_low_combo, self._refresh_portrait_preview),
            (self._upper_sprite_high_combo, self._refresh_mini_preview),
        ):
            try:
                combo.currentIndexChanged.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._session.release_sprite_pickers(self._picker_pool_generation)

    def rebind(self, target: Any) -> None:
        self._target_id = target.id
        if self._has_slot_for_target():
            slot = self._sprite_map[target.id]
            self._id_spin.rebind(slot)
            for picker, attr in self._sprite_picker_bindings(slot):
                picker.bind(slot, attr)
            self._battle_str_combo.rebind(self._battle_strings[target.id])
        self._apply_state()

    def refresh(self) -> None:
        if self._target_id < 0:
            return
        # refresh() runs on external state changes (undo/redo, tab switch).
        # SPR / BTCHR labels embed sprite_map cross-refs and depend on pak
        # length, so drop the session-level caches here — the next bind()
        # recomputes. Cheap in-form changes (selection switches) go through
        # rebind() and keep using the caches.
        self._session.invalidate_sprite_label_caches()
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
        # Refresh previews first — each one handles its own "no slot / bad
        # id" case so the early-return branches below don't have to.
        self._refresh_all_previews()
        self._refresh_footprint_check()

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

    def _make_preview_label(self) -> QLabel:
        """Build one fixed-size preview cell with the standard chrome."""
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setFixedSize(self._preview_size, self._preview_size)
        label.setStyleSheet(
            "QLabel { background: palette(base); border: 1px solid palette(mid); }"
        )
        label.setText("—")
        return label

    def _make_preview_caption(self, text: str) -> QLabel:
        """Small caption above a preview cell."""
        cap = QLabel(text)
        cap.setAlignment(Qt.AlignHCenter)
        cap.setStyleSheet("color: palette(mid); font-size: 10px;")
        return cap

    def _set_preview(self, label: QLabel, pix) -> None:
        """Helper: paint ``pix`` into ``label``, falling back to a dash."""
        if pix is None:
            label.clear()
            label.setText("—")
        else:
            label.setPixmap(pix)

    def _refresh_all_previews(self) -> None:
        """Repaint all four preview tiles from the current slot."""
        self._refresh_battle_preview()
        self._refresh_overworld_preview()
        self._refresh_portrait_preview()
        self._refresh_mini_preview()

    def _refresh_battle_preview(self, *_args) -> None:
        if not self._has_slot_for_target():
            self._set_preview(self._battle_preview, None)
            return
        group_idx = self._sprite_map[self._target_id].main_sprite
        self._set_preview(self._battle_preview, self._session.battle_sprite_pixmap(
            group_idx, max_size=self._preview_size
        ))

    def _refresh_overworld_preview(self, *_args) -> None:
        if not self._has_slot_for_target():
            self._set_preview(self._overworld_preview, None)
            return
        mchr_idx = self._sprite_map[self._target_id].unknown_0x4
        self._set_preview(self._overworld_preview, self._session.mchr_sprite_pixmap(
            mchr_idx, max_size=self._preview_size
        ))

    def _refresh_portrait_preview(self, *_args) -> None:
        if not self._has_slot_for_target():
            self._set_preview(self._portrait_preview, None)
            return
        spr_idx = self._sprite_map[self._target_id].upperscreen_low
        self._set_preview(self._portrait_preview, self._session.spr_sprite_pixmap(
            spr_idx, max_size=self._preview_size
        ))

    def _refresh_mini_preview(self, *_args) -> None:
        if not self._has_slot_for_target():
            self._set_preview(self._mini_preview, None)
            return
        spr_idx = self._sprite_map[self._target_id].upperscreen_high
        self._set_preview(self._mini_preview, self._session.spr_sprite_pixmap(
            spr_idx, max_size=self._preview_size
        ))

    def _refresh_footprint_check(self, *_args) -> None:
        """Show/hide the spawn-budget mismatch warning for the current id.

        Reads ``main_sprite`` from the model (the source of truth, updated
        synchronously by the picker/reskin command) and asks the session
        whether ``CHRSIZE.BIN[lo==id].hi`` matches the displayed sprite's
        real footprint. Surfaces a one-click Sync when they diverge;
        hidden when there's no slot / no chrsize entry / in sync.
        """
        tid = self._target_id
        if tid < 0 or not self._has_slot_for_target():
            self._pending_footprint_sync = None
            self._footprint_widget.setVisible(False)
            return
        main_sprite = self._sprite_map[tid].main_sprite
        mismatch = self._session.chrsize_footprint_mismatch(tid, main_sprite)
        if mismatch is None:
            self._pending_footprint_sync = None
            self._footprint_widget.setVisible(False)
            return
        entry_group, budget_fs, real_fs = mismatch
        self._pending_footprint_sync = (entry_group, real_fs)
        if real_fs > budget_fs:
            tail = "wild encounters can over-spawn and crash (VRAM)."
        else:
            tail = "wild encounters under-spawn this id."
        self._footprint_warn.setText(
            f"⚠ Spawn budget {budget_fs} tiles ≠ displayed sprite "
            f"{real_fs} — {tail}"
        )
        self._footprint_widget.setVisible(True)

    def _on_sync_footprint(self) -> None:
        if self._pending_footprint_sync is None:
            return
        entry_group, real_fs = self._pending_footprint_sync
        name = self._session.digimon_display_name(self._target_id)
        self._undo_stack.push(
            SyncChrsizeFootprintCommand(
                self._session, entry_group, real_fs,
                f"Sync spawn footprint ({name})",
                on_change=self._refresh_footprint_check,
            )
        )

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

        if (sprite_entry.unknown_0x4 == source_sprite.unknown_0x4
                and sprite_entry.main_sprite == source_sprite.main_sprite
                and sprite_entry.upperscreen_sprites == source_sprite.upperscreen_sprites
                and str_entry.value == source_str.value):
            return

        self._undo_stack.push(
            ReskinSlotCommand(
                sprite_entry,
                str_entry,
                source_sprite.unknown_0x4,
                source_sprite.main_sprite,
                source_sprite.upperscreen_sprites,
                source_str.value,
            )
        )
        # The quick-reskin rewrites main_sprite on the model directly
        # (the manual pickers don't fire), so refresh the previews and the
        # spawn-budget check off the updated slot.
        self._refresh_all_previews()
        self._refresh_footprint_check()


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


def displayed_as_name(
    sprite_map: List[model.SpriteMapEntry],
    sprite_to_base: Dict[int, int],
    digimon_id: int,
    own_name: str,
    name_resolver: Optional[Callable[[int], str]] = None,
) -> str:
    """Bare-name variant of :func:`displayed_as_suffix` for column views.

    Returns just the reskin target's display name (no ``[brackets]``,
    no leading padding) so it can live in its own list column, or ``""``
    when the slot renders as its own name. ``"???"`` when the slot's
    sprite doesn't resolve to any base id.
    """
    suffix = displayed_as_suffix(
        sprite_map, sprite_to_base, digimon_id, own_name,
        name_resolver=name_resolver,
    )
    if not suffix:
        return ""
    # ``displayed_as_suffix`` returns "  [Name]" — strip the padding and
    # brackets rather than duplicating the resolution logic.
    return suffix.strip().lstrip("[").rstrip("]")
