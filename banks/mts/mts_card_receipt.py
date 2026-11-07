#!/usr/bin/env python3
"""Card-transfer receipt model for MTS Bank.

Same producer as the SBP receipts (OpenPDF 1.3.30.jaspersoft.2 /
dbo-print-forms, MTSSans v1.0), but a shorter layout: no recipient /
phone / bank / message / SBP id.  Fields are «Карта списания»,
«Счет списания», «Карта зачисления», сумма, комиссия, код транзакции.

A genuine donor from ``donors_card/`` is patched field by field and
re-emitted as a single-pass PDF (one xref stream, one ``%%EOF``, zlib
level 6).  Object numbers differ from the SBP template (Size 60, page
375×608, 15 named dests) — this module does not touch ``mts_receipt.py``.
"""
from __future__ import annotations

import re
import secrets
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from mts_pdf import fmt_double, escape_string, pdf_string, compress, mangle_name_key
from mts_font import (
    EmbeddedSubset, FontPool, FontCoverageError,
    build_subset, build_tounicode, build_w_array,
)
from mts_receipt import (  # noqa: E402
    ReceiptError, MSK, _unescape, _Run, _TJ_RE, _TM_RE,
)

__all__ = ["CardReceipt", "ReceiptError", "MSK"]

RIGHT_EDGE = 355.0
REGULAR_SIZE = 12.0
MEDIUM_SIZE = 20.0
PAGE_HEIGHT = 608.0

ROW_Y = {
    "datetime": 531.35,
    "debit_card": 485.35,
    "account": 453.35,
    "credit_card": 421.35,
    "amount": 389.35,
    "fee": 357.35,
    "txcode": 325.35,
    "total": 260.58,
}
FIELD_LABELS = {
    "debit_card": "Карта списания",
    "account": "Счет списания",
    "credit_card": "Карта зачисления",
    "amount": "Сумма",
    "fee": "Комиссия",
    "txcode": "Код транзакции",
    "total": "Итого",
}
LABEL_ROW = {k: k for k in FIELD_LABELS}
PAGE_ANCHOR = "JR_PAGE_ANCHOR_0_1"

RUN_HEADER = 1
RUN_VALUES = {
    3: "debit_card", 5: "account", 7: "credit_card",
    9: "amount", 11: "fee", 13: "txcode", 15: "total",
}
RUN_LABELS = {
    2: "debit_card", 4: "account", 6: "credit_card",
    8: "amount", 10: "fee", 12: "txcode", 14: "total",
}

# physical / ObjStm map measured on both genuine card donors
_CONTENT = 29
_FONTFILE = {"regular": 47, "medium": 51}
_TOUNICODE = {"regular": 50, "medium": 54}
_FD = {"regular": 48, "medium": 52}
_CID = {"regular": 49, "medium": 53}
_TYPE0 = {"regular": 25, "medium": 26}
_OBJSTM = 31
_CATALOG = 57
_INFO = 58
_XREF = 59
_NAMES = 55
_DESTS = 56
_PAGES = 30
_SIZE = 60
_DEST_START = 32
_DEST_COUNT = 15
_OBJSTM_TYPE2 = (
    [1] + list(range(_DEST_START, _DEST_START + _DEST_COUNT))
    + [2, 48, 49, 25, 52, 53, 26, 30, 55, 56]
)
_PHYSICAL_ORDER = (
    list(range(4, 24)) + [3, 24, 27, 28, 29, 47, 50, 51, 54, 57, 58, 31]
)


