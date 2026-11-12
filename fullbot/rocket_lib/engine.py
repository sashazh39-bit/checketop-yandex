#!/usr/bin/env python3
"""Bot-facing engine for Rocket Bank SBP receipts.

This is the Rocket analogue of ``banks/yandex/engine.py``: one function,
``generate_rocket(...)``, takes the already-collected transfer data and
writes a finished PDF.  All PDF surgery lives in ``rocket_receipt.py`` /
``rocket_pdf.py`` / ``rocket_font.py`` — a genuine donor receipt is
patched field by field and saved as a single-pass file (single xref,
no incremental-update tail, no size padding), never redrawn from
scratch
(see ``BANK.md`` in this folder for the evidence and the
field/identifier rules measured on 20 genuine donors).

Contract (mirrors ``generate_yandex``)::

    generate_rocket(
        amount="10 000,00 ₽",                    # or 10000 / "10 000 ₽"
        operation_dt=datetime(..., tzinfo=MSK),  # MSK; naive = MSK
        sender_name="Жеребятьев Александр Евгеньевич",
        sender_phone="+7 900 351-70-80",
        recipient_name="Ковалёв Иван Петрович",
        recipient_phone="+7 963 297-20-08",
        recipient_bank="Сбербанк",
        output_path="/path/to/out.pdf",
    ) -> pathlib.Path

Names are accepted in the usual ``Фамилия Имя Отчество`` order (or as a
two-word ``Фамилия Имя``) and printed the Rocket way — ``ИМЯ ОТЧЕСТВО
Ф`` for the recipient and the same plus a trailing dot for the sender.
Names already in Rocket form (``ИВАН ПЕТРОВИЧ К``) pass through.
Banks are printed in the legal form seen on genuine receipts
(``Сбербанк`` -> ``ПАО СБЕРБАНК``, ``Т-Банк`` -> ``АО "ТБАНК"``, …);
unknown banks are upper-cased as given.

Every call is a *new* operation, so the identifiers genuine receipts
tie to the operation moment are re-issued, never copied from the donor:

* ``sbp_id``     -- UTC day/time of the operation + opaque segments in
                    the exact alphabets/tails of the donor corpus;
* ``doc_number`` -- ``M7…`` bank counter: donor number + a random
                    positive shift (format and era preserved);
* PDF ``/ID`` and ``CreationDate``/``ModDate`` -- fresh unique ID,
  CreationDate = the export (generation) moment, as on genuine
  receipts (``rocket_pdf.reidentify``).

If ``operation_dt`` carries no seconds (``second == 0``), the seconds
encoded in the SBP id are inherited from the donor's SBP id — the
printed field only has minutes, exactly as the Yandex patcher does.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Union

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import patch_rocket  # noqa: E402  (glyph harvesting from genuine donors)
import rocket_pdf  # noqa: E402
import rocket_receipt  # noqa: E402
from rocket_receipt import MSK, Receipt, ReceiptError  # noqa: E402

__all__ = ["generate_rocket", "generate_rocket_card", "make_sbp_id", "make_doc_number"]

# ---------------------------------------------------------------- constants

# Donor measured on the 20 genuine receipts in donors/ (BANK.md).
# Opaque SBP segments are drawn from the corpus' empirical
# distributions (weighted by observed frequency), not uniform ranges:
# e.g. segment 19-22 only ever shows the values below in Rocket
# receipts, and position 16 is strongly skewed towards '7'.
_SBP_POS1_WEIGHTED = "A" * 15 + "B" * 5
_SBP_POS16_WEIGHTED = "7" * 5 + "4" * 3 + "1" * 3 + "3" * 3 + "K" * 2 + "NB9CT"
_SBP_ERA_SEGMENTS = ("1004",)
# Serial era rolls for the whole SBP system, not per bank.  Each row is
# (from MSK, marker positions 17-18, segment 19-22, tail 23-32), taken
# from a genuine Rocket receipt of that day.  06.11.2026 donor 47 moved
# off the 03.11 profile 0B/1004/0011881102 onto 0G/1008/0011890101;
# keeping the old profile makes the checker reject the operation id.
_SBP_ERAS = (
    (datetime(2026, 11, 3, tzinfo=MSK), "0B", ("1004",), "0011881102"),
    (datetime(2026, 11, 6, tzinfo=MSK), "0G", ("1008",), "0011890101"),
)
_SBP_SEGMENTS = (
    "1004", "1004", "1004", "1004",
    "1003", "1003", "1003",
    "1002", "1002", "1002",
    "1018", "1018", "1006", "1006",
    "1017", "1012", "1020", "1007", "1001", "1014",
)
_SBP_TAILS = ("0011860303", "0011870501", "0011880703")
_DEFAULT_TEMPLATE = "receipt_rocket_47.pdf"  # freshest genuine donor (06.11.2026)

# Card-transfer layout (donors_card/, genuine donors 43-45 of 03.11.2026):
# freshest template *with* a recipient-bank block and the (only) template
# without one.  The two layouts differ by a whole label+value block and
# page height, so a bank line can never be inserted/removed by patching.
_CARD_TEMPLATE_WITH_BANK = "receipt_rocket_44.pdf"
_CARD_TEMPLATE_NO_BANK = "receipt_rocket_45.pdf"

# Recipient bank on a card receipt is a *short* name, without the legal
# form the SBP layout prints: РОСБАНК and АЛЬФА-БАНК are measured on
# genuine donors 43/44; the Сбер/ВТБ/Т-Банк short forms follow the same
# pattern (not present in the three card donors).  ВБ Банк is measured
# *absent*: donor 45 is a WB card (owner's word) and the receipt carries
# no bank block at all, so WB aliases map to "no bank block".
_CARD_BANK_FORMS = {
    "сбербанк": "СБЕРБАНК",
    "сбер": "СБЕРБАНК",
    "втб": "ВТБ",
    "банк втб": "ВТБ",
    "тбанк": "Т-БАНК",
    "т банк": "Т-БАНК",
    "тинькофф": "Т-БАНК",
    "альфа банк": "АЛЬФА-БАНК",
    "альфабанк": "АЛЬФА-БАНК",
    "росбанк": "РОСБАНК",
}
_CARD_BANK_OMITTED = {
    "вб банк", "вб", "wb bank", "wb", "wildberries", "вайлдберриз",
    "ооо вб банк",
}

_BANK_FORMS = {
    "сбербанк": 'ПАО СБЕРБАНК',
    "пао сбербанк": 'ПАО СБЕРБАНК',
    "сбер": 'ПАО СБЕРБАНК',
    "тбанк": 'АО "ТБАНК"',
    "т банк": 'АО "ТБАНК"',
    "тинькофф": 'АО "ТБАНК"',
    "ао тбанк": 'АО "ТБАНК"',
    "втб": 'БАНК ВТБ (ПАО)',
    "банк втб": 'БАНК ВТБ (ПАО)',
    "банк втб пао": 'БАНК ВТБ (ПАО)',
    "альфа банк": 'АО "АЛЬФА-БАНК"',
    "альфабанк": 'АО "АЛЬФА-БАНК"',
    "ао альфа банк": 'АО "АЛЬФА-БАНК"',
    "озон банк": 'ООО "ОЗОН БАНК"',
    "озон": 'ООО "ОЗОН БАНК"',
    "ооо озон банк": 'ООО "ОЗОН БАНК"',
    "вб банк": 'ООО "ВБ БАНК"',
    "ооо вб банк": 'ООО "ВБ БАНК"',
    "wildberries": 'ООО "ВБ БАНК"',
    "рокет": "РОКЕТ",
    "рокетбанк": "РОКЕТ",
}


# ------------------------------------------------------------- normalisation


def _render_amount(value) -> str:
    """Any amount spelling -> Rocket form ``1 001 ₽`` (whole rubles)."""
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        text = re.sub(r"(?i)\s*(₽|руб\.?|rub)\s*$", "", text)
        text = re.sub(r"[\s\u00a0\u202f]", "", text).replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"cannot parse amount {value!r}") from None
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount != amount.to_integral_value():
        raise ValueError(
            "Rocket donor receipts only contain whole rubles "
            f"(no kopecks); got {value!r}"
        )
    grouped = f"{int(amount):,}".replace(",", " ")
    return f"{grouped} ₽"


def _format_phone(value: str) -> str:
    """Any Russian phone spelling -> ``+7 900 351-70-80``."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError(f"cannot parse phone {value!r}")
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _format_person(value: str, *, sender: bool) -> str:
    """Full name -> Rocket print form (see module docstring)."""
    tokens = [t.strip(".") for t in str(value or "").replace(".", " .").split()]
    tokens = [t for t in tokens if t and t != "."]
    # A trailing one-letter token means the name is already in Rocket
    # form ``ИМЯ ОТЧЕСТВО Ф`` — keep the token order.
    already_rocket = bool(tokens) and len(tokens[-1]) == 1
    if already_rocket:
        if len(tokens) < 2:
            raise ValueError(f"cannot parse name {value!r}")
        parts = [t.upper() for t in tokens]
    elif len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        parts = [given.upper(), patronymic.upper(), surname[0].upper()]
    elif len(tokens) == 2:
        surname, given = tokens
        parts = [given.upper(), surname[0].upper()]
    else:
        raise ValueError(f"cannot parse name {value!r}")
    printed = " ".join(parts)
    return printed + "." if sender else printed


