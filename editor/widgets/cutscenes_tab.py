"""Cutscenes tab — per-map scene browser over the overlay5 chain index.

Sits next to the Events tab inside :class:`MapBrowser`. The Events tab is
for direct placement editing (drag a sprite, move an exit zone); this tab
is for browsing the scripted *scenes* attached to a map — the same OWS /
exit / hitbox / handler triggers, but indexed as chains of regions that
fire when the player interacts.

Layout:

* A wrapping chip row at the top — one chip per chain, plus a "Base"
  chip for the no-selection state. Chips are color-dotted by trigger
  kind (NPC dialog / exit / hitbox / handler / cross-script) so the
  user can scan the row by type.
* A map preview below the chip row (just the composite render for now;
  a later slice will paint selected-chain elements on top).
* A read-only detail panel on the right showing the selected chain's
  trigger label, region path, and any decoded dialog blocks inside its
  regions.

Data source: :meth:`RomSession.cutscene_index`. We never decode the
chain graph here — the session-level :class:`CutsceneIndex` is the
single source of truth and ``chains_for_map`` is O(1).

This tab is read-only for now; edit affordances (drag sprites within a
scene, change dialog text, customize battle params) layer on once the
browse experience is validated.
"""
from __future__ import annotations

import html
import struct
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Dict, List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from digimon_core import overlay5 as overlay5_mod
from digimon_core.constants import ITEM_ID_TO_STR
from digimon_core.map_labels import area_name
from digimon_core.overlay5_cutscenes import (
    CutsceneChain,
    TriggerKind,
)

from ..commands import (
    EditBattleBgCommand,
    EditBattleEnemyCommand,
    EditBattleMusicCommand,
    EditChestItemCommand,
    EditDialogFieldCommand,
    EditExitDestinationCommand,
    EditOverworldSpriteIdCommand,
    EditReactionFieldCommand,
    EditScriptFieldCommand,
    MoveOverworldSpriteCommand,
    SetAttrCommand,
    SetMusicIdCommand,
)
from ..session import BATTLE_SPRITE_TILE_BUDGET
from .events_canvas import EventMarkerSpec, EventsCanvas, ExitZoneSpec
from .validation import ValidationIssue


# Color dot per trigger kind. Matches the legend in the mock at
# ``research_docs/claude_notes/_ui_mocks/cutscenes_chip_row.html`` so the
# UX continues to read the same as what we previewed.
_KIND_COLORS = {
    "base":    QColor(0x88, 0x88, 0x88),
    TriggerKind.OWS:     QColor(0x4a, 0x8d, 0xb8),
    TriggerKind.EXIT:    QColor(0x4a, 0x9b, 0x5e),
    TriggerKind.HITBOX:  QColor(0xc0, 0x84, 0x42),
    TriggerKind.HANDLER: QColor(0xa0, 0x62, 0xb8),
    TriggerKind.EXT:     QColor(0x6a, 0x6a, 0x6a),
    TriggerKind.OTHER:   QColor(0x6a, 0x6a, 0x6a),
    TriggerKind.HEADER:  QColor(0x6a, 0x6a, 0x6a),
}

# Kind ordering for chip layout — player-visible triggers first
# (NPC dialogs > exits > hitboxes > scripted handlers), cross-script
# (ext) chains last and dimmed.
_KIND_ORDER = [
    TriggerKind.OWS,
    TriggerKind.EXIT,
    TriggerKind.HITBOX,
    TriggerKind.HANDLER,
    TriggerKind.OTHER,
    TriggerKind.EXT,
    TriggerKind.HEADER,
]


# Chains whose triggers correspond to a visible map object — these now
# live in the "Objects on Map" list on the right instead of as chips.
# Per the user's reframing: NPCs, exits, and hitboxes are part of the
# *base scene* (the map as the player walks in); the chip row is reserved
# for scripted scenes that change the scene composition (handlers,
# cross-script callers). The same OWS/EXIT/HITBOX chains still drive
# the dialog/destination content — they're just surfaced via marker
# selection now.
_OBJECTS_LIST_KINDS = {
    TriggerKind.OWS,
    TriggerKind.EXIT,
    TriggerKind.HITBOX,
}


# Every music-id → friendly-name lookup in this module now goes through
# ``session.bgm_label(music_id)`` so user-edited BGM labels (Sound editor
# Label field) show up in cutscene cards, row previews, and undo
# descriptions. The pinned research-doc names live in
# ``digimon_core.sound.music_names.MUSIC_ID_NAMES`` and act as the
# fallback inside ``session.bgm_label``.


def _bgm_music_choices(session) -> List[Tuple[int, str]]:
    """Return ``[(music_id, "0xNNNN — Label"), ...]`` for every BGM slot
    the ROM's SDAT currently exposes — vanilla + user-staged additions.

    ``music_id`` is the sequential array position (which is what the
    overlay5 ``SET_MUSIC`` opcode addresses). The display label pulls
    from :meth:`session.bgm_label` so:

    * pinned research-doc names show up by default,
    * user-edited labels (Sound editor's Label field) take precedence,
    * "Add As New Entry" additions appear in the dropdown as soon as
      they're staged — no hardcoded id cap.

    Falls back to an empty list when the session hasn't loaded the SDAT
    (test / headless code paths); the sticky-fallback logic in the
    cards' ``_refresh_from_block`` keeps the current selection visible
    even when the enumeration is empty.
    """
    try:
        vanilla = session.vanilla_bgm_summary()
    except Exception:  # noqa: BLE001 — session may be a stub in tests
        vanilla = []
    try:
        additions = session.staged_bgm_additions()
    except Exception:  # noqa: BLE001
        additions = []
    out: List[Tuple[int, str]] = []
    total = len(vanilla) + len(additions)
    for music_id in range(total):
        label = session.bgm_label(music_id)
        out.append((music_id, f"0x{music_id:04x} — {label}"))
    return out


def _populate_music_combo(combo, session) -> None:
    """(Re)populate a music combo from the session's BGM list.

    Preserves the current selection when possible so a re-populate
    triggered by ``refresh()`` doesn't yank the visible value out from
    under the user. Clears + refills the model — cheap even for a few
    dozen entries since combos don't own an editable model like the
    sprite pickers do.
    """
    prev_data = combo.currentData()
    combo.clear()
    for music_id, display in _bgm_music_choices(session):
        combo.addItem(display, music_id)
    if prev_data is not None:
        ix = combo.findData(prev_data)
        if ix >= 0:
            combo.setCurrentIndex(ix)


# Kind → header-fragment table. Order matches display priority so the
# section header reads "3 dialogs · 1 battle · 1 music".
_EVENT_KIND_HEADER_LABEL = {
    "dialog":      ("dialog", "dialogs"),
    "battle":      ("battle", "battles"),
    "reaction":    ("reaction", "reactions"),
    "set_music":   ("music cue", "music cues"),
    "wait":        ("wait", "waits"),
    "sprite_anim": ("anim", "anims"),
    "camera":      ("camera", "cameras"),
    "item":        ("item", "items"),
    "move":        ("move", "moves"),
    "control":     ("flag/branch", "flags/branches"),
}


# Row-preview text for the events browser list — matches the icons
# each card class uses in its own header so the list and the active
# card read as the same thing at different zoom levels.
def _event_row_preview(session, event, slot_labels=None) -> Tuple[str, str]:
    """Return ``(icon_char, one_line_preview)`` for the events browser.

    Kept independent of the card widgets so the list can render even
    before the card is built (or when the underlying widget model isn't
    attached yet). Truncates long dialog bodies to ~50 chars so the row
    stays scannable at typical list widths. ``slot_labels`` (slot → name)
    lets slot-addressed events (anim / camera) name their actor.
    """
    payload = event.payload

    def _who(slot):
        name = (slot_labels or {}).get(int(slot) & 0xFFFF)
        return name if name else f"slot {int(slot) & 0xFFFF}"
    if event.kind == overlay5_mod.EVENT_KIND_DIALOG:
        portrait = int(payload.portrait) & 0xFFFF
        msg_id = int(payload.msg_id) & 0xFFFF
        try:
            name = _safe_display_name(session, portrait)
        except Exception:
            name = f"0x{portrait:04x}"
        text = ""
        try:
            gs = session.dialog_msg_text(msg_id)
            if gs is not None:
                text = (getattr(gs, "text", "") or "").replace("[BR]", " ")
        except Exception:
            text = ""
        if not text:
            text = f"(msg 0x{msg_id:04x})"
        # Trim to a scannable one-liner. Elision keeps the list rows
        # from wrapping — QListWidget uses ElideRight when the row
        # overflows anyway, but capping here gives a nicer trim point
        # at a word boundary.
        if len(text) > 50:
            text = text[:47].rstrip() + "…"
        return ("💬", f"DIALOGUE - {name}: “{text}”")
    if event.kind == overlay5_mod.EVENT_KIND_SET_MUSIC:
        mid = int(payload.music_id) & 0xFFFF
        return ("♪", f"MUSIC - {session.bgm_label(mid)} (0x{mid:04x})")
    if event.kind == overlay5_mod.EVENT_KIND_REACTION:
        rid = int(payload.reaction) & 0xFFFF
        rname = overlay5_mod.REACTION_NAMES.get(rid, f"0x{rid:04x}")
        tgt = int(payload.target) & 0xFFFF
        return ("💭", f"REACTION - {rname} over {_who(tgt)}")
    if event.kind == overlay5_mod.EVENT_KIND_BATTLE:
        enemies = [
            e for e in payload.enemies
            if int(e) & 0xFFFF != overlay5_mod.BATTLE_ENEMY_EMPTY
        ]
        n = len(enemies)
        head = ""
        if n and enemies[0] != overlay5_mod.BATTLE_ENEMY_EMPTY:
            try:
                head = _safe_display_name(session, int(enemies[0]) & 0xFFFF)
            except Exception:
                head = f"0x{int(enemies[0]) & 0xFFFF:04x}"
        pieces: List[str] = [f"{n} enem{'ies' if n != 1 else 'y'}"]
        if head:
            pieces.append(head)
        return ("⚔", "BATTLE - " + ", ".join(pieces))
    if event.kind == overlay5_mod.EVENT_KIND_WAIT:
        f = int(payload.frames) & 0xFFFF
        return ("⏱", f"WAIT - {f} frames (~{f / 60.0:.1f}s)")
    if event.kind == overlay5_mod.EVENT_KIND_SPRITE_ANIM:
        anim = int(payload.anim) & 0xFFFF
        kind = "face" if anim < 8 else "pose"
        return ("🎬", f"ANIM - {_who(payload.sprite)} {kind} 0x{anim:02x}")
    if event.kind == overlay5_mod.EVENT_KIND_CAMERA:
        if payload.is_xy:
            return ("🎥", f"CAMERA - → ({payload.a}, {payload.b}) speed {payload.speed}")
        return ("🎥", f"CAMERA - → {_who(payload.a)} speed {payload.speed}")
    if event.kind == overlay5_mod.EVENT_KIND_ITEM:
        item = int(payload.item) & 0xFFFF
        try:
            name = ITEM_ID_TO_STR.get(item, f"0x{item:02x}")
        except Exception:
            name = f"0x{item:02x}"
        if payload.is_remove:
            return ("\U0001f381", f"ITEM - Remove {name} from inventory")  # 🎁
        return ("\U0001f381", f"ITEM - Add {name} to inventory")  # 🎁
    if event.kind == overlay5_mod.EVENT_KIND_MOVE:
        if payload.x == 0xFFFF and payload.y == 0xFFFF:
            dest = "in place"
        else:
            dest = f"→ ({payload.x}, {payload.y})"
        return ("➡️", f"MOVE - {_who(payload.tgt)} {dest}")  # ➡️
    if event.kind == overlay5_mod.EVENT_KIND_CONTROL:
        label, detail = payload.row_summary()
        return (_CONTROL_ICONS.get(payload.category, "❓"), f"{label} - {detail}")
    return ("?", f"{event.kind} @ 0x{event.rel:04x}")


def _compute_event_indents(events) -> List[int]:
    """Indent level per event so a CMP_BRANCH's gated block reads like code.

    A branch is a decision point at the base level; the events it gates —
    those inside its relative skip extent (:attr:`ControlBlock.gate_len`) —
    indent one step. Consecutive branches are siblings (each back at base),
    matching the NPC dialogue-tree pattern (``if var == X: <line>`` repeated).
    Deliberately flat (one level): branch skip-extents overlap slightly in the
    data, so a stack-nested staircase would misread — one level conveys the
    gating clearly without over-claiming structure."""
    indents: List[int] = []
    open_end: Optional[Tuple[int, int]] = None  # (entry_ix, end_rel)
    for entry_ix, ev in events:
        if open_end is not None and (
            open_end[0] != entry_ix or ev.rel >= open_end[1]
        ):
            open_end = None
        is_branch = (
            ev.kind == overlay5_mod.EVENT_KIND_CONTROL
            and getattr(ev.payload, "category", None) == "branch"
        )
        if is_branch:
            indents.append(0)
            glen = int(getattr(ev.payload, "gate_len", 0))
            if 0 < glen < 0x400:
                open_end = (entry_ix, ev.rel + glen)
            else:
                # A branch with no decoded skip extent (e.g. 0x06): gate the
                # events up to the NEXT branch (which resets this) or the end.
                open_end = (entry_ix, 1 << 30)
        else:
            indents.append(1 if open_end is not None else 0)
    return indents


# Roles for QListWidgetItem user-data on the events browser list.
_EVENTS_ROW_ENTRY_IX_ROLE = Qt.UserRole + 3
_EVENTS_ROW_EVENT_REL_ROLE = Qt.UserRole + 4
_EVENTS_ROW_EVENT_KIND_ROLE = Qt.UserRole + 5


def _events_section_header(
    events: List[Tuple[int, "overlay5_mod.RegionEvent"]],
) -> str:
    """Rich-text header summarising the mixed-event list for a chain.

    Renders as either ``Dialogs (N)`` (dialog-only case, keeps the old
    one-word section title) or ``Events (a dialogs · b battles ·
    c music cues)`` when other kinds are present.
    """
    counts: Dict[str, int] = {}
    for _entry_ix, ev in events:
        counts[ev.kind] = counts.get(ev.kind, 0) + 1
    if list(counts) == ["dialog"]:
        n = counts["dialog"]
        return (
            f"<div class='sec-hdr'>Dialogs "
            f"<span class='muted'>({n})</span></div>"
        )
    parts: List[str] = []
    for kind in ("dialog", "battle", "reaction", "set_music",
                 "wait", "sprite_anim", "camera", "item", "move", "control"):
        c = counts.get(kind, 0)
        if not c:
            continue
        singular, plural = _EVENT_KIND_HEADER_LABEL[kind]
        parts.append(f"{c} {singular if c == 1 else plural}")
    joined = " &middot; ".join(parts) if parts else "0"
    return (
        f"<div class='sec-hdr'>Events "
        f"<span class='muted'>({joined})</span></div>"
    )


# Object-list row classification — stored in each ``QListWidgetItem``'s
# user data so the selection handler can dispatch to the right
# offset → chain_ix map without re-parsing the row text.
_LIST_ROW_TYPE_ROLE = Qt.UserRole + 1
_LIST_ROW_SPRITE = "sprite"
_LIST_ROW_EXIT = "exit"
_LIST_ROW_HITBOX = "hitbox"
_LIST_ROW_SPAWN = "spawn"
_LIST_ROW_GROUP = "group"
# Block offsets a group header's checkbox governs (canvas visibility toggle).
_LIST_ROW_GROUP_OFFSETS = Qt.UserRole + 6
# Stable key (flag int, or "always") identifying a group for collapse state.
_LIST_ROW_GROUP_KEY = Qt.UserRole + 7


class _FlowLayout(QLayout):
    """Minimal wrapping horizontal layout (Qt has no built-in).

    Lays out children left-to-right, wrapping to the next row when the
    available width runs out. Used for the chip row so a low-chain map
    occupies one line and a 21-chip Dark Gate wraps to two.
    """

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: List = []
        self._spacing = spacing
        self.setContentsMargins(8, 8, 8, 8)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, ix: int):
        return self._items[ix] if 0 <= ix < len(self._items) else None

    def takeAt(self, ix: int):
        return self._items.pop(ix) if 0 <= ix < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right_edge = rect.right() - m.right()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width()
            if next_x > right_edge and line_height > 0:
                x = rect.x() + m.left()
                y = y + line_height + self._spacing
                next_x = x + hint.width()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + self._spacing
            line_height = max(line_height, hint.height())
        return (y + line_height + m.bottom()) - rect.y()


