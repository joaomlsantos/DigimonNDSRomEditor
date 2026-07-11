"""Encounters tab — a field map's `/ec/ENCTBL.BIN` assignment.

Each field map has one 8-byte entry in ``ENCTBL.BIN`` (decoded in
``research_docs/claude_notes/map_encounter_table.md``) that assigns it a
wild-encounter area (which digimon spawn) + a battle background, plus two
still-undecoded params. This tab surfaces that entry per map: the area and
background are editable dropdowns; the two unknowns are raw editable
spinboxes; and the selected area's spawn list is previewed read-only with a
jump into the standalone Wild Encounters editor.

The reverse direction ("which maps use this area") lives in the Wild
Encounters editor's detail panel.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import btmap, loaders

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundIdCombo,
    BoundSpinBox,
    make_form,
)


class MapEncounterTab(QWidget):
    """Per-map encounter-assignment editor (one ENCTBL.BIN entry)."""

    def __init__(
        self,
        session,
        undo_stack,
        navigate_to_area: Optional[Callable[[int], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._navigate_to_area = navigate_to_area
        self._entry = None

        # Choice lists — areas (labelled by location) and battle
        # backgrounds (bare ids; btmap has no friendly names).
        areas = session.wild_encounter_areas
        self._area_choices: List[Tuple[int, str]] = [
            (i, f"{i:02d} — {loaders.getLocationForAreaIndex(i) or '?'}")
            for i in range(len(areas))
        ]
        try:
            n_bg = len(btmap.discover_map_ids(session.vanilla_file_table()))
        except Exception:  # noqa: BLE001 — non-DWDD / stripped ROM
            n_bg = 0
        self._bg_choices: List[Tuple[int, str]] = [
            (i, f"{i:02d}") for i in range(n_bg)
        ]
        # Wild-battle BGM (+6) choices — every BGM slot the SDAT exposes,
        # labelled through the same ``session.bgm_label`` the Sound editor
        # and cutscene SET_MUSIC cards use (so a user-renamed track shows
        # its label here too).
        try:
            n_bgm = len(session.vanilla_bgm_summary()) + len(session.staged_bgm_additions())
        except Exception:  # noqa: BLE001
            n_bgm = 0
        self._bgm_choices: List[Tuple[int, str]] = [
            (i, f"0x{i:04x} — {session.bgm_label(i)}") for i in range(n_bgm)
        ]

        self._build_ui()

    # ---- construction ---------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._status = QLabel("Select a field map.")
        self._status.setStyleSheet("color: #888;")
        outer.addWidget(self._status)

        # Bound widgets need a target at construction; seed on the first
        # entry (rebind per map in set_map). Guard the empty-table case.
        table = self._session.map_encounter_table
        seed = table[0] if table else None

        box = QGroupBox("Encounter assignment")
        form = make_form(box)
        if seed is not None:
            self._area_combo = BoundIdCombo(
                seed, "area_index", self._area_choices, self._undo_stack,
                none_value=0xFFFF, none_label="(none)",
            )
            self._area_combo.currentIndexChanged.connect(
                lambda _i: self._refresh_preview()
            )
            self._bg_combo = BoundIdCombo(
                seed, "battle_bg", self._bg_choices, self._undo_stack,
                none_value=0xFFFF, none_label="(none)",
            )
            # +6 = wild-encounter battle BGM (music id). +4 is still
            # undecoded — surfaced as a raw editable spin (likely an
            # encounter rate, unconfirmed) rather than pretending a
            # meaning. See feedback_no_fabricated_game_mechanics.
            self._bgm_combo = BoundIdCombo(
                seed, "wild_battle_bgm", self._bgm_choices, self._undo_stack,
            )
            self._u4_spin = BoundSpinBox(seed, "unknown_0x4", 2, self._undo_stack)
            form.addRow("Wild encounter area", self._area_combo)
            form.addRow("Battle background", self._bg_combo)
            form.addRow("Wild battle music", self._bgm_combo)
            form.addRow("Unknown +4", self._u4_spin)
            self._info = QLabel("—")
            self._info.setStyleSheet("color: #888; font-size: 10px;")
            form.addRow("", self._info)
        else:
            self._area_combo = self._bg_combo = None
            self._bgm_combo = self._u4_spin = self._info = None
            form.addRow(QLabel("<i>No encounter table in this ROM.</i>"))
        outer.addWidget(box)

        # Spawn preview + jump-to-area.
        self._spawn_box = QGroupBox("Spawns in this area")
        spawn_layout = QVBoxLayout(self._spawn_box)
        spawn_layout.setContentsMargins(8, 4, 8, 4)
        spawn_layout.setSpacing(4)
        self._spawn_label = QLabel("—")
        self._spawn_label.setWordWrap(True)
        self._spawn_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._spawn_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        spawn_layout.addWidget(self._spawn_label)
        self._open_area_btn = QPushButton("Open in Wild Encounters editor →")
        self._open_area_btn.clicked.connect(self._on_open_area)
        self._open_area_btn.setEnabled(self._navigate_to_area is not None)
        spawn_layout.addWidget(self._open_area_btn, 0, Qt.AlignLeft)
        outer.addWidget(self._spawn_box, 1)

        outer.addStretch(1)

        # Keep the widgets in sync after undo/redo (the entry's fields can
        # change from an undone edit made on this same tab).
        if self._undo_stack is not None:
            self._undo_stack.indexChanged.connect(self._on_undo_index_changed)

    # ---- public API -----------------------------------------------------

    def set_map(self, map_id: int) -> None:
        """Bind the tab to field map ``map_id``'s encounter entry."""
        entry = self._session.map_encounter_entry(map_id)
        self._entry = entry
        if entry is None:
            self._status.setText(
                f"Map {map_id}: no encounter table entry."
            )
            self._set_widgets_enabled(False)
            self._spawn_label.setText("—")
            return
        self._status.setText(
            f"Map {map_id}  ·  ENCTBL entry @0x{entry.offset:06x}"
        )
        self._set_widgets_enabled(True)
        if self._area_combo is not None:
            self._area_combo.rebind(entry)
            self._bg_combo.rebind(entry)
            self._bgm_combo.rebind(entry)
            self._u4_spin.rebind(entry)
            self._info.setText(
                f"area=0x{entry.area_index:04x}  bg=0x{entry.battle_bg:04x}"
                f"  bgm=0x{entry.wild_battle_bgm:04x}"
            )
        self._refresh_preview()

    # ---- helpers --------------------------------------------------------

    def _set_widgets_enabled(self, on: bool) -> None:
        for w in (self._area_combo, self._bg_combo, self._bgm_combo, self._u4_spin):
            if w is not None:
                w.setEnabled(on)
        self._open_area_btn.setEnabled(on and self._navigate_to_area is not None)

    def _current_area(self) -> int:
        return self._entry.area_index if self._entry is not None else 0xFFFF

    def _refresh_preview(self) -> None:
        if self._entry is not None and self._info is not None:
            self._info.setText(
                f"area=0x{self._entry.area_index:04x}"
                f"  bg=0x{self._entry.battle_bg:04x}"
                f"  bgm=0x{self._entry.wild_battle_bgm:04x}"
            )
        area_ix = self._current_area()
        areas = self._session.wild_encounter_areas
        if area_ix == 0xFFFF or not (0 <= area_ix < len(areas)):
            self._spawn_box.setTitle("Spawns in this area")
            self._spawn_label.setText(
                "<span style='color:#888;'>No wild-encounter area assigned.</span>"
            )
            self._open_area_btn.setEnabled(False)
            return
        area = areas[area_ix]
        loc = loaders.getLocationForAreaIndex(area_ix) or "?"
        self._spawn_box.setTitle(f"Spawns — area {area_ix:02d} ({loc})")
        names = []
        for enc in getattr(area, "encounters", []):
            did = getattr(enc, "digimon_id", 0)
            names.append(self._session.digimon_display_name(did))
        if names:
            self._spawn_label.setText(", ".join(names))
        else:
            self._spawn_label.setText(
                "<span style='color:#888;'>(no encounters in this area)</span>"
            )
        self._open_area_btn.setEnabled(self._navigate_to_area is not None)

    def _on_open_area(self) -> None:
        if self._navigate_to_area is None:
            return
        area_ix = self._current_area()
        if 0 <= area_ix < len(self._session.wild_encounter_areas):
            self._navigate_to_area(area_ix)

    def _on_undo_index_changed(self, _ix: int = 0) -> None:
        # Re-seed the widgets + preview from the (possibly reverted) entry.
        if self._entry is not None:
            self.set_map(self._entry.map_id)
