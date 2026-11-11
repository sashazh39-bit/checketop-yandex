#!/usr/bin/env python3
"""Field-level model of a Yandex Bank receipt.

A Yandex receipt is a one-page JasperReports/OpenPDF document.  Every
line is its own ``BT ... Tm /F1 10 Tf (<2-byte CIDs>)Tj ET`` block in
the page content stream (object 7 in the donors inspected); values are
right-aligned so the text matrix x is ``570 - text width``.  The amount
(``Сумма операции``) is written with the Type0 font ``F1``
(``YSText-Regular``, CIDFontType2, ``/CIDToGIDMap /Identity``):

    ========  ======================  ======
    char      CID (hex)               width
    ========  ======================  ======
    ``0-9``   ``01FC-0205`` (508-517) 550
    space     ``0003``                200
    NBSP      ``0004`` (some donors)  200
    ``.``     ``022E`` (558)          239
    ``,``     ``022F`` (559)          239
    ``₽``     ``026F`` (623)          550
    ========  ======================  ======

The subset already contains every digit (dates, phones and IDs use
them all), so a plain amount change touches only the content stream.
If a needed character is missing anyway, :meth:`Receipt.set_amount`
grows the native YSText subset with glyphs harvested from other
genuine Yandex receipts (see ``yandex_font.GlyphPool``) instead of
substituting a foreign font.

Saving follows the Rocket pattern: the donor bytes are kept verbatim
and changed objects are appended as a PDF incremental update (the
low-level layer is ``rocket_pdf``, whose no-change round-trip is
byte-identical on Yandex donors too).  By default nothing else is
touched; ``save(..., reidentify=True)`` — used by the full-field
generator in ``patch_yandex.py`` — additionally rotates the trailer
``/ID`` and rewrites the Info CreationDate from the operation moment
(see ``banks/yandex/BANK.md`` for the linkage rules).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    import rocket_pdf
except ModuleNotFoundError:  # rocket moved into banks/rocket/ (chat 00)
    import importlib.util
    import sys
    from pathlib import Path

    _rocket_pdf_path = (
        Path(__file__).resolve().parent / "banks" / "rocket" / "rocket_pdf.py"
    )
    _spec = importlib.util.spec_from_file_location("rocket_pdf", _rocket_pdf_path)
    rocket_pdf = importlib.util.module_from_spec(_spec)
    sys.modules["rocket_pdf"] = rocket_pdf
    _spec.loader.exec_module(rocket_pdf)

import yandex_font

MSK = timezone(timedelta(hours=3))

AMOUNT_LABEL = "Сумма операции"
AMOUNT_FONT_FAMILY = "YSText-Regular"

# Grey labels, in the wording the bank uses, mapped to logical fields.
LABEL_FIELDS: Dict[str, str] = {
    "Дата и время операции МСК": "datetime",
    "Тип операции": "type",
    "Статус": "status",
    "Банк отправителя": "bank_from",
    "Телефон отправителя": "phone_from",
    "ФИО отправителя": "sender",
    "Телефон получателя": "phone_to",
    "ФИО получателя": "recipient",
    "Банк получателя": "bank_to",
    "Сообщение отправителя": "message",
    "Сумма операции": "amount",
    "Комиссия": "fee",
    "Код авторизации": "auth",
    "Идентификатор операции в СБП": "sbp_id",
}

# Characters that mean "a space" in a user-typed amount.
_SPACE_VARIANTS = " \u00a0\u2009\u202f"

_AMOUNT_RE = re.compile(
    r"^\s*(\d[\d \u00a0\u2009\u202f]*?)(?:[,.](\d{2}))?"
    r"(?:\s*(?:₽|руб\.?|RUB))?\s*$",
    re.IGNORECASE,
)


class ReceiptError(RuntimeError):
    pass


@dataclass
class TextRun:
    """One ``(<literal>)Tj`` run in the page content stream."""

    font: str
    size: float
    x: float
    y: float
    text: str
    cids: List[int]
    tm_x_span: Tuple[int, int]  # span of the Tm x number in the content
    literal_span: Tuple[int, int]  # span of the escaped literal body


@dataclass
class FontSlot:
    """Every PDF object that together makes up one Type0 font resource."""

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
    """A Yandex receipt opened for amount editing."""

    def __init__(
        self,
        path: str,
        glyph_pool: Optional[yandex_font.GlyphPool] = None,
        glyph_pool_loader: Optional[Callable[[], yandex_font.GlyphPool]] = None,
    ):
        self.path = path
        self.pdf = rocket_pdf.load(path)
        self.glyph_pool = glyph_pool
        self._glyph_pool_loader = glyph_pool_loader
        self.grown_chars: List[str] = []  # characters grown into the font
        self._page = self._find_page()
        self._content_obj = self.pdf.resolve_ref(self._page.dict_bytes, b"/Contents")
        if self._content_obj is None:
            raise ReceiptError("page has no /Contents reference")
        self._content = self.pdf.obj(self._content_obj).decoded_stream()
        self._content_original = self._content
        self.fonts = self._load_fonts()
        self.runs = self._parse_runs()
        self._fields = self._map_fields()
        self._amount_run = self._map_amount()
        # Exact operation moment (MSK, with seconds) once a new datetime
        # is known; drives CreationDate on a reidentified save.
        self.operation_dt: Optional[datetime] = None
        self.creation_delta = timedelta(minutes=5)

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
            # Yandex embeds TrueType (/FontFile2), Rocket embeds CFF (/FontFile3).
            fontfile = self.pdf.resolve_ref(descriptor_dict, b"/FontFile2")
            cidset = self.pdf.resolve_ref(descriptor_dict, b"/CIDSet")
            if fontfile is None or cidset is None:
                continue

            basefont = re.search(rb"/BaseFont\s*/([^\s/<>\[\]]+)", type0_dict)
            raw_name = basefont.group(1).decode("latin-1") if basefont else ""
            family = raw_name.split("+", 1)[-1]  # strip subset prefix ABCDEF+

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

    def _map_amount(self) -> TextRun:
        """The amount value is the run right after its grey-less label."""
        for index, run in enumerate(self.runs):
            if run.text.strip() != AMOUNT_LABEL:
                continue
            for candidate in self.runs[index + 1:]:
                if "₽" in candidate.text:
                    slot = self.fonts[candidate.font]
                    if slot.family != AMOUNT_FONT_FAMILY:
                        raise ReceiptError(
                            f"amount uses {slot.family}, expected {AMOUNT_FONT_FAMILY}"
                        )
                    return candidate
                break  # the value run immediately follows the label
        raise ReceiptError(f"label {AMOUNT_LABEL!r} with a ₽ value not found")

    def _map_fields(self) -> Dict[str, TextRun]:
        """Pair each grey label with the value run that follows it."""
        mapping: Dict[str, TextRun] = {}
        for index, run in enumerate(self.runs):
            text = run.text.strip()
            if text.startswith("Исх. № "):
                mapping["iskh"] = run
                continue
            if re.fullmatch(r"Дата \d{2}\.\d{2}\.\d{4}", text):
                mapping["doc_date"] = run
                continue
            name = LABEL_FIELDS.get(text)
            if not name:
                continue
            for candidate in self.runs[index + 1 :]:
                if candidate.text.strip() not in LABEL_FIELDS:
                    mapping[name] = candidate
                    break
        return mapping

    # ------------------------------------------------------------- accessors

    @property
    def fields(self) -> Dict[str, str]:
        return {name: run.text for name, run in self._fields.items()}

    def _require(self, name: str) -> TextRun:
        if name not in self._fields:
            available = ", ".join(sorted(self._fields))
            raise ReceiptError(f"unknown field {name!r}; available: {available}")
        return self._fields[name]

    def donor_sbp_seconds(self) -> int:
        """Seconds encoded in the donor's SBP id (chars 10-11, 1-based)."""
        sbp = self.fields.get("sbp_id", "")
        if len(sbp) >= 11 and sbp[5:11].isdigit():
            return int(sbp[9:11])
        return 0

    @property
    def amount(self) -> str:
        """The amount exactly as the text layer reads it."""
        return self._amount_run.text

    @property
    def amount_slot(self) -> FontSlot:
        return self.fonts[self._amount_run.font]

    def _text_width(self, slot: FontSlot, cids: List[int], size: float) -> float:
        return sum(slot.widths.get(cid, 1000) for cid in cids) / 1000.0 * size

    def _glyph_filled(self, slot: FontSlot, cid: int) -> bool:
        if not hasattr(slot, "_ttf"):
            slot._ttf = yandex_font.read_ttf(  # type: ignore[attr-defined]
                self.pdf.obj(slot.fontfile_obj).decoded_stream()
            )
        return yandex_font.glyph_has_outline(slot._ttf, cid)  # type: ignore[attr-defined]

    # -------------------------------------------------------- amount rendering

    def render_amount(self, value: str, grouping_char: Optional[str] = None) -> str:
        """Canonical Yandex rendering of a user-typed amount.

        ``"10000,00"`` becomes ``"10 000,00 ₽"``: thousands are grouped,
        the decimal separator is a comma and the ruble sign is appended.
        Grouping keeps the donor's own separator (a plain space in
        ``yandex_fresh.pdf``, NBSP in most UUID-named donors); the space
        before ``₽`` is a plain space in every donor inspected.
        """
        match = _AMOUNT_RE.match(value.replace("\u200b", ""))
        if not match:
            raise ReceiptError(
                f"cannot parse amount {value!r}; expected e.g. '10 000,00 ₽'"
            )
        digits = re.sub(r"\D", "", match.group(1))
        decimals = match.group(2) or "00"
        slot = self.amount_slot

        if grouping_char is None:
            grouping_char = self._donor_grouping_char(slot)
        groups: List[str] = []
        rest = digits
        while len(rest) > 3:
            groups.append(rest[-3:])
            rest = rest[:-3]
        groups.append(rest)
        grouped = grouping_char.join(reversed(groups))
        return f"{grouped},{decimals} ₽"

    def _donor_grouping_char(self, slot: FontSlot) -> str:
        original = self._amount_run.text
        integer_part = original.split(",", 1)[0]
        if "\u00a0" in integer_part:
            return "\u00a0"
        if " " in integer_part.strip() and len(integer_part.strip()) > 4:
            return " "
        # Donor amount had no grouping (< 1000 ₽): prefer NBSP, which is
        # what most genuine Yandex receipts use, when the font has it.
        if "\u00a0" in slot.char_to_cid:
            return "\u00a0"
        return " "

    # -------------------------------------------------------------- mutation

    def set(self, name: str, value: str) -> None:
        """Set any field by label name, growing YSText glyphs when needed."""
        if name == "amount":
            self.set_amount(value)
            return
        self._set_run(self._require(name), value)

    def _set_run(self, run: TextRun, text: str) -> None:
        slot = self.fonts[run.font]
        char_to_cid = slot.char_to_cid
        missing = []
        for char in text:
            cid = char_to_cid.get(char)
            if (
                cid is None
                or cid not in slot.widths
                or (
                    char not in _SPACE_VARIANTS
                    and not self._glyph_filled(slot, cid)
                )
            ):
                if char not in missing:
                    missing.append(char)
        if missing:
            self._extend_font(slot, missing)
            char_to_cid = slot.char_to_cid

        cids = yandex_font.cids_of_text(text, char_to_cid)
        if cids == run.cids:
            run.text = text
            return  # already reads exactly like this
        right_edge = run.x + self._text_width(slot, run.cids, run.size)
        new_x = right_edge - self._text_width(slot, cids, run.size)
        if new_x < 200:  # value column starts at x=200 in every donor
            raise ReceiptError(
                f"value {text!r} is too wide for the receipt column"
            )
        self._replace_run(run, text, cids, new_x)

    def set_amount(self, value: str, grouping_char: Optional[str] = None) -> None:
        """Replace only the amount, growing YSText glyphs when needed."""
        rendered = self.render_amount(value, grouping_char=grouping_char)
        self._set_run(self._amount_run, rendered)

    def _right_edge(self, slot: FontSlot) -> float:
        run = self._amount_run
        return run.x + self._text_width(slot, run.cids, run.size)

    @staticmethod
    def _format_coord(value: float) -> str:
        """Format like JasperReports: at most 2 decimals, no trailing 0."""
        text = f"{value:.2f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

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

    def _replace_amount_run(self, text: str, cids: List[int], new_x: float) -> None:
        self._replace_run(self._amount_run, text, cids, new_x)

    def _replace_run(
        self, run: TextRun, text: str, cids: List[int], new_x: float
    ) -> None:
        # The Tm sits before the literal: _replace_span shifts the spans
        # of every later run (and this run's literal span) for us.
        new_x_bytes = self._format_coord(new_x).encode("ascii")
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

    def _pool(self) -> yandex_font.GlyphPool:
        if self.glyph_pool is None and self._glyph_pool_loader is not None:
            self.glyph_pool = self._glyph_pool_loader()
        if self.glyph_pool is None:
            raise ReceiptError(
                "amount needs characters the donor font lacks and no glyph "
                "pool was supplied; pass --glyph-donor with other genuine "
                "Yandex receipts"
            )
        return self.glyph_pool

    def _extend_font(self, slot: FontSlot, missing: List[str]) -> None:
        """Grow the native YSText subset with glyphs from other donors."""
        pool = self._pool()
        additions: Dict[int, yandex_font.DonorGlyph] = {}
        for char in missing:
            donor = pool.get(slot.family, char)
            if donor is None:
                raise ReceiptError(
                    f"no donor glyph for {char!r} in {slot.family}; add a "
                    "genuine Yandex receipt containing it to the donor set"
                )
            existing = slot.char_to_cid.get(char)
            target_cid = existing if existing is not None else donor.cid
            occupant = slot.cid_to_char.get(target_cid)
            if occupant is not None and occupant != char:
                raise ReceiptError(
                    f"CID {target_cid} already maps to {occupant!r}, "
                    f"cannot also map it to {char!r}"
                )
            if target_cid >= yandex_font.num_glyphs(
                self.pdf.obj(slot.fontfile_obj).decoded_stream()
            ):
                raise ReceiptError(f"CID {target_cid} outside {slot.family}")
            slot.cid_to_char[target_cid] = char
            slot.widths[target_cid] = donor.width
            self.grown_chars.append(char)
            # Empty donor glyphs (space, NBSP) need no outline copied —
            # the slot is already empty and its hmtx advance is retained
            # in every subset.  Real outlines are injected at the same GID.
            if self._glyph_filled(slot, target_cid):
                continue
            if self._donor_filled(donor):
                additions[target_cid] = donor

        if additions:
            fontfile = self.pdf.obj(slot.fontfile_obj)
            merged = yandex_font.inject_glyphs(
                fontfile.decoded_stream(), additions
            )
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
        cidfont.dict_bytes = yandex_font.replace_widths(
            cidfont.dict_bytes, slot.widths
        )

        tounicode = self.pdf.obj(slot.tounicode_obj)
        tounicode.set_decoded_stream(yandex_font.build_tounicode(slot.cid_to_char))

        cidset = self.pdf.obj(slot.cidset_obj)
        cidset.set_decoded_stream(
            yandex_font.build_cidset(
                cidset.decoded_stream(), set(slot.cid_to_char)
            )
        )

    @staticmethod
    def _donor_filled(donor: yandex_font.DonorGlyph) -> bool:
        glyph = donor.glyph
        if getattr(glyph, "isComposite", lambda: False)():
            return True
        try:
            coords = glyph.coordinates
        except Exception:  # noqa: BLE001 - fall back to contour count
            return getattr(glyph, "numberOfContours", 0) != 0
        return len(coords) > 0

    # ------------------------------------------------------------------ save

    # ------------------------------------------------------- dates and save

    def _payment_dt(self) -> Optional[datetime]:
        """The operation moment (MSK) — set value wins over the field text."""
        if self.operation_dt is not None:
            return self.operation_dt
        raw = self.fields.get("datetime") or ""
        match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+в\s+(\d{2}):(\d{2})", raw)
        if not match:
            return None
        day, month, year, hour, minute = (int(p) for p in match.groups())
        return datetime(
            year, month, day, hour, minute,
            second=self.donor_sbp_seconds(), tzinfo=MSK,
        )

    def _created_dt(self) -> datetime:
        """PDF creation moment: a few minutes after the operation, in UTC.

        The donors put /CreationDate between ~0.5 min and ~4 hours after
        the operation (median ~43 min, see BANK.md); we mirror the Rocket
        convention of +5 minutes, inside the observed range.
        """
        payment = self._payment_dt()
        if payment is None:
            return datetime.now(timezone.utc).replace(microsecond=0)
        return (payment + self.creation_delta).astimezone(timezone.utc)

    def _pdf_timestamp(self) -> str:
        return self._created_dt().strftime("D:%Y%m%d%H%M%SZ")

    def _stamp_filesystem(self, path: str) -> None:
        """Set mtime/atime (and macOS birthtime) to the creation moment."""
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

    # ------------------------------------------------- subset exactness

    def _scan_used_cids(self, content: bytes) -> Dict[str, set]:
        """{font resource: CIDs actually drawn} over a content stream.

        Covers both ``(...)Tj`` runs and the ``[(...)]TJ`` arrays the
        footer/logo use, so the font subsets can be made exact.
        """
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
        """Make every font subset exactly the glyphs the new text uses.

        Genuine subsets satisfy ToUnicode == CIDSet == filled outlines
        == used characters; donor glyphs left over from replaced values
        (and nothing else) are removed — ToUnicode entries, ``/W``
        entries, CIDSet bits and the TTF outlines themselves.
        """
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
            if hasattr(slot, "_ttf"):
                delattr(slot, "_ttf")
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
        """Fresh 6-letter subset tags, as a fresh bank subsetting run."""
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

        Default: donor bytes verbatim + incremental update (the
        amount-only contract).  ``mode="natural"`` is the checker-facing
        Rocket-style save: subsets pruned to the used glyphs, fresh
        subset tags, then a single-pass classic-xref rewrite at its
        natural size (surgical in-place first when it fits) — no
        incremental tail, no ``/Prev``, one ``startxref``/``%%EOF``.

        With ``reidentify=True`` (the full-field generator) the file
        also gets a fresh unique trailer /ID and an Info CreationDate
        derived from the operation moment — as in
        ``rocket_receipt.Receipt.save``.
        """
        if self._content != self._content_original:
            self.pdf.obj(self._content_obj).set_decoded_stream(self._content)
        changed = self._content != self._content_original
        if mode == "natural":
            if reidentify and changed:
                self._prune_fonts()
                self._rotate_subset_tags()
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
