#!/usr/bin/env python3
"""Yandex Bank statement (``Выписка по договору``) generator.

A statement is a different document type from the Yandex receipts
(see ``banks/yandex/BANK.md``, «Выписка по договору»): the same
JasperReports 6.21.2 / OpenPDF 1.3.32 producer, the same object
layout and the same ``YSText`` font families, but with a holder /
contract header and a table of operations whose description column
wraps — taller descriptions push every lower row and the totals
down, while the signature/footer tail stays fixed to the page.

As with the receipts, a statement is never redrawn: a genuine donor
from ``donors_statement/`` is patched and re-saved in the frozen
receipt configuration (natural single-pass save, exact font
subsets, fresh subset tags, ``CreationDate`` = generation moment,
fresh trailer ``/ID``).  An unmodified round-trip reproduces the
donor byte for byte.

Public entry point: ``generate_yandex_statement(...)`` (bottom).
The receipt engine (``patch_yandex.py`` / ``engine.py``) is not
touched by this module.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _path in (_REPO_ROOT, _HERE, _REPO_ROOT / "banks" / "rocket"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:
    import rocket_pdf
except ModuleNotFoundError:  # pragma: no cover - direct file load fallback
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "rocket_pdf", _REPO_ROOT / "banks" / "rocket" / "rocket_pdf.py"
    )
    rocket_pdf = importlib.util.module_from_spec(_spec)
    sys.modules["rocket_pdf"] = rocket_pdf
    _spec.loader.exec_module(rocket_pdf)

import yandex_font  # noqa: E402

MSK = timezone(timedelta(hours=3))

__all__ = [
    "generate_yandex_statement",
    "Statement",
    "StatementError",
    "parse_statement",
    "wrap_description",
    "format_money",
    "parse_money",
]

# --------------------------------------------------------------------------
# measured layout constants (donor ff937840…, BANK.md «Выписка», этап 1)
# --------------------------------------------------------------------------

PAGE_RIGHT = 570.0          # right edge of header/total values
HEADER_BOTTOM = 603.0       # grey line under the table header
DESC_X = 20.0
DESC_WIDTH = 170.0          # description column 20..190 at F1 10 pt
DESC_STEP = 11.72           # description line leading
FIRST_LINE_DROP = 15.28     # row top -> first description baseline
TIME_DROP = 9.38            # first baseline -> «в ЧЧ:ММ» baseline (F1 8)
ROW_BASE = 15.0             # row height = ROW_BASE + ROW_PER_LINE * lines
ROW_PER_LINE = 11.0
COL_DATE_RIGHT = 260.0      # operation datetime column right edge
COL_PROC_RIGHT = 330.0      # processing date column right edge
COL_AMOUNT_RIGHT = 475.0    # «Сумма в валюте операции» right edge
COL_ESP_RIGHT = 570.0       # «Сумма в валюте ЭСП» right edge
AMOUNT_COL_WIDTH = 95.0
# totals baselines relative to the last row bottom (donor: bottom 256)
TOTALS_DROPS = {"closing": 17.42, "expenses": 45.42, "income": 73.42}
SIGNATURE_Y = 113.72        # fixed signature block («С уважением,»)
MIN_INCOME_Y = 140.0        # lowest honest income-total baseline (1 page)

EN_DASH = "\u2013"          # расход sign in the donor (not a hyphen)
PRODUCT_LINE = "\u00abСчёт ЭДС\u00bb (далее — «Договор»)."

PathLike = Union[str, Path]


class StatementError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _fmt_coord(value: float) -> str:
    """Format like JasperReports: at most 2 decimals, no trailing 0."""
    text = f"{float(value):.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _as_date(value, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise StatementError(f"cannot parse {name} {value!r}; expected ДД.ММ.ГГГГ")


def _as_datetime(value, *, name: str = "datetime") -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip().replace(" в ", " ")
        moment = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            raise StatementError(
                f"cannot parse {name} {value!r}; expected e.g. '03.11.2026 18:11'"
            )
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def parse_money(value) -> Decimal:
    """Parse a money input/text into a Decimal with kopecks."""
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float)):
        amount = Decimal(str(value))
    else:
        text = str(value or "").strip()
        text = text.replace("\u00a0", " ").replace("₽", "").strip()
        text = text.replace(EN_DASH, "-").replace("\u2212", "-")
        text = text.replace(" ", "").replace(",", ".")
        if text in ("", "+", "-"):
            raise StatementError(f"cannot parse money {value!r}")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise StatementError(f"cannot parse money {value!r}") from None
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise StatementError(f"money {value!r} has fractions of a kopeck")
    return amount.quantize(Decimal("0.01"))


def format_money(value: Decimal) -> str:
    """Unsigned statement rendering: ``1 234,56 ₽`` (plain-space groups)."""
    amount = parse_money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    grouped = f"{whole:,}".replace(",", " ")
    return f"{sign}{grouped},{cents:02d} ₽"


def _format_signed(value: Decimal) -> str:
    """Operation/total rendering: ``+40,00 ₽`` / ``–10,00 ₽`` / ``0,00 ₽``."""
    amount = parse_money(value)
    if amount > 0:
        return "+" + format_money(amount)
    if amount < 0:
        return EN_DASH + format_money(-amount)
    return format_money(amount)


def _format_balance(value: Decimal) -> str:
    """Opening/closing rendering: no sign unless negative (donor style)."""
    amount = parse_money(value)
    if amount < 0:
        return EN_DASH + format_money(-amount)
    return format_money(amount)


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------


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
    cid_to_char: Dict[int, str] = dc_field(default_factory=dict)
    widths: Dict[int, int] = dc_field(default_factory=dict)

    @property
    def char_to_cid(self) -> Dict[str, int]:
        return yandex_font.invert_cmap(self.cid_to_char)


@dataclass
class TextRun:
    font: str
    size: float
    x: float
    y: float
    text: str
    cids: List[int]
    part: int  # index of the BT block in Statement._parts


def _fonts_of_pdf(pdf) -> List[FontSlot]:
    """All Type0 font slots of a Yandex document (receipt or statement)."""
    page = None
    for obj in pdf.find_objects(b"/Type/Page"):
        if b"/Contents" in obj.dict_bytes:
            page = obj
            break
    if page is None:
        raise StatementError("no page object with contents found")
    resources = re.search(rb"/Font\s*<<(.+?)>>", page.dict_bytes, re.DOTALL)
    if not resources:
        raise StatementError("page resources carry no /Font dictionary")
    slots: List[FontSlot] = []
    for item in re.finditer(rb"/(F\d+)\s+(\d+)\s+0\s+R", resources.group(1)):
        resource = item.group(1).decode("ascii")
        type0 = int(item.group(2))
        type0_dict = pdf.obj(type0).dict_bytes
        descendants = re.search(rb"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", type0_dict)
        tounicode = pdf.resolve_ref(type0_dict, b"/ToUnicode")
        if not descendants or tounicode is None:
            continue
        cidfont = int(descendants.group(1))
        cidfont_dict = pdf.obj(cidfont).dict_bytes
        descriptor = pdf.resolve_ref(cidfont_dict, b"/FontDescriptor")
        if descriptor is None:
            continue
        descriptor_dict = pdf.obj(descriptor).dict_bytes
        fontfile = pdf.resolve_ref(descriptor_dict, b"/FontFile2")
        cidset = pdf.resolve_ref(descriptor_dict, b"/CIDSet")
        if fontfile is None or cidset is None:
            continue
        basefont = re.search(rb"/BaseFont\s*/([^\s/<>\[\]]+)", type0_dict)
        raw_name = basefont.group(1).decode("latin-1") if basefont else ""
        family = raw_name.split("+", 1)[-1]
        slots.append(FontSlot(
            resource=resource, type0_obj=type0, cidfont_obj=cidfont,
            descriptor_obj=descriptor, fontfile_obj=fontfile,
            tounicode_obj=tounicode, cidset_obj=cidset, family=family,
            cid_to_char=yandex_font.parse_tounicode(pdf.obj(tounicode).decoded_stream()),
            widths=yandex_font.parse_widths(cidfont_dict),
        ))
    if not slots:
        raise StatementError("no usable Type0 fonts on the page")
    return slots


_POOL_CACHE: Dict[Tuple[str, ...], yandex_font.GlyphPool] = {}


def default_glyph_donor_paths() -> List[Path]:
    """Genuine Yandex receipts + statements: the statement glyph pool."""
    paths: List[Path] = []
    for folder in (_HERE / "donors", _HERE / "donors_statement"):
        if folder.is_dir():
            paths.extend(sorted(folder.glob("*.pdf")))
    return paths


def load_glyph_pool(extra_paths: Optional[List[PathLike]] = None) -> yandex_font.GlyphPool:
    paths = default_glyph_donor_paths() + [Path(p).expanduser() for p in (extra_paths or [])]
    key = tuple(str(p) for p in paths)
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]
    pool = yandex_font.GlyphPool()
    for path in paths:
        try:
            pdf = rocket_pdf.load(str(path))
            slots = _fonts_of_pdf(pdf)
        except Exception:  # noqa: BLE001 - a stray file is not a donor
            continue
        for slot in slots:
            try:
                pool.add_font(
                    slot.family,
                    pdf.obj(slot.fontfile_obj).decoded_stream(),
                    slot.cid_to_char,
                    slot.widths,
                )
            except Exception:  # noqa: BLE001
                continue
    _POOL_CACHE[key] = pool
    return pool


# --------------------------------------------------------------------------
# description wrapping (measured on the donor, BANK.md этап 1)
# --------------------------------------------------------------------------


def _wrap_lines(text: str, width_of, max_width: float = DESC_WIDTH) -> List[str]:
    words = [w for w in re.split(r"\s+", str(text or "").strip()) if w]
    if not words:
        raise StatementError("operation description is empty")
    lines: List[str] = []
    current = ""
    for word in words:
        rest = word
        while True:
            candidate = rest if not current else current + " " + rest
            if width_of(candidate) <= max_width + 1e-9:
                current = candidate
                break
            if current:
                # Try to fill the line with a hyphen-terminated prefix.
                prefix = ""
                remainder = ""
                for index, char in enumerate(rest):
                    if char != "-" or index + 1 >= len(rest):
                        continue
                    head, tail = rest[: index + 1], rest[index + 1:]
                    if width_of(current + " " + head) <= max_width + 1e-9:
                        if len(head) > len(prefix):
                            prefix, remainder = head, tail
                if prefix:
                    lines.append(current + " " + prefix)
                    current = ""
                    rest = remainder
                    continue
                lines.append(current)
                current = ""
                continue
            # One word longer than a whole line: split by characters.
            cut = 0
            for index in range(1, len(rest) + 1):
                if width_of(rest[:index]) <= max_width + 1e-9:
                    cut = index
                else:
                    break
            if cut <= 0:
                raise StatementError(f"description word {rest!r} does not fit the column")
            if cut >= len(rest):
                current = rest
                break
            lines.append(rest[:cut])
            rest = rest[cut:]
    if current:
        lines.append(current)
    return lines


def wrap_description(text: str, slot: FontSlot) -> List[str]:
    """Wrap a description to the 170 pt column with the donor's rules."""
    def width_of(chunk: str) -> float:
        return _text_width(slot, _cids_lenient(slot, chunk), 10.0)
    return _wrap_lines(text, width_of)


