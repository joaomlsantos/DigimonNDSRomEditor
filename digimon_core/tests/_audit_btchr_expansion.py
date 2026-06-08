"""One-shot audit for BTCHR.PAK sprite-size expansion planning.

Walks all 415 Dusk (US) BTCHR groups and answers:
  1. Cross-cell tile range structure (disjoint partitions vs sharing).
  2. OAM count distribution (total per sprite, per cell).
  3. Frame bbox size distribution.
  4. Sanity: tiles referenced vs n_tiles.

Run with: python -m digimon_core.tests._audit_btchr_expansion
"""
import os
import sys
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import btchr, fnt, ncer as ncer_mod, pak  # noqa: E402

ROM_PATH = r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds"
BTCHR_PAK = "DAT/BTCHR.PAK"
CHRSIZE = "DAT/BTCHR/CHRSIZE.BIN"

SENTINEL_GROUPS = {400, 401}


def pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def union_range(ranges):
    """Given [(start,end), ...] return sorted disjoint merged list."""
    if not ranges:
        return []
    rs = sorted(ranges)
    merged = [list(rs[0])]
    for s, e in rs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def overlap(a, b):
    """Do two lists of (s,e) ranges overlap?"""
    for s1, e1 in a:
        for s2, e2 in b:
            if s1 < e2 and s2 < e1:
                return True
    return False