class _ChainChip(QPushButton):
    """One chip in the chip row.

    Displays a 7px color dot (kind color) + label text. Checkable so
    selection state survives across siblings via a manual exclusive
    group inside :class:`CutscenesTab`. We don't use ``QButtonGroup``
    because we want clicking the selected chip to be a no-op rather
    than toggle it off.
    """

    def __init__(self, kind: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setText(label)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        # Compact pill — wraps neatly in the flow layout. Padding tuned
        # to match the mock's 26 px chip height.
        color = _KIND_COLORS.get(kind, _KIND_COLORS["base"])
        dot_color = color.name()
        self.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid #3c3c3c;
                border-radius: 13px;
                padding: 3px 10px 3px 22px;
                color: #cccccc;
                text-align: left;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.08);
            }}
            QPushButton:checked {{
                background: #007acc;
                border-color: #007acc;
                color: white;
            }}
        """)
        # Color dot — drawn as a small fixed-size widget overlay.
        self._dot = QLabel(self)
        self._dot.setFixedSize(7, 7)
        self._dot.setStyleSheet(
            f"background: {dot_color}; border-radius: 3px;"
        )
        # Dim cross-script chips per the mock.
        if kind == TriggerKind.EXT:
            self.setStyleSheet(self.styleSheet() + """
                QPushButton:!checked { color: #888; }
            """)

    def resizeEvent(self, event) -> None:  # noqa: D401
        super().resizeEvent(event)
        # Re-center dot vertically inside the chip; 8 px from left edge.
        cy = (self.height() - self._dot.height()) // 2
        self._dot.move(8, cy)


@dataclass
class _MapState:
    """What we know about the currently displayed map.

    ``handler_summaries`` is keyed by chain index (position in
    :attr:`chains`) and only contains entries for ``TriggerKind.HANDLER``
    chains. Chip labels and the handler detail panel both read from
    this dict so the body scan runs exactly once per map.

    ``chain_extras`` carries OVERWORLD_SPRITE placements *outside* the
    base map entry that the chain's regions inject when it runs —
    typically NPCs spawned by handler entries (e.g. 0499) for a
    cutscene. Keyed by chain index, parallel to ``handler_summaries``
    but populated for every chain whose body contains a 0x0150 block.

    ``composite_pixmap``, ``sprite_specs``, ``exit_specs`` are retained
    so :meth:`_select_chip` can rebuild the read-only canvas with
    ``base + chain_extras`` on every chip flip. The map browser only
    hands them over once per map-load, so the tab caches them locally.
    """
    map_id: int
    entry_ix: int
    chains: List[CutsceneChain]
    selected_chip_ix: int  # -1 = Base, otherwise index into chains
    entry_bytes: bytes
    handler_summaries: Dict[int, _HandlerSummary] = field(default_factory=dict)
    composite_pixmap: Optional[QPixmap] = None
    sprite_specs: List[EventMarkerSpec] = field(default_factory=list)
    exit_specs: List[ExitZoneSpec] = field(default_factory=list)
    chain_extras: Dict[int, List[EventMarkerSpec]] = field(default_factory=dict)
    # Raw OVERWORLD_SPRITE placements injected by each chain, as
    # ``(entry_ix, placement)`` tuples. Parallel to ``chain_extras``
    # but exposes the placement object so the detail panel can follow
    # each spawned NPC's ``string_ptr`` to its dialog chain.
    chain_extra_placements: Dict[
        int, List[Tuple[int, "overlay5_mod.OverworldSpritePlacement"]]
    ] = field(default_factory=dict)
    # Per-chain inherited-from-caller report: ``(parent_entry_ix,
    # parent_rel, npc_count)`` tuples — one per parent region whose OWS
    # placements were pulled into this chain's extras. Drives the detail
    # panel's "inherited from caller @0xNNNN" annotation so the user
    # knows which inherited markers came from where.
    chain_inherited_parents: Dict[
        int, Tuple[Tuple[int, int, int], ...]
    ] = field(default_factory=dict)
    # Reverse of :func:`_synth_extra_offset`: maps every synthetic
    # block_offset emitted across all chains' extras back to the real
    # ``(entry_ix, block_offset)`` it stands for. The synthetic encoding
    # truncates ``entry_ix & 0xFF`` and is therefore lossy across the
    # full overlay5 entry space (e.g. entries 0xF3 and 0x1F3 collide), so
    # the drag handler must use this table to resolve a moved marker back
    # to the entry it physically belongs to.
    chain_extra_synth_to_real: Dict[int, Tuple[int, int]] = field(
        default_factory=dict,
    )
    # block_offset → chain index, keyed by row kind. Lets the Objects
    # list resolve "user clicked NPC slot 5" to "show OWS chain N's
    # dialog" without re-walking the chains list.
    ows_chain_by_offset: Dict[int, int] = field(default_factory=dict)
    exit_chain_by_offset: Dict[int, int] = field(default_factory=dict)
    hitbox_chain_by_offset: Dict[int, int] = field(default_factory=dict)
    # block_offset → resolved exit form data (duck-typed ``_ExitFormData``
    # from the map browser). Carries the handler location + current dest
    # so the exit-destination card can push an ``EditExitDestinationCommand``
    # without re-decoding the handler. Empty for maps the parent didn't
    # hand form data (older call paths / tests).
    exit_form_by_offset: Dict[int, object] = field(default_factory=dict)
    # (entry_ix, OWS-block-offset) → OWS chain index, indexed across the
    # WHOLE cutscene_index (not just ``chains_for_map``). Lets a HANDLER
    # chain that spawns NPCs in another entry (typically 0499) look up
    # each spawned NPC's dialog chain even when that chain is bucketed
    # under a different map.
    ows_chain_by_spawn: Dict[Tuple[int, int], int] = field(default_factory=dict)


def _chain_sort_key(chain: CutsceneChain) -> tuple:
    """Stable sort key — group by kind (per :data:`_KIND_ORDER`), then by
    a kind-specific numeric value so e.g. ``slot 4`` sorts before
    ``slot 15`` instead of lexicographically.
    """
    kind_rank = (
        _KIND_ORDER.index(chain.trigger_kind)
        if chain.trigger_kind in _KIND_ORDER else len(_KIND_ORDER)
    )
    label = chain.trigger_label
    kind = chain.trigger_kind
    primary = 0
    if kind == TriggerKind.OWS:
        primary = _int_after(label, "slot=")
    elif kind == TriggerKind.EXIT:
        primary = _int_after(label, "EXIT_ZONE#")
    elif kind == TriggerKind.HITBOX:
        primary = _int_after(label, "HITBOX#")
    return (kind_rank, primary, label)


def _int_after(s: str, prefix: str) -> int:
    """Parse the integer immediately following ``prefix`` in ``s``.

    Returns 0 when the prefix is missing or no digits follow — the chip
    falls back to alphabetical secondary ordering in that case.
    """
    i = s.find(prefix)
    if i < 0:
        return 0
    j = i + len(prefix)
    k = j
    while k < len(s) and s[k].isdigit():
        k += 1
    return int(s[j:k]) if k > j else 0


def _secondary_origin_hint(
    chain: CutsceneChain, current_map_id: Optional[int],
) -> Optional[str]:
    """Compact "(top from m52)" / "(bot from m52)" prefix for chains
    listed on a secondary map.

    A chain whose primary source map differs from ``current_map_id``
    is being shown here because its 0x014B / 0x014C body opcode loads
    ``current_map_id`` onto the top / bottom screen. Returns ``None``
    when this is the chain's home map (primary listing), or when the
    UI doesn't know the current map id (no decoration).
    """
    if current_map_id is None:
        return None
    primary_map = chain.source_entry_ix - 235
    if primary_map == current_map_id:
        return None
    via: List[str] = []
    if current_map_id in chain.secondary_top_map_ids:
        via.append("top")
    if current_map_id in chain.secondary_bottom_map_ids:
        via.append("bot")
    if not via:
        return None
    return f"({'/'.join(via)} from m{primary_map})"


def _short_chip_label(
    chain: CutsceneChain,
    handler_summary: Optional["_HandlerSummary"] = None,
    current_map_id: Optional[int] = None,
) -> str:
    """Human-readable chip label for ``chain``.

    Long triggers (the verbose ``OWS_str slot=4 ow_id=0x00ab @0x02ee``
    form) get compressed to ``NPC slot 4 (#00ab)``-style; exits become
    ``Exit #N``; cross-script chains aggregate as ``cross-script``.

    ``handler_summary`` is consulted for HANDLER, HITBOX, EXIT, and
    OTHER (orphan-promoted) chains — it carries the decoded speaker /
    battle data the chip uses to disambiguate one cutscene from
    another on the same trigger index. Optional so callers without a
    session (test fixtures, headless renders) still get a sensible
    structural fallback.

    ``current_map_id`` decorates the label with a "(top from mNN)" /
    "(bot from mNN)" prefix when the chain is being shown on a
    secondary map (its 0x014B / 0x014C body op loads the current map
    onto one of the screens but the chain actually fires from another
    map). Omitted callers get the unprefixed label.
    """
    base_label = _short_chip_label_core(chain, handler_summary)
    hint = _secondary_origin_hint(chain, current_map_id)
    return f"{hint} {base_label}" if hint else base_label


def _short_chip_label_core(
    chain: CutsceneChain,
    handler_summary: Optional["_HandlerSummary"] = None,
) -> str:
    """Inner implementation of :func:`_short_chip_label` minus the
    dual-screen origin hint — kept separate so the hint logic doesn't
    have to repeat the per-kind chip-label dispatch."""
    label = chain.trigger_label
    kind = chain.trigger_kind
    if kind == TriggerKind.OWS:
        # OWS_str slot=N ow_id=0xMMMM @0xPPPP  -> "NPC slot N (#MMMM)"
        slot = _between(label, "slot=", " ")
        ow_id = _between(label, "ow_id=", " ")
        if slot and ow_id:
            return f"NPC slot {slot} ({ow_id})"
        return label
    if kind == TriggerKind.EXIT:
        n = _between(label, "EXIT_ZONE#", " ")
        base = f"Exit #{n}" if n else label
        speaker = _summary_lead_speaker(handler_summary)
        return f"{base}: {speaker}" if speaker else base
    if kind == TriggerKind.HITBOX:
        n = _between(label, "HITBOX#", " ")
        base = f"Hitbox #{n}" if n else label
        speaker = _summary_lead_speaker(handler_summary)
        return f"{base}: {speaker}" if speaker else base
    if kind == TriggerKind.HANDLER:
        return _handler_chip_label(chain, handler_summary)
    if kind == TriggerKind.EXT:
        return "cross-script"
    if kind == TriggerKind.OTHER:
        # Orphan-promoted sub-scene (CALL_SYS-distant call-and-return
        # that the adjacency heuristic refused to chain). Compress the
        # raw "CALL_SYS @0xXXXX" tag to "Sub-scene @0xXXXX" and append
        # the speaker name when the summary picked one up.
        addr = _between(label, "@", " ") or _between(label, "@", "")
        base = f"Sub-scene {addr}" if addr else "Sub-scene"
        speaker = _summary_lead_speaker(handler_summary)
        return f"{base}: {speaker}" if speaker else base
    return label[:40] if len(label) > 40 else (label or "scene")


def _summary_lead_speaker(
    summary: Optional["_HandlerSummary"],
) -> Optional[str]:
    """Extract the dominant speaker name for chip-label decoration.

    Returns ``None`` when the chain has no decoded speakers (a stub
    region, or an opcode the walker can't follow). When a single
    speaker is present, returns just the name; when multiple, returns
    the leading speaker (chain opener) followed by a ``+N`` suffix
    indicating how many other distinct speakers the chain contains, so
    the chip stays compact but still hints at multi-NPC scenes.
    """
    if summary is None or not summary.speakers:
        return None
    name = summary.speakers[0][1]
    extra = len(summary.speakers) - 1
    return f"{name} +{extra}" if extra else name


def _handler_chip_label(
    chain: CutsceneChain, summary: Optional["_HandlerSummary"],
) -> str:
    """Disambiguating chip label for a HANDLER chain.

    Order of precedence — most identifying first:

    1. Battle setup → name the lead non-zero enemy ("Battle vs Mummymon").
    2. One unique speaker → use their name and line count (covers the
       common single-NPC cutscene like Phascomon's sleep dialog).
    3. Multiple speakers → name the first two so the user can tell two
       cutscenes on the same map apart without opening either.
    4. No decoded content but a long path → "Scripted (N regions)", so
       branchy handlers that ``iter_dialogs_from`` can't follow still
       read differently from a single-region empty stub.
    5. Fallback → the legacy ``Handler @0xPPPP`` form.
    """
    fallback_at = _between(chain.trigger_label, "@", " ")
    fallback = f"Handler {fallback_at}" if fallback_at else "Handler"
    if summary is None:
        return fallback
    if summary.battle_enemies:
        # 0 and 0xFFFF are both empty-slot sentinels in battle blocks
        # (the DA 00 layout pre-fills unused enemy positions). Skip
        # them so e.g. ``[ffff, ffff, Kokuwamon, ffff, ffff]`` reads as
        # "Battle vs Kokuwamon" instead of latching onto the sentinel.
        lead_name = next(
            (n for (i, n) in summary.battle_enemies
             if i and i != 0xFFFF), None,
        )
        if lead_name:
            return f"Battle vs {lead_name}"
        return "Battle setup"
    if summary.speakers:
        if len(summary.speakers) == 1:
            _, name, count = summary.speakers[0]
            suffix = f" ({count} lines)" if count > 1 else ""
            return f"{name}{suffix}"
        first_name = summary.speakers[0][1]
        second_name = summary.speakers[1][1]
        more = (
            f" +{len(summary.speakers) - 2}"
            if len(summary.speakers) > 2 else ""
        )
        return f"{first_name}, {second_name}{more}"
    if summary.region_count >= 3:
        return f"Scripted ({summary.region_count} regions)"
    return fallback


@dataclass(frozen=True)
class _HandlerSummary:
    """Decoded content summary for one HANDLER chain.

    Built once per chain on :meth:`CutscenesTab.set_map`, consumed by
    both the chip-label helper and the detail-panel renderer. Body
    decoding is best-effort — :func:`overlay5.iter_dialogs_from` stops
    at the first unrecognized opcode, so a branchy handler may
    under-report its later dialogs. The summary is structured rather
    than pre-formatted so the chip (compact) and the detail panel
    (verbose) can present the same data at different fidelity.

    Speaker tuples are ``(portrait_id, display_name, line_count)`` and
    preserve first-encounter order so the chip surfaces the *opener* of
    the cutscene (which is usually the salient NPC).
    """
    dialog_count: int
    speakers: Tuple[Tuple[int, str, int], ...]
    first_line: Optional[str]
    battle_enemies: Tuple[Tuple[int, str], ...]
    region_count: int
    meta_cond: Optional[int]
    meta_val: Optional[int]
    meta_opaque: Optional[int]


def _safe_display_name(session, sprite_id: int) -> str:
    """``session.digimon_display_name`` with a hex fallback.

    Used to resolve dialog portraits and battle-block enemy ids. Both
    are sprite_map indices per the project memory; the same resolver
    covers digimon, digieggs, fixed enemies, and NPC slots.
    """
    if sprite_id <= 0:
        return f"0x{sprite_id:04x}"
    try:
        return session.digimon_display_name(int(sprite_id))
    except (AttributeError, KeyError, ValueError):
        return f"0x{sprite_id:04x}"


def _load_entry_bytes(
    session, cache: Dict[int, bytes], entry_ix: int,
) -> bytes:
    """Memoized ``overlay5_entry_bytes`` lookup.

    Handler classification walks every region in a chain, and a chain
    that crosses entries (e.g. map 33 sleep cutscene: 499 → 261)
    re-enters the source entry on the next chain. Caching avoids
    re-reading the same 100KB+ entry blob inside one ``set_map``.
    """
    eb = cache.get(entry_ix)
    if eb is not None:
        return eb
    try:
        eb = session.overlay5_entry_bytes(entry_ix)
    except (ValueError, KeyError):
        eb = b""
    cache[entry_ix] = eb
    return eb


def _classify_handler_chain(
    chain: CutsceneChain,
    session,
    cutscene_index,
    entry_cache: Dict[int, bytes],
) -> _HandlerSummary:
    """Decode a HANDLER chain into a :class:`_HandlerSummary`.

    Walks every region in ``chain.path``:

    * collects DIALOG blocks via ``iter_dialogs_from``, bounded by
      ``region.end_rel`` so we don't bleed into the next region;
    * scans the region bytes for the first ``DA 00`` battle setup and
      records its 5 enemy u16s (the layout is documented in the
      :func:`overlay5._try_battle` matcher).

    Also peeks at the source entry's prologue ``HANDLER_META`` block —
    ``A6 00 [cond:2] [val:2] [opaque:4]`` immediately preceding the
    ``REGISTER_HANDLER`` position parsed from the trigger label, when
    present. ``cond`` / ``val`` semantics aren't decoded yet (likely a
    story-flag check), but surfacing the raw values in the detail
    panel lets the user spot handlers that share a trigger condition.
    """
    dialogs = []
    battle_enemies_ids: Tuple[int, ...] = ()
    for entry_ix, rel in chain.path:
        # A warp hop is the cutscene's terminator from this map's POV
        # (the engine is loading another field map); stop pulling
        # content past it so we don't surface the next map's dialogs.
        if _is_map_warp_hop(entry_ix, rel, chain.source_entry_ix):
            break
        region = cutscene_index.regions.get((entry_ix, rel))
        if region is None:
            continue
        eb = _load_entry_bytes(session, entry_cache, entry_ix)
        if not eb:
            continue
        # Bounded meta walker (matches the chain-detail renderer) — so
        # the chip label and the open detail panel can't disagree on
        # dialog count or speaker list.
        region_dialogs, _ = overlay5_mod.iter_dialogs_from_with_meta(
            eb, rel, region.end_rel,
        )
        dialogs.extend(region_dialogs)
        if not battle_enemies_ids:
            end = min(region.end_rel, len(eb) - 12)
            p = rel
            while p < end:
                if eb[p] == 0xDA and eb[p + 1] == 0x00:
                    battle_enemies_ids = tuple(
                        struct.unpack_from("<H", eb, p + 2 + 2 * i)[0]
                        for i in range(5)
                    )
                    break
                p += 1

    speaker_order: List[int] = []
    speaker_count: Dict[int, int] = {}
    for d in dialogs:
        if d.portrait not in speaker_count:
            speaker_order.append(d.portrait)
        speaker_count[d.portrait] = speaker_count.get(d.portrait, 0) + 1
    speakers = tuple(
        (pid, _safe_display_name(session, pid), speaker_count[pid])
        for pid in speaker_order
    )

    first_line: Optional[str] = None
    if dialogs:
        try:
            gs = session.dialog_msg_text(int(dialogs[0].msg_id))
        except (AttributeError, ValueError):
            gs = None
        if gs is not None:
            text = getattr(gs, "text", None) or ""
            if text:
                first_line = text

    battle_enemies = tuple(
        (eid, _safe_display_name(session, eid)) for eid in battle_enemies_ids
    )

    meta_cond = meta_val = meta_opaque = None
    p_reg = _parse_trigger_at_offset(chain.trigger_label)
    if p_reg is not None and p_reg >= 10:
        eb = _load_entry_bytes(session, entry_cache, chain.source_entry_ix)
        if (
            eb
            and p_reg + 6 <= len(eb)
            and eb[p_reg - 10] == 0xA6
            and eb[p_reg - 9] == 0x00
        ):
            meta_cond = struct.unpack_from("<H", eb, p_reg - 8)[0]
            meta_val = struct.unpack_from("<H", eb, p_reg - 6)[0]
            meta_opaque = struct.unpack_from("<I", eb, p_reg - 4)[0]

    return _HandlerSummary(
        dialog_count=len(dialogs),
        speakers=speakers,
        first_line=first_line,
        battle_enemies=battle_enemies,
        region_count=len(chain.path),
        meta_cond=meta_cond,
        meta_val=meta_val,
        meta_opaque=meta_opaque,
    )


def _between(s: str, prefix: str, terminator: str) -> str:
    i = s.find(prefix)
    if i < 0:
        return ""
    j = i + len(prefix)
    k = s.find(terminator, j)
    return s[j:k] if k >= 0 else s[j:]


# Synthetic block_offset namespace for chain-extras markers. The canvas
# keys ``_marker_by_offset`` by block_offset, so cross-entry extras
# (typically from entry 0499) must not collide with base markers (whose
# offsets sit in [0, 0x10000) inside the map entry). Shifting entry_ix
# into bits 24..31 and rel into bits 0..15 gives a unique key per
# (entry_ix, rel) and lifts every synthetic offset above 0x01000000.
_CHAIN_EXTRA_OFFSET_BASE = 0x01000000


def _synth_extra_offset(entry_ix: int, rel: int) -> int:
    return _CHAIN_EXTRA_OFFSET_BASE + ((entry_ix & 0xFF) << 16) + (rel & 0xFFFF)


def _placements_for_entry(
    session,
    entry_cache: Dict[int, bytes],
    placement_cache: Dict[int, List["overlay5_mod.OverworldSpritePlacement"]],
    entry_ix: int,
) -> List["overlay5_mod.OverworldSpritePlacement"]:
    """Memoized full-entry OVERWORLD_SPRITE scan.

    Handler entries (0499 in particular) are large — scanning once and
    filtering per region across many chains is much cheaper than
    rescanning the entry per region.
    """
    cached = placement_cache.get(entry_ix)
    if cached is not None:
        return cached
    eb = _load_entry_bytes(session, entry_cache, entry_ix)
    placements = overlay5_mod.iter_overworld_sprites(eb) if eb else []
    placement_cache[entry_ix] = placements
    return placements


def _resolve_marker_pixmap(
    session, ow_id: int, behavior: Optional[int],
    cache: Dict[Tuple[int, Optional[int]], Optional["QPixmap"]],
):
    """Memoized overworld-object pixmap lookup that falls through to the session.

    ``ow_id`` is an OVERWORLD_SPRITE placement id (the 906-entry ow-id space),
    resolved through the ARM9 table to a CHR graphic under that id's palette
    (see session.mchr_sprite_pixmap_by_ow_id) — so a chain-spawned chest
    (ow-id 0x2fb) renders as the chest, not the unrelated CHR-0x2fb graphic.
    The per-render cache avoids the session cache-key tuple build for sprites
    referenced by multiple placements (common in chain-spawned NPC pairs).
    """
    key = (int(ow_id), int(behavior) if behavior is not None else None)
    if key in cache:
        return cache[key]
    try:
        pix = session.mchr_sprite_pixmap_by_ow_id(
            int(ow_id), max_size=512,
            frame=int(behavior) if behavior is not None else None,
        )
    except (AttributeError, ValueError):
        pix = None
    cache[key] = pix
    return pix


def _compute_chain_extras(
    chain: CutsceneChain,
    session,
    cutscene_index,
    entry_cache: Dict[int, bytes],
    placement_cache: Dict[int, List["overlay5_mod.OverworldSpritePlacement"]],
    base_entry_ix: int,
    base_specs_by_offset: Dict[int, EventMarkerSpec],
    mchr_label_by_id: Dict[int, str],
    pixmap_cache: Dict[Tuple[int, Optional[int]], Optional["QPixmap"]],
) -> Tuple[
    List[EventMarkerSpec],
    List[Tuple[int, "overlay5_mod.OverworldSpritePlacement"]],
    Tuple[Tuple[int, int, int], ...],
]:
    """Collect every OVERWORLD_SPRITE placement *this chain* owns, with
    proper sprite pixmaps so the canvas renders the NPC's frame instead
    of a blank circle.

    What counts as "this chain's" sprite:

    * Any 0x0150 block that physically lives inside one of the chain's
      ``path`` regions (typically cutscene-spawned NPCs from handler
      entries like 0499) — these are added with a freshly resolved
      pixmap from ``session.mchr_sprite_pixmap``.
    * For OWS chains: the trigger NPC itself. Its placement lives in
      the base map's prologue (outside any chain region), so we look
      it up via the trigger label's ``@0x{offset:04x}`` and pull the
      base spec directly. Without this the OWS chip would render an
      empty canvas — the cutscene's subject NPC would be hidden the
      moment the user clicked it.

    Base-overlap is allowed inside chain regions but de-duplicated by
    ``(entry_ix, block_offset)`` so a region that re-lists an NPC
    already present in the base only produces one marker.
    """
    extras: List[EventMarkerSpec] = []
    raw_placements: List[Tuple[int, "overlay5_mod.OverworldSpritePlacement"]] = []
    seen_keys: set = set()

    # (1) OWS trigger NPC — pulled from the base spec list so the marker
    # carries the same pixmap + label as the Base view's render. Without
    # this, OWS chips would hide the talking NPC entirely. Rewrap the
    # spec with the chain-extra ``entry NNNN @0xPPPP`` display label so
    # every marker on the canvas reads in the same format.
    if chain.trigger_kind == TriggerKind.OWS:
        trig_off = _parse_trigger_at_offset(chain.trigger_label)
        if trig_off is not None:
            trig_spec = base_specs_by_offset.get(trig_off)
            if trig_spec is not None:
                seen_keys.add((base_entry_ix, trig_off))
                extras.append(EventMarkerSpec(
                    block_offset=trig_spec.block_offset,
                    overworld_sprite_id=trig_spec.overworld_sprite_id,
                    x=trig_spec.x,
                    y=trig_spec.y,
                    label=trig_spec.label,
                    pixmap=trig_spec.pixmap,
                    behavior=trig_spec.behavior,
                    display_label=f"entry {base_entry_ix:04d} @0x{trig_off:04x}",
                ))

    # (2) Chain-owned placements inside the path's region spans.
    for entry_ix, rel in chain.path:
        # Warp into another map: the placements past this hop belong
        # to the warped-into map and would render here as ghost NPCs.
        if _is_map_warp_hop(entry_ix, rel, base_entry_ix):
            break
        region = cutscene_index.regions.get((entry_ix, rel))
        if region is None:
            continue
        for placement in _placements_for_entry(
            session, entry_cache, placement_cache, entry_ix,
        ):
            if not (rel <= placement.block_offset < region.end_rel):
                continue
            key = (entry_ix, placement.block_offset)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            raw_placements.append((entry_ix, placement))
            sid = placement.overworld_sprite_id
            label = mchr_label_by_id.get(
                sid, f"MCHR 0x{sid:04x}",
            )
            extras.append(EventMarkerSpec(
                block_offset=_synth_extra_offset(entry_ix, placement.block_offset),
                overworld_sprite_id=sid,
                x=placement.x,
                y=placement.y,
                label=(
                    f"{label} \u00b7 cutscene placement "
                    f"\u00b7 entry {entry_ix:04d} +0x{placement.block_offset:04x} "
                    f"\u00b7 slot {placement.slot}"
                ),
                pixmap=_resolve_marker_pixmap(
                    session, sid, placement.behavior, pixmap_cache,
                ),
                behavior=placement.behavior,
                display_label=(
                    f"entry {entry_ix:04d} @0x{placement.block_offset:04x}"
                ),
            ))

    # (3) Inherited placements: if this chain's head is a sub-scene reached
    # via a CALL_SYS-distant link, pull the parent handler's spawned NPCs
    # into the canvas. The sub-scene itself only emits dialogs — the OWS
    # blocks that physically place the speakers live in the parent
    # handler's region span (e.g., entry 0499 handler_5960 spawns Julia +
    # the Day Care crew, then CALL_SYS-distant into region_5c98 which
    # carries only Julia's dialog). Without this propagation the sub-scene
    # chip would render an empty canvas with no speakers.
    #
    # Per-caller semantics: when multiple parents call the same sub-scene
    # with different OWS sets, all of them are merged into this chain's
    # extras (the chain identity itself doesn't fork per caller — a future
    # change may switch to one chain per caller-sub_scene pair).
    head_key = chain.path[0] if chain.path else None
    parent_keys: Tuple[Tuple[int, int], ...] = ()
    if head_key is not None:
        parent_keys = getattr(
            cutscene_index, "parent_regions_by_sub_scene", {},
        ).get(head_key, ())
    inherited_parents: List[Tuple[int, int, int]] = []
    for p_entry_ix, p_rel in parent_keys:
        p_region = cutscene_index.regions.get((p_entry_ix, p_rel))
        if p_region is None:
            continue
        added = 0
        for placement in _placements_for_entry(
            session, entry_cache, placement_cache, p_entry_ix,
        ):
            if not (p_rel <= placement.block_offset < p_region.end_rel):
                continue
            key = (p_entry_ix, placement.block_offset)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            raw_placements.append((p_entry_ix, placement))
            sid = placement.overworld_sprite_id
            label = mchr_label_by_id.get(
                sid, f"MCHR 0x{sid:04x}",
            )
            extras.append(EventMarkerSpec(
                block_offset=_synth_extra_offset(p_entry_ix, placement.block_offset),
                overworld_sprite_id=sid,
                x=placement.x,
                y=placement.y,
                label=(
                    f"{label} \u00b7 inherited from caller "
                    f"\u00b7 entry {p_entry_ix:04d} +0x{placement.block_offset:04x} "
                    f"\u00b7 slot {placement.slot}"
                ),
                pixmap=_resolve_marker_pixmap(
                    session, sid, placement.behavior, pixmap_cache,
                ),
                behavior=placement.behavior,
                display_label=(
                    f"entry {p_entry_ix:04d} @0x{placement.block_offset:04x}"
                ),
            ))
            added += 1
        if added:
            inherited_parents.append((p_entry_ix, p_rel, added))
    return extras, raw_placements, tuple(inherited_parents)


# Inline stylesheet prepended to every QLabel-rendered read-only section
# in the detail panel. Kept module-level so every render reuses the same
# string instead of rebuilding it per section.
_HTML_STYLE = """
<style>
  body { color: #cccccc; font-size: 12px; }
  .hdr { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
  .sec-hdr { font-weight: 600; margin: 12px 0 4px 0;
             border-bottom: 1px solid #3c3c3c; padding-bottom: 2px; }
  .muted { color: #858585; }
  .small { font-size: 11px; }
  .code { font-family: Consolas, monospace; color: #d4d4d4; }
  .kind { background: #007acc; color: white; padding: 1px 6px;
          border-radius: 8px; font-size: 11px; }
  table.kt { border-collapse: collapse; margin: 6px 0; }
  table.kt td { padding: 2px 8px 2px 0; }
  table.kt td.dot span { display: inline-block; width: 8px; height: 8px;
                         border-radius: 4px; }
  table.kt td.num { color: #cccccc; text-align: right; font-weight: 600; }
  ol.path { margin: 4px 0 0 0; padding-left: 22px; }
  ol.path li { margin-bottom: 2px; }
  .dlg-list { margin-top: 4px; }
  /* QTextDocument honors inner block margins reliably but its
     block-level padding/margin on the wrapping <div> is flaky, so we
     enforce inter-card spacing from inside: header sits flush with the
     card top, body carries the trailing gap that visually separates
     the next card's portrait. Zebra-striping comes via an INLINE
     ``style`` attribute on alternating cards rather than a sibling
     ``.dlg-alt`` class — Qt's CSS engine is unreliable about second-
     class matches inside class-rule documents, but inline style always
     wins. */
  .dlg { background: #1e1e1e; border-left: 2px solid #4a8db8;
         padding: 6px 8px; }
  .dlg-hdr { margin-bottom: 6px; }
  .dlg-hdr .portrait { font-weight: 600; color: #ffffff; }
  .dlg-body { color: #e6e6e6; white-space: pre-wrap; }
</style>
"""


# Game-script control tokens we want to render inline instead of leaking
# the bracketed form. ``[BR]`` is a newline (paragraph break is also
# rendered as a newline — visually identical inside the dialog card).
# Anything else stays bracketed; surfacing the unknown token helps the
# user spot scenes that depend on opcodes we haven't decoded yet.
_DIALOG_TOKEN_REPLACEMENTS = {
    "[BR]": "<br>",
    "[CP]": "<br>",
    "[PAGE]": "<br>",
}


def _format_dialog_text(text: str) -> str:
    """HTML-escape ``text`` and convert game-script control tokens.

    The MSG.PAK strings embed ``[BR]`` and similar tokens for line breaks
    / paragraph splits. We HTML-escape first (so a literal ``<`` in user
    text doesn't open a tag), then swap the bracketed tokens for ``<br>``
    so the dialog body wraps the way the game would render it.
    """
    escaped = html.escape(text)
    for token, repl in _DIALOG_TOKEN_REPLACEMENTS.items():
        escaped = escaped.replace(token, repl)
    return escaped


def _is_map_warp_hop(entry_ix: int, rel: int, base_entry_ix: int) -> bool:
    """``True`` when the chain hop ``(entry_ix, rel)`` is a warp out of
    the source map into another field map's init script.

    A CALL_SCRIPT_AT_OFFSET that targets a defined map entry
    (``map_id_for(entry_ix) is not None``) at rel ``0x0000`` is the
    engine's way of loading another map — the chain has effectively
    ended for the *current* map's purposes. Everything past this hop
    belongs to the warped-into map; deriving its dialogs, sprite
    placements, or battle blocks here would pollute the source map's
    cutscene view with content the player would only see *after* the
    warp.

    Hops back into the source map's own entry (``entry_ix ==
    base_entry_ix``) are NOT warps — they're tail-call jumps inside
    the same map's script body and should keep contributing content.
    """
    if rel != 0:
        return False
    if entry_ix == base_entry_ix:
        return False
    return overlay5_mod.map_id_for(entry_ix) is not None


def _chain_events_global(session, cindex, chain, entry_cache) -> list:
    """Walk a chain's regions and return its editable events, independent of
    any per-map tab ``_state`` — used by the cross-map cutscene Index.

    Mirrors :meth:`CutscenesTab._collect_events_for_chain` but takes the
    session + index explicitly so it can run over every chain in the game
    without a loaded map. Stops at map-warp hops (their content belongs to
    the warped-into map)."""
    out: list = []
    for entry_ix, rel in chain.path:
        if _is_map_warp_hop(entry_ix, rel, chain.source_entry_ix):
            break
        region = cindex.regions.get((entry_ix, rel))
        if region is None:
            continue
        eb = entry_cache.get(entry_ix)
        if eb is None:
            try:
                eb = session.overlay5_entry_bytes(entry_ix)
            except (ValueError, KeyError):
                eb = b""
            entry_cache[entry_ix] = eb
        if not eb:
            continue
        evs, _ = overlay5_mod.iter_region_events_with_meta(eb, rel, region.end_rel)
        out.extend(evs)
    return out


# Event kinds that make a chain a genuine cutscene. A bare ``wait`` doesn't
# count — a chain of only WAIT_FRAMES is a script-timing fragment (e.g. the
# ``CALL_SYS ext=0500`` stubs), not something worth listing/editing.
_SUBSTANTIVE_EVENT_KINDS = frozenset({
    overlay5_mod.EVENT_KIND_DIALOG,
    overlay5_mod.EVENT_KIND_SPRITE_ANIM,
    overlay5_mod.EVENT_KIND_CAMERA,
    overlay5_mod.EVENT_KIND_BATTLE,
    overlay5_mod.EVENT_KIND_ITEM,
    overlay5_mod.EVENT_KIND_REACTION,
    overlay5_mod.EVENT_KIND_MOVE,
})


# One row of the cross-map cutscene Index.
class _SceneRow(NamedTuple):
    map_id: Optional[int]   # field-map home, or host map for a shared scene
    shared: bool            # source entry isn't a field map (shared/cross-map)
    entry_ix: int
    first_rel: int
    global_ix: int
    kind: str
    label: str
    counts: Dict[str, int]


def _build_scene_index_rows(session) -> List["_SceneRow"]:
    """Ordered, cross-map list of the game's real cutscenes for the Index.

    One row per chain that carries a *substantive* event (dialog / anim /
    camera / battle / item / reaction — see :data:`_SUBSTANTIVE_EVENT_KINDS`).
    Wait-only fragments and pure-warp / empty chains are dropped: they aren't
    cutscenes and (for warps) already live in the map view's Exit zones.

    ``map_id`` is the chain's own field map, or — when the scene lives in a
    shared/non-field entry — the field map that hosts it (resolved through
    ``chains_by_map``), so a click can still navigate there. Scenes with no
    host stay ``map_id=None`` (``shared=True``) and get rendered standalone.

    Ordered by ``(entry_ix, first-region offset)`` — the scripts' own
    definition order."""
    try:
        cindex = session.cutscene_index()
    except (ValueError, KeyError):
        return []
    # global chain index → a field map that hosts it (for shared scenes).
    host_by_gix: Dict[int, int] = {}
    for mid, ixs in cindex.chains_by_map.items():
        for gix in ixs:
            host_by_gix.setdefault(gix, mid)
    entry_cache: Dict[int, bytes] = {}
    rows: List[_SceneRow] = []
    for global_ix, chain in enumerate(cindex.chains):
        events = _chain_events_global(session, cindex, chain, entry_cache)
        counts: Dict[str, int] = {}
        for ev in events:
            counts[ev.kind] = counts.get(ev.kind, 0) + 1
        if not any(counts.get(k) for k in _SUBSTANTIVE_EVENT_KINDS):
            continue
        own_map = overlay5_mod.map_id_for(chain.source_entry_ix)
        shared = own_map is None
        map_id = own_map if own_map is not None else host_by_gix.get(global_ix)
        first_rel = chain.path[0][1] if chain.path else 0
        rows.append(_SceneRow(
            map_id=map_id,
            shared=shared,
            entry_ix=int(chain.source_entry_ix),
            first_rel=int(first_rel),
            global_ix=global_ix,
            kind=chain.trigger_kind,
            label=chain.trigger_label or "",
            counts=counts,
        ))
    rows.sort(key=lambda r: (r.entry_ix, r.first_rel, r.global_ix))
    return rows


_SCENE_KIND_TAG = {
    TriggerKind.OWS: "NPC",
    TriggerKind.HANDLER: "Handler",
    TriggerKind.OTHER: "Sub-scene",
    TriggerKind.EXT: "Cross-script",
    TriggerKind.HITBOX: "Hitbox",
    TriggerKind.EXIT: "Exit",
}

# Order + short names for the per-row event-count summary in the Index.
_SCENE_COUNT_ORDER = [
    ("dialog", "dlg"), ("battle", "btl"), ("sprite_anim", "anim"),
    ("camera", "cam"), ("item", "item"), ("reaction", "react"),
    ("wait", "wait"),
]


def _scene_row_label(r: "_SceneRow", ow_name: Optional[str] = None) -> str:
    """Compact one-line label for an Index scene row: kind + trigger detail
    + an event-count summary (``NPC slot 4 (0x004b · Phascomon) · 14 dlg``).

    ``ow_name`` (resolved by the caller for OWS rows) names the sprite id in
    the brackets when the id has a known name."""
    tag = _SCENE_KIND_TAG.get(r.kind, r.kind)
    detail = ""
    if r.kind == TriggerKind.OWS:
        slot = _between(r.label, "slot=", " ")
        owid = _between(r.label, "ow_id=", " ")
        who = f"{owid} · {ow_name}" if ow_name else owid
        detail = f" slot {slot} ({who})" if slot else ""
    elif r.kind in (TriggerKind.OTHER, TriggerKind.HANDLER, TriggerKind.EXT):
        addr = _between(r.label, "@", " ") or _between(r.label, "@", "")
        detail = f" @{addr}" if addr else ""
    elif r.kind == TriggerKind.HITBOX:
        detail = f" #{_int_after(r.label, 'HITBOX#')}"
    elif r.kind == TriggerKind.EXIT:
        detail = f" #{_int_after(r.label, 'EXIT_ZONE#')}"
    parts = [
        f"{r.counts[k]} {nm}" for k, nm in _SCENE_COUNT_ORDER if r.counts.get(k)
    ]
    summary = ("  ·  " + ", ".join(parts)) if parts else ""
    # A scene sourced from a shared/non-field entry but shown under its host
    # map gets a marker so it's clear it isn't one of the map's own triggers.
    shared_mark = "⇱ " if r.shared else ""
    return f"{shared_mark}{tag}{detail}{summary}"


def _parse_trigger_at_offset(label: str) -> Optional[int]:
    """Extract the ``@0x{hex}`` block offset embedded in a trigger label.

    All three locatable kinds — ``OWS_str``, ``EXIT_ZONE#N``, ``HITBOX#N`` —
    end with ``@0x{p:04x}`` where ``p`` is the entry-relative offset of the
    OVERWORLD_SPRITE / EXIT_ZONE block that owns the trigger. That offset
    is exactly the ``block_offset`` field on :class:`EventMarkerSpec` /
    :class:`ExitZoneSpec`, so parsing it lets us drive canvas selection
    without keeping a separate slot/idx → block_offset map.

    Returns ``None`` when the label has no ``@0x...`` token (e.g.
    handler/ext/header chains, which don't correspond to a visible
    canvas item).
    """
    i = label.find("@0x")
    if i < 0:
        return None
    j = i + 3
    k = j
    while k < len(label) and label[k] in "0123456789abcdefABCDEF":
        k += 1
    if k == j:
        return None
    try:
        return int(label[j:k], 16)
    except ValueError:
        return None


# ---- exit-destination helpers -------------------------------------------
#
# A map exit's target is the CALL_SCRIPT_AT_OFFSET (op 0x0030) u32 inside
# its handler script — an absolute overlay5 file offset that points at the
# destination map entry's start. The Events tab already surfaces this as an
# editable combo; these helpers mirror that resolution for the Cutscenes
# tab but render the friendly "warp to map N (Area)" label the user asked
# for instead of the bare "Map N (entry NNNN)" form.

# Combo sentinel — the "leave as-is / raw offset" row that sits first so a
# destination the editor can't name still shows something selectable.
_EXIT_DEST_CUSTOM = -1


def _warp_dest_label(map_id: int) -> str:
    """The user-facing exit-destination string, e.g. ``warp to map 70
    (Chip Forest)``."""
    return f"warp to map {map_id} ({area_name(map_id)})"


def _resolve_warp_dest(session, file_off: int) -> Tuple[Optional[int], str]:
    """Resolve an exit handler's op 0x0030 u32 into ``(map_id, label)``.

    ``file_off`` is the absolute overlay5 offset the handler jumps to.
    When it starts a known field-map entry we return that map id and the
    ``warp to map N (Area)`` label; otherwise ``(None, raw-hex)`` so the
    combo can still display whatever's currently set as a Custom row.
    """
    if file_off <= 0:
        return None, ""
    try:
        index = session.overlay5_index()
    except (ValueError, KeyError, AttributeError):
        return None, f"0x{file_off:08x} (raw)"
    for entry_ix, start in index.entry_starts.items():
        if start != file_off:
            continue
        map_id = overlay5_mod.map_id_for(entry_ix)
        if map_id is not None:
            return map_id, _warp_dest_label(map_id)
        return None, f"entry {entry_ix:04d} (non-map)"
    return None, f"0x{file_off:08x} (raw)"


def _exit_dest_choices(session) -> List[Tuple[int, str]]:
    """``[(entry_start_file_off, "warp to map N (Area)"), ...]`` for every
    field map, sorted by map id — the pickable destinations for an exit.

    Returns an empty list when the session can't produce an overlay5
    index (headless / test paths); the card falls back to its sticky
    Custom row so the current destination is still visible.
    """
    try:
        index = session.overlay5_index()
    except (ValueError, KeyError, AttributeError):
        return []
    rows: List[Tuple[int, int, str]] = []
    for entry_ix, start in index.entry_starts.items():
        map_id = overlay5_mod.map_id_for(entry_ix)
        if map_id is None:
            continue
        rows.append((map_id, int(start), _warp_dest_label(map_id)))
    rows.sort(key=lambda r: r[0])
    return [(start, label) for _mid, start, label in rows]


# ---- overworld chest (give-item) helpers --------------------------------
#
# A chest is an OVERWORLD_SPRITE (id 0x02fb) whose interaction script sets
# ARG_1 (SET_VAR var 0x0005) to an item id then CALL_SYS 0x126a. See
# ``digimon_core.overlay5.replace_chest_item`` + the project memory
# ``project_overworld_chest_giveitem`` for the full anatomy. We surface the
# item as the one editable field; the flag / frame check stay read-only.

# How far past the script head to scan for the SET_VAR + CALL_SYS run.
_CHEST_SCAN_WINDOW = 0x40


@dataclass(frozen=True)
class _ChestInfo:
    """Decoded give-item data for one chest sprite.

    ``item_value_offset`` is the in-``script_entry_ix`` offset of the ARG_1
    item u16 — the splice address :class:`EditChestItemCommand` writes.
    ``flag`` / ``arg2`` / ``arg3`` are shown read-only for context.
    """
    script_entry_ix: int
    item_value_offset: int
    item_id: int
    flag: int
    arg2: int
    arg3: int


def _item_choices() -> List[Tuple[int, str]]:
    """``[(item_id, "Name (0xID)"), ...]`` for the chest item combo, id-sorted."""
    return [
        (iid, f"{name} (0x{iid:02x})")
        for iid, name in sorted(ITEM_ID_TO_STR.items())
    ]


def _item_display_name(item_id: int) -> str:
    """Item id → ``"Name (0xID)"`` with a hex fallback for unnamed ids."""
    name = ITEM_ID_TO_STR.get(int(item_id))
    if name is None:
        return f"0x{int(item_id):02x}"
    return f"{name} (0x{int(item_id):02x})"


# ---- slot / target disambiguation ---------------------------------------
#
# Scene opcodes address actors by *slot* index. Slots 0..3 are always the
# player + party digimon 1/2/3; slots 4+ are OVERWORLD_SPRITE placements in
# the scene (each placement carries its own ``slot``). A slot picker shows
# the resolved character so you edit "who", not a bare index.
_FIXED_SLOT_LABELS = {0: "Player", 1: "Party 1", 2: "Party 2", 3: "Party 3"}


def _clean_ow_name(raw: str) -> str:
    """Pull the character name out of a verbose ow-sprite label like
    ``"0x0226  [OW] OphanimonC - 6f 64×64"`` → ``"OphanimonC"``. Falls back
    to the raw string when it doesn't match that shape (unnamed sprites)."""
    s = raw
    if "[OW]" in s:
        s = s.split("[OW]", 1)[1]
    if " - " in s:
        s = s.split(" - ", 1)[0]
    s = s.strip()
    return s or raw


def _slot_label(slot_labels: Dict[int, str], slot: int) -> str:
    """Friendly one-liner for a slot: name if known, else ``slot N``."""
    slot = int(slot) & 0xFFFF
    name = slot_labels.get(slot)
    return f"slot {slot} — {name}" if name else f"slot {slot}"


def _populate_slot_combo(
    combo, slot_labels: Dict[int, str], current: int,
    referenced: Optional[set] = None,
) -> None:
    """Fill an editable combo with ``slot N — Name`` rows (userData=slot),
    landing on ``current`` (sticky-added when the slot has no known name).

    When ``referenced`` is given and splits the slots (some referenced, some
    not), the slots this scene's opcodes actually drive are grouped first
    under a non-selectable ``— in this scene —`` header, with the remaining
    map-declared slots under ``— other slots —``. This keeps the combo from
    implying every map sprite is part of the selected cutscene."""
    combo.clear()

    def _label(slot: int) -> str:
        nm = slot_labels.get(slot)
        return f"{slot} — {nm}" if nm else f"{slot} — (no sprite)"

    def _add_header(text: str) -> None:
        combo.addItem(text)
        model = combo.model()
        item = model.item(combo.count() - 1) if model is not None else None
        if item is not None:
            item.setFlags(
                item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled
            )

    ref = {int(s) & 0xFFFF for s in (referenced or ())}
    named = set(slot_labels)
    in_scene = sorted(named & ref)
    others = sorted(named - ref)
    if in_scene and others:
        _add_header("— in this scene —")
        for slot in in_scene:
            combo.addItem(_label(slot), slot)
        _add_header("— other slots —")
        for slot in others:
            combo.addItem(_label(slot), slot)
    else:
        for slot in sorted(named):
            combo.addItem(_label(slot), slot)

    cur = int(current) & 0xFFFF
    ix = combo.findData(cur)
    if ix < 0:
        combo.addItem(f"{cur} — (no sprite)", cur)
        ix = combo.findData(cur)
    combo.setCurrentIndex(ix)


def _slot_combo_value(combo) -> Optional[int]:
    """Resolve a slot combo to its slot index.

    The display text always leads with the slot number — both a picked
    row (``"4 — OphanimonC"``) and a freshly-typed value (``"7"`` /
    ``"0x10"``) — so parse that. A typed value leaves ``currentData``
    stale, which is why the text is the source of truth; userData is only
    a fallback when the text can't be parsed.
    """
    txt = (combo.currentText() or "").strip()
    tok = txt.split()[0] if txt else ""
    try:
        return int(tok, 0) & 0xFFFF
    except ValueError:
        data = combo.currentData()
        return int(data) & 0xFFFF if data is not None else None


# ---- wheel-ignoring input widgets ---------------------------------------
#
# Qt's default QSpinBox and QComboBox capture the mouse wheel and shift
# their value on scroll, even when the widget isn't focused. Inside a
# scrollable card grid that's a nasty foot-gun: dragging a two-finger
# scroll over the details panel would silently retarget every combo the
# cursor passed over. We swap the widgets used in the cutscene cards
# for these two subclasses that eat wheel events entirely — the outer
# scroll area still scrolls normally because Qt propagates the unhandled
# event to the parent.


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:  # noqa: D401
        event.ignore()


class _NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:  # noqa: D401
        event.ignore()


class _CollapsibleSection(QWidget):
    """A click-to-expand section — a header toggle + a hidden content
    column. Used to tuck the chain's script/debug details (handler
    summary, path, unmapped opcodes) out of the way so the editable
    events read first; collapsed by default.
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(0)
        self._btn = QToolButton()
        self._btn.setText(title)
        self._btn.setCheckable(True)
        self._btn.setChecked(False)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._btn.setArrowType(Qt.RightArrow)
        self._btn.setStyleSheet(
            "QToolButton { border: none; color: #9a9a9a; font-weight: 600;"
            " font-size: 11px; padding: 3px 0; text-align: left; }"
            "QToolButton:hover { color: #cccccc; }"
        )
        self._btn.toggled.connect(self._on_toggled)
        outer.addWidget(self._btn, 0)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)
        self._content.setVisible(False)
        outer.addWidget(self._content, 0)

    def _on_toggled(self, checked: bool) -> None:
        self._btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self._content.setVisible(checked)

    def add_html(self, html_str: str) -> None:
        label = QLabel(_HTML_STYLE + html_str)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._content_layout.addWidget(label)


class _DialogCard(QFrame):
    """One DIALOG block as an inline editable card.

    Replaces the read-only HTML render with native widgets so the user
    can edit the four mutable fields (portrait, target slot, msg id,
    string body) in place, without trapping any of them behind an
    "[edit]" link or modal popup. Each card owns its own debounced
    text-commit timer so a burst of keystrokes lands as a single
    SetAttrCommand on the undo stack instead of one per character.

    Field semantics (per project memory):

    * ``portrait`` — sprite_map index, resolves to a display name via
      ``digimon_display_name`` (covers digimon / digieggs / bosses /
      NPCs uniformly). Drives both the speaker name AND the face shown
      next to it.
    * ``target`` — OVERWORLD_SPRITE slot of *who is speaking*. The
      slot index is what dialog scripts identify the speaker by;
      exposed as a raw spinbox since slot ids change per chain.
    * ``msg_id`` — MSG.PAK row. Resolved through
      ``session.dialog_msg_text`` so the body text can be edited as the
      user reads it; the same-byte-budget rule applies.
    * Body text — rendered via SetAttrCommand on the resolved
      GameString, with ``[BR]`` round-tripping as ``\n``.
    """

    def __init__(
        self,
        session,
        undo_stack,
        entry_ix: int,
        dialog_block,
        alt: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block_offset = int(dialog_block.block_offset)
        self._dialog_block = dialog_block
        # The GameString currently bound to the body editor, or None
        # when the msg_id doesn't resolve (e.g. MSG.PAK rewrites that
        # dropped the slot). Used by the debounced commit + by the
        # redo/undo on-change callback to refresh the body in place.
        self._bound_gs = None
        # Suppress signal slots while we seed the widgets from model
        # state (initial build, undo/redo refresh) so spurious commits
        # don't fire.
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        bg = "#2f3036" if alt else "#27282d"
        self.setStyleSheet(
            f"_DialogCard {{ background: {bg}; border: 1px solid #1f1f23;"
            " border-radius: 3px; }"
            "QLabel { color: #cccccc; background: transparent; }"
            "QComboBox { color: #cccccc; background: #3c3c3c; }"
            "QComboBox QAbstractItemView { color: #cccccc; background: #2d2d30;"
            " selection-background-color: #094771; selection-color: white; }"
            "QSpinBox { color: #cccccc; background: #3c3c3c; }"
            "QPlainTextEdit { color: #e8e8e8; background: #1e1e21;"
            " selection-background-color: #094771; border: 1px solid #1f1f23; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        # ---- Header row: portrait icon + name combo + target/msg spinboxes
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        self._portrait_label = QLabel()
        self._portrait_label.setFixedSize(40, 40)
        self._portrait_label.setAlignment(Qt.AlignCenter)
        self._portrait_label.setStyleSheet(
            "QLabel { border: 1px solid #1f1f23; background: #1e1e21; }"
        )
        header.addWidget(self._portrait_label, 0)

        # Name (portrait id) picker — sprite_map model, completer for
        # name-substring search. The "portrait" u16 IS the sprite_map
        # index per project memory, so a single combo drives both the
        # face image and the speaker name.
        #
        # Built MINIMAL at construction: no editable mode, no shared
        # model, no completer. Each of those costs ~10-30ms (per the
        # editor widget-pool memory) and across N dialog cards the
        # compound cost made chain loads visibly slow ("long long time
        # to load"). The heavy attach happens on a deferred tick via
        # ``_attach_portrait_combo_model`` so all cards lay out in a
        # single fast frame and the editable/search behaviour fills
        # in over the next few ticks while the user reads the header.
        self._portrait_combo = _NoWheelComboBox()
        self._portrait_combo.setMaxVisibleItems(20)
        self._portrait_combo.setMinimumWidth(180)
        self._portrait_combo.currentIndexChanged.connect(
            self._on_portrait_committed,
        )
        header.addWidget(self._portrait_combo, 1)
        # Latches once the full model+completer attach is done so a
        # second deferred tick (e.g. on refresh) doesn't re-do the work.
        self._portrait_combo_fully_attached: bool = False

        target_label = QLabel("slot")
        target_label.setStyleSheet("color: #888;")
        header.addWidget(target_label, 0)
        self._target_spin = _NoWheelSpinBox()
        self._target_spin.setRange(0, 0xFFFF)
        self._target_spin.setKeyboardTracking(False)
        self._target_spin.setMaximumWidth(64)
        self._target_spin.editingFinished.connect(self._on_target_committed)
        header.addWidget(self._target_spin, 0)

        msg_label = QLabel("msg")
        msg_label.setStyleSheet("color: #888;")
        header.addWidget(msg_label, 0)
        self._msg_id_spin = _NoWheelSpinBox()
        self._msg_id_spin.setRange(0, 0xFFFF)
        self._msg_id_spin.setDisplayIntegerBase(16)
        self._msg_id_spin.setPrefix("0x")
        self._msg_id_spin.setKeyboardTracking(False)
        self._msg_id_spin.setMaximumWidth(82)
        self._msg_id_spin.editingFinished.connect(self._on_msg_id_committed)
        header.addWidget(self._msg_id_spin, 0)
        outer.addLayout(header)

        # ---- Body: the MSG.PAK string. Editable when the msg_id resolves,
        # disabled with a "missing" hint otherwise so the user can still
        # retarget via the msg-id spin.
        self._body = QPlainTextEdit()
        self._body.setMinimumHeight(60)
        self._body.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._body.textChanged.connect(self._on_body_changed)
        outer.addWidget(self._body, 1)

        # Debounced commit so a burst of keystrokes folds to one undo
        # frame (mirrors map_browser's StringEditor pattern).
        self._body_commit_timer = QTimer(self)
        self._body_commit_timer.setSingleShot(True)
        self._body_commit_timer.setInterval(250)
        self._body_commit_timer.timeout.connect(self._commit_body_text)

        # ---- Footer: muted info (entry/offset), helps users locate the
        # block in the overlay5 entry when cross-referencing notes.
        self._info_label = QLabel()
        self._info_label.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info_label, 0)

        self._refresh_from_block(self._dialog_block)

        # Defer the expensive sprite_map model + completer attach so the
        # whole batch of cards lays out in one fast frame. The
        # placeholder-bearing combo from ``_set_portrait_combo`` keeps
        # the speaker name readable in the meantime.
        QTimer.singleShot(0, self._attach_portrait_combo_model)

    # ---- public refresh hook (used by redo/undo on_change wiring) -----

    def refresh(self) -> None:
        """Re-pull the dialog block from the live overlay5 entry bytes.

        Called after an EditDialogFieldCommand or text-edit redo/undo
        applies on this same block. Re-parses the 12-byte DIALOG from
        the freshest entry buffer and re-seeds every widget without
        firing the commit slots. Flushes any in-flight body-edit timer
        first so a header-field commit (msg_id / target / portrait) on
        a card with unsaved typing doesn't blow that typing away when
        we re-seed.
        """
        self.flush_pending()
        try:
            entry_bytes = self._session.overlay5_entry_bytes(self._entry_ix)
            block = overlay5_mod.DialogBlock.from_bytes(
                entry_bytes, self._block_offset,
            )
        except (AttributeError, ValueError, IndexError):
            return
        self._dialog_block = block
        self._refresh_from_block(block)

    # ---- internal seeders --------------------------------------------

    def _refresh_from_block(self, block) -> None:
        self._syncing = True
        try:
            self._set_portrait_combo(int(block.portrait))
            self._target_spin.setValue(int(block.target))
            self._msg_id_spin.setValue(int(block.msg_id))
            self._refresh_portrait_pixmap(int(block.portrait))
            self._refresh_body_from_msg_id(int(block.msg_id))
            self._info_label.setText(
                f"entry {self._entry_ix:04d}  \u00b7  "
                f"@0x{self._block_offset:04x}"
            )
        finally:
            self._syncing = False

    def _set_portrait_combo(self, portrait_id: int) -> None:
        target = int(portrait_id) & 0xFFFF
        combo = self._portrait_combo
        for i in range(combo.count()):
            if combo.itemData(i, Qt.UserRole) == target:
                combo.setCurrentIndex(i)
                return
        # Path taken during initial construction (combo has no model
        # yet) and for ids the sprite_map model doesn't surface. Show
        # the resolved digimon display name so the placeholder reads
        # like a real entry — once the deferred attach completes the
        # combo gets re-selected against the full model, the placeholder
        # is dropped, and the substring search becomes available.
        try:
            name = self._session.digimon_display_name(target)
        except (AttributeError, KeyError, ValueError):
            name = f"0x{target:04x}"
        combo.addItem(name, userData=target)
        combo.setCurrentIndex(combo.count() - 1)

    def _attach_portrait_combo_model(self) -> None:
        """Deferred sprite_map model + completer attach.

        Each ``setEditable(True)`` + ``QCompleter(setModel(sprite_map))``
        costs ~30-40ms (per the editor widget-pool memory). Doing that
        eagerly in ``__init__`` for every dialog card froze the UI for
        hundreds of ms on long chains. Scheduling the attach via
        ``QTimer.singleShot(0, ...)`` lets the whole batch lay out in
        one frame, then this method runs once per card on the next event
        loop tick to upgrade the placeholder combo to the full model +
        completer.

        Guarded by ``RuntimeError`` because the card may have been
        scheduled for deletion (chip flip, chain teardown) between the
        ``singleShot`` enqueue and the tick that fires it — accessing
        a deleted QObject otherwise crashes the host process.
        """
        if self._portrait_combo_fully_attached:
            return
        try:
            combo = self._portrait_combo
            current_id = int(self._dialog_block.portrait) & 0xFFFF
            self._syncing = True
            try:
                combo.setEditable(True)
                combo.setInsertPolicy(QComboBox.NoInsert)
                pmodel = self._session.picker_model("sprite_map")
                if pmodel is not None:
                    combo.setModel(pmodel)
                completer = QCompleter(combo)
                completer.setCaseSensitivity(Qt.CaseInsensitive)
                completer.setFilterMode(Qt.MatchContains)
                completer.setCompletionMode(QCompleter.PopupCompletion)
                completer.setModel(combo.model())
                combo.setCompleter(completer)
                # Re-select against the real model; this drops the
                # placeholder added during _refresh_from_block.
                self._set_portrait_combo(current_id)
            finally:
                self._syncing = False
            self._portrait_combo_fully_attached = True
        except RuntimeError:
            # Card was torn down between enqueue and tick.
            return

    def _refresh_portrait_pixmap(self, portrait_id: int) -> None:
        try:
            icon = self._session.digimon_portrait_icon(int(portrait_id))
        except (AttributeError, KeyError, ValueError):
            icon = None
        if icon is None or icon.isNull():
            self._portrait_label.setPixmap(QPixmap())
            self._portrait_label.setText("?")
            return
        self._portrait_label.setText("")
        self._portrait_label.setPixmap(icon.pixmap(40, 40))

    def _refresh_body_from_msg_id(self, msg_id: int) -> None:
        try:
            gs = self._session.dialog_msg_text(int(msg_id))
        except (AttributeError, ValueError):
            gs = None
        self._bound_gs = gs
        if gs is None:
            with QSignalBlocker(self._body):
                self._body.setPlainText("")
            self._body.setPlaceholderText(
                f"<missing MSG.PAK row 0x{int(msg_id):04x}>"
            )
            self._body.setEnabled(False)
            return
        text = getattr(gs, "text", "") or ""
        with QSignalBlocker(self._body):
            self._body.setPlainText(text.replace("[BR]", "\n"))
        self._body.setEnabled(True)
        self._body.setPlaceholderText("")

    # ---- commit slots ------------------------------------------------

    def _on_portrait_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        value = self._portrait_combo.currentData(Qt.UserRole)
        if value is None:
            return
        new_val = int(value) & 0xFFFF
        if new_val == int(self._dialog_block.portrait):
            return
        self._push_field_command("portrait", new_val)

    def _on_target_committed(self) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_val = int(self._target_spin.value()) & 0xFFFF
        if new_val == int(self._dialog_block.target):
            return
        self._push_field_command("target", new_val)

    def _on_msg_id_committed(self) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_val = int(self._msg_id_spin.value()) & 0xFFFF
        if new_val == int(self._dialog_block.msg_id):
            return
        self._push_field_command("msg_id", new_val)

    def _push_field_command(self, field: str, new_value: int) -> None:
        cmd = EditDialogFieldCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            field,
            new_value,
            description=(
                f"Edit dialog {field} "
                f"(entry {self._entry_ix:04d} @0x{self._block_offset:04x})"
            ),
            on_change=lambda _v: self.refresh(),
        )
        self._undo_stack.push(cmd)

    # ---- body text (MSG.PAK splice) -----------------------------------

    def _on_body_changed(self) -> None:
        if self._syncing or self._bound_gs is None:
            return
        # User typed — restart the debounce timer; commit fires once
        # they pause. Switching cards mid-edit forces a flush via
        # ``flush_pending()``.
        self._body_commit_timer.start()

    def flush_pending(self) -> None:
        """Force any in-flight body-edit to commit immediately.

        Called by the parent before tearing down or switching the
        active chain so a mid-typing edit isn't lost to widget
        teardown.
        """
        if self._body_commit_timer.isActive():
            self._body_commit_timer.stop()
            self._commit_body_text()

    def _commit_body_text(self) -> None:
        if self._bound_gs is None or self._undo_stack is None:
            return
        original = getattr(self._bound_gs, "text", "") or ""
        edited = (
            self._body.toPlainText()
            .replace("\r\n", "[BR]")
            .replace("\r", "[BR]")
            .replace("\n", "[BR]")
        )
        if edited == original:
            return
        msg_id = int(self._dialog_block.msg_id)
        gs = self._bound_gs
        cmd = SetAttrCommand(
            gs, "text", edited,
            description=f"Edit dialog msg 0x{msg_id:04x}",
            on_change=lambda gs=gs: self._on_text_applied(gs),
        )
        self._undo_stack.push(cmd)

    def _on_text_applied(self, gs) -> None:
        """SetAttrCommand on_change — re-seed the body if it still
        points at the same GameString (undo/redo could lag a mid-edit
        switch). Skips if the user has since moved on to another card."""
        if gs is not self._bound_gs:
            return
        new_text = getattr(gs, "text", "") or ""
        with QSignalBlocker(self._body):
            self._body.setPlainText(new_text.replace("[BR]", "\n"))


# ---- shared card styling -------------------------------------------------

_SCENE_CARD_STYLE = (
    "QLabel {{ color: #cccccc; background: transparent; }}"
    "QComboBox {{ color: #cccccc; background: #3c3c3c; }}"
    "QComboBox QAbstractItemView {{ color: #cccccc; background: #2d2d30;"
    " selection-background-color: #094771; selection-color: white; }}"
    "QSpinBox {{ color: #cccccc; background: #3c3c3c; }}"
)


def _card_style_for(class_name: str, alt: bool) -> str:
    bg = "#2f3036" if alt else "#27282d"
    return (
        f"{class_name} {{ background: {bg}; border: 1px solid #1f1f23;"
        " border-radius: 3px; }"
        + _SCENE_CARD_STYLE.format()
    )


class _MusicCard(QFrame):
    """Inline editable card for a ``0e 00 [music_id]`` SET_MUSIC block.

    DORMANT: currently unreachable. The events walker no longer emits
    ``EVENT_KIND_SET_MUSIC`` (``overlay5.SURFACE_SET_MUSIC_EVENTS`` is
    off) because ``0e 00`` isn't reliably a SET_MUSIC opcode — it mostly
    latches onto mid-instruction bytes. Kept intact so re-enabling the
    flag restores the card once ``0e 00`` detection is trustworthy.

    One combo (BGM name) + a muted footer with the entry/offset. The
    combo enumerates every BGM slot the session's SDAT exposes
    (vanilla + user-staged "Add As New Entry" additions) via
    :func:`_bgm_music_choices` so a freshly-added BGM shows up here as
    soon as the chain is re-opened. A sticky raw-hex fallback still
    handles the case where a script references an id past the current
    BGM count.
    """

    def __init__(
        self,
        session,
        undo_stack,
        entry_ix: int,
        music_block,
        alt: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block_offset = int(music_block.block_offset)
        self._music_block = music_block
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_MusicCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        tag = QLabel("♪ SET_MUSIC")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        row.addWidget(tag, 0)

        self._combo = _NoWheelComboBox()
        self._combo.setMaxVisibleItems(24)
        self._combo.setMinimumWidth(240)
        _populate_music_combo(self._combo, self._session)
        self._combo.currentIndexChanged.connect(self._on_committed)
        row.addWidget(self._combo, 1)
        outer.addLayout(row)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)

        self._refresh_from_block(self._music_block)

    def refresh(self) -> None:
        try:
            entry_bytes = self._session.overlay5_entry_bytes(self._entry_ix)
            block = overlay5_mod.SetMusicBlock.from_bytes(
                entry_bytes, self._block_offset,
            )
        except (AttributeError, ValueError, IndexError):
            return
        self._music_block = block
        # Re-enumerate BGM choices from the session so newly-staged
        # additions (or freshly-edited labels) land in the dropdown
        # after undo/redo without waiting for a chain reopen.
        self._syncing = True
        try:
            _populate_music_combo(self._combo, self._session)
        finally:
            self._syncing = False
        self._refresh_from_block(block)

    def flush_pending(self) -> None:
        # SET_MUSIC has no debounced edit surface; kept for the parent's
        # iterating teardown loop.
        return

    def _refresh_from_block(self, block) -> None:
        self._syncing = True
        try:
            mid = int(block.music_id) & 0xFFFF
            ix = self._combo.findData(mid)
            if ix < 0:
                # Preserve the current unnamed id as a sticky entry so
                # the user can see what's set even when it's off-list
                # (e.g. a script referencing a BGM past the current
                # SDAT slot count).
                self._combo.addItem(
                    f"0x{mid:04x} — {self._session.bgm_label(mid)}", mid,
                )
                ix = self._combo.findData(mid)
            self._combo.setCurrentIndex(ix)
        finally:
            self._syncing = False
        self._info.setText(
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
        )

    def _on_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_id = self._combo.currentData()
        if new_id is None:
            return
        new_id = int(new_id) & 0xFFFF
        if new_id == int(self._music_block.music_id) & 0xFFFF:
            return
        cmd = SetMusicIdCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            new_id,
            description=f"Set map BGM to {self._session.bgm_label(new_id)}",
            on_change=lambda _v: self.refresh(),
        )
        self._undo_stack.push(cmd)


class _ReactionCard(QFrame):
    """Inline card for ``C0 00 [reaction] [target]`` — over-head balloon.

    Reaction combo (named ids + editable spin) + target slot spin.
    """

    _REACTION_LABELS = [
        (0, "! (exclaim)"),
        (1, "… (ellipsis)"),
        (2, "💧 waterdrop"),
        (3, "😠 anger"),
    ]

    def __init__(
        self,
        session,
        undo_stack,
        entry_ix: int,
        reaction_block,
        alt: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block_offset = int(reaction_block.block_offset)
        self._reaction_block = reaction_block
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_ReactionCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        tag = QLabel("💬 REACTION")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        row.addWidget(tag, 0)

        self._reaction_combo = _NoWheelComboBox()
        self._reaction_combo.setMinimumWidth(140)
        for rid, label in self._REACTION_LABELS:
            self._reaction_combo.addItem(label, rid)
        self._reaction_combo.currentIndexChanged.connect(
            self._on_reaction_committed,
        )
        row.addWidget(self._reaction_combo, 1)

        row.addWidget(QLabel("over slot"), 0)
        self._target_spin = _NoWheelSpinBox()
        self._target_spin.setRange(0, 0xFFFF)
        self._target_spin.setKeyboardTracking(False)
        self._target_spin.setMaximumWidth(64)
        self._target_spin.editingFinished.connect(
            self._on_target_committed,
        )
        row.addWidget(self._target_spin, 0)
        outer.addLayout(row)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)

        self._refresh_from_block(self._reaction_block)

    def refresh(self) -> None:
        try:
            entry_bytes = self._session.overlay5_entry_bytes(self._entry_ix)
            block = overlay5_mod.ReactionBlock.from_bytes(
                entry_bytes, self._block_offset,
            )
        except (AttributeError, ValueError, IndexError):
            return
        self._reaction_block = block
        self._refresh_from_block(block)

    def flush_pending(self) -> None:
        return

    def _refresh_from_block(self, block) -> None:
        self._syncing = True
        try:
            rid = int(block.reaction) & 0xFFFF
            ix = self._reaction_combo.findData(rid)
            if ix < 0:
                name = overlay5_mod.REACTION_NAMES.get(rid, f"0x{rid:04x}")
                self._reaction_combo.addItem(f"0x{rid:04x} — {name}", rid)
                ix = self._reaction_combo.findData(rid)
            self._reaction_combo.setCurrentIndex(ix)
            self._target_spin.setValue(int(block.target) & 0xFFFF)
        finally:
            self._syncing = False
        self._info.setText(
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
        )

    def _on_reaction_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_val = self._reaction_combo.currentData()
        if new_val is None:
            return
        self._push_field("reaction", int(new_val))

    def _on_target_committed(self) -> None:
        if self._syncing or self._undo_stack is None:
            return
        self._push_field("target", int(self._target_spin.value()))

    def _push_field(self, field: str, new_value: int) -> None:
        current = getattr(self._reaction_block, field) & 0xFFFF
        new_value = int(new_value) & 0xFFFF
        if current == new_value:
            return
        cmd = EditReactionFieldCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            field,
            new_value,
            description=f"Edit reaction {field}",
            on_change=lambda _v: self.refresh(),
        )
        self._undo_stack.push(cmd)


class _BattleCard(QFrame):
    """Inline card for a BATTLE block — five enemy slots + bg + music.

    Enemy combos share the pooled sprite_map model via a deferred
    attach so the card lays out fast even with five combos; bg is a
    raw hex spinbox (BG id table isn't decoded yet); music reuses the
    named-BGM combo the map-level SET_MUSIC card uses.

    The 5th enemy slot uses ``0xFFFF`` for "empty" per the codec; the
    combo shows that as a sticky ``0xFFFF — (empty)`` entry so the user
    can add / clear specific slots directly.
    """

    def __init__(
        self,
        session,
        undo_stack,
        entry_ix: int,
        battle_block,
        alt: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block_offset = int(battle_block.block_offset)
        self._battle_block = battle_block
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_BattleCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QLabel("⚔ BATTLE")
        header.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(header, 0)

        # Enemy rows — one combo per slot 0..4. Combos start minimal
        # (no editable / no completer) and get their sprite_map model
        # attached on a deferred tick, same as _DialogCard's portrait
        # combo. Names come from ``session.digimon_display_name`` so
        # the placeholder is readable before the model attaches.
        self._enemy_combos: List[QComboBox] = []
        for slot_ix in range(overlay5_mod.BATTLE_ENEMY_SLOTS):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(QLabel(f"E{slot_ix + 1}"), 0)
            combo = _NoWheelComboBox()
            combo.setMaxVisibleItems(20)
            combo.setMinimumWidth(220)
            combo.currentIndexChanged.connect(
                lambda _ix, sl=slot_ix: self._on_enemy_committed(sl),
            )
            row.addWidget(combo, 1)
            outer.addLayout(row)
            self._enemy_combos.append(combo)
        self._enemy_combos_fully_attached: bool = False

        # BG + Music row
        params = QHBoxLayout()
        params.setContentsMargins(0, 0, 0, 0)
        params.setSpacing(6)
        params.addWidget(QLabel("bg"), 0)
        self._bg_spin = _NoWheelSpinBox()
        self._bg_spin.setRange(0, 0xFFFF)
        self._bg_spin.setDisplayIntegerBase(16)
        self._bg_spin.setPrefix("0x")
        self._bg_spin.setKeyboardTracking(False)
        self._bg_spin.setMaximumWidth(82)
        self._bg_spin.editingFinished.connect(self._on_bg_committed)
        params.addWidget(self._bg_spin, 0)

        params.addWidget(QLabel("music"), 0)
        self._music_combo = _NoWheelComboBox()
        self._music_combo.setMaxVisibleItems(24)
        self._music_combo.setMinimumWidth(220)
        _populate_music_combo(self._music_combo, self._session)
        self._music_combo.currentIndexChanged.connect(self._on_music_committed)
        params.addWidget(self._music_combo, 1)
        outer.addLayout(params)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)

        # Sprite VRAM budget: scripted battles skip the wild roll's
        # Σ fs ≤ 1440 cap, so an over-budget enemy set crashes on load.
        self._budget_label = QLabel()
        self._budget_label.setWordWrap(True)
        outer.addWidget(self._budget_label, 0)

        self._refresh_from_block(self._battle_block)
        QTimer.singleShot(0, self._attach_enemy_combo_models)

    def refresh(self) -> None:
        block = overlay5_mod._parse_battle_at(
            self._session.overlay5_entry_bytes(self._entry_ix),
            self._block_offset,
        )
        if block is None:
            return
        self._battle_block = block
        # Re-enumerate BGM choices so newly-staged additions land in
        # the battle music dropdown on undo/redo without waiting for
        # a chain reopen. Same rationale as _MusicCard.refresh().
        self._syncing = True
        try:
            _populate_music_combo(self._music_combo, self._session)
        finally:
            self._syncing = False
        self._refresh_from_block(block)

    def flush_pending(self) -> None:
        return

    def _refresh_from_block(self, block) -> None:
        self._syncing = True
        try:
            for slot_ix, combo in enumerate(self._enemy_combos):
                enemy_id = int(block.enemies[slot_ix]) & 0xFFFF
                self._set_enemy_combo(combo, enemy_id)
            self._bg_spin.setValue(int(block.bg_id) & 0xFFFF)
            self._set_music_combo(int(block.music_id) & 0xFFFF)
        finally:
            self._syncing = False
        self._info.setText(
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
            f"  ({block.total_size} bytes)"
        )
        self._refresh_battle_budget(block)

    def _refresh_battle_budget(self, block) -> None:
        """Show the enemy set's summed sprite footprint vs the VRAM budget.

        Each non-empty slot's fs is resolved from the enemy's displayed
        battle sprite (id -> sprite_map.main_sprite -> group footprint).
        Scripted battles bypass the wild roll's Σ fs ≤ 1440 cap, so an
        over-budget set overflows the shared OBJ VRAM pool and crashes on
        load — flag it red. Unresolvable ids are counted as "+N?".
        """
        total = 0
        unknown = 0
        active = 0
        for slot_ix in range(overlay5_mod.BATTLE_ENEMY_SLOTS):
            eid = int(block.enemies[slot_ix]) & 0xFFFF
            if eid == overlay5_mod.BATTLE_ENEMY_EMPTY:
                continue
            active += 1
            fs = self._session.digimon_battle_footprint(eid)
            if fs is None:
                unknown += 1
            else:
                total += fs
        if active == 0:
            self._budget_label.setText("")
            return
        budget = BATTLE_SPRITE_TILE_BUDGET
        over = total > budget
        unk = f" +{unknown}?" if unknown else ""
        color = "#c0392b" if over else "#888"
        tail = " — over VRAM pool, crashes on load" if over else ""
        self._budget_label.setStyleSheet(f"color: {color}; font-size: 10px;")
        self._budget_label.setText(
            f"sprite VRAM: {total}{unk} / {budget} tiles{tail}"
        )

    def _set_enemy_combo(self, combo: QComboBox, enemy_id: int) -> None:
        ix = combo.findData(enemy_id)
        if ix < 0:
            # Placeholder until the pooled model gets attached — resolve
            # the display name via the session so users see the species
            # rather than a raw hex code.
            if enemy_id == overlay5_mod.BATTLE_ENEMY_EMPTY:
                label = f"0x{enemy_id:04x} — (empty)"
            else:
                name = _safe_display_name(self._session, enemy_id)
                label = f"0x{enemy_id:04x} — {name}"
            combo.addItem(label, enemy_id)
            ix = combo.findData(enemy_id)
        combo.setCurrentIndex(ix)

    def _set_music_combo(self, music_id: int) -> None:
        ix = self._music_combo.findData(music_id)
        if ix < 0:
            # Sticky fallback: an id past the current BGM count still
            # gets a row so the user can see what's set. Uses the same
            # session-aware label resolver as the enumerated rows so a
            # user-edited label shows up here too.
            self._music_combo.addItem(
                f"0x{music_id:04x} — {self._session.bgm_label(music_id)}",
                music_id,
            )
            ix = self._music_combo.findData(music_id)
        self._music_combo.setCurrentIndex(ix)

    def _attach_enemy_combo_models(self) -> None:
        if self._enemy_combos_fully_attached:
            return
        try:
            pmodel = self._session.picker_model("sprite_map")
        except (AttributeError, RuntimeError):
            pmodel = None
        if pmodel is None:
            return
        self._syncing = True
        try:
            for slot_ix, combo in enumerate(self._enemy_combos):
                try:
                    combo.setEditable(True)
                    combo.setInsertPolicy(QComboBox.NoInsert)
                    combo.setModel(pmodel)
                    completer = QCompleter(combo)
                    completer.setCaseSensitivity(Qt.CaseInsensitive)
                    completer.setFilterMode(Qt.MatchContains)
                    completer.setCompletionMode(QCompleter.PopupCompletion)
                    completer.setModel(combo.model())
                    combo.setCompleter(completer)
                    self._set_enemy_combo(
                        combo,
                        int(self._battle_block.enemies[slot_ix]) & 0xFFFF,
                    )
                except RuntimeError:
                    return
            self._enemy_combos_fully_attached = True
        finally:
            self._syncing = False

    def _on_enemy_committed(self, slot_ix: int) -> None:
        if self._syncing or self._undo_stack is None:
            return
        combo = self._enemy_combos[slot_ix]
        new_val = combo.currentData()
        if new_val is None:
            return
        new_id = int(new_val) & 0xFFFF
        if new_id == int(self._battle_block.enemies[slot_ix]) & 0xFFFF:
            return
        display = (
            "(empty)"
            if new_id == overlay5_mod.BATTLE_ENEMY_EMPTY
            else _safe_display_name(self._session, new_id)
        )
        cmd = EditBattleEnemyCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            slot_ix,
            new_id,
            description=f"Set battle E{slot_ix + 1} to {display}",
            on_change=lambda _sl, _v: self.refresh(),
        )
        self._undo_stack.push(cmd)

    def _on_bg_committed(self) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_val = int(self._bg_spin.value()) & 0xFFFF
        if new_val == int(self._battle_block.bg_id) & 0xFFFF:
            return
        cmd = EditBattleBgCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            new_val,
            description=f"Set battle BG to 0x{new_val:04x}",
            on_change=lambda _v: self.refresh(),
        )
        self._undo_stack.push(cmd)

    def _on_music_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_val = self._music_combo.currentData()
        if new_val is None:
            return
        new_id = int(new_val) & 0xFFFF
        if new_id == int(self._battle_block.music_id) & 0xFFFF:
            return
        cmd = EditBattleMusicCommand(
            self._session,
            self._entry_ix,
            self._block_offset,
            new_id,
            description=f"Set battle music to {self._session.bgm_label(new_id)}",
            on_change=lambda _v: self.refresh(),
        )
        self._undo_stack.push(cmd)


class _WaitCard(QFrame):
    """Inline card for WAIT_FRAMES (``05 00 [frames]``) — a cutscene pause.

    The dispatcher counts the frame value down each frame before running
    the actor's script again, so this is the pacing knob between beats.
    """

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self._syncing = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_WaitCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        tag = QLabel("⏱ Wait")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        row.addWidget(tag, 0)
        self._spin = _NoWheelSpinBox()
        self._spin.setRange(0, 0xFFFF)
        self._spin.setKeyboardTracking(False)
        self._spin.setSuffix(" frames")
        self._spin.setMaximumWidth(130)
        self._spin.editingFinished.connect(self._on_committed)
        row.addWidget(self._spin, 0)
        row.addStretch(1)
        outer.addLayout(row)
        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.WaitFramesBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        return

    def _refresh_from_block(self, block):
        self._syncing = True
        try:
            self._spin.setValue(int(block.frames) & 0xFFFF)
        finally:
            self._syncing = False
        self._info.setText(
            f"~{int(block.frames) / 60.0:.2f}s @60fps  ·  "
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
        )

    def _on_committed(self):
        if self._syncing or self._undo_stack is None:
            return
        new = int(self._spin.value()) & 0xFFFF
        if new == int(self._block.frames) & 0xFFFF:
            return
        self._undo_stack.push(EditScriptFieldCommand(
            self._session, self._entry_ix, self._block_offset, 2, new,
            overlay5_mod.WAIT_FRAMES_OPCODE,
            description=f"Set wait to {new} frames",
            on_change=lambda _v: self.refresh(),
        ))


class _SpriteAnimCard(QFrame):
    """Inline card for SET_SPRITE_ANIM (``64 00 [sprite] [anim]``).

    ``sprite`` is a scene slot id (same space as DIALOG.target); ``anim``
    directly indexes the target sprite's MCHR_ANM animation list (verified
    against scene data — low ids tend to be 8-way facing, higher ones named
    poses like 0x18 head-nod). The card resolves the slot to its overworld
    sprite so it can show a thumbnail, list the sprite's real animations, and
    play the selected one — the same MCHR playback the overworld editor has,
    trimmed to a preview.
    """

    _THUMB = 52
    _PLAY_FPS = 60   # flatten already encodes per-frame duration → play real-time

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self._slot_labels = slot_labels or {}
        self._referenced_slots = referenced_slots or set()
        self._slot_ow_ids = slot_ow_ids or {}
        self._syncing = False
        self._animations = []          # MchrAnimation list for the current sprite
        self._play_frames = []         # flattened frames of the selected anim
        self._play_ix = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_play_tick)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_SpriteAnimCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        tag = QLabel("\U0001f3ac Sprite anim")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(tag, 0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._thumb = QLabel()
        self._thumb.setFixedSize(self._THUMB, self._THUMB)
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setStyleSheet(
            "QLabel { border: 1px solid #1f1f23; background: #1e1e21; }"
        )
        row.addWidget(self._thumb, 0)

        fields = QVBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(3)
        who_row = QHBoxLayout()
        who_row.setContentsMargins(0, 0, 0, 0)
        who_row.setSpacing(6)
        who_row.addWidget(QLabel("who"), 0)
        self._sprite = _NoWheelComboBox()
        self._sprite.setEditable(True)
        self._sprite.setInsertPolicy(QComboBox.NoInsert)
        self._sprite.setMinimumWidth(150)
        self._sprite.setMaxVisibleItems(20)
        self._sprite.currentIndexChanged.connect(self._on_sprite_changed)
        self._sprite.lineEdit().editingFinished.connect(self._on_sprite_changed)
        who_row.addWidget(self._sprite, 1)
        fields.addLayout(who_row)

        anim_row = QHBoxLayout()
        anim_row.setContentsMargins(0, 0, 0, 0)
        anim_row.setSpacing(6)
        anim_row.addWidget(QLabel("anim"), 0)
        self._anim = _NoWheelComboBox()
        self._anim.setEditable(True)
        self._anim.setInsertPolicy(QComboBox.NoInsert)
        self._anim.setMinimumWidth(150)
        self._anim.setMaxVisibleItems(20)
        self._anim.currentIndexChanged.connect(lambda _i: self._commit("anim", 4))
        self._anim.lineEdit().editingFinished.connect(lambda: self._commit("anim", 4))
        anim_row.addWidget(self._anim, 1)
        self._play_btn = QPushButton("▶")
        self._play_btn.setCheckable(True)
        self._play_btn.setMaximumWidth(32)
        self._play_btn.setToolTip("Play / stop this animation")
        self._play_btn.toggled.connect(self._on_play_toggled)
        anim_row.addWidget(self._play_btn, 0)
        fields.addLayout(anim_row)
        row.addLayout(fields, 1)
        outer.addLayout(row)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    # ---- current-sprite resolution ---------------------------------------

    def _current_ow_id(self) -> Optional[int]:
        slot = _slot_combo_value(self._sprite)
        if slot is None:
            slot = int(self._block.sprite) & 0xFFFF
        return self._slot_ow_ids.get(int(slot))

    def _reload_sprite(self) -> None:
        """Repopulate the anim dropdown + thumbnail for the current 'who'.

        Called on first render and whenever the slot changes. Guarded by
        ``_syncing`` so the anim combo's repopulation doesn't fire a spurious
        commit."""
        self._stop_playback()
        ow_id = self._current_ow_id()
        self._animations = (
            self._session.mchr_animations_for_ow(ow_id) if ow_id is not None else []
        )
        cur_anim = int(self._block.anim) & 0xFFFF
        self._syncing = True
        try:
            self._anim.clear()
            for i, an in enumerate(self._animations):
                n = len(an.frames)
                self._anim.addItem(
                    f"0x{i:02x} — anim {i} ({n}f)", i,
                )
            ix = self._anim.findData(cur_anim)
            if ix < 0:
                # id outside this sprite's animation table — keep it editable
                self._anim.addItem(f"0x{cur_anim:02x}", cur_anim)
                ix = self._anim.findData(cur_anim)
            self._anim.setCurrentIndex(ix)
        finally:
            self._syncing = False
        self._update_thumb_static()
        playable = 0 <= cur_anim < len(self._animations)
        self._play_btn.setEnabled(playable)

    def _update_thumb_static(self) -> None:
        """Show a still frame of the current sprite (first frame of the
        selected animation when resolvable, else the sprite's default)."""
        ow_id = self._current_ow_id()
        if ow_id is None:
            self._thumb.clear()
            return
        anim_id = _slot_combo_value(self._anim)
        frame = None
        if anim_id is not None and 0 <= anim_id < len(self._animations):
            frames = self._animations[anim_id].frames
            if frames:
                frame = int(frames[0].frame)
        pix = self._session.mchr_sprite_pixmap_by_ow_id(
            int(ow_id), max_size=self._THUMB - 4, frame=frame,
        )
        self._thumb.setPixmap(pix) if pix is not None else self._thumb.clear()

    # ---- playback --------------------------------------------------------

    def _on_play_toggled(self, checked: bool) -> None:
        if checked:
            self._start_playback()
        else:
            self._stop_playback()

    def _start_playback(self) -> None:
        anim_id = _slot_combo_value(self._anim)
        if anim_id is None or not (0 <= anim_id < len(self._animations)):
            self._play_btn.setChecked(False)
            return
        from digimon_core import mchr_anm
        # flatten_animation already expands each frame by its duration into one
        # entry per NDS tick, so we advance one entry per FIXED-interval tick
        # (same as the overworld editor) — not a duration-scaled interval, or
        # the per-frame hold gets squared and playback looks frozen.
        self._play_frames = mchr_anm.flatten_animation(self._animations[anim_id])
        if not self._play_frames:
            self._play_btn.setChecked(False)
            return
        self._play_ix = 0
        self._play_btn.setText("■")
        self._paint_play_frame()
        self._timer.start(max(1, 1000 // self._PLAY_FPS))

    def _stop_playback(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
        self._play_btn.blockSignals(True)
        self._play_btn.setChecked(False)
        self._play_btn.setText("▶")
        self._play_btn.blockSignals(False)
        self._update_thumb_static()

    def _on_play_tick(self) -> None:
        if not self._play_frames:
            return
        self._play_ix = (self._play_ix + 1) % len(self._play_frames)
        self._paint_play_frame()

    def _paint_play_frame(self) -> None:
        ow_id = self._current_ow_id()
        if ow_id is None:
            return
        fr = self._play_frames[self._play_ix % len(self._play_frames)]
        pix = self._session.mchr_sprite_pixmap_by_ow_id(
            int(ow_id), max_size=self._THUMB - 4, frame=int(fr.frame),
        )
        if pix is not None:
            self._thumb.setPixmap(pix)

    # ---- edit plumbing ---------------------------------------------------

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.SpriteAnimBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        self._stop_playback()

    def _refresh_from_block(self, block):
        self._syncing = True
        try:
            _populate_slot_combo(
                self._sprite, self._slot_labels, int(block.sprite) & 0xFFFF,
                self._referenced_slots,
            )
        finally:
            self._syncing = False
        self._reload_sprite()
        n_anim = len(self._animations)
        avail = f"  ·  {n_anim} anim" if n_anim else "  ·  no MCHR anim"
        self._info.setText(
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}{avail}"
        )

    def _on_sprite_changed(self, *_a) -> None:
        # The slot drives both the committed value and which sprite's
        # animations/thumbnail we show, so commit then reload for the new who.
        self._commit("sprite", 2)
        if not self._syncing:
            self._reload_sprite()

    def _commit(self, field, field_off):
        if self._syncing or self._undo_stack is None:
            return
        cur = int(getattr(self._block, field)) & 0xFFFF
        if field == "sprite":
            new = _slot_combo_value(self._sprite)
        else:
            new = _slot_combo_value(self._anim)
            self._update_thumb_static()
        if new is None or new == cur:
            return
        self._undo_stack.push(EditScriptFieldCommand(
            self._session, self._entry_ix, self._block_offset, field_off, new,
            overlay5_mod.SPRITE_ANIM_OPCODE,
            description=f"Set sprite {field}",
            on_change=lambda _v: self.refresh(),
        ))


class _CameraCard(QFrame):
    """Inline card for a camera pan — CAMERA_PAN_TO_TARGET (``40 00 tgt
    speed``) or CAMERA_PAN_TO_XY (``c2 00 x y speed``). ``speed`` 0xFFFF
    resets the camera."""

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self._is_xy = bool(block.is_xy)
        self._opcode = int(block.opcode)
        self._slot_labels = slot_labels or {}
        self._referenced_slots = referenced_slots or set()
        self._syncing = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_CameraCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        tag = QLabel(
            "\U0001f3a5 Camera pan → point" if self._is_xy
            else "\U0001f3a5 Camera pan → target"
        )
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(tag, 0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        # a = target slot (0x40, a "who" picker) or x-coord (0xC2, spinbox);
        # b = y-coord (0xC2 only).
        if self._is_xy:
            self._a = _NoWheelSpinBox()
            self._a.setRange(0, 0xFFFF)
            self._a.setKeyboardTracking(False)
            self._a.setMaximumWidth(80)
            self._a.editingFinished.connect(lambda: self._commit("a", 2))
            row.addWidget(QLabel("x"), 0)
            row.addWidget(self._a, 0)
            row.addWidget(QLabel("y"), 0)
            self._b = _NoWheelSpinBox()
            self._b.setRange(0, 0xFFFF)
            self._b.setKeyboardTracking(False)
            self._b.setMaximumWidth(80)
            self._b.editingFinished.connect(lambda: self._commit("b", 4))
            row.addWidget(self._b, 0)
            speed_off = 6
        else:
            row.addWidget(QLabel("who"), 0)
            self._a = _NoWheelComboBox()
            self._a.setEditable(True)
            self._a.setInsertPolicy(QComboBox.NoInsert)
            self._a.setMinimumWidth(150)
            self._a.setMaxVisibleItems(20)
            self._a.currentIndexChanged.connect(
                lambda _i: self._commit("a", 2)
            )
            self._a.lineEdit().editingFinished.connect(
                lambda: self._commit("a", 2)
            )
            row.addWidget(self._a, 1)
            self._b = None
            speed_off = 4
        row.addWidget(QLabel("speed"), 0)
        self._speed = _NoWheelSpinBox()
        self._speed.setRange(0, 0xFFFF)
        self._speed.setKeyboardTracking(False)
        self._speed.setMaximumWidth(80)
        self._speed.editingFinished.connect(
            lambda: self._commit("speed", speed_off)
        )
        row.addWidget(self._speed, 0)
        row.addStretch(1)
        outer.addLayout(row)
        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.CameraBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        return

    def _refresh_from_block(self, block):
        self._syncing = True
        try:
            if self._is_xy:
                self._a.setValue(int(block.a) & 0xFFFF)
                self._b.setValue(int(block.b) & 0xFFFF)
            else:
                _populate_slot_combo(
                    self._a, self._slot_labels, int(block.a) & 0xFFFF,
                    self._referenced_slots,
                )
            self._speed.setValue(int(block.speed) & 0xFFFF)
        finally:
            self._syncing = False
        reset = "  ·  speed 0xFFFF = reset camera" if int(block.speed) == 0xFFFF else ""
        self._info.setText(
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}{reset}"
        )

    def _commit(self, field, field_off):
        if self._syncing or self._undo_stack is None:
            return
        cur = int(getattr(self._block, field)) & 0xFFFF
        if field == "a" and not self._is_xy:
            new = _slot_combo_value(self._a)
            if new is None:
                return
        else:
            widget = {"a": self._a, "b": self._b, "speed": self._speed}[field]
            if widget is None:
                return
            new = int(widget.value()) & 0xFFFF
        if new == cur:
            return
        self._undo_stack.push(EditScriptFieldCommand(
            self._session, self._entry_ix, self._block_offset, field_off, new,
            self._opcode,
            description="Edit camera pan",
            on_change=lambda _v: self.refresh(),
        ))


class _ItemEventCard(QFrame):
    """Inline card for GIVE_ITEM (``b4 00 [item]``) / REMOVE_ITEM
    (``f1 00 [item]``) — an item handed to or taken from the player mid-
    scene (e.g. a quest turn-in)."""

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self._opcode = int(block.opcode)
        self._is_remove = bool(block.is_remove)
        self._syncing = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_ItemEventCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        tag = QLabel("\U0001f381 Remove item" if self._is_remove else "\U0001f381 Add item")  # 🎁
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        row.addWidget(tag, 0)
        self._combo = _NoWheelComboBox()
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.NoInsert)
        self._combo.setMaxVisibleItems(20)
        self._combo.setMinimumContentsLength(16)
        for iid, label in _item_choices():
            self._combo.addItem(label, iid)
        comp = QCompleter(self._combo)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setModel(self._combo.model())
        self._combo.setCompleter(comp)
        self._combo.currentIndexChanged.connect(self._on_committed)
        row.addWidget(self._combo, 1)
        outer.addLayout(row)
        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.ItemBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        return

    def _refresh_from_block(self, block):
        self._syncing = True
        try:
            iid = int(block.item) & 0xFFFF
            ix = self._combo.findData(iid)
            if ix < 0:
                self._combo.addItem(f"(unknown 0x{iid:02x})", iid)
                ix = self._combo.findData(iid)
            self._combo.setCurrentIndex(ix)
        finally:
            self._syncing = False
        verb = "removed from" if self._is_remove else "added to"
        self._info.setText(
            f"{verb} bag  ·  entry {self._entry_ix:04d}"
            f" + 0x{self._block_offset:04x}"
        )

    def _on_committed(self, _ix=-1):
        if self._syncing or self._undo_stack is None:
            return
        val = self._combo.currentData()
        if val is None:
            return
        new = int(val) & 0xFFFF
        if new == int(self._block.item) & 0xFFFF:
            return
        verb = "Remove" if self._is_remove else "Give"
        self._undo_stack.push(EditScriptFieldCommand(
            self._session, self._entry_ix, self._block_offset, 2, new,
            self._opcode,
            description=f"{verb} item → {_item_display_name(new)}",
            on_change=lambda _v: self.refresh(),
        ))


class _MoveCard(QFrame):
    """Inline card for MOVE_BEGIN (``3C 00 [tgt] [type] [x] [y] [speed]``) —
    start walking a sprite to a destination.

    ``who`` (tgt) is the moved slot; ``x``/``y`` are the destination position
    for the common walk type (``0xFFFF`` = leave that axis unchanged);
    ``speed`` ``0xFFFF`` snaps instantly. ``type`` is an undecoded mode enum,
    exposed raw so it round-trips without pretending to know its meaning.
    """

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self._slot_labels = slot_labels or {}
        self._referenced_slots = referenced_slots or set()
        self._syncing = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_MoveCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        tag = QLabel("➡️ Move")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(tag, 0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(QLabel("who"), 0)
        self._who = _NoWheelComboBox()
        self._who.setEditable(True)
        self._who.setInsertPolicy(QComboBox.NoInsert)
        self._who.setMinimumWidth(140)
        self._who.setMaxVisibleItems(20)
        self._who.currentIndexChanged.connect(lambda _i: self._commit("tgt", 2))
        self._who.lineEdit().editingFinished.connect(lambda: self._commit("tgt", 2))
        row.addWidget(self._who, 1)
        outer.addLayout(row)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)
        self._x = self._coord_spin()
        self._y = self._coord_spin()
        self._speed = self._coord_spin()
        self._type = _NoWheelSpinBox()
        self._type.setRange(0, 0xFFFF)
        self._type.setKeyboardTracking(False)
        self._type.setDisplayIntegerBase(16)
        self._type.setPrefix("0x")
        self._type.setMaximumWidth(70)
        self._type.editingFinished.connect(lambda: self._commit("move_type", 4))
        for lbl, w in (("x", self._x), ("y", self._y),
                       ("speed", self._speed), ("type", self._type)):
            row2.addWidget(QLabel(lbl), 0)
            row2.addWidget(w, 0)
        self._x.editingFinished.connect(lambda: self._commit("x", 6))
        self._y.editingFinished.connect(lambda: self._commit("y", 8))
        self._speed.editingFinished.connect(lambda: self._commit("speed", 10))
        row2.addStretch(1)
        outer.addLayout(row2)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    @staticmethod
    def _coord_spin():
        sp = _NoWheelSpinBox()
        sp.setRange(0, 0xFFFF)
        sp.setKeyboardTracking(False)
        sp.setMaximumWidth(70)
        return sp

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.MoveBeginBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        return

    def _refresh_from_block(self, block):
        self._syncing = True
        try:
            _populate_slot_combo(
                self._who, self._slot_labels, int(block.tgt) & 0xFFFF,
                self._referenced_slots,
            )
            self._x.setValue(int(block.x) & 0xFFFF)
            self._y.setValue(int(block.y) & 0xFFFF)
            self._speed.setValue(int(block.speed) & 0xFFFF)
            self._type.setValue(int(block.move_type) & 0xFFFF)
        finally:
            self._syncing = False
        dest = "current" if block.x == 0xFFFF and block.y == 0xFFFF else f"({block.x}, {block.y})"
        snap = "  ·  speed 0xFFFF = snap" if int(block.speed) == 0xFFFF else ""
        self._info.setText(
            f"walk to {dest}  ·  0xFFFF x/y = keep axis{snap}"
            f"  ·  entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
        )

    def _commit(self, field, field_off):
        if self._syncing or self._undo_stack is None:
            return
        cur = int(getattr(self._block, field)) & 0xFFFF
        if field == "tgt":
            new = _slot_combo_value(self._who)
        else:
            widget = {"move_type": self._type, "x": self._x,
                      "y": self._y, "speed": self._speed}[field]
            new = int(widget.value()) & 0xFFFF
        if new is None or new == cur:
            return
        self._undo_stack.push(EditScriptFieldCommand(
            self._session, self._entry_ix, self._block_offset, field_off, new,
            overlay5_mod.MOVE_BEGIN_OPCODE,
            description=f"Edit move {field}",
            on_change=lambda _v: self.refresh(),
        ))


_CONTROL_ICONS = {
    "branch": "❓",       # ❓  condition / if
    "flag": "\U0001f516",     # 🔖  story-flag bookmark
    "set_var": "\U0001f522",  # 🔢  set variable
    "call_if": "❓",
}
_CONTROL_TITLES = {
    "branch": "If", "flag": "Story flag", "set_var": "Set var",
    "call_if": "Conditional call",
}


class _ControlCard(QFrame):
    """Read-only view of a flag/branch control opcode — the gating that
    decides when a scene runs and what story state it sets (CMP_BRANCH,
    SET_VAR, FLAG_SET_OR_CLEAR, IFEQ, CALL_IF_EQ).

    Not editable yet (by request — view only). Shows the decoded condition
    plus the raw bytes + offset so it's traceable back to the script dump.
    """

    def __init__(self, session, undo_stack, entry_ix, block, alt=False, parent=None, slot_labels=None, referenced_slots=None, slot_ow_ids=None):
        super().__init__(parent)
        self._session = session
        self._entry_ix = int(entry_ix)
        self._block = block
        self._block_offset = int(block.block_offset)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_ControlCard", alt))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)
        cat = block.category
        tag = QLabel(f"{_CONTROL_ICONS.get(cat, '⑂')} {_CONTROL_TITLES.get(cat, cat)}"
                     "   ·   read-only")
        tag.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(tag, 0)
        self._desc = QLabel()
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet("color: #dcdcaa;")
        outer.addWidget(self._desc, 0)
        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)
        self._refresh_from_block(block)

    def refresh(self):
        try:
            eb = self._session.overlay5_entry_bytes(self._entry_ix)
            self._block = overlay5_mod.ControlBlock.from_bytes(
                eb, self._block_offset,
            )
        except (ValueError, IndexError):
            return
        self._refresh_from_block(self._block)

    def flush_pending(self):
        return

    def _refresh_from_block(self, block):
        self._desc.setText(block.describe())
        size = overlay5_mod.CONTROL_OPCODE_SIZES.get(block.opcode, 2)
        try:
            raw = self._session.overlay5_entry_bytes(self._entry_ix)[
                self._block_offset:self._block_offset + size
            ]
            raw_hex = " ".join(f"{b:02x}" for b in raw)
        except (ValueError, IndexError):
            raw_hex = ""
        self._info.setText(
            f"op 0x{block.opcode:02x}  ·  {raw_hex}  ·  "
            f"entry {self._entry_ix:04d} + 0x{self._block_offset:04x}"
        )


class _SpriteEditorCard(QFrame):
    """Inline editable card for the selected OVERWORLD_SPRITE placement.

    Replaces the former top-right ``_form_stack`` — instead of a
    dedicated form pane, we surface the same three fields (sprite id,
    x, y) as a card that pins to the top of the Details panel whenever
    a sprite object is active. Semantics match the old form:

    * Sprite ID combo — shares the ``mchr`` pooled model, undo command
      is :class:`EditOverworldSpriteIdCommand`.
    * X / Y spinboxes — u16 pixel coords, undo command is
      :class:`MoveOverworldSpriteCommand`.
    * Info footer — resolved entry + offset (in-entry byte position of
      the 0x0150 block being edited).

    The card is bound to a canvas offset + resolved (entry_ix, block_offset)
    at construction; the tab calls :meth:`sync_state` after undo/redo
    so redo/undo of the underlying command updates the widgets without
    firing their own commit slots.
    """

    def __init__(
        self,
        session,
        undo_stack,
        canvas_offset: int,
        real_entry_ix: int,
        real_offset: int,
        sprite_id: int,
        x: int,
        y: int,
        on_id_commit,
        on_xy_commit,
        alt: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._canvas_offset = int(canvas_offset)
        self._real_entry_ix = int(real_entry_ix)
        self._real_offset = int(real_offset)
        self._current_sprite_id = int(sprite_id) & 0xFFFF
        self._current_xy = (int(x) & 0xFFFF, int(y) & 0xFFFF)
        self._on_id_commit = on_id_commit
        self._on_xy_commit = on_xy_commit
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_SpriteEditorCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QLabel("👤 Overworld sprite")
        header.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(header, 0)

        id_row = QHBoxLayout()
        id_row.setContentsMargins(0, 0, 0, 0)
        id_row.setSpacing(6)
        id_row.addWidget(QLabel("Sprite ID"), 0)
        self._id_combo = _NoWheelComboBox()
        self._id_combo.setEditable(True)
        self._id_combo.setInsertPolicy(QComboBox.NoInsert)
        self._id_combo.setMaxVisibleItems(20)
        self._id_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon,
        )
        self._id_combo.setMinimumContentsLength(14)
        # ow-id space (UserRole == overworld_sprite_id), not CHR index —
        # matches how the placement is stored + rendered.
        mchr_model = self._session.picker_model("mchr_ow")
        if mchr_model is not None:
            self._id_combo.setModel(mchr_model)
        id_completer = QCompleter(self._id_combo)
        id_completer.setCaseSensitivity(Qt.CaseInsensitive)
        id_completer.setFilterMode(Qt.MatchContains)
        id_completer.setCompletionMode(QCompleter.PopupCompletion)
        id_completer.setModel(self._id_combo.model())
        self._id_combo.setCompleter(id_completer)
        self._id_combo.currentIndexChanged.connect(self._on_id_committed)
        id_row.addWidget(self._id_combo, 1)
        outer.addLayout(id_row)

        xy_row = QHBoxLayout()
        xy_row.setContentsMargins(0, 0, 0, 0)
        xy_row.setSpacing(6)
        xy_row.addWidget(QLabel("X"), 0)
        self._x_spin = _NoWheelSpinBox()
        self._x_spin.setRange(0, 0xFFFF)
        self._x_spin.setKeyboardTracking(False)
        self._x_spin.setMaximumWidth(90)
        self._x_spin.editingFinished.connect(self._on_xy_committed)
        xy_row.addWidget(self._x_spin, 0)
        xy_row.addWidget(QLabel("Y"), 0)
        self._y_spin = _NoWheelSpinBox()
        self._y_spin.setRange(0, 0xFFFF)
        self._y_spin.setKeyboardTracking(False)
        self._y_spin.setMaximumWidth(90)
        self._y_spin.editingFinished.connect(self._on_xy_committed)
        xy_row.addWidget(self._y_spin, 0)
        xy_row.addStretch(1)
        outer.addLayout(xy_row)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        outer.addWidget(self._info, 0)

        self.sync_state(self._current_sprite_id, *self._current_xy)

    # ---- public interface (matches the other event cards) ----------------

    def refresh(self) -> None:
        # No underlying block re-parse — the tab's callbacks push
        # ``sync_state`` explicitly after redo/undo lands. Kept as a
        # no-op so ``_clear_detail_layout``'s iteration works uniformly.
        return

    def flush_pending(self) -> None:
        # editingFinished / currentIndexChanged commit immediately;
        # no debounce timer to drain.
        return

    def canvas_offset(self) -> int:
        return self._canvas_offset

    def sync_state(self, sprite_id: int, x: int, y: int) -> None:
        """Update the widgets from a fresh spec without firing commits.

        Called after an undo/redo callback lands from the tab so the
        card stays in lockstep with the canvas marker and the ROM
        bytes. The ``_syncing`` guard early-outs the commit slots.
        """
        sprite_id = int(sprite_id) & 0xFFFF
        x = int(x) & 0xFFFF
        y = int(y) & 0xFFFF
        self._current_sprite_id = sprite_id
        self._current_xy = (x, y)
        self._syncing = True
        try:
            self._set_sprite_id_combo(sprite_id)
            self._x_spin.setValue(x)
            self._y_spin.setValue(y)
        finally:
            self._syncing = False
        self._info.setText(
            f"entry {self._real_entry_ix:04d}  ·  @0x{self._real_offset:04x}"
        )

    # ---- combo binding ---------------------------------------------------

    def _set_sprite_id_combo(self, sprite_id: int) -> None:
        combo = self._id_combo
        target = int(sprite_id) & 0xFFFF
        for i in range(combo.count()):
            if combo.itemData(i, Qt.UserRole) == target:
                combo.setCurrentIndex(i)
                return
        combo.addItem(f"(undefined 0x{target:04x})", userData=target)
        combo.setCurrentIndex(combo.count() - 1)

    # ---- commit slots ----------------------------------------------------

    def _on_id_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        value = self._id_combo.currentData(Qt.UserRole)
        if value is None:
            return
        new_sid = int(value) & 0xFFFF
        if new_sid == self._current_sprite_id:
            return
        self._on_id_commit(
            self._canvas_offset, self._real_entry_ix, self._real_offset, new_sid,
        )

    def _on_xy_committed(self) -> None:
        if self._syncing or self._undo_stack is None:
            return
        new_x = int(self._x_spin.value()) & 0xFFFF
        new_y = int(self._y_spin.value()) & 0xFFFF
        if (new_x, new_y) == self._current_xy:
            return
        self._on_xy_commit(
            self._canvas_offset, self._real_entry_ix, self._real_offset,
            new_x, new_y,
        )


class _ExitDestinationCard(QFrame):
    """Inline editable card for a map-exit's warp destination.

    Surfaces the exit handler's CALL_SCRIPT_AT_OFFSET target (op 0x0030
    u32) as a ``warp to map N (Area)`` dropdown so the user can repoint an
    exit without leaving the Cutscenes panel. Mirrors
    :class:`_SpriteEditorCard`: bound to a handler location at
    construction, re-synced via :meth:`sync_state` after undo/redo lands.

    Shared-handler caveat (same as :class:`EditExitDestinationCommand`):
    several 0x001b zones can point at one handler, so an edit here moves
    every exit that shares it — the tab repaints all of them together.
    """

    def __init__(
        self,
        session,
        undo_stack,
        block_offset: int,
        handler_entry_ix: int,
        handler_rel_offset: int,
        current_dest: int,
        choices: List[Tuple[int, str]],
        on_dest_commit,
        alt: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._block_offset = int(block_offset)
        self._handler_entry_ix = int(handler_entry_ix)
        self._handler_rel_offset = int(handler_rel_offset)
        self._current_dest = int(current_dest) & 0xFFFFFFFF
        self._on_dest_commit = on_dest_commit
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_ExitDestinationCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QLabel("\U0001f6aa Exit destination")
        header.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(header, 0)

        self._combo = _NoWheelComboBox()
        self._combo.setMaxVisibleItems(24)
        self._combo.setMinimumWidth(240)
        self._combo.addItem(
            "Custom (raw offset) — leave as-is", _EXIT_DEST_CUSTOM,
        )
        for file_off, label in choices:
            self._combo.addItem(label, int(file_off))
        self._combo.currentIndexChanged.connect(self._on_committed)
        outer.addWidget(self._combo, 0)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        self._info.setWordWrap(True)
        outer.addWidget(self._info, 0)

        self.sync_state(self._current_dest)

    # ---- public interface (matches the other event cards) ----------------

    def refresh(self) -> None:
        # No block re-parse — the tab pushes ``sync_state`` after redo/undo.
        return

    def flush_pending(self) -> None:
        # currentIndexChanged commits immediately; no debounce to drain.
        return

    def block_offset(self) -> int:
        return self._block_offset

    def sync_state(self, dest_file_off: int) -> None:
        """Re-select the combo from a fresh dest without firing a commit."""
        dest = int(dest_file_off) & 0xFFFFFFFF
        self._current_dest = dest
        self._syncing = True
        try:
            ix = self._combo.findData(dest)
            if ix < 0:
                # Off-list destination (a raw offset that doesn't start a
                # known field-map entry). Park the Custom row on it so the
                # user still sees what's set.
                self._combo.setItemText(0, f"Custom (raw 0x{dest:08x})")
                self._combo.setCurrentIndex(0)
            else:
                self._combo.setItemText(
                    0, "Custom (raw offset) — leave as-is",
                )
                self._combo.setCurrentIndex(ix)
        finally:
            self._syncing = False
        self._info.setText(
            f"handler entry {self._handler_entry_ix:04d}"
            f"  ·  +0x{self._handler_rel_offset:04x}"
            f"  ·  dest 0x{dest:08x}"
        )

    def _on_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        data = self._combo.currentData()
        if data is None:
            return
        data = int(data)
        if data == _EXIT_DEST_CUSTOM:
            return
        if (data & 0xFFFFFFFF) == self._current_dest:
            return
        self._on_dest_commit(
            self._block_offset, self._handler_entry_ix,
            self._handler_rel_offset, data,
        )


class _ChestCard(QFrame):
    """Inline editable card for an overworld chest's given item.

    A chest is an OVERWORLD_SPRITE (id 0x02fb) whose interaction script
    sets ARG_1 (SET_VAR var 0x0005) to an item id then ``CALL_SYS 0x126a``.
    This card edits that one item u16 in place via
    :class:`EditChestItemCommand`; the opened-flag (which also drives the
    load-time opened/closed frame via the 0x0006 pre-check) and the other
    routine args are shown read-only, since editing the flag safely needs
    both sites rewritten together.
    """

    def __init__(
        self,
        session,
        undo_stack,
        chest: "_ChestInfo",
        choices: List[Tuple[int, str]],
        on_item_commit,
        alt: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._undo_stack = undo_stack
        self._chest = chest
        self._current_item = int(chest.item_id) & 0xFFFF
        self._on_item_commit = on_item_commit
        self._syncing: bool = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(_card_style_for("_ChestCard", alt))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QLabel("\U0001f381 Chest")
        header.setStyleSheet("color: #cccccc; font-weight: bold;")
        outer.addWidget(header, 0)

        item_row = QHBoxLayout()
        item_row.setContentsMargins(0, 0, 0, 0)
        item_row.setSpacing(6)
        item_row.addWidget(QLabel("Item"), 0)
        self._item_combo = _NoWheelComboBox()
        self._item_combo.setEditable(True)
        self._item_combo.setInsertPolicy(QComboBox.NoInsert)
        self._item_combo.setMaxVisibleItems(20)
        self._item_combo.setMinimumContentsLength(16)
        for iid, label in choices:
            self._item_combo.addItem(label, iid)
        completer = QCompleter(self._item_combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setModel(self._item_combo.model())
        self._item_combo.setCompleter(completer)
        self._item_combo.currentIndexChanged.connect(self._on_committed)
        item_row.addWidget(self._item_combo, 1)
        outer.addLayout(item_row)

        self._info = QLabel()
        self._info.setStyleSheet("color: #888; font-size: 10px;")
        self._info.setWordWrap(True)
        outer.addWidget(self._info, 0)

        self.sync_state(self._current_item)

    # ---- public interface (matches the other event cards) ----------------

    def refresh(self) -> None:
        return

    def flush_pending(self) -> None:
        return

    def sync_state(self, item_id: int) -> None:
        """Re-select the item combo from a fresh id without firing a commit."""
        item_id = int(item_id) & 0xFFFF
        self._current_item = item_id
        self._syncing = True
        try:
            ix = self._item_combo.findData(item_id)
            if ix < 0:
                self._item_combo.addItem(
                    f"(unknown 0x{item_id:02x})", item_id,
                )
                ix = self._item_combo.findData(item_id)
            self._item_combo.setCurrentIndex(ix)
        finally:
            self._syncing = False
        self._info.setText(
            f"opened flag 0x{self._chest.flag:04x} "
            f"(read-only — also drives the closed/opened frame)  ·  "
            f"args 0x{self._chest.arg2:x}/0x{self._chest.arg3:x}  ·  "
            f"give routine 0x{overlay5_mod.CHEST_GIVE_ROUTINE:04x}  ·  "
            f"entry {self._chest.script_entry_ix:04d}"
            f" @0x{self._chest.item_value_offset:04x}"
        )

    def _on_committed(self, _ix: int = -1) -> None:
        if self._syncing or self._undo_stack is None:
            return
        value = self._item_combo.currentData()
        if value is None:
            return
        new_item = int(value) & 0xFFFF
        if new_item == self._current_item:
            return
        self._on_item_commit(
            self._chest.script_entry_ix,
            self._chest.item_value_offset,
            new_item,
        )


class CutscenesTab(QWidget):
    """Read-only browser tab for a map's cutscene chains.

    Owns no model state — pulls ``chains_for_map`` from the session's
    :class:`CutsceneIndex` each time :meth:`set_map` is called. The chip
    row + detail panel are rebuilt on every map change; the cost is in
    the noise (a 47-chip outlier still rebuilds in <1 ms).
    """

    def __init__(
        self,
        session,
        parent: Optional[QWidget] = None,
        undo_stack=None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        # When wired, canvas marker drags push a MoveOverworldSpriteCommand
        # through this stack so the move is undoable + persists into the
        # ROM bytes. ``None`` keeps the canvas read-only (drag disabled).
        self._undo_stack = undo_stack
        self._state: Optional[_MapState] = None
        # Sparse list parallel to ``_state.chains``: ``_chips[i]`` holds
        # the chip for chain i when one was built, ``None`` when the
        # chain was filtered into the Objects list (OWS/EXIT/HITBOX).
        self._chips: List[Optional[_ChainChip]] = []
        self._base_chip: Optional[_ChainChip] = None
        # Cached row icons for the Objects list. Lazily built the first
        # time the list is populated.
        self._icon_exit: Optional[QIcon] = None
        self._icon_hitbox: Optional[QIcon] = None
        self._icon_spawn: Optional[QIcon] = None
        self._icon_sprite_fallback: Optional[QIcon] = None
        # Currently-pinned exit-destination editor card (top of Details,
        # position 0) plus a lazy cache of the ROM-static destination
        # choices its combo lists. Mutually exclusive with the sprite
        # editor card — an object selection is one or the other.
        self._exit_editor_card: Optional[_ExitDestinationCard] = None
        self._exit_dest_choices_cache: Optional[List[Tuple[int, str]]] = None
        # Chest give-item card, pinned above the sprite editor card when a
        # chest sprite (0x02fb give-item) is selected. Item choices cached
        # (static item-name table).
        self._chest_editor_card: Optional[_ChestCard] = None
        self._item_choices_cache: Optional[List[Tuple[int, str]]] = None
        # slot index → character name for the currently-rendered chain, so
        # the sprite-anim / camera cards can offer a "who" picker instead of
        # a bare slot number. Rebuilt per chain render.
        self._slot_labels: Dict[int, str] = {}
        # Slots the currently-rendered chain's opcodes actually drive (anim /
        # camera / dialog targets), so the "who" combos can rank the scene's
        # real cast above the other map-declared sprites. Rebuilt per render.
        self._referenced_slots: set = set()
        # slot → overworld-sprite id for this chain (base placements, with the
        # chain's own extras overriding), so the sprite-anim card can show the
        # right sprite thumbnail + its animation list. Rebuilt per render.
        self._slot_ow_ids: Dict[int, int] = {}
        # Chain indices for EXT ('cross-script') chains that are really just
        # warps (no editable events — the whole body hops into another map's
        # init). They duplicate this map's Exit objects, so they're filtered
        # out of the chip row. Recomputed per map render.
        self._hidden_ext_ixs: set = set()
        # Single flag that brackets every programmatic selection nudge
        # (canvas → list, list → canvas, chip → list-clear). The
        # itemSelectionChanged / markerSelected / exitSelected handlers
        # all early-out while this is set so the round-trip can't loop.
        self._selection_syncing = False
        # Cross-map cutscene Index: "map" (chip+canvas) vs "index" (flat scene
        # list). Both edit through the shared right-hand detail panel. The
        # tree is built once, lazily, on first entry to index mode. The
        # navigate callback is wired by the map browser so a scene click can
        # flip the map cursor + select the chain.
        self._mode = "map"
        self._scene_index_built = False
        self._scene_rows: List[_SceneRow] = []
        self._navigate_scene_cb = None
        self._reload_map_cb = None
        self._selecting_scene_row = False
        # True while the detail panel shows a standalone shared/non-field
        # scene (``_load_shared_scene``): its ``_state`` isn't the map
        # browser's map, so toggling back to Map view must reload.
        self._showing_shared = False
        # OWS placement block_offsets hidden from the canvas via the Objects
        # list's per-condition-group checkboxes. Reset per map.
        self._hidden_sprite_offsets: set = set()
        # Objects-list group keys the user has collapsed (header double-click).
        self._collapsed_groups: set = set()
        self._build_ui()
        self._show_empty_state()

    # ---- cross-map cutscene Index ----------------------------------------

    def set_navigate_cb(self, cb) -> None:
        """Wire the map browser's ``navigate_to_cutscene_chain(map_id,
        global_ix)`` so an Index row click can jump to that scene's map and
        select the chain (rendering its editable detail)."""
        self._navigate_scene_cb = cb

    def set_reload_cb(self, cb) -> None:
        """Wire the map browser's ``_refresh_cutscenes_tab`` so toggling from a
        standalone shared scene back to Map view can restore the real map."""
        self._reload_map_cb = cb

    def _build_scene_index_view(self) -> QWidget:
        """Left-column widget for Index mode: a filter box + a map→scene
        tree. Populated lazily (:meth:`_populate_scene_index`)."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._scene_filter = QLineEdit()
        self._scene_filter.setPlaceholderText("Filter scenes — map, kind, speaker…")
        self._scene_filter.setClearButtonEnabled(True)
        self._scene_filter.textChanged.connect(self._apply_scene_filter)
        self._scene_filter.setStyleSheet(
            "QLineEdit { background: #3c3c3c; color: #ddd; border: 1px solid "
            "#1f1f23; padding: 3px 6px; margin: 4px; }"
        )
        lay.addWidget(self._scene_filter, 0)
        self._scene_tree = QTreeWidget()
        self._scene_tree.setHeaderHidden(True)
        self._scene_tree.setUniformRowHeights(True)
        self._scene_tree.setStyleSheet(
            "QTreeWidget { background: #252526; color: #cccccc; border: none; }"
            "QTreeWidget::item { padding: 2px 2px; }"
            "QTreeWidget::item:selected { background: #094771; color: white; }"
        )
        self._scene_tree.itemSelectionChanged.connect(self._on_scene_row_selected)
        lay.addWidget(self._scene_tree, 1)
        self._scene_index_note = QLabel()
        self._scene_index_note.setStyleSheet("color: #888; padding: 2px 6px; font-size: 10px;")
        self._scene_index_note.setWordWrap(True)
        lay.addWidget(self._scene_index_note, 0)
        return w

    def _set_mode(self, mode: str) -> None:
        """Swap the left column between the map browser and the scene Index.

        The detail panel is shared, so switching modes never disturbs an
        in-progress edit. Entering index mode builds the tree once and
        highlights the scene matching the current detail selection."""
        self._mode = mode
        self._map_mode_btn.setChecked(mode == "map")
        self._index_mode_btn.setChecked(mode == "index")
        self._left_stack.setCurrentIndex(1 if mode == "index" else 0)
        if mode == "map":
            # A standalone shared scene left ``_state`` pointing at a non-field
            # entry; restore the map browser's actual map before showing it.
            if self._showing_shared and self._reload_map_cb is not None:
                self._showing_shared = False
                self._reload_map_cb()
        else:
            if not self._scene_index_built:
                self._populate_scene_index()
            self._select_scene_row_for_current()

    def _populate_scene_index(self) -> None:
        """Build the map→scene tree once from the whole cutscene index.

        Field-map scenes group under their map (shared scenes with a host map
        fold in there, marked ``⇱``); scenes from shared entries with no host
        collect under a trailing "Shared / cross-map" group and open via
        standalone rendering."""
        self._scene_rows = _build_scene_index_rows(self._session)
        self._scene_tree.clear()
        by_map: Dict[int, List[_SceneRow]] = {}
        no_host: List[_SceneRow] = []
        for r in self._scene_rows:
            if r.map_id is None:
                no_host.append(r)
            else:
                by_map.setdefault(r.map_id, []).append(r)

        def _add_child(parent: QTreeWidgetItem, r: _SceneRow) -> None:
            ow_name = (
                self._resolve_ow_name(_between(r.label, "ow_id=", " "))
                if r.kind == TriggerKind.OWS else None
            )
            child = QTreeWidgetItem([_scene_row_label(r, ow_name)])
            child.setData(0, Qt.UserRole, r.global_ix)
            child.setData(0, Qt.UserRole + 1, r.map_id)
            child.setToolTip(0, f"entry {r.entry_ix} · {r.label}")
            parent.addChild(child)

        for map_id in sorted(by_map):
            rows = by_map[map_id]
            top = QTreeWidgetItem([
                f"{map_id:>3}  {area_name(map_id)}  ({len(rows)})"
            ])
            top.setFirstColumnSpanned(True)
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            top.setForeground(0, QColor("#9cdcfe"))
            for r in rows:
                _add_child(top, r)
            self._scene_tree.addTopLevelItem(top)
            top.setExpanded(True)

        if no_host:
            top = QTreeWidgetItem([f"⇱  Shared / cross-map  ({len(no_host)})"])
            top.setFirstColumnSpanned(True)
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            top.setForeground(0, QColor("#c39cde"))
            for r in no_host:
                _add_child(top, r)
            self._scene_tree.addTopLevelItem(top)
            top.setExpanded(True)

        field_n = len(self._scene_rows) - len(no_host)
        note = f"{field_n} cutscenes · {len(by_map)} maps"
        if no_host:
            note += f"  ·  {len(no_host)} shared/cross-map"
        self._scene_index_note.setText(note)
        self._scene_index_built = True

    def _apply_scene_filter(self, text: str) -> None:
        """Hide tree rows that don't match ``text`` (matched against the row
        label + trigger label); drop map groups left with no visible child."""
        needle = (text or "").strip().lower()
        for i in range(self._scene_tree.topLevelItemCount()):
            top = self._scene_tree.topLevelItem(i)
            any_visible = False
            for j in range(top.childCount()):
                child = top.child(j)
                hay = (child.text(0) + " " + (child.toolTip(0) or "")).lower()
                hit = (needle in hay) if needle else True
                child.setHidden(not hit)
                any_visible = any_visible or hit
            top.setHidden(not any_visible)
            if needle and any_visible:
                top.setExpanded(True)

    def _on_scene_row_selected(self) -> None:
        """An Index row was clicked. Field-map (and host-mapped) scenes
        navigate through the map browser so they render in map context;
        no-host shared scenes render standalone in the detail panel."""
        if self._selecting_scene_row:
            return
        items = self._scene_tree.selectedItems()
        if not items:
            return
        gix = items[0].data(0, Qt.UserRole)
        map_id = items[0].data(0, Qt.UserRole + 1)
        if gix is None:
            return
        if map_id is None:
            self._load_shared_scene(int(gix))
        elif self._navigate_scene_cb is not None:
            self._navigate_scene_cb(int(map_id), int(gix))

    def _load_shared_scene(self, global_ix: int) -> None:
        """Render a shared/non-field-entry cutscene standalone in the detail
        panel (no field map to host it).

        Builds a minimal :class:`_MapState` around the chain's source entry —
        enough for :meth:`_render_chain_detail` and the event cards, which
        edit the ROM bytes through the session regardless of the map context.
        ``map_id = -1`` marks it as a non-field state so toggling back to the
        map view reloads the real map."""
        try:
            cindex = self._session.cutscene_index()
        except (ValueError, KeyError):
            return
        if global_ix < 0 or global_ix >= len(cindex.chains):
            return
        chain = cindex.chains[global_ix]
        entry_ix = int(chain.source_entry_ix)
        try:
            entry_bytes = self._session.overlay5_entry_bytes(entry_ix)
        except (ValueError, KeyError):
            return
        src_idxs = cindex.chains_by_source.get(entry_ix, [])
        chains = [cindex.chains[i] for i in src_idxs]
        try:
            local_ix = chains.index(chain)
        except ValueError:
            chains = [chain]
            local_ix = 0
        self._state = _MapState(
            map_id=-1,
            entry_ix=entry_ix,
            chains=chains,
            selected_chip_ix=local_ix,
            entry_bytes=entry_bytes,
        )
        self._hidden_ext_ixs = set()
        self._showing_shared = True
        self._render_chain_detail(chain, local_ix)

    def _select_scene_row_for_current(self) -> None:
        """Highlight the tree row for the scene currently shown in the detail
        panel (so toggling map→index lands on the same scene)."""
        if self._scene_tree is None or self._state is None:
            return
        sel = self._state.selected_chip_ix
        if sel is None or sel < 0 or sel >= len(self._state.chains):
            return
        try:
            cindex = self._session.cutscene_index()
        except (ValueError, KeyError):
            return
        target = self._state.chains[sel]
        gix = next(
            (i for i, c in enumerate(cindex.chains) if c is target), None,
        )
        if gix is None:
            return
        self._selecting_scene_row = True
        try:
            for i in range(self._scene_tree.topLevelItemCount()):
                top = self._scene_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    child = top.child(j)
                    if child.data(0, Qt.UserRole) == gix:
                        self._scene_tree.setCurrentItem(child)
                        self._scene_tree.scrollToItem(child)
                        return
        finally:
            self._selecting_scene_row = False

    # ---- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        # Status line (which map, total chain count).
        self._status = QLabel("Select a field map.")
        self._status.setStyleSheet("color: #888; padding: 2px 4px;")

        # Chip row inside a scroll area — we cap the height so a
        # multi-row wrap doesn't shove the map render off-screen on
        # short windows. The flow layout already wraps; vertical scroll
        # kicks in only at the high-density outliers (Dark Gate-style).
        self._chip_host = QWidget()
        self._chip_layout = _FlowLayout(self._chip_host, spacing=6)
        chip_scroll = QScrollArea()
        chip_scroll.setWidget(self._chip_host)
        chip_scroll.setWidgetResizable(True)
        chip_scroll.setFrameShape(QFrame.NoFrame)
        chip_scroll.setFixedHeight(210)
        chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_scroll.setStyleSheet("background: #2d2d30;")

        # Map canvas — same widget the Events tab uses, but with no
        # moved-callbacks so dragging is disabled (read-only browse).
        # Reusing it gives us sprite pixmaps, exit-box rendering, and
        # marker selection signaling for free; the chip row's
        # highlight-on-selection slice will hook into ``select_marker``
        # and ``select_exit`` on this same canvas.
        self._canvas = EventsCanvas()
        # User clicks on the canvas drive the right-side list / detail
        # panel: clicking an NPC reveals its dialog, clicking an exit
        # reveals its destination, etc.
        self._canvas.markerSelected.connect(self._on_canvas_marker_selected)
        self._canvas.exitSelected.connect(self._on_canvas_exit_selected)

        # Left side: status + chip row + map.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._status)
        left_layout.addWidget(chip_scroll)
        left_layout.addWidget(self._canvas, 1)

        # Right side, top: Objects on Map list — the base scene's
        # inventory (or, in chip mode, the extras injected by the
        # currently-selected scene). Selecting a row activates the
        # object; sprite rows also pin a ``_SpriteEditorCard`` at the
        # top of the Details panel below so the sprite's id / X / Y
        # can be edited without switching panes.
        self._objects_list = QListWidget()
        self._objects_list.setIconSize(QSize(28, 28))
        self._objects_list.setStyleSheet(
            "QListWidget { background: #252526; color: #cccccc; "
            "border: none; padding: 4px; }"
            "QListWidget::item { padding: 3px 4px; }"
            "QListWidget::item:selected { background: #094771; color: white; }"
        )
        self._objects_list.itemSelectionChanged.connect(
            self._on_objects_list_selection_changed
        )
        self._objects_list.itemChanged.connect(
            self._on_objects_group_toggled
        )
        self._objects_list.itemDoubleClicked.connect(
            self._on_objects_group_double_clicked
        )

        # Details surface — a scrollable column that mixes read-only
        # HTML section labels (chain header, path, handler summary,
        # unknown-opcode notes) with native _DialogCard widgets for
        # the editable dialog blocks. The single-column layout keeps
        # narrative reading order ("here's what fires, here are the
        # lines, here are the open questions") while letting each
        # dialog be edited in place. HTML lives only where the content
        # is genuinely read-only.
        self._detail_container = QWidget()
        self._detail_container.setStyleSheet(
            "QWidget { background: #252526; color: #cccccc; }"
            "QLabel { color: #cccccc; background: transparent; }"
        )
        self._detail_layout = QVBoxLayout(self._detail_container)
        self._detail_layout.setContentsMargins(8, 8, 8, 8)
        self._detail_layout.setSpacing(6)
        self._detail_layout.addStretch(1)
        self._detail = QScrollArea()
        self._detail.setWidget(self._detail_container)
        self._detail.setWidgetResizable(True)
        self._detail.setFrameShape(QFrame.NoFrame)
        self._detail.setStyleSheet(
            "QScrollArea { background: #252526; border: none; }"
        )
        # Active event cards (dialog / music / reaction / battle) — held
        # so we can flush pending edits and route redo/undo refresh hooks
        # before tearing them down. All four card types expose
        # ``flush_pending()`` and ``refresh()`` with the same signature.
        self._event_cards: List[QFrame] = []

        # Currently-active sprite editor card, pinned at the top of the
        # Details panel whenever a sprite object is selected. Removed +
        # rebuilt on selection change or map switch. Stored separately
        # from ``_event_cards`` because it lives in a fixed position
        # (top of layout, position 0) and must survive chain re-renders
        # in chip mode when the user clicks a chain-extra NPC on the
        # canvas but the chain narrative stays put.
        self._sprite_editor_card: Optional[_SpriteEditorCard] = None

        # Events browser widgets — populated lazily by
        # ``_render_events_browser_for_chain``. The list carries one
        # row per sub-event (dialog / music / battle / reaction) plus
        # optional NPC group headers (disabled rows); clicking a row
        # swaps the widget in ``_events_card_container``'s single
        # child slot. ``_events_row_data`` parallels the list rows so
        # the row-changed handler can rebuild the card without
        # re-walking the events walker.
        self._events_list_widget: Optional[QListWidget] = None
        self._events_card_container: Optional[QWidget] = None
        self._events_card_layout: Optional[QVBoxLayout] = None
        self._events_row_data: List[
            Optional[Tuple[int, "overlay5_mod.RegionEvent"]]
        ] = []

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(self._objects_list)
        right_split.addWidget(self._detail)
        right_split.setStretchFactor(0, 0)
        right_split.setStretchFactor(1, 1)
        right_split.setSizes([240, 560])

        # ---- Map / Index view toggle -------------------------------------
        # The chip+canvas browser above is the map-tied way in; the Index is a
        # flat, ordered, cross-map list of every scripted cutscene so scenes
        # can be browsed/edited by listing instead of hunting map entries.
        # Both modes share the right-hand detail/edit panel — only the left
        # column swaps, so the current map view is untouched.
        self._map_view_widget = left
        self._index_view_widget = self._build_scene_index_view()
        self._left_stack = QStackedWidget()
        self._left_stack.addWidget(self._map_view_widget)    # page 0 = map
        self._left_stack.addWidget(self._index_view_widget)  # page 1 = index

        toggle_bar = QHBoxLayout()
        toggle_bar.setContentsMargins(6, 4, 6, 0)
        toggle_bar.setSpacing(4)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._map_mode_btn = QPushButton("\U0001f5fa Map")
        self._index_mode_btn = QPushButton("☰ Index")
        for _b in (self._map_mode_btn, self._index_mode_btn):
            _b.setCheckable(True)
            _b.setMaximumWidth(90)
            self._mode_group.addButton(_b)
            toggle_bar.addWidget(_b, 0)
        self._map_mode_btn.setChecked(True)
        self._map_mode_btn.clicked.connect(lambda: self._set_mode("map"))
        self._index_mode_btn.clicked.connect(lambda: self._set_mode("index"))
        toggle_bar.addStretch(1)
        # De-clutter toggle for the on-canvas sprite/zone labels (default on).
        self._labels_checkbox = QCheckBox("Show labels")
        self._labels_checkbox.setChecked(self._canvas.labels_visible())
        self._labels_checkbox.toggled.connect(self._canvas.set_labels_visible)
        toggle_bar.addWidget(self._labels_checkbox, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._left_stack)
        splitter.addWidget(right_split)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([1000, 500])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(toggle_bar)
        outer.addWidget(splitter)

    # ---- public API -------------------------------------------------------

    def clear(self) -> None:
        """Drop the current map state and reset to the empty view."""
        self._state = None
        self._clear_chips()
        self._canvas.clear()
        self._selection_syncing = True
        try:
            self._objects_list.clear()
        finally:
            self._selection_syncing = False
        self._hide_sprite_editor()
        self._hide_exit_editor()
        self._hide_chest_editor()
        self._show_empty_state()

    def set_map(
        self,
        map_id: int,
        entry_ix: int,
        composite_pixmap: QPixmap,
        sprite_specs: List[EventMarkerSpec],
        exit_specs: List[ExitZoneSpec],
        exit_form_data: Optional[List[object]] = None,
    ) -> None:
        """Render the chip row + map (with overworld objects) for ``map_id``.

        ``composite_pixmap`` is the same layer-A/B composite the parent
        map browser builds for the Events / Composite tabs.
        ``sprite_specs`` / ``exit_specs`` are the OVERWORLD_SPRITE
        markers and 0x001b exit/spawn/hitbox specs the Events tab
        already decoded for this map — we paint them on an
        :class:`EventsCanvas`.

        ``exit_form_data`` is the parent's resolved per-exit handler
        payload (duck-typed ``_ExitFormData`` — handler location, current
        dest, dest map id). When supplied, real map exits become editable:
        selecting one pins an :class:`_ExitDestinationCard` that repoints
        the warp via :class:`EditExitDestinationCommand`. Real-exit specs
        also get their tooltip/label rewritten to the ``warp to map N
        (Area)`` form. Omitted (older call paths / tests) → exits stay
        read-only with their incoming labels.
        """
        # A real field map is loading — any standalone shared-scene state is
        # superseded, and per-map canvas-visibility toggles reset.
        self._showing_shared = False
        self._hidden_sprite_offsets = set()
        self._collapsed_groups = set()
        try:
            cutscene_index = self._session.cutscene_index()
        except (ValueError, KeyError) as e:
            self._status.setText(f"Failed to load cutscene index: {e}")
            self._clear_chips()
            return

        chains = list(cutscene_index.chains_for_map(map_id))
        chains.sort(key=_chain_sort_key)

        try:
            entry_bytes = self._session.overlay5_entry_bytes(entry_ix)
        except (ValueError, KeyError):
            entry_bytes = b""

        # Pre-decode every dialog-bearing chain into a summary so the
        # chip-row label and the detail panel both read from the same
        # scan. HITBOX / EXIT chains gain a speaker-name label when they
        # carry their own decoded dialog (e.g. map 87's HITBOX#12 is
        # voiced by Barone via a cutscene-injected sprite). The
        # OTHER kind covers orphan-promoted sub-scenes — they're chain-
        # less from the structural index's POV but still need a speaker
        # label so the user can tell two sub-scenes apart at a glance.
        # OWS chains skip the scan: their trigger label already encodes
        # the NPC identity (`NPC slot N (#ow_id)`).
        entry_cache: Dict[int, bytes] = {entry_ix: entry_bytes}
        handler_summaries: Dict[int, _HandlerSummary] = {}
        _summary_kinds = (
            TriggerKind.HANDLER,
            TriggerKind.HITBOX,
            TriggerKind.EXIT,
            TriggerKind.OTHER,
        )
        for ix, chain in enumerate(chains):
            if chain.trigger_kind not in _summary_kinds:
                continue
            handler_summaries[ix] = _classify_handler_chain(
                chain, self._session, cutscene_index, entry_cache,
            )

        # Per-chain extra OVERWORLD_SPRITE placements. The base
        # ``sprite_specs`` only covers placements physically present in
        # the map's overlay5 entry; cutscene scripts living in handler
        # entries (typically 0499) inject additional NPCs whose 0x0150
        # blocks live there instead. Walk each chain's regions and pick
        # up those placements so chip-selection can paint them on top.
        placement_cache: Dict[
            int, List[overlay5_mod.OverworldSpritePlacement]
        ] = {}
        base_specs_by_offset: Dict[int, EventMarkerSpec] = {
            s.block_offset: s for s in sprite_specs
        }
        mchr_label_by_id: Dict[int, str] = {}
        for s in sprite_specs:
            if s.overworld_sprite_id not in mchr_label_by_id:
                mchr_label_by_id[s.overworld_sprite_id] = s.label
        chain_extras: Dict[int, List[EventMarkerSpec]] = {}
        chain_extra_placements: Dict[
            int, List[Tuple[int, overlay5_mod.OverworldSpritePlacement]]
        ] = {}
        chain_inherited_parents: Dict[
            int, Tuple[Tuple[int, int, int], ...]
        ] = {}
        chain_extra_synth_to_real: Dict[int, Tuple[int, int]] = {}
        # One pixmap cache per map render — every chain on this map
        # reuses the same MCHR ids (e.g. the talker in two sub-scenes),
        # so a per-render dict pays off without growing unboundedly.
        pixmap_cache: Dict[Tuple[int, Optional[int]], Optional[QPixmap]] = {}
        for ix, chain in enumerate(chains):
            extras, raw_placements, inherited_parents = _compute_chain_extras(
                chain, self._session, cutscene_index, entry_cache,
                placement_cache, entry_ix, base_specs_by_offset,
                mchr_label_by_id, pixmap_cache,
            )
            if extras:
                chain_extras[ix] = extras
            if raw_placements:
                chain_extra_placements[ix] = raw_placements
            if inherited_parents:
                chain_inherited_parents[ix] = inherited_parents
            # Reverse-table every synthetic offset emitted for this chain
            # back to the real (entry_ix, block_offset). The OWS trigger
            # NPC (path (1) above) reuses a base offset and is therefore
            # not synthetic — skip it.
            for spec, (real_entry_ix, placement) in zip(
                extras[-len(raw_placements):] if raw_placements else (),
                raw_placements,
            ):
                if spec.block_offset >= _CHAIN_EXTRA_OFFSET_BASE:
                    chain_extra_synth_to_real[spec.block_offset] = (
                        real_entry_ix, placement.block_offset,
                    )

        # Index OWS / EXIT / HITBOX chains by the block_offset embedded
        # in their trigger label, so the Objects list can dispatch a
        # row click straight to the chain that owns the object.
        ows_chain_by_offset: Dict[int, int] = {}
        exit_chain_by_offset: Dict[int, int] = {}
        hitbox_chain_by_offset: Dict[int, int] = {}
        for ix, chain in enumerate(chains):
            offset = _parse_trigger_at_offset(chain.trigger_label)
            if offset is None:
                continue
            if chain.trigger_kind == TriggerKind.OWS:
                ows_chain_by_offset.setdefault(offset, ix)
            elif chain.trigger_kind == TriggerKind.EXIT:
                exit_chain_by_offset.setdefault(offset, ix)
            elif chain.trigger_kind == TriggerKind.HITBOX:
                hitbox_chain_by_offset.setdefault(offset, ix)

        # (entry_ix, OWS block offset) → chain index — over the WHOLE
        # cutscene index, not just this map's bucket. A handler chain
        # in entry 0499 spawns NPCs whose dialog chains live in 0499
        # (or another handler entry) and bucket under map 264, not under
        # the map the player is currently on. Letting the detail panel
        # follow those by ``(entry_ix, block_offset)`` is what surfaces
        # the spawned NPCs' dialog content.
        ows_chain_by_spawn: Dict[Tuple[int, int], int] = {}
        for ix, chain in enumerate(cutscene_index.chains):
            if chain.trigger_kind != TriggerKind.OWS:
                continue
            spawn_off = _parse_trigger_at_offset(chain.trigger_label)
            if spawn_off is None:
                continue
            ows_chain_by_spawn.setdefault(
                (chain.source_entry_ix, spawn_off), ix,
            )

        # Index the parent's resolved exit form data by block offset, and
        # rewrite each real map exit's ``dest_label`` to the friendly
        # "warp to map N (Area)" form so the Objects list rows + canvas
        # tooltips read the way the user asked. Hitboxes / spawns keep
        # their incoming labels (a hitbox label is its handler location,
        # a spawn has none).
        exit_form_by_offset: Dict[int, object] = {}
        for fd in exit_form_data or ():
            exit_form_by_offset[int(fd.block_offset)] = fd
        relabeled_exit_specs: List[ExitZoneSpec] = []
        for s in exit_specs:
            fd = exit_form_by_offset.get(s.block_offset)
            dest_map_id = getattr(fd, "dest_map_id", None) if fd else None
            if not s.is_hitbox and not s.is_spawn and dest_map_id is not None:
                relabeled_exit_specs.append(
                    _dc_replace(s, dest_label=_warp_dest_label(dest_map_id))
                )
            else:
                relabeled_exit_specs.append(s)

        self._state = _MapState(
            map_id=map_id,
            entry_ix=entry_ix,
            chains=chains,
            selected_chip_ix=-1,
            entry_bytes=entry_bytes,
            handler_summaries=handler_summaries,
            composite_pixmap=composite_pixmap,
            sprite_specs=list(sprite_specs),
            exit_specs=relabeled_exit_specs,
            exit_form_by_offset=exit_form_by_offset,
            chain_extras=chain_extras,
            chain_extra_placements=chain_extra_placements,
            chain_inherited_parents=chain_inherited_parents,
            chain_extra_synth_to_real=chain_extra_synth_to_real,
            ows_chain_by_offset=ows_chain_by_offset,
            exit_chain_by_offset=exit_chain_by_offset,
            hitbox_chain_by_offset=hitbox_chain_by_offset,
            ows_chain_by_spawn=ows_chain_by_spawn,
        )

        # EXT chains whose whole body is a warp into another map's init carry
        # no editable events — they duplicate this map's Exit objects, so
        # they're dropped from the chip row (the warp stays editable as an
        # Exit zone). Content-bearing cross-map scenes keep their chip.
        self._hidden_ext_ixs = self._compute_hidden_ext_ixs()

        # Status line. Only content-bearing EXT chains count as cross-script;
        # the warp-only ones are hidden, so they don't inflate the count.
        visible_n = sum(1 for c in chains if c.trigger_kind != TriggerKind.EXT)
        ext_n = sum(
            1 for i, c in enumerate(chains)
            if c.trigger_kind == TriggerKind.EXT and i not in self._hidden_ext_ixs
        )
        suffix = f" (+ {ext_n} cross-script)" if ext_n else ""
        self._status.setText(
            f"Map {map_id}  ·  entry {entry_ix:04d}  ·  "
            f"{visible_n} scene{'s' if visible_n != 1 else ''}{suffix}  ·  "
            f"{len(sprite_specs)} sprite{'s' if len(sprite_specs) != 1 else ''}, "
            f"{len(exit_specs)} zone{'s' if len(exit_specs) != 1 else ''}"
        )

        # Chip row + initial canvas render. ``_select_chip(-1)`` calls
        # ``_rebuild_canvas_for_selection`` which paints the Base view
        # (composite + base sprite_specs, no chain extras) — no separate
        # initial ``set_map`` needed, and the same code path covers chip
        # flips.
        self._rebuild_chips()
        self._populate_objects_list()
        self._select_chip(-1)

    # ---- chip row management ---------------------------------------------

    def _compute_hidden_ext_ixs(self) -> set:
        """Chain indices for EXT ('cross-script') chains that carry no
        editable events.

        Such a chain's entire body is a warp-hop into another map's init
        (``entry N + 0x0000``) with nothing scripted before it — i.e. it's a
        plain map-to-map warp, already editable as an Exit zone. Surfacing it
        as a "cross-script" chip is misleading (85% of EXT chains game-wide
        are this), so we drop it from the chip row. Genuine cross-map scenes
        — shared ``CALL_SYS`` bodies with dialog / anim / battle events —
        walk to a non-empty event list and keep their chip.
        """
        hidden: set = set()
        if self._state is None:
            return hidden
        entry_cache: Dict[int, bytes] = {
            self._state.entry_ix: self._state.entry_bytes,
        }
        for ix, chain in enumerate(self._state.chains):
            if chain.trigger_kind != TriggerKind.EXT:
                continue
            try:
                events, _ = self._collect_events_for_chain(chain, entry_cache)
            except (ValueError, KeyError, AssertionError):
                events = []
            if not events:
                hidden.add(ix)
        return hidden

    def _clear_chips(self) -> None:
        if getattr(self, "_base_chip", None) is not None:
            self._base_chip.setParent(None)
            self._base_chip.deleteLater()
        self._base_chip = None
        for chip in self._chips:
            if chip is None:
                continue
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []

    def _rebuild_chips(self) -> None:
        """Build the chip row for the current map.

        Skips OWS/EXIT/HITBOX chains — those drive the Objects-on-Map
        list rows instead. Chips are reserved for chains that change the
        base scene composition: scripted handlers, cross-script callers,
        and the catch-all OTHER/HEADER kinds.

        ``_chips`` is a sparse list parallel to ``_state.chains``: index
        i holds the chip for chain i when one exists, ``None`` when the
        chain was filtered into the Objects list. ``chips[-1]`` is the
        "Base" chip (stored outside the per-chain array) and tracked via
        ``_base_chip``.
        """
        self._clear_chips()
        assert self._state is not None
        # "Base" chip first — represents the no-scene-selected default.
        base = _ChainChip("base", "Base", self._chip_host)
        base.clicked.connect(lambda _ck=False: self._select_chip(-1))
        self._chip_layout.addWidget(base)
        self._base_chip = base
        self._chips = [None] * len(self._state.chains)
        for ix, chain in enumerate(self._state.chains):
            if chain.trigger_kind in _OBJECTS_LIST_KINDS:
                continue
            if ix in self._hidden_ext_ixs:
                continue
            summary = self._state.handler_summaries.get(ix)
            chip = _ChainChip(
                chain.trigger_kind,
                _short_chip_label(chain, summary, self._state.map_id),
                self._chip_host,
            )
            chip.setToolTip(chain.trigger_label or "(no trigger info)")
            chip.clicked.connect(
                lambda _ck=False, i=ix: self._select_chip(i)
            )
            self._chip_layout.addWidget(chip)
            self._chips[ix] = chip
        # Force a layout refresh so the flow layout's height kicks in.
        self._chip_host.adjustSize()

    def select_chain_by_global_ix(self, global_chain_ix: int) -> None:
        """Public entry point: select the chain identified by its position
        in ``session.cutscene_index().chains`` (a global index, not the
        sorted per-map subset ``_state.chains`` uses).

        Deferred one tick so the caller can chain it after a
        ``set_map`` / tab-switch — the map may still be mid-render when
        the click that triggered navigation lands. Silently no-ops when
        the chain isn't attached to the current map (an enemy that
        appears on multiple maps could have its click resolve to a
        different map than the one currently loaded).
        """
        QTimer.singleShot(0, lambda: self._select_chain_now(global_chain_ix))

    def _select_chain_now(self, global_chain_ix: int) -> None:
        if self._state is None or global_chain_ix < 0:
            return
        try:
            cindex = self._session.cutscene_index()
        except (ValueError, KeyError):
            return
        if global_chain_ix >= len(cindex.chains):
            return
        target = cindex.chains[global_chain_ix]
        # Identity match — chains inside ``_state.chains`` are the same
        # ``CutsceneChain`` objects the cutscene index owns, just sorted
        # and filtered to this map's subset.
        for local_ix, chain in enumerate(self._state.chains):
            if chain is target:
                self._select_chip(local_ix)
                return

    def _select_chip(self, chip_ix: int) -> None:
        """Mark the chip at ``chip_ix`` (-1 = Base) as the active scene
        and re-render the detail panel.

        Rebuilds the canvas with ``base sprite_specs + chain extras``
        every time the selection changes: extras are placements injected
        by handler entries (typically 0499) that aren't in the base map
        entry, so they only become visible while their chain is active.
        Switching back to Base drops them. Also clears the Objects list
        selection — chip and object are two competing selection states,
        and the latest action wins the detail panel.
        """
        if self._state is None:
            return
        if self._base_chip is not None:
            self._base_chip.setChecked(chip_ix < 0)
        for i, chip in enumerate(self._chips):
            if chip is None:
                continue
            chip.setChecked(i == chip_ix)
        self._state.selected_chip_ix = chip_ix
        self._rebuild_canvas_for_selection(chip_ix)
        # Refill the Objects on Map list for the new selection — base
        # mode shows the full inventory, chip mode shows only the
        # scene's own chain_extras sprites. Dropping the sprite editor
        # card here keeps the detail panel coherent until the user
        # clicks a new object.
        self._populate_objects_list()
        self._hide_sprite_editor()
        if chip_ix < 0:
            self._render_base_detail()
            self._highlight_chain_on_canvas(None)
        else:
            chain = self._state.chains[chip_ix]
            self._render_chain_detail(chain, chip_ix)
            self._highlight_chain_on_canvas(chain)

    def _rebuild_canvas_for_selection(self, chip_ix: int) -> None:
        """Re-render the canvas backdrop with the scene's own sprites.

        ``chip_ix == -1`` (Base) renders the full base sprite list — the
        map's "what's normally here" snapshot. A chain index renders
        ONLY the chain's own sprites (its OWS trigger NPC for OWS
        chains, plus any cutscene-spawned 0x0150 placements). Base
        sprites are deliberately hidden so the chip view answers
        "what does this cutscene put on screen?" cleanly. The
        :class:`EventsCanvas` clears its scene on every ``set_map``,
        so the previous chip's extras drop cleanly without manual
        bookkeeping.
        """
        if self._state is None or self._state.composite_pixmap is None:
            return
        if chip_ix < 0:
            # Base view honours the Objects-list group checkboxes — hidden
            # placements drop off the canvas but stay in the list.
            specs = [
                s for s in self._state.sprite_specs
                if s.block_offset not in self._hidden_sprite_offsets
            ]
        else:
            specs = list(self._state.chain_extras.get(chip_ix, []))
        moved_cb = (
            self._on_chain_marker_moved if self._undo_stack is not None
            else None
        )
        self._canvas.set_map(
            self._state.composite_pixmap, specs, moved_cb=moved_cb,
            exit_specs=self._state.exit_specs, exit_moved_cb=None,
        )

    # ---- marker drag → undo command -------------------------------------

    def _resolve_canvas_offset(
        self, canvas_offset: int,
    ) -> Optional[Tuple[int, int]]:
        """Map a canvas marker's ``block_offset`` to its real
        ``(entry_ix, block_offset)`` pair.

        Base sprites carry the real offset directly and live in the map's
        own entry. Chain-extras carry a synthetic offset
        (>= ``_CHAIN_EXTRA_OFFSET_BASE``) which is looked up in the
        per-map reverse table populated during :meth:`set_map`. The
        synthetic encoding truncates the entry index, so a fresh lookup
        is the only safe way to recover the real entry.
        """
        if self._state is None:
            return None
        if canvas_offset >= _CHAIN_EXTRA_OFFSET_BASE:
            return self._state.chain_extra_synth_to_real.get(canvas_offset)
        return (self._state.entry_ix, canvas_offset)

    def _on_chain_marker_moved(
        self, canvas_offset: int, new_x: int, new_y: int,
    ) -> None:
        """Push a :class:`MoveOverworldSpriteCommand` for a dragged marker.

        The canvas optimistically moved the QGraphicsItem to (new_x, new_y);
        we own the model-side persistence + cross-chip propagation. Edits
        made in a child sub-scene chip propagate to the parent handler
        chip (and vice versa) because both chips inherit their NPC list
        from the same overlay5 entry — patching the cache by real
        ``(entry_ix, block_offset)`` covers every chip that references it.
        """
        if self._state is None or self._undo_stack is None:
            return
        resolved = self._resolve_canvas_offset(canvas_offset)
        if resolved is None:
            return
        real_entry_ix, real_offset = resolved
        cmd = MoveOverworldSpriteCommand(
            self._session,
            real_entry_ix,
            real_offset,
            new_x,
            new_y,
            description=(
                f"Move overworld sprite "
                f"(entry {real_entry_ix:04d} @0x{real_offset:04x})"
            ),
            on_change=lambda x, y, ce=canvas_offset,
            rentry=real_entry_ix, roff=real_offset:
                self._on_chain_marker_xy_applied(ce, rentry, roff, x, y),
        )
        self._undo_stack.push(cmd)

    def _on_chain_marker_xy_applied(
        self,
        canvas_offset: int,
        real_entry_ix: int,
        real_offset: int,
        x: int,
        y: int,
    ) -> None:
        """Model→view sync after the Move command applies (redo or undo).

        Updates the canvas item that was actually dragged, plus every
        cached spec/placement across chips that points at the same real
        ``(entry_ix, block_offset)``. The cross-chip patch is what makes
        parent-handler ↔ child-sub-scene propagation visible without a
        full tab rebuild.
        """
        if self._state is None:
            return
        self._canvas.update_marker_position(canvas_offset, x, y)
        # Patch base sprite_specs if the moved sprite belongs to the
        # map's own entry (the only entry whose blocks land in
        # sprite_specs).
        if real_entry_ix == self._state.entry_ix:
            for i, s in enumerate(self._state.sprite_specs):
                if s.block_offset != real_offset:
                    continue
                self._state.sprite_specs[i] = _dc_replace(s, x=x, y=y)
                break
        # Patch every chip's cached extras + raw placements that match.
        # An OWS chain's trigger NPC reuses the base entry's offset
        # directly (not synthetic), so the chain_extras entry for it is
        # keyed by the same offset as the base spec. Chain-spawned and
        # inherited placements carry a synthetic offset that we reverse
        # via the per-map synth_to_real table.
        for chip_ix, specs in self._state.chain_extras.items():
            for i, s in enumerate(specs):
                resolved = (
                    self._state.chain_extra_synth_to_real.get(s.block_offset)
                    if s.block_offset >= _CHAIN_EXTRA_OFFSET_BASE
                    else (self._state.entry_ix, s.block_offset)
                )
                if resolved != (real_entry_ix, real_offset):
                    continue
                specs[i] = _dc_replace(s, x=x, y=y)
        for chip_ix, raws in self._state.chain_extra_placements.items():
            for i, (e_ix, p) in enumerate(raws):
                if e_ix != real_entry_ix or p.block_offset != real_offset:
                    continue
                raws[i] = (e_ix, _dc_replace(p, x=int(x), y=int(y)))
        # Re-sync the sprite editor card if it's bound to this marker,
        # and repaint the matching Objects list row label so the new xy
        # shows even when the user undoes a long way back.
        card = self._sprite_editor_card
        if card is not None and card.canvas_offset() == canvas_offset:
            card.sync_state(int(card._current_sprite_id), int(x), int(y))
        self._refresh_objects_list_sprite_row(canvas_offset)

    # ---- inline sprite editor card (top of Details panel) ---------------

    def _hide_sprite_editor(self) -> None:
        """Remove the pinned sprite editor card, if any.

        Called when the active selection changes to a non-sprite object
        or the map switches. Safe to call when no card is present.
        """
        card = self._sprite_editor_card
        if card is None:
            return
        self._detail_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._sprite_editor_card = None

    def _show_sprite_editor_for(self, canvas_offset: int) -> None:
        """Pin a fresh sprite editor card at the top of the Details panel.

        Reads the freshest id + xy from the canvas item (same source
        the right-click flow used) so a just-applied undo or redo lands
        in the card correctly. Replaces any previous card so switching
        selection swaps in place instead of stacking.
        """
        if self._state is None:
            return
        spec = self._canvas.marker_spec(canvas_offset)
        if spec is None:
            self._hide_sprite_editor()
            return
        resolved = self._resolve_canvas_offset(canvas_offset)
        if resolved is None:
            self._hide_sprite_editor()
            return
        real_entry_ix, real_offset = resolved
        self._hide_sprite_editor()
        card = _SpriteEditorCard(
            self._session,
            self._undo_stack,
            canvas_offset,
            real_entry_ix,
            real_offset,
            int(spec.overworld_sprite_id),
            int(spec.x),
            int(spec.y),
            on_id_commit=self._push_sprite_id_command,
            on_xy_commit=self._push_sprite_xy_command,
            alt=False,
            parent=self._detail_container,
        )
        self._sprite_editor_card = card
        self._detail_layout.insertWidget(0, card)

    # ---- exit-destination editor card ------------------------------------

    def _get_exit_dest_choices(self) -> List[Tuple[int, str]]:
        """ROM-static ``(file_off, "warp to map N (Area)")`` choices, cached."""
        if self._exit_dest_choices_cache is None:
            self._exit_dest_choices_cache = _exit_dest_choices(self._session)
        return self._exit_dest_choices_cache

    def _hide_exit_editor(self) -> None:
        """Remove the pinned exit-destination card, if any."""
        card = self._exit_editor_card
        if card is None:
            return
        self._detail_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._exit_editor_card = None

    def _show_exit_editor_for(self, offset: int) -> None:
        """Pin a fresh exit-destination card at the top of the Details panel.

        Only standard map exits (a ``0x0002 + 0x0030`` handler the parent
        resolved) are editable; spawns and bespoke hitboxes have no
        repointable destination and get no card. No-ops without an undo
        stack (read-only session).
        """
        if self._state is None or self._undo_stack is None:
            self._hide_exit_editor()
            return
        fd = self._state.exit_form_by_offset.get(offset)
        if (
            fd is None
            or getattr(fd, "is_spawn", False)
            or getattr(fd, "is_hitbox", False)
            or int(getattr(fd, "handler_entry_ix", -1)) < 0
        ):
            self._hide_exit_editor()
            return
        self._hide_exit_editor()
        card = _ExitDestinationCard(
            self._session,
            self._undo_stack,
            offset,
            int(fd.handler_entry_ix),
            int(fd.handler_rel_offset),
            int(fd.handler_dest),
            self._get_exit_dest_choices(),
            on_dest_commit=self._push_exit_dest_command,
            alt=False,
            parent=self._detail_container,
        )
        self._exit_editor_card = card
        self._detail_layout.insertWidget(0, card)

    def _push_exit_dest_command(
        self,
        block_offset: int,
        handler_entry_ix: int,
        handler_rel_offset: int,
        new_dest: int,
    ) -> None:
        """Bridge from a card's destination commit to the undo stack."""
        if self._undo_stack is None:
            return
        _mid, label = _resolve_warp_dest(self._session, new_dest)
        cmd = EditExitDestinationCommand(
            self._session,
            handler_entry_ix,
            handler_rel_offset,
            new_dest,
            description=(
                f"Repoint exit @0x{block_offset:04x} → "
                f"{label or f'0x{new_dest:08x}'}"
            ),
            on_change=lambda dest, bo=block_offset, he=handler_entry_ix,
            hr=handler_rel_offset:
                self._on_exit_dest_applied(bo, he, hr, dest),
        )
        self._undo_stack.push(cmd)

    def _on_exit_dest_applied(
        self,
        block_offset: int,
        handler_entry_ix: int,
        handler_rel_offset: int,
        new_dest: int,
    ) -> None:
        """Model→view sync after an EditExitDestinationCommand redo/undo.

        Fires on both directions, so it re-resolves the label from the
        actual applied dest each time. Shared-handler aware: every exit
        whose handler location matches gets its cached form data, spec
        label, list row, and canvas tooltip refreshed together.
        """
        if self._state is None:
            return
        map_id, label = _resolve_warp_dest(self._session, new_dest)
        for off, fd in self._state.exit_form_by_offset.items():
            if (
                int(getattr(fd, "handler_entry_ix", -1)) != handler_entry_ix
                or int(getattr(fd, "handler_rel_offset", -1)) != handler_rel_offset
            ):
                continue
            fd.handler_dest = int(new_dest) & 0xFFFFFFFF
            fd.dest_map_id = map_id
            fd.dest_label = label
            for i, s in enumerate(self._state.exit_specs):
                if s.block_offset != off:
                    continue
                self._state.exit_specs[i] = _dc_replace(s, dest_label=label)
                self._refresh_exit_objects_row(off, label)
                self._canvas.update_exit_dest_label(off, label)
                break
        card = self._exit_editor_card
        if card is not None and card.block_offset() == block_offset:
            card.sync_state(new_dest)

    def _refresh_exit_objects_row(self, offset: int, label: str) -> None:
        """Repaint the Objects-list exit row whose block_offset matches."""
        if self._state is None:
            return
        spec = next(
            (e for e in self._state.exit_specs if e.block_offset == offset),
            None,
        )
        if spec is None:
            return
        for i in range(self._objects_list.count()):
            item = self._objects_list.item(i)
            if (
                item.data(_LIST_ROW_TYPE_ROLE) != _LIST_ROW_EXIT
                or item.data(Qt.UserRole) != offset
            ):
                continue
            item.setText(f"Exit {spec.display_idx}  →  {label or '?'}")
            item.setToolTip(
                f"Exit zone (tile {spec.x1},{spec.y1} — {spec.x2},{spec.y2})\n"
                f"to: {label or '(unknown)'}"
            )
            return

    # ---- chest (give-item) editor card -----------------------------------

    def _get_item_choices(self) -> List[Tuple[int, str]]:
        """Item combo choices, cached (the name table is static)."""
        if self._item_choices_cache is None:
            self._item_choices_cache = _item_choices()
        return self._item_choices_cache

    def _resolve_abs_overlay5_offset(
        self, abs_off: int,
    ) -> Tuple[Optional[int], Optional[int]]:
        """Absolute overlay5 file offset → ``(entry_ix, rel)`` or ``(None,
        None)``. Used to follow an OWS ``string_ptr`` to its interaction
        script (string_ptr is absolute per project memory)."""
        try:
            index = self._session.overlay5_index()
        except (ValueError, KeyError, AttributeError):
            return None, None
        for entry_ix, start in index.entry_starts.items():
            if start <= abs_off < index.entry_ends[entry_ix]:
                return entry_ix, abs_off - start
        return None, None

    def _detect_chest(
        self, entry_ix: int, block_offset: int,
    ) -> Optional[_ChestInfo]:
        """Return chest give-item data for the OWS at ``(entry_ix,
        block_offset)``, or ``None`` when it isn't a chest.

        Follows the sprite's ``string_ptr`` to its interaction script and
        looks for the give-item run — SET_VAR of ARG_0..3 with a
        ``CALL_SYS`` to the shared chest routine. Gating on the routine id
        keeps ordinary NPC dialog scripts from matching.
        """
        try:
            entry = self._session.overlay5_entry_bytes(entry_ix)
        except (ValueError, KeyError):
            return None
        if block_offset + overlay5_mod.OVERWORLD_SPRITE_BLOCK_SIZE > len(entry):
            return None
        try:
            pl = overlay5_mod.OverworldSpritePlacement.from_bytes(
                entry, block_offset,
            )
        except (AssertionError, struct.error, ValueError):
            return None
        script_entry_ix, rel = self._resolve_abs_overlay5_offset(pl.string_ptr)
        if script_entry_ix is None:
            return None
        try:
            seb = self._session.overlay5_entry_bytes(script_entry_ix)
        except (ValueError, KeyError):
            return None
        vars_: Dict[int, int] = {}
        item_off: Optional[int] = None
        call_target: Optional[int] = None
        p = rel
        n = min(len(seb), rel + _CHEST_SCAN_WINDOW)
        while p + 2 <= n:
            op = struct.unpack_from("<H", seb, p)[0]
            if op == 0x0015 and p + 6 <= n:  # SET_VAR
                var, val = struct.unpack_from("<HH", seb, p + 2)
                vars_[var] = val
                if var == 0x0005:
                    item_off = p + 4
                p += 6
            elif op == 0x0002 and p + 6 <= n:  # CALL_SYS
                call_target = struct.unpack_from("<I", seb, p + 2)[0]
                p += 6
                if item_off is not None:
                    break
            elif op in (0x0006, 0x0030) and p + 6 <= n:
                p += 6
            else:
                p += 2
        if item_off is None or call_target != overlay5_mod.CHEST_GIVE_ROUTINE:
            return None
        return _ChestInfo(
            script_entry_ix=script_entry_ix,
            item_value_offset=item_off,
            item_id=vars_.get(0x0005, 0),
            flag=vars_.get(0x0004, 0),
            arg2=vars_.get(0x0006, 0),
            arg3=vars_.get(0x0007, 0),
        )

    def _hide_chest_editor(self) -> None:
        """Remove the pinned chest card, if any."""
        card = self._chest_editor_card
        if card is None:
            return
        self._detail_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._chest_editor_card = None

    def _show_chest_editor_for(self, canvas_offset: int) -> None:
        """Pin a chest card on top of the sprite card when ``canvas_offset``
        names a chest give-item sprite. No-op (and hides any stale card)
        otherwise."""
        if self._state is None or self._undo_stack is None:
            self._hide_chest_editor()
            return
        resolved = self._resolve_canvas_offset(canvas_offset)
        if resolved is None:
            self._hide_chest_editor()
            return
        real_entry_ix, real_offset = resolved
        chest = self._detect_chest(real_entry_ix, real_offset)
        if chest is None:
            self._hide_chest_editor()
            return
        self._hide_chest_editor()
        card = _ChestCard(
            self._session,
            self._undo_stack,
            chest,
            self._get_item_choices(),
            on_item_commit=self._push_chest_item_command,
            alt=False,
            parent=self._detail_container,
        )
        self._chest_editor_card = card
        # Above the sprite editor card (inserted at 0 earlier) so the chest
        # — the salient thing about this object — reads first.
        self._detail_layout.insertWidget(0, card)

    def _push_chest_item_command(
        self, script_entry_ix: int, item_value_offset: int, new_item_id: int,
    ) -> None:
        """Bridge from a chest card's item commit to the undo stack."""
        if self._undo_stack is None:
            return
        cmd = EditChestItemCommand(
            self._session,
            script_entry_ix,
            item_value_offset,
            new_item_id,
            description=f"Set chest item → {_item_display_name(new_item_id)}",
            on_change=self._on_chest_item_applied,
        )
        self._undo_stack.push(cmd)

    def _on_chest_item_applied(self, item_id: int) -> None:
        """Re-sync the chest card after an EditChestItemCommand redo/undo."""
        card = self._chest_editor_card
        if card is not None:
            card.sync_state(item_id)

    def _push_sprite_id_command(
        self,
        canvas_offset: int,
        real_entry_ix: int,
        real_offset: int,
        new_sid: int,
    ) -> None:
        """Bridge from a card's Sprite-ID commit to the undo stack."""
        if self._undo_stack is None:
            return
        cmd = EditOverworldSpriteIdCommand(
            self._session,
            real_entry_ix,
            real_offset,
            new_sid,
            description=(
                f"Edit overworld sprite id "
                f"(entry {real_entry_ix:04d} @0x{real_offset:04x})"
            ),
            on_change=lambda sid, ce=canvas_offset,
            rentry=real_entry_ix, roff=real_offset:
                self._on_chain_marker_id_applied(ce, rentry, roff, sid),
        )
        self._undo_stack.push(cmd)

    def _push_sprite_xy_command(
        self,
        canvas_offset: int,
        real_entry_ix: int,
        real_offset: int,
        new_x: int,
        new_y: int,
    ) -> None:
        """Bridge from a card's X/Y commit to the undo stack."""
        if self._undo_stack is None:
            return
        cmd = MoveOverworldSpriteCommand(
            self._session,
            real_entry_ix,
            real_offset,
            new_x,
            new_y,
            description=(
                f"Move overworld sprite "
                f"(entry {real_entry_ix:04d} @0x{real_offset:04x})"
            ),
            on_change=lambda x, y, ce=canvas_offset,
            rentry=real_entry_ix, roff=real_offset:
                self._on_chain_marker_xy_applied(ce, rentry, roff, x, y),
        )
        self._undo_stack.push(cmd)

    def _on_chain_marker_id_applied(
        self,
        canvas_offset: int,
        real_entry_ix: int,
        real_offset: int,
        sprite_id: int,
    ) -> None:
        """Model→view sync after EditOverworldSpriteIdCommand.

        Mirrors :meth:`_on_chain_marker_xy_applied`: refreshes the
        dragged canvas marker plus every cached spec/placement across
        chips that points at the same real ``(entry_ix, block_offset)``,
        so an id swap on the parent handler is also reflected on the
        child sub-scene chip (and vice versa).
        """
        if self._state is None:
            return
        new_label = self._sprite_marker_label_for(sprite_id)
        new_pixmap = self._sprite_marker_pixmap_for(sprite_id)
        self._canvas.update_marker_sprite_id(
            canvas_offset, sprite_id, new_label, new_pixmap,
        )
        # Patch base sprite_specs.
        if real_entry_ix == self._state.entry_ix:
            for i, s in enumerate(self._state.sprite_specs):
                if s.block_offset != real_offset:
                    continue
                self._state.sprite_specs[i] = _dc_replace(
                    s, overworld_sprite_id=sprite_id,
                    label=new_label, pixmap=new_pixmap,
                )
                break
        # Patch every chip's cached extras + raw placements that match.
        for _chip_ix, specs in self._state.chain_extras.items():
            for i, s in enumerate(specs):
                resolved = (
                    self._state.chain_extra_synth_to_real.get(s.block_offset)
                    if s.block_offset >= _CHAIN_EXTRA_OFFSET_BASE
                    else (self._state.entry_ix, s.block_offset)
                )
                if resolved != (real_entry_ix, real_offset):
                    continue
                specs[i] = _dc_replace(
                    s, overworld_sprite_id=sprite_id,
                    label=new_label, pixmap=new_pixmap,
                )
        for _chip_ix, raws in self._state.chain_extra_placements.items():
            for i, (e_ix, p) in enumerate(raws):
                if e_ix != real_entry_ix or p.block_offset != real_offset:
                    continue
                raws[i] = (
                    e_ix,
                    _dc_replace(p, overworld_sprite_id=int(sprite_id)),
                )
        # Re-sync the sprite editor card's Sprite-ID combo if it's bound
        # to this marker, and repaint the matching Objects list row so
        # the new sprite + label show after redo/undo without a manual
        # reselect.
        card = self._sprite_editor_card
        if card is not None and card.canvas_offset() == canvas_offset:
            x, y = card._current_xy
            card.sync_state(int(sprite_id), x, y)
        self._refresh_objects_list_sprite_row(canvas_offset)

    def _refresh_objects_list_sprite_row(self, canvas_offset: int) -> None:
        """Repaint the Objects list row whose block_offset matches.

        Used by xy / id apply handlers so the row text + icon track the
        latest spec without rebuilding the whole list (which would
        clear the current selection and unbind the form).
        """
        if self._state is None:
            return
        spec = self._canvas.marker_spec(canvas_offset)
        if spec is None:
            return
        for i in range(self._objects_list.count()):
            item = self._objects_list.item(i)
            if (
                item.data(_LIST_ROW_TYPE_ROLE) != _LIST_ROW_SPRITE
                or item.data(Qt.UserRole) != canvas_offset
            ):
                continue
            item.setText(
                f"0x{spec.overworld_sprite_id:04x}  ({spec.x}, {spec.y})"
            )
            if spec.pixmap is not None and not spec.pixmap.isNull():
                item.setIcon(QIcon(spec.pixmap))
            else:
                item.setIcon(self._sprite_fallback_icon())
            item.setToolTip(spec.label)
            return

    def _sprite_marker_label_for(self, sprite_id: int) -> str:
        """ow-id → label string, matching what the chain-extras
        renderer uses for cutscene placements."""
        try:
            labels = self._session.get_mchr_ow_labels()
        except (AttributeError, ValueError):
            labels = []
        if 0 <= int(sprite_id) < len(labels):
            return str(labels[int(sprite_id)])
        return f"MCHR 0x{int(sprite_id):04x}"

    def _resolve_ow_name(self, ow_id_str: str) -> Optional[str]:
        """Clean character name for an OWS ``ow_id`` (hex string from a
        trigger label), or ``None`` when the id has no known name.

        Rejects the fallback forms (raw ``MCHR 0x..`` label, leftover ``[OW]``
        / hex) so the Index only shows a name when there really is one."""
        try:
            ow = int(ow_id_str, 0)
        except (ValueError, TypeError):
            return None
        name = _clean_ow_name(self._sprite_marker_label_for(ow))
        if (not name or "[OW]" in name or name.startswith("MCHR ")
                or name.lower().startswith("0x")):
            return None
        return name

    def _build_slot_labels(self, chain_ix: int) -> Dict[int, str]:
        """Map slot index → character name for the selected chain.

        0..3 are always player + party 1/2/3. 4+ come from the scene's
        OVERWORLD_SPRITE placements (each carries its own ``slot``): the
        map's own placements first, then the chain-injected ones so a
        scene actor spawned by this cutscene wins a shared slot.
        """
        labels: Dict[int, str] = dict(_FIXED_SLOT_LABELS)
        if self._state is None:
            return labels
        placements: List = []
        try:
            placements.extend(
                overlay5_mod.iter_overworld_sprites(self._state.entry_bytes)
            )
        except (ValueError, AttributeError):
            pass
        for _e_ix, placement in self._state.chain_extra_placements.get(
            chain_ix, [],
        ):
            placements.append(placement)
        for p in placements:
            slot = int(getattr(p, "slot", -1))
            if slot < 4 or slot > 0xFFFF:   # keep player/party fixed
                continue
            labels[slot] = _clean_ow_name(self._sprite_marker_label_for(
                int(p.overworld_sprite_id),
            ))
        return labels

    def _build_referenced_slots(self, chain_ix: int) -> set:
        """Slots the chain's opcodes actually target — sprite-anim subjects,
        camera pan-to-sprite targets, and dialog speakers.

        These are the scene's genuine cast (vs. every sprite the map merely
        declares). The "who" combos group them first so a user editing a
        Phascomon scene sees Player + Phascomon up top rather than the whole
        map's sprite list. Walk errors degrade to an empty set (combo just
        falls back to the flat, unranked list)."""
        refs: set = set()
        if self._state is None:
            return refs
        try:
            chain = self._state.chains[chain_ix]
        except (IndexError, TypeError):
            return refs
        entry_cache: Dict[int, bytes] = {
            self._state.entry_ix: self._state.entry_bytes,
        }
        try:
            events, _ = self._collect_events_for_chain(chain, entry_cache)
        except (ValueError, KeyError, AssertionError):
            return refs
        for _entry_ix, ev in events:
            pay = ev.payload
            if ev.kind == overlay5_mod.EVENT_KIND_SPRITE_ANIM:
                refs.add(int(pay.sprite) & 0xFFFF)
            elif ev.kind == overlay5_mod.EVENT_KIND_CAMERA and not pay.is_xy:
                refs.add(int(pay.a) & 0xFFFF)
            elif ev.kind == overlay5_mod.EVENT_KIND_MOVE:
                refs.add(int(pay.tgt) & 0xFFFF)
            elif ev.kind == overlay5_mod.EVENT_KIND_DIALOG:
                target = getattr(pay, "target", None)
                if target is not None:
                    refs.add(int(target) & 0xFFFF)
        return refs

    def _build_slot_ow_ids(self, chain_ix: int) -> Dict[int, int]:
        """slot index → overworld-sprite id for the selected chain.

        Same resolution rule the anim-id verification relies on: start from
        the base map entry's OVERWORLD_SPRITE placements, then let this
        chain's own ``chain_extra_placements`` override — a scene actor
        spawned by the cutscene wins the slot. Player/party slots (0-3) have
        no placement and are intentionally absent (the sprite-anim thumbnail
        only resolves real overworld sprites)."""
        mapping: Dict[int, int] = {}
        if self._state is None:
            return mapping
        placements: List = []
        try:
            placements.extend(
                overlay5_mod.iter_overworld_sprites(self._state.entry_bytes)
            )
        except (ValueError, AttributeError):
            pass
        for _e_ix, placement in self._state.chain_extra_placements.get(
            chain_ix, [],
        ):
            placements.append(placement)
        for p in placements:
            slot = int(getattr(p, "slot", -1))
            if 0 <= slot <= 0xFFFF:
                mapping[slot] = int(p.overworld_sprite_id)
        return mapping

    def _sprite_marker_pixmap_for(self, sprite_id: int):
        """ow-id → 32x32 marker pixmap, or ``None`` for unmapped ids."""
        try:
            return self._session.mchr_sprite_pixmap_by_ow_id(
                int(sprite_id), max_size=512, frame=None,
            )
        except (AttributeError, ValueError):
            return None

    # ---- canvas highlight sync -------------------------------------------

    def _highlight_chain_on_canvas(self, chain: Optional[CutsceneChain]) -> None:
        """Select the on-canvas item that owns ``chain``'s trigger.

        OWS chains point at an OVERWORLD_SPRITE block (sprite marker);
        EXIT_ZONE / HITBOX chains point at a 0x001b block (exit/hitbox
        zone). All three labels embed the block offset as ``@0x{p:04x}``,
        so we forward that to the right canvas selection method.
        Chain kinds without a 2D location (handler / cross-script /
        base) just clear the selection.
        """
        if chain is None:
            self._canvas.scene().clearSelection()
            return
        offset = _parse_trigger_at_offset(chain.trigger_label)
        if offset is None:
            self._canvas.scene().clearSelection()
            return
        if chain.trigger_kind == TriggerKind.OWS:
            self._canvas.select_marker(offset)
        elif chain.trigger_kind in (TriggerKind.EXIT, TriggerKind.HITBOX):
            self._canvas.select_exit(offset)
        else:
            self._canvas.scene().clearSelection()

    # ---- detail panel rendering ------------------------------------------

    _KIND_NAMES = {
        TriggerKind.OWS:     "NPC dialogs",
        TriggerKind.EXIT:    "Map exits",
        TriggerKind.HITBOX:  "Hitboxes",
        TriggerKind.HANDLER: "Scripted handlers",
        TriggerKind.EXT:     "Cross-script callers",
        TriggerKind.OTHER:   "Other",
        TriggerKind.HEADER:  "Headers",
    }

    # ---- widget-layout helpers (detail panel) ----------------------------

    def _clear_detail_layout(self) -> None:
        """Drop every widget the detail panel is currently showing.

        Flushes pending body-edit timers on each outgoing ``_DialogCard``
        first so a mid-typing burst still lands on the undo stack before
        the card is torn down. The trailing stretch (added once in
        ``_build_ui``) is preserved so freshly-added sections always sit
        flush with the top.
        """
        for card in self._event_cards:
            card.flush_pending()
        self._event_cards.clear()
        # Walk in reverse, leaving the trailing stretch (a QSpacerItem,
        # not a widget) in place. ``takeAt`` detaches each child; widgets
        # get scheduled for deletion via ``deleteLater``. The sprite
        # editor card gets torn down along with everything else — its
        # reference is cleared so ``_hide_sprite_editor`` no-ops.
        layout = self._detail_layout
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            w = item.widget()
            if w is None:
                continue
            layout.takeAt(i)
            w.setParent(None)
            w.deleteLater()
        self._sprite_editor_card = None
        self._exit_editor_card = None
        self._chest_editor_card = None
        # The events browser widgets are children of the detail layout
        # that we just tore down; drop the references so a stale list
        # widget can't be reused across chain switches.
        self._events_list_widget = None
        self._events_card_container = None
        self._events_card_layout = None
        self._events_row_data = []

    def _add_html_section(self, html_str: str) -> None:
        """Append a rich-text QLabel block to the detail layout.

        Used for read-only narrative content (chain header, path,
        handler summary, unknown-opcode notes). The label sits before
        the trailing stretch so the column grows top-down as we add.
        """
        label = QLabel(_HTML_STYLE + html_str)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._detail_layout.insertWidget(
            self._detail_layout.count() - 1, label,
        )

    def _add_dialog_card(
        self, entry_ix: int, dialog_block, alt: bool,
    ) -> None:
        """Append a native editable ``_DialogCard`` to the detail layout."""
        self._install_event_card(_DialogCard(
            self._session,
            self._undo_stack,
            entry_ix,
            dialog_block,
            alt,
            self._detail_container,
        ))

    def _add_music_card(
        self, entry_ix: int, music_block, alt: bool,
    ) -> None:
        self._install_event_card(_MusicCard(
            self._session,
            self._undo_stack,
            entry_ix,
            music_block,
            alt,
            self._detail_container,
        ))

    def _add_reaction_card(
        self, entry_ix: int, reaction_block, alt: bool,
    ) -> None:
        self._install_event_card(_ReactionCard(
            self._session,
            self._undo_stack,
            entry_ix,
            reaction_block,
            alt,
            self._detail_container,
        ))

    def _add_battle_card(
        self, entry_ix: int, battle_block, alt: bool,
    ) -> None:
        self._install_event_card(_BattleCard(
            self._session,
            self._undo_stack,
            entry_ix,
            battle_block,
            alt,
            self._detail_container,
        ))

    def _install_event_card(self, card: QFrame) -> None:
        """Register + place a freshly-built event card in the detail column."""
        self._event_cards.append(card)
        self._detail_layout.insertWidget(
            self._detail_layout.count() - 1, card,
        )

    # Tier-1 scene-action cards → their (payload class is already parsed
    # by the walker) constructors. Keyed by event kind so both the inline
    # column (_add_event_card) and the events browser (_card_for_event)
    # share one dispatch table.
    _EVENT_CARD_CTORS = {
        overlay5_mod.EVENT_KIND_WAIT: _WaitCard,
        overlay5_mod.EVENT_KIND_SPRITE_ANIM: _SpriteAnimCard,
        overlay5_mod.EVENT_KIND_CAMERA: _CameraCard,
        overlay5_mod.EVENT_KIND_ITEM: _ItemEventCard,
        overlay5_mod.EVENT_KIND_MOVE: _MoveCard,
        overlay5_mod.EVENT_KIND_CONTROL: _ControlCard,
    }

    def _add_event_card(
        self, entry_ix: int, event, alt: bool,
    ) -> None:
        """Dispatch to the right card constructor for ``event.kind``.

        Unknown kinds are ignored — a defensive fallthrough for a codec
        that adds a kind before the UI catches up.
        """
        payload = event.payload
        if event.kind == overlay5_mod.EVENT_KIND_DIALOG:
            self._add_dialog_card(entry_ix, payload, alt)
        elif event.kind == overlay5_mod.EVENT_KIND_SET_MUSIC:
            self._add_music_card(entry_ix, payload, alt)
        elif event.kind == overlay5_mod.EVENT_KIND_REACTION:
            self._add_reaction_card(entry_ix, payload, alt)
        elif event.kind == overlay5_mod.EVENT_KIND_BATTLE:
            self._add_battle_card(entry_ix, payload, alt)
        else:
            ctor = self._EVENT_CARD_CTORS.get(event.kind)
            if ctor is not None:
                self._install_event_card(ctor(
                    self._session, self._undo_stack, entry_ix, payload,
                    alt, self._detail_container, self._slot_labels,
                    self._referenced_slots, self._slot_ow_ids,
                ))

    # ---- events browser (list of sub-events + active card slot) ---------

    def _ensure_events_browser(self) -> None:
        """Lazily build the events browser once per chain render.

        The browser is a QListWidget (rows = sub-events) + a container
        QFrame that holds the currently-selected event's card. Both
        get inserted at the current bottom of the detail column so
        subsequent unmapped-opcode / footer sections still land below.
        """
        if self._events_list_widget is not None:
            return
        listw = QListWidget()
        listw.setUniformItemSizes(False)
        listw.setMaximumHeight(220)
        listw.setTextElideMode(Qt.ElideRight)
        listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        listw.setStyleSheet(
            "QListWidget { background: #1e1e21; color: #e8e8e8;"
            " border: 1px solid #1f1f23; padding: 2px; }"
            "QListWidget::item { padding: 2px 4px; }"
            "QListWidget::item:selected"
            " { background: #094771; color: white; }"
        )
        listw.currentRowChanged.connect(self._on_events_row_changed)
        self._events_list_widget = listw
        self._detail_layout.insertWidget(
            self._detail_layout.count() - 1, listw,
        )

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._events_card_container = container
        self._events_card_layout = cl
        self._detail_layout.insertWidget(
            self._detail_layout.count() - 1, container,
        )

    def _events_browser_add_event_row(
        self,
        entry_ix: int,
        event,
        npc_prefix: Optional[str] = None,
        indent: int = 0,
    ) -> None:
        """Append one clickable row to the events browser list.

        ``indent`` (from :func:`_compute_event_indents`) shifts events gated by
        a preceding CMP_BRANCH one step right so the list reads like indented
        code (``if var … :`` then its gated block)."""
        assert self._events_list_widget is not None
        icon, preview = _event_row_preview(
            self._session, event, self._slot_labels,
        )
        text = f"{icon}  {preview}"
        if npc_prefix:
            text = f"{icon}  [{npc_prefix}] {preview}"
        if indent > 0:
            text = ("    " * indent) + text
        item = QListWidgetItem(text)
        item.setData(_EVENTS_ROW_ENTRY_IX_ROLE, int(entry_ix))
        item.setData(_EVENTS_ROW_EVENT_REL_ROLE, int(event.rel))
        item.setData(_EVENTS_ROW_EVENT_KIND_ROLE, str(event.kind))
        self._events_list_widget.addItem(item)
        self._events_row_data.append((entry_ix, event))

    def _events_browser_add_group_header(self, label: str) -> None:
        """Add a non-selectable header row (used for spawned-NPC groups)."""
        assert self._events_list_widget is not None
        item = QListWidgetItem(f"— {label} —")
        item.setFlags(Qt.NoItemFlags)  # non-selectable, non-enabled
        item.setForeground(QColor("#888"))
        self._events_list_widget.addItem(item)
        self._events_row_data.append(None)

    def _on_events_row_changed(self, row: int) -> None:
        """Swap the active card in the container when a list row is picked.

        Row -1 (empty selection) clears the container.  Header rows
        (whose parallel entry is ``None`` in ``_events_row_data``) are
        also ignored — Qt.NoItemFlags means the user can't actually
        land on them, but the guard is cheap.
        """
        if self._events_card_layout is None:
            return
        # Flush + drop any prior card in the container.
        for i in reversed(range(self._events_card_layout.count())):
            item = self._events_card_layout.itemAt(i)
            w = item.widget()
            if w is None:
                continue
            if w in self._event_cards:
                w.flush_pending()
                self._event_cards.remove(w)
            self._events_card_layout.takeAt(i)
            w.setParent(None)
            w.deleteLater()
        if row < 0 or row >= len(self._events_row_data):
            return
        entry = self._events_row_data[row]
        if entry is None:
            return
        entry_ix, event = entry
        card = self._build_event_card(entry_ix, event)
        if card is None:
            return
        self._event_cards.append(card)
        self._events_card_layout.addWidget(card)

    def _build_event_card(self, entry_ix: int, event):
        """Instantiate the right card for ``event`` and return the widget.

        Doesn't parent into the detail layout — the browser owns the
        placement. Returns ``None`` for an unrecognized kind so
        forward-compat additions to the codec don't crash the UI.
        """
        payload = event.payload
        if event.kind == overlay5_mod.EVENT_KIND_DIALOG:
            return _DialogCard(
                self._session, self._undo_stack,
                entry_ix, payload, False, self._events_card_container,
            )
        if event.kind == overlay5_mod.EVENT_KIND_SET_MUSIC:
            return _MusicCard(
                self._session, self._undo_stack,
                entry_ix, payload, False, self._events_card_container,
            )
        if event.kind == overlay5_mod.EVENT_KIND_REACTION:
            return _ReactionCard(
                self._session, self._undo_stack,
                entry_ix, payload, False, self._events_card_container,
            )
        if event.kind == overlay5_mod.EVENT_KIND_BATTLE:
            return _BattleCard(
                self._session, self._undo_stack,
                entry_ix, payload, False, self._events_card_container,
            )
        ctor = self._EVENT_CARD_CTORS.get(event.kind)
        if ctor is not None:
            return ctor(
                self._session, self._undo_stack,
                entry_ix, payload, False, self._events_card_container,
                self._slot_labels, self._referenced_slots, self._slot_ow_ids,
            )
        return None

    def _events_browser_select_first_selectable(self) -> None:
        """After populating rows, land on the first non-header entry so
        a card is always visible when the browser is non-empty."""
        for row, entry in enumerate(self._events_row_data):
            if entry is not None:
                if self._events_list_widget is not None:
                    self._events_list_widget.setCurrentRow(row)
                return

    def _show_empty_state(self) -> None:
        self._clear_detail_layout()
        self._status.setText("Select a field map.")

    def _render_base_detail(self) -> None:
        """Detail panel content for the Base (no-scene) selection.

        Shows a summary table of the map's chains grouped by trigger
        kind so the user can see the full inventory at a glance.
        """
        assert self._state is not None
        self._clear_detail_layout()
        parts: List[str] = []
        parts.append(
            f"<div class='hdr'>Map {self._state.map_id}"
            f" <span class='muted'>(entry {self._state.entry_ix:04d})</span></div>"
        )
        if not self._state.chains:
            parts.append("<p class='muted'>No scenes indexed for this map.</p>")
            self._add_html_section("".join(parts))
            return
        buckets: Dict[str, list] = {}
        for c in self._state.chains:
            buckets.setdefault(c.trigger_kind, []).append(c)
        parts.append("<table class='kt'>")
        for kind in _KIND_ORDER:
            bucket = buckets.get(kind, [])
            if not bucket:
                continue
            color = _KIND_COLORS.get(kind, _KIND_COLORS["base"]).name()
            parts.append(
                "<tr>"
                f"<td class='dot'><span style='background:{color}'></span></td>"
                f"<td>{html.escape(self._KIND_NAMES.get(kind, kind))}</td>"
                f"<td class='num'>{len(bucket)}</td>"
                "</tr>"
            )
        parts.append("</table>")
        parts.append(
            "<p class='muted small'>Click a chip above to inspect a scene.</p>"
        )
        self._add_html_section("".join(parts))

    def _render_chain_detail(self, chain: CutsceneChain, chain_ix: int) -> None:
        """Detail panel content for a selected chain.

        Sections:

        * Trigger header — kind label + short summary parsed from the
          trigger_label so the user doesn't have to read the raw
          ``OWS_str slot=N ow_id=...`` form.
        * Path — every region the chain visits, with its short_name
          dimmed. Cross-entry hops show the destination entry id.
        * Dialogs — every DIALOG block we can decode along the chain's
          regions, rendered as inline-editable ``_DialogCard`` widgets
          so the portrait, slot, msg id, and body text can all be
          edited without leaving the panel. Walks every region in
          ``chain.path`` (not just the first) so multi-region dialog
          flows like Dark Gate's tutorial NPCs surface their later
          lines too.
        """
        assert self._state is not None
        self._clear_detail_layout()
        # slot → character map for this scene, so the event cards can offer
        # a "who" picker instead of a bare slot index. Built before the
        # events browser populates (its cards read it at construction).
        self._slot_labels = self._build_slot_labels(chain_ix)
        # Slots this scene's opcodes actually drive — the "who" combos group
        # these under "in this scene" so the reader isn't offered map sprites
        # that aren't part of the selected cutscene.
        self._referenced_slots = self._build_referenced_slots(chain_ix)
        # slot → ow-id for this chain (extras override base) so the sprite-anim
        # card resolves each slot to the correct sprite for its thumbnail/anims.
        self._slot_ow_ids = self._build_slot_ow_ids(chain_ix)
        color = _KIND_COLORS.get(chain.trigger_kind, _KIND_COLORS["base"]).name()

        # Top header block — just the trigger-kind chip + short label so
        # the reader knows which scene they're looking at. Everything
        # else (source-entry subtitle, handler content summary, sprite
        # placement notes, path listing) drops below the events browser
        # so the editable content lands first in the reading order.
        top_html = (
            f"<div class='hdr'>"
            f"<span class='kind' style='background:{color}'>"
            f"{html.escape(self._KIND_NAMES.get(chain.trigger_kind, chain.trigger_kind))}"
            f"</span> "
            f"{html.escape(_short_chip_label(chain, self._state.handler_summaries.get(chain_ix), self._state.map_id))}"
            f"</div>"
        )
        self._add_html_section(top_html)

        # Events browser — sub-events list (dialog / music / battle /
        # reaction) plus a container that renders the currently-selected
        # event's editable card. Spawned NPCs extend the same list with
        # grouping headers so the reader gets one unified view. Both
        # methods return their trailing debug-footer HTML (unmapped
        # opcodes, skipped-NPC counts) instead of emitting it inline
        # so it can land at the very bottom, after the chain-metadata
        # block.
        main_unknowns_html = self._append_dialog_cards_for_chain(chain)
        skipped_html = ""
        npc_unknowns_html = ""
        if chain.trigger_kind == TriggerKind.HANDLER:
            skipped_html, npc_unknowns_html = (
                self._append_spawned_npc_dialog_cards(chain_ix)
            )

        # Bottom "chain metadata" block — trigger label subtitle,
        # handler-content summary, sprite placement notes, and Path
        # region list. Grouped below the events browser so the user's
        # eye lands on the editable events first; the meta context is
        # right there when needed but doesn't push the browser off-screen.
        bottom_parts: List[str] = []
        bottom_parts.append(
            f"<p class='muted small'>{html.escape(chain.trigger_label or '(no trigger info)')}"
            f" &middot; source entry {chain.source_entry_ix:04d}</p>"
        )

        handler_summary = self._state.handler_summaries.get(chain_ix)
        if handler_summary is not None:
            bottom_parts.append(self._render_handler_summary(handler_summary))

        extras = self._state.chain_extras.get(chain_ix, [])
        inherited_parents = self._state.chain_inherited_parents.get(chain_ix, ())
        inherited_total = sum(count for _, _, count in inherited_parents)
        own_count = max(0, len(extras) - inherited_total)
        if own_count:
            bottom_parts.append(
                "<div class='small muted' style='margin:4px 0;'>"
                f"+ {own_count} cutscene-injected sprite placement"
                f"{'s' if own_count != 1 else ''} on the map "
                "(extra NPCs spawned by this scene)."
                "</div>"
            )
        if inherited_parents:
            cite = ", ".join(
                f"entry {pe:04d} +0x{pr:04x} ({n})"
                for pe, pr, n in inherited_parents
            )
            bottom_parts.append(
                "<div class='small muted' style='margin:4px 0;'>"
                f"+ {inherited_total} NPC placement"
                f"{'s' if inherited_total != 1 else ''} inherited from caller "
                f"(this sub-scene reuses sprites spawned by {cite})."
                "</div>"
            )

        cutscene_index = self._session.cutscene_index()
        bottom_parts.append(
            f"<div class='sec-hdr'>Path "
            f"<span class='muted'>({len(chain.path)} region"
            f"{'s' if len(chain.path) != 1 else ''})</span></div>"
        )
        bottom_parts.append("<ol class='path'>")
        for entry_ix, rel in chain.path:
            region = cutscene_index.regions.get((entry_ix, rel))
            short = region.short_name if region else "(unresolved)"
            if _is_map_warp_hop(entry_ix, rel, self._state.entry_ix):
                warp_map = overlay5_mod.map_id_for(entry_ix)
                bottom_parts.append(
                    f"<li><span class='code'>entry {entry_ix:04d} + 0x{rel:04x}</span>"
                    f" <span class='muted'>→ warp to map {warp_map}</span></li>"
                )
                break
            bottom_parts.append(
                f"<li><span class='code'>entry {entry_ix:04d} + 0x{rel:04x}</span>"
                f" <span class='muted'>{html.escape(short)}</span></li>"
            )
        bottom_parts.append("</ol>")

        # Chain metadata + debug (handler summary, path, unmapped opcodes)
        # is useful but noisy while editing — tuck it into a collapsed
        # "Script details" section so the events browser reads first. The
        # detail-layout teardown removes it like any other child widget.
        details = _CollapsibleSection("Script details", self._detail_container)
        details.add_html("".join(bottom_parts))
        for html_str in (skipped_html, main_unknowns_html, npc_unknowns_html):
            if html_str:
                details.add_html(html_str)
        self._detail_layout.insertWidget(
            self._detail_layout.count() - 1, details,
        )

        # Auto-select the first selectable row so a card is always
        # visible when the browser is non-empty. Deferred one tick so
        # any per-card expensive attach (portrait combo model, etc.)
        # lands on the same frame as the surrounding layout paint.
        if self._events_list_widget is not None and self._events_row_data:
            QTimer.singleShot(0, self._events_browser_select_first_selectable)

    def _collect_events_for_chain(
        self,
        chain: CutsceneChain,
        entry_cache: Dict[int, bytes],
    ) -> Tuple[
        List[Tuple[int, "overlay5_mod.RegionEvent"]],
        List[Tuple[int, int, int]],
    ]:
        """Walk ``chain``'s regions and return every editable event
        (dialog / set_music / reaction / battle) plus the unmapped
        opcodes the walker stepped over.

        Shared by :meth:`_append_dialog_cards_for_chain` and the
        spawned-NPC sub-renderer so they walk the same way. ``entry_cache``
        is in/out — callers pre-seed it with bytes they already hold and
        we extend it as new entries get loaded.

        Returns ``(events, unknowns)`` where ``events`` is a list of
        ``(entry_ix, RegionEvent)`` in per-region walk order and
        ``unknowns`` is ``(entry_ix, offset, opcode)``. The unknown list
        surfaces in the detail panel so users can correlate unmapped
        opcode ids with in-game behaviour observed while the cutscene
        plays.

        The warp guard references ``chain.source_entry_ix`` (not the
        currently-displayed map's entry) so the helper stays correct
        when invoked on chains sourced from a *different* entry — e.g.
        a spawned NPC's OWS chain originating in handler entry 0499.
        """
        assert self._state is not None
        cutscene_index = self._session.cutscene_index()
        events: List[Tuple[int, "overlay5_mod.RegionEvent"]] = []
        unknowns: List[Tuple[int, int, int]] = []
        for entry_ix, rel in chain.path:
            if _is_map_warp_hop(entry_ix, rel, chain.source_entry_ix):
                break
            region = cutscene_index.regions.get((entry_ix, rel))
            if region is None:
                continue
            entry_bytes = entry_cache.get(entry_ix)
            if entry_bytes is None:
                try:
                    entry_bytes = self._session.overlay5_entry_bytes(entry_ix)
                except (ValueError, KeyError):
                    entry_bytes = b""
                entry_cache[entry_ix] = entry_bytes
            if not entry_bytes:
                continue
            region_events, region_unknowns = overlay5_mod.iter_region_events_with_meta(
                entry_bytes, rel, region.end_rel,
            )
            for ev in region_events:
                events.append((entry_ix, ev))
            for off, op in region_unknowns:
                unknowns.append((entry_ix, off, op))
        return events, unknowns

    def _collect_dialogs_for_chain(
        self,
        chain: CutsceneChain,
        entry_cache: Dict[int, bytes],
    ) -> Tuple[
        List[Tuple[int, "overlay5_mod.DialogBlock"]],
        List[Tuple[int, int, int]],
    ]:
        """DIALOG-only view of ``_collect_events_for_chain`` for callers
        that need the dialog subset (e.g. spawned-NPC filtering that
        decides whether to render an NPC based on whether it has any
        speaker lines)."""
        events, unknowns = self._collect_events_for_chain(chain, entry_cache)
        dialogs = [
            (entry_ix, ev.payload)
            for entry_ix, ev in events
            if ev.kind == overlay5_mod.EVENT_KIND_DIALOG
        ]
        return dialogs, unknowns

    def _render_unknown_opcodes_section(
        self, unknowns: List[Tuple[int, int, int]],
    ) -> str:
        """Render a per-chain "unmapped opcodes" report as HTML.

        The forgiving dialog walker
        (:func:`overlay5.iter_dialogs_from_with_meta`) records every
        opcode that wasn't in its size table. Surfacing the distinct
        ids — sorted by first encounter — lets the user correlate them
        with in-game behaviour observed while the cutscene plays,
        which is the practical way we've been growing the opcode size
        table. Hidden when the walk was clean.

        Each row shows ``op (count)`` plus the *first* occurrence's
        ``entry@offset`` so the user can drop straight into the
        annotated overlay dump and decode the opcode by hand.
        """
        if not unknowns:
            return ""
        first_by_op: Dict[int, Tuple[int, int]] = {}
        counts: Dict[int, int] = {}
        order: List[int] = []
        for entry_ix, off, op in unknowns:
            if op not in counts:
                order.append(op)
                first_by_op[op] = (entry_ix, off)
            counts[op] = counts.get(op, 0) + 1
        out: List[str] = []
        out.append(
            f"<div class='sec-hdr'>Unmapped opcodes "
            f"<span class='muted'>({len(order)} distinct"
            f" &middot; {len(unknowns)} encounters)</span></div>"
        )
        out.append(
            "<div class='small muted' style='margin:2px 0 4px 0;'>"
            "The dialog walker stepped over these — they aren't in the"
            " opcode size table yet. Cross-reference against in-game"
            " behaviour to identify them; the entry@offset is where"
            " each id is first seen in this chain."
            "</div>"
        )
        out.append("<ul style='margin:2px 0 6px 0; padding-left:18px;'>")
        for op in order:
            entry_ix, off = first_by_op[op]
            count = counts[op]
            count_suffix = (
                f" <span class='muted small'>{count}\u00d7</span>"
                if count > 1 else ""
            )
            out.append(
                f"<li><code>0x{op:04x}</code>{count_suffix}"
                f" <span class='muted small'>first @"
                f" entry {entry_ix:04d} +0x{off:04x}</span></li>"
            )
        out.append("</ul>")
        return "".join(out)

    def _sibling_ows_chains(
        self, chain: CutsceneChain,
    ) -> List[CutsceneChain]:
        """Every OWS chain for the same sprite (source entry + slot + ow_id).

        A sprite placed several times — one placement per story state — has
        one OWS chain each, and each points at that state's dialog handler via
        ``string_ptr``. The deduped Objects list shows only the FIRST
        placement, whose handler is frequently an empty ``END_SCRIPT`` stub
        (the state where the NPC does nothing), so its dialogue looks missing.
        Aggregating the siblings surfaces the state(s) that actually carry the
        lines — e.g. map 96 (entry 0331) Gabumon's dialogue lives in its 2nd
        placement's handler in entry 0499, lost by the dedup otherwise."""
        label = chain.trigger_label or ""
        slot = _between(label, "slot=", " ")
        ow = _between(label, "ow_id=", " ")
        if not (slot and ow):
            return [chain]
        try:
            cindex = self._session.cutscene_index()
        except (ValueError, KeyError):
            return [chain]
        marker_slot = f"slot={slot} "
        marker_ow = f"ow_id={ow} "
        out = [
            c for c in cindex.chains
            if c.trigger_kind == TriggerKind.OWS
            and c.source_entry_ix == chain.source_entry_ix
            and marker_slot in (c.trigger_label or "")
            and marker_ow in (c.trigger_label or "")
        ]
        return out or [chain]

    def _append_dialog_cards_for_chain(self, chain: CutsceneChain) -> str:
        """Populate the events browser with the chain's editable events.

        Instead of stacking one card per event down the details column,
        we build a single sub-events list widget (dialog / music /
        reaction / battle in walk order) and let the user pick a row
        to see the corresponding card in a container below. The
        HANDLER-mode "spawned NPC" caller extends the same list with
        per-NPC groups underneath.

        For an OWS sprite we aggregate every placement's handler (see
        :meth:`_sibling_ows_chains`) so a story-state whose 1st placement is
        an empty stub still shows the dialogue carried by another placement.

        Returns the trailing "unmapped opcodes" HTML string (empty when
        the walk was clean) so the caller can position it after the
        chain-metadata block, keeping debug info at the bottom.
        Silently returns "" when no region yields any decodable event
        AND no unknowns were emitted (chain-metadata still renders).
        """
        assert self._state is not None
        entry_cache: Dict[int, bytes] = {self._state.entry_ix: self._state.entry_bytes}
        source_chains = (
            self._sibling_ows_chains(chain)
            if chain.trigger_kind == TriggerKind.OWS else [chain]
        )
        events: List[Tuple[int, "overlay5_mod.RegionEvent"]] = []
        unknowns: List[Tuple[int, int, int]] = []
        seen: set = set()
        for c in source_chains:
            evs, unk = self._collect_events_for_chain(c, entry_cache)
            for entry_ix, ev in evs:
                key = (entry_ix, ev.rel)
                if key in seen:
                    continue
                seen.add(key)
                events.append((entry_ix, ev))
            unknowns.extend(unk)
        if not events:
            return self._render_unknown_opcodes_section(unknowns)
        events.sort(key=lambda x: (x[0], x[1].rel))
        self._add_html_section(_events_section_header(events))
        self._ensure_events_browser()
        indents = _compute_event_indents(events)
        for (entry_ix, ev), indent in zip(events, indents):
            self._events_browser_add_event_row(entry_ix, ev, indent=indent)
        return self._render_unknown_opcodes_section(unknowns)

    def _append_spawned_npc_dialog_cards(
        self, chain_ix: int,
    ) -> Tuple[str, str]:
        """Extend the events browser with per-spawned-NPC event rows.

        Handler regions (typically in entry 0499) contain the SPAWN
        opcodes for cutscene-only NPCs — not the dialog text. Each
        spawned NPC's dialog handler lives in its own region pointed
        to by ``OVERWORLD_SPRITE.string_ptr``, which the cutscene
        index registers as a separate ``OWS_str`` chain. Those OWS
        chains source from the spawn entry (e.g. 0499) and therefore
        bucket under that entry's map (264) rather than the map the
        player is currently on, so walking ``self._state.chains``
        alone won't find them — :attr:`_MapState.ows_chain_by_spawn`
        is an index across the whole cutscene_index that lets us
        resolve ``(spawn_entry_ix, placement.block_offset)`` to the
        right OWS chain.

        Each NPC becomes a disabled ``— NpcName —`` header row in the
        same list widget the main chain feeds, followed by the NPC's
        own events prefixed with the NPC name in the row text. Users
        get one unified list with light grouping instead of a stack
        of per-NPC card decks.

        Returns ``(skipped_msgs_html, unmapped_opcodes_html)`` — both
        empty strings when the walk was clean. The caller positions
        both after the chain-metadata block so debug info stays at the
        bottom of the details column.
        """
        assert self._state is not None
        placements = self._state.chain_extra_placements.get(chain_ix, [])
        if not placements:
            return "", ""

        cutscene_index = self._session.cutscene_index()
        entry_cache: Dict[int, bytes] = {self._state.entry_ix: self._state.entry_bytes}

        per_npc: List[Tuple[
            int,
            "overlay5_mod.OverworldSpritePlacement",
            List[Tuple[int, "overlay5_mod.RegionEvent"]],
        ]] = []
        total_events = 0
        npcs_without_chain = 0
        npcs_without_dialog = 0
        npcs_with_string_ptr_zero = 0
        aggregated_unknowns: List[Tuple[int, int, int]] = []
        for entry_ix, placement in placements:
            if placement.string_ptr == 0:
                npcs_with_string_ptr_zero += 1
                continue
            key = (entry_ix, placement.block_offset)
            ows_chain_ix = self._state.ows_chain_by_spawn.get(key)
            if ows_chain_ix is None:
                npcs_without_chain += 1
                continue
            ows_chain = cutscene_index.chains[ows_chain_ix]
            events, unknowns = self._collect_events_for_chain(
                ows_chain, entry_cache,
            )
            aggregated_unknowns.extend(unknowns)
            if not events:
                npcs_without_dialog += 1
                continue
            per_npc.append((entry_ix, placement, events))
            total_events += len(events)

        rendered_npcs = len(per_npc)
        if (rendered_npcs == 0 and npcs_without_chain == 0
                and npcs_without_dialog == 0
                and npcs_with_string_ptr_zero == 0):
            return "", ""

        if per_npc:
            # If the main chain didn't emit any events, add the section
            # header now — otherwise the events browser exists already
            # from ``_append_dialog_cards_for_chain`` and the NPCs get
            # tacked onto its list under grouping headers.
            if self._events_list_widget is None:
                self._add_html_section(
                    f"<div class='sec-hdr'>Spawned NPC dialogs "
                    f"<span class='muted'>({rendered_npcs} NPC"
                    f"{'s' if rendered_npcs != 1 else ''}"
                    f" &middot; {total_events} event"
                    f"{'s' if total_events != 1 else ''})</span></div>"
                )
                self._ensure_events_browser()
            for entry_ix, placement, events in per_npc:
                sid = placement.overworld_sprite_id
                name = _safe_display_name(self._session, sid)
                header_label = (
                    f"{name}  (slot {placement.slot} "
                    f"· entry {entry_ix:04d} +0x{placement.block_offset:04x})"
                )
                self._events_browser_add_group_header(header_label)
                npc_indents = _compute_event_indents(events)
                for (d_entry_ix, ev), indent in zip(events, npc_indents):
                    self._events_browser_add_event_row(
                        d_entry_ix, ev, npc_prefix=name, indent=indent,
                    )

        skipped_msgs: List[str] = []
        if npcs_with_string_ptr_zero:
            skipped_msgs.append(
                f"{npcs_with_string_ptr_zero} decorative NPC"
                f"{'s' if npcs_with_string_ptr_zero != 1 else ''}"
                f" (no dialog attached)"
            )
        if npcs_without_chain:
            skipped_msgs.append(
                f"{npcs_without_chain} spawned NPC"
                f"{'s have' if npcs_without_chain != 1 else ' has'}"
                f" a string_ptr that doesn't map to a known chain"
            )
        if npcs_without_dialog:
            skipped_msgs.append(
                f"{npcs_without_dialog} spawned NPC"
                f"{'s have' if npcs_without_dialog != 1 else ' has'}"
                f" a chain with no decoded events"
            )
        skipped_html = ""
        if skipped_msgs:
            skipped_html = (
                "<div class='small muted' style='margin:2px 0 6px 0;'>"
                + " &middot; ".join(skipped_msgs)
                + ".</div>"
            )
        unknowns_html = self._render_unknown_opcodes_section(aggregated_unknowns)
        return skipped_html, unknowns_html

    def _render_handler_summary(self, summary: _HandlerSummary) -> str:
        """Render the handler-specific 'what this scene does' block.

        Three optional sub-sections — only what's present shows up:

        * Speaker breakdown with portrait names + line counts, plus an
          opening-line preview so the user can confirm the cutscene at
          a glance.
        * Battle setup — the 5 enemy slots from the first ``DA 00``,
          resolved to names. Empty slots render dimmed.
        * Prologue ``HANDLER_META`` fingerprint — raw cond / val /
          opaque triple. Semantics aren't known, but identical triples
          across handlers indicate a shared trigger condition.
        """
        out: List[str] = []
        out.append("<div class='sec-hdr'>Handler content</div>")
        wrote_any = False
        if summary.speakers:
            wrote_any = True
            out.append("<p class='small muted' style='margin:2px 0;'>")
            out.append(
                f"{summary.dialog_count} dialog"
                f"{'s' if summary.dialog_count != 1 else ''}"
            )
            out.append(
                f" &middot; {len(summary.speakers)} speaker"
                f"{'s' if len(summary.speakers) != 1 else ''}"
            )
            out.append("</p>")
            out.append("<ul style='margin:2px 0 6px 0; padding-left:18px;'>")
            for pid, name, count in summary.speakers:
                out.append(
                    f"<li><b>{html.escape(name)}</b>"
                    f" <span class='muted small'>portrait 0x{pid:04x}"
                    f" &middot; {count} line{'s' if count != 1 else ''}</span></li>"
                )
            out.append("</ul>")
            if summary.first_line:
                preview = summary.first_line.replace("\n", " ")
                if len(preview) > 100:
                    preview = preview[:97] + "..."
                out.append(
                    "<div class='small muted' style='margin:0 0 6px 0;'>"
                    f"Opens with: \u201c{_format_dialog_text(preview)}\u201d"
                    "</div>"
                )
        if summary.battle_enemies:
            wrote_any = True
            out.append(
                "<p class='small' style='margin:2px 0;'>"
                "<b>Battle setup</b></p>"
            )
            out.append("<ol style='margin:2px 0 6px 0; padding-left:22px;'>")
            for eid, ename in summary.battle_enemies:
                if eid and eid != 0xFFFF:
                    out.append(
                        f"<li>{html.escape(ename)}"
                        f" <span class='muted small'>0x{eid:04x}</span></li>"
                    )
                else:
                    out.append(
                        "<li><span class='muted'>(empty slot)</span></li>"
                    )
            out.append("</ol>")
        if summary.meta_cond is not None:
            wrote_any = True
            out.append(
                "<div class='small muted' style='margin:2px 0 6px 0;'>"
                f"HANDLER_META"
                f" &middot; cond 0x{summary.meta_cond:04x}"
                f" &middot; val 0x{summary.meta_val:04x}"
                f" &middot; opaque 0x{summary.meta_opaque:08x}"
                "</div>"
            )
        if not wrote_any:
            out.append(
                "<p class='small muted'>"
                "No dialogs or battles decoded inside the chain. "
                "The handler may use opcodes the walker doesn't recognize yet."
                "</p>"
            )
        return "".join(out)

    # ---- objects list ----------------------------------------------------

    def _add_object_sprite_row(self, s, chip_ix: int) -> None:
        """Append one sprite row to the Objects list (shared by the flat and
        grouped paths)."""
        item = QListWidgetItem()
        item.setText(f"0x{s.overworld_sprite_id:04x}  ({s.x}, {s.y})")
        if s.pixmap is not None and not s.pixmap.isNull():
            item.setIcon(QIcon(s.pixmap))
        else:
            item.setIcon(self._sprite_fallback_icon())
        item.setData(Qt.UserRole, s.block_offset)
        item.setData(_LIST_ROW_TYPE_ROLE, _LIST_ROW_SPRITE)
        tooltip = s.label
        if (
            chip_ix < 0
            and s.block_offset not in self._state.ows_chain_by_offset
        ):
            tooltip += "\n(no scripted scene attached)"
        item.setToolTip(tooltip)
        self._objects_list.addItem(item)

    def _add_object_group_header(
        self, label: str, block_offsets: List[int], tooltip: str,
        group_key,
    ) -> bool:
        """A non-selectable, checkable header row.

        The checkbox toggles the canvas visibility of the group's placements
        (editor-only — no effect on the game). Double-clicking the header
        collapses/expands the group in the list. Returns ``True`` when the
        group is currently collapsed (caller then skips its child rows)."""
        offs = [int(o) for o in block_offsets]
        visible = not (
            offs and all(o in self._hidden_sprite_offsets for o in offs)
        )
        collapsed = group_key in self._collapsed_groups
        arrow = "▸ " if collapsed else "▾ "  # ▸ / ▾
        item = QListWidgetItem(arrow + label)
        item.setData(_LIST_ROW_TYPE_ROLE, _LIST_ROW_GROUP)
        item.setData(_LIST_ROW_GROUP_OFFSETS, offs)
        item.setData(_LIST_ROW_GROUP_KEY, group_key)
        item.setToolTip(
            tooltip
            + "\n\n☑ = show on the canvas — editor view only,"
            " no effect on the game.\nDouble-click to collapse / expand."
        )
        item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        item.setCheckState(Qt.Checked if visible else Qt.Unchecked)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor("#9cdcfe"))
        self._objects_list.addItem(item)
        return collapsed

    def _on_objects_group_double_clicked(self, item) -> None:
        """Header double-click → collapse/expand that group in the list."""
        if self._state is None:
            return
        if item.data(_LIST_ROW_TYPE_ROLE) != _LIST_ROW_GROUP:
            return
        key = item.data(_LIST_ROW_GROUP_KEY)
        if key in self._collapsed_groups:
            self._collapsed_groups.discard(key)
        else:
            self._collapsed_groups.add(key)
        self._populate_objects_list()

    def _populate_object_sprite_groups(self, sprite_specs: list) -> None:
        """Base-mode sprite listing grouped by spawn condition.

        ``overlay5.sprite_gate_conditions`` maps each placement gated by a
        conditional (0x06 IFEQ / 0x2A CMP directly before the 0x0150) to that
        condition. Unconditional sprites go under an "Always on map" header;
        each distinct condition gets its own checkbox group (short id +
        raw-condition tooltip). When no sprite is conditional, the list stays
        a plain flat row set."""
        try:
            gates = overlay5_mod.sprite_gate_conditions(self._state.entry_bytes)
        except (ValueError, AttributeError):
            gates = {}
        always: list = []
        groups: Dict[int, list] = {}
        group_op: Dict[int, int] = {}
        order: List[int] = []
        for s in sprite_specs:
            cond = gates.get(s.block_offset)
            if cond is None:
                always.append(s)
            else:
                op, flag = cond
                if flag not in groups:
                    groups[flag] = []
                    group_op[flag] = op
                    order.append(flag)
                groups[flag].append(s)
        if not groups:
            # No conditional sprites — flat list, then the zones.
            for s in sprite_specs:
                self._add_object_sprite_row(s, -1)
            self._add_object_zone_rows()
            return
        # Always group holds the ungated sprites AND the (never-gated) exit /
        # hitbox / spawn zones, so those don't read as part of a flag group.
        # Its checkbox governs only the sprite offsets (zones aren't hideable).
        collapsed = self._add_object_group_header(
            f"Always on map  ({len(always)})",
            [s.block_offset for s in always],
            "Sprites with no detected spawn gate, plus the map's zones.",
            "always",
        )
        if not collapsed:
            for s in always:
                self._add_object_sprite_row(s, -1)
            self._add_object_zone_rows()
        gate_names = {0x0006: "IFEQ", 0x00A6: "HANDLER_META", 0x002A: "CMP"}
        for flag in order:
            specs = groups[flag]
            opname = gate_names.get(group_op[flag], f"op 0x{group_op[flag]:02x}")
            header = f"Flag · 0x{flag:04x}  ({len(specs)})"
            tip = (
                "Spawns only when this story flag is set\n"
                f"gate {opname} · flag 0x{flag:04x}"
            )
            collapsed = self._add_object_group_header(
                header, [s.block_offset for s in specs], tip, flag,
            )
            if not collapsed:
                for s in specs:
                    self._add_object_sprite_row(s, -1)

    def _on_objects_group_toggled(self, item) -> None:
        """A group-header checkbox flipped — hide/show its placements on the
        canvas. Sprite/exit rows and programmatic populates are ignored."""
        if self._selection_syncing or self._state is None:
            return
        if item.data(_LIST_ROW_TYPE_ROLE) != _LIST_ROW_GROUP:
            return
        offs = item.data(_LIST_ROW_GROUP_OFFSETS) or []
        hide = item.checkState() != Qt.Checked
        for o in offs:
            if hide:
                self._hidden_sprite_offsets.add(int(o))
            else:
                self._hidden_sprite_offsets.discard(int(o))
        self._rebuild_canvas_for_selection(self._state.selected_chip_ix)

    def _populate_objects_list(self) -> None:
        """Refill the Objects on Map list for the current selection.

        Chip-aware: in Base mode (selected_chip_ix < 0) shows the full
        per-map inventory (sprites + exits + hitboxes + spawns) — the
        "what does the player normally walk into" snapshot. In chip
        mode (a handler / cross-script chain is active) shows ONLY the
        objects that scene actually composites onto the map, i.e. the
        chain's own ``chain_extras`` sprite list. Exits, hitboxes, and
        spawns are part of the static map layout and don't differ per
        chip, so they're suppressed in chip mode to keep the list
        focused.

        Each row tags its block_offset (``Qt.UserRole``) and row kind
        (``_LIST_ROW_TYPE_ROLE``) so the selection handler can dispatch
        to the right offset→chain map without re-parsing the label.
        """
        assert self._state is not None
        chip_ix = self._state.selected_chip_ix
        if chip_ix < 0:
            sprite_specs = list(self._state.sprite_specs)
            show_zones = True
        else:
            sprite_specs = list(self._state.chain_extras.get(chip_ix, []))
            show_zones = False
        self._selection_syncing = True
        try:
            self._objects_list.clear()
            if chip_ix < 0:
                # Base view: grouped sprites (Always first, then one checkbox
                # group per gating flag). The always-present zones render
                # under the Always group so they don't read as flag-gated.
                self._populate_object_sprite_groups(sprite_specs)
            else:
                for s in sprite_specs:
                    self._add_object_sprite_row(s, chip_ix)
            self._objects_list.setCurrentRow(-1)
        finally:
            self._selection_syncing = False

    def _add_object_zone_rows(self) -> None:
        """Append the map's exit / hitbox / spawn zones. These aren't
        flag-gated, so in grouped mode they belong under "Always on map"."""
        for e in self._state.exit_specs:
            if e.is_spawn or e.is_hitbox:
                continue
            item = QListWidgetItem()
            item.setText(f"Exit {e.display_idx}  \u2192  {e.dest_label or '?'}")
            item.setIcon(self._exit_icon())
            item.setData(Qt.UserRole, e.block_offset)
            item.setData(_LIST_ROW_TYPE_ROLE, _LIST_ROW_EXIT)
            item.setToolTip(
                f"Exit zone (tile {e.x1},{e.y1} \u2014 {e.x2},{e.y2})\n"
                f"to: {e.dest_label or '(unknown)'}"
            )
            self._objects_list.addItem(item)
        for e in self._state.exit_specs:
            if not e.is_hitbox:
                continue
            item = QListWidgetItem()
            item.setText(
                f"Hitbox {e.display_idx}  "
                f"(tile {e.x1},{e.y1} \u2014 {e.x2},{e.y2})"
            )
            item.setIcon(self._hitbox_icon())
            item.setData(Qt.UserRole, e.block_offset)
            item.setData(_LIST_ROW_TYPE_ROLE, _LIST_ROW_HITBOX)
            item.setToolTip(
                f"Interaction hitbox\n"
                f"tile ({e.x1},{e.y1}) \u2014 ({e.x2},{e.y2})\n"
                f"{e.dest_label or ''}".rstrip()
            )
            self._objects_list.addItem(item)
        for e in self._state.exit_specs:
            if not e.is_spawn:
                continue
            item = QListWidgetItem()
            item.setText(f"Spawn {e.display_idx}  ({e.x1}, {e.y1})")
            item.setIcon(self._spawn_icon())
            item.setData(Qt.UserRole, e.block_offset)
            item.setData(_LIST_ROW_TYPE_ROLE, _LIST_ROW_SPAWN)
            item.setToolTip(f"Spawn point (tile {e.x1}, {e.y1})")
            self._objects_list.addItem(item)

    def _lookup_chain_for_row(
        self, row_type: str, offset: int,
    ) -> Optional[int]:
        """Resolve an Objects-list row to its owning chain index.

        Spawns never have an attached chain (they're 0x001b prologue
        blocks, not triggers). Sprites/exits/hitboxes may or may not,
        depending on whether the map's overlay5 entry registered a
        chain for that block offset.
        """
        if self._state is None:
            return None
        if row_type == _LIST_ROW_SPRITE:
            return self._state.ows_chain_by_offset.get(offset)
        if row_type == _LIST_ROW_EXIT:
            return self._state.exit_chain_by_offset.get(offset)
        if row_type == _LIST_ROW_HITBOX:
            return self._state.hitbox_chain_by_offset.get(offset)
        return None

    def _classify_exit_offset(self, offset: int) -> str:
        """Decide whether ``offset`` names an exit, hitbox, or spawn.

        Used when the canvas signals an exit selection — the canvas
        only knows it's a 0x001b zone item and doesn't distinguish the
        three sub-kinds. Walk the cached :class:`ExitZoneSpec` list to
        recover the row type.
        """
        if self._state is None:
            return _LIST_ROW_EXIT
        for e in self._state.exit_specs:
            if e.block_offset == offset:
                if e.is_spawn:
                    return _LIST_ROW_SPAWN
                if e.is_hitbox:
                    return _LIST_ROW_HITBOX
                return _LIST_ROW_EXIT
        return _LIST_ROW_EXIT

    def _set_list_current_row(self, row_type: str, offset: int) -> None:
        """Move the Objects list cursor to the row matching ``(row_type,
        offset)`` without firing :meth:`_on_objects_list_selection_changed`.
        """
        self._selection_syncing = True
        try:
            for i in range(self._objects_list.count()):
                item = self._objects_list.item(i)
                if (
                    item.data(Qt.UserRole) == offset
                    and item.data(_LIST_ROW_TYPE_ROLE) == row_type
                ):
                    self._objects_list.setCurrentRow(i)
                    return
            self._objects_list.setCurrentRow(-1)
        finally:
            self._selection_syncing = False

    def _clear_chip_checked_state(self) -> None:
        """Uncheck every chip (including Base) without rebuilding state.

        The chip row stays visible but no chip reads as "active" — the
        Objects list / canvas selection is now the source of truth for
        the detail panel.
        """
        if self._base_chip is not None:
            self._base_chip.setChecked(False)
        for chip in self._chips:
            if chip is not None:
                chip.setChecked(False)

    def _activate_object(self, row_type: str, offset: int) -> None:
        """Common path for "user selected a base-list map object".

        Used only when the Objects list is in Base mode — drops chip
        checked state, switches the canvas backdrop back to Base
        (dropping any chain-extras), and renders the chain detail when
        one is attached (NPC dialog, exit destination, hitbox handler)
        or a minimal no-scene fallback otherwise (spawns, NPCs that
        don't talk). Sprite selections also populate the editor form;
        exits/hitboxes/spawns clear the form (no inline editor for
        those yet).
        """
        if self._state is None:
            return
        self._clear_chip_checked_state()
        if self._state.selected_chip_ix >= 0:
            self._rebuild_canvas_for_selection(-1)
            self._state.selected_chip_ix = -1
            self._populate_objects_list()
        self._state.selected_chip_ix = -1
        chain_ix = self._lookup_chain_for_row(row_type, offset)
        if chain_ix is not None:
            self._render_chain_detail(self._state.chains[chain_ix], chain_ix)
        else:
            self._render_object_no_scene_detail(row_type, offset)
        # Pin the editor card for the active object on top of whatever the
        # detail render produced (both render paths clear the layout first,
        # so this must come after). Sprite and exit cards are mutually
        # exclusive — a selection is one kind of object.
        if row_type == _LIST_ROW_SPRITE:
            self._hide_exit_editor()
            self._show_sprite_editor_for(offset)
            self._show_chest_editor_for(offset)
        elif row_type == _LIST_ROW_EXIT:
            self._hide_sprite_editor()
            self._hide_chest_editor()
            self._show_exit_editor_for(offset)
        else:
            self._hide_sprite_editor()
            self._hide_exit_editor()
            self._hide_chest_editor()

    def _activate_chain_extra_sprite(self, canvas_offset: int) -> None:
        """Chip-mode sprite click: pin the sprite editor card to one of
        the currently-active chain's extras WITHOUT falling back to Base.

        Mirror of :meth:`_activate_object` for the chip path. The
        detail panel stays on the chain's narrative; only the sprite
        editor card at the top swaps to bind the clicked NPC so the
        user can retarget / move them without leaving the scene.
        """
        if self._state is None:
            return
        # Chain-extra sprites are cutscene-spawned NPCs, never chests.
        self._hide_chest_editor()
        self._show_sprite_editor_for(canvas_offset)

    def _on_objects_list_selection_changed(self) -> None:
        if self._selection_syncing or self._state is None:
            return
        item = self._objects_list.currentItem()
        if item is None:
            return
        offset = int(item.data(Qt.UserRole) or 0)
        row_type = str(item.data(_LIST_ROW_TYPE_ROLE) or "")
        # In chip mode the list is showing chain extras; clicking one
        # should populate the form without tearing down the active
        # chip. Base mode goes through the full _activate_object path.
        if self._state.selected_chip_ix >= 0:
            if row_type == _LIST_ROW_SPRITE:
                self._activate_chain_extra_sprite(offset)
                self._canvas.select_marker(offset)
            return
        self._activate_object(row_type, offset)
        # Sync the canvas cursor to the same item. ``select_marker`` /
        # ``select_exit`` suppress their own signals so this won't loop
        # back through the canvas handlers.
        if row_type == _LIST_ROW_SPRITE:
            self._canvas.select_marker(offset)
        else:
            self._canvas.select_exit(offset)

    def _on_canvas_marker_selected(self, block_offset: int) -> None:
        """Canvas told us a sprite item was selected (or deselected).

        Routes synthetic (chain-extra) offsets to the chip-mode form
        binder and real offsets to the base-mode activator. Both end
        up populating the editor form on top so the user can retarget
        the clicked sprite without leaving the current scene context.
        """
        if self._selection_syncing or self._state is None:
            return
        if block_offset < 0:
            # Deselection (or non-marker selection) — leave the
            # list / chip state alone; the exitSelected partner will
            # report the actual new selection if there is one.
            return
        if block_offset >= _CHAIN_EXTRA_OFFSET_BASE:
            # Chain-injected cutscene extra. Bind the form to it,
            # keep the chip selected, sync the list cursor.
            self._set_list_current_row(_LIST_ROW_SPRITE, block_offset)
            self._activate_chain_extra_sprite(block_offset)
            return
        self._set_list_current_row(_LIST_ROW_SPRITE, block_offset)
        # Deferred for the same reason as the exit path — activation can clear
        # the scene, which is unsafe inside ``selectionChanged``.
        QTimer.singleShot(
            0, lambda: self._activate_object(_LIST_ROW_SPRITE, block_offset),
        )

    def _on_canvas_exit_selected(self, block_offset: int) -> None:
        """Canvas told us an exit/hitbox/spawn item was selected."""
        if self._selection_syncing or self._state is None:
            return
        if block_offset < 0:
            return
        row_type = self._classify_exit_offset(block_offset)
        self._set_list_current_row(row_type, block_offset)
        # Defer the activation: in chip mode it rebuilds the canvas scene,
        # which must NOT happen inside the scene's ``selectionChanged``
        # emission — clearing the scene deletes the very item being selected
        # and Qt crashes (access violation). singleShot(0) runs it after the
        # selection event unwinds.
        QTimer.singleShot(
            0, lambda: self._activate_object(row_type, block_offset),
        )

    def _render_object_no_scene_detail(
        self, row_type: str, offset: int,
    ) -> None:
        """Detail panel content for an object with no attached chain.

        Spawns are inherently chainless. NPCs without a chain just walk
        their loop; exits without a chain are unusual but possible (the
        entry block sets up the zone but no REGISTER_HANDLER fires).
        """
        self._clear_detail_layout()
        if self._state is None:
            return
        parts: List[str] = []
        if row_type == _LIST_ROW_SPRITE:
            spec = next(
                (s for s in self._state.sprite_specs
                 if s.block_offset == offset),
                None,
            )
            if spec is None:
                return
            parts.append(
                "<div class='hdr'>NPC <span class='muted'>"
                f"0x{spec.overworld_sprite_id:04x}"
                f" \u00b7 ({spec.x}, {spec.y})</span></div>"
            )
            parts.append(
                f"<p class='muted small'>{html.escape(spec.label)}</p>"
            )
            parts.append(
                "<p class='muted'>No scripted scene is attached to this "
                "sprite slot. It walks its idle loop but doesn't trigger "
                "a dialog or cutscene.</p>"
            )
        elif row_type == _LIST_ROW_SPAWN:
            spec = next(
                (e for e in self._state.exit_specs
                 if e.block_offset == offset),
                None,
            )
            if spec is None:
                return
            parts.append("<div class='hdr'>Spawn point</div>")
            parts.append(
                f"<p class='muted small'>Tile ({spec.x1}, {spec.y1})"
                f" \u00b7 entry +0x{spec.block_offset:04x}</p>"
            )
            parts.append(
                "<p class='muted'>Player arrives at this tile when "
                "entering the map from a connected exit. Spawn points "
                "are placement data only \u2014 no cutscene chain "
                "attaches to them.</p>"
            )
        elif row_type in (_LIST_ROW_EXIT, _LIST_ROW_HITBOX):
            spec = next(
                (e for e in self._state.exit_specs
                 if e.block_offset == offset),
                None,
            )
            if spec is None:
                return
            kind = "Exit zone" if row_type == _LIST_ROW_EXIT else "Hitbox"
            parts.append(f"<div class='hdr'>{kind}</div>")
            parts.append(
                f"<p class='muted small'>Tile ({spec.x1},{spec.y1})"
                f" \u2014 ({spec.x2},{spec.y2})"
                f" \u00b7 entry +0x{spec.block_offset:04x}</p>"
            )
            if spec.dest_label:
                parts.append(
                    f"<p>{html.escape(spec.dest_label)}</p>"
                )
            parts.append(
                "<p class='muted'>No cutscene chain is registered for "
                "this zone.</p>"
            )
        else:
            parts.append("<p class='muted'>(no detail)</p>")
        self._add_html_section("".join(parts))

    # ---- list-row icon builders -----------------------------------------

    def _sprite_fallback_icon(self) -> QIcon:
        """Neutral grey square for sprite rows missing a pixmap."""
        if self._icon_sprite_fallback is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(80, 80, 100))
            self._icon_sprite_fallback = QIcon(pm)
        return self._icon_sprite_fallback

    def _exit_icon(self) -> QIcon:
        """Cached blue-rectangle swatch for exit-zone rows.

        Same hue family as the Events tab's exit icon so the two read
        as the same kind of object across tabs.
        """
        if self._icon_exit is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(80, 180, 255, 240), 2))
            p.setBrush(QBrush(QColor(80, 180, 255, 90)))
            p.drawRect(4, 8, 24, 16)
            p.end()
            self._icon_exit = QIcon(pm)
        return self._icon_exit

    def _hitbox_icon(self) -> QIcon:
        """Cached orange-rectangle swatch for interaction-hitbox rows."""
        if self._icon_hitbox is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(255, 165, 0, 240), 2))
            p.setBrush(QBrush(QColor(255, 165, 0, 90)))
            p.drawRect(4, 8, 24, 16)
            p.end()
            self._icon_hitbox = QIcon(pm)
        return self._icon_hitbox

    def _spawn_icon(self) -> QIcon:
        """Cached green-diamond swatch for spawn-point rows."""
        if self._icon_spawn is None:
            pm = QPixmap(32, 32)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(60, 180, 60, 240), 2))
            p.setBrush(QBrush(QColor(120, 220, 120, 150)))
            p.drawPolygon([
                QPoint(16, 4), QPoint(28, 16),
                QPoint(16, 28), QPoint(4, 16),
            ])
            p.end()
            self._icon_spawn = QIcon(pm)
        return self._icon_spawn


def battle_vram_issues(session) -> List[ValidationIssue]:
    """Validation collector: scripted battles whose enemy sprites overflow the
    shared OBJ VRAM pool.

    Scripted battles skip the wild roll's ``Σ fs ≤ 1440`` cap, so an over-
    budget enemy set overflows VRAM and crashes on load. This is surfaced in
    the validation footer only — it does NOT gate saving (mirrors the
    informational ``sprite VRAM`` label on each battle card). Enemy ids are
    read live per block so the list tracks edits; block positions come from
    the cached :meth:`RomSession.scripted_battle_positions`.
    """
    from digimon_core import overlay5 as ov5
    from digimon_core.map_labels import area_name

    budget = BATTLE_SPRITE_TILE_BUDGET
    issues: List[ValidationIssue] = []
    for entry_ix, block_offset, chain_ix, _source_ix, map_id in \
            session.scripted_battle_positions():
        block = ov5._parse_battle_at(
            session.overlay5_entry_bytes(entry_ix), block_offset,
        )
        if block is None:
            continue
        total = 0
        for eid_raw in block.enemies:
            eid = int(eid_raw) & 0xFFFF
            if eid == ov5.BATTLE_ENEMY_EMPTY:
                continue
            fs = session.digimon_battle_footprint(eid)
            if fs:
                total += fs
        if total <= budget:
            continue
        where = area_name(map_id) if map_id is not None else f"entry 0x{entry_ix:04X}"
        issues.append(ValidationIssue(
            section="Scripted Battles",
            category="Sprite VRAM",
            message=(
                f"{where} (entry 0x{entry_ix:04X}+0x{block_offset:04X}): enemy "
                f"sprites total {total} tiles, over the {budget}-tile VRAM pool "
                "— this battle can crash on load."
            ),
            editor_key="cutscene_battle" if chain_ix >= 0 else "map_browser",
            record_id=chain_ix if chain_ix >= 0 else None,
        ))
    return issues
