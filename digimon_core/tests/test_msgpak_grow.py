"""End-to-end tests for the MSG.PAK grow + trailing-FF trim save path.

Covers PLAN.md §12 Phase B (grow MSG.PAK strings past their byte budget,
re-splice via fat.resize_fat_entry) and Phase C (trim 0xFF tail past
header[0x80]). The session API (``serialize_all_with_qol``) is the public
surface; tests assert downstream file content stays intact through the
shift and the grown strings reparse back to their new text on a fresh
load of the saved ROM bytes.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from digimon_core import fnt, loaders, rom  # noqa: E402
from editor.session import RomSession  # noqa: E402


ROM_PATHS = {
    "DUSK_US": r"C:\Workspace\digimon_stuffs\rom_files\1420 - Digimon World - Dusk (US).nds",
    "DAWN_US": r"C:\Workspace\digimon_stuffs\rom_files\1421 - Digimon World - Dawn (USA).nds",
}


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _fat_entry(buf: bytes, idx: int):
    fat_off = _u32(buf, 0x48)
    return struct.unpack_from("<II", buf, fat_off + idx * 8)


def _fat_count(buf: bytes) -> int:
    return _u32(buf, 0x4C) // 8


class _MsgpakGrowBase:
    VERSION: str = ""

    @classmethod
    def setUpClass(cls):
        path = ROM_PATHS[cls.VERSION]
        if not os.path.exists(path):
            raise unittest.SkipTest(f"ROM not found: {path}")
        cls.path = path

    def _session(self) -> RomSession:
        return RomSession.from_file(self.path)

    # -- Phase C: trailing 0xFF trim + vanilla 64 MiB cart size --------

    def test_vanilla_save_keeps_64mib_cart_size(self):
        sess = self._session()
        out = sess.serialize_all_with_qol()
        used = _u32(out, 0x80)
        # ROM length stays at the vanilla 64 MiB cart unless content
        # genuinely overflows past it (it never does for a no-edit save).
        self.assertEqual(len(out), 0x4000000,
                         "no-edit save changed cart size off 0x4000000")
        # max(FAT.end) must equal header[0x80] (invariant from fat.py).
        n = _fat_count(out)
        max_end = max(_fat_entry(out, i)[1] for i in range(n))
        self.assertEqual(used, max_end)
        # Padding past used must be 0xFF.
        self.assertTrue(all(b == 0xFF for b in out[used:]),
                        "tail padding is not all 0xFF")

    def test_vanilla_save_preserves_fat_table(self):
        sess = self._session()
        out = sess.serialize_all_with_qol()
        van = bytes(sess.original_rom_data)
        n = _fat_count(out)
        # Every FAT entry's (start, end) must match vanilla exactly — no
        # spurious shifts on a no-edit save.
        for i in range(n):
            self.assertEqual(_fat_entry(out, i), _fat_entry(van, i),
                             f"FAT[{i}] shifted on no-edit save")

    # -- Phase B: grow one MSG.PAK string ------------------------------

    def test_grow_single_string_shifts_downstream(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        # Pick a printable string with no markers — easy to triple.
        victim = next(
            s for s in ms
            if 5 < len(s.text) < 30 and "[" not in s.text and "]" not in s.text
        )
        original_offset = victim.offset
        original_budget = victim.original_byte_length
        new_text = victim.text * 3
        victim.text = new_text
        self.assertFalse(victim.fits())

        out = sess.serialize_all_with_qol()
        van = bytes(sess.original_rom_data)

        # MSG.PAK grew → downstream files shifted by aligned step.
        ft_old = fnt.FileTable.from_rom(van)
        ft_new = fnt.FileTable.from_rom(bytes(out))
        old_ms, old_me = ft_old.resolve("DAT/MSG.PAK")
        new_ms, new_me = ft_new.resolve("DAT/MSG.PAK")
        self.assertEqual(new_ms, old_ms, "MSG.PAK start moved")
        self.assertGreater(new_me, old_me, "MSG.PAK didn't grow")
        # MSG.PAK's stored FAT size grows by raw content delta (not aligned).
        # Downstream files shift by aligned_shift = ceil(delta / 0x200) * 0x200.
        content_delta = new_me - old_me
        downstream_shift = ((content_delta + 0x1FF) // 0x200) * 0x200

        # Pick a file downstream of MSG.PAK and check it shifted by
        # downstream_shift, with content preserved.
        n = _fat_count(out)
        downstream_idx = None
        for i in range(n):
            s, _ = _fat_entry(van, i)
            if s > old_me:
                downstream_idx = i
                break
        self.assertIsNotNone(downstream_idx)
        v_s, v_e = _fat_entry(van, downstream_idx)
        n_s, n_e = _fat_entry(out, downstream_idx)
        self.assertEqual(n_s - v_s, downstream_shift,
                         f"downstream FAT[{downstream_idx}] shift mismatch")
        self.assertEqual(n_e - v_e, downstream_shift)
        self.assertEqual(van[v_s:v_e], bytes(out[n_s:n_e]),
                         "downstream file content corrupted by shift")

        # The grown string reparses at its (preserved) original offset.
        reparsed = loaders.loadStringRegion(self.VERSION, out, "msgpak_all")
        match = next((s for s in reparsed if s.offset == original_offset), None)
        self.assertIsNotNone(match,
                             f"grown string not found at 0x{original_offset:x}")
        self.assertEqual(match.text, new_text)
        # And its new budget is at least the encoded length.
        self.assertGreaterEqual(match.original_byte_length, original_budget)

    def test_grow_multiple_entries_independent(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        # Pick three growable strings spaced far apart so they likely land
        # in different pak entries (entries span ~few-KB chunks).
        candidates = [
            s for s in ms
            if 5 < len(s.text) < 30 and "[" not in s.text and "]" not in s.text
        ]
        victims = candidates[::max(1, len(candidates) // 4)][:3]
        self.assertEqual(len(victims), 3)
        grown_texts = []
        for v in victims:
            v.text = v.text * 3
            grown_texts.append(v.text)

        out = sess.serialize_all_with_qol()

        # Each grown text appears in the reparsed string set. (We don't pin
        # to original offsets: when entry K grows by D, all later entries
        # — and the strings in them — shift by D within MSG.PAK, so a
        # downstream-victim string's ROM offset will have moved.)
        reparsed = loaders.loadStringRegion(self.VERSION, out, "msgpak_all")
        reparsed_texts = {s.text for s in reparsed}
        for t in grown_texts:
            self.assertIn(t, reparsed_texts, "grown text not in reparsed set")

    # -- over_budget_strings gating ------------------------------------

    def test_over_budget_excludes_msgpak(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        victim = next(s for s in ms if 5 < len(s.text) < 30)
        victim.text = victim.text * 5
        # MSG.PAK over-budget strings no longer block the save gate.
        self.assertEqual(sess.over_budget_strings(), [])

    # -- 1024-byte per-string cap --------------------------------------

    def test_per_string_cap_clean_when_unedited(self):
        from digimon_core import model
        sess = self._session()
        # CAP is the empirical 1024-byte engine limit.
        self.assertEqual(model.MSGPAK_STRING_CAP, 1024)
        # Vanilla MSG.PAK never ships a string past the 1024-byte cap.
        self.assertEqual(sess.over_cap_msgpak_strings(), [])

    def test_grow_past_1024_flags_string(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        victim = next(iter(ms))
        # 2 KiB filler — guaranteed past the 1024-byte cap.
        victim.text = "A" * 2048
        flagged = sess.over_cap_msgpak_strings()
        self.assertEqual(len(flagged), 1, "expected exactly one over-cap string")
        self.assertIs(flagged[0], victim)

    def test_modest_grow_stays_under_cap(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        # Tiny edit — must still fit cleanly.
        victim = next(s for s in ms if 5 < len(s.text) < 40)
        victim.text = victim.text + "!"
        self.assertEqual(sess.over_cap_msgpak_strings(), [])

    # -- 0x4000000 ROM-size invariant ----------------------------------

    def test_in_budget_msgpak_grow_keeps_64mib_size(self):
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        # Modest in-cap grow — total ROM stays at the vanilla 64 MiB cart.
        victim = next(s for s in ms if 5 < len(s.text) < 40)
        victim.text = victim.text + "!!!"
        out = sess.serialize_all_with_qol()
        used = _u32(out, 0x80)
        self.assertEqual(len(out), 0x4000000,
                         "in-cap grow shouldn't push file past 64 MiB")
        self.assertLessEqual(used, 0x4000000)
        # Tail past used must still be 0xFF padding.
        self.assertTrue(all(b == 0xFF for b in out[used:]))

    def test_genuine_overflow_lets_rom_exceed_64mib(self):
        """If content genuinely fills past 0x4000000 the file is allowed
        to grow — the 64 MiB target is a floor, not a ceiling."""
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        van_used = _u32(bytes(sess.original_rom_data), 0x80)
        slack = 0x4000000 - van_used  # vanilla 0xFF padding between max(FAT.end) and 64 MiB
        # Grow many strings just under the 1024-byte cap until cumulative
        # delta crosses the slack + alignment overhead.
        target_grow = slack + 0x4000
        grown_delta = 0
        for s in ms:
            if grown_delta > target_grow:
                break
            cur_size = len(s.encoded_bytes_for_grow())
            headroom = 1024 - cur_size
            if headroom < 50:
                continue
            # Bytes per added char: ASCII letter = 2 bytes encoded.
            add_chars = (headroom - 8) // 2
            s.text = s.text + ("A" * add_chars)
            grown_delta += len(s.encoded_bytes_for_grow()) - cur_size
        # Sanity: nothing crossed the cap.
        self.assertEqual(sess.over_cap_msgpak_strings(), [])
        out = sess.serialize_all_with_qol()
        used = _u32(out, 0x80)
        if used > 0x4000000:
            self.assertEqual(len(out), used,
                             "overflow case: file should match header[0x80] exactly")
        else:
            # Setup didn't manage to overflow — confirm the 64 MiB floor holds.
            self.assertEqual(len(out), 0x4000000)

    # -- Shrink: no NUL-pad leak into next string in group -------------

    def test_shrink_does_not_leak_space_padding_into_next_string(self):
        """Regression for the `00 00`/`@SP@` padding leak: when a MSG.PAK
        string was shrunk, the in-place writer used to pad the remaining
        budget with `\x00`, and within a group (no per-string offset)
        the engine read that padding as leading spaces on the next string.
        The fix routes every edited MSG.PAK string through the entry
        rebuild, which uses ``encoded_bytes_for_grow`` (no padding,
        original terminator) so the next string's bytes stay flush.
        """
        sess = self._session()
        ms = sess.string_regions["msgpak_all"]
        # Find a string with a successor in the same group (i.e. a string
        # whose original terminator is FE FF — the per-string [END] marker
        # within a group). Shrink it and make sure the successor's text
        # doesn't gain a leading "@SP@" run.
        from digimon_core import strings as core_strings  # local: test-only
        candidates = [
            (i, s) for i, s in enumerate(ms)
            if (s.original_terminator == core_strings.END_MARKER
                and 6 < len(s.text) < 30
                and "[" not in s.text and "]" not in s.text)
        ]
        # Pick one whose immediate successor in the list is in the same
        # group (i.e. starts right after this one in the same MSG.PAK
        # entry). Vanilla offsets are sequential per group; same group =
        # successor.offset - this.offset == this.original_byte_length.
        idx, victim = next(
            (i, s) for i, s in candidates
            if i + 1 < len(ms)
            and ms[i + 1].offset == s.offset + s.original_byte_length
        )
        successor = ms[idx + 1]
        successor_text_before = successor.text

        # Shrink: drop ~4 chars from the end. Stay well in-budget so we
        # would have hit the in-place padded-write path before the fix.
        victim.text = victim.text[:-4]
        self.assertTrue(victim.fits(), "test setup must stay in budget")

        out = sess.serialize_all_with_qol()
        reparsed = loaders.loadStringRegion(self.VERSION, out, "msgpak_all")
        # The successor sits at a (possibly shifted) offset post-resize;
        # match it by its vanilla offset, then verify text is intact.
        # Because the only edit is a shrink in the same entry, downstream
        # files shift by a non-positive aligned step (often 0 — pak
        # shrinks under 0x200 don't shift downstream), but the successor
        # itself stays at its original offset since its group is rebuilt
        # in place with the shrunk predecessor.
        # We don't pin to a specific offset — search by content vicinity.
        # Strongest invariant: successor text must NOT have gained leading
        # spaces from `\x00` padding.
        match = next(
            (s for s in reparsed if s.text.lstrip() == successor_text_before.lstrip()),
            None,
        )
        self.assertIsNotNone(match, "successor string not found in reparsed set")
        self.assertEqual(
            match.text, successor_text_before,
            "successor gained leading characters — NUL padding leaked",
        )
        self.assertFalse(
            match.text.startswith(" "),
            "successor starts with space — `00 00` padding decoded as `@SP@`",
        )


class DuskUsMsgpakGrow(_MsgpakGrowBase, unittest.TestCase):
    VERSION = "DUSK_US"


class DawnUsMsgpakGrow(_MsgpakGrowBase, unittest.TestCase):
    VERSION = "DAWN_US"


if __name__ == "__main__":
    unittest.main(verbosity=2)