def main():
    rom_bytes = open(ROM_PATH, "rb").read()
    ft = fnt.FileTable.from_rom(rom_bytes)

    def _resolve(name):
        s, e = ft.resolve(name)
        return rom_bytes[s:e]

    pak_file = pak.PakFile(_resolve(BTCHR_PAK))
    chrsize = btchr.parse_chrsize(_resolve(CHRSIZE))
    n_groups = btchr.parse_pak_groups(pak_file)
    assert n_groups == 415

    # Per-digimon summary records
    records = []
    cat_counts = {
        "disjoint_equal_partition": 0,
        "disjoint_nonequal": 0,
        "shared_full_overlap": 0,   # every cell uses identical union
        "shared_partial": 0,
        "single_cell_only": 0,
    }
    cat_examples = {k: [] for k in cat_counts}

    same_bbox_count = 0
    diff_bbox_count = 0
    referenced_eq_ntiles = 0
    referenced_lt_ntiles = 0
    referenced_gt_ntiles = 0  # impossible normally
    trailing_unused_examples = []

    all_total_oams = []
    all_per_cell_oams = []
    all_max_w = []
    all_max_h = []

    skipped = []
    failures = []

    for gi in range(n_groups):
        if gi in SENTINEL_GROUPS:
            skipped.append(gi)
            continue
        try:
            digi_id, tpf = chrsize[gi]
            d = btchr.decode_digimon(pak_file, gi, digi_id)
            n_tiles = d.n_tiles
            cells = d.ncer.cells

            # Per-cell tile ranges (8bpp)
            per_cell_ranges = ncer_mod.cell_tile_ranges(d.ncer, bpp4=False)
            cell_unions = [union_range(r) for r in per_cell_ranges]

            # OAM counts
            per_cell_oams = [len(c.oams) for c in cells]
            total_oams = sum(per_cell_oams)
            all_total_oams.append((gi, total_oams, n_tiles))
            all_per_cell_oams.extend(per_cell_oams)

            # Bbox per cell
            bboxes = [btchr.cell_bbox(c) for c in cells]
            whs = [(b[2] - b[0], b[3] - b[1]) for b in bboxes]
            max_w = max(w for w, _ in whs)
            max_h = max(h for _, h in whs)
            all_max_w.append(max_w)
            all_max_h.append(max_h)
            if len(set(whs)) == 1:
                same_bbox_count += 1
            else:
                diff_bbox_count += 1

            # Classify cross-cell tile structure
            non_empty_unions = [u for u in cell_unions if u]
            if len(non_empty_unions) <= 1:
                cat = "single_cell_only"
            else:
                # Pairwise overlap test
                has_overlap = False
                for i in range(len(non_empty_unions)):
                    for j in range(i + 1, len(non_empty_unions)):
                        if overlap(non_empty_unions[i], non_empty_unions[j]):
                            has_overlap = True
                            break
                    if has_overlap:
                        break

                if has_overlap:
                    if all(u == non_empty_unions[0] for u in non_empty_unions):
                        cat = "shared_full_overlap"
                    else:
                        cat = "shared_partial"
                else:
                    # Disjoint — check whether each cell sits in its own
                    # tpf-sized slot [k*tpf, (k+1)*tpf). Allow the cell's
                    # referenced tiles to be a SUBSET of the slot (some
                    # vanilla digimon leave a trailing unreferenced tile).
                    is_partition = True
                    used_slots = []
                    for u in cell_unions:
                        if not u:
                            # Empty cell — would be flagged as such but
                            # we've already handled single-cell-only case.
                            is_partition = False
                            break
                        umin = min(s for s, _ in u)
                        umax = max(e for _, e in u)
                        slot_k = umin // tpf
                        if (slot_k * tpf <= umin and
                                umax <= (slot_k + 1) * tpf):
                            used_slots.append(slot_k)
                        else:
                            is_partition = False
                            break
                    if is_partition and sorted(used_slots) == list(range(len(cells))):
                        cat = "disjoint_equal_partition"
                    else:
                        cat = "disjoint_nonequal"

            cat_counts[cat] += 1
            if len(cat_examples[cat]) < 5:
                cat_examples[cat].append((gi, digi_id, tpf, n_tiles))

            # Referenced tiles sanity
            all_refs = []
            for r in per_cell_ranges:
                all_refs.extend(r)
            ref_union = union_range(all_refs)
            max_ref = max((e for _, e in ref_union), default=0)
            if max_ref == n_tiles:
                referenced_eq_ntiles += 1
            elif max_ref < n_tiles:
                referenced_lt_ntiles += 1
                if len(trailing_unused_examples) < 10:
                    trailing_unused_examples.append(
                        (gi, digi_id, max_ref, n_tiles, n_tiles - max_ref)
                    )
            else:
                referenced_gt_ntiles += 1

            records.append({
                "gi": gi, "digi_id": digi_id, "tpf": tpf, "n_tiles": n_tiles,
                "total_oams": total_oams, "max_w": max_w, "max_h": max_h,
                "cat": cat,
            })
        except Exception as e:
            failures.append((gi, repr(e)))

    # Sentinels: also report their stats briefly
    sentinel_info = []
    for gi in sorted(SENTINEL_GROUPS):
        try:
            digi_id, tpf = chrsize[gi]
            d = btchr.decode_digimon(pak_file, gi, digi_id)
            n_oams = sum(len(c.oams) for c in d.ncer.cells)
            sentinel_info.append((gi, digi_id, tpf, d.n_tiles, n_oams, len(d.ncer.cells)))
        except Exception as e:
            sentinel_info.append((gi, "ERR", repr(e)))

    # ---- print report ----
    print("=" * 72)
    print("BTCHR.PAK audit — Digimon World Dusk (US)")
    print(f"Groups processed: {len(records)}  (skipped sentinels: {sorted(SENTINEL_GROUPS)})")
    print(f"Decode failures: {len(failures)}")
    if failures:
        for f in failures[:5]:
            print(f"  {f}")
    print()

    # Q1: cross-cell tile structure
    print("Q1: Cross-cell tile range structure")
    for k, v in cat_counts.items():
        print(f"  {k:30s} : {v}")
        for ex in cat_examples[k]:
            print(f"      e.g. gi={ex[0]} digi_id={ex[1]} tpf={ex[2]} n_tiles={ex[3]}")
    print()

    # Q2: OAM distribution
    totals = [t for _, t, _ in all_total_oams]
    print("Q2: OAM counts")
    print(f"  total/sprite (5 cells): min={min(totals)} median={median(totals):.0f} "
          f"max={max(totals)} p95={pct(totals, 95)}")
    print(f"  per-cell:               min={min(all_per_cell_oams)} "
          f"median={median(all_per_cell_oams):.0f} max={max(all_per_cell_oams)} "
          f"p95={pct(all_per_cell_oams, 95)}")
    top5 = sorted(all_total_oams, key=lambda x: -x[1])[:5]
    print("  Top 5 sprites by total OAM count:")
    for gi, tot, nt in top5:
        digi_id = chrsize[gi][0]
        print(f"    gi={gi} digi_id={digi_id} total_oams={tot} n_tiles={nt}")
    print()

    # Q3: bbox distribution
    print("Q3: Max-frame bbox per digimon (over 5 cells)")
    print(f"  width:  min={min(all_max_w)} median={median(all_max_w):.0f} "
          f"max={max(all_max_w)} p95={pct(all_max_w, 95)}")
    print(f"  height: min={min(all_max_h)} median={median(all_max_h):.0f} "
          f"max={max(all_max_h)} p95={pct(all_max_h, 95)}")
    print(f"  cells all identical bbox: {same_bbox_count}")
    print(f"  cells differ in bbox:     {diff_bbox_count}")
    print()

    # Q4: sanity
    print("Q4: Tile-reference sanity")
    print(f"  max_ref == n_tiles : {referenced_eq_ntiles}")
    print(f"  max_ref <  n_tiles : {referenced_lt_ntiles}  (trailing unused tiles)")
    print(f"  max_ref >  n_tiles : {referenced_gt_ntiles}  (WOULD BE A BUG)")
    if trailing_unused_examples:
        print("  Examples of trailing-unused-tiles:")
        for gi, did, mr, nt, extra in trailing_unused_examples:
            print(f"    gi={gi} digi_id={did} max_ref={mr} n_tiles={nt} unused={extra}")
    print()

    print("Sentinel groups (400, 401) raw stats:")
    for s in sentinel_info:
        print(f"  {s}")
    print()

    # extra detail: distribution of tpf
    tpfs = sorted(set(r["tpf"] for r in records))
    print(f"tpf set: {tpfs}")
    from collections import Counter
    tpf_counts = Counter(r["tpf"] for r in records)
    print(f"tpf counts: {dict(sorted(tpf_counts.items()))}")
    bbox_counts = Counter((r["max_w"], r["max_h"]) for r in records)
    print(f"top 10 max-bbox (w,h) values:")
    for (w, h), c in bbox_counts.most_common(10):
        print(f"  ({w},{h}) : {c}")


if __name__ == "__main__":
    main()
