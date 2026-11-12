#!/usr/bin/env python3
"""CID mapping for TinkoffSans fonts used in T-Bank PDF receipts and statements.

Three fonts:
  F1 — TinkoffSans-Regular (body text, labels, values)
  F2 — TinkoffSans-Medium  (bold headers, "Итого" amount in receipts)
  F3 — ALSRubl / Symbola   (₽ sign only)

CID encoding: Identity-H (2-byte big-endian CID codes).
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Optional

# ── Unicode → CID (Regular, superset from all templates) ─────────────

REGULAR_UNI_TO_CID: dict[int, int] = {
    0x0020: 0x0003,  # space
    0x0041: 0x0004,  # A
    0x0042: 0x000F,  # B
    0x0047: 0x0023,  # G
    0x004B: 0x0032,  # K
    0x004C: 0x0034,  # L
    0x004E: 0x003A,  # N
    0x004F: 0x0040,  # O
    0x0050: 0x004A,  # P
    0x0052: 0x004D,  # R
    0x0054: 0x0057,  # T
    0x0055: 0x005C,  # U
    0x0058: 0x006B,  # X
    0x0061: 0x0075,  # a
    0x0062: 0x0080,  # b
    0x0063: 0x0081,  # c
    0x0065: 0x008A,  # e
    0x0066: 0x0093,  # f
    0x006B: 0x00A4,  # k
    0x006E: 0x00AC,  # n
    0x006F: 0x00B2,  # o
    0x0072: 0x00BF,  # r
    0x0074: 0x00C9,  # t
    0x0075: 0x00CE,  # u
    0x007A: 0x00E3,  # z
    # Cyrillic uppercase
    0x0410: 0x00EB,  # А
    0x0411: 0x00EC,  # Б
    0x0412: 0x00ED,  # В
    0x0413: 0x00EE,  # Г
    0x0414: 0x00EF,  # Д
    0x0415: 0x00F0,  # Е
    0x0401: 0x00F1,  # Ё  (gap between Е 0x00F0 and Ж 0x00F2)
    0x0416: 0x00F2,  # Ж
    0x0417: 0x00F3,  # З  (inferred from alphabet sequence)
    0x0418: 0x00F4,  # И
    0x0419: 0x00F5,  # Й  (inferred)
    0x041A: 0x00F6,  # К
    0x041B: 0x00F7,  # Л
    0x041C: 0x00F8,  # М
    0x041D: 0x00F9,  # Н
    0x041E: 0x00FA,  # О
    0x041F: 0x00FB,  # П
    0x0420: 0x00FC,  # Р
    0x0421: 0x00FD,  # С
    0x0422: 0x00FE,  # Т
    0x0423: 0x00FF,  # У
    0x0424: 0x0100,  # Ф
    0x0425: 0x0101,  # Х
    0x0426: 0x0103,  # Ц
    0x0427: 0x0102,  # Ч
    0x0428: 0x0104,  # Ш
    0x0429: 0x0105,  # Щ
    0x042A: 0x0106,  # Ъ  (inferred)
    0x042B: 0x0107,  # Ы  (inferred)
    0x042C: 0x0108,  # Ь  (inferred)
    0x042D: 0x0109,  # Э
    0x042E: 0x010A,  # Ю  (inferred)
    0x042F: 0x010B,  # Я
    # Cyrillic lowercase
    0x0430: 0x010C,  # а
    0x0431: 0x010D,  # б
    0x0432: 0x010E,  # в
    0x0433: 0x010F,  # г
    0x0434: 0x0110,  # д
    0x0435: 0x0111,  # е
    0x0436: 0x0113,  # ж
    0x0437: 0x0114,  # з
    0x0438: 0x0115,  # и
    0x0439: 0x0116,  # й
    0x043A: 0x0117,  # к
    0x043B: 0x0118,  # л
    0x043C: 0x0119,  # м
    0x043D: 0x011A,  # н
    0x043E: 0x011B,  # о
    0x043F: 0x011C,  # п
    0x0440: 0x011D,  # р
    0x0441: 0x011E,  # с
    0x0442: 0x011F,  # т
    0x0443: 0x0120,  # у
    0x0444: 0x0121,  # ф
    0x0445: 0x0122,  # х
    0x0446: 0x0124,  # ц
    0x0447: 0x0123,  # ч
    0x0448: 0x0125,  # ш
    0x0449: 0x0126,  # щ
    0x044A: 0x0128,  # ъ  (gap between щ 0x0126 and ы 0x0129)
    0x044B: 0x0129,  # ы
    0x044C: 0x0127,  # ь
    0x044D: 0x012A,  # э
    0x044E: 0x012B,  # ю
    0x044F: 0x012C,  # я
    0x0451: 0x0112,  # ё
    # Digits
    0x0030: 0x0131,  # 0
    0x0031: 0x0132,  # 1
    0x0032: 0x0133,  # 2
    0x0033: 0x0134,  # 3
    0x0034: 0x0135,  # 4
    0x0035: 0x0136,  # 5
    0x0036: 0x0137,  # 6
    0x0037: 0x0138,  # 7
    0x0038: 0x0139,  # 8
    0x0039: 0x013A,  # 9
    # Punctuation / special
    0x002E: 0x0156,  # .
    0x002C: 0x0157,  # ,
    0x003A: 0x0158,  # :
    0x002A: 0x0161,  # *
    0x002F: 0x0163,  # /
    0x0028: 0x0165,  # (
    0x0029: 0x0166,  # )
    0x002D: 0x016B,  # -
    0x00AB: 0x0176,  # «
    0x00BB: 0x0177,  # »
    0x002B: 0x0186,  # +
    0x0040: 0x019F,  # @
    0x2116: 0x01AB,  # №
}

REGULAR_CID_TO_UNI: dict[int, int] = {v: k for k, v in REGULAR_UNI_TO_CID.items()}

# ── Unicode → CID (Medium, subset for bold text) ─────────────────────

MEDIUM_UNI_TO_CID: dict[int, int] = {
    0x0020: 0x0003,  # space
    # Cyrillic uppercase (CIDs match Regular — same subset numbering)
    0x0410: 0x00EB,  # А
    0x0411: 0x00EC,  # Б
    0x0412: 0x00ED,  # В
    0x0413: 0x00EE,  # Г
    0x0414: 0x00EF,  # Д
    0x0415: 0x00F0,  # Е
    0x0401: 0x00F1,  # Ё
    0x0416: 0x00F2,  # Ж
    0x0417: 0x00F3,  # З
    0x0418: 0x00F4,  # И
    0x0419: 0x00F5,  # Й
    0x041A: 0x00F6,  # К
    0x041B: 0x00F7,  # Л
    0x041C: 0x00F8,  # М
    0x041D: 0x00F9,  # Н
    0x041E: 0x00FA,  # О
    0x041F: 0x00FB,  # П
    0x0420: 0x00FC,  # Р
    0x0421: 0x00FD,  # С
    0x0422: 0x00FE,  # Т
    0x0423: 0x00FF,  # У
    0x0424: 0x0100,  # Ф
    0x0425: 0x0101,  # Х
    0x0426: 0x0103,  # Ц
    0x0427: 0x0102,  # Ч
    0x0428: 0x0104,  # Ш
    0x0429: 0x0105,  # Щ
    0x042A: 0x0106,  # Ъ
    0x042B: 0x0107,  # Ы
    0x042C: 0x0108,  # Ь
    0x042D: 0x0109,  # Э
    0x042E: 0x010A,  # Ю
    0x042F: 0x010B,  # Я
    # Cyrillic lowercase
    0x0430: 0x010C,  # а
    0x0431: 0x010D,  # б
    0x0432: 0x010E,  # в
    0x0433: 0x010F,  # г
    0x0434: 0x0110,  # д
    0x0435: 0x0111,  # е
    0x0451: 0x0112,  # ё
    0x0436: 0x0113,  # ж
    0x0437: 0x0114,  # з
    0x0438: 0x0115,  # и
    0x0439: 0x0116,  # й
    0x043A: 0x0117,  # к
    0x043B: 0x0118,  # л
    0x043C: 0x0119,  # м
    0x043D: 0x011A,  # н
    0x043E: 0x011B,  # о
    0x043F: 0x011C,  # п
    0x0440: 0x011D,  # р
    0x0441: 0x011E,  # с
    0x0442: 0x011F,  # т
    0x0443: 0x0120,  # у
    0x0444: 0x0121,  # ф
    0x0445: 0x0122,  # х
    0x0447: 0x0123,  # ч
    0x0446: 0x0124,  # ц
    0x0448: 0x0125,  # ш
    0x0449: 0x0126,  # щ
    0x044A: 0x0128,  # ъ
    0x044B: 0x0129,  # ы
    0x044C: 0x0127,  # ь
    0x044D: 0x012A,  # э
    0x044E: 0x012B,  # ю
    0x044F: 0x012C,  # я
    0x0030: 0x0131,  # 0
    0x0031: 0x0132,  # 1
    0x0032: 0x0133,  # 2
    0x0033: 0x0134,  # 3
    0x0034: 0x0135,  # 4
    0x0035: 0x0136,  # 5
    0x0036: 0x0137,  # 6
    0x0037: 0x0138,  # 7
    0x0038: 0x0139,  # 8
    0x0039: 0x013A,  # 9
    0x002E: 0x0156,  # .
    0x003A: 0x0158,  # :
}

MEDIUM_CID_TO_UNI: dict[int, int] = {v: k for k, v in MEDIUM_UNI_TO_CID.items()}

RUBL_CID_ALSRUBL = 0x0069
RUBL_CID_SYMBOLA = 0x0432

# ── Font width tables (CID → width in 1/1000 em units) ───────────────

REGULAR_WIDTHS: dict[int, int] = {}
_REGULAR_W_RAW = [
    (3, [190, 540]), (15, [573]), (35, [600]), (50, [559]),
    (58, [654]), (77, [553]), (87, [500]), (92, [624]),
    (117, [476]), (128, [518]), (129, [469]), (138, [481]),
    (147, [278]), (164, [454]), (172, [490]), (191, [325]),
    (201, [278]), (206, [485]),
    (235, [540, 561, 573, 492, 628, 530]),
    (244, [647]),
    (246, [559, 586, 745, 649, 633, 630, 549, 580, 500, 552, 756, 550]),
    (259, [652, 856, 869]),
    (267, [553, 476, 502, 490, 365, 496, 481]),
    (275, [629, 446, 515, 515, 454, 484, 650, 514, 500, 499, 522, 470, 403, 428, 691, 425]),
    (292, [518, 699]),
    (295, [461]),
    (297, [646, 473, 672, 467]),
    (305, [503, 520, 520, 520, 520, 520, 520, 520, 520, 520]),
    (342, [265, 265, 265]),
    (353, [419]),
    (355, [265]),
    (357, [265, 265]),
    (363, [362]),
    (374, [375, 375]),
    (390, [566]),
    (415, [598]),
    (427, [840]),
]
for _start, _ws in _REGULAR_W_RAW:
    for _j, _w in enumerate(_ws):
        REGULAR_WIDTHS[_start + _j] = _w

MEDIUM_WIDTHS: dict[int, int] = {}
_MEDIUM_W_RAW = [
    (3, [200]),
    (235, [577]),
    (237, [578]),
    (239, [655]),
    (244, [675]),
    (249, [657]),
    (252, [569, 600]),
    (268, [509]),
    (270, [515, 390, 589, 517]),
    (275, [735, 471, 561]),
    (279, [510, 523, 719, 548, 544, 536, 571, 513, 418]),
    (291, [506, 558]),
    (295, [488]),
    (299, [749, 520]),
    (305, [570, 509, 540, 540, 540, 540, 540, 540, 540, 540]),
    (342, [265]),
    (344, [249]),
]
for _start, _ws in _MEDIUM_W_RAW:
    for _j, _w in enumerate(_ws):
        MEDIUM_WIDTHS[_start + _j] = _w

DEFAULT_WIDTH = 500


# ── Encoding helpers ──────────────────────────────────────────────────

def encode_text(text: str, font: str = "regular") -> bytes:
    """Encode Unicode text to CID bytes (2 bytes per glyph, big-endian)."""
    table = REGULAR_UNI_TO_CID if font == "regular" else MEDIUM_UNI_TO_CID
    result = bytearray()
    for ch in text:
        cp = ord(ch)
        cid = table.get(cp)
        if cid is None:
            raise ValueError(f"Character '{ch}' (U+{cp:04X}) not in {font} CMap")
        result.append((cid >> 8) & 0xFF)
        result.append(cid & 0xFF)
    return bytes(result)


def decode_text(raw: bytes, font: str = "regular") -> str:
    """Decode CID bytes back to Unicode string."""
    table = REGULAR_CID_TO_UNI if font == "regular" else MEDIUM_CID_TO_UNI
    chars = []
    for i in range(0, len(raw) - 1, 2):
        cid = (raw[i] << 8) | raw[i + 1]
        uni = table.get(cid)
        chars.append(chr(uni) if uni else f"[{cid:04X}]")
    return "".join(chars)


def text_width_pt(text: str, font: str = "regular", font_size: float = 9.0) -> float:
    """Calculate text width in points for a given font and size."""
    uni_table = REGULAR_UNI_TO_CID if font == "regular" else MEDIUM_UNI_TO_CID
    w_table = REGULAR_WIDTHS if font == "regular" else MEDIUM_WIDTHS
    total = 0
    for ch in text:
        cp = ord(ch)
        cid = uni_table.get(cp, 0)
        total += w_table.get(cid, DEFAULT_WIDTH)
    return total * font_size / 1000


def get_unsupported_chars(text: str, font: str = "regular") -> set[str]:
    """Return set of characters from text that cannot be encoded."""
    table = REGULAR_UNI_TO_CID if font == "regular" else MEDIUM_UNI_TO_CID
    return {ch for ch in text if ord(ch) not in table}


def format_unsupported_error(bad_chars: set[str]) -> str:
    """Human-readable error for unsupported characters."""
    parts = [f"«{ch}» (U+{ord(ch):04X})" for ch in sorted(bad_chars)]
    return (
        f"Символы не поддерживаются шрифтом: {', '.join(parts)}.\n"
        "Попробуйте другой вариант или вернитесь в главное меню."
    )


FALLBACK_MAP: dict[str, str] = {
    "\u2011": "-",   # non-breaking hyphen
    "\u00A0": " ",   # non-breaking space
    # ё/Ё now have real CIDs — no fallback needed
}


def suggest_replacement(ch: str) -> Optional[str]:
    """Suggest a fallback character if available."""
    return FALLBACK_MAP.get(ch)


# ── PDF literal string escape/unescape ───────────────────────────────

def escape_pdf_literal(raw: bytes) -> bytes:
    """Escape raw bytes for use inside a PDF literal string (...)."""
    out = bytearray()
    for b in raw:
        if b == 0x5C:
            out += b"\\\\"
        elif b == 0x28:
            out += b"\\("
        elif b == 0x29:
            out += b"\\)"
        else:
            out.append(b)
    return bytes(out)


def unescape_pdf_literal(escaped: bytes) -> bytes:
    """Reverse PDF literal escaping."""
    out = bytearray()
    i = 0
    while i < len(escaped):
        if escaped[i] == 0x5C and i + 1 < len(escaped):
            nxt = escaped[i + 1]
            if nxt == 0x5C:
                out.append(0x5C)
            elif nxt == 0x28:
                out.append(0x28)
            elif nxt == 0x29:
                out.append(0x29)
            elif nxt == 0x6E:
                out.append(0x0A)
            elif nxt == 0x72:
                out.append(0x0D)
            elif nxt == 0x74:
                out.append(0x09)
            elif nxt == 0x66:
                out.append(0x0C)
            elif nxt == 0x62:
                out.append(0x08)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(escaped[i])
            i += 1
    return bytes(out)


TJ_REGEX = rb"\(((?:[^()\\]|\\.)*)\)Tj"
TM_REGEX = rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm"


def cid_advance_units(raw_cid: bytes, widths: dict[int, int] | None = None) -> int:
    """Sum font /W widths for a raw CID byte sequence (2 bytes per glyph)."""
    if widths is None:
        widths = REGULAR_WIDTHS
    total = 0
    for i in range(0, len(raw_cid) - 1, 2):
        cid = (raw_cid[i] << 8) | raw_cid[i + 1]
        total += widths.get(cid, DEFAULT_WIDTH)
    return total


def cid_width_pt(raw_cid: bytes, font_size: float, widths: dict[int, int] | None = None) -> float:
    """Width in points for a CID byte sequence at a given font size."""
    return cid_advance_units(raw_cid, widths) * font_size / 1000.0


def find_tj_at_coords(
    stream: bytes, target_y: float, target_x: float,
    tol_y: float = 1.5, tol_x: float = 8.0,
) -> tuple[bytes, int, int] | None:
    """Find a Tj near (target_x, target_y).

    Returns (raw_unescaped_cid_bytes, tj_match_start, tj_match_end) or None.
    """
    tms = list(re.finditer(TM_REGEX, stream))
    tjs = list(re.finditer(TJ_REGEX, stream))

    for tj in tjs:
        closest_tm = None
        for tm in tms:
            if tm.end() <= tj.start():
                closest_tm = tm
            else:
                break
        if closest_tm is None:
            continue
        x = float(closest_tm.group(1))
        y = float(closest_tm.group(2))
        if abs(y - target_y) < tol_y and abs(x - target_x) < tol_x:
            escaped_content = tj.group(1)
            raw = unescape_pdf_literal(escaped_content)
            return raw, tj.start(), tj.end()
    return None


# ── Runtime font width extraction from PDF ────────────────────────────

def _parse_w_array(pdf_bytes: bytes, start: int) -> dict[int, int]:
    """Parse a /W array starting at position `start` (right after the '[')."""
    text = pdf_bytes.decode("latin-1", errors="replace")
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
        i += 1
    w_str = text[start:i - 1]
    tokens = []
    for t in re.finditer(r"\[([^\]]*)\]|(\d+)", w_str):
        if t.group(1) is not None:
            tokens.append(("arr", [int(float(x)) for x in t.group(1).split()]))
        else:
            tokens.append(("num", int(t.group(2))))
    widths: dict[int, int] = {}
    j = 0
    while j < len(tokens):
        if tokens[j][0] == "num":
            if j + 1 < len(tokens) and tokens[j + 1][0] == "arr":
                cid = tokens[j][1]
                for k, w in enumerate(tokens[j + 1][1]):
                    widths[cid + k] = w
                j += 2
            else:
                j += 1
        else:
            j += 1
    return widths


def extract_pdf_font_widths(pdf_bytes: bytes) -> tuple[dict[int, int], dict[int, int]]:
    """Extract F1 (Regular) and F2 (Medium) width tables from a T-Bank PDF.

    Associates /W arrays with font names by inspecting nearby /BaseFont entries.
    Returns (regular_widths, medium_widths) with fallback to hardcoded tables.
    """
    regular_w: dict[int, int] = {}
    medium_w: dict[int, int] = {}

    for m in re.finditer(rb"/W\s*\[", pdf_bytes):
        chunk_before = pdf_bytes[max(0, m.start() - 600):m.start()]
        name_m = re.search(rb"/BaseFont\s*/([^\s/\]>]+)", chunk_before)
        font_name = name_m.group(1).decode("latin-1") if name_m else ""

        widths = _parse_w_array(pdf_bytes, m.end())
        if not widths:
            continue

        if "Medium" in font_name:
            medium_w = widths
        elif "Regular" in font_name or not regular_w:
            regular_w = widths

    return regular_w or REGULAR_WIDTHS, medium_w or MEDIUM_WIDTHS


def can_encode_in_font(text: str, font: str, font_widths: dict[int, int]) -> bool:
    """Check if ALL characters in text can be rendered in the font subset.

    A character is renderable only if its CID has a glyph in the font
    (approximated by having an entry in the /W table or being CID 3 = space).
    """
    uni_table = REGULAR_UNI_TO_CID if font == "regular" else MEDIUM_UNI_TO_CID
    for ch in text:
        cid = uni_table.get(ord(ch))
        if cid is None:
            return False
        if cid != 3 and cid not in font_widths:
            return False
    return True


# Legacy wrappers for backward compatibility
def extract_cmap_from_pdf(pdf_bytes: bytes) -> dict[int, int]:
    """Parse all ToUnicode CMaps from a PDF. Returns Unicode→CID mapping."""
    stream_pattern = rb"stream\r?\n(.*?)\r?\nendstream"
    uni_to_cid: dict[int, int] = {}
    for m in re.finditer(stream_pattern, pdf_bytes, re.DOTALL):
        raw = m.group(1)
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = raw
        text = data.decode("latin-1", errors="replace")
        if "beginbfrange" not in text:
            continue
        for bfm in re.finditer(
            r"<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>", text
        ):
            cid_start = int(bfm.group(1), 16)
            cid_end = int(bfm.group(2), 16)
            uni_start = int(bfm.group(3), 16)
            for i in range(cid_start, cid_end + 1):
                uni_to_cid[uni_start + (i - cid_start)] = i
    return uni_to_cid


def extract_font_widths(pdf_bytes: bytes) -> dict[int, int]:
    """Extract the first /W array from a CIDFont object."""
    for m in re.finditer(rb"/W\s*\[", pdf_bytes):
        widths = _parse_w_array(pdf_bytes, m.end())
        if widths:
            return widths
    return {}
