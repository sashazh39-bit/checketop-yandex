#!/usr/bin/env python3
"""VTB receipt generation without a real transfer.

Uses bundled template PDFs from шаблоны/ (in git, for deploy) or
база_чеков/vtb/ locally — patches fields via vtb_patch_from_config.

Receipt types:
  - sbp:     шаблоны/vtb_sbp_template.pdf (from check (3).pdf)
  - vtb_vtb: шаблоны/vtb_vtb_template.pdf
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from vtb_cmap import get_unsupported_chars, format_unsupported_error, gen_sbp_operation_id
from vtb_patch_from_config import patch_from_values, patch_amount_only
from vtb_test_generator import update_creation_date

_BASE = Path(__file__).resolve().parent
TEMPLATES_DIR = _BASE / "шаблоны"
TEMPLATES_ASCII = _BASE / "templates"
SBP_DIR = _BASE / "база_чеков" / "vtb" / "СБП"
VTB_VTB_DIR = _BASE / "база_чеков" / "vtb" / "ВТБ на ВТБ"

# Bundled in git — root (deploy), templates/, шаблоны/, then база_чеков/.
SBP_BUNDLED = _BASE / "vtb_sbp_template.pdf"
VTB_VTB_BUNDLED = _BASE / "vtb_vtb_template.pdf"
SBP_BUNDLED_TEMPLATES = TEMPLATES_ASCII / "vtb_sbp_template.pdf"
VTB_VTB_BUNDLED_TEMPLATES = TEMPLATES_ASCII / "vtb_vtb_template.pdf"
SBP_BUNDLED_ALT = TEMPLATES_DIR / "vtb_sbp_template.pdf"
VTB_VTB_BUNDLED_ALT = TEMPLATES_DIR / "vtb_vtb_template.pdf"

# Optional local overrides from база_чеков.
SBP_TEMPLATE = SBP_DIR / "check (3).pdf"
SBP_TEMPLATE_ALT = SBP_DIR / "15-03-26_00-00.pdf"
VTB_VTB_TEMPLATE = VTB_VTB_DIR / "14-02-26_14-01.pdf"


def _decimal_safe_incs(hex_char: str) -> list[int]:
    base = int(hex_char.upper(), 16)
    return [i for i in range(1, 16) if (base + i) % 16 < 10]


def _change_one_char_in_id(data: bytearray) -> None:
    """Change exactly one decimal-safe nibble in /ID[0] (VTB verifier)."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    hex1 = id_m.group(1).decode().upper()
    pos = 0
    incs = _decimal_safe_incs(hex1[pos])
    if not incs:
        return
    inc = incs[0]
    idx = "0123456789ABCDEF".find(hex1[pos])
    new_c = "0123456789ABCDEF"[(idx + inc) % 16]
    new1 = hex1[:pos] + new_c + hex1[pos + 1:]
    slot_len = id_m.end(1) - id_m.start(1)
    new_enc = new1.encode().ljust(slot_len)[:slot_len]
    data[id_m.start(1) : id_m.end(1)] = new_enc
    data[id_m.start(2) : id_m.end(2)] = new_enc


def _parse_date(date_str: str | None) -> tuple[str, str]:
    if not date_str or str(date_str).strip().lower() in ("now", "сейчас", "auto"):
        dt = datetime.now()
        return dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M")
    parts = str(date_str).strip().split(",")
    date_part = parts[0].strip()
    time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
    return date_part, time_part


def _gen_phone() -> str:
    d = lambda n: "".join(str(random.randint(0, 9)) for _ in range(n))
    return f"+7 ({d(3)}) {d(3)}-{d(2)}-{d(2)}"


def _validate_text(*texts: str) -> Optional[str]:
    combined = "".join(t for t in texts if t)
    bad = get_unsupported_chars(combined)
    if bad:
        return format_unsupported_error(bad)
    return None


def _pick_sbp_template() -> Path:
    for p in (
        SBP_BUNDLED,
        SBP_BUNDLED_TEMPLATES,
        SBP_BUNDLED_ALT,
        SBP_TEMPLATE,
        SBP_TEMPLATE_ALT,
    ):
        if p.exists():
            return p
    if SBP_DIR.exists():
        pdfs = sorted(SBP_DIR.glob("*.pdf"))
        if pdfs:
            return pdfs[0]
    raise FileNotFoundError(
        f"Шаблон ВТБ СБП не найден. Нужен {SBP_BUNDLED.name} в шаблоны/ "
        f"или PDF в {SBP_DIR}"
    )


def _pick_vtb_vtb_template() -> Path:
    for p in (
        VTB_VTB_BUNDLED,
        VTB_VTB_BUNDLED_TEMPLATES,
        VTB_VTB_BUNDLED_ALT,
        VTB_VTB_TEMPLATE,
    ):
        if p.exists():
            return p
    if VTB_VTB_DIR.exists():
        pdfs = sorted(VTB_VTB_DIR.glob("*.pdf"))
        if pdfs:
            return pdfs[-1]
    raise FileNotFoundError(
        f"Шаблон ВТБ→ВТБ не найден. Нужен {VTB_VTB_BUNDLED.name} в шаблоны/ "
        f"или PDF в {VTB_VTB_DIR}"
    )


