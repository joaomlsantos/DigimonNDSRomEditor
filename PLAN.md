# Digimon NDS ROM Editor — Implementation Plan

A direct-editing companion to **DWDDRandomizer**. Where the randomizer takes a config and procedurally rewrites a ROM, the editor lets users hand-pick every value (digimon stats, movesets, evolutions, encounters, quests, items, etc.) and save out a custom ROM hack — comparable in concept to Pokémon difficulty hacks like Radical Red.

Supports the same ROMs the randomizer supports today: **Digimon World: Dawn (US)** and **Digimon World: Dusk (US)**.

---

## 1. What already exists in DWDDRandomizer that we can reuse

DWDDRandomizer is already structured as a "core + features + UI" stack. The first three layers are exactly what an editor needs.

| Layer | Files | What it provides | Editor reuse |
| --- | --- | --- | --- |
| **Model** | [DWDDRandomizer/src/model.py](../DWDDRandomizer/src/model.py) | Typed classes parsing byte-aligned ROM structures: `BaseDataDigimon`, `EnemyDataDigimon`, `MoveData`, `QuestData`, `EncounterRewardTable`, `StandardDigivolution`, `ArmorDigivolution`, `DNADigivolution`, `FarmTerrain`, `HabitatWorldmap`, `SpriteMapEntry`, `BattleStringEntry`. Enums: `Species`, `Element`, `DigimonType`, `LvlUpMode`, `ItemType`. | **Full reuse.** Every editor page maps to one of these classes. |
| **Constants** | [DWDDRandomizer/src/constants.py](../DWDDRandomizer/src/constants.py) (≈5k lines) | Version-keyed offsets for every data table, `HEADER_VALUES`, `DIGIMON_ID_TO_STR`, `DIGIMON_IDS` (by stage), `MOVE_ARRAY_STR`, `TRAIT_ARRAY_STR`, `ITEM_ID_TO_STR`, `DIGIVOLUTION_CONDITIONS`, `FARM_TERRAINS_NAMES`, `LOCATION_OFFSETS_TO_NAMES`, etc. | **Full reuse.** This is the most expensive asset to recreate. |
| **Loaders** | [DWDDRandomizer/src/utils.py](../DWDDRandomizer/src/utils.py) | One loader per data table: `loadBaseDigimonInfo`, `loadEnemyDigimonInfo`, `loadQuestData`, `loadMoveData`, `loadEncounterRewardData`, `loadStandardDigivolutions`, `loadArmorDigivolutions`, `loadDnaDigivolutions`, `loadSpriteMapTable`, `loadBattleStringTable`, `loadHabitatsWorldmap`, `loadLvlupTypeTable`. | **Full reuse.** These produce the in-memory model graph from a `bytearray`. |
| **ROM I/O & header check** | [DWDDRandomizer/qol_script.py](../DWDDRandomizer/qol_script.py) `DigimonROM` | `loadRom`, `writeRom`, `checkHeader` (maps NDS header bytes → `DUSK_US`/`DAWN_US`/etc). | **Reuse the loader; the writer is fine.** QoL byte-patches stay in the randomizer (we expose them as toggles, not editor pages). |
| **Config / TOML** | [DWDDRandomizer/configs.py](../DWDDRandomizer/configs.py) | `PreferencesManager` for TOML round-trip. | **Partial reuse** — useful pattern, but the editor needs a different "project file" format (see §4). |

### What's missing in the model layer

**Update:** every model now has a `writeToRom(rom_data)`. The table below is kept as historical context; all rows are implemented today and exercised by `smoke_test.py` round-trip checks on both DUSK_US and DAWN_US ROMs. Item models (`Equipment`, `Consumable`, `FarmItem`) were added beyond the original list once item-data structures landed.

| Class | Has parser | Has serializer |
| --- | --- | --- |
| `BaseDataDigimon` | yes | yes |
| `EnemyDataDigimon` | yes | yes |
| `MoveData` | yes | yes |
| `QuestData` | yes | yes |
| `FarmTerrain` | yes | yes |
| `StandardDigivolution` | yes | yes |
| `ArmorDigivolution` | yes | yes |
| `DNADigivolution` | yes | yes |
| `EncounterRewardTable` | yes | yes |
| `HabitatWorldmap` | yes | yes |
| `SpriteMapEntry` / `BattleStringEntry` | yes | yes |
| `WildEncounterArea` / `WildEncounter` | yes | yes |
| `Equipment` / `Consumable` / `FarmItem` | yes | yes |
| `StarterEntry` | yes | yes |

