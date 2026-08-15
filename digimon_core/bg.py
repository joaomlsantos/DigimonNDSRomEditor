"""File-family helpers for menu backgrounds under ``DAT/bg/``.

Menu backgrounds are standard Nitro triples — an ``.NSCR`` tilemap, an
``.NCGR`` tile bank, and an ``.NCLR`` palette — named by role (``t_menubg01``,
``black``, ``bt_all_pn01`` …) rather than the numeric ids ``DAT/btmap/`` uses.
A record is driven off its **NSCR** (the actual background layout). The tile
bank and palette usually share the NSCR's stem, but not always: a couple of
tilemaps reuse another background's ``.NCGR``, and ~15% pull their palette
from a shared ``.NCLR`` elsewhere in the folder. So each record carries
*candidate* NCGR/NCLR lists — same-stem first — for the browser to default
from and expose in dropdowns.

Rendering reuses the btmap codec unchanged (``btmap.parse_nscr`` /
``render_single_layer`` / the ``sprite`` NCGR+NCLR parsers) — the on-disk
format is identical; only the file naming and (crucially) the compression
differ. Menu-background files are stored **uncompressed** in the FAT, so the
save path writes them back verbatim with no RLE-30 re-wrap — see
``RomSession._apply_bg_splice``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

BG_DIR = "DAT/bg/"

# A background's stem is often ``{FAMILY}_{NN}`` (or ``{FAMILY}{NN}``) — e.g.
# SP_NM_00..SP_NM_15 are one family. Within a family only some members ship
# their own ``.NCLR`` / ``.NCGR``; the rest share a sibling's (SP_NM_10 pulls
# its palette from SP_NM_00.NCLR). Stripping the trailing number yields the
# family key, used to *order* candidates (best-guess first) — never to hide
# the rest, since the real screen→palette association isn't recoverable from
# names alone (it lives in scattered UI-setup code).
_FAMILY_RE = re.compile(r"^(.*?)_?\d+$")

# Hand-verified screen→palette associations that naming can't recover (a screen
# borrowing an unrelated screen's palette). Keys + values are UPPERCASE stems
# with no extension and no ``_M`` — the ``_M`` variant is auto-preferred on
# Dusk when present. These only set the *default*; every palette stays
# selectable in the dropdown. Extend as more are confirmed.
_BG_PALETTE_HINTS = {
    "BT_LVUP_PN": "BT_EXP_PN01",
    "B_WIN02A": "B_WIN02",
    "MD_FLAME_N": "MD_FLAME01",
    "MTM_ST00D": "MTM_TOP",
}
# ``b_win02a`` → ``b_win02``: a trailing letter after the number is a variant
# that borrows the numbered base's palette.
_VARIANT_SUFFIX_RE = re.compile(r"^(.*\d)[A-Za-z]+$")


def _bg_paths(file_table) -> List[str]:
    prefix = BG_DIR.upper()
    return [p for p in file_table.paths() if p.upper().startswith(prefix)]


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _stem(path: str) -> str:
    return _basename(path).rsplit(".", 1)[0]


def _ext(path: str) -> str:
    name = _basename(path)
    return name.rsplit(".", 1)[-1].upper() if "." in name else ""


def _family(stem: str) -> str:
    m = _FAMILY_RE.match(stem.upper())
    return m.group(1) if m else stem.upper()


@dataclass(frozen=True)
class BgRecord:
    """One menu-background record, keyed by its NSCR tilemap.

    ``ncgrs`` / ``nclrs`` are candidate FAT paths with same-stem entries
    first, so the browser defaults to index 0 yet can still retarget a
    shared tile bank or compare the base vs ``_m`` palette. ``nclrs`` orders
    the same-stem base ``.NCLR`` ahead of the ``_m`` (main-screen) variant.
    """
    stem: str
    nscr: str
    ncgrs: Tuple[str, ...]
    nclrs: Tuple[str, ...]
    # How many leading entries of ncgrs/nclrs are this background's *own*
    # same-stem (and, for palettes, ``_m``) files vs the shared-pool fallback
    # that follows — lets the browser draw a separator between them.
    own_ncgr: int
    own_nclr: int

    @property
    def display_name(self) -> str:
        return self.stem.lower()


def _resolve_nclrs(stem, nclr_paths, prefer_m):
    """Order the palette candidates best-guess-first, then every remaining
    palette (nothing hidden). Returns ``(ordered_paths, n_guesses)`` — the
    first ``n_guesses`` are the confident matches (the combo separates them
    from the rest). The best guess becomes the default.
    """
    upper = stem.upper()
    by_stem = {}
    for p in nclr_paths:
        by_stem.setdefault(_stem(p).upper(), p)

    ordered: List[str] = []
    seen = set()

    def add(path):
        if path and path not in seen:
            ordered.append(path)
            seen.add(path)

    def add_base(base, *, prefer_m_first):
        """Add ``base`` and its ``_M`` variant; on Dusk the ``_M`` goes first."""
        m = by_stem.get(f"{base}_M")
        plain = by_stem.get(base)
        if prefer_m_first and prefer_m:
            add(m)
            add(plain)
        else:
            add(plain)
            add(m)

    # 1. Hand-verified association (Dusk → _M first).
    hint = _BG_PALETTE_HINTS.get(upper)
    if hint:
        add_base(hint, prefer_m_first=True)
    # 2. Same-stem — keep the plain palette as the default (unchanged for the
    #    common case), with its _M variant right after.
    add_base(upper, prefer_m_first=False)
    # 3. Family base / numeric variants (only reached when nothing above hit).
    fam = _family(stem)
    if fam != upper:
        for g in (f"{fam}_00", f"{fam}01", f"{fam}00", fam):
            add_base(g, prefer_m_first=True)
    vm = _VARIANT_SUFFIX_RE.match(upper)
    if vm:
        add_base(vm.group(1), prefer_m_first=True)

    n_guesses = len(ordered)
    for p in nclr_paths:  # everything else, still selectable
        add(p)
    return tuple(ordered), n_guesses


def _resolve_ncgrs(stem, ncgr_paths):
    """Tile-bank candidates: same-stem first, then family siblings, then every
    other bank (deprioritised but reachable)."""
    upper = stem.upper()
    fam = _family(stem)
    same = [p for p in ncgr_paths if _stem(p).upper() == upper]
    fam_sib = sorted(
        (p for p in ncgr_paths
         if _stem(p).upper() != upper and _family(_stem(p)) == fam),
        key=lambda p: _stem(p).upper(),
    )
    relevant = same + fam_sib
    seen = set(relevant)
    others = [p for p in ncgr_paths if p not in seen]
    return tuple(relevant + others), len(relevant)


def discover_bg_records(file_table, prefer_m: bool = False) -> List[BgRecord]:
    """Every ``DAT/bg/`` background present, sorted by stem.

    One record per ``.NSCR`` (the background layout). ``ncgrs`` / ``nclrs`` are
    ordered best-guess-first (same-stem, then a curated hint / family fallback
    for the screens that borrow another's palette), then every remaining file
    so a wrong default is always correctable. ``own_ncgr`` / ``own_nclr`` mark
    how many leading entries are confident matches (the combo draws a separator
    after them). ``prefer_m`` (set on Dusk) makes the ``_M`` variant the
    default where a hint/family match has one — see ``_BG_PALETTE_HINTS``.
    """
    paths = _bg_paths(file_table)
    nscrs = sorted(
        (p for p in paths if _ext(p) == "NSCR"),
        key=lambda p: _stem(p).upper(),
    )
    ncgr_paths = [p for p in paths if _ext(p) == "NCGR"]
    nclr_paths = [p for p in paths if _ext(p) == "NCLR"]

    records: List[BgRecord] = []
    for nscr in nscrs:
        stem = _stem(nscr)
        ncgrs, own_ncgr = _resolve_ncgrs(stem, ncgr_paths)
        nclrs, own_nclr = _resolve_nclrs(stem, nclr_paths, prefer_m)
        records.append(BgRecord(
            stem=stem, nscr=nscr, ncgrs=ncgrs, nclrs=nclrs,
            own_ncgr=own_ncgr, own_nclr=own_nclr,
        ))
    return records
