"""Digivolution tree viewer — read-only panel showing forward evolution
trees rooted at digimon with no pre-evolution.

Mirrors DWDDRandomizer's DigivolutionTreeLogger: roots are digimon whose
`degen_evo_id` is the empty-slot sentinel or points to a record outside
the set, and traversal uses a per-tree visited set to dedupe shared
targets (in/training → rookie → champion graphs are DAG-shaped, so
without dedupe each shared mega would re-expand its whole subtree).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QPointF, QRectF, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from digimon_core import constants, model

from .form_helpers import NO_EVO_SENTINEL, digimon_name, digimon_stage_rank


_EVO_ATTRS = ("evolution_1_id", "evolution_2_id", "evolution_3_id")

# Per-slot (cond_id_attr, cond_value_attr) triples — indexed by slot 0..2
# matching evolution_{1,2,3}_id above so we can format the edge gating each
# child node directly from the parent record.
_COND_ATTRS = [
    [(f"evo_{n}_condition_id_{i}", f"evo_{n}_condition_value_{i}") for i in (1, 2, 3)]
    for n in (1, 2, 3)
]

# Condition codes whose value semantically references a digimon id instead
# of a numeric stat/level — render the partner's name rather than the raw id.
_DIGIMON_VALUED_CONDITIONS = {0x15, 0x16}
_FRIENDSHIP_CONDITION = 0x12
_NONE_CONDITION = 0x0


def _find_roots(records: Dict[int, model.StandardDigivolution]) -> List[int]:
    roots: List[int] = []
    for did, rec in records.items():
        degen = rec.degen_evo_id
        if degen == NO_EVO_SENTINEL or degen not in records:
            roots.append(did)
    roots.sort()
    return roots


def _walk_descendants(
    root_id: int,
    records: Dict[int, model.StandardDigivolution],
) -> List[int]:
    """Return every digimon reachable from `root_id` via evolution edges,
    using the same per-tree dedupe as the renderer so the search index and
    the rendered tree agree on coverage."""
    out: List[int] = []
    visited: Set[int] = set()

    def visit(did: int) -> None:
        if did in visited:
            return
        visited.add(did)
        out.append(did)
        rec = records.get(did)
        if rec is None:
            return
        for evo_attr in _EVO_ATTRS:
            target = getattr(rec, evo_attr)
            if target != NO_EVO_SENTINEL:
                visit(target)

    visit(root_id)
    return out


# Custom Qt.UserRole offsets. UserRole holds the root id (used by the row's
# selection handler); UserRole+1 holds the search blob ("Koromon Agumon
# Greymon ...") so the proxy can match on any descendant's name.
ROLE_ROOT_ID = Qt.UserRole
ROLE_SEARCH_BLOB = Qt.UserRole + 1


class _DescendantFilterProxy(QSortFilterProxyModel):
    """Filter that matches against the search blob (all descendant names),
    not just the visible label, so typing 'Greymon' surfaces the root whose
    tree contains Greymon even if the root is Koromon."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle: str = ""

    def set_needle(self, text: str) -> None:
        self._needle = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: D401
        if not self._needle:
            return True
        source_model = self.sourceModel()
        index = source_model.index(source_row, 0, source_parent)
        blob = index.data(ROLE_SEARCH_BLOB) or ""
        return self._needle in blob.lower()


def _format_condition(cond_id: int, cond_value: int) -> str:
    name = constants.DIGIVOLUTION_CONDITIONS.get(cond_id, f"UNKNOWN_0x{cond_id:x}")
    if cond_id in _DIGIMON_VALUED_CONDITIONS:
        return f"{name} {digimon_name(cond_value)}"
    if cond_id == _FRIENDSHIP_CONDITION:
        return f"{name} {cond_value}%"
    return f"{name} {cond_value}"


def _format_edge_conditions(rec: model.StandardDigivolution, slot_index: int) -> str:
    parts: List[str] = []
    for cid_attr, cval_attr in _COND_ATTRS[slot_index]:
        cid = getattr(rec, cid_attr)
        if cid == _NONE_CONDITION:
            continue
        parts.append(_format_condition(cid, getattr(rec, cval_attr)))
    return ", ".join(parts)


