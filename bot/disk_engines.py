# -*- coding: utf-8 -*-
"""Чеки МТС, ВТБ, ВБ и Яндекса — те же генераторы, что в боте с Яндекс Диска."""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DISK = Path(__file__).resolve().parents[1] / "banks" / "disk"
MSK = timezone(timedelta(hours=3))
_mods: dict = {}


def _prepare() -> None:
    os.environ["MTS_DONOR_SBP"] = str(DISK / "donors" / "file25.pdf")
    os.environ.setdefault("JZCOMP_DIR", str(DISK))
    jre = DISK.parents[1] / "jre" / "bin" / "java"
    if jre.exists():
        os.environ.setdefault("JAVA_BIN", str(jre))
    elif not os.environ.get("JAVA_BIN"):
        java = shutil.which("java")
        if java:
            os.environ["JAVA_BIN"] = java
    if str(DISK) not in sys.path:
        sys.path.insert(0, str(DISK))


def _mod(name: str):
    _prepare()
    if name not in _mods:
        import importlib
        _mods[name] = importlib.import_module(name)
    return _mods[name]


def rubles(value) -> int:
    if isinstance(value, bool):
        raise ValueError("пустая сумма")
    if isinstance(value, int):
        return value
    text = str(value).replace("\u20bd", "").replace("₽", "")
    text = text.replace("\u00a0", "").replace("\u2009", "").replace(" ", "")
    text = text.replace(",", ".")
    text = re.sub(r"[^\d.]", "", text)
    if not text:
        raise ValueError("не понял сумму")
    return int(float(text))


def phone_bank(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        return (value or "").strip()
    return f"+7 ({digits[0:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def short_name(raw: str) -> str:
    """Как в том боте: «Имя Отчество Ф», без точки."""
    text = (raw or "").replace("Ё", "Е").replace("ё", "е").strip()
    parts = [p for p in text.split() if p]
    if len(parts) >= 3 and len(parts[2]) > 1 and parts[2].lower().endswith(
        ("вич", "вна", "ич", "ична")
    ):
        return f"{parts[1]} {parts[2]} {parts[0][0].upper()}"
    return text.rstrip(".")


def naive_msk(moment):
    if moment is None:
        return None
    if getattr(moment, "tzinfo", None) is not None:
        return moment.astimezone(MSK).replace(tzinfo=None)
    return moment


def _last4(account) -> str | None:
    digits = re.sub(r"\D", "", str(account or ""))
    return digits[-4:] if len(digits) >= 4 else None


def mts_sbp(data: dict) -> bytes:
    mod = _mod("mts_generator")
    return mod.generate(
        short_name(data.get("recipient_name") or ""),
        phone_bank(data.get("recipient_phone") or ""),
        data.get("recipient_bank") or "Т-Банк",
        rubles(data.get("amount")),
        dt=naive_msk(data.get("operation_dt")),
        account_last4=_last4(data.get("sender_account")),
    )


def vtb_sbp(data: dict) -> bytes:
    mod = _mod("vtb_generator")
    return mod.generate(
        data.get("sender_name") or "",
        short_name(data.get("recipient_name") or ""),
        phone_bank(data.get("recipient_phone") or ""),
        data.get("recipient_bank") or "Т-Банк",
        rubles(data.get("amount")),
        message=data.get("vtb_message") or None,
        dt=naive_msk(data.get("operation_dt")),
        account=data.get("sender_account"),
    )


def wb_sbp(data: dict) -> bytes:
    mod = _mod("wb_generator")
    return mod.generate(
        short_name(data.get("recipient_name") or ""),
        phone_bank(data.get("recipient_phone") or ""),
        data.get("recipient_bank") or "Т-Банк",
        rubles(data.get("amount")),
        message=data.get("wb_message") or "",
        dt=naive_msk(data.get("operation_dt")),
    )


def yandex_receipt(data: dict) -> bytes:
    mod = _mod("yandex_receipt_generator")
    template = None
    if data.get("yandex_mode") == "template" and data.get("y_template_path"):
        template = str(data["y_template_path"])
    return mod.generate(
        sender=data.get("sender_name") or "",
        sender_phone=phone_bank(data.get("sender_phone") or ""),
        recipient=short_name(data.get("recipient_name") or ""),
        recipient_phone=phone_bank(data.get("recipient_phone") or ""),
        recipient_bank=data.get("recipient_bank") or "Сбербанк",
        amount=rubles(data.get("amount")),
        op_dt=naive_msk(data.get("operation_dt")) or datetime.now(),
        template_path=template,
    )