---

## 2. Architectural decision: `digimon_core` lives inside the editor repo

The original plan was to extract a workspace-level `digimon_core/` shared by both DWDDRandomizer and the editor. In practice the randomizer kept its own `src/model.py` / `src/constants.py` / `src/utils.py` and never adopted the shared package, so "shared" was theoretical.

To match reality, `digimon_core/` now lives inside `DigimonNDSRomEditor/` as a normal sub-package:

```
DigimonNDSRomEditor/
├── digimon_core/                  ← model.py, constants.py, loaders.py, rom.py, tests/
├── editor/                        ← Qt UI
├── main.py
└── smoke_test.py
```

If the randomizer ever needs to consume the shared model, the extraction is mechanical — copy `digimon_core/` back out and have both repos import it. Until then, keeping it co-located with its only real consumer avoids cross-repo drift and simplifies version control.

**Stack for the editor itself:**
- **Python 3.9+** (matches randomizer; lets us share code without an FFI boundary).
- **PySide6** (Qt) for the UI rather than Tk. The editor is fundamentally a list-of-tables app — `QTableView`/`QTreeView` + Qt's model-view + the built-in `QUndoStack` give us undo/redo, sortable/filterable tables, and proper dock layout essentially for free. Tk would force us to reinvent all of this.
- Keep `tomli`/`toml` for project-file I/O.
- `pytest` for round-trip tests (parse → serialize → byte-equal on vanilla ROM).

---

## 3. Core additions before any UI work — ✅ done

These are prerequisites — UI is dead weight without them.

1. ~~**Symmetric serializers.**~~ Done — `writeToRom(rom_data)` lives on every model and is exercised by `smoke_test.py` (vanilla → edit → undo → vanilla, byte-equal).
2. ~~**A `RomSession` object**~~ Done — `editor/session.py` owns `original_rom_data`, the parsed graph, source path, dirty flag, and a `serialize_all()` that rebuilds the output bytes.
3. ~~**A `Command` abstraction**~~ Done — `editor/commands.py` (`SetAttrCommand` etc.) plugs into `QUndoStack`; every bound widget pushes commands.
4. ~~**Validators** centralized.~~ Done as **inline widget caps + footer aggregation** rather than a single `digimon_core/validation.py` module. Each editor exposes a `*_issues()` collector function; the validation footer (`editor/widgets/validation.py`) aggregates them into a click-to-navigate popup. Widget-level caps catch out-of-range stat/MP/ATK values at the source; the footer surfaces semantic issues (reciprocal mismatches, exp_curve ranges, reward_slot overruns, starter level vs aptitude, etc.).
5. ~~**Cross-reference index.**~~ Done — `register_nav_handler` in `editor/widgets/form_helpers.py` exposes "Open in X editor" for digimon/move/item/encounter-reward pickers; the standard-digivolution editor links to the tree viewer via `treeRequested`; the validation footer routes clicks to the offending editor + record id.

---

## 4. Editor UI design

A single window with a left-hand navigation tree picking the data domain, and a center panel showing a master list + a detail editor for the selected entry. Standard ROM-editor layout (cf. PKHeX, Universal Pokémon Randomizer, AdvanceMap).

```
┌──────────────────────────────────────────────────────────────┐
│ File  Edit  View  Tools  Help                                │
├──────────┬───────────────────────────────────────────────────┤
│ Digimon  │ ┌────────── master list ───────────┐ ┌─ detail ─┐ │
│ ├ Base   │ │ ID | Name      | Stage | Species │ │ HP: 120  │ │
│ ├ Enemy  │ │ 97 | Agumon    | Rooki | Dragon  │ │ MP:  60  │ │
│ │  data  │ │ 98 | Patamon   | Rooki | Holy    │ │ ATK: 90  │ │
│ ├ Evos   │ │ ...                              │ │ Traits…  │ │
│ ├ DNA    │ └──────────────────────────────────┘ │ Moves…   │ │
│ Moves    │                                      │ Resists… │ │
│ Traits   │                                      └──────────┘ │
│ Items    ├──────────────────────────────────────────────────┤
│ Quests   │ Status: 3 unsaved changes • Source: Dusk_US.nds  │
│ ...      └──────────────────────────────────────────────────┘
└──────────┘
```

