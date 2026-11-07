#!/usr/bin/env python3
"""Telegram-бот checketop — Яндекс Банк и Рокетбанк.

Бот ведёт пошаговый диалог, собирает данные перевода/выписки и вызывает
готовые движки: Яндекса ``banks/yandex/engine.py`` (``generate_yandex``)
и Рокета ``banks/rocket/engine.py`` (``generate_rocket`` /
``generate_rocket_card``) + ``banks/rocket/rocket_statement.py``
(``generate_rocket_statement``, ``operation_from_receipt``).
Никакой логики работы с PDF в боте нет: бот только валидирует ввод,
вызывает движок и отправляет пользователю полученный файл.

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
    return user_id in allowed_user_ids()


async def restrict_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропустить только разрешённые id. Остальным — «Нет доступа» и стоп."""
    user = update.effective_user
    if is_allowed_user(getattr(user, "id", None)):
        return
    query = update.callback_query
    if query is not None:
        await query.answer("Нет доступа.", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("Нет доступа.")
    logger.info(
        "Rejected update from user_id=%s",
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
MAIN_MENU_CB = "menu:main"
BACK_CB = "nav:back"
ROCKET_SBP_CB = "rocket:sbp"
ROCKET_CARD_CB = "rocket:card"
ROCKET_STMT_CB = "rocket:stmt"
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
    return f"…{ROCKET_DEFAULT_ACCOUNT_TAIL}"


def statement_sources_text(data: dict) -> str:
    """Человекочитаемый источник платежей выписки для сводки/подписи."""
    total = int(data.get("stmt_operation_count") or 0)
    uploaded = len(data.get("stmt_receipt_paths") or [])
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
    """Вызвать движок и вернуть путь к готовому PDF. PDF-логики тут нет."""
    generate = load_generate_yandex()
    extra = {}
    if data.get("yandex_mode") == "template" and data.get("y_template_path"):
        # Режим «с шаблоном»: чек генерируется именно из квитанции
        # пользователя; в обычном режиме donor_path не передаётся и
        # движок сам выбирает донора из пула.
        extra["donor_path"] = str(data["y_template_path"])
    result = await asyncio.to_thread(
        generate,
        amount=data["amount"],
        operation_dt=data["operation_dt"],
        sender_name=data["sender_name"],
        sender_phone=data["sender_phone"],
        recipient_name=data["recipient_name"],
        recipient_phone=data["recipient_phone"],
        recipient_bank=data["recipient_bank"],
        output_path=str(output_path),
        **extra,
    )
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


async def generate_rocket_statement_pdf(data: dict, output_path: Path) -> Path:
    """Выписка Рокета: только вызов ``generate_rocket_statement``."""
    generate = load_generate_rocket_statement()
    account = data.get("stmt_account") if data.get("stmt_account_mode") == "full" else None
    account_tail = (
        data.get("stmt_account_tail") if data.get("stmt_account_mode") == "tail" else None
    )
    result = await asyncio.to_thread(
        generate,
        period_start=data["stmt_period_start"],
        period_end=data["stmt_period_end"],
        issue_date=data["stmt_issue_date"],
        holder_name=data["stmt_holder_name"],
        account=account,
        account_tail=account_tail,
        random_account_tail=False,
        opening_balance=data["stmt_opening_balance"],
        operation_count=int(data["stmt_operation_count"]),
        operation_date=data.get("stmt_operation_date"),
        receipt_paths=list(data.get("stmt_receipt_paths") or []),
        phone=data["stmt_phone"],
        fill_random=bool(data.get("stmt_fill_random", True)),
        output_path=str(output_path),
    )
    return _ensure_pdf_result(result, output_path, "generate_rocket_statement")


def import_receipt_operation(path) -> dict:
    """Чек PDF -> операция выписки, строго через движок выписки."""
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
        elif scenario == "rocket_statement":
            builders = [lambda: _stmt_prompt(context, state)]
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


def _main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("🟡 Яндекс чек", YANDEX_CB), _btn("🚀 Рокетбанк", ROCKET_MENU_CB)]]
    )


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
        rows = _rows(
            [
                _btn("0", "scount:0"), _btn("1", "scount:1"), _btn("3", "scount:3"),
                _btn("5", "scount:5"), _btn("6", "scount:6"),
                _btn("✍️ Другое (0–10)", "scount:custom"),
            ]
        ) + [nav]
        return ("🔢 <b>Сколько платежей</b> в выписке (0–10)?", InlineKeyboardMarkup(rows))
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
            ]
        ) + [nav]
        return ("🧾 <b>Откуда платежи?</b>", InlineKeyboardMarkup(rows))
    if state == S_UPLOAD:
        uploaded = len(data.get("stmt_receipt_paths") or [])
        total = int(data.get("stmt_operation_count") or 0)
        rows = [[_btn("✅ Готово", S_UPLOADS_DONE_CB)], nav]
        return (
            f"📎 Загружайте чеки Рокета <b>PDF по одному</b>.\n"
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
    raise ValueError(f"no statement prompt for state {state}")


async def _send_stmt_prompt(update, context, state: int, prefix: str = "") -> int:
    context.user_data["current_step"] = state
    text, markup = _stmt_prompt(context, state)
    await _send_tracked_message(update, context, f"{prefix}{text}", reply_markup=markup)
    return state


def _stmt_summary_text(data: dict) -> str:
    lines = [
        "<b>Проверь данные выписки</b>",
        "",
        f"📅 Период: <b>{data['stmt_period_start']:%d.%m.%Y}–{data['stmt_period_end']:%d.%m.%Y}</b>",
        f"📅 Дата выдачи: <b>{data['stmt_issue_date']:%d.%m.%Y}</b>",
        f"👤 Держатель: <b>{html.escape(str(data.get('stmt_holder_name', '')))}</b>",
        f"📱 Телефон: <b>{html.escape(str(data.get('stmt_phone', '')))}</b>",
        f"💳 Счёт: <b>{statement_account_display(data)}</b>",
        f"💰 Входящий остаток: <b>{format_money_display(data.get('stmt_opening_balance', '0'))}</b>",
        f"🔢 Платежи: <b>{html.escape(statement_sources_text(data))}</b>",
    ]
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
    "stmt_opening": S_OPENING,
    "stmt_op_count": S_OP_COUNT_CHOICE,
}


