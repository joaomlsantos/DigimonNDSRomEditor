"""Aggregated validation issues — shared registry + footer UI.

Editors publish collector callables on ROM load; the main window asks the
registry to recompute on every undo-stack tick and routes the result into a
footer pill + collapsible popup (`ValidationFooter`).

Issues are pure-data: a collector reads from the model (NOT from the active
editor's widgets), so the registry stays valid even when the editor for a
given section isn't currently constructed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class ValidationIssue:
    """One row in the footer popup.

    `section` groups issues at the top level (one heading per editor table).
    `category` groups within a section (e.g. "Duplicate Moves", "Reciprocal
    Mismatch"). `editor_key` + `record_id` drive click-to-navigate; record_id
    is optional so non-record-scoped issues can still appear.
    """
    section: str
    category: str
    message: str
    editor_key: str
    record_id: Optional[int] = None


Collector = Callable[[], List[ValidationIssue]]


class _ValidationRegistry(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._collectors: List[Collector] = []

    def register(self, collector: Collector) -> None:
        self._collectors.append(collector)

    def clear(self) -> None:
        self._collectors.clear()
        self._safe_emit_changed()

    def collect_all(self) -> List[ValidationIssue]:
        out: List[ValidationIssue] = []
        for fn in self._collectors:
            try:
                out.extend(fn())
            except Exception:
                # A misbehaving collector shouldn't kill the whole footer —
                # swallow its errors and keep aggregating the rest.
                continue
        return out

    def notify_changed(self) -> None:
        self._safe_emit_changed()

    def _safe_emit_changed(self) -> None:
        # The registry is a module-level singleton without a Qt parent, so
        # during app shutdown Qt may delete its C++ side while late slot
        # invocations (e.g. QUndoStack.indexChanged firing as widgets tear
        # down) still hold a Python reference. Emitting on a dead QObject
        # raises RuntimeError — swallow it; there's nothing useful to do
        # once Qt is mid-teardown.
        try:
            self.changed.emit()
        except RuntimeError:
            pass


_REGISTRY: Optional[_ValidationRegistry] = None


def registry() -> _ValidationRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _ValidationRegistry()
    return _REGISTRY


# ---- footer UI -----------------------------------------------------------


class ValidationFooter(QWidget):
    """Status-bar pill that opens a popup grouped by section / category.

    Lives in the main window's status bar via `addPermanentWidget`. The
    popup is a top-level Qt.Popup-flagged frame positioned just above the
    pill — clicking outside closes it automatically.

    `navigate(editor_key, record_id)` callback is invoked when the user
    clicks an issue row; the main window wires it to its existing nav path.
    """

    def __init__(
        self,
        navigate: Callable[[str, Optional[int]], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._navigate = navigate
        self._popup: Optional[_IssuesPopup] = None

        self._pill = QPushButton("● 0 issues")
        self._pill.setCursor(Qt.PointingHandCursor)
        self._pill.setFlat(True)
        self._pill.clicked.connect(self._toggle_popup)
        self._apply_pill_style(0)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._pill)

        registry().changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        issues = registry().collect_all()
        count = len(issues)
        label = "1 issue" if count == 1 else f"{count} issues"
        self._pill.setText(f"● {label}")
        self._apply_pill_style(count)
        if self._popup is not None and self._popup.isVisible():
            self._popup.set_issues(issues)

    def _apply_pill_style(self, count: int) -> None:
        # Red when issues are present, muted grey when clean — matches the
        # reference design where the pill only "lights up" if something
        # actually needs attention.
        if count == 0:
            self._pill.setStyleSheet(
                "QPushButton { color: palette(mid); border: 1px solid palette(mid);"
                " border-radius: 10px; padding: 2px 10px; background: transparent; }"
                "QPushButton:hover { background: palette(button); }"
            )
        else:
            self._pill.setStyleSheet(
                "QPushButton { color: white; border: 1px solid #c0392b;"
                " border-radius: 10px; padding: 2px 10px; background: #c0392b; }"
                "QPushButton:hover { background: #a93226; }"
            )

    def _toggle_popup(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()
            return
        if self._popup is None:
            self._popup = _IssuesPopup(self._on_issue_clicked, self)
        self._popup.set_issues(registry().collect_all())
        self._position_popup()
        self._popup.show()
        self._popup.raise_()
        self._popup.activateWindow()

    def _position_popup(self) -> None:
        assert self._popup is not None
        size = self._popup.sizeHint()
        # Anchor the popup's bottom-left to the pill's top-left so it floats
        # above the status bar, matching the reference design.
        pill_top_left = self._pill.mapToGlobal(QPoint(0, 0))
        x = pill_top_left.x()
        y = pill_top_left.y() - size.height() - 4
        self._popup.setGeometry(x, y, size.width(), size.height())

    def _on_issue_clicked(self, issue: ValidationIssue) -> None:
        if self._popup is not None:
            self._popup.hide()
        self._navigate(issue.editor_key, issue.record_id)


class _IssuesPopup(QFrame):
    """Top-level frame listing issues grouped by section → category."""

    def __init__(
        self,
        on_issue_clicked: Callable[[ValidationIssue], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        # Qt.Popup makes the frame self-close on click-outside; combined with
        # Qt.FramelessWindowHint, it renders as a floating panel without OS chrome.
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self._on_issue_clicked = on_issue_clicked
        self.setObjectName("ValidationIssuesPopup")
        # Crisp 1-px border + slight padding so the floating frame visually
        # separates from the status bar underneath.
        self.setStyleSheet(
            "#ValidationIssuesPopup { background: palette(window);"
            " border: 1px solid palette(mid); }"
            "QLabel#section { font-weight: bold; color: palette(mid);"
            " padding: 4px 8px; background: palette(alternate-base); }"
            "QLabel#category { font-weight: bold; color: palette(text);"
            " padding: 2px 12px; }"
        )

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)
        scroll.setFrameShape(QFrame.NoFrame)

        header = QLabel("Validation Issues")
        header_font = header.font()
        header_font.setBold(True)
        header.setFont(header_font)
        header.setStyleSheet("padding: 6px 8px; border-bottom: 1px solid palette(mid);")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(scroll)

        self.setMinimumWidth(560)
        self.setMinimumHeight(420)
        self.setMaximumHeight(640)

    def sizeHint(self):  # noqa: N802 — Qt override name
        # Cap so a long issue list doesn't push the popup off-screen; the
        # inner scroll area handles overflow.
        hint = super().sizeHint()
        return hint.boundedTo(self.maximumSize()).expandedTo(self.minimumSize())

    def set_issues(self, issues: List[ValidationIssue]) -> None:
        # Tear down old rows and rebuild — issue lists are short enough that
        # diff-and-patch isn't worth the complexity.
        while self._container_layout.count():
            item = self._container_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not issues:
            empty = QLabel("No validation issues.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("padding: 24px; color: palette(mid);")
            self._container_layout.addWidget(empty)
            self._container_layout.addStretch(1)
            return

        # Group preserving first-seen order so the list is stable across edits.
        sections: List[str] = []
        by_section: dict[str, List[ValidationIssue]] = {}
        for issue in issues:
            if issue.section not in by_section:
                sections.append(issue.section)
                by_section[issue.section] = []
            by_section[issue.section].append(issue)

        for section in sections:
            section_label = QLabel(section.upper())
            section_label.setObjectName("section")
            self._container_layout.addWidget(section_label)

            categories: List[str] = []
            by_cat: dict[str, List[ValidationIssue]] = {}
            for issue in by_section[section]:
                if issue.category not in by_cat:
                    categories.append(issue.category)
                    by_cat[issue.category] = []
                by_cat[issue.category].append(issue)

            for cat in categories:
                cat_label = QLabel(cat.upper())
                cat_label.setObjectName("category")
                self._container_layout.addWidget(cat_label)
                for issue in by_cat[cat]:
                    self._container_layout.addWidget(self._make_row(issue))

        self._container_layout.addStretch(1)

    def _make_row(self, issue: ValidationIssue) -> QWidget:
        row = QPushButton(f"●  {issue.message}")
        row.setCursor(Qt.PointingHandCursor)
        row.setFlat(True)
        # Hug-left so messages line up; allow wrapping via setMinimumWidth so
        # the row resizes with the popup rather than truncating.
        row.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 24px;"
            " color: #c0392b; border: none; background: transparent; }"
            "QPushButton:hover { background: palette(alternate-base); }"
        )
        row.clicked.connect(lambda _checked=False, i=issue: self._on_issue_clicked(i))
        return row
