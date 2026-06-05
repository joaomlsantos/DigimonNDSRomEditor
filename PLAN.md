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
17. **Text / Strings** ✅ *(beyond plan)* — in-game text editor with three buckets in the nav tree: **ARM9**, **Overlays**, **MSG.PAK**. Each bucket merges its constituent regions into a single offset-sorted list with substring search; the right pane has a [BR]/[END]/marker-aware text edit + a live "X / Y bytes — Z free" budget meter. **ARM9 / Overlay** strings are at fixed ROM offsets with absolute pointers baked into code, so per-string `original_byte_length` is a hard cap — over-budget edits are flagged in the validation footer and the Save path refuses to write until they're shortened. **MSG.PAK** is parsed as a single full-range region (not the named sub-blocks from the research doc) so dev/debug strings and tail content stay editable, and — because it's a FAT-listed container with its own internal offset table — has **no hard byte cap**: edits that exceed the original slot route through the pak-directory rewrite + FAT-resize path documented in §12. The budget meter on MSG.PAK strings is therefore informational only; no Save gating.

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
   Walks every loaded model, calls `writeToRom(rom_data)` on a fresh copy of the original ROM bytes (`RomSession.serialize_all_with_qol()` — model writes then QoL byte-patches on top), then writes the `.nds`. **Rolling `.bak`** alongside the target: `shutil.copy2(target, target + ".bak")` before each overwrite so a botched write (or a regretted save) doesn't destroy the previous on-disk copy. Save is gated on `session.over_budget_strings()` **restricted to ARM9 / Overlay regions** — those have absolute pointers baked into code and cannot grow without repointing work that isn't built yet, so the writer refuses and the validation footer points to the offenders. MSG.PAK strings are not gated (see §4.17, §12): over-budget edits there flow through the pak-directory rewrite + FAT-resize path. Saves preserve the original ROM length whenever possible — see §12.3.
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
- ~~**Sprite/portrait editing is still out of scope.**~~ Partially unblocked — NCGR/NCLR round-trip is now working (see §11). Pointer-swap reskins (`SpriteMapEntry`) remain the only in-editor mechanism today; actually swapping pixels still requires the standalone `rom_files/ncgr_to_png.py` converter + manual ROM injection. A first-class sprite editor is now a feasible next push (§11). String editing is no longer out of scope and ships in §4.17.
- **Region support** still sticks to `DUSK_US`/`DAWN_US`. JP coverage can extend later by filling the offset tables. §13 (FAT-driven loading) additionally unlocks compatibility with language-patched DUSK_US / DAWN_US ROMs where the patch grew FAT-listed files (translation jobs typically extend MSG.PAK) — see §13.7.
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
5. **Sprite editor (§11)** — newly feasible now that NCGR/NCLR round-trip works and the load-bearing RAHC+0x12 field is understood. First domain in the editor that touches graphics rather than data tables; opens the door to portrait/UI reskins from inside the app.
6. **Packaging (M7)** — PyInstaller spec mirroring DWDDRandomizer's.
7. **IPS/xdelta patch export** — v0.2+; useful once distribution workflow is established.

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

---

## 11. Plan: Sprite editor (Graphics page)

**Goal.** Edit DWDD's tile-based sprites (portraits, UI frames, battle minis, move animations) from inside the editor: pick a sprite, export to PNG for external editing in GIMP/Aseprite, then re-import and have the change take effect in-game. First domain in the editor that touches pixel data; built on the round-trip work in `rom_files/ncgr_to_png.py`.

### 11.1 Findings from the standalone converter work

Established empirically while building `rom_files/ncgr_to_png.py` (decode/encode) and `rom_files/inject_test.py` (ROM-level splice):

