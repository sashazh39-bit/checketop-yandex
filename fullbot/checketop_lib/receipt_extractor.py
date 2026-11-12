#!/usr/bin/env python3
"""Извлечение данных из PDF-чеков для генерации выписки."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

# Фамилии по первой букве для генерации ФИО
SURNAME_BY_LETTER: dict[str, str] = {
    "А": "Александров", "Б": "Борисов", "В": "Васильев", "Г": "Григорьев",
    "Д": "Дмитриев", "Е": "Егоров", "Ж": "Жуков", "З": "Захаров",
    "И": "Иванов", "К": "Козлов", "Л": "Лебедев", "М": "Михайлов",
    "Н": "Николаев", "О": "Орлов", "П": "Петров", "Р": "Романов",
    "С": "Сергеев", "Т": "Тихонов", "У": "Устинов", "Ф": "Федоров",
    "Х": "Харитонов", "Ц": "Цветков", "Ч": "Чернов", "Ш": "Шаров",
    "Щ": "Щербаков", "Э": "Эльдаров", "Ю": "Юдин", "Я": "Яковлев",
}


def extract_from_receipt(pdf_path: Path | str) -> dict[str, Any]:
    """
    Извлечь данные из чека.
    Возвращает: amount, date, time, account_last4, fio_payer, fio_recipient,
                bank_recipient, phone_recipient, operation_id, ...
    """
    path = Path(pdf_path)
    if not path.exists() or not fitz:
        return {}

    try:
        doc = fitz.open(path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return {}

    result: dict[str, Any] = {}

    # Сумма: receipt_db + fallback scan_templates
    try:
        from receipt_db import get_receipt_amount
        amount = get_receipt_amount(path)
        if amount is not None:
            result["amount"] = amount
    except ImportError:
        pass

    if "amount" not in result:
        m = re.search(r"[\d\s]+(?:\s*[₽]|RUR|р\.|RUB)", text)
        if m:
            nums = re.sub(r"\s", "", m.group(0))
            nums = re.sub(r"[^\d]", "", nums)
            if nums:
                result["amount"] = int(nums)

    # Поля через scan_templates
    try:
        from scan_templates import extract_fields_simple
        fields = extract_fields_simple(text)
        if fields.get("Сумма перевода"):
            m = re.search(r"[\d\s]+", fields["Сумма перевода"])
            if m:
                nums = re.sub(r"\s", "", m.group(0))
                nums = re.sub(r"[^\d]", "", nums)
                if nums and "amount" not in result:
                    result["amount"] = int(nums)
        if fields.get("Сумма списания, включая все комиссии"):
            m = re.search(r"[\d\s]+", fields["Сумма списания, включая все комиссии"])
            if m:
                nums = re.sub(r"\s", "", m.group(0))
                nums = re.sub(r"[^\d]", "", nums)
                if nums and "amount" not in result:
                    result["amount"] = int(nums)
        result["fio_recipient"] = fields.get("Получатель", "")
        result["fio_payer"] = fields.get("Плательщик", "")
        result["bank_recipient"] = fields.get("Банк получателя", "")
        result["phone_recipient"] = fields.get("Номер телефона получателя", "")
        result["operation_id"] = fields.get("Номер операции", "") or fields.get("Номер операции в банке", "")
        result["date_time"] = fields.get("Дата и время перевода", "")
        result["account"] = fields.get("Счёт списания", "") or fields.get("Номер карты отправителя", "")
    except ImportError:
        pass

    # Дата и время: regex
    dt_match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})", text)
    if dt_match:
        result["date"] = dt_match.group(1)
        result["time"] = dt_match.group(2)
    if result.get("date_time") and not result.get("date"):
        dt_match = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})", result["date_time"])
        if dt_match:
            result["date"] = dt_match.group(1)
            result["time"] = dt_match.group(2)

    # Последние 4 цифры счёта
    account = result.get("account", "")
    if not account:
        m = re.search(r"\*(\d{4})", text)
        if m:
            result["account_last4"] = m.group(1)
        else:
            m = re.search(r"(\d{4})\s*$", text)
            if m:
                result["account_last4"] = m.group(1)
    else:
        m = re.search(r"\*(\d{4})", account)
        if m:
            result["account_last4"] = m.group(1)
        else:
            m = re.search(r"(\d{4})\s*$", account)
            if m:
                result["account_last4"] = m.group(1)
        nums = re.sub(r"\D", "", account)
        if len(nums) >= 4:
            result["account_last4"] = nums[-4:]

    return result


def generate_fio_from_first_letter(first_letter: str, name: str = "", patronymic: str = "") -> str:
    """
    Сгенерировать ФИО: фамилия по первой букве + имя/отчество из чека или placeholder.
    """
    letter = (first_letter or "И").upper()
    surname = SURNAME_BY_LETTER.get(letter, "Иванов")
    n = name.strip() if name else "Иван"
    p = patronymic.strip() if patronymic else "Иванович"
    return f"{surname} {n} {p}"


def extract_commission_from_pdf(pdf_path: str | Path) -> int | None:
    """Извлечь сумму комиссии из PDF-чека. Возвращает рубли (целое) или None."""
    path = Path(pdf_path)
    if not path.exists() or not fitz:
        return None
    try:
        doc = fitz.open(str(path))
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return None
    patterns = [
        r"[Кк]омиссия[^\d]*?([\d\s]+)[,\.](\d{2})",
        r"[Кк]омиссия[^\d\n]*?([\d\s]+)\s*(?:₽|руб|RUR|RUB)",
        r"Commission[^\d]*?([\d\s]+)[,\.](\d{2})",
        r"[Кк]омиссия банка[^\d]*?([\d\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1).replace(" ", "").replace("\xa0", "")
            try:
                val = int(raw)
                return val
            except ValueError:
                continue
    return None