def pick_sbp_template() -> Path:
    """Public: resolve SBP template path (bundled first)."""
    return _pick_sbp_template()


def pick_vtb_vtb_template() -> Path:
    """Public: resolve VTB→VTB template path (bundled first)."""
    return _pick_vtb_vtb_template()


def _finalize(data: bytearray, date_str_full: str) -> bytes:
    meta_date = datetime.strptime(date_str_full, "%d.%m.%Y, %H:%M").strftime(
        "D:%Y%m%d%H%M00+03'00'"
    )
    update_creation_date(data, meta_date)
    _change_one_char_in_id(data)
    return bytes(data)


def get_fields_for_scratch(receipt_type: str) -> list[dict]:
    """Field definitions for step-by-step wizard UI."""
    if receipt_type == "vtb_vtb":
        return [
            {"key": "amount", "label": "Сумма", "prompt": "Сумма (целое число, ₽)", "default": "5000"},
            {"key": "datetime", "label": "Дата", "prompt": "Дата (ДД.ММ.ГГГГ, ЧЧ:ММ или now)", "default": "now"},
            {"key": "payer", "label": "Плательщик", "prompt": "Плательщик (ФИО)", "default": "Елена Андреевна А."},
            {"key": "recipient", "label": "Получатель", "prompt": "Получатель (ФИО)", "default": "Алан Петрович Е."},
            {"key": "phone", "label": "Телефон", "prompt": "Телефон (+7 ...)", "default": ""},
            {"key": "bank", "label": "Банк", "prompt": "Банк получателя", "default": "ВТБ"},
            {"key": "operation_id", "label": "ID операции", "prompt": "ID операции (или пусто — авто)", "default": ""},
        ]
    return [
        {"key": "amount", "label": "Сумма", "prompt": "Сумма (целое число, ₽)", "default": "5000"},
        {"key": "datetime", "label": "Дата", "prompt": "Дата (ДД.ММ.ГГГГ, ЧЧ:ММ или now)", "default": "now"},
        {"key": "payer", "label": "Плательщик", "prompt": "Плательщик (ФИО)", "default": "Елена Андреевна А."},
        {"key": "recipient", "label": "Получатель", "prompt": "Получатель (ФИО)", "default": "Алан Петрович Е."},
        {"key": "phone", "label": "Телефон", "prompt": "Телефон (+7 ...)", "default": ""},
        {"key": "bank", "label": "Банк", "prompt": "Банк получателя", "default": "Т-Банк"},
        {"key": "account", "label": "Счёт", "prompt": "Последние 4 цифры счёта", "default": "9426"},
        {"key": "message", "label": "Сообщение", "prompt": "Сообщение (или - для дефолта)", "default": "Перевод по номеру телефона"},
        {"key": "operation_id", "label": "ID операции", "prompt": "ID операции СБП (или пусто — авто)", "default": ""},
    ]


_DEFAULT_SBP_MESSAGE = "Перевод по номеру телефона"


def generate_vtb_sbp(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str = "now",
    bank: str = "Т-Банк",
    phone: str | None = None,
    account: str | None = None,
    operation_id: str | None = None,
    message: str | None = None,
    template_path: Path | None = None,
    output_path: Path | str | None = None,
) -> bytes:
    """Generate VTB SBP receipt from bundled template (no user donor)."""
    msg = message if message and message.strip() not in ("-", "—", "авто", "auto") else _DEFAULT_SBP_MESSAGE
    err = _validate_text(payer or "", recipient, phone or "", bank or "", msg)
    if err:
        raise ValueError(err)

    donor = Path(template_path) if template_path else _pick_sbp_template()
    if not donor.exists():
        raise FileNotFoundError(f"Шаблон не найден: {donor}")

    date_part, time_part = _parse_date(date_str)
    date_str_full = f"{date_part}, {time_part}"
    phone_val = _format_phone(phone) if phone else _gen_phone()
    bank_val = (bank or "Т-Банк").replace("-", "\u2011")

    op_id = operation_id
    if not op_id:
        try:
            dt = datetime.strptime(f"{date_part} {time_part}", "%d.%m.%Y %H:%M")
            op_id = gen_sbp_operation_id(
                op_date=dt.date(),
                op_time_moscow=time_part,
                direction="B",
                recipient_bank=bank_val,
            )
        except Exception:
            op_id = None

    data = bytearray(donor.read_bytes())
    try:
        data = bytearray(patch_amount_only(data, donor, amount))
    except Exception:
        pass

    out = patch_from_values(
        data,
        donor,
        date_str=date_str_full,
        payer=payer if payer else None,
        recipient=recipient or None,
        phone=phone_val,
        bank=bank_val,
        amount=amount,
        operation_id=op_id,
        message=msg,
        keep_metadata=True,
        account_last4=account if account and re.match(r"^\d{4}$", str(account)) else None,
    )
    result = _finalize(bytearray(out), date_str_full)
    if output_path:
        Path(output_path).write_bytes(result)
    return result


