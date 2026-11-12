# -*- coding: utf-8 -*-
"""Single-field editor for Ozon Bank SBP receipts.

Strategy: when the bank stores the receipt ID in a DB and verifies all 32
characters byte-for-byte, the safest route is to take a REAL genuine PDF
(~1300 in our corpus) and rewrite exactly ONE visible field (amount, recipient,
or phone number), leaving the entire rest of the file untouched — including
the SBP operation ID (MCID 21), the Creator/Producer/CreationDate/ModDate
block (Info object 1), the embedded font subsets (F4/F5 fontfiles + their
sfnt table checksums), the Skia /W widths, the ToUnicode CMaps, the trailer
dict, and all static drawing operators in the content stream.

Mechanics:
  1. Parse the input PDF with ozon_generator._Tpl (donor-template parser).
  2. Locate the MCID block(s) carrying the target field in the content
     stream (object 9):
       * amount    -> MCID 3 (F5 24pt Итого value) + MCID 9 (F4 14pt Сумма row)
       * recipient -> MCID 13
       * phone     -> MCID 15
     Dates could be supported the same way via MCID 1 (not exposed yet; the
     spec only requires amount / recipient / phone).
  3. Re-emit each target block with ozon_generator._build_block, which
     reuses the donor's own sfntly subset (no font rewrite!) and the
     harvested Td pair atlas for Skia-correct kerning.
  4. Deflate the new content stream ONLY via Java Deflater (JZComp,
     level 6, zlib wrapper 78 9c).  Python ``zlib`` is NEVER used here:
     it emits a different deflate dialect that provenance checks flag
     as foreign (see DOCS/README: CPython zlib dialect is rejected).
     If Java/JZComp is unavailable the edit fails loudly — no silent
     Python-zlib fallback, ever.
  5. Splice the new object 9 back into the raw PDF bytes; every other object
     (Info, fontfiles, FontDescriptor, ToUnicode, catalog, page) stays
     byte-identical.
  6. Rewrite /Length on the stream dict, patch the xref offsets for every
     object that physically moved, and fix the startxref value.  The xref
     layout itself is preserved — same row count, same '0000000000 65535 f'
     free entry, same '00000 n' generations, same '\\n' line terminators.

Public API: ``edit_amount``, ``edit_recipient``, ``edit_phone`` all accept
raw PDF bytes and a new value and return raw PDF bytes.  They raise
``ValueError`` when the donor's font subset does not cover a glyph required
by the new text (the spec forbids rewriting the fonts: anything outside the
donor's subset is impossible without breaking byte-equality on /FontFile2).

Supported input is the 86-object Skia/PDF m105 layout parsed by
``ozon_generator._Tpl`` (Root 77; see ``_ensure_supported_layout``).
Other genuine variants (e.g. 90-object receipts with a "Сообщение"
field) are refused with a clear ``ValueError`` — never silently edited.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zlib  # only for verifying JZComp output decompresses; never to compress

# Import strictly from ozon_generator — this file DOES NOT duplicate any of
# its logic.  We rely on its parsed donor template and its _build_block
# emitter (Skia glyph-per-Tj with HarfBuzz-shaped Td advances).
from ozon_generator import (
    _Tpl,
    _build_block,
    _obj_bytes,
    format_amount,
    format_phone,
)

# MCID -> which field it carries in a genuine Ozon "Перевод СБП" receipt.
# These MCIDs are stable across the 01-07.11.2026 corpus (85-object Skia
# skeleton).  MCID 3 is the big Итого value (F5 24pt), MCID 9 is the Сумма
# row value (F4 14pt) — both change when the amount changes.
_MCID_DATE = 1
_MCID_AMOUNT_HEAD = 3    # F5, "Итого"
_MCID_AMOUNT_ROW = 9     # F4, "Сумма"
_MCID_RECIPIENT = 13     # F4
_MCID_PHONE = 15         # F4
_MCID_BANK = 17          # F4
_MCID_OPID = 21          # F4 — MUST stay byte-identical, never rewritten

# Object number of the first (and only) content stream in Skia's Ozon layout.
_CONTENT_OBJ = 9

# Deflate: ONLY Java Deflater via JZComp (level 6).  Genuine Ozon streams
# carry the zlib wrapper 78 9c; Java Deflater level 6 emits the same wrapper
# and header, while CPython zlib — byte-different dialect — is a provenance
# fingerprint and is forbidden here (project law for every bank).
_DEFLATE_LEVEL = 6
_EDITOR_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_jzcomp_dir():
    for d in (os.environ.get("JZCOMP_DIR"), _EDITOR_DIR,
              os.path.join(_EDITOR_DIR, "wb_project"),
              "/opt/mts-bot", os.getcwd()):
        if d and os.path.exists(os.path.join(d, "JZComp.class")):
            return d
    return None


def _find_java():
    for j in (os.environ.get("JAVA_BIN"),
              r"C:\Users\nemor\AppData\Local\Programs\Microsoft\jdk-17.0.17.10-hotspot\bin\java.exe",
              shutil.which("java")):
        if j and (os.path.exists(j) or shutil.which(j)):
            return j
    return None


def _java_deflate(data: bytes) -> bytes:
    cp = _find_jzcomp_dir()
    if cp is None:
        raise RuntimeError(
            "JZComp.class not found (searched JZCOMP_DIR, editor dir, "
            "wb_project/, /opt/mts-bot, cwd); Java Deflater is mandatory "
            "for Ozon edit, no Python zlib fallback")
    java = _find_java()
    if java is None:
        raise RuntimeError(
            "java executable not found (set JAVA_BIN); Java Deflater is "
            "mandatory for Ozon edit, no Python zlib fallback")
    fd_in, inn = tempfile.mkstemp(prefix="ozed_jz_in_", suffix=".bin")
    fd_out, out = tempfile.mkstemp(prefix="ozed_jz_out_", suffix=".bin")
    try:
        with os.fdopen(fd_in, "wb") as fh:
            fh.write(data)
        os.close(fd_out)
        subprocess.check_call(
            [java, "-cp", cp, "JZComp", inn, out, str(_DEFLATE_LEVEL)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out, "rb") as fh:
            comp = fh.read()
    finally:
        for p in (inn, out):
            try:
                os.unlink(p)
            except OSError:
                pass
    if len(comp) < 2 or (comp[0] & 0x0F) != 8 or ((comp[0] << 8) + comp[1]) % 31 != 0:
        raise RuntimeError("JZComp output is not a valid zlib stream (bad CMF/FLG)")
    if zlib.decompress(comp) != data:
        raise RuntimeError("JZComp output does not round-trip to the content stream")
    return comp


# --------------------------------------------------------------------- utils
def _deflate(data: bytes) -> bytes:
    """Java Deflater (JZComp) ONLY. Never falls back to Python zlib."""
    return _java_deflate(data)


# Supported layout: the 86-object Skia/PDF m105 skeleton that
# ozon_generator._Tpl parses (Root 77, F4 font objects 80/79/78/81,
# F5 84/83/82/85).  Genuine variants with extra fields (e.g. a
# "Сообщение" block, 90 objects, Root 81, font objects shifted by +4)
# shift MCID numbering and cannot be edited without rewriting fonts —
# they are refused loudly instead of crashing inside _Tpl with a
# cryptic "subsection not found" or, worse, emitting a corrupt PDF.
_SUPPORTED_FONT_OBJS = {"F4": (4, 80, 79, 78, 81), "F5": (5, 84, 83, 82, 85)}


def _font_obj_refs(raw: bytes, type0_num: int):
    """(type0, descendant, descriptor, fontfile, tounicode) for a Type0 font."""
    body = _obj_bytes(raw, type0_num)
    descendant = int(re.search(rb"/DescendantFonts \[(\d+) 0 R\]", body).group(1))
    tounicode = int(re.search(rb"/ToUnicode (\d+) 0 R", body).group(1))
    desc_body = _obj_bytes(raw, descendant)
    descriptor = int(re.search(rb"/FontDescriptor (\d+) 0 R", desc_body).group(1))
    fd_body = _obj_bytes(raw, descriptor)
    fontfile = int(re.search(rb"/FontFile2 (\d+) 0 R", fd_body).group(1))
    return (type0_num, descendant, descriptor, fontfile, tounicode)


def _ensure_supported_layout(raw: bytes) -> None:
    """Raise ValueError unless `raw` is the supported 86-object layout."""
    try:
        root_m = list(re.finditer(rb"/Root (\d+) 0 R", raw))
        root = int(root_m[-1].group(1)) if root_m else None
        refs = {face: _font_obj_refs(raw, num)
                for face, (num, *_rest) in _SUPPORTED_FONT_OBJS.items()}
    except Exception:
        refs, root = {}, None
    if root != 77 or refs != _SUPPORTED_FONT_OBJS:
        raise ValueError(
            "Неподдерживаемый вариант чека Ozon: нужен 86-объектный "
            "Skia/PDF m105 (Root 77, шрифты F4/F5 = объекты 78–85); "
            "варианты с доп. полями (напр. «Сообщение», 90 объектов) "
            "редактор не меняет — иначе пришлось бы переписывать шрифты "
            "и ID операции.")


def _tpl_from_bytes(pdf_bytes: bytes) -> _Tpl:
    """Build a donor template from raw bytes (ozon_generator._Tpl reads from disk).

    Uses a short-lived temp file rather than duplicating _Tpl.__init__ so that
    any future fix to the parser propagates here automatically.
    """
    _ensure_supported_layout(pdf_bytes)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="ozed_")
    try:
        os.write(fd, pdf_bytes)
        os.close(fd)
        return _Tpl(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _find_obj_span(raw: bytes, num: int) -> tuple:
    """Return (start, end) byte span of 'N 0 obj ... endobj\\n' in raw PDF."""
    m = re.search(("(?m)^%d 0 obj" % num).encode("latin-1"), raw)
    if m is None:
        raise ValueError("object %d not found" % num)
    end = raw.find(b"endobj", m.end())
    if end < 0:
        raise ValueError("endobj not found for object %d" % num)
    end += len(b"endobj")
    # Skia writes 'endobj\n' — consume that trailing newline too, so the
    # spliced replacement carries its own terminator and the following
    # object starts exactly where it did before (modulo the length delta).
    if raw[end:end + 1] == b"\n":
        end += 1
    elif raw[end:end + 2] == b"\r\n":
        end += 2
    return m.start(), end


def _replace_content_stream(raw: bytes, new_inflated: bytes) -> bytes:
    """Rebuild object 9 with a new inflated content stream.

    Preserves the exact dict layout used by Skia for Ozon receipts
    ('<</Filter /FlateDecode\\n/Length N>> stream\\n<comp>\\nendstream\\nendobj\\n')
    so that only /Length and the compressed payload change inside obj 9.
    """
    start, end = _find_obj_span(raw, _CONTENT_OBJ)
    comp = _deflate(new_inflated)
    new_obj = (
        b"%d 0 obj\n" % _CONTENT_OBJ
        + b"<</Filter /FlateDecode\n/Length %d>> stream\n" % len(comp)
        + comp
        + b"\nendstream\nendobj\n"
    )
    return raw[:start] + new_obj + raw[end:]


def _fix_xref(raw: bytes) -> bytes:
    """Rewrite every xref offset and startxref after any object moved.

    Ozon receipts use a classic (non-stream) xref table emitted by Skia:
      xref
      0 86
      0000000000 65535 f \n
      0000000015 00000 n \n
      ...
      trailer
      <</Size 86 /Root 77 0 R /Info 1 0 R>>
      startxref
      62300
      %%EOF
    We keep the table shape and only patch the 10-digit offsets.
    """
    data = bytearray(raw)
    # map obj-num -> byte offset of its 'N 0 obj' line
    offsets = {}
    for m in re.finditer(rb"(?m)^(\d+) 0 obj\b", bytes(data)):
        offsets[int(m.group(1))] = m.start()
    xref_m = re.search(rb"(?m)^xref\s*$", bytes(data))
    if xref_m is None:
        raise ValueError("xref table not found")
    p = xref_m.end()
    if data[p:p + 1] == b"\r":
        p += 1
    if data[p:p + 1] == b"\n":
        p += 1
    # 'first count' header line
    hdr_end = bytes(data).find(b"\n", p)
    first, count = (int(x) for x in bytes(data[p:hdr_end]).split())
    entries_start = hdr_end + 1
    for i in range(count):
        obj_num = first + i
        row = entries_start + i * 20
        entry = bytes(data[row:row + 20])
        if len(entry) < 20:
            break
        if entry[17:18] != b"n":  # free entry ('f') — leave alone
            continue
        if obj_num in offsets:
            data[row:row + 10] = ("%010d" % offsets[obj_num]).encode("latin-1")
    # startxref: locate the '^xref' table start (pre-patch position matches
    # post-patch since the xref table is the LAST object-region in the file,
    # so its offset might change only if the content stream grew/shrank).
    new_xref_off = re.search(rb"(?m)^xref\s*$", bytes(data)).start()
    sx = bytes(data).rfind(b"startxref")
    if sx == -1:
        raise ValueError("startxref not found")
    num_start = sx + len(b"startxref")
    while data[num_start:num_start + 1] in (b" ", b"\n", b"\r"):
        num_start += 1
    num_end = num_start
    while num_end < len(data) and data[num_end:num_end + 1].isdigit():
        num_end += 1
    data[num_start:num_end] = str(new_xref_off).encode("latin-1")
    return bytes(data)


# ----------------------------------------------------------------- rewriting
def _rewrite_blocks(tpl: _Tpl, replacements: dict) -> str:
    """Rebuild the full content stream text, re-emitting only the listed MCIDs."""
    out = []
    pos = 0
    for b in sorted(tpl.blocks, key=lambda bb: bb["span"][0]):
        out.append(tpl.content[pos:b["span"][0]])
        if b["mcid"] in replacements and replacements[b["mcid"]] != b["text"]:
            cm = tpl.fonts[b["font"]]["char2cid"]
            missing = [c for c in replacements[b["mcid"]] if c not in cm]
            if missing:
                raise ValueError(
                    "donor %s lacks %s glyph(s) for MCID %d: %r"
                    % (os.path.basename(tpl.path), b["font"], b["mcid"], missing)
                )
            out.append(_build_block(tpl, b, replacements[b["mcid"]], char2cid=cm))
        else:
            out.append(b["body"])
        pos = b["span"][1]
    out.append(tpl.content[pos:])
    return "".join(out)


def _apply(pdf_bytes: bytes, replacements: dict) -> bytes:
    """Core edit path: parse -> re-emit target MCIDs -> deflate -> splice -> fix xref."""
    if not replacements:
        return pdf_bytes
    tpl = _tpl_from_bytes(pdf_bytes)
    # Sanity: the SBP ID (MCID 21) is NEVER touched by this editor.  Any
    # caller that tries to put it in the replacements map is a bug — reject
    # loudly so no one accidentally breaks byte-equality on the one field
    # Ozon cross-checks against its own database.
    if _MCID_OPID in replacements:
        raise ValueError("MCID %d (SBP ID) must not be rewritten" % _MCID_OPID)
    new_content = _rewrite_blocks(tpl, replacements)
    if new_content == tpl.content:
        # Nothing actually changed — return input untouched.  Keeps the
        # idempotency guarantee callers rely on when batch-editing.
        return pdf_bytes
    raw = tpl.raw
    new_raw = _replace_content_stream(raw, new_content.encode("latin-1"))
    new_raw = _fix_xref(new_raw)
    return new_raw


# --------------------------------------------------------------- public API
def edit_amount(pdf_bytes: bytes, new_amount) -> bytes:
    """Return a PDF where only the amount (MCID 3 + MCID 9) has been changed.

    Accepts int, float, Decimal, or a preformatted string.  Numeric values
    are formatted with ozon_generator.format_amount (NBSP-free thin-space
    thousands separator, trailing ' ₽').  Everything else — ID, dates,
    fonts, static chrome — stays byte-identical.
    """
    if isinstance(new_amount, str):
        amount_s = new_amount
    else:
        amount_s = format_amount(new_amount)
    return _apply(pdf_bytes, {_MCID_AMOUNT_HEAD: amount_s, _MCID_AMOUNT_ROW: amount_s})


def edit_recipient(pdf_bytes: bytes, new_name: str) -> bytes:
    """Return a PDF where only the recipient name (MCID 13) has been changed."""
    if not isinstance(new_name, str):
        raise TypeError("new_name must be str")
    return _apply(pdf_bytes, {_MCID_RECIPIENT: new_name})


def edit_phone(pdf_bytes: bytes, new_phone: str) -> bytes:
    """Return a PDF where only the recipient phone (MCID 15) has been changed.

    Accepts any input that format_phone can normalize (10/11-digit strings,
    with or without punctuation); otherwise pass the exact display string.
    """
    if not isinstance(new_phone, str):
        raise TypeError("new_phone must be str")
    phone_s = format_phone(new_phone)
    return _apply(pdf_bytes, {_MCID_PHONE: phone_s})