def _statement_edit_fields():
    labels = {
        "stmt_period": "📅 Период", "stmt_issue": "📅 Дата выдачи",
        "stmt_holder": "👤 Держатель", "stmt_phone": "📱 Телефон",
        "stmt_account": "💳 Счёт", "stmt_opening": "💰 Входящий остаток",
        "stmt_op_count": "🔢 Число платежей",
    }
    return [(key, labels[key], STATEMENT_EDIT_STATES[key]) for key in labels]


def _back_target(data: dict, current: Optional[int]) -> Optional[int]:
    scenario = data.get("scenario")
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
        S_ACCOUNT_CHOICE, S_ACCOUNT_TAIL_INPUT, S_ACCOUNT_FULL_INPUT,
        S_OPENING, S_OP_COUNT_CHOICE, S_OP_COUNT_INPUT, S_SOURCE_CHOICE,
        S_UPLOAD, S_UPLOAD_PENDING, S_SHORTAGE, S_OP_DAY_CHOICE,
        S_OP_DATE_INPUT, S_SUMMARY, S_EDIT_SELECT,
    }
    if current is None:
        return R_MENU if scenario in ("rocket_sbp", "rocket_card", "rocket_statement") else None
    if current in (R_EDIT_SELECT,):
        return R_SUMMARY
    if current in (S_EDIT_SELECT,):
        return S_SUMMARY
    if current in stmt_states or scenario == "rocket_statement":
        mapping = {
            S_PERIOD: R_MENU, S_ISSUE_CHOICE: S_PERIOD, S_ISSUE_INPUT: S_ISSUE_CHOICE,
            S_HOLDER: S_ISSUE_CHOICE, S_PHONE: S_HOLDER, S_ACCOUNT_CHOICE: S_PHONE,
            S_ACCOUNT_TAIL_INPUT: S_ACCOUNT_CHOICE, S_ACCOUNT_FULL_INPUT: S_ACCOUNT_CHOICE,
            S_OPENING: S_ACCOUNT_CHOICE, S_OP_COUNT_CHOICE: S_OPENING,
            S_OP_COUNT_INPUT: S_OP_COUNT_CHOICE, S_SOURCE_CHOICE: S_OP_COUNT_CHOICE,
            S_UPLOAD: S_SOURCE_CHOICE, S_UPLOAD_PENDING: S_UPLOAD,
            S_SHORTAGE: S_UPLOAD, S_OP_DAY_CHOICE: S_SOURCE_CHOICE,
            S_OP_DATE_INPUT: S_OP_DAY_CHOICE, S_SUMMARY: S_OP_COUNT_CHOICE,
        }
        if current == S_SUMMARY and data.get("stmt_receipt_paths"):
            return S_UPLOAD
        if current == S_SUMMARY and int(data.get("stmt_operation_count") or 0) > 0:
            return S_OP_DAY_CHOICE if data.get("stmt_fill_random") else S_SOURCE_CHOICE
        return mapping.get(current, R_MENU)
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
        reply_markup=_main_menu_markup(),
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
        reply_markup=_main_menu_markup(),
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
        if current in STATEMENT_EDIT_STATES.values() or data.get("scenario") == "rocket_statement":
            return await _send_stmt_summary(update, context)
        return await _send_rocket_summary(update, context)
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
    target = _back_target(data, current)
    if target is None:
        _cleanup_stmt_files(dict(context.user_data))
        _clear_draft(context)
        return await _send_main_menu(update, context)
    if target == R_MENU:
        return await _send_rocket_menu(update, context)
    if target == R_SUMMARY:
        return await _send_rocket_summary(update, context)
    if target == S_SUMMARY:
        return await _send_stmt_summary(update, context)
    if target >= S_PERIOD:
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
    return await _stmt_after_field(update, context, S_PHONE)