class DigivolutionTreeEditor(QWidget):
    """Read-only tree viewer. Double-clicking a node emits `digimonActivated`
    with the clicked digimon's id so the main window can route to the
    StandardDigivolution editor for editing."""

    digimonActivated = Signal(int)

    def __init__(
        self,
        records: Dict[int, model.StandardDigivolution],
        session=None,
        parent=None,
    ):
        super().__init__(parent)
        self._records = records
        self._session = session

        roots = _find_roots(records)

        # Reverse index: any digimon id reachable from a root → that root.
        # Populated from the same per-tree dedupe walk the renderer uses, so
        # `select_by_id` can find the canonical root for any descendant.
        self._root_for_descendant: Dict[int, int] = {}
        self._row_for_root: Dict[int, int] = {}

        self._source_model = QStandardItemModel(self)
        for row_ix, did in enumerate(roots):
            descendants = _walk_descendants(did, records)
            search_blob = " ".join(
                f"0x{d:03x} {digimon_name(d)}" for d in descendants
            )
            item = QStandardItem(f"0x{did:03x} — {digimon_name(did)}")
            item.setEditable(False)
            item.setData(did, ROLE_ROOT_ID)
            item.setData(search_blob, ROLE_SEARCH_BLOB)
            self._source_model.appendRow(item)
            self._row_for_root[did] = row_ix
            for d in descendants:
                # First-wins keeps reverse lookup stable when a digimon appears
                # in multiple trees (the renderer dedupes per-tree, so only the
                # first containing root will actually show that node anyway).
                self._root_for_descendant.setdefault(d, did)

        self._proxy = _DescendantFilterProxy(self)
        self._proxy.setSourceModel(self._source_model)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("Filter by any digimon in the tree…")
        self._filter_box.textChanged.connect(self._proxy.set_needle)

        self._root_list = QListView()
        self._root_list.setModel(self._proxy)
        self._root_list.setUniformItemSizes(True)
        self._root_list.setEditTriggers(QListView.NoEditTriggers)
        self._root_list.selectionModel().currentChanged.connect(self._on_root_changed)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(self._filter_box)
        left_layout.addWidget(self._root_list)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Digimon", "Conditions"])
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(self._on_double_click)

        expand_btn = QPushButton("Expand all")
        collapse_btn = QPushButton("Collapse all")
        expand_btn.clicked.connect(self._tree.expandAll)
        collapse_btn.clicked.connect(self._tree.collapseAll)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.addWidget(expand_btn)
        toolbar_layout.addWidget(collapse_btn)
        toolbar_layout.addStretch(1)

        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(toolbar)
        text_layout.addWidget(self._tree)

        # Graphical view: overworld-sprite nodes laid out by stage with
        # bezier connectors. Built per-root in `_on_root_changed`.
        self._graph_view = _TreeGraphView(records, session)
        self._graph_view.digimonActivated.connect(self.digimonActivated)

        # Stack text on top, graph below — both visible at once, with a
        # draggable splitter so the user can give either pane more room.
        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(text_page)
        right_split.addWidget(self._graph_view)
        right_split.setStretchFactor(0, 1)
        right_split.setStretchFactor(1, 1)
        right_split.setSizes([300, 300])

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.addWidget(right_split)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        if roots:
            self._root_list.setCurrentIndex(self._proxy.index(0, 0))

    def _on_root_changed(self, current, _previous) -> None:
        if not current.isValid():
            self._tree.clear()
            self._graph_view.clear()
            return
        did = current.data(ROLE_ROOT_ID)
        if did is None:
            return
        self._render_tree(int(did))
        self._graph_view.render_tree(int(did))

    def _render_tree(self, root_id: int) -> None:
        self._tree.clear()
        visited: Set[int] = set()
        item = self._build_node(root_id, "", visited)
        if item is None:
            return
        self._tree.addTopLevelItem(item)
        self._tree.expandAll()
        self._tree.resizeColumnToContents(0)

    def _build_node(
        self,
        digimon_id: int,
        edge_conditions: str,
        visited: Set[int],
    ) -> Optional[QTreeWidgetItem]:
        if digimon_id in visited:
            return None
        visited.add(digimon_id)
        item = QTreeWidgetItem([digimon_name(digimon_id), edge_conditions])
        item.setData(0, Qt.UserRole, digimon_id)
        rec = self._records.get(digimon_id)
        if rec is None:
            return item
        for slot_ix, evo_attr in enumerate(_EVO_ATTRS):
            target = getattr(rec, evo_attr)
            if target == NO_EVO_SENTINEL:
                continue
            child = self._build_node(target, _format_edge_conditions(rec, slot_ix), visited)
            if child is not None:
                item.addChild(child)
        return item

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        did = item.data(0, Qt.UserRole)
        if did is not None:
            self.digimonActivated.emit(int(did))

    def select_by_id(self, digimon_id: int) -> bool:
        """Locate `digimon_id` somewhere in the forest: select its containing
        root in the left list, then expand and highlight its node in the tree.
        Clears any active filter so the row is reachable. Returns False if the
        id has no tree (no root in the std digivolution graph reaches it)."""
        root_id = self._root_for_descendant.get(digimon_id)
        if root_id is None:
            return False
        row = self._row_for_root.get(root_id)
        if row is None:
            return False
        self._filter_box.clear()
        src_index = self._source_model.index(row, 0)
        proxy_index = self._proxy.mapFromSource(src_index)
        if not proxy_index.isValid():
            return False
        # setCurrentIndex fires the selectionModel's currentChanged signal,
        # which in turn calls _render_tree — so the QTreeWidget is populated
        # before we walk it looking for the target node.
        self._root_list.setCurrentIndex(proxy_index)
        self._root_list.scrollTo(proxy_index)
        self._highlight_node(digimon_id)
        return True

    def _highlight_node(self, digimon_id: int) -> None:
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            if item.data(0, Qt.UserRole) == digimon_id:
                self._tree.setCurrentItem(item)
                self._tree.scrollToItem(item)
                # Make sure every ancestor is open so the highlighted row is
                # visible without the user having to expand manually.
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                return
            it += 1


