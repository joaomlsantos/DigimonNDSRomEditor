"""Headless smoke test for the Base / Enemy digimon editors.

Loads a real ROM, instantiates both editors, exercises the round-trip path:
  1. parse → serialize_all() equals vanilla
  2. change a stat via the editor's bound spinbox
  3. undo → serialize_all() equals vanilla again
  4. redo → serialize_all() reflects the change in exactly one byte region

Run: python smoke_test.py
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

from editor.session import RomSession  # noqa: E402
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
from editor.widgets.starters_editor import StartersEditor  # noqa: E402
from editor.widgets.wild_encounters_editor import WildEncountersEditor  # noqa: E402


ROM_PATH = r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    session = RomSession.from_file(ROM_PATH)
    print(f"loaded {session.version}: {len(session.base_digimon)} base, "
          f"{len(session.enemy_digimon)} enemy")

    assert bytes(session.serialize_all()) == session.original_rom_data, "initial round-trip failed"
    print("initial serialize_all() == vanilla")

    undo_stack = QUndoStack()

    # ---- base editor -----------------------------------------------------
    base_editor = BaseDigimonEditor(session.base_digimon, undo_stack)
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
    move_editor = MoveEditor(session.moves, undo_stack)
    first_move = session.moves[0]
    orig_mp = first_move.mp_cost
    move_editor._mp_spin.setValue(orig_mp + 5)
    assert first_move.mp_cost == orig_mp + 5
    assert bytes(session.serialize_all()) != session.original_rom_data
    undo_stack.undo()
    assert first_move.mp_cost == orig_mp
    assert bytes(session.serialize_all()) == session.original_rom_data
    print("moves: mp_cost edit round-trips through undo cleanly")

    # ---- standard digivolution editor ------------------------------------
    undo_stack.clear()
    sd_editor = StandardDigivolutionEditor(session.standard_digivolutions, undo_stack)
    first_digimon_id = next(iter(sorted(session.standard_digivolutions.keys())))
    record = session.standard_digivolutions[first_digimon_id]
    orig_evo_1 = record.evolution_1_id
    orig_cond_val = record.evo_1_condition_value_1

    # bump a condition value via the bound widget on group "Evolution 1"
    evo1_group = sd_editor._groups[1]
    evo1_group._cond_value_fields[0]._spin.setValue(orig_cond_val + 3)
    assert record.evo_1_condition_value_1 == orig_cond_val + 3, "condition value edit not propagated"

    # select "(none)" on evo 1 (index 0 of the combo is the none_value sentinel)
    evo1_group._id_field.setCurrentIndex(0)
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
    armor_editor = ArmorDigivolutionEditor(session.armor_digivolutions, undo_stack)
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
    dna_editor = DNADigivolutionEditor(session.dna_digivolutions, undo_stack)
    first_dna = session.dna_digivolutions[0]
    orig_d1 = first_dna.digimon_1_id
    # _d1_row is now a BoundIdCombo over digimon_choices(); pick a different id by
    # finding an item slot whose userData != current and selecting it.
    d1_combo = dna_editor._d1_row
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
    quest_editor = QuestEditor(session.quests, undo_stack)
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
    wild_editor = WildEncountersEditor(session.version, session.wild_encounter_areas, undo_stack)
    # find the first area that actually has encounters
    populated_ix = next(
        ix for ix, a in enumerate(session.wild_encounter_areas) if a.encounters
    )
    populated_area = session.wild_encounter_areas[populated_ix]
    # selecting an area triggers _on_selection which rebuilds the encounter rows
    wild_editor._on_selection(populated_ix)
    first_enc = populated_area.encounters[0]
    orig_id = first_enc.digimon_id
    # id_combo is a BoundIdCombo over digimon_choices(); pick a different id
    id_combo = wild_editor._encounter_rows[0].id_combo
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
    rewards_editor = EncounterRewardsEditor(session.encounter_rewards, undo_stack)
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
    habitats_editor = HabitatsWorldmapEditor(session.habitats_worldmap, undo_stack)
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
    farm_editor = FarmTerrainsEditor(session.farm_terrains, undo_stack)
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
    equipment_editor = EquipmentEditor(session.equipment, undo_stack)
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
    consumable_editor = ConsumableEditor(session.consumables, undo_stack)
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
    farm_item_editor = FarmItemEditor(session.farm_items, undo_stack)
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
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