def _format_bank(value: str) -> str:
    """Recipient bank -> the legal form printed on genuine receipts."""
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ValueError("recipient bank is empty")
    key = cleaned.lower().replace("ё", "е").replace("-", " ")
    key = " ".join(re.sub(r"[\"«»()]", " ", key).split())
    return _BANK_FORMS.get(key, cleaned.upper())


def _format_account(value: str) -> str:
    """Account number -> donor grouping ``40817 81045 02313 64892``."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 20:
        raise ValueError(f"sender account must have 20 digits, got {value!r}")
    return " ".join(digits[i : i + 5] for i in range(0, 20, 5))


def _format_card(value: str) -> str:
    """Any card spelling -> Rocket mask ``2200 **** **** 9515``.

    Accepts a full 16-digit number, digit groups, or an already masked
    ``2200 **** **** 9515`` / ``2200 **** 9515`` string; only the first
    and last four digits are ever printed.
    """
    text = str(value or "")
    groups = re.findall(r"\d+", text)
    digits = "".join(groups)
    if "*" in text:
        if len(groups) < 2 or len(groups[0]) != 4 or len(groups[-1]) != 4:
            raise ValueError(f"cannot parse masked card {value!r}")
        first, last = groups[0], groups[-1]
    elif len(digits) == 16:
        first, last = digits[:4], digits[-4:]
    else:
        raise ValueError(
            f"card must be 16 digits or a mask like '2200 **** **** 9515', "
            f"got {value!r}"
        )
    return f"{first} **** **** {last}"


def _format_card_bank(value) -> Optional[str]:
    """Recipient bank -> short card-layout form, or None for no block.

    ``None`` (and WB aliases, measured absent on genuine donor 45)
    means the receipt carries no ``БАНК ПОЛУЧАТЕЛЯ`` block at all.
    Unknown banks fall back to the name as given, upper-cased and
    stripped of the legal form (``ПАО``/``АО``/``ООО`` + quotes) —
    the card layout never prints the legal form.
    """
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return None
    key = cleaned.lower().replace("ё", "е").replace("-", " ")
    key = " ".join(re.sub(r"[\"«»()]", " ", key).split())
    if key in _CARD_BANK_OMITTED:
        return None
    if key in _CARD_BANK_FORMS:
        return _CARD_BANK_FORMS[key]
    short = re.sub(
        r"(?i)^(ПАО|АО|ООО|НАО|ЗАО)\s+", "", cleaned
    ).strip(" \"«»")
    return short.upper()


def _parse_operation_dt(value) -> datetime:
    """Accept a datetime or a string; always return MSK-aware."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = " ".join(str(value or "").strip().split())
        text = re.sub(r"\s+ПО МСК$", "", text, flags=re.IGNORECASE)
        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
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


