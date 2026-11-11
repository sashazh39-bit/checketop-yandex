#!/usr/bin/env python3
"""Telegram-бот checketop — Яндекс, Рокет, МТС, ВТБ, ВБ Банк, Альфа.

Бот ведёт пошаговый диалог, собирает данные перевода/выписки и вызывает
готовые движки. Логики работы с PDF в боте нет: бот только валидирует
ввод, вызывает ``generate_*`` и отправляет пользователю полученный файл.

Запуск (токен — только из окружения, см. PROJECT.md)::

    BOT_TOKEN=... python3 bot/yandex_bot.py

Контракт движка (бот импортирует его лениво, в момент генерации, поэтому
бот стартует и ведёт диалог даже без движка; без него шаг генерации
честно завершится ошибкой, а traceback уйдёт в ``logs/bot_errors.log``)::

    generate_yandex(
        amount="10 000,00 ₽",                  # канонический вид, см. parse_amount
        operation_dt=datetime(..., tzinfo=MSK),  # дата/время операции, МСК
        sender_name="Иванов Иван Иванович",
        sender_phone="+7 900 351-70-80",       # нормализованный, см. parse_phone
        recipient_name="Петрова Анна Ивановна",
        recipient_phone="+7 963 297-20-08",
        recipient_bank="Сбербанк",
        output_path="/path/to/out.pdf",        # куда движок пишет PDF
        donor_path="/tmp/.../template.pdf",    # только в режиме «с шаблоном»
    ) -> str | Path | bytes | None

У Яндекса два режима создания чека (шаг «Как создать чек?» после выбора
банка): «без PDF» — движок сам берёт донора из пула
``banks/yandex/donors/``; «с PDF» — пользователь присылает
PDF настоящей квитанции, бот проверяет её парсером движка
(``yandex_receipt.Receipt``: валидная квитанция, есть СБП-ID, сообщение
«—»), сохраняет во временную папку и передаёт движку как ``donor_path``.
Шаблон одноразовый: после генерации, отмены или по таймауту временный
файл удаляется, в постоянный пул он не попадает.

Возврат движка: путь к готовому PDF (``str``/``Path``), ``bytes`` PDF
или ``None``, если файл уже записан в ``output_path``.

Состояния диалога хранятся в ``context.user_data`` python-telegram-bot:
это хранилище с привязкой к конкретному пользователю (ConversationHandler
по умолчанию ведёт отдельные диалоги per-chat + per-user). Глобальных
переменных с состоянием диалога в боте нет.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import inspect
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

logger = logging.getLogger("yandex_bot")

# Бот отвечает только этим Telegram user id. Другие апдейты отсекаются
# до диалога. Переопределяется переменной ALLOWED_USER_IDS=id1,id2.
ALLOWED_USER_IDS = {1445265832}


def allowed_user_ids() -> set:
    raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set(ALLOWED_USER_IDS)
    return {int(part) for part in raw.split(",") if part.strip()}


def is_allowed_user(user_id: Optional[int]) -> bool:
    if user_id in allowed_user_ids():
        return True
    try:
        import admin_access
        return admin_access.has_access(user_id)
    except Exception:
        return False


async def restrict_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропустить только разрешённые id. Остальным — полная тишина:
    ни сообщения, ни алерта (только запись в серверный лог)."""
    user = update.effective_user
    if is_allowed_user(getattr(user, "id", None)):
        return
    query = update.callback_query
    if query is not None:
        # Пустой answer — не сообщение, а только снять «часики» у кнопки.
        try:
            await query.answer()
        except Exception:
            pass
    logger.info(
        "Silently ignored update from user_id=%s",
        getattr(user, "id", None),
    )
    raise ApplicationHandlerStop

MSK = timezone(timedelta(hours=3), name="MSK")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE_PATH = REPO_ROOT / "banks" / "yandex" / "engine.py"

# --- состояния диалога (ConversationHandler) -------------------------------
BANK_SELECT, AMOUNT, DATETIME, SENDER_NAME, SENDER_PHONE = range(5)
RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_BANK, CONFIRM, EDIT_SELECT = range(5, 10)

CANCEL_CB = "cancel"
YANDEX_CB = "bank:yandex"
NOW_CB = "dt:now"
CONFIRM_YES_CB = "confirm:yes"
CONFIRM_EDIT_CB = "confirm:edit"

# Дополнительные состояния Яндекс-сценария (паритет с Рокетом: банк
# кнопками и отдельный шаг «Время операции»).
Y_RECIPIENT_BANK_CUSTOM, Y_TIME_CHOICE = range(44, 46)
Y_MEM_RECIPIENT_CB = "ymem:recipient"
Y_MEM_SENDER_CB = "ymem:sender"

# Режимы создания чека Яндекса: выбор режима, загрузка шаблона,
# предупреждение о шаблоне (потраченный токен / чужая эпоха).
Y_MODE, Y_TEMPLATE_UPLOAD, Y_TEMPLATE_WARN = range(46, 49)
Y_MODE_NORMAL_CB = "ymode:normal"
Y_MODE_TEMPLATE_CB = "ymode:template"
Y_TEMPLATE_PROCEED_CB = "ytpl:proceed"

# [Яндекс] -> [Чек] / [Выписка]
Y_MENU = 49
Y_DOC_CHECK_CB = "ydoc:check"
Y_DOC_STMT_CB = "ydoc:stmt"

# --- состояния выписки Яндекса (banks/yandex/statement.py) ----------------
(
    YS_PERIOD,
    YS_ISSUE_CHOICE,
    YS_ISSUE_INPUT,
    YS_HOLDER_NAME,
    YS_HOLDER_BIRTH,
    YS_HOLDER_PASSPORT,
    YS_CONTRACT_DATE_CHOICE,
    YS_CONTRACT_DATE_INPUT,
    YS_CONTRACT_NUMBER_CHOICE,
    YS_CONTRACT_NUMBER_INPUT,
    YS_OPENING,
    YS_OPS_EDITOR,
    YS_OP_TYPE,
    YS_OP_NAME,
    YS_OP_PHONE,
    YS_OP_BANK,
    YS_OP_BANK_CUSTOM,
    YS_OP_CUSTOM_SIGN,
    YS_OP_CUSTOM_DESC,
    YS_OP_DT_CHOICE,
    YS_OP_DT_INPUT,
    YS_OP_AMOUNT,
    YS_OP_DELETE,
    YS_SUMMARY,
    YS_EDIT_SELECT,
) = range(50, 75)
(
    M_RECIPIENT_NAME,
    M_RECIPIENT_PHONE,
    M_RECIPIENT_BANK,
    M_RECIPIENT_BANK_CUSTOM,
    M_AMOUNT,
    M_ACCOUNT_CHOICE,
    M_ACCOUNT_INPUT,
    M_MESSAGE_CHOICE,
    M_MESSAGE_INPUT,
    M_TIME_CHOICE,
    M_TIME_INPUT,
    M_SUMMARY,
    M_EDIT_SELECT,
) = range(75, 88)
M_MENU = 88
(
    MC_RECIPIENT_CARD,
    MC_AMOUNT,
    MC_SENDER_CARD_CHOICE,
    MC_SENDER_CARD_INPUT,
    MC_ACCOUNT_CHOICE,
    MC_ACCOUNT_INPUT,
    MC_TIME_CHOICE,
    MC_TIME_INPUT,
    MC_SUMMARY,
    MC_EDIT_SELECT,
) = range(89, 99)
V_MENU = 99
(
    V_RECIPIENT_NAME,
    V_RECIPIENT_PHONE,
    V_RECIPIENT_BANK,
    V_RECIPIENT_BANK_CUSTOM,
    V_RECIPIENT_CARD,
    V_AMOUNT,
    V_SENDER_NAME,
    V_SENDER_PHONE,
    V_SENDER_ACCOUNT_CHOICE,
    V_SENDER_ACCOUNT_INPUT,
    V_PAYER_CARD_CHOICE,
    V_PAYER_CARD_INPUT,
    V_MESSAGE_CHOICE,
    V_MESSAGE_INPUT,
    V_TIME_CHOICE,
    V_TIME_INPUT,
    V_SUMMARY,
    V_EDIT_SELECT,
) = range(100, 118)
W_MENU = 118
(
    W_RECIPIENT_NAME,
    W_RECIPIENT_PHONE,
    W_RECIPIENT_BANK,
    W_RECIPIENT_BANK_CUSTOM,
    W_AMOUNT,
    W_TIME_CHOICE,
    W_TIME_INPUT,
    W_SUMMARY,
    W_EDIT_SELECT,
) = range(119, 128)
S_CARD = 128
(
    S_OPS_EDITOR,
    S_OP_KIND,
    S_OP_AMOUNT,
    S_OP_DT_CHOICE,
    S_OP_DT_INPUT,
    S_OP_DETAIL,
    S_OP_DELETE,
) = range(129, 136)
A_UPLOAD, A_AMOUNT = range(140, 142)
VU_UPLOAD, VU_AMOUNT, VU_ACCOUNT, VU_PAYER, VU_RECIPIENT = range(142, 147)
M_MEM_RECIPIENT_CB = "mmem:recipient"
M_GENERATE_CB = "mgen:generate"
M_EDIT_CB = "mgen:edit"
MTS_BANK_OPTIONS = (
    ("sber", "Сбербанк"),
    ("tbank", "Т-Банк"),
    ("vtb", "ВТБ"),
    ("alfa", "Альфа-Банк"),
    ("psb", "ПСБ"),
    ("ozon", "Озон Банк (Ozon)"),
    ("otp", "ОТП Банк"),
    ("sovcom", "Совкомбанк"),
    ("rshb", "Россельхозбанк"),
    ("yandex", "Яндекс"),
)
VTB_BANK_OPTIONS = (
    ("sber", "Сбербанк"),
    ("tbank", "Т-Банк"),
    ("alfa", "Альфа-Банк"),
    ("psb", "ПСБ"),
    ("wb", "Wildberries (Вайлдберриз Банк)"),
    ("rocket", "Рокетбанк"),
    ("ozon", "Озон Банк"),
    ("otp", "ОТП Банк"),
)
WB_BANK_OPTIONS = (
    ("sber", "Сбербанк"),
    ("tbank", "Т-Банк"),
    ("vtb", "ВТБ"),
    ("alfa", "Альфа-Банк"),
    ("psb", "ПСБ"),
    ("rocket", "Рокетбанк"),
)
STMT_ALLOWED_COUNTS = {
    "rocket": tuple(range(0, 11)),
    "mts": (0, 2, 3, 5, 20),
    "vtb": (0, 5, 15),
}
STMT_KIND_LABELS = {
    "sbp_out": "Исходящий СБП",
    "sbp_in": "Входящий СБП",
    "card": "Перевод с карты",
    "topup": "Пополнение",
    "posted": "Операция",
    "internal": "Приход",
    "expense": "Расход",
}
YS_GENERATE_CB = "ysgen:generate"
YS_EDIT_CB = "ysgen:edit"
YS_MEM_HOLDER_CB = "ysmem:holder"

# Актуальная эпоха СБП-ID (хвост, поз. 23-32) на ноябрь 2026 — та же
# константа, что в движке/патчере (banks/yandex/BANK.md).
YANDEX_SBP_CURRENT_TAIL = "0011881102"
# Временный шаблон живёт не дольше часа: брошенные по таймауту файлы
# добирает чистка при следующем входе в Яндекс-сценарий.
YANDEX_TEMPLATE_TTL_SECONDS = 3600

# Банки получателя Яндекса — в написании, как печатается в чеке.
YANDEX_BANK_OPTIONS = (
    ("sber", "Сбербанк"),
    ("tbank", "Т-Банк"),
    ("vtb", "ВТБ"),
    ("alfa", "Альфа-Банк"),
    ("psb", "ПСБ"),
    ("ozon", "Озон Банк (Ozon)"),
    ("wb", "Wildberries (Вайлдберриз Банк)"),
    ("rocket", "Рокетбанк"),
)

# --- состояния и кнопки Рокета (тот же ConversationHandler, что у Яндекса) --
(
    R_MENU,
    R_RECIPIENT_NAME,
    R_RECIPIENT_PHONE,
    R_RECIPIENT_BANK,
    R_RECIPIENT_BANK_CUSTOM,
    R_RECIPIENT_CARD,
    R_AMOUNT,
    R_SENDER_NAME,
    R_SENDER_PHONE,
    R_SENDER_ACCOUNT_CHOICE,
    R_SENDER_ACCOUNT_INPUT,
    R_TIME_CHOICE,
    R_TIME_INPUT,
    R_SUMMARY,
    R_EDIT_SELECT,
) = range(10, 25)
(
    S_PERIOD,
    S_ISSUE_CHOICE,
    S_ISSUE_INPUT,
    S_HOLDER,
    S_PHONE,
    S_ACCOUNT_CHOICE,
    S_ACCOUNT_TAIL_INPUT,
    S_ACCOUNT_FULL_INPUT,
    S_OPENING,
    S_OP_COUNT_CHOICE,
    S_OP_COUNT_INPUT,
    S_SOURCE_CHOICE,
    S_UPLOAD,
    S_UPLOAD_PENDING,
    S_SHORTAGE,
    S_OP_DAY_CHOICE,
    S_OP_DATE_INPUT,
    S_SUMMARY,
    S_EDIT_SELECT,
) = range(25, 44)

ROCKET_MENU_CB = "bank:rocket"
MTS_CB = "bank:mts"
VTB_CB = "bank:vtb"
WB_CB = "bank:wb"
ALFA_CB = "bank:alfa"
MAIN_MENU_CB = "menu:main"
BACK_CB = "nav:back"
ROCKET_SBP_CB = "rocket:sbp"
ROCKET_CARD_CB = "rocket:card"
ROCKET_STMT_CB = "rocket:stmt"
MTS_SBP_CB = "mts:sbp"
MTS_CARD_CB = "mts:card"
MTS_STMT_CB = "mts:stmt"
VTB_SBP_CB = "vtb:sbp"
VTB_CARD_CB = "vtb:card"
VTB_STMT_CB = "vtb:stmt"
VTB_UPLOAD_CB = "vtb:upload"
WB_SBP_CB = "wb:sbp"
MC_GENERATE_CB = "mcgen:generate"
MC_EDIT_CB = "mcgen:edit"
MC_MEM_CARD_CB = "mcmem:card"
V_GENERATE_CB = "vgen:generate"
V_EDIT_CB = "vgen:edit"
V_MEM_RECIPIENT_CB = "vmem:recipient"
V_MEM_SENDER_CB = "vmem:sender"
W_GENERATE_CB = "wgen:generate"
W_EDIT_CB = "wgen:edit"
W_MEM_RECIPIENT_CB = "wmem:recipient"
MEM_RECIPIENT_CB = "rmem:recipient"
MEM_SENDER_CB = "rmem:sender"
R_GENERATE_CB = "rgen:generate"
R_EDIT_CB = "rgen:edit"
S_GENERATE_CB = "sgen:generate"
S_EDIT_CB = "sgen:edit"
S_UPLOADS_DONE_CB = "stmt:uploads_done"
S_PERIOD_FIX_CB = "stmt:period_fix"
S_FILL_RANDOM_CB = "stmt:fill_random"
S_REDUCE_COUNT_CB = "stmt:reduce_count"
S_DAY_RECEIPT_CB = "stmt:day_receipt"
S_DAY_CHOOSE_CB = "stmt:day_choose"

MAX_AMOUNT = Decimal("999999999.99")

# Поля, доступные для правки со сводки: ключ -> (подпись, состояние, ключ данных)
EDIT_FIELDS = {
    "amount": ("💰 Сумма", AMOUNT, "amount"),
    "datetime": ("🕒 Время операции", Y_TIME_CHOICE, "operation_dt"),
    "sender_name": ("👤 ФИО отправителя", SENDER_NAME, "sender_name"),
    "sender_phone": ("📱 Телефон отправителя", SENDER_PHONE, "sender_phone"),
    "recipient_name": ("👤 ФИО получателя", RECIPIENT_NAME, "recipient_name"),
    "recipient_phone": ("📱 Телефон получателя", RECIPIENT_PHONE, "recipient_phone"),
    "recipient_bank": ("🏦 Банк получателя", RECIPIENT_BANK, "recipient_bank"),
}


# ---------------------------------------------------------------------------
# Валидация и нормализация ввода (чистые функции — покрыты тестами)
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(
    r"^\s*(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{1,12})"
    r"(?:[.,](\d{1,2}))?\s*(?:₽|руб\.?|RUB)?\s*$",
    re.IGNORECASE,
)


def parse_amount(text: str) -> str:
    """``"10000"`` / ``"10 000,5"`` -> каноническое ``"10 000,50 ₽"``."""
    match = _AMOUNT_RE.match(text or "")
    if not match:
        raise ValueError(
            "Не понял сумму. Введите, например: 10000 или 10 000,50"
        )
    digits = re.sub(r"\D", "", match.group(1))
    decimals = (match.group(2) or "0").ljust(2, "0")
    try:
        value = Decimal(f"{digits}.{decimals}")
    except InvalidOperation:
        raise ValueError("Не понял сумму. Введите, например: 10000 или 10 000,50")
    if value <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    if value > MAX_AMOUNT:
        raise ValueError("Сумма слишком большая для одного перевода.")
    grouped = f"{int(value):,}".replace(",", " ")
    return f"{grouped},{decimals} ₽"


_DT_FORMATS = (
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y в %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%y %H:%M",
    "%d.%m.%Y, %H:%M",
)


def parse_datetime(text: str, now: Optional[datetime] = None) -> datetime:
    """Время операции Яндекса (МСК).

    ``"сейчас"`` -> текущий момент с секундами (их использует движок);
    ``"03.11.2026 14:20"`` и только ``"14:20"`` (= сегодня) — без
    секунд: секунды из донора подставит сам движок при генерации.
    """
    cleaned = " ".join((text or "").strip().split())
    if cleaned.casefold() in ("сейчас", "now", "сейчас пожалуйста", "текущее время"):
        moment = now or datetime.now(MSK)
        return moment.astimezone(MSK).replace(microsecond=0)
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            only_time = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        today = (now or datetime.now(MSK)).astimezone(MSK).date()
        return datetime(
            today.year, today.month, today.day,
            only_time.hour, only_time.minute, only_time.second, tzinfo=MSK,
        )
    raise ValueError(
        "Не понял время. Введите, например: 03.11.2026 14:20 или только "
        "14:20 (= сегодня). Секунды придумывать не надо — их подставит движок."
    )


_NAME_TOKEN_RE = re.compile(r"^[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)?\.?$")


def parse_name(text: str, who: str = "ФИО") -> str:
    """ФИО: минимум два слова, только буквы, дефисы и точки инициалов."""
    cleaned = " ".join((text or "").strip().split())
    tokens = cleaned.split(" ") if cleaned else []
    if len(tokens) < 2 or not all(_NAME_TOKEN_RE.match(t) for t in tokens):
        raise ValueError(
            f"Не похоже на {who}. Введите, например: Иванов Иван Иванович"
        )
    if len(cleaned) > 100:
        raise ValueError(f"{who} слишком длинное (максимум 100 символов).")
    return cleaned


def parse_phone(text: str) -> str:
    """Телефон РФ -> нормализованный ``+7 900 351-70-80`` (как в чеке)."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError(
            "Не понял телефон. Введите, например: +7 900 351-70-80"
        )
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


_BANK_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 .,\-«»()№/&+]+$")


def parse_bank(text: str) -> str:
    """Банк получателя: непустое название, 2–64 символа."""
    cleaned = " ".join((text or "").strip().split())
    if (
        not (2 <= len(cleaned) <= 64)
        or not _BANK_RE.match(cleaned)
        or not re.search(r"[A-Za-zА-Яа-яЁё]", cleaned)
    ):
        raise ValueError(
            "Не понял название банка. Введите, например: Сбербанк"
        )
    return cleaned


def parse_alfa_amounts(text: str) -> tuple[int, int]:
    """``"10 5000"`` или ``"10 5 000"`` -> ``(10, 5000)``.

    Первое число — сумма, которая уже напечатана в загруженном чеке.
    Остальные цифры склеиваются в новую сумму.
    """
    nums = re.findall(r"\d+", text or "")
    if len(nums) < 2:
        raise ValueError(
            "Введите две суммы, например: 10 5000 — сначала та, что в чеке, потом новая."
        )
    amount_from = int(nums[0])
    amount_to = int("".join(nums[1:]))
    if amount_from <= 0 or amount_to <= 0:
        raise ValueError("Суммы должны быть больше нуля.")
    if Decimal(amount_to) > MAX_AMOUNT:
        raise ValueError("Сумма слишком большая для одного перевода.")
    return amount_from, amount_to


def parse_vtb_account_digits(text: str) -> str:
    """Номер счёта ВТБ: последние 4 цифры или длиннее, без звёздочки."""
    digits = re.sub(r"\D", "", text or "")
    if not (4 <= len(digits) <= 20):
        raise ValueError(
            "Введите номер счёта цифрами. Например последние 4: 9481"
        )
    return digits


def parse_mts_message(text: str) -> str:
    """Короткое сообщение МТС: кириллица, до 40 символов."""
    cleaned = " ".join((text or "").strip().split())
    if (
        not (2 <= len(cleaned) <= 40)
        or not re.search(r"[А-Яа-яЁё]", cleaned)
        or not re.fullmatch(r"[А-Яа-яЁё0-9 .,«»()-]+", cleaned)
    ):
        raise ValueError(
            "Сообщение — короткое, кириллицей. Например: Перевод СБП"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Выписка Яндекса: валидация и сборка операций (чистые функции)
# ---------------------------------------------------------------------------


def parse_passport(text: str) -> str:
    """Паспорт: 10 цифр в любом виде -> ``4510 123456``."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 10:
        raise ValueError(
            "Паспорт — это 10 цифр (серия и номер). Введите, например: 4510 123456"
        )
    return f"{digits[:4]} {digits[4:]}"


def parse_birth_date(text: str) -> date:
    """Дата рождения ``12.03.1995`` -> date."""
    cleaned = " ".join((text or "").strip().split())
    try:
        born = datetime.strptime(cleaned, "%d.%m.%Y").date()
    except ValueError:
        raise ValueError("Не понял дату рождения. Введите, например: 12.03.1995")
    if born.year < 1900 or born > datetime.now(MSK).date():
        raise ValueError("Дата рождения выглядит нереальной. Введите, например: 12.03.1995")
    return born


def parse_contract_number(text: str) -> str:
    """Номер договора выписки: «Э» + 17 цифр (формат движка)."""
    cleaned = (text or "").strip().replace(" ", "")
    if not re.fullmatch(r"Э\d{17}", cleaned):
        raise ValueError(
            "Номер договора — это Э и 17 цифр. Введите, например: Э20261103102094982"
        )
    return cleaned


def format_person_short(value: str) -> str:
    """ФИО -> «Имя Отчество Ф.», как печатает движок чеков Яндекса."""
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]

    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.strip(".").split("-"))

    if len(tokens) >= 2 and len(tokens[-1].strip(".")) == 1:
        parts = [title(t) for t in tokens[:-1]]
        return " ".join(parts) + f" {tokens[-1].strip('.')[0].upper()}."
    if len(tokens) >= 3:
        surname, given, patronymic = tokens[0], tokens[1], tokens[2]
        return f"{title(given)} {title(patronymic)} {surname.strip('.')[0].upper()}."
    if len(tokens) == 2:
        surname, given = tokens
        return f"{title(given)} {surname.strip('.')[0].upper()}."
    raise ValueError(f"Не похоже на ФИО: {value!r}")


def build_ystmt_sbp_description(kind: str, name: str, phone: str, bank: str) -> str:
    """Описание операции СБП в формате настоящей выписки Яндекса."""
    direction = "Входящий" if kind == "in" else "Исходящий"
    return f"{direction} перевод СБП, {format_person_short(name)}, {phone}, {bank}"


def parse_positive_amount(text: str) -> Decimal:
    """Положительная сумма с копейками -> Decimal (знак ставит сценарий)."""
    canonical = parse_amount(text)  # "1 500,50 ₽", ноль и минус отклоняет
    return Decimal(canonical.replace("₽", "").replace(" ", "").replace(",", "."))


def format_signed_amount(value: Decimal, sign: int) -> str:
    """Decimal -> подписанная сумма для движка: ``+1 500,50``/``-250,00``."""
    prefix = "+" if sign >= 0 else "-"
    return prefix + format_money_display(abs(value))


def ystmt_amount_decimal(operation: dict) -> Decimal:
    text = str(operation.get("amount") or "").replace("\u2013", "-")
    return Decimal(text.replace(" ", "").replace(",", "."))


def ystmt_totals(operations, opening_balance) -> Tuple[Decimal, Decimal, Decimal]:
    """(приход, расход, исходящий) — арифметика движка выписки."""
    income = sum(
        (ystmt_amount_decimal(op) for op in operations if ystmt_amount_decimal(op) > 0),
        Decimal("0"),
    )
    expense = -sum(
        (ystmt_amount_decimal(op) for op in operations if ystmt_amount_decimal(op) < 0),
        Decimal("0"),
    )
    opening = Decimal(str(opening_balance))
    return income, expense, opening + income - expense


def parse_ystmt_op_datetime(
    text: str, period_start: date, period_end: date, now: Optional[datetime] = None
) -> datetime:
    """Дата-время операции внутри периода.

    ``04.11.2026 12:40`` — полная форма; только ``12:40`` — если период
    из одного дня или сегодня внутри периода, дата понятна из периода.
    """
    cleaned = " ".join((text or "").strip().split())
    if cleaned.casefold() in ("сейчас", "now"):
        moment = (now or datetime.now(MSK)).astimezone(MSK).replace(second=0, microsecond=0)
    else:
        moment = None
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y в %H:%M", "%d.%m.%Y %H:%M:%S"):
            try:
                moment = datetime.strptime(cleaned, fmt).replace(tzinfo=MSK)
                break
            except ValueError:
                continue
        if moment is None:
            try:
                only = datetime.strptime(cleaned, "%H:%M")
            except ValueError:
                raise ValueError(
                    "Не понял дату и время. Введите, например: 04.11.2026 12:40"
                )
            today = (now or datetime.now(MSK)).astimezone(MSK).date()
            if period_start == period_end:
                day = period_start
            elif period_start <= today <= period_end:
                day = today
            else:
                raise ValueError(
                    "Одного времени мало: период из нескольких дней и сегодня "
                    "вне его. Введите дату и время, например: 04.11.2026 12:40"
                )
            moment = datetime(day.year, day.month, day.day, only.hour, only.minute, tzinfo=MSK)
    if not (period_start <= moment.date() <= period_end):
        raise ValueError(
            f"Операция {moment:%d.%m.%Y %H:%M} вне периода "
            f"{period_start:%d.%m.%Y}–{period_end:%d.%m.%Y}. Исправьте дату и время."
        )
    return moment


def parse_ystmt_bulk_operations(text: str, period_start: date, period_end: date) -> list:
    """Быстрый ввод: по одной операции на строку.

    ``04.11.2026 12:40 | описание | -250,00`` — дата-время, описание и
    подписанная сумма через ``|``. Любая битая строка отменяет весь ввод.
    """
    operations = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            raise ValueError(
                "Строка операции — это дата-время, описание и сумма через |, "
                "например: 04.11.2026 12:40 | описание | -250,00"
            )
        moment = parse_ystmt_op_datetime(parts[0], period_start, period_end)
        description = " ".join(parts[1].split())
        if not description:
            raise ValueError("В строке операции пустое описание.")
        amount_text = parts[2].replace("₽", "").strip()
        if not amount_text or amount_text[0] not in "+-–":
            raise ValueError(
                "Сумма в быстром вводе — со знаком, например: +1 500,50 или -250,00"
            )
        sign = -1 if amount_text[0] in "-–" else 1
        try:
            value = Decimal(amount_text[1:].replace(" ", "").replace(",", "."))
        except InvalidOperation:
            raise ValueError(f"Не понял сумму в строке: {parts[2]!r}")
        if value <= 0:
            raise ValueError("Сумма операции должна быть больше нуля (знак — отдельно).")
        operations.append(
            {
                "datetime": moment.strftime("%d.%m.%Y %H:%M"),
                "description": description,
                "amount": format_signed_amount(value, sign),
            }
        )
    if not operations:
        raise ValueError("Не нашёл ни одной операции в сообщении.")
    return operations


# ---------------------------------------------------------------------------
# Рокет: валидация и нормализация (чистые функции — покрыты тестами)
# ---------------------------------------------------------------------------

# Измеренные у Рокета формы (banks/rocket/BANK.md). ``engine_value`` — то,
# что получает движок; он сам приведёт СБП-банк к юридической форме, а для
# карты — к короткой (ВБ Банк у карточного движка означает «без блока»).
ROCKET_SBP_BANK_OPTIONS = (
    ("sber", "Сбербанк", "Сбербанк"),
    ("tbank", "Т-Банк", "Т-Банк"),
    ("vtb", "ВТБ", "ВТБ"),
    ("alfa", "Альфа-Банк", "Альфа-Банк"),
    ("ozon", "Ozon Банк (Озон)", "Озон Банк"),
    ("wb", "Wildberries (Вайлдберриз Банк)", "Wildberries"),
    ("psb", "ПСБ", "ПСБ"),
    ("otp", "ОТП Банк", "ОТП Банк"),
    ("mkb", "МКБ", "МКБ"),
    ("sovcom", "Совкомбанк", "Совкомбанк"),
    ("rosbank", "Росбанк", "Росбанк"),
    ("gazprom", "Газпромбанк", "Газпромбанк"),
)
ROCKET_CARD_BANK_OPTIONS = (
    ("sber", "Сбербанк", "Сбербанк"),
    ("tbank", "Т-Банк", "Т-Банк"),
    ("vtb", "ВТБ", "ВТБ"),
    ("alfa", "Альфа-Банк", "Альфа-Банк"),
    ("rosbank", "Росбанк", "Росбанк"),
    ("sovcom", "Совкомбанк", "Совкомбанк"),
    ("psb", "ПСБ", "ПСБ"),
    ("otp", "ОТП Банк", "ОТП Банк"),
    ("mkb", "МКБ", "МКБ"),
    ("gazprom", "Газпромбанк", "Газпромбанк"),
    ("ozon", "Ozon Банк (Озон)", "Озон Банк"),
    ("wb", "Wildberries (в чеке без банка)", None),
)
# Последние 5 цифр счёта, который движки наследуют от шаблона, если счёт
# явно не задан (СБП/карта: 40817 81045 02313 64892; выписка — тот же
# хвост у 40817810450231364892, см. BANK.md). Только для показа в сводке.
ROCKET_DEFAULT_ACCOUNT_TAIL = "64892"


def _format_rubles(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def parse_rocket_amount(text: str) -> str:
    """Сумма Рокета -> ``"10 000 ₽"``. Только целые рубли, без копеек."""
    match = _AMOUNT_RE.match(text or "")
    if not match:
        raise ValueError(
            "Не понял сумму. У Рокета сумма — только целые рубли. "
            "Введите, например: 500, 1 000 или 10 000"
        )
    decimals = match.group(2)
    if decimals and int(decimals) != 0:
        raise ValueError(
            "У Рокета сумма — только целые рубли, копейки движок не умеет. "
            "Введите, например: 1 000 (а не 1 000,50)"
        )
    digits = re.sub(r"\D", "", match.group(1))
    try:
        value = Decimal(digits)
    except InvalidOperation:
        raise ValueError("Не понял сумму. Введите, например: 1 000")
    if value <= 0:
        raise ValueError("Сумма должна быть больше нуля, например: 1 000")
    if value > MAX_AMOUNT:
        raise ValueError("Сумма слишком большая для одного перевода.")
    return _format_rubles(int(value))


def rocket_current_time(now: Optional[datetime] = None) -> datetime:
    """Текущий момент МСК с минутной точностью (секунды подберёт движок)."""
    moment = now or datetime.now(MSK)
    return moment.astimezone(MSK).replace(second=0, microsecond=0)


def parse_rocket_time(text: str, now: Optional[datetime] = None) -> datetime:
    """``"03.11.2026 16:26"`` или ``"16:26"`` (= сегодня) -> datetime МСК.

    Секунды всегда обнуляются: движок Рокета сам унаследует их из донора,
    как задумано в ``generate_rocket``/``generate_rocket_card``.
    """
    cleaned = " ".join((text or "").strip().split())
    if cleaned.casefold() in ("сейчас", "now", "текущее", "текущее время"):
        return rocket_current_time(now)
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).replace(
                tzinfo=MSK, second=0, microsecond=0
            )
        except ValueError:
            continue
    try:
        only_time = datetime.strptime(cleaned, "%H:%M")
    except ValueError:
        raise ValueError(
            "Не понял время. Введите, например: 03.11.2026 16:26 — "
            "или только 16:26 (это будет сегодня). Секунды не нужны."
        )
    today = rocket_current_time(now).date()
    return datetime(
        today.year, today.month, today.day,
        only_time.hour, only_time.minute, tzinfo=MSK,
    )


def parse_recipient_card(text: str) -> str:
    """Карта получателя -> маска ``2200 **** **** 9515`` для движка/сводки."""
    raw = (text or "").strip()
    if "*" in raw:
        groups = re.findall(r"\d+", raw)
        if len(groups) < 2 or len(groups[0]) != 4 or len(groups[-1]) != 4:
            raise ValueError(
                "Не понял карту. Введите полные 16 цифр или маску, "
                "например: 2200 **** **** 9515"
            )
        return f"{groups[0]} **** **** {groups[-1]}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 16:
        raise ValueError(
            "Не понял карту. Введите полные 16 цифр, например: "
            "2200 1234 5678 9515 — или маску 2200 **** **** 9515"
        )
    return f"{digits[:4]} **** **** {digits[-4:]}"


