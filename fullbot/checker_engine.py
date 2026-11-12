# -*- coding: utf-8 -*-
"""Анти-фрод движок отдельного бота проверки чеков.

Ядро PDF-проверки не дублируется: импортируется receipt_validator.py
(локально из корня проекта, на VPS — копия рядом с ботом). Здесь:
разбор полей по каждому банку, строгий слой (strict_checks.py) и
итоговый вердикт с учётом базы дублей.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
for _path in (HERE, PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import fitz  # type: ignore
import receipt_validator as core  # type: ignore

import law_overrides
import strict_checks


BANK_NAMES = {
    "WB": "Wildberries Банк",
    "MTS": "МТС Банк",
    "VTB": "ВТБ",
    "RSHB": "Россельхозбанк",
    "OZON": "Озон Банк",
    "YANDEX": "Яндекс Банк",
}

# Достоверность зелёного вердикта по банку: чем больше genuine-корпус,
# на котором откалиброваны законы банка, тем выше.
GENUINE_CONFIDENCE = {"OZON": 98, "VTB": 97, "WB": 97, "MTS": 95, "RSHB": 95, "YANDEX": 94}

_MONEY_RE = re.compile(r"^\s*(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₽|RUR|RUB)\s*$")
_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}\s*(?:,|в)?\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*\(UTC\+3\))?")
_PO_RE = re.compile(r"po-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EMPTY = {"", "—", "-", "–"}


@dataclass
class ReceiptData:
    bank: str | None
    bank_name: str
    product: str | None = None
    status: str | None = None
    date: str | None = None
    amount: str | None = None
    amount_minor: int | None = None
    commission: str | None = None
    commission_minor: int | None = None
    total: str | None = None
    total_minor: int | None = None
    sender: str | None = None
    sender_phone: str | None = None
    sender_account: str | None = None
    sender_card: str | None = None
    recipient: str | None = None
    recipient_phone: str | None = None
    recipient_card: str | None = None
    recipient_account: str | None = None
    recipient_bank: str | None = None
    op_id: str | None = None
    doc_label: str | None = None
    doc_no: str | None = None
    auth_code: str | None = None


@dataclass
class AuditResult:
    file_sha256: str
    data: ReceiptData
    core_passed: bool
    hard_failed: list[dict[str, Any]] = field(default_factory=list)
    # Полный перечень проверок (пройденные + проваленные): технический
    # разбор для админа/selftest, где вердикт строится только по параметрам
    # PDF, а не по базам дублей/реестра генератора.
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def bank(self) -> str | None:
        return self.data.bank

    @property
    def core_ok(self) -> bool:
        return not self.hard_failed


@dataclass(frozen=True)
class Verdict:
    kind: str  # genuine | suspicious | fake
    confidence: int
    dup_kind: str | None = None
    dup_first_seen: str | None = None
    dup_count: int = 0


def to_minor(value: str | None) -> int | None:
    """'10 234 ₽' → 1023400, '3 046,52 ₽' → 304652, 'Без комиссии' → 0."""
    if value is None:
        return None
    if "без комиссии" in value.lower() or value.strip() in _EMPTY:
        return 0
    match = _MONEY_RE.match(value)
    if not match:
        return None
    digits = re.sub(r"[\s\u00a0]", "", match.group(1)).replace(",", ".")
    rub, _, frac = digits.partition(".")
    try:
        return int(rub) * 100 + int((frac + "00")[:2])
    except ValueError:
        return None


class _Lines:
    def __init__(self, text: str):
        self.items = [line.strip() for line in text.splitlines()]
        self.items = [line for line in self.items if line]

    def after(self, *labels: str) -> str | None:
        for label in labels:
            for index, line in enumerate(self.items[:-1]):
                if line == label:
                    value = self.items[index + 1]
                    return None if value in _EMPTY else value
        return None

    def first_money(self) -> str | None:
        return next((line for line in self.items if _MONEY_RE.match(line)), None)

    def first_date(self) -> str | None:
        for line in self.items:
            match = _DATE_RE.search(line)
            if match:
                return match.group(0)
        return None


def _text(raw: bytes) -> str:
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            return "".join(page.get_text() for page in document)
    except Exception:
        return ""


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _fill_wb(d: ReceiptData, lines: _Lines, text: str) -> None:
    d.amount = lines.first_money()
    d.date = lines.first_date()
    d.product = lines.after("Вид операции")
    d.status = lines.after("Статус операции")
    d.recipient = lines.after("Получатель")
    d.recipient_phone = lines.after("Телефон получателя")
    d.recipient_bank = lines.after("Банк получателя")
    d.doc_label, d.doc_no = "Квитанция №", _search(r"Квитанция\s*№\s*(\d+)", text)


def _fill_mts(d: ReceiptData, lines: _Lines, text: str) -> None:
    d.date = lines.first_date()
    d.recipient = lines.after("Получатель")
    d.recipient_phone = lines.after("Телефон получателя")
    d.recipient_bank = lines.after("Банк получателя")
    d.sender_account = lines.after("Счет списания", "Счёт списания")
    d.sender_card = lines.after("Карта списания")
    d.recipient_card = lines.after("Карта зачисления", "Карта получателя")
    d.amount = lines.after("Сумма", "Сумма перевода")
    d.commission = lines.after("Комиссия")
    d.total = lines.after("Итого")
    d.status = lines.after("Статус")
    d.doc_label, d.doc_no = "Код транзакции", lines.after("Код транзакции")
    if not d.doc_no and "Номер перевода" in text:
        d.doc_label, d.doc_no = "Номер перевода", lines.after("Номер перевода")
    if "БИК банка получателя" in text:
        d.product = "Перевод по реквизитам"
        d.recipient = " ".join(filter(None, [
            lines.after("Наименование получателя"),
            _search(r"Наименование получателя\s*\n[^\n]+\n\s*([А-ЯЁ]+)\s*\n", text),
        ])) or None
        d.recipient_account = lines.after("Номер счета")
        d.recipient_bank = _search(r"Наименование банка\s*\n\s*получателя\s*\n\s*([^\n]+)", text)
    elif "Счет-выписка" in text:
        d.product = "Счёт-выписка"
    elif "Номер перевода" in text:
        d.product = "Перевод по номеру карты"
    elif d.sender_card:
        d.product = "Перевод с карты"
    else:
        d.product = "Перевод по СБП"


def _fill_vtb(d: ReceiptData, lines: _Lines, text: str) -> None:
    d.product = lines.items[0] if lines.items else None
    d.status = lines.after("Статус")
    d.date = lines.after("Дата операции")
    d.sender = lines.after("Имя плательщика", "Отправитель")
    d.sender_account = lines.after("Счет списания", "Счёт списания")
    d.sender_card = lines.after("Номер карты")
    d.recipient = lines.after("Получатель")
    d.recipient_phone = lines.after("Телефон получателя")
    d.recipient_card = lines.after("Карта получателя")
    d.recipient_account = lines.after("Счет получателя", "Счёт получателя")
    bm = re.search(
        r"(?m)^Банк получателя\s*\n\s*(.+?)(?=\n(?:Комиссия|ID операции|Сумма операции)\b)",
        text, re.S)
    if bm:
        d.recipient_bank = " ".join(ln.strip() for ln in bm.group(1).splitlines() if ln.strip())
    else:
        d.recipient_bank = lines.after("Банк получателя")
    d.amount = lines.after("Сумма операции")
    d.commission = lines.after("Комиссия")
    d.total = lines.after("Сумма с комиссией")


def _fill_rshb(d: ReceiptData, lines: _Lines, text: str) -> None:
    items = lines.items
    if items:
        d.product = items[0]
        if len(items) > 1 and items[1].startswith("через"):
            d.product = f"{items[0]} {items[1]}"
    d.status = next((x for x in items[:4] if x in ("Исполнен", "Выполнен", "Отклонён")), None)
    d.amount = lines.first_money()
    d.date = lines.first_date()
    d.sender_account = lines.after("Счёт списания", "Счет списания")
    d.recipient = lines.after("Получатель")
    d.recipient_phone = lines.after("Номер телефона получателя")
    d.commission = lines.after("Комиссия")
    d.recipient_bank = lines.after("Банк получателя")


def _fill_ozon(d: ReceiptData, lines: _Lines, text: str) -> None:
    d.product = lines.items[0] if lines.items else None
    d.date = lines.first_date()
    d.total = lines.after("Итого")
    d.status = lines.after("Статус")
    d.sender_account = lines.after("Счёт списания")
    d.amount = lines.after("Сумма")
    d.commission = lines.after("Комиссия")
    d.recipient = lines.after("Получатель")
    d.recipient_phone = lines.after("Телефон получателя")
    d.recipient_bank = lines.after("Банк получателя")
    d.sender = lines.after("Отправитель")
    d.recipient_card = lines.after("Номер карты получателя")
    po = _PO_RE.search(re.sub(r"\s+", "", text))
    if po:
        d.doc_label, d.doc_no = "ID операции", po.group(0)


def _fill_yandex(d: ReceiptData, lines: _Lines, text: str) -> None:
    d.doc_label, d.doc_no = "Исх. №", _search(r"Исх\.\s*№\s*(\d+)", text)
    d.date = lines.after("Дата и время операции МСК") or lines.first_date()
    d.product = lines.after("Тип операции")
    if "Выписка по договору" in text:
        d.product = "Выписка по договору"
        d.date = d.date or _search(r"Дата\s+(\d{2}\.\d{2}\.\d{4})", text)
    d.status = lines.after("Статус")
    d.sender_phone = lines.after("Телефон отправителя", "Откуда")
    d.sender = lines.after("ФИО отправителя")
    d.recipient_phone = lines.after("Телефон получателя", "Куда")
    d.recipient = lines.after("ФИО получателя")
    d.recipient_bank = lines.after("Банк получателя")
    d.amount = lines.after("Сумма операции")
    d.commission = lines.after("Комиссия")
    d.auth_code = lines.after("Код авторизации")


_FILLERS = {
    "WB": _fill_wb, "MTS": _fill_mts, "VTB": _fill_vtb,
    "RSHB": _fill_rshb, "OZON": _fill_ozon, "YANDEX": _fill_yandex,
}


def extract(raw: bytes, bank: str | None, text: str) -> ReceiptData:
    data = ReceiptData(bank=bank, bank_name=BANK_NAMES.get(bank or "", "не определён"))
    lines = _Lines(text)
    filler = _FILLERS.get(bank or "")
    if filler:
        filler(data, lines, text)
    else:
        data.date = lines.first_date()
        data.amount = lines.first_money()
    data.op_id = core._extract_opid(text) if text else None
    data.amount_minor = to_minor(data.amount)
    data.commission_minor = to_minor(data.commission) if data.commission else None
    data.total_minor = to_minor(data.total)
    return data


def _strict(raw: bytes, data: ReceiptData) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [strict_checks.font_subset_exact(raw)]
    prov = strict_checks.engine_provenance(data.bank, raw)
    if prov:
        checks.append(prov)
    if data.bank == "VTB":
        checks.append(strict_checks.vtb_subset_tag(raw))
        checks.append(strict_checks.vtb_amount_alignment(raw))
    parsed = core._parse_opid(data.op_id) if data.op_id else None
    displayed = strict_checks.parse_display_datetime(data.date)
    if parsed and displayed:
        checks.append(strict_checks.opid_display_time(parsed["op_utc"], displayed))
    if data.bank in ("OZON", "MTS"):
        arithmetic = strict_checks.amount_arithmetic(
            data.amount_minor, data.commission_minor, data.total_minor
        )
        if arithmetic:
            checks.append(arithmetic)
    current = strict_checks.current_profile_consistency(
        data.bank,
        displayed,
        data.doc_no,
        parsed,
        law_overrides.creation_date(raw),
    )
    if current:
        checks.append(current)
    return checks


def audit(pdf_bytes: bytes) -> AuditResult:
    raw = bytes(pdf_bytes)
    digest = hashlib.sha256(raw).hexdigest()
    result = core.validate(raw)
    text = _text(raw) if result.get("bank") else ""
    data = extract(raw, result.get("bank"), text)
    checks = list(result["checks"])
    if result.get("bank"):
        checks = law_overrides.apply(checks, raw, text, data.bank, data.op_id, data.date)
        checks += _strict(raw, data)
    failed = [check for check in checks if not check["passed"]]
    return AuditResult(
        file_sha256=digest,
        data=data,
        core_passed=bool(result.get("passed")),
        hard_failed=failed,
        checks=checks,
    )


_DUP_RISK = {"mismatch": 97, "file": 90, "same_op": 88, "issued": 86}


def decide_technical(result: AuditResult) -> Verdict:
    """Чисто технический вердикт: только параметры самого PDF.

    Базы дублей, реестр генератора (generated_documents) и жалобы здесь
    НЕ учитываются. Это режим админа и selftest «генератор ↔ чекер»:
    он отвечает на вопрос «что в файле хорошо, а что плохо» и показывает,
    сломался ли генератор, даже если файл уже проверялся или был выдан
    нашим ботом. Для обычных пользователей применяется decide() ниже.
    """
    if result.hard_failed:
        return Verdict("fake", min(99, 96 + len(result.hard_failed)))
    return Verdict("genuine", GENUINE_CONFIDENCE.get(result.bank or "", 93))


def decide(result: AuditResult, dup: Any | None = None) -> Verdict:
    """Публичный вердикт: технический слой + базы дублей/реестра/жалоб.

    Любой провал решающей проверки — ❌. Повтор файла, ID операции или
    номера документа — ⚠️ (при другой сумме риск выше). Иначе — ✅.

    Повтор ID с другой суммой не делается ❌ намеренно: какой из двух
    файлов оригинал, по одной базе не решить, а genuine не должен краснеть.
    """
    dup_kind = getattr(dup, "kind", None)
    first_seen = getattr(dup, "first_seen", None)
    count = int(getattr(dup, "seen_count", 0) or 0)
    # Подтверждённый фейк из жалоб пользователей — красный у всех, даже если
    # PDF технически чистый (donor-edit): это и есть «обучение на фейках».
    # synthetic — файл, который выдал наш бот-генератор (его SHA в
    # generated_documents): тоже красный у всех, по той же логике.
    if dup_kind in ("fraud", "synthetic"):
        return Verdict("fake", 99, dup_kind, first_seen, count)
    if result.hard_failed:
        return Verdict("fake", min(99, 96 + len(result.hard_failed)), dup_kind, first_seen, count)
    if dup_kind in _DUP_RISK:
        return Verdict("suspicious", _DUP_RISK[dup_kind], dup_kind, first_seen, count)
    return Verdict("genuine", GENUINE_CONFIDENCE.get(result.bank or "", 93))
