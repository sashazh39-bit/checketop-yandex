# -*- coding: utf-8 -*-
"""Поправки законов receipt_validator.py, откалиброванные на genuine-корпусе.

Ядро не меняется (его же используют генераторы проекта); поправки
применяются только в чекере и только ослабляют законы там, где ядро
давало ложный ❌ на настоящих чеках. Эра G (суффикс 0011890101, живые
переводы ноября 2026) остаётся строгой: на ней 0 нарушений у всех банков.

* opid_flag — в исторических эрах флаг шире {0,1,2}: WB 3/4/5/7/H,
  Яндекс 0–9 (≈100 genuine).
* opid_gateway — связка prefix↔gateway теперь жёсткая для ВСЕХ банков
  (прежняя поблажка для Яндекса была выучена на фейках и удалена 2026-11-10).
* creation_lag — CreationDate пишется с точностью до секунды, а SBP ID —
  до миллисекунды: у Яндекса CD на 0.1–0.9 с «раньше» ID. Закон: CD не
  раньше секунды операции; верхней границы нет (скачивание задним числом).
* продукты без SBP ID, которых ядро не знает: переводы МТС по номеру карты
  («Номер перевода») и по реквизитам (БИК/счёт), внутрибанковский перевод
  и выписка Яндекса.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import receipt_validator as core  # type: ignore

from strict_checks import _dynamic_anchors, parse_display_datetime

_OPID_CHECKS = {"opid_present", "opid_format", "opid_era", "opid_flag",
                "opid_node", "opid_gateway", "opid_tail"}
_MSK = timezone(timedelta(hours=3))


def no_opid_product(bank: str | None, text: str) -> bool:
    if bank == "MTS":
        return "Карта списания" in text and any(
            marker in text for marker in ("Номер перевода", "Код транзакции", "БИК банка получателя"))
    if bank == "YANDEX":
        return "Внутрибанковский перевод" in text or "Выписка по договору" in text
    if bank == "VTB":
        return "По номеру телефона клиенту ВТБ" in text and (
            "Счет получателя" in text or "Счёт получателя" in text)
    return False


def _vtb_kop(label: str, text: str) -> int | None:
    match = re.search(
        r"(?m)^" + re.escape(label) + r"\s*\n\s*([\d][\d\s\u00a0]*(?:[.,]\d{2})?)\s*₽",
        text,
    )
    if not match:
        return None
    clean = re.sub(r"[\s\u00a0]", "", match.group(1)).replace(",", ".")
    rub, _, frac = clean.partition(".")
    try:
        return int(rub) * 100 + int((frac + "00")[:2])
    except ValueError:
        return None


def creation_date(raw: bytes) -> datetime | None:
    value = core._info_dict(raw).get("/CreationDate", "")
    match = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", value)
    if not match:
        return None
    offset = 0
    tz_match = re.search(r"([+\-]\d{2})'(\d{2})'$", value)
    if tz_match:
        sign = 1 if tz_match.group(1)[0] == "+" else -1
        offset = sign * (abs(int(tz_match.group(1))) * 60 + int(tz_match.group(2))) * 60
    return datetime(*map(int, match.groups()), tzinfo=timezone(timedelta(seconds=offset)))


def _ok(check: dict[str, Any], note: str) -> dict[str, Any]:
    return {**check, "passed": True, "detail": f"{check.get('detail', '')} | override: {note}"}


def apply(checks: list[dict[str, Any]], raw: bytes, text: str, bank: str | None,
          op_id: str | None, displayed: str | None) -> list[dict[str, Any]]:
    parsed = core._parse_opid(op_id) if op_id else None
    historical = bool(parsed and not core._is_current_era(parsed))
    product = op_id is None and no_opid_product(bank, text)
    by_name = {c["name"]: bool(c["passed"]) for c in checks}
    cd = None
    out = []
    for check in checks:
        name = check["name"]
        if check["passed"]:
            out.append(check)
            continue
        if product and name in _OPID_CHECKS:
            check = _ok(check, "product without SBP op_id")
        elif product and name == "text_phone":
            check = _ok(check, "no recipient phone in this product")
        elif name == "text_date" and bank == "YANDEX" and "Выписка по договору" in text \
                and re.search(r"\d{2}\.\d{2}\.\d{4}", text):
            check = _ok(check, "statement carries a date without time")
        elif name == "opid_flag" and historical and parsed["flag"].isalnum():
            check = _ok(check, "historical era: wide flag alphabet")
        elif name == "opid_era" and parsed:
            eras = _dynamic_anchors().get("sbp_eras") or []
            if [parsed["route"], parsed["suffix"]] in eras or (
                f"{parsed['route']}/{parsed['suffix']}" in eras
            ):
                check = _ok(check, f"live attested era {parsed['route']}/{parsed['suffix']}")
        elif name == "card_commission" and bank == "VTB" \
                and by_name.get("card_total") and by_name.get("card_bin_bank_match"):
            amt_op = _vtb_kop("Сумма операции", text)
            comm = _vtb_kop("Комиссия", text)
            if amt_op is not None and comm is not None:
                if comm == 0:
                    check = _ok(check, "VTB 0 RUB package/subscription card tariff")
                elif comm == max(7900, (amt_op * 2 + 50) // 100):
                    check = _ok(check, "VTB attested min-79 RUB card tariff")
        # NOTE: the former Yandex "historical era ignores prefix<->gateway
        # binding" override is REMOVED (2026-11-10). It was learned from a
        # fake-contaminated corpus: 58/112 "genuine" Yandex were an old
        # generator's output (suffix 0011850501, CreationDate == op second,
        # random gateway). Every user-confirmed genuine Yandex obeys the
        # prefix<->gateway binding, so it is a hard NSPK law for all banks.
        elif name == "creation_lag":
            cd = cd or creation_date(raw)
            if cd and parsed:
                lag = (cd - parsed["op_utc"]).total_seconds()
                if lag >= 0:
                    check = _ok(check, f"CD - op = {lag:.1f}s >= 0")
            elif cd and product:
                shown = parse_display_datetime(displayed)
                if shown is None or (cd - shown.replace(tzinfo=_MSK)).total_seconds() >= -60:
                    check = _ok(check, "document product: CD not before displayed minute")
        out.append(check)
    return out
