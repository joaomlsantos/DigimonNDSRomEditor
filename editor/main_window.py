"""Main editor window — File menu, undo stack, navigation tree, status bar.

Domain editors (digimon table, move table, etc.) plug into the right-hand
content area via `set_content(widget)`. For now the navigation tree is a flat
list of placeholders so the skeleton can be opened and clicked through; each
node will be wired to a real editor widget in subsequent milestones.
"""
from __future__ import annotations

import os
from collections import namedtuple
from typing import Optional, Tuple

from PySide6.QtCore import QItemSelectionModel, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QStandardItem, QStandardItemModel, QUndoStack
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import project_file
from ._perf import report_and_clear, span
from .auto_updater import UpdateChecker
from .changelog import changelog_dir, changelog_root, record_save, rom_hash
from .prefs import prefs
from .session import RomSession
from digimon_core import rom as rom_module
from .widgets.cell_export_policy import (
    allow_shared_tile_exports,
    set_allow_shared_tile_exports,
)
from .widgets.form_helpers import (
    clear_details_providers,
    clear_nav_handlers,
    register_nav_handler,
    set_details_providers,
    set_unknown_fields_visible,
    unknown_fields_visible,
)
from digimon_core import constants
from .widgets.armor_digivolution_editor import (
    ArmorDigivolutionEditor,
    armor_digivolution_issues,
)
from .widgets.base_digimon_editor import BaseDigimonEditor, base_digimon_issues
from .widgets.consumable_editor import ConsumableEditor, consumable_issues
from .widgets.digivolution_tree_editor import DigivolutionTreeEditor
from .widgets.dna_digivolution_editor import (
    DNADigivolutionEditor,
    dna_digivolution_issues,
)
from .widgets.encounter_rewards_editor import (
    EncounterRewardsEditor,
    encounter_reward_issues,
)
from .widgets.enemy_digimon_editor import EnemyDigimonEditor
from .widgets.equipment_editor import EquipmentEditor
from .widgets.farm_item_editor import FarmItemEditor, farm_item_issues
from .widgets.farm_terrains_editor import FarmTerrainsEditor, farm_terrain_issues
from .widgets.farm_training_pen_editor import (
    FarmTrainingPenEditor,
    farm_training_pen_issues,
)
from .widgets.string_editor import StringEditor, string_issues
from .widgets.habitats_editor import HabitatsWorldmapEditor
from .widgets.move_editor import MoveEditor, move_issues
from .widgets.qol_editor import QolEditor
from .widgets.quest_editor import QuestEditor
from .widgets.btchr_browser import BtchrBrowser
from .widgets.btmap_browser import BtmapBrowser
from .widgets.map_browser import MapBrowser
from .widgets.mchr_browser import MchrBrowser
from .widgets.sound_editor import SoundEditor
from .widgets.sprite_browser import SpriteBrowser
from .widgets.standard_digivolution_editor import (
    StandardDigivolutionEditor,
    std_digivolution_issues,
)
from .widgets.starters_editor import StartersEditor, starter_issues
from .widgets.validation import ValidationFooter, registry as validation_registry
from .widgets.wild_encounters_editor import WildEncountersEditor, wild_encounter_issues


# Hierarchical nav tree: (group label, [(leaf label, key), ...]). Group rows
# carry no key and are non-selectable — they exist purely as headers. Leaves
# carry the session-attr key the editor dispatcher resolves.
# QSettings keys for persistent window/UI state. Versioning the geometry key
# means a future layout change can bump the suffix to invalidate stale values
# instead of restoring a broken split.
SETTINGS_GEOMETRY = "window/geometry"
SETTINGS_STATE = "window/state"
SETTINGS_SPLITTER = "window/splitter_sizes"
SETTINGS_NAV_KEY = "window/last_nav_key"
SETTINGS_RECENT = "files/recent"
SETTINGS_RECENT_PROJECTS = "files/recent_projects"
# Per-vanilla-ROM-hash cache mapping sha256 → on-disk path. When opening a
# project, we look up the cached path before falling back to a file dialog,
# so users who consistently keep their vanilla ROM in one place are never
# re-prompted after the first project of that version.
SETTINGS_VANILLA_PATHS_PREFIX = "vanilla_paths/"
MAX_RECENT_FILES = 5


# Bundle of (QSettings key, QMenu to populate, open-callback) — passed around
# as one arg instead of threading three parallels through every recent-files
# helper. One bundle per recent list (ROMs, projects).
_RecentList = namedtuple("_RecentList", ("setting_key", "menu", "on_open"))


NAV_GROUPS = [
    ("Digimon Data", [
        ("Base Digimon", "base_digimon"),
        ("Enemy Digimon", "enemy_digimon"),
        ("Moves", "moves"),
        ("Starter Packs", "starters"),
    ]),
    ("Digivolutions", [
        ("Standard Digivolutions", "standard_digivolutions"),
        ("Armor Digivolutions", "armor_digivolutions"),
        ("DNA Digivolutions", "dna_digivolutions"),
        ("Evolution Trees", "digivolution_trees"),
    ]),
    ("Dungeons", [
        ("Wild Encounters", "wild_encounter_areas"),
        ("Encounter Rewards", "encounter_rewards"),
        ("Habitats / World Map", "habitats_worldmap"),
        ("Quests", "quests"),
    ]),
    ("Items", [
        ("Equipment", "equipment"),
        ("Consumables", "consumables"),
        ("Farm Items", "farm_items"),
        ("Farm Terrains", "farm_terrains"),
        ("Farm Training Pens", "farm_training_pens"),
    ]),
    ("Text", [
        ("ARM9", "strings_bucket:arm9"),
        ("Overlays", "strings_bucket:overlay"),
        ("MSG.PAK", "strings_bucket:msgpak"),
    ]),
    ("Graphics", [
        ("Icons/Portraits/UI", "sprite_browser"),
        ("Overworld Sprites", "mchr_browser"),
        ("Battle Sprites", "btchr_browser"),
        ("Battle Backgrounds", "btmap_browser"),
        ("Field Maps", "map_browser"),
    ]),
    ("Sound", [
        ("BGMs", "sound_editor"),
    ]),
    ("Settings", [
        ("QoL Toggles", "qol_settings"),
    ]),
]