# --------------------------------------------------------- linked identifiers


def _donor_sbp_seconds(sbp_id: str) -> int:
    if len(sbp_id) == 32 and sbp_id[5:11].isdigit():
        return int(sbp_id[9:11])
    return 0


def make_sbp_id(op_msk: datetime, donor_sbp: str = "", charset=None) -> str:
    """Build a 32-char SBP operation id for the operation moment.

    What the ID is made of (BANK.md, «Логика СБП-ID»): positions 1-11
    are a header around the UTC calendar/clock of the operation;
    positions 12-32 are an opaque serial assigned by the ОПКЦ.  No
    public standard discloses that serial's algorithm, and across the
    donor corpus it correlates with *nothing* on the receipt (bank,
    amount, phones, names, account, document number) — it is a random
    unique token.  Randomly re-rolling its segments therefore creates
    segment combinations the ОПКЦ never issued, which is what the
    checker kept flagging (33/35).  So the opaque tail (from position
    12, header letter included) is inherited verbatim from the genuine
    donor template; only the timestamp header is rebuilt.  The random
    weighted fallback below is used only without a valid donor ID.
    """
    utc = op_msk.astimezone(timezone.utc)
    stamp = (
        str(utc.year % 10)
        + f"{utc.timetuple().tm_yday:03d}"
        + utc.strftime("%H%M%S")
    )
    # Serial era of the operation day (see _SBP_ERAS).  Before the first
    # recorded era the opaque tail is inherited from the donor template.
    era = None
    for start, marker, segments, tail in _SBP_ERAS:
        if op_msk >= start:
            era = (marker, segments, tail)
    if era is not None:
        marker, segments, tail = era
        first = donor_sbp[0] if len(donor_sbp) == 32 else "A"
        segment = secrets.choice(segments)
        # Prefer serial characters the template's SBP font already
        # carries, so the operation font needs no glyph surgery at all
        # where the era profile allows it.
        digits = [c for c in "0123456789" if not charset or c in charset]
        pos16_pool = [c for c in _SBP_POS16_WEIGHTED if not charset or c in charset]
        if not digits:
            digits = list("0123456789")
        if not pos16_pool:
            pos16_pool = list(_SBP_POS16_WEIGHTED)
        # Cover characters of the donor ID that the fixed segments do
        # not already contain, so the new ID's character set equals the
        # donor's and its font stays byte-identical (no prune/inject).
        fixed_chars = set(first + stamp + "0" + marker + segment + tail)
        missing = [c for c in set(donor_sbp) if c not in fixed_chars]
        serial = [secrets.choice(digits) for _ in range(3)]
        for slot, char in enumerate(c for c in missing if c.isdigit()):
            if slot < 3:
                serial[slot] = char
        missing_letters = [c for c in missing if c in pos16_pool]
        while True:
            candidate = (
                first
                + stamp
                + "".join(serial)
                + "0"
                + (missing_letters[0] if missing_letters else secrets.choice(pos16_pool))
                + marker
                + segment
                + tail
            )
            if candidate != donor_sbp:
                return candidate
    if len(donor_sbp) == 32 and donor_sbp[1:11].isdigit():
        candidate = donor_sbp[0] + stamp + donor_sbp[11:]
        if candidate != donor_sbp:
            return candidate
    tail = donor_sbp[22:] if len(donor_sbp) == 32 else _SBP_TAILS[-1]
    while True:
        sbp = (
            secrets.choice(_SBP_POS1_WEIGHTED)
            + str(utc.year % 10)
            + f"{utc.timetuple().tm_yday:03d}"
            + utc.strftime("%H%M%S")
            + f"{secrets.randbelow(1000):03d}"
            + "0"
            + secrets.choice(_SBP_POS16_WEIGHTED)
            + "0"
            # Positions 17-18 are a route/generation marker, not a
            # per-operation roll: B in the corpus only ever pairs with
            # the oldest tail, G with the current one.  Inherit the
            # donor's marker instead of inventing unseen combinations.
            + (donor_sbp[17] if len(donor_sbp) == 32 else "G")
            + secrets.choice(_SBP_SEGMENTS)
            + tail
        )
        if sbp != donor_sbp:
            return sbp