def parse_account20(text: str) -> str:
    """Счёт целиком -> строка из 20 цифр (группировку сделает движок)."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 20:
        raise ValueError(
            "Счёт должен быть целиком из 20 цифр. Введите, например, "
            "20 цифр подряд или группами по 5 — как в вашем счёте."
        )
    return digits


def masked_account(account: Optional[str]) -> str:
    """Для сводок: только последние 5 цифр счёта (``…64892``)."""
    digits = re.sub(r"\D", "", str(account or ""))
    tail = digits[-5:] if len(digits) >= 5 else ROCKET_DEFAULT_ACCOUNT_TAIL
    return f"…{tail}"


def make_rocket_account(tail: str) -> str:
    """Счёт Рокета по формуле: база 40817 810… + хвост, ключ — 9-я цифра.

    Формула ключа измерена на настоящих счетах Рокета/Совкомбанка
    (banks/rocket/BANK.md, раздел выписки): префикс БИК ``763``, веса
    7,1,3 по кругу, ключ = младший разряд от (сумма с нулём вместо
    ключа mod 10) x 3. Хвост ``64892`` воспроизводит счёт доноров.
    """
    digits_tail = re.sub(r"\D", "", str(tail or ""))
    if len(digits_tail) != 5:
        raise ValueError("Хвост счёта должен быть из 5 цифр")
    base = "4081781045023" + "13" + digits_tail
    digits = list(base)
    digits[8] = "0"
    seq = "763" + "".join(digits)
    weights = (7, 1, 3)
    total = sum((int(ch) * weights[i % 3]) % 10 for i, ch in enumerate(seq))
    key = ((total % 10) * 3) % 10
    return base[:8] + str(key) + base[9:]


def random_rocket_account() -> str:
    """Случайный счёт Рокета: случайный хвост + ключ по формуле выше."""
    tail = "".join(secrets.choice("0123456789") for _ in range(5))
    return make_rocket_account(tail)


def random_rocket_accounts(count: int = 3) -> list:
    """Несколько разных случайных счетов-кандидатов для кнопок выбора."""
    accounts = []
    while len(accounts) < count:
        account = random_rocket_account()
        if account not in accounts:
            accounts.append(account)
    return accounts


def parse_statement_period(text: str) -> Tuple[date, date]:
    """``"16.10.2026–23.10.2026"`` -> (дата начала, дата конца)."""
    found = re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if len(found) != 2:
        raise ValueError(
            "Не понял период. Введите, например: 16.10.2026–23.10.2026"
        )
    try:
        start = date(int(found[0][2]), int(found[0][1]), int(found[0][0]))
        end = date(int(found[1][2]), int(found[1][1]), int(found[1][0]))
    except ValueError:
        raise ValueError(
            "В периоде нет такой даты. Введите, например: "
            "16.10.2026–23.10.2026"
        )
    if end < start:
        raise ValueError(
            "Конец периода раньше начала. Введите, например: "
            "16.10.2026–23.10.2026"
        )
    return start, end


def parse_statement_date(text: str, what: str = "дату") -> date:
    cleaned = " ".join((text or "").strip().split())
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Не понял {what}. Введите, например: 03.11.2026")


def parse_opening_balance(text: str) -> str:
    """Входящий остаток выписки (копейки можно) -> ``"1000.50"`` для движка."""
    cleaned = re.sub(r"[\s\u00a0\u202f]", "", (text or "").strip()).replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(
            "Не понял остаток. Введите, например: 16,00 или 1 000,50"
        )
    if value < 0:
        raise ValueError("Входящий остаток не может быть отрицательным. Пример: 16,00")
    if value > MAX_AMOUNT:
        raise ValueError("Остаток слишком большой.")
    return format(value.quantize(Decimal("0.01")), "f")


def format_money_display(value) -> str:
    """``"1000.50"``/Decimal -> ``1 000,50`` для сводок в чате."""
    amount = Decimal(str(value))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def parse_operation_count(text: str) -> int:
    cleaned = (text or "").strip()
    if not re.fullmatch(r"\d{1,2}", cleaned):
        raise ValueError("Введите число платежей от 0 до 10, например: 3")
    value = int(cleaned)
    if not 0 <= value <= 10:
        raise ValueError("Платежей может быть от 0 до 10. Введите, например: 3")
    return value


def stmt_bank_of(data: dict) -> str:
    """Банк текущей выписки: rocket / mts / vtb."""
    scenario = data.get("scenario") or ""
    if scenario == "mts_statement":
        return "mts"
    if scenario == "vtb_statement":
        return "vtb"
    return "rocket"


def stmt_allowed_counts(bank: str) -> tuple:
    return STMT_ALLOWED_COUNTS.get(bank, STMT_ALLOWED_COUNTS["rocket"])


def parse_stmt_operation_count(text: str, bank: str) -> int:
    """Число платежей выписки — только допустимая раскладка банка."""
    allowed = stmt_allowed_counts(bank)
    cleaned = (text or "").strip()
    if not re.fullmatch(r"\d{1,2}", cleaned):
        pretty = ", ".join(str(n) for n in allowed)
        raise ValueError(f"Введите одно из чисел: {pretty}")
    value = int(cleaned)
    if value not in allowed:
        pretty = ", ".join(str(n) for n in allowed)
        raise ValueError(f"Для этой выписки можно только {pretty} платежей.")
    return value


def parse_mts_card(text: str) -> str:
    """Карта МТС: 16 цифр, маска или последние 4 — движок напечатает ``•• 8429``."""
    raw = (text or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 4:
        return digits
    if len(digits) == 16:
        return digits
    if ("*" in raw or "•" in raw) and len(digits) >= 4:
        return digits[-4:] if len(digits) != 16 else digits
    raise ValueError(
        "Не понял карту. Введите 16 цифр, маску или последние 4, например: 8429"
    )


def parse_vtb_payee_card(text: str) -> str:
    """Карта получателя ВТБ: 16 цифр или маска ``2200 27** **** 4432``."""
    raw = (text or "").strip()
    digits = re.sub(r"\D", "", raw)
    if "*" in raw:
        groups = re.findall(r"\d+", raw)
        if len(groups) >= 2 and len(groups[0]) == 4 and len(groups[-1]) == 4:
            return raw
    if len(digits) == 16:
        return digits
    raise ValueError(
        "Не понял карту. Введите 16 цифр или маску, например: 2200 27** **** 4432"
    )


def parse_wb_amount(text: str) -> str:
    """Сумма ВБ — только целые рубли, как у Рокета."""
    return parse_rocket_amount(text)


def parse_account_tail(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 5:
        raise ValueError(
            "Нужны ровно последние 5 цифр счёта, например: 12345"
        )
    return digits


def statement_account_display(data: dict) -> str:
    """Счёт выписки для сводки: только последние 5 цифр."""
    if data.get("stmt_account"):
        return masked_account(str(data["stmt_account"]))
    if data.get("stmt_account_tail"):
        return f"…{data['stmt_account_tail']}"
    bank = stmt_bank_of(data)
    if bank == "vtb":
        return "счёт донора"
    if bank == "mts":
        return "счёт шаблона"
    return f"…{ROCKET_DEFAULT_ACCOUNT_TAIL}"


def statement_sources_text(data: dict) -> str:
    """Человекочитаемый источник платежей выписки для сводки/подписи."""
    total = int(data.get("stmt_operation_count") or 0)
    uploaded = len(data.get("stmt_receipt_paths") or [])
    if data.get("stmt_source") == "manual":
        ops = list(data.get("stmt_ops") or [])
        n = len(ops)
        if n == 0:
            return "0 платежей: настроены вручную"
        return f"{n} платежей: настроены вручную"
    if total == 0:
        return "0 платежей"
    if uploaded and data.get("stmt_fill_random") and uploaded < total:
        return (
            f"{total} платежей: {uploaded} из загруженных чеков + "
            f"{total - uploaded} случайно по настоящим операциям"
        )
    if uploaded:
        return f"{total} платежей: {uploaded} из загруженных чеков"
    return f"{total} платежей: случайно по настоящим операциям"


def statement_after_upload_action(uploaded: int, total: int) -> str:
    """Ветка после загрузки чеков: ``shortage`` или сразу ``summary``."""
    return "shortage" if uploaded < total else "summary"


# ---------------------------------------------------------------------------
# Вызов движка (единственное место, где бот касается генерации PDF)
# ---------------------------------------------------------------------------


def engine_path() -> Path:
    override = os.environ.get("YANDEX_ENGINE_PATH")
    return Path(override).expanduser() if override else DEFAULT_ENGINE_PATH


def _load_yandex_engine_module():
    """Лениво загрузить модуль движка ``banks/yandex/engine.py``."""
    path = engine_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Движок Яндекса не найден: {path} "
            "(ожидается banks/yandex/engine.py с функцией generate_yandex)"
        )
    spec = importlib.util.spec_from_file_location("checketop_yandex_engine", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить движок Яндекса из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_yandex_engine"] = module
    spec.loader.exec_module(module)
    return module


def load_generate_yandex() -> Callable[..., Any]:
    """Лениво загрузить ``generate_yandex`` из ``banks/yandex/engine.py``."""
    module = _load_yandex_engine_module()
    func = getattr(module, "generate_yandex", None)
    if not callable(func):
        raise AttributeError(f"В {engine_path()} нет функции generate_yandex(...)")
    return func


def load_yandex_receipt_class():
    """Класс ``Receipt`` — существующий парсер квитанций движка.

    Берётся из модуля движка (он импортирует его из ``yandex_receipt``);
    если движок заменён минимальным модулем без него — напрямую из
    ``yandex_receipt.py`` в корне репозитория (тот же парсер).
    """
    try:
        module = _load_yandex_engine_module()
        receipt_cls = getattr(module, "Receipt", None)
        if receipt_cls is not None:
            return receipt_cls
    except Exception:  # noqa: BLE001 - парсер ниже попробует сам
        pass
    parser_path = REPO_ROOT / "yandex_receipt.py"
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "checketop_yandex_receipt", parser_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить парсер квитанций из {parser_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_yandex_receipt"] = module
    spec.loader.exec_module(module)
    return module.Receipt


def load_yandex_spent_tokens() -> set:
    """Потраченные серийные токены СБП (реестр движка + засеянные)."""
    try:
        module = _load_yandex_engine_module()
        loader = getattr(module, "load_spent_tokens", None)
        if callable(loader):
            return set(loader())
    except Exception:  # noqa: BLE001 - ниже запасное чтение реестра
        pass
    # Запасной путь: сам файл реестра (без засеянных в движке токенов).
    import json

    override = os.environ.get("YANDEX_TOKEN_REGISTRY")
    registry = (
        Path(override).expanduser()
        if override
        else REPO_ROOT / "banks" / "yandex" / "donors" / "spent_tokens.json"
    )
    try:
        raw = json.loads(registry.read_text(encoding="utf-8"))
        entries = raw.get("spent", raw) if isinstance(raw, dict) else {}
        return set(entries) if isinstance(entries, dict) else set()
    except (OSError, ValueError):
        return set()


def yandex_template_token(sbp_id: str) -> str:
    """Серийный токен СБП-ID: позиции 12–22 (как в движке)."""
    return str(sbp_id or "")[11:22]


def yandex_template_warnings(sbp_id: str, spent_tokens) -> list:
    """Предупреждения по шаблону: потраченный токен и/или чужая эпоха."""
    warnings = []
    if yandex_template_token(sbp_id) in set(spent_tokens or ()):
        warnings.append("spent")
    if str(sbp_id or "")[22:] != YANDEX_SBP_CURRENT_TAIL:
        warnings.append("era")
    return warnings


def inspect_yandex_template(path) -> dict:
    """Проверить загруженный PDF существующим парсером движка.

    Валидная квитанция Яндекс Банка: парсится ``Receipt``, есть
    СБП-ID из 32 символов и сообщение отправителя «—» (другое
    сообщение шаблоны-доноры движка не принимают). Возвращает данные
    донора для подтверждения и список предупреждений.
    """
    receipt_cls = load_yandex_receipt_class()
    receipt = receipt_cls(str(path))
    fields = dict(receipt.fields)
    sbp_id = str(fields.get("sbp_id") or "").strip()
    if len(sbp_id) != 32:
        raise ValueError(
            "в квитанции нет идентификатора операции СБП из 32 символов — "
            "это не настоящая квитанция Яндекс Банка"
        )
    if fields.get("message") != "—":
        raise ValueError(
            "сообщение отправителя в квитанции не «—» — такой файл "
            "шаблоном быть не может"
        )
    if not fields.get("datetime") or not fields.get("amount"):
        raise ValueError("в квитанции не читаются дата операции и сумма")
    return {
        "sbp_id": sbp_id,
        "token": yandex_template_token(sbp_id),
        "donor_datetime": str(fields["datetime"]),
        "donor_amount": str(fields["amount"]),
        "warnings": yandex_template_warnings(
            sbp_id, load_yandex_spent_tokens()
        ),
    }


def yandex_template_warning_text(warnings) -> str:
    lines = ["⚠️ <b>Проверьте шаблон</b>", ""]
    if "spent" in warnings:
        lines.append(
            "Этот шаблон уже использовался: токен его операции есть в "
            "списке потраченных — проверяющий может пометить чек как повтор."
        )
    if "era" in warnings:
        lines.append(
            "Шаблон чужой эпохи: хвост номера операции у него не "
            f"{YANDEX_SBP_CURRENT_TAIL} — номер операции может не пройти проверку."
        )
    lines.append("")
    lines.append("Всё равно создать чек из этого шаблона?")
    return "\n".join(lines)


def yandex_template_root() -> Path:
    return Path(tempfile.gettempdir()) / "checketop_yandex_templates"


def _cleanup_yandex_template_files(data: dict) -> None:
    """Удалить временный шаблон (файл и его папку) из снимка user_data."""
    raw_path = data.get("y_template_path")
    raw_dir = data.get("y_template_dir")
    if raw_path:
        try:
            Path(str(raw_path)).unlink(missing_ok=True)
        except OSError:
            pass
    if raw_dir:
        try:
            Path(str(raw_dir)).rmdir()
        except OSError:
            pass


def _cleanup_stale_yandex_templates() -> None:
    """Таймаут шаблонов: стереть брошенные папки старше TTL."""
    root = yandex_template_root()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    import time

    cutoff = time.time() - YANDEX_TEMPLATE_TTL_SECONDS
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                for child in entry.iterdir():
                    child.unlink(missing_ok=True)
                entry.rmdir()
        except OSError:
            pass


def _is_fresh_donor_refusal(error: BaseException) -> bool:
    """Отказ движка «нужен свежий настоящий чек» (пул/токены/эпоха)."""
    if error.__class__.__name__ == "NoUnusedDonorTokenError":
        return True
    text = str(error)
    return any(
        marker in text
        for marker in (
            "Нет свободного токена",
            "В пуле нет настоящих чеков",
            "Нужен новый настоящий чек",
            "нужен свежий настоящий чек",
            "чужой эпохи",
        )
    )


async def generate_pdf(data: dict, output_path: Path) -> Path:
    """Чек Яндекса — генератор из бота с Яндекс Диска."""
    import disk_engines
    result = await asyncio.to_thread(disk_engines.yandex_receipt, data)
    if inspect.isawaitable(result):
        result = await result

    if isinstance(result, (bytes, bytearray)):
        output_path.write_bytes(bytes(result))
        pdf_path = output_path
    elif result is None:
        pdf_path = output_path
    elif isinstance(result, (str, os.PathLike)):
        pdf_path = Path(result)
    else:
        raise TypeError(
            f"generate_yandex вернул {type(result).__name__}, "
            "ожидался путь к PDF, bytes или None"
        )

    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise FileNotFoundError(f"Движок не создал PDF: {pdf_path}")
    with pdf_path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError(f"Движок вернул не PDF-файл: {pdf_path}")
    return pdf_path


def receipt_filename(data: dict) -> str:
    """Имя как у настоящих скачиваний Яндекса: ``<uuid4>.pdf``."""
    return f"{uuid.uuid4()}.pdf"


# --- Выписка Яндекса: ленивая загрузка движка (в момент генерации) --------

DEFAULT_YANDEX_STATEMENT_PATH = REPO_ROOT / "banks" / "yandex" / "statement.py"


def yandex_statement_path() -> Path:
    override = os.environ.get("YANDEX_STATEMENT_PATH")
    return Path(override).expanduser() if override else DEFAULT_YANDEX_STATEMENT_PATH


def load_generate_yandex_statement() -> Callable[..., Any]:
    return _load_engine_func(
        yandex_statement_path(), "checketop_yandex_statement",
        "generate_yandex_statement", "Движок выписки Яндекса",
    )


async def generate_yandex_statement_pdf(data: dict, output_path: Path) -> Path:
    """Выписка Яндекса: только вызов ``generate_yandex_statement``."""
    generate = load_generate_yandex_statement()
    result = await asyncio.to_thread(
        generate,
        period_start=data["ystmt_period_start"].strftime("%d.%m.%Y"),
        period_end=data["ystmt_period_end"].strftime("%d.%m.%Y"),
        holder_name=data["ystmt_holder_name"],
        holder_birth=data["ystmt_holder_birth"].strftime("%d.%m.%Y"),
        holder_passport=data["ystmt_holder_passport"],
        opening_balance=data["ystmt_opening_balance"],
        operations=[dict(op) for op in data.get("ystmt_ops") or []],
        contract_date=data["ystmt_contract_date"].strftime("%d.%m.%Y"),
        contract_number=data.get("ystmt_contract_number"),
        issue_date=data["ystmt_issue_date"].strftime("%d.%m.%Y"),
        output_path=str(output_path),
        generated_at=datetime.now(MSK),
    )
    return _ensure_pdf_result(result, output_path, "generate_yandex_statement")


def explain_ystmt_error(error: BaseException) -> str:
    """Короткое понятное объяснение ошибки движка выписки Яндекса."""
    text = str(error)
    low = text.lower()
    if "no donor glyph" in low or "glyph" in low:
        return (
            "Не получилось: в выписке есть буква, которой нет в глифах "
            "настоящих чеков и выписок Яндекса. Измените имя или описание "
            "без этой буквы — либо владельцу нужна новая настоящая "
            "выписка или чек Яндекса с этой буквой в пул."
        )
    if "fit one page" in low or "do not fit" in low or "not fit" in low:
        return (
            "Не получилось: операции не помещаются на одну страницу "
            "выписки. Уменьшите число операций или сократите описания."
        )
    if "statement donor" in low or "no statement donor" in low or "donor not found" in low:
        return (
            "Не получилось: на сервере нет донора выписки Яндекса — это "
            "вопрос к владельцу, от вас тут ничего не нужно."
        )
    return f"Не получилось: {_short_error(error)}"


# --- Рокет: ленивая загрузка движков (как у Яндекса, в момент генерации) ---

DEFAULT_ROCKET_ENGINE_PATH = REPO_ROOT / "banks" / "rocket" / "engine.py"
DEFAULT_ROCKET_STATEMENT_PATH = REPO_ROOT / "banks" / "rocket" / "rocket_statement.py"


def rocket_engine_path() -> Path:
    override = os.environ.get("ROCKET_ENGINE_PATH")
    return Path(override).expanduser() if override else DEFAULT_ROCKET_ENGINE_PATH


def rocket_statement_path() -> Path:
    override = os.environ.get("ROCKET_STATEMENT_PATH")
    return Path(override).expanduser() if override else DEFAULT_ROCKET_STATEMENT_PATH


def _load_engine_func(path: Path, module_name: str, func_name: str, label: str):
    if not path.is_file():
        raise FileNotFoundError(f"{label} не найден: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {label} из {path}")
    module = importlib.util.module_from_spec(spec)
    # Регистрация в sys.modules обязательна: движок выписки использует
    # dataclass, который во время exec смотрит sys.modules[cls.__module__].
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise AttributeError(f"В {path} нет функции {func_name}(...)")
    return func


def load_generate_rocket() -> Callable[..., Any]:
    return _load_engine_func(
        rocket_engine_path(), "checketop_rocket_engine_sbp", "generate_rocket",
        "Движок Рокета (СБП)",
    )


def load_generate_rocket_card() -> Callable[..., Any]:
    return _load_engine_func(
        rocket_engine_path(), "checketop_rocket_engine_card", "generate_rocket_card",
        "Движок Рокета (карта)",
    )


def load_generate_rocket_statement() -> Callable[..., Any]:
    return _load_engine_func(
        rocket_statement_path(), "checketop_rocket_statement",
        "generate_rocket_statement", "Движок выписки Рокета",
    )


def load_operation_from_receipt() -> Callable[..., Any]:
    return _load_engine_func(
        rocket_statement_path(), "checketop_rocket_statement_import",
        "operation_from_receipt", "Импорт чека Рокета в выписку",
    )


def _ensure_pdf_result(result: Any, output_path: Path, engine_label: str) -> Path:
    """Возврат движка (путь / bytes / None) -> проверенный путь к PDF."""
    if inspect.isawaitable(result):  # pragma: no cover - движки синхронные
        raise TypeError(f"{engine_label} вернул awaitable вместо PDF")
    if isinstance(result, (bytes, bytearray)):
        output_path.write_bytes(bytes(result))
        pdf_path = output_path
    elif result is None:
        pdf_path = output_path
    elif isinstance(result, (str, os.PathLike)):
        pdf_path = Path(result)
    else:
        raise TypeError(
            f"{engine_label} вернул {type(result).__name__}, "
            "ожидался путь к PDF, bytes или None"
        )
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise FileNotFoundError(f"Движок не создал PDF: {pdf_path}")
    with pdf_path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError(f"Движок вернул не PDF-файл: {pdf_path}")
    return pdf_path


async def generate_rocket_pdf(data: dict, output_path: Path) -> Path:
    """Чек Рокета по СБП: только вызов ``generate_rocket``. PDF-логики нет."""
    generate = load_generate_rocket()
    result = await asyncio.to_thread(
        generate,
        amount=data["amount"],
        operation_dt=data["operation_dt"],
        sender_name=data["sender_name"],
        sender_phone=data["sender_phone"],
        recipient_name=data["recipient_name"],
        recipient_phone=data["recipient_phone"],
        recipient_bank=data["recipient_bank"],
        sender_account=data.get("sender_account"),
        output_path=str(output_path),
    )
    return _ensure_pdf_result(result, output_path, "generate_rocket")


async def generate_rocket_card_pdf(data: dict, output_path: Path) -> Path:
    """Чек Рокета по карте: только вызов ``generate_rocket_card``."""
    generate = load_generate_rocket_card()
    result = await asyncio.to_thread(
        generate,
        amount=data["amount"],
        operation_dt=data["operation_dt"],
        sender_name=data["sender_name"],
        recipient_card=data["recipient_card"],
        recipient_bank=data.get("recipient_bank"),
        sender_account=data.get("sender_account"),
        output_path=str(output_path),
    )
    return _ensure_pdf_result(result, output_path, "generate_rocket_card")


def _statement_engine_kwargs(data: dict, output_path: Path, bank: str) -> dict:
    """Общие kwargs выписки + operations= только в режиме «Настроить»."""
    kwargs = {
        "period_start": data["stmt_period_start"],
        "period_end": data["stmt_period_end"],
        "holder_name": data["stmt_holder_name"],
        "opening_balance": data.get("stmt_opening_balance"),
        "output_path": str(output_path),
        "fill_random": bool(data.get("stmt_fill_random", True)),
        "operation_count": int(data.get("stmt_operation_count") or 0),
        "receipt_paths": list(data.get("stmt_receipt_paths") or []),
    }
    if data.get("stmt_issue_date") is not None:
        kwargs["issue_date"] = data["stmt_issue_date"]
    if bank == "rocket":
        kwargs["account"] = (
            data.get("stmt_account") if data.get("stmt_account_mode") == "full" else None
        )
        kwargs["account_tail"] = (
            data.get("stmt_account_tail") if data.get("stmt_account_mode") == "tail" else None
        )
        kwargs["random_account_tail"] = False
        kwargs["operation_date"] = data.get("stmt_operation_date")
        kwargs["phone"] = data.get("stmt_phone")
    elif bank == "mts":
        kwargs["account"] = data.get("stmt_account")
        kwargs["card"] = data.get("stmt_card")
    else:
        kwargs["account"] = data.get("stmt_account")
    if data.get("stmt_source") == "manual":
        ops = list(data.get("stmt_ops") or [])
        kwargs["operations"] = ops
        kwargs["fill_random"] = False
        kwargs["operation_count"] = len(ops)
        kwargs["receipt_paths"] = []
    return kwargs


async def generate_rocket_statement_pdf(data: dict, output_path: Path) -> Path:
    """Выписка Рокета: только вызов ``generate_rocket_statement``."""
    generate = load_generate_rocket_statement()
    kwargs = _statement_engine_kwargs(data, output_path, bank="rocket")
    result = await asyncio.to_thread(generate, **kwargs)
    return _ensure_pdf_result(result, output_path, "generate_rocket_statement")


def import_receipt_operation(path, bank: str = "rocket") -> dict:
    """Чек PDF -> операция выписки, строго через движок выписки."""
    if bank == "mts":
        module = load_mts_engine()
        func = getattr(module, "operation_from_receipt", None)
        if not callable(func):
            raise AttributeError("В движке МТС нет operation_from_receipt(...)")
    elif bank == "vtb":
        func = load_vtb_operation_from_receipt()
    else:
        func = load_operation_from_receipt()
    result = func(str(path))
    if not isinstance(result, dict):
        raise TypeError("operation_from_receipt вернул не словарь операции")
    return result


def receipt_operation_date(operation: dict) -> date:
    """Дата операции, импортированной движком из чека."""
    value = operation.get("datetime") or operation.get("date") or operation.get("dt")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return parse_statement_date(str(value)[:10], "дату чека")
    raise ValueError("Движок не вернул дату операции чека")


DEFAULT_MTS_ENGINE_PATH = REPO_ROOT / "banks" / "mts" / "engine.py"
_MTS_ENGINE_CACHE: dict = {"path": None, "module": None, "pool": None}


def mts_engine_path() -> Path:
    override = os.environ.get("MTS_ENGINE_PATH")
    return Path(override).expanduser() if override else DEFAULT_MTS_ENGINE_PATH


def load_mts_engine():
    """Модуль движка МТС. Пул глифов строится один раз на этот путь."""
    path = mts_engine_path()
    if _MTS_ENGINE_CACHE["path"] == path and _MTS_ENGINE_CACHE["module"] is not None:
        return _MTS_ENGINE_CACHE["module"]
    if not path.is_file():
        raise FileNotFoundError(f"Движок МТС не найден: {path}")
    spec = importlib.util.spec_from_file_location("checketop_mts_engine", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить движок МТС из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_mts_engine"] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "generate_mts", None)):
        raise AttributeError(f"В {path} нет функции generate_mts(...)")
    _MTS_ENGINE_CACHE["path"] = path
    _MTS_ENGINE_CACHE["module"] = module
    _MTS_ENGINE_CACHE["pool"] = None
    return module


def mts_glyph_pool(module):
    """Один пул на процесс: 14 PDF и конверсия шрифтов не повторяются."""
    if _MTS_ENGINE_CACHE.get("pool") is not None:
        return _MTS_ENGINE_CACHE["pool"]
    builder = getattr(module, "build_pool", None)
    donors = getattr(module, "_donor_paths", None)
    if not callable(builder) or not callable(donors):
        return None
    pool = builder(donors())
    _MTS_ENGINE_CACHE["pool"] = pool
    return pool


async def generate_mts_pdf(data: dict, output_path: Path) -> Path:
    """Чек МТС СБП — генератор из бота с Яндекс Диска."""
    import disk_engines
    pdf = await asyncio.to_thread(disk_engines.mts_sbp, data)
    output_path.write_bytes(pdf)
    return output_path


async def generate_mts_card_pdf(data: dict, output_path: Path) -> Path:
    """Чек МТС по карте: только вызов ``generate_mts_card``."""
    module = load_mts_engine()
    generate = getattr(module, "generate_mts_card", None)
    if not callable(generate):
        raise AttributeError("В движке МТС нет generate_mts_card(...)")
    result = await asyncio.to_thread(
        generate,
        amount=data["amount"],
        operation_dt=data["operation_dt"],
        recipient_card=data["recipient_card"],
        output_path=str(output_path),
        sender_card=data.get("sender_card"),
        sender_account=data.get("sender_account"),
    )
    return _ensure_pdf_result(result, output_path, "generate_mts_card")


async def generate_mts_statement_pdf(data: dict, output_path: Path) -> Path:
    """Выписка МТС: только вызов ``generate_mts_statement``."""
    module = load_mts_engine()
    generate = getattr(module, "generate_mts_statement", None)
    if not callable(generate):
        raise AttributeError("В движке МТС нет generate_mts_statement(...)")
    kwargs = _statement_engine_kwargs(data, output_path, bank="mts")
    result = await asyncio.to_thread(generate, **kwargs)
    return _ensure_pdf_result(result, output_path, "generate_mts_statement")


def mts_user_error(error: BaseException) -> str:
    if error.__class__.__name__ == "GlyphCoverageError":
        return (
            "Не получилось: буквы вне пула настоящих чеков МТС, "
            "нужен новый чек владельца"
        )
    return f"Не получилось: {_short_error(error)}"


def mts_receipt_filename(data: dict) -> str:
    """Имя как у настоящей выгрузки МТС: ``Unknown.pdf``."""
    return "Unknown.pdf"


def mts_statement_filename(data: dict) -> str:
    start = data.get("stmt_period_start")
    end = data.get("stmt_period_end")
    count = int(data.get("stmt_operation_count") or 0)
    if start and end:
        return f"mts_statement_{start:%Y%m%d}_{end:%Y%m%d}_{count}ops.pdf"
    return "mts_statement.pdf"


DEFAULT_VTB_ENGINE_PATH = REPO_ROOT / "banks" / "vtb" / "engine.py"
DEFAULT_VTB_STATEMENT_PATH = REPO_ROOT / "banks" / "vtb" / "vtb_statement.py"
_VTB_ENGINE_CACHE: dict = {"path": None, "module": None}


def vtb_engine_path() -> Path:
    override = os.environ.get("VTB_ENGINE_PATH")
    return Path(override).expanduser() if override else DEFAULT_VTB_ENGINE_PATH


def vtb_statement_path() -> Path:
    override = os.environ.get("VTB_STATEMENT_PATH")
    return Path(override).expanduser() if override else DEFAULT_VTB_STATEMENT_PATH


def load_vtb_engine():
    path = vtb_engine_path()
    if _VTB_ENGINE_CACHE["path"] == path and _VTB_ENGINE_CACHE["module"] is not None:
        return _VTB_ENGINE_CACHE["module"]
    if not path.is_file():
        raise FileNotFoundError(f"Движок ВТБ не найден: {path}")
    spec = importlib.util.spec_from_file_location("checketop_vtb_engine", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить движок ВТБ из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_vtb_engine"] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "generate_vtb", None)):
        raise AttributeError(f"В {path} нет функции generate_vtb(...)")
    _VTB_ENGINE_CACHE["path"] = path
    _VTB_ENGINE_CACHE["module"] = module
    return module


def load_vtb_operation_from_receipt() -> Callable[..., Any]:
    module = load_vtb_engine()
    func = getattr(module, "operation_from_receipt", None)
    if callable(func):
        return func
    return _load_engine_func(
        vtb_statement_path(), "checketop_vtb_statement",
        "operation_from_receipt", "Движок выписки ВТБ",
    )


async def generate_vtb_pdf(data: dict, output_path: Path) -> Path:
    """Чек ВТБ СБП — генератор из бота с Яндекс Диска."""
    import disk_engines
    pdf = await asyncio.to_thread(disk_engines.vtb_sbp, data)
    output_path.write_bytes(pdf)
    return output_path


async def generate_vtb_card_pdf(data: dict, output_path: Path) -> Path:
    module = load_vtb_engine()
    generate = getattr(module, "generate_vtb_card", None)
    if not callable(generate):
        raise AttributeError("В движке ВТБ нет generate_vtb_card(...)")
    result = await asyncio.to_thread(
        generate,
        amount=data["amount"],
        operation_dt=data["operation_dt"],
        sender_name=data["sender_name"],
        recipient_card=data["recipient_card"],
        recipient_bank=data.get("recipient_bank"),
        output_path=str(output_path),
        sender_account=data.get("sender_account"),
        payer_card=data.get("payer_card"),
    )
    return _ensure_pdf_result(result, output_path, "generate_vtb_card")


async def generate_vtb_statement_pdf(data: dict, output_path: Path) -> Path:
    module = load_vtb_engine()
    generate = getattr(module, "generate_vtb_statement", None)
    if not callable(generate):
        generate = _load_engine_func(
            vtb_statement_path(), "checketop_vtb_statement_gen",
            "generate_vtb_statement", "Движок выписки ВТБ",
        )
    kwargs = _statement_engine_kwargs(data, output_path, bank="vtb")
    result = await asyncio.to_thread(generate, **kwargs)
    return _ensure_pdf_result(result, output_path, "generate_vtb_statement")


def vtb_receipt_filename(data: dict) -> str:
    moment = data.get("operation_dt") or rocket_current_time()
    kind = "card" if data.get("scenario") == "vtb_card" else "sbp"
    return f"vtb_{kind}_{moment:%Y%m%d_%H%M}.pdf"


def vtb_statement_filename(data: dict) -> str:
    start = data.get("stmt_period_start")
    end = data.get("stmt_period_end")
    count = int(data.get("stmt_operation_count") or 0)
    if start and end:
        return f"vtb_statement_{start:%Y%m%d}_{end:%Y%m%d}_{count}ops.pdf"
    return "vtb_statement.pdf"


def vtb_user_error(error: BaseException) -> str:
    return f"Не получилось: {_short_error(error)}"


DEFAULT_WB_ENGINE_PATH = REPO_ROOT / "banks" / "wb" / "engine.py"
_WB_ENGINE_CACHE: dict = {"path": None, "module": None}


def wb_engine_path() -> Path:
    override = os.environ.get("WB_ENGINE_PATH")
    return Path(override).expanduser() if override else DEFAULT_WB_ENGINE_PATH


def load_wb_engine():
    path = wb_engine_path()
    if _WB_ENGINE_CACHE["path"] == path and _WB_ENGINE_CACHE["module"] is not None:
        return _WB_ENGINE_CACHE["module"]
    if not path.is_file():
        raise FileNotFoundError(f"Движок ВБ Банка не найден: {path}")
    spec = importlib.util.spec_from_file_location("checketop_wb_engine", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить движок ВБ Банка из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_wb_engine"] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "generate_wb", None)):
        raise AttributeError(f"В {path} нет функции generate_wb(...)")
    _WB_ENGINE_CACHE["path"] = path
    _WB_ENGINE_CACHE["module"] = module
    return module


async def generate_wb_pdf(data: dict, output_path: Path) -> Path:
    """Чек ВБ СБП — генератор из бота с Яндекс Диска."""
    import disk_engines
    pdf = await asyncio.to_thread(disk_engines.wb_sbp, data)
    output_path.write_bytes(pdf)
    return output_path


def wb_user_error(error: BaseException) -> str:
    return f"Не получилось: {_short_error(error)}"


def wb_receipt_filename(data: dict) -> str:
    return f"{uuid.uuid4()}.pdf"


def rocket_receipt_filename(data: dict) -> str:
    """Имя как у настоящей выгрузки Рокета из банка: ``receipt_rocket.pdf``."""
    return "receipt_rocket.pdf"


def rocket_statement_filename(data: dict, moment: Optional[datetime] = None) -> str:
    """Имя как у настоящей выписки Рокета: ``Документ.pdf``."""
    return "Документ.pdf"


# ---------------------------------------------------------------------------
# Клавиатуры и тексты шагов
# ---------------------------------------------------------------------------


def _cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data=CANCEL_CB)]]
    )


def _prompt(state: int, context=None) -> Tuple[str, InlineKeyboardMarkup]:
    data = context.user_data if context is not None else {}
    nav = _nav_row()
    if state == RECIPIENT_NAME:
        rows = []
        mem = data.get("mem_yandex_recipient") or {}
        if mem.get("recipient_name"):
            rows.append([InlineKeyboardButton(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_name']})",
                callback_data=Y_MEM_RECIPIENT_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Получатель.</b> Введите ФИО получателя.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == RECIPIENT_PHONE:
        return (
            "📱 <b>Телефон получателя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 963 297-20-08</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == RECIPIENT_BANK:
        buttons = [
            InlineKeyboardButton(label, callback_data=f"ybank:{slug}")
            for slug, label in YANDEX_BANK_OPTIONS
        ]
        buttons.append(InlineKeyboardButton("✍️ Ввести свой", callback_data="ybank:custom"))
        return (
            "🏦 <b>Банк получателя</b> — выберите кнопкой, как в чеке:",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == Y_RECIPIENT_BANK_CUSTOM:
        return (
            "🏦 Введите банк получателя.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == AMOUNT:
        return (
            "💰 <b>Сумма перевода.</b> Введите сумму сами, копейки можно.\n"
            "Нажмите, чтобы скопировать: <code>17 350,00 ₽</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == SENDER_NAME:
        rows = []
        mem = data.get("mem_yandex_sender") or {}
        if mem.get("sender_name"):
            rows.append([InlineKeyboardButton(
                f"↩️ Оставить как в прошлый раз ({mem['sender_name']})",
                callback_data=Y_MEM_SENDER_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Отправитель.</b> Введите ФИО отправителя — его можно "
            "менять каждый раз.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == SENDER_PHONE:
        return (
            "📱 <b>Телефон отправителя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == Y_TIME_CHOICE:
        rows = [
            [InlineKeyboardButton("🕒 Текущее время", callback_data="ytime:now"),
             InlineKeyboardButton("✏️ Изменить время", callback_data="ytime:change")],
            nav,
        ]
        return ("🕒 <b>Время операции</b> — текущее или изменить?", InlineKeyboardMarkup(rows))
    if state == DATETIME:
        return (
            "🕒 Введите дату и время операции (МСК).\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 14:20</code> или "
            "только время <code>14:20</code> (= сегодня). Секунды "
            "придумывать не надо — движок подставит секунды донора.",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no prompt for state {state}")


async def _delete_chat_message(context, chat_id, message_id) -> None:
    """Удалить сообщение чата; нет прав/уже удалено — молча пропустить."""
    if not message_id:
        return
    deleter = getattr(context.bot, "delete_message", None)
    if deleter is None:
        return
    try:
        await deleter(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001 - чистка чата не должна ронять диалог
        pass


async def _delete_user_message(update: Update, context) -> None:
    """Удалить сообщение-ответ пользователя, чтобы чат не катился вниз."""
    message = update.effective_message
    await _delete_chat_message(
        context, update.effective_chat.id, getattr(message, "message_id", None)
    )


async def _drop_prompt(update, context) -> None:
    """Убрать вопрос бота перед отправкой готового PDF."""
    message_id = context.user_data.pop("_bot_prompt_id", None)
    if message_id:
        await _delete_chat_message(context, update.effective_chat.id, message_id)


async def _send_tracked_message(update, context, text, reply_markup=None):
    """Показать следующий шаг, отредактировав сообщение бота на месте.

    Своё предыдущее сообщение бот не удаляет и не шлёт новое —
    он правит его текст и кнопки прямо на месте (``editMessageText``):
    вопрос просто превращается в следующий вопрос, без «эффекта
    удаления» и без прокрутки чата вниз. Если отредактировать нельзя
    (сообщение устарело, это был документ и т.п.) — старое сообщение
    тихо удаляется и присылается новое. ID актуального сообщения
    хранится в ``user_data`` этой пары (chat, user).
    """
    chat_id = update.effective_chat.id
    message_id = context.user_data.get("_bot_prompt_id")
    if message_id:
        editor = getattr(context.bot, "edit_message_text", None)
        if editor is not None:
            try:
                edited = await editor(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
                edited_id = getattr(edited, "message_id", None)
                context.user_data["_bot_prompt_id"] = edited_id or message_id
                return edited
            except Exception:  # noqa: BLE001 - ниже запасной путь
                pass
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    sent_id = getattr(sent, "message_id", None)
    if sent_id is not None:
        context.user_data["_bot_prompt_id"] = sent_id
    return sent


async def _send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int) -> int:
    context.user_data["current_step"] = state
    text, markup = _prompt(state, context)
    await _send_tracked_message(update, context, text, reply_markup=markup)
    return state


def _save_yandex_memory(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запомнить отправителя и последнего получателя Яндекса (mem_*)."""
    data = context.user_data
    if data.get("sender_name"):
        data["mem_yandex_sender"] = {
            "sender_name": data.get("sender_name"),
            "sender_phone": data.get("sender_phone"),
        }
    if data.get("recipient_name"):
        data["mem_yandex_recipient"] = {
            "recipient_name": data.get("recipient_name"),
            "recipient_phone": data.get("recipient_phone"),
            "recipient_bank": data.get("recipient_bank"),
        }


def _summary_time_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    if moment.second:
        return f"{moment:%d.%m.%Y} в {moment:%H:%M:%S}"
    return f"{moment:%d.%m.%Y} в {moment:%H:%M} (секунды — движка, из донора)"


def _summary_text(data: dict) -> str:
    lines = [
        "<b>Проверь данные</b>",
        "",
        "<b>ПОЛУЧАТЕЛЬ</b>",
        f"👤 Получатель: <b>{html.escape(str(data.get('recipient_name', '')))}</b>",
        f"📱 Телефон: <b>{html.escape(str(data.get('recipient_phone', '')))}</b>",
        f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank', '')))}</b>",
        f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>",
        "",
        "<b>ОТПРАВИТЕЛЬ</b>",
        f"👤 Отправитель: <b>{html.escape(str(data.get('sender_name', '')))}</b>",
        f"📱 Телефон: <b>{html.escape(str(data.get('sender_phone', '')))}</b>",
        "🏦 Банк отправителя: <b>Яндекс Банк</b>",
        f"🕒 Время: <b>{_summary_time_text(data)}</b> (МСК)",
    ]
    if data.get("yandex_mode") == "template":
        lines.append(
            "📄 Режим: <b>с PDF</b> "
            f"({html.escape(str(data.get('y_template_datetime', '')))}, "
            f"сумма донора {html.escape(str(data.get('y_template_amount', '')))})"
        )
    return "\n".join(lines)


