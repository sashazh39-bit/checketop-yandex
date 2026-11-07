#!/usr/bin/env python3
"""Bot-facing engine for MTS Bank SBP receipts.

This is the MTS analogue of ``banks/yandex/engine.py`` /
``banks/rocket/engine.py``: one function, ``generate_mts(...)``, takes
the already-collected transfer data and writes a finished PDF through
the MTS patcher (``mts_receipt.py`` + ``mts_font.py`` +
``mts_pdf.py`` in this folder).  The patcher re-emits a genuine donor
as a single-pass OpenPDF file -- the round-trip test proves the writer
reproduces every genuine donor byte-for-byte
(``python3 banks/mts/test_mts.py``).

Contract (mirrors ``generate_rocket``)::

    generate_mts(
        amount="15,00 ₽",                          # or 15 / "15.5" -> "15,50 ₽"
        operation_dt=datetime(..., tzinfo=MSK),    # MSK; naive = MSK
        recipient_name="Жеребятьев Александр Евгеньевич",
        recipient_phone="+7 900 351-70-80",        # any RU format
        recipient_bank="Т-Банк",
        sender_account=None,                       # 20 digits, or donor's
        message=None,                              # default "Перевод СБП"
        output_path="/path/to/out.pdf",
    ) -> pathlib.Path

Field rules measured on the genuine donors (banks/mts/BANK.md):

* the receipt carries NO sender block -- only the recipient, the
  recipient's phone/bank, the own debit account, amounts and the
  operation identifiers;
* the printed name form is ``Имя Отчество Ф`` (no trailing dot, unlike
  Yandex);
* the phone prints as ``+7 (993) 134-95-57``;
* the amount prints as ``15,00 ₽`` (comma kopecks always shown, space
  thousands grouping); Итого always equals Сумма (комиссия 0,00 ₽);
* every value is right-aligned to x = 355 - width, and the SBP id
  wraps at the 167 pt box -- both recomputed from the real font
  metrics on every generation;
* identifiers tied to the operation are re-issued, never copied:
  the 12-digit transaction code (a bank counter growing ~172/s,
  anchored on the donor corpus), the 32-char SBP id (UTC timestamp
  header + serial re-rolled in the donor's own character set, era
  marker/tail by the operation's UTC date), the Info CorrelationId
  and the trailer /ID.  ``CreationDate``/``ModDate`` are the export
  (generation) moment, UTC, as on genuine re-downloads;
* the printed header time leads the SBP-encoded UTC moment by a few
  seconds (genuine donors: +6/+6/+15 s) -- the same delta is modelled.

Glyph pool
----------
The bank's MTSSans v1.0 font is not publicly available (the public
MTS Sans is v1.003 at 2048 upm with different outlines -- BANK.md), so
glyphs are harvested from the genuine donors in ``donors/`` (the
embedded subsets carry the original v1.0 tables and outlines).  A
string the pool cannot type is an honest ``GlyphCoverageError`` asking
for another genuine MTS receipt, never a foreign-font substitution.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mts_font import FontPool, FontCoverageError, OtfGlyphSource  # noqa: E402
from mts_receipt import MSK, Receipt, ReceiptError  # noqa: E402

__all__ = ["generate_mts", "GlyphCoverageError", "ReceiptError",
           "make_sbp_id", "make_txcode", "build_pool"]

DONORS_DIR = _HERE / "donors"
DEFAULT_OUT_DIR = _HERE / "out"

# --------------------------------------------------------------------------
# SBP id rules (BANK.md): era = system-wide marker/tail by UTC op date
# --------------------------------------------------------------------------
_SBP_ERAS: Tuple[Tuple[datetime, str, str], ...] = (
    (datetime(2026, 11, 3, tzinfo=MSK), "0B", "0011881102"),
    (datetime(2026, 11, 6, tzinfo=MSK), "0G", "0011890101"),
)
# serial alphabets observed on the genuine MTS donors (12 receipts):
#   pos 12-14: three digits; pos 15: a digit;
#   pos 16: C E 2 O D T 0 4 9 seen; pos 19-22: 1001..1004 and 1012 seen.
_SBP_POS16 = "CE2ODT49"
_SBP_SEGMENTS = ("1001", "1002", "1003", "1004", "1012")

_BANK_FORMS = {
    "сбербанк": "Сбербанк",
    "сбер": "Сбербанк",
    "тбанк": "Т-Банк",
    "т банк": "Т-Банк",
    "тинькофф": "Т-Банк",
    "втб": "ВТБ",
    "банк втб": "ВТБ",
    "альфа банк": "Альфа-Банк",
    "альфабанк": "Альфа-Банк",
    "псб": "ПСБ",
    "промсвязьбанк": "ПСБ",
    "озон банк": "Озон Банк (Ozon)",
    "озон": "Озон Банк (Ozon)",
    "ozon": "Озон Банк (Ozon)",
    "озон банк (ozon)": "Озон Банк (Ozon)",
    "вб банк": "Wildberries (Вайлдберриз Банк)",
    "wildberries": "Wildberries (Вайлдберриз Банк)",
    "вайлдберриз": "Wildberries (Вайлдберриз Банк)",
    "рокет": "Рокетбанк",
    "рокетбанк": "Рокетбанк",
    "мтс": "МТС Банк",
    "мтс банк": "МТС Банк",
}


class GlyphCoverageError(FontCoverageError):
    """No donor-pool glyph for one of the supplied strings."""


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def _render_amount(value) -> str:
    """Any amount spelling -> MTS form ``1 500,00 ₽``."""
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        text = re.sub(r"(?i)\s*(₽|руб\.?|rub)\s*$", "", text)
        text = re.sub(r"[\s  ]", "", text).replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"cannot parse amount {value!r}") from None
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount > Decimal("999999.99"):
        raise ValueError(
            "amounts above 999 999,99 are untested against genuine donors"
        )
    grouped = f"{int(amount):,}".replace(",", " ")
    cents = int((amount * 100).to_integral_value() % 100)
    return f"{grouped},{cents:02d} ₽"


def _format_phone(value: str) -> str:
    """Any Russian phone spelling -> MTS form ``+7 (993) 134-95-57``."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError(f"cannot parse phone {value!r}")
    return (f"+7 ({digits[1:4]}) {digits[4:7]}-"
            f"{digits[7:9]}-{digits[9:11]}")


