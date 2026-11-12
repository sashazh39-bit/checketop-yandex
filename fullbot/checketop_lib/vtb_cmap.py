#!/usr/bin/env python3
"""CMap ВТБ: кириллица и цифры → CID hex (4 символа).

Используется для кодирования произвольного текста в content stream.
Источник: 09-03-26_03-47.pdf, ToUnicode.
"""
import unicodedata

# Цифры и символы (date, amount, номер счета *9483)
_CID_DIGIT = {
    "0": "0013", "1": "0014", "2": "0015", "3": "0016", "4": "0017",
    "5": "0018", "6": "0019", "7": "001A", "8": "001B", "9": "001C",
    ".": "0011", ",": "000F", " ": "0003", ":": "001D",
    "(": "000B", ")": "000C", "+": "000E", "-": "0010", "*": "000D",  # CID 0x000D — фактический CID звёздочки в шрифте ВТБ (НЕ 0x002A)
}
# Рубль (в amount)
_CID_RUBLE = "0440"  # ₽

# Кириллица (заглавные и строчные) — ВТБ font subset
_CID_CYRILLIC = {
    # === Заглавные: CID должны совпадать с bfrange из ToUnicode шаблона 15-03-26 ===
    # CID в bfrange → Unicode (проверено побайтово)
    "А": "021C",  # bfrange: 021C→А
    "Б": "021D",  # bfrange: 021D→Б
    "В": "021E",  # bfrange: 021E→В (было 023E=в — ИСПРАВЛЕНО)
    "Г": "023F",  # НЕТ в bfrange для Г(uppercase), CID 023F=г(lowercase) в bfrange
    "Д": "0220",  # bfrange: 0220→Д (было 0240=д — ИСПРАВЛЕНО)
    "Е": "0221",  # bfrange: 0221→Е
    "Ж": "0222",  # bfrange: 0222→Ж
    "З": "0243",  # НЕТ в bfrange для З, CID без bfrange-маппинга
    "И": "0224",  # bfrange: 0224→И
    "Й": "0225",  # НЕТ в bfrange для Й, CID без bfrange-маппинга
    "К": "0226",  # НЕТ в bfrange для К, GID=0 → нужна инъекция
    "Л": "0227",  # НЕТ в bfrange, GID=0 → инъекция
    "М": "0228",  # НЕТ в bfrange, GID=0 → инъекция
    "Н": "0229",  # НЕТ в bfrange, GID=0 → инъекция
    "О": "022A",  # bfrange: 022A→О (было 024A=о — ИСПРАВЛЕНО)
    "П": "022B",  # bfrange: 022B→П
    "Р": "022C",  # НЕТ в bfrange, GID=0 → инъекция
    "С": "022D",  # bfrange: 022D→С
    "Т": "022E",  # bfrange: 022E→Т
    "У": "024F",  # НЕТ в bfrange для У(uppercase), CID 024F=у(lowercase)
    "Ф": "0230",  # НЕТ в bfrange, GID=0 → инъекция
    "Х": "0251",  # НЕТ в bfrange для Х(uppercase), CID 0251=х(lowercase)
    "Ц": "0252",  # НЕТ в bfrange для Ц(uppercase), CID 0252=ц(lowercase)
    "Ч": "0253",  # НЕТ в bfrange для Ч(uppercase), CID 0253=ч(lowercase)
    "Ш": "0254",  # НЕТ в bfrange, CID без bfrange-маппинга
    "Щ": "0255",  # НЕТ в bfrange для Щ(uppercase), CID 0255=щ(lowercase)
    "Ъ": "0256",  # НЕТ в bfrange, CID без bfrange-маппинга
    "Ы": "0257",  # НЕТ в bfrange для Ы(uppercase), CID 0257=ы(lowercase)
    "Ь": "0258",  # НЕТ в bfrange для Ь(uppercase), CID 0258=ь(lowercase)
    "Э": "0242",  # НЕТ в bfrange, CID без bfrange-маппинга
    "Ю": "023A",  # НЕТ в bfrange, CID без bfrange-маппинга
    "Я": "023B",  # НЕТ в bfrange, GID=0 → инъекция
    # === Строчные: CID совпадают с bfrange ===
    "а": "023C",  # bfrange: 023C→а
    "б": "021D",  # НЕТ отдельного CID для б; bfrange: 021D→Б (используем Б-глиф)
    "в": "023E",  # bfrange: 023E→в
    "г": "023F",  # bfrange: 023F→г
    "д": "0240",  # bfrange: 0240→д
    "е": "0241",  # bfrange: 0241→е
    "ж": "0223",  # НЕТ в bfrange, CID без маппинга
    "з": "0243",  # НЕТ в bfrange, CID без маппинга
    "и": "0244",  # bfrange: 0244→и
    "й": "0245",  # bfrange: 0245→й
    "к": "0246",  # bfrange: 0246→к
    "л": "0247",  # bfrange: 0247→л
    "м": "0248",  # bfrange: 0248→м
    "н": "0249",  # bfrange: 0249→н
    "о": "024A",  # bfrange: 024A→о
    "п": "024B",  # bfrange: 024B→п (было 022B=П — ИСПРАВЛЕНО)
    "р": "024C",  # bfrange: 024C→р
    "с": "024D",  # bfrange: 024D→с
    "т": "024E",  # bfrange: 024E→т
    "у": "024F",  # bfrange: 024F→у
    "ф": "0250",  # bfrange: 0250→ф
    "х": "0251",  # bfrange: 0251→х
    "ц": "0252",  # bfrange: 0252→ц
    "ч": "0253",  # bfrange: 0253→ч
    "ш": "0254",  # НЕТ в bfrange, CID без маппинга
    "щ": "0255",  # bfrange: 0255→щ
    "ъ": "0256",  # НЕТ в bfrange, CID без маппинга
    "ы": "0257",  # bfrange: 0257→ы
    "ь": "0258",  # bfrange: 0258→ь
    "э": "0242",  # НЕТ в bfrange, CID без маппинга
    "ю": "023A",  # НЕТ в bfrange, CID без маппинга
    "я": "025B",  # bfrange: 025B→я
}
# Некоторые символы могут отсутствовать — homoglyph (замена при вводе)
_FALLBACK = {"ё": "е", "Ё": "Е", "‑": "-"}  # U+2011 non‑breaking hyphen
FALLBACK_TIPS = dict(_FALLBACK)  # публично для подсказок в боте

