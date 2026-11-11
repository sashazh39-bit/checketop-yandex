#!/usr/bin/env python3
"""Patch an Alfa-Bank BI Publisher transfer receipt.

The donor (a real ``Квитанция о переводе на счёт``) is kept: images
(including the round seal), the Tahoma subset, the Info dictionary
(``Oracle BI Publisher``, no dates) and the classic xref. Only the
text values change. Glyphs missing from the donor subset are copied
from another genuine Alfa receipt of the same Tahoma build into
unused CID slots, so the font stays the bank's own.
"""

from __future__ import annotations

import os
import re
import sys
import zlib
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rocket"))

import rocket_font  # noqa: E402
import rocket_pdf  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402

CONTENT_OBJ = 9
FONT_OBJ = 13
CIDFONT_OBJ = 11
TOUNICODE_OBJ = 14


def _cmap(pdf, obj):
    return rocket_font.parse_tounicode(pdf.obj(obj).decoded_stream())


def _encode(text: str, char_to_cid: dict) -> str:
    missing = sorted({c for c in text if c not in char_to_cid})
    if missing:
        raise KeyError("no glyph for " + "".join(missing))
    return "".join(f"{char_to_cid[c]:04X}" for c in text)


def _append_glyphs(donor_ttf: bytes, src_ttf: bytes, src_cids: list[int]) -> tuple[bytes, list[int]]:
    """Append simple glyphs. Donor CIDs 2, 4, 5, … are composites whose
    outlines live in glyphs 65–82; overwriting those slots redraws every
    existing letter. New characters get fresh glyph ids past the end.
    """
    import copy
    from fontTools.ttLib.tables._g_l_y_f import Glyph

    font = TTFont(BytesIO(donor_ttf))
    src = TTFont(BytesIO(src_ttf))
    order = list(font.getGlyphOrder())
    src_order = src.getGlyphOrder()
    new_ids = []
    for src_cid in src_cids:
        srcg = src["glyf"][src_order[src_cid]]
        if srcg.numberOfContours < 0:
            raise RuntimeError(f"source CID {src_cid} is a composite")
        new_id = len(order)
        name = f"glyph{new_id:05d}"
        glyph = Glyph()
        glyph.numberOfContours = srcg.numberOfContours
        glyph.coordinates = copy.deepcopy(srcg.coordinates)
        glyph.endPtsOfContours = list(srcg.endPtsOfContours)
        glyph.flags = bytearray(srcg.flags)
        glyph.program = copy.deepcopy(srcg.program)
        glyph.xMin, glyph.yMin = srcg.xMin, srcg.yMin
        glyph.xMax, glyph.yMax = srcg.xMax, srcg.yMax
        order.append(name)
        font["glyf"].glyphs[name] = glyph
        font["hmtx"].metrics[name] = src["hmtx"].metrics[src_order[src_cid]]
        new_ids.append(new_id)
    font.setGlyphOrder(order)
    font["maxp"].numGlyphs = len(order)
    out = BytesIO()
    font.save(out)
    return out.getvalue(), new_ids


def _width_units(src_ttf: bytes, cid: int) -> int:
    font = TTFont(BytesIO(src_ttf))
    name = font.getGlyphOrder()[cid]
    advance, _lsb = font["hmtx"].metrics[name]
    upem = font["head"].unitsPerEm
    return int(round(advance * 1000 / upem))


def _append_widths(dict_bytes: bytes, updates: dict[int, int]) -> bytes:
    text = dict_bytes.decode("latin-1")
    extra = " ".join(f"{cid} [{width}]" for cid, width in sorted(updates.items()))
    marker = " ]"
    if marker not in text:
        raise RuntimeError("CID width array has no end")
    return text.replace(marker, " " + extra + marker, 1).encode("latin-1")


def _extend_tounicode(stream: bytes, extra: dict[int, str]) -> bytes:
    text = stream.decode("latin-1")
    count = int(re.search(r"(\d+) beginbfchar", text).group(1))
    lines = "\n".join(f"<{cid:04X}> <{ord(ch):04X}>" for cid, ch in sorted(extra.items()))
    text = text.replace(f"{count} beginbfchar", f"{count + len(extra)} beginbfchar", 1)
    text = text.replace("endbfchar", lines + "\nendbfchar", 1)
    return text.replace("\n", "\r\n").encode("latin-1")


def _replace_runs(content: bytes, swaps: list[tuple[str, str]], char_to_cid: dict) -> bytes:
    text = content.decode("latin-1")
    for old, new in swaps:
        old_hex = _encode(old, char_to_cid)
        new_hex = _encode(new, char_to_cid)
        needle = f"<{old_hex}>"
        if text.count(needle) != 1:
            raise RuntimeError(f"{old!r} occurs {text.count(needle)} times")
        text = text.replace(needle, f"<{new_hex}>", 1)
    return text.encode("latin-1")


def _emit(obj: rocket_pdf.PdfObject) -> bytes:
    body = bytearray(f"{obj.num} {obj.gen} obj\r\n".encode())
    body += obj.dict_bytes
    if obj.stream_raw is not None:
        body += b"\r\nstream\r\n"
        body += obj.stream_raw
        body += b"\r\nendstream"
    body += b"\r\nendobj\r\n"
    return bytes(body)