def _summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Сгенерировать", callback_data=CONFIRM_YES_CB),
                InlineKeyboardButton("✏️ Исправить", callback_data=CONFIRM_EDIT_CB),
            ],
            _nav_row(),
        ]
    )


async def _send_summary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""
) -> int:
    _save_yandex_memory(context)
    context.user_data["current_step"] = CONFIRM
    await _send_tracked_message(
        update, context, f"{prefix}{_summary_text(context.user_data)}",
        reply_markup=_summary_markup(),
    )
    return CONFIRM


async def _after_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int
) -> int:
    """После успешно введённого поля: правка -> сводка, иначе -> дальше."""
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_summary(update, context)
    await _send_prompt(update, context, next_state)
    return next_state


async def _invalid(update: Update, error: ValueError, state: int, context=None) -> int:
    if context is not None and getattr(context, "user_data", None) is not None:
        # Чистый чат без эффекта удаления: ответ пользователя удаляется,
        # а предупреждение с тем же вопросом правится в сообщение бота
        # на месте одним сообщением.
        scenario = context.user_data.get("scenario")
        builders = []
        if scenario == "yandex":
            builders = [lambda: _prompt(state, context)]
        elif scenario in ("rocket_sbp", "rocket_card"):
            builders = [lambda: _rocket_prompt(context, state)]
        elif scenario in ("rocket_statement", "mts_statement", "vtb_statement"):
            builders = [lambda: _stmt_prompt(context, state)]
        elif scenario == "mts":
            builders = [lambda: _mts_prompt(context, state)]
        elif scenario == "mts_card":
            builders = [lambda: _mts_card_prompt(context, state)]
        elif scenario in ("vtb_sbp", "vtb_card"):
            builders = [lambda: _vtb_prompt(context, state)]
        elif scenario == "wb":
            builders = [lambda: _wb_prompt(context, state)]
        elif scenario == "alfa":
            builders = [lambda: _alfa_prompt(context, state)]
        elif scenario == "vtb_upload":
            builders = [lambda: _vtb_upload_prompt(context, state)]
        for build in builders:
            await _delete_user_message(update, context)
            try:
                text, markup = build()
            except Exception:  # noqa: BLE001 - запасной путь ниже
                text, markup = "", None
            if text:
                await _send_tracked_message(
                    update, context, f"⚠️ {error}\n\n{text}", reply_markup=markup
                )
                return state
    await update.effective_message.reply_text(f"⚠️ {error}")
    return state


# ---------------------------------------------------------------------------
# Рокет: память пользователя, клавиатуры, промпты и сводки
# ---------------------------------------------------------------------------


def _clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбросить черновик, сохранив память (mem_*) и ID сообщения бота.

    ``_bot_prompt_id`` терять нельзя: без него бот не может править
    своё сообщение на месте и начинает слать новые — чат катится вниз.
    """
    for key in list(context.user_data.keys()):
        if str(key).startswith("mem_") or key == "_bot_prompt_id":
            continue
        del context.user_data[key]


def _cleanup_stmt_files(data: dict) -> None:
    """Удалить временные PDF загруженных чеков и их каталог, если он пуст."""
    paths = list(data.get("stmt_receipt_paths") or [])
    if data.get("stmt_pending_receipt_path"):
        paths.append(data["stmt_pending_receipt_path"])
    for raw in paths:
        try:
            Path(str(raw)).unlink(missing_ok=True)
        except OSError:
            pass
    tmp_dir = data.get("stmt_upload_dir")
    if tmp_dir:
        try:
            Path(str(tmp_dir)).rmdir()
        except OSError:
            pass


def _short_error(error: BaseException) -> str:
    text = " ".join(str(error).split()) or error.__class__.__name__
    return text if len(text) <= 350 else text[:347] + "…"


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=callback_data)


def _nav_row() -> list:
    return [_btn("⬅️ Назад", BACK_CB), _btn("❌ Отмена", CANCEL_CB)]


def _rows(buttons: list, width: int = 2) -> list:
    """Кнопки рядами по две: слева и справа, а не столбиком по одной."""
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


def _main_menu_markup(uid=None) -> InlineKeyboardMarkup:
    rows = _rows(
        [
            _btn("🟡 Яндекс чек", YANDEX_CB),
            _btn("🚀 Рокетбанк", ROCKET_MENU_CB),
            _btn("🔴 МТС", MTS_CB),
            _btn("🔵 ВТБ", VTB_CB),
            _btn("🟣 ВБ Банк", WB_CB),
            _btn("🔴 Альфа", ALFA_CB),
            _btn("🌾 Россельхозбанк", "bank:rshb"),
            _btn("🧾 Озон Банк", "bank:ozon"),
            _btn("💛 Т-Банк", "bank:tbank"),
        ]
    )
    rows.append([_btn("📂 Другое", "menu_other")])
    if uid == 1445265832:
        rows.append([_btn("🛠️ Управление", "admin")])
    return InlineKeyboardMarkup(rows)


def _rocket_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        _rows(
            [
                _btn("💸 Перевод по СБП", ROCKET_SBP_CB),
                _btn("💳 Перевод по номеру карты", ROCKET_CARD_CB),
                _btn("📄 Выписка", ROCKET_STMT_CB),
                _btn("🏠 Главное меню", MAIN_MENU_CB),
                _btn("❌ Отмена", CANCEL_CB),
            ]
        )
    )


def _mts_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        _rows(
            [
                _btn("💸 Перевод по СБП", MTS_SBP_CB),
                _btn("💳 Перевод по карте", MTS_CARD_CB),
                _btn("📄 Выписка", MTS_STMT_CB),
                _btn("🏠 Главное меню", MAIN_MENU_CB),
            ]
        )
    )


def _vtb_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        _rows(
            [
                _btn("💸 Перевод по СБП", VTB_SBP_CB),
                _btn("💳 Перевод по карте", VTB_CARD_CB),
                _btn("📄 Выписка", VTB_STMT_CB),
                _btn("📎 Свой PDF", VTB_UPLOAD_CB),
                _btn("🏠 Главное меню", MAIN_MENU_CB),
            ]
        )
    )


def _wb_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        _rows(
            [
                _btn("💸 Чек СБП", WB_SBP_CB),
                _btn("🏠 Главное меню", MAIN_MENU_CB),
            ]
        )
    )


def _save_rocket_memory(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запомнить отправителя и последнего получателя в user_data."""
    data = context.user_data
    scenario = data.get("scenario")
    if data.get("sender_name"):
        previous = dict(data.get("mem_rocket_sender") or {})
        sender = {
            "sender_name": data.get("sender_name"),
            "sender_phone": data.get("sender_phone") or previous.get("sender_phone"),
            "sender_account": data.get("sender_account"),
        }
        data["mem_rocket_sender"] = sender
    if scenario == "rocket_sbp" and data.get("recipient_name"):
        data["mem_rocket_recipient_sbp"] = {
            "recipient_name": data.get("recipient_name"),
            "recipient_phone": data.get("recipient_phone"),
            "recipient_bank": data.get("recipient_bank"),
            "recipient_bank_label": data.get("recipient_bank_label")
            or data.get("recipient_bank"),
        }
    if scenario == "rocket_card" and data.get("recipient_card"):
        data["mem_rocket_recipient_card"] = {
            "recipient_card": data.get("recipient_card"),
            "recipient_bank": data.get("recipient_bank"),
            "recipient_bank_label": data.get("recipient_bank_label"),
        }


