#!/usr/bin/env python3
"""Bot-facing engine for Wildberries Bank SBP receipts.

One function, ``generate_wb(...)``, takes the already-collected transfer
data and writes a finished PDF through the WB patcher
(``wb_receipt.py`` + ``wb_font.py``).  The donor is a genuine Jasper 7.0.3
/ OpenPDF 1.3.32 export; only field values (and the font subset needed
to type them) are rewritten.

Contract::

    generate_wb(
        amount="12 ₽",                             # whole rubles
        operation_dt=datetime(..., tzinfo=MSK),    # MSK; naive = MSK
        recipient_name="Жеребятьев Александр Евгеньевич",
        recipient_phone="+7 963 297-20-08",
        recipient_bank="Альфа-Банк",
        output_path="/path/to/out.pdf",
        sender_name=None,                          # ignored (no field)
        sender_phone=None,                         # ignored (no field)
    ) -> pathlib.Path

Identifiers:

* SBP id — UTC time header of (printed MSK − 1 s) + inherited serial
  ``donor[11:22]`` + era tail (``0011890101`` from 06.11.2026).  The
  first character (A/B) is kept from the donor.  Serial tokens are
  one-time (``donors/spent_tokens.json``).
* «Квитанция №» — 10-digit counter anchored on the donor corpus
  CreationDates (~1.6/s), digits restricted to the Bold pool
  ``0125789``.
* ``CreationDate`` is the generation moment (UTC, ``Z``); trailer
  ``/ID`` is fresh and paired.

There is no sender block on these receipts.  Amount is whole rubles
(``12 ₽``) because Medium Google Sans in the four donors only has
digits 0–3.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_ROCKET = _REPO_ROOT / "banks" / "rocket"
for _path in (_ROCKET, _HERE, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import wb_font  # noqa: E402
from wb_receipt import (  # noqa: E402
    BOLD_FAMILY,
    MEDIUM_FAMILY,
    MSK,
    REGULAR_FAMILY,
    Receipt,
    ReceiptError,
)

__all__ = [
    "generate_wb",
    "serial_token",
    "load_spent_tokens",
    "donor_pool_paths",
    "make_sbp_id",
    "make_receipt_no",
    "decode_sbp_moment",
    "NoUnusedDonorTokenError",
    "GlyphCoverageError",
    "ReceiptError",
]

DEFAULT_POOL_DIR = _HERE / "donors"
DEFAULT_REGISTRY_PATH = DEFAULT_POOL_DIR / "spent_tokens.json"
DEFAULT_OUT_DIR = _HERE / "out"

_SBP_TAIL_ERAS: Tuple[Tuple[datetime, str], ...] = (
    (datetime(2026, 11, 3, tzinfo=MSK), "0011881102"),
    (datetime(2026, 11, 6, tzinfo=MSK), "0011890101"),
)
_RECEIPT_POINTS: Tuple[Tuple[datetime, int], ...] = (
    (datetime(2026, 11, 7, 8, 27, 34, tzinfo=timezone.utc), 1022107512),
    (datetime(2026, 11, 7, 8, 28, 30, tzinfo=timezone.utc), 1022107588),
    (datetime(2026, 11, 7, 8, 29, 33, tzinfo=timezone.utc), 1022107707),
    (datetime(2026, 11, 7, 8, 30, 8, tzinfo=timezone.utc), 1022107759),
)
_RECEIPT_RATE = 1.6
_BOLD_DIGITS = set("0125789")
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
    "вб банк": "Вайлдберриз Банк",
    "wildberries": "Вайлдберриз Банк",
    "вайлдберриз": "Вайлдберриз Банк",
}


class NoUnusedDonorTokenError(ReceiptError):
    """All genuine SBP serial tokens in the donor pool are spent."""


class GlyphCoverageError(ReceiptError):
    """The genuine-donor glyph pool cannot type one of the supplied strings."""


# --------------------------------------------------------------------------
# spent-token registry
# --------------------------------------------------------------------------


def registry_path(path: Optional[Union[str, Path]] = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("WB_TOKEN_REGISTRY")
    return Path(override).expanduser() if override else DEFAULT_REGISTRY_PATH


def _load_registry(path: Path) -> Dict[str, dict]:
    spent: Dict[str, dict] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("spent", raw) if isinstance(raw, dict) else {}
            if isinstance(entries, dict):
                for token, info in entries.items():
                    spent[str(token)] = info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            spent = {}
    return spent


def _save_registry(path: Path, spent: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "spent": dict(sorted(spent.items()))}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load_spent_tokens(path: Optional[Union[str, Path]] = None) -> set:
    return set(_load_registry(registry_path(path)))


@contextmanager
def _registry_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    handle = None
    deadline = time.monotonic() + 30
    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise ReceiptError(f"token registry is locked: {lock_path}")
            time.sleep(0.05)
    try:
        os.write(handle, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(handle)
        try:
            lock_path.unlink()
        except OSError:
            pass


def serial_token(sbp_id: str) -> str:
    """The one-time serial token of an SBP id: positions 12-22."""
    return str(sbp_id or "")[11:22]


# --------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------


def _sbp_era_tail(op_msk: datetime) -> str:
    tail = ""
    for start, value in _SBP_TAIL_ERAS:
        if op_msk >= start:
            tail = value
    return tail


def decode_sbp_moment(sbp_id: str) -> Optional[datetime]:
    """SBP-encoded moment (MSK) — printed time on WB receipts is this + 1 s."""
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
        hour=int(sbp_id[5:7]),
        minute=int(sbp_id[7:9]),
        second=int(sbp_id[9:11]),
    ).astimezone(MSK)


def make_sbp_id(printed_msk: datetime, donor_sbp: str = "") -> str:
    """32-char SBP id: UTC header of printed−1s, inherited serial, era tail."""
    sbp_moment = printed_msk - timedelta(seconds=1)
    utc = sbp_moment.astimezone(timezone.utc)

    def stamp_of(moment_utc: datetime) -> str:
        return (
            str(moment_utc.year % 10)
            + f"{moment_utc.timetuple().tm_yday:03d}"
            + moment_utc.strftime("%H%M%S")
        )

    stamp = stamp_of(utc)
    if len(donor_sbp) == 32 and donor_sbp[1:11].isdigit():
        first = donor_sbp[0]
        if printed_msk >= datetime(2026, 11, 3, tzinfo=MSK):
            candidate = first + stamp + donor_sbp[11:22] + _sbp_era_tail(printed_msk)
        else:
            candidate = first + stamp + donor_sbp[11:]
        if candidate != donor_sbp:
            return candidate
        utc = (sbp_moment - timedelta(seconds=1)).astimezone(timezone.utc)
        stamp = stamp_of(utc)
        if printed_msk >= datetime(2026, 11, 3, tzinfo=MSK):
            return first + stamp + donor_sbp[11:22] + _sbp_era_tail(printed_msk)
        return first + stamp + donor_sbp[11:]
    first = "B"
    serial = "000000G1001"
    return first + stamp + serial + (_sbp_era_tail(printed_msk) or "0011890101")


def make_receipt_no(
    created_utc: datetime, donor_no: str = "", *, points: Optional[List[Tuple[datetime, int]]] = None
) -> str:
    """10-digit «Квитанция №» from the CreationDate counter.

    Anchored on the four genuine WB donors (~1.6/s).  Digits 3/4/6 are
    not in the Bold subset, so the counter is incremented until every
    digit is in ``0125789``.
    """
    pts = list(points) if points is not None else list(_RECEIPT_POINTS)
    pts.sort()
    if created_utc.tzinfo is None:
        created_utc = created_utc.replace(tzinfo=timezone.utc)
    created_utc = created_utc.astimezone(timezone.utc)
    if not pts:
        value = 1022107512
    else:
        anchor_t, anchor_n = pts[0]
        rate = _RECEIPT_RATE
        for index, (moment, number) in enumerate(pts):
            if moment <= created_utc:
                anchor_t, anchor_n = moment, number
                if index + 1 < len(pts):
                    span = max((pts[index + 1][0] - moment).total_seconds(), 1.0)
                    rate = (pts[index + 1][1] - number) / span
                elif index > 0:
                    span = max((moment - pts[index - 1][0]).total_seconds(), 1.0)
                    rate = (number - pts[index - 1][1]) / span
                else:
                    rate = _RECEIPT_RATE
            else:
                if index == 0:
                    span = max((pts[0][0] - pts[min(1, len(pts) - 1)][0]).total_seconds(), 1.0)
                    if len(pts) >= 2:
                        rate = (pts[1][1] - pts[0][1]) / max(
                            (pts[1][0] - pts[0][0]).total_seconds(), 1.0
                        )
                break
        dt = (created_utc - anchor_t).total_seconds()
        value = int(round(anchor_n + rate * dt))
    donor_int = int(donor_no) if str(donor_no).isdigit() else -1
    while True:
        text = f"{value:010d}"[-10:]
        if set(text) <= _BOLD_DIGITS and int(text) != donor_int:
            return text
        value += 1


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def _parse_operation_dt(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = " ".join(str(value or "").strip().split())
        text = re.sub(r"\s*\(UTC\+3\)$", "", text).replace(" в ", " ")
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
            raise ReceiptError(f"cannot parse operation datetime {value!r}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def _format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ReceiptError(f"cannot parse phone {value!r}")
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _format_person(value: str) -> str:
    """ФИО -> printed WB form ``Имя Отчество Ф.`` (trailing dot)."""
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]

    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.strip(".").split("-"))

    if not tokens:
        raise ReceiptError(f"cannot parse name {value!r}")
    if len(tokens[-1].strip(".")) == 1 and len(tokens) >= 2:
        parts = [title(t) for t in tokens[:-1]]
        return " ".join(parts) + f" {tokens[-1].strip('.')[0].upper()}."
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{title(given)} {title(patronymic)} {surname.strip('.')[0].upper()}."
    if len(tokens) == 2:
        surname, given = tokens
        return f"{title(given)} {surname.strip('.')[0].upper()}."
    raise ReceiptError(f"cannot parse name {value!r}")


def _format_bank(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ReceiptError("recipient bank is empty")
    key = cleaned.lower().replace("ё", "е").replace("-", " ")
    key = " ".join(re.sub(r"[\"«»()]", " ", key).split())
    return _BANK_FORMS.get(key, cleaned)


def _parse_amount_text(value) -> str:
    """Whole-ruble WB amount ``1 230 ₽`` without opening a PDF."""
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int) and not isinstance(value, bool):
        amount = Decimal(value)
    elif isinstance(value, float):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        text = re.sub(r"(?i)\s*(₽|руб\.?|rub)\s*$", "", text)
        text = re.sub(r"[\s\u00a0\u2009\u202f]", "", text).replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ReceiptError(f"cannot parse amount {value!r}") from exc
    if amount <= 0:
        raise ReceiptError("amount must be positive")
    if amount != amount.to_integral_value():
        raise ReceiptError(
            "WB SBP amount is whole rubles only (genuine receipts have no kopecks)"
        )
    grouped = f"{int(amount):,}".replace(",", " ")
    return f"{grouped} ₽"


# --------------------------------------------------------------------------
# donor pool / glyphs
# --------------------------------------------------------------------------


def donor_pool_paths(
    donor_path: Optional[Union[str, Path]] = None,
    pool_dir: Optional[Union[str, Path]] = None,
) -> List[Path]:
    if donor_path is not None:
        donor = Path(donor_path).expanduser()
        if donor.is_dir():
            return sorted(donor.glob("*.pdf"))
        return [donor]
    env = os.environ.get("WB_DONOR_PATH")
    if env:
        pinned = Path(env).expanduser()
        if pinned.is_dir():
            return sorted(pinned.glob("*.pdf"))
        return [pinned]
    directory = Path(pool_dir).expanduser() if pool_dir else DEFAULT_POOL_DIR
    return sorted(directory.glob("*.pdf"))


def build_pool(paths: Iterable[Path]) -> wb_font.GlyphPool:
    pool = wb_font.GlyphPool()
    for path in paths:
        if not Path(path).is_file():
            continue
        try:
            receipt = Receipt(str(path))
        except Exception:  # noqa: BLE001
            continue
        for slot in receipt.fonts.values():
            ttf = receipt.pdf.obj(slot.fontfile_obj).decoded_stream()
            pool.add_font(slot.family, ttf, slot.cid_to_char, slot.widths)
    return pool


def _field_family(name: str) -> str:
    if name == "amount":
        return MEDIUM_FAMILY
    if name == "receipt":
        return BOLD_FAMILY
    return REGULAR_FAMILY


def _missing_in_pool(pool: wb_font.GlyphPool, planned: Dict[str, str]) -> Dict[str, List[str]]:
    missing: Dict[str, List[str]] = {}
    for field, text in planned.items():
        available = pool.available_chars(_field_family(field))
        absent = sorted({char for char in text if char not in available})
        if absent:
            missing[field] = absent
    return missing


def _corpus_receipt_points(paths: Iterable[Path]) -> List[Tuple[datetime, int]]:
    points: List[Tuple[datetime, int]] = []
    for path in paths:
        try:
            receipt = Receipt(str(path))
        except Exception:  # noqa: BLE001
            continue
        info_num = receipt.pdf.resolve_ref(receipt.pdf.trailer_dict, b"/Info")
        if info_num is None:
            continue
        info = receipt.pdf.obj(info_num).dict_bytes
        match = re.search(rb"/CreationDate\(D:(\d{14})Z\)", info)
        receipt_line = receipt.fields.get("receipt") or ""
        number = re.search(r"(\d{10})", receipt_line)
        if match and number:
            created = datetime.strptime(
                match.group(1).decode("ascii"), "%Y%m%d%H%M%S"
            ).replace(tzinfo=timezone.utc)
            points.append((created, int(number.group(1))))
    return points


class _Candidate:
    def __init__(self, path: Path, receipt: Receipt):
        self.path = path
        self.fields = dict(receipt.fields)
        self.sbp_id = self.fields.get("sbp_id", "")
        self.token = serial_token(self.sbp_id)
        self.moment = decode_sbp_moment(self.sbp_id)
        self.printed_seconds = receipt.donor_printed_seconds()
        self.receipt_no = ""
        m = re.search(r"(\d+)", self.fields.get("receipt") or "")
        if m:
            self.receipt_no = m.group(1)


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def generate_wb(
    amount,
    operation_dt,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    output_path: Optional[Union[str, Path]] = None,
    *,
    sender_name: Optional[str] = None,
    sender_phone: Optional[str] = None,
    donor_path: Optional[Union[str, Path]] = None,
    donor_pool_dir: Optional[Union[str, Path]] = None,
    token_registry_path: Optional[Union[str, Path]] = None,
    generated_at: Optional[datetime] = None,
    sbp_id: Optional[str] = None,
    receipt_no: Optional[str] = None,
) -> Path:
    """Generate a WB Bank SBP receipt PDF and return its path.

    ``sender_name`` / ``sender_phone`` are accepted for API symmetry with
    Yandex and ignored: genuine WB SBP receipts have no sender block.
    """
    del sender_name, sender_phone  # no field on the donor
    op = _parse_operation_dt(operation_dt)
    printed_amount = _parse_amount_text(amount)
    printed_name = _format_person(recipient_name)
    printed_phone = _format_phone(recipient_phone)
    printed_bank = _format_bank(recipient_bank)

    if output_path is None:
        output = DEFAULT_OUT_DIR / f"{uuid.uuid4()}.pdf"
    else:
        output = Path(output_path).expanduser()

    template_paths = donor_pool_paths(donor_path, donor_pool_dir)
    glyph_dir = donor_pool_dir or DEFAULT_POOL_DIR
    glyph_paths = donor_pool_paths(None, glyph_dir)
    if donor_path is not None and Path(donor_path).expanduser().is_file():
        # still harvest glyphs from the whole pool next to the pin
        extra = donor_pool_paths(None, Path(donor_path).expanduser().parent)
        for path in extra:
            if path not in glyph_paths:
                glyph_paths.append(path)
    if not template_paths:
        raise NoUnusedDonorTokenError(
            "В пуле нет настоящих чеков ВБ Банка. Нужен новый настоящий "
            "чек — свежий перевод СБП и скачанная квитанция; положите её "
            "PDF в banks/wb/donors/."
        )

    pool = build_pool(glyph_paths)
    points = _corpus_receipt_points(glyph_paths) or list(_RECEIPT_POINTS)

    candidates: List[_Candidate] = []
    for path in template_paths:
        if not path.is_file():
            continue
        try:
            receipt = Receipt(str(path), glyph_pool=pool)
        except Exception:  # noqa: BLE001
            continue
        candidate = _Candidate(path, receipt)
        if len(candidate.sbp_id) != 32 or not candidate.token:
            continue
        candidates.append(candidate)
    candidates.sort(
        key=lambda c: (c.moment or datetime.min.replace(tzinfo=MSK), c.path.name),
        reverse=True,
    )
    if donor_path is not None and not Path(donor_path).expanduser().is_dir():
        pin = Path(donor_path).expanduser()
        candidates = [c for c in candidates if c.path == pin]

    gen = generated_at or datetime.now(MSK)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=MSK)
    gen_utc = gen.astimezone(timezone.utc).replace(microsecond=0)

    # coverage is against the harvested pool (inject is allowed)
    preview_sbp = "B" + "0" * 31
    preview_receipt = "Квитанция № 1022107512"
    if candidates:
        preview_sbp = make_sbp_id(
            op.replace(second=op.second or candidates[0].printed_seconds),
            candidates[0].sbp_id,
        )
        preview_receipt = "Квитанция № " + (
            receipt_no
            or make_receipt_no(gen_utc, candidates[0].receipt_no, points=points)
        )
    planned_preview = {
        "amount": printed_amount,
        "datetime": op.strftime("%d.%m.%Y %H:%M:%S"),
        "recipient": printed_name,
        "phone": printed_phone,
        "bank": printed_bank,
        "sbp_id": sbp_id or preview_sbp,
        "receipt": preview_receipt if receipt_no is None else f"Квитанция № {receipt_no}",
    }
    missing = _missing_in_pool(pool, planned_preview)
    if missing:
        absent = "".join(sorted({c for chars in missing.values() for c in chars}))
        raise GlyphCoverageError(
            f"Буквы вне пула глифов настоящих чеков ВБ Банка: {absent}. "
            "Нужен новый настоящий чек ВБ с этими буквами — свежий перевод "
            "СБП и скачанная квитанция; положите её PDF в banks/wb/donors/."
        )

    reg_path = registry_path(token_registry_path)
    chosen: Optional[_Candidate] = None
    with _registry_lock(reg_path):
        spent = _load_registry(reg_path)
        free = [c for c in candidates if c.token not in spent]
        if not free:
            if candidates:
                raise NoUnusedDonorTokenError(
                    "Нет свободного токена: все настоящие чеки ВБ Банка в пуле "
                    "уже использованы по одному разу. Нужен новый настоящий "
                    "чек ВБ — свежий перевод СБП и скачанная квитанция; "
                    "положите её PDF в banks/wb/donors/."
                )
            raise NoUnusedDonorTokenError(
                "В пуле нет настоящих чеков ВБ Банка. Нужен новый настоящий "
                "чек — свежий перевод СБП и скачанная квитанция; положите её "
                "PDF в banks/wb/donors/."
            )
        chosen = free[0]
        spent[chosen.token] = {
            "donor": chosen.path.name,
            "spent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "operation": op.isoformat(),
        }
        _save_registry(reg_path, spent)

    assert chosen is not None
    if op.second == 0 and op.microsecond == 0:
        op = op.replace(second=chosen.printed_seconds)
    new_sbp = sbp_id or make_sbp_id(op, chosen.sbp_id)
    new_receipt = receipt_no or make_receipt_no(
        gen_utc, chosen.receipt_no, points=points
    )

    template = chosen.path
    if output.resolve() == template.resolve():
        raise ReceiptError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    receipt = Receipt(str(template), glyph_pool=pool)
    receipt.operation_dt = op
    receipt.created_dt = gen
    receipt.set("datetime", op.strftime("%d.%m.%Y %H:%M:%S"))
    receipt.set("recipient", printed_name)
    receipt.set("phone", printed_phone)
    receipt.set("bank", printed_bank)
    receipt.set_amount(printed_amount)
    receipt.set("sbp_id", new_sbp)
    receipt.set("receipt", f"Квитанция № {new_receipt}")
    receipt.save(str(output), reidentify=True, mode="natural")

    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/Prev" in raw
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            "natural save failed: generated file is not a single-pass PDF"
        )
    return output
