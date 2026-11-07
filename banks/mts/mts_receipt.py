#!/usr/bin/env python3
"""Receipt model for MTS Bank SBP receipts.

Parses a genuine MTS donor PDF (OpenPDF 1.3.30.jaspersoft.2 output,
see banks/mts/BANK.md), exposes its fields, and re-emits the file with
new field values as a *single-pass* PDF that is structurally
indistinguishable from a fresh bank export:

* same objects in the same order with the same numbers (4..24 images,
  3 logo form, 27/28 QR-ish image, 29 content, 53/56 + 57/60 fonts,
  63 catalog, 64 info, 31 ObjStm, 65 xref stream);
* font subsets rebuilt from the recovered v1.0 MTSSans tables with the
  exact OpenPDF algorithm (``mts_font``), fresh random 6-letter subset
  tags, /W and ToUnicode pruned to exactly the new text;
* every value re-right-aligned to x = 355 - width (verified against all
  genuine donors), the SBP id wrapped at the 167 pt box like the
  original;
* the /Names tree (Jaspersoft's per-field named destinations, keys are
  the field texts mangled to low bytes) re-sorted and re-numbered the
  OpenPDF way;
* Info: CreationDate/ModDate = export moment (UTC, ``Z``), fresh
  CorrelationId; trailer /ID fresh and random;
* saved in ONE pass -- one xref stream, one ``%%EOF``, no ``/Prev``,
  no incremental tail, no padding.

Usage::

    receipt = Receipt("donors/mts_donor_1.pdf", pool)
    receipt.set("amount", "100,00 ₽")
    receipt.save("out/Unknown.pdf")
"""
from __future__ import annotations

import re
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from mts_pdf import fmt_double, escape_string, pdf_string, compress, mangle_name_key
from mts_font import (
    EmbeddedSubset, FontPool, FontCoverageError,
    build_subset, build_tounicode, build_w_array,
)

__all__ = ["Receipt", "ReceiptError", "MSK"]

_MSK_TZ = timezone(__import__("datetime").timedelta(hours=3))
MSK = _MSK_TZ


class ReceiptError(Exception):
    """Fatal problem with the donor or the requested field values."""


# --------------------------------------------------------------------------
# fixed layout (measured on the genuine donors, BANK.md)
# --------------------------------------------------------------------------

PAGE_WIDTH = 375.0
RIGHT_EDGE = 355.0           # all values right-align here (page 375 - 20)
SBP_WRAP_WIDTH = 167.0       # SBP element box width in pt (188..355)
REGULAR_SIZE = 12.0
MEDIUM_SIZE = 20.0

# row baseline y (PDF coords) per field, and the dest y offset (+size)
ROW_Y = {
    "datetime": 643.35,
    "recipient": 597.35,
    "phone": 565.35,
    "bank": 533.35,
    "message": 501.35,
    "account": 469.35,
    "amount": 437.35,
    "fee": 405.35,
    "txcode": 373.35,
    "sbp": 341.35,
    "sbp2": 325.77,
    "total": 260.58,
}
FIELD_LABELS = {
    "recipient": "Получатель",
    "phone": "Телефон получателя",
    "bank": "Банк получателя",
    "message": "Сообщение",
    "account": "Счет списания",
    "amount": "Сумма",
    "fee": "Комиссия",
    "txcode": "Код транзакции",
    "sbp": "Код операции СБП",
    "total": "Итого",
}
LABEL_ROW = {  # label field -> row key in ROW_Y
    "recipient": "recipient", "phone": "phone", "bank": "bank",
    "message": "message", "account": "account", "amount": "amount",
    "fee": "fee", "txcode": "txcode", "sbp": "sbp", "total": "total",
}
PAGE_ANCHOR = "JR_PAGE_ANCHOR_0_1"

