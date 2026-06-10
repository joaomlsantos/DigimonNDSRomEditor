"""Headless smoke test for every editor in the app.

Loads a real ROM and walks each editor (digimon, moves, digivolutions,
quests, encounters, items, habitats, farm, strings, QoL, project files),
exercising the round-trip path for each:
  1. parse → serialize_all() equals vanilla
  2. mutate one field via the editor's bound widget
  3. assert serialize_all() differs at the expected offset
  4. undo → model + serialize_all() restored to vanilla

Beyond the per-editor round-trips, also checks: select_by_id navigation
for the click-to-jump pickers; validation-cap collectors mirror their
widget-level warnings; dirty-state markers; the over-budget save guard
for in-game text; and project-file save/reload preserves QoL + edits.

Run: python smoke_test.py [optional path to .nds]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Qt requires a QApplication before any QWidget is constructed; with no display
# available we use the offscreen platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import QUndoStack  # noqa: E402

from digimon_core import constants  # noqa: E402
from editor.session import RomSession  # noqa: E402
from editor.widgets.form_helpers import is_record_dirty, set_details_providers  # noqa: E402
from editor.widgets.validation import registry as validation_registry  # noqa: E402
from editor.widgets.base_digimon_editor import base_digimon_issues  # noqa: E402
from editor.widgets.move_editor import move_issues  # noqa: E402
from editor.widgets.standard_digivolution_editor import std_digivolution_issues  # noqa: E402
from editor.widgets.starters_editor import starter_issues  # noqa: E402
from editor.widgets.wild_encounters_editor import wild_encounter_issues  # noqa: E402
from editor.widgets.armor_digivolution_editor import ArmorDigivolutionEditor  # noqa: E402
from editor.widgets.base_digimon_editor import BaseDigimonEditor  # noqa: E402
from editor.widgets.dna_digivolution_editor import DNADigivolutionEditor  # noqa: E402
from editor.widgets.enemy_digimon_editor import EnemyDigimonEditor  # noqa: E402
from editor.widgets.move_editor import MoveEditor  # noqa: E402
from editor.widgets.quest_editor import QuestEditor  # noqa: E402
from editor.widgets.standard_digivolution_editor import (  # noqa: E402
    NO_EVO_SENTINEL,
    StandardDigivolutionEditor,
)
from editor.widgets.consumable_editor import ConsumableEditor  # noqa: E402
from editor.widgets.encounter_rewards_editor import EncounterRewardsEditor  # noqa: E402
from editor.widgets.equipment_editor import EquipmentEditor  # noqa: E402
from editor.widgets.farm_item_editor import FarmItemEditor  # noqa: E402
from editor.widgets.farm_terrains_editor import FarmTerrainsEditor  # noqa: E402
from editor.widgets.habitats_editor import HabitatsWorldmapEditor  # noqa: E402
from editor.widgets.qol_editor import QolEditor  # noqa: E402
from editor.widgets.starters_editor import StartersEditor  # noqa: E402
from editor.widgets.string_editor import StringEditor, string_issues  # noqa: E402
from editor.widgets.wild_encounters_editor import WildEncountersEditor  # noqa: E402


DEFAULT_ROM_PATH = r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    rom_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROM_PATH
    print(f"using ROM: {rom_path}")
    session = RomSession.from_file(rom_path)
    # Install session-data registry so dynamic-cap warnings (starter level
    # capped by aptitude, reward_slot capped by encounter_rewards length)
    # have the data they need.
    set_details_providers(session)
    print(f"loaded {session.version}: {len(session.base_digimon)} base, "
          f"{len(session.enemy_digimon)} enemy")

    assert bytes(session.serialize_all()) == session.original_rom_data, "initial round-trip failed"
    print("initial serialize_all() == vanilla")

    undo_stack = QUndoStack()

    # ---- base editor -----------------------------------------------------
    base_editor = BaseDigimonEditor(
        session.base_digimon,
        undo_stack,
        session.sprite_map,
        session.battle_strings,
        session,
    )
    first_id = next(iter(sorted(session.base_digimon.keys())))
    target = session.base_digimon[first_id]
    orig_hp = target.hp

    hp_spin = base_editor._stat_widgets["hp"]
    new_hp = orig_hp + 7
    hp_spin.setValue(new_hp)
    assert target.hp == new_hp, "spinbox did not propagate to model"
    assert undo_stack.count() == 1, f"expected 1 undo entry, got {undo_stack.count()}"
    print(f"base: hp {orig_hp} -> {new_hp}, undo_stack count = 1")

    # merging: a second adjacent edit collapses into the same undo entry
    hp_spin.setValue(new_hp + 1)
    assert undo_stack.count() == 1, "expected merge to keep stack size = 1"

    serialized = bytes(session.serialize_all())
    assert serialized != session.original_rom_data, "edit not reflected in serialize_all()"
    print("base: edited bytes differ from vanilla")

    undo_stack.undo()
    assert target.hp == orig_hp, "undo did not restore hp"
    assert bytes(session.serialize_all()) == session.original_rom_data, "undo did not restore bytes"
    print("base: undo restored vanilla bytes")

    undo_stack.redo()
    assert target.hp == new_hp + 1
    undo_stack.undo()  # leave clean for the next editor
    undo_stack.clear()

    # ---- enemy editor ----------------------------------------------------
    enemy_editor = EnemyDigimonEditor(
        session.enemy_digimon,
        undo_stack,
        session.sprite_map,
        session.battle_strings,
        session,
    )
    enemy_id = next(iter(sorted(session.enemy_digimon.keys())))
    enemy = session.enemy_digimon[enemy_id]
    orig_atk = enemy.attack
    atk_spin = enemy_editor._stat_widgets["attack"]
    atk_spin.setValue(orig_atk + 42)
    assert enemy.attack == orig_atk + 42
    undo_stack.undo()
    assert enemy.attack == orig_atk
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("enemy: undo restored vanilla bytes")

    # exp total label should reflect the parsed sum
    enemy_editor._refresh_exp_total()
    assert "Total exp" in enemy_editor._exp_total.text()
    print(f"enemy: {enemy_editor._exp_total.text()}")

    # ---- fixed-enemy reskin ---------------------------------------------
    # Find a fixed enemy (id > 0x1f4) whose current sprite reverse-looks-up
    # to a base digimon — we need an enabled combo to exercise the command.
    fixed_enemy_id = None
    for eid in sorted(session.enemy_digimon.keys()):
        if eid < 0x1f5:
            continue
        sm = session.sprite_map[eid]
        if sm.main_sprite in enemy_editor._reskin_row._sprite_to_base:
            fixed_enemy_id = eid
            break
    assert fixed_enemy_id is not None, "no reskinnable fixed enemy found"

    # select that enemy so the reskin row rebinds
    enemy_editor._on_selection(fixed_enemy_id)
    reskin = enemy_editor._reskin_row
    assert reskin._combo.isEnabled(), "reskin combo should be enabled for valid fixed enemy"
    # pickable list should cover the full sprite_map, not just 0..0x1f4
    assert reskin._combo.count() == len(session.sprite_map), \
        f"combo should include every sprite_map entry ({len(session.sprite_map)}), got {reskin._combo.count()}"

    sprite_slot = session.sprite_map[fixed_enemy_id]
    str_slot = session.battle_strings[fixed_enemy_id]
    orig_main = sprite_slot.main_sprite
    orig_upper = sprite_slot.upperscreen_sprites
    orig_str = str_slot.value

    # pick a different base — find a combo index whose userData != current selection
    current_ix = reskin._combo.currentIndex()
    new_ix = (current_ix + 7) % reskin._combo.count()
    if new_ix == current_ix:
        new_ix = (current_ix + 1) % reskin._combo.count()
    new_base_id = reskin._combo.itemData(new_ix)
    new_source = session.sprite_map[new_base_id]
    new_source_str = session.battle_strings[new_base_id]

    reskin._combo.setCurrentIndex(new_ix)
    assert sprite_slot.main_sprite == new_source.main_sprite, "reskin didn't copy main_sprite"
    assert sprite_slot.upperscreen_sprites == new_source.upperscreen_sprites
    assert str_slot.value == new_source_str.value
    assert undo_stack.count() == 1, "reskin should be one undo entry"
    assert bytes(session.serialize_all()) != session.original_rom_data

    undo_stack.undo()
    assert sprite_slot.main_sprite == orig_main, "undo did not restore main_sprite"
    assert sprite_slot.upperscreen_sprites == orig_upper
    assert str_slot.value == orig_str
    assert bytes(session.serialize_all()) == session.original_rom_data, "reskin undo did not restore bytes"
    print(f"enemy reskin: 0x{fixed_enemy_id:x} -> slot 0x{new_base_id:x} round-trips cleanly")

    # custom-sprite enemies (e.g. 0x205 Grimmon) reverse-lookup to themselves
    # since the pickable list now covers every sprite_map slot. The combo is
    # still enabled — no special-case gating.
    enemy_editor._on_selection(0x205)
    assert reskin._combo.isEnabled(), "combo should remain enabled for custom-sprite bosses"
    print("enemy reskin: custom-sprite boss combo stays enabled (full sprite-map coverage)")

    undo_stack.clear()

    # ---- move editor -----------------------------------------------------
    undo_stack.clear()
    move_editor = MoveEditor(session.moves, undo_stack, session)
    first_move = session.moves[0]
    orig_mp = first_move.mp_cost
    move_editor._mp_spin.setValue(orig_mp + 5)
    assert first_move.mp_cost == orig_mp + 5
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_move.mp_cost == orig_mp
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("moves: mp_cost edit round-trips through undo cleanly")

    # ---- move signed-value encoding --------------------------------------
    # Stat-modifier effects (atk/def/spi/spd, resistance, elemental res) store
    # the value as a signed two's-complement u16. The editor's primary/
    # secondary value spinbox should flip into signed mode when the effect id
    # is one of those, and convert -28 ↔ 65508 transparently.
    # Find an existing move that already uses a Reduce-style primary effect.
    reduce_move = next(
        (m for m in session.moves if m.primary_effect in (0x05, 0x06, 0x07, 0x08, 0x0F)),
        None,
    )
    if reduce_move is not None:
        move_editor._on_selection(session.moves.index(reduce_move))
        pv = move_editor._primary_value_spin
        # vanilla raw → expected display under signed interpretation
        raw = reduce_move.primary_value
        expected_display = raw - 0x10000 if raw >= 0x8000 else raw
        assert pv.value() == expected_display, (
            f"signed display mismatch: raw=0x{raw:04x}, "
            f"expected display={expected_display}, got {pv.value()}"
        )
        # typing -28 should store 65508 on the model
        pv.setValue(-28)
        assert reduce_move.primary_value == 0xFFE4, (
            f"-28 should store as 0xFFE4 (65508); got 0x{reduce_move.primary_value:04x}"
        )
        # undo restores
        undo_stack.undo()
        assert reduce_move.primary_value == raw
        assert bytes(session.serialize_all()) == session.original_rom_data
        print(f"moves: signed value encoding works for effect 0x{reduce_move.primary_effect:02x}")

    # ---- standard digivolution editor ------------------------------------
    undo_stack.clear()
    sd_editor = StandardDigivolutionEditor(session.standard_digivolutions, undo_stack, session)
    first_digimon_id = next(iter(sorted(session.standard_digivolutions.keys())))
    record = session.standard_digivolutions[first_digimon_id]
    orig_evo_1 = record.evolution_1_id
    orig_cond_val = record.evo_1_condition_value_1

    # bump a condition value via the bound widget on group "Evolution 1"
    evo1_group = sd_editor._groups[1]
    evo1_group._cond_value_fields[0]._spin.setValue(orig_cond_val + 3)
    assert record.evo_1_condition_value_1 == orig_cond_val + 3, "condition value edit not propagated"

    # select "(none)" on evo 1 (index 0 of the combo is the none_value sentinel)
    evo1_group._id_field.combo.setCurrentIndex(0)
    assert record.evolution_1_id == NO_EVO_SENTINEL, "(none) selection did not write the 0xFFFFFFFF sentinel"

    # undo both edits
    undo_stack.undo()
    assert record.evolution_1_id == orig_evo_1, "undo did not restore evolution_1_id"
    undo_stack.undo()
    assert record.evo_1_condition_value_1 == orig_cond_val, "undo did not restore condition value"
    assert bytes(session.serialize_all()) == session.original_rom_data, "std-evo undo did not restore vanilla bytes"
    print("std_evo: condition-value edit + None toggle both round-trip cleanly")

    # ---- armor digivolution editor ---------------------------------------
    undo_stack.clear()
    armor_editor = ArmorDigivolutionEditor(session.armor_digivolutions, undo_stack, session)
    first_armor = session.armor_digivolutions[0]
    orig_cond_val = first_armor.condition_value_1
    armor_editor._cond_rows[0]._value_spin.setValue(orig_cond_val + 11)  # spin still bound directly
    assert first_armor.condition_value_1 == orig_cond_val + 11
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_armor.condition_value_1 == orig_cond_val
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("armor: condition-value edit round-trips through undo cleanly")

    # ---- DNA digivolution editor -----------------------------------------
    undo_stack.clear()
    dna_editor = DNADigivolutionEditor(session.dna_digivolutions, undo_stack, session)
    first_dna = session.dna_digivolutions[0]
    orig_d1 = first_dna.digimon_1_id
    # _d1_row is a BoundIdComboRow (combo + inline label); reach into `.combo`
    # to drive the underlying picker.
    d1_combo = dna_editor._d1_row.combo
    new_ix = next(
        i for i in range(d1_combo.count())
        if d1_combo.itemData(i) != orig_d1
    )
    target_d1 = d1_combo.itemData(new_ix)
    d1_combo.setCurrentIndex(new_ix)
    assert first_dna.digimon_1_id == target_d1
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_dna.digimon_1_id == orig_d1
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("dna: digimon_1_id combo edit round-trips through undo cleanly")

    # ---- quest editor ----------------------------------------------------
    undo_stack.clear()
    quest_editor = QuestEditor(session.quests, undo_stack, session)
    first_quest = session.quests[0]
    orig_money = first_quest.money_reward
    quest_editor._money_spin.setValue(orig_money + 1000)
    assert first_quest.money_reward == orig_money + 1000
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_quest.money_reward == orig_money
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("quests: money_reward edit round-trips through undo cleanly")

    # ---- starters editor -------------------------------------------------
    undo_stack.clear()
    starters_editor = StartersEditor(session.starters, undo_stack)
    first_starter = session.starters[0]
    orig_level = first_starter.level
    # _rows is a flat list of all 12 starter rows across the 4 pack group-boxes
    starters_editor._rows[0].level_spin.setValue(orig_level + 3)
    assert first_starter.level == orig_level + 3
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_starter.level == orig_level
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"starters: {len(session.starters)} loaded, level edit round-trips cleanly")

    # ---- wild encounters editor ------------------------------------------
    undo_stack.clear()
    wild_editor = WildEncountersEditor(session.version, session.wild_encounter_areas, undo_stack, session)
    # find the first area that actually has encounters
    populated_ix = next(
        ix for ix, a in enumerate(session.wild_encounter_areas) if a.encounters
    )
    populated_area = session.wild_encounter_areas[populated_ix]
    # selecting an area triggers _on_selection which rebuilds the encounter rows
    wild_editor._on_selection(populated_ix)
    first_enc = populated_area.encounters[0]
    orig_id = first_enc.digimon_id
    # id_combo is a BoundIdComboRow (combo + inline label); reach into `.combo`
    id_combo = wild_editor._encounter_rows[0].id_combo.combo
    new_ix = next(
        i for i in range(id_combo.count())
        if id_combo.itemData(i) is not None and id_combo.itemData(i) != orig_id
    )
    target_id = id_combo.itemData(new_ix)
    id_combo.setCurrentIndex(new_ix)
    assert first_enc.digimon_id == target_id
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_enc.digimon_id == orig_id
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(
        f"wild encounters: {len(session.wild_encounter_areas)} areas loaded, "
        f"digimon_id edit round-trips cleanly"
    )

    # ---- encounter rewards editor ----------------------------------------
    undo_stack.clear()
    rewards_editor = EncounterRewardsEditor(session.encounter_rewards, undo_stack, session)
    first_table = session.encounter_rewards[0]
    orig_prob = first_table.probabilitiesArray[0]
    orig_reward = first_table.rewardsArray[0]

    # bump probability via the bound spinbox
    rewards_editor._rows[0].prob_spin.setValue(orig_prob + 5)
    assert first_table.probabilitiesArray[0] == orig_prob + 5

    # switch slot 0 to Money type and set the user-visible amount to 250 bit.
    # The ROM encoding should be 0x10000 - 250.
    slot0 = rewards_editor._rows[0]
    from editor.widgets.encounter_rewards_editor import TYPE_MONEY, _pack_money
    money_ix = next(
        i for i in range(slot0.type_combo.count())
        if slot0.type_combo.itemData(i) == TYPE_MONEY
    )
    slot0.type_combo.setCurrentIndex(money_ix)
    slot0.money_spin.setValue(250)
    assert first_table.rewardsArray[0] == _pack_money(250), \
        f"money pack failed: expected raw 0x{_pack_money(250):x}, got 0x{first_table.rewardsArray[0]:x}"

    assert bytes(session.serialize_all()) != session.original_rom_data
    # Undo until vanilla. The money path may push two commands (type switch +
    # money spin), so undo until the stack is empty.
    while undo_stack.canUndo():
        undo_stack.undo()
    assert first_table.probabilitiesArray[0] == orig_prob
    assert first_table.rewardsArray[0] == orig_reward
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"encounter rewards: {len(session.encounter_rewards)} tables, money-encoding edit round-trips cleanly")

    # ---- habitats / worldmap editor --------------------------------------
    undo_stack.clear()
    habitats_editor = HabitatsWorldmapEditor(session.habitats_worldmap, undo_stack, session)
    first_habitat = session.habitats_worldmap[0]
    orig_mask = first_habitat.species_living
    # toggle bit 0 (HOLY)
    species_row = next(
        w for w in habitats_editor._all_widgets
        if w.__class__.__name__ == "_SpeciesFlagsRow"
    )
    species_row._checks[0].setChecked(not species_row._checks[0].isChecked())
    assert first_habitat.species_living != orig_mask, "species mask edit not propagated"
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_habitat.species_living == orig_mask
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"habitats: {len(session.habitats_worldmap)} entries, species-flag toggle round-trips cleanly")

    # ---- farm terrains editor --------------------------------------------
    undo_stack.clear()
    farm_editor = FarmTerrainsEditor(session.farm_terrains, undo_stack, session)
    first_terrain = session.farm_terrains[0]
    orig_limit = first_terrain.farm_digimon_limit
    # find the farm_digimon_limit spinbox via _all_widgets[1] (id, then limit)
    limit_spin = farm_editor._all_widgets[1]
    limit_spin.setValue(orig_limit + 1)
    assert first_terrain.farm_digimon_limit == orig_limit + 1
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_terrain.farm_digimon_limit == orig_limit
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"farm terrains: {len(session.farm_terrains)} entries, limit edit round-trips cleanly")

    # ---- equipment editor ------------------------------------------------
    undo_stack.clear()
    equipment_editor = EquipmentEditor(session.equipment, undo_stack, session)
    first_eq_id = next(iter(sorted(session.equipment.keys())))
    first_eq = session.equipment[first_eq_id]
    orig_atk = first_eq.atk_boost
    # find the atk_boost widget — first widget in the "Stat Boosts" group is atk
    atk_widget = None
    for w in equipment_editor._all_widgets:
        if hasattr(w, "_attr") and getattr(w, "_attr", None) == "atk_boost":
            atk_widget = w
            break
    assert atk_widget is not None, "could not locate atk_boost widget"
    atk_widget.setValue(orig_atk + 5)
    assert first_eq.atk_boost == orig_atk + 5
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_eq.atk_boost == orig_atk
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"equipment: {len(session.equipment)} loaded, atk_boost edit round-trips cleanly")

    # ---- consumable editor -----------------------------------------------
    undo_stack.clear()
    consumable_editor = ConsumableEditor(session.consumables, undo_stack, session)
    first_cons = session.consumables[0]
    orig_cost = first_cons.bit_cost
    cost_widget = None
    for w in consumable_editor._all_widgets:
        if hasattr(w, "_attr") and getattr(w, "_attr", None) == "bit_cost":
            cost_widget = w
            break
    assert cost_widget is not None
    cost_widget.setValue(orig_cost + 100)
    assert first_cons.bit_cost == orig_cost + 100
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_cons.bit_cost == orig_cost
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"consumables: {len(session.consumables)} loaded, bit_cost edit round-trips cleanly")

    # ---- farm-items editor -----------------------------------------------
    undo_stack.clear()
    farm_item_editor = FarmItemEditor(session.farm_items, undo_stack, session)
    first_fi = session.farm_items[0]
    orig_max = first_fi.max_points
    max_widget = None
    for w in farm_item_editor._all_widgets:
        if hasattr(w, "_attr") and getattr(w, "_attr", None) == "max_points":
            max_widget = w
            break
    assert max_widget is not None
    max_widget.setValue(orig_max + 25)
    assert first_fi.max_points == orig_max + 25
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_fi.max_points == orig_max
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"farm items: {len(session.farm_items)} loaded, max_points edit round-trips cleanly")

    # ---- string editor (text) --------------------------------------------
    # Strings sit at fixed ROM offsets — pointers aren't repointed — so the
    # editor's whole correctness story is "the encoded bytes stay within
    # original_byte_length". Walk one bucket end-to-end: pick a string,
    # round-trip a same-length edit through undo, then deliberately blow
    # past the budget and check both the save-guard list and the validation
    # collector light up. Use a bucket with non-trivial content (MSG.PAK)
    # to also exercise the fast-path identity check on _initial_text.
    undo_stack.clear()
    msgpak_strings = []
    for region_id, region_strings in session.string_regions.items():
        if region_id.startswith("msgpak_"):
            msgpak_strings.extend(region_strings)
    msgpak_strings.sort(key=lambda s: s.offset)
    assert msgpak_strings, "no MSG.PAK strings loaded — region config regression?"
    string_editor = StringEditor(msgpak_strings, undo_stack)

    # Find a string with at least 4 chars of plain text we can mutate
    # in-place without changing encoded length (so the round-trip survives
    # without padding semantics getting in the way).
    plain = next(
        (s for s in msgpak_strings
         if len(s.text) >= 4 and "[" not in s.text and s.fits()),
        None,
    )
    assert plain is not None, "no plain MSG.PAK string available for editing"
    orig_text = plain.text
    # `_set_current` jumps the editor to a specific GameString.
    string_editor._set_current(plain)
    # Substitute first char with one of equal encoded width (any ASCII letter
    # is 2 bytes encoded, same as any other). textChanged starts the debounce
    # timer; bypass it by calling _commit_pending_edit directly so the test
    # doesn't depend on the Qt event loop.
    new_first = "Z" if orig_text[0] != "Z" else "Y"
    string_editor._text_edit.setPlainText(new_first + orig_text[1:])
    string_editor._commit_pending_edit()
    assert plain.text != orig_text, "string edit did not propagate to model"
    assert undo_stack.count() == 1
    assert bytes(session.serialize_all()) != session.original_rom_data, \
        "string edit not reflected in serialize_all()"
    undo_stack.undo()
    assert plain.text == orig_text, "undo did not restore string text"
    assert bytes(session.serialize_all()) == session.original_rom_data, \
        "undo did not restore vanilla bytes"
    undo_stack.clear()
    print(f"strings: edited MSG.PAK string at 0x{plain.offset:08X}, round-trip clean")

    # Over-budget detection — must show up in both session.over_budget_strings()
    # and the string_issues() collector (the latter is what routes through the
    # validation footer's click-to-navigate).
    assert session.over_budget_strings() == [], "unexpected over-budget strings on vanilla"
    assert string_issues(session.string_regions) == [], "string_issues dirty on vanilla"
    plain.text = "X" * (plain.original_byte_length * 2)  # guaranteed overflow
    bad = session.over_budget_strings()
    assert plain in bad, "over_budget_strings() failed to surface inflated string"
    issues = string_issues(session.string_regions)
    assert any(i.record_id == plain.offset for i in issues), \
        "string_issues() did not include the inflated string"
    assert any(i.category == "Over budget" for i in issues)
    # Restore — leave the session clean for whatever runs next.
    plain.text = orig_text
    assert session.over_budget_strings() == []
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"strings: {sum(len(v) for v in session.string_regions.values())} loaded, "
          f"over-budget detection + collector wired correctly")

    # ---- cross-reference navigation --------------------------------------
    # select_by_id should clear any active filter and land on the target row.
    target_did = next(iter(sorted(session.base_digimon.keys())))
    assert base_editor.select_by_id(target_did), "base select_by_id failed"
    assert enemy_editor.select_by_id(target_did), "enemy select_by_id failed"
    assert move_editor.select_by_id(3), "move select_by_id failed"
    assert equipment_editor.select_by_id(next(iter(sorted(session.equipment.keys())))), \
        "equipment select_by_id failed"
    assert consumable_editor.select_by_id(session.consumables[0].id), \
        "consumable select_by_id failed"
    assert farm_item_editor.select_by_id(session.farm_items[0].id), \
        "farm-item select_by_id failed"
    print("nav: select_by_id works on base/enemy/move/equipment/consumable/farm-item editors")

    # ---- validation warnings --------------------------------------------
    # stat caps in base digimon: HP > 9999 → warn, ATK > 999 → warn
    hp_widget = base_editor._stat_widgets["hp"]
    atk_widget = base_editor._stat_widgets["attack"]
    orig_hp = hp_widget.value()
    orig_atk = atk_widget.value()
    hp_widget.setValue(10000)
    assert "border" in hp_widget.styleSheet(), "HP warning style not applied for >9999"
    hp_widget.setValue(9999)
    assert "border" not in hp_widget.styleSheet(), "HP warning style not cleared at cap"
    atk_widget.setValue(1000)
    assert "border" in atk_widget.styleSheet(), "ATK warning style not applied for >999"
    # restore + drain undo so the cleanliness check at end stays green
    hp_widget.setValue(orig_hp)
    atk_widget.setValue(orig_atk)
    while undo_stack.canUndo():
        undo_stack.undo()
    assert bytes(session.serialize_all()) == session.original_rom_data, \
        "validation test must leave bytes vanilla"
    print("validation: HP/MP/ATK caps flag visually and clear when in range")

    # starter level cap reacts to digimon_id change → uses base aptitude
    starter_row = starters_editor._rows[0]
    # find the first combo entry whose digimon id has a base record (so we can
    # compare against a real aptitude). digimon_choices() may include named-
    # only ids not present in base_digimon, so iterate until we hit a match.
    combo = starter_row.id_combo.combo
    sample_ix = next(
        i for i in range(combo.count())
        if isinstance(combo.itemData(i), int) and combo.itemData(i) in session.base_digimon
    )
    sample_id = combo.itemData(sample_ix)
    sample_apt = session.base_digimon[sample_id].aptitude
    combo.setCurrentIndex(sample_ix)
    # level under the aptitude → safe; above → warning
    starter_row.level_spin.setValue(max(sample_apt - 1, 0))
    assert "border" not in starter_row.level_spin.styleSheet(), \
        "starter level should be safe at apt-1"
    starter_row.level_spin.setValue(sample_apt + 1)
    assert "border" in starter_row.level_spin.styleSheet(), \
        "starter level should warn above aptitude"
    while undo_stack.canUndo():
        undo_stack.undo()
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"validation: starter level reacts to aptitude (sampled apt={sample_apt})")

    # exp_curve cap warns above 2 (only 0/1/2 are valid engine curves)
    exp_widget = base_editor._misc_widgets["exp_curve"]
    orig_exp = exp_widget.value()
    exp_widget.setValue(2)
    assert "border" not in exp_widget.styleSheet(), "exp_curve=2 should be safe"
    exp_widget.setValue(3)
    assert "border" in exp_widget.styleSheet(), "exp_curve=3 should warn"
    exp_widget.setValue(orig_exp)
    while undo_stack.canUndo():
        undo_stack.undo()
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("validation: exp_curve flags values outside 0..2")

    # standard-digivolution reciprocal invariant: vanilla data is consistent,
    # so all four group combos should be unmarked at load. Pick a record with
    # an evolution_1 target and rewrite it to a digimon whose `degen_evo_id`
    # does NOT point back — the evo_1 combo should light up.
    undo_stack.clear()
    sd_id_with_evo = next(
        did for did, r in session.standard_digivolutions.items()
        if r.evolution_1_id != NO_EVO_SENTINEL
    )
    sd_editor._on_selection(sd_id_with_evo)
    evo1_combo = sd_editor._groups[1]._id_field.combo
    # vanilla should be clean
    assert "border" not in evo1_combo.styleSheet(), \
        f"vanilla evo_1 on 0x{sd_id_with_evo:x} should not warn"
    # walk combo items directly (digimon_choices() is a subset of all ids),
    # picking the first one whose degen_evo_id doesn't reciprocate
    rec = session.standard_digivolutions[sd_id_with_evo]
    target_ix = None
    for i in range(evo1_combo.count()):
        cid = evo1_combo.itemData(i)
        if not isinstance(cid, int) or cid == sd_id_with_evo:
            continue
        cand_rec = session.standard_digivolutions.get(cid)
        if cand_rec is None or cand_rec.degen_evo_id == sd_id_with_evo:
            continue
        if cid in (rec.evolution_1_id, rec.evolution_2_id, rec.evolution_3_id):
            continue
        target_ix = i
        break
    assert target_ix is not None, "no suitable non-reciprocal candidate found"
    evo1_combo.setCurrentIndex(target_ix)
    assert "border" in evo1_combo.styleSheet(), \
        "evo_1 should warn after pointing at a digimon whose degen doesn't reciprocate"
    while undo_stack.canUndo():
        undo_stack.undo()
    assert "border" not in evo1_combo.styleSheet(), \
        "undo should clear the reciprocal-mismatch warning"
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("validation: std_evo reciprocal mismatch flags & clears")

    # wild-encounter reward_slot warns above table length
    wild_editor._on_selection(populated_ix)
    rs = wild_editor._encounter_rows[0].reward_spin
    rs.setValue(len(session.encounter_rewards) - 1)
    assert "border" not in rs.styleSheet(), "reward_slot at table-last should be safe"
    rs.setValue(len(session.encounter_rewards))
    assert "border" in rs.styleSheet(), "reward_slot past table should warn"
    while undo_stack.canUndo():
        undo_stack.undo()
    assert bytes(session.serialize_all()) == session.original_rom_data
    print(f"validation: reward_slot warns past table size ({len(session.encounter_rewards)})")

    # ---- dirty markers ---------------------------------------------------
    # is_record_dirty reports clean records at load time, dirty after an edit,
    # clean again after undo. Each list-panel kind (DigimonListPanel,
    # RecordListPanel, MoveListPanel) prepends "● " to the row label when
    # dirty and matching whitespace when clean.
    undo_stack.clear()
    base_target_id = next(iter(sorted(session.base_digimon.keys())))
    base_target = session.base_digimon[base_target_id]
    base_row = base_editor._list_panel._row_by_id[base_target_id]
    base_item = base_editor._list_panel._source_model.item(base_row)

    assert not is_record_dirty(base_target), "base record dirty at load"
    assert base_item.text().startswith("  "), "clean base row should have whitespace prefix"

    base_editor._on_selection(base_target_id)
    hp_spin = base_editor._stat_widgets["hp"]
    orig_hp = hp_spin.value()
    hp_spin.setValue(orig_hp + 13)
    assert is_record_dirty(base_target), "base record should be dirty after edit"
    assert base_item.text().startswith("● "), \
        f"dirty base row should start with '● ', got {base_item.text()!r}"

    undo_stack.undo()
    assert not is_record_dirty(base_target), "base record should be clean after undo"
    assert base_item.text().startswith("  "), "post-undo base row should drop dirty marker"

    # RecordListPanel — exercise via the quest editor.
    quest_target = session.quests[0]
    quest_item = quest_editor._list_panel._source_model.item(0)
    assert not is_record_dirty(quest_target)
    assert quest_item.text().startswith("  ")
    quest_editor._on_selection(0)
    quest_editor._money_spin.setValue(quest_target.money_reward + 500)
    assert is_record_dirty(quest_target)
    assert quest_item.text().startswith("● ")
    undo_stack.undo()
    assert not is_record_dirty(quest_target)
    assert quest_item.text().startswith("  ")

    # MoveListPanel — exercise via the move editor.
    move_target = session.moves[1]
    move_row = move_editor._list_panel._row_by_id[move_target.id]
    move_item = move_editor._list_panel._source_model.item(move_row)
    assert not is_record_dirty(move_target)
    assert move_item.text().startswith("  ")
    move_editor._on_selection(1)
    move_editor._mp_spin.setValue(move_target.mp_cost + 7)
    assert is_record_dirty(move_target)
    assert move_item.text().startswith("● ")
    undo_stack.undo()
    assert not is_record_dirty(move_target)
    assert move_item.text().startswith("  ")

    assert bytes(session.serialize_all()) == session.original_rom_data, \
        "dirty-marker test must leave bytes vanilla"
    print("dirty markers: base/record/move list panels toggle marker on edit and clear on undo")

    # ---- validation footer registry --------------------------------------
    # Footer collectors mirror the per-widget red-border caps so every visible
    # warning also surfaces in the footer list. Vanilla data on Dusk sits
    # within every cap, so the baseline is empty.
    undo_stack.clear()
    reg = validation_registry()
    reg.clear()
    reg.register(lambda: base_digimon_issues(session.base_digimon))
    reg.register(lambda: std_digivolution_issues(session.standard_digivolutions))
    reg.register(lambda: move_issues(session.moves))
    reg.register(lambda: starter_issues(session.starters, session.base_digimon))
    reg.register(lambda: wild_encounter_issues(
        session.version,
        session.wild_encounter_areas,
        len(session.encounter_rewards),
    ))

    assert reg.collect_all() == [], \
        f"vanilla data should yield no footer issues; got {len(reg.collect_all())}"

    # Exp Curve breach on the first base digimon.
    base_first_id = next(iter(sorted(session.base_digimon.keys())))
    base_first = session.base_digimon[base_first_id]
    saved_exp = base_first.exp_curve
    base_first.exp_curve = 5
    issues = reg.collect_all()
    assert any(
        i.section == "Base Digimon" and i.category == "Exp Curve"
        for i in issues
    ), f"exp_curve=5 should surface as a footer issue; got {issues}"
    base_first.exp_curve = saved_exp
    assert reg.collect_all() == [], "restoring exp_curve must return to clean"

    # Stat cap breach: HP > 9999 must surface in the footer.
    saved_hp = base_first.hp
    base_first.hp = 10000
    issues = reg.collect_all()
    assert any(
        i.section == "Base Digimon" and i.category == "HP"
        for i in issues
    ), f"HP=10000 should surface as a footer issue; got {issues}"
    base_first.hp = saved_hp
    assert reg.collect_all() == [], "restoring HP must return to clean"

    # Level cap breach: level > 99 must surface.
    saved_level = base_first.level
    base_first.level = 100
    issues = reg.collect_all()
    assert any(
        i.section == "Base Digimon" and i.category == "Level"
        for i in issues
    ), f"level=100 should surface as a footer issue; got {issues}"
    base_first.level = saved_level
    assert reg.collect_all() == [], "restoring level must return to clean"

    # Move mp_cost cap breach.
    saved_mp = session.moves[1].mp_cost
    session.moves[1].mp_cost = 10000
    issues = reg.collect_all()
    assert any(
        i.section == "Moves" and i.category == "MP Cost"
        for i in issues
    ), f"mp_cost=10000 should surface as a footer issue; got {issues}"
    session.moves[1].mp_cost = saved_mp
    assert reg.collect_all() == [], "restoring mp_cost must return to clean"

    # Starter level above aptitude.
    s0 = session.starters[0]
    apt = session.base_digimon[s0.digimon_id].aptitude if s0.digimon_id in session.base_digimon else 99
    saved_starter_level = s0.level
    s0.level = apt + 5
    issues = reg.collect_all()
    assert any(
        i.section == "Starters" and i.category == "Level vs Aptitude"
        for i in issues
    ), f"starter level above aptitude should surface; got {issues}"
    s0.level = saved_starter_level
    assert reg.collect_all() == [], "restoring starter level must return to clean"

    # Wild encounter reward_slot above the table.
    enc = next(
        e for area in session.wild_encounter_areas for e in area.encounters
    )
    saved_slot = enc.reward_slot
    enc.reward_slot = len(session.encounter_rewards) + 1
    issues = reg.collect_all()
    assert any(
        i.section == "Wild Encounters" and i.category == "Reward Slot"
        for i in issues
    ), f"reward_slot past the table should surface; got {issues}"
    enc.reward_slot = saved_slot
    assert reg.collect_all() == [], "restoring reward_slot must return to clean"

    # Digivolution reciprocal mismatch.
    sd_ids = sorted(session.standard_digivolutions.keys())
    src_rec = session.standard_digivolutions[sd_ids[0]]
    saved_evo = src_rec.evolution_1_id
    bad_target = next(
        did for did in sd_ids[1:]
        if session.standard_digivolutions[did].degen_evo_id != sd_ids[0]
        and did != saved_evo
    )
    src_rec.evolution_1_id = bad_target
    issues = reg.collect_all()
    assert any(i.section == "Standard Digivolutions" for i in issues), \
        "std digivolution collector should flag the new reciprocity break"
    src_rec.evolution_1_id = saved_evo
    assert reg.collect_all() == [], "restoring std-evo state must return to clean"

    assert bytes(session.serialize_all()) == session.original_rom_data, \
        "validation-collector test must leave bytes vanilla"
    print("validation footer: all collectors mirror widget caps; vanilla is clean")

    # ---- QoL editor ------------------------------------------------------
    # QoL toggles live on session.qol (a QolSettings dataclass); flipping a
    # checkbox should push an undo entry, scale the output bytes via
    # serialize_all_with_qol(), and leave plain serialize_all() unaffected.
    undo_stack.clear()
    qol_editor = QolEditor(session.qol, undo_stack)
    # all off by default → both serialisers must match vanilla
    assert bytes(session.serialize_all_with_qol()) == session.original_rom_data, \
        "QoL-off serialize_all_with_qol must equal vanilla"
    # find the fast_text checkbox among the rows and flip it
    fast_text_check = next(c for c, _p in qol_editor._rows if c._attr == "fast_text")
    assert not fast_text_check.isChecked()
    fast_text_check.setChecked(True)
    assert session.qol.fast_text is True
    assert undo_stack.count() == 1, "QoL toggle should be one undo entry"
    assert bytes(session.serialize_all()) == session.original_rom_data, \
        "plain serialize_all must stay vanilla even with QoL on"
    assert bytes(session.serialize_all_with_qol()) != session.original_rom_data, \
        "QoL-on serialize_all_with_qol must differ from vanilla"
    undo_stack.undo()
    assert session.qol.fast_text is False
    assert bytes(session.serialize_all_with_qol()) == session.original_rom_data, \
        "QoL undo must restore vanilla output"
    print("qol: toggle pushes undo, scales output bytes, leaves serialize_all clean")

    # exercise the absolute-value movement-speed param: toggle on, bump value.
    # Vanilla byte is 2; pick something else so the output differs.
    mv_check = next(c for c, _p in qol_editor._rows if c._attr == "fast_movement")
    mv_param = next(p for c, p in qol_editor._rows if c is mv_check)
    assert mv_param is not None, "fast_movement row should have a linked param"
    assert session.qol.movement_speed == 2, "vanilla movement byte should seed default"
    mv_check.setChecked(True)
    mv_param.setValue(8)
    assert session.qol.fast_movement is True
    assert session.qol.movement_speed == 8
    mvd = bytes(session.serialize_all_with_qol())
    assert mvd != session.original_rom_data, "movement-speed QoL should mutate the output"
    assert mvd[constants.MOVEMENT_SPEED_OFFSET[session.version]] == 8
    while undo_stack.canUndo():
        undo_stack.undo()
    assert bytes(session.serialize_all_with_qol()) == session.original_rom_data
    print("qol: movement-speed absolute value round-trips through undo cleanly")

    # ---- project file round-trip ----------------------------------------
    # Save the (currently-vanilla) session's edited state as a .romproj, then
    # apply some edits, save again, reload via from_project and verify the
    # patched bytes match what was in memory when the project was written.
    import tempfile
    from editor import project_file as pf

    undo_stack.clear()
    # mutate a known field so the diff has at least one entry
    base_target_id = next(iter(sorted(session.base_digimon.keys())))
    base_target = session.base_digimon[base_target_id]
    orig_hp = base_target.hp
    base_target.hp = orig_hp + 42
    # set a QoL toggle to verify it survives the round-trip without polluting
    # the byte diff (QoL goes into its own field on the project file)
    session.qol.fast_text = True

    edited_bytes = bytes(session.serialize_all())
    with tempfile.NamedTemporaryFile(suffix=".romproj", delete=False, mode="w", encoding="utf-8") as tf:
        tmp_path = tf.name
    try:
        pf.save_project(
            tmp_path,
            rom_version=session.version,
            vanilla_rom_data=session.original_rom_data,
            edited_rom_data=edited_bytes,
            qol=session.qol,
        )
        project = pf.load_project(tmp_path)
        assert project["rom_version"] == session.version
        assert project["vanilla_rom_sha256"] == pf.vanilla_sha256(session.original_rom_data)
        assert project["qol"].fast_text is True
        # rebuild patched bytes from vanilla + diffs and compare
        patched = bytearray(session.original_rom_data)
        pf.apply_byte_diff(patched, project["diffs"])
        assert bytes(patched) == edited_bytes, "round-tripped bytes diverge from edited bytes"
        # round-trip via RomSession.from_project
        reloaded = RomSession.from_project(
            project_path=tmp_path,
            vanilla_path=rom_path,
            vanilla_data=session.original_rom_data,
            version=session.version,
            patched_data=bytes(patched),
            qol_settings=project["qol"],
        )
        assert reloaded.base_digimon[base_target_id].hp == orig_hp + 42
        assert reloaded.qol.fast_text is True
        assert reloaded.project_path == tmp_path
        assert reloaded.source_path is None
        print("project file: save + reload preserves edits, QoL state, and vanilla baseline")
    finally:
        os.unlink(tmp_path)

    # restore state for any tests that follow
    base_target.hp = orig_hp
    session.qol.fast_text = False
    assert bytes(session.serialize_all()) == session.original_rom_data

    base_editor.deleteLater()
    enemy_editor.deleteLater()
    move_editor.deleteLater()
    sd_editor.deleteLater()
    armor_editor.deleteLater()
    dna_editor.deleteLater()
    quest_editor.deleteLater()
    starters_editor.deleteLater()
    wild_editor.deleteLater()
    rewards_editor.deleteLater()
    habitats_editor.deleteLater()
    farm_editor.deleteLater()
    equipment_editor.deleteLater()
    consumable_editor.deleteLater()
    farm_item_editor.deleteLater()
    string_editor.deleteLater()
    qol_editor.deleteLater()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
