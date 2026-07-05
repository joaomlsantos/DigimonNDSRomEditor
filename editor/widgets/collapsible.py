"""A titled expand/collapse section with a disclosure arrow.

Replaces the checkbox-style ``QGroupBox.setCheckable(True)`` look (which
renders a checkbox in the title, reading as an on/off option rather than
an expander) with a left-aligned ▶/▼ toggle — the conventional
disclosure-triangle affordance.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """Header row (arrow + title) over a body that shows/hides on click.

    Set the body with :meth:`set_content_widget`. Starts collapsed unless
    ``expanded=True``.
    """

    _STYLE = (
        "QToolButton { border: none; text-align: left; padding: 4px 0; "
        "font-weight: bold; }"
    )

    def __init__(self, title: str, expanded: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._toggle = QToolButton()
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        # Native style-drawn arrow (crisp at any DPI) beside the text,
        # rather than a font triangle glyph which renders inconsistently.
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setStyleSheet(self._STYLE)
        self._toggle.toggled.connect(self._on_toggled)

        self._content = QWidget()
        self._content.setVisible(expanded)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._toggle)
        outer.addWidget(self._content)
        self._update_arrow()

    def set_content_widget(self, widget: QWidget) -> None:
        """Host ``widget`` as the collapsible body (indented under the header)."""
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(12, 4, 0, 4)
        layout.addWidget(widget)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(expanded)

    def _on_toggled(self, checked: bool) -> None:
        self._content.setVisible(checked)
        self._update_arrow()

    def _update_arrow(self) -> None:
        self._toggle.setArrowType(
            Qt.DownArrow if self._toggle.isChecked() else Qt.RightArrow
        )
