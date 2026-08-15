"""Shop editor — the buy lists inside overlay 9_9 (:mod:`digimon_core.shop`).

Each shop is a single-type stock list (item / equipment / farm-goods). Because a
shop renders + prices its stock by *type*, mixing types in one list breaks it
(a farm item in an equipment shop shows a bogus price). So every slot's picker
is constrained to the shop's own type, and edits are in-place swaps only (the
lists are contiguous + pointer-indexed, so counts can't change).
"""
from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, shop as shop_mod

from ..commands import SetShopItemCommand
from .form_helpers import NoWheelComboBox, wrap_in_scroll
from .record_list_panel import RecordListPanel


def _item_name(item_id: int) -> str:
    return constants.ITEM_ID_TO_STR.get(item_id, f"<0x{item_id:x}>")


class ShopEditor(QWidget):
    _CURSOR_KEY = "shops"

    def __init__(self, shops, undo_stack: QUndoStack, session, parent=None):
        super().__init__(parent)
        self._shops = shops
        self._undo_stack = undo_stack
        self._session = session
        self._current = None
        self._slot_combos: List[NoWheelComboBox] = []
        # Per-type item choices, built once (id -> row, and the (id, name) list).
        self._choices: Dict[str, List] = {
            t: shop_mod.items_of_type(t) for t in shop_mod.SHOP_ITEM_TYPES
        }

        self._list_panel = RecordListPanel(
            shops,
            columns_for=self._columns,
            headers=("Shop", "Items", "Contents"),
        )
        self._list_panel.indexSelected.connect(self._on_selection)

        self._detail = self._build_detail()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 640])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_index(int(remembered)):
            self._list_panel.select_first()

    def _columns(self, _ix: int, s):
        preview = ", ".join(_item_name(i) for i in s.item_ids[:4])
        if len(s.item_ids) > 4:
            preview += ", …"
        return (s.label, str(len(s.item_ids)), preview)

    def _build_detail(self) -> QWidget:
        self._detail_inner = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_inner)
        self._detail_layout.setContentsMargins(10, 10, 10, 10)
        self._detail_layout.setSpacing(6)

        self._header = QLabel("Select a shop.")
        self._header.setWordWrap(True)
        self._header.setStyleSheet("font-weight: bold;")
        self._detail_layout.addWidget(self._header)

        self._note = QLabel(
            "Each slot is limited to this shop's item type — mixing types "
            "(e.g. a farm item into an equipment shop) makes the shop show "
            "wrong prices in-game. Slots can be swapped, not added/removed.")
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: palette(mid);")
        self._detail_layout.addWidget(self._note)

        self._slots_host = QWidget()  # replaced wholesale on each selection
        QVBoxLayout(self._slots_host).setContentsMargins(0, 0, 0, 0)
        self._detail_layout.addWidget(self._slots_host)
        self._detail_layout.addStretch(1)

        return wrap_in_scroll(self._detail_inner)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._shops)):
            return
        self._current = self._shops[ix]
        self._session.remember_selection(self._CURSOR_KEY, ix)
        self._rebuild_slots()

    def _rebuild_slots(self) -> None:
        s = self._current
        self._slot_combos = []
        self._header.setText(f"{s.label}  ·  {len(s.item_ids)} slots")

        # Build a fresh host + form and swap it in — simpler and safer than
        # tearing down QFormLayout rows one by one.
        new_host = QWidget()
        form = QFormLayout(new_host)
        form.setContentsMargins(0, 6, 0, 0)
        form.setHorizontalSpacing(10)
        choices = self._choices[s.item_type]
        for slot, item_id in enumerate(s.item_ids):
            combo = NoWheelComboBox()
            for cid, cname in choices:
                combo.addItem(f"{cname}  (0x{cid:x})", cid)
            self._set_combo_id(combo, item_id)
            combo.currentIndexChanged.connect(
                lambda _i, sl=slot, c=combo: self._on_slot_changed(sl, c))
            form.addRow(f"Slot {slot + 1}", combo)
            self._slot_combos.append(combo)

        self._detail_layout.replaceWidget(self._slots_host, new_host)
        self._slots_host.deleteLater()
        self._slots_host = new_host

    @staticmethod
    def _set_combo_id(combo: NoWheelComboBox, item_id: int) -> None:
        combo.blockSignals(True)
        ix = combo.findData(item_id)
        if ix < 0:
            # Out-of-type / unknown id (shouldn't happen for vanilla) — surface
            # it as a literal so nothing is silently lost.
            combo.insertItem(0, f"<0x{item_id:x}>", item_id)
            ix = 0
        combo.setCurrentIndex(ix)
        combo.blockSignals(False)

    def _on_slot_changed(self, slot: int, combo: NoWheelComboBox) -> None:
        if self._current is None:
            return
        new_id = combo.currentData()
        if new_id is None or new_id == self._current.item_ids[slot]:
            return
        self._undo_stack.push(SetShopItemCommand(
            self._current, slot, int(new_id),
            on_change=self._refresh_current))

    def _refresh_current(self) -> None:
        """Re-sync the slot combos after an undo/redo flip (the list-panel
        contents preview refreshes on the next reselection)."""
        s = self._current
        if s is None:
            return
        for slot, combo in enumerate(self._slot_combos):
            if slot < len(s.item_ids):
                self._set_combo_id(combo, s.item_ids[slot])