class CardReceipt:
    """A parsed genuine MTS card-transfer donor plus patch/save."""

    def __init__(self, path: Union[str, Path], pool: Optional[Dict[str, FontPool]] = None):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self._parse_objects()
        self._parse_objstm()
        self._parse_content()
        self._parse_fonts()
        self._parse_info()
        self.pool = pool or {}
        for family, subset in self.subsets.items():
            self.pool.setdefault(family, FontPool()).absorb(subset)
        self.fields: Dict[str, str] = self._read_fields()

    def _parse_objects(self) -> None:
        self.objects: Dict[int, bytes] = {}
        self.stream_data: Dict[int, bytes] = {}
        for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?$", self.raw):
            num = int(m.group(1))
            end = self.raw.index(b"endobj\n", m.start()) + len(b"endobj\n")
            self.objects[num] = self.raw[m.start():end]
            sidx = self.raw.find(b"stream\n", m.start(), end)
            if sidx != -1:
                send = self.raw.index(b"endstream", sidx)
                comp = self.raw[sidx + 7:send - 1]
                if b"/FlateDecode" in self.objects[num]:
                    self.stream_data[num] = zlib.decompress(comp)
                else:
                    self.stream_data[num] = comp

    def _parse_objstm(self) -> None:
        data = self.stream_data[_OBJSTM]
        m = re.search(rb"/First (\d+)/N (\d+)", self.objects[_OBJSTM])
        first, n = int(m.group(1)), int(m.group(2))
        if n != 26:
            raise ReceiptError(
                f"{self.path.name}: expected ObjStm N=26 for a card receipt, "
                f"got {n} — unknown MTS card layout"
            )
        index = data[:first].split()
        pairs = [(int(index[i]), int(index[i + 1])) for i in range(0, len(index), 2)]
        byoff = sorted(pairs, key=lambda p: p[1])
        self.objstm_members: Dict[int, bytes] = {}
        for i, (num, off) in enumerate(byoff):
            stop = byoff[i + 1][1] if i + 1 < len(byoff) else len(data) - first
            self.objstm_members[num] = data[first + off:first + stop].rstrip(b" ")
        self.objstm_order: List[int] = [num for num, _ in byoff]

    def _parse_content(self) -> None:
        content = self.stream_data[_CONTENT]
        matches = list(_TJ_RE.finditer(content))
        self.runs: List[_Run] = []
        prev_end = 0
        for m in matches:
            seg = content[prev_end:m.start()]
            tm = None
            for tm_m in _TM_RE.finditer(seg):
                tm = tm_m
            if tm is None:
                self.runs.append(_Run(seg, "", b"", m.group(1)))
            else:
                self.runs.append(_Run(
                    seg[:tm.start(1)], tm.group(1).decode("latin1"),
                    seg[tm.end(1):], m.group(1)))
            prev_end = m.end()
        self.content_tail = content[prev_end:]
        if len(self.runs) != 16:
            raise ReceiptError(
                f"{self.path.name}: expected 16 text runs in a card receipt, "
                f"found {len(self.runs)} — unknown MTS card layout"
            )

    def _parse_fonts(self) -> None:
        self.subsets: Dict[str, EmbeddedSubset] = {}
        for family in ("regular", "medium"):
            font_bytes = self.stream_data[_FONTFILE[family]]
            tounicode = self.stream_data[_TOUNICODE[family]].decode("latin1")
            self.subsets[family] = EmbeddedSubset(font_bytes, tounicode)
        self._decoders: Dict[str, Dict[int, str]] = {}
        for res, family in (("F2", "regular"), ("F3", "medium")):
            inv = {gid: ch for ch, gid in self.subsets[family].char_to_gid.items()}
            self._decoders[res] = inv

    def _parse_info(self) -> None:
        blob = self.objects[_INFO]
        dm = re.search(rb"/CreationDate\(([^)]*)\)", blob)
        sm = re.search(rb"/Subject\(CorrelationId:([0-9a-f]{16})\)", blob)
        self.donor_creation_date = dm.group(1).decode() if dm else None
        self.donor_correlation_id = sm.group(1).decode() if sm else None
        im = re.search(rb"/ID \[<([0-9a-f]{32})><([0-9a-f]{32})>\]", self.raw)
        self.donor_file_id = (im.group(1).decode(), im.group(2).decode()) if im else None

    def _read_fields(self) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for idx, field in RUN_VALUES.items():
            fields[field] = self.runs[idx].text(self._decoders)
        fields["datetime"] = self.runs[RUN_HEADER].text(self._decoders)
        return fields

    def set(self, field: str, value: str) -> None:
        if field not in RUN_VALUES.values() and field != "datetime":
            raise ReceiptError(f"unknown card field {field!r}")
        self.fields[field] = str(value)

    def _width(self, family: str, text: str) -> int:
        return sum(self.pool[family].width_of(c) for c in text)

    def _check_coverage(self) -> None:
        missing: Dict[str, List[str]] = {}
        texts_reg = [self.fields[f] for f in
                     ("datetime", "debit_card", "account", "credit_card",
                      "amount", "fee", "txcode")]
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
                "MTS card receipt containing these characters to "
                "banks/mts/donors_card/"
            )

    def _encode(self, family: str, text: str) -> bytes:
        pool = self.pool[family]
        out = bytearray()
        for char in text:
            gid = pool.gid_of(char)
            if gid is None or gid not in pool.known_gids:
                raise FontCoverageError(
                    f"no glyph for {char!r} in the {family} donor pool"
                )
            out += gid.to_bytes(2, "big")
        return bytes(out)

    def _build_content(self) -> bytes:
        out = bytearray()
        for idx, run in enumerate(self.runs):
            out += run.fixed_a
            if idx == 0:
                out += run.x.encode("latin1")
                out += run.fixed_b + b"(" + run.raw + b")Tj"
                continue
            if idx in RUN_LABELS:
                family = "medium" if run.font == "F3" else "regular"
                body = self._encode(family, FIELD_LABELS[RUN_LABELS[idx]])
                out += run.x.encode("latin1")
                out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
                continue
            if idx == RUN_HEADER:
                text = self.fields["datetime"]
                body = self._encode("regular", text)
                x = RIGHT_EDGE - self._width("regular", text) * REGULAR_SIZE / 1000
                out += fmt_double(x).encode("latin1")
                out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
                continue
            field = RUN_VALUES[idx]
            family = "medium" if run.font == "F3" else "regular"
            size = MEDIUM_SIZE if run.font == "F3" else REGULAR_SIZE
            text = self.fields[field]
            body = self._encode(family, text)
            x = RIGHT_EDGE - self._width(family, text) * size / 1000
            out += fmt_double(x).encode("latin1")
            out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
        out += self.content_tail
        return bytes(out)

    def _build_dests(self) -> List[Tuple[str, str, str]]:
        entries: List[Tuple[str, str, str]] = []
        for field, label in FIELD_LABELS.items():
            size = MEDIUM_SIZE if field == "total" else REGULAR_SIZE
            y = ROW_Y[field] + size
            entries.append((label, "20", fmt_double(y)))
        for field in ("debit_card", "account", "credit_card",
                      "amount", "fee", "txcode"):
            x = fmt_double(
                RIGHT_EDGE
                - self._width("regular", self.fields[field]) * REGULAR_SIZE / 1000
            )
            y = ROW_Y[field] + REGULAR_SIZE
            entries.append((self.fields[field], x, fmt_double(y)))
        xdt = fmt_double(
            RIGHT_EDGE
            - self._width("regular", self.fields["datetime"]) * REGULAR_SIZE / 1000
        )
        entries.append((self.fields["datetime"], xdt,
                        fmt_double(ROW_Y["datetime"] + REGULAR_SIZE)))
        entries.append((PAGE_ANCHOR, "0", "618"))
        keys = [e[0] for e in entries]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ReceiptError(
                f"duplicate /Names keys would make an invalid name tree: "
                f"{dupes!r} (two fields print the same text; genuine "
                "card receipts never do this)"
            )
        entries.sort(key=lambda e: e[0])
        if len(entries) != _DEST_COUNT:
            raise ReceiptError(
                f"card layout has {_DEST_COUNT} named dests, got {len(entries)}"
            )
        return entries

    def _donor_tag(self, family: str) -> bytes:
        member = self.objstm_members[_FD[family]]
        m = re.search(rb"/FontName/([A-Z]{6})\+", member)
        if not m:
            raise ReceiptError(f"subset tag not found in {self.path.name}")
        return m.group(1)

    def _build_objstm(self, dests: List[Tuple[str, str, str]],
                      tags: Tuple[bytes, bytes],
                      w_reg: bytes, w_med: bytes) -> bytes:
        members: List[Tuple[int, bytes]] = []
        members.append((1, self.objstm_members[1]))
        for i, (key, x, y) in enumerate(dests):
            members.append((_DEST_START + i,
                            f"[1 0 R/XYZ {x} {y} 0]".encode("latin1")))
        members.append((2, self.objstm_members[2]))
        tag_reg, tag_med = tags
        fd_reg = (self.objstm_members[_FD["regular"]]
                  .replace(self._donor_tag("regular") + b"+MTSSans-Regular",
                           tag_reg + b"+MTSSans-Regular"))
        fd_med = (self.objstm_members[_FD["medium"]]
                  .replace(self._donor_tag("medium") + b"+MTSSans-Medium",
                           tag_med + b"+MTSSans-Medium"))
        cid_reg = (
            b"<</DW 1000/Subtype/CIDFontType2"
            b"/CIDSystemInfo<</Supplement 0/Registry(Adobe)/Ordering(Identity)>>"
            b"/Type/Font/BaseFont/" + tag_reg + b"+MTSSans-Regular"
            b"/FontDescriptor 48 0 R/W " + w_reg + b"/CIDToGIDMap/Identity>>"
        )
        cid_med = (
            b"<</DW 1000/Subtype/CIDFontType2"
            b"/CIDSystemInfo<</Supplement 0/Registry(Adobe)/Ordering(Identity)>>"
            b"/Type/Font/BaseFont/" + tag_med + b"+MTSSans-Medium"
            b"/FontDescriptor 52 0 R/W " + w_med + b"/CIDToGIDMap/Identity>>"
        )
        type0_reg = (
            b"<</Subtype/Type0/Type/Font/BaseFont/" + tag_reg +
            b"+MTSSans-Regular/Encoding/Identity-H"
            b"/DescendantFonts[49 0 R]/ToUnicode 50 0 R>>"
        )
        type0_med = (
            b"<</Subtype/Type0/Type/Font/BaseFont/" + tag_med +
            b"+MTSSans-Medium/Encoding/Identity-H"
            b"/DescendantFonts[53 0 R]/ToUnicode 54 0 R>>"
        )
        members.append((48, fd_reg))
        members.append((49, cid_reg))
        members.append((25, type0_reg))
        members.append((52, fd_med))
        members.append((53, cid_med))
        members.append((26, type0_med))
        members.append((30, self.objstm_members[30]))
        names = bytearray(b"<</Names[")
        for i, (key, _x, _y) in enumerate(dests):
            names += pdf_string(mangle_name_key(key))
            names += f" {_DEST_START + i} 0 R".encode("latin1")
        names += b"]>>"
        members.append((55, bytes(names)))
        members.append((56, self.objstm_members[56]))
        index = bytearray()
        bodies = bytearray()
        for num, body in members:
            index += f"{num} {len(bodies)} ".encode("latin1")
            bodies += body + b" "
        payload = bytes(index) + bytes(bodies)
        head = (b"<</Filter/FlateDecode/Type/ObjStm/Length %d/First %d/N %d>>"
                % (len(compress(payload)), len(index), len(members)))
        return head + b"stream\n" + compress(payload) + b"\nendstream"

    def save(self, output: Union[str, Path], *,
             creation_date: Optional[str] = None,
             correlation_id: Optional[str] = None,
             file_id: Optional[Tuple[str, str]] = None,
             subset_tags: Optional[Tuple[str, str]] = None) -> Path:
        """Write the patched card receipt as a single-pass PDF."""
        self._check_coverage()
        creation_date = creation_date or datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        correlation_id = correlation_id or secrets.token_hex(8)
        file_id = file_id or (secrets.token_hex(16), secrets.token_hex(16))
        subset_tags = subset_tags or (
            "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6)),
            "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6)),
        )
        tags = (subset_tags[0].encode("latin1"), subset_tags[1].encode("latin1"))

        text_reg = "".join(self.fields[f] for f in
                           ("datetime", "debit_card", "account", "credit_card",
                            "amount", "fee", "txcode"))
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

        content = self._build_content()
        dests = self._build_dests()
        objstm = self._build_objstm(dests, tags, w_reg, w_med)

        info = (b"<</ModDate(" + creation_date.encode("latin1") +
                b")/Creator(dbo-print-forms)/CreationDate(" +
                creation_date.encode("latin1") +
                b")/Producer(OpenPDF 1.3.30.jaspersoft.2)/Subject(CorrelationId:" +
                correlation_id.encode("latin1") +
                b")/Author(MTS Bank)/Title(Printable document)>>")
        catalog_dict = (b"<</Names 56 0 R/Type/Catalog/Pages 30 0 R"
                        b"/ViewerPreferences<</PrintScaling/AppDefault>>>>")

        chunks: List[Tuple[int, bytes]] = []
        for num in list(range(4, 24)) + [3, 24, 27, 28]:
            chunks.append((num, self.objects[num]))
        chunks.append((
            29,
            b"29 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n"
            % len(compress(content)) + compress(content) + b"\nendstream\nendobj\n",
        ))
        chunks.append((
            47,
            b"47 0 obj\n<</Length1 %d/Filter/FlateDecode/Length %d>>stream\n"
            % (len(font_reg), len(compress(font_reg)))
            + compress(font_reg) + b"\nendstream\nendobj\n",
        ))
        chunks.append((
            50,
            b"50 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n"
            % len(compress(touni_reg)) + compress(touni_reg)
            + b"\nendstream\nendobj\n",
        ))
        chunks.append((
            51,
            b"51 0 obj\n<</Length1 %d/Filter/FlateDecode/Length %d>>stream\n"
            % (len(font_med), len(compress(font_med)))
            + compress(font_med) + b"\nendstream\nendobj\n",
        ))
        chunks.append((
            54,
            b"54 0 obj\n<</Filter/FlateDecode/Length %d>>stream\n"
            % len(compress(touni_med)) + compress(touni_med)
            + b"\nendstream\nendobj\n",
        ))
        chunks.append((57, b"57 0 obj\n" + catalog_dict + b"\nendobj\n"))
        chunks.append((58, b"58 0 obj\n" + info + b"\nendobj\n"))
        chunks.append((31, b"31 0 obj\n" + objstm + b"\nendobj\n"))

        body = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets: Dict[int, int] = {}
        for num, blob in chunks:
            offsets[num] = len(body)
            body += blob

        type2_index = {num: i for i, num in enumerate(_OBJSTM_TYPE2)}
        entries = bytearray()
        for num in range(0, _SIZE):
            if num in (0, _XREF):
                entries += b"\x00\x00\x00\xff\xff"
            elif num in type2_index:
                entries += (b"\x02" + (31).to_bytes(2, "big")
                            + type2_index[num].to_bytes(2, "big"))
            else:
                entries += b"\x01" + offsets[num].to_bytes(2, "big") + b"\x00\x00"
        xref_dict = (
            b"<</Info 58 0 R/Filter/FlateDecode/Type/XRef/W[1 2 2]"
            b"/Index[0 60]/ID [<" + file_id[0].encode("latin1") +
            b"><" + file_id[1].encode("latin1") +
            b">]/Root 57 0 R/Length %d/Size 60>>" % len(compress(bytes(entries)))
        )
        startxref = len(body)
        body += (b"59 0 obj\n" + xref_dict + b"stream\n" + compress(bytes(entries)) +
                 b"\nendstream\nendobj\nstartxref\n" +
                 str(startxref).encode("latin1") + b"\n%%EOF\n")

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(body)
        if (not raw.startswith(b"%PDF-")
                or raw.count(b"startxref") != 1
                or raw.count(b"%%EOF") != 1
                or b"/Prev" in raw):
            raise ReceiptError("natural save failed: not a single-pass PDF")
        output.write_bytes(raw)
        return output
