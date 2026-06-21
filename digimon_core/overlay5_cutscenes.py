"""In-memory cutscene index over an :class:`overlay5.Overlay5Index`.

This is the runtime counterpart of the offline research dump under
``research_docs/claude_notes/_overlay5_cutscenes/`` (built by
``compile_cutscenes.py`` + ``build_map_chain_index.py``). Same chain-
walking semantics, same trigger classification, same two-pass external-
label seeding — but no text emission, no filesystem detour. The editor
consumes the result through O(1) ``chains_by_map[map_id]`` /
``regions[(entry_ix, rel)]`` lookups so the Events / Cutscenes tab can
populate the map picker without per-map work.

Pipeline:

* **Pass 1** decodes every owner entry once, accumulating cross-entry
  xrefs (script dispatches that land outside their source entry's byte
  range). The structural decoder mirrors the opcode set + alignment
  guards from ``research_docs/claude_notes/overlay5_annotate.py``;
  matchers that don't feed the cutscene graph (SET_VAR, MOVE, reaction
  balloons, …) are still walked so their bytes are consumed correctly
  and don't get misread as CALL_SYS / END_SCRIPT.

* **Cross-entry grouping** buckets each xref into ``(target_entry_ix,
  entry_rel) -> [external_tag, ...]`` so Pass 2's region slicer cuts
  every cross-entry entry-point as its own region, not a continuation
  of the previous block.

* **Pass 2** re-decodes every entry with the external labels seeded,
  emits ``(rel, kind, params)`` events plus per-rel label callers, and
  slices the entry into regions using the END_SCRIPT-aware merge rule
  (a label whose only caller is a single intra-script CALL_SYS folds
  inline unless the previous decoded op was END_SCRIPT, which forces
  a fresh region — chained cutscenes ``... END_SCRIPT CALL_SYS @0xXX
  -> +next_scene`` would otherwise fold into one giant block).

* **Chain walk** starts from each entry-point region and follows
  *tail* CALL_SYS / CALL_SCRIPT links (the call whose immediate-next
  decoded op is END_SCRIPT) until it leaves the region index, hits a
  cycle, or runs out of tail calls. The walked path is the logical
  cutscene.

The structural index does NOT resolve message text, portrait names,
overworld names, etc. — those are display concerns. The UI resolves
``msg_id`` / ``portrait_id`` / ``overworld_id`` u16s at render time
against MSG.PAK / portrait_ids / overworld_list.

Build time on a vanilla Dusk ROM: ~1-2s end-to-end (dominated by the
two byte-walks). Memory: ~2k regions + ~2k chains, all small
dataclasses — single-digit MB.
"""
from __future__ import annotations

import bisect
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import overlay5 as ov5


# ---- structural opcode kinds ---------------------------------------------

# Only the opcodes whose POSITION the chain graph cares about. Everything
# else is recognized to keep the walker aligned but isn't emitted.
class OpKind:
    END_SCRIPT = "END_SCRIPT"
    CALL_SYS = "CALL_SYS"
    CALL_SCRIPT = "CALL_SCRIPT"
    # Per-screen map loads. 0x014B and 0x014C bodies set the top and
    # bottom screen maps respectively (the entry's prologue header at
    # offset 0 also carries a 0x014C). The chain graph uses these to
    # fan-out attribution: a chain that loads a non-self map onto either
    # screen also surfaces under that map's chip list so the user can
    # find the scene from the secondary NPC's home map.
    LOAD_TOP_MAP = "LOAD_TOP_MAP"
    LOAD_BOTTOM_MAP = "LOAD_BOTTOM_MAP"


# Trigger kinds for entry-point regions, matching the offline indexer.
class TriggerKind:
    OWS = "ows"
    HITBOX = "hitbox"
    EXIT = "exit"
    HANDLER = "handler"
    EXT = "ext"
    OTHER = "other"
    HEADER = "header"  # synthetic header region at entry+0x0000


@dataclass(frozen=True)
class _Op:
    """One structurally-interesting decoded opcode hit."""
    rel: int       # entry-relative offset of the op
    kind: str      # one of OpKind.*
    target: int    # resolved target offset (entry-relative if intra-, file-absolute if cross-)
    cross: bool    # True if target is cross-entry (file offset, not entry-rel)


# ---- region / chain dataclasses ------------------------------------------


@dataclass(frozen=True)
class CutsceneRegion:
    """One END_SCRIPT-bounded script block inside an overlay5 entry.

    ``entry_ix`` + ``rel`` together name the region (the offline indexer
    uses the same ``(entry, rel)`` key in its chain paths). ``end_rel``
    is exclusive — the next region in the same entry starts there, or
    it equals the entry length for the last region.

    ``is_entry_point`` flags regions reached from external triggers
    (REGISTER_HANDLER / HITBOX / EXIT_ZONE / OWS_str interactions, or
    cross-entry CALL_SYS / CALL_SCRIPT). Non-entry-point regions are
    intra-script jump targets — they're visited as continuations of a
    chain but never originate one.

    ``tail_target_key`` is the ``(entry_ix, rel)`` the region's last
    decoded op tail-calls into, if any. ``None`` when the region ends
    on a non-tail terminator or its tail target lands outside the
    known region map (system routine / off-overlay).

    ``sub_scene_rels`` lists intra-entry rels reached via *dropped*
    CALL_SYS-distant tail links (sub-scene call-and-return calls that
    the adjacency heuristic refuses to chain). The chain walker doesn't
    follow these, but :func:`build_cutscene_index` uses them to
    propagate source-map attribution so the orphaned sub-scenes still
    surface as chips on the originating map.

    ``top_screen_map_ids`` / ``bottom_screen_map_ids`` list any map ids
    loaded by 0x014B / 0x014C opcodes inside this region's byte span.
    Many entries' prologue start with ``4C 01 [self_map_id]`` (the
    region's "native" bottom map); mid-region loads onto either screen
    flag a dual-screen scene. :func:`build_cutscene_index` unions these
    across each chain's path and fans the chain into the secondary
    map's chip list.
    """
    entry_ix: int
    rel: int
    end_rel: int
    is_entry_point: bool
    trigger_kind: str
    trigger_label: str
    short_name: str
    ext_callers: Tuple[int, ...]
    tail_target_key: Optional[Tuple[int, int]]
    sub_scene_rels: Tuple[int, ...] = ()
    top_screen_map_ids: Tuple[int, ...] = ()
    bottom_screen_map_ids: Tuple[int, ...] = ()

    @property
    def key(self) -> Tuple[int, int]:
        return (self.entry_ix, self.rel)