def make_doc_number(donor_doc: str = "", op_msk=None, points=None,
                    allowed_chars=None) -> str:
    """New ``M7…`` document number.

    The number is the bank's global document counter.  Across the
    October corpus it grew at ~18.6M per day, but by 03.11 the fresh
    donors sit +42M above that extrapolation — the rate itself moves,
    so the prediction is anchored on the *latest* corpus point with a
    slope fitted over the last two weeks of points, not on a global
    fit.  ``points`` are (moment, number) pairs from genuine donors;
    without them, fall back to a donor-relative shift.
    """
    if op_msk is not None and points and len(points) >= 2:
        pts = sorted(set(points))
        anchor_moment, anchor_num = pts[-1]
        recent = [
            (m, n) for m, n in pts
            if (anchor_moment - m).days <= 14
        ]
        fit = recent if len(recent) >= 2 else pts
        xs = [moment.timestamp() for moment, _num in fit]
        ys = [num for _moment, num in fit]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom:
            slope = sum(
                (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
            ) / denom
            if 1_000 < slope * 86400 < 100_000_000:  # sane counter rate/day
                predicted = anchor_num + slope * (
                    op_msk.timestamp() - anchor_moment.timestamp()
                )
                # Prefer a value whose digits the template's document
                # font already carries (no glyph surgery in that font);
                # best of all, one whose digit *set* equals the font's,
                # so the font stays byte-identical to the donor's.
                if allowed_chars:
                    allowed_digits = {c for c in allowed_chars if c.isdigit()}
                    for window in (1_000_000, 6_000_000):
                        for _try in range(4000):
                            value = int(predicted) + secrets.randbelow(2 * window + 1) - window
                            if set(f"{value:011d}") == allowed_digits:
                                return f"M{value:011d}"
                    for _try in range(300):
                        value = int(predicted) + secrets.randbelow(2_000_001) - 1_000_000
                        if set(f"{value:011d}") <= allowed_digits:
                            return f"M{value:011d}"
                value = int(predicted) + secrets.randbelow(2_000_001) - 1_000_000
                if value > 70_000_000_000:
                    return f"M{value:011d}"
    match = re.fullmatch(r"M(\d{11})", donor_doc or "")
    if match:
        value = int(match.group(1)) + secrets.randbelow(2_000_000) + 1
        return f"M{value:011d}"
    return "M7" + f"{secrets.randbelow(10_000_000_000):010d}"


def _corpus_doc_points(paths) -> list:
    """(operation moment, doc counter) pairs from genuine SBP donors."""
    points = []
    for path in paths:
        if Path(path).parent.name != "donors":
            continue  # only the verified genuine corpus, no old samples
        try:
            fields = Receipt(str(path)).fields
            moment = _decode_sbp_moment(fields.get("sbp_id", ""))
            match = re.fullmatch(r"M(\d{11})", fields.get("doc_number", ""))
        except Exception:  # noqa: BLE001 - a stray file must not break us
            continue
        if moment is not None and match:
            points.append((moment, int(match.group(1))))
    return points


def _decode_sbp_moment(sbp_id: str) -> Optional[datetime]:
    """Operation moment (MSK) encoded in an SBP id, for verification."""
    if len(sbp_id) != 32 or not sbp_id[1:11].isdigit():
        return None
    year_digit = int(sbp_id[1])
    now_year = datetime.now(timezone.utc).year
    year = now_year - (now_year % 10) + year_digit
    if year > now_year:
        year -= 10
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=int(sbp_id[2:5]) - 1
    )
    return base.replace(
        hour=int(sbp_id[5:7]), minute=int(sbp_id[7:9]), second=int(sbp_id[9:11])
    ).astimezone(MSK)