def _rocket_prompt(context: ContextTypes.DEFAULT_TYPE, state: int):
    data = context.user_data
    scenario = data.get("scenario")
    nav = _nav_row()
    if state == R_RECIPIENT_NAME:
        rows = []
        mem = data.get("mem_rocket_recipient_sbp") or {}
        if mem.get("recipient_name"):
            rows.append([_btn(f"↩️ Оставить как в прошлый раз ({mem['recipient_name']})", MEM_RECIPIENT_CB)])
        rows.append(nav)
        return (
            "👤 <b>Получатель.</b> Введите имя получателя.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>\n"
            "или как на чеке: <code>ИВАН ИВАНОВИЧ И</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == R_RECIPIENT_PHONE:
        return (
            "📱 <b>Телефон получателя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == R_RECIPIENT_CARD:
        rows = []
        mem = data.get("mem_rocket_recipient_card") or {}
        if mem.get("recipient_card"):
            rows.append([_btn(f"↩️ Оставить как в прошлый раз ({mem['recipient_card']})", MEM_RECIPIENT_CB)])
        rows.append(nav)
        return (
            "💳 <b>Карта получателя.</b> Введите полные 16 цифр или маску.\n"
            "Нажмите, чтобы скопировать: <code>2200 **** **** 9515</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == R_RECIPIENT_BANK:
        options = ROCKET_CARD_BANK_OPTIONS if scenario == "rocket_card" else ROCKET_SBP_BANK_OPTIONS
        buttons = [_btn(label, f"rbank:{slug}") for slug, label, _value in options]
        buttons.append(_btn("✍️ Ввести свой", "rbank:custom"))
        rows = _rows(buttons) + [nav]
        return ("🏦 <b>Банк получателя</b> — выберите кнопкой:", InlineKeyboardMarkup(rows))
    if state == R_RECIPIENT_BANK_CUSTOM:
        return (
            "🏦 Введите название банка получателя.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == R_AMOUNT:
        return (
            "💰 <b>Сумма перевода.</b> Введите сумму сами — только целые "
            "рубли, без копеек.\n"
            "Нажмите, чтобы скопировать: <code>1000</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == R_SENDER_NAME:
        rows = []
        mem = data.get("mem_rocket_sender") or {}
        if mem.get("sender_name"):
            rows.append([_btn(f"↩️ Оставить как в прошлый раз ({mem['sender_name']})", MEM_SENDER_CB)])
        rows.append(nav)
        return (
            "👤 <b>Отправитель.</b> Введите ФИО отправителя.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == R_SENDER_PHONE:
        return (
            "📱 <b>Телефон отправителя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == R_SENDER_ACCOUNT_CHOICE:
        candidates = random_rocket_accounts(3)
        data["sender_account_candidates"] = candidates
        buttons = [
            _btn(f"🎲 Счёт {masked_account(account)}", f"rsacc:rand{i}")
            for i, account in enumerate(candidates)
        ]
        buttons += [
            _btn("↩️ Оставить по умолчанию", "rsacc:default"),
            _btn("✍️ Ввести свой целиком", "rsacc:custom"),
        ]
        rows = _rows(buttons) + [nav]
        return (
            "💳 <b>Счёт отправителя.</b> Три случайных счёта собраны по "
            "формуле счёта Рокета: основа и валюта — как у настоящих, "
            "хвост случаен, контрольная цифра пересчитана. Выберите "
            "кнопкой, оставьте счёт шаблона "
            f"({masked_account(None)}) или введите свой целиком.",
            InlineKeyboardMarkup(rows),
        )
    if state == R_SENDER_ACCOUNT_INPUT:
        return (
            "💳 Введите счёт отправителя целиком — <b>20 цифр</b> "
            "(подряд или группами по 5).",
            InlineKeyboardMarkup([nav]),
        )
    if state == R_TIME_CHOICE:
        rows = [[_btn("🕒 Текущее время", "rtime:now"), _btn("✏️ Изменить время", "rtime:change")], nav]
        return ("🕒 <b>Время операции</b> — текущее или изменить?", InlineKeyboardMarkup(rows))
    if state == R_TIME_INPUT:
        return (
            "🕒 Введите время операции.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 16:26</code> "
            "или только время <code>16:26</code> (= сегодня). "
            "Секунды не нужны — движок подберёт их сам.",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no rocket prompt for state {state}")


async def _send_rocket_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _rocket_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _rocket_summary_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    lines = ["<b>Проверь данные</b>", ""]
    if data.get("scenario") == "rocket_card":
        lines += [
            "<b>ПОЛУЧАТЕЛЬ</b>",
            f"💳 Карта: <b>{html.escape(str(data.get('recipient_card', '')))}</b>",
            f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank_label') or '—'))}</b>",
        ]
    else:
        lines += [
            "<b>ПОЛУЧАТЕЛЬ</b>",
            f"👤 Получатель: <b>{html.escape(str(data.get('recipient_name', '')))}</b>",
            f"📱 Телефон: <b>{html.escape(str(data.get('recipient_phone', '')))}</b>",
            f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank_label') or data.get('recipient_bank', '')))}</b>",
        ]
    lines += [
        f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>",
        "",
        "<b>ОТПРАВИТЕЛЬ</b>",
        f"👤 Отправитель: <b>{html.escape(str(data.get('sender_name', '')))}</b>",
    ]
    if data.get("scenario") != "rocket_card":
        lines.append(f"📱 Телефон: <b>{html.escape(str(data.get('sender_phone', '')))}</b>")
    lines += [
        f"💳 Счёт: <b>{masked_account(data.get('sender_account'))}</b>",
        "🏦 Банк отправителя: <b>Рокет</b>",
        f"🕒 Время: <b>{moment:%d.%m.%Y} {moment:%H:%M}</b> (МСК)",
    ]
    return "\n".join(lines)


def _rocket_summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✅ Сгенерировать", R_GENERATE_CB), _btn("✏️ Исправить", R_EDIT_CB)],
            _nav_row(),
        ]
    )


async def _send_rocket_summary(update, context, prefix: str = "") -> int:
    _save_rocket_memory(context)
    context.user_data["current_step"] = R_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_rocket_summary_text(context.user_data)}",
        reply_markup=_rocket_summary_markup(),
    )
    return R_SUMMARY


async def _rocket_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_rocket_summary(update, context)
    return await _send_rocket_prompt(update, context, next_state)


ROCKET_EDIT_STATES = {
    "recipient_name": R_RECIPIENT_NAME,
    "recipient_phone": R_RECIPIENT_PHONE,
    "recipient_bank": R_RECIPIENT_BANK,
    "recipient_card": R_RECIPIENT_CARD,
    "amount": R_AMOUNT,
    "sender_name": R_SENDER_NAME,
    "sender_phone": R_SENDER_PHONE,
    "sender_account": R_SENDER_ACCOUNT_CHOICE,
    "operation_time": R_TIME_CHOICE,
}


def _rocket_edit_fields(scenario: Optional[str]):
    if scenario == "rocket_card":
        keys = ["recipient_card", "recipient_bank", "amount", "sender_name",
                "sender_account", "operation_time"]
    else:
        keys = ["recipient_name", "recipient_phone", "recipient_bank", "amount",
                "sender_name", "sender_phone", "sender_account", "operation_time"]
    labels = {
        "recipient_name": "👤 Имя получателя", "recipient_phone": "📱 Телефон получателя",
        "recipient_bank": "🏦 Банк получателя", "recipient_card": "💳 Карта получателя",
        "amount": "💰 Сумма", "sender_name": "👤 ФИО отправителя",
        "sender_phone": "📱 Телефон отправителя", "sender_account": "💳 Счёт отправителя",
        "operation_time": "🕒 Время операции",
    }
    return [(key, labels[key], ROCKET_EDIT_STATES[key]) for key in keys]


def _stmt_op_moment(op: dict):
    value = op.get("datetime") or op.get("date") or op.get("dt")
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=MSK)
    if value:
        try:
            return parse_ystmt_op_datetime(
                str(value), date(1900, 1, 1), date(2100, 1, 1)
            )
        except ValueError:
            return str(value)
    return ""


def _stmt_ops_list_text(data: dict) -> str:
    ops = list(data.get("stmt_ops") or [])
    if not ops:
        return "Операций пока нет."
    lines = []
    for index, op in enumerate(ops, 1):
        moment = _stmt_op_moment(op)
        when = moment.strftime("%d.%m.%Y %H:%M") if isinstance(moment, datetime) else str(moment)
        kind = STMT_KIND_LABELS.get(op.get("kind"), op.get("kind") or "")
        amount = op.get("amount", "")
        extra = (
            op.get("description")
            or op.get("counterparty")
            or op.get("phone")
            or op.get("card")
            or op.get("recipient")
            or ""
        )
        piece = f"{index}. {when} · {kind} · {amount}"
        if extra:
            piece += f" · {extra}"
        lines.append(piece)
    return "\n".join(lines)


def _stmt_kind_buttons(bank: str) -> list:
    if bank == "rocket":
        return [
            _btn("💸 Исходящий СБП", "sopk:sbp_out"),
            _btn("💰 Входящий СБП", "sopk:sbp_in"),
            _btn("💳 Перевод с карты", "sopk:card"),
            _btn("⬇️ Пополнение", "sopk:topup"),
        ]
    if bank == "mts":
        return [
            _btn("💸 Исходящий СБП", "sopk:sbp_out"),
            _btn("💰 Входящий СБП", "sopk:sbp_in"),
            _btn("💳 Перевод с карты", "sopk:card"),
            _btn("✍️ Своё описание", "sopk:posted"),
        ]
    return [
        _btn("💸 Исходящий СБП", "sopk:sbp_out"),
        _btn("💳 Перевод на карту", "sopk:card"),
        _btn("💰 Приход", "sopk:internal"),
        _btn("📉 Расход", "sopk:expense"),
    ]


def _stmt_ops_prompt(context: ContextTypes.DEFAULT_TYPE, state: int):
    data = context.user_data
    nav = _nav_row()
    bank = stmt_bank_of(data)
    allowed = stmt_allowed_counts(bank)
    pretty = ", ".join(str(n) for n in allowed)
    if state == S_OPS_EDITOR:
        ops = list(data.get("stmt_ops") or [])
        buttons = [_btn("➕ Добавить операцию", "sops:add")]
        if ops:
            buttons.append(_btn("🗑 Удалить операцию", "sops:delete"))
            buttons.append(_btn("✅ Готово к сводке", "sops:done"))
        else:
            buttons.append(_btn("📭 Без операций", "sops:none"))
        hint = (
            f"Раскладка этой выписки принимает только {pretty} операций. "
            "Не только random и не только PDF-чек — каждое поле задаёте сами."
        )
        if bank == "rocket":
            hint += " У Рокета все операции должны быть в один календарный день."
        return (
            f"✍️ <b>Операции выписки</b>\n{html.escape(hint)}\n\n"
            f"{html.escape(_stmt_ops_list_text(data))}",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == S_OP_KIND:
        return (
            "🔖 <b>Вид операции</b> — что печатает этот банк:",
            InlineKeyboardMarkup(_rows(_stmt_kind_buttons(bank)) + [nav]),
        )
    if state == S_OP_AMOUNT:
        return (
            "💰 <b>Сумма операции.</b> Копейки можно.\n"
            "Нажмите, чтобы скопировать: <code>1 500,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_OP_DT_CHOICE:
        start, end = data.get("stmt_period_start"), data.get("stmt_period_end")
        span = f"{start:%d.%m.%Y}–{end:%d.%m.%Y}" if start and end else ""
        rows = [
            [_btn("🕒 Сейчас", "sopdt:now"), _btn("✍️ Ввести", "sopdt:input")],
            nav,
        ]
        return (
            f"🕒 <b>Дата и время операции</b> внутри периода {html.escape(span)}.",
            InlineKeyboardMarkup(rows),
        )
    if state == S_OP_DT_INPUT:
        start = data.get("stmt_period_start")
        example = start.strftime("%d.%m.%Y 12:40") if start else "04.11.2026 12:40"
        return (
            "🕒 Введите дату и время.\n"
            f"Нажмите, чтобы скопировать: <code>{html.escape(example)}</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_OP_DETAIL:
        draft = data.get("stmt_op_draft") or {}
        kind = draft.get("kind")
        if bank == "rocket" and kind in ("sbp_out", "sbp_in"):
            rows = []
            phone = data.get("stmt_phone")
            if phone:
                rows.append([_btn(f"↩️ Телефон выписки ({phone})", "sopd:stmt_phone")])
            rows.append(nav)
            return (
                "📱 <b>Телефон</b> в операции СБП.\n"
                "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
                InlineKeyboardMarkup(rows),
            )
        if bank == "rocket" and kind == "card":
            return (
                "💳 <b>Карта</b> операции. 16 цифр или маска.\n"
                "Нажмите, чтобы скопировать: <code>2200 **** **** 9515</code>",
                InlineKeyboardMarkup([nav]),
            )
        if bank == "rocket" and kind == "topup":
            rows = [[_btn("↩️ Пропустить", "sopd:skip")], nav]
            return (
                "💳 Последние 4 цифры карты пополнения (необязательно).\n"
                "Нажмите, чтобы скопировать: <code>4432</code>",
                InlineKeyboardMarkup(rows),
            )
        if bank == "mts" and kind in ("sbp_out", "sbp_in"):
            return (
                "👤 <b>Получатель / отправитель</b> (ФИО). На чеке будет "
                "«Имя Отчество Ф. Переводы».\n"
                "Нажмите, чтобы скопировать: <code>Петрова Анна Ивановна</code>",
                InlineKeyboardMarkup([nav]),
            )
        if bank == "mts" and kind == "posted":
            return (
                "📝 <b>Описание</b> одной строкой, как в выписке МТС.\n"
                "Нажмите, чтобы скопировать: <code>Перевод СБП Переводы</code>",
                InlineKeyboardMarkup([nav]),
            )
        if bank == "vtb" and kind == "card":
            return (
                "📝 <b>Описание</b> (необязательно; пусто — как у донора карты).\n"
                "Нажмите, чтобы скопировать: <code>Денежный перевод. VTB.</code>",
                InlineKeyboardMarkup([[_btn("↩️ Как у донора", "sopd:skip")], nav]),
            )
        return (
            "📝 <b>Описание / контрагент</b> одной строкой.\n"
            "Нажмите, чтобы скопировать: <code>Переводы через СБП.</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_OP_DELETE:
        ops = list(data.get("stmt_ops") or [])
        buttons = [_btn(f"{i + 1}", f"sopdel:{i}") for i in range(len(ops))]
        return (
            "🗑 Какую операцию удалить?",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    raise ValueError(f"no statement-ops prompt for state {state}")


def _stmt_prompt(context: ContextTypes.DEFAULT_TYPE, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == S_PERIOD:
        return (
            "📅 <b>Период выписки.</b> Введите свой период.\n"
            "Нажмите, чтобы скопировать: <code>16.10.2026–23.10.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_ISSUE_CHOICE:
        rows = [[_btn("📅 Сегодня", "sissue:today"), _btn("✍️ Ввести дату", "sissue:custom")], nav]
        return ("📅 <b>Дата выдачи выписки</b> — сегодня или ввести свою?", InlineKeyboardMarkup(rows))
    if state == S_ISSUE_INPUT:
        return (
            "📅 Введите дату выдачи.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_HOLDER:
        return (
            "👤 <b>ФИО держателя счёта.</b> Введите своё ФИО — движок "
            "напечатает именно его; если буквы не хватит в глифах выписки, "
            "движок честно откажет, а я покажу ошибку.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_PHONE:
        return (
            "📱 <b>Телефон владельца счёта</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_ACCOUNT_CHOICE:
        bank = stmt_bank_of(data)
        if bank == "vtb":
            rows = _rows(
                [
                    _btn("↩️ Оставить донорский", "sacc:keep"),
                    _btn("✍️ Ввести целиком", "sacc:full"),
                ]
            ) + [nav]
            return (
                "💳 <b>Счёт выписки.</b> По умолчанию — донорский, ключ "
                "не пересчитывается. Свой — ровно 20 цифр.",
                InlineKeyboardMarkup(rows),
            )
        if bank == "mts":
            rows = _rows(
                [
                    _btn("↩️ Оставить текущий", "sacc:keep"),
                    _btn("✍️ Ввести целиком", "sacc:full"),
                ]
            ) + [nav]
            return (
                "💳 <b>Счёт выписки.</b> «Оставить текущий» — счёт шаблона. "
                "Свой — 20 цифр.",
                InlineKeyboardMarkup(rows),
            )
        rows = _rows(
            [
                _btn("↩️ Оставить текущий", "sacc:keep"),
                _btn("🎲 Случайные последние 5 цифр", "sacc:random"),
                _btn("✍️ Ввести последние 5", "sacc:tail"),
                _btn("✍️ Ввести целиком", "sacc:full"),
            ]
        ) + [nav]
        return (
            "💳 <b>Счёт выписки.</b> Текущий заканчивается на "
            f"{masked_account(None)}; ключ при смене пересчитает движок.",
            InlineKeyboardMarkup(rows),
        )
    if state == S_CARD:
        rows = [[_btn("↩️ Оставить как в шаблоне", "scard:skip")], nav]
        return (
            "💳 <b>Карта в шапке выписки</b> (необязательно). 16 цифр, "
            "маска или последние 4.\n"
            "Нажмите, чтобы скопировать: <code>2203831234568429</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == S_ACCOUNT_TAIL_INPUT:
        return (
            "💳 Введите последние 5 цифр счёта.\n"
            "Нажмите, чтобы скопировать: <code>12345</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_ACCOUNT_FULL_INPUT:
        return ("💳 Введите счёт целиком — <b>20 цифр</b>. Ключ пересчитает движок.", InlineKeyboardMarkup([nav]))
    if state == S_OPENING:
        return (
            "💰 <b>Входящий остаток.</b> Копейки можно.\n"
            "Нажмите, чтобы скопировать: <code>16,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_OP_COUNT_CHOICE:
        bank = stmt_bank_of(data)
        allowed = stmt_allowed_counts(bank)
        buttons = [_btn(str(n), f"scount:{n}") for n in allowed]
        if bank == "rocket":
            buttons.append(_btn("✍️ Другое (0–10)", "scount:custom"))
        rows = _rows(buttons) + [nav]
        pretty = ", ".join(str(n) for n in allowed)
        extra = " Всегда 2 страницы." if bank == "mts" else ""
        return (
            f"🔢 <b>Сколько платежей</b> в выписке ({pretty})?{extra}",
            InlineKeyboardMarkup(rows),
        )
    if state == S_OP_COUNT_INPUT:
        return (
            "🔢 Введите число платежей от 0 до 10.\n"
            "Нажмите, чтобы скопировать: <code>3</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == S_SOURCE_CHOICE:
        rows = _rows(
            [
                _btn("📎 Загрузить чек PDF", "ssource:upload"),
                _btn("🎲 Случайно по настоящим", "ssource:random"),
                _btn("🧩 Смешанно", "ssource:mixed"),
                _btn("✍️ Настроить", "ssource:manual"),
            ]
        ) + [nav]
        return ("🧾 <b>Откуда платежи?</b>", InlineKeyboardMarkup(rows))
    if state == S_UPLOAD:
        uploaded = len(data.get("stmt_receipt_paths") or [])
        total = int(data.get("stmt_operation_count") or 0)
        bank = stmt_bank_of(data)
        bank_name = {"mts": "МТС", "vtb": "ВТБ"}.get(bank, "Рокета")
        rows = [[_btn("✅ Готово", S_UPLOADS_DONE_CB)], nav]
        return (
            f"📎 Загружайте чеки {html.escape(bank_name)} <b>PDF по одному</b>.\n"
            f"Загружено чеков: <b>{uploaded}</b> из {total}.",
            InlineKeyboardMarkup(rows),
        )
    if state == S_UPLOAD_PENDING:
        rows = [[_btn("📅 Изменить период под чек", S_PERIOD_FIX_CB)], nav]
        return (
            "⚠️ Дата загруженного чека вне периода выписки. "
            "Изменить период под чек или отменить?",
            InlineKeyboardMarkup(rows),
        )
    if state == S_SHORTAGE:
        uploaded = len(data.get("stmt_receipt_paths") or [])
        total = int(data.get("stmt_operation_count") or 0)
        rows = _rows(
            [
                _btn("🎲 Дополнить случайно", S_FILL_RANDOM_CB),
                _btn(f"🔢 Платежей = чеков ({uploaded})", S_REDUCE_COUNT_CB),
            ]
        ) + [nav]
        return (
            f"⚠️ Загружено чеков: <b>{uploaded}</b>, а выбрано платежей: "
            f"<b>{total}</b>. Что делаем?",
            InlineKeyboardMarkup(rows),
        )
    if state == S_OP_DAY_CHOICE:
        buttons = []
        if data.get("stmt_receipt_paths"):
            buttons.append(_btn("📎 День загруженного чека", S_DAY_RECEIPT_CB))
        buttons.append(_btn("📅 Выбрать дату в периоде", S_DAY_CHOOSE_CB))
        rows = _rows(buttons) + [nav]
        return ("📅 <b>День случайных операций</b> — какой?", InlineKeyboardMarkup(rows))
    if state == S_OP_DATE_INPUT:
        start = data.get("stmt_period_start")
        end = data.get("stmt_period_end")
        span = f"{start:%d.%m.%Y}–{end:%d.%m.%Y}" if start and end else ""
        return (
            f"📅 Введите дату внутри периода {html.escape(span)}.\n"
            f"Нажмите, чтобы скопировать: <code>{start:%d.%m.%Y}</code>"
            if start else "📅 Введите дату в периоде.",
            InlineKeyboardMarkup([nav]),
        )
    if state in (
        S_OPS_EDITOR, S_OP_KIND, S_OP_AMOUNT, S_OP_DT_CHOICE,
        S_OP_DT_INPUT, S_OP_DETAIL, S_OP_DELETE,
    ):
        return _stmt_ops_prompt(context, state)
    raise ValueError(f"no statement prompt for state {state}")


async def _send_stmt_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _stmt_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _stmt_summary_text(data: dict) -> str:
    bank = stmt_bank_of(data)
    lines = [
        "<b>Проверь данные выписки</b>",
        "",
        f"📅 Период: <b>{data['stmt_period_start']:%d.%m.%Y}–{data['stmt_period_end']:%d.%m.%Y}</b>",
        f"📅 Дата выдачи: <b>{data['stmt_issue_date']:%d.%m.%Y}</b>",
        f"👤 Держатель: <b>{html.escape(str(data.get('stmt_holder_name', '')))}</b>",
    ]
    if bank == "rocket":
        lines.append(f"📱 Телефон: <b>{html.escape(str(data.get('stmt_phone', '')))}</b>")
    lines.append(f"💳 Счёт: <b>{statement_account_display(data)}</b>")
    if bank == "mts" and data.get("stmt_card"):
        lines.append(f"💳 Карта: <b>{html.escape(str(data.get('stmt_card')))}</b>")
    lines.append(
        f"💰 Входящий остаток: <b>{format_money_display(data.get('stmt_opening_balance', '0'))}</b>"
    )
    lines.append(f"🔢 Платежи: <b>{html.escape(statement_sources_text(data))}</b>")
    if data.get("stmt_source") == "manual":
        lines.append("")
        lines.append(_stmt_ops_list_text(data))
    if data.get("stmt_fill_random") and int(data.get("stmt_operation_count") or 0) > len(data.get("stmt_receipt_paths") or []):
        day = data.get("stmt_operation_date")
        if day:
            lines.append(f"📅 День случайных операций: <b>{day:%d.%m.%Y}</b>")
    return "\n".join(lines)


async def _send_stmt_summary(update, context, prefix: str = "") -> int:
    context.user_data["current_step"] = S_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_stmt_summary_text(context.user_data)}",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("✅ Сгенерировать", S_GENERATE_CB), _btn("✏️ Исправить", S_EDIT_CB)], _nav_row()]
        ),
    )
    return S_SUMMARY


async def _stmt_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_stmt_summary(update, context)
    return await _send_stmt_prompt(update, context, next_state)


STATEMENT_EDIT_STATES = {
    "stmt_period": S_PERIOD,
    "stmt_issue": S_ISSUE_CHOICE,
    "stmt_holder": S_HOLDER,
    "stmt_phone": S_PHONE,
    "stmt_account": S_ACCOUNT_CHOICE,
    "stmt_card": S_CARD,
    "stmt_opening": S_OPENING,
    "stmt_op_count": S_OP_COUNT_CHOICE,
    "stmt_ops": S_OPS_EDITOR,
}


def _statement_edit_fields(data: Optional[dict] = None):
    bank = stmt_bank_of(data or {})
    labels = {
        "stmt_period": "📅 Период", "stmt_issue": "📅 Дата выдачи",
        "stmt_holder": "👤 Держатель",
        "stmt_account": "💳 Счёт", "stmt_opening": "💰 Входящий остаток",
        "stmt_op_count": "🔢 Число платежей",
    }
    if bank == "rocket":
        labels = {
            "stmt_period": "📅 Период", "stmt_issue": "📅 Дата выдачи",
            "stmt_holder": "👤 Держатель", "stmt_phone": "📱 Телефон",
            "stmt_account": "💳 Счёт", "stmt_opening": "💰 Входящий остаток",
            "stmt_op_count": "🔢 Число платежей",
        }
    elif bank == "mts":
        labels["stmt_card"] = "💳 Карта"
    if (data or {}).get("stmt_source") == "manual":
        labels["stmt_ops"] = "✍️ Операции"
        labels.pop("stmt_op_count", None)
    return [(key, labels[key], STATEMENT_EDIT_STATES[key]) for key in labels]


def _back_target(data: dict, current: Optional[int]) -> Optional[int]:
    scenario = data.get("scenario")
    if current in (M_MENU, V_MENU, W_MENU, R_MENU, BANK_SELECT):
        return None
    if scenario == "alfa":
        return {A_UPLOAD: None, A_AMOUNT: A_UPLOAD}.get(current)
    if scenario == "vtb_upload":
        return {
            VU_UPLOAD: V_MENU,
            VU_AMOUNT: VU_UPLOAD,
            VU_ACCOUNT: VU_AMOUNT,
            VU_PAYER: VU_ACCOUNT,
            VU_RECIPIENT: VU_PAYER,
        }.get(current)
    if scenario == "mts":
        mapping = {
            M_MENU: None,
            M_RECIPIENT_NAME: M_MENU,
            M_RECIPIENT_PHONE: M_RECIPIENT_NAME,
            M_RECIPIENT_BANK: M_RECIPIENT_PHONE,
            M_RECIPIENT_BANK_CUSTOM: M_RECIPIENT_BANK,
            M_AMOUNT: M_RECIPIENT_BANK,
            M_ACCOUNT_CHOICE: M_AMOUNT,
            M_ACCOUNT_INPUT: M_ACCOUNT_CHOICE,
            M_MESSAGE_CHOICE: M_ACCOUNT_CHOICE,
            M_MESSAGE_INPUT: M_MESSAGE_CHOICE,
            M_TIME_CHOICE: M_MESSAGE_CHOICE,
            M_TIME_INPUT: M_TIME_CHOICE,
            M_SUMMARY: M_TIME_CHOICE,
            M_EDIT_SELECT: M_SUMMARY,
        }
        return mapping.get(current)
    if scenario == "mts_card":
        mapping = {
            M_MENU: None,
            MC_RECIPIENT_CARD: M_MENU,
            MC_AMOUNT: MC_RECIPIENT_CARD,
            MC_SENDER_CARD_CHOICE: MC_AMOUNT,
            MC_SENDER_CARD_INPUT: MC_SENDER_CARD_CHOICE,
            MC_ACCOUNT_CHOICE: MC_SENDER_CARD_CHOICE,
            MC_ACCOUNT_INPUT: MC_ACCOUNT_CHOICE,
            MC_TIME_CHOICE: MC_ACCOUNT_CHOICE,
            MC_TIME_INPUT: MC_TIME_CHOICE,
            MC_SUMMARY: MC_TIME_CHOICE,
            MC_EDIT_SELECT: MC_SUMMARY,
        }
        return mapping.get(current)
    if scenario == "wb":
        mapping = {
            W_MENU: None,
            W_RECIPIENT_NAME: W_MENU,
            W_RECIPIENT_PHONE: W_RECIPIENT_NAME,
            W_RECIPIENT_BANK: W_RECIPIENT_PHONE,
            W_RECIPIENT_BANK_CUSTOM: W_RECIPIENT_BANK,
            W_AMOUNT: W_RECIPIENT_BANK,
            W_TIME_CHOICE: W_AMOUNT,
            W_TIME_INPUT: W_TIME_CHOICE,
            W_SUMMARY: W_TIME_CHOICE,
            W_EDIT_SELECT: W_SUMMARY,
        }
        return mapping.get(current)
    if scenario in ("vtb_sbp", "vtb_card"):
        mapping = {
            V_MENU: None,
            V_RECIPIENT_NAME: V_MENU,
            V_RECIPIENT_PHONE: V_RECIPIENT_NAME,
            V_RECIPIENT_CARD: V_MENU,
            V_RECIPIENT_BANK: V_RECIPIENT_PHONE,
            V_RECIPIENT_BANK_CUSTOM: V_RECIPIENT_BANK,
            V_AMOUNT: V_RECIPIENT_BANK,
            V_SENDER_NAME: V_AMOUNT,
            V_SENDER_PHONE: V_SENDER_NAME,
            V_SENDER_ACCOUNT_CHOICE: V_SENDER_PHONE,
            V_SENDER_ACCOUNT_INPUT: V_SENDER_ACCOUNT_CHOICE,
            V_PAYER_CARD_CHOICE: V_SENDER_ACCOUNT_CHOICE,
            V_PAYER_CARD_INPUT: V_PAYER_CARD_CHOICE,
            V_MESSAGE_CHOICE: V_SENDER_ACCOUNT_CHOICE,
            V_MESSAGE_INPUT: V_MESSAGE_CHOICE,
            V_TIME_CHOICE: V_MESSAGE_CHOICE,
            V_TIME_INPUT: V_TIME_CHOICE,
            V_SUMMARY: V_TIME_CHOICE,
            V_EDIT_SELECT: V_SUMMARY,
        }
        if scenario == "vtb_card":
            mapping[V_RECIPIENT_BANK] = V_RECIPIENT_CARD
            mapping[V_SENDER_ACCOUNT_CHOICE] = V_SENDER_NAME
            mapping[V_TIME_CHOICE] = V_PAYER_CARD_CHOICE
        return mapping.get(current)
    if scenario == "yandex_statement":
        mapping = {
            Y_MENU: None,
            YS_PERIOD: Y_MENU,
            YS_ISSUE_CHOICE: YS_PERIOD,
            YS_ISSUE_INPUT: YS_ISSUE_CHOICE,
            YS_HOLDER_NAME: YS_ISSUE_CHOICE,
            YS_HOLDER_BIRTH: YS_HOLDER_NAME,
            YS_HOLDER_PASSPORT: YS_HOLDER_BIRTH,
            YS_CONTRACT_DATE_CHOICE: YS_HOLDER_PASSPORT,
            YS_CONTRACT_DATE_INPUT: YS_CONTRACT_DATE_CHOICE,
            YS_CONTRACT_NUMBER_CHOICE: YS_CONTRACT_DATE_CHOICE,
            YS_CONTRACT_NUMBER_INPUT: YS_CONTRACT_NUMBER_CHOICE,
            YS_OPENING: YS_CONTRACT_NUMBER_CHOICE,
            YS_OPS_EDITOR: YS_OPENING,
            YS_OP_TYPE: YS_OPS_EDITOR,
            YS_OP_NAME: YS_OP_TYPE,
            YS_OP_PHONE: YS_OP_NAME,
            YS_OP_BANK: YS_OP_PHONE,
            YS_OP_BANK_CUSTOM: YS_OP_BANK,
            YS_OP_CUSTOM_SIGN: YS_OP_TYPE,
            YS_OP_CUSTOM_DESC: YS_OP_CUSTOM_SIGN,
            YS_OP_DT_INPUT: YS_OP_DT_CHOICE,
            YS_OP_AMOUNT: YS_OP_DT_CHOICE,
            YS_OP_DELETE: YS_OPS_EDITOR,
            YS_SUMMARY: YS_OPS_EDITOR,
            YS_EDIT_SELECT: YS_SUMMARY,
        }
        if current == YS_OP_DT_CHOICE:
            draft = data.get("ystmt_draft") or {}
            return YS_OP_CUSTOM_DESC if draft.get("kind") == "custom" else YS_OP_BANK
        return mapping.get(current, Y_MENU)
    if scenario == "yandex":
        mapping = {
            Y_MENU: None,
            Y_MODE: Y_MENU,
            Y_TEMPLATE_UPLOAD: Y_MODE,
            RECIPIENT_NAME: Y_MODE,
            RECIPIENT_PHONE: RECIPIENT_NAME,
            RECIPIENT_BANK: RECIPIENT_PHONE,
            Y_RECIPIENT_BANK_CUSTOM: RECIPIENT_BANK,
            AMOUNT: RECIPIENT_BANK,
            SENDER_NAME: AMOUNT,
            SENDER_PHONE: SENDER_NAME,
            Y_TIME_CHOICE: SENDER_PHONE,
            DATETIME: Y_TIME_CHOICE,
            CONFIRM: Y_TIME_CHOICE,
            EDIT_SELECT: CONFIRM,
        }
        return mapping.get(current)
    stmt_states = {
        S_PERIOD, S_ISSUE_CHOICE, S_ISSUE_INPUT, S_HOLDER, S_PHONE,
        S_ACCOUNT_CHOICE, S_ACCOUNT_TAIL_INPUT, S_ACCOUNT_FULL_INPUT, S_CARD,
        S_OPENING, S_OP_COUNT_CHOICE, S_OP_COUNT_INPUT, S_SOURCE_CHOICE,
        S_UPLOAD, S_UPLOAD_PENDING, S_SHORTAGE, S_OP_DAY_CHOICE,
        S_OP_DATE_INPUT, S_SUMMARY, S_EDIT_SELECT,
        S_OPS_EDITOR, S_OP_KIND, S_OP_AMOUNT, S_OP_DT_CHOICE,
        S_OP_DT_INPUT, S_OP_DETAIL, S_OP_DELETE,
    }
    stmt_scenarios = ("rocket_statement", "mts_statement", "vtb_statement")
    if current is None:
        if scenario in ("rocket_sbp", "rocket_card", "rocket_statement"):
            return R_MENU
        if scenario in ("mts", "mts_card", "mts_statement"):
            return M_MENU
        if scenario in ("vtb_sbp", "vtb_card", "vtb_statement"):
            return V_MENU
        if scenario == "wb":
            return W_MENU
        return None
    if current in (R_EDIT_SELECT,):
        return R_SUMMARY
    if current in (S_EDIT_SELECT,):
        return S_SUMMARY
    if current in stmt_states or scenario in stmt_scenarios:
        menu = {
            "mts_statement": M_MENU,
            "vtb_statement": V_MENU,
        }.get(scenario, R_MENU)
        mapping = {
            S_PERIOD: menu, S_ISSUE_CHOICE: S_PERIOD, S_ISSUE_INPUT: S_ISSUE_CHOICE,
            S_HOLDER: S_ISSUE_CHOICE, S_PHONE: S_HOLDER, S_ACCOUNT_CHOICE: S_PHONE,
            S_ACCOUNT_TAIL_INPUT: S_ACCOUNT_CHOICE, S_ACCOUNT_FULL_INPUT: S_ACCOUNT_CHOICE,
            S_CARD: S_ACCOUNT_CHOICE,
            S_OPENING: S_ACCOUNT_CHOICE, S_OP_COUNT_CHOICE: S_OPENING,
            S_OP_COUNT_INPUT: S_OP_COUNT_CHOICE, S_SOURCE_CHOICE: S_OP_COUNT_CHOICE,
            S_UPLOAD: S_SOURCE_CHOICE, S_UPLOAD_PENDING: S_UPLOAD,
            S_SHORTAGE: S_UPLOAD, S_OP_DAY_CHOICE: S_SOURCE_CHOICE,
            S_OP_DATE_INPUT: S_OP_DAY_CHOICE, S_SUMMARY: S_OP_COUNT_CHOICE,
            S_OPS_EDITOR: S_SOURCE_CHOICE, S_OP_KIND: S_OPS_EDITOR,
            S_OP_AMOUNT: S_OP_KIND, S_OP_DT_CHOICE: S_OP_AMOUNT,
            S_OP_DT_INPUT: S_OP_DT_CHOICE, S_OP_DETAIL: S_OP_DT_CHOICE,
            S_OP_DELETE: S_OPS_EDITOR,
        }
        if stmt_bank_of(data) != "rocket":
            mapping[S_ACCOUNT_CHOICE] = S_HOLDER
        if stmt_bank_of(data) == "mts":
            mapping[S_OPENING] = S_CARD
            mapping[S_OP_COUNT_CHOICE] = S_OPENING
        if current == S_SUMMARY and data.get("stmt_source") == "manual":
            return S_OPS_EDITOR
        if current == S_SUMMARY and data.get("stmt_receipt_paths"):
            return S_UPLOAD
        if current == S_SUMMARY and int(data.get("stmt_operation_count") or 0) > 0:
            return S_OP_DAY_CHOICE if data.get("stmt_fill_random") else S_SOURCE_CHOICE
        return mapping.get(current, menu)
    mapping = {
        R_MENU: None, R_RECIPIENT_NAME: R_MENU, R_RECIPIENT_PHONE: R_RECIPIENT_NAME,
        R_RECIPIENT_CARD: R_MENU,
        R_RECIPIENT_BANK: R_RECIPIENT_PHONE, R_RECIPIENT_BANK_CUSTOM: R_RECIPIENT_BANK,
        R_AMOUNT: R_RECIPIENT_BANK, R_SENDER_NAME: R_AMOUNT,
        R_SENDER_PHONE: R_SENDER_NAME, R_SENDER_ACCOUNT_CHOICE: R_SENDER_PHONE,
        R_SENDER_ACCOUNT_INPUT: R_SENDER_ACCOUNT_CHOICE,
        R_TIME_CHOICE: R_SENDER_ACCOUNT_CHOICE, R_TIME_INPUT: R_TIME_CHOICE,
        R_SUMMARY: R_TIME_CHOICE,
    }
    if scenario == "rocket_card":
        mapping[R_RECIPIENT_BANK] = R_RECIPIENT_CARD
        mapping[R_SENDER_ACCOUNT_CHOICE] = R_SENDER_NAME
    return mapping.get(current, R_MENU)


# ---------------------------------------------------------------------------
# Хендлеры диалога
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _cleanup_stale_yandex_templates()
    _clear_draft(context)
    context.user_data["current_step"] = BANK_SELECT
    await _delete_user_message(update, context)
    await _send_tracked_message(
        update,
        context,
        "Здравствуйте! Сформирую чек перевода или выписку.\nВыберите банк:",
        reply_markup=_main_menu_markup(getattr(update.effective_user, "id", None)),
    )
    return BANK_SELECT


async def _send_yandex_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""
) -> int:
    """Шаг «Как создать чек?»: без PDF или с PDF."""
    context.user_data["current_step"] = Y_MODE
    await _send_tracked_message(
        update,
        context,
        (
            f"{prefix}"
            "🟡 <b>Яндекс чек.</b> Как создать чек?\n\n"
            "<b>без PDF</b> — возьму настоящий чек из запаса.\n"
            "<b>с PDF</b> — вы пришлёте PDF настоящей квитанции "
            "Яндекс Банка, и чек будет сделан именно из неё."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    _btn("📝 без PDF", Y_MODE_NORMAL_CB),
                    _btn("📄 с PDF", Y_MODE_TEMPLATE_CB),
                ],
                _nav_row(),
            ]
        ),
    )
    return Y_MODE


async def _send_yandex_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """[Яндекс] -> [Чек] / [Выписка]."""
    context.user_data["current_step"] = Y_MENU
    await _send_tracked_message(
        update,
        context,
        "🟡 <b>Яндекс.</b> Что сформировать?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    _btn("🧾 Чек", Y_DOC_CHECK_CB),
                    _btn("📄 Выписка", Y_DOC_STMT_CB),
                ],
                _nav_row(),
            ]
        ),
    )
    return Y_MENU


async def choose_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is not None:
        await query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _cleanup_stale_yandex_templates()
    _clear_draft(context)
    context.user_data["scenario"] = "yandex"
    return await _send_yandex_menu(update, context)


async def on_yandex_doc_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор в меню Яндекса: чек (прежний сценарий) или выписка."""
    if update.callback_query is not None:
        await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _clear_draft(context)
    if slug == "stmt":
        context.user_data["scenario"] = "yandex_statement"
        context.user_data["ystmt_ops"] = []
        context.user_data["ystmt_draft"] = None
        return await _send_ystmt_prompt(update, context, YS_PERIOD)
    context.user_data["scenario"] = "yandex"
    return await _send_yandex_mode(update, context)


async def on_y_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    _cleanup_yandex_template_files(dict(context.user_data))
    for key in (
        "y_template_path", "y_template_dir", "y_template_token",
        "y_template_sbp_id", "y_template_datetime", "y_template_amount",
        "y_template_warnings",
    ):
        context.user_data.pop(key, None)
    if slug == "template":
        context.user_data["yandex_mode"] = "template"
        _cleanup_stale_yandex_templates()
        context.user_data["current_step"] = Y_TEMPLATE_UPLOAD
        await _send_tracked_message(
            update,
            context,
            (
                "📄 Пришлите <b>PDF настоящей квитанции Яндекс Банка</b> "
                "файлом. Проверю её и сделаю чек именно из неё.\n"
                "Шаблон одноразовый: после создания чека файл удалю, "
                "в общий запас он не попадёт."
            ),
            reply_markup=InlineKeyboardMarkup([_nav_row()]),
        )
        return Y_TEMPLATE_UPLOAD
    context.user_data["yandex_mode"] = "normal"
    return await _send_prompt(update, context, RECIPIENT_NAME)


async def _send_template_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data
    text, markup = _prompt(RECIPIENT_NAME, context)
    context.user_data["current_step"] = RECIPIENT_NAME
    await _send_tracked_message(
        update,
        context,
        (
            "✅ <b>Шаблон принят</b> — настоящая квитанция Яндекс Банка: "
            f"операция {html.escape(str(data.get('y_template_datetime', '')))}, "
            f"сумма донора {html.escape(str(data.get('y_template_amount', '')))}.\n"
            "Чек будет сделан именно из этого шаблона.\n\n"
            f"{text}"
        ),
        reply_markup=markup,
    )
    return RECIPIENT_NAME


async def on_y_template_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data
    message = update.effective_message
    document = getattr(message, "document", None)
    await _delete_user_message(update, context)
    if document is None or not (document.file_name or "").lower().endswith(".pdf"):
        await _send_tracked_message(
            update,
            context,
            (
                "⚠️ Нужен именно PDF настоящей квитанции Яндекс Банка.\n\n"
                "📄 Пришлите <b>PDF настоящей квитанции Яндекс Банка</b> файлом."
            ),
            reply_markup=InlineKeyboardMarkup([_nav_row()]),
        )
        data["current_step"] = Y_TEMPLATE_UPLOAD
        return Y_TEMPLATE_UPLOAD

    _cleanup_yandex_template_files(dict(data))
    root = yandex_template_root()
    root.mkdir(parents=True, exist_ok=True)
    template_dir = Path(tempfile.mkdtemp(prefix="tpl_", dir=str(root)))
    local_path = template_dir / "template.pdf"
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(local_path))
        info = await asyncio.to_thread(inspect_yandex_template, local_path)
    except Exception as error:  # не Яндекс / битый PDF — понятный отказ
        logger.exception(
            "Yandex template rejected (user_id=%s)",
            getattr(update.effective_user, "id", "?"),
        )
        _cleanup_yandex_template_files(
            {"y_template_path": str(local_path), "y_template_dir": str(template_dir)}
        )
        return await _send_yandex_mode(
            update,
            context,
            prefix=(
                "Не получилось принять шаблон: это не валидная квитанция "
                f"Яндекс Банка ({_short_error(error)}).\n\n"
            ),
        )

    data["y_template_path"] = str(local_path)
    data["y_template_dir"] = str(template_dir)
    data["y_template_token"] = info["token"]
    data["y_template_sbp_id"] = info["sbp_id"]
    data["y_template_datetime"] = info["donor_datetime"]
    data["y_template_amount"] = info["donor_amount"]
    data["y_template_warnings"] = list(info["warnings"])
    if info["warnings"]:
        data["current_step"] = Y_TEMPLATE_WARN
        await _send_tracked_message(
            update,
            context,
            yandex_template_warning_text(info["warnings"]),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        _btn("✅ Всё равно создать", Y_TEMPLATE_PROCEED_CB),
                        _btn("❌ Отмена", CANCEL_CB),
                    ]
                ]
            ),
        )
        return Y_TEMPLATE_WARN
    return await _send_template_confirmation(update, context)


async def on_y_template_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_user_message(update, context)
    await _send_tracked_message(
        update,
        context,
        (
            "⚠️ Пришлите квитанцию файлом PDF или нажмите «Назад».\n\n"
            "📄 Пришлите <b>PDF настоящей квитанции Яндекс Банка</b> файлом."
        ),
        reply_markup=InlineKeyboardMarkup([_nav_row()]),
    )
    context.user_data["current_step"] = Y_TEMPLATE_UPLOAD
    return Y_TEMPLATE_UPLOAD


async def on_y_template_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _send_template_confirmation(update, context)


async def on_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_name"] = parse_name(
            update.effective_message.text, "ФИО получателя"
        )
    except ValueError as error:
        return await _invalid(update, error, RECIPIENT_NAME, context)
    return await _after_field(update, context, RECIPIENT_PHONE)


async def on_y_mem_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_yandex_recipient") or {}
    context.user_data["recipient_name"] = mem.get("recipient_name")
    context.user_data["recipient_phone"] = mem.get("recipient_phone")
    context.user_data["recipient_bank"] = mem.get("recipient_bank")
    return await _after_field(update, context, AMOUNT)


async def on_recipient_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, RECIPIENT_PHONE, context)
    return await _after_field(update, context, RECIPIENT_BANK)


async def on_y_bank_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_prompt(update, context, Y_RECIPIENT_BANK_CUSTOM)
    for key, label in YANDEX_BANK_OPTIONS:
        if key == slug:
            context.user_data["recipient_bank"] = label
            break
    return await _after_field(update, context, AMOUNT)


async def on_recipient_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_bank"] = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, Y_RECIPIENT_BANK_CUSTOM, context)
    return await _after_field(update, context, AMOUNT)


async def on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["amount"] = parse_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, AMOUNT, context)
    return await _after_field(update, context, SENDER_NAME)


async def on_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["sender_name"] = parse_name(
            update.effective_message.text, "ФИО отправителя"
        )
    except ValueError as error:
        return await _invalid(update, error, SENDER_NAME, context)
    return await _after_field(update, context, SENDER_PHONE)


async def on_y_mem_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_yandex_sender") or {}
    context.user_data["sender_name"] = mem.get("sender_name")
    context.user_data["sender_phone"] = mem.get("sender_phone")
    return await _after_field(update, context, Y_TIME_CHOICE)


async def on_sender_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["sender_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, SENDER_PHONE, context)
    return await _after_field(update, context, Y_TIME_CHOICE)


async def on_y_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_prompt(update, context, DATETIME)
    context.user_data["operation_dt"] = parse_datetime("сейчас")
    context.user_data.pop("editing", False)
    return await _send_summary(update, context)


async def _store_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE, moment: datetime) -> int:
    await _delete_user_message(update, context)
    context.user_data["operation_dt"] = moment
    context.user_data.pop("editing", False)
    return await _send_summary(update, context)


async def on_datetime_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        moment = parse_datetime(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, DATETIME, context)
    return await _store_datetime(update, context, moment)


async def on_datetime_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _store_datetime(update, context, parse_datetime("сейчас"))


async def on_confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    buttons = _rows(
        [
            InlineKeyboardButton(label, callback_data=f"edit:{key}")
            for key, (label, _state, _field) in EDIT_FIELDS.items()
        ]
    )
    buttons.append(_nav_row())
    context.user_data["current_step"] = EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_SELECT


async def on_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state, _field = EDIT_FIELDS[key]
    context.user_data["editing"] = True
    await _send_prompt(update, context, state)
    return state


def _yandex_caption(data: dict) -> str:
    return (
        "✅ Чек готов\n"
        f"Получатель: {data.get('recipient_name')} · {data.get('recipient_phone')} · "
        f"{data.get('recipient_bank')}\n"
        f"Сумма: {data.get('amount')}\n"
        f"Отправитель: {data.get('sender_name')} · {data.get('sender_phone')}\n"
        f"Время: {_summary_time_text(data)} МСК"
    )


async def on_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    # Таймаут шаблона: временный файл старше TTL не используем.
    if data.get("yandex_mode") == "template" and data.get("y_template_path"):
        template_file = Path(str(data["y_template_path"]))
        expired = not template_file.is_file()
        if not expired:
            import time

            expired = (
                time.time() - template_file.stat().st_mtime
                > YANDEX_TEMPLATE_TTL_SECONDS
            )
        if expired:
            _cleanup_yandex_template_files(data)
            for key in list(context.user_data.keys()):
                if str(key).startswith("y_template_"):
                    del context.user_data[key]
            return await _send_yandex_mode(
                update,
                context,
                prefix=(
                    "⏰ Шаблон истёк и был удалён. Пришлите квитанцию "
                    "ещё раз в режиме «с PDF».\n\n"
                ),
            )
    await _send_tracked_message(update, context, "⏳ Формирую чек…")

    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_yandex_"))
    filename = receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_pdf(data, output_path)
        markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("🔁 Ещё чек", callback_data=YANDEX_CB),
                InlineKeyboardButton("🏠 Главное меню", callback_data=MAIN_MENU_CB),
            ]]
        )
        # «Формирую чек…» больше не нужно: вместо него останется документ.
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id,
                document=handle,
                filename=filename,
                caption=_yandex_caption(data),
                reply_markup=markup,
            )
    except Exception as error:  # движок/отправка упали — бот не падает
        logger.exception(
            "Yandex receipt generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"),
            chat_id,
        )
        if _is_fresh_donor_refusal(error):
            # В пуле не осталось доноров актуальной эпохи/свободных
            # токенов — честный отказ движка, а не падение бота.
            _cleanup_yandex_template_files(dict(context.user_data))
            for key in list(context.user_data.keys()):
                if str(key).startswith("y_template_"):
                    del context.user_data[key]
            return await _send_yandex_mode(
                update,
                context,
                prefix=(
                    "Нужен свежий настоящий чек Яндекса за последние дни: "
                    "в запасе не осталось подходящих доноров актуальной "
                    "эпохи со свободными токенами.\n"
                    "Можно прислать свежую квитанцию в режиме «с PDF» — "
                    "или режим «без PDF» заработает, когда запас "
                    "пополнят свежими чеками.\n\n"
                ),
            )
        return await _send_summary(
            update,
            context,
            prefix=(
                f"Не получилось: {_short_error(error)}\n"
                "Исправьте данные в сводке и попробуйте ещё раз.\n\n"
            ),
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass

    _save_yandex_memory(context)
    # Шаблон одноразовый: после генерации временный файл удаляется.
    _cleanup_yandex_template_files(dict(context.user_data))
    _clear_draft(context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _cleanup_alfa_upload(dict(context.user_data))
    _cleanup_vtb_upload(dict(context.user_data))
    await _send_tracked_message(update, context, "Отменено. Чтобы начать заново — /start")
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Выписка Яндекса: промпты, редактор операций, сводка, генерация
# ---------------------------------------------------------------------------


def _ystmt_ops_text(data: dict) -> str:
    ops = list(data.get("ystmt_ops") or [])
    if not ops:
        return "Операций пока нет."
    lines = []
    for index, op in enumerate(ops, 1):
        lines.append(
            f"{index}. {op['datetime']} — {op['description']} — {op['amount']}"
        )
    income, expense, _closing = ystmt_totals(ops, data.get("ystmt_opening_balance") or "0")
    lines.append("")
    lines.append(f"Приход: +{format_money_display(income)} ₽ · Расход: -{format_money_display(expense)} ₽")
    return "\n".join(lines)


def _ystmt_prompt(context: ContextTypes.DEFAULT_TYPE, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == YS_PERIOD:
        return (
            "📅 <b>Период выписки.</b> Введите одной строкой.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026–05.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_ISSUE_CHOICE:
        today = rocket_current_time().date()
        rows = [[_btn("📅 Сегодня", "ysissue:today"), _btn("✍️ Ввести дату", "ysissue:custom")], nav]
        return (
            f"📅 <b>Дата выдачи выписки.</b> Сегодня — {today:%d.%m.%Y}. Оставить или ввести свою?",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_ISSUE_INPUT:
        return (
            "📅 Введите дату выдачи.\n"
            "Нажмите, чтобы скопировать: <code>05.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_HOLDER_NAME:
        rows = []
        mem = data.get("mem_yandex_statement_holder") or {}
        if mem.get("ystmt_holder_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['ystmt_holder_name']})",
                YS_MEM_HOLDER_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Держатель.</b> Введите ФИО полностью.\n"
            "Нажмите, чтобы скопировать: <code>Васенин Иван Андреевич</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_HOLDER_BIRTH:
        return (
            "🎂 <b>Дата рождения держателя.</b>\n"
            "Нажмите, чтобы скопировать: <code>12.03.1995</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_HOLDER_PASSPORT:
        return (
            "🪪 <b>Паспорт держателя</b> — 10 цифр, отформатирую сам.\n"
            "Нажмите, чтобы скопировать: <code>4510 123456</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_CONTRACT_DATE_CHOICE:
        start = data.get("ystmt_period_start")
        label = f"📅 Как начало периода ({start:%d.%m.%Y})" if start else "📅 Как начало периода"
        rows = [[_btn(label, "yscontract:start"), _btn("✍️ Ввести дату", "yscontract:custom")], nav]
        return ("📄 <b>Дата договора</b> — как начало периода или ввести свою?", InlineKeyboardMarkup(rows))
    if state == YS_CONTRACT_DATE_INPUT:
        return (
            "📄 Введите дату договора.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_CONTRACT_NUMBER_CHOICE:
        rows = [[_btn("🎲 Сгенерировать автоматически", "ysnum:auto"), _btn("✍️ Ввести свой", "ysnum:custom")], nav]
        return (
            "📄 <b>Номер договора.</b> Сгенерировать автоматически (Э + дата + цифры) или ввести свой?",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_CONTRACT_NUMBER_INPUT:
        return (
            "📄 Введите номер договора: Э и 17 цифр.\n"
            "Нажмите, чтобы скопировать: <code>Э20261103102094982</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OPENING:
        return (
            "💰 <b>Входящий остаток</b> на начало периода. Копейки можно.\n"
            "Нажмите, чтобы скопировать: <code>100,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OPS_EDITOR:
        ops = list(data.get("ystmt_ops") or [])
        if ops:
            buttons = [
                _btn("➕ Добавить ещё", "ysops:add"),
                _btn("🗑 Удалить операцию", "ysops:delete"),
                _btn("✅ Готово к сводке", "ysops:done"),
            ]
        else:
            buttons = [
                _btn("➕ Добавить операцию", "ysops:add"),
                _btn("🚫 Без операций", "ysops:none"),
            ]
        return (
            "🧾 <b>Операции выписки</b>\n\n"
            f"{html.escape(_ystmt_ops_text(data))}\n\n"
            "Можно прислать несколько операций одним сообщением — по одной на строку:\n"
            "<code>04.11.2026 12:40 | описание | -250,00</code>",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == YS_OP_TYPE:
        buttons = [
            _btn("📥 Входящий перевод СБП", "ysop:in"),
            _btn("📤 Исходящий перевод СБП", "ysop:out"),
            _btn("✍️ Своё описание", "ysop:custom"),
        ]
        return ("🧾 <b>Какая операция?</b>", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_NAME:
        return (
            "👤 Введите ФИО контрагента.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Петрович</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_PHONE:
        return (
            "📱 <b>Телефон контрагента</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 111-22-33</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_BANK:
        buttons = [
            InlineKeyboardButton(label, callback_data=f"ysbank:{slug}")
            for slug, label in YANDEX_BANK_OPTIONS
        ]
        buttons.append(InlineKeyboardButton("✍️ Ввести свой", callback_data="ysbank:custom"))
        return ("🏦 <b>Банк контрагента</b> — выберите кнопкой:", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_BANK_CUSTOM:
        return (
            "🏦 Введите банк контрагента.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_CUSTOM_SIGN:
        rows = [[_btn("📥 Приход", "yssign:in"), _btn("📤 Расход", "yssign:out")], nav]
        return ("🧾 Это <b>приход</b> или <b>расход</b>?", InlineKeyboardMarkup(rows))
    if state == YS_OP_CUSTOM_DESC:
        return (
            "✍️ Введите описание операции одной строкой.\n"
            "Нажмите, чтобы скопировать: <code>Оплата услуг, договор 12</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_DT_CHOICE:
        buttons = []
        now = datetime.now(MSK)
        start, end = data.get("ystmt_period_start"), data.get("ystmt_period_end")
        if start and end and start <= now.date() <= end:
            buttons.append(_btn("🕒 Сейчас", "ysdt:now"))
        buttons.append(_btn("✍️ Ввести дату и время", "ysdt:input"))
        return ("🕒 <b>Дата и время операции</b> — сейчас или ввести?", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_DT_INPUT:
        return (
            "🕒 Введите дату и время операции внутри периода.\n"
            "Нажмите, чтобы скопировать: <code>04.11.2026 12:40</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_AMOUNT:
        return (
            "💰 <b>Сумма операции</b> положительным числом, копейки можно. "
            "Знак поставлю сам по виду операции.\n"
            "Нажмите, чтобы скопировать: <code>1 500,50</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_DELETE:
        ops = list(data.get("ystmt_ops") or [])
        buttons = [
            _btn(f"{i}. {op['datetime']} {op['amount']}", f"ysdel:{i - 1}")
            for i, op in enumerate(ops, 1)
        ]
        return ("🗑 <b>Какую операцию удалить?</b>", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    raise ValueError(f"no yandex statement prompt for state {state}")


async def _send_ystmt_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _ystmt_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


async def _ystmt_invalid(update, context, error, state: int) -> int:
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, state, prefix=f"⚠️ {error}\n\n")


async def _ystmt_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_ystmt_summary(update, context)
    return await _send_ystmt_prompt(update, context, next_state)


async def _ystmt_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_ystmt_summary(update, context)
    return await _send_ystmt_prompt(update, context, next_state)


def _save_ystmt_memory(context) -> None:
    data = context.user_data
    if data.get("ystmt_holder_name"):
        data["mem_yandex_statement_holder"] = {
            "ystmt_holder_name": data.get("ystmt_holder_name"),
            "ystmt_holder_birth": data["ystmt_holder_birth"].isoformat()
            if data.get("ystmt_holder_birth") else None,
            "ystmt_holder_passport": data.get("ystmt_holder_passport"),
        }


def _ystmt_summary_text(data: dict) -> str:
    income, expense, closing = ystmt_totals(
        list(data.get("ystmt_ops") or []), data.get("ystmt_opening_balance") or "0"
    )
    lines = [
        "<b>Проверь данные выписки</b>",
        "",
        f"📅 Период: <b>{data['ystmt_period_start']:%d.%m.%Y}–{data['ystmt_period_end']:%d.%m.%Y}</b>",
        f"📅 Дата выдачи: <b>{data['ystmt_issue_date']:%d.%m.%Y}</b>",
        f"👤 Держатель: <b>{html.escape(str(data.get('ystmt_holder_name', '')))}</b>",
        f"🎂 Дата рождения: <b>{data['ystmt_holder_birth']:%d.%m.%Y}</b>",
        f"🪪 Паспорт: <b>{html.escape(str(data.get('ystmt_holder_passport', '')))}</b>",
        f"📄 Договор: <b>{html.escape(str(data.get('ystmt_contract_number') or 'будет сгенерирован автоматически'))}</b> от <b>{data['ystmt_contract_date']:%d.%m.%Y}</b>",
        f"💰 Входящий остаток: <b>{format_money_display(data.get('ystmt_opening_balance', '0'))} ₽</b>",
        "",
        "<b>Операции</b>",
    ]
    ops = list(data.get("ystmt_ops") or [])
    if ops:
        for index, op in enumerate(ops, 1):
            lines.append(
                f"{index}. {html.escape(op['datetime'])} — "
                f"{html.escape(op['description'])} — <b>{html.escape(op['amount'])} ₽</b>"
            )
    else:
        lines.append("Без операций.")
    lines += [
        "",
        f"📥 Приход: <b>+{format_money_display(income)} ₽</b>",
        f"📤 Расход: <b>-{format_money_display(expense)} ₽</b>",
        f"💰 Исходящий остаток: <b>{format_money_display(closing)} ₽</b>",
    ]
    return "\n".join(lines)


def _ystmt_summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✅ Сгенерировать", YS_GENERATE_CB), _btn("✏️ Исправить", YS_EDIT_CB)],
            _nav_row(),
        ]
    )


async def _send_ystmt_summary(update, context, prefix: str = "") -> int:
    _save_ystmt_memory(context)
    context.user_data["current_step"] = YS_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_ystmt_summary_text(context.user_data)}",
        reply_markup=_ystmt_summary_markup(),
    )
    return YS_SUMMARY


YSTMT_EDIT_FIELDS = {
    "period": ("📅 Период", YS_PERIOD),
    "issue": ("📅 Дата выдачи", YS_ISSUE_CHOICE),
    "holder_name": ("👤 Держатель", YS_HOLDER_NAME),
    "holder_birth": ("🎂 Дата рождения", YS_HOLDER_BIRTH),
    "holder_passport": ("🪪 Паспорт", YS_HOLDER_PASSPORT),
    "contract_date": ("📄 Дата договора", YS_CONTRACT_DATE_CHOICE),
    "contract_number": ("📄 Номер договора", YS_CONTRACT_NUMBER_CHOICE),
    "opening": ("💰 Входящий остаток", YS_OPENING),
    "operations": ("🧾 Операции", YS_OPS_EDITOR),
}


async def on_ystmt_period(update, context) -> int:
    try:
        start, end = parse_statement_period(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_PERIOD)
    context.user_data["ystmt_period_start"] = start
    context.user_data["ystmt_period_end"] = end
    return await _ystmt_after_field(update, context, YS_ISSUE_CHOICE)


async def on_ystmt_issue_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_ISSUE_INPUT)
    context.user_data["ystmt_issue_date"] = rocket_current_time().date()
    return await _ystmt_choice_next(update, context, YS_HOLDER_NAME)


async def on_ystmt_issue_input(update, context) -> int:
    try:
        context.user_data["ystmt_issue_date"] = parse_statement_date(
            update.effective_message.text, "дату выдачи"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_ISSUE_INPUT)
    return await _ystmt_after_field(update, context, YS_HOLDER_NAME)


async def on_ystmt_holder_name(update, context) -> int:
    try:
        context.user_data["ystmt_holder_name"] = parse_name(
            update.effective_message.text, "ФИО держателя"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_NAME)
    return await _ystmt_after_field(update, context, YS_HOLDER_BIRTH)


async def on_ystmt_mem_holder(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_yandex_statement_holder") or {}
    context.user_data["ystmt_holder_name"] = mem.get("ystmt_holder_name")
    if mem.get("ystmt_holder_birth"):
        context.user_data["ystmt_holder_birth"] = date.fromisoformat(mem["ystmt_holder_birth"])
    context.user_data["ystmt_holder_passport"] = mem.get("ystmt_holder_passport")
    return await _ystmt_choice_next(update, context, YS_CONTRACT_DATE_CHOICE)


async def on_ystmt_holder_birth(update, context) -> int:
    try:
        context.user_data["ystmt_holder_birth"] = parse_birth_date(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_BIRTH)
    return await _ystmt_after_field(update, context, YS_HOLDER_PASSPORT)


async def on_ystmt_holder_passport(update, context) -> int:
    try:
        context.user_data["ystmt_holder_passport"] = parse_passport(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_PASSPORT)
    return await _ystmt_after_field(update, context, YS_CONTRACT_DATE_CHOICE)


async def on_ystmt_contract_date_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_CONTRACT_DATE_INPUT)
    context.user_data["ystmt_contract_date"] = context.user_data.get("ystmt_period_start")
    return await _ystmt_choice_next(update, context, YS_CONTRACT_NUMBER_CHOICE)


async def on_ystmt_contract_date_input(update, context) -> int:
    try:
        context.user_data["ystmt_contract_date"] = parse_statement_date(
            update.effective_message.text, "дату договора"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_CONTRACT_DATE_INPUT)
    return await _ystmt_after_field(update, context, YS_CONTRACT_NUMBER_CHOICE)


async def on_ystmt_contract_number_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_CONTRACT_NUMBER_INPUT)
    context.user_data["ystmt_contract_number"] = None
    return await _ystmt_choice_next(update, context, YS_OPENING)


async def on_ystmt_contract_number_input(update, context) -> int:
    try:
        context.user_data["ystmt_contract_number"] = parse_contract_number(
            update.effective_message.text
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_CONTRACT_NUMBER_INPUT)
    return await _ystmt_after_field(update, context, YS_OPENING)


async def on_ystmt_opening(update, context) -> int:
    try:
        context.user_data["ystmt_opening_balance"] = parse_opening_balance(
            update.effective_message.text
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OPENING)
    return await _ystmt_after_field(update, context, YS_OPS_EDITOR)


async def on_ystmt_ops_editor_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "add":
        return await _send_ystmt_prompt(update, context, YS_OP_TYPE)
    if slug == "delete":
        return await _send_ystmt_prompt(update, context, YS_OP_DELETE)
    # done / none -> сводка
    return await _send_ystmt_summary(update, context)


async def on_ystmt_ops_editor_text(update, context) -> int:
    data = context.user_data
    try:
        new_ops = parse_ystmt_bulk_operations(
            update.effective_message.text,
            data["ystmt_period_start"],
            data["ystmt_period_end"],
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OPS_EDITOR)
    await _delete_user_message(update, context)
    data.setdefault("ystmt_ops", []).extend(new_ops)
    return await _send_ystmt_prompt(
        update, context, YS_OPS_EDITOR,
        prefix=f"✅ Добавлено операций: {len(new_ops)}\n\n",
    )


async def on_ystmt_op_type(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        context.user_data["ystmt_draft"] = {"kind": "custom"}
        return await _send_ystmt_prompt(update, context, YS_OP_CUSTOM_SIGN)
    context.user_data["ystmt_draft"] = {"kind": slug}
    return await _send_ystmt_prompt(update, context, YS_OP_NAME)


async def on_ystmt_op_custom_sign(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    draft = dict(context.user_data.get("ystmt_draft") or {"kind": "custom"})
    draft["sign"] = 1 if slug == "in" else -1
    context.user_data["ystmt_draft"] = draft
    return await _send_ystmt_prompt(update, context, YS_OP_CUSTOM_DESC)


async def on_ystmt_op_custom_desc(update, context) -> int:
    text = " ".join((update.effective_message.text or "").strip().split())
    if len(text) < 3:
        return await _ystmt_invalid(
            update, context, ValueError("Описание слишком короткое. Введите описание одной строкой."),
            YS_OP_CUSTOM_DESC,
        )
    draft = dict(context.user_data.get("ystmt_draft") or {"kind": "custom"})
    draft["description"] = text
    context.user_data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_name(update, context) -> int:
    try:
        context.user_data["ystmt_draft"] = {
            **(context.user_data.get("ystmt_draft") or {}),
            "name": parse_name(update.effective_message.text, "ФИО контрагента"),
        }
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_NAME)
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_PHONE)


async def on_ystmt_op_phone(update, context) -> int:
    try:
        context.user_data["ystmt_draft"] = {
            **(context.user_data.get("ystmt_draft") or {}),
            "phone": parse_phone(update.effective_message.text),
        }
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_PHONE)
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_BANK)


async def on_ystmt_op_bank_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_OP_BANK_CUSTOM)
    for key, label in YANDEX_BANK_OPTIONS:
        if key == slug:
            draft = dict(context.user_data.get("ystmt_draft") or {})
            draft["bank"] = label
            context.user_data["ystmt_draft"] = draft
            break
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_bank_custom(update, context) -> int:
    try:
        bank = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_BANK_CUSTOM)
    draft = dict(context.user_data.get("ystmt_draft") or {})
    draft["bank"] = bank
    context.user_data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_dt_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "input":
        return await _send_ystmt_prompt(update, context, YS_OP_DT_INPUT)
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            "сейчас", data["ystmt_period_start"], data["ystmt_period_end"]
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_DT_CHOICE)
    draft = dict(data.get("ystmt_draft") or {})
    draft["datetime"] = moment.strftime("%d.%m.%Y %H:%M")
    data["ystmt_draft"] = draft
    return await _send_ystmt_prompt(update, context, YS_OP_AMOUNT)


async def on_ystmt_op_dt_input(update, context) -> int:
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            update.effective_message.text,
            data["ystmt_period_start"],
            data["ystmt_period_end"],
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_DT_INPUT)
    draft = dict(data.get("ystmt_draft") or {})
    draft["datetime"] = moment.strftime("%d.%m.%Y %H:%M")
    data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_AMOUNT)


async def on_ystmt_op_amount(update, context) -> int:
    data = context.user_data
    draft = dict(data.get("ystmt_draft") or {})
    try:
        value = parse_positive_amount(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_AMOUNT)
    if draft.get("kind") in ("in", "out"):
        sign = 1 if draft["kind"] == "in" else -1
        description = build_ystmt_sbp_description(
            draft["kind"], draft.get("name", ""), draft.get("phone", ""), draft.get("bank", "")
        )
    else:
        sign = int(draft.get("sign") or 1)
        description = draft.get("description", "")
    data.setdefault("ystmt_ops", []).append(
        {
            "datetime": draft.get("datetime", ""),
            "description": description,
            "amount": format_signed_amount(value, sign),
        }
    )
    data["ystmt_draft"] = None
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OPS_EDITOR)


async def on_ystmt_op_delete_choice(update, context) -> int:
    await update.callback_query.answer()
    index = int(update.callback_query.data.split(":", 1)[1])
    ops = list(context.user_data.get("ystmt_ops") or [])
    if 0 <= index < len(ops):
        removed = ops.pop(index)
        context.user_data["ystmt_ops"] = ops
        prefix = f"🗑 Удалена операция: {removed['datetime']} {removed['amount']} ₽\n\n"
    else:
        prefix = ""
    return await _send_ystmt_prompt(update, context, YS_OPS_EDITOR, prefix=prefix)


async def on_ystmt_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    buttons = _rows(
        [_btn(label, f"ysedit:{key}") for key, (label, _state) in YSTMT_EDIT_FIELDS.items()]
    )
    buttons.append(_nav_row())
    context.user_data["current_step"] = YS_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return YS_EDIT_SELECT


async def on_ystmt_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state = YSTMT_EDIT_FIELDS[key]
    context.user_data["editing"] = True
    return await _send_ystmt_prompt(update, context, state)


def _ystmt_caption(data: dict) -> str:
    text = _ystmt_summary_text(data)
    plain = re.sub(r"</?b>", "", text)
    return "✅ Выписка готова\n" + plain.replace("<b>", "").replace("</b>", "")


async def on_ystmt_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую выписку…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_yandex_stmt_"))
    filename = receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_yandex_statement_pdf(data, output_path)
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    _btn("🔁 Ещё выписка", Y_DOC_STMT_CB),
                    _btn("🧾 Чек", Y_DOC_CHECK_CB),
                ],
                [_btn("🏠 Главное меню", MAIN_MENU_CB)],
            ]
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=_ystmt_caption(data), reply_markup=markup,
            )
    except Exception as error:  # движок упал — бот жив, возврат в сводку
        logger.exception(
            "Yandex statement generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_ystmt_summary(
            update, context, prefix=f"{explain_ystmt_error(error)}\n\n"
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_ystmt_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Выписка Яндекса: промпты, редактор операций, сводка, генерация
# ---------------------------------------------------------------------------


def _ystmt_ops_text(data: dict) -> str:
    ops = list(data.get("ystmt_ops") or [])
    if not ops:
        return "Операций пока нет."
    lines = []
    for index, op in enumerate(ops, 1):
        lines.append(
            f"{index}. {op['datetime']} — {op['description']} — {op['amount']}"
        )
    income, expense, _closing = ystmt_totals(ops, data.get("ystmt_opening_balance") or "0")
    lines.append("")
    lines.append(f"Приход: +{format_money_display(income)} ₽ · Расход: -{format_money_display(expense)} ₽")
    return "\n".join(lines)


def _ystmt_prompt(context: ContextTypes.DEFAULT_TYPE, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == YS_PERIOD:
        return (
            "📅 <b>Период выписки.</b> Введите одной строкой.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026–05.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_ISSUE_CHOICE:
        today = rocket_current_time().date()
        rows = [[_btn("📅 Сегодня", "ysissue:today"), _btn("✍️ Ввести дату", "ysissue:custom")], nav]
        return (
            f"📅 <b>Дата выдачи выписки.</b> Сегодня — {today:%d.%m.%Y}. Оставить или ввести свою?",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_ISSUE_INPUT:
        return (
            "📅 Введите дату выдачи.\n"
            "Нажмите, чтобы скопировать: <code>05.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_HOLDER_NAME:
        rows = []
        mem = data.get("mem_yandex_statement_holder") or {}
        if mem.get("ystmt_holder_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['ystmt_holder_name']})",
                YS_MEM_HOLDER_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Держатель.</b> Введите ФИО полностью.\n"
            "Нажмите, чтобы скопировать: <code>Васенин Иван Андреевич</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_HOLDER_BIRTH:
        return (
            "🎂 <b>Дата рождения держателя.</b>\n"
            "Нажмите, чтобы скопировать: <code>12.03.1995</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_HOLDER_PASSPORT:
        return (
            "🪪 <b>Паспорт держателя</b> — 10 цифр, отформатирую сам.\n"
            "Нажмите, чтобы скопировать: <code>4510 123456</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_CONTRACT_DATE_CHOICE:
        start = data.get("ystmt_period_start")
        label = f"📅 Как начало периода ({start:%d.%m.%Y})" if start else "📅 Как начало периода"
        rows = [[_btn(label, "yscontract:start"), _btn("✍️ Ввести дату", "yscontract:custom")], nav]
        return ("📄 <b>Дата договора</b> — как начало периода или ввести свою?", InlineKeyboardMarkup(rows))
    if state == YS_CONTRACT_DATE_INPUT:
        return (
            "📄 Введите дату договора.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_CONTRACT_NUMBER_CHOICE:
        rows = [[_btn("🎲 Сгенерировать автоматически", "ysnum:auto"), _btn("✍️ Ввести свой", "ysnum:custom")], nav]
        return (
            "📄 <b>Номер договора.</b> Сгенерировать автоматически (Э + дата + цифры) или ввести свой?",
            InlineKeyboardMarkup(rows),
        )
    if state == YS_CONTRACT_NUMBER_INPUT:
        return (
            "📄 Введите номер договора: Э и 17 цифр.\n"
            "Нажмите, чтобы скопировать: <code>Э20261103102094982</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OPENING:
        return (
            "💰 <b>Входящий остаток</b> на начало периода. Копейки можно.\n"
            "Нажмите, чтобы скопировать: <code>100,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OPS_EDITOR:
        ops = list(data.get("ystmt_ops") or [])
        if ops:
            buttons = [
                _btn("➕ Добавить ещё", "ysops:add"),
                _btn("🗑 Удалить операцию", "ysops:delete"),
                _btn("✅ Готово к сводке", "ysops:done"),
            ]
        else:
            buttons = [
                _btn("➕ Добавить операцию", "ysops:add"),
                _btn("🚫 Без операций", "ysops:none"),
            ]
        return (
            "🧾 <b>Операции выписки</b>\n\n"
            f"{html.escape(_ystmt_ops_text(data))}\n\n"
            "Можно прислать несколько операций одним сообщением — по одной на строку:\n"
            "<code>04.11.2026 12:40 | описание | -250,00</code>",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == YS_OP_TYPE:
        buttons = [
            _btn("📥 Входящий перевод СБП", "ysop:in"),
            _btn("📤 Исходящий перевод СБП", "ysop:out"),
            _btn("✍️ Своё описание", "ysop:custom"),
        ]
        return ("🧾 <b>Какая операция?</b>", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_NAME:
        return (
            "👤 Введите ФИО контрагента.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Петрович</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_PHONE:
        return (
            "📱 <b>Телефон контрагента</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 900 111-22-33</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_BANK:
        buttons = [
            InlineKeyboardButton(label, callback_data=f"ysbank:{slug}")
            for slug, label in YANDEX_BANK_OPTIONS
        ]
        buttons.append(InlineKeyboardButton("✍️ Ввести свой", callback_data="ysbank:custom"))
        return ("🏦 <b>Банк контрагента</b> — выберите кнопкой:", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_BANK_CUSTOM:
        return (
            "🏦 Введите банк контрагента.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_CUSTOM_SIGN:
        rows = [[_btn("📥 Приход", "yssign:in"), _btn("📤 Расход", "yssign:out")], nav]
        return ("🧾 Это <b>приход</b> или <b>расход</b>?", InlineKeyboardMarkup(rows))
    if state == YS_OP_CUSTOM_DESC:
        return (
            "✍️ Введите описание операции одной строкой.\n"
            "Нажмите, чтобы скопировать: <code>Оплата услуг, договор 12</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_DT_CHOICE:
        buttons = []
        now = datetime.now(MSK)
        start, end = data.get("ystmt_period_start"), data.get("ystmt_period_end")
        if start and end and start <= now.date() <= end:
            buttons.append(_btn("🕒 Сейчас", "ysdt:now"))
        buttons.append(_btn("✍️ Ввести дату и время", "ysdt:input"))
        return ("🕒 <b>Дата и время операции</b> — сейчас или ввести?", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    if state == YS_OP_DT_INPUT:
        return (
            "🕒 Введите дату и время операции внутри периода.\n"
            "Нажмите, чтобы скопировать: <code>04.11.2026 12:40</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_AMOUNT:
        return (
            "💰 <b>Сумма операции</b> положительным числом, копейки можно. "
            "Знак поставлю сам по виду операции.\n"
            "Нажмите, чтобы скопировать: <code>1 500,50</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == YS_OP_DELETE:
        ops = list(data.get("ystmt_ops") or [])
        buttons = [
            _btn(f"{i}. {op['datetime']} {op['amount']}", f"ysdel:{i - 1}")
            for i, op in enumerate(ops, 1)
        ]
        return ("🗑 <b>Какую операцию удалить?</b>", InlineKeyboardMarkup(_rows(buttons) + [nav]))
    raise ValueError(f"no yandex statement prompt for state {state}")


async def _send_ystmt_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _ystmt_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


async def _ystmt_invalid(update, context, error, state: int) -> int:
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, state, prefix=f"⚠️ {error}\n\n")


async def _ystmt_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_ystmt_summary(update, context)
    return await _send_ystmt_prompt(update, context, next_state)


async def _ystmt_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_ystmt_summary(update, context)
    return await _send_ystmt_prompt(update, context, next_state)


def _save_ystmt_memory(context) -> None:
    data = context.user_data
    if data.get("ystmt_holder_name"):
        data["mem_yandex_statement_holder"] = {
            "ystmt_holder_name": data.get("ystmt_holder_name"),
            "ystmt_holder_birth": data["ystmt_holder_birth"].isoformat()
            if data.get("ystmt_holder_birth") else None,
            "ystmt_holder_passport": data.get("ystmt_holder_passport"),
        }


def _ystmt_summary_text(data: dict) -> str:
    income, expense, closing = ystmt_totals(
        list(data.get("ystmt_ops") or []), data.get("ystmt_opening_balance") or "0"
    )
    lines = [
        "<b>Проверь данные выписки</b>",
        "",
        f"📅 Период: <b>{data['ystmt_period_start']:%d.%m.%Y}–{data['ystmt_period_end']:%d.%m.%Y}</b>",
        f"📅 Дата выдачи: <b>{data['ystmt_issue_date']:%d.%m.%Y}</b>",
        f"👤 Держатель: <b>{html.escape(str(data.get('ystmt_holder_name', '')))}</b>",
        f"🎂 Дата рождения: <b>{data['ystmt_holder_birth']:%d.%m.%Y}</b>",
        f"🪪 Паспорт: <b>{html.escape(str(data.get('ystmt_holder_passport', '')))}</b>",
        f"📄 Договор: <b>{html.escape(str(data.get('ystmt_contract_number') or 'будет сгенерирован автоматически'))}</b> от <b>{data['ystmt_contract_date']:%d.%m.%Y}</b>",
        f"💰 Входящий остаток: <b>{format_money_display(data.get('ystmt_opening_balance', '0'))} ₽</b>",
        "",
        "<b>Операции</b>",
    ]
    ops = list(data.get("ystmt_ops") or [])
    if ops:
        for index, op in enumerate(ops, 1):
            lines.append(
                f"{index}. {html.escape(op['datetime'])} — "
                f"{html.escape(op['description'])} — <b>{html.escape(op['amount'])} ₽</b>"
            )
    else:
        lines.append("Без операций.")
    lines += [
        "",
        f"📥 Приход: <b>+{format_money_display(income)} ₽</b>",
        f"📤 Расход: <b>-{format_money_display(expense)} ₽</b>",
        f"💰 Исходящий остаток: <b>{format_money_display(closing)} ₽</b>",
    ]
    return "\n".join(lines)


def _ystmt_summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("✅ Сгенерировать", YS_GENERATE_CB), _btn("✏️ Исправить", YS_EDIT_CB)],
            _nav_row(),
        ]
    )


async def _send_ystmt_summary(update, context, prefix: str = "") -> int:
    _save_ystmt_memory(context)
    context.user_data["current_step"] = YS_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_ystmt_summary_text(context.user_data)}",
        reply_markup=_ystmt_summary_markup(),
    )
    return YS_SUMMARY


YSTMT_EDIT_FIELDS = {
    "period": ("📅 Период", YS_PERIOD),
    "issue": ("📅 Дата выдачи", YS_ISSUE_CHOICE),
    "holder_name": ("👤 Держатель", YS_HOLDER_NAME),
    "holder_birth": ("🎂 Дата рождения", YS_HOLDER_BIRTH),
    "holder_passport": ("🪪 Паспорт", YS_HOLDER_PASSPORT),
    "contract_date": ("📄 Дата договора", YS_CONTRACT_DATE_CHOICE),
    "contract_number": ("📄 Номер договора", YS_CONTRACT_NUMBER_CHOICE),
    "opening": ("💰 Входящий остаток", YS_OPENING),
    "operations": ("🧾 Операции", YS_OPS_EDITOR),
}


async def on_ystmt_period(update, context) -> int:
    try:
        start, end = parse_statement_period(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_PERIOD)
    context.user_data["ystmt_period_start"] = start
    context.user_data["ystmt_period_end"] = end
    return await _ystmt_after_field(update, context, YS_ISSUE_CHOICE)


async def on_ystmt_issue_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_ISSUE_INPUT)
    context.user_data["ystmt_issue_date"] = rocket_current_time().date()
    return await _ystmt_choice_next(update, context, YS_HOLDER_NAME)


async def on_ystmt_issue_input(update, context) -> int:
    try:
        context.user_data["ystmt_issue_date"] = parse_statement_date(
            update.effective_message.text, "дату выдачи"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_ISSUE_INPUT)
    return await _ystmt_after_field(update, context, YS_HOLDER_NAME)


async def on_ystmt_holder_name(update, context) -> int:
    try:
        context.user_data["ystmt_holder_name"] = parse_name(
            update.effective_message.text, "ФИО держателя"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_NAME)
    return await _ystmt_after_field(update, context, YS_HOLDER_BIRTH)


async def on_ystmt_mem_holder(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_yandex_statement_holder") or {}
    context.user_data["ystmt_holder_name"] = mem.get("ystmt_holder_name")
    if mem.get("ystmt_holder_birth"):
        context.user_data["ystmt_holder_birth"] = date.fromisoformat(mem["ystmt_holder_birth"])
    context.user_data["ystmt_holder_passport"] = mem.get("ystmt_holder_passport")
    return await _ystmt_choice_next(update, context, YS_CONTRACT_DATE_CHOICE)


async def on_ystmt_holder_birth(update, context) -> int:
    try:
        context.user_data["ystmt_holder_birth"] = parse_birth_date(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_BIRTH)
    return await _ystmt_after_field(update, context, YS_HOLDER_PASSPORT)


async def on_ystmt_holder_passport(update, context) -> int:
    try:
        context.user_data["ystmt_holder_passport"] = parse_passport(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_HOLDER_PASSPORT)
    return await _ystmt_after_field(update, context, YS_CONTRACT_DATE_CHOICE)


async def on_ystmt_contract_date_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_CONTRACT_DATE_INPUT)
    context.user_data["ystmt_contract_date"] = context.user_data.get("ystmt_period_start")
    return await _ystmt_choice_next(update, context, YS_CONTRACT_NUMBER_CHOICE)


async def on_ystmt_contract_date_input(update, context) -> int:
    try:
        context.user_data["ystmt_contract_date"] = parse_statement_date(
            update.effective_message.text, "дату договора"
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_CONTRACT_DATE_INPUT)
    return await _ystmt_after_field(update, context, YS_CONTRACT_NUMBER_CHOICE)


async def on_ystmt_contract_number_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_CONTRACT_NUMBER_INPUT)
    context.user_data["ystmt_contract_number"] = None
    return await _ystmt_choice_next(update, context, YS_OPENING)


async def on_ystmt_contract_number_input(update, context) -> int:
    try:
        context.user_data["ystmt_contract_number"] = parse_contract_number(
            update.effective_message.text
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_CONTRACT_NUMBER_INPUT)
    return await _ystmt_after_field(update, context, YS_OPENING)


async def on_ystmt_opening(update, context) -> int:
    try:
        context.user_data["ystmt_opening_balance"] = parse_opening_balance(
            update.effective_message.text
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OPENING)
    return await _ystmt_after_field(update, context, YS_OPS_EDITOR)


async def on_ystmt_ops_editor_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "add":
        return await _send_ystmt_prompt(update, context, YS_OP_TYPE)
    if slug == "delete":
        return await _send_ystmt_prompt(update, context, YS_OP_DELETE)
    # done / none -> сводка
    return await _send_ystmt_summary(update, context)


async def on_ystmt_ops_editor_text(update, context) -> int:
    data = context.user_data
    try:
        new_ops = parse_ystmt_bulk_operations(
            update.effective_message.text,
            data["ystmt_period_start"],
            data["ystmt_period_end"],
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OPS_EDITOR)
    await _delete_user_message(update, context)
    data.setdefault("ystmt_ops", []).extend(new_ops)
    return await _send_ystmt_prompt(
        update, context, YS_OPS_EDITOR,
        prefix=f"✅ Добавлено операций: {len(new_ops)}\n\n",
    )


async def on_ystmt_op_type(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        context.user_data["ystmt_draft"] = {"kind": "custom"}
        return await _send_ystmt_prompt(update, context, YS_OP_CUSTOM_SIGN)
    context.user_data["ystmt_draft"] = {"kind": slug}
    return await _send_ystmt_prompt(update, context, YS_OP_NAME)


async def on_ystmt_op_custom_sign(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    draft = dict(context.user_data.get("ystmt_draft") or {"kind": "custom"})
    draft["sign"] = 1 if slug == "in" else -1
    context.user_data["ystmt_draft"] = draft
    return await _send_ystmt_prompt(update, context, YS_OP_CUSTOM_DESC)


async def on_ystmt_op_custom_desc(update, context) -> int:
    text = " ".join((update.effective_message.text or "").strip().split())
    if len(text) < 3:
        return await _ystmt_invalid(
            update, context, ValueError("Описание слишком короткое. Введите описание одной строкой."),
            YS_OP_CUSTOM_DESC,
        )
    draft = dict(context.user_data.get("ystmt_draft") or {"kind": "custom"})
    draft["description"] = text
    context.user_data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_name(update, context) -> int:
    try:
        context.user_data["ystmt_draft"] = {
            **(context.user_data.get("ystmt_draft") or {}),
            "name": parse_name(update.effective_message.text, "ФИО контрагента"),
        }
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_NAME)
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_PHONE)


async def on_ystmt_op_phone(update, context) -> int:
    try:
        context.user_data["ystmt_draft"] = {
            **(context.user_data.get("ystmt_draft") or {}),
            "phone": parse_phone(update.effective_message.text),
        }
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_PHONE)
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_BANK)


async def on_ystmt_op_bank_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_ystmt_prompt(update, context, YS_OP_BANK_CUSTOM)
    for key, label in YANDEX_BANK_OPTIONS:
        if key == slug:
            draft = dict(context.user_data.get("ystmt_draft") or {})
            draft["bank"] = label
            context.user_data["ystmt_draft"] = draft
            break
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_bank_custom(update, context) -> int:
    try:
        bank = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_BANK_CUSTOM)
    draft = dict(context.user_data.get("ystmt_draft") or {})
    draft["bank"] = bank
    context.user_data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_DT_CHOICE)


async def on_ystmt_op_dt_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "input":
        return await _send_ystmt_prompt(update, context, YS_OP_DT_INPUT)
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            "сейчас", data["ystmt_period_start"], data["ystmt_period_end"]
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_DT_CHOICE)
    draft = dict(data.get("ystmt_draft") or {})
    draft["datetime"] = moment.strftime("%d.%m.%Y %H:%M")
    data["ystmt_draft"] = draft
    return await _send_ystmt_prompt(update, context, YS_OP_AMOUNT)


async def on_ystmt_op_dt_input(update, context) -> int:
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            update.effective_message.text,
            data["ystmt_period_start"],
            data["ystmt_period_end"],
        )
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_DT_INPUT)
    draft = dict(data.get("ystmt_draft") or {})
    draft["datetime"] = moment.strftime("%d.%m.%Y %H:%M")
    data["ystmt_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OP_AMOUNT)


async def on_ystmt_op_amount(update, context) -> int:
    data = context.user_data
    draft = dict(data.get("ystmt_draft") or {})
    try:
        value = parse_positive_amount(update.effective_message.text)
    except ValueError as error:
        return await _ystmt_invalid(update, context, error, YS_OP_AMOUNT)
    if draft.get("kind") in ("in", "out"):
        sign = 1 if draft["kind"] == "in" else -1
        description = build_ystmt_sbp_description(
            draft["kind"], draft.get("name", ""), draft.get("phone", ""), draft.get("bank", "")
        )
    else:
        sign = int(draft.get("sign") or 1)
        description = draft.get("description", "")
    data.setdefault("ystmt_ops", []).append(
        {
            "datetime": draft.get("datetime", ""),
            "description": description,
            "amount": format_signed_amount(value, sign),
        }
    )
    data["ystmt_draft"] = None
    await _delete_user_message(update, context)
    return await _send_ystmt_prompt(update, context, YS_OPS_EDITOR)


async def on_ystmt_op_delete_choice(update, context) -> int:
    await update.callback_query.answer()
    index = int(update.callback_query.data.split(":", 1)[1])
    ops = list(context.user_data.get("ystmt_ops") or [])
    if 0 <= index < len(ops):
        removed = ops.pop(index)
        context.user_data["ystmt_ops"] = ops
        prefix = f"🗑 Удалена операция: {removed['datetime']} {removed['amount']} ₽\n\n"
    else:
        prefix = ""
    return await _send_ystmt_prompt(update, context, YS_OPS_EDITOR, prefix=prefix)


async def on_ystmt_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    buttons = _rows(
        [_btn(label, f"ysedit:{key}") for key, (label, _state) in YSTMT_EDIT_FIELDS.items()]
    )
    buttons.append(_nav_row())
    context.user_data["current_step"] = YS_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return YS_EDIT_SELECT


async def on_ystmt_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state = YSTMT_EDIT_FIELDS[key]
    context.user_data["editing"] = True
    return await _send_ystmt_prompt(update, context, state)


def _ystmt_caption(data: dict) -> str:
    text = _ystmt_summary_text(data)
    plain = re.sub(r"</?b>", "", text)
    return "✅ Выписка готова\n" + plain.replace("<b>", "").replace("</b>", "")


async def on_ystmt_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую выписку…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_yandex_stmt_"))
    filename = receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_yandex_statement_pdf(data, output_path)
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    _btn("🔁 Ещё выписка", Y_DOC_STMT_CB),
                    _btn("🧾 Чек", Y_DOC_CHECK_CB),
                ],
                [_btn("🏠 Главное меню", MAIN_MENU_CB)],
            ]
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=_ystmt_caption(data), reply_markup=markup,
            )
    except Exception as error:  # движок упал — бот жив, возврат в сводку
        logger.exception(
            "Yandex statement generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_ystmt_summary(
            update, context, prefix=f"{explain_ystmt_error(error)}\n\n"
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_ystmt_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Рокет: хендлеры меню, чеков СБП/по карте
# ---------------------------------------------------------------------------


async def _send_main_menu(update, context) -> int:
    context.user_data["current_step"] = BANK_SELECT
    await _send_tracked_message(
        update, context,
        "Здравствуйте! Сформирую чек перевода или выписку.\nВыберите банк:",
        reply_markup=_main_menu_markup(getattr(update.effective_user, "id", None)),
    )
    return BANK_SELECT


async def _send_rocket_menu(update, context) -> int:
    context.user_data["current_step"] = R_MENU
    await _send_tracked_message(
        update, context,
        "🚀 <b>Рокетбанк</b>. Что сформировать?",
        reply_markup=_rocket_menu_markup(),
    )
    return R_MENU


async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _clear_draft(context)
    return await _send_main_menu(update, context)


async def on_rocket_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    # Из главного меню входим с чистого черновика; «Назад» сюда ведёт
    # отдельный on_nav_back и черновик не трогает.
    if context.user_data.get("current_step") in (None, BANK_SELECT):
        _cleanup_stmt_files(dict(context.user_data))
        _clear_draft(context)
    return await _send_rocket_menu(update, context)


async def on_rocket_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "rocket_sbp"
    return await _send_rocket_prompt(update, context, R_RECIPIENT_NAME)


async def on_rocket_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "rocket_card"
    return await _send_rocket_prompt(update, context, R_RECIPIENT_CARD)


async def on_nav_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    data = context.user_data
    if str(data.get("scenario") or "") in (
        "rshb", "ozon", "ozon_menu", "ozon_edit", "tbank", "tbank_menu", "tbank_edit",
    ):
        import extra_banks
        return await extra_banks.on_back(update, context)
    if data.get("scenario") == "admin_access":
        return await _send_main_menu(update, context)
    current = data.get("current_step")
    if current == S_UPLOAD_PENDING:
        pending = data.pop("stmt_pending_receipt_path", None)
        data.pop("stmt_pending_receipt_date", None)
        if pending:
            try:
                Path(str(pending)).unlink(missing_ok=True)
            except OSError:
                pass
        return await _send_stmt_prompt(update, context, S_UPLOAD)
    if data.pop("editing", False):
        if data.get("scenario") == "yandex":
            return await _send_summary(update, context)
        if data.get("scenario") == "yandex_statement":
            return await _send_ystmt_summary(update, context)
        if data.get("scenario") == "mts":
            return await _send_mts_summary(update, context)
        if data.get("scenario") == "mts_card":
            return await _send_mts_card_summary(update, context)
        if data.get("scenario") in ("vtb_sbp", "vtb_card"):
            return await _send_vtb_summary(update, context)
        if data.get("scenario") == "wb":
            return await _send_wb_summary(update, context)
        if current in STATEMENT_EDIT_STATES.values() or data.get("scenario") in (
            "rocket_statement", "mts_statement", "vtb_statement",
        ):
            return await _send_stmt_summary(update, context)
        return await _send_rocket_summary(update, context)
    if data.get("scenario") == "alfa":
        target = _back_target(data, current)
        if target is None:
            _cleanup_alfa_upload(dict(data))
            _clear_draft(context)
            return await _send_main_menu(update, context)
        return await _send_alfa_prompt(update, context, target)
    if data.get("scenario") == "yandex_statement":
        target = _back_target(data, current)
        if target is None or target == Y_MENU:
            if target is None:
                _clear_draft(context)
                return await _send_main_menu(update, context)
            return await _send_yandex_menu(update, context)
        if target == YS_SUMMARY:
            return await _send_ystmt_summary(update, context)
        return await _send_ystmt_prompt(update, context, target)
    if data.get("scenario") == "yandex":
        target = _back_target(data, current)
        if target is None:
            _cleanup_yandex_template_files(dict(data))
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == Y_MENU:
            return await _send_yandex_menu(update, context)
        if target == Y_MODE:
            return await _send_yandex_mode(update, context)
        if target == CONFIRM:
            return await _send_summary(update, context)
        return await _send_prompt(update, context, target)
    if data.get("scenario") == "mts":
        target = _back_target(data, current)
        if target is None:
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == M_MENU:
            return await _send_mts_menu(update, context)
        if target == M_SUMMARY:
            return await _send_mts_summary(update, context)
        return await _send_mts_prompt(update, context, target)
    if data.get("scenario") == "mts_card":
        target = _back_target(data, current)
        if target is None:
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == M_MENU:
            return await _send_mts_menu(update, context)
        if target == MC_SUMMARY:
            return await _send_mts_card_summary(update, context)
        return await _send_mts_card_prompt(update, context, target)
    if data.get("scenario") == "vtb_upload":
        target = _back_target(data, current)
        if target is None:
            _cleanup_vtb_upload(dict(data))
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == V_MENU:
            _cleanup_vtb_upload(dict(data))
            return await _send_vtb_menu(update, context)
        return await _send_vtb_upload_prompt(update, context, target)
    if data.get("scenario") in ("vtb_sbp", "vtb_card"):
        target = _back_target(data, current)
        if target is None:
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == V_MENU:
            return await _send_vtb_menu(update, context)
        if target == V_SUMMARY:
            return await _send_vtb_summary(update, context)
        return await _send_vtb_prompt(update, context, target)
    if data.get("scenario") == "wb":
        target = _back_target(data, current)
        if target is None:
            _clear_draft(context)
            return await _send_main_menu(update, context)
        if target == W_MENU:
            return await _send_wb_menu(update, context)
        if target == W_SUMMARY:
            return await _send_wb_summary(update, context)
        return await _send_wb_prompt(update, context, target)
    target = _back_target(data, current)
    if target is None:
        _cleanup_stmt_files(dict(context.user_data))
        _clear_draft(context)
        return await _send_main_menu(update, context)
    if target == R_MENU:
        return await _send_rocket_menu(update, context)
    if target == M_MENU:
        return await _send_mts_menu(update, context)
    if target == V_MENU:
        return await _send_vtb_menu(update, context)
    if target == W_MENU:
        return await _send_wb_menu(update, context)
    if target == R_SUMMARY:
        return await _send_rocket_summary(update, context)
    if target == S_SUMMARY:
        return await _send_stmt_summary(update, context)
    stmt_states = {
        S_PERIOD, S_ISSUE_CHOICE, S_ISSUE_INPUT, S_HOLDER, S_PHONE,
        S_ACCOUNT_CHOICE, S_ACCOUNT_TAIL_INPUT, S_ACCOUNT_FULL_INPUT, S_CARD,
        S_OPENING, S_OP_COUNT_CHOICE, S_OP_COUNT_INPUT, S_SOURCE_CHOICE,
        S_UPLOAD, S_UPLOAD_PENDING, S_SHORTAGE, S_OP_DAY_CHOICE,
        S_OP_DATE_INPUT, S_EDIT_SELECT,
        S_OPS_EDITOR, S_OP_KIND, S_OP_AMOUNT, S_OP_DT_CHOICE,
        S_OP_DT_INPUT, S_OP_DETAIL, S_OP_DELETE,
    }
    if target in stmt_states:
        return await _send_stmt_prompt(update, context, target)
    return await _send_rocket_prompt(update, context, target)


async def on_r_recipient_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_name"] = parse_name(
            update.effective_message.text, "имя получателя"
        )
    except ValueError as error:
        return await _invalid(update, error, R_RECIPIENT_NAME, context)
    return await _rocket_after_field(update, context, R_RECIPIENT_PHONE)


async def on_r_recipient_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, R_RECIPIENT_PHONE, context)
    return await _rocket_after_field(update, context, R_RECIPIENT_BANK)


async def on_r_recipient_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["recipient_card"] = parse_recipient_card(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, R_RECIPIENT_CARD, context)
    return await _rocket_after_field(update, context, R_RECIPIENT_BANK)


async def on_r_mem_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    scenario = context.user_data.get("scenario")
    if scenario == "rocket_card":
        mem = context.user_data.get("mem_rocket_recipient_card") or {}
        context.user_data["recipient_card"] = mem.get("recipient_card")
        context.user_data["recipient_bank"] = mem.get("recipient_bank")
        context.user_data["recipient_bank_label"] = mem.get("recipient_bank_label")
    else:
        mem = context.user_data.get("mem_rocket_recipient_sbp") or {}
        context.user_data["recipient_name"] = mem.get("recipient_name")
        context.user_data["recipient_phone"] = mem.get("recipient_phone")
        context.user_data["recipient_bank"] = mem.get("recipient_bank")
        context.user_data["recipient_bank_label"] = mem.get("recipient_bank_label")
    return await _rocket_after_field(update, context, R_AMOUNT)


async def on_r_bank_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_rocket_prompt(update, context, R_RECIPIENT_BANK_CUSTOM)
    options = (
        ROCKET_CARD_BANK_OPTIONS
        if context.user_data.get("scenario") == "rocket_card"
        else ROCKET_SBP_BANK_OPTIONS
    )
    for key, label, engine_value in options:
        if key == slug:
            context.user_data["recipient_bank"] = engine_value
            context.user_data["recipient_bank_label"] = label
            break
    return await _rocket_after_field(update, context, R_AMOUNT)


async def on_r_bank_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        bank = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, R_RECIPIENT_BANK_CUSTOM, context)
    # Карточный движок сам обрежет юридическую форму; ВБ и там, и тут —
    # это «без блока банка» (None), чтобы сводка и вызов не разошлись.
    if context.user_data.get("scenario") == "rocket_card" and bank.casefold().replace("ё", "е") in {
        "вб банк", "вб", "wb bank", "wb", "wildberries", "вайлдберриз",
    }:
        context.user_data["recipient_bank"] = None
        context.user_data["recipient_bank_label"] = "Wildberries (в чеке без банка)"
    else:
        context.user_data["recipient_bank"] = bank
        context.user_data["recipient_bank_label"] = bank
    return await _rocket_after_field(update, context, R_AMOUNT)


async def on_r_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["amount"] = parse_rocket_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, R_AMOUNT, context)
    return await _rocket_after_field(update, context, R_SENDER_NAME)


async def on_r_sender_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["sender_name"] = parse_name(
            update.effective_message.text, "ФИО отправителя"
        )
    except ValueError as error:
        return await _invalid(update, error, R_SENDER_NAME, context)
    next_state = (
        R_SENDER_ACCOUNT_CHOICE
        if context.user_data.get("scenario") == "rocket_card"
        else R_SENDER_PHONE
    )
    return await _rocket_after_field(update, context, next_state)


async def on_r_mem_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_rocket_sender") or {}
    context.user_data["sender_name"] = mem.get("sender_name")
    if context.user_data.get("scenario") != "rocket_card":
        context.user_data["sender_phone"] = mem.get("sender_phone")
    context.user_data["sender_account"] = mem.get("sender_account")
    return await _rocket_after_field(update, context, R_TIME_CHOICE)


async def on_r_sender_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["sender_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, R_SENDER_PHONE, context)
    return await _rocket_after_field(update, context, R_SENDER_ACCOUNT_CHOICE)


async def on_r_sender_account_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_rocket_prompt(update, context, R_SENDER_ACCOUNT_INPUT)
    if slug.startswith("rand"):
        candidates = context.user_data.get("sender_account_candidates") or []
        index = int(slug[-1])
        context.user_data["sender_account"] = (
            candidates[index] if index < len(candidates) else random_rocket_account()
        )
        return await _rocket_after_field(update, context, R_TIME_CHOICE)
    context.user_data["sender_account"] = None
    return await _rocket_after_field(update, context, R_TIME_CHOICE)


async def on_r_sender_account_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["sender_account"] = parse_account20(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, R_SENDER_ACCOUNT_INPUT, context)
    return await _rocket_after_field(update, context, R_TIME_CHOICE)


async def on_r_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_rocket_prompt(update, context, R_TIME_INPUT)
    context.user_data["operation_dt"] = rocket_current_time()
    context.user_data.pop("editing", False)
    return await _send_rocket_summary(update, context)


async def on_r_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["operation_dt"] = parse_rocket_time(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, R_TIME_INPUT, context)
    await _delete_user_message(update, context)
    context.user_data.pop("editing", False)
    return await _send_rocket_summary(update, context)


async def on_r_summary_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    buttons = _rows(
        [
            _btn(label, f"redit:{key}")
            for key, label, _state in _rocket_edit_fields(context.user_data.get("scenario"))
        ]
    )
    buttons.append(_nav_row())
    context.user_data["current_step"] = R_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return R_EDIT_SELECT


async def on_r_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    state = ROCKET_EDIT_STATES[key]
    context.user_data["editing"] = True
    return await _send_rocket_prompt(update, context, state)


def _rocket_caption(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    if data.get("scenario") == "rocket_card":
        who = f"Карта {data.get('recipient_card')} · {data.get('recipient_bank_label') or 'без банка'}"
    else:
        who = (
            f"{data.get('recipient_name')} · {data.get('recipient_phone')} · "
            f"{data.get('recipient_bank_label') or data.get('recipient_bank')}"
        )
    return (
        "✅ Чек готов\n"
        f"{who}\n"
        f"Сумма: {data.get('amount')}\n"
        f"Отправитель: {data.get('sender_name')} · счёт {masked_account(data.get('sender_account'))}\n"
        f"Время: {moment:%d.%m.%Y} {moment:%H:%M} МСК"
    )


async def on_r_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    scenario = data.get("scenario")
    restart_cb = ROCKET_CARD_CB if scenario == "rocket_card" else ROCKET_SBP_CB
    restart_label = "🔁 Ещё чек"
    await _send_tracked_message(update, context, "⏳ Формирую чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_rocket_"))
    output_path = tmp_dir / rocket_receipt_filename(data)
    try:
        if scenario == "rocket_card":
            pdf_path = await generate_rocket_card_pdf(data, output_path)
        else:
            pdf_path = await generate_rocket_pdf(data, output_path)
        markup = InlineKeyboardMarkup(
            [[_btn(restart_label, restart_cb)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle,
                filename=rocket_receipt_filename(data),
                caption=_rocket_caption(data), reply_markup=markup,
            )
    except Exception as error:  # движок упал — бот жив, возврат в сводку
        logger.exception(
            "Rocket receipt generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_rocket_summary(
            update, context,
            prefix=(
                f"Не получилось: {_short_error(error)}\n"
                "Исправьте данные в сводке и попробуйте ещё раз.\n\n"
            ),
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_rocket_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Рокет: хендлеры выписки
# ---------------------------------------------------------------------------


async def on_rocket_stmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data.update(
        {
            "scenario": "rocket_statement",
            "stmt_receipt_paths": [],
            "stmt_receipt_dates": [],
            "stmt_fill_random": True,
            "stmt_operation_date": None,
            "stmt_account_mode": "keep",
            "stmt_account": None,
            "stmt_account_tail": None,
        }
    )
    return await _send_stmt_prompt(update, context, S_PERIOD)


async def on_s_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        start, end = parse_statement_period(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, S_PERIOD, context)
    context.user_data["stmt_period_start"] = start
    context.user_data["stmt_period_end"] = end
    return await _stmt_after_field(update, context, S_ISSUE_CHOICE)


async def on_s_issue_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_stmt_prompt(update, context, S_ISSUE_INPUT)
    context.user_data["stmt_issue_date"] = rocket_current_time().date()
    return await _stmt_after_field(update, context, S_HOLDER)


async def on_s_issue_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_issue_date"] = parse_statement_date(
            update.effective_message.text, "дату выдачи"
        )
    except ValueError as error:
        return await _invalid(update, error, S_ISSUE_INPUT, context)
    return await _stmt_after_field(update, context, S_HOLDER)


async def on_s_holder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_holder_name"] = parse_name(
            update.effective_message.text, "ФИО держателя"
        )
    except ValueError as error:
        return await _invalid(update, error, S_HOLDER, context)
    next_state = S_PHONE if stmt_bank_of(context.user_data) == "rocket" else S_ACCOUNT_CHOICE
    return await _stmt_after_field(update, context, next_state)


async def on_s_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, S_PHONE, context)
    return await _stmt_after_field(update, context, S_ACCOUNT_CHOICE)


async def on_s_account_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    bank = stmt_bank_of(context.user_data)
    if slug == "tail":
        if bank != "rocket":
            return await _send_stmt_prompt(update, context, S_ACCOUNT_CHOICE)
        return await _send_stmt_prompt(update, context, S_ACCOUNT_TAIL_INPUT)
    if slug == "full":
        return await _send_stmt_prompt(update, context, S_ACCOUNT_FULL_INPUT)
    if slug == "random":
        if bank != "rocket":
            return await _send_stmt_prompt(update, context, S_ACCOUNT_CHOICE)
        context.user_data["stmt_account_mode"] = "tail"
        context.user_data["stmt_account_tail"] = "".join(
            secrets.choice("0123456789") for _ in range(5)
        )
        context.user_data["stmt_account"] = None
    else:  # keep
        context.user_data["stmt_account_mode"] = "keep"
        context.user_data["stmt_account"] = None
        context.user_data["stmt_account_tail"] = None
    next_state = S_CARD if stmt_bank_of(context.user_data) == "mts" else S_OPENING
    return await _stmt_after_field(update, context, next_state)


async def on_s_account_tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_account_tail"] = parse_account_tail(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_ACCOUNT_TAIL_INPUT, context)
    context.user_data["stmt_account_mode"] = "tail"
    context.user_data["stmt_account"] = None
    next_state = S_CARD if stmt_bank_of(context.user_data) == "mts" else S_OPENING
    return await _stmt_after_field(update, context, next_state)


async def on_s_account_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_account"] = parse_account20(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_ACCOUNT_FULL_INPUT, context)
    context.user_data["stmt_account_mode"] = "full"
    context.user_data["stmt_account_tail"] = None
    next_state = S_CARD if stmt_bank_of(context.user_data) == "mts" else S_OPENING
    return await _stmt_after_field(update, context, next_state)


async def on_s_card_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["stmt_card"] = None
    return await _stmt_after_field(update, context, S_OPENING)


async def on_s_card_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_card"] = parse_mts_card(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, S_CARD, context)
    return await _stmt_after_field(update, context, S_OPENING)


async def on_s_opening(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_opening_balance"] = parse_opening_balance(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_OPENING, context)
    return await _stmt_after_field(update, context, S_OP_COUNT_CHOICE)


async def _s_after_count_set(update, context) -> int:
    """Число платежей выбрано: дальше источник, день или сразу сводка."""
    data = context.user_data
    total = int(data.get("stmt_operation_count") or 0)
    uploaded = len(data.get("stmt_receipt_paths") or [])
    editing = bool(data.pop("editing", False))
    if total < uploaded:
        data["editing"] = editing
        return await _send_stmt_prompt(
            update, context, S_OP_COUNT_CHOICE,
            prefix=(
                f"⚠️ Уже загружено чеков: {uploaded}, поэтому платежей "
                "не может быть меньше. Выберите число не меньше "
                f"{uploaded} или начните выписку заново через «Отмена».\n\n"
            ),
        )
    if total == 0:
        data["stmt_fill_random"] = False
        data["stmt_operation_date"] = None
        return await _send_stmt_summary(update, context)
    if editing:
        if uploaded and total == uploaded:
            data["stmt_fill_random"] = False
            return await _send_stmt_summary(update, context)
        if uploaded:
            return await _send_stmt_prompt(update, context, S_SHORTAGE)
        data["stmt_fill_random"] = True
        return await _send_stmt_prompt(update, context, S_OP_DAY_CHOICE)
    return await _send_stmt_prompt(update, context, S_SOURCE_CHOICE)


async def on_s_op_count_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        if stmt_bank_of(context.user_data) != "rocket":
            return await _send_stmt_prompt(update, context, S_OP_COUNT_CHOICE)
        return await _send_stmt_prompt(update, context, S_OP_COUNT_INPUT)
    value = int(slug)
    allowed = stmt_allowed_counts(stmt_bank_of(context.user_data))
    if value not in allowed:
        return await _send_stmt_prompt(
            update, context, S_OP_COUNT_CHOICE,
            prefix=(
                f"⚠️ Для этой выписки можно только "
                f"{', '.join(str(n) for n in allowed)} платежей.\n\n"
            ),
        )
    context.user_data["stmt_operation_count"] = value
    return await _s_after_count_set(update, context)


async def on_s_op_count_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_operation_count"] = parse_stmt_operation_count(
            update.effective_message.text, stmt_bank_of(context.user_data)
        )
    except ValueError as error:
        return await _invalid(update, error, S_OP_COUNT_INPUT, context)
    return await _s_after_count_set(update, context)


async def on_s_source_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    data = context.user_data
    data["stmt_source"] = slug
    if slug == "manual":
        if data.get("stmt_receipt_paths"):
            _cleanup_stmt_files(dict(data))
            data["stmt_receipt_paths"] = []
            data["stmt_receipt_dates"] = []
        data["stmt_fill_random"] = False
        data.setdefault("stmt_ops", [])
        return await _send_stmt_prompt(update, context, S_OPS_EDITOR)
    if slug == "random":
        if data.get("stmt_receipt_paths"):
            _cleanup_stmt_files(dict(data))
            data["stmt_receipt_paths"] = []
            data["stmt_receipt_dates"] = []
        data["stmt_fill_random"] = True
        data["stmt_ops"] = []
        return await _send_stmt_prompt(update, context, S_OP_DAY_CHOICE)
    data["stmt_fill_random"] = slug == "mixed"
    data["stmt_ops"] = []
    return await _send_stmt_prompt(update, context, S_UPLOAD)


async def on_s_upload_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data
    message = update.effective_message
    document = getattr(message, "document", None)
    total = int(data.get("stmt_operation_count") or 0)
    uploaded_paths = data.setdefault("stmt_receipt_paths", [])
    if document is None or not (document.file_name or "").lower().endswith(".pdf"):
        await _delete_user_message(update, context)
        return await _send_stmt_prompt(
            update, context, S_UPLOAD,
            prefix="⚠️ Нужен именно PDF-чека Рокета. Пришлите файл PDF по одному.\n\n",
        )
    if len(uploaded_paths) >= total and total > 0:
        await _delete_user_message(update, context)
        return await _send_stmt_prompt(
            update, context, S_UPLOAD,
            prefix=(
                f"⚠️ Уже загружено чеков: {len(uploaded_paths)} — этого достаточно "
                f"для {total} платежей. Нажмите «Готово».\n\n"
            ),
        )
    upload_dir = data.get("stmt_upload_dir")
    if not upload_dir:
        upload_dir = tempfile.mkdtemp(prefix="checketop_rocket_stmt_")
        data["stmt_upload_dir"] = upload_dir
    local_path = Path(upload_dir) / f"receipt_{len(uploaded_paths) + 1}_{document.file_id[:12]}.pdf"
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(local_path))
        operation = await asyncio.to_thread(
            import_receipt_operation, local_path, stmt_bank_of(data)
        )
        receipt_date = receipt_operation_date(operation)
    except Exception as error:  # не чек Рокета / битый PDF — не считаем его
        logger.exception("Rocket receipt import for statement failed")
        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            pass
        await _delete_user_message(update, context)
        return await _send_stmt_prompt(
            update, context, S_UPLOAD,
            prefix=(
                f"Не получилось прочитать чек: {_short_error(error)}\n"
                "Пришлите настоящий чек Рокета в PDF или нажмите «Готово».\n\n"
            ),
        )
    start, end = data.get("stmt_period_start"), data.get("stmt_period_end")
    if start and end and not (start <= receipt_date <= end):
        data["stmt_pending_receipt_path"] = str(local_path)
        data["stmt_pending_receipt_date"] = receipt_date.isoformat()
        return await _send_stmt_prompt(update, context, S_UPLOAD_PENDING)
    uploaded_paths.append(str(local_path))
    data.setdefault("stmt_receipt_dates", []).append(receipt_date.isoformat())
    await _delete_user_message(update, context)
    return await _send_stmt_prompt(
        update, context, S_UPLOAD,
        prefix=(
            f"Загружено чеков: {len(uploaded_paths)}"
            + (" — достаточно, можно нажимать «Готово»." if len(uploaded_paths) >= total else "")
            + "\n\n"
        ),
    )


async def on_s_upload_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_user_message(update, context)
    context.user_data["current_step"] = S_UPLOAD
    return await _send_stmt_prompt(
        update, context, S_UPLOAD,
        prefix="⚠️ Пришлите чек файлом PDF (по одному) или нажмите «Готово».\n\n",
    )


async def on_s_period_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = context.user_data
    pending_path = data.pop("stmt_pending_receipt_path", None)
    pending_date_raw = data.pop("stmt_pending_receipt_date", None)
    if not pending_path or not pending_date_raw:
        return await _send_stmt_prompt(update, context, S_UPLOAD)
    receipt_date = date.fromisoformat(str(pending_date_raw))
    start, end = data["stmt_period_start"], data["stmt_period_end"]
    data["stmt_period_start"] = min(start, receipt_date)
    data["stmt_period_end"] = max(end, receipt_date)
    data.setdefault("stmt_receipt_paths", []).append(str(pending_path))
    data.setdefault("stmt_receipt_dates", []).append(receipt_date.isoformat())
    return await _send_stmt_prompt(
        update, context, S_UPLOAD,
        prefix=(
            f"📅 Период изменён под чек: {data['stmt_period_start']:%d.%m.%Y}–"
            f"{data['stmt_period_end']:%d.%m.%Y}.\n"
            f"Загружено чеков: {len(data['stmt_receipt_paths'])}\n\n"
        ),
    )


async def on_s_uploads_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = context.user_data
    uploaded = len(data.get("stmt_receipt_paths") or [])
    total = int(data.get("stmt_operation_count") or 0)
    if statement_after_upload_action(uploaded, total) == "shortage":
        return await _send_stmt_prompt(update, context, S_SHORTAGE)
    data["stmt_fill_random"] = False
    return await _send_stmt_summary(update, context)


async def on_s_shortage_fill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data["stmt_fill_random"] = True
    return await _send_stmt_prompt(update, context, S_OP_DAY_CHOICE)


async def on_s_shortage_reduce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    data = context.user_data
    data["stmt_operation_count"] = len(data.get("stmt_receipt_paths") or [])
    data["stmt_fill_random"] = False
    data["stmt_operation_date"] = None
    return await _send_stmt_summary(update, context)


async def on_s_day_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    dates = context.user_data.get("stmt_receipt_dates") or []
    if not dates:
        return await _send_stmt_prompt(update, context, S_OP_DATE_INPUT)
    context.user_data["stmt_operation_date"] = date.fromisoformat(str(dates[0]))
    context.user_data.pop("editing", False)
    return await _send_stmt_summary(update, context)


async def on_s_day_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _send_stmt_prompt(update, context, S_OP_DATE_INPUT)


async def on_s_op_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data
    try:
        chosen = parse_statement_date(update.effective_message.text, "дату операций")
    except ValueError as error:
        return await _invalid(update, error, S_OP_DATE_INPUT, context)
    start, end = data.get("stmt_period_start"), data.get("stmt_period_end")
    if start and end and not (start <= chosen <= end):
        return await _invalid(
            update,
            ValueError(
                f"Дата вне периода {start:%d.%m.%Y}–{end:%d.%m.%Y}. "
                f"Введите, например: {start:%d.%m.%Y}"
            ),
            S_OP_DATE_INPUT, context,
        )
    receipt_dates = data.get("stmt_receipt_dates") or []
    if receipt_dates and str(chosen.isoformat()) != str(receipt_dates[0]):
        first = date.fromisoformat(str(receipt_dates[0]))
        return await _invalid(
            update,
            ValueError(
                "Случайные операции выписки должны быть в тот же день, "
                f"что и загруженный чек ({first:%d.%m.%Y}), иначе движок "
                "честно откажет. Выберите эту дату или «День загруженного чека»."
            ),
            S_OP_DATE_INPUT, context,
        )
    data["stmt_operation_date"] = chosen
    data.pop("editing", False)
    await _delete_user_message(update, context)
    return await _send_stmt_summary(update, context)


async def on_s_summary_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    buttons = _rows(
        [
            _btn(label, f"sedit:{key}")
            for key, label, _state in _statement_edit_fields(context.user_data)
        ]
    )
    buttons.append(_nav_row())
    context.user_data["current_step"] = S_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return S_EDIT_SELECT


async def on_s_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    context.user_data["editing"] = True
    return await _send_stmt_prompt(update, context, STATEMENT_EDIT_STATES[key])


def _stmt_caption(data: dict) -> str:
    return (
        "✅ Выписка готова\n"
        f"Период: {data['stmt_period_start']:%d.%m.%Y}–{data['stmt_period_end']:%d.%m.%Y}\n"
        f"Держатель: {data.get('stmt_holder_name')}\n"
        f"Счёт: {statement_account_display(data)}\n"
        f"Входящий остаток: {format_money_display(data.get('stmt_opening_balance', '0'))}\n"
        f"{statement_sources_text(data)}"
    )


async def on_s_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    bank = stmt_bank_of(context.user_data)
    if context.user_data.get("stmt_source") == "manual":
        ops = list(context.user_data.get("stmt_ops") or [])
        allowed = stmt_allowed_counts(bank)
        if len(ops) not in allowed:
            pretty = ", ".join(str(n) for n in allowed)
            return await _send_stmt_summary(
                update, context,
                prefix=(
                    f"⚠️ Сейчас операций: {len(ops)}. Для этой выписки "
                    f"нужно ровно {pretty}. Добавьте или удалите операции.\n\n"
                ),
            )
        context.user_data["stmt_operation_count"] = len(ops)
        context.user_data["stmt_fill_random"] = False
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую выписку…")
    prefix = {
        "mts": "checketop_mts_stmt_out_",
        "vtb": "checketop_vtb_stmt_out_",
    }.get(bank, "checketop_rocket_stmt_out_")
    tmp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    if bank == "mts":
        filename = mts_statement_filename(data)
        generate = generate_mts_statement_pdf
        again_cb = MTS_STMT_CB
    elif bank == "vtb":
        filename = vtb_statement_filename(data)
        generate = generate_vtb_statement_pdf
        again_cb = VTB_STMT_CB
    else:
        filename = rocket_statement_filename(data)
        generate = generate_rocket_statement_pdf
        again_cb = ROCKET_STMT_CB
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate(data, output_path)
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Новая выписка", again_cb)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=_stmt_caption(data), reply_markup=markup,
            )
    except Exception as error:  # движок упал — бот жив, возврат в сводку
        logger.exception(
            "Statement generation failed (user_id=%s, chat_id=%s, bank=%s)",
            getattr(user, "id", "?"), chat_id, bank,
        )
        return await _send_stmt_summary(
            update, context,
            prefix=(
                f"Не получилось: {_short_error(error)}\n"
                "Исправьте данные в сводке и попробуйте ещё раз.\n\n"
            ),
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _cleanup_stmt_files(data)
    _clear_draft(context)
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик: любой сбой Telegram/хендлеров — в лог-файл."""
    logger.exception("Unhandled bot error", exc_info=context.error)


# ---------------------------------------------------------------------------
# МТС: чек СБП
# ---------------------------------------------------------------------------


def _save_mts_memory(context) -> None:
    data = context.user_data
    if data.get("recipient_name"):
        data["mem_mts_recipient"] = {
            "recipient_name": data.get("recipient_name"),
            "recipient_phone": data.get("recipient_phone"),
            "recipient_bank": data.get("recipient_bank"),
        }


def _mts_prompt(context, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == M_RECIPIENT_NAME:
        rows = []
        mem = data.get("mem_mts_recipient") or {}
        if mem.get("recipient_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_name']})",
                M_MEM_RECIPIENT_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Получатель.</b> Введите ФИО.\n"
            "Нажмите, чтобы скопировать: <code>Петрова Анна Ивановна</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == M_RECIPIENT_PHONE:
        return (
            "📱 <b>Телефон получателя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 963 297-20-08</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == M_RECIPIENT_BANK:
        buttons = [_btn(label, f"mbank:{slug}") for slug, label in MTS_BANK_OPTIONS]
        buttons.append(_btn("✍️ Ввести свой", "mbank:custom"))
        return (
            "🏦 <b>Банк получателя</b> — как в чеке МТС:",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == M_RECIPIENT_BANK_CUSTOM:
        return (
            "🏦 Введите банк получателя.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == M_AMOUNT:
        return (
            "💰 <b>Сумма.</b> Копейки можно. Комиссия в чеке всегда 0,00 ₽.\n"
            "Нажмите, чтобы скопировать: <code>1 500,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == M_ACCOUNT_CHOICE:
        rows = [
            [_btn("↩️ Оставить свой", "macc:keep"), _btn("✍️ Ввести свой целиком", "macc:custom")],
            nav,
        ]
        return (
            "💳 <b>Счёт списания.</b> «Оставить свой» — счёт из шаблона. "
            "Свой — 20 цифр.",
            InlineKeyboardMarkup(rows),
        )
    if state == M_ACCOUNT_INPUT:
        return (
            "💳 Введите счёт целиком — <b>20 цифр</b>.",
            InlineKeyboardMarkup([nav]),
        )
    if state == M_MESSAGE_CHOICE:
        rows = [
            [_btn("Перевод СБП", "mmsg:default"), _btn("✍️ Ввести своё", "mmsg:custom")],
            nav,
        ]
        return ("💬 <b>Сообщение</b> в чеке:", InlineKeyboardMarkup(rows))
    if state == M_MESSAGE_INPUT:
        return (
            "💬 Введите короткое сообщение кириллицей.\n"
            "Нажмите, чтобы скопировать: <code>Перевод СБП</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == M_TIME_CHOICE:
        rows = [
            [_btn("🕒 Текущее время", "mtime:now"), _btn("✏️ Изменить время", "mtime:change")],
            nav,
        ]
        return (
            "🕒 <b>Время операции.</b> Секунды движок выведет сам.",
            InlineKeyboardMarkup(rows),
        )
    if state == M_TIME_INPUT:
        return (
            "🕒 Введите время операции.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 14:20</code> "
            "или только <code>14:20</code> (= сегодня).",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no mts prompt for state {state}")


async def _send_mts_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _mts_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _mts_summary_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    account = data.get("sender_account")
    account_text = masked_account(account) if account else "счёт шаблона"
    message = data.get("mts_message") or "Перевод СБП"
    lines = [
        "<b>Проверь данные</b>",
        "",
        "<b>ПОЛУЧАТЕЛЬ</b>",
        f"👤 Получатель: <b>{html.escape(str(data.get('recipient_name', '')))}</b>",
        f"📱 Телефон: <b>{html.escape(str(data.get('recipient_phone', '')))}</b>",
        f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank', '')))}</b>",
        f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>",
        f"💬 Сообщение: <b>{html.escape(message)}</b>",
        "",
        "<b>СПИСАНИЕ</b>",
        f"💳 Счёт: <b>{html.escape(account_text)}</b>",
        "Комиссия: <b>0,00 ₽</b>",
        f"🕒 Время: <b>{moment:%d.%m.%Y} {moment:%H:%M}</b> (МСК)",
    ]
    return "\n".join(lines)


def _mts_summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("✅ Сгенерировать", M_GENERATE_CB), _btn("✏️ Исправить", M_EDIT_CB)], _nav_row()]
    )


async def _send_mts_summary(update, context, prefix: str = "") -> int:
    _save_mts_memory(context)
    context.user_data["current_step"] = M_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_mts_summary_text(context.user_data)}",
        reply_markup=_mts_summary_markup(),
    )
    return M_SUMMARY


async def _mts_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_mts_summary(update, context)
    return await _send_mts_prompt(update, context, next_state)


async def _mts_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_mts_summary(update, context)
    return await _send_mts_prompt(update, context, next_state)


MTS_EDIT_FIELDS = {
    "recipient_name": ("👤 Получатель", M_RECIPIENT_NAME),
    "recipient_phone": ("📱 Телефон", M_RECIPIENT_PHONE),
    "recipient_bank": ("🏦 Банк", M_RECIPIENT_BANK),
    "amount": ("💰 Сумма", M_AMOUNT),
    "sender_account": ("💳 Счёт списания", M_ACCOUNT_CHOICE),
    "message": ("💬 Сообщение", M_MESSAGE_CHOICE),
    "operation_time": ("🕒 Время", M_TIME_CHOICE),
}


async def _send_mts_menu(update, context) -> int:
    context.user_data["current_step"] = M_MENU
    await _send_tracked_message(
        update, context,
        "🔴 <b>МТС Банк</b>. Что сформировать?",
        reply_markup=_mts_menu_markup(),
    )
    return M_MENU


async def on_mts_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    if context.user_data.get("current_step") in (None, BANK_SELECT):
        _cleanup_stmt_files(dict(context.user_data))
        _cleanup_yandex_template_files(dict(context.user_data))
        _clear_draft(context)
    return await _send_mts_menu(update, context)


async def on_mts_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "mts"
    return await _send_mts_prompt(update, context, M_RECIPIENT_NAME)


async def on_mts_name(update, context) -> int:
    try:
        context.user_data["recipient_name"] = parse_name(
            update.effective_message.text, "ФИО получателя"
        )
    except ValueError as error:
        return await _invalid(update, error, M_RECIPIENT_NAME, context)
    return await _mts_after_field(update, context, M_RECIPIENT_PHONE)


async def on_mts_mem_recipient(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_mts_recipient") or {}
    context.user_data["recipient_name"] = mem.get("recipient_name")
    context.user_data["recipient_phone"] = mem.get("recipient_phone")
    context.user_data["recipient_bank"] = mem.get("recipient_bank")
    return await _mts_choice_next(update, context, M_AMOUNT)


async def on_mts_phone(update, context) -> int:
    try:
        context.user_data["recipient_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_RECIPIENT_PHONE, context)
    return await _mts_after_field(update, context, M_RECIPIENT_BANK)


async def on_mts_bank_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_mts_prompt(update, context, M_RECIPIENT_BANK_CUSTOM)
    for key, label in MTS_BANK_OPTIONS:
        if key == slug:
            context.user_data["recipient_bank"] = label
            break
    return await _mts_choice_next(update, context, M_AMOUNT)


async def on_mts_bank_custom(update, context) -> int:
    try:
        context.user_data["recipient_bank"] = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_RECIPIENT_BANK_CUSTOM, context)
    return await _mts_after_field(update, context, M_AMOUNT)


async def on_mts_amount(update, context) -> int:
    try:
        context.user_data["amount"] = parse_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_AMOUNT, context)
    return await _mts_after_field(update, context, M_ACCOUNT_CHOICE)


async def on_mts_account_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_mts_prompt(update, context, M_ACCOUNT_INPUT)
    context.user_data["sender_account"] = None
    return await _mts_choice_next(update, context, M_MESSAGE_CHOICE)


async def on_mts_account_input(update, context) -> int:
    try:
        context.user_data["sender_account"] = parse_account20(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_ACCOUNT_INPUT, context)
    return await _mts_after_field(update, context, M_MESSAGE_CHOICE)


async def on_mts_message_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_mts_prompt(update, context, M_MESSAGE_INPUT)
    context.user_data["mts_message"] = None
    return await _mts_choice_next(update, context, M_TIME_CHOICE)


async def on_mts_message_input(update, context) -> int:
    try:
        context.user_data["mts_message"] = parse_mts_message(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_MESSAGE_INPUT, context)
    return await _mts_after_field(update, context, M_TIME_CHOICE)


async def on_mts_time_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_mts_prompt(update, context, M_TIME_INPUT)
    context.user_data["operation_dt"] = rocket_current_time()
    context.user_data.pop("editing", False)
    return await _send_mts_summary(update, context)


async def on_mts_time_input(update, context) -> int:
    try:
        context.user_data["operation_dt"] = parse_rocket_time(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, M_TIME_INPUT, context)
    await _delete_user_message(update, context)
    context.user_data.pop("editing", False)
    return await _send_mts_summary(update, context)


async def on_mts_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    buttons = _rows([
        _btn(label, f"medit:{key}") for key, (label, _state) in MTS_EDIT_FIELDS.items()
    ])
    buttons.append(_nav_row())
    context.user_data["current_step"] = M_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return M_EDIT_SELECT


async def on_mts_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state = MTS_EDIT_FIELDS[key]
    context.user_data["editing"] = True
    return await _send_mts_prompt(update, context, state)


def _mts_caption(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    account = data.get("sender_account")
    account_text = masked_account(account) if account else "счёт шаблона"
    return (
        "✅ Чек готов\n"
        f"Получатель: {data.get('recipient_name')} · {data.get('recipient_phone')} · "
        f"{data.get('recipient_bank')}\n"
        f"Сумма: {data.get('amount')}\n"
        f"Счёт: {account_text}\n"
        f"Сообщение: {data.get('mts_message') or 'Перевод СБП'}\n"
        f"Время: {moment:%d.%m.%Y} {moment:%H:%M} МСК"
    )


async def on_mts_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_mts_"))
    filename = mts_receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_mts_pdf(data, output_path)
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", MTS_SBP_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=_mts_caption(data), reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "MTS receipt generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_mts_summary(update, context, prefix=f"{mts_user_error(error)}\n\n")
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_mts_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# МТС: чек по карте и выписка
# ---------------------------------------------------------------------------


def _save_mts_card_memory(context) -> None:
    data = context.user_data
    if data.get("recipient_card"):
        data["mem_mts_card"] = {
            "recipient_card": data.get("recipient_card"),
            "sender_card": data.get("sender_card"),
            "sender_account": data.get("sender_account"),
        }


def _mts_card_prompt(context, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == MC_RECIPIENT_CARD:
        rows = []
        mem = data.get("mem_mts_card") or {}
        if mem.get("recipient_card"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_card']})",
                MC_MEM_CARD_CB,
            )])
        rows.append(nav)
        return (
            "💳 <b>Карта зачисления.</b> 16 цифр, маска или последние 4.\n"
            "Нажмите, чтобы скопировать: <code>8429</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == MC_AMOUNT:
        return (
            "💰 <b>Сумма.</b> Копейки можно. Комиссия в чеке всегда 0,00 ₽.\n"
            "Нажмите, чтобы скопировать: <code>225,00</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == MC_SENDER_CARD_CHOICE:
        rows = [
            [_btn("↩️ Пропустить", "mccard:skip"), _btn("✍️ Ввести карту списания", "mccard:custom")],
            nav,
        ]
        return (
            "💳 <b>Карта списания</b> — необязательно. Без ввода останется "
            "карта шаблона.",
            InlineKeyboardMarkup(rows),
        )
    if state == MC_SENDER_CARD_INPUT:
        return (
            "💳 Карта списания: 16 цифр, маска или последние 4.\n"
            "Нажмите, чтобы скопировать: <code>7826</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == MC_ACCOUNT_CHOICE:
        rows = [
            [_btn("↩️ Оставить шаблон", "mcacc:keep"), _btn("✍️ Ввести 20 цифр", "mcacc:custom")],
            nav,
        ]
        return (
            "💳 <b>Счёт списания</b> — необязательно, 20 цифр.",
            InlineKeyboardMarkup(rows),
        )
    if state == MC_ACCOUNT_INPUT:
        return ("💳 Введите счёт целиком — <b>20 цифр</b>.", InlineKeyboardMarkup([nav]))
    if state == MC_TIME_CHOICE:
        rows = [
            [_btn("🕒 Текущее время", "mctime:now"), _btn("✏️ Изменить время", "mctime:change")],
            nav,
        ]
        return (
            "🕒 <b>Время операции.</b> Секунды движок выведет сам.",
            InlineKeyboardMarkup(rows),
        )
    if state == MC_TIME_INPUT:
        return (
            "🕒 Введите время операции.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 14:20</code> "
            "или только <code>14:20</code> (= сегодня).",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no mts card prompt for state {state}")


async def _send_mts_card_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _mts_card_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _mts_card_summary_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    sender_card = data.get("sender_card") or "карта шаблона"
    account = data.get("sender_account")
    account_text = masked_account(account) if account else "счёт шаблона"
    return "\n".join([
        "<b>Проверь данные</b>",
        "",
        "<b>ЗАЧИСЛЕНИЕ</b>",
        f"💳 Карта зачисления: <b>{html.escape(str(data.get('recipient_card', '')))}</b>",
        f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>",
        "",
        "<b>СПИСАНИЕ</b>",
        f"💳 Карта списания: <b>{html.escape(str(sender_card))}</b>",
        f"💳 Счёт: <b>{html.escape(account_text)}</b>",
        "Комиссия: <b>0,00 ₽</b>",
        f"🕒 Время: <b>{moment:%d.%m.%Y} {moment:%H:%M}</b> (МСК)",
    ])


async def _send_mts_card_summary(update, context, prefix: str = "") -> int:
    _save_mts_card_memory(context)
    context.user_data["current_step"] = MC_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_mts_card_summary_text(context.user_data)}",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("✅ Сгенерировать", MC_GENERATE_CB), _btn("✏️ Исправить", MC_EDIT_CB)], _nav_row()]
        ),
    )
    return MC_SUMMARY


async def _mts_card_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_mts_card_summary(update, context)
    return await _send_mts_card_prompt(update, context, next_state)


async def _mts_card_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_mts_card_summary(update, context)
    return await _send_mts_card_prompt(update, context, next_state)


MTS_CARD_EDIT_FIELDS = {
    "recipient_card": ("💳 Карта зачисления", MC_RECIPIENT_CARD),
    "amount": ("💰 Сумма", MC_AMOUNT),
    "sender_card": ("💳 Карта списания", MC_SENDER_CARD_CHOICE),
    "sender_account": ("💳 Счёт списания", MC_ACCOUNT_CHOICE),
    "operation_time": ("🕒 Время", MC_TIME_CHOICE),
}


async def on_mts_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "mts_card"
    return await _send_mts_card_prompt(update, context, MC_RECIPIENT_CARD)


async def on_mts_stmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data.update(
        {
            "scenario": "mts_statement",
            "stmt_receipt_paths": [],
            "stmt_receipt_dates": [],
            "stmt_fill_random": True,
            "stmt_ops": [],
            "stmt_account_mode": "keep",
            "stmt_account": None,
            "stmt_card": None,
        }
    )
    return await _send_stmt_prompt(update, context, S_PERIOD)


async def on_mc_recipient_card(update, context) -> int:
    try:
        context.user_data["recipient_card"] = parse_mts_card(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, MC_RECIPIENT_CARD, context)
    return await _mts_card_after_field(update, context, MC_AMOUNT)


async def on_mc_mem_card(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_mts_card") or {}
    context.user_data["recipient_card"] = mem.get("recipient_card")
    context.user_data["sender_card"] = mem.get("sender_card")
    context.user_data["sender_account"] = mem.get("sender_account")
    return await _mts_card_choice_next(update, context, MC_AMOUNT)


async def on_mc_amount(update, context) -> int:
    try:
        context.user_data["amount"] = parse_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, MC_AMOUNT, context)
    return await _mts_card_after_field(update, context, MC_SENDER_CARD_CHOICE)


async def on_mc_sender_card_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_mts_card_prompt(update, context, MC_SENDER_CARD_INPUT)
    context.user_data["sender_card"] = None
    return await _mts_card_choice_next(update, context, MC_ACCOUNT_CHOICE)


async def on_mc_sender_card_input(update, context) -> int:
    try:
        context.user_data["sender_card"] = parse_mts_card(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, MC_SENDER_CARD_INPUT, context)
    return await _mts_card_after_field(update, context, MC_ACCOUNT_CHOICE)


async def on_mc_account_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_mts_card_prompt(update, context, MC_ACCOUNT_INPUT)
    context.user_data["sender_account"] = None
    return await _mts_card_choice_next(update, context, MC_TIME_CHOICE)


async def on_mc_account_input(update, context) -> int:
    try:
        context.user_data["sender_account"] = parse_account20(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, MC_ACCOUNT_INPUT, context)
    return await _mts_card_after_field(update, context, MC_TIME_CHOICE)


async def on_mc_time_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_mts_card_prompt(update, context, MC_TIME_INPUT)
    context.user_data["operation_dt"] = rocket_current_time()
    context.user_data.pop("editing", False)
    return await _send_mts_card_summary(update, context)


async def on_mc_time_input(update, context) -> int:
    try:
        context.user_data["operation_dt"] = parse_rocket_time(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, MC_TIME_INPUT, context)
    await _delete_user_message(update, context)
    context.user_data.pop("editing", False)
    return await _send_mts_card_summary(update, context)


async def on_mc_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    buttons = _rows([
        _btn(label, f"mcedit:{key}") for key, (label, _state) in MTS_CARD_EDIT_FIELDS.items()
    ])
    buttons.append(_nav_row())
    context.user_data["current_step"] = MC_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return MC_EDIT_SELECT


async def on_mc_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state = MTS_CARD_EDIT_FIELDS[key]
    context.user_data["editing"] = True
    return await _send_mts_card_prompt(update, context, state)


async def on_mc_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_mts_card_"))
    filename = mts_receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_mts_card_pdf(data, output_path)
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", MTS_CARD_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        moment = data["operation_dt"]
        caption = (
            "✅ Чек готов\n"
            f"Карта зачисления: {data.get('recipient_card')}\n"
            f"Сумма: {data.get('amount')}\n"
            f"Время: {moment:%d.%m.%Y} {moment:%H:%M} МСК"
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=caption, reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "MTS card generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_mts_card_summary(
            update, context, prefix=f"{mts_user_error(error)}\n\n"
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_mts_card_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Выписка: ручная настройка операций (Рокет / МТС / ВТБ)
# ---------------------------------------------------------------------------


def _stmt_ops_layout_ok(data: dict) -> Tuple[bool, str]:
    bank = stmt_bank_of(data)
    allowed = stmt_allowed_counts(bank)
    count = len(data.get("stmt_ops") or [])
    if count in allowed:
        return True, ""
    pretty = ", ".join(str(n) for n in allowed)
    return False, (
        f"Сейчас операций: {count}. Для этой выписки нужно ровно {pretty}. "
        "Добавьте или удалите операции."
    )


async def on_s_ops_editor_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "add":
        context.user_data["stmt_op_draft"] = {}
        return await _send_stmt_prompt(update, context, S_OP_KIND)
    if slug == "delete":
        if not (context.user_data.get("stmt_ops") or []):
            return await _send_stmt_prompt(update, context, S_OPS_EDITOR)
        return await _send_stmt_prompt(update, context, S_OP_DELETE)
    ok, message = _stmt_ops_layout_ok(context.user_data)
    if not ok:
        return await _send_stmt_prompt(
            update, context, S_OPS_EDITOR, prefix=f"⚠️ {message}\n\n"
        )
    context.user_data["stmt_source"] = "manual"
    context.user_data["stmt_fill_random"] = False
    context.user_data["stmt_operation_count"] = len(context.user_data.get("stmt_ops") or [])
    context.user_data.pop("editing", False)
    return await _send_stmt_summary(update, context)


async def on_s_op_kind(update, context) -> int:
    await update.callback_query.answer()
    kind = update.callback_query.data.split(":", 1)[1]
    context.user_data["stmt_op_draft"] = {"kind": kind}
    return await _send_stmt_prompt(update, context, S_OP_AMOUNT)


async def on_s_op_amount(update, context) -> int:
    try:
        value = parse_positive_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, S_OP_AMOUNT, context)
    draft = dict(context.user_data.get("stmt_op_draft") or {})
    draft["amount"] = format(value, "f")
    context.user_data["stmt_op_draft"] = draft
    await _delete_user_message(update, context)
    return await _send_stmt_prompt(update, context, S_OP_DT_CHOICE)


async def on_s_op_dt_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "input":
        return await _send_stmt_prompt(update, context, S_OP_DT_INPUT)
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            "сейчас", data["stmt_period_start"], data["stmt_period_end"]
        )
    except ValueError as error:
        return await _invalid(update, error, S_OP_DT_CHOICE, context)
    draft = dict(data.get("stmt_op_draft") or {})
    draft["datetime"] = moment
    data["stmt_op_draft"] = draft
    if not _stmt_op_needs_detail(data):
        _finish_stmt_op(data)
        return await _send_stmt_prompt(update, context, S_OPS_EDITOR)
    return await _send_stmt_prompt(update, context, S_OP_DETAIL)


async def on_s_op_dt_input(update, context) -> int:
    data = context.user_data
    try:
        moment = parse_ystmt_op_datetime(
            update.effective_message.text,
            data["stmt_period_start"],
            data["stmt_period_end"],
        )
    except ValueError as error:
        return await _invalid(update, error, S_OP_DT_INPUT, context)
    draft = dict(data.get("stmt_op_draft") or {})
    draft["datetime"] = moment
    data["stmt_op_draft"] = draft
    await _delete_user_message(update, context)
    if not _stmt_op_needs_detail(data):
        _finish_stmt_op(data)
        return await _send_stmt_prompt(update, context, S_OPS_EDITOR)
    return await _send_stmt_prompt(update, context, S_OP_DETAIL)


def _finish_stmt_op(data: dict) -> None:
    draft = dict(data.get("stmt_op_draft") or {})
    bank = stmt_bank_of(data)
    kind = draft.get("kind") or "sbp_out"
    op = {
        "kind": kind,
        "datetime": draft.get("datetime"),
        "amount": draft.get("amount"),
    }
    if bank == "rocket":
        if kind in ("sbp_out", "sbp_in"):
            op["phone"] = draft.get("phone") or data.get("stmt_phone")
        elif kind == "card":
            op["card"] = draft.get("card")
        elif kind == "topup" and draft.get("card_last4"):
            op["card_last4"] = draft["card_last4"]
    elif bank == "mts":
        if kind in ("sbp_out", "sbp_in"):
            op["recipient"] = draft.get("recipient") or draft.get("description")
        elif kind == "posted":
            op["description"] = draft.get("description")
        elif kind == "card":
            op["description"] = "Перевод с карты Переводы"
    else:
        if kind == "expense":
            op["kind"] = "sbp_out"
            op["direction"] = "expense"
        elif kind == "internal":
            op["direction"] = "income"
        elif kind == "card":
            op["direction"] = "expense"
        else:
            op["direction"] = "expense"
        if draft.get("description"):
            op["description"] = draft["description"]
        if draft.get("counterparty"):
            op["counterparty"] = draft["counterparty"]
    data.setdefault("stmt_ops", []).append(op)
    data["stmt_op_draft"] = None


def _stmt_op_needs_detail(data: dict) -> bool:
    bank = stmt_bank_of(data)
    kind = (data.get("stmt_op_draft") or {}).get("kind")
    if bank == "mts" and kind == "card":
        return False
    return True


async def on_s_op_detail_skip(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    data = context.user_data
    draft = dict(data.get("stmt_op_draft") or {})
    if slug == "stmt_phone":
        draft["phone"] = data.get("stmt_phone")
        data["stmt_op_draft"] = draft
    _finish_stmt_op(data)
    return await _send_stmt_prompt(update, context, S_OPS_EDITOR)


async def on_s_op_detail_text(update, context) -> int:
    data = context.user_data
    bank = stmt_bank_of(data)
    draft = dict(data.get("stmt_op_draft") or {})
    kind = draft.get("kind")
    text = update.effective_message.text or ""
    try:
        if bank == "rocket" and kind in ("sbp_out", "sbp_in"):
            draft["phone"] = parse_phone(text)
        elif bank == "rocket" and kind == "card":
            draft["card"] = parse_recipient_card(text)
        elif bank == "rocket" and kind == "topup":
            digits = re.sub(r"\D", "", text)
            if len(digits) != 4:
                raise ValueError("Нужны последние 4 цифры карты, например: 4432")
            draft["card_last4"] = digits
        elif bank == "mts" and kind in ("sbp_out", "sbp_in"):
            draft["recipient"] = parse_name(text, "ФИО")
        elif bank == "vtb":
            cleaned = " ".join(text.strip().split())
            if len(cleaned) < 3:
                raise ValueError("Описание слишком короткое.")
            if kind in ("sbp_out", "internal", "expense"):
                draft["counterparty"] = cleaned
            draft["description"] = cleaned
        else:
            cleaned = " ".join(text.strip().split())
            if len(cleaned) < 3:
                raise ValueError("Описание слишком короткое.")
            draft["description"] = cleaned
    except ValueError as error:
        return await _invalid(update, error, S_OP_DETAIL, context)
    data["stmt_op_draft"] = draft
    await _delete_user_message(update, context)
    _finish_stmt_op(data)
    return await _send_stmt_prompt(update, context, S_OPS_EDITOR)


async def on_s_op_delete_choice(update, context) -> int:
    await update.callback_query.answer()
    index = int(update.callback_query.data.split(":", 1)[1])
    ops = list(context.user_data.get("stmt_ops") or [])
    if 0 <= index < len(ops):
        ops.pop(index)
        context.user_data["stmt_ops"] = ops
    return await _send_stmt_prompt(update, context, S_OPS_EDITOR)


# ---------------------------------------------------------------------------
# ВТБ: меню, СБП, карта, выписка
# ---------------------------------------------------------------------------


async def _send_vtb_menu(update, context) -> int:
    context.user_data["current_step"] = V_MENU
    await _send_tracked_message(
        update, context,
        "🔵 <b>ВТБ</b>. Что сформировать?",
        reply_markup=_vtb_menu_markup(),
    )
    return V_MENU


async def on_vtb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    if context.user_data.get("current_step") in (None, BANK_SELECT):
        _cleanup_stmt_files(dict(context.user_data))
        _clear_draft(context)
    return await _send_vtb_menu(update, context)


def _save_vtb_memory(context) -> None:
    data = context.user_data
    if data.get("sender_name"):
        previous = dict(data.get("mem_vtb_sender") or {})
        data["mem_vtb_sender"] = {
            "sender_name": data.get("sender_name"),
            "sender_phone": data.get("sender_phone") or previous.get("sender_phone"),
            "sender_account": data.get("sender_account"),
        }
    if data.get("scenario") == "vtb_sbp" and data.get("recipient_name"):
        data["mem_vtb_recipient"] = {
            "recipient_name": data.get("recipient_name"),
            "recipient_phone": data.get("recipient_phone"),
            "recipient_bank": data.get("recipient_bank"),
        }
    if data.get("scenario") == "vtb_card" and data.get("recipient_card"):
        data["mem_vtb_recipient_card"] = {
            "recipient_card": data.get("recipient_card"),
            "recipient_bank": data.get("recipient_bank"),
            "payer_card": data.get("payer_card"),
        }


def _vtb_prompt(context, state: int):
    data = context.user_data
    nav = _nav_row()
    scenario = data.get("scenario")
    if state == V_RECIPIENT_NAME:
        rows = []
        mem = data.get("mem_vtb_recipient") or {}
        if mem.get("recipient_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_name']})",
                V_MEM_RECIPIENT_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Получатель.</b> Введите ФИО.\n"
            "Нажмите, чтобы скопировать: <code>Петрова Анна Ивановна</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == V_RECIPIENT_PHONE:
        return (
            "📱 <b>Телефон получателя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 963 297-20-08</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_RECIPIENT_CARD:
        rows = []
        mem = data.get("mem_vtb_recipient_card") or {}
        if mem.get("recipient_card"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_card']})",
                V_MEM_RECIPIENT_CB,
            )])
        rows.append(nav)
        return (
            "💳 <b>Карта получателя.</b> 16 цифр или маска "
            "<code>2200 27** **** 4432</code>.",
            InlineKeyboardMarkup(rows),
        )
    if state == V_RECIPIENT_BANK:
        buttons = [_btn(label, f"vbank:{slug}") for slug, label in VTB_BANK_OPTIONS]
        buttons.append(_btn("✍️ Ввести свой", "vbank:custom"))
        return (
            "🏦 <b>Банк получателя</b> — как в чеке ВТБ:",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == V_RECIPIENT_BANK_CUSTOM:
        return (
            "🏦 Введите банк получателя.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_AMOUNT:
        note = " Комиссия перевода на карту всегда 99 ₽." if scenario == "vtb_card" else ""
        return (
            f"💰 <b>Сумма.</b> Копейки можно.{note}\n"
            "Нажмите, чтобы скопировать: <code>5 000</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_SENDER_NAME:
        rows = []
        mem = data.get("mem_vtb_sender") or {}
        if mem.get("sender_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['sender_name']})",
                V_MEM_SENDER_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Отправитель.</b> Введите ФИО.\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == V_SENDER_PHONE:
        return (
            "📱 <b>Телефон отправителя</b> (на чеке СБП не печатается, но нужен движку).\n"
            "Нажмите, чтобы скопировать: <code>+7 900 351-70-80</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_SENDER_ACCOUNT_CHOICE:
        rows = [
            [_btn("↩️ Оставить шаблон", "vacc:keep"), _btn("✍️ Ввести 20 цифр", "vacc:custom")],
            nav,
        ]
        return (
            "💳 <b>Счёт отправителя.</b> «Оставить шаблон» — счёт донора.",
            InlineKeyboardMarkup(rows),
        )
    if state == V_SENDER_ACCOUNT_INPUT:
        return ("💳 Введите счёт целиком — <b>20 цифр</b>.", InlineKeyboardMarkup([nav]))
    if state == V_PAYER_CARD_CHOICE:
        rows = [
            [_btn("↩️ Пропустить", "vpcard:skip"), _btn("✍️ Ввести карту списания", "vpcard:custom")],
            nav,
        ]
        return (
            "💳 <b>Карта списания</b> — необязательно.",
            InlineKeyboardMarkup(rows),
        )
    if state == V_PAYER_CARD_INPUT:
        return (
            "💳 Карта списания — 16 цифр.\n"
            "Нажмите, чтобы скопировать: <code>2200270011114432</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_MESSAGE_CHOICE:
        rows = [
            [_btn("Без сообщения", "vmsg:none"), _btn("✍️ Ввести своё", "vmsg:custom")],
            nav,
        ]
        return ("💬 <b>Сообщение</b> в чеке СБП:", InlineKeyboardMarkup(rows))
    if state == V_MESSAGE_INPUT:
        return (
            "💬 Введите сообщение.\n"
            "Нажмите, чтобы скопировать: <code>Перевод СБП</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == V_TIME_CHOICE:
        rows = [
            [_btn("🕒 Текущее время", "vtime:now"), _btn("✏️ Изменить время", "vtime:change")],
            nav,
        ]
        return (
            "🕒 <b>Время операции.</b> Секунды движок выведет сам.",
            InlineKeyboardMarkup(rows),
        )
    if state == V_TIME_INPUT:
        return (
            "🕒 Введите время операции.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 14:20</code> "
            "или только <code>14:20</code> (= сегодня).",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no vtb prompt for state {state}")


async def _send_vtb_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _vtb_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _vtb_summary_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    account = data.get("sender_account")
    account_text = masked_account(account) if account else "счёт шаблона"
    lines = ["<b>Проверь данные</b>", "", "<b>ПОЛУЧАТЕЛЬ</b>"]
    if data.get("scenario") == "vtb_card":
        lines.append(f"💳 Карта: <b>{html.escape(str(data.get('recipient_card', '')))}</b>")
        lines.append(f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank') or '—'))}</b>")
        lines.append(f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>")
        lines.append("Комиссия: <b>99 ₽</b>")
    else:
        lines.append(f"👤 Получатель: <b>{html.escape(str(data.get('recipient_name', '')))}</b>")
        lines.append(f"📱 Телефон: <b>{html.escape(str(data.get('recipient_phone', '')))}</b>")
        lines.append(f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank', '')))}</b>")
        lines.append(f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>")
        if data.get("vtb_message"):
            lines.append(f"💬 Сообщение: <b>{html.escape(str(data['vtb_message']))}</b>")
    lines.extend([
        "",
        "<b>ОТПРАВИТЕЛЬ</b>",
        f"👤 ФИО: <b>{html.escape(str(data.get('sender_name', '')))}</b>",
    ])
    if data.get("scenario") == "vtb_sbp":
        lines.append(f"📱 Телефон: <b>{html.escape(str(data.get('sender_phone', '')))}</b>")
    lines.append(f"💳 Счёт: <b>{html.escape(account_text)}</b>")
    if data.get("payer_card"):
        lines.append(f"💳 Карта списания: <b>{html.escape(str(data['payer_card']))}</b>")
    lines.append(f"🕒 Время: <b>{moment:%d.%m.%Y} {moment:%H:%M}</b> (МСК)")
    return "\n".join(lines)


async def _send_vtb_summary(update, context, prefix: str = "") -> int:
    _save_vtb_memory(context)
    context.user_data["current_step"] = V_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_vtb_summary_text(context.user_data)}",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("✅ Сгенерировать", V_GENERATE_CB), _btn("✏️ Исправить", V_EDIT_CB)], _nav_row()]
        ),
    )
    return V_SUMMARY


async def _vtb_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_vtb_summary(update, context)
    return await _send_vtb_prompt(update, context, next_state)


async def _vtb_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_vtb_summary(update, context)
    return await _send_vtb_prompt(update, context, next_state)


def _vtb_edit_fields(scenario: Optional[str]):
    if scenario == "vtb_card":
        return {
            "recipient_card": ("💳 Карта", V_RECIPIENT_CARD),
            "recipient_bank": ("🏦 Банк", V_RECIPIENT_BANK),
            "amount": ("💰 Сумма", V_AMOUNT),
            "sender_name": ("👤 Отправитель", V_SENDER_NAME),
            "sender_account": ("💳 Счёт", V_SENDER_ACCOUNT_CHOICE),
            "payer_card": ("💳 Карта списания", V_PAYER_CARD_CHOICE),
            "operation_time": ("🕒 Время", V_TIME_CHOICE),
        }
    return {
        "recipient_name": ("👤 Получатель", V_RECIPIENT_NAME),
        "recipient_phone": ("📱 Телефон", V_RECIPIENT_PHONE),
        "recipient_bank": ("🏦 Банк", V_RECIPIENT_BANK),
        "amount": ("💰 Сумма", V_AMOUNT),
        "sender_name": ("👤 Отправитель", V_SENDER_NAME),
        "sender_phone": ("📱 Телефон отправителя", V_SENDER_PHONE),
        "sender_account": ("💳 Счёт", V_SENDER_ACCOUNT_CHOICE),
        "message": ("💬 Сообщение", V_MESSAGE_CHOICE),
        "operation_time": ("🕒 Время", V_TIME_CHOICE),
    }


async def on_vtb_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "vtb_sbp"
    return await _send_vtb_prompt(update, context, V_RECIPIENT_NAME)


async def on_vtb_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "vtb_card"
    return await _send_vtb_prompt(update, context, V_RECIPIENT_CARD)


async def on_vtb_stmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _clear_draft(context)
    context.user_data.update(
        {
            "scenario": "vtb_statement",
            "stmt_receipt_paths": [],
            "stmt_receipt_dates": [],
            "stmt_fill_random": True,
            "stmt_ops": [],
            "stmt_account_mode": "keep",
            "stmt_account": None,
        }
    )
    return await _send_stmt_prompt(update, context, S_PERIOD)


async def on_v_recipient_name(update, context) -> int:
    try:
        context.user_data["recipient_name"] = parse_name(
            update.effective_message.text, "ФИО получателя"
        )
    except ValueError as error:
        return await _invalid(update, error, V_RECIPIENT_NAME, context)
    return await _vtb_after_field(update, context, V_RECIPIENT_PHONE)


async def on_v_mem_recipient(update, context) -> int:
    await update.callback_query.answer()
    if context.user_data.get("scenario") == "vtb_card":
        mem = context.user_data.get("mem_vtb_recipient_card") or {}
        context.user_data["recipient_card"] = mem.get("recipient_card")
        context.user_data["recipient_bank"] = mem.get("recipient_bank")
        context.user_data["payer_card"] = mem.get("payer_card")
        return await _vtb_choice_next(update, context, V_AMOUNT)
    mem = context.user_data.get("mem_vtb_recipient") or {}
    context.user_data["recipient_name"] = mem.get("recipient_name")
    context.user_data["recipient_phone"] = mem.get("recipient_phone")
    context.user_data["recipient_bank"] = mem.get("recipient_bank")
    return await _vtb_choice_next(update, context, V_AMOUNT)


async def on_v_recipient_phone(update, context) -> int:
    try:
        context.user_data["recipient_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_RECIPIENT_PHONE, context)
    return await _vtb_after_field(update, context, V_RECIPIENT_BANK)


async def on_v_recipient_card(update, context) -> int:
    try:
        context.user_data["recipient_card"] = parse_vtb_payee_card(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, V_RECIPIENT_CARD, context)
    return await _vtb_after_field(update, context, V_RECIPIENT_BANK)


async def on_v_bank_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_vtb_prompt(update, context, V_RECIPIENT_BANK_CUSTOM)
    for key, label in VTB_BANK_OPTIONS:
        if key == slug:
            context.user_data["recipient_bank"] = label
            break
    return await _vtb_choice_next(update, context, V_AMOUNT)


async def on_v_bank_custom(update, context) -> int:
    try:
        context.user_data["recipient_bank"] = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_RECIPIENT_BANK_CUSTOM, context)
    return await _vtb_after_field(update, context, V_AMOUNT)


async def on_v_amount(update, context) -> int:
    try:
        context.user_data["amount"] = parse_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_AMOUNT, context)
    return await _vtb_after_field(update, context, V_SENDER_NAME)


async def on_v_sender_name(update, context) -> int:
    try:
        context.user_data["sender_name"] = parse_name(
            update.effective_message.text, "ФИО отправителя"
        )
    except ValueError as error:
        return await _invalid(update, error, V_SENDER_NAME, context)
    next_state = (
        V_SENDER_ACCOUNT_CHOICE
        if context.user_data.get("scenario") == "vtb_card"
        else V_SENDER_PHONE
    )
    return await _vtb_after_field(update, context, next_state)


async def on_v_mem_sender(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_vtb_sender") or {}
    context.user_data["sender_name"] = mem.get("sender_name")
    context.user_data["sender_phone"] = mem.get("sender_phone")
    context.user_data["sender_account"] = mem.get("sender_account")
    next_state = (
        V_PAYER_CARD_CHOICE
        if context.user_data.get("scenario") == "vtb_card"
        else V_MESSAGE_CHOICE
    )
    return await _vtb_choice_next(update, context, next_state)


async def on_v_sender_phone(update, context) -> int:
    try:
        context.user_data["sender_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_SENDER_PHONE, context)
    return await _vtb_after_field(update, context, V_SENDER_ACCOUNT_CHOICE)


async def on_v_account_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_vtb_prompt(update, context, V_SENDER_ACCOUNT_INPUT)
    context.user_data["sender_account"] = None
    next_state = (
        V_PAYER_CARD_CHOICE
        if context.user_data.get("scenario") == "vtb_card"
        else V_MESSAGE_CHOICE
    )
    return await _vtb_choice_next(update, context, next_state)


async def on_v_account_input(update, context) -> int:
    try:
        context.user_data["sender_account"] = parse_account20(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_SENDER_ACCOUNT_INPUT, context)
    next_state = (
        V_PAYER_CARD_CHOICE
        if context.user_data.get("scenario") == "vtb_card"
        else V_MESSAGE_CHOICE
    )
    return await _vtb_after_field(update, context, next_state)


async def on_v_payer_card_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_vtb_prompt(update, context, V_PAYER_CARD_INPUT)
    context.user_data["payer_card"] = None
    return await _vtb_choice_next(update, context, V_TIME_CHOICE)


async def on_v_payer_card_input(update, context) -> int:
    try:
        context.user_data["payer_card"] = parse_recipient_card(
            update.effective_message.text
        ).replace(" **** **** ", "")
        digits = re.sub(r"\D", "", update.effective_message.text or "")
        if len(digits) != 16:
            raise ValueError("Карта списания — 16 цифр.")
        context.user_data["payer_card"] = digits
    except ValueError as error:
        return await _invalid(update, error, V_PAYER_CARD_INPUT, context)
    return await _vtb_after_field(update, context, V_TIME_CHOICE)


async def on_v_message_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_vtb_prompt(update, context, V_MESSAGE_INPUT)
    context.user_data["vtb_message"] = None
    return await _vtb_choice_next(update, context, V_TIME_CHOICE)


async def on_v_message_input(update, context) -> int:
    cleaned = " ".join((update.effective_message.text or "").strip().split())
    if not (2 <= len(cleaned) <= 80):
        return await _invalid(
            update, ValueError("Сообщение — от 2 до 80 символов."),
            V_MESSAGE_INPUT, context,
        )
    context.user_data["vtb_message"] = cleaned
    return await _vtb_after_field(update, context, V_TIME_CHOICE)


async def on_v_time_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_vtb_prompt(update, context, V_TIME_INPUT)
    context.user_data["operation_dt"] = rocket_current_time()
    context.user_data.pop("editing", False)
    return await _send_vtb_summary(update, context)


async def on_v_time_input(update, context) -> int:
    try:
        context.user_data["operation_dt"] = parse_rocket_time(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, V_TIME_INPUT, context)
    await _delete_user_message(update, context)
    context.user_data.pop("editing", False)
    return await _send_vtb_summary(update, context)


async def on_v_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    fields = _vtb_edit_fields(context.user_data.get("scenario"))
    buttons = _rows([_btn(label, f"vedit:{key}") for key, (label, _state) in fields.items()])
    buttons.append(_nav_row())
    context.user_data["current_step"] = V_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return V_EDIT_SELECT


async def on_v_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    fields = _vtb_edit_fields(context.user_data.get("scenario"))
    _label, state = fields[key]
    context.user_data["editing"] = True
    return await _send_vtb_prompt(update, context, state)


async def on_v_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    scenario = data.get("scenario")
    await _send_tracked_message(update, context, "⏳ Формирую чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_vtb_"))
    filename = vtb_receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        if scenario == "vtb_card":
            pdf_path = await generate_vtb_card_pdf(data, output_path)
            again_cb = VTB_CARD_CB
        else:
            pdf_path = await generate_vtb_pdf(data, output_path)
            again_cb = VTB_SBP_CB
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", again_cb)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=_vtb_summary_text(data).replace("<b>", "").replace("</b>", ""),
                reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "VTB generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_vtb_summary(update, context, prefix=f"{vtb_user_error(error)}\n\n")
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_vtb_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ВБ Банк: только чек СБП
# ---------------------------------------------------------------------------


async def _send_wb_menu(update, context) -> int:
    context.user_data["current_step"] = W_MENU
    await _send_tracked_message(
        update, context,
        "🟣 <b>ВБ Банк</b>. Что сформировать?",
        reply_markup=_wb_menu_markup(),
    )
    return W_MENU


async def on_wb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    if context.user_data.get("current_step") in (None, BANK_SELECT):
        _clear_draft(context)
    return await _send_wb_menu(update, context)


def _save_wb_memory(context) -> None:
    data = context.user_data
    if data.get("recipient_name"):
        data["mem_wb_recipient"] = {
            "recipient_name": data.get("recipient_name"),
            "recipient_phone": data.get("recipient_phone"),
            "recipient_bank": data.get("recipient_bank"),
        }


def _wb_prompt(context, state: int):
    data = context.user_data
    nav = _nav_row()
    if state == W_RECIPIENT_NAME:
        rows = []
        mem = data.get("mem_wb_recipient") or {}
        if mem.get("recipient_name"):
            rows.append([_btn(
                f"↩️ Оставить как в прошлый раз ({mem['recipient_name']})",
                W_MEM_RECIPIENT_CB,
            )])
        rows.append(nav)
        return (
            "👤 <b>Получатель.</b> Введите ФИО. Нет Ё, нет «Константин».\n"
            "Нажмите, чтобы скопировать: <code>Иванов Иван Иванович</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == W_RECIPIENT_PHONE:
        return (
            "📱 <b>Телефон получателя</b> в любом формате.\n"
            "Нажмите, чтобы скопировать: <code>+7 963 297-20-08</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == W_RECIPIENT_BANK:
        buttons = [_btn(label, f"wbank:{slug}") for slug, label in WB_BANK_OPTIONS]
        buttons.append(_btn("✍️ Ввести свой", "wbank:custom"))
        return (
            "🏦 <b>Банк получателя.</b> «Озон» в пуле глифов нет.",
            InlineKeyboardMarkup(_rows(buttons) + [nav]),
        )
    if state == W_RECIPIENT_BANK_CUSTOM:
        return (
            "🏦 Введите банк получателя.\n"
            "Нажмите, чтобы скопировать: <code>Сбербанк</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == W_AMOUNT:
        return (
            "💰 <b>Сумма — только целые рубли</b> (цифры 0–3 в Medium: "
            "15 ₽ откажет).\n"
            "Нажмите, чтобы скопировать: <code>12</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == W_TIME_CHOICE:
        rows = [
            [_btn("🕒 Текущее время", "wtime:now"), _btn("✏️ Изменить время", "wtime:change")],
            nav,
        ]
        return (
            "🕒 <b>Время операции.</b> Секунды движок выведет сам.",
            InlineKeyboardMarkup(rows),
        )
    if state == W_TIME_INPUT:
        return (
            "🕒 Введите время операции.\n"
            "Нажмите, чтобы скопировать: <code>03.11.2026 14:20</code> "
            "или только <code>14:20</code> (= сегодня).",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no wb prompt for state {state}")


async def _send_wb_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _wb_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _wb_summary_text(data: dict) -> str:
    moment: datetime = data["operation_dt"]
    return "\n".join([
        "<b>Проверь данные</b>",
        "",
        "<b>ПОЛУЧАТЕЛЬ</b>",
        f"👤 Получатель: <b>{html.escape(str(data.get('recipient_name', '')))}</b>",
        f"📱 Телефон: <b>{html.escape(str(data.get('recipient_phone', '')))}</b>",
        f"🏦 Банк: <b>{html.escape(str(data.get('recipient_bank', '')))}</b>",
        f"💰 Сумма: <b>{html.escape(str(data.get('amount', '')))}</b>",
        f"🕒 Время: <b>{moment:%d.%m.%Y} {moment:%H:%M}</b> (МСК)",
    ])


async def _send_wb_summary(update, context, prefix: str = "") -> int:
    _save_wb_memory(context)
    context.user_data["current_step"] = W_SUMMARY
    await _send_tracked_message(
        update, context, f"{prefix}{_wb_summary_text(context.user_data)}",
        reply_markup=InlineKeyboardMarkup(
            [[_btn("✅ Сгенерировать", W_GENERATE_CB), _btn("✏️ Исправить", W_EDIT_CB)], _nav_row()]
        ),
    )
    return W_SUMMARY


async def _wb_after_field(update, context, next_state: int) -> int:
    await _delete_user_message(update, context)
    if context.user_data.pop("editing", False):
        return await _send_wb_summary(update, context)
    return await _send_wb_prompt(update, context, next_state)


async def _wb_choice_next(update, context, next_state: int) -> int:
    if context.user_data.pop("editing", False):
        return await _send_wb_summary(update, context)
    return await _send_wb_prompt(update, context, next_state)


WB_EDIT_FIELDS = {
    "recipient_name": ("👤 Получатель", W_RECIPIENT_NAME),
    "recipient_phone": ("📱 Телефон", W_RECIPIENT_PHONE),
    "recipient_bank": ("🏦 Банк", W_RECIPIENT_BANK),
    "amount": ("💰 Сумма", W_AMOUNT),
    "operation_time": ("🕒 Время", W_TIME_CHOICE),
}


async def on_wb_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _clear_draft(context)
    context.user_data["scenario"] = "wb"
    return await _send_wb_prompt(update, context, W_RECIPIENT_NAME)


async def on_w_recipient_name(update, context) -> int:
    try:
        context.user_data["recipient_name"] = parse_name(
            update.effective_message.text, "ФИО получателя"
        )
    except ValueError as error:
        return await _invalid(update, error, W_RECIPIENT_NAME, context)
    return await _wb_after_field(update, context, W_RECIPIENT_PHONE)


async def on_w_mem_recipient(update, context) -> int:
    await update.callback_query.answer()
    mem = context.user_data.get("mem_wb_recipient") or {}
    context.user_data["recipient_name"] = mem.get("recipient_name")
    context.user_data["recipient_phone"] = mem.get("recipient_phone")
    context.user_data["recipient_bank"] = mem.get("recipient_bank")
    return await _wb_choice_next(update, context, W_AMOUNT)


async def on_w_recipient_phone(update, context) -> int:
    try:
        context.user_data["recipient_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, W_RECIPIENT_PHONE, context)
    return await _wb_after_field(update, context, W_RECIPIENT_BANK)


async def on_w_bank_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _send_wb_prompt(update, context, W_RECIPIENT_BANK_CUSTOM)
    for key, label in WB_BANK_OPTIONS:
        if key == slug:
            context.user_data["recipient_bank"] = label
            break
    return await _wb_choice_next(update, context, W_AMOUNT)


async def on_w_bank_custom(update, context) -> int:
    try:
        context.user_data["recipient_bank"] = parse_bank(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, W_RECIPIENT_BANK_CUSTOM, context)
    return await _wb_after_field(update, context, W_AMOUNT)


async def on_w_amount(update, context) -> int:
    try:
        context.user_data["amount"] = parse_wb_amount(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, W_AMOUNT, context)
    return await _wb_after_field(update, context, W_TIME_CHOICE)


async def on_w_time_choice(update, context) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "change":
        return await _send_wb_prompt(update, context, W_TIME_INPUT)
    context.user_data["operation_dt"] = rocket_current_time()
    context.user_data.pop("editing", False)
    return await _send_wb_summary(update, context)


async def on_w_time_input(update, context) -> int:
    try:
        context.user_data["operation_dt"] = parse_rocket_time(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, W_TIME_INPUT, context)
    await _delete_user_message(update, context)
    context.user_data.pop("editing", False)
    return await _send_wb_summary(update, context)


async def on_w_summary_edit(update, context) -> int:
    await update.callback_query.answer()
    buttons = _rows([
        _btn(label, f"wedit:{key}") for key, (label, _state) in WB_EDIT_FIELDS.items()
    ])
    buttons.append(_nav_row())
    context.user_data["current_step"] = W_EDIT_SELECT
    await _send_tracked_message(
        update, context, "Что исправить?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return W_EDIT_SELECT


async def on_w_edit_field(update, context) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data.split(":", 1)[1]
    _label, state = WB_EDIT_FIELDS[key]
    context.user_data["editing"] = True
    return await _send_wb_prompt(update, context, state)


async def on_w_generate(update, context) -> int:
    await update.callback_query.answer()
    chat_id = update.effective_chat.id
    user = update.effective_user
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_wb_"))
    filename = wb_receipt_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_wb_pdf(data, output_path)
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", WB_SBP_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        moment = data["operation_dt"]
        caption = (
            "✅ Чек готов\n"
            f"{data.get('recipient_name')} · {data.get('recipient_phone')} · "
            f"{data.get('recipient_bank')}\n"
            f"Сумма: {data.get('amount')}\n"
            f"Время: {moment:%d.%m.%Y} {moment:%H:%M} МСК"
        )
        with pdf_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=caption, reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "WB generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_wb_summary(update, context, prefix=f"{wb_user_error(error)}\n\n")
    finally:
        try:
            output_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    _save_wb_memory(context)
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Альфа: загрузка PDF и замена суммы
# ---------------------------------------------------------------------------


DEFAULT_ALFA_PATCHER_PATH = REPO_ROOT / "banks" / "alfa" / "pdf_patcher.py"
_ALFA_PATCHER_CACHE: dict = {"path": None, "module": None}


def alfa_patcher_path() -> Path:
    override = os.environ.get("ALFA_PATCHER_PATH")
    return Path(override).expanduser() if override else DEFAULT_ALFA_PATCHER_PATH


def load_alfa_patcher():
    """Патчер суммы Альфа-чека. PDF-логики в боте нет."""
    path = alfa_patcher_path()
    if _ALFA_PATCHER_CACHE["path"] == path and _ALFA_PATCHER_CACHE["module"] is not None:
        return _ALFA_PATCHER_CACHE["module"]
    if not path.is_file():
        raise FileNotFoundError(f"Патчер Альфы не найден: {path}")
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location("checketop_alfa_patcher", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить патчер Альфы из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_alfa_patcher"] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "patch_pdf_file", None)):
        raise AttributeError(f"В {path} нет функции patch_pdf_file(...)")
    _ALFA_PATCHER_CACHE["path"] = path
    _ALFA_PATCHER_CACHE["module"] = module
    return module


def _cleanup_alfa_upload(data: dict) -> None:
    raw = data.get("alfa_pdf_path")
    if raw:
        try:
            Path(str(raw)).unlink(missing_ok=True)
        except OSError:
            pass
    directory = data.get("alfa_pdf_dir")
    if directory:
        try:
            Path(str(directory)).rmdir()
        except OSError:
            pass


def _alfa_prompt(context, state: int):
    nav = _nav_row()
    if state == A_UPLOAD:
        return (
            "📄 Пришлите <b>PDF чека Альфа-Банка</b> файлом. "
            "Поменяю в нём только сумму.",
            InlineKeyboardMarkup([nav]),
        )
    if state == A_AMOUNT:
        return (
            "💰 <b>Сумма: с какой на какую.</b>\n"
            "Сначала та, что уже напечатана в чеке, потом новая.\n"
            "Нажмите, чтобы скопировать: <code>10 5000</code>",
            InlineKeyboardMarkup([nav]),
        )
    raise ValueError(f"no alfa prompt for state {state}")


async def _send_alfa_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _alfa_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


async def on_alfa_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _cleanup_alfa_upload(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "alfa"
    return await _send_alfa_prompt(update, context, A_UPLOAD)


async def on_alfa_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    document = getattr(message, "document", None)
    await _delete_user_message(update, context)
    name = (getattr(document, "file_name", None) or "")
    if document is None or not name.lower().endswith(".pdf"):
        return await _send_alfa_prompt(
            update, context, A_UPLOAD, prefix="⚠️ Нужен PDF-файл.\n\n"
        )
    _cleanup_alfa_upload(dict(context.user_data))
    directory = Path(tempfile.mkdtemp(prefix="checketop_alfa_"))
    local_path = directory / "donor.pdf"
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(local_path))
    except Exception:
        logger.exception(
            "Alfa PDF download failed (user_id=%s)",
            getattr(update.effective_user, "id", "?"),
        )
        _cleanup_alfa_upload({"alfa_pdf_path": str(local_path), "alfa_pdf_dir": str(directory)})
        return await _send_alfa_prompt(
            update, context, A_UPLOAD, prefix="⚠️ Не получилось прочитать PDF. Пришлите файл ещё раз.\n\n"
        )
    context.user_data["alfa_pdf_path"] = str(local_path)
    context.user_data["alfa_pdf_dir"] = str(directory)
    context.user_data["alfa_pdf_name"] = Path(name).name
    return await _send_alfa_prompt(update, context, A_AMOUNT)


async def on_alfa_amounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount_from, amount_to = parse_alfa_amounts(update.effective_message.text or "")
    except ValueError as error:
        return await _invalid(update, error, A_AMOUNT, context)
    await _delete_user_message(update, context)
    data = context.user_data
    source = data.get("alfa_pdf_path")
    if not source or not Path(str(source)).is_file():
        return await _send_alfa_prompt(
            update, context, A_UPLOAD, prefix="⚠️ Чек не найден. Пришлите PDF ещё раз.\n\n"
        )
    chat_id = update.effective_chat.id
    user = update.effective_user
    out_dir = Path(tempfile.mkdtemp(prefix="checketop_alfa_out_"))
    stem = Path(str(data.get("alfa_pdf_name") or "alfa.pdf")).stem
    filename = f"{stem}_{amount_to}.pdf"
    output_path = out_dir / filename
    await _send_tracked_message(update, context, "⏳ Меняю сумму…")
    try:
        module = load_alfa_patcher()
        ok, err = await asyncio.to_thread(
            module.patch_pdf_file, source, str(output_path), amount_from, amount_to, "alfa"
        )
        if not ok or not output_path.is_file():
            raise RuntimeError(err or "Сумма в чеке не найдена")
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", ALFA_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        with output_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption=f"✅ Готово: {amount_from} ₽ → {amount_to} ₽",
                reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "Alfa amount patch failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_alfa_prompt(
            update, context, A_AMOUNT, prefix=f"Не получилось: {_short_error(error)}\n\n"
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            out_dir.rmdir()
        except OSError:
            pass
    _cleanup_alfa_upload(dict(context.user_data))
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ВТБ: загруженный PDF — сумма, счёт, ФИО
# ---------------------------------------------------------------------------


DEFAULT_VTB_UPLOAD_PATH = REPO_ROOT / "banks" / "vtb" / "upload" / "vtb_patch_from_config.py"
_VTB_UPLOAD_CACHE: dict = {"path": None, "module": None}


def vtb_upload_patcher_path() -> Path:
    override = os.environ.get("VTB_UPLOAD_PATCHER_PATH")
    return Path(override).expanduser() if override else DEFAULT_VTB_UPLOAD_PATH


def load_vtb_upload_patcher():
    """Патчер загруженного чека ВТБ: сумма, счёт, ФИО. PDF-логики в боте нет."""
    path = vtb_upload_patcher_path()
    if _VTB_UPLOAD_CACHE["path"] == path and _VTB_UPLOAD_CACHE["module"] is not None:
        return _VTB_UPLOAD_CACHE["module"]
    if not path.is_file():
        raise FileNotFoundError(f"Патчер ВТБ не найден: {path}")
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location("checketop_vtb_upload", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить патчер ВТБ из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["checketop_vtb_upload"] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "patch_amount_only", None)):
        raise AttributeError(f"В {path} нет функции patch_amount_only(...)")
    if not callable(getattr(module, "patch_from_values", None)):
        raise AttributeError(f"В {path} нет функции patch_from_values(...)")
    _VTB_UPLOAD_CACHE["path"] = path
    _VTB_UPLOAD_CACHE["module"] = module
    return module


def _cleanup_vtb_upload(data: dict) -> None:
    raw = data.get("vtb_up_path")
    if raw:
        try:
            Path(str(raw)).unlink(missing_ok=True)
        except OSError:
            pass
    directory = data.get("vtb_up_dir")
    if directory:
        try:
            Path(str(directory)).rmdir()
        except OSError:
            pass


def apply_vtb_upload_patch(
    source: Path,
    output_path: Path,
    amount_from: int,
    amount_to: int,
    account: Optional[str],
    payer: Optional[str],
    recipient: Optional[str],
) -> None:
    """Сначала сумма, затем счёт и ФИО. Дату чека не трогаем."""
    module = load_vtb_upload_patcher()
    data = bytearray(source.read_bytes())
    try:
        data = bytearray(module.patch_amount_only(data, source, amount_to))
    except Exception:
        alfa = load_alfa_patcher()
        ok, err = alfa.patch_pdf_file(source, output_path, amount_from, amount_to, "vtb")
        if not ok or not output_path.is_file():
            raise RuntimeError(err or "Сумма в чеке не найдена")
        data = bytearray(output_path.read_bytes())
    if payer or recipient or account:
        data = module.patch_from_values(
            data,
            source,
            payer=payer,
            recipient=recipient,
            account=account,
            amount=None,
            keep_date=True,
        )
    output_path.write_bytes(bytes(data))


def _vtb_upload_prompt(context, state: int):
    nav = _nav_row()
    if state == VU_UPLOAD:
        return (
            "📄 Пришлите <b>PDF чека ВТБ</b> файлом. "
            "Можно поменять сумму, номер счёта и ФИО.",
            InlineKeyboardMarkup([nav]),
        )
    if state == VU_AMOUNT:
        return (
            "💰 <b>Сумма: с какой на какую.</b>\n"
            "Сначала та, что уже в чеке, потом новая.\n"
            "Нажмите, чтобы скопировать: <code>10 1000</code>",
            InlineKeyboardMarkup([nav]),
        )
    if state == VU_ACCOUNT:
        rows = [[_btn("↩️ Оставить свой", "vuacc:keep")], nav]
        return (
            "💳 <b>Номер счёта.</b> Последние 4 цифры или длиннее.\n"
            "Нажмите, чтобы скопировать: <code>9481</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == VU_PAYER:
        rows = [[_btn("↩️ Оставить", "vukeep:payer")], nav]
        return (
            "👤 <b>ФИО плательщика.</b> Или оставьте как в чеке.\n"
            "Нажмите, чтобы скопировать: <code>Евгений Александрович Е.</code>",
            InlineKeyboardMarkup(rows),
        )
    if state == VU_RECIPIENT:
        rows = [[_btn("↩️ Оставить", "vukeep:recipient")], nav]
        return (
            "👤 <b>ФИО получателя.</b> Или оставьте как в чеке.\n"
            "Нажмите, чтобы скопировать: <code>Анна Петрова С.</code>",
            InlineKeyboardMarkup(rows),
        )
    raise ValueError(f"no vtb upload prompt for state {state}")


async def _send_vtb_upload_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _vtb_upload_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


async def on_vtb_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query is not None:
        await update.callback_query.answer()
    _cleanup_stmt_files(dict(context.user_data))
    _cleanup_yandex_template_files(dict(context.user_data))
    _cleanup_alfa_upload(dict(context.user_data))
    _cleanup_vtb_upload(dict(context.user_data))
    _clear_draft(context)
    context.user_data["scenario"] = "vtb_upload"
    return await _send_vtb_upload_prompt(update, context, VU_UPLOAD)


async def on_vtb_upload_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    document = getattr(message, "document", None)
    await _delete_user_message(update, context)
    name = getattr(document, "file_name", None) or ""
    if document is None or not name.lower().endswith(".pdf"):
        return await _send_vtb_upload_prompt(
            update, context, VU_UPLOAD, prefix="⚠️ Нужен PDF-файл.\n\n"
        )
    _cleanup_vtb_upload(dict(context.user_data))
    directory = Path(tempfile.mkdtemp(prefix="checketop_vtb_up_"))
    local_path = directory / "donor.pdf"
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(local_path))
    except Exception:
        logger.exception(
            "VTB PDF download failed (user_id=%s)",
            getattr(update.effective_user, "id", "?"),
        )
        _cleanup_vtb_upload({"vtb_up_path": str(local_path), "vtb_up_dir": str(directory)})
        return await _send_vtb_upload_prompt(
            update, context, VU_UPLOAD,
            prefix="⚠️ Не получилось прочитать PDF. Пришлите файл ещё раз.\n\n",
        )
    context.user_data["vtb_up_path"] = str(local_path)
    context.user_data["vtb_up_dir"] = str(directory)
    context.user_data["vtb_up_name"] = Path(name).name
    return await _send_vtb_upload_prompt(update, context, VU_AMOUNT)


async def on_vtb_upload_amount(update, context) -> int:
    try:
        amount_from, amount_to = parse_alfa_amounts(update.effective_message.text or "")
    except ValueError as error:
        return await _invalid(update, error, VU_AMOUNT, context)
    context.user_data["vtb_amount_from"] = amount_from
    context.user_data["vtb_amount_to"] = amount_to
    await _delete_user_message(update, context)
    return await _send_vtb_upload_prompt(update, context, VU_ACCOUNT)


async def on_vtb_upload_account_keep(update, context) -> int:
    await update.callback_query.answer()
    context.user_data["vtb_account"] = None
    return await _send_vtb_upload_prompt(update, context, VU_PAYER)


async def on_vtb_upload_account(update, context) -> int:
    try:
        context.user_data["vtb_account"] = parse_vtb_account_digits(
            update.effective_message.text or ""
        )
    except ValueError as error:
        return await _invalid(update, error, VU_ACCOUNT, context)
    await _delete_user_message(update, context)
    return await _send_vtb_upload_prompt(update, context, VU_PAYER)


async def on_vtb_upload_payer_keep(update, context) -> int:
    await update.callback_query.answer()
    context.user_data["vtb_payer"] = None
    return await _send_vtb_upload_prompt(update, context, VU_RECIPIENT)


async def on_vtb_upload_payer(update, context) -> int:
    text = (update.effective_message.text or "").strip()
    if text in ("-", "оставить", "Оставить"):
        context.user_data["vtb_payer"] = None
    else:
        try:
            context.user_data["vtb_payer"] = parse_name(text, "ФИО плательщика")
        except ValueError as error:
            return await _invalid(update, error, VU_PAYER, context)
    await _delete_user_message(update, context)
    return await _send_vtb_upload_prompt(update, context, VU_RECIPIENT)


async def on_vtb_upload_recipient_keep(update, context) -> int:
    await update.callback_query.answer()
    context.user_data["vtb_recipient"] = None
    return await _run_vtb_upload(update, context)


async def on_vtb_upload_recipient(update, context) -> int:
    text = (update.effective_message.text or "").strip()
    if text in ("-", "оставить", "Оставить"):
        context.user_data["vtb_recipient"] = None
    else:
        try:
            context.user_data["vtb_recipient"] = parse_name(text, "ФИО получателя")
        except ValueError as error:
            return await _invalid(update, error, VU_RECIPIENT, context)
    await _delete_user_message(update, context)
    return await _run_vtb_upload(update, context)


async def _run_vtb_upload(update, context) -> int:
    data = context.user_data
    source = data.get("vtb_up_path")
    if not source or not Path(str(source)).is_file():
        return await _send_vtb_upload_prompt(
            update, context, VU_UPLOAD, prefix="⚠️ Чек не найден. Пришлите PDF ещё раз.\n\n"
        )
    amount_from = int(data["vtb_amount_from"])
    amount_to = int(data["vtb_amount_to"])
    chat_id = update.effective_chat.id
    user = update.effective_user
    out_dir = Path(tempfile.mkdtemp(prefix="checketop_vtb_out_"))
    stem = Path(str(data.get("vtb_up_name") or "vtb.pdf")).stem
    filename = f"{stem}_{amount_to}.pdf"
    output_path = out_dir / filename
    await _send_tracked_message(update, context, "⏳ Меняю чек…")
    try:
        await asyncio.to_thread(
            apply_vtb_upload_patch,
            Path(str(source)),
            output_path,
            amount_from,
            amount_to,
            data.get("vtb_account"),
            data.get("vtb_payer"),
            data.get("vtb_recipient"),
        )
        if not output_path.is_file():
            raise RuntimeError("Патчер не записал PDF")
        await _delete_chat_message(
            context, chat_id, context.user_data.pop("_bot_prompt_id", None)
        )
        bits = [f"{amount_from} ₽ → {amount_to} ₽"]
        if data.get("vtb_account"):
            bits.append(f"счёт …{data['vtb_account'][-4:]}")
        if data.get("vtb_payer"):
            bits.append(f"плательщик {data['vtb_payer']}")
        if data.get("vtb_recipient"):
            bits.append(f"получатель {data['vtb_recipient']}")
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Ещё чек", VTB_UPLOAD_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
        )
        with output_path.open("rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id, document=handle, filename=filename,
                caption="✅ Готово: " + ", ".join(bits),
                reply_markup=markup,
            )
    except Exception as error:
        logger.exception(
            "VTB upload patch failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
        )
        return await _send_vtb_upload_prompt(
            update, context, VU_RECIPIENT,
            prefix=f"Не получилось: {_short_error(error)}\n\n",
        )
    finally:
        try:
            output_path.unlink(missing_ok=True)
            out_dir.rmdir()
        except OSError:
            pass
    _cleanup_vtb_upload(dict(context.user_data))
    _clear_draft(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Сборка приложения
# ---------------------------------------------------------------------------


def build_application(token: str) -> Application:
    import extra_banks
    import admin_access
    extra_banks.bind(
        send=_send_tracked_message,
        delete_user=_delete_user_message,
        drop_prompt=_drop_prompt,
        clear=_clear_draft,
        nav=_nav_row,
        main=on_main_menu,
        main_cb=MAIN_MENU_CB,
    )
    admin_access.bind(
        send=_send_tracked_message,
        delete_user=_delete_user_message,
        main_cb=MAIN_MENU_CB,
    )
    app = Application.builder().token(token).build()
    text = filters.TEXT & ~filters.COMMAND
    back = CallbackQueryHandler(on_nav_back, pattern=f"^{BACK_CB}$")
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(choose_yandex, pattern=f"^{YANDEX_CB}$"),
            CallbackQueryHandler(on_rocket_menu, pattern=f"^{ROCKET_MENU_CB}$"),
            CallbackQueryHandler(on_mts_start, pattern=f"^{MTS_CB}$"),
            CallbackQueryHandler(on_vtb_menu, pattern=f"^{VTB_CB}$"),
            CallbackQueryHandler(on_wb_menu, pattern=f"^{WB_CB}$"),
            CallbackQueryHandler(on_alfa_start, pattern=f"^{ALFA_CB}$"),
            *extra_banks.entry_points(),
            *admin_access.menu_handlers(),
            CallbackQueryHandler(on_rocket_sbp_start, pattern=f"^{ROCKET_SBP_CB}$"),
            CallbackQueryHandler(on_rocket_card_start, pattern=f"^{ROCKET_CARD_CB}$"),
            CallbackQueryHandler(on_rocket_stmt_start, pattern=f"^{ROCKET_STMT_CB}$"),
            CallbackQueryHandler(on_mts_sbp_start, pattern=f"^{MTS_SBP_CB}$"),
            CallbackQueryHandler(on_mts_card_start, pattern=f"^{MTS_CARD_CB}$"),
            CallbackQueryHandler(on_mts_stmt_start, pattern=f"^{MTS_STMT_CB}$"),
            CallbackQueryHandler(on_vtb_sbp_start, pattern=f"^{VTB_SBP_CB}$"),
            CallbackQueryHandler(on_vtb_card_start, pattern=f"^{VTB_CARD_CB}$"),
            CallbackQueryHandler(on_vtb_stmt_start, pattern=f"^{VTB_STMT_CB}$"),
            CallbackQueryHandler(on_vtb_upload_start, pattern=f"^{VTB_UPLOAD_CB}$"),
            CallbackQueryHandler(on_wb_sbp_start, pattern=f"^{WB_SBP_CB}$"),
            CallbackQueryHandler(on_main_menu, pattern=f"^{MAIN_MENU_CB}$"),
            CallbackQueryHandler(
                on_yandex_doc_choice,
                pattern=rf"^({Y_DOC_CHECK_CB}|{Y_DOC_STMT_CB})$",
            ),
        ],
        states={
            BANK_SELECT: [
                CallbackQueryHandler(choose_yandex, pattern=f"^{YANDEX_CB}$"),
                CallbackQueryHandler(on_rocket_menu, pattern=f"^{ROCKET_MENU_CB}$"),
                CallbackQueryHandler(on_mts_start, pattern=f"^{MTS_CB}$"),
                CallbackQueryHandler(on_vtb_menu, pattern=f"^{VTB_CB}$"),
                CallbackQueryHandler(on_wb_menu, pattern=f"^{WB_CB}$"),
                CallbackQueryHandler(on_alfa_start, pattern=f"^{ALFA_CB}$"),
                *extra_banks.menu_handlers(),
                *admin_access.menu_handlers(),
            ],
            A_UPLOAD: [
                MessageHandler(filters.Document.ALL, on_alfa_document),
                MessageHandler(text, on_alfa_document),
                back,
            ],
            A_AMOUNT: [MessageHandler(text, on_alfa_amounts), back],
            M_RECIPIENT_NAME: [
                MessageHandler(text, on_mts_name),
                CallbackQueryHandler(on_mts_mem_recipient, pattern=f"^{M_MEM_RECIPIENT_CB}$"),
                back,
            ],
            M_RECIPIENT_PHONE: [MessageHandler(text, on_mts_phone), back],
            M_RECIPIENT_BANK: [
                CallbackQueryHandler(on_mts_bank_choice, pattern=r"^mbank:[a-z]+$"),
                back,
            ],
            M_RECIPIENT_BANK_CUSTOM: [MessageHandler(text, on_mts_bank_custom), back],
            M_AMOUNT: [MessageHandler(text, on_mts_amount), back],
            M_ACCOUNT_CHOICE: [
                CallbackQueryHandler(on_mts_account_choice, pattern=r"^macc:(keep|custom)$"),
                back,
            ],
            M_ACCOUNT_INPUT: [MessageHandler(text, on_mts_account_input), back],
            M_MESSAGE_CHOICE: [
                CallbackQueryHandler(on_mts_message_choice, pattern=r"^mmsg:(default|custom)$"),
                back,
            ],
            M_MESSAGE_INPUT: [MessageHandler(text, on_mts_message_input), back],
            M_TIME_CHOICE: [
                CallbackQueryHandler(on_mts_time_choice, pattern=r"^mtime:[a-z]+$"),
                back,
            ],
            M_TIME_INPUT: [MessageHandler(text, on_mts_time_input), back],
            M_SUMMARY: [
                CallbackQueryHandler(on_mts_generate, pattern=f"^{M_GENERATE_CB}$"),
                CallbackQueryHandler(on_mts_summary_edit, pattern=f"^{M_EDIT_CB}$"),
                back,
            ],
            M_EDIT_SELECT: [
                CallbackQueryHandler(on_mts_edit_field, pattern=r"^medit:[a-z_]+$"),
                back,
            ],
            M_MENU: [
                CallbackQueryHandler(on_mts_sbp_start, pattern=f"^{MTS_SBP_CB}$"),
                CallbackQueryHandler(on_mts_card_start, pattern=f"^{MTS_CARD_CB}$"),
                CallbackQueryHandler(on_mts_stmt_start, pattern=f"^{MTS_STMT_CB}$"),
                CallbackQueryHandler(on_main_menu, pattern=f"^{MAIN_MENU_CB}$"),
                back,
            ],
            MC_RECIPIENT_CARD: [
                MessageHandler(text, on_mc_recipient_card),
                CallbackQueryHandler(on_mc_mem_card, pattern=f"^{MC_MEM_CARD_CB}$"),
                back,
            ],
            MC_AMOUNT: [MessageHandler(text, on_mc_amount), back],
            MC_SENDER_CARD_CHOICE: [
                CallbackQueryHandler(on_mc_sender_card_choice, pattern=r"^mccard:(skip|custom)$"),
                back,
            ],
            MC_SENDER_CARD_INPUT: [MessageHandler(text, on_mc_sender_card_input), back],
            MC_ACCOUNT_CHOICE: [
                CallbackQueryHandler(on_mc_account_choice, pattern=r"^mcacc:(keep|custom)$"),
                back,
            ],
            MC_ACCOUNT_INPUT: [MessageHandler(text, on_mc_account_input), back],
            MC_TIME_CHOICE: [
                CallbackQueryHandler(on_mc_time_choice, pattern=r"^mctime:[a-z]+$"),
                back,
            ],
            MC_TIME_INPUT: [MessageHandler(text, on_mc_time_input), back],
            MC_SUMMARY: [
                CallbackQueryHandler(on_mc_generate, pattern=f"^{MC_GENERATE_CB}$"),
                CallbackQueryHandler(on_mc_summary_edit, pattern=f"^{MC_EDIT_CB}$"),
                back,
            ],
            MC_EDIT_SELECT: [
                CallbackQueryHandler(on_mc_edit_field, pattern=r"^mcedit:[a-z_]+$"),
                back,
            ],
            V_MENU: [
                CallbackQueryHandler(on_vtb_sbp_start, pattern=f"^{VTB_SBP_CB}$"),
                CallbackQueryHandler(on_vtb_card_start, pattern=f"^{VTB_CARD_CB}$"),
                CallbackQueryHandler(on_vtb_stmt_start, pattern=f"^{VTB_STMT_CB}$"),
                CallbackQueryHandler(on_vtb_upload_start, pattern=f"^{VTB_UPLOAD_CB}$"),
                CallbackQueryHandler(on_main_menu, pattern=f"^{MAIN_MENU_CB}$"),
                back,
            ],
            VU_UPLOAD: [
                MessageHandler(filters.Document.ALL, on_vtb_upload_document),
                MessageHandler(text, on_vtb_upload_document),
                back,
            ],
            VU_AMOUNT: [MessageHandler(text, on_vtb_upload_amount), back],
            VU_ACCOUNT: [
                CallbackQueryHandler(on_vtb_upload_account_keep, pattern=r"^vuacc:keep$"),
                MessageHandler(text, on_vtb_upload_account),
                back,
            ],
            VU_PAYER: [
                CallbackQueryHandler(on_vtb_upload_payer_keep, pattern=r"^vukeep:payer$"),
                MessageHandler(text, on_vtb_upload_payer),
                back,
            ],
            VU_RECIPIENT: [
                CallbackQueryHandler(on_vtb_upload_recipient_keep, pattern=r"^vukeep:recipient$"),
                MessageHandler(text, on_vtb_upload_recipient),
                back,
            ],
            V_RECIPIENT_NAME: [
                MessageHandler(text, on_v_recipient_name),
                CallbackQueryHandler(on_v_mem_recipient, pattern=f"^{V_MEM_RECIPIENT_CB}$"),
                back,
            ],
            V_RECIPIENT_PHONE: [MessageHandler(text, on_v_recipient_phone), back],
            V_RECIPIENT_CARD: [
                MessageHandler(text, on_v_recipient_card),
                CallbackQueryHandler(on_v_mem_recipient, pattern=f"^{V_MEM_RECIPIENT_CB}$"),
                back,
            ],
            V_RECIPIENT_BANK: [
                CallbackQueryHandler(on_v_bank_choice, pattern=r"^vbank:[a-z]+$"),
                back,
            ],
            V_RECIPIENT_BANK_CUSTOM: [MessageHandler(text, on_v_bank_custom), back],
            V_AMOUNT: [MessageHandler(text, on_v_amount), back],
            V_SENDER_NAME: [
                MessageHandler(text, on_v_sender_name),
                CallbackQueryHandler(on_v_mem_sender, pattern=f"^{V_MEM_SENDER_CB}$"),
                back,
            ],
            V_SENDER_PHONE: [MessageHandler(text, on_v_sender_phone), back],
            V_SENDER_ACCOUNT_CHOICE: [
                CallbackQueryHandler(on_v_account_choice, pattern=r"^vacc:(keep|custom)$"),
                back,
            ],
            V_SENDER_ACCOUNT_INPUT: [MessageHandler(text, on_v_account_input), back],
            V_PAYER_CARD_CHOICE: [
                CallbackQueryHandler(on_v_payer_card_choice, pattern=r"^vpcard:(skip|custom)$"),
                back,
            ],
            V_PAYER_CARD_INPUT: [MessageHandler(text, on_v_payer_card_input), back],
            V_MESSAGE_CHOICE: [
                CallbackQueryHandler(on_v_message_choice, pattern=r"^vmsg:(none|custom)$"),
                back,
            ],
            V_MESSAGE_INPUT: [MessageHandler(text, on_v_message_input), back],
            V_TIME_CHOICE: [
                CallbackQueryHandler(on_v_time_choice, pattern=r"^vtime:[a-z]+$"),
                back,
            ],
            V_TIME_INPUT: [MessageHandler(text, on_v_time_input), back],
            V_SUMMARY: [
                CallbackQueryHandler(on_v_generate, pattern=f"^{V_GENERATE_CB}$"),
                CallbackQueryHandler(on_v_summary_edit, pattern=f"^{V_EDIT_CB}$"),
                back,
            ],
            V_EDIT_SELECT: [
                CallbackQueryHandler(on_v_edit_field, pattern=r"^vedit:[a-z_]+$"),
                back,
            ],
            W_MENU: [
                CallbackQueryHandler(on_wb_sbp_start, pattern=f"^{WB_SBP_CB}$"),
                CallbackQueryHandler(on_main_menu, pattern=f"^{MAIN_MENU_CB}$"),
                back,
            ],
            W_RECIPIENT_NAME: [
                MessageHandler(text, on_w_recipient_name),
                CallbackQueryHandler(on_w_mem_recipient, pattern=f"^{W_MEM_RECIPIENT_CB}$"),
                back,
            ],
            W_RECIPIENT_PHONE: [MessageHandler(text, on_w_recipient_phone), back],
            W_RECIPIENT_BANK: [
                CallbackQueryHandler(on_w_bank_choice, pattern=r"^wbank:[a-z]+$"),
                back,
            ],
            W_RECIPIENT_BANK_CUSTOM: [MessageHandler(text, on_w_bank_custom), back],
            W_AMOUNT: [MessageHandler(text, on_w_amount), back],
            W_TIME_CHOICE: [
                CallbackQueryHandler(on_w_time_choice, pattern=r"^wtime:[a-z]+$"),
                back,
            ],
            W_TIME_INPUT: [MessageHandler(text, on_w_time_input), back],
            W_SUMMARY: [
                CallbackQueryHandler(on_w_generate, pattern=f"^{W_GENERATE_CB}$"),
                CallbackQueryHandler(on_w_summary_edit, pattern=f"^{W_EDIT_CB}$"),
                back,
            ],
            W_EDIT_SELECT: [
                CallbackQueryHandler(on_w_edit_field, pattern=r"^wedit:[a-z_]+$"),
                back,
            ],
            R_MENU: [
                CallbackQueryHandler(on_rocket_sbp_start, pattern=f"^{ROCKET_SBP_CB}$"),
                CallbackQueryHandler(on_rocket_card_start, pattern=f"^{ROCKET_CARD_CB}$"),
                CallbackQueryHandler(on_rocket_stmt_start, pattern=f"^{ROCKET_STMT_CB}$"),
                CallbackQueryHandler(on_main_menu, pattern=f"^{MAIN_MENU_CB}$"),
                back,
            ],
            R_RECIPIENT_NAME: [
                MessageHandler(text, on_r_recipient_name),
                CallbackQueryHandler(on_r_mem_recipient, pattern=f"^{MEM_RECIPIENT_CB}$"),
                back,
            ],
            R_RECIPIENT_PHONE: [MessageHandler(text, on_r_recipient_phone), back],
            R_RECIPIENT_CARD: [
                MessageHandler(text, on_r_recipient_card),
                CallbackQueryHandler(on_r_mem_recipient, pattern=f"^{MEM_RECIPIENT_CB}$"),
                back,
            ],
            R_RECIPIENT_BANK: [
                CallbackQueryHandler(on_r_bank_choice, pattern=r"^rbank:[a-z]+$"),
                back,
            ],
            R_RECIPIENT_BANK_CUSTOM: [MessageHandler(text, on_r_bank_custom), back],
            R_AMOUNT: [
                MessageHandler(text, on_r_amount_text),
                back,
            ],
            R_SENDER_NAME: [
                MessageHandler(text, on_r_sender_name),
                CallbackQueryHandler(on_r_mem_sender, pattern=f"^{MEM_SENDER_CB}$"),
                back,
            ],
            R_SENDER_PHONE: [MessageHandler(text, on_r_sender_phone), back],
            R_SENDER_ACCOUNT_CHOICE: [
                CallbackQueryHandler(on_r_sender_account_choice, pattern=r"^rsacc:(default|custom|rand[0-2])$"),
                back,
            ],
            R_SENDER_ACCOUNT_INPUT: [MessageHandler(text, on_r_sender_account_input), back],
            R_TIME_CHOICE: [
                CallbackQueryHandler(on_r_time_choice, pattern=r"^rtime:[a-z]+$"),
                back,
            ],
            R_TIME_INPUT: [MessageHandler(text, on_r_time_input), back],
            R_SUMMARY: [
                CallbackQueryHandler(on_r_generate, pattern=f"^{R_GENERATE_CB}$"),
                CallbackQueryHandler(on_r_summary_edit, pattern=f"^{R_EDIT_CB}$"),
                back,
            ],
            R_EDIT_SELECT: [
                CallbackQueryHandler(on_r_edit_field, pattern=r"^redit:[a-z_]+$"),
                back,
            ],
            S_PERIOD: [MessageHandler(text, on_s_period), back],
            S_ISSUE_CHOICE: [
                CallbackQueryHandler(on_s_issue_choice, pattern=r"^sissue:[a-z]+$"),
                back,
            ],
            S_ISSUE_INPUT: [MessageHandler(text, on_s_issue_input), back],
            S_HOLDER: [MessageHandler(text, on_s_holder), back],
            S_PHONE: [MessageHandler(text, on_s_phone), back],
            S_ACCOUNT_CHOICE: [
                CallbackQueryHandler(on_s_account_choice, pattern=r"^sacc:[a-z]+$"),
                back,
            ],
            S_ACCOUNT_TAIL_INPUT: [MessageHandler(text, on_s_account_tail), back],
            S_ACCOUNT_FULL_INPUT: [MessageHandler(text, on_s_account_full), back],
            S_CARD: [
                CallbackQueryHandler(on_s_card_choice, pattern=r"^scard:skip$"),
                MessageHandler(text, on_s_card_input),
                back,
            ],
            S_OPENING: [MessageHandler(text, on_s_opening), back],
            S_OP_COUNT_CHOICE: [
                CallbackQueryHandler(on_s_op_count_choice, pattern=r"^scount:(custom|\d+)$"),
                back,
            ],
            S_OP_COUNT_INPUT: [MessageHandler(text, on_s_op_count_input), back],
            S_SOURCE_CHOICE: [
                CallbackQueryHandler(on_s_source_choice, pattern=r"^ssource:[a-z]+$"),
                back,
            ],
            S_UPLOAD: [
                MessageHandler(filters.Document.PDF, on_s_upload_document),
                CallbackQueryHandler(on_s_uploads_done, pattern=f"^{S_UPLOADS_DONE_CB}$"),
                MessageHandler(text, on_s_upload_text),
                back,
            ],
            S_UPLOAD_PENDING: [
                CallbackQueryHandler(on_s_period_fix, pattern=f"^{S_PERIOD_FIX_CB}$"),
                back,
            ],
            S_SHORTAGE: [
                CallbackQueryHandler(on_s_shortage_fill, pattern=f"^{S_FILL_RANDOM_CB}$"),
                CallbackQueryHandler(on_s_shortage_reduce, pattern=f"^{S_REDUCE_COUNT_CB}$"),
                back,
            ],
            S_OP_DAY_CHOICE: [
                CallbackQueryHandler(on_s_day_receipt, pattern=f"^{S_DAY_RECEIPT_CB}$"),
                CallbackQueryHandler(on_s_day_choose, pattern=f"^{S_DAY_CHOOSE_CB}$"),
                back,
            ],
            S_OP_DATE_INPUT: [MessageHandler(text, on_s_op_date_input), back],
            S_SUMMARY: [
                CallbackQueryHandler(on_s_generate, pattern=f"^{S_GENERATE_CB}$"),
                CallbackQueryHandler(on_s_summary_edit, pattern=f"^{S_EDIT_CB}$"),
                back,
            ],
            S_EDIT_SELECT: [
                CallbackQueryHandler(on_s_edit_field, pattern=r"^sedit:[a-z_]+$"),
                back,
            ],
            S_OPS_EDITOR: [
                CallbackQueryHandler(on_s_ops_editor_choice, pattern=r"^sops:[a-z]+$"),
                back,
            ],
            S_OP_KIND: [
                CallbackQueryHandler(on_s_op_kind, pattern=r"^sopk:[a-z_]+$"),
                back,
            ],
            S_OP_AMOUNT: [MessageHandler(text, on_s_op_amount), back],
            S_OP_DT_CHOICE: [
                CallbackQueryHandler(on_s_op_dt_choice, pattern=r"^sopdt:[a-z]+$"),
                back,
            ],
            S_OP_DT_INPUT: [MessageHandler(text, on_s_op_dt_input), back],
            S_OP_DETAIL: [
                CallbackQueryHandler(on_s_op_detail_skip, pattern=r"^sopd:[a-z_]+$"),
                MessageHandler(text, on_s_op_detail_text),
                back,
            ],
            S_OP_DELETE: [
                CallbackQueryHandler(on_s_op_delete_choice, pattern=r"^sopdel:\d+$"),
                back,
            ],
            Y_MENU: [
                CallbackQueryHandler(
                    on_yandex_doc_choice,
                    pattern=rf"^({Y_DOC_CHECK_CB}|{Y_DOC_STMT_CB})$",
                ),
                back,
            ],
            YS_PERIOD: [MessageHandler(text, on_ystmt_period), back],
            YS_ISSUE_CHOICE: [
                CallbackQueryHandler(on_ystmt_issue_choice, pattern=r"^ysissue:[a-z]+$"),
                back,
            ],
            YS_ISSUE_INPUT: [MessageHandler(text, on_ystmt_issue_input), back],
            YS_HOLDER_NAME: [
                MessageHandler(text, on_ystmt_holder_name),
                CallbackQueryHandler(on_ystmt_mem_holder, pattern=f"^{YS_MEM_HOLDER_CB}$"),
                back,
            ],
            YS_HOLDER_BIRTH: [MessageHandler(text, on_ystmt_holder_birth), back],
            YS_HOLDER_PASSPORT: [MessageHandler(text, on_ystmt_holder_passport), back],
            YS_CONTRACT_DATE_CHOICE: [
                CallbackQueryHandler(on_ystmt_contract_date_choice, pattern=r"^yscontract:[a-z]+$"),
                back,
            ],
            YS_CONTRACT_DATE_INPUT: [MessageHandler(text, on_ystmt_contract_date_input), back],
            YS_CONTRACT_NUMBER_CHOICE: [
                CallbackQueryHandler(on_ystmt_contract_number_choice, pattern=r"^ysnum:[a-z]+$"),
                back,
            ],
            YS_CONTRACT_NUMBER_INPUT: [MessageHandler(text, on_ystmt_contract_number_input), back],
            YS_OPENING: [MessageHandler(text, on_ystmt_opening), back],
            YS_OPS_EDITOR: [
                CallbackQueryHandler(on_ystmt_ops_editor_choice, pattern=r"^ysops:[a-z]+$"),
                MessageHandler(text, on_ystmt_ops_editor_text),
                back,
            ],
            YS_OP_TYPE: [
                CallbackQueryHandler(on_ystmt_op_type, pattern=r"^ysop:[a-z]+$"),
                back,
            ],
            YS_OP_NAME: [MessageHandler(text, on_ystmt_op_name), back],
            YS_OP_PHONE: [MessageHandler(text, on_ystmt_op_phone), back],
            YS_OP_BANK: [
                CallbackQueryHandler(on_ystmt_op_bank_choice, pattern=r"^ysbank:[a-z]+$"),
                back,
            ],
            YS_OP_BANK_CUSTOM: [MessageHandler(text, on_ystmt_op_bank_custom), back],
            YS_OP_CUSTOM_SIGN: [
                CallbackQueryHandler(on_ystmt_op_custom_sign, pattern=r"^yssign:[a-z]+$"),
                back,
            ],
            YS_OP_CUSTOM_DESC: [MessageHandler(text, on_ystmt_op_custom_desc), back],
            YS_OP_DT_CHOICE: [
                CallbackQueryHandler(on_ystmt_op_dt_choice, pattern=r"^ysdt:[a-z]+$"),
                back,
            ],
            YS_OP_DT_INPUT: [MessageHandler(text, on_ystmt_op_dt_input), back],
            YS_OP_AMOUNT: [MessageHandler(text, on_ystmt_op_amount), back],
            YS_OP_DELETE: [
                CallbackQueryHandler(on_ystmt_op_delete_choice, pattern=r"^ysdel:\d+$"),
                back,
            ],
            YS_SUMMARY: [
                CallbackQueryHandler(on_ystmt_generate, pattern=f"^{YS_GENERATE_CB}$"),
                CallbackQueryHandler(on_ystmt_summary_edit, pattern=f"^{YS_EDIT_CB}$"),
                back,
            ],
            YS_EDIT_SELECT: [
                CallbackQueryHandler(on_ystmt_edit_field, pattern=r"^ysedit:[a-z_]+$"),
                back,
            ],
            Y_MODE: [
                CallbackQueryHandler(
                    on_y_mode_choice,
                    pattern=rf"^({Y_MODE_NORMAL_CB}|{Y_MODE_TEMPLATE_CB})$",
                ),
                back,
            ],
            Y_TEMPLATE_UPLOAD: [
                MessageHandler(filters.Document.PDF, on_y_template_document),
                MessageHandler(text, on_y_template_text),
                back,
            ],
            Y_TEMPLATE_WARN: [
                CallbackQueryHandler(
                    on_y_template_proceed, pattern=f"^{Y_TEMPLATE_PROCEED_CB}$"
                ),
            ],
            RECIPIENT_NAME: [
                MessageHandler(text, on_recipient_name),
                CallbackQueryHandler(on_y_mem_recipient, pattern=f"^{Y_MEM_RECIPIENT_CB}$"),
                back,
            ],
            RECIPIENT_PHONE: [MessageHandler(text, on_recipient_phone), back],
            RECIPIENT_BANK: [
                CallbackQueryHandler(on_y_bank_choice, pattern=r"^ybank:[a-z]+$"),
                MessageHandler(text, on_recipient_bank),
                back,
            ],
            Y_RECIPIENT_BANK_CUSTOM: [MessageHandler(text, on_recipient_bank), back],
            AMOUNT: [MessageHandler(text, on_amount), back],
            SENDER_NAME: [
                MessageHandler(text, on_sender_name),
                CallbackQueryHandler(on_y_mem_sender, pattern=f"^{Y_MEM_SENDER_CB}$"),
                back,
            ],
            SENDER_PHONE: [MessageHandler(text, on_sender_phone), back],
            Y_TIME_CHOICE: [
                CallbackQueryHandler(on_y_time_choice, pattern=r"^ytime:[a-z]+$"),
                CallbackQueryHandler(on_datetime_now, pattern=f"^{NOW_CB}$"),
                back,
            ],
            DATETIME: [MessageHandler(text, on_datetime_text), back],
            CONFIRM: [
                CallbackQueryHandler(on_confirm_yes, pattern=f"^{CONFIRM_YES_CB}$"),
                CallbackQueryHandler(on_confirm_edit, pattern=f"^{CONFIRM_EDIT_CB}$"),
                back,
            ],
            EDIT_SELECT: [
                CallbackQueryHandler(on_edit_field, pattern=r"^edit:[a-z_]+$"),
                back,
            ],
            **extra_banks.conversation_states(back),
            **admin_access.conversation_states(back),
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=f"^{CANCEL_CB}$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(TypeHandler(Update, restrict_user), group=-1)
    app.add_handler(conversation)
    app.add_error_handler(on_error)
    return app


def setup_logging() -> Path:
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot_errors.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(logging.INFO)
    return log_file


def _start_health_server():
    """Крошечный HTTP-ответ для хостингов с PORT и внешних пингеров.

    Бесплатные web-сервисы (Render и т.п.) требуют открытый порт и
    засыпают без входящих запросов; пингер (UptimeRobot и т.п.) раз в
    5 минут дёргает ``/`` и не даёт сервису уснуть. Если PORT не задан
    (локальный запуск), сервер не стартует и ни на что не влияет.
    """
    port_raw = os.environ.get("PORT", "").strip()
    if not port_raw:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        logger.error("PORT задан неверно: %r — health-сервер не запущен", port_raw)
        return None
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - имя из http.server
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # не засорять лог пингами
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", port)
    return server


def main() -> int:
    log_file = setup_logging()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        message = (
            "BOT_TOKEN не задан. Задайте токен в переменной окружения, "
            "например: BOT_TOKEN=... python3 bot/yandex_bot.py"
        )
        logger.error(message)
        print(message, file=sys.stderr)
        return 2
    jre_bin = REPO_ROOT / "jre" / "bin" / "java"
    if jre_bin.exists():
        os.environ.setdefault("JAVA_BIN", str(jre_bin))
    os.environ.setdefault("JZCOMP_DIR", str(REPO_ROOT / "banks" / "rshb"))
    logger.info("Starting checketop bot (Yandex + Rocket); errors log: %s", log_file)
    _start_health_server()
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
