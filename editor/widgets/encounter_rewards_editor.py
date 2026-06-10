"""EncounterRewardTable editor.

Each table is a parallel pair of arrays (probabilitiesArray, rewardsArray),
both of length N. In Dusk/Dawn every table observed is N=8, so we render 8
fixed rows in the detail panel and rebind on selection. If a future ROM trips
a different N, `_build_slots` would need to rebuild the rows per table — kept
simple for now since vanilla is uniform.

Reward field encoding:
  * 0               → empty slot
  * 0 < value < 0x8000 → item id
  * value >= 0x8000 → money amount, where amount = 0x10000 - value

The editor surfaces this as a "Type" combo (Empty / Item / Money) and swaps
between an item picker and a money spinbox. The money spinbox shows the
amount in user-visible bit (the ROM encoding is hidden from the user — set/get
go through `_pack_money` / `_unpack_money`).
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from digimon_core import model

from ..commands import SetAttrCommand
from .form_helpers import (
    BoldGroupBox as QGroupBox,
    BoundIdCombo,
    BoundSpinBox,
    NoWheelComboBox,
    NoWheelSpinBox,
    item_choices,
    silenced,
    with_open_button,
    with_open_button_placeholder,
    wrap_in_scroll,
)
from .record_list_panel import RecordListPanel


# Money encoding boundary. raw >= 0x8000 means money; the amount on screen is
# 0x10000 - raw. Max user-visible amount is 0x8000 (encoded as raw=0x8000).
MONEY_BOUNDARY = 0x8000
MAX_MONEY_AMOUNT = 0x8000


TYPE_EMPTY = 0
TYPE_ITEM = 1
TYPE_MONEY = 2

# Probabilities across a table's slots must sum to exactly this value
# (8 slots × 25 in vanilla, but the constraint is the sum, not the per-slot value).
EXPECTED_PROBABILITY_SUM = 200


def _record_label(ix: int, rec: model.EncounterRewardTable) -> str:
    return f"Table {ix:03d}  ({len(rec.probabilitiesArray)} slots)  offset 0x{rec.offset:08x}"


def _slot_type(raw: int) -> int:
    if raw == 0:
        return TYPE_EMPTY
    if raw >= MONEY_BOUNDARY:
        return TYPE_MONEY
    return TYPE_ITEM


def _unpack_money(raw: int) -> int:
    return 0x10000 - raw


def _pack_money(amount: int) -> int:
    return 0x10000 - amount


def _reward_item_choices() -> List[tuple]:
    """Item picker for encounter-reward slots.

    Item id 0 ("Scale") is filtered out because the slot encoding can't
    represent it — raw=0 means "empty slot", so a Scale reward would
    silently re-encode as empty. Suppressing id 0 from the picker keeps
    the type/value invariants consistent.
    """
    return [(cid, name) for cid, name in item_choices() if cid != 0]


class _RewardSlotProxy:
    """Adapter so BoundSpinBox can target one (probability, reward) slot."""

    def __init__(self, table: model.EncounterRewardTable, ix: int):
        self._table = table
        self._ix = ix

    @property
    def probability(self) -> int:
        return self._table.probabilitiesArray[self._ix]

    @probability.setter
    def probability(self, value: int) -> None:
        self._table.probabilitiesArray[self._ix] = value

    @property
    def reward(self) -> int:
        return self._table.rewardsArray[self._ix]

    @reward.setter
    def reward(self, value: int) -> None:
        self._table.rewardsArray[self._ix] = value


class _SlotRow:
    """One (probability, reward) editing row.

    Layout: probability spinbox, Type combo (Empty/Item/Money), and a stacked
    value widget that swaps between empty/item-combo/money-spinbox based on the
    selected type. Type changes and money edits push manual SetAttrCommands so
    the user-facing amount can be different from the ROM encoding.
    """

    def __init__(self, proxy: _RewardSlotProxy, undo_stack: QUndoStack):
        self._proxy = proxy
        self._undo_stack = undo_stack

        self.prob_spin = BoundSpinBox(proxy, "probability", 2, undo_stack)

        self.type_combo = NoWheelComboBox()
        self.type_combo.addItem("(empty)", userData=TYPE_EMPTY)
        self.type_combo.addItem("Item", userData=TYPE_ITEM)
        self.type_combo.addItem("Money", userData=TYPE_MONEY)

        self._empty_filler = QLabel("—")
        self._empty_filler.setStyleSheet("color: palette(mid);")
        self.item_combo = BoundIdCombo(
            proxy, "reward", _reward_item_choices(), undo_stack,
            details_kind="item", shared_kind="item_reward",
        )
        self.money_spin = NoWheelSpinBox()
        self.money_spin.setRange(1, MAX_MONEY_AMOUNT)
        self.money_spin.setSuffix(" bit")
        # Match the item combo's cap so money rows don't stretch to fill the
        # whole reward column.
        self.money_spin.setMaximumWidth(280)

        # All three pages get the same right-side footprint (button or
        # button-width spacer) so the input's right edge aligns across rows
        # regardless of the actual reward-column width.
        self.value_stack = QStackedWidget()
        self.value_stack.addWidget(with_open_button_placeholder(self._empty_filler))  # TYPE_EMPTY
        self.value_stack.addWidget(with_open_button(self.item_combo))                 # TYPE_ITEM
        self.value_stack.addWidget(with_open_button_placeholder(self.money_spin))     # TYPE_MONEY

        self._sync_from_model()

        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.money_spin.valueChanged.connect(self._on_money_changed)

    def _sync_from_model(self) -> None:
        raw = self._proxy.reward
        slot_type = _slot_type(raw)
        with silenced(self.type_combo):
            self.type_combo.setCurrentIndex(slot_type)
        with silenced(self.money_spin):
            self.money_spin.setValue(_unpack_money(raw) if slot_type == TYPE_MONEY else 1)
        # item_combo is bound to proxy.reward — its ensure_index_for handles the
        # case where the raw value isn't a recognized item id by adding a
        # fallback row. It's only visible when slot_type == TYPE_ITEM.
        self.value_stack.setCurrentIndex(slot_type)

    def _push_raw(self, new_raw: int) -> None:
        if self._proxy.reward == new_raw:
            return
        self._undo_stack.push(SetAttrCommand(self._proxy, "reward", new_raw))

    def _on_type_changed(self, _ix: int) -> None:
        new_type = self.type_combo.currentData(Qt.UserRole)
        cur_raw = self._proxy.reward
        cur_type = _slot_type(cur_raw)
        if new_type == cur_type:
            self.value_stack.setCurrentIndex(new_type)
            return
        if new_type == TYPE_EMPTY:
            self._push_raw(0)
        elif new_type == TYPE_MONEY:
            self._push_raw(_pack_money(self.money_spin.value()))
        elif new_type == TYPE_ITEM:
            # Pick the first reward-eligible item id (id 0 is filtered out
            # because raw=0 collides with the "empty" encoding).
            choices = _reward_item_choices()
            default_id = choices[0][0] if choices else 1
            self._push_raw(default_id)
        # _sync_from_model will run via refresh() driven by undo_stack.indexChanged

    def _on_money_changed(self, amount: int) -> None:
        new_raw = _pack_money(amount)
        if self._proxy.reward == new_raw:
            return
        # only commit while in money-mode; otherwise this would clobber an
        # item-id selection that hasn't been confirmed yet
        if _slot_type(self._proxy.reward) != TYPE_MONEY:
            return
        self._push_raw(new_raw)

    def rebind(self, new_proxy: _RewardSlotProxy) -> None:
        self._proxy = new_proxy
        self.prob_spin.rebind(new_proxy)
        self.item_combo.rebind(new_proxy)
        self._sync_from_model()

    def refresh(self) -> None:
        self.prob_spin.refresh()
        self.item_combo.refresh()
        self._sync_from_model()


class EncounterRewardsEditor(QWidget):
    _CURSOR_KEY = "encounter_rewards"

    def __init__(
        self,
        records: List[model.EncounterRewardTable],
        undo_stack: QUndoStack,
        session,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._undo_stack = undo_stack
        self._session = session
        self._current_ix: int = -1

        self._list_panel = RecordListPanel(records, _record_label, dirty_aware=True)
        self._list_panel.indexSelected.connect(self._on_selection)

        self._detail = self._build_detail_container()

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 720])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        undo_stack.indexChanged.connect(self._refresh_form)
        undo_stack.indexChanged.connect(self._list_panel.refresh_dirty_state)
        remembered = self._session.recall_selection(self._CURSOR_KEY)
        if remembered is None or not self._list_panel.select_index(int(remembered)):
            self._list_panel.select_first()

    def _build_detail_container(self) -> QWidget:
        first = self._records[0]
        self._slot_count = len(first.probabilitiesArray)

        self._title = QLabel("—")
        font = self._title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._title.setFont(font)

        group = QGroupBox("Reward Slots")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for col, text in enumerate(["#", "Probability", "Type", "Reward"]):
            hdr = QLabel(text)
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            grid.addWidget(hdr, 0, col)

        self._rows: List[_SlotRow] = []
        for ix in range(self._slot_count):
            proxy = _RewardSlotProxy(first, ix)
            row = _SlotRow(proxy, self._undo_stack)
            self._rows.append(row)
            grid.addWidget(QLabel(f"{ix + 1:02d}"), ix + 1, 0)
            grid.addWidget(row.prob_spin, ix + 1, 1)
            grid.addWidget(row.type_combo, ix + 1, 2)
            grid.addWidget(row.value_stack, ix + 1, 3)
            row.prob_spin.valueChanged.connect(lambda _v: self._refresh_prob_sum())
        grid.setColumnStretch(3, 1)

        self._prob_sum_label = QLabel("")
        self._prob_sum_label.setWordWrap(True)

        content = QWidget()
        cl = QVBoxLayout(content)

        cl.setContentsMargins(6, 6, 6, 6)

        cl.setSpacing(4)
        cl.addWidget(self._title)
        cl.addWidget(self._prob_sum_label)
        cl.addWidget(group)
        cl.addStretch(1)

        return wrap_in_scroll(content)

    def _on_selection(self, ix: int) -> None:
        if not (0 <= ix < len(self._records)):
            return
        self._current_ix = ix
        self._session.remember_selection(self._CURSOR_KEY, ix)
        target = self._records[ix]
        self._title.setText(
            f"Encounter Reward Table #{ix:03d}    (offset 0x{target.offset:08x})"
        )
        if len(target.probabilitiesArray) != self._slot_count:
            self._title.setText(self._title.text() + "  [slot count mismatch — read-only]")
            return
        for ix_slot, row in enumerate(self._rows):
            row.rebind(_RewardSlotProxy(target, ix_slot))
        self._refresh_prob_sum()

    def _refresh_form(self, _index: int) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            return
        for row in self._rows:
            row.refresh()
        self._refresh_prob_sum()

    def select_by_id(self, table_index: int) -> bool:
        """Nav hook — table index is also the slot index used by wild
        encounters' `reward_slot`. Out-of-range indices silently no-op so
        callers can pass through user-edited values without pre-validating."""
        return self._list_panel.select_index(table_index)

    def _refresh_prob_sum(self) -> None:
        if not (0 <= self._current_ix < len(self._records)):
            self._prob_sum_label.setText("")
            return
        target = self._records[self._current_ix]
        total = sum(target.probabilitiesArray)
        if total == EXPECTED_PROBABILITY_SUM:
            self._prob_sum_label.setStyleSheet("color: palette(mid);")
            self._prob_sum_label.setText(f"Probability sum: {total} / {EXPECTED_PROBABILITY_SUM} ✓")
        else:
            self._prob_sum_label.setStyleSheet("color: #d44; font-weight: bold;")
            diff = total - EXPECTED_PROBABILITY_SUM
            sign = "+" if diff > 0 else ""
            self._prob_sum_label.setText(
                f"⚠ Probability sum: {total} / {EXPECTED_PROBABILITY_SUM} "
                f"({sign}{diff}) — must equal {EXPECTED_PROBABILITY_SUM}"
            )