async def on_s_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_phone"] = parse_phone(update.effective_message.text)
    except ValueError as error:
        return await _invalid(update, error, S_PHONE, context)
    return await _stmt_after_field(update, context, S_ACCOUNT_CHOICE)


async def on_s_account_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "tail":
        return await _send_stmt_prompt(update, context, S_ACCOUNT_TAIL_INPUT)
    if slug == "full":
        return await _send_stmt_prompt(update, context, S_ACCOUNT_FULL_INPUT)
    if slug == "random":
        context.user_data["stmt_account_mode"] = "tail"
        context.user_data["stmt_account_tail"] = "".join(
            secrets.choice("0123456789") for _ in range(5)
        )
        context.user_data["stmt_account"] = None
    else:  # keep
        context.user_data["stmt_account_mode"] = "keep"
        context.user_data["stmt_account"] = None
        context.user_data["stmt_account_tail"] = None
    return await _stmt_after_field(update, context, S_OPENING)


async def on_s_account_tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_account_tail"] = parse_account_tail(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_ACCOUNT_TAIL_INPUT, context)
    context.user_data["stmt_account_mode"] = "tail"
    context.user_data["stmt_account"] = None
    return await _stmt_after_field(update, context, S_OPENING)


async def on_s_account_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_account"] = parse_account20(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_ACCOUNT_FULL_INPUT, context)
    context.user_data["stmt_account_mode"] = "full"
    context.user_data["stmt_account_tail"] = None
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
        return await _send_stmt_prompt(update, context, S_OP_COUNT_INPUT)
    context.user_data["stmt_operation_count"] = int(slug)
    return await _s_after_count_set(update, context)


async def on_s_op_count_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["stmt_operation_count"] = parse_operation_count(
            update.effective_message.text
        )
    except ValueError as error:
        return await _invalid(update, error, S_OP_COUNT_INPUT, context)
    return await _s_after_count_set(update, context)


async def on_s_source_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    slug = update.callback_query.data.split(":", 1)[1]
    data = context.user_data
    data["stmt_source"] = slug
    if slug == "random":
        if data.get("stmt_receipt_paths"):
            _cleanup_stmt_files(dict(data))
            data["stmt_receipt_paths"] = []
            data["stmt_receipt_dates"] = []
        data["stmt_fill_random"] = True
        return await _send_stmt_prompt(update, context, S_OP_DAY_CHOICE)
    data["stmt_fill_random"] = slug == "mixed"
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
        operation = await asyncio.to_thread(import_receipt_operation, local_path)
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
            for key, label, _state in _statement_edit_fields()
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
    data = dict(context.user_data)
    await _send_tracked_message(update, context, "⏳ Формирую выписку…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="checketop_rocket_stmt_out_"))
    filename = rocket_statement_filename(data)
    output_path = tmp_dir / filename
    try:
        pdf_path = await generate_rocket_statement_pdf(data, output_path)
        markup = InlineKeyboardMarkup(
            [[_btn("🔁 Новая выписка", ROCKET_STMT_CB)], [_btn("🏠 Главное меню", MAIN_MENU_CB)]]
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
            "Rocket statement generation failed (user_id=%s, chat_id=%s)",
            getattr(user, "id", "?"), chat_id,
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
# Сборка приложения
# ---------------------------------------------------------------------------


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    text = filters.TEXT & ~filters.COMMAND
    back = CallbackQueryHandler(on_nav_back, pattern=f"^{BACK_CB}$")
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(choose_yandex, pattern=f"^{YANDEX_CB}$"),
            CallbackQueryHandler(on_rocket_menu, pattern=f"^{ROCKET_MENU_CB}$"),
            CallbackQueryHandler(on_rocket_sbp_start, pattern=f"^{ROCKET_SBP_CB}$"),
            CallbackQueryHandler(on_rocket_card_start, pattern=f"^{ROCKET_CARD_CB}$"),
            CallbackQueryHandler(on_rocket_stmt_start, pattern=f"^{ROCKET_STMT_CB}$"),
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
    logger.info("Starting checketop bot (Yandex + Rocket); errors log: %s", log_file)
    _start_health_server()
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