- **File format.** DWDD's `SPR_*.PAK` files are flat containers whose directory entries (4-byte offset + 4-byte size, size's high bit being a 0x80 flag) point at NDS-standard NCGR (tile data) and NCLR (palette) files. Cell layout / animation lives in companion NCER (`SPR_CEL.PAK`) and NANR (`SPR_ANM.PAK`). Every PAK entry is **RLE-0x30 compressed** (NDS BIOS RLE: header `0x30 + 24-bit out size`, MSB-flagged flag bytes for runs vs literals). `SPR_CHR.PAK` alone contains 1627 entries.
- **Round-trip works.** Decode (`NCGR + NCLR → PNG`) and encode (`PNG → NCGR + NCLR`) are byte-stable for tile data given the same palette. Compression is also reversible — random fuzz survives `compress_rle30 → decompress_rle30`.
- **RAHC+0x12 is load-bearing and per-sprite.** Most NCGRs use `0x0000` there, but a subset (e.g. Julia's portrait 1566, sprite 0500) use `0x0020`. Zeroing it on re-encode does not glitch colors — it shifts tile rows (bottom row missing, top row duplicated). Diagnosis path was a byte-diff of the offending NCGR; preservation via `--template-ncgr` in `rom_files/ncgr_to_png.py encode` fixed it. The field is metadata about how the loader allocates the sprite, not a parameter the tile bytes are encoded against; same-shape tile data under the original header always works (saved memory: `project_ncgr_rahc_header_preserve.md`).
- **Width during decode is cosmetic.** Tile-byte order in the NCGR is a flat raster sequence; the `--width` flag only changes the PNG's tile-row wrap for human viewing. Re-encode infers `(tw, th)` from PNG dimensions and walks tiles in the same raster order. Only gotcha: if `n_tiles` doesn't divide evenly by the chosen width, decode pads with transparent slots and re-encode treats those as real all-zero tiles, inflating the NCGR. For exact round-trips pick a divisor.
- **Palette flexibility.** 4bpp NCGRs always need 16 palette slots even when fewer colors are used (unused slots padded with `(0,0,0)`); the engine ignores them but they must be present. Slot 0 is hardware-transparent on OBJ layer — the encoder supports `--transparent-rgb R,G,B` to flag a GIMP "key color" as slot 0 and write the same RGB back into slot 0 of the output NCLR so GIMP shows the key color on re-open.
- **NCER constrains tile count, not tile contents.** OAMs reference tile indices and dimensions (parsed by `rom_files/dump_ncer.py`); replacing tile bytes is safe as long as the number of tiles and the OAM-implied geometry are preserved. Shrinking tile count strands OAMs at higher indices on garbage; growing is harmless but wasted. For v0.1 the editor enforces "PNG must produce the same tile count as the original" — same-shape replacement only.
- **PAK-internal injection.** Same-compressed-size replacements need no PAK directory rewrite. Anything else requires patching the affected directory entry's size and adding the signed delta to every downstream entry's offset (`rom_files/shift_offsets.py` automates that). If the PAK itself grows beyond its FAT slot, `rom_files/inject_test.py` already handles ROM-level FAT + header shifting in 0x200-aligned increments.

### 11.2 Architecture

**`digimon_core/sprite.py`** — port of the pure functions from `rom_files/ncgr_to_png.py` (`decompress_rle30`, `compress_rle30`, `parse_nclr`, `parse_ncgr`, `unpack_pixels`, `encode_tiles`, `build_ncgr_from_template`, `build_nclr`, `png_to_indexed`). No PIL dependency in the parsing side; rendering returns a flat `bytes` of RGBA pixels and `(w, h)`, which the Qt layer wraps in a `QImage` (`QImage.Format_RGBA8888`). Re-exporting to PNG goes through `QImage.save()` so we avoid pulling Pillow into the editor's runtime.

**`digimon_core/pak.py`** — PAK directory reader/writer. One class:

```python
class PakFile:
    entries: List[bytes]    # decompressed payloads, in directory order

    @classmethod
    def from_bytes(cls, data: bytes) -> "PakFile": ...
    def to_bytes(self) -> bytes: ...   # re-compresses, rebuilds directory
```

Directory format is documented in `research_docs/spr_paks_research.txt`: `u32 count, then count × (u32 offset, u32 size_with_0x80_flag)`. `to_bytes` writes RLE-0x30 payloads concatenated, alignment-preserved, and patches the directory in lockstep.

**Session integration.** `RomSession` gains a `paks: Dict[str, PakFile]` field lazily populated on demand (only when the sprite editor is opened — full 1627-entry decompression of every PAK is too slow for cold-load). Saves re-serialize each touched PAK and splice it back via the existing FAT-resize path (port `inject_test.py`'s container-resize logic into a session method `replace_fat_entry(filename, new_bytes)`). Same-size PAK replacements are the fast path and need no FAT touch.

**Asset model.**

```python
@dataclass
class SpriteAsset:
    pak_name: str             # "SPR_CHR.PAK"
    ncgr_index: int           # entry index inside the PAK
    nclr_index: int           # paired NCLR — found by walking PAK for matching-stem entries
    ncer_index: Optional[int] # cell metadata if known (cross-PAK reference)
    width_tiles: int          # hint for the decoder; defaults from research notes per PAK kind

    def to_qimage(self, pak: PakFile) -> QImage: ...
    def replace_from_png(self, png_path: Path, pak: PakFile,
                         transparent_rgb: Optional[Tuple[int,int,int]]) -> None:
        # Reuses the original NCGR's RAHC header as the template, so RAHC+0x12
        # is preserved automatically.
```

The "find paired NCLR" heuristic deserves its own investigation: PAK entries don't carry filenames, only positions. First cut: most sprites use the same numeric index across PAKs (CHR[N] pairs with PAL[N]). Validate before committing UI to it.

### 11.3 UI

**New nav-tree group: "Graphics".** Sub-pages (one per PAK or per sprite category — TBD once we know what 1627 entries actually contain):

- **Portraits** — `SPR_CHR.PAK` filtered to portrait-shaped entries (32×40 dimensions, by far the easiest visual category to triage).
- **UI frames** — `SPR_CHR.PAK` filtered to small tile blocks.
- **Battle minis / animations / habitat previews** — same backing PAK, different filter heuristics.

Within each page:

- **Master list.** A `QListView` in icon mode (thumbnails), backed by a `QAbstractListModel` that lazy-decodes on `data(Qt.DecorationRole)` and caches the resulting `QImage`. Numeric index as the label until we have semantic names.
- **Detail panel.** Larger preview (zoomed), file-info readout (dimensions, palette size, RAHC+0x12 value), three buttons:
  - **Export PNG…** — writes the decoded PNG to a user-chosen path.
  - **Replace from PNG…** — opens a file picker, runs `replace_from_png`, refreshes the thumbnail, pushes a `ReplaceSpriteCommand` onto the undo stack so Ctrl+Z reverts the swap.
  - **Revert to vanilla** — re-decodes from `original_rom_data`.
- **Transparent-color picker** in the detail panel — colour swatch that defaults to whatever sits at slot 0 of the original NCLR; the user can repick before importing so GIMP-painted key colors get recognised.

Undo/redo: `ReplaceSpriteCommand` snapshots the old PAK payload bytes and the new payload bytes; redo/undo swap them on the PAK instance. Cheap (PAKs are small in memory once decompressed).

### 11.4 Phasing

| Phase | Output | Effort |
| --- | --- | --- |
| **A — Port + bench** | `digimon_core/sprite.py` (parsers, encoders, RAHC-template builder) and `digimon_core/pak.py` (read-only `from_bytes`). Round-trip unit test: every entry in `SPR_CHR.PAK` decode → render → compare against the reference PNG dump from `rom_files/ncgr_to_png.py batch`. | 1 d |
| **B — Read-only thumbnail browser** | Graphics nav group + master-list thumbnails for `SPR_CHR.PAK`. No editing yet. Lazy-decode + per-entry image cache; verify total cold-load < 2 s on Dusk. | 1 d |
| **C — Export PNG** | "Export PNG…" button + width-divisor enforcement so re-import is exact. | 0.5 d |
| **D — Replace from PNG (same-size)** | "Replace from PNG…" using `build_ncgr_from_template` for header preservation. Requires the new encoded NCGR to be the same compressed size as the original (validation at import time). Wire into `ReplaceSpriteCommand` + undo stack. Verify in DeSmuME against the Julia portrait test. | 1 d |
| **E — PAK directory rewrite** | Lift the "same compressed size" constraint by patching the affected entry's size and shifting every downstream entry's offset (port `rom_files/shift_offsets.py`'s logic). PAK total size still ≤ original FAT slot. | 0.5 d |
| **F — ROM-level FAT shifting** | Lift the "PAK fits in FAT slot" constraint by routing through the shared `digimon_core/fat.py` helpers (§12.2). This is the last constraint — after this, sprites can grow arbitrarily. MSG.PAK saves share the same code path. | 1 d |
| **G — Categorisation pass** | Replace numeric index labels with human-readable categories (portraits / UI / battle minis / animations / habitat previews). Heuristic-driven: dimensions, palette size, OAM count, and any naming pattern that surfaces in the research repo. | 0.5 d |

Phases A–D are independently shippable as a v0.1 sprite editor (same-size replacement only). E–F unlock arbitrary edits. G is polish.

### 11.5 Risks

- **Tile-count drift.** A PNG of different dimensions than the original silently produces a different tile count, which breaks NCER OAMs that reference high-index tiles. Mitigation: import-time validation that rejects PNGs whose `(tw × th)` doesn't match the original; surface as a clear error, not a silent breakage. Long term, add a "view OAMs" overlay on the preview so the user can see which tile ranges are load-bearing.
- **PAK-pair heuristic.** "CHR[N] pairs with PAL[N]" is unverified across the full 1627 entries. If it doesn't hold universally, the asset list builder needs a fallback (probably index ranges learned from the research repo). Mitigation: Phase A's round-trip test surfaces any miss because the rendered PNG will be wrong-colored.
- **Cold-load cost.** Decompressing 1627 entries on PAK open is ~tens of MB of RLE-30 work. If naive Python is too slow, the decompressor moves to a C extension or we keep PAK entries compressed in memory and decompress on demand. Bench in Phase B before optimising.
- **Save-side compression speed.** Saving a PAK after edits re-encodes touched entries. The greedy RLE-30 encoder is fast on typical sprite payloads (~few hundred bytes each); a full PAK save touches all entries only when the directory shifts. Keep an LRU of `(payload bytes → compressed bytes)` keyed by hash to avoid re-compressing unchanged entries.
- **Undo blast radius.** Replacing a sprite changes a PAK entry, which changes the PAK's compressed size, which (post-Phase E) shifts every downstream PAK entry's directory offset. The `ReplaceSpriteCommand` should hold the *PAK payload bytes* not just the entry bytes, so undo restores the directory state exactly.
- **No NCER-aware editing yet.** Users can't see OAM boxes on the preview — they have to trust dimensions match. If this proves error-prone in practice, add an "overlay OAMs" toggle on the preview using `rom_files/dump_ncer.py`'s parser ported to `digimon_core/sprite.py`.

### 11.6 Acceptance criteria

- Opening the Graphics → Portraits page on Dusk shows a thumbnail grid of every portrait in `SPR_CHR.PAK` within 2 s of nav click.
- Selecting Julia's portrait (index 1566) and clicking "Export PNG…" produces a PNG identical to `rom_files/ncgr_to_png.py one` on the same NCGR.
- Replacing that PNG with an unedited copy of itself, saving the ROM, and loading in DeSmuME shows the portrait byte-for-byte unchanged (no row shift — i.e. RAHC+0x12 preserved).
- Replacing with an edited PNG (Julia hair recoloured) shows the new colors in-game.
- Ctrl+Z after a replacement restores the previous payload; saving after undo produces a ROM byte-identical to a save with no replacement at all.
- Save → reopen → the replacement persists (PAK directory was rewritten correctly).

---

## 12. Plan: ROM-level capacity — compaction, FAT shifts, sub-range injection

DWDD's ROM has roughly 6.5 MB of `0xFF` tail padding between the last used byte (~0x039650C0) and the cart-declared end at 0x04000000. The editor's previous save path stayed strictly inside original byte budgets to avoid touching that boundary. Two empirical findings from the `rom_files/` test scripts change the constraint surface and unlock both the MSG.PAK budget removal (§4.17) and the sprite-grow workflow (§11 phases E–F). This section consolidates the findings and lays out the shared infrastructure both editors will use.

> **Hard prerequisite: §13.** §12's save-path mechanics produce ROMs that boot fine in-emulator, but the editor itself cannot **re-open** a grown ROM until §13 lands — every hardcoded ROM-offset loader after the grown file would mis-target. Ship §13 first or in tandem; §12 standalone is a one-way export at best.

### 12.1 Findings

1. **Compaction is safe on DWDD.** `rom_files/compact_test.py` splices a 1 MB gap at the BTCHR.PAK boundary (0x002C8C00), shifts every downstream FAT entry by +0x100000, bumps the seven header offset fields that store absolute ROM positions, and rewrites header 0x80 (used ROM size). Booted in DeSmuME, played through title screen → save load → battles → cutscenes → menus → fresh save → reload — no regressions. This means **no DWDD code path hardcodes ROM offsets past the FAT**; the FAT and the seven header fields below are the only consumers of absolute file positions.
2. **Sub-range FAT-internal injection works.** `rom_files/inject_test.py` resizes any byte range that lies inside a single FAT entry — *not* just whole files. It finds the containing FAT entry (`s <= start AND end <= e`), splices `new_content + preserved_tail_of_container + 0xFF pad` over the target range, shifts downstream FAT entries by the smallest 0x200-aligned delta, and updates the seven header offsets that point past the container. Used end-to-end to grow Julia's portrait NCGR inside `SPR_CHR.PAK` from 0x224 → 0x258 bytes (+0x34) with the operator manually patching the pak's internal directory for the shifted sub-entries.
3. **Original ROM size preservation is preferred.** DeSmuME's "device size increased / header invalid" warning fires whenever non-`0xFF` content extends past the cart-declared capacity at 0x04000000. The warning vanishes if the trailing FF run between header 0x80's used-size value and EOF is trimmed back. Editor saves should therefore keep ROM length ≤ vanilla whenever an edit fits, and only grow when it actually doesn't.
4. **Header fields the editor must keep in sync.** Absolute ROM offsets live at: `0x20` ARM9 ROM offset, `0x30` ARM7 ROM offset, `0x40` FNT offset, `0x48` FAT offset (+ `0x4C` FAT size), `0x50` ARM9 overlay table offset, `0x58` ARM7 overlay table offset, `0x68` icon/banner offset, `0x80` total used ROM size. Header `0x14` (device capacity) and `0x15E` (CRC16) do **not** need updates — DeSmuME ignores both for ROMs under the declared capacity. Real hardware tolerance for 0x14/0x15E is unverified; flag if/when the editor would grow the cart past its declared 256 MB capacity.
5. **Pak-internal directories are the caller's job.** `inject_test.py` operates on raw ROM byte ranges and is deliberately format-agnostic. For pak-internal edits (NCGR inside SPR_CHR.PAK, single string inside MSG.PAK, etc.), the editor must update the pak's own offset/size table to match the new sub-entry layout *before* invoking the FAT-resize path. For DWDD pak format = `u32 count + count × (u32 offset, u32 size_with_0x80_flag)`.

### 12.2 Architecture

Two helper modules ported from the `rom_files/` test scripts, both operating on `RomSession.rom_data` in place:

**`digimon_core/fat.py`** — pure FAT/header arithmetic, no container knowledge:
- `find_container(rom, start, end) -> (idx, container_start, container_end)`
- `resize_fat_entry(rom, container_idx, delta_bytes)` — patches the entry's end, computes the smallest 0x200-aligned downstream shift, shifts every FAT entry whose start ≥ container_end, shifts any of the seven header offset fields pointing past container_end, rewrites header 0x80.
- `splice_range(rom, target_start, target_end, container_end, new_content)` — bytes-level splice: `new_content + preserved_tail + 0xFF padding` so downstream stays 0x200-aligned.
- `signed_align(delta, alignment)` — helper that rounds up for growth and rounds magnitude down for shrink (symmetric).

**`digimon_core/pak.py`** (extending the read-only `PakFile` from §11.2) — pak-aware injection:
- `PakFile.replace_entry(idx, new_bytes)` — updates the pak's internal directory and any compression state; returns the new pak bytes ready for FAT-level splicing.
- `RomSession.replace_pak_entry(pak_filename, entry_idx, new_bytes)` — locates the pak's FAT entry, calls `PakFile.replace_entry`, splices the new pak bytes over the old, then calls `fat.resize_fat_entry` if the pak grew or shrank.

Both the sprite editor (§11) and the MSG.PAK string editor (§4.17) consume `RomSession.replace_pak_entry` — they don't see the FAT helpers directly.

### 12.3 Save-path integration

The serialize path becomes:
1. Apply all model `writeToRom` calls + QoL patches as today (no size changes).
2. For each pak the session has marked dirty (MSG.PAK string edits, sprite swaps), call `RomSession.replace_pak_entry(...)`. Pak replacements are applied **in ascending ROM-offset order** so each subsequent shift sees the post-previous-shift offsets, not stale ones (see §12.5).
3. Trim any trailing `0xFF` run between `header[0x80]` and `len(rom)` to suppress the DeSmuME capacity warning. Cheap; pure tail scan.
4. Write to disk + rolling `.bak`.

Editing the same pak entry multiple times in one session collapses to a single re-serialization at save time; the session keeps the in-memory `PakFile` authoritative and only re-walks ROM bytes on save.

### 12.4 Phasing

| Phase | Output | Effort |
| --- | --- | --- |
| **A — Port FAT helpers** ✅ **Done** | `digimon_core/fat.py` ships `find_container`, `signed_align`, `splice_range`, `resize_fat_entry`. `digimon_core/tests/test_fat.py` covers `signed_align` edge cases, synthetic splice growth/shrink/same-size, vanilla-ROM `find_container` (Dusk + Dawn) including cross-file-range rejection, the `header[0x80] == max(FAT.end)` invariant, and byte-exact equivalence with an in-test port of `inject_test.py`'s arithmetic across same-size / growth / shrink / no-op injections. Notable design choice: `resize_fat_entry` recomputes `header[0x80]` as `max(FAT.end)` rather than `len(rom)` (which is what `inject_test.py` did); the latter would inflate by the trailing cart-FF padding and break §12.3's trim boundary. | 0.5 d |
| **B — `replace_pak_entry` + MSG.PAK** ✅ **Done** | `digimon_core/pak.py` ships `PakFile` (parse / `replace_entry` / `to_bytes` with 4-byte inter-entry alignment preserved) plus MSG.PAK sub-format helpers `parse_msgpak_entry_groups` + `rebuild_msgpak_entry`. `editor/session.py` adds `_apply_msgpak_grow`: it groups over-budget MSG.PAK strings by `(entry_idx, group_idx)`, rebuilds only the affected groups (re-encoding every string in each via the new `GameString.encoded_bytes_for_grow()`, which keeps the original FF FF / FE FF terminator so group structure is preserved), then calls `fat.find_container` + `splice_range` + `resize_fat_entry` to ripple the size delta through the FAT and header. `serialize_all` skips over-budget MSG.PAK strings (their in-place writes would corrupt neighbours); `over_budget_strings()` filters MSG.PAK so the Save gate no longer blocks. `digimon_core/tests/test_pak.py` (9 tests) covers parse / round-trip / replace / grow shift / `entry_index_at` / group parse + rebuild. `digimon_core/tests/test_msgpak_grow.py` (5 × Dusk + Dawn = 10 tests) covers vanilla-save FAT preservation, vanilla-save trim invariant, single-string grow with downstream FAT shift + content equality, multi-entry grow round-trip via text-set comparison, and gate exclusion. | 0.5 d |
| **C — Trailing FF trim** ✅ **Done** | `RomSession._trim_trailing_padding` runs at the end of `serialize_all_with_qol`: reads `header[0x80]`, deletes bytes past it, and raises `RuntimeError` if any non-`0xFF` byte sits in the trimmed range (the §12.5 safety check — ensures the trim never silently swallows real data). Verified by `test_msgpak_grow.test_vanilla_save_trims_to_max_fat_end`: `len(out) == header[0x80] == max(FAT.end)` on a no-edit save of both Dusk and Dawn. | 0.25 d |
| **D — Sprite save path** | Hook §11 phases E–F into the same `RomSession.replace_pak_entry` helper so the sprite editor doesn't need its own ROM-level FAT-shifting code. | shared with §11 |
| **E — Dawn US verification** ✅ **Done** | `test_msgpak_grow.DawnUsMsgpakGrow` runs the full Phase B/C suite against Dawn US (vanilla trim, FAT preservation on no-edit save, single-string grow with downstream FAT shift + content equality, multi-entry grow). User confirmed empirically that Dawn behaves identically to Dusk through the grow path; the §12 save path is unblocked for both regions. | 0.25 d |
| **F — `.romproj` string_edits channel** ✅ **Done** | `.romproj` bumped to `format_version=2` (v1 still loads, with empty string_edits). `RomSession.msgpak_string_edits()` snapshots over-budget MSG.PAK strings as `(region, vanilla_offset, text)` triples on Save Project; `apply_string_edits()` replays them on Open Project after the byte diff + reparse. Rationale: `serialize_all` skips over-budget MSG.PAK strings (an in-place write would corrupt neighbours), so they don't appear in the equal-length byte diff — without this channel, Save Project silently dropped them. Compaction is still ROM-save-only; the project file stays offset-agnostic. `digimon_core/tests/test_project_msgpak_grow.py` (5 × Dusk + Dawn = 10 tests) covers v1 backward-compat load, over-budget round-trip (save project → reload → serialize_all_with_qol byte-equal to original session), in-budget edits riding the byte diff with empty string_edits, and `apply_string_edits` rejecting unknown region / offset. | 0.25 d |

### 12.5 Risks

- **Compaction was tested on Dusk only.** Dawn US almost certainly behaves the same (sister engine, identical loader patterns) but should be re-verified before the editor ships a Dawn-aware grow path — Phase E.
- **Multi-pak save ordering.** If both MSG.PAK and a sprite PAK grow in one save, the shifts compose. The `replace_pak_entry` calls must run in ascending ROM-offset order so each operates on a coherent FAT snapshot. A unit test in Phase B should exercise the "edit two paks in one save" path explicitly.
- **Trailing FF trim could mask a real write bug.** If a code path accidentally extends the ROM with non-`0xFF` data past `header[0x80]`, the trim won't touch it and the user gets a silently oversized ROM. Phase C adds an assert that the trim's tail run actually covers the gap; abort the trim (and the save, loudly) otherwise.
- **Header 0x14 / CRC16 untouched.** DeSmuME doesn't care. Real-hardware behaviour is unverified — flag in the save log if anyone reports a flashcart refusing to boot an editor-saved ROM.
- **Pak format variance across games.** DWDD's pak format is straightforward and the helpers assume it. Lost Evolution uses a different 16-byte header + 16-byte entry layout with a custom LZSS variant (reverse-engineered in [`StoryLostEvolutionExtracted/root/spr_pak_extract.py`](../StoryLostEvolutionExtracted/root/spr_pak_extract.py)); not in scope for this editor, but mentioned so the `PakFile` abstraction stays format-pluggable if a future LE editor wants to share `digimon_core/fat.py`.

### 12.6 Acceptance criteria

- Editing a MSG.PAK string to exceed its original slot size and saving produces a ROM that boots in DeSmuME, displays the longer string correctly in-game, and survives a save / reload cycle.
- A save with no size-changing edits produces a ROM byte-identical to the vanilla input (modulo intended model/QoL writes) — no spurious FAT shifts, no header bumps.
- A save that grows the ROM keeps the trailing-FF gap intact so DeSmuME doesn't surface the "device size increased" warning, as long as the new total fits within the cart-declared capacity.
- Two pak edits applied to the same save (MSG.PAK + a sprite, say) produce a ROM where both edits are visible in-game and downstream files (everything after the second pak) still load correctly.

---

## 13. Plan: FAT/FNT-driven file resolution

Today every loader in `digimon_core/loaders.py` (ported from `DWDDRandomizer/src/utils.py`) reads from **hardcoded ROM offsets** keyed off version constants in `constants.py`. That works only as long as the ROM layout matches vanilla byte-for-byte — true today because no save path changes file sizes. The moment §12 actually grows MSG.PAK or a sprite PAK, every downstream FAT entry shifts and the hardcoded offsets point at wrong bytes. **§13 is a hard prerequisite for §12 being useful**: capacity work without indirect file resolution produces ROMs that boot once but can't be re-opened in the editor (loaders parse vanilla offsets against a shifted ROM, get garbage, no further edits possible).

### 13.1 What needs to move from hardcoded offsets to FAT lookup

FAT-listed files the editor currently reaches via hardcoded ROM offsets:

DWDD nests every FAT-listed asset under a single `DAT/` root subdirectory; bare paths like `MSG.PAK` don't exist at the FNT root. Path references below use the literal FNT path (`DAT/...`); the §13 narrative occasionally still uses the bare shorthand when the location is obvious.

| Content | File path | Internal layout |
| --- | --- | --- |
| In-game strings | `DAT/MSG.PAK` | DWDD-format directory (already pak-driven via §4.17; ROM-offset side still hardcoded). |
| Digimon base data | `DAT/dm/N` (N=0..97) | Page-offset header (§13.3). |
| Enemy data | `DAT/en/N` (N=0..97) | Page-offset header (§13.3). |
| Wild encounter areas | `DAT/ec/E0XX.BIN` (80 files) | Per-area record; sub-format §13.3.a. |
| Encounter reward tables | `DAT/ec/I0XX.BIN` (5 files) | Raw `0x20`-byte record array, no header (§13.3.b). |
| Encounter cross-table | `DAT/ec/ENCTBL.BIN` | **Unmapped** — purpose / format TBD (§13.3.c). |
| Equipment | `DAT/eq/N` (N=0..18) | Page-offset header (§13.3). |
| Standard digivolutions | `DAT/sk/N` (N=0..62) | Page-offset header (§13.3). |
| Sprite tile data, palettes, cells, animations | `DAT/SPR_CHR.PAK`, `DAT/SPR_PAL.PAK`, `DAT/SPR_CEL.PAK`, `DAT/SPR_ANM.PAK`, … | DWDD-format directory (§11.1). |

Out of scope for §13 — these stay on hardcoded offsets because the data lives inside arm9.bin / overlays, not in FAT-listed files: move data, trait data, DNA / armor digivolution recipes, quest data, habitat / worldmap, starter packs, QoL byte-patch targets, ARM9 / Overlay strings, header offset fields. Anything in arm9.bin is pointer-locked and stays at fixed offsets; growing arm9.bin would require relocation work not in scope here.

### 13.2 Architecture

**`digimon_core/fnt.py`** — pure FNT/FAT path resolver. Built once on session load, queried by loaders thereafter:

```python
class FileTable:
    @classmethod
    def from_rom(cls, rom: bytes) -> "FileTable":
        # Parse FNT (header 0x40) into path → file_id map.
        # Parse FAT (header 0x48) into file_id → (start, end) map.
        # Compose into path → (start, end).
        ...

    def resolve(self, path: str) -> Tuple[int, int]: ...
    def slice(self, rom: bytes, path: str) -> bytes: ...
```

**Loader refactor.** Each affected loader stops taking a raw ROM offset and starts taking a `(FileTable, internal_offset)` pair. The version-keyed constants in `constants.py` split into two buckets:
- ROM-absolute offsets to FAT-listed content → become `path: str` + `within_file_offset: int`. The within-file offsets are engine-internal and don't depend on cart layout, so the per-version dimension typically collapses to one set per file (verify per loader).
- ROM-absolute offsets to ARM9 / Overlay content → stay single integer constants, still version-keyed.

**Session integration.** `RomSession.__init__` parses the `FileTable` once. Loaders accept the resolver via the session. Save-side writers do the same in reverse: `writeToRom` takes the file path + within-file offset, splices into the FAT-listed file, and (if the write changes the file's size) routes through `RomSession.replace_pak_entry` from §12.2.

### 13.3 Per-file pagination

The FAT-listed data directories use **three distinct** internal formats. `DAT/ec/` is the only directory that mixes shapes; the rest are uniform.

#### 13.3 — Page-offset header (used by `DAT/dm/`, `DAT/en/`, `DAT/eq/`, `DAT/sk/`)

- File starts with an array of `u32` little-endian offsets (file-relative). Each entry points at the start of one page (one model record).
- The header is **self-describing**: its byte length is the value of the first offset, so the page count is `first_offset / 4` and the array runs `first_offset / 4` entries deep before the first record begins.
- Pages are stored back-to-back; page `i` spans `offsets[i]` → `offsets[i + 1]` (or end-of-file for the last page). In vanilla ROMs the pages are uniformly sized and match the existing model struct width (e.g. 0x44 bytes per `BaseDataDigimon` in `DAT/dm/N`; 0x6C per `EnemyDataDigimon` in `DAT/en/N`; 0x48 per `Equipment` in `DAT/eq/N`; 0x70 per `StandardDigivolution` in `DAT/sk/N`).
- One file holds 8 records (verified across `DAT/dm/`, `DAT/en/`, `DAT/eq/`, `DAT/sk/` on Dusk), so logical record id `R` lives in file `R / 8`, page `R % 8`.
- Each page maps to one record of the file's existing model class (`DAT/dm/` → `BaseDataDigimon`, `DAT/en/` → `EnemyDataDigimon`, `DAT/eq/` → `Equipment`, `DAT/sk/` → `StandardDigivolution`). Parsers are already implemented; what's new is locating each record via the page header instead of `base + N * sizeof(entry)`.

Example header from `DAT/dm/0` (hex):

```
20 00 00 00  64 00 00 00  A8 00 00 00  EC 00 00 00
30 01 00 00  74 01 00 00  B8 01 00 00  FC 01 00 00
```

First offset `0x20` ⇒ 8-entry header (`0x20 / 4 = 8`) ⇒ pages at `0x20, 0x64, 0xA8, 0xEC, 0x130, 0x174, 0x1B8, 0x1FC`. Page 0 then begins with the BaseDigimon record bytes (`00 00 01 02 4B 00 00 00 ...`).

#### 13.3.a — Per-area record (`DAT/ec/E0XX.BIN`)

80 files; one per wild encounter area. Sizes vary (0x44 → 0x14C bytes on Dusk). Layout matches the existing `WildEncounterArea` model except the file is trimmed to its actual content instead of padded to 0x200:

- **Header** (0x10 bytes): `u16 num_encounters`, `u16 rate_lower`, `u16 rate_upper`, then 10 bytes of zero filler (`model.WildEncounterArea.HEADER_SIZE`).
- **Records**: `num_encounters` × 0x18-byte `WildEncounter` entries (`digimon_id`, `reward_slot`, plus interior bytes documented in `model.WildEncounter`).
- **Terminator**: 4 trailing bytes of zeros (a partial `WildEncounter` slot whose `digimon_id` field reads 0; matches the model's `if dig_id == 0: break` exit).

Identity holds across all 80 files on both regions: `file_size == 0x10 + num_encounters * 0x18 + 4`. The pre-FNT loader walked a contiguous 0x200-per-area blob (with `FF FF` padding past the terminator) — under FNT-driven loading each area is its own self-contained file and the padding is dropped.

#### 13.3.b — Encounter reward array (`DAT/ec/I0XX.BIN`)

5 files; sizes 0x40 → 0x280. No header — each file is a flat array of `0x20`-byte `EncounterReward` records. Page parser doesn't apply; loaders read `file_size / 0x20` records directly.

#### 13.3.c — `DAT/ec/ENCTBL.BIN`

Single 0x84C-byte file. Format and purpose currently unknown; likely a cross-reference table between areas (`E0XX.BIN`) and reward sets (`I0XX.BIN`), but unverified. Out of scope for Phase B unless a downstream loader actually depends on it.

**Save side.** For page-headered files, editing a record in place doesn't touch the header. Growing a record means the page-header offsets shift downstream — straightforward `u32` array rewrite, no inner pak directory like §12's MSG.PAK case. For `DAT/ec/` sub-formats, save-side behavior is per-file: reward arrays just rewrite contiguous records; per-area records depend on the layout reversed in §13.3.a. Vanilla writers don't need to handle grow for §13 itself (no editor surface grows these records today); flag for §12-style routing only when a future feature does.

Reference starting point for cross-checking: [`research_docs/data locations_dusk.txt`](../research_docs/data%20locations_dusk.txt).

### 13.4 Phasing

| Phase | Output | Effort |
| --- | --- | --- |
| **A — FileTable + tests** | `digimon_core/fnt.py` with FNT/FAT parsing + path resolver. Round-trip test: `FileTable.from_rom(vanilla).resolve("DAT/MSG.PAK")` matches the hardcoded constant on Dusk and Dawn. **Done.** | 0.5 d |
| **B — Internal format parsers** | (1) Page-offset header parser (`digimon_core/pagination.py`) covering `DAT/dm/`, `DAT/en/`, `DAT/eq/`, `DAT/sk/` with structural + legacy-equivalence tests on both regions. (2) `DAT/ec/I0XX.BIN` raw-array reader via `iter_fixed_records`. (3) Reverse `DAT/ec/E0XX.BIN` per-area record format (§13.3.a) and confirm the existing `WildEncounterArea` model parses it correctly. `DAT/ec/ENCTBL.BIN` (§13.3.c) deferred. **Done.** | 0.5 d |
| **C — Loader refactor (read path)** | Switch every loader for FAT-listed content to FileTable + page-parser resolution. Verify byte-identical parsed model state vs. the hardcoded-offset loaders on vanilla Dusk + Dawn. **Done.** | 1 d |
| **D — Writer refactor (write path)** | Same migration for every `writeToRom`. `smoke_test.py` round-trip must keep passing. **Done.** Writers already split bytes at `obj.offset`, so the read-path refactor (which sets offsets via FNT) inherently fixed the write side. The /sk/ digivolution map needed a hardcoded `(file_idx, first_id, count)` table because vanilla `DIGIVOLUTION_ADDRESSES` aren't 0x400-aligned on Dawn and DAT/SK/0..7 + SK/20 hold unrelated data — see `_SK_DIGIVOLUTION_LAYOUT` in `loaders.py`. | 0.5 d |
| **E — Sprite PAK indirection** | §11's sprite editor stops hardcoding `DAT/SPR_*.PAK` ROM offsets; routes through `FileTable`. | folded into §11 |
| **F — Unblocks §12** | With Phase D landed, §12's grow-aware save path can ship without breaking re-open of grown ROMs. | gating only |

Phases A–B are independently shippable as plumbing; C–D land the user-visible change (no behaviour difference on vanilla ROMs, but layout-tolerant on grown ROMs). F is a gating milestone, not work.

### 13.5 Risks

- **Page-offset header consistency.** `DAT/dm`, `DAT/en`, `DAT/eq`, `DAT/sk` all share §13.3's page-offset header on Dusk spot-checks (8 records × file, uniform page sizes matching the model struct width). Verify per-file across both regions in Phase B before merging C/D; if any file diverges, branch on file id.
- **`DAT/ec/` mixed formats.** Three internal shapes coexist (`E0XX.BIN`, `I0XX.BIN`, `ENCTBL.BIN`). Phase B (3) — reversing the `E0XX.BIN` per-area record — is the highest-risk piece of §13 because old loaders hid the structure inside 0x200-padded slabs. Surface it first so the team has a calibration point; `ENCTBL.BIN` stays deferred until a loader actually needs it.
- **Per-version constant migration is mechanical but wide.** Splitting `constants.py` touches every loader site. Script the grep-and-replace (find each `ROM_OFFSET_*` constant, classify FAT-listed vs ARM9-resident, rewrite call sites) rather than hand-editing to keep regressions out.
- **Round-trip equivalence on vanilla.** Phases C/D must not change model bytes against a vanilla ROM. The existing `smoke_test.py` round-trip is the right safety net; extend it to cover every loader before merging C.
- **Out-of-tree ROMs (modded vanilla).** A ROM already edited by an older editor build with shifted FAT entries will Just Work under FileTable-driven loading, but model contents may have moved within their files in ways the per-file pagination parser doesn't yet expect. Flag as a follow-up only if it becomes a real-world failure mode.

### 13.6 Acceptance criteria

- A vanilla Dusk ROM loaded via `FileTable` produces the same parsed model state byte-for-byte as the hardcoded-offset loader. Same for Dawn US.
- `smoke_test.py` round-trip still passes against vanilla on both regions after the migration.
- A ROM where MSG.PAK has been grown via the §12 save path can be re-opened in the editor with every other data table loading correctly: digimon base data, enemy data, wild encounters, equipment, standard digivolutions, and sprite PAKs all resolve via FAT.bin and parse without garbage.
- Combined acceptance with §12: a Save → exit → re-open → edit again → Save cycle with a grown MSG.PAK survives end-to-end with no data corruption.

### 13.7 Bonus: language-patch tolerance

A non-obvious side-effect of FAT-driven loading: any community mod that grows FAT-listed files without changing the version header becomes readable in the editor. The canonical case is fan translation patches — they almost always extend MSG.PAK to fit the longer target language, leaving the cart still identified as `DUSK_US` / `DAWN_US` by `checkHeader` but with every loader after the first shifted file mis-targeting today. §13 doesn't add an explicit translation feature; it removes the structural blocker, so loading a translated ROM, viewing its tables, and saving further edits Just Works.

Caveats:
- ARM9 / Overlay string edits the patch made are still subject to the original byte budgets (§4.17) — editor edits on top stay capped there.
- Patches that fundamentally rearrange overlay layout (uncommon for translations) remain out of scope; this is about layout-tolerant *loading*, not arbitrary ROM forensics.
- Header version detection still requires the patch to leave the title/gamecode bytes intact, which translations typically do.