def _cids_lenient(slot: FontSlot, text: str) -> List[int]:
    char_to_cid = slot.char_to_cid
    cids = []
    for char in text:
        cid = char_to_cid.get(char)
        if cid is None:
            raise StatementError(
                f"no donor glyph for {char!r} in {slot.family}; add a genuine "
                "Yandex receipt/statement containing it to the donor set"
            )
        cids.append(cid)
    return cids


def _text_width(slot: FontSlot, cids: List[int], size: float) -> float:
    return sum(slot.widths.get(cid, 1000) for cid in cids) / 1000.0 * size


# --------------------------------------------------------------------------
# the statement model
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    rb"/(F\d+)\s+([\d.]+)\s+Tf"
    rb"|1\s+0\s+0\s+1\s+([-0-9.]+)\s+([-0-9.]+)\s+Tm"
    rb"|\[(?:\\.|[^\]\\])*\]\s*TJ"
    rb"|\(((?:\\.|[^\\()])*)\)\s*Tj"
    rb"|\bET\b",
    re.DOTALL,
)
_BT_RE = re.compile(rb"BT\n.*?ET\n", re.DOTALL)


class Statement:
    """A Yandex statement opened for editing (donor-preserving)."""

    def __init__(self, path: str, glyph_pool: Optional[yandex_font.GlyphPool] = None):
        self.path = path
        self.pdf = rocket_pdf.load(path)
        self.glyph_pool = glyph_pool
        self.grown_chars: List[str] = []
        self._page = None
        for obj in self.pdf.find_objects(b"/Type/Page"):
            if b"/Contents" in obj.dict_bytes:
                self._page = obj
                break
        if self._page is None:
            raise StatementError("no page object with contents found")
        self._content_obj = self.pdf.resolve_ref(self._page.dict_bytes, b"/Contents")
        if self._content_obj is None:
            raise StatementError("page has no /Contents reference")
        self._content_original = self.pdf.obj(self._content_obj).decoded_stream()
        self.fonts: Dict[str, FontSlot] = {s.resource: s for s in _fonts_of_pdf(self.pdf)}
        self._parts: List[bytes] = self._split_parts(self._content_original)
        self.runs: List[TextRun] = []
        self._parse_runs()

    # ---------------------------------------------------------- parsing

    @staticmethod
    def _split_parts(content: bytes) -> List[bytes]:
        parts: List[bytes] = []
        pos = 0
        for match in _BT_RE.finditer(content):
            if match.start() > pos:
                parts.append(content[pos:match.start()])
            parts.append(match.group(0))
            pos = match.end()
        if pos < len(content):
            parts.append(content[pos:])
        return parts

    @property
    def content(self) -> bytes:
        return b"".join(self._parts)

    def _parse_runs(self) -> None:
        self.runs = []
        for part_index, part in enumerate(self._parts):
            if not part.startswith(b"BT"):
                continue
            font = ""
            size = 0.0
            x = y = 0.0
            for match in _TOKEN_RE.finditer(part):
                if match.group(1):
                    font, size = match.group(1).decode("ascii"), float(match.group(2))
                elif match.group(3):
                    x, y = float(match.group(3)), float(match.group(4))
                elif match.group(5) is not None:
                    slot = self.fonts.get(font)
                    if slot is None:
                        continue
                    raw = yandex_font.decode_literal(match.group(5))
                    text, cids = yandex_font.text_of_raw(raw, slot.cid_to_char)
                    if text == "":
                        continue
                    self.runs.append(TextRun(font, size, x, y, text, cids, part_index))
                elif match.group(0).rstrip().endswith(b"TJ"):
                    slot = self.fonts.get(font)
                    if slot is None:
                        continue
                    text = ""
                    cids: List[int] = []
                    for literal in re.findall(rb"\(((?:\\.|[^\\()])*)\)", match.group(0)):
                        raw = yandex_font.decode_literal(literal)
                        chunk, chunk_cids = yandex_font.text_of_raw(raw, slot.cid_to_char)
                        text += chunk
                        cids.extend(chunk_cids)
                    if text.strip():
                        self.runs.append(TextRun(font, size, x, y, text, cids, part_index))

    def _slot_ttf(self, slot: FontSlot):
        if not hasattr(slot, "_ttf"):
            slot._ttf = yandex_font.read_ttf(  # type: ignore[attr-defined]
                self.pdf.obj(slot.fontfile_obj).decoded_stream()
            )
        return slot._ttf  # type: ignore[attr-defined]

    def _glyph_filled(self, slot: FontSlot, cid: int) -> bool:
        return yandex_font.glyph_has_outline(self._slot_ttf(slot), cid)

    def text_width(self, slot: FontSlot, text: str, size: float) -> float:
        return _text_width(slot, _cids_lenient(slot, text), size)

    # ------------------------------------------------------ glyph growth

    def _pool(self) -> yandex_font.GlyphPool:
        if self.glyph_pool is None:
            self.glyph_pool = load_glyph_pool()
        return self.glyph_pool

    def ensure_glyphs(self, slot: FontSlot, texts: List[str]) -> None:
        """Grow the native subset with glyphs from genuine donors."""
        missing: List[str] = []
        for text in texts:
            for char in text:
                if char in missing:
                    continue
                cid = slot.char_to_cid.get(char)
                if (
                    cid is None
                    or cid not in slot.widths
                    or (char not in " \u00a0" and not self._glyph_filled(slot, cid))
                ):
                    missing.append(char)
        if not missing:
            return
        pool = self._pool()
        additions: Dict[int, yandex_font.DonorGlyph] = {}
        for char in missing:
            donor = pool.get(slot.family, char)
            if donor is None:
                raise StatementError(
                    f"no donor glyph for {char!r} in {slot.family}; add a genuine "
                    "Yandex receipt/statement containing it to the donor set"
                )
            target_cid = slot.char_to_cid.get(char, donor.cid)
            occupant = slot.cid_to_char.get(target_cid)
            if occupant is not None and occupant != char:
                raise StatementError(
                    f"CID {target_cid} already maps to {occupant!r}, "
                    f"cannot also map it to {char!r}"
                )
            if target_cid >= yandex_font.num_glyphs(
                self.pdf.obj(slot.fontfile_obj).decoded_stream()
            ):
                raise StatementError(f"CID {target_cid} outside {slot.family}")
            slot.cid_to_char[target_cid] = char
            slot.widths[target_cid] = donor.width
            self.grown_chars.append(char)
            if self._glyph_filled(slot, target_cid):
                continue
            glyph = donor.glyph
            filled = getattr(glyph, "isComposite", lambda: False)() or getattr(
                glyph, "numberOfContours", 0
            ) != 0
            if filled:
                additions[target_cid] = donor
        if additions:
            fontfile = self.pdf.obj(slot.fontfile_obj)
            merged = yandex_font.inject_glyphs(fontfile.decoded_stream(), additions)
            fontfile.set_decoded_stream(merged)
            fontfile.dict_bytes = re.sub(
                rb"/Length1\s+\d+",
                b"/Length1 " + str(len(merged)).encode("ascii"),
                fontfile.dict_bytes,
                count=1,
            )
            if hasattr(slot, "_ttf"):
                delattr(slot, "_ttf")
        cidfont = self.pdf.obj(slot.cidfont_obj)
        cidfont.dict_bytes = yandex_font.replace_widths(cidfont.dict_bytes, slot.widths)
        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(yandex_font.build_tounicode(slot.cid_to_char))
        cidset = self.pdf.obj(slot.cidset_obj)
        cidset.set_decoded_stream(
            yandex_font.build_cidset(cidset.decoded_stream(), set(slot.cid_to_char))
        )

    # ------------------------------------------------------- block build

    def _literal(self, slot: FontSlot, text: str) -> bytes:
        cids = yandex_font.cids_of_text(text, slot.char_to_cid)
        return yandex_font.encode_literal(yandex_font.raw_of_cids(cids))

    def _bt_single(self, run_font: str, size: float, x: float, y: float, text: str) -> bytes:
        slot = self.fonts[run_font]
        return (
            f"BT\n1 0 0 1 {_fmt_coord(x)} {_fmt_coord(y)} Tm\n"
            f"/{run_font} {_fmt_coord(size)} Tf\n0 0 0 rg\n("
        ).encode("ascii") + self._literal(slot, text) + b")Tj\n0 g\nET\n"

    def _bt_description(self, lines: List[str], first_y: float) -> bytes:
        slot = self.fonts["F1"]
        out = bytearray()
        for index, line in enumerate(lines):
            y = first_y - index * DESC_STEP
            if index == 0:
                out += (
                    f"BT\n1 0 0 1 20 {_fmt_coord(y)} Tm\n/F1 10 Tf\n0 0 0 rg\n("
                ).encode("ascii")
            else:
                out += f"1 0 0 1 20 {_fmt_coord(y)} Tm\n0 0 0 rg\n(".encode("ascii")
            out += self._literal(slot, line) + b")Tj\n0 g\n"
        out += b"ET\n"
        return bytes(out)

    @staticmethod
    def _gap_separator(x1: float, x2: float, bottom: float, *, last: bool = False) -> bytes:
        text = (
            "1 0 0 1 0 0 cm\n0.25 w\n0 J\n0.74902 0.74902 0.74902 RG\n[] 0 d\n"
            f"{_fmt_coord(x1)} {_fmt_coord(bottom)} m\n"
            f"{_fmt_coord(x2)} {_fmt_coord(bottom)} l\nS\n[] 0 d\n2 J\n"
        )
        if last:
            text += "[] 0 d\n2 J\n"
        text += "1 0 0 1 0 0 cm\n"
        return text.encode("ascii")

    def _right_x(self, slot: FontSlot, text: str, size: float, right: float) -> float:
        return right - self.text_width(slot, text, size)

    # ----------------------------------------------------- single edit

    def set_run_text(self, run: TextRun, text: str, *, right_edge: Optional[float] = None) -> None:
        """Replace one single-run BT block's text (Stage-3 primitive)."""
        block_runs = [r for r in self.runs if r.part == run.part]
        if len(block_runs) != 1:
            raise StatementError("set_run_text only supports single-run blocks")
        slot = self.fonts[run.font]
        self.ensure_glyphs(slot, [text])
        if right_edge is None:
            right_edge = run.x + self.text_width(slot, run.text, run.size)
        new_x = right_edge - self.text_width(slot, text, run.size)
        self._parts[run.part] = self._bt_single(run.font, run.size, new_x, run.y, text)
        run.text = text
        run.x = new_x
        run.cids = yandex_font.cids_of_text(text, slot.char_to_cid)

    # ------------------------------------------------- subset exactness

    def _scan_used_cids(self, content: bytes) -> Dict[str, set]:
        used: Dict[str, set] = {}
        font = ""
        for match in _TOKEN_RE.finditer(content):
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
        used_by_font = self._scan_used_cids(self.content)
        for slot in self.fonts.values():
            used = {
                cid for cid in used_by_font.get(slot.resource, set())
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
            if hasattr(slot, "_ttf"):
                delattr(slot, "_ttf")
            slot.cid_to_char = {c: ch for c, ch in slot.cid_to_char.items() if c in used}
            slot.widths = {c: w for c, w in slot.widths.items() if c in used}
            cidfont = self.pdf.obj(slot.cidfont_obj)
            cidfont.dict_bytes = yandex_font.replace_widths(cidfont.dict_bytes, slot.widths)
            tounicode = self.pdf.obj(slot.tounicode_obj)
            tounicode.set_decoded_stream(yandex_font.build_tounicode(slot.cid_to_char))
            cidset = self.pdf.obj(slot.cidset_obj)
            cidset.set_decoded_stream(
                yandex_font.build_cidset_exact(cidset.decoded_stream(), used)
            )

    def _rotate_subset_tags(self) -> None:
        import string

        for slot in self.fonts.values():
            type0 = self.pdf.obj(slot.type0_obj)
            match = re.search(rb"/BaseFont\s*/([A-Z]{6})\+", type0.dict_bytes)
            if not match:
                continue
            old = match.group(1) + b"+" + slot.family.encode("latin-1")
            while True:
                tag = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6)).encode("ascii")
                if tag != match.group(1):
                    break
            new = tag + b"+" + slot.family.encode("latin-1")
            for obj_num in (slot.type0_obj, slot.cidfont_obj, slot.descriptor_obj):
                obj = self.pdf.obj(obj_num)
                obj.dict_bytes = obj.dict_bytes.replace(b"/" + old, b"/" + new)

    def _stamp_filesystem(self, path: str, created_utc: datetime) -> None:
        created = created_utc + timedelta(seconds=1)
        ts = created.timestamp()
        os.utime(path, (ts, ts))
        local = created.astimezone()
        stamp = local.strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(
            ["SetFile", "-d", stamp, "-m", stamp, path],
            check=False, capture_output=True,
        )

    # ---------------------------------------------------------------- save

    def save(self, path: str, *, reidentify: bool = False, mode: str = "incremental",
             creation_dt: Optional[datetime] = None) -> None:
        """Write the statement.

        Default is the donor-preserving incremental save (an unchanged
        statement round-trips byte for byte).  ``mode="natural"`` is
        the checker-facing save of the frozen receipt configuration:
        single pass, one ``startxref``/``%%EOF``, no ``/Prev``; with
        ``reidentify=True`` the font subsets are first pruned to
        exactly the used glyphs, all subset tags are re-issued, the
        trailer ``/ID`` is fresh and ``CreationDate`` is
        ``creation_dt`` (the generation moment).
        """
        content = self.content
        changed = content != self._content_original
        if changed:
            self.pdf.obj(self._content_obj).set_decoded_stream(content)
        if mode == "natural":
            if reidentify and changed:
                if creation_dt is None:
                    raise StatementError("natural reidentified save needs creation_dt")
                self._prune_fonts()
                self._rotate_subset_tags()
                stamp = creation_dt.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
                self.pdf.reidentify(stamp)
            written = self.pdf.inplace_save(path, content_stream_objs={self._content_obj})
            if not written:
                written = self.pdf.refit_save(path, exact_size=False)
            if not written:
                raise StatementError("natural save could not be written")
            if reidentify and changed and creation_dt is not None:
                self._stamp_filesystem(
                    path, creation_dt.astimezone(timezone.utc)
                )
            return
        if mode != "incremental":
            raise StatementError(f"unknown save mode {mode!r}")
        if reidentify and changed:
            if creation_dt is None:
                raise StatementError("reidentified save needs creation_dt")
            stamp = creation_dt.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
            self.pdf.reidentify(stamp)
        self.pdf.incremental_save(path)
        if reidentify and changed and creation_dt is not None:
            self._stamp_filesystem(path, creation_dt.astimezone(timezone.utc))


