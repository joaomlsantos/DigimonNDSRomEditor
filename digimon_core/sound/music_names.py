"""Human-readable names for the vanilla DWDD BGM slots.

The overlay5 ``SET_MUSIC`` opcode (``0e 00 [music_id:u16]``) references a
BGM by its position in the SDAT's SSEQ array — the sequential array id,
NOT the trailing number in the ``bgmNN`` filename. In vanilla DWDD the
two happen to coincide (bgm00..bgm45 fill the array contiguously) so
either interpretation resolves to the same slot; for donor SDATs or
future edits that add / remove sequences this may not hold.

Names below come from ``research_docs/music_ids_formalized.txt`` — the
user-curated table pinned by cross-referencing each SET_MUSIC hit
against the map / cutscene the id fires under. Kept inline (rather than
loaded at runtime from research_docs) so the packaged editor doesn't
need the research tree on disk. Update in lock-step when the research
file gains ids.
"""
from __future__ import annotations

from typing import Dict, Optional


MUSIC_ID_NAMES: Dict[int, str] = {
    0x00: "Sunshine City",
    0x01: "Darkmoon City",
    0x02: "Shine/Dark Terminal",
    0x03: "Tamer Home",
    0x04: "Coliseum Theme",
    0x05: "Login Mountain",
    0x06: "Sunken Tunnel",
    0x07: "Loop Swamp",
    0x08: "Process Factory",
    0x09: "Access Glacier",
    0x0A: "Highlight Heaven",
    0x0B: "Proxy Island",
    0x0C: "Shadow Abyss",
    0x0D: "Limit Valley",
    0x0E: "Thriller Ruins",
    0x0F: "Chaos Brain",
    0x10: "Normal Battle Theme",
    0x11: "Alt Battle Theme (Sunken Tunnel)",
    0x12: "Chaos Brain Battle Theme",
    0x13: "Boss Battle Theme",
    0x14: "EXOGrimmon Battle Theme",
    0x15: "Multiplayer Theme (?)",
    0x16: "CITY Attacked Event Theme",
    0x17: "Event Theme — Apprehension",
    0x18: "Event Theme — Combined Power",
    0x19: "Game Over Theme",
    0x1A: "ChronoCore Theme",
    0x1B: "Victory Over EXOGrimmon Theme",
    0x1C: "Menu / DigiLab / DigiFarm PC",
    0x1D: "Start Menu Theme",
    0x1E: "Credits Theme",
    0x1F: "Digivolution Theme",
    0x20: "Battle Victory Jingle",
    0x21: "Digimon Matching (DigiEggs)",
    0x22: "Chill Unknown Theme",
    0x23: "World Map",
    0x24: "Farm Island Theme",
    0x25: "Angel BGM Board",
    0x26: "Dragon BGM Board",
    0x27: "Bird BGM Board",
    0x28: "Sea BGM Board",
    0x29: "Devil BGM Board",
    0x2A: "Dragon/Dark BGM Board",
    0x2B: "Mech BGM Board",
    0x2C: "Forest BGM Board",
    0x2D: "Kowloon Co. Theme",
}


def music_name(music_id: int) -> str:
    """Friendly name for ``music_id`` (the SET_MUSIC sequential array id).

    Falls back to ``bgmNN`` (matching the SDAT filename convention) for
    ids past the pinned table — a stable placeholder that the sound
    editor's Label field can override per-slot.
    """
    named = MUSIC_ID_NAMES.get(int(music_id) & 0xFFFF)
    if named is not None:
        return named
    return f"bgm{int(music_id) & 0xFFFF:02x}"


def default_music_label(music_id: int, override: Optional[str] = None) -> str:
    """Resolve the user-facing label for a BGM slot.

    ``override`` (typically ``session.bgm_label_edits.get(music_id)``)
    wins when non-empty so the sound editor's Label field is the
    authority; falls back to :data:`MUSIC_ID_NAMES` and finally to the
    ``bgmNN`` placeholder.
    """
    if override:
        return override
    return music_name(music_id)