@dataclass(frozen=True)
class CutsceneChain:
    """A linear chain of regions linked by tail CALL_SYS / CALL_SCRIPT.

    ``source_entry_ix`` is the entry whose map the chain belongs to
    (the trigger's host entry, which is what the player is standing
    on when the chain fires). The body may cross into other entries
    — ``path`` records each link's ``(entry_ix, rel)`` in execution
    order.

    ``secondary_top_map_ids`` / ``secondary_bottom_map_ids`` are the
    map ids the chain's regions explicitly load onto the top / bottom
    DS screen via 0x014B / 0x014C, EXCLUDING the chain's own source
    map. These drive dual-screen attribution: a chain firing on map 52
    that loads map 51 onto the top screen also surfaces on map 51's
    chip list (with a "top from m52" hint) so the user can find the
    scene from the secondary NPC's home map.
    """
    source_entry_ix: int
    trigger_kind: str
    trigger_label: str
    path: Tuple[Tuple[int, int], ...]
    secondary_top_map_ids: Tuple[int, ...] = ()
    secondary_bottom_map_ids: Tuple[int, ...] = ()

    @property
    def start_key(self) -> Tuple[int, int]:
        return self.path[0]

    @property
    def length(self) -> int:
        return len(self.path)


@dataclass
class CutsceneIndex:
    """Read-only view exposed to the editor UI."""
    regions: Dict[Tuple[int, int], CutsceneRegion]
    regions_by_entry: Dict[int, List[CutsceneRegion]]
    chains: List[CutsceneChain]
    chains_by_map: Dict[int, List[int]]
    chains_by_source: Dict[int, List[int]]
    # Reverse of ``CutsceneRegion.sub_scene_rels``: for each orphan-promoted
    # sub-scene ``(entry_ix, sub_rel)``, the list of parent regions that
    # tail-call-distantly invoke it. Lets the renderer inherit the parent's
    # OWS spawns into the sub-scene's canvas — the sub-scene itself only
    # carries dialog while the spawned NPCs live in the parent handler's
    # byte span (typical pattern: entry 0499 handler loads 7 NPCs, then
    # CALL_SYS-distant into a sub-scene that emits only dialogs).
    parent_regions_by_sub_scene: Dict[
        Tuple[int, int], Tuple[Tuple[int, int], ...]
    ] = field(default_factory=dict)

    def chains_for_map(self, map_id: int) -> List[CutsceneChain]:
        """O(1) lookup of all chains whose source entry maps to ``map_id``.

        Returns chain objects (not indices) so callers don't need to dereference
        through ``self.chains``. Empty list when the map has no triggers
        (or no entry — out-of-range ids return empty rather than raising).
        """
        idxs = self.chains_by_map.get(map_id, ())
        return [self.chains[i] for i in idxs]


# ---- map_id resolution ---------------------------------------------------

def _chain_map_id_for(entry_ix: int) -> Optional[int]:
    if ov5.MAP_INDEX_OFFSET <= entry_ix < ov5.MAP_INDEX_MAX:
        return entry_ix - ov5.MAP_INDEX_OFFSET
    return None


# ---- prologue / decoder constants ----------------------------------------

# Sizes of opcodes the prologue walker recognizes — used both to find
# the prologue's extent (so EXIT_ZONE / REGISTER_HANDLER recognition is
# gated to that region) and to know how many bytes each consumes.
_PROLOGUE_OPCODE_SIZES: Dict[int, int] = {
    0x001B: 18,  # EXIT_ZONE / HITBOX / SPAWN
    0x0004: 6,   # REGISTER_HANDLER
    0x0006: 6,
    0x0009: 4,
    0x00A6: 10,  # HANDLER_META (also a body-opcode with different layout)
    0x00A7: 10,
    0x014D: 10,
}

# The body walker's opcode size table. Bytes inside an opcode payload
# can spell ``02 00`` / ``03 00`` / ``9A 00`` / ``50 01`` and trick the
# matchers; consuming each opcode in one step keeps the walker aligned.
# Subset of the annotator's matchers — only what's needed to avoid mid-
# payload misalignment for our three structural opcodes.
_BODY_FIXED_OPCODE_SIZES: Dict[int, int] = {
    0x0005: 6,   # SET_VAR_ALT
    0x000E: 4,   # HANDLER_ENTRY (0e 00 [arg:2]) — start of a registered handler script
    0x0015: 6,   # SET_VAR
    0x003C: 4,   # MOVE_BEGIN
    0x003E: 4,   # MOVE_END
    0x0076: 4,   # MOVE_OPEN
    0x00C0: 6,   # REACTION_BALLOON
    0x00BB: 4,   # OPEN_SHOP
}


# ---- per-entry decoder ---------------------------------------------------