# Node geometry — sprite tile is `_NODE_PX` square inside a 1px border;
# columns are spaced `_COL_GAP` apart, rows `_ROW_GAP`. Bezier connectors
# leave each parent's right edge and enter each child's left edge so the
# horizontal stage axis reads left → right (Baby → Mega).
_NODE_PX = 56
_COL_GAP = 56
_ROW_GAP = 16
_BORDER_PEN_W = 1
_EDGE_PEN_W = 1.5


class _NodePixmapItem(QGraphicsPixmapItem):
    """QGraphicsPixmapItem that emits its digimon id on double-click.

    Routed to the view's ``digimonActivated`` signal so a double-click on
    any node opens the StandardDigivolution editor for that digimon —
    same UX as the text-tree view.
    """

    def __init__(self, pixmap, digimon_id: int, view: "_TreeGraphView"):
        super().__init__(pixmap)
        self._digimon_id = digimon_id
        self._view = view
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"0x{digimon_id:03x}  {digimon_name(digimon_id)}")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: D401
        self._view.digimonActivated.emit(self._digimon_id)
        super().mouseDoubleClickEvent(event)


class _TreeGraphView(QGraphicsView):
    """Sprite-graph view of a single digivolution tree.

    Layout strategy — column by stage (`digimon_stage_rank`), pack rows
    centroid-of-children so each parent sits at the y midpoint of its
    children. Unknown-stage nodes fall back to ``parent_col + 1``. Edges
    are quadratic beziers drawn parent → child. The whole scene fits to
    view on render so the user always sees the full tree at a glance;
    they can wheel-zoom for detail.
    """

    digimonActivated = Signal(int)

    def __init__(self, records: Dict[int, model.StandardDigivolution], session):
        super().__init__()
        self._records = records
        self._session = session
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(self.palette().base())
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)

    def clear(self) -> None:
        self._scene.clear()
        self.setSceneRect(QRectF())

    def render_tree(self, root_id: int) -> None:
        self._scene.clear()
        positions, child_edges = self._layout(root_id)
        if not positions:
            return
        self._draw_edges(child_edges, positions)
        for did, (col, row) in positions.items():
            self._draw_node(did, col, row)
        # Natural size with padding — QGraphicsView's built-in scrollbars
        # take over when the tree exceeds the pane. Ctrl+wheel still zooms
        # if the user wants to shrink-to-fit or magnify.
        self.setSceneRect(self._scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        self.resetTransform()

    def wheelEvent(self, event) -> None:  # noqa: D401
        # Smooth wheel-zoom around the cursor. Plain wheel = vertical scroll
        # only with Ctrl held, to match the rest of the editor's conventions.
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    # ---- layout ---------------------------------------------------------

    def _layout(
        self, root_id: int,
    ) -> Tuple[Dict[int, Tuple[int, float]], List[Tuple[int, int]]]:
        """Walk the tree and assign each node a ``(column, row)``.

        Column = digimon stage rank with parent+1 fallback for unknown
        stages. Row = centroid of children's rows (leaves get the next
        free slot in their column). Also returns the (parent → child)
        edge list so the renderer can draw connectors without re-walking.
        """
        positions: Dict[int, Tuple[int, float]] = {}
        edges: List[Tuple[int, int]] = []
        visited: Set[int] = set()
        next_row: Dict[int, float] = defaultdict(float)

        def column_for(did: int, parent_col: Optional[int]) -> int:
            rank = digimon_stage_rank(did)
            if rank is not None:
                return rank
            return (parent_col + 1) if parent_col is not None else 0

        def visit(did: int, parent_col: Optional[int]) -> Optional[float]:
            if did in visited:
                return positions.get(did, (0, 0))[1]
            visited.add(did)
            col = column_for(did, parent_col)
            rec = self._records.get(did)
            child_rows: List[float] = []
            if rec is not None:
                for evo_attr in _EVO_ATTRS:
                    target = getattr(rec, evo_attr)
                    if target == NO_EVO_SENTINEL:
                        continue
                    edges.append((did, target))
                    y = visit(target, col)
                    if y is not None:
                        child_rows.append(y)
            if child_rows:
                row = (min(child_rows) + max(child_rows)) / 2.0
                row = max(row, next_row[col])
            else:
                row = next_row[col]
            next_row[col] = row + 1
            positions[did] = (col, row)
            return row

        visit(root_id, None)
        return positions, edges

    # ---- drawing --------------------------------------------------------

    def _node_rect(self, col: int, row: float) -> QRectF:
        x = col * (_NODE_PX + _COL_GAP)
        y = row * (_NODE_PX + _ROW_GAP)
        return QRectF(x, y, _NODE_PX, _NODE_PX)

    def _draw_node(self, digimon_id: int, col: int, row: float) -> None:
        rect = self._node_rect(col, row)
        # Background tile so missing sprites still render as a clickable
        # node with the digimon's name underneath.
        border = QGraphicsRectItem(rect)
        border.setPen(QPen(self.palette().mid().color(), _BORDER_PEN_W))
        border.setBrush(QBrush(self.palette().alternateBase().color()))
        self._scene.addItem(border)

        pix = self._overworld_pixmap(digimon_id)
        if pix is not None and not pix.isNull():
            item = _NodePixmapItem(pix, digimon_id, self)
            # Center the pixmap inside the node tile.
            item.setOffset(
                rect.x() + (rect.width() - pix.width()) / 2,
                rect.y() + (rect.height() - pix.height()) / 2,
            )
            self._scene.addItem(item)
        else:
            # Sprite unavailable — still make the border clickable via a
            # transparent overlay so the user can navigate by name.
            placeholder = QGraphicsRectItem(rect)
            placeholder.setPen(QPen(Qt.NoPen))
            placeholder.setBrush(QBrush(Qt.transparent))
            placeholder.setCursor(Qt.PointingHandCursor)
            placeholder.setToolTip(f"0x{digimon_id:03x}  {digimon_name(digimon_id)}")
            self._scene.addItem(placeholder)

        # Name caption below the tile so the user can identify the node
        # without hovering for the tooltip.
        caption = QGraphicsSimpleTextItem(digimon_name(digimon_id))
        caption.setBrush(self.palette().text())
        font = caption.font()
        font.setPointSize(max(7, font.pointSize() - 1))
        caption.setFont(font)
        cap_rect = caption.boundingRect()
        caption.setPos(
            rect.x() + (rect.width() - cap_rect.width()) / 2,
            rect.bottom() + 2,
        )
        self._scene.addItem(caption)

    def _draw_edges(
        self,
        edges: List[Tuple[int, int]],
        positions: Dict[int, Tuple[int, float]],
    ) -> None:
        pen = QPen(self.palette().mid().color(), _EDGE_PEN_W)
        pen.setCapStyle(Qt.RoundCap)
        for parent_id, child_id in edges:
            if parent_id not in positions or child_id not in positions:
                continue
            p_col, p_row = positions[parent_id]
            c_col, c_row = positions[child_id]
            p_rect = self._node_rect(p_col, p_row)
            c_rect = self._node_rect(c_col, c_row)
            p_pt = QPointF(p_rect.right(), p_rect.center().y())
            c_pt = QPointF(c_rect.left(), c_rect.center().y())
            mid_x = (p_pt.x() + c_pt.x()) / 2
            path = QPainterPath(p_pt)
            path.cubicTo(QPointF(mid_x, p_pt.y()), QPointF(mid_x, c_pt.y()), c_pt)
            edge_item = QGraphicsPathItem(path)
            edge_item.setPen(pen)
            edge_item.setZValue(-1)  # behind nodes
            self._scene.addItem(edge_item)

    def _overworld_pixmap(self, digimon_id: int):
        """Resolve the digimon's overworld MCHR frame via the session cache."""
        if self._session is None:
            return None
        sprite_map = getattr(self._session, "sprite_map", None)
        if not sprite_map or not (0 <= digimon_id < len(sprite_map)):
            return None
        mchr_idx = sprite_map[digimon_id].unknown_0x4
        return self._session.mchr_sprite_pixmap(mchr_idx, max_size=_NODE_PX - 6)
