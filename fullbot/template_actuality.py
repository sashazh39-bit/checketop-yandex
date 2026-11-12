# -*- coding: utf-8 -*-
"""Проверка актуальности шаблона банка по свежему оригинальному PDF.

Админ жмёт «Проверить на актуальность» и кидает свежий настоящий PDF из
приложения банка. Модуль сравнивает всё, КРОМЕ самого SBP ID и динамических
значений (ФИО, сумма, телефон, дата, номер транзакции), с нашими эталонами
и генераторами по всем 6 банкам (WB, МТС, ВТБ, РСХБ, Озон, Яндекс):

  • движок PDF: версия заголовка (%PDF-1.x), тип таблицы xref (classic / XRef stream),
    количество объектов и потоков FlateDecode, число страниц и размер страницы;
  • словарь /Info: точные строки /Producer и /Creator (включая версию и git-хэш
    JasperReports / OpenPDF / Skia / openhtmltopdf), наличие/отсутствие /ModDate,
    формат таймзоны /CreationDate;
  • trailer /ID: присутствует или отсутствует (Skia), равны ли половины;
  • шрифты: точный набор семейств /BaseFont (не поменял ли банк гарнитуру/начертание),
    наличие /CIDSet, /ToUnicode, /W, /FontFile2, а для ВТБ — формула subset-тега;
  • вёрстка и подписи полей: все обязательные лейблы и фразы шаблона на своих местах,
    не появились ли новые неизвестные строки-лейблы;
  • технический прогон нашего чекера (кроме дублей).

Если банк что-то обновил — возвращает подробный список отличий простым языком,
чтобы владелец сразу увидел, что поменялось, и позвал обновить генератор/чекер.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

import fitz  # type: ignore
import receipt_validator as rv  # type: ignore

BANK_TITLES = {
    "WB": "Wildberries Банк",
    "MTS": "МТС Банк",
    "VTB": "ВТБ",
    "RSHB": "Россельхозбанк",
    "OZON": "Озон Банк",
    "YANDEX": "Яндекс Банк",
}


# Эталонные паспорта шаблонов наших генераторов и доноров (без SBP ID).
_PROFILES: dict[str, dict[str, Any]] = {
    "WB": {
        "pdf_headers": {"%PDF-1.5"},
        "xref_stream": False,
        "producer_exact": {"OpenPDF 1.3.32"},
        "creator_exact": {
            "JasperReports Library version 7.0.3-41034ca841d452f3305ba55b9042260aaa1ab5dd"
        },
        "id_mode": "equal",
        "cd_tz": "Z",
        "fonts_expected": {"GoogleSans-Bold", "GoogleSans-Medium", "GoogleSans-Regular"},
        "variants": [
            {
                "name": "СБП (без сообщения / с сообщением)",
                "match": lambda t: "Вид операции" in t and "Квитанция" in t,
                "objs_range": (26, 31),
                "pages": {1},
                "width": (594.0, 596.0),
                "height": (790.0, 880.0),
                "labels": [
                    "Вид операции",
                    "Статус операции",
                    "Получатель",
                    "Телефон получателя",
                    "Банк получателя",
                    "Идентификатор операции",
                    "По вопросам зачисления обращайтесь к получателю",
                ],
            },
        ],
    },
    "MTS": {
        "pdf_headers": {"%PDF-1.5"},
        "xref_stream": True,
        "producer_exact": {"OpenPDF 1.3.30.jaspersoft.2"},
        "creator_exact": {"dbo-print-forms"},
        "id_mode": "diff",
        "cd_tz": "Z",
        "fonts_expected": {"MTSSans-Medium", "MTSSans-Regular"},
        "variants": [
            {
                "name": "Перевод по СБП",
                "match": lambda t: "Код операции СБП" in t or ("Телефон получателя" in t and "Код транзакции" in t),
                "objs_range": (31, 36),
                "pages": {1},
                "width": (374.0, 376.0),
                "height": (680.0, 760.0),
                "labels": [
                    "Получатель",
                    "Телефон получателя",
                    "Банк получателя",
                    "Счет списания",
                    "Сумма",
                    "Комиссия",
                    "Код транзакции",
                    "Итого",
                ],
            },
            {
                "name": "Перевод по карте (сайт/приложение)",
                "match": lambda t: "Карта списания" in t and ("Код транзакции" in t or "Номер перевода" in t),
                "objs_range": (28, 38),
                "pages": {1},
                "width": (374.0, 376.0),
                "height": (520.0, 720.0),
                "labels": ["Карта списания", "Сумма", "Комиссия", "Итого"],
            },
            {
                "name": "Счёт-выписка (ЭКСИ-Банк)",
                "match": lambda t: "Счет-выписка" in t,
                "objs_range": (12, 40),
                "pages": set(range(1, 20)),
                "width": (594.0, 596.0),
                "height": (840.0, 844.0),
                "fonts_allow": {"MTSSans-Bold"},
                "labels": ["Счет-выписка", "Номер счета:", "Всего поступлений:", "Всего списаний:"],
            },
        ],
    },
    "VTB": {
        "pdf_headers": {"%PDF-1.4"},
        "xref_stream": False,
        "producer_exact": {"openhtmltopdf.com"},
        "creator_exact": {""},
        "id_mode": "equal",
        "cd_tz": "+03'00'",
        "fonts_expected": {"SFProDisplay-Regular"},
        "variants": [
            {
                "name": "Исходящий перевод СБП",
                "match": lambda t: "Исходящий перевод СБП" in t,
                "objs_range": (18, 18),
                "pages": {1},
                "width": (280.5, 282.0),
                "height": (430.0, 540.0),
                "labels": [
                    "Исходящий перевод СБП",
                    "Статус",
                    "Дата операции",
                    "Счет списания",
                    "Имя плательщика",
                    "Получатель",
                    "Телефон получателя",
                    "Банк получателя",
                    "ID операции в СБП",
                    "Сумма операции",
                    "Банк ВТБ (ПАО)",
                    "Операция выполнена",
                ],
            },
            {
                "name": "Перевод на карту",
                "match": lambda t: "Перевод на карту" in t and "Карта получателя" in t,
                "objs_range": (18, 18),
                "pages": {1},
                "width": (280.5, 282.0),
                "height": (428.0, 460.0),
                "labels": [
                    "Перевод на карту",
                    "Статус",
                    "Дата операции",
                    "Номер карты",
                    "Счет списания",
                    "Имя плательщика",
                    "Карта получателя",
                    "Банк получателя",
                    "Комиссия",
                    "Сумма с комиссией",
                    "Сумма операции",
                    "Банк ВТБ (ПАО)",
                    "Операция выполнена",
                ],
            },
            {
                "name": "По номеру телефона клиенту ВТБ",
                "match": lambda t: "По номеру телефона клиенту ВТБ" in t,
                "objs_range": (18, 18),
                "pages": {1},
                "width": (280.5, 282.0),
                "height": (395.0, 470.0),
                "labels": [
                    "По номеру телефона клиенту ВТБ",
                    "Статус",
                    "Дата операции",
                    "Счет списания",
                    "Получатель",
                    "Телефон получателя",
                    "Счет получателя",
                    "Отправитель",
                    "Сумма операции",
                    "Банк ВТБ (ПАО)",
                    "Операция выполнена",
                ],
            },
        ],
    },
    "RSHB": {
        "pdf_headers": {"%PDF-1.5"},
        "xref_stream": False,
        "producer_exact": {"OpenPDF 1.3.32"},
        "creator_exact": {
            "JasperReports Library version 7.0.1-573496633c2b4074e32f433154b543003f7d2498"
        },
        "id_mode": "equal",
        "cd_tz": "Z",
        "fonts_expected": {"Onest-Regular", "Onest-SemiBold"},
        "variants": [
            {
                "name": "Перевод по номеру телефона через СБП",
                "match": lambda t: "через СБП" in t or "Идентификатор операции в СБП" in t,
                "objs_range": (29, 32),
                "pages": {1},
                "width": (374.0, 376.0),
                "height": (898.0, 902.0),
                "labels": [
                    "Перевод по номеру телефона",
                    "Счёт списания",
                    "Получатель",
                    "Номер телефона получателя",
                    "Комиссия",
                    "Банк получателя",
                    "Идентификатор операции в СБП",
                    'АО "Россельхозбанк"',
                ],
            },
        ],
    },
    "OZON": {
        "pdf_headers": {"%PDF-1.4"},
        "xref_stream": False,
        "producer_exact": {"Skia/PDF m105"},
        "creator_exact": {"Chromium"},
        "id_mode": "none",
        "cd_tz": "+00'00'",
        "fonts_expected": {"GTEestiProDisplay-Bold", "GTEestiProDisplay-Regular"},
        "variants": [
            {
                "name": "Перевод СБП",
                "match": lambda t: "Перевод СБП" in t,
                "objs_range": (82, 88),
                "pages": {1},
                "width": (373.0, 375.0),
                "height": (860.0, 910.0),
                "labels": [
                    "Перевод СБП",
                    "Итого",
                    "Статус",
                    "Счёт списания",
                    "Сумма",
                    "Комиссия",
                    "Получатель",
                    "Телефон получателя",
                    "Банк получателя",
                    "Отправитель",
                    "Служба поддержки Ozon Банка: 8 (800) 555-89-82",
                    "БИК 044525068 ИНН 9703077050",
                ],
            },
            {
                "name": "Перевод на карту (po-…)",
                "match": lambda t: bool(re.search(r"\bpo-[0-9a-fA-F-]{8,}", t)),
                "objs_range": (70, 80),
                "pages": {1},
                "width": (373.0, 375.0),
                "height": (760.0, 820.0),
                "labels": [
                    "Перевод",
                    "Итого",
                    "Статус",
                    "Сумма",
                    "Счёт списания",
                    "Номер карты получателя",
                    "Комиссия",
                    "Служба поддержки Ozon Банка: 8 (800) 555-89-82",
                ],
            },
            {
                "name": "Внутренний перевод / по реквизитам",
                "match": lambda t: "Перевод" in t and "Основной счёт" in t,
                "objs_range": (72, 86),
                "pages": {1},
                "width": (373.0, 375.0),
                "height": (740.0, 980.0),
                "labels": [
                    "Перевод",
                    "Итого",
                    "Статус",
                    "Счёт списания",
                    "Сумма",
                    "Комиссия",
                    "Служба поддержки Ozon Банка: 8 (800) 555-89-82",
                ],
            },
        ],
    },
    "YANDEX": {
        "pdf_headers": {"%PDF-1.5"},
        "xref_stream": False,
        "producer_exact": {"OpenPDF 1.3.32"},
        "creator_exact": {
            "JasperReports Library version 6.21.2-8434a0bd7c3bbc37cbf916f2968d35e4b165821a"
        },
        "id_mode": "equal",
        "cd_tz": "Z",
        "fonts_expected": {"YSLogotype-Regular", "YSText-Medium", "YSText-Regular"},
        "variants": [
            {
                "name": "Квитанция СБП",
                "match": lambda t: "Квитанция" in t and "Исх." in t,
                "objs_range": (26, 31),
                "pages": {1},
                "width": (594.0, 596.0),
                "height": (840.0, 844.0),
                "labels": [
                    "Квитанция",
                    "Дата и время операции МСК",
                    "Тип операции",
                    "Статус",
                    "Банк отправителя",
                    "Телефон отправителя",
                    "ФИО отправителя",
                    "Телефон получателя",
                    "ФИО получателя",
                    "Банк получателя",
                    "Сумма операции",
                    "Комиссия",
                    "Код авторизации",
                    "Идентификатор операции в СБП",
                    "АО «Яндекс Банк»",
                ],
            },
            {
                "name": "Выписка по договору",
                "match": lambda t: "Выписка по договору" in t,
                "objs_range": (15, 120),
                "pages": set(range(1, 30)),
                "width": (594.0, 596.0),
                "height": (840.0, 844.0),
                "labels": ["Выписка по договору", "Яндекс Банк"],
            },
        ],
    },
}

# Проверки SBP ID намеренно игнорируются в режиме «проверка шаблона на актуальность»
# (по ТЗ: смотреть структуру, метаданные, шрифты и вёрстку, кроме SBP ID).
_IGNORE_CHECKS = {
    "opid_present",
    "opid_format",
    "opid_era",
    "opid_flag",
    "opid_node",
    "opid_gateway",
    "opid_tail",
    "opid_display_shift",
    "opid_display_time",
    "current_profile_consistency",
    "creation_not_future",
    "creation_lag",
}


@dataclass
class ActualityReport:
    bank: str | None
    bank_title: str
    variant: str | None
    up_to_date: bool
    diffs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _guess_bank_by_text(text: str) -> str | None:
    if "Банк ВТБ (ПАО)" in text:
        return "VTB"
    if "Ozon Банка" in text or "ОЗОН БАНК" in text:
        return "OZON"
    if "Яндекс Банк" in text:
        return "YANDEX"
    if "Россельхозбанк" in text:
        return "RSHB"
    if "Квитанция №" in text or "Вайлдберриз" in text:
        return "WB"
    if "Код транзакции" in text or "www.exibank.ru" in text or "МТС Банк" in text:
        return "MTS"
    return None


def check_actuality(pdf_bytes: bytes) -> ActualityReport:
    """Сверить свежий оригинальный PDF с эталоном генератора/чекера (кроме SBP ID)."""
    raw = bytes(pdf_bytes)
    if not raw.startswith(b"%PDF-"):
        return ActualityReport(None, "не PDF", None, False, ["Файл не начинается с %PDF-."])

    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            pages = len(doc)
            rect = tuple(round(float(x), 2) for x in doc[0].rect) if pages else (0, 0, 0, 0)
            text = "".join(p.get_text() for p in doc)
            repaired = bool(doc.is_repaired)
    except Exception as exc:
        return ActualityReport(None, "ошибка PDF", None, False, [f"PyMuPDF не открыл файл: {exc}"])

    det_bank, prod, crea, _id_present, _bases = rv._detect_bank(raw)
    bank = det_bank or _guess_bank_by_text(text)
    if not bank or bank not in _PROFILES:
        info = rv._info_dict(raw)
        return ActualityReport(
            None,
            "банк не распознан",
            None,
            False,
            [
                "Не удалось сопоставить чек ни с одним из 6 банков (WB/МТС/ВТБ/РСХБ/Озон/Яндекс).",
                f"Producer: {info.get('/Producer')!r}, Creator: {info.get('/Creator')!r}",
            ],
        )

    prof = _PROFILES[bank]
    title = BANK_TITLES[bank]
    diffs: list[str] = []
    notes: list[str] = []

    if det_bank is None:
        diffs.append(
            f"Банк угадан только по тексту ({title}), но по Producer/Creator/шрифтам "
            f"детектор его ПЕРЕСТАЛ узнавать (Producer={prod!r}, Creator={crea!r})!"
        )

    # 1. Заголовок PDF и тип xref
    hdr = raw.splitlines()[0].decode("latin-1", "replace").strip()
    if hdr not in prof["pdf_headers"]:
        diffs.append(f"Версия заголовка PDF изменилась: {hdr!r} (у нас {sorted(prof['pdf_headers'])})")

    has_xref_stm = rv._startxref_obj(raw) > 0
    if has_xref_stm != prof["xref_stream"]:
        diffs.append(
            f"Тип таблицы объектов изменился: XRef-stream={has_xref_stm} "
            f"(у нас {prof['xref_stream']})"
        )

    if repaired:
        diffs.append("MuPDF чинил таблицу xref при открытии (неожиданная структура PDF)")

    # 2. /Producer, /Creator, /CreationDate
    info = rv._info_dict(raw)
    prod_val = info.get("/Producer") or ""
    crea_val = info.get("/Creator") or ""
    if prod_val not in prof["producer_exact"]:
        diffs.append(
            f"Изменился /Producer (движок PDF банка): {prod_val!r} "
            f"(у нас {sorted(prof['producer_exact'])})"
        )
    if crea_val not in prof["creator_exact"]:
        diffs.append(
            f"Изменился /Creator (версия шаблонизатора банка): {crea_val!r} "
            f"(у нас {sorted(prof['creator_exact'])})"
        )

    cd_raw = info.get("/CreationDate") or ""
    if cd_raw:
        if prof["cd_tz"] == "Z" and not cd_raw.endswith("Z"):
            diffs.append(f"Формат таймзоны /CreationDate изменился: {cd_raw!r} (ожидали '...Z')")
        elif prof["cd_tz"] != "Z" and not cd_raw.endswith(prof["cd_tz"]):
            notes.append(f"Необычная таймзона /CreationDate: {cd_raw!r} (обычно {prof['cd_tz']!r})")
    else:
        diffs.append("В /Info пропало поле /CreationDate")

    # 3. Trailer /ID
    tid = rv._trailer_id(raw)
    actual_id_mode = "none" if tid is None else ("equal" if tid[0] == tid[1] else "diff")
    if actual_id_mode != prof["id_mode"]:
        diffs.append(
            f"Поведение trailer /ID изменилось: сейчас {actual_id_mode!r}, у нас {prof['id_mode']!r}"
        )

    # 4–5. Вариант шаблона, шрифты, объекты, геометрия и лейблы
    variant = next((v for v in prof["variants"] if v["match"](text)), None)
    allowed_fonts = set(prof["fonts_expected"]) | set((variant or {}).get("fonts_allow") or ())

    hay = raw + rv._all_decompressed(raw)
    found_fonts = {
        re.sub(r"^[A-Z]{6}\+", "", m.decode("latin-1", "replace"))
        for m in re.findall(rb"/BaseFont\s*/([^\s/<>\[\]()]+)", hay)
    }
    found_custom = {f for f in found_fonts if f != "Helvetica"}
    missing_fonts = prof["fonts_expected"] - found_custom
    extra_fonts = found_custom - allowed_fonts
    if missing_fonts and not (bank == "YANDEX" and "Выписка по договору" in text):
        notes.append(f"Не задействованы начертания: {sorted(missing_fonts)} (может зависеть от текста)")
    if extra_fonts:
        diffs.append(f"Появились новые шрифты банка: {sorted(extra_fonts)} (у нас {sorted(allowed_fonts)})")

    n_objs = len(rv._obj_headers(raw))
    w_pt, h_pt = rect[2] - rect[0], rect[3] - rect[1]
    if variant is None:
        first_lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
        diffs.append(
            f"Неизвестный тип/шаблон документа {title} (первые строки: {first_lines})"
        )
        variant_name = None
    else:
        variant_name = variant["name"]
        if pages not in variant["pages"]:
            diffs.append(f"Неожиданное число страниц: {pages} (ожидали {sorted(variant['pages'])})")
        if not (variant["width"][0] <= w_pt <= variant["width"][1]):
            diffs.append(f"Ширина страницы изменилась: {w_pt} pt (у нас {variant['width']} pt)")
        if not (variant["height"][0] <= h_pt <= variant["height"][1]):
            diffs.append(f"Высота страницы вышла за пределы шаблона: {h_pt} pt (у нас {variant['height']} pt)")
        lo_obj, hi_obj = variant["objs_range"]
        if not (lo_obj <= n_objs <= hi_obj):
            diffs.append(
                f"Количество PDF-объектов изменилось: {n_objs} (у нашего шаблона {lo_obj}..{hi_obj})"
            )
        missing_labels = [lbl for lbl in variant["labels"] if lbl not in text]
        if missing_labels:
            diffs.append(f"В тексте чека пропали/переименованы поля: {missing_labels}")

    # 6. Не упали ли структурные/шрифтовые проверки нашего чекера (кроме SBP ID)
    try:
        from checker_engine import audit
        aud = audit(raw)
        non_sbp_fails = [c for c in aud.hard_failed if c["name"] not in _IGNORE_CHECKS]
        for fail in non_sbp_fails:
            diffs.append(f"Проверка чекера `{fail['name']}` не прошла: {fail['detail']}")
    except Exception:
        val = rv.validate(raw)
        for fail in val.get("checks", []):
            if not fail["passed"] and fail["name"] not in _IGNORE_CHECKS:
                diffs.append(f"Проверка валидатора `{fail['name']}` не прошла: {fail['detail']}")

    notes.append(
        f"Параметры PDF: {hdr}, объектов {n_objs}, стр. {pages} ({w_pt}×{h_pt} pt), "
        f"шрифты {sorted(found_custom) or sorted(found_fonts)}"
    )
    return ActualityReport(
        bank=bank,
        bank_title=title,
        variant=variant_name,
        up_to_date=len(diffs) == 0,
        diffs=diffs,
        notes=notes,
    )


def render_report_html(rep: ActualityReport) -> str:
    """Готовое HTML-сообщение для админа в Telegram."""
    esc = lambda s: html.escape(str(s), quote=False)
    v_str = f" · {esc(rep.variant)}" if rep.variant else ""
    if rep.up_to_date:
        lines = [
            f"✅ <b>Шаблон актуален — {esc(rep.bank_title)}{v_str}</b>",
            "",
            "Структура PDF, версии Producer/Creator, шрифты, размер страницы и все поля "
            "полностью совпадают с нашим шаблоном (SBP ID не учитывался).",
        ]
    else:
        lines = [
            f"⚠️ <b>Обнаружены отличия от нашего шаблона!</b>",
            f"🏦 Банк: <b>{esc(rep.bank_title)}</b>{v_str}",
            "",
            "<b>Что изменилось у банка:</b>",
        ]
        for d in rep.diffs:
            lines.append(f"• {esc(d)}")
        lines += [
            "",
            "👉 Пришли этот чек в чат с разработкой — обновим генератор и чекер под свежий формат банка.",
        ]
    if rep.notes:
        lines.append("")
        for n in rep.notes:
            lines.append(f"<i>ℹ️ {esc(n)}</i>")
    return "\n".join(lines)