# --------------------------------------------------------------------------
# reading a statement back
# --------------------------------------------------------------------------


@dataclass
class ParsedStatement:
    iskh: str
    doc_date: date
    holder_name: str
    holder_birth: date
    holder_passport: str
    contract_number: str
    contract_date: date
    period_start: date
    period_end: date
    opening: Decimal
    closing: Decimal
    income_total: Decimal
    expense_total: Decimal
    operations: List[dict]
    texts: List[str]


def parse_statement(stmt: Statement) -> ParsedStatement:
    runs = stmt.runs
    texts = [r.text for r in runs]

    def find(pattern: str) -> TextRun:
        for run in runs:
            if re.fullmatch(pattern, run.text.strip()):
                return run
        raise StatementError(f"run {pattern!r} not found; texts: {texts!r}")

    iskh_run = next(r for r in runs if r.text.startswith("Исх. № "))
    iskh = iskh_run.text[len("Исх. № "):].strip()
    doc_run = find(r"Дата \d{2}\.\d{2}\.\d{4}")
    doc_date = _as_date(doc_run.text[len("Дата "):], name="doc_date")

    holder_run = next(r for r in runs if r.font == "F2" and abs(r.y - 752.72) < 0.01 and r.x > 100)
    birth_run = next(r for r in runs if r.text.startswith("Дата рождения "))
    birth = _as_date(birth_run.text[len("Дата рождения "):], name="holder_birth")
    passport_run = next(r for r in runs if r.text.startswith("Паспорт РФ № "))
    passport = passport_run.text[len("Паспорт РФ № "):].strip()

    contract_run = next(r for r in runs if "№ Э" in r.text and "от" in r.text)
    contract_match = re.search(r"№\s+(Э\d+)\s+от\s+(\d{2}\.\d{2}\.\d{4})", contract_run.text)
    if not contract_match:
        raise StatementError(f"cannot parse contract run {contract_run.text!r}")
    contract_number = contract_match.group(1)
    contract_date = _as_date(contract_match.group(2), name="contract_date")

    period_run = next(r for r in runs if r.font == "F1" and abs(r.size - 14) < 0.01)
    period_match = re.search(
        r"с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", period_run.text
    )
    if not period_match:
        raise StatementError(f"cannot parse period run {period_run.text!r}")
    period_start = _as_date(period_match.group(1), name="period_start")
    period_end = _as_date(period_match.group(2), name="period_end")

    def money_of(run: TextRun) -> Decimal:
        return parse_money(run.text)

    opening_label = next(r for r in runs if r.text.startswith("Входящий остаток на"))
    opening_value = next(
        r for r in runs if r.font == "F2" and abs(r.y - opening_label.y) < 0.01 and r.x > 100
    )
    closing_label = next(r for r in runs if r.text.startswith("Исходящий остаток за"))
    closing_value = next(
        r for r in runs if r.font == "F2" and abs(r.y - closing_label.y) < 0.01 and r.x > 100
    )
    expense_label = next(r for r in runs if r.text.strip() == "Всего расходных операций")
    expense_value = next(
        r for r in runs if r.font == "F2" and abs(r.y - expense_label.y) < 0.01 and r.x > 100
    )
    income_label = next(r for r in runs if r.text.strip() == "Всего приходных операций")
    income_value = next(
        r for r in runs if r.font == "F2" and abs(r.y - income_label.y) < 0.01 and r.x > 100
    )

    # Operations: description BT blocks (F1 10 at x=20, between the
    # table header and the signature), grouped by BT part.
    desc_parts: List[int] = []
    for run in runs:
        if (
            run.font == "F1" and abs(run.size - 10) < 0.01 and run.x == 20
            and SIGNATURE_Y + 40 < run.y < HEADER_BOTTOM
            and run.part not in desc_parts
        ):
            desc_parts.append(run.part)
    operations: List[dict] = []
    for part in desc_parts:
        lines = [r.text for r in runs if r.part == part]
        description = ""
        for line in lines:
            if not description:
                description = line
            elif description.endswith("-"):
                description += line
            else:
                description += " " + line
        first_y = max(r.y for r in runs if r.part == part)
        date_run = next(
            r for r in runs
            if r.font == "F1" and abs(r.size - 10) < 0.01
            and abs(r.y - first_y) < 0.01 and 190 < r.x < 260
        )
        time_run = next(
            r for r in runs
            if r.font == "F1" and abs(r.size - 8) < 0.01
            and abs(r.y - (first_y - TIME_DROP)) < 0.05
        )
        proc_run = next(
            r for r in runs
            if r.font == "F1" and abs(r.size - 10) < 0.01
            and abs(r.y - first_y) < 0.01 and 260 < r.x < 330
        )
        amount_runs = sorted(
            (r for r in runs
             if r.font == "F1" and abs(r.size - 10) < 0.01
             and abs(r.y - first_y) < 0.01 and r.x > 330),
            key=lambda r: r.x,
        )
        if len(amount_runs) != 2:
            raise StatementError(f"operation at y={first_y} has {len(amount_runs)} amount runs")
        op_date = _as_date(date_run.text, name="operation date")
        time_match = re.fullmatch(r"в\s+(\d{2}):(\d{2})", time_run.text.strip())
        if not time_match:
            raise StatementError(f"cannot parse operation time {time_run.text!r}")
        moment = datetime(
            op_date.year, op_date.month, op_date.day,
            int(time_match.group(1)), int(time_match.group(2)), tzinfo=MSK,
        )
        operations.append({
            "datetime": moment,
            "processing_date": _as_date(proc_run.text, name="processing date"),
            "description": description,
            "amount": money_of(amount_runs[0]),
            "amount_esp": money_of(amount_runs[1]),
            "first_y": first_y,
        })
    operations.sort(key=lambda op: -op["first_y"])

    return ParsedStatement(
        iskh=iskh, doc_date=doc_date,
        holder_name=holder_run.text.strip(),
        holder_birth=birth, holder_passport=passport,
        contract_number=contract_number, contract_date=contract_date,
        period_start=period_start, period_end=period_end,
        opening=money_of(opening_value), closing=money_of(closing_value),
        income_total=abs(money_of(income_value)),
        expense_total=abs(money_of(expense_value)),
        operations=operations, texts=texts,
    )


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def _normalise_holder(value: str) -> str:
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if len(tokens) < 2:
        raise StatementError(f"cannot parse holder name {value!r}")
    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.split("-"))
    return " ".join(title(t) for t in tokens)