def _save(pdf: rocket_pdf.PdfFile, path: str, trailer_id: bytes) -> None:
    chunks = [b"%PDF-1.6\r\n"]
    offsets = {}
    cursor = len(chunks[0])
    for num in pdf.order:
        offsets[num] = cursor
        blob = _emit(pdf.obj(num))
        chunks.append(blob)
        cursor += len(blob)
    size = max(pdf.objects) + 1
    xref = [f"xref\n0 {size}\n".encode(), b"0000000000 65535 f \n"]
    for num in range(1, size):
        if num in offsets:
            xref.append(f"{offsets[num]:010d} 00000 n \n".encode())
        else:
            xref.append(b"0000000000 65535 f \n")
    ident = trailer_id.decode()
    trailer = (
        "trailer\n<<\n/Size "
        + str(size)
        + "\n/Root 1 0 R\n/Info 2 0 R\n/ID [<"
        + ident
        + "><"
        + ident
        + ">]\n>>\nstartxref\n"
        + str(cursor)
        + "\n%%EOF\n"
    ).encode()
    with open(path, "wb") as handle:
        handle.write(b"".join(chunks) + b"".join(xref) + trailer)


def build(donor_path: str, output_path: str, glyph_source: str, replacements: list[tuple[str, str]]) -> None:
    pdf = rocket_pdf.load(donor_path)
    src = rocket_pdf.load(glyph_source)
    cmap = _cmap(pdf, TOUNICODE_OBJ)
    src_cmap = _cmap(src, 13)
    char_to_cid = {ch: cid for cid, ch in cmap.items()}
    src_char = {ch: cid for cid, ch in src_cmap.items()}

    needed = sorted({c for _o, n in replacements for c in n if c not in char_to_cid})
    extra = {}
    src_ttf = src.obj(12).decoded_stream()
    if needed:
        missing_src = [ch for ch in needed if ch not in src_char]
        if missing_src:
            raise RuntimeError("glyph source has no " + "".join(missing_src))
        src_cids = [src_char[ch] for ch in needed]
        ttf, new_ids = _append_glyphs(pdf.obj(FONT_OBJ).decoded_stream(), src_ttf, src_cids)
        width_updates = {}
        for ch, slot in zip(needed, new_ids):
            char_to_cid[ch] = slot
            extra[slot] = ch
            width_updates[slot] = _width_units(src_ttf, src_char[ch])
        font_obj = pdf.obj(FONT_OBJ)
        font_obj.dict_bytes = re.sub(
            rb"/Length1 \d+", f"/Length1 {len(ttf)}".encode(), font_obj.dict_bytes, count=1
        )
        font_obj.set_decoded_stream(ttf)
        pdf.obj(CIDFONT_OBJ).dict_bytes = _append_widths(pdf.obj(CIDFONT_OBJ).dict_bytes, width_updates)
        pdf.obj(TOUNICODE_OBJ).set_decoded_stream(
            _extend_tounicode(pdf.obj(TOUNICODE_OBJ).decoded_stream(), extra)
        )

    content = _replace_runs(pdf.obj(CONTENT_OBJ).decoded_stream(), replacements, char_to_cid)
    pdf.obj(CONTENT_OBJ).set_decoded_stream(content)

    # Same metadata shape as BI Publisher: Producer only, paired 32-hex /ID.
    # A new /ID so the file is not the donor's fingerprint; Info is untouched.
    new_id = os.urandom(16).hex().encode()
    _save(pdf, output_path, new_id)


def from_second_receipt(donor_path: str, output_path: str, glyph_source: str) -> None:
    """Donor layout and seal, field values taken from the second receipt."""
    build(
        donor_path,
        output_path,
        glyph_source,
        [
            ("10.11.2026 17:18 мск", "10.11.2026 17:38 мск"),
            ("Исполнен", "Принята к исполнению"),
            ("11 420 RUR", "25 452 RUR"),
            ("10.11.2026 16:36:44 мск", "10.11.2026 17:38:01 мск"),
            ("C201011260059292", "C201011260065055"),
            ("360994", "500604"),
            ("Ефременко Елена Викторовна", "САЖАЕВ ЕВГЕНИЙ АЛЕКСАНДРОВИЧ (ИП)"),
            ("40817810051045147810", "40802810616560001142"),
            ('ПАО "Банк ПСБ"', 'АО "АЛЬФА-БАНК"'),
            ("044525555", "044525593"),
            ("30101810400000000555", "30101810200000000593"),
            ("Оплата заказа #4993761; Ефременко Елена ", "Оплата заказа #4994096"),
            ("Викторовна", "ИНН 424603089962"),
        ],
    )


if __name__ == "__main__":
    donor = sys.argv[1] if len(sys.argv) > 1 else "/Users/aleksandrzerebatav/Downloads/AM_1794320287017.pdf"
    source = sys.argv[2] if len(sys.argv) > 2 else "/Users/aleksandrzerebatav/Downloads/AM_1794321522798.pdf"
    output = sys.argv[3] if len(sys.argv) > 3 else "/Users/aleksandrzerebatav/Desktop/AM_receipt_with_seal.pdf"
    from_second_receipt(donor, output, source)
    print(output, os.path.getsize(output))