# Единственный источник истины: символы, которые шрифт ВТБ поддерживает напрямую
_SUPPORTED_CHARS = frozenset(_CID_DIGIT) | frozenset(_CID_CYRILLIC) | {"₽"}

# Латинские буквы (частые при копировании) — шрифт не поддерживает
_LATIN_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_LATIN_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_LATIN_LETTERS = _LATIN_UPPER | _LATIN_LOWER

# Другие частые неподдерживаемые символы
_OTHER_UNSUPPORTED = {
    "\u00a0",   # no-break space
    "\u2010",   # hyphen
    "\u2011",   # non-breaking hyphen (same as ‑ in _FALLBACK)
    "\u2012",   # figure dash
    "\u2013",   # en dash –
    "\u2014",   # em dash —
    "\u2015",   # horizontal bar
    "\u2212",   # minus −
    "\u200b",   # zero-width space
    "\u200c",   # zero-width non-joiner
    "\u200d",   # zero-width joiner
    "\ufeff",   # BOM / zero-width no-break
}


def get_unsupported_chars(text: str) -> list[str]:
    """Вернуть список неподдерживаемых символов (порядок первого появления).
    Пустой список = всё ок. ё/Ё/‑ — нужна замена на е/Е/-."""
    if not isinstance(text, str):
        return []
    text = unicodedata.normalize("NFC", text)
    seen: set[str] = set()
    bad: list[str] = []
    for c in text:
        if c in seen:
            continue
        if c in _SUPPORTED_CHARS:
            continue
        if c in _FALLBACK:
            seen.add(c)
            bad.append(c)
            continue
        if c in _LATIN_LETTERS:
            seen.add(c)
            bad.append(c)
            continue
        if c in _OTHER_UNSUPPORTED:
            seen.add(c)
            bad.append(c)
            continue
        # Любой другой символ — неподдерживаемый
        seen.add(c)
        bad.append(c)
    return bad


