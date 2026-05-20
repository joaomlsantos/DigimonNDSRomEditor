"""Main editor window — File menu, undo stack, navigation tree, status bar.

Domain editors (digimon table, move table, etc.) plug into the right-hand
content area via `set_content(widget)`. For now the navigation tree is a flat
list of placeholders so the skeleton can be opened and clicked through; each
node will be wired to a real editor widget in subsequent milestones.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QStandardItem, QStandardItemModel, QUndoStack
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTreeView,
    QWidget,
)

from .session import RomSession
from .widgets.armor_digivolution_editor import ArmorDigivolutionEditor
from .widgets.base_digimon_editor import BaseDigimonEditor
from .widgets.consumable_editor import ConsumableEditor
from .widgets.dna_digivolution_editor import DNADigivolutionEditor
from .widgets.encounter_rewards_editor import EncounterRewardsEditor
from .widgets.enemy_digimon_editor import EnemyDigimonEditor
from .widgets.equipment_editor import EquipmentEditor
from .widgets.farm_item_editor import FarmItemEditor
from .widgets.farm_terrains_editor import FarmTerrainsEditor
from .widgets.habitats_editor import HabitatsWorldmapEditor
from .widgets.move_editor import MoveEditor
from .widgets.quest_editor import QuestEditor
from .widgets.standard_digivolution_editor import StandardDigivolutionEditor
from .widgets.starters_editor import StartersEditor
from .widgets.wild_encounters_editor import WildEncountersEditor


# nodes shown in the left-hand navigation tree (label, session-attr key)
NAV_NODES = [
    ("Base Digimon", "base_digimon"),
    ("Enemy Digimon", "enemy_digimon"),
    ("Moves", "moves"),
    ("Quests", "quests"),
    ("Encounter Rewards", "encounter_rewards"),
    ("Standard Digivolutions", "standard_digivolutions"),
    ("Armor Digivolutions", "armor_digivolutions"),
    ("DNA Digivolutions", "dna_digivolutions"),
    ("Starter Packs", "starters"),
    ("Wild Encounters", "wild_encounter_areas"),
    ("Habitats / World Map", "habitats_worldmap"),
    ("Farm Terrains", "farm_terrains"),
    ("Equipment", "equipment"),
    ("Consumables", "consumables"),
    ("Farm Items", "farm_items"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Digimon NDS ROM Editor")
        self.resize(1280, 820)

        self.session: Optional[RomSession] = None
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self._refresh_status)

        self._build_actions()
        self._build_menus()
        self._build_central()
        self._build_status_bar()
        self._refresh_status()
        self._refresh_actions()

    # ---- UI construction -------------------------------------------------

    def _build_actions(self) -> None:
        self.action_open = QAction("&Open ROM…", self)
        self.action_open.setShortcut(QKeySequence.Open)
        self.action_open.triggered.connect(self._on_open)

        self.action_save = QAction("&Save", self)
        self.action_save.setShortcut(QKeySequence.Save)
        self.action_save.triggered.connect(self._on_save)

        self.action_save_as = QAction("Save &As…", self)
        self.action_save_as.setShortcut(QKeySequence.SaveAs)
        self.action_save_as.triggered.connect(self._on_save_as)

        self.action_quit = QAction("&Quit", self)
        self.action_quit.setShortcut(QKeySequence.Quit)
        self.action_quit.triggered.connect(self.close)

        self.action_undo = self.undo_stack.createUndoAction(self, "&Undo")
        self.action_undo.setShortcut(QKeySequence.Undo)
        self.action_redo = self.undo_stack.createRedoAction(self, "&Redo")
        self.action_redo.setShortcut(QKeySequence.Redo)

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self.action_open)
        file_menu.addSeparator()
        file_menu.addAction(self.action_save)
        file_menu.addAction(self.action_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal, self)

        self.nav_model = QStandardItemModel(self)
        self.nav_model.setHorizontalHeaderLabels(["Data"])
        for label, key in NAV_NODES:
            item = QStandardItem(label)
            item.setEditable(False)
            item.setData(key, Qt.UserRole)
            self.nav_model.appendRow(item)

        self.nav_view = QTreeView()
        self.nav_view.setModel(self.nav_model)
        self.nav_view.setHeaderHidden(True)
        self.nav_view.clicked.connect(self._on_nav_clicked)

        self.content_placeholder = QLabel("Open a ROM to begin.")
        self.content_placeholder.setAlignment(Qt.AlignCenter)
        self.content_placeholder.setMargin(40)

        splitter.addWidget(self.nav_view)
        splitter.addWidget(self.content_placeholder)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 1100])
        self.setCentralWidget(splitter)

    def _build_status_bar(self) -> None:
        self.status_version = QLabel("No ROM loaded")
        self.status_dirty = QLabel("")
        self.statusBar().addWidget(self.status_version)
        self.statusBar().addPermanentWidget(self.status_dirty)

    # ---- helpers ---------------------------------------------------------

    def set_content(self, widget: QWidget) -> None:
        splitter: QSplitter = self.centralWidget()  # type: ignore[assignment]
        old = splitter.widget(1)
        old.setParent(None)
        old.deleteLater()
        splitter.addWidget(widget)
        splitter.setSizes([180, 1100])

    def _refresh_status(self) -> None:
        if self.session is None:
            self.status_version.setText("No ROM loaded")
            self.status_dirty.setText("")
            self.setWindowTitle("Digimon NDS ROM Editor")
            return
        name = os.path.basename(self.session.source_path)
        self.status_version.setText(f"{self.session.version}  —  {name}")
        is_clean = self.undo_stack.isClean()
        self.status_dirty.setText("" if is_clean else "● unsaved changes")
        title = f"Digimon NDS ROM Editor — {name}"
        if not is_clean:
            title += " *"
        self.setWindowTitle(title)

    def _refresh_actions(self) -> None:
        has_session = self.session is not None
        self.action_save.setEnabled(has_session)
        self.action_save_as.setEnabled(has_session)

    # ---- slots -----------------------------------------------------------

    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ROM",
            "",
            "NDS ROMs (*.nds);;All files (*)",
        )
        if not path:
            return
        try:
            session = RomSession.from_file(path)
        except Exception as exc:  # noqa: BLE001 — surface any load failure
            QMessageBox.critical(self, "Failed to open ROM", str(exc))
            return
        self.session = session
        self.undo_stack.clear()
        # Drop any open editor — it still references the previous session's
        # model objects, which serialize_all() would no longer write back.
        placeholder = QLabel("Pick a section on the left to begin editing.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setMargin(40)
        self.set_content(placeholder)
        self.nav_view.clearSelection()
        self._refresh_status()
        self._refresh_actions()

    def _on_save(self) -> None:
        if self.session is None:
            return
        try:
            self.session.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to save ROM", str(exc))
            return
        self.undo_stack.setClean()
        self._refresh_status()

    def _on_save_as(self) -> None:
        if self.session is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROM As",
            self.session.source_path,
            "NDS ROMs (*.nds);;All files (*)",
        )
        if not path:
            return
        try:
            self.session.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to save ROM", str(exc))
            return
        self.undo_stack.setClean()
        self._refresh_status()

    def _on_nav_clicked(self, index) -> None:
        key = index.data(Qt.UserRole)
        label = index.data(Qt.DisplayRole)
        if self.session is None or key is None:
            return

        if key == "base_digimon":
            self.set_content(BaseDigimonEditor(self.session.base_digimon, self.undo_stack))
            return
        if key == "enemy_digimon":
            self.set_content(EnemyDigimonEditor(
                self.session.enemy_digimon,
                self.undo_stack,
                self.session.sprite_map,
                self.session.battle_strings,
            ))
            return
        if key == "moves":
            self.set_content(MoveEditor(self.session.moves, self.undo_stack))
            return
        if key == "standard_digivolutions":
            self.set_content(
                StandardDigivolutionEditor(self.session.standard_digivolutions, self.undo_stack)
            )
            return
        if key == "armor_digivolutions":
            self.set_content(
                ArmorDigivolutionEditor(self.session.armor_digivolutions, self.undo_stack)
            )
            return
        if key == "dna_digivolutions":
            self.set_content(
                DNADigivolutionEditor(self.session.dna_digivolutions, self.undo_stack)
            )
            return
        if key == "quests":
            self.set_content(QuestEditor(self.session.quests, self.undo_stack))
            return
        if key == "starters":
            self.set_content(StartersEditor(self.session.starters, self.undo_stack))
            return
        if key == "wild_encounter_areas":
            self.set_content(
                WildEncountersEditor(
                    self.session.version,
                    self.session.wild_encounter_areas,
                    self.undo_stack,
                )
            )
            return
        if key == "encounter_rewards":
            self.set_content(
                EncounterRewardsEditor(self.session.encounter_rewards, self.undo_stack)
            )
            return
        if key == "habitats_worldmap":
            self.set_content(
                HabitatsWorldmapEditor(self.session.habitats_worldmap, self.undo_stack)
            )
            return
        if key == "farm_terrains":
            self.set_content(
                FarmTerrainsEditor(self.session.farm_terrains, self.undo_stack)
            )
            return
        if key == "equipment":
            self.set_content(EquipmentEditor(self.session.equipment, self.undo_stack))
            return
        if key == "consumables":
            self.set_content(ConsumableEditor(self.session.consumables, self.undo_stack))
            return
        if key == "farm_items":
            self.set_content(FarmItemEditor(self.session.farm_items, self.undo_stack))
            return

        collection = getattr(self.session, key)
        count = len(collection)
        placeholder = QLabel(f"{label}\n\n{count} records parsed.\n\nEditor coming soon.")
        placeholder.setAlignment(Qt.AlignCenter)
        self.set_content(placeholder)
