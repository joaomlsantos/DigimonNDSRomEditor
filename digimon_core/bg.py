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

from dataclasses import dataclass
from typing import List, Tuple

BG_DIR = "DAT/bg/"


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


def discover_bg_records(file_table) -> List[BgRecord]:
    """Every ``DAT/bg/`` background present, sorted by stem.

    One record per ``.NSCR`` (the background layout). Same-stem ``.NCGR`` /
    ``.NCLR`` are placed first in the candidate lists; when a tilemap has no
    same-stem tile bank (a handful reuse another's), every other bank follows
    as a fallback so the user can retarget it in the browser.
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
        upper = stem.upper()

        same_ncgr = [p for p in ncgr_paths if _stem(p).upper() == upper]
        other_ncgr = [p for p in ncgr_paths if _stem(p).upper() != upper]
        ncgrs = tuple(same_ncgr + other_ncgr)

        base_nclr = [p for p in nclr_paths if _stem(p).upper() == upper]
        m_nclr = [p for p in nclr_paths if _stem(p).upper() == f"{upper}_M"]
        used = set(base_nclr) | set(m_nclr)
        other_nclr = [p for p in nclr_paths if p not in used]
        nclrs = tuple(base_nclr + m_nclr + other_nclr)

        records.append(BgRecord(
            stem=stem, nscr=nscr, ncgrs=ncgrs, nclrs=nclrs,
            own_ncgr=len(same_ncgr), own_nclr=len(base_nclr) + len(m_nclr),
        ))
    return records