class _EntryDecoder:
    """Walks one overlay5 entry's bytes once, producing structural events
    + intra-entry / cross-entry xref records.

    Holds the same state shape ``research_docs/.../overlay5_annotate.py``
    keeps on its ``Annotator``, but doesn't format text. Reused across
    entries — the driver resets per-entry state in :meth:`decode`.
    """

    def __init__(self, overlay_payload: bytes) -> None:
        self.overlay = overlay_payload
        # Filled per-decode call.
        self._b: bytes = b""
        self._entry_offset: int = 0
        self._prologue_end: int = 0
        self._prologue_boundaries: Set[int] = set()
        # Targets that land inside this entry — ``rel -> [caller_tag, ...]``.
        self.pending_labels: Dict[int, List[str]] = {}
        # Targets that land outside this entry — ``(target_file_ptr, tag)`` list.
        self.unresolved_refs: List[Tuple[int, str]] = []
        # Structural ops, in walk order. Used by the slicer to find END_SCRIPT
        # positions and tail-call links.
        self.ops: List[_Op] = []
        # Externally-seeded labels (Pass 2). Same shape as ``pending_labels``.
        self.external_labels: Dict[int, List[str]] = {}

    # ---- xref bookkeeping -------------------------------------------------

    def _register_xref(
        self, file_ptr: int, tag: str,
    ) -> Tuple[bool, int]:
        """Record ``file_ptr`` as a cross-reference target.

        Returns ``(intra_entry, value)`` — when ``intra_entry`` is True
        the value is the entry-relative offset and the label was added
        to :attr:`pending_labels`; otherwise the value is ``file_ptr``
        unchanged and the ref was appended to :attr:`unresolved_refs`.
        Callers that emit a structural :class:`_Op` use the return to
        fill the op's ``target`` + ``cross`` fields.
        """
        if self._entry_offset <= 0:
            return (False, file_ptr)
        rel = file_ptr - self._entry_offset
        if 0 <= rel < len(self._b):
            self.pending_labels.setdefault(rel, []).append(tag)
            return (True, rel)
        self.unresolved_refs.append((file_ptr, tag))
        return (False, file_ptr)

    # ---- prologue scan ----------------------------------------------------

    def _scan_prologue_end(self) -> int:
        """Length of the prologue region (run of known prologue opcodes
        starting at offset 4 past a 0x014C map header).

        Side effect: populates :attr:`_prologue_boundaries` so prologue-
        gated matchers know which byte offsets are real opcode starts.
        """
        self._prologue_boundaries = set()
        b = self._b
        if len(b) < 4 or b[0] != 0x4C or b[1] != 0x01:
            return 0
        off = 4
        n = len(b)
        while off + 2 <= n:
            opcode = struct.unpack_from("<H", b, off)[0]
            size = _PROLOGUE_OPCODE_SIZES.get(opcode)
            if size is None or off + size > n:
                break
            self._prologue_boundaries.add(off)
            off += size
        return off

    # ---- prologue opcode matchers (xref-emitting) ------------------------

    def _try_prologue_register_handler(self, p: int) -> Optional[int]:
        """``04 00 [u32:file_off]`` — handler registration. Records the
        target as a cross-ref tagged ``REGISTER_HANDLER @0xPPPP`` so
        Pass 2's slicer recognizes the destination as an entry point.
        """
        b = self._b
        if p not in self._prologue_boundaries:
            return None
        if p + 6 > len(b) or b[p] != 0x04 or b[p + 1] != 0x00:
            return None
        tgt = struct.unpack_from("<I", b, p + 2)[0]
        self._register_xref(tgt, f"REGISTER_HANDLER @0x{p:04x}")
        return 6

    def _try_body_register_handler(self, p: int) -> Optional[int]:
        """Body-side ``04 00 [u32:file_off]`` — sibling of the prologue
        REGISTER_HANDLER. Some entries (e.g., 0265's 25-entry dispatch
        table) emit handler registrations from the body, interleaved
        with HANDLER_META and conditional preambles. Without this, the
        walker skips the opcode byte-by-byte, mis-aligning the slicer's
        region cuts inside the registered handler's target entry.

        Strict target signature (``overlay[u32:u32+2] == 0e 00``) is
        load-bearing: random ``04 00 [u32]`` byte coincidences in dialog
        payloads or move blocks must NOT emit cross-entry xrefs.
        """
        b = self._b
        # Prologue's matcher already handles boundary-aligned opcodes;
        # skip them defensively in case priority order ever changes.
        if p in self._prologue_boundaries:
            return None
        if p + 6 > len(b) or b[p] != 0x04 or b[p + 1] != 0x00:
            return None
        tgt = struct.unpack_from("<I", b, p + 2)[0]
        if tgt < 0x100 or tgt + 2 > len(self.overlay):
            return None
        if self.overlay[tgt] != 0x0E or self.overlay[tgt + 1] != 0x00:
            return None
        self._register_xref(tgt, f"REGISTER_HANDLER @0x{p:04x}")
        return 6

    def _try_prologue_exit_zone(self, p: int) -> Optional[int]:
        """``1B 00 [idx:2][x1:2][y1:2][x2:2][y2:2][flag:2][dst:4]``.

        Distinguishes EXIT_ZONE (warp) vs HITBOX (bespoke handler) vs
        SPAWN (dst==0) by peeking at the handler shape at ``dst``. The
        emitted xref tag uses the resolved kind so Pass 2's slicer
        picks the right ``trigger_kind`` for the destination region.
        """
        b = self._b
        if p >= self._prologue_end:
            return None
        if p + 18 > len(b) or b[p] != 0x1B or b[p + 1] != 0x00:
            return None
        idx = struct.unpack_from("<H", b, p + 2)[0]
        dst = struct.unpack_from("<I", b, p + 14)[0]
        if dst != 0:
            kind = self._classify_exit_dst(dst)
            self._register_xref(dst, f"{kind}#{idx} @0x{p:04x}")
        return 18

    def _classify_exit_dst(self, dst: int) -> str:
        """``02 00 [u32] 30 00 [u32]`` at ``dst`` => EXIT_ZONE (warp).
        Anything else => HITBOX (bespoke). Mirrors
        :class:`overlay5.ExitHandler` shape check."""
        if dst < 0 or dst + 12 > len(self.overlay):
            return "HITBOX"
        op_fade = struct.unpack_from("<H", self.overlay, dst)[0]
        op_call = struct.unpack_from("<H", self.overlay, dst + 6)[0]
        if op_fade == 0x0002 and op_call == 0x0030:
            return "EXIT_ZONE"
        return "HITBOX"

    # ---- body opcode matchers --------------------------------------------

    def _try_dialog(self, p: int) -> Optional[int]:
        """12-byte ``9A 00 [tgt:2] 9E 00 [msg:2] [portrait:2] 9C 00``.
        Recognized so CALL_SYS can't eat its bytes (and conversely so a
        ``02 00`` at p+2 isn't decoded as CALL_SYS into the message id)."""
        b = self._b
        if p + 12 > len(b):
            return None
        if not (b[p] == 0x9A and b[p + 1] == 0x00
                and b[p + 4] == 0x9E and b[p + 5] == 0x00
                and b[p + 10] == 0x9C and b[p + 11] == 0x00):
            return None
        return 12

    def _try_overworld_sprite(self, p: int) -> Optional[int]:
        """26-byte ``50 01 ...`` block. Records the optional ``string_ptr``
        u32 (offset 16) as an OWS_str xref so a sprite's dialog handler
        is surfaced as its own entry-point region."""
        b = self._b
        if p + 26 > len(b):
            return None
        if not (b[p] == 0x50 and b[p + 1] == 0x01):
            return None
        slot = struct.unpack_from("<H", b, p + 8)[0]
        slot_a = struct.unpack_from("<H", b, p + 14)[0]
        slot_b = struct.unpack_from("<H", b, p + 22)[0]
        # Fingerprint: real OWS blocks always mirror slot at offsets 8/14/22.
        # Payload bytes that happen to spell `50 01` almost never line up
        # all three mirrors — filters out false positives without a full
        # prologue/body model.
        if slot_a != slot or slot_b != slot:
            return None
        ow_id = struct.unpack_from("<H", b, p + 2)[0]
        string_ptr = struct.unpack_from("<I", b, p + 16)[0]
        if string_ptr != 0:
            self._register_xref(
                string_ptr,
                f"OWS_str slot={slot} ow_id=0x{ow_id:04x} @0x{p:04x}",
            )
        return 26

    def _try_battle(self, p: int) -> Optional[int]:
        """Variable-length ``DA 00 ... BA 00`` battle setup. We only need
        to consume the bytes so the END_SCRIPT after the battle doesn't
        get misread as part of the payload. Returns the consumed length
        or None if the block doesn't decode."""
        b = self._b
        if p + 12 > len(b) or b[p] != 0xDA or b[p + 1] != 0x00:
            return None
        cursor = p + 12  # past DA 00 + 5 enemy u16s
        if cursor + 2 > len(b) or b[cursor] != 0xD7 or b[cursor + 1] != 0x00:
            return None
        cursor += 2
        d7_start = cursor
        while cursor + 1 < len(b) and not (
            b[cursor] == 0xD8 and b[cursor + 1] == 0x00
        ):
            cursor += 2
            if cursor - d7_start > 64:  # don't run away
                return None
        # D8 00 [bg:2] D9 00 [music:2] BA 00
        if cursor + 8 > len(b):
            return None
        cursor += 4  # past D8 00 [bg]
        if b[cursor] != 0xD9 or b[cursor + 1] != 0x00:
            return None
        cursor += 4  # past D9 00 [music]
        if b[cursor] != 0xBA or b[cursor + 1] != 0x00:
            return None
        cursor += 2
        return cursor - p

    def _try_call_sys(self, p: int) -> Optional[int]:
        """``02 00 [u32]`` — system / dispatch call. Emits a CALL_SYS op
        with resolved target."""
        b = self._b
        if p + 6 > len(b) or b[p] != 0x02 or b[p + 1] != 0x00:
            return None
        # DIALOG-victim guard — ``02 00 9A 00 ...`` is a counter+DIALOG
        # glue, not CALL_SYS into 0x...009A.
        if b[p + 2] == 0x9A and b[p + 3] == 0x00:
            return None
        # END_SCRIPT-glue guard — ``02 00 03 00 ...`` is the tail of one
        # script (END_SCRIPT at p+2) followed by the start of the next,
        # not CALL_SYS into 0x...0003.
        if b[p + 2] == 0x03 and b[p + 3] == 0x00:
            return None
        tgt = struct.unpack_from("<I", b, p + 2)[0]
        if tgt == 0 or tgt >= len(self.overlay):
            return None
        intra, value = self._register_xref(tgt, f"CALL_SYS @0x{p:04x}")
        self.ops.append(
            _Op(rel=p, kind=OpKind.CALL_SYS, target=value, cross=not intra)
        )
        return 6

    def _try_call_script(self, p: int) -> Optional[int]:
        """``30 00 [u32]`` — explicit CALL_SCRIPT_AT_OFFSET."""
        b = self._b
        if p + 6 > len(b) or b[p] != 0x30 or b[p + 1] != 0x00:
            return None
        if b[p + 2] == 0x9A and b[p + 3] == 0x00:
            return None
        if b[p + 2] == 0x03 and b[p + 3] == 0x00:
            return None
        tgt = struct.unpack_from("<I", b, p + 2)[0]
        if tgt == 0 or tgt >= len(self.overlay):
            return None
        intra, value = self._register_xref(tgt, f"CALL_SCRIPT @0x{p:04x}")
        self.ops.append(
            _Op(rel=p, kind=OpKind.CALL_SCRIPT, target=value, cross=not intra)
        )
        return 6

    def _try_end_script(self, p: int) -> Optional[int]:
        """``03 00`` — END_SCRIPT terminator. Short signature: matches
        on payload bytes too, but the slicer's merge rule + tail-call
        check handle the noise cleanly."""
        b = self._b
        if p + 2 > len(b) or b[p] != 0x03 or b[p + 1] != 0x00:
            return None
        self.ops.append(
            _Op(rel=p, kind=OpKind.END_SCRIPT, target=0, cross=False)
        )
        return 2

    def _try_load_screen_map(self, p: int) -> Optional[int]:
        """4-byte ``4B 01 [map_id:u16]`` (top screen) or ``4C 01
        [map_id:u16]`` (bottom screen). Emits :data:`OpKind.LOAD_TOP_MAP`
        / :data:`OpKind.LOAD_BOTTOM_MAP` ops carrying the loaded map id
        in ``target``. The slicer buckets these per-region and the chain
        index fans them out into secondary chip listings."""
        b = self._b
        if p + 4 > len(b):
            return None
        op_lo = b[p]
        if op_lo not in (0x4B, 0x4C) or b[p + 1] != 0x01:
            return None
        map_id = struct.unpack_from("<H", b, p + 2)[0]
        kind = OpKind.LOAD_TOP_MAP if op_lo == 0x4B else OpKind.LOAD_BOTTOM_MAP
        self.ops.append(
            _Op(rel=p, kind=kind, target=map_id, cross=False)
        )
        return 4

    def _try_fixed_body(self, p: int) -> Optional[int]:
        """Recognized-but-not-emitted body opcodes from
        :data:`_BODY_FIXED_OPCODE_SIZES`. Consumes the bytes so the
        following CALL_SYS / END_SCRIPT decodes don't land mid-payload."""
        b = self._b
        if p + 2 > len(b):
            return None
        opcode = struct.unpack_from("<H", b, p)[0]
        size = _BODY_FIXED_OPCODE_SIZES.get(opcode)
        if size is None or p + size > len(b):
            return None
        # Mirror the annotator's DIALOG-victim guard on SET_VAR / SET_VAR_ALT
        # — refuse if the 6-byte read would swallow a DIALOG magic 9A 00
        # at the var or val u16 position.
        if opcode in (0x0005, 0x0015):
            var = struct.unpack_from("<H", b, p + 2)[0]
            val = struct.unpack_from("<H", b, p + 4)[0]
            if var == 0x009A or val == 0x009A:
                return None
        return size

    def _try_handler_meta(self, p: int) -> Optional[int]:
        """Prologue ``A6 00 [cond:2] [val:2] [opaque:4]``. Only matches
        inside the prologue; the body uses the same opcode byte for a
        different layout, so this guard is load-bearing."""
        b = self._b
        if p not in self._prologue_boundaries:
            return None
        if p + 10 > len(b) or b[p] != 0xA6 or b[p + 1] != 0x00:
            return None
        return 10

    # ---- step driver ------------------------------------------------------

    def _step(self, p: int) -> Optional[int]:
        """One walker step. Returns the number of bytes consumed by a
        recognized opcode, or None when none matched (caller advances 1
        byte). Order matches the annotator's priority — more-specific
        / prologue-gated matchers before generic body matchers."""
        for fn in (
            self._try_prologue_exit_zone,
            self._try_prologue_register_handler,
            self._try_handler_meta,
            self._try_dialog,
            self._try_body_register_handler,
            self._try_overworld_sprite,
            self._try_battle,
            self._try_call_sys,
            self._try_call_script,
            self._try_load_screen_map,
            self._try_fixed_body,
            self._try_end_script,
        ):
            consumed = fn(p)
            if consumed is not None:
                return consumed
        return None

    def decode(self, entry_bytes: bytes, entry_offset: int) -> None:
        """Walk one entry. Idempotent — fully resets per-entry state, so
        the same decoder instance can be reused across many entries
        without leaking xref records between them.

        After return, callers consume:
          * ``self.ops`` for the structural event list
          * ``self.pending_labels`` for intra-entry label callers
          * ``self.unresolved_refs`` for cross-entry callers
        """
        self._b = entry_bytes
        self._entry_offset = entry_offset
        self.pending_labels = {}
        # Seed external labels (Pass 2). Defensive bounds check — a
        # caller bug could land a bogus rel here; the guard keeps the
        # walker honest.
        for rel, tags in self.external_labels.items():
            if 0 <= rel < len(entry_bytes):
                self.pending_labels[rel] = list(tags)
        self.unresolved_refs = []
        self.ops = []
        self._prologue_end = self._scan_prologue_end()
        p = 0
        n = len(entry_bytes)
        while p < n:
            consumed = self._step(p)
            if consumed is None:
                p += 1
            else:
                p += consumed


