"""Game string encoding/decoding.

The game stores text as 2-byte little-endian char codes. Printable chars live
in the 0x0001..0x00BC range; high bytes 0xFF flag special markers (line break,
end, color toggles, variable inserts, etc.). Strings terminate with FF FF.

Pointers to strings are baked into the ROM and not currently editable, so
edits must preserve each string's original byte length (encoded bytes +
terminator). See GameString.encode_capped().
"""
from typing import Dict, List, Optional, Tuple

# ---- character table ---------------------------------------------------------
#
# Format: one entry per line, "<hex pair>|<token>". `hex pair` is the LE byte
# pair as it appears in the ROM, with spaces. `token` is the rendered string
# in the editor — single chars for printable text, [BRACKET] for markers.
#
# Sourced from research_docs/character_table_test.txt and
# research_docs/msgpak_parser_test.py. Update both columns together when
# adding new mappings; the round-trip test will flag misses on any vanilla
# ROM byte that isn't in the table.

_RAW_TABLE = """\
00 00|@SP@
01 00|0
02 00|1
03 00|2
04 00|3
05 00|4
06 00|5
07 00|6
08 00|7
09 00|8
0A 00|9
0B 00|/
0C 00|%
0D 00|:
0E 00|'
0F 00|"
10 00|·
11 00|,
12 00|.
13 00|!
14 00|?
15 00|(
16 00|)
17 00|+
18 00|-
19 00|=
20 00|A
21 00|B
22 00|C
23 00|D
24 00|E
25 00|F
26 00|G
27 00|H
28 00|I
29 00|J
2A 00|K
2B 00|L
2C 00|M
2D 00|N
2E 00|O
2F 00|P
30 00|Q
31 00|R
32 00|S
33 00|T
34 00|U
35 00|V
36 00|W
37 00|X
38 00|Y
39 00|Z
3A 00|a
3B 00|b
3C 00|c
3D 00|d
3E 00|e
3F 00|f
40 00|g
41 00|h
42 00|i
43 00|j
44 00|k
45 00|l
46 00|m
47 00|n
48 00|o
49 00|p
4A 00|q
4B 00|r
4C 00|s
4D 00|t
4E 00|u
4F 00|v
50 00|w
51 00|x
52 00|y
53 00|z
54 00|*
55 00|×
56 00|₋
57 00|→
58 00|←
59 00|↑
5A 00|↓
5B 00|&
5C 00|[bit]
5D 00|[money]
5E 00|[Is.]
5F 00|▬
60 00|α
61 00|β
62 00|γ
63 00|ε
64 00|δ
65 00|∞
66 00|♥
67 00|♪
68 00|★
69 00|#
6A 00|;
6B 00|$
78 00|[LIGHT]
79 00|[DARK]
7A 00|[FIRE]
7B 00|[EARTH]
7C 00|[WIND]
7D 00|[STEEL]
7E 00|[WATER]
7F 00|[THUNDER]
80 00|À
81 00|Á
82 00|Â
83 00|Ã
84 00|Ä
85 00|Å
86 00|Æ
87 00|Ç
88 00|È
89 00|É
8A 00|Ê
8B 00|Ë
8C 00|Ì
8D 00|Í
8E 00|Î
8F 00|Ï
90 00|Đ
91 00|Ñ
92 00|Ò
93 00|Ó
94 00|Ô
95 00|Õ
96 00|Ö
97 00|Ø
98 00|Ù
99 00|Ú
9A 00|Û
9C 00|Ü
9D 00|Ý
9E 00|¡
9F 00|¿
A0 00|à
A1 00|á
A2 00|â
A3 00|ã
A4 00|ä
A5 00|å
A6 00|æ
A7 00|ç
A8 00|è
A9 00|é
AA 00|ê
AB 00|ë
AC 00|ì
AD 00|í
AE 00|î
AF 00|ï
B0 00|ð
B1 00|ñ
B2 00|ò
B3 00|ó
B4 00|ô
B5 00|õ
B6 00|ö
B7 00|ø
B8 00|ù
B9 00|ú
BA 00|û
BB 00|ü
BC 00|ý
E7 FF|[CYAN]
E8 FF|[ORANGE]
E9 FF|[WHITE]
EA FF|[GREEN]
EB FF|[GREY]
ED FF|[PLAYER_NAME]
EE FF|[RED]
EF FF|[MENU]
F0 FF|[VAR]
FD FF|[BR]
FE FF|[END]
"""

# 9B 00 also maps to Û in the source table (duplicate of 9A 00) — kept out of
# the encode side; it still round-trips via the unknown-byte placeholder.

TERMINATOR = 0xFFFF  # FF FF — record/region terminator
END_MARKER = 0xFFFE  # FE FF — [END]: per-string terminator within a record
PLACEHOLDER_PREFIX = "[?"
PLACEHOLDER_SUFFIX = "]"


def _build_tables() -> Tuple[Dict[int, str], Dict[str, int]]:
    byte_to_token: Dict[int, str] = {}
    token_to_byte: Dict[str, int] = {}
    for line in _RAW_TABLE.splitlines():
        if not line or "|" not in line:
            continue
        hex_part, token = line.split("|", 1)
        # On-disk order: <first> <second>. LE value = (second << 8) | first.
        first, second = hex_part.split()
        value = (int(second, 16) << 8) | int(first, 16)
        # Sentinel: @SP@ stands in for a literal space character so the raw
        # table doesn't carry trailing whitespace on that line.
        if token == "@SP@":
            token = " "
        byte_to_token[value] = token
        # First occurrence wins for the reverse table (handles future dupes).
        token_to_byte.setdefault(token, value)
    return byte_to_token, token_to_byte


