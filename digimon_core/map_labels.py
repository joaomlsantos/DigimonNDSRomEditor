"""Map-id → area-name labels for the field-map browser's Index tab.

Sourced from ``research_docs/claude_notes/_map_full/index.tsv``, itself
derived by joining overlay-5 zone-entry offsets with the ARM9 area-name
table at 0x0F0FD8..0x0F1716. The TSV is the recon artifact; this module
embeds the result so the editor doesn't need the research tree on disk.

The TSV also carries per-map event counts (DIALOG / OVERWORLD_SPRITE /
BATTLE / etc.) and zone-slot stats — kept out of the editor for now to
avoid bundling a snapshot that decays as the user edits. The label list
is the load-bearing piece for visualizing ``.d`` data against a known
area name; the event counts can be regenerated on demand from the live
overlay if needed later.
"""
from __future__ import annotations

from typing import List


# 265 entries — index = map id (0..264). Names mirror the in-game area
# strings rather than fan-localized variants so a cross-reference with
# the ROM's own label table stays direct.
AREA_NAMES: List[str] = [
    "SunshineCITY", "Shine Gate", "Shine Office", "Shine Hall", "Shine Plaza", "Shine Square", "Shine Center", "Shine Market",
    "Union Room", "Union Room", "Tamer Home", "Tamer Home", "Shine Terminal", "Shine Terminal", "Shine W Route", "Shine W Route",
    "Shine W Area", "Shine S Route", "Shine S Route", "Shine S Area", "Shine N Route", "Shine N Route", "Shine N Area", "Dark Gate",
    "Dark Office", "Dark Hall", "Dark Plaza", "Dark Square", "Dark Center", "Dark Market", "Union Room", "Union Room",
    "Tamer Home", "Tamer Home", "Dark Terminal", "Dark Terminal", "Dark E Route", "Dark E Route", "Dark E Area", "Dark S Route",
    "Dark S Route", "Dark S Area", "Dark N Route", "Dark N Route", "Dark N Area", "Center Bridge", "Center Bridge", "Coliseum Entry",
    "Coliseum Lobby", "NC Wait Room", "LF Wait Room", "SpectatorSeats", "Battle Stage", "Basic", "Marble", "Volcano",
    "Coral", "Grassland", "Tropical Zone", "Machine", "Desert", "Eerie", "Glass Castle", "Glass Coral",
    "Magma Coral", "Magma Castle", "Night Jungle", "Jungle Panel", "Desert Panel", "Night Desert", "Chip Forest", "Chip Forest",
    "Chip Forest", "Chip Forest", "Chip Forest", "Chip Forest", "Chip Forest", "Chip Forest", "Login Mountain", "Login Mountain",
    "Login Mountain", "Login Mountain", "Login Mountain", "Login Mountain", "Login Mountain", "Login Mountain", "Login Mountain", "Sunken Tunnel",
    "Sunken Tunnel", "Sunken Tunnel", "Sunken Tunnel", "Sunken Tunnel", "Sunken Tunnel", "Sunken Tunnel", "Sunken Tunnel", "ResistorJungle",
    "ResistorJungle", "ResistorJungle", "ResistorJungle", "ResistorJungle", "ResistorJungle", "ResistorJungle", "ResistorJungle", "Limit Valley",
    "Limit Valley", "Limit Valley", "Limit Valley", "Limit Valley", "Limit Valley", "Limit Valley", "Limit Valley", "Magnet Mine",
    "Magnet Mine", "Magnet Mine", "Magnet Mine", "Magnet Mine", "Magnet Mine", "Magnet Mine", "Magnet Mine", "Magnet Mine",
    "Magnet Mine", "Magnet Mine", "Loop Swamp", "Loop Swamp", "Loop Swamp", "Loop Swamp", "Loop Swamp", "Loop Swamp",
    "Loop Swamp", "Loop Swamp", "Loop Swamp", "PaletteAmazon", "PaletteAmazon", "PaletteAmazon", "PaletteAmazon", "PaletteAmazon",
    "PaletteAmazon", "PaletteAmazon", "PaletteAmazon", "PaletteAmazon", "PaletteAmazon", "Task Canyon", "Task Canyon", "Task Canyon",
    "Task Canyon", "Task Canyon", "Task Canyon", "Task Canyon", "Task Canyon", "Task Canyon", "Task Canyon", "Task Canyon",
    "ProcessFactory", "ProcessFactory", "ProcessFactory", "ProcessFactory", "ProcessFactory", "ProcessFactory", "ProcessFactory", "ProcessFactory",
    "ProcessFactory", "ProcessFactory", "ProcessFactory", "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier",
    "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier", "AccessGlacier", "Macro Sea",
    "Macro Sea", "Macro Sea", "Macro Sea", "Macro Sea", "Macro Sea", "Macro Sea", "Proxy Island", "Proxy Island",
    "Proxy Island", "Proxy Island", "Proxy Island", "Proxy Island", "Proxy Island", "Proxy Island", "Proxy Island", "Proxy Island",
    "Proxy Island", "Proxy Island", "Proxy Island", "HighlightHaven", "HighlightHaven", "HighlightHaven", "HighlightHaven", "HighlightHaven",
    "HighlightHaven", "HighlightHaven", "HighlightHaven", "HighlightHaven", "HighlightHaven", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss",
    "Shadow Abyss", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss", "Shadow Abyss",
    "Shadow Abyss", "Shadow Abyss", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain",
    "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain", "Chaos Brain",
    "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield",
    "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield", "Transfield",
    "Transfield", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins",
    "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins", "Thriller Ruins",
    "Thriller Ruins",
]


def area_name(map_id: int) -> str:
    """Return the area name for a map id, or ``"?"`` if out of range.

    Out-of-range is the natural state for any map id added past the
    vanilla 265 (no name has been authored yet); the caller renders the
    placeholder so the row is still visible in the index table.
    """
    if 0 <= map_id < len(AREA_NAMES):
        return AREA_NAMES[map_id]
    return "?"