### Pages (one per data domain)

Mapped directly onto the model classes. Navigation tree is grouped (Digimon Data / Digivolutions / Dungeons / Items) in the left pane.

1. **Digimon → Base Data** ✅ — every field of `BaseDataDigimon`: stats, species, type, traits, moves, resistances, signature move, dex habitat, scannable flag, exp curve.
2. **Digimon → Enemy Data** ✅ — `EnemyDataDigimon`: as above but for enemy variants, plus per-species EXP yields. **Fixed-battle (tamers/bosses) coverage lives here** — most `<unknown>` entries in the enemy table correspond to scripted battle slots, edited through the same form (reskin via sprite-map combo, battle-string overrides). A separate Fixed Battles page (originally §4.11) was therefore folded into this editor and not built standalone.
3. **Digivolutions → Standard** ✅ — per-digimon detail (degeneration target + 3 evolutions × 3 conditions each). "Show in evolution tree" jumps to the tree viewer (page 4).
4. **Digivolutions → Evolution Trees** ✅ *(beyond plan)* — read-only forest of forward-evolution trees rooted at digimon with no pre-evolution; filter searches across every descendant name so e.g. typing "Greymon" surfaces Koromon's root. Mirrors DWDDRandomizer's `digivolution_tree_logger.py` semantics (per-tree dedupe for DAG-shaped graphs).
5. **Digivolutions → Armor** ✅ — armor digivolutions; per-entry: source digimon, trigger item, target digimon, conditions.
6. **Digivolutions → DNA** ✅ — DNA recipes: two source digimon + result + conditions.
7. **Moves** ✅ — `MoveData` table; per-entry MP cost, element, effects, hits, range, level learned.
8. **Traits** — name-only lookup; used as trait picker dropdowns on the Digimon page. No standalone editor (deferred until trait effects research lands; unchanged from plan).
9. **Items** ✅ *(beyond plan)* — promoted to real editors because item-data structures got modeled: **Equipment**, **Consumables**, **Farm Items**. Per-entry attribute editing (atk_boost, bit_cost, max_points, etc.).
10. **Dungeons → Wild Encounters** ✅ — areas labelled via `getCurrentLocation`; per-area header (rate bounds) + per-encounter digimon + reward_slot. `num_encounters` field is hidden until slot insertion/deletion is reverse-engineered.
11. **Dungeons → Encounter Rewards** ✅ — `EncounterRewardTable` per area: probability × reward (item or money).
12. **Quests** ✅ — `QuestData`: rewards (money/item/tamer points), unlock conditions, flags.
13. **Starters** ✅ — the starter packs; pick digimon per pack + starting level (level-vs-aptitude cap validated).
14. **Farm Terrains** ✅ — `FarmTerrain`: digimon limit, per-species EXP yields.
15. **Habitats / Worldmap** ✅ — `HabitatWorldmap` entries: species shown per location, location flags.
16. **QoL toggles** ✅ — DWDDRandomizer's byte-patches surfaced as checkboxes (text speed, movement speed, scan rate, farm exp, battle perf, version-exclusive areas, …). `RomSession.serialize_all_with_qol()` applies enabled patches on top of model writes at save time.
17. **Text / Strings** ✅ *(beyond plan)* — in-game text editor with three buckets in the nav tree: **ARM9**, **Overlays**, **MSG.PAK**. Each bucket merges its constituent regions into a single offset-sorted list with substring search; the right pane has a [BR]/[END]/marker-aware text edit + a live "X / Y bytes — Z free" budget meter. Strings are at fixed ROM offsets (pointers aren't repointed), so per-string `original_byte_length` is a hard cap — over-budget edits are flagged in the validation footer and the Save path refuses to write until they're shortened. MSG.PAK is parsed as a single full-range region (not the named sub-blocks from the research doc) so dev/debug strings and tail content stay editable.

### UI patterns shared across pages

- Master list = `QTableView` with sort/filter/search.
- Detail editor = form layout; ID-typed fields use combos backed by `DIGIMON_ID_TO_STR` / `MOVE_ARRAY_STR` / `ITEM_ID_TO_STR`.
- Every editable widget is wrapped so its `valueChanged` signal pushes a `Command` onto the undo stack; this is what gives us Ctrl+Z for free across the whole app.
- "Revert this entry to vanilla" button per page — needs a copy of the originally-loaded model graph kept around (`session.original` vs `session.current`).
- Cross-reference links: clicking a digivolution target switches to the Base Data page focused on that digimon. Implemented as Qt signals on a central `Navigator` service.
- Tabs / sub-pages keep stat blocks, moves, traits, resistances visually grouped.

---

## 5. Persistence: ROM out + project file

Save paths:

1. **Save Patched ROM** ✅ (`File → Save` / `Save As…`)
   Walks every loaded model, calls `writeToRom(rom_data)` on a fresh copy of the original ROM bytes (`RomSession.serialize_all_with_qol()` — model writes then QoL byte-patches on top), then writes the `.nds`. **Rolling `.bak`** alongside the target: `shutil.copy2(target, target + ".bak")` before each overwrite so a botched write (or a regretted save) doesn't destroy the previous on-disk copy. Save is gated on `session.over_budget_strings()` — any string whose encoded bytes exceed its slot would clobber the next field on disk, so the writer refuses and the validation footer points to the offenders.
2. **Save Project** (`.romproj`) ✅ — JSON file capturing `(format_version, editor_version, rom_version, vanilla_sha256, qol_settings, byte_diff)`. Byte-range diffs (`compute_byte_diff` walks vanilla and edited in lockstep emitting `(offset, bytes)` per mismatch run) won over field-level for robustness against model changes. Opening a project verifies vanilla SHA-256, applies the diff, reparses the model graph. QoL state is stored separately from the diff so it doesn't compound across round-trips.
3. **Generate IPS/xdelta patch** for distribution. Still v0.2+.

### Persistence sidecars already shipped

- **Rolling `.bak`** next to the saved ROM (see above).
- **Per-save changelog** under `<AppDataLocation>/DigimonNDSRomEditor/changelogs/<rom_hash>/`:
  - `index.txt`: one line per save (`timestamp · N edits · saved_path`).
  - `<timestamp>.log`: detail file with the `QUndoCommand.text()` of every command between the previous clean index and `index()`. Backward-direction saves (user undid past a previous clean point and saved again) write `[undone] <text>` markers. Action-text only for v1; before/after value capture deferred.
  - `<rom_hash>` is a SHA-256 prefix of the *vanilla* bytes, so Save As to a new path and re-opens of the same base ROM share history.
  - File menu has "Open Changelog Folder" entry.
- **Recent files**: 5-slot list under `File → Open Recent`, deduped by normalised absolute path. Failed/missing-file opens prune themselves.
- **Window state**: geometry, splitter sizes, last-selected nav key persisted via `QSettings` and restored on next launch.
- **Close-confirm on unsaved changes**: `closeEvent` and `Open` both route through `_confirm_discard_changes()` (Save/Discard/Cancel). A failed Save is treated as Cancel to avoid silent data loss.

---

## 6. Milestones

Each milestone is independently shippable.

### M0 — Scaffolding (1–2 days) — ✅ done
- ~~Create `digimon_core` package, move `src/model.py`, `src/constants.py`, `src/utils.py` into it.~~ Done — `digimon_core/` lives under `DigimonNDSRomEditor/`. (DWDDRandomizer was left on its own copy.)
- ~~Add serializers + round-trip test on vanilla Dusk + Dawn ROMs.~~ Done — see `digimon_core/tests/test_roundtrip.py` and `smoke_test.py`.
- ~~PySide6 project skeleton with `RomSession`, `Command`, `QUndoStack`, navigation tree, File menu.~~ Done.

### M1 — Digimon Base Data editor — ✅ done
- ~~Full editing of `BaseDataDigimon` for every digimon.~~
- ~~Undo/redo working end-to-end on this page.~~
- ~~Save Patched ROM that round-trips through `writeToRom`.~~

### M2 — Movesets, Traits, Resistances — ✅ done
- ~~Move editor (`MoveData`).~~
- ~~Polish the trait/move/resistance pickers on Base Data.~~

### M3 — Digivolutions — ✅ done
- ~~Standard digivolution editor.~~
- ~~Armor + DNA digivolution editors.~~
- Added beyond plan: read-only Evolution Trees viewer (§4.4).

### M4 — Encounters — ✅ done
- ~~Wild encounter tables (by area).~~
- ~~Encounter reward tables.~~
- ~~Fixed battles (tamers/bosses).~~ Folded into the Enemy Data editor — see §4.2.
- ~~Starters page.~~

### M5 — Quests, Farm, Worldmap — ✅ done
- ~~Quest editor.~~
- ~~Farm terrain editor.~~
- ~~Habitat / worldmap editor.~~
- Item editors (Equipment, Consumables, Farm Items) added beyond plan.

### M6 — QoL tab + Project files + Polish — ✅ done
- ~~Surface QoL toggles, wire into the save path.~~ Done — `editor/widgets/qol_editor.py` + `serialize_all_with_qol()`.
- ~~Project-file save/load with diff format.~~ Done — `.romproj` JSON with byte-range diffs against vanilla; see §5.2.
- ~~Cross-reference jump-to-related-entry~~ — done (nav handlers + footer routing + tree-viewer link).
- Per-editor list filters in place; **cross-page search** still pending.
- Shipped beyond plan in this milestone: validation footer, recent-files menu, window-state persistence, rolling `.bak` on save, per-save changelog, close-confirm on unsaved changes, Strings editor (§4.17), over-budget save guard, perf-snapshot fast path on string-budget validation.

### M7 — Distribution — not yet
- Packaging (PyInstaller, mirroring DWDDRandomizer's `.spec`).
- Optional: IPS/xdelta patch export.

---

## 7. Risks & open questions

- ~~**Models without serializers are a hidden cost.**~~ Resolved — every model is covered; `smoke_test.py` runs the round-trip on every editor on both DUSK_US and DAWN_US.
- ~~**Unknown fields exposure.**~~ Resolved — `View → Show unknown fields` toggle (off by default) reveals the `unknown_0xN` properties across every editor.
- ~~**Item structure isn't modeled.**~~ Resolved — Equipment, Consumables, and Farm Items each have full editors.
- **Sprite/portrait/string editing is still out of scope.** `SpriteMapEntry` / `BattleStringEntry` are surfaced indirectly (enemy reskin combo, battle-string overrides on the enemy editor) but no standalone graphics editor — sprite data isn't fully decoded yet (everything beyond pointer-swaps needs more research).
- **Region support** still sticks to `DUSK_US`/`DAWN_US`. JP coverage can extend later by filling the offset tables.
- **Concurrent edits across pages** — handled by editors re-reading the model on `_on_selection` / `_on_undo_redo` rather than caching widget values; works in practice. Footer collectors read from the session, not from open widgets, so they stay valid even when the relevant editor isn't constructed.
- **Shutdown race in module-level singletons.** Fixed for the validation registry (`_safe_emit_changed` swallows `RuntimeError` from emitting on a deleted QObject during Qt teardown). Worth keeping in mind for any future module-level `QObject` singleton.

---

## 8. Recommended starting point — historical

Original sequencing (M0 first, then M1 as vertical slice) executed as planned. The editor reached feature parity with DWDDRandomizer's data coverage at the end of M5 as predicted; M6 polish is now the active milestone.

## 9. What's next (current focus)

The data-coverage milestones (M0–M6) are all done. The active push is on workflow / discoverability features that make the editor faster to use on a real romhack.

Ordered by load-bearing-ness:

1. **Cross-reference / "Used by" panel** (§10) — when viewing a digimon (or move, or item), show every record that references it: evolutions in both directions, encounters, starters, drops, quest rewards, DNA recipes. Uniquely valuable for this game's interconnected data; can't be done outside the editor. Reuses the existing nav-handler routing.
2. **Cross-page search** — single search box that queries every editor's record list (digimon names, move names, item names, in-game strings) and routes click-throughs via the nav handlers / footer routing.
3. **Bulk-edit operations** — apply a transform across a record set with a diff preview before commit. Saves hundreds of clicks for balance passes.
4. **Header toolbar with icon buttons** — Ghidra-style icon row for Save ROM / Save Project / Export (saved memory: `project_future_header_bar.md`).
5. **Packaging (M7)** — PyInstaller spec mirroring DWDDRandomizer's.
6. **IPS/xdelta patch export** — v0.2+; useful once distribution workflow is established.

Beyond plan but possibly worth picking up:
- Before/after value capture in changelog entries (currently action-text only).
- "Revert this entry to vanilla" per editor (needs the `session.original` reference path; partly implementable today via re-parsing a slice of `original_rom_data`).
- About dialog with editor version + ROM version readback.
- Wider validation coverage (encounter rewards, equipment, consumables, farm items have no `*_issues()` collectors yet).
- Strings export/import (CSV/JSON) — deferred: byte-budget per string limits translation workflow value.
- Randomizer integration — seed-based randomization of evolution trees / starters / encounters; pairs with the cross-reference panel.

---

## 10. Plan: Cross-reference / "Used by" panel

**Goal.** From any record in the editor, see every other record that references it, with click-to-jump for each reference. Initial scope: digimon, moves, items, standard digivolutions (the four most interconnected entity types). Romhackers routinely break routes by editing a digimon without seeing what's wired into it; this panel is the antidote.

### 10.1 Reference inventory

What references what, by entity type. Drawn directly from the model classes — every row below maps to a literal field on a literal class.

| Target entity | Source record | Source field(s) |
| --- | --- | --- |
| **Digimon (by id)** | `StandardDigivolution` | per-entry `id` (self), `degeneration_target`, each `digivolutions[i].target` |
| | `ArmorDigivolution` | `source_id`, `target_id` |
| | `DNADigivolution` | `source_1_id`, `source_2_id`, `result_id` |
| | `EnemyDataDigimon` | row id (enemy variant of a base digimon) |
| | `WildEncounter` (within `WildEncounterArea`) | `digimon_id` |
| | `StarterEntry` | `digimon_id` |
| | `BaseDataDigimon` | own row — for completeness, plus other digimon's `signature_move` learners can be derived via the move panel |
| **Move (by id)** | `BaseDataDigimon` | `moves[i].move_id`, `signature_move` |
| | `EnemyDataDigimon` | `moves[i].move_id`, `signature_move` |
| **Item (by id)** | `EncounterRewardTable` | `rewards[i].reward_id` (when reward kind == item) |
| | `QuestData` | reward fields (item kind) |
| | `ArmorDigivolution` | `trigger_item` |
| | `StandardDigivolution` / `ArmorDigivolution` / `DNADigivolution` | any condition referencing an item id (TBD per-condition decoding) |
| **Standard digivolution (by digimon id)** | reverse-evolution edges: every other `StandardDigivolution` whose `digivolutions[i].target` matches | (already used by the Evolution Trees viewer's pre-evolution walk) |

### 10.2 Architecture

**`editor/xref.py`** — pure-data reverse index. One class `XrefIndex` with:

- A constructor that takes a `RomSession` and builds reverse maps in O(N) one-shot.
- Query methods returning `List[XrefRef]`:
  - `references_to_digimon(digimon_id) -> List[XrefRef]`
  - `references_to_move(move_id) -> List[XrefRef]`
  - `references_to_item(item_id) -> List[XrefRef]`
  - `references_to_evolution_source(digimon_id) -> List[XrefRef]` (reverse evolution edges)
- An `XrefRef` dataclass mirroring `ValidationIssue`'s shape so the same nav-handler infra moves rows:
  ```python
  @dataclass(frozen=True)
  class XrefRef:
      group: str          # "Evolutions", "Encounters", "Starters", "DNA Recipes", …
      label: str          # human-readable: "Greymon — evolves from (lvl 14)"
      editor_key: str     # routes through _build_editor_for()
      record_id: Optional[int]   # passed to widget.select_by_id() after open
  ```
- No QObject inheritance; pure functions tested in isolation against a fixture session.

The session owns one `XrefIndex` instance, rebuilt on `_install_session()` and on every undo-stack `indexChanged` (same hook the validation registry already uses). Build cost target: **< 10 ms** on Dusk — same envelope as the post-fix `string_issues()`. If we miss that target, fall back to dirty-flag invalidation per entity type.

### 10.3 UI

**`editor/widgets/xref_panel.py`** — a single collapsible widget:

```
┌─ Used by (12) ────────────────────────────────[ ▼ ]─┐
│ Evolutions (4)                                       │
│   • Greymon — evolves to (lvl 14)         [open →]   │
│   • Koromon — evolves from (lvl 8)        [open →]   │
│   • Greymon X — evolves to (lvl 30)       [open →]   │
│   • Tyrannomon — DNA recipe partner       [open →]   │
│ Encounters (3)                                       │
│   • Tropical Jungle slot 4 (15% rate)     [open →]   │
│   …                                                  │
│ Starters (1)                                         │
│   • Pack 3, level 5                       [open →]   │
└──────────────────────────────────────────────────────┘
```

- One row per `XrefRef`, grouped by `group` header.
- Click anywhere on the row → `_navigate_to_issue(editor_key, record_id)` (reuse the existing main-window method; rename to `_navigate_to_key` if its docstring's "issue" framing reads wrong).
- Collapsed by default if `len(refs) == 0`; auto-expanded otherwise. Persisted to `QSettings` like the other panel-state bits.

**Placement.** Two options, decide before implementing:

- **(a) Bottom of the detail form.** Lives inside the editor widget, scrolls with the form. Pro: stays attached to the record. Con: requires plumbing into four editors; vertical space competes with the form fields.
- **(b) Right-side dock that reacts to selection.** One dock for all editors; the dock listens for a selection-changed signal and re-queries the index. Pro: zero-touch in the editors; consistent location. Con: needs a session-wide "what's selected right now" signal which doesn't exist today.

Preference is **(a)** for the first pass — touch each editor once, ship something useful, defer the cross-cutting selection signal until we have a second consumer (cross-page search would be that consumer).

### 10.4 Phasing

| Phase | Output | Effort |
| --- | --- | --- |
| **A — Index + tests** | `editor/xref.py` with `XrefIndex`, `XrefRef`, full reverse-map build, unit tests on a real session covering each `references_to_*` method | 0.5 d |
| **B — Panel widget** | `XrefPanel(refs: List[XrefRef], navigate: Callable[[str, Optional[int]], None])` rendering grouped rows; reused across editors | 0.5 d |
| **C — Wire into Base Digimon editor** | Panel under the detail form; rebuild on selection change, refresh on `indexChanged`. Validate UX against a real Dusk ROM session before moving on. | 0.5 d |
| **D — Wire into Enemy Digimon, Move, Item editors** | Same pattern, three editors. Each takes ~1 hr if Phase C lands the shape. | 0.5 d |
| **E — `select_by_id` audit** | A few editors don't expose `select_by_id` yet (or expose it under a different name). Add where missing so click-through always lands on the right row. | 0.25 d |

Phase A is independently shippable as an API — the panel can be added later. Phases C–E benefit from being verified in a browser/session before moving on (per the UI-testing guidance), not just type-checked.

### 10.5 Risks

- **Index rebuild cost.** If `XrefIndex` rebuild on every `indexChanged` exceeds ~10 ms, the perf regression we just fixed comes back through a different door. Mitigation: benchmark before wiring to `indexChanged`; if too slow, swap to per-entity-type dirty flags driven by the `SetAttrCommand` path.
- **Field-path naming drift.** The inventory in §10.1 references field names that need to be cross-checked against the actual model classes before coding — anything renamed since the table was written will fail at import time. Resolve by grep-confirming each field name during Phase A.
- **Item-condition decoding.** Some digivolution conditions reference items but the per-condition decoding isn't centralized today (lives inline in each digivolution editor). The first cut can skip item-condition refs and revisit once a `digivolution_conditions` decoder helper exists.
- **Click-through to in-game strings.** The strings editor already exposes `select_by_id(offset)`. Strings aren't in the §10.1 inventory yet, but if cross-page search lands as the next workflow item it'll want the same routing.

### 10.6 Acceptance criteria

- Opening the Base Digimon editor and selecting Agumon shows a populated "Used by" panel with at least: evolutions (both directions), at least one wild encounter, the starter pack entry, every DNA recipe Agumon participates in.
- Clicking any row opens the named editor and selects the named record.
- The panel updates within 50 ms of switching selections in the master list.
- An edit that adds a reference (e.g. changing a `StandardDigivolution.digivolutions[0].target` to Agumon) makes the panel reflect the new reference within one undo-stack tick.
- The whole feature adds < 10 ms to per-keystroke validation refresh on a clean Dusk ROM.