def is_text_supported(text: str) -> bool:
    """True, если весь текст можно закодировать (с учётом FALLBACK-замены в text_to_cids)."""
    return len(get_unsupported_chars(text)) == 0


def format_unsupported_error(unsupported: list[str], field_name: str = "") -> str:
    """Сообщение об ошибке с подсказкой замены."""
    if not unsupported:
        return ""
    pre = f"В поле «{field_name}» " if field_name else ""
    chars = ", ".join(f"«{c}»" for c in unsupported)
    tips = []
    has_latin = False
    others = []
    for c in unsupported:
        if c in _FALLBACK:
            tips.append(f"«{c}» → «{_FALLBACK[c]}»")
        elif c in _LATIN_LETTERS:
            has_latin = True
        else:
            others.append(c)
    parts = []
    if tips:
        parts.append(f"Используйте замену: {'; '.join(tips)}")
    if has_latin:
        parts.append("Латинские буквы (a-z, A-Z) не поддерживаются — введите кириллицу.")
    if others:
        parts.append(f"Символы {', '.join(repr(c) for c in others)} не поддерживаются.")
    return f"❌ {pre}найдены неподдерживаемые буквы: {chars}.\n" + " ".join(parts)


def suggest_replacement(text: str) -> str | None:
    """Вернуть вариант текста с автозаменой ё→е, Ё→Е, ‑→-. None если есть другие неподдерживаемые символы."""
    result = []
    for c in text:
        if c in _FALLBACK:
            result.append(_FALLBACK[c])
        elif c in _CID_DIGIT or c in _CID_CYRILLIC or c == "₽":
            result.append(c)
        else:
            return None
    return "".join(result)


# Latin letters для ID операции — используем НАСТОЯЩИЕ Latin CIDs из bfrange,
# чтобы text extraction давала Latin Unicode (как в оригинальных чеках ВТБ).
# Доступные Latin в bfrange: B(0x0025), D(0x0027), I(0x002C), K(0x002E)
_CID_OPID = {
    "B": "0025",  # Latin B (U+0042) — bfrange CID
    "D": "0027",  # Latin D (U+0044) — bfrange CID
    "G": "002A",  # Latin G (U+0047) — bfrange CID (17-03 шаблон)
    "I": "002C",  # Latin I (U+0049) — bfrange CID
    "K": "002E",  # Latin K (U+004B) — bfrange CID (15-03 шаблон)
    "A": "021C",  # А кириллическая (нет Latin A в bfrange)
    "C": "022D",  # С кириллическая (нет Latin C в bfrange)
    "T": "022E",  # Т кириллическая (нет Latin T в bfrange)
    "R": "024C",  # р строчная (нет Latin R в bfrange)
    "P": "024C",  # р строчная (нет Latin P)
    "H": "0249",  # н строчная (нет Latin H)
}

# Маппинг банков → буква для позиции bank_letter в Operation ID
# Приоритет: Latin CIDs из bfrange (B, D, I, K) — text extraction корректна
_BANK_LETTER_MAP = {
    "Т-Банк": "K", "Тинькофф": "K", "Tinkoff": "K",
    "Сбербанк": "K", "Сбер": "K",
    "Альфа-Банк": "K", "Альфа": "K", "АльфаБанк": "K",
    "ВТБ": "B", "Банк ВТБ": "B",
    "Райффайзен": "K", "Райффайзенбанк": "K",
}


def operation_id_to_cids(text: str) -> list[str] | None:
    """ID операции (B606...) → CID. Поддерживает 0-9, A-F (hex)."""
    result = []
    for c in text.upper():
        cid = _CID_DIGIT.get(c) or _CID_OPID.get(c)
        if cid is None:
            return None
        result.append(cid)
    return result


def text_to_cids(text: str) -> list[str] | None:
    """Текст → список CID hex. None если символ не найден."""
    result = []
    for c in text:
        c = _FALLBACK.get(c, c)
        cid = _CID_DIGIT.get(c) or _CID_CYRILLIC.get(c)
        if c == "₽":
            cid = _CID_RUBLE
        if cid is None:
            return None
        result.append(cid)
    return result


def format_amount(amount: int) -> str:
    """10000 → '10 000 ₽'."""
    parts = []
    s = str(amount)
    for i, c in enumerate(reversed(s)):
        if i and i % 3 == 0:
            parts.append(" ")
        parts.append(c)
    return "".join(reversed(parts)) + " ₽"


