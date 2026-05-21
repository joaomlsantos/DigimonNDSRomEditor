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
16. **QoL toggles** — not yet implemented. Will surface DWDDRandomizer's QoL byte-patches as checkboxes (text speed, movement speed, scan rate, farm exp, battle perf, version-exclusive areas, …) and write at save time.

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
   Walks every loaded model, calls `writeToRom(rom_data)` on a fresh copy of the original ROM bytes (`RomSession.serialize_all()`), then writes the `.nds`. **Rolling `.bak`** alongside the target: `shutil.copy2(target, target + ".bak")` before each overwrite so a botched write (or a regretted save) doesn't destroy the previous on-disk copy. QoL byte-patches not yet wired (will piggy-back on this path once the QoL tab lands).
2. **Save Project** (`.dnedit`) — **not yet implemented**. Design under discussion: TOML/JSON capturing `(format_version, editor_version, rom_version, qol_toggles, diffs)`. Diff layer choice still open — byte-range diffs (`(offset, length, bytes)` tuples) are leading because they're robust to model changes; field-level diffs would be more readable but need a stable per-field path scheme. QoL section forward-compatible empty until §4.16 lands.
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

### M6 — QoL tab + Project files + Polish — partial
- **Not yet:** Surface QoL toggles, wire to `DigimonROM.executeQolChanges`.
- **Not yet:** Project-file save/load with diff format (`.dnedit`).
- ~~Cross-reference jump-to-related-entry~~ — done (nav handlers + footer routing + tree-viewer link).
- Per-editor list filters in place; **cross-page search** still pending.
- Shipped beyond plan in this milestone: validation footer, recent-files menu, window-state persistence, rolling `.bak` on save, per-save changelog, close-confirm on unsaved changes.

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

Ordered by load-bearing-ness:

1. **Project files (`.dnedit`)** — pending design decision (byte-range vs field-level diffs); see §5.2. Independent of QoL, so a forward-compatible empty `qol_toggles` slot is acceptable in v1.
2. **QoL toggles tab** — port DWDDRandomizer's byte-patches as checkboxes; write at save time (piggy-back on the existing Save path).
3. **Cross-page search** — single search box that queries every editor's record list and routes click-throughs via the existing nav handlers / footer routing.
4. **Packaging (M7)** — PyInstaller spec mirroring DWDDRandomizer's.
5. **IPS/xdelta export** — v0.2+; useful once project-file workflow is established.

Beyond plan but possibly worth picking up:
- Before/after value capture in changelog entries (currently action-text only).
- "Revert this entry to vanilla" per editor (needs the `session.original` reference path; partly implementable today via re-parsing a slice of `original_rom_data`).
- About dialog with editor version + ROM version readback.
