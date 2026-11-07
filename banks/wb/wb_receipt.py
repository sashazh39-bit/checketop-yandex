#!/usr/bin/env python3
"""Field-level model of a Wildberries Bank SBP receipt.

A WB receipt is a one-page JasperReports 7.0.3 / OpenPDF 1.3.32
document (classic xref, compact ``>>stream``, zlib 6).  Values sit in
``BT 1 0 0 1 <x> <y> Tm /Fn <size> Tf (...)Tj ET`` blocks:

* amount (GoogleSans-Medium 55.5 pt) and datetime (Regular 17 pt) are
  centred at x = 297.5;
* row values (Regular 17 pt) are right-aligned at x = 555;
* the 32-char SBP id wraps at 245 pt into two Regular runs;
* «Квитанция №» (Bold 14 pt) is left-aligned at x = 40.

CID == GID in the Identity-H Google Sans subsets (7525 slots).  Missing
characters are grown by copying the glyf bytes at the same GID from
another genuine WB receipt (``wb_font.inject_raw_glyphs``), then pruned
back to exactly the used set on a natural save.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_ROCKET = _REPO_ROOT / "banks" / "rocket"
for _path in (_ROCKET, _HERE, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import rocket_pdf  # noqa: E402
import yandex_font  # noqa: E402
import wb_font  # noqa: E402

MSK = timezone(timedelta(hours=3))

PAGE_WIDTH = 595.0
PAGE_CENTER = 297.5
RIGHT_EDGE = 555.0
LEFT_X = 40.0
SBP_WRAP_WIDTH = 245.0

MEDIUM_FAMILY = "GoogleSans-Medium"
REGULAR_FAMILY = "GoogleSans-Regular"
BOLD_FAMILY = "GoogleSans-Bold"

LABEL_FIELDS: Dict[str, str] = {
    "Вид операции": "type",
    "Статус операции": "status",
    "Получатель": "recipient",
    "Телефон получателя": "phone",
    "Банк получателя": "bank",
}
_STRUCTURAL_LABELS = set(LABEL_FIELDS) | {
    "Идентификатор операции",
    "СБП",
}
_ALIGN = {
    "amount": "center",
    "datetime": "center",
    "type": "right",
    "status": "right",
    "recipient": "right",
    "phone": "right",
    "bank": "right",
    "sbp_1": "right",
    "sbp_2": "right",
    "receipt": "left",
}
_SPACE_CHARS = set(" \u00a0\u2009\u202f")
_DATETIME_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}$")
_AMOUNT_RE = re.compile(
    r"^\s*(\d[\d \u00a0\u2009\u202f]*?)(?:[,.](\d{1,2}))?"
    r"(?:\s*(?:₽|руб\.?|RUB))?\s*$",
    re.IGNORECASE,
)


class ReceiptError(RuntimeError):
    pass


def fmt_double(value: float) -> str:
    """OpenPDF ``ByteBuffer.formatDouble`` for receipt coordinates."""
    d = float(value)
    negative = d < 0
    if abs(d) < 0.000015:
        return "0"
    if negative:
        d = -d
    if d < 1.0:
        d += 0.000005
        if d >= 1:
            return "-1" if negative else "1"
        v = int(d * 100000)
        digits = f"{v:05d}".rstrip("0") or "0"
        return ("-" if negative else "") + "0." + digits
    if d <= 32767:
        d += 0.005
        v = int(d * 100)
        whole, frac = divmod(v, 100)
        if frac == 0:
            return ("-" if negative else "") + str(whole)
        return (("-" if negative else "") + f"{whole}.{frac:02d}").rstrip("0")
    text = repr(round(d, 6))
    return ("-" if negative else "") + text


@dataclass
class TextRun:
    font: str
    size: float
    x: float
    y: float
    text: str
    cids: List[int]
    tm_x_span: Tuple[int, int]
    literal_span: Tuple[int, int]


@dataclass
class FontSlot:
    resource: str
    type0_obj: int
    cidfont_obj: int
    descriptor_obj: int
    fontfile_obj: int
    tounicode_obj: int
    cidset_obj: int
    family: str
    cid_to_char: Dict[int, str] = dataclass_field(default_factory=dict)
    widths: Dict[int, int] = dataclass_field(default_factory=dict)

    @property
    def char_to_cid(self) -> Dict[str, int]:
        return yandex_font.invert_cmap(self.cid_to_char)


class Receipt:
    """A WB SBP receipt opened for field editing."""

    def __init__(
        self,
        path: str,
        glyph_pool: Optional[wb_font.GlyphPool] = None,
        glyph_pool_loader: Optional[Callable[[], wb_font.GlyphPool]] = None,
    ):
        self.path = path
        self.pdf = rocket_pdf.load(path)
        self.glyph_pool = glyph_pool
        self._glyph_pool_loader = glyph_pool_loader
        self.grown_chars: List[str] = []
        self._page = self._find_page()
        self._content_obj = self.pdf.resolve_ref(self._page.dict_bytes, b"/Contents")
        if self._content_obj is None:
            raise ReceiptError("page has no /Contents reference")
        self._content = self.pdf.obj(self._content_obj).decoded_stream()
        self._content_original = self._content
        self.fonts = self._load_fonts()
        self.runs = self._parse_runs()
        self._fields = self._map_fields()
        self.operation_dt: Optional[datetime] = None
        self.created_dt: Optional[datetime] = None

    # ------------------------------------------------------------- structure

    def _find_page(self) -> rocket_pdf.PdfObject:
        for obj in self.pdf.find_objects(b"/Type/Page"):
            if b"/Contents" in obj.dict_bytes:
                return obj
        raise ReceiptError("no page object with contents found")

    def _load_fonts(self) -> Dict[str, FontSlot]:
        resources = re.search(
            rb"/Font\s*<<(.+?)>>", self._page.dict_bytes, re.DOTALL
        )
        if not resources:
            raise ReceiptError("page resources carry no /Font dictionary")

        slots: Dict[str, FontSlot] = {}
        for item in re.finditer(rb"/(F\d+)\s+(\d+)\s+0\s+R", resources.group(1)):
            resource = item.group(1).decode("ascii")
            type0 = int(item.group(2))
            type0_dict = self.pdf.obj(type0).dict_bytes
            descendants = re.search(
                rb"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", type0_dict
            )
            tounicode = self.pdf.resolve_ref(type0_dict, b"/ToUnicode")
            if not descendants or tounicode is None:
                continue
            cidfont = int(descendants.group(1))
            cidfont_dict = self.pdf.obj(cidfont).dict_bytes
            descriptor = self.pdf.resolve_ref(cidfont_dict, b"/FontDescriptor")
            if descriptor is None:
                continue
            descriptor_dict = self.pdf.obj(descriptor).dict_bytes
            fontfile = self.pdf.resolve_ref(descriptor_dict, b"/FontFile2")
            cidset = self.pdf.resolve_ref(descriptor_dict, b"/CIDSet")
            if fontfile is None or cidset is None:
                continue
            basefont = re.search(rb"/BaseFont\s*/([^\s/<>\[\]]+)", type0_dict)
            raw_name = basefont.group(1).decode("latin-1") if basefont else ""
            family = raw_name.split("+", 1)[-1]
            slots[resource] = FontSlot(
                resource=resource,
                type0_obj=type0,
                cidfont_obj=cidfont,
                descriptor_obj=descriptor,
                fontfile_obj=fontfile,
                tounicode_obj=tounicode,
                cidset_obj=cidset,
                family=family,
                cid_to_char=yandex_font.parse_tounicode(
                    self.pdf.obj(tounicode).decoded_stream()
                ),
                widths=yandex_font.parse_widths(cidfont_dict),
            )
        if not slots:
            raise ReceiptError("no usable Type0 fonts on the page")
        return slots

    _TOKEN_RE = re.compile(
        rb"/(F\d+)\s+([\d.]+)\s+Tf"
        rb"|1\s+0\s+0\s+1\s+([-0-9.]+)\s+([-0-9.]+)\s+Tm"
        rb"|\[(?:\\.|[^\]\\])*\]\s*TJ"
        rb"|\(((?:\\.|[^\\()])*)\)\s*Tj"
        rb"|\bET\b",
        re.DOTALL,
    )

    def _parse_runs(self) -> List[TextRun]:
        runs: List[TextRun] = []
        font = ""
        size = 0.0
        x = y = 0.0
        tm_x_span = (0, 0)
        for match in self._TOKEN_RE.finditer(self._content):
            if match.group(1):
                font, size = match.group(1).decode("ascii"), float(match.group(2))
            elif match.group(3):
                x, y = float(match.group(3)), float(match.group(4))
                tm_x_span = (match.start(3), match.end(3))
            elif match.group(5) is not None:
                slot = self.fonts.get(font)
                if slot is None:
                    continue
                raw = yandex_font.decode_literal(match.group(5))
                text, cids = yandex_font.text_of_raw(raw, slot.cid_to_char)
                if not text.strip():
                    continue
                runs.append(
                    TextRun(
                        font=font,
                        size=size,
                        x=x,
                        y=y,
                        text=text,
                        cids=cids,
                        tm_x_span=tm_x_span,
                        literal_span=(match.start(5), match.end(5)),
                    )
                )
        return runs

    def _map_fields(self) -> Dict[str, TextRun]:
        mapping: Dict[str, TextRun] = {}
        for index, run in enumerate(self.runs):
            text = run.text.strip()
            slot = self.fonts.get(run.font)
            if slot and slot.family == MEDIUM_FAMILY and "₽" in text:
                mapping["amount"] = run
                continue
            if _DATETIME_RE.fullmatch(text):
                mapping["datetime"] = run
                continue
            if text.startswith("Квитанция"):
                mapping["receipt"] = run
                continue
            name = LABEL_FIELDS.get(text)
            if name:
                for candidate in self.runs[index + 1 :]:
                    if candidate.text.strip() not in _STRUCTURAL_LABELS:
                        mapping[name] = candidate
                        break
                continue
            if text == "СБП" and run.x < 100:
                sbp_runs = []
                for candidate in self.runs[index + 1 :]:
                    label = candidate.text.strip()
                    if label in _STRUCTURAL_LABELS or label.startswith("Квитанция"):
                        break
                    sbp_runs.append(candidate)
                    if len(sbp_runs) == 2:
                        break
                if len(sbp_runs) >= 1:
                    mapping["sbp_1"] = sbp_runs[0]
                if len(sbp_runs) >= 2:
                    mapping["sbp_2"] = sbp_runs[1]
        return mapping

    # ------------------------------------------------------------- accessors

    @property
    def fields(self) -> Dict[str, str]:
        result = {
            name: run.text
            for name, run in self._fields.items()
            if name not in ("sbp_1", "sbp_2")
        }
        if "sbp_1" in self._fields:
            result["sbp_id"] = self._fields["sbp_1"].text + (
                self._fields["sbp_2"].text if "sbp_2" in self._fields else ""
            )
        return result

    def _require(self, name: str) -> TextRun:
        if name not in self._fields:
            available = ", ".join(sorted(self._fields))
            raise ReceiptError(f"unknown field {name!r}; available: {available}")
        return self._fields[name]

    def donor_printed_seconds(self) -> int:
        raw = self.fields.get("datetime") or ""
        match = re.search(r":(\d{2})$", raw)
        return int(match.group(1)) if match else 0

    def _text_width(self, slot: FontSlot, cids: List[int], size: float) -> float:
        return sum(slot.widths.get(cid, 1000) for cid in cids) / 1000.0 * size

    def _ttf_bytes(self, slot: FontSlot) -> bytes:
        cached = getattr(slot, "_ttf_bytes", None)
        if cached is None:
            cached = self.pdf.obj(slot.fontfile_obj).decoded_stream()
            slot._ttf_bytes = cached  # type: ignore[attr-defined]
        return cached

    def _glyph_filled(self, slot: FontSlot, cid: int) -> bool:
        return len(wb_font.glyph_blob(self._ttf_bytes(slot), cid)) > 0

    # -------------------------------------------------------- amount rendering

    def render_amount(self, value) -> str:
        """Canonical WB rendering: whole rubles, space thousands, ``₽``.

        Genuine donors never print kopecks (``13 ₽``, ``10 ₽``).
        """
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
        else:
            text = str(value).replace("\u200b", "")
        match = _AMOUNT_RE.match(text)
        if not match:
            raise ReceiptError(
                f"cannot parse amount {value!r}; expected whole rubles e.g. '1 230 ₽'"
            )
        digits = re.sub(r"\D", "", match.group(1))
        if not digits:
            raise ReceiptError(f"cannot parse amount {value!r}")
        decimals = match.group(2)
        if decimals and int(decimals.ljust(2, "0")[:2]) != 0:
            raise ReceiptError(
                "WB SBP amount is whole rubles only (genuine receipts have no kopecks)"
            )
        number = int(digits)
        if number <= 0:
            raise ReceiptError("amount must be positive")
        grouped = f"{number:,}".replace(",", " ")
        return f"{grouped} ₽"

    # -------------------------------------------------------------- mutation

    def set(self, name: str, value: str) -> None:
        if name == "amount":
            self.set_amount(value)
            return
        if name == "sbp_id":
            self.set_sbp(value)
            return
        self._set_run(self._require(name), value, align=_ALIGN.get(name, "right"))

    def set_amount(self, value) -> None:
        self._set_run(
            self._require("amount"), self.render_amount(value), align="center"
        )

    def wrap_sbp(self, sbp_id: str) -> Tuple[str, str]:
        """Split a 32-char SBP id at the 245 pt Regular box (measured)."""
        run = self._require("sbp_1")
        slot = self.fonts[run.font]
        char_to_cid = slot.char_to_cid

        def width_of(text: str) -> float:
            cids = yandex_font.cids_of_text(text, char_to_cid)
            return self._text_width(slot, cids, run.size)

        cut = 0
        for index in range(1, len(sbp_id) + 1):
            if width_of(sbp_id[:index]) <= SBP_WRAP_WIDTH:
                cut = index
            else:
                break
        if cut <= 0:
            cut = max(len(sbp_id) - 1, 1)
        line1, line2 = sbp_id[:cut], sbp_id[cut:]
        # Genuine donors always wrap onto two lines.
        if not line2 and line1:
            line1, line2 = line1[:-1], line1[-1]
        return line1, line2

    def set_sbp(self, sbp_id: str) -> None:
        sbp_id = str(sbp_id).strip()
        if len(sbp_id) != 32:
            raise ReceiptError(f"SBP id must be 32 characters, got {sbp_id!r}")
        run = self._require("sbp_1")
        self._ensure_chars(self.fonts[run.font], sbp_id)
        line1, line2 = self.wrap_sbp(sbp_id)
        self._set_run(run, line1, align="right")
        if "sbp_2" in self._fields:
            self._set_run(self._fields["sbp_2"], line2, align="right")

    def _set_run(self, run: TextRun, text: str, *, align: str) -> None:
        slot = self.fonts[run.font]
        self._ensure_chars(slot, text)
        cids = yandex_font.cids_of_text(text, slot.char_to_cid)
        width = self._text_width(slot, cids, run.size)
        if align == "right":
            new_x = RIGHT_EDGE - width
        elif align == "center":
            new_x = PAGE_CENTER - width / 2.0
        else:
            new_x = run.x
        if cids == run.cids and abs(new_x - run.x) < 1e-9:
            run.text = text
            return
        self._replace_run(run, text, cids, new_x)

    def _ensure_chars(self, slot: FontSlot, text: str) -> None:
        missing: List[str] = []
        char_to_cid = slot.char_to_cid
        for char in text:
            cid = char_to_cid.get(char)
            if (
                cid is None
                or cid not in slot.widths
                or (char not in _SPACE_CHARS and not self._glyph_filled(slot, cid))
            ):
                if char not in missing:
                    missing.append(char)
        if missing:
            self._extend_font(slot, missing)

    def _replace_span(self, span: Tuple[int, int], replacement: bytes) -> None:
        start, end = span
        self._content = self._content[:start] + replacement + self._content[end:]
        shift = len(replacement) - (end - start)
        if not shift:
            return
        for run in self.runs:
            for attr in ("tm_x_span", "literal_span"):
                run_start, run_end = getattr(run, attr)
                if run_start >= end:
                    setattr(run, attr, (run_start + shift, run_end + shift))

    def _replace_run(
        self, run: TextRun, text: str, cids: List[int], new_x: float
    ) -> None:
        new_x_bytes = fmt_double(new_x).encode("ascii")
        tm_start = run.tm_x_span[0]
        self._replace_span(run.tm_x_span, new_x_bytes)
        run.tm_x_span = (tm_start, tm_start + len(new_x_bytes))

        raw = yandex_font.encode_literal(yandex_font.raw_of_cids(cids))
        start, end = run.literal_span
        self._content = self._content[:start] + raw + self._content[end:]
        shift = len(raw) - (end - start)
        run.literal_span = (start, start + len(raw))
        if shift:
            for other in self.runs:
                if other is run:
                    continue
                for attr in ("tm_x_span", "literal_span"):
                    run_start, run_end = getattr(other, attr)
                    if run_start >= end:
                        setattr(other, attr, (run_start + shift, run_end + shift))
        run.text = text
        run.cids = cids
        run.x = new_x

    # ------------------------------------------------------------ glyph growth

    def _pool(self) -> wb_font.GlyphPool:
        if self.glyph_pool is None and self._glyph_pool_loader is not None:
            self.glyph_pool = self._glyph_pool_loader()
        if self.glyph_pool is None:
            raise ReceiptError(
                "text needs characters the donor font lacks and no glyph "
                "pool was supplied"
            )
        return self.glyph_pool

    def _extend_font(self, slot: FontSlot, missing: List[str]) -> None:
        pool = self._pool()
        gid_blobs: Dict[int, bytes] = {}
        ttf = self._ttf_bytes(slot)
        limit = yandex_font.num_glyphs(ttf)
        for char in missing:
            donor = pool.get(slot.family, char)
            if donor is None:
                raise ReceiptError(
                    f"no donor glyph for {char!r} in {slot.family}; add a "
                    "genuine WB receipt containing it to banks/wb/donors/"
                )
            existing = slot.char_to_cid.get(char)
            target_cid = existing if existing is not None else donor.cid
            occupant = slot.cid_to_char.get(target_cid)
            if occupant is not None and occupant != char:
                raise ReceiptError(
                    f"CID {target_cid} already maps to {occupant!r}, "
                    f"cannot also map it to {char!r}"
                )
            if target_cid >= limit:
                raise ReceiptError(f"CID {target_cid} outside {slot.family}")
            slot.cid_to_char[target_cid] = char
            slot.widths[target_cid] = donor.width
            self.grown_chars.append(char)
            if self._glyph_filled(slot, target_cid):
                continue
            if donor.blob:
                gid_blobs[target_cid] = donor.blob
            for comp_gid, blob in donor.components.items():
                if blob and not self._glyph_filled(slot, comp_gid):
                    gid_blobs.setdefault(comp_gid, blob)

        if gid_blobs:
            fontfile = self.pdf.obj(slot.fontfile_obj)
            merged = wb_font.inject_raw_glyphs(ttf, gid_blobs)
            fontfile.set_decoded_stream(merged)
            fontfile.dict_bytes = re.sub(
                rb"/Length1\s+\d+",
                b"/Length1 " + str(len(merged)).encode("ascii"),
                fontfile.dict_bytes,
                count=1,
            )
            slot._ttf_bytes = merged  # type: ignore[attr-defined]

        cidfont = self.pdf.obj(slot.cidfont_obj)
        cidfont.dict_bytes = yandex_font.replace_widths(
            cidfont.dict_bytes, slot.widths
        )
        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(yandex_font.build_tounicode(slot.cid_to_char))
        cidset = self.pdf.obj(slot.cidset_obj)
        cidset.set_decoded_stream(
            yandex_font.build_cidset_exact(
                cidset.decoded_stream(), set(slot.cid_to_char)
            )
        )

    # ------------------------------------------------------- dates and save

    def _created_dt(self) -> datetime:
        if self.created_dt is not None:
            moment = self.created_dt
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=MSK)
            return moment.astimezone(timezone.utc).replace(microsecond=0)
        if self.operation_dt is not None:
            return (self.operation_dt + timedelta(minutes=5)).astimezone(
                timezone.utc
            ).replace(microsecond=0)
        return datetime.now(timezone.utc).replace(microsecond=0)

    def _pdf_timestamp(self) -> str:
        return self._created_dt().strftime("D:%Y%m%d%H%M%SZ")

    def _stamp_filesystem(self, path: str) -> None:
        created = self._created_dt() + timedelta(seconds=1)
        ts = created.timestamp()
        os.utime(path, (ts, ts))
        local = created.astimezone()
        stamp = local.strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(
            ["SetFile", "-d", stamp, "-m", stamp, path],
            check=False,
            capture_output=True,
        )

    def _scan_used_cids(self, content: bytes) -> Dict[str, set]:
        used: Dict[str, set] = {}
        font = ""
        for match in self._TOKEN_RE.finditer(content):
            if match.group(1):
                font = match.group(1).decode("ascii")
                continue
            slot = self.fonts.get(font)
            if slot is None:
                continue
            literals = []
            if match.group(5) is not None:
                literals = [match.group(5)]
            elif match.group(3) is None and match.group(0).rstrip().endswith(b"TJ"):
                literals = re.findall(rb"\(((?:\\.|[^\\()])*)\)", match.group(0))
            for literal in literals:
                raw = yandex_font.decode_literal(literal)
                _text, cids = yandex_font.text_of_raw(raw, slot.cid_to_char)
                used.setdefault(font, set()).update(cids)
        return used

    def _prune_fonts(self) -> None:
        used_by_font = self._scan_used_cids(self._content)
        for slot in self.fonts.values():
            used = {
                cid
                for cid in used_by_font.get(slot.resource, set())
                if cid in slot.cid_to_char
            }
            if not used or used == set(slot.cid_to_char):
                continue
            fontfile = self.pdf.obj(slot.fontfile_obj)
            pruned_ttf = yandex_font.prune_ttf(fontfile.decoded_stream(), used)
            fontfile.set_decoded_stream(pruned_ttf)
            fontfile.dict_bytes = re.sub(
                rb"/Length1\s+\d+",
                b"/Length1 " + str(len(pruned_ttf)).encode("ascii"),
                fontfile.dict_bytes,
                count=1,
            )
            slot._ttf_bytes = pruned_ttf  # type: ignore[attr-defined]
            slot.cid_to_char = {
                cid: char for cid, char in slot.cid_to_char.items() if cid in used
            }
            slot.widths = {
                cid: width for cid, width in slot.widths.items() if cid in used
            }
            cidfont = self.pdf.obj(slot.cidfont_obj)
            cidfont.dict_bytes = yandex_font.replace_widths(
                cidfont.dict_bytes, slot.widths
            )
            tounicode = self.pdf.obj(slot.tounicode_obj)
            tounicode.set_decoded_stream(
                yandex_font.build_tounicode(slot.cid_to_char)
            )
            cidset = self.pdf.obj(slot.cidset_obj)
            cidset.set_decoded_stream(
                yandex_font.build_cidset_exact(cidset.decoded_stream(), used)
            )

    def _rotate_subset_tags(self) -> None:
        import secrets
        import string

        for slot in self.fonts.values():
            type0 = self.pdf.obj(slot.type0_obj)
            match = re.search(rb"/BaseFont\s*/([A-Z]{6})\+", type0.dict_bytes)
            if not match:
                continue
            old = match.group(1) + b"+" + slot.family.encode("latin-1")
            while True:
                tag = "".join(
                    secrets.choice(string.ascii_uppercase) for _ in range(6)
                ).encode("ascii")
                if tag != match.group(1):
                    break
            new = tag + b"+" + slot.family.encode("latin-1")
            for obj_num in (slot.type0_obj, slot.cidfont_obj, slot.descriptor_obj):
                obj = self.pdf.obj(obj_num)
                obj.dict_bytes = obj.dict_bytes.replace(b"/" + old, b"/" + new)

    def save(
        self, path: str, *, reidentify: bool = False, mode: str = "incremental"
    ) -> None:
        """Write the receipt.

        Default incremental (donor bytes + tail).  ``mode="natural"`` is
        the checker-facing save: prune subsets, fresh tags, single-pass
        classic xref, compact ``>>stream``.
        """
        if self._content != self._content_original:
            self.pdf.obj(self._content_obj).set_decoded_stream(self._content)
        changed = self._content != self._content_original
        if mode == "natural":
            if changed:
                self._prune_fonts()
                self._rotate_subset_tags()
            if reidentify:
                self.pdf.reidentify(self._pdf_timestamp())
            written = self.pdf.inplace_save(
                path, content_stream_objs={self._content_obj}
            )
            if not written:
                written = self.pdf.refit_save(path, exact_size=False)
            if not written:
                raise ReceiptError("natural save could not be written")
            if reidentify and changed:
                self._stamp_filesystem(path)
            return
        if mode != "incremental":
            raise ReceiptError(f"unknown save mode {mode!r}")
        if reidentify and changed:
            self.pdf.reidentify(self._pdf_timestamp())
        self.pdf.incremental_save(path)
        if reidentify and changed:
            self._stamp_filesystem(path)
