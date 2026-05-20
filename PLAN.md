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

Several model classes parse bytes but never serialize back. The randomizer gets away with this because it either rewrites individual fields in-place (e.g. `randomizeDnaDigivolutions` only touches a few offsets) or uses `getByteArray()` on `BaseDataDigimon` (the one model that has it). For a true editor we need symmetric round-tripping on **every** model.

| Class | Has parser | Has serializer |
| --- | --- | --- |
| `BaseDataDigimon` | yes | yes (`getByteArray`) |
| `EnemyDataDigimon` | yes | **missing** |
| `MoveData` | yes | **missing** |
| `QuestData` | yes | **missing** |
| `FarmTerrain` | yes | **missing** |
| `StandardDigivolution` | yes | **missing** (randomizer writes fields directly) |
| `ArmorDigivolution` | yes | **missing** |
| `DNADigivolution` | yes | partial (`writeDnaDigivolutionToRom`) |
| `EncounterRewardTable` | yes | yes (`getByteRepresentation`) |
| `HabitatWorldmap` | yes | **missing** |
| `SpriteMapEntry` / `BattleStringEntry` | yes | **missing** (rarely edited; lower priority) |

Adding `getByteArray()` (or `writeToRom(rom_data)`) to each is mechanical — every field already knows its offset and size.

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

## 3. Core additions before any UI work

These are prerequisites — UI is dead weight without them.

1. **Symmetric serializers.** For every model in the table above, add `getByteArray() -> bytearray` and `writeToRom(rom_data: bytearray)`. Verify with a round-trip test on a clean ROM: parse-all → serialize-all → `assert bytes_match`.
2. **A `RomSession` object** that owns the loaded `bytearray`, the parsed model graph, the source path, and a dirty flag. All editor mutations go through it so we can drive undo/redo and "Save"/"Save As".
3. **A `Command` abstraction** for every edit (set HP to N, swap move slot, add evolution, etc). Each command implements `do(session)` / `undo(session)`. Plug into Qt's `QUndoStack`.
4. **Validators** centralized in `digimon_core/validation.py`:
   - Digimon IDs must exist in `DIGIMON_ID_TO_STR`.
   - Move/trait/item IDs must be in range.
   - Digivolution chain stage transitions are valid (e.g. Rookie → Champion).
   - Stat values clamped to ROM-storage size (e.g. 16-bit unsigned).
   - Resistance values within game bounds.
5. **Cross-reference index.** Build once after load: "which encounter tables reference digimon X", "which digivolutions target digimon X", "which quests grant item Y". Lets the UI jump between related entries.

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

Mapped directly onto the model classes:

1. **Digimon → Base Data** — every field of `BaseDataDigimon`: stats, species, type, traits, moves, resistances, signature move, dex habitat, scannable flag, exp curve.
2. **Digimon → Enemy Data** — `EnemyDataDigimon`: as above but for enemy variants, plus per-species EXP yields.
3. **Digivolutions → Standard** — tree view per digimon: degeneration target + up to 3 evolutions, each with up to 3 conditions. Drag-drop or "Add evolution" buttons.
4. **Digivolutions → Armor** — list of armor digivolutions; per-entry: source digimon, trigger item, target digimon, conditions.
5. **Digivolutions → DNA** — list of DNA recipes: two source digimon + result + conditions.
6. **Moves** — `MoveData` table; per-entry editor (MP cost, element, effects, hits, range, level learned).
7. **Traits** — name-only lookup today (no in-ROM structure to edit besides assignment); the UI uses this for trait picker dropdowns on the Digimon page. No standalone editor needed until trait effects research lands.
8. **Items** — `ITEM_ID_TO_STR` is the catalog; per-item attributes are not yet modeled. **Out of scope for v0.1** beyond picker dropdowns elsewhere. Promote to a real page once item-data structures are documented.
9. **Encounters → Wild** — area list (using `LOCATION_OFFSETS_TO_NAMES`) → per-area encounter table → per-slot digimon picker + level. Patterned after the existing C# `DigimonWorldDuskEditor`.
10. **Encounters → Rewards** — `EncounterRewardTable` per area: probability × reward (item or money).
11. **Battles → Fixed (tamers/bosses)** — same shape as wild encounters but for scripted battles.
12. **Quests** — `QuestData` editor: rewards (money/item/tamer points), unlock conditions, flags.
13. **Starters** — the four starter packs; pick 3 digimon per pack + starting level.
14. **Farm** — `FarmTerrain` editor: digimon limit, per-species EXP yields.
15. **Habitats / Worldmap** — `HabitatWorldmap` entries: which species the worldmap shows as living in each location, location flags.
16. **QoL toggles** — a single tab that surfaces DWDDRandomizer's QoL byte-patches as checkboxes (text speed, movement speed, scan rate, farm exp, battle perf, version-exclusive areas, …). These are not "edits to data tables", so they live in their own page and write at save time, not on field change.

### UI patterns shared across pages

