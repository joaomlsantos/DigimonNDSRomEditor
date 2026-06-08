"""Render each cell of probed ROM via existing renderer — confirms my
on-disk NCGR/NCER layout is internally consistent."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from PySide6.QtGui import QImage  # type: ignore

from digimon_core import btchr, fnt, pak


def main(rom_path: str, group: int = 2, out_prefix: str = "probed_render"):
    rom = Path(rom_path).read_bytes()
    ft = fnt.FileTable.from_rom(rom)
    s, e = ft.resolve("DAT/BTCHR.PAK")
    pak_obj = pak.PakFile(rom[s:e])
    cs, ce = ft.resolve("DAT/BTCHR/CHRSIZE.BIN")
    digimon_id, tpf = btchr.parse_chrsize(rom[cs:ce])[group]

    d = btchr.decode_digimon(pak_obj, group, digimon_id=digimon_id)
    print(f"group={group} digimon_id={digimon_id} tpf={tpf} n_tiles={d.n_tiles}")

    for ci, cell in enumerate(d.ncer.cells):
        rgba, w, h = btchr.render_cell_rgba(
            cell, d.tile_bytes, d.palette, pad=0,
        )
        # Wrap into QImage and save PNG
        img = QImage(rgba, w, h, QImage.Format.Format_RGBA8888).copy()
        out = f"{out_prefix}_g{group}_cell{ci}.png"
        img.save(out)
        oam_summary = [f"slot={o.tile}@({o.x},{o.y}) {o.w}x{o.h}" for o in cell.oams]
        print(f"  cell {ci}: {w}x{h} OAMs={oam_summary} -> {out}")


if __name__ == "__main__":
    rom = sys.argv[1] if len(sys.argv) > 1 else r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"
    grp = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    prefix = sys.argv[3] if len(sys.argv) > 3 else "probed_render"
    main(rom, grp, prefix)