# content-run indices (order of (...)Tj emissions in a genuine stream)
RUN_HEADER = 1
RUN_VALUES = {  # run index -> field
    3: "recipient", 5: "phone", 7: "bank", 9: "message", 11: "account",
    13: "amount", 15: "fee", 17: "txcode", 19: "sbp", 20: "sbp2",
    22: "total",
}
RUN_LABELS = {  # run index -> label field
    2: "recipient", 4: "phone", 6: "bank", 8: "message", 10: "account",
    12: "amount", 14: "fee", 16: "txcode", 18: "sbp", 21: "total",
}

_TJ_RE = re.compile(rb"\(((?:[^()\\]|\\.)*)\)Tj")
_TM_RE = re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")


def _unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x5C and i + 1 < len(raw):
            nxt = raw[i + 1]
            simple = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C}
            if nxt in simple:
                out.append(simple[nxt])
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


class _Run:
    """One (...)Tj emission inside the content stream."""
    __slots__ = ("fixed_a", "x", "fixed_b", "raw", "font", "size")

    def __init__(self, fixed_a: bytes, x: str, fixed_b: bytes, raw: bytes):
        self.fixed_a = fixed_a      # bytes before the Tm x coordinate
        self.x = x                  # Tm x as printed
        self.fixed_b = fixed_b      # ` Y Tm\n/F2 12 Tf\n0 0 0 rg\n(`
        self.raw = raw              # escaped string body (no parens)
        m = re.search(rb"/(F\d) (\d+) Tf", fixed_b)
        self.font = m.group(1).decode() if m else "F2"
        self.size = float(m.group(2)) if m else 12.0

    def text(self, decoders: Dict[str, Dict[int, str]]) -> str:
        data = _unescape(self.raw)
        table = decoders.get(self.font, {})
        chars = []
        for i in range(0, len(data), 2):
            cid = int.from_bytes(data[i:i + 2], "big")
            chars.append(table.get(cid, ""))
        return "".join(chars)