_BYTE_TO_TOKEN, _TOKEN_TO_BYTE = _build_tables()


# ---- error types -------------------------------------------------------------

class UnknownCharError(ValueError):
    """Raised when encode() encounters a char/token not in the table."""


class StringTooLongError(ValueError):
    """Raised when an encoded string exceeds its original byte budget."""


# ---- helpers -----------------------------------------------------------------

def _read_word(data: bytes, offset: int) -> int:
    """Read a 2-byte LE word."""
    return data[offset] | (data[offset + 1] << 8)


def _word_to_bytes(value: int) -> bytes:
    return bytes([value & 0xFF, (value >> 8) & 0xFF])


def _placeholder_for(value: int) -> str:
    # Render in on-disk byte order, matching the table style.
    first = value & 0xFF
    second = (value >> 8) & 0xFF
    return f"{PLACEHOLDER_PREFIX}{first:02X}{second:02X}{PLACEHOLDER_SUFFIX}"


def _parse_placeholder(token: str) -> int:
    body = token[len(PLACEHOLDER_PREFIX):-len(PLACEHOLDER_SUFFIX)]
    first = int(body[0:2], 16)
    second = int(body[2:4], 16)
    return (second << 8) | first


# ---- public API --------------------------------------------------------------

def decode_string(data: bytes, offset: int, *, end: Optional[int] = None) -> Tuple[str, int, int]:
    """Decode one in-game string starting at `offset`.

    A string ends at the first FE FF ([END]) or FF FF byte pair. The
    terminator is consumed but NOT included in the returned text (it's
    tracked separately so encode() can re-emit the same one).

    `end` caps where the scan is allowed to read. Callers parsing a bounded
    region (e.g. an ARM9 string block whose declared bounds butt up against
    the next data table) pass the region's exclusive end so a string that
    fails to terminate inside the region can't silently chew into the
    neighbouring bytes — which would mark them owned by this string at
    save time and overwrite any edits made through the neighbouring
    model's editor.

    Returns (text, bytes_consumed, terminator_value). `terminator_value` is
    END_MARKER or TERMINATOR, or 0 if the stream ran out before a terminator
    was found.
    """
    tokens: List[str] = []
    cur = offset
    hard_end = len(data) if end is None else min(end, len(data))
    terminator = 0
    while cur + 1 < hard_end:
        word = _read_word(data, cur)
        cur += 2
        if word == TERMINATOR or word == END_MARKER:
            terminator = word
            break
        token = _BYTE_TO_TOKEN.get(word)
        if token is None:
            token = _placeholder_for(word)
        tokens.append(token)
    return "".join(tokens), cur - offset, terminator


def encode_string(text: str, *, terminator: Optional[int] = TERMINATOR) -> bytes:
    """Encode a token-string back to ROM bytes.

    `terminator` is appended as a 2-byte LE word if not None. Pass None to
    encode just the chars (for nested encoding, length probes, etc.).

    Multi-char tokens (e.g. [PLAYER_NAME], [?ABCD]) are matched greedily
    when they start with '['. Single chars are looked up directly. Any
    literal CR / LF (the QPlainTextEdit emits ``\n`` when the user presses
    Enter; pasted text may carry ``\r\n``) is folded to ``[BR]`` here so
    the editor doesn't surface a phantom encode error for what's clearly
    a line break.
    Raises UnknownCharError if a char/token isn't in the table.
    """
    text = text.replace("\r\n", "[BR]").replace("\r", "[BR]").replace("\n", "[BR]")
    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            # Scan for matching ']'. Tokens never nest.
            close = text.find("]", i + 1)
            if close == -1:
                raise UnknownCharError(
                    f"unterminated token starting at position {i}: {text[i:i+16]!r}"
                )
            token = text[i:close + 1]
            if token.startswith(PLACEHOLDER_PREFIX):
                value = _parse_placeholder(token)
            else:
                value = _TOKEN_TO_BYTE.get(token)
                if value is None:
                    raise UnknownCharError(f"unknown token {token!r} at position {i}")
            out.extend(_word_to_bytes(value))
            i = close + 1
        else:
            value = _TOKEN_TO_BYTE.get(ch)
            if value is None:
                raise UnknownCharError(f"unknown char {ch!r} at position {i}")
            out.extend(_word_to_bytes(value))
            i += 1
    if terminator is not None:
        out.extend(_word_to_bytes(terminator))
    return bytes(out)


def byte_length(text: str, *, terminator: Optional[int] = TERMINATOR) -> int:
    """Encoded byte length without allocating the bytes."""
    return len(encode_string(text, terminator=terminator))


def decode_block(data: bytes, start: int, end: int) -> List[Tuple[int, str, int, int]]:
    """Decode a packed run of strings between `start` (inclusive) and `end`.

    Returns a list of (offset, text, byte_length, terminator) tuples. Strings
    are split on FE FF ([END]) and FF FF; the terminator is consumed but not
    embedded in `text`. Stops once `cur` reaches or passes `end`.
    """
    out: List[Tuple[int, str, int, int]] = []
    cur = start
    while cur + 1 < end:
        text, consumed, terminator = decode_string(data, cur, end=end)
        if consumed == 0 or terminator == 0:
            # No more terminators in the region — bail to avoid an infinite
            # loop and to leave any trailing tail-bytes unowned. They keep
            # their original bytes through `serialize_all` because no model
            # writes them back; the neighbouring data table (whose bounds
            # this region butts up against) keeps full ownership.
            break
        out.append((cur, text, consumed, terminator))
        cur += consumed
    return out