def gen_sbp_operation_id(
    op_date: "date | None" = None,
    op_time_moscow: str = "",
    direction: str = "B",
    bank_code: str = "60",
    recipient_bank: str = "",
    available_latin: set[str] | None = None,
) -> str:
    """Сгенерировать SBP Operation ID по структуре реальных ID ВТБ.

    Формат (из анализа 30+ реальных ID, все 32 символа):
      A606711136304180K0000080011700501 — НЕВЕРНО (примеры ниже)

      direction(1) + bank(2) + julian(2) + hhmm(4)  ← prefix, 9 символов
      + routing_digits(6)                            ← случайные 6 цифр
      + bank_letter(1)                               ← код банка-получателя (цифра или буква)
      + "000NNN001"(9)                               ← счётчик (NNN = 001..017)
      + "1700501"(7)                                 ← постоянный суффикс ВТБ
      = 9 + 6 + 1 + 9 + 7 = 32 символа

    Время в ID = московское время − 2ч (UTC+1 / Калининград)
    Дата (julian) = день года по часовому поясу UTC+1

    Аргументы:
      op_date         — дата операции (Moscow, default: сегодня)
      op_time_moscow  — время по Москве "HH:MM" (default: текущее московское)
      direction       — 'A' (исходящий) или 'B' (входящий)
      bank_code       — код банка-отправителя (60 = ВТБ)
      recipient_bank  — название банка-получателя (Т-Банк, Сбербанк и т.д.)
    """
    import random
    from datetime import date as _date, datetime as _datetime, timezone as _tz, timedelta as _td

    # Московское время → UTC+1 (Калининград, −2ч от Москвы)
    if op_date is None:
        now_msk = _datetime.now(_tz.utc) + _td(hours=3)
        op_date = now_msk.date()
        if not op_time_moscow:
            op_time_moscow = now_msk.strftime("%H:%M")

    if not op_time_moscow:
        now_msk = _datetime.now(_tz.utc) + _td(hours=3)
        op_time_moscow = now_msk.strftime("%H:%M")

    # Конвертируем московское время в UTC+1 (Калининград = MSK−2)
    msk_h, msk_m = int(op_time_moscow[:2]), int(op_time_moscow[3:5])
    kal_total = msk_h * 60 + msk_m - 120  # −2 часа
    # Учитываем переход через полночь
    kal_date = op_date
    if kal_total < 0:
        kal_total += 1440
        kal_date = op_date - _td(days=1)
    elif kal_total >= 1440:
        kal_total -= 1440
        kal_date = op_date + _td(days=1)
    kal_h, kal_m = divmod(kal_total, 60)

    julian = kal_date.timetuple().tm_yday   # 1-366, 1-indexed
    hhmm = f"{kal_h:02d}{kal_m:02d}"        # "HHMM" в UTC+1

    # Буква банка-получателя (или "0" если неизвестен)
    bank_letter = "0"
    for name, letter in _BANK_LETTER_MAP.items():
        if name.lower() in recipient_bank.lower() or recipient_bank.lower() in name.lower():
            bank_letter = letter
            break

    # Если bank_letter не существует в bfrange шаблона — заменить на доступную
    if available_latin and bank_letter != "0" and bank_letter not in available_latin:
        for fallback in ("G", "K", "B", "D", "I"):
            if fallback in available_latin:
                bank_letter = fallback
                break

    routing = "".join(random.choices("0123456789", k=6))          # 6 случайных цифр
    # Суффикс из анализа 10+ реальных VTB op_id:
    # K@15 стиль: "0B10XX001" (XX = 10-19)
    # Пример: K + 0B1014001, K + 0B1018001, N + 0B1016001
    xx = random.randint(10, 19)
    counter = f"0B10{xx:02d}001"                                   # "0B10XXXXXX001" — точный VTB формат

    prefix = f"{direction}{bank_code}{julian:02d}{hhmm}"           # 9 символов
    # Итого 25 символов — "1700501" уже есть в PDF как отдельная строка, не добавляем
    return prefix + routing + bank_letter + counter                 # 9+6+1+9=25