def _normalise_passport(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 10:
        raise StatementError(f"passport {value!r} must have 10 digits (серия + номер)")
    return f"{digits[:4]} {digits[4:]}"


def _prepare_operation(spec: dict, period_start: date, period_end: date) -> dict:
    if not isinstance(spec, dict):
        raise StatementError(f"operation must be a dict, got {spec!r}")
    if spec.get("card"):
        raise StatementError(
            "filled «Карта» column is not in the donor; a statement with "
            "card operations needs a genuine donor showing it"
        )
    if "datetime" not in spec:
        raise StatementError(f"operation {spec!r} has no datetime")
    moment = _as_datetime(spec["datetime"], name="operation datetime")
    if not (period_start <= moment.date() <= period_end):
        raise StatementError(
            f"operation {moment:%d.%m.%Y} is outside the period "
            f"{period_start:%d.%m.%Y}-{period_end:%d.%m.%Y}"
        )
    description = " ".join(str(spec.get("description") or "").split())
    if not description:
        raise StatementError("operation description is empty")
    amount = parse_money(spec.get("amount"))
    if amount == 0:
        raise StatementError("operation amount is zero")
    if "processing_date" in spec and spec["processing_date"] is not None:
        processing = _as_date(spec["processing_date"], name="processing_date")
        if processing < moment.date():
            raise StatementError("processing date is before the operation date")
    else:
        processing = moment.date()
    return {
        "datetime": moment,
        "processing_date": processing,
        "description": description,
        "amount": amount,
    }


def generate_yandex_statement(
    period_start,
    period_end,
    holder_name: str,
    holder_birth,
    holder_passport,
    opening_balance,
    operations: Optional[list] = None,
    *,
    contract_date=None,
    contract_number: Optional[str] = None,
    issue_date=None,
    donor_path: Optional[PathLike] = None,
    glyph_donor_paths: Optional[List[PathLike]] = None,
    output_path: Optional[PathLike] = None,
    generated_at: Optional[datetime] = None,
) -> Path:
    """Generate a Yandex statement PDF and return its path.

    ``period_start``/``period_end``/``issue_date``/``contract_date``
    are dates (``date``/``datetime`` or ``"03.11.2026"``);
    ``holder_name`` is the full ``Фамилия Имя Отчество`` as printed,
    ``holder_birth`` a birth date, ``holder_passport`` the 10 digits
    (``"2721 923501"``).  ``opening_balance`` is a money value; the
    closing balance and the income/expense totals are computed from
    ``operations`` so the printed arithmetic always agrees:
    closing = opening + income − expenses, totals = sums of the
    printed operations by sign.

    ``operations`` is a list of dicts ``{"datetime": …, "description":
    …, "amount": signed money, "processing_date": … (optional, the
    operation date by default)}``.  Operations are printed in
    chronological order; descriptions wrap to the 170 pt column with
    the donor's rules and taller rows push the totals down.  The
    «Карта» column stays empty, the product stays «Счёт ЭДС» and the
    currency stays ₽, as in the donor — anything else (a filled card
    column, a second page) is honestly refused: it needs genuine
    donors showing it.  ``contract_number`` defaults to
    ``Э`` + contract date + 9 opaque digits, ``issue_date`` to the
    generation date; ``Исх. №`` (issue date + 7 digits), the printed
    «Дата» and the PDF ``CreationDate`` (= ``generated_at``, the
    generation moment) are linked to the issue as in the receipts.
    """
    p_start = _as_date(period_start, name="period_start")
    p_end = _as_date(period_end, name="period_end")
    if p_end < p_start:
        raise StatementError("period_end is before period_start")
    holder = _normalise_holder(holder_name)
    birth = _as_date(holder_birth, name="holder_birth")
    passport = _normalise_passport(holder_passport)
    opening = parse_money(opening_balance)

    gen = generated_at or datetime.now(MSK)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=MSK)
    gen_msk = gen.astimezone(MSK)
    issue = _as_date(issue_date, name="issue_date") if issue_date is not None else gen_msk.date()

    c_date = _as_date(contract_date, name="contract_date") if contract_date is not None else p_start
    if contract_number is None:
        contract = "Э" + c_date.strftime("%Y%m%d") + f"{secrets.randbelow(1_000_000_000):09d}"
    else:
        contract = str(contract_number).strip()
        if not re.fullmatch(r"Э\d{17}", contract):
            raise StatementError(
                f"contract_number {contract_number!r} must look like Э + 17 digits (donor format)"
            )

    prepared = [_prepare_operation(spec, p_start, p_end) for spec in (operations or [])]
    prepared.sort(key=lambda op: op["datetime"])

    income_total = sum((op["amount"] for op in prepared if op["amount"] > 0), Decimal("0"))
    expense_total = -sum((op["amount"] for op in prepared if op["amount"] < 0), Decimal("0"))
    closing = opening + income_total - expense_total

    # --- template ------------------------------------------------------
    if donor_path is not None:
        template = Path(donor_path).expanduser()
    else:
        candidates = sorted((_HERE / "donors_statement").glob("*.pdf"))
        if not candidates:
            raise StatementError(
                "no statement donor in banks/yandex/donors_statement/; "
                "a genuine Yandex statement is needed as a template"
            )
        template = candidates[0]
    if not template.is_file():
        raise FileNotFoundError(f"statement donor not found: {template}")
    if output_path is None:
        output = _HERE / "out" / f"{uuid.uuid4()}.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise StatementError("output_path must not overwrite the donor")

    stmt = Statement(str(template))
    if glyph_donor_paths:
        stmt.glyph_pool = load_glyph_pool(glyph_donor_paths)
    donor = parse_statement(stmt)
    f1 = stmt.fonts["F1"]
    f2 = stmt.fonts["F2"]

    # --- linked issue identifiers --------------------------------------
    while True:
        iskh = issue.strftime("%Y%m%d") + f"{secrets.randbelow(10_000_000):07d}"
        if iskh != donor.iskh:
            break
    iskh_text = f"Исх. № {iskh}"
    doc_date_text = "Дата " + issue.strftime("%d.%m.%Y")
    intro_contract = (
        f"№ {contract} от {c_date.strftime('%d.%m.%Y')} в рамках продукта"
    )
    period_text = (
        f"Выписка по Договору за период с {p_start.strftime('%d.%m.%Y')} "
        f"по {p_end.strftime('%d.%m.%Y')}"
    )
    birth_text = "Дата рождения " + birth.strftime("%d.%m.%Y")
    passport_text = f"Паспорт РФ № {passport}"
    opening_label = "Входящий остаток на " + p_start.strftime("%d.%m.%Y")
    closing_label = "Исходящий остаток за " + p_end.strftime("%d.%m.%Y")
    opening_text = _format_balance(opening)
    closing_text = _format_balance(closing)
    income_text = _format_signed(income_total)
    expense_text = _format_signed(-expense_total)

    # --- glyph coverage first (wrap widths need the final font) --------
    f1_texts = [
        iskh_text, doc_date_text, intro_contract, birth_text, passport_text,
        period_text,
    ]
    f2_texts = [holder, opening_label, closing_label, opening_text,
                closing_text, income_text, expense_text]
    for op in prepared:
        moment = op["datetime"]
        f1_texts.append(op["description"])
        f1_texts.append(moment.strftime("%d.%m.%Y"))
        f1_texts.append("в " + moment.strftime("%H:%M"))
        f1_texts.append(op["processing_date"].strftime("%d.%m.%Y"))
        f1_texts.append(_format_signed(op["amount"]))
    stmt.ensure_glyphs(f1, f1_texts)
    stmt.ensure_glyphs(f2, f2_texts)

    # Period title and amounts must physically fit their columns.
    if stmt.text_width(f1, period_text, 14) > PAGE_RIGHT - 20:
        raise StatementError("period title does not fit the page width")
    if stmt.text_width(f2, holder, 10) > PAGE_RIGHT - 365:
        raise StatementError("holder name is too wide for the header block")
    for op in prepared:
        amount_text = _format_signed(op["amount"])
        if stmt.text_width(f1, amount_text, 10) > AMOUNT_COL_WIDTH:
            raise StatementError(f"amount {amount_text!r} does not fit its column")

    wrapped = [wrap_description(op["description"], f1) for op in prepared]
    heights = [ROW_BASE + ROW_PER_LINE * len(lines) for lines in wrapped]
    last_bottom = HEADER_BOTTOM - sum(heights)
    # The template's own last-row bottom, read from its totals
    # geometry (closing baseline + its drop; 256 on the donor).
    donor_last_bottom = _donor_last_bottom(stmt)
    delta = last_bottom - donor_last_bottom
    income_y = last_bottom - TOTALS_DROPS["income"]
    if income_y < MIN_INCOME_Y:
        raise StatementError(
            "operations do not fit one page (Страница 1 из 1): the totals "
            "would run into the signature block; fewer/shorter operations "
            "or a genuine multi-page statement donor is needed"
        )

    # --- rebuild the content stream ------------------------------------
    parts = stmt._parts
    runs = stmt.runs

    def donor_run(part_index: int) -> TextRun:
        block = [r for r in runs if r.part == part_index]
        if len(block) != 1:
            raise StatementError("expected a single-run donor block")
        return block[0]

    first_desc_part = min(op_part for op_part in _desc_part_indexes(stmt))
    opening_part = next(r.part for r in runs if r.text.startswith("Входящий остаток на"))

    new_parts: List[bytes] = []
    # prefix: everything before the first operation, with header edits
    for index in range(first_desc_part):
        part = parts[index]
        if not part.startswith(b"BT"):
            new_parts.append(part)
            continue
        block = [r for r in runs if r.part == index]
        if len(block) != 1:
            new_parts.append(part)
            continue
        run = block[0]
        text = run.text
        if text.startswith("Исх. № "):
            slot = stmt.fonts[run.font]
            new_parts.append(stmt._bt_single(
                run.font, run.size,
                stmt._right_x(slot, iskh_text, run.size, PAGE_RIGHT), run.y, iskh_text))
        elif re.fullmatch(r"Дата \d{2}\.\d{2}\.\d{4}", text.strip()):
            slot = stmt.fonts[run.font]
            new_parts.append(stmt._bt_single(
                run.font, run.size,
                stmt._right_x(slot, doc_date_text, run.size, PAGE_RIGHT), run.y, doc_date_text))
        elif "№ Э" in text and "от" in text:
            new_parts.append(stmt._bt_single(run.font, run.size, run.x, run.y, intro_contract))
        elif run.font == "F2" and abs(run.y - 752.72) < 0.01 and run.x > 100:
            slot = stmt.fonts[run.font]
            new_parts.append(stmt._bt_single(
                run.font, run.size,
                stmt._right_x(slot, holder, run.size, PAGE_RIGHT), run.y, holder))
        elif text.startswith("Дата рождения "):
            slot = stmt.fonts[run.font]
            new_parts.append(stmt._bt_single(
                run.font, run.size,
                stmt._right_x(slot, birth_text, run.size, PAGE_RIGHT), run.y, birth_text))
        elif text.startswith("Паспорт РФ № "):
            slot = stmt.fonts[run.font]
            new_parts.append(stmt._bt_single(
                run.font, run.size,
                stmt._right_x(slot, passport_text, run.size, PAGE_RIGHT), run.y, passport_text))
        elif run.font == "F1" and abs(run.size - 14) < 0.01:
            new_parts.append(stmt._bt_single(run.font, run.size, run.x, run.y, period_text))
        else:
            new_parts.append(part)

    # operation rows
    top = HEADER_BOTTOM
    for op, lines in zip(prepared, wrapped):
        height = ROW_BASE + ROW_PER_LINE * len(lines)
        bottom = top - height
        first_y = top - FIRST_LINE_DROP
        moment = op["datetime"]
        date_text = moment.strftime("%d.%m.%Y")
        time_text = "в " + moment.strftime("%H:%M")
        proc_text = op["processing_date"].strftime("%d.%m.%Y")
        amount_text = _format_signed(op["amount"])
        is_last = op is prepared[-1]
        new_parts.append(stmt._bt_description(lines, first_y))
        new_parts.append(stmt._gap_separator(20, 190, bottom))
        new_parts.append(stmt._bt_single(
            "F1", 10, stmt._right_x(f1, date_text, 10, COL_DATE_RIGHT), first_y, date_text))
        new_parts.append(stmt._bt_single(
            "F1", 8, stmt._right_x(f1, time_text, 8, COL_DATE_RIGHT),
            first_y - TIME_DROP, time_text))
        new_parts.append(stmt._gap_separator(190, 260, bottom))
        new_parts.append(stmt._bt_single(
            "F1", 10, stmt._right_x(f1, proc_text, 10, COL_PROC_RIGHT), first_y, proc_text))
        new_parts.append(stmt._gap_separator(260, 330, bottom))
        new_parts.append(stmt._gap_separator(330, 380, bottom))
        new_parts.append(stmt._bt_single(
            "F1", 10, stmt._right_x(f1, amount_text, 10, COL_AMOUNT_RIGHT), first_y, amount_text))
        new_parts.append(stmt._gap_separator(380, 475, bottom))
        new_parts.append(stmt._bt_single(
            "F1", 10, stmt._right_x(f1, amount_text, 10, COL_ESP_RIGHT), first_y, amount_text))
        new_parts.append(stmt._gap_separator(475, 570, bottom, last=is_last))
        top = bottom

    # suffix: opening balances, totals (shifted), fixed signature/footer
    totals_by_y = sorted(
        (r for r in runs if r.font == "F2" and r.part >= opening_part),
        key=lambda r: -r.y,
    )
    for index in range(opening_part, len(parts)):
        part = parts[index]
        if not part.startswith(b"BT"):
            new_parts.append(part)
            continue
        block = [r for r in runs if r.part == index]
        if len(block) != 1:
            new_parts.append(part)
            continue
        run = block[0]
        text = run.text
        slot = stmt.fonts[run.font]
        if text.startswith("Входящий остаток на"):
            new_parts.append(stmt._bt_single(run.font, run.size, run.x, run.y, opening_label))
        elif text.startswith("Исходящий остаток за"):
            new_parts.append(stmt._bt_single(
                run.font, run.size, run.x, run.y + delta, closing_label))
        elif text.strip() == "Всего расходных операций":
            new_parts.append(stmt._bt_single(run.font, run.size, run.x, run.y + delta, text))
        elif text.strip() == "Всего приходных операций":
            new_parts.append(stmt._bt_single(run.font, run.size, run.x, run.y + delta, text))
        elif run.font == "F2" and run.x > 100:
            # a right-aligned F2 value: opening (unshifted) or a total
            if abs(run.y - 648.58) < 0.01:
                new_parts.append(stmt._bt_single(
                    run.font, run.size,
                    stmt._right_x(slot, opening_text, run.size, PAGE_RIGHT), run.y, opening_text))
            else:
                # order by donor height: closing, expenses, income
                peers = [r for r in totals_by_y if r.x > 100 and abs(r.y - 648.58) >= 0.01]
                role = {id(peers[0]): closing_text, id(peers[1]): expense_text,
                        id(peers[2]): income_text}[id(run)]
                new_parts.append(stmt._bt_single(
                    run.font, run.size,
                    stmt._right_x(slot, role, run.size, PAGE_RIGHT), run.y + delta, role))
        else:
            new_parts.append(part)

    stmt._parts = new_parts
    stmt._parse_runs()

    output.parent.mkdir(parents=True, exist_ok=True)
    stmt.save(
        str(output), reidentify=True, mode="natural",
        creation_dt=gen_msk,
    )

    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/Prev" in raw
    ):
        output.unlink(missing_ok=True)
        raise StatementError("natural save failed: generated file is not a single-pass PDF")
    return output


def _desc_part_indexes(stmt: Statement) -> List[int]:
    indexes: List[int] = []
    for run in stmt.runs:
        if (
            run.font == "F1" and abs(run.size - 10) < 0.01 and run.x == 20
            and SIGNATURE_Y + 40 < run.y < HEADER_BOTTOM
            and run.part not in indexes
        ):
            indexes.append(run.part)
    if not indexes:
        raise StatementError("donor has no operation rows to use as a template")
    return indexes


def _donor_last_bottom(stmt: Statement) -> float:
    """The template's last row bottom, from its totals geometry."""
    closing = next(r for r in stmt.runs if r.text.startswith("Исходящий остаток за"))
    return closing.y + TOTALS_DROPS["closing"]