# ------------------------------------------------------------------- donors


def _unique_existing(paths: List[Path]) -> List[Path]:
    seen = []
    for path in paths:
        if path and path.is_file() and path not in seen:
            seen.append(path)
    return seen


def _donor_candidates() -> List[Path]:
    """Genuine Rocket donors shipped in this folder, best first."""
    candidates: List[Path] = []
    env = os.environ.get("ROCKET_DONOR_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    donors_dir = _HERE / "donors"
    candidates.append(donors_dir / _DEFAULT_TEMPLATE)
    candidates.extend(sorted(donors_dir.glob("receipt_rocket_*.pdf")))
    candidates.append(_HERE / "rocket_sbp_check.pdf")
    candidates.append(_HERE / "rocket_card_check.pdf")
    return _unique_existing(candidates)


def _default_donor() -> Path:
    candidates = _donor_candidates()
    if not candidates:
        raise FileNotFoundError(
            "no genuine Rocket donor found (looked in banks/rocket/donors/ "
            "and banks/rocket/; pass donor_path=... to use another one)"
        )
    return candidates[0]


def _card_donor_candidates(*, with_bank: Optional[bool] = None) -> List[Path]:
    """Genuine Rocket *card* donors in donors_card/, best first.

    With ``with_bank`` set, the freshest template of that layout comes
    first (with-bank: donor 44; without bank block: donor 45).  Only
    genuine receipts are listed — the old generated samples in this
    folder are never card donors or glyph sources.
    """
    candidates: List[Path] = []
    env = os.environ.get("ROCKET_CARD_DONOR_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    donors_dir = _HERE / "donors_card"
    if with_bank is True:
        candidates.append(donors_dir / _CARD_TEMPLATE_WITH_BANK)
    elif with_bank is False:
        candidates.append(donors_dir / _CARD_TEMPLATE_NO_BANK)
    candidates.extend(sorted(donors_dir.glob("receipt_rocket_*.pdf")))
    return _unique_existing(candidates)


def _card_doc_points(paths) -> list:
    """(operation moment, doc counter) pairs from genuine card donors.

    A card receipt has no SBP id, so the moment comes from its printed
    date field (minute precision).  The counter is the same bank-wide
    ``M7…`` sequence as SBP (card donors 43-45 continue the SBP series
    of the same day), so both corpora anchor one model.
    """
    points = []
    for path in paths:
        if Path(path).parent.name != "donors_card":
            continue
        try:
            fields = Receipt(str(path)).fields
            match = re.fullmatch(r"M(\d{11})", fields.get("doc_number", ""))
            moment = _parse_operation_dt(fields.get("date", ""))
        except Exception:  # noqa: BLE001 - a stray file must not break us
            continue
        if match:
            points.append((moment, int(match.group(1))))
    return points


# ------------------------------------------------------------------ generate


def generate_rocket(
    amount,
    operation_dt,
    sender_name: str,
    sender_phone: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    output_path: Optional[Union[str, Path]] = None,
    *,
    donor_path: Optional[Union[str, Path]] = None,
    glyph_donor_paths: Optional[List[Union[str, Path]]] = None,
    sender_account: Optional[str] = None,
    fee: Optional[str] = None,
    sbp_id: Optional[str] = None,
    doc_number: Optional[str] = None,
    creation_delta_minutes: Optional[float] = None,
) -> Path:
    """Generate a Rocket SBP receipt PDF and return its path.

    See the module docstring for the full contract.  ``donor_path``
    overrides the genuine-donor template; ``glyph_donor_paths`` adds
    extra genuine Rocket receipts to harvest missing glyphs from.
    """
    template = Path(donor_path).expanduser() if donor_path else _default_donor()
    if not template.is_file():
        raise FileNotFoundError(f"Rocket donor not found: {template}")

    if output_path is None:
        moment = _parse_operation_dt(operation_dt)
        output = _HERE / "out" / f"rocket_receipt_{moment:%Y%m%d_%H%M}.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise ValueError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    pool_paths = [Path(p).expanduser() for p in (glyph_donor_paths or [])]
    pool_paths += _donor_candidates()
    pool_paths = _unique_existing(pool_paths)
    cache = patch_rocket.harvest([str(p) for p in pool_paths], verbose=False)

    receipt = Receipt(str(template), cache)
    if "sbp_id" not in receipt.fields:
        raise ReceiptError(
            f"donor {template.name} is not an SBP receipt "
            f"(fields: {', '.join(sorted(receipt.fields))}); "
            "generate_rocket only supports the SBP layout"
        )
    donor = dict(receipt.fields)
    # Natural single-pass save: surgical in-place when it fits, else a
    # classic-xref refit at whatever size the new text naturally makes
    # (genuine receipts vary 88.8-89.4 KB between operations).  Forcing
    # the donor's exact byte count needed a multi-KB trailer padding
    # comment, and an incremental tail is visibly an after-the-fact
    # edit — the checker rejects both as "modified after export".
    receipt.minimal_glyphs = True

    op = _parse_operation_dt(operation_dt)
    if op.second == 0 and op.microsecond == 0:
        op = op.replace(second=_donor_sbp_seconds(donor.get("sbp_id", "")))
    receipt.operation_dt = op
    # CreationDate is the export moment, not a fixed lag: fresh genuine
    # receipts made seconds after a transfer carry CreationDate +7/+9 s
    # after the operation (donors 41/42), and re-downloads carry the
    # download date.  Default therefore stamps the generation moment
    # (never before operation + 8 s); an explicit delta overrides it.
    if creation_delta_minutes is None:
        created = max(datetime.now(MSK), op + timedelta(seconds=8))
        receipt.creation_delta = created - op
    else:
        receipt.creation_delta = timedelta(minutes=creation_delta_minutes)

    receipt.set("date", op.strftime("%d.%m.%Y %H:%M") + " ПО МСК")
    receipt.set("amount", _render_amount(amount))
    if fee is not None:
        receipt.set("fee", fee)
    receipt.set("recipient", _format_person(recipient_name, sender=False))
    receipt.set("phone_to", _format_phone(recipient_phone))
    receipt.set("bank_to", _format_bank(recipient_bank))
    receipt.set("sender", _format_person(sender_name, sender=True))
    receipt.set("phone_from", _format_phone(sender_phone))
    if sender_account is not None:
        receipt.set("account", _format_account(sender_account))
    receipt.set(
        "sbp_id",
        sbp_id
        or make_sbp_id(
            op, donor.get("sbp_id", ""), set(receipt.field_charset("sbp_id"))
        ),
    )
    receipt.set(
        "doc_number",
        doc_number
        or make_doc_number(
            donor.get("doc_number", ""),
            op,
            _corpus_doc_points(pool_paths),
            set(receipt.field_charset("doc_number")),
        ),
    )

    try:
        receipt.save(str(output), mode="surgical")
    except ReceiptError:
        # A changed stream could not be fitted at its original size:
        # fall back to the natural single-pass refit with the same
        # already-assembled receipt.
        receipt.save(str(output), mode="natural")
    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/XRef" in raw
        or abs(len(raw) - template.stat().st_size) > 4096
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            f"natural save failed for {template.name}: got {len(raw)} "
            f"bytes / {raw.count(b'startxref')} xref, donor is "
            f"{template.stat().st_size} bytes / 1 xref; refusing to emit "
            "a structurally implausible file"
        )
    return output


def generate_rocket_card(
    amount,
    operation_dt,
    sender_name: str,
    recipient_card: str,
    recipient_bank: Optional[str] = None,
    output_path: Optional[Union[str, Path]] = None,
    *,
    donor_path: Optional[Union[str, Path]] = None,
    glyph_donor_paths: Optional[List[Union[str, Path]]] = None,
    sender_account: Optional[str] = None,
    fee: Optional[str] = None,
    doc_number: Optional[str] = None,
    creation_delta_minutes: Optional[float] = None,
) -> Path:
    """Generate a Rocket card-transfer receipt PDF and return its path.

    The card layout (genuine donors 43-45 in ``donors_card/``, see
    ``BANK.md``) has no recipient name/phones and no SBP id: the
    recipient is a card mask (``2200 **** **** 9515``) plus an optional
    short bank name, and the only operation-linked number is the
    ``M7…`` document counter shared with the SBP corpus.

    ``recipient_bank=None`` — or a WB alias, measured absent on donor
    45 — selects the no-bank template (donor 45); any other bank
    selects the with-bank template (donor 44) and is printed in the
    short card form (``Сбербанк`` -> ``СБЕРБАНК``, ``Альфа-Банк`` ->
    ``АЛЬФА-БАНК``).  An explicit ``donor_path`` whose layout cannot
    carry the requested bank block is rejected, never patched around.
    CreationDate is the export (generation) moment, as on the SBP
    engine; the document number is predicted by ``make_doc_number``
    over the joint SBP+card corpus points.
    """
    printed_bank = _format_card_bank(recipient_bank)
    card_mask = _format_card(recipient_card)

    if donor_path:
        template = Path(donor_path).expanduser()
    else:
        candidates = _card_donor_candidates(with_bank=printed_bank is not None)
        if not candidates:
            raise FileNotFoundError(
                "no genuine Rocket card donor found (looked in "
                "banks/rocket/donors_card/; pass donor_path=... to use "
                "another one)"
            )
        template = candidates[0]
    if not template.is_file():
        raise FileNotFoundError(f"Rocket card donor not found: {template}")

    op = _parse_operation_dt(operation_dt)
    if output_path is None:
        output = _HERE / "out" / f"rocket_card_{op:%Y%m%d_%H%M}.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise ValueError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Glyph pool: genuine Rocket receipts only — the card donors plus
    # the SBP corpus (same font families; charstrings for shared CIDs
    # are byte-identical across the two layouts).  Old generated
    # samples in this folder are deliberately excluded.
    pool_paths = [Path(p).expanduser() for p in (glyph_donor_paths or [])]
    pool_paths += _card_donor_candidates()
    pool_paths += sorted((_HERE / "donors").glob("receipt_rocket_*.pdf"))
    pool_paths = _unique_existing(pool_paths)
    cache = patch_rocket.harvest([str(p) for p in pool_paths], verbose=False)

    receipt = Receipt(str(template), cache)
    if receipt.format != rocket_receipt.FORMAT_CARD:
        raise ReceiptError(
            f"donor {template.name} is not a card-transfer receipt "
            f"(fields: {', '.join(sorted(receipt.fields))}); "
            "generate_rocket_card only supports the card layout"
        )
    if printed_bank is not None and "bank_to" not in receipt.fields:
        raise ReceiptError(
            f"donor {template.name} has no recipient-bank block and "
            f"cannot carry bank {printed_bank!r}; use a with-bank "
            "card donor (e.g. donors_card/receipt_rocket_44.pdf)"
        )
    if printed_bank is None and "bank_to" in receipt.fields:
        raise ReceiptError(
            f"donor {template.name} carries a recipient-bank block but "
            "no bank is to be printed; use the no-bank card donor "
            "(donors_card/receipt_rocket_45.pdf)"
        )
    donor = dict(receipt.fields)
    receipt.minimal_glyphs = True
    receipt.operation_dt = op

    if creation_delta_minutes is None:
        created = max(datetime.now(MSK), op + timedelta(seconds=8))
        receipt.creation_delta = created - op
    else:
        receipt.creation_delta = timedelta(minutes=creation_delta_minutes)

    receipt.set("date", op.strftime("%d.%m.%Y %H:%M") + " ПО МСК")
    receipt.set("amount", _render_amount(amount))
    if fee is not None:
        receipt.set("fee", fee)
    receipt.set("card", card_mask)
    if printed_bank is not None:
        receipt.set("bank_to", printed_bank)
    receipt.set("sender", _format_person(sender_name, sender=True))
    if sender_account is not None:
        receipt.set("account", _format_account(sender_account))
    points = _corpus_doc_points(pool_paths) + _card_doc_points(pool_paths)
    receipt.set(
        "doc_number",
        doc_number
        or make_doc_number(
            donor.get("doc_number", ""),
            op,
            points,
            set(receipt.field_charset("doc_number")),
        ),
    )

    try:
        receipt.save(str(output), mode="surgical")
    except ReceiptError:
        receipt.save(str(output), mode="natural")
    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/XRef" in raw
        or abs(len(raw) - template.stat().st_size) > 4096
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            f"natural save failed for {template.name}: got {len(raw)} "
            f"bytes / {raw.count(b'startxref')} xref, donor is "
            f"{template.stat().st_size} bytes / 1 xref; refusing to emit "
            "a structurally implausible file"
        )
    return output


if __name__ == "__main__":  # tiny smoke CLI; the bot imports the function
    raise SystemExit(
        "generate_rocket() is a library function; "
        "use patch_rocket.py for a command line"
    )