- Master list = `QTableView` with sort/filter/search.
- Detail editor = form layout; ID-typed fields use combos backed by `DIGIMON_ID_TO_STR` / `MOVE_ARRAY_STR` / `ITEM_ID_TO_STR`.
- Every editable widget is wrapped so its `valueChanged` signal pushes a `Command` onto the undo stack; this is what gives us Ctrl+Z for free across the whole app.
- "Revert this entry to vanilla" button per page — needs a copy of the originally-loaded model graph kept around (`session.original` vs `session.current`).
- Cross-reference links: clicking a digivolution target switches to the Base Data page focused on that digimon. Implemented as Qt signals on a central `Navigator` service.
- Tabs / sub-pages keep stat blocks, moves, traits, resistances visually grouped.

---

## 5. Persistence: ROM out + project file

Two distinct save paths:

1. **Save Patched ROM** (`File → Export ROM…`)
   Walks every loaded model, calls `writeToRom(rom_data)`, then runs any enabled QoL byte-patches (reusing `DigimonROM.executeQolChanges`), then writes the `.nds`.
2. **Save Project** (`File → Save Project…`)
   A `.dnedit` file (TOML or JSON) capturing **diffs from vanilla**, plus QoL toggle states, ROM header version, and editor version. Loading a project requires a vanilla ROM of the same version; the editor applies the diffs into the freshly-loaded model graph. This:
   - keeps project files tiny and shareable,
   - lets users rebase their hack onto an updated randomizer/editor release,
   - dodges any copyright issue around shipping ROM bytes.

Optional third path:
3. **Generate IPS/xdelta patch** for distribution. Patch tools exist as Python libs (`ndspy`, `python-ips`). Probably v0.2+.

---

## 6. Milestones

Each milestone is independently shippable.

### M0 — Scaffolding (1–2 days) — ✅ done
- ~~Create `digimon_core` package, move `src/model.py`, `src/constants.py`, `src/utils.py` into it.~~ Done — `digimon_core/` lives under `DigimonNDSRomEditor/`. (DWDDRandomizer was left on its own copy.)
- ~~Add serializers + round-trip test on vanilla Dusk + Dawn ROMs.~~ Done — see `digimon_core/tests/test_roundtrip.py` and `smoke_test.py`.
- ~~PySide6 project skeleton with `RomSession`, `Command`, `QUndoStack`, navigation tree, File menu.~~ Done.

### M1 — Digimon Base Data editor (1 week)
- Full editing of `BaseDataDigimon` for every digimon.
- Undo/redo working end-to-end on this page.
- Save Patched ROM that round-trips through `writeToRom`.
- This proves the architecture; everything after is repetition.

### M2 — Movesets, Traits, Resistances (3–5 days)
- Move editor (`MoveData`).
- Polish the trait/move/resistance pickers on Base Data.

### M3 — Digivolutions (1 week)
- Standard digivolution tree editor — by far the most complex page; visualize the per-digimon graph, edit conditions.
- Armor + DNA digivolution editors.

### M4 — Encounters (1 week)
- Wild encounter tables (by area).
- Encounter reward tables.
- Fixed battles (tamers/bosses).
- Starters page (small).

### M5 — Quests, Farm, Worldmap (1 week)
- Quest editor (rewards, unlock conditions).
- Farm terrain editor.
- Habitat / worldmap editor.

### M6 — QoL tab + Project files + Polish (1 week)
- Surface QoL toggles, wire to `DigimonROM.executeQolChanges`.
- Implement project-file save/load with diff format.
- Cross-reference jump-to-related-entry.
- Search across all pages.

### M7 — Distribution
- Packaging (PyInstaller, mirroring DWDDRandomizer's `.spec`).
- Optional: IPS/xdelta patch export.

---

## 7. Risks & open questions

- **Models without serializers are a hidden cost.** Adding them is mostly typing, but every model must be covered by a byte-exact round-trip test before any editor page on top of it is trusted. Vanilla-ROM round-trip tests run in CI catch this cheaply.
- **Some data tables have unknown fields** (`unknown_0xN` properties throughout `model.py`). Editor should expose them as raw hex in an "Advanced" expander rather than hide them — users hacking the ROM will want them.
- **Item structure isn't modeled** in DWDDRandomizer (only ID→name). Until research lands, item editing is read-only / picker-only.
- **Sprite/portrait/string editing is out of scope** for v0.1. The randomizer already loads `SpriteMapEntry` / `BattleStringEntry` but treats them as opaque. Promote to editor pages when there's documented research.
- **Region support** sticks to `DUSK_US`/`DAWN_US` since those are the only `IMPLEMENTED_HEADERS` in the randomizer. JP support can extend later by filling the offset tables.
- **Concurrent edits across pages** (e.g. renaming Agumon's species while another page references Agumon's species) must be handled by re-querying the model graph on focus, not by caching widgets' values.

---

## 8. Recommended starting point

1. **M0 first.** Extract `digimon_core`, write round-trip tests, add missing serializers. This pays for itself five times over before any UI code is written.
2. **Then M1** (Base Data) as a vertical slice to validate `RomSession` + `QUndoStack` + `writeToRom` end-to-end.
3. Decide after M1 whether the C#-based `DigimonWorldDuskEditor` should be deprecated and folded in, or kept as a niche tool.

The editor should reach feature parity with DWDDRandomizer's data coverage by end of M5; M6 makes it actually pleasant to use.
