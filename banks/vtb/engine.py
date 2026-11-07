#!/usr/bin/env python3
"""Bot-facing engines for VTB SBP and card-transfer receipts.

Public contract mirrors ``generate_rocket`` / ``generate_rocket_card``:

    generate_vtb(...)       — исходящий перевод СБП (openhtmltopdf, title Чек-PDF)
    generate_vtb_card(...)  — перевод на карту (те же доноры openhtmltopdf,
                              комиссия всегда 99 ₽)

A genuine donor from ``donors_sbp/`` or ``donors_card/`` is patched
field by field and saved as a single-pass PDFBox file.  The old
``vtb_card_receipt.py`` rebuilds a *different* layout from scratch and
is not used here.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import List, Optional, Tuple, Union

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from vtb_receipt import (  # noqa: E402
    FORMAT_CARD,
    FORMAT_SBP,
    MSK,
    NB_HYPHEN,
    Receipt,
    ReceiptError,
    default_glyph_paths,
    load_catalog,
)

__all__ = ["generate_vtb", "generate_vtb_card", "generate_vtb_statement", "make_sbp_id"]

PathLike = Union[str, Path]


def _unique_existing(paths: List[Path]) -> List[Path]:
    seen: List[Path] = []
    for path in paths:
        if path and path.is_file() and path not in seen:
            seen.append(path)
    return seen


def _sbp_donors() -> List[Path]:
    env = os.environ.get("VTB_DONOR_PATH")
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(sorted((_HERE / "donors_sbp").glob("*.pdf")))
    return _unique_existing(candidates)


def _card_donors() -> List[Path]:
    env = os.environ.get("VTB_CARD_DONOR_PATH")
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(sorted((_HERE / "donors_card").glob("*.pdf")))
    return _unique_existing(candidates)


# ------------------------------------------------------------------ formatters


def _parse_amount(value) -> Decimal:
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
    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if cents != amount * 100:
        raise ValueError(f"amount {value!r} has fractions of a kopeck")
    return amount.quantize(Decimal("0.01"))


def render_amount(value) -> str:
    """VTB receipt form: ``13 ₽``, ``5 000 ₽``, ``12 995.74 ₽`` (dot, not comma)."""
    amount = _parse_amount(value)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    grouped = f"{whole:,}".replace(",", " ")
    if cents:
        return f"{grouped}.{cents:02d} ₽"
    return f"{grouped} ₽"


def _format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError(f"cannot parse phone {value!r}")
    h = NB_HYPHEN
    return f"+7 ({digits[1:4]}) {digits[4:7]}{h}{digits[7:9]}{h}{digits[9:11]}"


def _format_person(value: str) -> str:
    """Full name -> VTB print form ``Имя Отчество Ф.`` (title case)."""
    tokens = [t.strip(".") for t in str(value or "").replace(".", " .").split()]
    tokens = [t for t in tokens if t and t != "."]
    already = bool(tokens) and len(tokens[-1]) == 1
    if already:
        if len(tokens) < 2:
            raise ValueError(f"cannot parse name {value!r}")
        parts = [t.capitalize() if len(t) > 1 else t.upper() for t in tokens]
        printed = " ".join(parts)
        return printed if printed.endswith(".") else printed + "."
    def cap(token: str) -> str:
        return "-".join(p.capitalize() for p in token.split("-"))
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{cap(given)} {cap(patronymic)} {surname[0].upper()}."
    if len(tokens) == 2:
        surname, given = tokens
        return f"{cap(given)} {surname[0].upper()}."
    raise ValueError(f"cannot parse name {value!r}")


_BANK_FORMS = {
    "сбербанк": "Сбербанк",
    "сбер": "Сбербанк",
    "тбанк": f"Т{NB_HYPHEN}Банк",
    "т банк": f"Т{NB_HYPHEN}Банк",
    "тинькофф": f"Т{NB_HYPHEN}Банк",
    "т-банк": f"Т{NB_HYPHEN}Банк",
    "втб": "Банк ВТБ (ПАО)",
    "банк втб": "Банк ВТБ (ПАО)",
    "альфа банк": "Альфа-Банк",
    "альфабанк": "Альфа-Банк",
    "озон банк": "Озон Банк (Ozon)",
    "озон": "Озон Банк (Ozon)",
    "ozon": "Озон Банк (Ozon)",
    "рокет": "Рокетбанк",
    "рокетбанк": "Рокетбанк",
    "wildberries": "Wildberries (Вайлдберриз Банк)",
    "вайлдберриз": "Wildberries (Вайлдберриз Банк)",
    "вб банк": "Wildberries (Вайлдберриз Банк)",
    "вб": "Wildberries (Вайлдберриз Банк)",
    "совкомбанк": "Совкомбанк",
    "совком": "Совкомбанк",
}


def _format_bank(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ValueError("recipient bank is empty")
    key = cleaned.lower().replace("ё", "е").replace(NB_HYPHEN, "-")
    key = " ".join(re.sub(r"[\"«»()]", " ", key).split())
    return _BANK_FORMS.get(key, cleaned)


def _format_account(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 4:
        return "*" + digits
    if len(digits) == 20:
        return "*" + digits[-4:]
    if str(value).strip().startswith("*") and len(digits) == 4:
        return "*" + digits
    raise ValueError(f"account must be *XXXX or 20 digits, got {value!r}")


def _format_payer_card(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if "*" in str(value) and len(digits) >= 8:
        groups = re.findall(r"\d+", str(value))
        return f"{groups[0]}{'*' * 6}{groups[-1]}"
    if len(digits) != 16:
        raise ValueError(f"payer card must be 16 digits, got {value!r}")
    return f"{digits[:6]}{'*' * 6}{digits[-4:]}"


def _format_payee_card(value: str) -> str:
    """``220027******4432`` / ``2200 27** **** 4432`` -> ``2200 27** **** 4432``."""
    text = str(value or "")
    groups = re.findall(r"\d+", text)
    digits = "".join(groups)
    if "*" in text:
        if len(groups[0]) == 4 and len(groups) >= 2:
            first4 = groups[0]
            last4 = groups[-1]
            mid2 = groups[1][:2] if len(groups) > 2 or len(groups[1]) == 2 else digits[4:6]
            if len(mid2) != 2:
                mid2 = digits[4:6] if len(digits) >= 6 else "00"
            return f"{first4} {mid2}** **** {last4}"
        if len(digits) >= 10:
            return f"{digits[:4]} {digits[4:6]}** **** {digits[-4:]}"
        raise ValueError(f"cannot parse masked card {value!r}")
    if len(digits) == 16:
        return f"{digits[:4]} {digits[4:6]}** **** {digits[-4:]}"
    raise ValueError(
        f"card must be 16 digits or a mask like '2200 27** **** 4432', got {value!r}"
    )


def _parse_operation_dt(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = " ".join(str(value or "").strip().split())
        text = re.sub(r"\s+ПО МСК$", "", text, flags=re.IGNORECASE)
        moment = None
        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y, %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y, %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            raise ValueError(f"cannot parse operation datetime {value!r}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def format_op_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y, %H:%M")


# ------------------------------------------------------------------ SBP id


def _donor_sbp_seconds(sbp_id: str) -> int:
    if len(sbp_id) == 32 and sbp_id[5:11].isdigit():
        return int(sbp_id[9:11])
    return 0


def make_sbp_id(op_msk: datetime, donor_sbp: str = "") -> str:
    """Rebuild the UTC timestamp header; inherit the donor's opaque serial.

    Positions 1-11 encode the UTC calendar/clock of the operation (same
    ОПКЦ header as Yandex/Rocket).  Positions 12-32 are inherited from
    the genuine donor — they cannot be invented.
    """
    utc = op_msk.astimezone(timezone.utc)
    stamp = (
        str(utc.year % 10)
        + f"{utc.timetuple().tm_yday:03d}"
        + utc.strftime("%H%M%S")
    )
    if len(donor_sbp) == 32 and donor_sbp[1:11].isdigit():
        candidate = donor_sbp[0] + stamp + donor_sbp[11:]
        if candidate != donor_sbp:
            return candidate
        # same second as the donor: bump the inherited seconds by 1.
        utc2 = utc + timedelta(seconds=1)
        stamp2 = (
            str(utc2.year % 10)
            + f"{utc2.timetuple().tm_yday:03d}"
            + utc2.strftime("%H%M%S")
        )
        return donor_sbp[0] + stamp2 + donor_sbp[11:]
    return "A" + stamp + "00000G10080011890101"


def decode_sbp_moment(sbp_id: str) -> Optional[datetime]:
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


# ------------------------------------------------------------------ layout pick


def _message_lines(message: Optional[str], receipt: Receipt) -> Optional[List[str]]:
    if message is None:
        return None
    text = " ".join(str(message).split())
    if not text or text in ("—", "-"):
        return None
    extra = receipt.fields.get("message_extra") or []
    slots = 1 + len(extra)
    if slots <= 1:
        return [text]
    # greedy wrap by the donor's first-line length
    first_len = max(len(receipt.fields["message"].text), 20)
    words = text.split(" ")
    lines: List[str] = []
    current = ""
    for word in words:
        cand = word if not current else current + " " + word
        if len(cand) <= first_len or not current:
            current = cand
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > slots:
        raise ReceiptError(
            f"message wraps to {len(lines)} lines, donor has {slots}; "
            "use a longer-message donor (donors_sbp/vtb_sbp_06_*.pdf)"
        )
    while len(lines) < slots:
        lines.append("")
    return lines[:slots]


def _needs_bank_wrap(bank: str) -> bool:
    return "Wildberries" in bank or "Вайлдберриз" in bank


def _pick_sbp_donor(
    *,
    message: Optional[str],
    bank: str,
    donor_path: Optional[PathLike],
) -> Path:
    if donor_path:
        template = Path(donor_path).expanduser()
        if not template.is_file():
            raise FileNotFoundError(f"VTB SBP donor not found: {template}")
        return template
    candidates = _sbp_donors()
    if not candidates:
        raise FileNotFoundError(
            "no genuine VTB SBP donor in banks/vtb/donors_sbp/"
        )
    want_msg = bool(message and str(message).strip() not in ("", "—", "-"))
    want_wrap = _needs_bank_wrap(bank)
    scored: List[Tuple[int, Path]] = []
    for path in candidates:
        try:
            rec = Receipt(str(path))
        except Exception:  # noqa: BLE001
            continue
        if rec.format != FORMAT_SBP:
            continue
        has_msg = "message" in rec.fields
        has_wrap = "bank_to_2" in rec.fields
        long_msg = bool(rec.fields.get("message_extra"))
        score = 0
        if has_msg == want_msg:
            score += 4
        if has_wrap == want_wrap:
            score += 2
        if want_msg and long_msg and message and len(str(message)) > 12:
            score += 2
        if not want_msg and not has_msg:
            score += 1
        scored.append((score, path))
    if not scored:
        return candidates[0]
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return scored[0][1]


# ------------------------------------------------------------------ generate


def generate_vtb(
    amount,
    operation_dt,
    sender_name: str,
    sender_phone: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    output_path: Optional[PathLike] = None,
    *,
    donor_path: Optional[PathLike] = None,
    glyph_donor_paths: Optional[List[PathLike]] = None,
    sender_account: Optional[str] = None,
    message: Optional[str] = None,
    sbp_id: Optional[str] = None,
    creation_delta_minutes: Optional[float] = None,
) -> Path:
    """Generate a VTB SBP receipt PDF and return its path.

    ``sender_phone`` is accepted for Rocket-parity of the public
    contract; VTB SBP receipts do not print the sender's phone.
    """
    bank = _format_bank(recipient_bank)
    template = _pick_sbp_donor(message=message, bank=bank, donor_path=donor_path)
    extra = [Path(p).expanduser() for p in (glyph_donor_paths or [])]
    catalog = load_catalog(_unique_existing(extra + default_glyph_paths()))
    receipt = Receipt(str(template), catalog)
    if receipt.format != FORMAT_SBP:
        raise ReceiptError(
            f"donor {template.name} is not an SBP receipt; generate_vtb "
            "only supports the «Исходящий перевод СБП» layout"
        )
    if _needs_bank_wrap(bank) and "bank_to_2" not in receipt.fields:
        raise ReceiptError(
            f"bank {bank!r} wraps on genuine receipts; use the Wildberries "
            "donor (donors_sbp/vtb_sbp_07_18_wb.pdf)"
        )
    if (not _needs_bank_wrap(bank)) and "bank_to_2" in receipt.fields and donor_path:
        raise ReceiptError(
            f"donor {template.name} has a two-line bank block; pass a "
            "one-line SBP donor or a wrapping bank name"
        )
    want_msg = bool(message and str(message).strip() not in ("", "—", "-"))
    if want_msg and "message" not in receipt.fields:
        raise ReceiptError(
            f"donor {template.name} has no message row; use "
            "donors_sbp/vtb_sbp_05_*.pdf or vtb_sbp_06_*.pdf"
        )
    if (not want_msg) and "message" in receipt.fields and donor_path:
        raise ReceiptError(
            f"donor {template.name} carries a message row; use a no-message "
            "SBP donor or pass message=..."
        )

    op = _parse_operation_dt(operation_dt)
    donor_sbp = receipt.sbp_id
    if op.second == 0 and op.microsecond == 0:
        op = op.replace(second=_donor_sbp_seconds(donor_sbp))

    if output_path is None:
        output = _HERE / "out" / f"vtb_sbp_{op:%Y%m%d_%H%M}.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise ValueError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    if creation_delta_minutes is None:
        created = max(datetime.now(MSK), op + timedelta(seconds=8))
    else:
        created = op + timedelta(minutes=creation_delta_minutes)
    receipt.creation_dt = created

    recipient = _format_person(recipient_name)
    sender = _format_person(sender_name)
    receipt.set("headline", recipient)
    receipt.set("date", format_op_dt(op))
    receipt.set("amount", render_amount(amount))
    receipt.set("recipient", recipient)
    receipt.set("phone_to", _format_phone(recipient_phone))
    receipt.set("sender", sender)
    if sender_account is not None:
        receipt.set("account", _format_account(sender_account))
    if "bank_to_2" in receipt.fields:
        # Wildberries donor: keep the measured two-line split.
        receipt.set("bank_to", "Wildberries (Вайлдберриз")
        receipt.set_run(receipt.fields["bank_to_2"], "Банк)")
    else:
        receipt.set("bank_to", bank)
    if want_msg:
        lines = _message_lines(message, receipt)
        if lines:
            receipt.set("message", lines if len(lines) > 1 else lines[0])
    new_sbp = sbp_id or make_sbp_id(op, donor_sbp)
    receipt.set("sbp_id", new_sbp)

    receipt.save(str(output))
    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/XRef" in raw
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            f"save failed for {template.name}: structurally implausible file"
        )
    return output


def generate_vtb_card(
    amount,
    operation_dt,
    sender_name: str,
    recipient_card: str,
    recipient_bank: Optional[str] = None,
    output_path: Optional[PathLike] = None,
    *,
    donor_path: Optional[PathLike] = None,
    glyph_donor_paths: Optional[List[PathLike]] = None,
    sender_account: Optional[str] = None,
    payer_card: Optional[str] = None,
    fee: Optional[str] = None,
    creation_delta_minutes: Optional[float] = None,
) -> Path:
    """Generate a VTB «Перевод на карту» receipt. Commission is always 99 ₽."""
    if donor_path:
        template = Path(donor_path).expanduser()
    else:
        candidates = _card_donors()
        if not candidates:
            raise FileNotFoundError(
                "no genuine VTB card donor in banks/vtb/donors_card/"
            )
        template = candidates[0]
        if recipient_bank:
            want_bank = _format_bank(recipient_bank)
            for path in candidates:
                try:
                    probe = Receipt(str(path), catalog=None)
                except Exception:  # noqa: BLE001
                    continue
                bank_run = probe.fields.get("bank_to")
                if bank_run is not None and bank_run.text == want_bank:
                    template = path
                    break
    if not template.is_file():
        raise FileNotFoundError(f"VTB card donor not found: {template}")

    extra = [Path(p).expanduser() for p in (glyph_donor_paths or [])]
    catalog = load_catalog(_unique_existing(extra + default_glyph_paths()))
    receipt = Receipt(str(template), catalog)
    if receipt.format != FORMAT_CARD:
        raise ReceiptError(
            f"donor {template.name} is not a card-transfer receipt; "
            "generate_vtb_card only supports the «Перевод на карту» layout"
        )

    op = _parse_operation_dt(operation_dt)
    if output_path is None:
        output = _HERE / "out" / f"vtb_card_{op:%Y%m%d_%H%M}.pdf"
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == template.resolve():
        raise ValueError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    if creation_delta_minutes is None:
        created = max(datetime.now(MSK), op + timedelta(seconds=8))
    else:
        created = op + timedelta(minutes=creation_delta_minutes)
    receipt.creation_dt = created

    amount_d = _parse_amount(amount)
    commission = Decimal("99")
    if fee is not None:
        fee_d = _parse_amount(fee)
        if fee_d != commission:
            raise ReceiptError(
                "VTB card donors only ever print a 99 ₽ commission; "
                f"got {fee!r}"
            )
    total = amount_d + commission

    receipt.set("date", format_op_dt(op))
    receipt.set("amount", render_amount(amount_d))
    receipt.set("fee", render_amount(commission))
    receipt.set("total", render_amount(total))
    receipt.set("sender", _format_person(sender_name))
    receipt.set("card", _format_payee_card(recipient_card))
    if recipient_bank is not None:
        receipt.set("bank_to", _format_bank(recipient_bank))
    if sender_account is not None:
        receipt.set("account", _format_account(sender_account))
    if payer_card is not None:
        receipt.set("payer_card", _format_payer_card(payer_card))

    receipt.save(str(output))
    raw = output.read_bytes() if output.is_file() else b""
    if (
        not raw.startswith(b"%PDF-")
        or raw.count(b"startxref") != 1
        or raw.count(b"%%EOF") != 1
        or b"/XRef" in raw
    ):
        output.unlink(missing_ok=True)
        raise ReceiptError(
            f"save failed for {template.name}: structurally implausible file"
        )
    return output


def generate_vtb_statement(*args, **kwargs):
    """See ``vtb_statement.generate_vtb_statement`` (Jasper/OpenPDF stack)."""
    from vtb_statement import generate_vtb_statement as _impl

    return _impl(*args, **kwargs)