def generate_vtb_vtb(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str = "now",
    bank: str = "ВТБ",
    phone: str | None = None,
    operation_id: str | None = None,
    template_path: Path | None = None,
    output_path: Path | str | None = None,
) -> bytes:
    """Generate VTB→VTB receipt from bundled template (no user donor)."""
    err = _validate_text(payer or "", recipient, phone or "", bank or "")
    if err:
        raise ValueError(err)

    donor = Path(template_path) if template_path else _pick_vtb_vtb_template()
    if not donor.exists():
        raise FileNotFoundError(f"Шаблон не найден: {donor}")

    date_part, time_part = _parse_date(date_str)
    date_str_full = f"{date_part}, {time_part}"
    phone_val = _format_phone(phone) if phone else _gen_phone()
    bank_val = bank or "ВТБ"

    data = bytearray(donor.read_bytes())
    try:
        data = bytearray(patch_amount_only(data, donor, amount))
    except Exception:
        pass

    out = patch_from_values(
        data,
        donor,
        date_str=date_str_full,
        payer=payer if payer else None,
        recipient=recipient or None,
        phone=phone_val,
        bank=bank_val,
        amount=None,
        operation_id=operation_id,
        keep_metadata=True,
    )
    result = _finalize(bytearray(out), date_str_full)
    if output_path:
        Path(output_path).write_bytes(result)
    return result


def _auto_val(v) -> bool:
    return v in ("auto", "-", "—", "", None)


def _normalize_form_values(values: dict) -> dict:
    """Map alfa-style new_gen form keys to VTB generator keys."""
    out = dict(values)

    if values.get("recipient_name") and not out.get("recipient"):
        out["recipient"] = values["recipient_name"]
    if values.get("recipient_phone") and not _auto_val(values["recipient_phone"]):
        out["phone"] = values["recipient_phone"]
    if values.get("recipient_bank") and not _auto_val(values["recipient_bank"]):
        out["bank"] = values["recipient_bank"]
    if values.get("account_last4") and not _auto_val(values["account_last4"]):
        out["account"] = values["account_last4"]
    if values.get("sbp_id") and not _auto_val(values["sbp_id"]):
        out["operation_id"] = values["sbp_id"]
    if values.get("operation_id") and not _auto_val(values["operation_id"]):
        out["operation_id"] = values["operation_id"]

    od = values.get("operation_date")
    ot = values.get("operation_time")
    gd = values.get("gen_date")
    if not _auto_val(gd) and _auto_val(od):
        od = gd
    if not _auto_val(od):
        if not _auto_val(ot):
            time_part = str(ot).strip()
            parts = time_part.split(":")
            if len(parts) >= 2:
                time_part = f"{parts[0]}:{parts[1]}"
            out["datetime"] = f"{str(od).strip()}, {time_part}"
        else:
            out["datetime"] = str(od).strip()

    return out


def _format_phone(phone: str) -> str:
    """Normalize phone: +79502501743 → +7 (950) 250-17-43 style when possible."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("7"):
        d = digits[1:]
        return f"+7 ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
    if len(digits) == 10:
        return f"+7 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"
    return phone


def generate_from_values(
    receipt_type: str,
    values: dict,
) -> bytes:
    """Unified entry: receipt_type is 'sbp' or 'vtb_vtb'. Accepts alfa-style form keys."""
    values = _normalize_form_values(values)

    amount_raw = values.get("amount") or values.get("gen_amount") or "5000"
    amount = int(str(amount_raw).replace(" ", "").replace(",", ""))
    date_str = values.get("datetime") or values.get("gen_date") or "now"
    payer = values.get("payer") or values.get("gen_payer") or None
    recipient = values.get("recipient") or values.get("gen_recipient") or values.get("recipient_name") or ""
    phone = values.get("phone") or values.get("gen_phone") or values.get("recipient_phone")
    bank = values.get("bank") or values.get("gen_bank") or values.get("recipient_bank")
    account = values.get("account") or values.get("gen_account") or values.get("account_last4")
    op_id = values.get("operation_id") or values.get("gen_operation_id") or values.get("sbp_id")
    message = values.get("message") or values.get("gen_message")

    if _auto_val(op_id):
        op_id = None
    if _auto_val(phone):
        phone = None
    if _auto_val(bank):
        bank = None
    if _auto_val(account):
        account = None
    if _auto_val(payer):
        payer = None
    if _auto_val(message):
        message = None

    if not recipient:
        raise ValueError("Укажите получателя")

    if receipt_type in ("vtb_vtb", "vtb_vtb_vtb"):
        return generate_vtb_vtb(
            payer=payer,
            recipient=recipient,
            amount=amount,
            date_str=date_str,
            bank=bank or "ВТБ",
            phone=phone,
            operation_id=op_id,
        )
    return generate_vtb_sbp(
        payer=payer,
        recipient=recipient,
        amount=amount,
        date_str=date_str,
        bank=bank or "Т-Банк",
        phone=phone,
        account=account,
        operation_id=op_id,
        message=message,
    )
