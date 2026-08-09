"""Damage Calculator — a stateless utility tool (not a model editor).

Two combatant sides, each is BOTH attacker and defender. Per side: pick a species
(typable, id-prefixed), toggle Base vs Enemy data, and see its 5 moves as
clickable highlighted buttons (with element icons) showing flat + % damage vs the
other side. Click a move (either side) to select it for the detailed readout; a
compact combo per slot swaps the move.

Engine: digimon_core.damage (dump-verified port of ov2 BattleApplyMoveDamage).
Hit-rate is decompile-derived, NOT dump-verified — labelled as such.

State is stashed on the session so edits survive switching tabs away and back.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, damage, map_labels, model
from digimon_core.stat_progression import ProgressionMode, compute_expected_stats

from .form_helpers import (
    BoldGroupBox as QGroupBox,
    NoWheelComboBox,
    NoWheelSpinBox,
    make_form,
    move_choices,
    wrap_in_scroll,
)

_LEVEL_DEFAULT = 20
_STATS = [("hp", "HP"), ("atk", "Attack"), ("dfn", "Defense"), ("spi", "Spirit"),
          ("spd", "Speed"), ("eva", "Evasion")]
_SCALE_KEY = {"hp": "hp", "atk": "attack", "dfn": "defense", "spi": "spirit", "spd": "speed"}
# stat attr -> boost key (Evasion is not scaled; HP takes no boost).
_STAT_BOOST_KEY = {"atk": "atk", "dfn": "dfn", "spi": "spi", "spd": "spd", "eva": "evade"}
_STATE_KEY = "_damage_calc_state"
_NO_MOVE = 0xFFFF
_DEFAULT_LEFT = 0x9D    # Lunamon
_DEFAULT_RIGHT = 0x21E  # Kokuwamon (Thriller Ruins fixed enemy)
_MOVE_BTN_STYLE = (
    "QPushButton { text-align: left; padding: 2px 6px; }"
    "QPushButton:checked { font-weight: bold; border: 2px solid palette(highlight);"
    " background: palette(highlight); color: palette(highlighted-text); }"
)


def _typable(combo: QComboBox) -> QComboBox:
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    comp = QCompleter(combo)
    comp.setCaseSensitivity(Qt.CaseInsensitive)
    comp.setFilterMode(Qt.MatchContains)
    comp.setCompletionMode(QCompleter.PopupCompletion)
    combo.setCompleter(comp)
    return combo


def _arm(combo: QComboBox):
    # Re-point the completer at the (now-populated) model — the step BoundIdCombo
    # does at form_helpers.py:850, without which type-to-filter does nothing.
    if combo.completer() is not None:
        combo.completer().setModel(combo.model())


_NO_TRAIT = 0xFFFF
_NO_EQUIP = 0xFFFF

# Trait/equipment boosts are FLAT adds to the matching field. Trait effect_type
# -> boost key; and effect_type -> element index for the per-element resistances.
_TRAIT_STAT = {0x01: "atk", 0x02: "dfn", 0x03: "spi", 0x04: "spd",
               0x13: "hit", 0x14: "evade", 0x15: "crit", 0x17: "dmg"}
_TRAIT_RESIST = {0x05: damage.DARK, 0x06: damage.LIGHT, 0x07: damage.STEEL, 0x08: damage.FIRE,
                 0x09: damage.THUNDER, 0x0A: damage.WATER, 0x0B: damage.WIND, 0x32: damage.EARTH}
_TRAIT_ALL_RESIST = 0x0C
_EQUIP_STAT = {"atk_boost": "atk", "defense_boost": "dfn", "spirit_boost": "spi",
               "speed_boost": "spd", "critical_boost": "crit", "dmg_boost": "dmg",
               "accuracy_boost": "hit", "dodge_boost": "evade"}
_EQUIP_RESIST = {"light_res_boost": damage.LIGHT, "dark_res_boost": damage.DARK,
                 "fire_res_boost": damage.FIRE, "earth_res_boost": damage.EARTH,
                 "wind_res_boost": damage.WIND, "steel_res_boost": damage.STEEL,
                 "water_res_boost": damage.WATER, "thunder_res_boost": damage.THUNDER}

def _rows_model(none_id, rows) -> QStandardItemModel:
    m = QStandardItemModel()
    it = QStandardItem("(none)")
    it.setData(none_id, Qt.UserRole)
    m.appendRow(it)
    for rid, name in rows:
        it = QStandardItem(f"0x{rid:03x}  {name}")
        it.setData(rid, Qt.UserRole)
        m.appendRow(it)
    return m


def _move_model() -> QStandardItemModel:
    return _rows_model(_NO_MOVE, move_choices())


def _trait_model() -> QStandardItemModel:
    return _rows_model(_NO_TRAIT, list(enumerate(constants.TRAIT_ARRAY_STR)))


def _equip_model(session) -> QStandardItemModel:
    rows = [(eid, constants.ITEM_ID_TO_STR.get(eid, f"item 0x{eid:03x}"))
            for eid in sorted(session.equipment)]
    return _rows_model(_NO_EQUIP, rows)


class _Side:
    def __init__(self, title, session, move_model, trait_model, equip_model, on_change):
        self.title = title
        self.session = session
        self.on_change = on_change
        self.updating = True
        self.affinity = 0
        self.slot_elem = [0] * 5
        self.slot_hits = [1] * 5
        self._trait_model = trait_model
        self._equip_model = equip_model

        self.species = _typable(NoWheelComboBox())
        self.species.currentIndexChanged.connect(self._on_species)
        self.base_radio = QRadioButton("Base")
        self.enemy_radio = QRadioButton("Enemy")
        self.base_radio.setChecked(True)
        grp = QButtonGroup(self.base_radio)
        grp.addButton(self.base_radio)
        grp.addButton(self.enemy_radio)
        self.base_radio.toggled.connect(self._on_source)
        self.sprite_lbl = QLabel()
        self.sprite_lbl.setFixedSize(84, 84)
        self.sprite_lbl.setAlignment(Qt.AlignCenter)
        self.sprite_lbl.setStyleSheet("border: 1px solid palette(mid);")
        self.affinity_lbl = QLabel("—")
        self.affinity_lbl.setStyleSheet("color: palette(mid);")
        self.level = self._spin(1, 99, _LEVEL_DEFAULT, width=90)
        self.level.valueChanged.connect(self._on_level)
        self.stat = {a: self._spin(0, 99999) for a, _ in _STATS}
        self.stat["spd"].setToolTip("Speed drives turn order, not damage — shown for reference.")
        self.resist = [self._spin(0, 9999, damage.NEUTRAL_RESIST, width=52) for _ in range(8)]
        self.crit_mod = self._spin(0, 100)
        self.flat_bonus = self._spin(-9999, 9999)
        self.trait_def = self._spin(0, 9999)
        self.mode = QCheckBox("Wireless battle mode (ATK ×1.2)")
        self.mode.setToolTip(
            "Online/wireless battles set a battle-wide flag that multiplies the "
            "attacker's power term by 1.2 (before defense is subtracted, so net "
            "damage rises by more than 1.2×). Off for wild/single-player battles.")
        self.mode.toggled.connect(self._emit)

        # move rows: a clickable/highlighted name button (selector), a compact
        # combo to swap the move, and power/crit overrides. Element comes from the
        # move (shown as the button's icon); damage is a flat + % readout.
        self.m_btn, self.m_move, self.m_pow, self.m_crit, self.m_res = [], [], [], [], []
        for i in range(5):
            btn = QPushButton("—")
            btn.setCheckable(True)
            btn.setStyleSheet(_MOVE_BTN_STYLE)
            btn.setIconSize(QSize(16, 16))
            self.m_btn.append(btn)
            combo = _typable(NoWheelComboBox())
            combo.setModel(move_model)
            combo.setMinimumWidth(150)
            combo.setMaximumWidth(190)
            _arm(combo)
            combo.currentIndexChanged.connect(lambda _v, ix=i: self._on_move(ix))
            self.m_move.append(combo)
            self.m_pow.append(self._spin(0, 9999, width=50))
            self.m_crit.append(self._spin(0, 100, width=46))
            lbl = QLabel("—")
            lbl.setStyleSheet("font-weight: bold;")
            self.m_res.append(lbl)

        self.trait_combos = [self._picker(trait_model) for _ in range(5)]   # trait_1..4 + own support
        # support traits granted by the two adjacent party members — in-game a
        # support trait boosts its NEIGHBOURS, so these apply to THIS digimon
        # (while this digimon's own support, trait_combos[4], goes to its allies).
        self.ally_support = [self._picker(trait_model) for _ in range(2)]
        self.equip_combos = [self._picker(equip_model) for _ in range(3)]
        # inline "+N" labels shown beside each boostable stat / resistance / modifier;
        # resistances also get a "×mult" label below the input.
        self.stat_boost_lbl = {a: QLabel("") for a in _STAT_BOOST_KEY}
        self.resist_boost_lbl = [QLabel("") for _ in range(8)]
        self.resist_mult_lbl = [QLabel("") for _ in range(8)]
        self.crit_boost_lbl = QLabel("")
        self.dmg_boost_lbl = QLabel("")
        for lbl in (*self.stat_boost_lbl.values(), *self.resist_boost_lbl,
                    *self.resist_mult_lbl, self.crit_boost_lbl, self.dmg_boost_lbl):
            lbl.setStyleSheet("color: palette(mid);")

        self._populate_species()
        self.updating = False

    def _picker(self, model_):
        c = _typable(NoWheelComboBox())
        c.setModel(model_)
        c.setMinimumWidth(140)
        _arm(c)
        c.currentIndexChanged.connect(self._emit)
        return c

    # ---- factories --------------------------------------------------------

    def _spin(self, lo, hi, val=0, width=70):
        sb = NoWheelSpinBox()
        sb.setRange(lo, hi)
        sb.setValue(val)
        sb.setMaximumWidth(width)
        sb.valueChanged.connect(self._emit)
        return sb

    def _emit(self, *_):
        if not self.updating:
            self.on_change()

    def _elem_icon(self, ev):
        try:
            pm = self.session.element_icon_pixmap(ev, max_size=16)
            if pm is not None and not pm.isNull():
                return QIcon(pm)
        except Exception:
            pass
        return QIcon()

    # ---- layout -----------------------------------------------------------

    def build_panel(self) -> QGroupBox:
        box = QGroupBox(self.title)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        top = QHBoxLayout()
        idw = QWidget()
        idf = make_form(idw)
        idf.addRow("Species", self.species)
        src = QHBoxLayout()
        src.setContentsMargins(0, 0, 0, 0)
        src.addWidget(self.base_radio)
        src.addWidget(self.enemy_radio)
        src.addStretch(1)
        srcw = QWidget()
        srcw.setLayout(src)
        idf.addRow("Data", srcw)
        idf.addRow("Level", self.level)
        idf.addRow("Affinity", self.affinity_lbl)
        top.addWidget(idw, 1)
        top.addWidget(self.sprite_lbl, 0, Qt.AlignTop)
        outer.addLayout(top)

        outer.addWidget(self._stats_box())
        outer.addWidget(self._resist_box())
        outer.addWidget(self._traits_equip_box())
        outer.addWidget(self._mods_box())
        return box

    def _stats_box(self):
        box = QGroupBox("Stats (effective)")
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        for r, (attr, label) in enumerate(_STATS):
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(self.stat[attr], r, 1)
            if attr in self.stat_boost_lbl:
                grid.addWidget(self.stat_boost_lbl[attr], r, 2)
        grid.setColumnStretch(3, 1)
        return box

    def _resist_box(self):
        box = QGroupBox("Resistances (500 = neutral)")
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 4, 8, 4)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(1)
        for i, name in enumerate(damage.ELEMENT_NAMES):
            head = QLabel(name)
            head.setAlignment(Qt.AlignCenter)
            head.setStyleSheet("color: palette(mid);")
            grid.addWidget(head, 0, i)

            # input + "+N" boost to its right
            cell = QWidget()
            row = QHBoxLayout(cell)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            row.addWidget(self.resist[i])
            self.resist_boost_lbl[i].setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.resist_boost_lbl[i].setToolTip("Resistance boost from traits / equipment.")
            row.addWidget(self.resist_boost_lbl[i])
            grid.addWidget(cell, 1, i, Qt.AlignCenter)

            # multiplier below the input
            self.resist_mult_lbl[i].setAlignment(Qt.AlignCenter)
            self.resist_mult_lbl[i].setToolTip(
                "Damage multiplier vs this element (500 = ×1.00, 1000 = ×0.50).")
            grid.addWidget(self.resist_mult_lbl[i], 2, i)
        return box

    def _mods_box(self):
        box = QGroupBox("Modifiers")
        form = make_form(box)
        form.addRow("Crit mod (atk)", self._with_boost(self.crit_mod, self.crit_boost_lbl))
        form.addRow("Flat bonus (atk)", self._with_boost(self.flat_bonus, self.dmg_boost_lbl))
        form.addRow("Trait reduction (def)", self.trait_def)
        form.addRow("", self.mode)
        return box

    def _with_boost(self, widget, boost_lbl):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(widget)
        row.addWidget(boost_lbl)
        row.addStretch(1)
        cell = QWidget()
        cell.setLayout(row)
        return cell

    def _traits_equip_box(self):
        box = QGroupBox("Traits && Equipment  (boosts apply to the damage)")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        cols = QHBoxLayout()
        cols.setSpacing(10)
        tw = QWidget()
        tf = make_form(tw)
        for label, combo in zip(["Trait 1", "Trait 2", "Trait 3", "Trait 4", "Support (→ ally)"],
                                self.trait_combos):
            tf.addRow(label, combo)
        self.trait_combos[4].setToolTip(
            "This digimon's own support trait. In-game it boosts the ADJACENT party "
            "members, not itself — so it does not affect this calculation.")
        ew = QWidget()
        ef = make_form(ew)
        for i, combo in enumerate(self.equip_combos):
            ef.addRow(f"Equip {i + 1}", combo)
        cols.addWidget(tw, 1)
        cols.addWidget(ew, 1)
        outer.addLayout(cols)

        # distinct zone: support traits received FROM the two adjacent party members
        ally_box = QGroupBox("Ally support  (support traits from adjacent party members)")
        af = make_form(ally_box)
        for i, combo in enumerate(self.ally_support):
            combo.setToolTip(
                "A support trait from an adjacent party member, applied to THIS "
                "digimon. Not auto-filled — set it to match your party formation.")
            af.addRow(f"Ally support {i + 1}", combo)
        outer.addWidget(ally_box)
        return box

    def build_moves(self, sel_group: QButtonGroup, base_id: int, mirror: bool = False) -> QGroupBox:
        box = QGroupBox(f"{self.title}'s moves  (click to select)")
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        # Mirror Digimon 2 so its damage column sits on the inner (centre) edge.
        dmg_align = (Qt.AlignLeft if mirror else Qt.AlignRight) | Qt.AlignVCenter
        for i in range(5):
            self.m_res[i].setAlignment(dmg_align)
        spec = [
            ("move", "Move", self.m_btn),
            ("change", "Change", self.m_move),
            ("pwr", "Pwr", self.m_pow),
            ("crit", "Crit", self.m_crit),
            ("dmg", "Damage vs. other", self.m_res),
        ]
        if mirror:
            # only the damage column moves — to the left of the moves.
            spec = spec[-1:] + spec[:-1]
        for c, (key, h, widgets) in enumerate(spec):
            lab = QLabel(h)
            lab.setStyleSheet("color: palette(mid);")
            if key == "dmg":
                lab.setAlignment(dmg_align)
            grid.addWidget(lab, 0, c)
            if key in ("move", "dmg"):
                grid.setColumnStretch(c, 1)
            for i in range(5):
                if key == "move":
                    sel_group.addButton(self.m_btn[i], base_id + i)
                grid.addWidget(widgets[i], i + 1, c)
        return box

    # ---- population -------------------------------------------------------

    def _populate_species(self):
        block = self.species.blockSignals(True)
        for did in sorted(self.session.base_digimon):
            self.species.addItem(self._species_label(did), did)
        self.species.blockSignals(block)
        _arm(self.species)

    def _species_label(self, did):
        name = self.session.digimon_display_name(did)
        base = f"0x{did:03x}  {name}"
        if not self.enemy_radio.isChecked():
            return base
        if self.session.wild_areas_by_digimon().get(did):
            return f"{base}  [Wild]"
        locs = self.session.battle_locations_by_digimon().get(did)
        if locs:
            loc = locs[0]
            where = map_labels.area_name(loc.map_id) if loc.map_id is not None else f"entry {loc.entry_ix}"
            return f"{base}  [Event: {where}]"
        return base

    def _refill_species_labels(self):
        cur = self.species.currentData()
        block = self.species.blockSignals(True)
        for ix in range(self.species.count()):
            self.species.setItemText(ix, self._species_label(self.species.itemData(ix)))
        j = self.species.findData(cur)
        if j >= 0:
            self.species.setCurrentIndex(j)
        self.species.blockSignals(block)

    # ---- data source ------------------------------------------------------

    def _record(self):
        did = self.species.currentData()
        if self.enemy_radio.isChecked():
            return self.session.enemy_digimon.get(did)
        return self.session.base_digimon.get(did)

    def _on_source(self, *_):
        if self.updating:
            return
        self._refill_species_labels()
        self.autofill()

    def _on_species(self, *_):
        if not self.updating:
            self.autofill()

    def _on_level(self, *_):
        if self.updating:
            return
        if self.enemy_radio.isChecked():
            self._emit()
        else:
            self.autofill()

    def _on_move(self, i):
        if self.updating:
            return
        self.updating = True
        self._apply_slot(i, self.m_move[i].currentData())
        self.updating = False
        self.on_change()

    # ---- autofill ---------------------------------------------------------

    def autofill(self):
        did = self.species.currentData()
        base = self.session.base_digimon.get(did)
        rec = self._record()
        if rec is None or base is None:
            return
        # STAB reads base[battler+0x30], and battler+0x30 is the record id (dump-
        # verified: scripted Kokuwamon 0x21e -> battler+0x30 = 0x21e -> base[0x21e]).
        # For scripted enemies that base record is a 9999/999 dummy whose affinity is
        # Light, so the game genuinely gives them Light STAB — not the enemy +0x24.
        self.affinity = base.element_affinity
        els = [n for i, n in enumerate(damage.ELEMENT_NAMES) if (self.affinity >> i) & 1]
        self.affinity_lbl.setText(" + ".join(els) if els else "(none)")
        self._set_sprite(did)

        self.updating = True
        if self.enemy_radio.isChecked():
            self.level.setValue(max(1, min(99, rec.level)))
            vals = {"hp": rec.hp, "atk": rec.attack, "dfn": rec.defense, "spi": rec.spirit,
                    "spd": rec.speed, "eva": rec.evasion}
            resist = [rec.light_res, rec.dark_res, rec.fire_res, rec.earth_res,
                      rec.wind_res, rec.steel_res, rec.water_res, rec.thunder_res]
        else:
            stats = compute_expected_stats(base, self.level.value(), mode=ProgressionMode.FIXED_AVG).stats
            vals = {a: stats.get(k, 0) for a, k in _SCALE_KEY.items()}
            vals["eva"] = base.evasion   # evasion doesn't scale — read it straight off the record
            resist = base.getResistanceValues()
        for a, _ in _STATS:
            self.stat[a].setValue(max(0, vals[a]))
        for i, r in enumerate(resist):
            self.resist[i].setValue(r)
        for i, mid in enumerate([rec.move_signature, rec.move_1, rec.move_2, rec.move_3, rec.move_4]):
            self._apply_slot(i, mid)
        traits = ([rec.trait_1, rec.trait_2, rec.trait_3, rec.trait_4, rec.support_trait]
                  if not self.enemy_radio.isChecked()
                  else [rec.trait_1, rec.trait_2, rec.trait_3, rec.trait_4, _NO_TRAIT])
        for combo, tid in zip(self.trait_combos, traits):
            valid = isinstance(tid, int) and 0 <= tid < len(self.session.traits)
            j = combo.findData(tid if valid else _NO_TRAIT)
            combo.setCurrentIndex(j if j >= 0 else 0)
        self.updating = False
        self.on_change()

    def _apply_slot(self, i, mid):
        valid = isinstance(mid, int) and 0 <= mid < len(self.session.moves)
        j = self.m_move[i].findData(mid if valid else _NO_MOVE)
        self.m_move[i].setCurrentIndex(j if j >= 0 else 0)
        if valid:
            mv = self.session.moves[mid]
            ev = int(mv.element.value)
            self.slot_elem[i] = ev
            self.slot_hits[i] = max(1, mv.num_hits)
            nm = constants.MOVE_ARRAY_STR[mid] if mid < len(constants.MOVE_ARRAY_STR) else f"Move {mid}"
            self.m_btn[i].setText(nm)
            self.m_btn[i].setIcon(self._elem_icon(ev))
            self.m_btn[i].setEnabled(True)
            self.m_pow[i].setValue(mv.primary_value)
            self.m_crit[i].setValue(mv.crit_rate)
        else:
            self.slot_elem[i] = 0
            self.slot_hits[i] = 1
            self.m_btn[i].setText("—")
            self.m_btn[i].setIcon(QIcon())
            self.m_btn[i].setEnabled(False)
            self.m_pow[i].setValue(0)
            self.m_crit[i].setValue(0)
            self.m_res[i].setText("—")

    def _set_sprite(self, did):
        pm = None
        try:
            sm = self.session.sprite_map[did] if did < len(self.session.sprite_map) else None
            if sm is not None:
                pm = self.session.battle_sprite_pixmap(sm.main_sprite, max_size=80)
        except Exception:
            pm = None
        if pm is None:
            try:
                icon = self.session.digimon_portrait_icon(did)
                pm = icon.pixmap(QSize(64, 64)) if icon else None
            except Exception:
                pm = None
        if pm is not None and not pm.isNull():
            self.sprite_lbl.setPixmap(pm)
        else:
            self.sprite_lbl.clear()
            self.sprite_lbl.setText("(no sprite)")

    # ---- engine inputs ----------------------------------------------------

    def _boosts(self):
        """Aggregate flat boosts from this side's traits + equipment.

        Applied to THIS digimon: its 4 regular traits + the support traits granted
        by its two neighbours. Its own support trait (``trait_combos[4]``) is NOT
        included — in-game that boosts the adjacent party members, not itself.
        """
        b = {"atk": 0, "dfn": 0, "spi": 0, "spd": 0, "crit": 0, "dmg": 0, "hit": 0, "evade": 0,
             "resist": [0] * 8}

        def add_trait(tid):
            if isinstance(tid, int) and 0 <= tid < len(self.session.traits):
                t = self.session.traits[tid]
                if t.effect_type in _TRAIT_STAT:
                    b[_TRAIT_STAT[t.effect_type]] += t.magnitude
                elif t.effect_type in _TRAIT_RESIST:
                    b["resist"][_TRAIT_RESIST[t.effect_type]] += t.magnitude
                elif t.effect_type == _TRAIT_ALL_RESIST:
                    for i in range(8):
                        b["resist"][i] += t.magnitude

        for combo in (*self.trait_combos[:4], *self.ally_support):
            add_trait(combo.currentData())
        for combo in self.equip_combos:
            eid = combo.currentData()
            eq = self.session.equipment.get(eid) if isinstance(eid, int) else None
            if eq is not None:
                for attr, key in _EQUIP_STAT.items():
                    b[key] += getattr(eq, attr)
                for attr, el in _EQUIP_RESIST.items():
                    b["resist"][el] += getattr(eq, attr)
        return b

    def attacker(self):
        b = self._boosts()
        return damage.Attacker(
            level=self.level.value(), atk=self.stat["atk"].value() + b["atk"],
            spi=self.stat["spi"].value() + b["spi"], affinity=self.affinity,
            crit_mod=self.crit_mod.value() + b["crit"],
            flat_bonus=self.flat_bonus.value() + b["dmg"], mode_bonus=self.mode.isChecked(),
            hit_stat=b["hit"])

    def defender(self):
        b = self._boosts()
        return damage.Defender(
            level=self.level.value(), dfn=self.stat["dfn"].value() + b["dfn"],
            spi=self.stat["spi"].value() + b["spi"],
            resist=[self.resist[i].value() + b["resist"][i] for i in range(8)],
            max_hp=max(1, self.stat["hp"].value()), evade_stat=self.stat["eva"].value() + b["evade"])

    def update_boosts(self):
        b = self._boosts()
        for a, lbl in self.stat_boost_lbl.items():
            v = b[_STAT_BOOST_KEY[a]]
            lbl.setText(f"+{v}" if v else "")
        for i in range(8):
            v = b["resist"][i]
            mult = damage.resist_multiplier(self.resist[i].value() + v)
            self.resist_boost_lbl[i].setText(f"+{v}" if v else "")
            self.resist_mult_lbl[i].setText(f"×{mult:.2f}")
        self.crit_boost_lbl.setText(f"+{b['crit']}" if b["crit"] else "")
        self.dmg_boost_lbl.setText(f"+{b['dmg']}" if b["dmg"] else "")

    def move_input(self, i):
        return damage.Move(power=self.m_pow[i].value(), element=self.slot_elem[i],
                           crit_rate=self.m_crit[i].value(), hits=self.slot_hits[i])

    def valid_move(self, i):
        return self.m_move[i].currentData() not in (None, _NO_MOVE)

    def name(self):
        return self.session.digimon_display_name(self.species.currentData())

    def move_name(self, i):
        return self.m_btn[i].text()

    # ---- persistence ------------------------------------------------------

    def snapshot(self):
        return dict(
            species=self.species.currentData(), enemy=self.enemy_radio.isChecked(),
            level=self.level.value(),
            stats={a: self.stat[a].value() for a, _ in _STATS},
            resist=[sb.value() for sb in self.resist],
            moves=[dict(m=self.m_move[i].currentData(), p=self.m_pow[i].value(),
                        c=self.m_crit[i].value()) for i in range(5)],
            traits=[c.currentData() for c in self.trait_combos],
            ally_support=[c.currentData() for c in self.ally_support],
            equip=[c.currentData() for c in self.equip_combos],
            crit_mod=self.crit_mod.value(), flat=self.flat_bonus.value(),
            tdef=self.trait_def.value(), mode=self.mode.isChecked())

    def restore(self, st):
        self.updating = True
        (self.enemy_radio if st["enemy"] else self.base_radio).setChecked(True)
        self._refill_species_labels()
        j = self.species.findData(st["species"])
        if j >= 0:
            self.species.setCurrentIndex(j)
        self.updating = False
        self.autofill()
        self.updating = True
        self.level.setValue(st["level"])
        for a, v in st["stats"].items():
            self.stat[a].setValue(v)
        for i, v in enumerate(st["resist"]):
            self.resist[i].setValue(v)
        for i, m in enumerate(st["moves"]):
            self._apply_slot(i, m["m"])
            self.m_pow[i].setValue(m["p"])
            self.m_crit[i].setValue(m["c"])
        for i, tid in enumerate(st.get("traits", [])):
            j = self.trait_combos[i].findData(tid)
            if j >= 0:
                self.trait_combos[i].setCurrentIndex(j)
        for i, tid in enumerate(st.get("ally_support", [])):
            if i < len(self.ally_support):
                j = self.ally_support[i].findData(tid)
                if j >= 0:
                    self.ally_support[i].setCurrentIndex(j)
        for i, eid in enumerate(st.get("equip", [])):
            j = self.equip_combos[i].findData(eid)
            if j >= 0:
                self.equip_combos[i].setCurrentIndex(j)
        self.crit_mod.setValue(st["crit_mod"])
        self.flat_bonus.setValue(st["flat"])
        self.trait_def.setValue(st["tdef"])
        self.mode.setChecked(st["mode"])
        self.updating = False

    def set_default(self, species_id, enemy):
        self.updating = True
        (self.enemy_radio if enemy else self.base_radio).setChecked(True)
        self._refill_species_labels()
        j = self.species.findData(species_id)
        if j >= 0:
            self.species.setCurrentIndex(j)
        self.updating = False
        self.autofill()


class DamageCalculatorWidget(QWidget):
    _CURSOR_KEY = "damage_calculator"

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self._session = session
        move_model = _move_model()
        trait_model = _trait_model()
        equip_model = _equip_model(session)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.a = _Side("Digimon 1", session, move_model, trait_model, equip_model, self._recompute)
        self.b = _Side("Digimon 2", session, move_model, trait_model, equip_model, self._recompute)

        self._sel = QButtonGroup(self)
        self._sel.setExclusive(True)
        moves = QHBoxLayout()
        moves.setSpacing(32)
        moves.addWidget(self.a.build_moves(self._sel, 0), 1)
        moves.addWidget(self.b.build_moves(self._sel, 10, mirror=True), 1)
        root.addLayout(moves)
        self._sel.idToggled.connect(lambda _i, on: on and self._recompute())

        self._out = self._result_block()
        root.addWidget(self._out["box"])

        panels = QHBoxLayout()
        panels.setSpacing(6)
        panels.addWidget(self.a.build_panel(), 1)
        panels.addWidget(self.b.build_panel(), 1)
        root.addLayout(panels)
        root.addStretch(1)

        saved = getattr(session, _STATE_KEY, None)
        if saved:
            self.a.restore(saved["a"])
            self.b.restore(saved["b"])
            btn = self._sel.button(saved.get("sel", 0))
            (btn or self._sel.button(0)).setChecked(True)
        else:
            self.a.set_default(_DEFAULT_LEFT, enemy=False)
            self.b.set_default(_DEFAULT_RIGHT, enemy=True)
            self._sel.button(0).setChecked(True)
        self._recompute()

    def _result_block(self):
        box = QGroupBox("—")
        grid = QGridLayout(box)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(3)
        labels = {"box": box}
        dmg = QLabel("—")
        f = dmg.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        dmg.setFont(f)
        labels["dmg"] = dmg
        labels["crit"] = QLabel("—")
        labels["meta"] = QLabel("—")
        hit = QLabel("—")
        hit.setStyleSheet("color: palette(mid); font-style: italic;")
        labels["hit"] = hit
        for r, k in enumerate(["dmg", "crit", "meta", "hit"]):
            grid.addWidget(labels[k], r, 0)
        return labels

    def _recompute(self, *_):
        if not self._session.moves:
            return
        self.a.update_boosts()
        self.b.update_boosts()
        self._fill_rows(self.a, self.b)
        self._fill_rows(self.b, self.a)
        self._fill_detail()
        setattr(self._session, _STATE_KEY,
                {"a": self.a.snapshot(), "b": self.b.snapshot(), "sel": self._sel.checkedId()})

    def _fill_rows(self, atk, dfn):
        for i in range(5):
            if not atk.valid_move(i):
                atk.m_res[i].setText("—")
                continue
            s = damage.damage_spread(atk.move_input(i), atk.attacker(), dfn.defender(),
                                     trait_def=dfn.trait_def.value())
            hits = f"  ({s.hits} hits total)" if s.hits > 1 else ""
            atk.m_res[i].setText(
                f"{s.nocrit_min}–{s.nocrit_max}{hits}   "
                f"({s.pct(s.nocrit_min):.1f}–{s.pct(s.nocrit_max):.1f}%)")

    def _fill_detail(self):
        cid = self._sel.checkedId()
        if cid < 0:
            return
        atk, dfn = (self.a, self.b) if cid < 10 else (self.b, self.a)
        i = cid % 10
        out = self._out
        out["box"].setTitle(f"{atk.name()}   {atk.move_name(i)}   →   {dfn.name()}")
        if not atk.valid_move(i):
            for k in ("dmg", "crit", "meta", "hit"):
                out[k].setText("—")
            return
        mv = atk.move_input(i)
        s = damage.damage_spread(mv, atk.attacker(), dfn.defender(), trait_def=dfn.trait_def.value())
        hr = damage.hit_rate(mv, atk.attacker(), dfn.defender())
        best = math.ceil(s.max_hp / s.nocrit_max) if s.nocrit_max else 0
        worst = math.ceil(s.max_hp / s.nocrit_min) if s.nocrit_min else 0
        out["dmg"].setText(
            f"{s.nocrit_min}–{s.nocrit_max}   ({s.pct(s.nocrit_min):.1f}%–{s.pct(s.nocrit_max):.1f}% HP)"
            f"   avg {s.nocrit_avg:.0f}   ·   {best}–{worst} hits to KO")
        out["crit"].setText(
            f"crit {s.crit_min}–{s.crit_max}   ({s.pct(s.crit_min):.1f}%–{s.pct(s.crit_max):.1f}% HP)")
        hits_meta = f"    ·    {s.hits} hits (total shown)" if s.hits > 1 else ""
        out["meta"].setText(
            f"STAB {'✓' if s.stab else '✗'}    ·    element ×{s.resist_mult:.2f}    ·    "
            f"crit chance {s.crit_rate}%{hits_meta}")
        out["hit"].setText(f"Hit rate ≈ {hr}%  (estimate — decompile-derived, not verified)")


def build_damage_calculator(session) -> QWidget:
    return wrap_in_scroll(DamageCalculatorWidget(session))
