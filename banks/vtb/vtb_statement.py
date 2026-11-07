#!/usr/bin/env python3
"""VTB account statement (``Выписка по счёту``) patcher / generator.

A statement is a *different* document type from the VTB receipts
(openhtmltopdf / ``Чек-PDF``).  Genuine statements are produced by
JasperReports 6.21.4 / OpenPDF 1.3.32:

* PDF-1.5, classic xref ``n \\n`` (space before *and* after n);
* ``>>stream\\n`` (no extra newline), zlib level 6;
* one Type0 Identity-H subset of ArialMT, CID = GID, ToUnicode TTX+0;
* unused Helvetica ``()Tj`` at (0, 842) — left untouched;
* money as ``X.00 RUB`` (no grouping), newest operations first.

A statement is never redrawn: a genuine donor from
``donors_statement/`` is patched field by field.  Unmodified
round-trip reproduces the donor byte for byte.  ``rocket_pdf.rebuild()``
is not used — it would insert ``>>\\nstream``.

Public entry point: ``generate_vtb_statement(...)``.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _path in (_HERE, _REPO_ROOT, _REPO_ROOT / "banks" / "rocket"):
    s = str(_path)
    if s not in sys.path:
        sys.path.append(s)

try:
    import rocket_pdf
except ModuleNotFoundError:  # pragma: no cover
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "rocket_pdf", _REPO_ROOT / "banks" / "rocket" / "rocket_pdf.py"
    )
    rocket_pdf = importlib.util.module_from_spec(_spec)
    sys.modules["rocket_pdf"] = rocket_pdf
    _spec.loader.exec_module(rocket_pdf)

import yandex_font  # noqa: E402

from vtb_receipt import FORMAT_CARD, FORMAT_SBP, Receipt  # noqa: E402

MSK = timezone(timedelta(hours=3))

__all__ = [
    "generate_vtb_statement",
    "Statement",
    "StatementError",
    "parse_statement",
    "format_money",
    "parse_money",
]

# measured on the three genuine donors
HEADER_LEFT = 290.0       # opening / closing, 8 pt
HEADER_RIGHT = 567.0      # income / expenses, 8 pt
COL_DATE = 67.50
COL_PROC = 132.50
COL_AMOUNT = 228.0
COL_INCOME = 308.0
COL_EXPENSE = 388.0
COL_NAME = 574.0
DESC_STEP = 8.05
DONOR_ACCOUNT = "40817810590049129426"
DONOR_HOLDER = "Жеребятьев Александр Евгеньевич"

_TEMPLATE_EMPTY = "vtb_statement_01_empty.pdf"
_TEMPLATE_1P = "vtb_statement_02_ops_1p.pdf"
_TEMPLATE_2P = "vtb_statement_03_ops_2p.pdf"

PathLike = Union[str, Path]


class StatementError(RuntimeError):
    pass


def _fmt_coord(value: float) -> str:
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
        text = str(value or "").strip().replace(" в ", " ").replace(",", "")
        moment = None
        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d.%m.%Y",
        ):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            raise StatementError(f"cannot parse {name} {value!r}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def parse_money(value) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float)):
        amount = Decimal(str(value))
    else:
        text = str(value or "").strip()
        text = text.replace("\u00a0", " ").replace("₽", "").replace("RUB", "")
        text = text.replace("\u2212", "-").replace("\u2013", "-")
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
    """Unsigned/signed statement rendering: ``15.00 RUB`` / ``-15.00 RUB``."""
    amount = parse_money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    return f"{sign}{whole}.{cents:02d} RUB"


def _title_name(value: str) -> str:
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if len(tokens) < 2:
        raise StatementError(f"cannot parse holder name {value!r}")

    def cap(token: str) -> str:
        return "-".join(part.capitalize() for part in token.split("-"))

    return " ".join(cap(t) for t in tokens)


def _split_fio(value: str, n_lines: int, *, uppercase: bool) -> List[str]:
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if not tokens:
        return [" "] * n_lines
    if n_lines <= 0:
        return []
    if len(tokens) >= n_lines:
        parts = tokens[: n_lines - 1] + [" ".join(tokens[n_lines - 1 :])]
    else:
        parts = tokens + [""] * (n_lines - len(tokens))
    out = []
    for part in parts:
        text = part if part else " "
        if uppercase:
            text = text.upper()
        else:
            text = "-".join(p.capitalize() for p in text.split("-")) if text.strip() else " "
        out.append(text)
    return out


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
    page: int
    part: int


def _fonts_of_pdf(pdf) -> List[FontSlot]:
    slots: List[FontSlot] = []
    for obj in pdf.find_objects(b"/Type/Page"):
        resources = re.search(rb"/Font<<(.+?)>>", obj.dict_bytes, re.DOTALL)
        if not resources:
            continue
        for item in re.finditer(rb"/(F\d+)\s+(\d+)\s+0\s+R", resources.group(1)):
            resource = item.group(1).decode("ascii")
            type0 = int(item.group(2))
            type0_dict = pdf.obj(type0).dict_bytes
            if b"/Subtype/Type0" not in type0_dict and b"/Subtype /Type0" not in type0_dict:
                continue
            descendants = re.search(rb"/DescendantFonts\[(\d+)\s+0\s+R", type0_dict)
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
            basefont = re.search(rb"/BaseFont/([^\s/<>\[\]]+)", type0_dict)
            raw_name = basefont.group(1).decode("latin-1") if basefont else ""
            family = raw_name.split("+", 1)[-1]
            slots.append(
                FontSlot(
                    resource=resource,
                    type0_obj=type0,
                    cidfont_obj=cidfont,
                    descriptor_obj=descriptor,
                    fontfile_obj=fontfile,
                    tounicode_obj=tounicode,
                    cidset_obj=cidset,
                    family=family,
                    cid_to_char=yandex_font.parse_tounicode(
                        pdf.obj(tounicode).decoded_stream()
                    ),
                    widths=yandex_font.parse_widths(cidfont_dict),
                )
            )
        if slots:
            break
    if not slots:
        raise StatementError("no ArialMT Type0 font on the statement")
    return slots


_POOL_CACHE: Dict[Tuple[str, ...], yandex_font.GlyphPool] = {}


def default_glyph_donor_paths() -> List[Path]:
    folder = _HERE / "donors_statement"
    return sorted(folder.glob("*.pdf")) if folder.is_dir() else []


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
        except Exception:  # noqa: BLE001
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
# content model
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
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_MONEY_RE = re.compile(r"^-?\d+\.\d{2} RUB$")


@dataclass
class PageContent:
    obj_num: int
    original: bytes
    parts: List[bytes]


class Statement:
    """A genuine VTB statement opened for field-level patching."""

    def __init__(self, path: str, glyph_donor_paths: Optional[List[PathLike]] = None):
        self.path = path
        self.pdf = rocket_pdf.load(path)
        self.glyph_pool: Optional[yandex_font.GlyphPool] = None
        self._extra_glyph_paths = glyph_donor_paths
        self.grown_chars: List[str] = []
        self.fonts: Dict[str, FontSlot] = {s.resource: s for s in _fonts_of_pdf(self.pdf)}
        self.pages: List[PageContent] = []
        page_objs = [
            obj
            for obj in self.pdf.objects.values()
            if b"/Type/Page" in obj.dict_bytes and b"/Contents" in obj.dict_bytes
        ]
        page_objs.sort(key=lambda o: o.num)
        for obj in page_objs:
            cnum = self.pdf.resolve_ref(obj.dict_bytes, b"/Contents")
            if cnum is None:
                continue
            original = self.pdf.obj(cnum).decoded_stream()
            self.pages.append(
                PageContent(obj_num=cnum, original=original, parts=self._split_parts(original))
            )
        if not self.pages:
            raise StatementError("statement has no page content streams")
        self.runs: List[TextRun] = []
        self._parse_runs()
        self.creation_dt: Optional[datetime] = None

    @staticmethod
    def _split_parts(content: bytes) -> List[bytes]:
        parts: List[bytes] = []
        pos = 0
        for match in _BT_RE.finditer(content):
            if match.start() > pos:
                parts.append(content[pos : match.start()])
            parts.append(match.group(0))
            pos = match.end()
        if pos < len(content):
            parts.append(content[pos:])
        return parts

    def _parse_runs(self) -> None:
        self.runs = []
        arial = self.fonts.get("F2")
        for page_i, page in enumerate(self.pages):
            for part_i, part in enumerate(page.parts):
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
                        if font != "F2" or arial is None:
                            continue
                        raw = yandex_font.decode_literal(match.group(5))
                        text, cids = yandex_font.text_of_raw(raw, arial.cid_to_char)
                        if text == "":
                            continue
                        self.runs.append(
                            TextRun(font, size, x, y, text, cids, page_i, part_i)
                        )

    def text_width(self, text: str, size: float) -> float:
        slot = self.fonts["F2"]
        cids = yandex_font.cids_of_text(text, slot.char_to_cid)
        return sum(slot.widths.get(cid, 1000) for cid in cids) / 1000.0 * size

    def _pool(self) -> yandex_font.GlyphPool:
        if self.glyph_pool is None:
            self.glyph_pool = load_glyph_pool(self._extra_glyph_paths)
        return self.glyph_pool

    def ensure_glyphs(self, texts: List[str]) -> None:
        slot = self.fonts["F2"]
        missing: List[str] = []
        for text in texts:
            for char in text:
                if char in missing:
                    continue
                cid = slot.char_to_cid.get(char)
                if cid is None or cid not in slot.widths:
                    missing.append(char)
        if not missing:
            return
        pool = self._pool()
        additions: Dict[int, yandex_font.DonorGlyph] = {}
        ttf_bytes = self.pdf.obj(slot.fontfile_obj).decoded_stream()
        ttf = yandex_font.read_ttf(ttf_bytes)
        for char in missing:
            donor = pool.get(slot.family, char)
            if donor is None:
                raise StatementError(
                    f"no donor glyph for {char!r} in {slot.family}; add a genuine "
                    "VTB statement containing it to donors_statement/"
                )
            target_cid = slot.char_to_cid.get(char, donor.cid)
            occupant = slot.cid_to_char.get(target_cid)
            if occupant is not None and occupant != char:
                raise StatementError(
                    f"CID {target_cid} already maps to {occupant!r}, cannot also map {char!r}"
                )
            if target_cid >= yandex_font.num_glyphs(ttf_bytes):
                raise StatementError(f"CID {target_cid} outside {slot.family}")
            slot.cid_to_char[target_cid] = char
            slot.widths[target_cid] = donor.width
            self.grown_chars.append(char)
            if yandex_font.glyph_has_outline(ttf, target_cid):
                continue
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
        cidfont = self.pdf.obj(slot.cidfont_obj)
        cidfont.dict_bytes = yandex_font.replace_widths(cidfont.dict_bytes, slot.widths)
        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(yandex_font.build_tounicode(slot.cid_to_char))
        cidset = self.pdf.obj(slot.cidset_obj)
        cidset.set_decoded_stream(
            yandex_font.build_cidset(cidset.decoded_stream(), set(slot.cid_to_char))
        )

    def _literal(self, text: str) -> bytes:
        slot = self.fonts["F2"]
        cids = yandex_font.cids_of_text(text, slot.char_to_cid)
        return yandex_font.encode_literal(yandex_font.raw_of_cids(cids))

    def _bt_single(self, size: float, x: float, y: float, text: str) -> bytes:
        return (
            f"BT\n1 0 0 1 {_fmt_coord(x)} {_fmt_coord(y)} Tm\n"
            f"/F2 {_fmt_coord(size)} Tf\n0 0 0 rg\n("
        ).encode("ascii") + self._literal(text) + b")Tj\n0 g\nET\n"

    def set_run_text(
        self, run: TextRun, text: str, *, right_edge: Optional[float] = None
    ) -> None:
        if text == run.text and right_edge is None:
            return
        self.ensure_glyphs([text])
        if right_edge is None:
            new_x = run.x
        else:
            new_x = right_edge - self.text_width(text, run.size)
        run.text = text
        run.x = new_x
        run.cids = yandex_font.cids_of_text(text, self.fonts["F2"].char_to_cid)
        self._rewrite_part(run.page, run.part)

    def _rewrite_part(self, page_i: int, part_i: int) -> None:
        """Rebuild a BT block. Names/descriptions share one BT; replacing
        the whole part with a single run would wipe the sibling lines."""
        runs = [r for r in self.runs if r.page == page_i and r.part == part_i]
        if not runs:
            return
        out = bytearray(b"BT\n")
        for index, run in enumerate(runs):
            out += f"1 0 0 1 {_fmt_coord(run.x)} {_fmt_coord(run.y)} Tm\n".encode("ascii")
            if index == 0:
                out += f"/F2 {_fmt_coord(run.size)} Tf\n".encode("ascii")
            out += b"0 0 0 rg\n(" + self._literal(run.text) + b")Tj\n0 g\n"
        out += b"ET\n"
        self.pages[page_i].parts[part_i] = bytes(out)

    def _stamp_info(self, dt: datetime) -> None:
        info_num = self.pdf.resolve_ref(self.pdf.trailer_dict, b"/Info")
        if info_num is None:
            return
        info = self.pdf.obj(info_num)
        stamp = dt.astimezone(MSK).strftime("D:%Y%m%d%H%M%S+03'00'")
        info.dict_bytes, n = re.subn(
            rb"/CreationDate\(D:[^)]+\)",
            b"/CreationDate(" + stamp.encode("ascii") + b")",
            info.dict_bytes,
            count=1,
        )
        if n == 0:
            raise StatementError("Info dictionary has no CreationDate")
        id_m = re.search(
            rb"/ID\s*\[<([0-9a-fA-F]+)><([0-9a-fA-F]+)>\]", self.pdf.trailer_dict
        )
        if id_m is None:
            return
        width = len(id_m.group(1))
        new_hex = os.urandom((width + 1) // 2).hex()[:width].encode("ascii")
        new_id = b"/ID [<" + new_hex + b"><" + new_hex + b">]"
        self.pdf.trailer_dict = (
            self.pdf.trailer_dict[: id_m.start()] + new_id + self.pdf.trailer_dict[id_m.end() :]
        )
        data_m = re.search(
            rb"/ID\s*\[<([0-9a-fA-F]+)><([0-9a-fA-F]+)>\]", self.pdf.data
        )
        if data_m is not None:
            self.pdf.data = self.pdf.data[: data_m.start()] + new_id + self.pdf.data[data_m.end() :]

    def save(self, path: str) -> None:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        content_objs = set()
        changed = False
        for page in self.pages:
            new = b"".join(page.parts)
            if new != page.original:
                self.pdf.obj(page.obj_num).set_decoded_stream(new)
                content_objs.add(page.obj_num)
                changed = True
        if self.creation_dt is not None:
            self._stamp_info(self.creation_dt)
            changed = True
        if not changed:
            dest.write_bytes(self.pdf.data)
            return
        written = self.pdf.inplace_save(str(dest), content_stream_objs=content_objs)
        if not written:
            written = self.pdf.refit_save(str(dest), exact_size=False)
        if not written:
            raise StatementError("could not write the patched statement")
        if self.creation_dt is not None:
            stamp = self.creation_dt.astimezone(MSK).timestamp()
            os.utime(dest, (stamp, stamp))


# --------------------------------------------------------------------------
# parse operations
# --------------------------------------------------------------------------


@dataclass
class OpSlot:
    page: int
    date_run: TextRun
    proc_run: TextRun
    amount_run: TextRun
    income_run: TextRun
    expense_run: TextRun
    desc_runs: List[TextRun]
    name_runs: List[TextRun]
    kind: str


@dataclass
class ParsedOp:
    op_date: date
    proc_date: date
    amount: Decimal
    income: Decimal
    expense: Decimal
    description: str
    counterparty: str
    kind: str


@dataclass
class ParsedStatement:
    holder: str
    account: str
    period_start: date
    period_end: date
    opening: Decimal
    closing: Decimal
    income: Decimal
    expenses: Decimal
    operations: List[ParsedOp]
    page_count: int
    holder_run: TextRun
    account_run: TextRun
    period_run: TextRun
    opening_run: TextRun
    closing_run: TextRun
    income_run: TextRun
    expenses_run: TextRun
    slots: List[OpSlot]


def _cluster(runs: List[TextRun], step_max: float = 10.0) -> List[List[TextRun]]:
    ordered = sorted(runs, key=lambda r: (-r.page, -r.y, r.x))
    groups: List[List[TextRun]] = []
    for run in ordered:
        if (
            groups
            and groups[-1][0].page == run.page
            and groups[-1][-1].y - run.y <= step_max
        ):
            groups[-1].append(run)
        else:
            groups.append([run])
    return groups


def _kind_of(desc_runs: List[TextRun]) -> str:
    first = desc_runs[0].text if desc_runs else ""
    if first.startswith("Переводы через СБП"):
        return "sbp_msg" if len(desc_runs) > 1 else "sbp"
    if first.startswith("Перевод между своими"):
        return "internal"
    if first.startswith("Зачисление"):
        return "salary"
    if first.startswith("Денежный перевод"):
        return "card"
    return "other"


def parse_slots(stmt: Statement) -> List[OpSlot]:
    date_runs = [
        r
        for r in stmt.runs
        if r.size == 7 and _DATE_RE.match(r.text) and abs(r.x - 32.49) < 2.0
    ]
    date_runs.sort(key=lambda r: (r.page, -r.y))
    desc_all = [r for r in stmt.runs if r.size == 7 and 390 <= r.x <= 500]
    name_all = [r for r in stmt.runs if r.size == 7 and r.x >= 505]
    desc_groups = _cluster(desc_all)
    name_groups = _cluster(name_all)

    def nearest_group(groups: List[List[TextRun]], date_run: TextRun) -> List[TextRun]:
        best = []
        best_d = 1e9
        for group in groups:
            if group[0].page != date_run.page:
                continue
            lo = min(r.y for r in group) - 4
            hi = max(r.y for r in group) + 4
            if lo <= date_run.y <= hi:
                d = abs(sum(r.y for r in group) / len(group) - date_run.y)
                if d < best_d:
                    best, best_d = group, d
        return best

    slots: List[OpSlot] = []
    for date_run in date_runs:
        same = [
            r
            for r in stmt.runs
            if r.page == date_run.page and r.size == 7 and abs(r.y - date_run.y) < 0.2
        ]
        proc = next((r for r in same if abs(r.x - 97.49) < 2 and _DATE_RE.match(r.text)), None)
        amount = next((r for r in same if _MONEY_RE.match(r.text) and r.x < 240), None)
        income = next(
            (r for r in same if _MONEY_RE.match(r.text) and 240 <= r.x < 330), None
        )
        expense = next(
            (r for r in same if _MONEY_RE.match(r.text) and r.x >= 330), None
        )
        if proc is None or amount is None or income is None or expense is None:
            raise StatementError(f"operation row at y={date_run.y} is missing columns")
        desc = nearest_group(desc_groups, date_run)
        names = nearest_group(name_groups, date_run)
        slots.append(
            OpSlot(
                page=date_run.page,
                date_run=date_run,
                proc_run=proc,
                amount_run=amount,
                income_run=income,
                expense_run=expense,
                desc_runs=desc,
                name_runs=names,
                kind=_kind_of(desc),
            )
        )
    return slots


def parse_statement(stmt: Statement) -> ParsedStatement:
    holder_run = next(r for r in stmt.runs if r.size == 14)
    account_run = next(r for r in stmt.runs if re.fullmatch(r"\d{20} \(RUB\)", r.text))
    period_run = next(
        r for r in stmt.runs if re.fullmatch(r"\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4}", r.text)
    )
    opening_run = next(
        r
        for r in stmt.runs
        if r.size == 8 and _MONEY_RE.match(r.text) and abs(r.y - 609.1) < 0.2 and r.x < 300
    )
    income_run = next(
        r
        for r in stmt.runs
        if r.size == 8 and _MONEY_RE.match(r.text) and abs(r.y - 609.1) < 0.2 and r.x > 300
    )
    closing_run = next(
        r
        for r in stmt.runs
        if r.size == 8 and _MONEY_RE.match(r.text) and abs(r.y - 587.1) < 0.2 and r.x < 300
    )
    expenses_run = next(
        r
        for r in stmt.runs
        if r.size == 8 and _MONEY_RE.match(r.text) and abs(r.y - 587.1) < 0.2 and r.x > 300
    )
    p_start, p_end = period_run.text.split(" - ")
    slots = parse_slots(stmt)
    operations = []
    for slot in slots:
        operations.append(
            ParsedOp(
                op_date=_as_date(slot.date_run.text, name="op date"),
                proc_date=_as_date(slot.proc_run.text, name="proc date"),
                amount=parse_money(slot.amount_run.text),
                income=parse_money(slot.income_run.text),
                expense=abs(parse_money(slot.expense_run.text)),
                description=" ".join(r.text for r in slot.desc_runs),
                counterparty=" ".join(r.text for r in slot.name_runs),
                kind=slot.kind,
            )
        )
    return ParsedStatement(
        holder=holder_run.text,
        account=re.sub(r"\D", "", account_run.text),
        period_start=_as_date(p_start, name="period_start"),
        period_end=_as_date(p_end, name="period_end"),
        opening=parse_money(opening_run.text),
        closing=parse_money(closing_run.text),
        income=parse_money(income_run.text),
        expenses=abs(parse_money(expenses_run.text)),
        operations=operations,
        page_count=len(stmt.pages),
        holder_run=holder_run,
        account_run=account_run,
        period_run=period_run,
        opening_run=opening_run,
        closing_run=closing_run,
        income_run=income_run,
        expenses_run=expenses_run,
        slots=slots,
    )


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

_IN_AMOUNTS = [
    Decimal("50.00"),
    Decimal("950.00"),
    Decimal("1000.00"),
    Decimal("2500.00"),
    Decimal("4008.00"),
    Decimal("12995.74"),
    Decimal("22307.00"),
]
_OUT_AMOUNTS = [
    Decimal("15.00"),
    Decimal("109.00"),
    Decimal("127.00"),
    Decimal("224.00"),
    Decimal("225.00"),
    Decimal("373.00"),
    Decimal("2500.00"),
    Decimal("5000.00"),
    Decimal("11000.00"),
    Decimal("11307.00"),
    Decimal("12995.74"),
]
_NAME_POOL = [
    "Иванов Иван Иванович",
    "Петров Петр Петрович",
    "Сидоров Сидор Сидорович",
]


def operation_from_receipt(path: PathLike) -> dict:
    receipt = Receipt(str(path))
    printed = receipt.printed_fields
    amount = parse_money(printed["amount"])
    moment = _as_datetime(printed["date"], name="receipt date")
    if receipt.format == FORMAT_SBP:
        return {
            "kind": "sbp_msg" if printed.get("message") else "sbp_out",
            "datetime": moment,
            "amount": -abs(amount),
            "direction": "expense",
            "description": "Переводы через СБП.",
            "counterparty": printed.get("recipient") or printed.get("headline") or "",
            "message": printed.get("message"),
        }
    if receipt.format == FORMAT_CARD:
        return {
            "kind": "card",
            "datetime": moment,
            "amount": -(abs(amount) + Decimal("99.00")),
            "direction": "expense",
            "description": "Денежный перевод. VTB.",
            "counterparty": "",  # card rows have no FIO; '*' of the mask is not in ArialMT
        }
    raise StatementError(f"{path} is not a VTB SBP/card receipt")


def _prepare_operation(spec: dict, p_start: date, p_end: date) -> dict:
    if not isinstance(spec, dict):
        raise StatementError(f"operation must be a dict, got {spec!r}")
    if spec.get("receipt_path"):
        spec = {**operation_from_receipt(spec["receipt_path"]), **{
            k: v for k, v in spec.items() if k != "receipt_path" and v is not None
        }}
    if "datetime" not in spec:
        raise StatementError(f"operation {spec!r} has no datetime")
    moment = _as_datetime(spec["datetime"], name="operation datetime")
    if not (p_start <= moment.date() <= p_end):
        raise StatementError(
            f"operation {moment:%d.%m.%Y} is outside the period "
            f"{p_start:%d.%m.%Y}-{p_end:%d.%m.%Y}"
        )
    amount = parse_money(spec.get("amount"))
    direction = str(spec.get("direction") or spec.get("side") or "").lower()
    if direction in ("расход", "expense", "debit", "out"):
        amount = -abs(amount)
    elif direction in ("приход", "income", "credit", "in"):
        amount = abs(amount)
    kind = spec.get("kind") or ("sbp_out" if amount < 0 else "internal")
    if kind == "sbp_out" and spec.get("message"):
        kind = "sbp_msg"
    return {
        "kind": kind,
        "dt": moment,
        "amount": amount,
        "description": spec.get("description"),
        "counterparty": spec.get("counterparty") or spec.get("name") or "",
        "message": spec.get("message"),
        "_explicit": True,
    }


def _place(ops: List[dict], slots: List[OpSlot]) -> List[Optional[dict]]:
    placed: List[Optional[dict]] = [None] * len(slots)
    used = set()

    def take(kinds: Tuple[str, ...]) -> Optional[int]:
        for i, slot in enumerate(slots):
            if i not in used and slot.kind in kinds:
                return i
        return None

    leftover: List[dict] = []
    for op in ops:
        kind = op["kind"]
        if kind == "sbp_msg":
            idx = take(("sbp_msg",)) or take(("sbp",))
        elif kind in ("sbp_out", "sbp"):
            idx = take(("sbp", "sbp_msg"))
        elif kind == "card":
            idx = take(("card",))
        elif kind == "salary":
            idx = take(("salary",))
        elif kind == "internal":
            idx = take(("internal",))
        else:
            idx = take((kind,))
        if idx is None:
            leftover.append(op)
            continue
        placed[idx] = op
        used.add(idx)
    for op in leftover:
        idx = next((i for i in range(len(slots)) if i not in used), None)
        if idx is None:
            raise StatementError(
                f"no free slot for {op['kind']} (template has {len(slots)} operations)"
            )
        placed[idx] = op
        used.add(idx)
    return placed


def generate_vtb_statement(
    period_start,
    period_end,
    holder_name: str,
    account: Optional[str] = None,
    *,
    opening_balance=None,
    operations: Optional[list] = None,
    operation_count: Optional[int] = None,
    receipt_paths: Optional[list] = None,
    donor_path: Optional[PathLike] = None,
    glyph_donor_paths: Optional[List[PathLike]] = None,
    output_path: Optional[PathLike] = None,
    generated_at: Optional[datetime] = None,
    random_seed: Optional[int] = None,
    fill_random: bool = True,
    issue_date=None,
) -> Path:
    """Generate a VTB account statement PDF and return its path.

    ``period_start`` / ``period_end`` are dates (``date`` / ``datetime``
    / ``"07.11.2026"``).  ``holder_name`` is the printed ФИО.  ``account``
    is the 20-digit account; by default the genuine donor account is
    kept (the key digit is not invented).

    Operations come from three sources, merged:

    * ``operations=[{datetime, amount, description, counterparty,
      direction/приход-расход, kind}, ...]``
    * ``receipt_paths`` — genuine VTB SBP / card receipts
    * ``fill_random=True`` (default) pads to the donor layout
      (0 / 5 / 15 rows) from the template's own operation pool

    Layouts are not redrawn: 0 operations → 1 empty page; 1–5 → the
    1-page 5-row donor; 6–15 (or any card transfer) → the 2-page
    15-row donor.  ``fill_random=False`` requires 0, 5 or 15 operations.
    """
    import random as _random

    rng = (
        _random.Random(random_seed)
        if random_seed is not None
        else _random.Random(secrets.randbits(64))
    )
    p_start = _as_date(period_start, name="period_start")
    p_end = _as_date(period_end, name="period_end")
    if p_end < p_start:
        raise StatementError("period_end is before period_start")
    holder = _title_name(holder_name)

    if account is None:
        account_digits = DONOR_ACCOUNT
    else:
        digits = re.sub(r"\D", "", str(account))
        if len(digits) == 20:
            account_digits = digits
        elif len(digits) == 4:
            if digits != DONOR_ACCOUNT[-4:]:
                raise StatementError(
                    "account tail does not match the donor account; pass the "
                    "full 20 digits — the key digit is not invented"
                )
            account_digits = DONOR_ACCOUNT
        else:
            raise StatementError(f"account must be 20 digits, got {account!r}")

    explicit: List[dict] = []
    for spec in operations or []:
        explicit.append(_prepare_operation(spec, p_start, p_end))
    for path in receipt_paths or []:
        explicit.append(_prepare_operation(operation_from_receipt(path), p_start, p_end))
    explicit.sort(key=lambda o: o["dt"], reverse=True)

    n_explicit = len(explicit)
    total = operation_count if operation_count is not None else n_explicit
    if total < n_explicit:
        raise StatementError(
            f"operation_count={total} is smaller than the {n_explicit} operations given"
        )
    if total > 15:
        raise StatementError("statement layout supports at most 15 operations (2-page donor)")
    needs_card = any(o["kind"] == "card" for o in explicit)
    if not fill_random:
        if total not in (0, 5, 15):
            raise StatementError(
                "fill_random=False requires 0, 5 or 15 operations "
                "(the genuine 1-page / 2-page layouts)"
            )
        if total != n_explicit:
            raise StatementError(
                "fill_random=False requires operations to fill the layout exactly"
            )
    else:
        if total == 0:
            pass
        elif needs_card or total > 5:
            total = 15
        else:
            total = 5

    if donor_path:
        template = Path(donor_path).expanduser()
    elif total == 0:
        template = _HERE / "donors_statement" / _TEMPLATE_EMPTY
    elif total <= 5:
        template = _HERE / "donors_statement" / _TEMPLATE_1P
    else:
        template = _HERE / "donors_statement" / _TEMPLATE_2P
    if not template.is_file():
        raise FileNotFoundError(f"statement donor not found: {template}")

    if output_path is None:
        output = _HERE / "out" / f"vtb_statement_{p_start:%Y%m%d}_{p_end:%Y%m%d}_{total}ops.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise StatementError("output_path must not overwrite the donor")

    stmt = Statement(str(template), glyph_donor_paths=glyph_donor_paths)
    parsed = parse_statement(stmt)
    slots = parsed.slots
    if total != len(slots):
        raise StatementError(
            f"template {template.name} has {len(slots)} operation slots, need {total}"
        )

    placed = _place(explicit, slots) if explicit else [None] * len(slots)
    if sum(1 for p in placed if p is not None) < len(explicit):
        raise StatementError("could not place every explicit operation")

    span = (p_end - p_start).days
    texts: List[str] = [holder, f"{account_digits} (RUB)", f"{p_start:%d.%m.%Y} - {p_end:%d.%m.%Y}"]

    filled_ops: List[dict] = []
    for i, slot in enumerate(slots):
        op = placed[i]
        if op is None:
            if not fill_random and slots:
                # keep donor values but still rewrite dates into the period
                donor_amount = parse_money(slot.amount_run.text)
            else:
                donor_amount = parse_money(slot.amount_run.text)
            if fill_random:
                pool = _OUT_AMOUNTS if donor_amount < 0 else _IN_AMOUNTS
                amount = rng.choice(pool)
                if donor_amount < 0:
                    amount = -abs(amount)
            else:
                amount = donor_amount
            if span:
                day = p_end - timedelta(days=round(span * i / max(len(slots) - 1, 1)))
            else:
                day = p_end
            moment = datetime(day.year, day.month, day.day, tzinfo=MSK)
            counterparty = " ".join(r.text for r in slot.name_runs)
            op = {
                "kind": slot.kind,
                "dt": moment,
                "amount": amount,
                "description": None,
                "counterparty": counterparty,
                "message": None,
                "_explicit": False,
            }
        filled_ops.append(op)
        texts.append(op["dt"].strftime("%d.%m.%Y"))
        texts.append(format_money(op["amount"]))
        texts.append(format_money(Decimal("0.00")))
        if op.get("counterparty"):
            texts.append(str(op["counterparty"]))
        if op.get("description"):
            texts.append(str(op["description"]))
        if op.get("message"):
            texts.append(str(op["message"]))

    income_total = sum((op["amount"] for op in filled_ops if op["amount"] > 0), Decimal("0"))
    expense_total = -sum((op["amount"] for op in filled_ops if op["amount"] < 0), Decimal("0"))
    opening = (
        parse_money(opening_balance)
        if opening_balance is not None
        else parsed.opening
    )
    closing = opening + income_total - expense_total
    texts.extend(
        [
            format_money(opening),
            format_money(closing),
            format_money(income_total),
            format_money(expense_total),
        ]
    )
    stmt.ensure_glyphs(texts)

    # header
    stmt.set_run_text(parsed.holder_run, holder)
    stmt.set_run_text(parsed.account_run, f"{account_digits} (RUB)")
    stmt.set_run_text(parsed.period_run, f"{p_start:%d.%m.%Y} - {p_end:%d.%m.%Y}")
    stmt.set_run_text(parsed.opening_run, format_money(opening), right_edge=HEADER_LEFT)
    stmt.set_run_text(parsed.closing_run, format_money(closing), right_edge=HEADER_LEFT)
    stmt.set_run_text(parsed.income_run, format_money(income_total), right_edge=HEADER_RIGHT)
    stmt.set_run_text(parsed.expenses_run, format_money(expense_total), right_edge=HEADER_RIGHT)

    for slot, op in zip(slots, filled_ops):
        day_s = op["dt"].strftime("%d.%m.%Y")
        stmt.set_run_text(slot.date_run, day_s, right_edge=COL_DATE)
        stmt.set_run_text(slot.proc_run, day_s, right_edge=COL_PROC)
        amount = op["amount"]
        income = amount if amount > 0 else Decimal("0.00")
        expense = amount if amount < 0 else Decimal("0.00")
        stmt.set_run_text(slot.amount_run, format_money(amount), right_edge=COL_AMOUNT)
        stmt.set_run_text(slot.income_run, format_money(income), right_edge=COL_INCOME)
        stmt.set_run_text(slot.expense_run, format_money(expense), right_edge=COL_EXPENSE)

        if op.get("_explicit") and op.get("description") and slot.desc_runs:
            lines = [ln for ln in str(op["description"]).split("\n") if ln]
            if op.get("message") and slot.kind in ("sbp", "sbp_msg"):
                extra = [ln for ln in str(op["message"]).split("\n") if ln]
                lines = (lines + extra)[: len(slot.desc_runs)]
            if len(lines) == 1 and len(slot.desc_runs) == 1:
                stmt.set_run_text(slot.desc_runs[0], lines[0])
            elif len(lines) == len(slot.desc_runs):
                for run, line in zip(slot.desc_runs, lines):
                    stmt.set_run_text(run, line)
            # otherwise keep the donor description (line counts must match)

        if op.get("_explicit") and op.get("counterparty") and slot.name_runs:
            upper = slot.kind in ("sbp", "sbp_msg")
            parts = _split_fio(str(op["counterparty"]), len(slot.name_runs), uppercase=upper)
            for run, part in zip(slot.name_runs, parts):
                stmt.set_run_text(run, part, right_edge=COL_NAME)

    gen = generated_at or datetime.now(MSK)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=MSK)
    if issue_date is not None and generated_at is None:
        issue = _as_date(issue_date, name="issue_date")
        gen = datetime(issue.year, issue.month, issue.day, 12, 0, tzinfo=MSK)
    stmt.creation_dt = gen.astimezone(MSK)

    stmt.save(str(output))
    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
    ):
        output.unlink(missing_ok=True)
        raise StatementError("generated statement failed the structural sanity check")
    return output
