"""DWDD (Dusk US) battle damage engine — port of ov2 ``BattleApplyMoveDamage``
(``FUN_0017cdbc``).

Verified against a live battle RAM dump: Lunamon Hydro Water vs Kokuwamon =
**103 observed**, 102 at the mean variance roll. Full derivation and the
decompile mapping live in ``research_docs/claude_notes/ov2_damage_formula.md``.

Pure Python / no Qt, so the calculator UI and the tests share one engine. All
``/`` follow the ARM reciprocal-multiplies (truncate toward zero); the A/B terms
are single-precision (soft-float), reproduced with :func:`f32`.

The hit-rate half (:func:`hit_rate`) is decompile-DERIVED and NOT dump-verified —
treat it as an estimate.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List

# Element indices — the base-digimon resistance / affinity order.
LIGHT, DARK, FIRE, EARTH, WIND, STEEL, WATER, THUNDER = range(8)
ELEMENT_NAMES = ["Light", "Dark", "Fire", "Earth", "Wind", "Steel", "Water", "Thunder"]

NEUTRAL_RESIST = 500
DAMAGE_CAP = 9999            # 0x270F
VARIANCE_ROLLS = range(11)   # rand(0..10) -> multiplier 95..105


def f32(x: float) -> float:
    """Round to IEEE-754 single precision (the A/B terms are soft-float)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def tdiv(a: int, b: int) -> int:
    """Truncate-toward-zero integer division (the ARM /N reciprocal-multiplies)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def resist_multiplier(r: int) -> float:
    """Damage multiplier from a resistance value: `(1500 - R)/1000`, floored at 0.

    500 = neutral (×1.00), 1000 = ×0.50 (half damage), 0 = ×1.50 (weak). Values
    above 1500 would go negative (the engine floors damage to 1), shown as ×0.00.
    """
    return max(0.0, (1500 - r) / 1000.0)


@dataclass
class Move:
    power: int                 # +0x08 primary_value
    element: int               # +0x04  (0..7)
    crit_rate: int = 0         # +0x16
    accuracy: int = 100        # +0x15  (hit gate only)
    hits: int = 1              # +0x12 num_hits — the move lands this many times (Gatling etc.)


@dataclass
class Attacker:
    level: int = 1
    atk: int = 0               # EFFECTIVE stats (base+buff+debuff; include trait/support mods)
    spi: int = 0
    affinity: int = 0          # element-affinity bitmask (bit per element) — drives STAB
    crit_mod: int = 0          # +0x0f
    flat_bonus: int = 0        # +0x10
    mode_bonus: bool = False   # special battle mode multiplies A by 1.2
    hit_stat: int = 0          # +0x80 + +0x82 (hit-rate only; unverified)


@dataclass
class Defender:
    level: int = 1
    dfn: int = 0               # EFFECTIVE stats
    spi: int = 0
    resist: List[int] = field(default_factory=lambda: [NEUTRAL_RESIST] * 8)
    max_hp: int = 1
    evade_stat: int = 0        # +0x86 + +0x88 (hit-rate only; unverified)


def has_stab(a: Attacker, m: Move) -> bool:
    return bool((a.affinity >> m.element) & 1)


def base_damage(m: Move, a: Attacker, d: Defender) -> int:
    """``Base = trunc(A - B)`` — the single-precision core (decompile order)."""
    A = f32(f32(a.atk) + f32(0.75) * f32(a.spi))          # ATK + 0.75*SPI
    if a.mode_bonus:
        A = f32(A * f32(1.2))                             # special-mode x1.2
    A = f32(A * f32(1.0 + f32(a.level) / f32(400.0)))     # *(1 + Lv/400)
    A = f32(A / f32(4.0))                                 # / 4
    A = f32(A * f32(2.0 + f32(m.power) / f32(14.0)))      # *(2 + Power/14)
    B = f32(f32(d.dfn) + f32(d.spi) / f32(2.0))           # DEF + SPI/2
    B = f32(B * f32(f32(m.power) / f32(64.0) + f32(0.4)))  # *(Power/64 + 0.4)
    return int(f32(A - B))


def _pre_variance(m: Move, a: Attacker, d: Defender, *, trait_def: int = 0):
    """Damage after STAB + element resistance + trait reduction, before crit/variance.

    Returns ``(dmg, stab, R)`` where ``dmg`` is the ``iVar43`` fed into the
    crit/variance assembly and ``R`` is the target's resistance to the element.
    """
    dmg = base_damage(m, a, d)
    stab = has_stab(a, m)
    if stab:
        dmg = tdiv(dmg * 115, 100)                        # x1.15
    R = d.resist[m.element]
    dmg = dmg - tdiv(dmg * tdiv((R - 500) * 100, 1000), 100)
    if dmg < 0:                                           # engine floors negatives to 1 here
        dmg = 1
    dmg -= trait_def
    return dmg, stab, R


def _assemble(dmg: int, *, crit: bool, variance_roll: int, flat_bonus: int) -> int:
    """``final = clamp(flat_bonus + (dmg//2 if crit) + dmg*(roll+95)/100, 1, 9999)``.

    The crit half is added to the flat accumulator and is NOT scaled by variance
    (matches the decompile: only the base is varied, then the crit half + flat
    bonus are added on).
    """
    bonus = flat_bonus + (tdiv(dmg, 2) if crit else 0)
    varied = tdiv(dmg * (variance_roll + 95), 100)
    return max(1, min(DAMAGE_CAP, bonus + varied))


@dataclass
class DamageSpread:
    base: int                  # trunc(A - B) before STAB/resist (per hit)
    stab: bool
    resist_mult: float         # approx (1500 - R)/1000
    crit_rate: int             # % chance of a crit (rand(100) < this), per hit
    hits: int                  # move lands this many times; the ranges below are TOTALs
    nocrit_min: int
    nocrit_avg: float
    nocrit_max: int
    crit_min: int
    crit_max: int
    max_hp: int

    def pct(self, dmg: float) -> float:
        return 100.0 * dmg / self.max_hp if self.max_hp else 0.0

    def hits_to_ko(self, dmg: float) -> int:
        if dmg <= 0:
            return 0
        return -(-self.max_hp // int(dmg)) if int(dmg) else 0


def damage_spread(m: Move, a: Attacker, d: Defender, *, trait_def: int = 0) -> DamageSpread:
    """Full non-crit + crit damage ranges over the 11 variance rolls (95..105%)."""
    base = base_damage(m, a, d)
    dmg, stab, R = _pre_variance(m, a, d, trait_def=trait_def)
    # Each hit is an independent full damage roll (the engine calls resolve-damage
    # once per hit — no hit loop inside it), so the total is the per-hit result ×hits.
    # Each hit is capped to [1, 9999] individually, then summed, so totals may exceed 9999.
    h = max(1, m.hits)
    nocrit = [h * _assemble(dmg, crit=False, variance_roll=v, flat_bonus=a.flat_bonus) for v in VARIANCE_ROLLS]
    crit = [h * _assemble(dmg, crit=True, variance_roll=v, flat_bonus=a.flat_bonus) for v in VARIANCE_ROLLS]
    crit_rate = max(0, a.crit_mod + m.crit_rate + (5 if stab else 0))
    return DamageSpread(
        base=base, stab=stab, resist_mult=(1500 - R) / 1000.0, crit_rate=crit_rate, hits=h,
        nocrit_min=min(nocrit), nocrit_avg=sum(nocrit) / len(nocrit), nocrit_max=max(nocrit),
        crit_min=min(crit), crit_max=max(crit), max_hp=d.max_hp,
    )


def hit_rate(m: Move, a: Attacker, d: Defender) -> int:
    """Hit chance %, 0..100. DERIVED FROM DECOMPILE, NOT dump-verified — an estimate.

    ``hit = (accuracy + hit_stat)*10 + Lv*2) * (1000 - (evade_stat*10 + Lv*2)) / 1000``
    (+20 if STAB), then the engine rolls ``rand(1000) < hit``. The level term reads
    the *attacker's* level on both sides in the decompile (the evade-side read is
    suspect — see the RE note).
    """
    acc = (m.accuracy + a.hit_stat) * 10 + a.level * 2
    eva = d.evade_stat * 10 + a.level * 2
    hit = tdiv(acc * (1000 - eva), 1000)
    if has_stab(a, m):
        hit += 20
    return max(0, min(100, tdiv(hit, 10)))