def _format_person(value: str) -> str:
    """ФИО -> the printed MTS form ``Имя Отчество Ф`` (no dot).

    Accepts ``Фамилия Имя Отчество`` (or two words) and names already
    in the printed form (``ИВАН ИВАНОВИЧ И``).
    """
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]

    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.strip(".").split("-"))

    if not tokens:
        raise ValueError(f"cannot parse name {value!r}")
    if len(tokens[-1].strip(".")) == 1 and len(tokens) >= 2:
        parts = [title(t) for t in tokens[:-1]]
        return " ".join(parts) + f" {tokens[-1].strip('.')[0].upper()}"
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{title(given)} {title(patronymic)} {surname.strip('.')[0].upper()}"
    if len(tokens) == 2:
        surname, given = tokens
        return f"{title(given)} {surname.strip('.')[0].upper()}"
    raise ValueError(f"cannot parse name {value!r}")


def _format_bank(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ValueError("recipient bank is empty")
    key = cleaned.lower().replace("ё", "е").replace("-", " ")
    key = " ".join(re.sub(r"[\"«»()]", " ", key).split())
    return _BANK_FORMS.get(key, cleaned)


def _format_account(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 20:
        raise ValueError(f"sender account must have 20 digits, got {value!r}")
    return " ".join(digits[i:i + 5] for i in range(0, 20, 5))


def _parse_operation_dt(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = " ".join(str(value or "").strip().split())
        text = re.sub(r"\s*\(UTC\+3\)$", "", text)
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"cannot parse operation datetime {value!r}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


# --------------------------------------------------------------------------
# linked identifiers
# --------------------------------------------------------------------------

def decode_sbp_moment(sbp_id: str) -> Optional[datetime]:
    """Operation moment (MSK) encoded in an SBP id (for verification)."""
    if len(sbp_id) != 32 or not sbp_id[1:11].isdigit():
        return None
    year_digit = int(sbp_id[1])
    now_year = datetime.now(timezone.utc).year
    year = now_year - (now_year % 10) + year_digit
    if year > now_year:
        year -= 10
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=int(sbp_id[2:5]) - 1)
    return base.replace(hour=int(sbp_id[5:7]), minute=int(sbp_id[7:9]),
                        second=int(sbp_id[9:11])).astimezone(MSK)


def make_sbp_id(op_msk: datetime, donor_sbp: str = "") -> str:
    """New 32-char SBP operation id for the operation moment.

    Positions 1-11 are the UTC calendar/clock header of the SBP
    moment (a few seconds BEFORE the printed header time, see
    ``generate_mts``); positions 12-22 are the serial, re-rolled in
    the donor id's own character set (BANK.md: the checker remembers
    the serial's character composition); positions 17-18 and 23-32 are
    the system-wide era marker/tail by the operation's UTC date; the
    19-22 segment is drawn from the values seen on genuine MTS donors.
    """
    utc = op_msk.astimezone(timezone.utc)
    stamp = (str(utc.year % 10) + f"{utc.timetuple().tm_yday:03d}"
             + utc.strftime("%H%M%S"))
    era = None
    for start, marker, tail in _SBP_ERAS:
        if op_msk >= start:
            era = (marker, tail)
    first = donor_sbp[0] if len(donor_sbp) == 32 else "A"
    donor_chars = set(donor_sbp)
    if era is not None:
        marker, tail = era
    elif len(donor_sbp) == 32:
        marker, tail = donor_sbp[16:18], donor_sbp[22:]
    else:
        marker, tail = _SBP_ERAS[-1][1], _SBP_ERAS[-1][2]
    segment = (donor_sbp[18:22] if not era and len(donor_sbp) == 32
               else secrets.choice(_SBP_SEGMENTS))
    # cover the donor id's digit/letter set with the random serial part
    fixed = set(first + stamp + marker + segment + tail)
    missing_digits = [c for c in sorted(donor_chars) if c.isdigit() and c not in fixed]
    serial = [secrets.choice("0123456789") for _ in range(3)]
    for slot, char in enumerate(missing_digits[:3]):
        serial[slot] = char
    pos15 = secrets.choice("0123456789")
    pos16_pool = [c for c in _SBP_POS16 if not donor_chars or c in donor_chars]
    if not pos16_pool:
        pos16_pool = list(_SBP_POS16)
    pos16 = secrets.choice(pos16_pool)
    while True:
        candidate = (first + stamp + "".join(serial) + pos15 + pos16
                     + marker + segment + tail)
        if candidate != donor_sbp:
            return candidate


def make_txcode(op_msk: datetime, points: List[Tuple[datetime, int]],
                donor_tx: str = "") -> str:
    """New 12-digit transaction code.

    A bank-wide counter growing ~172/s (measured between the genuine
    donors of 05.11 and 07.11, BANK.md); anchored on the latest donor
    point with the corpus slope, never a plain donor+shift.
    """
    if points:
        pts = sorted(set(points))
        anchor_moment, anchor_num = pts[-1]
        if len(pts) >= 2:
            # local slope over the two freshest corpus points (the global
            # average is polluted by the overnight gap, BANK.md)
            (m0, n0), (m1, n1) = pts[-2], pts[-1]
            span = max((m1 - m0).total_seconds(), 1.0)
            rate = (n1 - n0) / span
            if not 50 < rate < 1000:
                rate = 150.0
        else:
            rate = 150.0
        dt = max((op_msk - anchor_moment).total_seconds(), 0.0)
        value = int(anchor_num + rate * dt) + secrets.randbelow(200_000)
        if value != int(donor_tx or -1):
            return f"{value:012d}"
    match = re.fullmatch(r"(\d{12})", donor_tx or "")
    if match:
        return f"{int(match.group(1)) + secrets.randbelow(1_000_000) + 1:012d}"
    return f"{195_000_000_000 + secrets.randbelow(900_000_000):012d}"[-12:]


# --------------------------------------------------------------------------
# donor pool
# --------------------------------------------------------------------------

FONTS_DIR = _HERE / "fonts"


def build_pool(donor_paths: List[Path], *, synthetic: bool = True) -> Dict[str, FontPool]:
    """Glyph pool: genuine donor outlines first, then (with
    ``synthetic``) the uncovered gaps converted from the 2019 CFF
    master in ``fonts/`` (BANK.md §5).  Donor glyphs always win."""
    pool = {"regular": FontPool(), "medium": FontPool()}
    for path in donor_paths:
        donor = Receipt(path)
        for family in pool:
            pool[family].absorb(donor.subsets[family])
    if synthetic:
        for family, filename in (("regular", "MTSSans-Regular.otf"),
                                 ("medium", "MTSSans-Medium.otf")):
            otf = FONTS_DIR / filename
            if otf.is_file():
                pool[family].absorb_synthetic(OtfGlyphSource(otf))
    return pool


def _donor_paths(donor_path: Optional[Union[str, Path]] = None) -> List[Path]:
    if donor_path is not None:
        donor = Path(donor_path).expanduser()
        if donor.is_dir():
            return sorted(donor.glob("*.pdf"))
        return [donor]
    env = os.environ.get("MTS_DONOR_PATH")
    if env:
        return [Path(env).expanduser()]
    return sorted(DONORS_DIR.glob("*.pdf"))


def _corpus_tx_points(receipts: List[Receipt]) -> List[Tuple[datetime, int]]:
    points = []
    for receipt in receipts:
        moment = decode_sbp_moment(receipt.fields.get("sbp", ""))
        tx = receipt.fields.get("txcode", "")
        if moment is not None and re.fullmatch(r"\d{12}", tx or ""):
            points.append((moment, int(tx)))
    return points


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------

def generate_mts(
    amount,
    operation_dt,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    output_path: Optional[Union[str, Path]] = None,
    *,
    sender_account: Optional[str] = None,
    message: Optional[str] = None,
    donor_path: Optional[Union[str, Path]] = None,
    pool: Optional[Dict[str, FontPool]] = None,
    generated_at: Optional[datetime] = None,
    sbp_id: Optional[str] = None,
    txcode: Optional[str] = None,
) -> Path:
    """Generate an MTS SBP receipt PDF and return its path.

    See the module docstring for the contract.  ``donor_path`` pins the
    template (otherwise the freshest genuine donor that can type every
    supplied string is used); ``pool`` may pass a pre-built glyph pool
    (tests / the bot build it once).
    """
    op = _parse_operation_dt(operation_dt)
    printed_amount = _render_amount(amount)
    printed = {
        "recipient": _format_person(recipient_name),
        "phone": _format_phone(recipient_phone),
        "bank": _format_bank(recipient_bank),
        "message": (message if message is not None else "Перевод СБП"),
        "fee": "0,00 ₽",
    }

    paths = _donor_paths(donor_path)
    if not paths:
        raise ReceiptError(
            "no genuine MTS donor found (looked in banks/mts/donors/; "
            "pass donor_path=... to use another one)"
        )
    pool = pool or build_pool(paths)

    # header time -> SBP moment a few seconds earlier (genuine delta 6-15 s)
    donor_seconds = None
    receipts: List[Receipt] = []
    for path in paths:
        try:
            receipts.append(Receipt(path, pool))
        except Exception:  # noqa: BLE001 - a stray file is not a donor
            continue
    if not receipts:
        raise ReceiptError("no readable genuine MTS donor in the pool")

    if op.second == 0 and op.microsecond == 0:
        donor_seconds = max(r.donor_sbp_seconds() for r in receipts)
        sbp_moment = op.replace(second=donor_seconds)
        header = sbp_moment + timedelta(seconds=(3 + secrets.randbelow(27)))
    else:
        header = op
        sbp_moment = op - timedelta(seconds=(3 + secrets.randbelow(27)))

    # freshest donor that can type everything (glyph coverage)
    def coverage_ok(receipt: Receipt) -> List[str]:
        missing = []
        texts = [printed["recipient"], printed["phone"], printed["bank"],
                 printed["message"], printed_amount, printed["fee"],
                 header.strftime("%d.%m.%Y %H:%M:%S") + " (UTC+3)"]
        for char in set("".join(texts)):
            if not pool["regular"].has_char(char):
                missing.append(char)
        for char in set(printed_amount + "Итого"):
            if not pool["medium"].has_char(char):
                missing.append(char)
        return sorted(set(missing))

    candidates = []
    missing_union: Dict[str, set] = {}
    for receipt in receipts:
        missing = coverage_ok(receipt)
        moment = decode_sbp_moment(receipt.fields.get("sbp", ""))
        if missing:
            for char in missing:
                missing_union.setdefault(char, set()).add(receipt.path.name)
            continue
        candidates.append((moment or datetime.min.replace(tzinfo=MSK), receipt))
    if not candidates:
        absent = "".join(sorted(missing_union))
        raise GlyphCoverageError(
            f"Буквы вне пула глифов настоящих чеков МТС: {absent}. "
            "Нужен новый настоящий чек МТС с этими буквами — свежий "
            "перевод и скачанная квитанция; положите её PDF в "
            "banks/mts/donors/."
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    receipt = candidates[0][1]
    donor = dict(receipt.fields)

    account = (_format_account(sender_account) if sender_account is not None
               else donor.get("account", ""))
    if not re.fullmatch(r"\d{5}( \d{5}){3}", account or ""):
        raise ReceiptError(f"donor {receipt.path.name} has no usable account")

    new_sbp = sbp_id or make_sbp_id(sbp_moment, donor.get("sbp", ""))
    new_tx = txcode or make_txcode(sbp_moment, _corpus_tx_points(receipts),
                                   donor.get("txcode", ""))

    receipt.set("datetime", header.strftime("%d.%m.%Y %H:%M:%S") + " (UTC+3)")
    receipt.set("recipient", printed["recipient"])
    receipt.set("phone", printed["phone"])
    receipt.set("bank", printed["bank"])
    receipt.set("message", printed["message"])
    receipt.set("account", account)
    receipt.set("amount", printed_amount)
    receipt.set("total", printed_amount)
    receipt.set("fee", printed["fee"])
    receipt.set("txcode", new_tx)
    receipt.set("sbp", new_sbp)

    if output_path is None:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        output = DEFAULT_OUT_DIR / "Unknown.pdf"
        n = 2
        while output.exists():
            output = DEFAULT_OUT_DIR / f"Unknown {n}.pdf"
            n += 1
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == receipt.path.resolve():
        raise ReceiptError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    gen = generated_at or datetime.now(timezone.utc)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    receipt.save(output, creation_date=gen.strftime("D:%Y%m%d%H%M%SZ"))

    raw = output.read_bytes() if output.is_file() else b""
    if (not raw.startswith(b"%PDF-")
            or raw.count(b"startxref") != 1
            or raw.count(b"%%EOF") != 1
            or b"/Prev" in raw):
        output.unlink(missing_ok=True)
        raise ReceiptError("natural save failed: generated file is not a "
                           "single-pass PDF")
    return output


if __name__ == "__main__":  # tiny smoke CLI
    out = generate_mts(
        amount=100,
        operation_dt=datetime.now(MSK).replace(microsecond=0),
        recipient_name="Иванов Иван Иванович",
        recipient_phone="+7 900 111-22-33",
        recipient_bank="Т-Банк",
    )
    print(out)