# Region-id prefix per nav bucket. The dispatcher merges every region whose
# id starts with the prefix into a single flat string list.
_STRING_BUCKET_PREFIXES = {
    "arm9": "arm9_",
    "overlay": "overlay",
    "msgpak": "msgpak_",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Digimon NDS ROM Editor")
        self.resize(1280, 820)

        self.session: Optional[RomSession] = None
        self._last_nav_key: Optional[str] = None
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self._refresh_status)
        # Whenever the model mutates, ask the validation registry to recompute
        # so the footer pill + popup reflect the new state.
        self.undo_stack.indexChanged.connect(lambda _i: validation_registry().notify_changed())

        self._build_actions()
        self._build_menus()
        self._build_central()
        self._build_status_bar()
        self._refresh_status()
        self._refresh_actions()
        self._restore_window_state()

        # Fire-and-forget GitHub release check. Stored on self so the QObject
        # outlives the daemon thread; the dialog only appears if a newer
        # version is published than `EDITOR_VERSION` and the user hasn't
        # already skipped it.
        self._update_checker = UpdateChecker(self)
        self._update_checker.start(project_file.EDITOR_VERSION)

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

        self.action_show_unknowns = QAction("Show unknown fields", self)
        self.action_show_unknowns.setCheckable(True)
        self.action_show_unknowns.setChecked(unknown_fields_visible())
        self.action_show_unknowns.toggled.connect(set_unknown_fields_visible)

        # Off by default — shared-tile sprites can't be round-tripped
        # through cell-mode IO. Persisted in the .ini via
        # ``cell_export_policy.set_allow_shared_tile_exports``. The
        # confirm dialog *always* fires when enabling (per the user's
        # spec), not "first time only" — there's no muscle-memory path
        # to flipping this on without being reminded what it means.
        self.action_allow_cell_shared = QAction(
            "Allow cell exports for shared-tile sprites", self,
        )
        self.action_allow_cell_shared.setCheckable(True)
        self.action_allow_cell_shared.setChecked(allow_shared_tile_exports())
        # `triggered(bool)` fires after the check state has already
        # flipped, so the dialog opens with the action visually checked
        # — and we can roll the check back if the user cancels.
        self.action_allow_cell_shared.triggered.connect(
            self._on_toggle_allow_cell_shared,
        )

        self.action_open_changelog = QAction("Open &Changelog Folder", self)
        self.action_open_changelog.triggered.connect(self._on_open_changelog)

        self.action_open_project = QAction("Open &Project…", self)
        self.action_open_project.triggered.connect(self._on_open_project)

        self.action_save_project_as = QAction("Save Project &As…", self)
        self.action_save_project_as.triggered.connect(self._on_save_project_as)

        self.action_about = QAction("&About…", self)
        self.action_about.triggered.connect(self._show_about)

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self.action_open)
        self._recent_menu = QMenu("Open &Recent", self)
        file_menu.addMenu(self._recent_menu)
        self._recent_roms = _RecentList(
            SETTINGS_RECENT, self._recent_menu, self._on_open_recent,
        )
        self._rebuild_recent_menu(self._recent_roms)
        file_menu.addSeparator()
        file_menu.addAction(self.action_save)
        file_menu.addAction(self.action_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.action_open_project)
        self._recent_project_menu = QMenu("Open Recent &Project", self)
        file_menu.addMenu(self._recent_project_menu)
        self._recent_projects = _RecentList(
            SETTINGS_RECENT_PROJECTS, self._recent_project_menu, self._on_open_recent_project,
        )
        self._rebuild_recent_menu(self._recent_projects)
        file_menu.addAction(self.action_save_project_as)
        file_menu.addSeparator()
        file_menu.addAction(self.action_open_changelog)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_show_unknowns)
        edit_menu.addAction(self.action_allow_cell_shared)

        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self.action_about)

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal, self)
        self._splitter = splitter

        self.nav_model = QStandardItemModel(self)
        self.nav_model.setHorizontalHeaderLabels(["Data"])
        for group_label, leaves in NAV_GROUPS:
            group_item = QStandardItem(group_label)
            group_item.setEditable(False)
            # Group rows are headers — clickable for expand/collapse but not
            # selectable, so they never trigger the editor dispatcher.
            group_item.setFlags(Qt.ItemIsEnabled)
            group_font = group_item.font()
            group_font.setBold(True)
            group_item.setFont(group_font)
            for leaf_label, key in leaves:
                leaf_item = QStandardItem(leaf_label)
                leaf_item.setEditable(False)
                leaf_item.setData(key, Qt.UserRole)
                group_item.appendRow(leaf_item)
            self.nav_model.appendRow(group_item)

        self.nav_view = QTreeView()
        self.nav_view.setModel(self.nav_model)
        self.nav_view.setHeaderHidden(True)
        self.nav_view.expandAll()
        # `currentChanged` fires for both mouse clicks and keyboard arrow keys,
        # so a single connection covers every selection path.
        self.nav_view.selectionModel().currentChanged.connect(
            lambda current, _previous: self._on_nav_clicked(current)
        )

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
        self.validation_footer = ValidationFooter(self._navigate_to_issue, self)
        self.statusBar().addWidget(self.validation_footer)
        self.statusBar().addWidget(self.status_version)
        self.statusBar().addPermanentWidget(self.status_dirty)

    # ---- helpers ---------------------------------------------------------

    def set_content(self, widget: QWidget) -> None:
        splitter: QSplitter = self.centralWidget()  # type: ignore[assignment]
        with span("teardown_old"):
            old = splitter.widget(1)
            # Editors that own pooled resources (e.g. SpriteMapRow's
            # pooled sprite pickers) detach them here so Qt's
            # parent-deletes-children rule doesn't drag pooled widgets
            # down with the editor.
            teardown = getattr(old, "aboutToTeardown", None)
            if callable(teardown):
                teardown()
            old.setParent(None)
            old.deleteLater()
        with span("addWidget_new"):
            splitter.addWidget(widget)
            splitter.setSizes([180, 1100])

    def _refresh_status(self) -> None:
        if self.session is None:
            self.status_version.setText("No ROM loaded")
            self.status_dirty.setText("")
            self.setWindowTitle("Digimon NDS ROM Editor")
            return
        # Title prefers the project file when one is active, falling back to
        # the ROM path; "(unsaved)" appears only when neither is set (fresh
        # project session before its first Save As).
        if self.session.project_path:
            name = os.path.basename(self.session.project_path)
        elif self.session.source_path:
            name = os.path.basename(self.session.source_path)
        else:
            name = "(unsaved)"
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
        self.action_save_project_as.setEnabled(has_session)

    def _confirm_discard_changes(self) -> bool:
        """Prompt before discarding unsaved edits.

        Returns True if it's safe to proceed (no session, clean stack, user
        chose Save and the save succeeded, or user chose Discard). Returns
        False if the user cancelled or a Save attempt failed — caller should
        abort whatever action would have lost work.
        """
        if self.session is None or self.undo_stack.isClean():
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved changes",
            "You have unsaved changes. Save before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Discard:
            return True
        # Save path: route through _on_save; if it failed the undo stack
        # stays dirty, so treat that as a cancel rather than silently losing.
        self._on_save()
        return self.undo_stack.isClean()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override name
        if self._confirm_discard_changes():
            # Persist before accepting — once the event is accepted Qt starts
            # tearing widgets down and the splitter/window queries may return
            # stale or zeroed values.
            self._save_window_state()
            event.accept()
        else:
            event.ignore()

    # ---- window state persistence ---------------------------------------

    def _restore_window_state(self) -> None:
        settings = prefs()
        geom = settings.value(SETTINGS_GEOMETRY)
        if geom is not None:
            self.restoreGeometry(geom)
        state = settings.value(SETTINGS_STATE)
        if state is not None:
            self.restoreState(state)
        # Splitter sizes come back from QSettings as a list of strings on some
        # platforms; coerce to int and ignore corrupt/partial values rather
        # than crashing on a malformed settings file.
        sizes = settings.value(SETTINGS_SPLITTER)
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([int(s) for s in sizes])
            except (TypeError, ValueError):
                pass

    def _save_window_state(self) -> None:
        settings = prefs()
        settings.setValue(SETTINGS_GEOMETRY, self.saveGeometry())
        settings.setValue(SETTINGS_STATE, self.saveState())
        settings.setValue(SETTINGS_SPLITTER, self._splitter.sizes())
        if self._last_nav_key is not None:
            settings.setValue(SETTINGS_NAV_KEY, self._last_nav_key)

    # ---- recent files ----------------------------------------------------

    def _recent_paths(self, rl: _RecentList) -> list[str]:
        settings = prefs()
        raw = settings.value(rl.setting_key, [])
        if isinstance(raw, str):
            # QSettings collapses single-element lists to a bare string on some
            # backends — re-wrap so callers can treat the return uniformly.
            return [raw] if raw else []
        return [str(p) for p in (raw or [])]

    def _write_recent_paths(self, rl: _RecentList, paths: list[str]) -> None:
        prefs().setValue(rl.setting_key, paths[:MAX_RECENT_FILES])
        self._rebuild_recent_menu(rl)

    def _push_recent(self, rl: _RecentList, path: str) -> None:
        # Normalise to absolute path so the same file opened via two different
        # spellings (relative vs absolute, mixed separators) collapses into a
        # single entry. Most-recent-first ordering.
        norm = os.path.abspath(path)
        paths = [p for p in self._recent_paths(rl) if os.path.normcase(p) != os.path.normcase(norm)]
        paths.insert(0, norm)
        self._write_recent_paths(rl, paths)

    def _remove_from_recent(self, rl: _RecentList, path: str) -> None:
        norm = os.path.normcase(os.path.abspath(path))
        paths = [p for p in self._recent_paths(rl) if os.path.normcase(p) != norm]
        self._write_recent_paths(rl, paths)

    def _rebuild_recent_menu(self, rl: _RecentList) -> None:
        rl.menu.clear()
        paths = self._recent_paths(rl)
        if not paths:
            empty = rl.menu.addAction("(no recent files)")
            empty.setEnabled(False)
            return
        for path in paths:
            label = os.path.basename(path) or path
            action = rl.menu.addAction(label)
            action.setToolTip(path)
            action.triggered.connect(lambda _checked=False, p=path: rl.on_open(p))
        rl.menu.addSeparator()
        clear_action = rl.menu.addAction("Clear Recent")
        clear_action.triggered.connect(lambda: self._write_recent_paths(rl, []))

    def _on_open_changelog(self) -> None:
        """Open the changelog folder in the system file manager.

        If a ROM is loaded, drill into that ROM's per-hash subdir so the user
        lands directly on its history; otherwise open the root and let them
        pick. The folder is created on demand — without this an unsaved
        session would have nothing to open.
        """
        if self.session is not None:
            target = changelog_dir(rom_hash(self.session.original_rom_data))
        else:
            target = changelog_root()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot open changelog folder", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_toggle_allow_cell_shared(self, checked: bool) -> None:
        """Edit-menu hook for the shared-tile cell-export policy.

        The confirm dialog always fires when enabling (per spec) so
        there's no muscle-memory path to flipping the policy on without
        being reminded the round-trip is unsafe. Cancel rolls the menu
        check back to its prior state via ``blockSignals`` so the
        rollback ``setChecked`` doesn't re-enter this handler.
        """
        if checked:
            choice = QMessageBox.warning(
                self,
                "Allow cell exports for shared-tile sprites?",
                "Cell-mode export/import treats each OAM as an "
                "independent tile region. Sprites that reuse tile data "
                "across OAMs (common for icons and portraits) can't be "
                "round-tripped this way — re-importing an edited PNG "
                "will produce visual artifacts on the cells that share "
                "the affected tiles.\n\n"
                "Use \"Export tiles PNG\" / \"Import tiles PNG\" instead "
                "for these sprites whenever possible.\n\n"
                "Enable cell exports on shared-tile sprites anyway?",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if choice != QMessageBox.Ok:
                self.action_allow_cell_shared.blockSignals(True)
                self.action_allow_cell_shared.setChecked(False)
                self.action_allow_cell_shared.blockSignals(False)
                return
        set_allow_shared_tile_exports(checked)

    def _show_about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.setMinimumWidth(380)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(6)

        title = QLabel("Digimon NDS ROM Editor")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        version = QLabel(f"Version {project_file.EDITOR_VERSION}")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)

        layout.addSpacing(4)

        description = QLabel(
            "ROM editor for Digimon World Dawn/Dusk that provides direct "
            "editing of digimon, moves, evolutions, encounters, quests, "
            "items, and farm data, with optional quality-of-life patches."
        )
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignCenter)
        layout.addWidget(description)

        layout.addSpacing(4)

        copyright_label = QLabel(
            "Copyright © 2026 João Santos\nLicensed under the GPL-3.0 license"
        )
        copyright_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(copyright_label)

        layout.addSpacing(4)

        attribution = QLabel(
            "All rights to the original Digimon World Dawn/Dusk belong to "
            "Bandai Namco Entertainment."
        )
        attribution.setWordWrap(True)
        attribution.setAlignment(Qt.AlignCenter)
        layout.addWidget(attribution)

        layout.addSpacing(8)

        links = QLabel(
            '<a href="https://github.com/joaomlsantos/DigimonNDSRomEditor">GitHub</a>'
            '&nbsp;&nbsp;&nbsp;'
            '<a href="https://ko-fi.com/joaomlsantos">Ko-Fi</a>'
        )
        links.setAlignment(Qt.AlignCenter)
        links.setOpenExternalLinks(True)
        layout.addWidget(links)

        dlg.exec()

    # ---- project files ---------------------------------------------------

    def _resolve_vanilla_for_project(
        self,
        expected_sha256: str,
        expected_version: str,
    ) -> Optional[Tuple[bytes, str, str]]:
        """Locate the vanilla ROM that matches a project's expected hash.

        Returns (rom_bytes, detected_version, on_disk_path) on success, or
        None if the user cancelled or no matching ROM could be loaded. Caches
        the chosen path in QSettings under the SHA-256 so future project
        opens skip the prompt entirely.
        """
        settings = prefs()
        cache_key = SETTINGS_VANILLA_PATHS_PREFIX + expected_sha256
        cached = settings.value(cache_key)
        if isinstance(cached, str) and cached and os.path.exists(cached):
            try:
                rom_data = rom_module.loadRom(cached)
            except Exception:  # noqa: BLE001 — fall through to prompt
                rom_data = None
            if rom_data is not None and project_file.vanilla_sha256(bytes(rom_data)) == expected_sha256:
                version = rom_module.detectVersion(rom_data, cached)
                return bytes(rom_data), version, cached
            # Stale cache (file moved or edited) — clear and re-prompt.
            settings.remove(cache_key)

        while True:
            path, _ = QFileDialog.getOpenFileName(
                self,
                f"Locate vanilla {expected_version} ROM for this project",
                "",
                "NDS ROMs (*.nds);;All files (*)",
            )
            if not path:
                return None
            try:
                rom_data = rom_module.loadRom(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Cannot read ROM", str(exc))
                continue
            actual = project_file.vanilla_sha256(bytes(rom_data))
            if actual != expected_sha256:
                QMessageBox.warning(
                    self,
                    "ROM doesn't match",
                    "The selected ROM doesn't match the SHA-256 this project "
                    "was built against. Pick the vanilla (unmodified) ROM the "
                    f"project expects ({expected_version}).",
                )
                continue
            settings.setValue(cache_key, path)
            version = rom_module.detectVersion(rom_data, path)
            return bytes(rom_data), version, path

    def _on_open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            "",
            "ROM Projects (*.romproj);;All files (*)",
        )
        if not path:
            return
        self._open_project_at(path)

    def _open_project_at(self, path: str) -> None:
        """Load `path`, resolve its vanilla baseline, and install the session.
        Shared by the Open Project dialog and the Recent Projects menu so the
        recent-list push and the error-path pruning live in one place.
        """
        try:
            project = project_file.load_project(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Failed to open project", str(exc))
            # Mirror the ROM-open failure path: a broken project shouldn't
            # keep being offered as a Recent entry.
            self._remove_from_recent(self._recent_projects, path)
            return
        resolved = self._resolve_vanilla_for_project(
            project["vanilla_rom_sha256"], project["rom_version"],
        )
        if resolved is None:
            return
        vanilla_bytes, version, vanilla_path = resolved
        if version != project["rom_version"]:
            QMessageBox.critical(
                self,
                "ROM version mismatch",
                f"Project was built for {project['rom_version']!r} but the "
                f"selected ROM identifies as {version!r}.",
            )
            return
        patched = bytearray(vanilla_bytes)
        project_file.apply_byte_diff(patched, project["diffs"])
        try:
            session = RomSession.from_project(
                project_path=path,
                vanilla_path=vanilla_path,
                vanilla_data=vanilla_bytes,
                version=version,
                patched_data=bytes(patched),
                qol_settings=project["qol"],
            )
            # Over-budget MSG.PAK edits weren't representable in the byte
            # diff — replay them onto the reparsed model so the editor sees
            # the user's text and the next ROM save runs the grow path.
            session.apply_string_edits(project["string_edits"])
            # Sprite PAK replacements live in their own channel so a grown
            # sprite doesn't fat-shift every later file into the byte diff.
            # Replay them so the next save splices them back in.
            session.apply_sprite_pak_edits(project["sprite_edits"])
            # Sidecar u32s for any BTCHR groups appended past vanilla 415.
            # Replayed AFTER apply_sprite_pak_edits — the 5 PAK appends per
            # group already rode the sprite_edits channel, so the appended
            # group count is now correct; this only catches up the chrsize/
            # btchrsize twins.
            session.apply_btchr_appended_sidecars(
                project.get("btchr_appended_sidecars", [])
            )
            # Per-path btmap file overrides land in the dirty cache; the
            # next ROM save runs them through the btmap FAT splice.
            session.apply_btmap_file_edits(project.get("btmap_edits", []))
            # Same channel for DAT/map/* field-map file overrides — paint
            # tool edits (walkability, tilemap) round-trip via here.
            session.apply_map_file_edits(project.get("map_edits", []))
            # Per-entry overlay5 overrides (Events tab x/y drags). The
            # entries are same-length, so this doesn't grow overlay5;
            # the next save splices them in through the overlay5 path.
            session.apply_overlay5_entry_edits(
                project.get("overlay5_entry_edits", [])
            )
            # Staged BGM swap payloads (Sound editor). Replayed onto the
            # swap cache so the next ROM save runs them through the
            # SDAT rebuild + splice path.
            session.apply_bgm_swap_edits(project.get("bgm_swap_edits", []))
            # Staged "Add As New Entry" additions; positional, applied
            # in list order by the same SDAT splice path.
            session.apply_bgm_addition_edits(project.get("bgm_addition_edits", []))
            # User-editable BGM slot labels — pure UI metadata (the ROM
            # itself doesn't carry them), so this replays into the
            # session's override dict without touching any splice path.
            session.apply_bgm_label_edits(project.get("bgm_label_edits", []))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to load project", str(exc))
            return
        self._push_recent(self._recent_projects, path)
        self._install_session(session)

    def _on_save_project_as(self) -> None:
        if self.session is None:
            return
        default = self.session.project_path or ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project As",
            default,
            "ROM Projects (*.romproj);;All files (*)",
        )
        if not path:
            return
        # The native save dialog already prompts when the user picks an
        # existing file, but it skips that check when we silently append the
        # `.romproj` extension below — guard explicitly for that case so we
        # don't quietly clobber a neighbour project.
        if not path.lower().endswith(".romproj"):
            path += ".romproj"
            if os.path.exists(path):
                confirm = QMessageBox.question(
                    self,
                    "Overwrite project?",
                    f"'{os.path.basename(path)}' already exists. Overwrite?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if confirm != QMessageBox.Yes:
                    return
        try:
            project_file.save_project(
                path,
                rom_version=self.session.version,
                vanilla_rom_data=self.session.original_rom_data,
                edited_rom_data=bytes(
                    self.session.serialize_all(
                        skip_sprite_splice=True,
                        skip_btmap_splice=True,
                        skip_map_splice=True,
                        skip_overlay5_splice=True,
                        skip_sound_splice=True,
                    )
                ),
                qol=self.session.qol,
                string_edits=self.session.msgpak_string_edits(),
                sprite_edits=self.session.sprite_pak_edits(),
                btchr_appended_sidecars=self.session.btchr_appended_sidecars(),
                btmap_edits=self.session.btmap_file_edits(),
                map_edits=self.session.map_file_edits(),
                overlay5_entry_edits=self.session.overlay5_entry_edits(),
                bgm_swap_edits=self.session.bgm_swap_edits(),
                bgm_addition_edits=self.session.bgm_addition_edits(),
                bgm_label_edits=self.session.bgm_label_edits(),
            )
        except OSError as exc:
            QMessageBox.critical(self, "Failed to save project", str(exc))
            return
        self.session.project_path = path
        self._push_recent(self._recent_projects, path)
        self.undo_stack.setClean()
        self._refresh_status()

    def _on_open_recent(self, path: str) -> None:
        if not self._confirm_discard_changes():
            return
        if not os.path.exists(path):
            QMessageBox.warning(
                self,
                "File not found",
                f"The ROM at\n{path}\nno longer exists. Removing from Recent.",
            )
            self._remove_from_recent(self._recent_roms, path)
            return
        self._load_rom(path)

    def _on_open_recent_project(self, path: str) -> None:
        if not self._confirm_discard_changes():
            return
        if not os.path.exists(path):
            QMessageBox.warning(
                self,
                "File not found",
                f"The project at\n{path}\nno longer exists. Removing from Recent.",
            )
            self._remove_from_recent(self._recent_projects, path)
            return
        self._open_project_at(path)

    # ---- slots -----------------------------------------------------------

    def _on_open(self) -> None:
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ROM",
            "",
            "NDS ROMs (*.nds);;All files (*)",
        )
        if not path:
            return
        self._load_rom(path)

    def _load_rom(self, path: str) -> None:
        try:
            session = RomSession.from_file(path)
        except Exception as exc:  # noqa: BLE001 — surface any load failure
            QMessageBox.critical(self, "Failed to open ROM", str(exc))
            clear_details_providers()
            clear_nav_handlers()
            validation_registry().clear()
            # A path the user tried but couldn't open shouldn't keep showing up
            # in the Recent list — prune it so the next launch isn't offering
            # a stale or broken file.
            self._remove_from_recent(self._recent_roms, path)
            return
        self._push_recent(self._recent_roms, path)
        self._install_session(session)

    def _install_session(self, session: RomSession) -> None:
        """Common post-construction wiring shared by ROM-open and project-open
        paths. Replaces any in-flight editor, resets validators/nav handlers,
        and clears the undo stack — the previous session's commands wouldn't
        apply against the new model objects."""
        self.session = session
        # Combos use this to render hover tooltips with extra context. Must be
        # installed before any editor referencing this session is constructed.
        set_details_providers(session)
        clear_nav_handlers()
        # One handler per nav-kind. Pickers default to the base-digimon editor
        # via `nav_kind="digimon"`; the wild-encounters picker explicitly opts
        # into the enemy editor by passing `nav_kind="enemy_digimon"`.
        register_nav_handler("digimon", "Open in Base Digimon editor",
                             lambda did: self.navigate_to_digimon(did, enemy=False))
        register_nav_handler("enemy_digimon", "Open in Enemy Digimon editor",
                             lambda did: self.navigate_to_digimon(did, enemy=True))
        register_nav_handler("standard_digivolution", "Open in Standard Digivolutions editor",
                             self.navigate_to_standard_digivolution)
        register_nav_handler("move", "Open in Move editor", self.navigate_to_move)
        register_nav_handler("item", "Open in Item editor", self.navigate_to_item)
        register_nav_handler("encounter_rewards", "Open in Encounter Rewards editor",
                             self.navigate_to_encounter_reward)
        # Validation collectors read from session data, not editor widgets — so
        # they stay valid whether the corresponding editor is open or not.
        # Reset on every load so the previous session's collectors can't
        # silently keep firing against stale model objects.
        reg = validation_registry()
        reg.clear()
        reg.register(lambda: base_digimon_issues(session.base_digimon))
        reg.register(lambda: std_digivolution_issues(session.standard_digivolutions))
        reg.register(lambda: armor_digivolution_issues(
            session.armor_digivolutions, session.base_digimon,
        ))
        reg.register(lambda: dna_digivolution_issues(
            session.dna_digivolutions, session.base_digimon,
        ))
        reg.register(lambda: move_issues(session.moves))
        reg.register(lambda: starter_issues(session.starters, session.base_digimon))
        reg.register(lambda: wild_encounter_issues(
            session.version,
            session.wild_encounter_areas,
            len(session.encounter_rewards),
        ))
        reg.register(lambda: encounter_reward_issues(session.encounter_rewards))
        reg.register(lambda: consumable_issues(session.consumables))
        reg.register(lambda: farm_terrain_issues(session.farm_terrains))
        reg.register(lambda: farm_item_issues(session.farm_items))
        reg.register(lambda: farm_training_pen_issues(
            session.farm_training_pens, session.sprite_pak("DAT/SPR_CEL.PAK"),
        ))
        reg.register(lambda: string_issues(session.string_regions))
        reg.notify_changed()
        self.undo_stack.clear()
        # Drop any open editor — it still references the previous session's
        # model objects, which serialize_all() would no longer write back.
        placeholder = QLabel("Pick a section on the left to begin editing.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setMargin(40)
        self.set_content(placeholder)
        # Clear BOTH the selection and the current index. `clearSelection()`
        # leaves the current index in place, so if a fresh QTreeView's default
        # current index happens to be row 0, the user's first click on row 0
        # wouldn't fire `currentChanged` and the editor wouldn't open.
        sel = self.nav_view.selectionModel()
        sel.clearSelection()
        sel.clearCurrentIndex()
        self._refresh_status()
        self._refresh_actions()

    def _save_guard_ok(self) -> bool:
        """Block save when an ARM9/overlay string overruns its byte budget
        OR a MSG.PAK string exceeds the 1024-byte engine cap.

        ARM9 / overlay strings have hardcoded code pointers, so an
        over-budget encoded string would overflow into the next field on
        disk — checked via ``over_budget_strings()``. MSG.PAK strings ride
        the §12 grow path so per-slot budgets don't apply, but the
        empirical 1024-byte engine cap (textbox renderer corrupts past it)
        still does — checked via ``over_cap_msgpak_strings()``. Returns
        True if save can proceed.
        """
        if self.session is None:
            return False
        bad = self.session.over_budget_strings()
        if bad:
            sample_lines = []
            for s in bad[:5]:
                preview = s.text.replace("[BR]", " ")[:60]
                sample_lines.append(
                    f"  • 0x{s.offset:08X}: {s.encoded_length()} / {s.original_byte_length} bytes — {preview!r}"
                )
            more = f"\n  …and {len(bad) - 5} more" if len(bad) > 5 else ""
            QMessageBox.critical(
                self,
                "Cannot save: strings over budget",
                f"{len(bad)} string(s) exceed their original byte budget and can't be saved "
                f"without misaligning ROM pointers.\n\n" + "\n".join(sample_lines) + more +
                "\n\nShorten or rewrite the offending strings, then save again.",
            )
            return False
        over_cap = self.session.over_cap_msgpak_strings()
        if over_cap:
            from digimon_core.model import MSGPAK_STRING_CAP
            sample_lines = []
            for s in over_cap[:5]:
                size = len(s.encoded_bytes_for_grow())
                preview = s.text.replace("[BR]", " ")[:60]
                sample_lines.append(
                    f"  • 0x{s.offset:08X}: {size} / {MSGPAK_STRING_CAP} bytes — {preview!r}"
                )
            more = (
                f"\n  …and {len(over_cap) - 5} more"
                if len(over_cap) > 5 else ""
            )
            QMessageBox.critical(
                self,
                "Cannot save: MSG.PAK string over engine cap",
                f"{len(over_cap)} MSG.PAK string(s) exceed the 1024-byte engine "
                f"cap. The in-game textbox renderer corrupts past this size "
                f"regardless of content.\n\n" + "\n".join(sample_lines) + more +
                "\n\nShorten the offending strings, then save again.",
            )
            return False
        return True

    def _on_save(self) -> None:
        if self.session is None:
            return
        if not self._save_guard_ok():
            return
        # Project sessions start with no source_path so "Save" doesn't
        # silently overwrite the vanilla ROM — fall through to Save As the
        # first time, then subsequent Saves reuse the chosen path.
        if not self.session.source_path:
            self._on_save_as()
            return
        # Capture the previous clean point *before* save+setClean so the
        # changelog reflects the edits this particular save persisted, not
        # the whole session history.
        prev_clean_ix = self.undo_stack.cleanIndex()
        try:
            saved_path = self.session.save()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to save ROM", str(exc))
            return
        record_save(self.session.original_rom_data, saved_path, self.undo_stack, prev_clean_ix)
        self.undo_stack.setClean()
        self._refresh_status()

    def _on_save_as(self) -> None:
        if self.session is None:
            return
        if not self._save_guard_ok():
            return
        # Default the dialog to the session's last ROM path when it exists;
        # otherwise (project sessions, fresh) leave it empty so the user picks
        # a fresh location instead of staring at "(unsaved)".
        default = self.session.source_path or ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROM As",
            default,
            "NDS ROMs (*.nds);;All files (*)",
        )
        if not path:
            return
        prev_clean_ix = self.undo_stack.cleanIndex()
        try:
            saved_path = self.session.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to save ROM", str(exc))
            return
        record_save(self.session.original_rom_data, saved_path, self.undo_stack, prev_clean_ix)
        self.undo_stack.setClean()
        self._refresh_status()

    def _on_nav_clicked(self, index) -> None:
        if not index.isValid():
            return
        key = index.data(Qt.UserRole)
        label = index.data(Qt.DisplayRole)
        if self.session is None or key is None:
            return
        with span(f"nav {key}"):
            with span("build_editor"):
                widget = self._build_editor_for(key)
            if widget is None:
                collection = getattr(self.session, key)
                count = len(collection)
                placeholder = QLabel(f"{label}\n\n{count} records parsed.\n\nEditor coming soon.")
                placeholder.setAlignment(Qt.AlignCenter)
                widget = placeholder
            with span("set_content"):
                self.set_content(widget)
            self._last_nav_key = key
        report_and_clear()

    def _build_editor_for(self, key: str) -> Optional[QWidget]:
        """Construct the editor widget for `key`, or None if unmapped.

        Centralises the per-key wiring so both `_on_nav_clicked` and the
        cross-reference `navigate_to_*` paths produce identical editors.
        """
        if self.session is None:
            return None
        if key == "base_digimon":
            return BaseDigimonEditor(
                self.session.base_digimon,
                self.undo_stack,
                self.session.sprite_map,
                self.session.battle_strings,
                self.session,
            )
        if key == "enemy_digimon":
            return EnemyDigimonEditor(
                self.session.enemy_digimon,
                self.undo_stack,
                self.session.sprite_map,
                self.session.battle_strings,
                self.session,
            )
        if key == "moves":
            return MoveEditor(self.session.moves, self.undo_stack, self.session)
        if key == "standard_digivolutions":
            widget = StandardDigivolutionEditor(self.session.standard_digivolutions, self.undo_stack, self.session)
            widget.treeRequested.connect(self.navigate_to_digivolution_tree)
            return widget
        if key == "digivolution_trees":
            widget = DigivolutionTreeEditor(self.session.standard_digivolutions, self.session)
            widget.digimonActivated.connect(
                lambda did: self._navigate_to_issue("standard_digivolutions", did)
            )
            return widget
        if key == "armor_digivolutions":
            return ArmorDigivolutionEditor(self.session.armor_digivolutions, self.undo_stack, self.session)
        if key == "dna_digivolutions":
            return DNADigivolutionEditor(self.session.dna_digivolutions, self.undo_stack, self.session)
        if key == "quests":
            return QuestEditor(self.session.quests, self.undo_stack, self.session)
        if key == "starters":
            return StartersEditor(self.session.starters, self.undo_stack)
        if key == "wild_encounter_areas":
            return WildEncountersEditor(
                self.session.version,
                self.session.wild_encounter_areas,
                self.undo_stack,
                self.session,
            )
        if key == "encounter_rewards":
            return EncounterRewardsEditor(self.session.encounter_rewards, self.undo_stack, self.session)
        if key == "habitats_worldmap":
            return HabitatsWorldmapEditor(self.session.habitats_worldmap, self.undo_stack, self.session)
        if key == "farm_terrains":
            return FarmTerrainsEditor(self.session.farm_terrains, self.undo_stack, self.session)
        if key == "equipment":
            return EquipmentEditor(self.session.equipment, self.undo_stack, self.session)
        if key == "consumables":
            return ConsumableEditor(self.session.consumables, self.undo_stack, self.session)
        if key == "farm_items":
            return FarmItemEditor(self.session.farm_items, self.undo_stack, self.session)
        if key == "farm_training_pens":
            return FarmTrainingPenEditor(self.session.farm_training_pens, self.undo_stack, self.session)
        if key == "qol_settings":
            return QolEditor(self.session.qol, self.undo_stack)
        if key == "sprite_browser":
            return SpriteBrowser(self.session, self.undo_stack)
        if key == "mchr_browser":
            return MchrBrowser(self.session, self.undo_stack)
        if key == "btchr_browser":
            return BtchrBrowser(self.session, self.undo_stack)
        if key == "btmap_browser":
            return BtmapBrowser(self.session, self.undo_stack)
        if key == "map_browser":
            return MapBrowser(self.session, self.undo_stack)
        if key == "sound_editor":
            return SoundEditor(self.session, self.undo_stack)
        if key.startswith("strings_bucket:"):
            bucket = key.split(":", 1)[1]
            prefix = _STRING_BUCKET_PREFIXES.get(bucket)
            if prefix is None:
                return None
            merged: list = []
            for region_id, region_strings in self.session.string_regions.items():
                if region_id.startswith(prefix):
                    merged.extend(region_strings)
            merged.sort(key=lambda s: s.offset)
            # MSG.PAK rides the §12 grow path on save, so over-budget edits
            # aren't an error here — let the editor show informational text
            # rather than a red warning.
            return StringEditor(
                merged,
                self.undo_stack,
                growable=(bucket == "msgpak"),
                session=self.session,
                cursor_key=key,
            )
        return None

    # ---- cross-reference navigation --------------------------------------
    # Right-click on a picker → "Open in X editor" routes through these. Each
    # method opens the appropriate section (building the editor if it isn't
    # already showing) and asks it to select the target id.

    def _highlight_nav_row(self, key: str) -> None:
        """Highlight the nav-tree row whose UserRole equals `key`.

        Walks every leaf under every group since the tree is two levels deep.
        Callers drive `set_content` themselves, so the selectionModel signal
        is blocked while we move the current index — otherwise `_on_nav_clicked`
        would fire and rebuild the editor on top of the one we just installed.
        """
        for group_row in range(self.nav_model.rowCount()):
            group_item = self.nav_model.item(group_row)
            if group_item is None:
                continue
            for leaf_row in range(group_item.rowCount()):
                leaf_item = group_item.child(leaf_row)
                if leaf_item is None or leaf_item.data(Qt.UserRole) != key:
                    continue
                index = self.nav_model.indexFromItem(leaf_item)
                sel = self.nav_view.selectionModel()
                sel.blockSignals(True)
                try:
                    sel.setCurrentIndex(
                        index,
                        QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
                    )
                finally:
                    sel.blockSignals(False)
                return

    def navigate_to_digimon(self, digimon_id: int, *, enemy: bool = False) -> None:
        key = "enemy_digimon" if enemy else "base_digimon"
        widget = self._build_editor_for(key)
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row(key)
        widget.select_by_id(digimon_id)

    def navigate_to_move(self, move_id: int) -> None:
        widget = self._build_editor_for("moves")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("moves")
        widget.select_by_id(move_id)

    def _navigate_to_issue(self, editor_key: str, record_id: Optional[int]) -> None:
        """Footer popup click → open the right editor and select the row.

        `record_id` is optional; non-record-scoped issues just open the
        editor without a select-by-id step.
        """
        widget = self._build_editor_for(editor_key)
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row(editor_key)
        if record_id is not None and hasattr(widget, "select_by_id"):
            widget.select_by_id(record_id)

    def navigate_to_standard_digivolution(self, digimon_id: int) -> None:
        """Picker right-click → open the Standard Digivolutions editor at the
        target digimon. Pickers inside the three digivolution editors use this
        instead of the base-digimon handler so the user follows evo edges
        without flipping back to the main digimon table."""
        widget = self._build_editor_for("standard_digivolutions")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("standard_digivolutions")
        widget.select_by_id(digimon_id)

    def navigate_to_digivolution_tree(self, digimon_id: int) -> None:
        """Standard Digivolution editor → Evolution Trees tab, navigated to
        the tree containing `digimon_id` with that node highlighted."""
        widget = self._build_editor_for("digivolution_trees")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("digivolution_trees")
        if hasattr(widget, "select_by_id"):
            widget.select_by_id(digimon_id)

    def navigate_to_wild_area(self, area_index: int) -> None:
        """Map browser Encounters tab → Wild Encounters editor at
        ``area_index`` (the wild-encounter-area list index)."""
        widget = self._build_editor_for("wild_encounter_areas")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("wild_encounter_areas")
        if hasattr(widget, "select_by_id"):
            widget.select_by_id(area_index)

    def navigate_to_map_encounters(self, map_id: int) -> None:
        """Wild Encounters editor "Used by maps" link → the field-map
        browser's Encounters tab at ``map_id``."""
        widget = self._build_editor_for("map_browser")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("map_browser")
        widget.navigate_to_map_encounters(map_id)

    def navigate_to_cutscene_chain(self, map_id: int, chain_ix: int) -> None:
        """Open the field-map browser at ``map_id`` on the Cutscenes tab,
        with the chain at global ``chain_ix`` selected.

        Called from the enemy digimon editor's "Appears in scripted events"
        cross-reference links. ``chain_ix`` is the position in
        ``session.cutscene_index().chains`` (not the sorted per-map
        subset the tab uses internally) — the MapBrowser handles the
        translation so callers don't need to know about the sort order.
        """
        widget = self._build_editor_for("map_browser")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("map_browser")
        widget.navigate_to_cutscene_chain(map_id, chain_ix)

    def navigate_to_encounter_reward(self, slot_index: int) -> None:
        """Wild-encounter reward_slot → open the encounter-rewards editor at
        the referenced table. Out-of-range slot values still open the editor
        (the editor itself shows the user what's available)."""
        widget = self._build_editor_for("encounter_rewards")
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row("encounter_rewards")
        if hasattr(widget, "select_by_id"):
            widget.select_by_id(slot_index)

    def navigate_to_item(self, item_id: int) -> None:
        # Items live in three separate tables keyed by id range; route to
        # whichever editor owns the id, matching the constants.ITEM_TYPE_IDS
        # mapping. Unknown ranges (DIGIEGG/KEY_ITEM/unmapped) silently no-op.
        ranges = constants.ITEM_TYPE_IDS
        if ranges["FARM_ITEM"][0] <= item_id <= ranges["FARM_ITEM"][1]:
            key = "farm_items"
        elif ranges["CONSUMABLE"][0] <= item_id <= ranges["CONSUMABLE"][1]:
            key = "consumables"
        elif ranges["EQUIPMENT"][0] <= item_id <= ranges["EQUIPMENT"][1]:
            key = "equipment"
        else:
            return
        widget = self._build_editor_for(key)
        if widget is None:
            return
        self.set_content(widget)
        self._highlight_nav_row(key)
        widget.select_by_id(item_id)