class Receipt:
    """A parsed genuine MTS donor plus the patch/save machinery."""

    def __init__(self, path: Union[str, Path], pool: Optional[Dict[str, FontPool]] = None):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self._parse_objects()
        self._parse_objstm()
        self._parse_content()
        self._parse_fonts()
        self._parse_info()
        self.pool = pool or {}
        # absorb this donor into the pools too (a donor always knows its
        # own glyphs)
        for family, subset in self.subsets.items():
            self.pool.setdefault(family, FontPool()).absorb(subset)
        self.fields: Dict[str, str] = self._read_fields()
        # generation-time overridables (tests pin them to the donor's)
        self.creation_date: Optional[str] = None       # D:...Z
        self.correlation_id: Optional[str] = None
        self.file_id: Optional[Tuple[str, str]] = None
        self.subset_tags: Optional[Tuple[str, str]] = None

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------

    def _parse_objects(self) -> None:
        self.objects: Dict[int, bytes] = {}       # num -> full serialised object
        self.stream_data: Dict[int, bytes] = {}   # num -> decompressed stream
        for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?$", self.raw):
            num = int(m.group(1))
            end = self.raw.index(b"endobj\n", m.start()) + len(b"endobj\n")
            self.objects[num] = self.raw[m.start():end]
            sidx = self.raw.find(b"stream\n", m.start(), end)
            if sidx != -1:
                send = self.raw.index(b"endstream", sidx)
                comp = self.raw[sidx + 7:send - 1]
                self.stream_data[num] = zlib.decompress(comp)

    def _parse_objstm(self) -> None:
        data = self.stream_data[31]
        m = re.search(rb"/First (\d+)/N (\d+)", self.objects[31])
        first, _n = int(m.group(1)), int(m.group(2))
        index = data[:first].split()
        pairs = [(int(index[i]), int(index[i + 1])) for i in range(0, len(index), 2)]
        byoff = sorted(pairs, key=lambda p: p[1])
        self.objstm_members: Dict[int, bytes] = {}
        for i, (num, off) in enumerate(byoff):
            stop = byoff[i + 1][1] if i + 1 < len(byoff) else len(data) - first
            body = data[first + off:first + stop]
            self.objstm_members[num] = body.rstrip(b" ")
        self.objstm_order: List[int] = [num for num, _ in byoff]

    def _parse_content(self) -> None:
        content = self.stream_data[29]
        matches = list(_TJ_RE.finditer(content))
        self.runs: List[_Run] = []
        prev_end = 0
        for m in matches:
            seg = content[prev_end:m.start()]
            tm = None
            for tm_m in _TM_RE.finditer(seg):
                tm = tm_m
            if tm is None:
                # run without its own Tm (not expected on this template)
                self.runs.append(_Run(seg, "", b"", m.group(1)))
            else:
                fixed_a = seg[:tm.start(1)]
                x = tm.group(1).decode("latin1")
                fixed_b = seg[tm.end(1):]
                self.runs.append(_Run(fixed_a, x, fixed_b, m.group(1)))
            prev_end = m.end()
        self.content_tail = content[prev_end:]
        if len(self.runs) != 23:
            raise ReceiptError(
                f"{self.path.name}: expected 23 text runs in the content "
                f"stream, found {len(self.runs)} -- unknown MTS layout"
            )

    def _parse_fonts(self) -> None:
        self.subsets: Dict[str, EmbeddedSubset] = {}
        for family, fobj, tobj in (("regular", 53, 56), ("medium", 57, 60)):
            font_bytes = self.stream_data[fobj]
            tounicode = self.stream_data[tobj].decode("latin1")
            self.subsets[family] = EmbeddedSubset(font_bytes, tounicode)
        # decoders for run text: F2 -> regular, F3 -> medium
        self._decoders: Dict[str, Dict[int, str]] = {}
        for res, family in (("F2", "regular"), ("F3", "medium")):
            inv = {gid: ch for ch, gid in self.subsets[family].char_to_gid.items()}
            self._decoders[res] = inv

    def _parse_info(self) -> None:
        m = re.search(rb"64 0 obj\n(<<.*?>>)\nendobj", self.raw, re.S)
        if not m:
            raise ReceiptError(f"{self.path.name}: Info object 64 not found")
        self.info_dict = m.group(1)
        dm = re.search(rb"/CreationDate\(([^)]*)\)", self.info_dict)
        sm = re.search(rb"/Subject\(CorrelationId:([0-9a-f]{16})\)", self.info_dict)
        self.donor_creation_date = dm.group(1).decode() if dm else None
        self.donor_correlation_id = sm.group(1).decode() if sm else None
        im = re.search(rb"/ID \[<([0-9a-f]{32})><([0-9a-f]{32})>\]", self.raw)
        self.donor_file_id = (im.group(1).decode(), im.group(2).decode()) if im else None

    def _read_fields(self) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for idx, field in RUN_VALUES.items():
            if field == "sbp2":
                continue  # glued onto "sbp" below
            fields[field] = self.runs[idx].text(self._decoders)
        fields["sbp"] += self.runs[20].text(self._decoders)
        fields["datetime"] = self.runs[RUN_HEADER].text(self._decoders)
        return fields

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------

    def donor_sbp_seconds(self) -> int:
        sbp = self.fields.get("sbp", "")
        if len(sbp) == 32 and sbp[5:11].isdigit():
            return int(sbp[9:11])
        return 0

    # ------------------------------------------------------------------
    # patching
    # ------------------------------------------------------------------

    def set(self, field: str, value: str) -> None:
        if field not in RUN_VALUES.values() and field != "datetime":
            raise ReceiptError(f"unknown field {field!r}")
        self.fields[field] = str(value)

    def _width(self, family: str, text: str) -> int:
        pool = self.pool[family]
        return sum(pool.width_of(c) for c in text)

    def _check_coverage(self) -> None:
        missing: Dict[str, List[str]] = {}
        texts_reg = [self.fields[f] for f in
                     ("datetime", "recipient", "phone", "bank", "message",
                      "account", "amount", "fee", "txcode", "sbp")]
        for char in set("".join(texts_reg)):
            if not self.pool["regular"].has_char(char):
                missing.setdefault("regular", []).append(char)
        for char in set(self.fields["total"] + "Итого"):
            if not self.pool["medium"].has_char(char):
                missing.setdefault("medium", []).append(char)
        if missing:
            detail = "; ".join(
                f"{family}: {''.join(sorted(set(chars)))}"
                for family, chars in missing.items()
            )
            raise FontCoverageError(
                f"glyphs outside the donor pool ({detail}); add a genuine "
                "MTS receipt containing these characters to banks/mts/donors/"
            )

    # ------------------------------------------------------------------
    # saving
    # ------------------------------------------------------------------

    def _encode(self, family: str, text: str) -> Tuple[bytes, List[Tuple[int, int, int]]]:
        """Text -> (UTF-16BE CID bytes, metrics) using the family pool."""
        pool = self.pool[family]
        out = bytearray()
        metrics: Dict[int, Tuple[int, int, int]] = {}
        for char in text:
            gid = pool.gid_of(char)
            if gid is None or gid not in pool.known_gids:
                raise FontCoverageError(
                    f"no glyph for {char!r} in the {family} donor pool"
                )
            out += gid.to_bytes(2, "big")
            metrics.setdefault(gid, (gid, pool.width_of_gid(gid), ord(char)))
        return bytes(out), sorted(metrics.values())

    def _encode_sbp(self) -> Tuple[bytes, bytes, float, float]:
        """Wrap the 32-char SBP id at the 167 pt box like the original."""
        sbp = self.fields["sbp"]
        pool = self.pool["regular"]
        limit = SBP_WRAP_WIDTH * 1000 / REGULAR_SIZE   # in font units
        line1 = ""
        used = 0
        for char in sbp:
            w = pool.width_of(char)
            if line1 and used + w > limit:
                break
            line1 += char
            used += w
        line2 = sbp[len(line1):]
        if not line1 or not line2:
            raise ReceiptError(
                f"SBP id {sbp!r} does not wrap into two lines as on genuine "
                "receipts (refusing to invent a one-line layout)"
            )
        b1, _ = self._encode("regular", line1)
        b2, _ = self._encode("regular", line2)
        x1 = RIGHT_EDGE - used * REGULAR_SIZE / 1000
        w2 = sum(pool.width_of(c) for c in line2)
        x2 = RIGHT_EDGE - w2 * REGULAR_SIZE / 1000
        return b1, b2, x1, x2

    def _build_content(self, sbp_line1_x: Optional[str] = None) -> bytes:
        out = bytearray()
        sbp_b1, sbp_b2, sbp_x1, sbp_x2 = self._encode_sbp()
        for idx, run in enumerate(self.runs):
            out += run.fixed_a
            if idx == 0:
                out += run.x.encode("latin1")
                out += run.fixed_b + b"(" + run.raw + b")Tj"
                continue
            if idx in RUN_LABELS:
                family = "medium" if run.font == "F3" else "regular"
                body, _m = self._encode(family, FIELD_LABELS[RUN_LABELS[idx]])
                out += run.x.encode("latin1")
                out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
                continue
            if idx == RUN_HEADER:
                body, _m = self._encode("regular", self.fields["datetime"])
                x = RIGHT_EDGE - self._width("regular", self.fields["datetime"]) * REGULAR_SIZE / 1000
                out += fmt_double(x).encode("latin1")
                out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
                continue
            field = RUN_VALUES[idx]
            if field == "sbp":
                x1 = sbp_line1_x if sbp_line1_x is not None else fmt_double(sbp_x1)
                out += x1.encode("latin1")
                out += run.fixed_b + b"(" + escape_string(sbp_b1) + b")Tj"
                continue
            if field == "sbp2":
                out += fmt_double(sbp_x2).encode("latin1")
                out += run.fixed_b + b"(" + escape_string(sbp_b2) + b")Tj"
                continue
            family = "medium" if run.font == "F3" else "regular"
            size = MEDIUM_SIZE if run.font == "F3" else REGULAR_SIZE
            text = self.fields[field]
            body, _m = self._encode(family, text)
            x = RIGHT_EDGE - self._width(family, text) * size / 1000
            out += fmt_double(x).encode("latin1")
            out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
        out += self.content_tail
        return bytes(out)

    def _build_dests(self, sbp_x1: float) -> List[Tuple[str, str, str]]:
        """(key text, x, y) per named destination, before sorting."""
        entries: List[Tuple[str, str, str]] = []
        # 10 labels (x = 20) + 10 values (x = recomputed) + page anchor
        for field, label in FIELD_LABELS.items():
            row = LABEL_ROW[field]
            size = MEDIUM_SIZE if row == "total" else REGULAR_SIZE
            y = ROW_Y[row] + size
            entries.append((label, "20", fmt_double(y)))
        for run_idx, field in RUN_VALUES.items():
            if field == "sbp":
                x = fmt_double(sbp_x1)
                y = ROW_Y["sbp"] + REGULAR_SIZE
            elif field in ("sbp2", "total"):
                # the wrapped second line and the Итого value carry no
                # anchor on genuine receipts (BANK.md)
                continue
            else:
                x = fmt_double(RIGHT_EDGE - self._width("regular", self.fields[field]) * REGULAR_SIZE / 1000)
                y = ROW_Y[field] + REGULAR_SIZE
            entries.append((self.fields[field], x, fmt_double(y)))
        # datetime value dest
        xdt = fmt_double(RIGHT_EDGE - self._width("regular", self.fields["datetime"]) * REGULAR_SIZE / 1000)
        entries.append((self.fields["datetime"], xdt, fmt_double(ROW_Y["datetime"] + REGULAR_SIZE)))
        entries.append((PAGE_ANCHOR, "0", "730"))
        keys = [e[0] for e in entries]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ReceiptError(
                f"duplicate /Names keys would make an invalid name tree: "
                f"{dupes!r} (two fields print the same text; genuine "
                "receipts never do this)"
            )
        # Java String.compareTo == Python str order for BMP-only keys
        entries.sort(key=lambda e: e[0])
        return entries

    def _build_objstm(self, dests: List[Tuple[str, str, str]],
                      tags: Tuple[str, str],
                      w_reg: bytes, w_med: bytes) -> bytes:
        members: List[Tuple[int, bytes]] = []
        members.append((1, self.objstm_members[1]))
        for i, (key, x, y) in enumerate(dests):
            members.append((32 + i, f"[1 0 R/XYZ {x} {y} 0]".encode("latin1")))
        members.append((2, self.objstm_members[2]))
        tag_reg, tag_med = tags
        bbox_reg = self.pool["regular"].head_bbox
        bbox_med = self.pool["medium"].head_bbox
        fd_reg = (self.objstm_members[54]
                  .replace(self._donor_tag("regular") + b"+MTSSans-Regular",
                           tag_reg + b"+MTSSans-Regular"))
        fd_med = (self.objstm_members[58]
                  .replace(self._donor_tag("medium") + b"+MTSSans-Medium",
                           tag_med + b"+MTSSans-Medium"))
        cid_reg = (b"<</DW 1000/Subtype/CIDFontType2"
                   b"/CIDSystemInfo<</Supplement 0/Registry(Adobe)/Ordering(Identity)>>"
                   b"/Type/Font/BaseFont/" + tag_reg + b"+MTSSans-Regular"
                   b"/FontDescriptor 54 0 R/W " + w_reg + b"/CIDToGIDMap/Identity>>")
        cid_med = (b"<</DW 1000/Subtype/CIDFontType2"
                   b"/CIDSystemInfo<</Supplement 0/Registry(Adobe)/Ordering(Identity)>>"
                   b"/Type/Font/BaseFont/" + tag_med + b"+MTSSans-Medium"
                   b"/FontDescriptor 58 0 R/W " + w_med + b"/CIDToGIDMap/Identity>>")
        type0_reg = (b"<</Subtype/Type0/Type/Font/BaseFont/" + tag_reg +
                     b"+MTSSans-Regular/Encoding/Identity-H"
                     b"/DescendantFonts[55 0 R]/ToUnicode 56 0 R>>")
        type0_med = (b"<</Subtype/Type0/Type/Font/BaseFont/" + tag_med +
                     b"+MTSSans-Medium/Encoding/Identity-H"
                     b"/DescendantFonts[59 0 R]/ToUnicode 60 0 R>>")
        members.append((54, fd_reg))
        members.append((55, cid_reg))
        members.append((25, type0_reg))
        members.append((58, fd_med))
        members.append((59, cid_med))
        members.append((26, type0_med))
        members.append((30, self.objstm_members[30]))
        names = bytearray(b"<</Names[")
        for i, (key, _x, _y) in enumerate(dests):
            names += pdf_string(mangle_name_key(key))
            names += f" {32 + i} 0 R".encode("latin1")
        names += b"]>>"
        members.append((61, bytes(names)))
        members.append((62, self.objstm_members[62]))
        # serialise: "num off " pairs (trailing space), bodies + single space
        index = bytearray()
        bodies = bytearray()
        for num, body in members:
            index += f"{num} {len(bodies)} ".encode("latin1")
            bodies += body + b" "
        payload = bytes(index) + bytes(bodies)
        head = (b"<</Filter/FlateDecode/Type/ObjStm/Length %d/First %d/N %d>>"
                % (len(compress(payload)), len(index), len(members)))
        return head + b"stream\n" + compress(payload) + b"\nendstream"

    def _donor_tag(self, family: str) -> bytes:
        member = self.objstm_members[54 if family == "regular" else 58]
        m = re.search(rb"/FontName/([A-Z]{6})\+", member)
        if not m:
            raise ReceiptError(f"subset tag not found in {self.path.name}")
        return m.group(1)

    def save(self, output: Union[str, Path], *,
             creation_date: Optional[str] = None,
             correlation_id: Optional[str] = None,
             file_id: Optional[Tuple[str, str]] = None,
             subset_tags: Optional[Tuple[str, str]] = None,
             sbp_line1_x: Optional[str] = None) -> Path:
        """Write the patched receipt as a single-pass PDF.

        The four metadata knobs exist so tests can re-emit a donor with
        its own values byte-for-byte; in production they stay ``None``
        and everything is fresh.
        """
        self._check_coverage()
        import secrets

        creation_date = creation_date or datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        correlation_id = correlation_id or secrets.token_hex(8)
        file_id = file_id or (secrets.token_hex(16), secrets.token_hex(16))
        subset_tags = subset_tags or (
            "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6)),
            "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6)),
        )
        subset_tags = (subset_tags[0].encode("latin1"), subset_tags[1].encode("latin1"))

        # --- font subsets for the new text ------------------------------
        text_reg = "".join(self.fields[f] for f in
                           ("datetime", "recipient", "phone", "bank", "message",
                            "account", "amount", "fee", "txcode", "sbp"))
        text_reg += "".join(l for l in FIELD_LABELS.values() if l != "Итого")
        text_med = "Итого" + self.fields["total"]
        gids_reg = sorted({self.pool["regular"].gid_of(c) for c in set(text_reg)})
        gids_med = sorted({self.pool["medium"].gid_of(c) for c in set(text_med)})
        font_reg = build_subset(self.pool["regular"], gids_reg)
        font_med = build_subset(self.pool["medium"], gids_med)

        def metrics_for(family: str, text: str) -> List[Tuple[int, int, int]]:
            pool = self.pool[family]
            seen: Dict[int, Tuple[int, int, int]] = {}
            for char in text:
                gid = pool.gid_of(char)
                seen.setdefault(gid, (gid, pool.width_of_gid(gid), ord(char)))
            return sorted(seen.values())

        met_reg = metrics_for("regular", text_reg)
        met_med = metrics_for("medium", text_med)
        touni_reg = build_tounicode(met_reg)
        touni_med = build_tounicode(met_med)
        w_reg = build_w_array(met_reg)
        w_med = build_w_array(met_med)

        # --- content stream ----------------------------------------------
        content = self._build_content(sbp_line1_x=sbp_line1_x)
        sbp_b1, _sbp_b2, sbp_x1, _sbp_x2 = self._encode_sbp()

        # --- ObjStm --------------------------------------------------------
        if sbp_line1_x is not None:
            try:
                sbp_x1 = float(sbp_line1_x)
            except ValueError:
                pass
        dests = self._build_dests(sbp_x1)
        objstm = self._build_objstm(dests, subset_tags, w_reg, w_med)

        # --- Info ----------------------------------------------------------
        info = (b"<</ModDate(" + creation_date.encode("latin1") +
                b")/Creator(dbo-print-forms)/CreationDate(" +
                creation_date.encode("latin1") +
                b")/Producer(OpenPDF 1.3.30.jaspersoft.2)/Subject(CorrelationId:" +
                correlation_id.encode("latin1") +
                b")/Author(MTS Bank)/Title(Printable document)>>")

        catalog_dict = (b"<</Names 62 0 R/Type/Catalog/Pages 30 0 R"
                        b"/ViewerPreferences<</PrintScaling/AppDefault>>>>")

        # --- emit ------------------------------------------------------------
        chunks: List[Tuple[int, bytes]] = []
        for num in list(range(4, 24)) + [3, 24, 27, 28]:
            chunks.append((num, self.objects[num]))
        chunks.append((29, b"29 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n" % len(compress(content))
                          + compress(content) + b"\nendstream\nendobj\n"))
        chunks.append((53, b"53 0 obj\n<</Length1 %d/Filter/FlateDecode/Length %d>>stream\n"
                          % (len(font_reg), len(compress(font_reg)))
                          + compress(font_reg) + b"\nendstream\nendobj\n"))
        chunks.append((56, b"56 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n" % len(compress(touni_reg))
                          + compress(touni_reg) + b"\nendstream\nendobj\n"))
        chunks.append((57, b"57 0 obj\n<</Length1 %d/Filter/FlateDecode/Length %d>>stream\n"
                          % (len(font_med), len(compress(font_med)))
                          + compress(font_med) + b"\nendstream\nendobj\n"))
        chunks.append((60, b"60 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n" % len(compress(touni_med))
                          + compress(touni_med) + b"\nendstream\nendobj\n"))
        chunks.append((63, b"63 0 obj\n" + catalog_dict + b"\nendobj\n"))
        chunks.append((64, b"64 0 obj\n" + info + b"\nendobj\n"))
        chunks.append((31, b"31 0 obj\n" + objstm + b"\nendobj\n"))

        body = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets: Dict[int, int] = {}
        for num, blob in chunks:
            offsets[num] = len(body)
            body += blob

        # --- xref stream -----------------------------------------------------
        type2_index = {num: i for i, num in enumerate(
            [1] + list(range(32, 53)) + [2, 54, 55, 25, 58, 59, 26, 30, 61, 62])}
        entries = bytearray()
        for num in range(0, 66):
            if num in (0, 65):
                entries += b"\x00\x00\x00\xff\xff"
            elif num in type2_index:
                entries += b"\x02" + (31).to_bytes(2, "big") + type2_index[num].to_bytes(2, "big")
            else:
                entries += b"\x01" + offsets[num].to_bytes(2, "big") + b"\x00\x00"
        xref_dict = (b"<</Info 64 0 R/Filter/FlateDecode/Type/XRef/W[1 2 2]"
                     b"/Index[0 66]/ID [<" + file_id[0].encode("latin1") +
                     b"><" + file_id[1].encode("latin1") +
                     b">]/Root 63 0 R/Length %d/Size 66>>" % len(compress(bytes(entries))))
        startxref = len(body)
        body += (b"65 0 obj\n" + xref_dict + b"stream\n" + compress(bytes(entries)) +
                 b"\nendstream\nendobj\nstartxref\n" +
                 str(startxref).encode("latin1") + b"\n%%EOF\n")

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(bytes(body))
        raw = bytes(body)
        if (not raw.startswith(b"%PDF-")
                or raw.count(b"startxref") != 1
                or raw.count(b"%%EOF") != 1
                or b"/Prev" in raw):
            output.unlink(missing_ok=True)
            raise ReceiptError("natural save failed: not a single-pass PDF")
        return output