# ---- region naming + classification --------------------------------------

# Caller-tag regex — first ref's tag drives the region's short name and
# trigger classification. Mirrors the offline indexer's classification
# so the editor's UI labels match the research artifact's filenames.
_EXIT_TAG_RE = re.compile(r"^EXIT_ZONE#(\d+)")
_HITBOX_TAG_RE = re.compile(r"^HITBOX#(\d+)")
_OWS_TAG_RE = re.compile(r"^OWS_str slot=(\d+)\s+ow_id=0x([0-9a-f]+)\s+@0x([0-9a-f]+)")
_HANDLER_TAG_RE = re.compile(r"^REGISTER_HANDLER")
# Anchored to end so the ``ext=NNNN`` suffix on cross-entry boundary
# tags forces a region cut even when there's only one caller (see the
# merge rule in :func:`_derive_region` below).
_GOTO_TAG_RE = re.compile(r"^(?:CALL_SYS|CALL_SCRIPT)\s+@0x[0-9a-f]+$")
_EXT_SUFFIX_RE = re.compile(r"\bext=(\d{4})\b")


def _safe_name(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_")


def _classify_first_tag(tag: str) -> Tuple[str, str]:
    """Return ``(trigger_kind, short_name)`` for the first caller tag of
    a region. ``short_name`` is the base name (without ``_NNNN`` suffix);
    the slicer appends ``_{rel:04x}`` so duplicates of the same name
    stay unique."""
    if m := _EXIT_TAG_RE.match(tag):
        return TriggerKind.EXIT, f"exit_{int(m.group(1))}"
    if m := _HITBOX_TAG_RE.match(tag):
        return TriggerKind.HITBOX, f"hitbox_{int(m.group(1))}"
    if m := _OWS_TAG_RE.match(tag):
        slot = int(m.group(1))
        ow_id = int(m.group(2), 16)
        return TriggerKind.OWS, f"ows_slot{slot:02d}_ow_0x{ow_id:04x}"
    if _HANDLER_TAG_RE.match(tag):
        return TriggerKind.HANDLER, "handler"
    if _EXT_SUFFIX_RE.search(tag):
        return TriggerKind.EXT, "region"
    return TriggerKind.OTHER, "region"


def _derive_region(
    rel: int, tags: List[str], *, force_cut: bool,
) -> Optional[Tuple[str, str, str]]:
    """Decide whether ``rel`` starts a new region.

    Returns ``(trigger_kind, trigger_label, short_name)`` when ``rel``
    is a region boundary, or None when it should stay inline as an
    intra-script goto target.

    Merge rule: a single-caller tag matching ``CALL_SYS @0xXXXX``
    (without ``ext=``) is an intra-script jump and folds inline,
    UNLESS the prior decoded op was END_SCRIPT (then the label starts
    a fresh scene — the prior script terminated). External callers
    (carrying ``ext=NNNN``), multi-caller labels, and structural tags
    (OWS / HITBOX / EXIT / REGISTER_HANDLER) always cut.
    """
    if not tags:
        return None
    if not force_cut and len(tags) == 1 and _GOTO_TAG_RE.match(tags[0]):
        return None
    first = tags[0]
    kind, base = _classify_first_tag(first)
    # Embed the rel into the short_name so two ``handler`` regions in
    # the same entry don't collide (matches the offline filename format).
    return kind, first, f"{base}_{rel:04x}"


# ---- two-pass driver -----------------------------------------------------


def _collect_cross_entry_xrefs(
    decoder: _EntryDecoder,
    src_entry_ix: int,
    bounds_sorted: List[Tuple[int, int, int]],
    bounds_starts: List[int],
) -> List[Tuple[int, int, str]]:
    """Group the decoder's ``unresolved_refs`` (file_ptr, tag) entries
    by which target entry they land in.

    Returns ``[(target_entry_ix, target_rel, ext_tag)]``. ``ext_tag``
    carries an ``ext=NNNN`` suffix so the slicer's merge rule treats
    the destination as a real entry-point even with a single caller.
    """
    out: List[Tuple[int, int, str]] = []
    src_stem = f"{src_entry_ix:04d}"
    for file_ptr, tag in decoder.unresolved_refs:
        i = bisect.bisect_right(bounds_starts, file_ptr) - 1
        if i < 0:
            continue
        start, end, ix = bounds_sorted[i]
        if not (start <= file_ptr < end):
            continue
        rel = file_ptr - start
        out.append((ix, rel, f"{tag} ext={src_stem}"))
    return out


def _owner_bounds(
    overlay: ov5.Overlay5Index,
) -> List[Tuple[int, int, int]]:
    """Owner-only ``(start, end, entry_ix)`` bounds sorted by start.

    Duplicate-target entries share content with the first owner that
    points at a given offset; only owners contribute bounds because
    region segmentation happens once per shared blob.
    """
    seen: Set[int] = set()
    bounds: List[Tuple[int, int, int]] = []
    for ix in range(overlay.entry_count()):
        start = overlay.entry_starts[ix]
        end = overlay.entry_ends[ix]
        if start in seen:
            continue
        seen.add(start)
        bounds.append((start, end, ix))
    bounds.sort()
    return bounds


def _slice_entry_into_regions(
    decoder: _EntryDecoder,
    entry_ix: int,
    entry_len: int,
    label_callers: Dict[int, List[str]],
    ops: List[_Op],
) -> List[CutsceneRegion]:
    """Cut an entry into regions using the END_SCRIPT-aware merge rule.

    Mirrors the offline slicer's behavior on a per-entry basis. The
    synthetic region at ``rel=0`` is always emitted (it carries the map
    header / overworld sprite placements / prologue) even if no caller
    tags landed there — gives the chain walker a stable entry-point key
    when something tail-calls into ``entry+0x0000`` (most cross-entry
    hops land at offset 0).

    Tail-target resolution: for each region we look at the last decoded
    op AT or BEFORE the region's terminating END_SCRIPT; if that op is
    a CALL_SYS / CALL_SCRIPT we treat its target as the tail link. Cross-
    entry tail targets are resolved against the owner bounds at chain-
    walk time (here we only store the intra-entry rel; the chain walker
    handles cross-entry resolution).
    """
    # Collect boundary rels in walk order. ops + END_SCRIPT positions
    # drive the force_cut flag for each label rel.
    end_script_rels: Set[int] = {op.rel for op in ops if op.kind == OpKind.END_SCRIPT}

    # Walking ops in order, track which label rels follow an END_SCRIPT
    # immediately (with no other decoded op in between). That set is
    # what triggers force_cut for the goto-merge rule.
    forced_cut_rels: Set[int] = set()
    # Sort label rels so we can sweep ops + labels together.
    label_rels_sorted = sorted(label_callers.keys())
    next_label_ix = 0
    last_op_was_end_script = False
    for op in ops:
        # Any label rel between the previous op and this one inherits
        # the previous decoded line's END_SCRIPT-ness.
        while (
            next_label_ix < len(label_rels_sorted)
            and label_rels_sorted[next_label_ix] <= op.rel
        ):
            if last_op_was_end_script:
                forced_cut_rels.add(label_rels_sorted[next_label_ix])
            next_label_ix += 1
        last_op_was_end_script = (op.kind == OpKind.END_SCRIPT)
    # Tail: labels after the last op inherit the final op's state.
    while next_label_ix < len(label_rels_sorted):
        if last_op_was_end_script:
            forced_cut_rels.add(label_rels_sorted[next_label_ix])
        next_label_ix += 1

    # Build the region list. Always include rel=0 as the entry header
    # so chain walks landing at entry+0x0000 have somewhere to dock.
    region_rels: List[int] = []
    region_meta: Dict[int, Tuple[str, str, str, List[str]]] = {}
    if 0 not in label_callers:
        region_rels.append(0)
        region_meta[0] = (TriggerKind.HEADER, "(map header)", "region_0000", [])
    for rel in sorted(label_callers.keys()):
        tags = label_callers[rel]
        info = _derive_region(rel, tags, force_cut=(rel in forced_cut_rels))
        if info is None:
            # Intra-script goto — folds inline, no region cut here.
            continue
        if rel not in region_meta:
            region_rels.append(rel)
            kind, label, short = info
            region_meta[rel] = (kind, label, short, tags)
    region_rels.sort()

    # Map rel -> exclusive end (next region's rel, or entry length).
    # The synthetic header at 0 inherits the next cut's offset.
    end_for_rel: Dict[int, int] = {}
    for i, rel in enumerate(region_rels):
        end_for_rel[rel] = (
            region_rels[i + 1] if i + 1 < len(region_rels) else entry_len
        )

    # Tail-call resolution: for each region, find the last decoded op
    # at or before its terminating END_SCRIPT (which must be the last
    # END_SCRIPT inside the region span). If that op is CALL_SYS or
    # CALL_SCRIPT, that's our tail target.
    #
    # Adjacency heuristic (intra-entry CALL_SYS only): an intra-entry
    # ``CALL_SYS @target ; END_SCRIPT`` pair is treated as a tail link
    # only when ``target`` lands on the NEXT region boundary (i.e. the
    # call falls through into the region that immediately follows). A
    # distant intra-entry CALL_SYS is a call-and-return invocation of a
    # sub-scene, not a chain continuation — folding it into the source
    # chain over-aggregates the scene (observed on map 29's HANDLER and
    # the Sayo handler at REGISTER_HANDLER @0x0054 in entry 0287). The
    # orphaned target gets promoted to its own entry-point on the same
    # map so its content still surfaces as a separate chip.
    #
    # CALL_SCRIPT (opcode 0x0030) is always a tail link, regardless of
    # adjacency — it's the explicit chain-link opcode and the engine
    # never returns from it. Cross-entry CALL_SYS is also kept as a
    # tail link (the chain walker resolves it against owner bounds).
    tail_target_per_rel: Dict[int, Optional[Tuple[int, int]]] = {}
    sub_scene_per_rel: Dict[int, List[int]] = {}
    promoted_rels: Set[int] = set()
    top_map_ids_per_rel: Dict[int, List[int]] = {}
    bottom_map_ids_per_rel: Dict[int, List[int]] = {}
    # Precompute op index by rel for fast scan.
    ops_sorted = sorted(ops, key=lambda o: o.rel)
    op_rels = [o.rel for o in ops_sorted]
    # Region-rel lookup helper for the adjacency check / orphan
    # promotion. ``bisect_right`` on the sorted region rels + step-back
    # finds the region containing an arbitrary rel.
    region_rels_sorted = sorted(region_rels)
    # Ops the tail-link check should ignore when looking for the
    # CALL_SYS/CALL_SCRIPT right before END_SCRIPT — LOAD_TOP_MAP and
    # LOAD_BOTTOM_MAP can sit between the chain link and the script
    # terminator (e.g. ``CALL_SYS @sub ; LOAD_TOP_MAP 51 ; END_SCRIPT``)
    # and we mustn't lose the chain link because of them.
    _STRUCTURAL_LOAD_KINDS = (OpKind.LOAD_TOP_MAP, OpKind.LOAD_BOTTOM_MAP)
    for rel in region_rels:
        end = end_for_rel[rel]
        # Find the last END_SCRIPT in [rel, end). Also bucket LOAD_*_MAP
        # ops inside the region span as we sweep — saves a second scan.
        lo = bisect.bisect_left(op_rels, rel)
        hi = bisect.bisect_left(op_rels, end)
        last_end_ix = -1
        for i in range(lo, hi):
            op = ops_sorted[i]
            if op.kind == OpKind.LOAD_TOP_MAP:
                top_map_ids_per_rel.setdefault(rel, []).append(op.target)
            elif op.kind == OpKind.LOAD_BOTTOM_MAP:
                bottom_map_ids_per_rel.setdefault(rel, []).append(op.target)
            elif op.kind == OpKind.END_SCRIPT:
                last_end_ix = i
        if last_end_ix <= 0:
            tail_target_per_rel[rel] = None
            continue
        # Walk backward from the terminator, skipping LOAD_*_MAP ops, to
        # find the structural op that actually carries the chain link.
        prev_ix = last_end_ix - 1
        while prev_ix >= lo and ops_sorted[prev_ix].kind in _STRUCTURAL_LOAD_KINDS:
            prev_ix -= 1
        if prev_ix < lo:
            tail_target_per_rel[rel] = None
            continue
        prev = ops_sorted[prev_ix]
        if prev.kind not in (OpKind.CALL_SYS, OpKind.CALL_SCRIPT):
            tail_target_per_rel[rel] = None
            continue
        if prev.cross:
            # Cross-entry — the chain walker resolves the (entry, rel)
            # against the owner bounds at walk time. Store as a sentinel
            # ``(-1, file_offset)`` so the walker knows to look it up.
            tail_target_per_rel[rel] = (-1, prev.target)
            continue
        # Intra-entry CALL_SYS — gate on adjacency.
        if prev.kind == OpKind.CALL_SYS and prev.target != end:
            # Distant intra-entry CALL_SYS — drop the chain link and
            # promote the enclosing target region to its own entry-point.
            tail_target_per_rel[rel] = None
            # Find the region containing prev.target (floor-search).
            ti = bisect.bisect_right(region_rels_sorted, prev.target) - 1
            if 0 <= ti < len(region_rels_sorted):
                target_region_rel = region_rels_sorted[ti]
                if target_region_rel < end_for_rel[target_region_rel]:
                    promoted_rels.add(target_region_rel)
                    sub_scene_per_rel.setdefault(rel, []).append(
                        target_region_rel,
                    )
            continue
        # CALL_SCRIPT (always tail) or adjacent intra-entry CALL_SYS.
        tail_target_per_rel[rel] = (entry_ix, prev.target)

    regions: List[CutsceneRegion] = []
    for rel in region_rels:
        kind, label, short, tags = region_meta[rel]
        # ext_callers: extract source entry ixs from tags carrying ext=NNNN.
        ext_callers_set: Set[int] = set()
        for t in tags:
            for m in _EXT_SUFFIX_RE.finditer(t):
                ext_callers_set.add(int(m.group(1)))
        # Orphan-promotion: a region that was reachable only via a now-
        # dropped CALL_SYS-distant tail link becomes its own entry-point
        # so the user still sees its content as a separate chip on the
        # same map. Surface the synthetic ``OTHER`` kind unchanged — the
        # UI compresses ``CALL_SYS @0xXXXX`` labels into a short form.
        is_promoted = rel in promoted_rels
        is_entry_point = bool(ext_callers_set) or kind in (
            TriggerKind.OWS, TriggerKind.HITBOX,
            TriggerKind.EXIT, TriggerKind.HANDLER,
        ) or is_promoted
        regions.append(CutsceneRegion(
            entry_ix=entry_ix,
            rel=rel,
            end_rel=end_for_rel[rel],
            is_entry_point=is_entry_point,
            trigger_kind=kind,
            trigger_label=label,
            short_name=short,
            ext_callers=tuple(sorted(ext_callers_set)),
            tail_target_key=tail_target_per_rel[rel],
            sub_scene_rels=tuple(sub_scene_per_rel.get(rel, ())),
            top_screen_map_ids=tuple(top_map_ids_per_rel.get(rel, ())),
            bottom_screen_map_ids=tuple(bottom_map_ids_per_rel.get(rel, ())),
        ))
    return regions


def _resolve_tail_key(
    key: Optional[Tuple[int, int]],
    regions: Dict[Tuple[int, int], CutsceneRegion],
    bounds_sorted: List[Tuple[int, int, int]],
    bounds_starts: List[int],
) -> Optional[Tuple[int, int]]:
    """Map a region's raw ``tail_target_key`` to a concrete region key.

    Intra-entry keys ``(entry_ix, rel)`` resolve directly when the
    target falls on a known region start, or by floor-search when the
    target lands mid-region (the enclosing region is the chain link).

    Cross-entry sentinels ``(-1, file_offset)`` go through the owner-
    bounds bisect, then floor-search inside the target entry to find
    the enclosing region.
    """
    if key is None:
        return None
    entry_ix, value = key
    if entry_ix < 0:
        # Cross-entry — resolve via owner bounds.
        file_ptr = value
        i = bisect.bisect_right(bounds_starts, file_ptr) - 1
        if i < 0:
            return None
        start, end, ix = bounds_sorted[i]
        if not (start <= file_ptr < end):
            return None
        target_rel = file_ptr - start
        entry_ix = ix
    else:
        target_rel = value
    # Floor-search in regions_by_entry: find the region whose
    # ``rel <= target_rel < end_rel``.
    return _find_enclosing_region(entry_ix, target_rel, regions)


# Helper: lazy per-entry sorted-rels cache populated by the chain walker.
def _find_enclosing_region(
    entry_ix: int,
    target_rel: int,
    regions: Dict[Tuple[int, int], CutsceneRegion],
) -> Optional[Tuple[int, int]]:
    """Return the region key whose ``(rel, end_rel)`` span contains
    ``target_rel`` in entry ``entry_ix``. Linear scan — fine because
    the typical entry has a handful of regions; the chain walker
    invokes this once per link, not per byte.
    """
    best: Optional[Tuple[int, int]] = None
    best_rel = -1
    for k, r in regions.items():
        if k[0] != entry_ix:
            continue
        if r.rel <= target_rel < r.end_rel and r.rel > best_rel:
            best = k
            best_rel = r.rel
    return best


def _walk_chain(
    start_key: Tuple[int, int],
    regions: Dict[Tuple[int, int], CutsceneRegion],
    bounds_sorted: List[Tuple[int, int, int]],
    bounds_starts: List[int],
    max_links: int = 64,
) -> Tuple[Tuple[int, int], ...]:
    """Follow tail-CALL links from ``start_key`` until the chain exits
    the known region map, hits a cycle, or runs out of tail calls.

    ``max_links`` caps runaway chains; the offline indexer observed a
    max of 17 links across all of overlay5, so 64 is generous.
    """
    path: List[Tuple[int, int]] = [start_key]
    visited: Set[Tuple[int, int]] = {start_key}
    current_key = start_key
    for _ in range(max_links - 1):
        region = regions.get(current_key)
        if region is None or region.tail_target_key is None:
            break
        next_key = _resolve_tail_key(
            region.tail_target_key, regions, bounds_sorted, bounds_starts,
        )
        if next_key is None or next_key in visited:
            break
        path.append(next_key)
        visited.add(next_key)
        current_key = next_key
    return tuple(path)


def build_cutscene_index(
    overlay: ov5.Overlay5Index,
) -> CutsceneIndex:
    """Build the full index from an :class:`overlay5.Overlay5Index`.

    Eager, single-shot — runs the two-pass decode + region slice + chain
    walk over every owner entry. Caller (typically :class:`RomSession`)
    caches the result for the lifetime of the session.
    """
    bounds_sorted = _owner_bounds(overlay)
    bounds_starts = [b[0] for b in bounds_sorted]

    decoder = _EntryDecoder(overlay.payload)

    # Pass 1: decode every owner entry once, accumulating cross-entry xrefs.
    # We discard the intra-entry pending_labels collected here — Pass 2
    # rebuilds them alongside the external labels.
    ext_labels_by_ix: Dict[int, Dict[int, List[str]]] = {}
    for start, end, ix in bounds_sorted:
        entry_bytes = overlay.read_entry(ix)
        decoder.external_labels = {}
        decoder.decode(entry_bytes, entry_offset=start)
        cross = _collect_cross_entry_xrefs(
            decoder, ix, bounds_sorted, bounds_starts,
        )
        for target_ix, target_rel, ext_tag in cross:
            (ext_labels_by_ix
             .setdefault(target_ix, {})
             .setdefault(target_rel, [])
             .append(ext_tag))

    # Pass 2: re-decode with external labels seeded, slice into regions,
    # and resolve tail-call targets to region keys after all regions
    # exist (so cross-entry chain links can be looked up cleanly).
    regions: Dict[Tuple[int, int], CutsceneRegion] = {}
    regions_by_entry: Dict[int, List[CutsceneRegion]] = {}
    for start, end, ix in bounds_sorted:
        entry_bytes = overlay.read_entry(ix)
        decoder.external_labels = ext_labels_by_ix.get(ix, {})
        decoder.decode(entry_bytes, entry_offset=start)
        entry_regions = _slice_entry_into_regions(
            decoder, ix, len(entry_bytes),
            label_callers=dict(decoder.pending_labels),
            ops=list(decoder.ops),
        )
        for r in entry_regions:
            regions[r.key] = r
        regions_by_entry[ix] = entry_regions

    # Orphan source-attribution: one-level propagation from each non-
    # orphan entry-point's tail chain to its directly-called sub-scenes.
    #
    # For each explicit entry-point chain, walk its tail-CALL chain
    # (legitimate continuations only) and attribute the chain's source
    # to every sub_scene_rel touched along the walk. We DON'T recurse
    # into sub-scene-of-sub-scene transitively — deep chained sub-scenes
    # (observed in entry 0499's Tournament cutscene at the Sayo handler:
    # ``0x5742 → 0x612e → 0x65ca → ... → 0xba08`` via 20 dropped tail
    # links) would otherwise all collapse onto the calling map even
    # though only the first sub-scene actually fires from there. Deep
    # orphans fall through to ``entry_ix``'s default map (map 264 for
    # entry 499) — findable but not over-claimed for the parent's map.
    inherited_sources: Dict[Tuple[int, int], Set[int]] = {}
    for key, region in regions.items():
        if not region.is_entry_point:
            continue
        # Orphan-promoted regions (kind=OTHER with no ext_callers) wait
        # for their parent chain to seed sources via propagation. All
        # other entry-points carry their own source attribution.
        is_orphan = (
            region.trigger_kind == TriggerKind.OTHER
            and not region.ext_callers
        )
        if is_orphan:
            continue
        if region.ext_callers:
            sources = set(region.ext_callers)
        else:
            sources = {region.entry_ix}
        # Walk this entry-point's tail chain and attribute ``sources`` to
        # each sub_scene_rel encountered. The walk is bounded by
        # ``visited_in_walk`` to guard against malformed cycles.
        visited_in_walk: Set[Tuple[int, int]] = set()
        cur: Optional[Tuple[int, int]] = key
        while cur is not None and cur not in visited_in_walk:
            visited_in_walk.add(cur)
            cur_region = regions.get(cur)
            if cur_region is None:
                break
            for sub_rel in cur_region.sub_scene_rels:
                sub_key = (cur_region.entry_ix, sub_rel)
                inherited_sources.setdefault(sub_key, set()).update(sources)
            if cur_region.tail_target_key is None:
                break
            cur = _resolve_tail_key(
                cur_region.tail_target_key, regions,
                bounds_sorted, bounds_starts,
            )

    # Chain walk: every entry-point region originates one chain. Multiple
    # ext_callers fan out into one chain per source (so the editor can
    # bucket the chain under each caller's map). Orphan-promoted regions
    # use their inherited source set; explicit entry-points use ext_callers
    # or their own entry_ix.
    chains: List[CutsceneChain] = []
    chains_by_map: Dict[int, List[int]] = {}
    chains_by_source: Dict[int, List[int]] = {}
    for key, region in regions.items():
        if not region.is_entry_point:
            continue
        path = _walk_chain(key, regions, bounds_sorted, bounds_starts)
        # Determine which source entry/entries this chain belongs to.
        sources: List[int]
        is_orphan = (
            region.trigger_kind == TriggerKind.OTHER
            and not region.ext_callers
        )
        if is_orphan:
            inh = inherited_sources.get(key)
            if not inh:
                # Unreachable orphan — no parent chain seeded sources.
                # Fall back to entry_ix so the chip at least surfaces
                # somewhere (rare; would indicate a CALL_SYS into a
                # never-walked region of a never-triggered entry).
                sources = [region.entry_ix]
            else:
                sources = sorted(inh)
        elif region.ext_callers:
            sources = list(region.ext_callers)
        else:
            sources = [region.entry_ix]
        # Union the per-region LOAD_*_MAP arguments across the chain's
        # path. Two scenes can share the same source-screen pair (e.g.
        # entry 0499's Tournament chain loads ``top=51, bot=52``) and
        # they should both fan-out to map 51's chip list.
        top_union: Set[int] = set()
        bottom_union: Set[int] = set()
        for region_key in path:
            r = regions.get(region_key)
            if r is None:
                continue
            top_union.update(r.top_screen_map_ids)
            bottom_union.update(r.bottom_screen_map_ids)
        # Drop the prologue header's self-load (``4C 01 [self_map]`` at
        # offset 0 of each entry) and arg=0 sentinels — neither is a
        # meaningful secondary listing. The per-source filter inside the
        # fan-out loop below also drops the source map itself, so a chain
        # that loads its own map mid-script doesn't surface twice.
        bottom_union.discard(0)
        top_union.discard(0)

        for src_ix in sources:
            chain_idx = len(chains)
            src_map = _chain_map_id_for(src_ix)
            top_secondary = (
                tuple(sorted(top_union - ({src_map} if src_map is not None else set())))
            )
            bottom_secondary = (
                tuple(sorted(bottom_union - ({src_map} if src_map is not None else set())))
            )
            chains.append(CutsceneChain(
                source_entry_ix=src_ix,
                trigger_kind=region.trigger_kind,
                trigger_label=region.trigger_label,
                path=path,
                secondary_top_map_ids=top_secondary,
                secondary_bottom_map_ids=bottom_secondary,
            ))
            chains_by_source.setdefault(src_ix, []).append(chain_idx)
            seen_for_chain: Set[int] = set()
            if src_map is not None:
                chains_by_map.setdefault(src_map, []).append(chain_idx)
                seen_for_chain.add(src_map)
            for secondary_map in (*top_secondary, *bottom_secondary):
                if secondary_map in seen_for_chain:
                    continue
                seen_for_chain.add(secondary_map)
                chains_by_map.setdefault(secondary_map, []).append(chain_idx)

    # Reverse-map sub-scene parentage so the renderer can pull the parent
    # handler's spawned NPCs into the sub-scene's canvas. Stable ordering
    # (sorted by parent key) so downstream rendering is deterministic.
    sub_scene_parents_acc: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for region in regions.values():
        if not region.sub_scene_rels:
            continue
        parent_key = region.key
        for sub_rel in region.sub_scene_rels:
            sub_key = (region.entry_ix, sub_rel)
            sub_scene_parents_acc.setdefault(sub_key, []).append(parent_key)
    parent_regions_by_sub_scene = {
        sub_key: tuple(sorted(set(parents)))
        for sub_key, parents in sub_scene_parents_acc.items()
    }

    return CutsceneIndex(
        regions=regions,
        regions_by_entry=regions_by_entry,
        chains=chains,
        chains_by_map=chains_by_map,
        chains_by_source=chains_by_source,
        parent_regions_by_sub_scene=parent_regions_by_sub_scene,
    )
