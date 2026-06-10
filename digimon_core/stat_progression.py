"""Expected-stat preview for the enemy editor's coherency sidecar.

The "expected stats" panel in the enemy editor shows what an enemy's
six numeric stats *would* be if they were derived from the matching
base-data record by walking the game's per-level gain formula. This
keeps the two authoring surfaces honest about each other — see the
randomizer's :func:`generateLvlupStats` for the original formulation.

Inputs come from :mod:`digimon_core.constants`:

* :data:`~digimon_core.constants.LEVEL_UP_TABLE` — per-``DigimonType``
  gain pairs (max, base) for the six stats in HP/MP/atk/def/spr/spd
  order.
* :data:`~digimon_core.constants.HP_BUFF_BY_STAGE` — optional stage
  multiplier the randomizer applies to wild HP, surfaced as a toggle
  here so the preview can match the randomized world.
* :data:`~digimon_core.constants.STAGE_ID_RANGES` — id windows for the
  five stages, used to look up the HP multiplier without storing a
  per-record stage field.

The formulas are deterministic-only (no RNG) so the sidecar can
recompute on every keystroke without the user seeing values jitter.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

from .constants import HP_BUFF_BY_STAGE, LEVEL_UP_TABLE, STAGE_ID_RANGES


# Six stats whose growth the level-up table drives, in the same order
# the table's inner tuples use. Names match the BaseDataDigimon
# attributes so callers can `getattr(base, name)` directly.
PROGRESSION_STATS = ("hp", "mp", "attack", "defense", "spirit", "speed")


class ProgressionMode(Enum):
    """Which of the per-level roll's branches to use.

    ``RANDOM`` is intentionally absent — the sidecar previews stats
    while the user edits, so a stable answer is more useful than a
    realistic one. Callers that want a distribution should sample
    :func:`compute_expected_stats` at ``FIXED_MIN`` / ``MAX`` and
    treat the span as a range.
    """
    FIXED_MIN = "min"
    FIXED_AVG = "avg"
    FIXED_MAX = "max"


@dataclass(frozen=True)
class ExpectedStats:
    """Result of walking the level-up formula at a single mode.

    ``stats`` is keyed by the names in :data:`PROGRESSION_STATS`. The
    HP entry is post-buff if ``hp_buff_applied`` is True (the toggle's
    on-state), pre-buff otherwise. ``stage`` is empty when the base id
    falls outside every :data:`STAGE_ID_RANGES` window — the HP buff
    can't be applied in that case.
    """
    stats: Dict[str, int]
    stage: str
    hp_buff_applied: bool


def stage_for_base_id(base_id: int) -> str:
    """Map a base-data id to its stage name (or "" if outside the windows).

    Implementation mirrors ``getDigimonStage`` in
    ``DWDDRandomizer/src/utils.py``. The lone hole (0xBA inside the
    Champion range) is encoded in :data:`STAGE_ID_RANGES`.
    """
    for stage, lo, hi, holes in STAGE_ID_RANGES:
        if lo <= base_id <= hi and base_id not in holes:
            return stage
    return ""


def compute_expected_stats(
    base,
    target_level: int,
    *,
    mode: ProgressionMode = ProgressionMode.FIXED_AVG,
    apply_hp_buff: bool = False,
) -> ExpectedStats:
    """Walk the level-up formula from ``base.level`` to ``target_level``.

    Per-level stat update mirrors the randomizer:
    ``stat += (roll + base_gain) // 10`` where ``roll`` is uniform on
    ``[1, max_gain]``. Each :class:`ProgressionMode` collapses that
    distribution to a single per-level number:

    * ``FIXED_MIN`` / ``FIXED_MAX`` — deterministic worst/best roll,
      so the displayed totals are the literal lower/upper bounds.
    * ``FIXED_AVG`` — the *true* per-level expectation
      ``E[(roll + base_gain) // 10]`` computed as a closed-form sum
      over the integer roll. The earlier median-roll shortcut
      collapsed onto MIN or MAX whenever the median floor-bucket
      coincided with one extreme (the case for most attacker stats,
      whose ``max_gain`` is small enough not to span a 10-boundary).

    ``target_level`` below ``base.level`` returns the base's stats
    untouched — the formula doesn't run backwards. The float
    accumulators are rounded to ``int`` at the end to match how
    :class:`BaseDataDigimon` stores them on disk.

    When ``apply_hp_buff`` is True and the base id resolves to a
    known stage, HP is multiplied by the stage's buff factor *after*
    the level walk (same order the randomizer applies it). MP and the
    attacker stats are never buffed.
    """
    type_row = LEVEL_UP_TABLE[base.digimon_type.value]
    working: Dict[str, float] = {
        name: float(getattr(base, name)) for name in PROGRESSION_STATS
    }
    start = max(int(base.level), 1)
    target = max(int(target_level), start)
    levels_walked = target - start
    for stat_ix, name in enumerate(PROGRESSION_STATS):
        max_gain, base_gain = type_row[stat_ix]
        per_level = _per_level_gain(mode, int(max_gain), int(base_gain))
        working[name] += per_level * levels_walked
    stage = stage_for_base_id(int(base.id))
    buffed = False
    if apply_hp_buff and stage:
        multiplier = HP_BUFF_BY_STAGE.get(stage, 1)
        if multiplier != 1:
            # Matches the randomizer's `math.floor(hp * mult)` after the
            # level walk; the int(round(...)) at the end handles the
            # final quantization back to the storage format.
            working["hp"] = working["hp"] * multiplier
            buffed = True
    return ExpectedStats(
        stats={name: int(round(v)) for name, v in working.items()},
        stage=stage,
        hp_buff_applied=buffed,
    )


def _per_level_gain(mode: ProgressionMode, max_gain: int, base_gain: int) -> float:
    """Per-level stat increment for the given mode.

    MIN/MAX return the deterministic floor-divided gain at the extreme
    rolls — the smallest/largest possible per-level increment.

    AVG returns the closed-form mean
    ``sum_{r=1..max_gain} ((r + base_gain) // 10) / max_gain``.
    Because the floor-by-10 quantizes the per-level gain into a small
    set of integers, the *integer* expectation differs from the
    increment you'd get by feeding the average roll into the formula
    — this method captures the actual mean instead of the rounded one.
    """
    if mode is ProgressionMode.FIXED_MIN:
        return float((1 + base_gain) // 10)
    if mode is ProgressionMode.FIXED_MAX:
        return float((max_gain + base_gain) // 10)
    total = sum((r + base_gain) // 10 for r in range(1, max_gain + 1))
    return total / max_gain


def expected_range(
    base,
    target_level: int,
    *,
    apply_hp_buff: bool = False,
) -> Dict[str, tuple]:
    """Convenience: per-stat (min, avg, max) tuple at ``target_level``.

    Used by the sidecar to show ``expected (min – max)`` next to each
    stat. The three modes share the same level walk shape, so this is
    a thin wrapper over three :func:`compute_expected_stats` calls.
    """
    mins = compute_expected_stats(
        base, target_level,
        mode=ProgressionMode.FIXED_MIN, apply_hp_buff=apply_hp_buff,
    ).stats
    avgs = compute_expected_stats(
        base, target_level,
        mode=ProgressionMode.FIXED_AVG, apply_hp_buff=apply_hp_buff,
    ).stats
    maxs = compute_expected_stats(
        base, target_level,
        mode=ProgressionMode.FIXED_MAX, apply_hp_buff=apply_hp_buff,
    ).stats
    return {
        name: (mins[name], avgs[name], maxs[name])
        for name in PROGRESSION_STATS
    }
