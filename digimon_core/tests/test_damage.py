"""Pin the damage engine to the dump-verified Lunamon/Kokuwamon battle."""
from digimon_core.damage import (
    Attacker, Defender, Move, DARK, WATER,
    base_damage, has_stab, _pre_variance, _assemble, damage_spread, hit_rate,
)

HYDRO = Move(power=25, element=WATER, crit_rate=10)
# Lunamon lv20, effective ATK99 SPI96, affinity Dark+Water (0x42).
LUNA = Attacker(level=20, atk=99, spi=96, affinity=(1 << DARK) | (1 << WATER))
# Kokuwamon lv13, DEF98 SPI82, water-res 1, 340 HP.
KOKU = Defender(level=13, dfn=98, spi=82,
                resist=[500, 200, 500, 500, 500, 900, 1, 1000], max_hp=340)


def test_base_damage_matches_dump():
    assert base_damage(HYDRO, LUNA, KOKU) == 60


def test_stab_read_from_affinity_bitmask():
    assert has_stab(LUNA, HYDRO) is True


def test_mean_roll_reproduces_102():
    dmg, stab, R = _pre_variance(HYDRO, LUNA, KOKU)
    assert stab and R == 1
    assert dmg == 102  # 60 -> x1.15 -> 69 -> water weakness -> 102
    assert _assemble(dmg, crit=False, variance_roll=5, flat_bonus=0) == 102


def test_spread_brackets_observed_103():
    s = damage_spread(HYDRO, LUNA, KOKU)
    assert s.base == 60 and s.stab
    assert (s.nocrit_min, s.nocrit_max) == (96, 107)
    assert s.nocrit_min <= 103 <= s.nocrit_max  # 103 observed in-game
    assert s.crit_min > s.nocrit_min and s.crit_max > s.nocrit_max


def test_crit_half_is_not_varied():
    # crit half = dmg//2 (unvaried); final = flat + half + varied
    dmg, _, _ = _pre_variance(HYDRO, LUNA, KOKU)
    assert _assemble(dmg, crit=True, variance_roll=5, flat_bonus=0) == dmg // 2 + dmg


def test_resistant_target_takes_less():
    tough = Defender(level=13, dfn=98, spi=82,
                     resist=[500] * 6 + [1000, 500], max_hp=340)  # water-resistant
    weak = damage_spread(HYDRO, LUNA, KOKU).nocrit_avg
    strong = damage_spread(HYDRO, LUNA, tough).nocrit_avg
    assert strong < weak


def test_hit_rate_bounded():
    assert 0 <= hit_rate(HYDRO, LUNA, KOKU) <= 100


def test_multi_hit_totals_are_per_hit_times_hits():
    single = damage_spread(HYDRO, LUNA, KOKU)
    triple = damage_spread(Move(power=25, element=WATER, crit_rate=10, hits=3), LUNA, KOKU)
    assert triple.hits == 3
    assert triple.nocrit_min == single.nocrit_min * 3
    assert triple.nocrit_max == single.nocrit_max * 3
    assert triple.crit_max == single.crit_max * 3
    assert single.hits == 1  # default
