# -*- coding: utf-8 -*-
"""Россельхозбанк, Озон и Т-Банк — движки из новой папки, кнопки как у остальных.

Россельхоз и фабричный Озон — генераторы mts-bot с VPS.
Т-Банк СБП — донор tbank_sbp_verified_05052026_enriched и scratch-сервис чекетопа.
Правка суммы — настоящий PDF, ID операции не трогаем.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger("extra_banks")

REPO = Path(__file__).resolve().parents[1]
RSHB_CB = "bank:rshb"
OZON_CB = "bank:ozon"
TBANK_CB = "bank:tbank"
RSHB_NEW_CB = "rshb:new"
OZ_NEW_CB = "ozon:new"
OZ_EDIT_CB = "ozon:edit"
TB_NEW_CB = "tbank:new"
TB_EDIT_CB = "tbank:edit"
GEN_CB = "nb:gen"
EDIT_CB = "nb:edit"
SKIP_CB = "nb:skip"
NOW_CB = "nb:now"
AUTO_CB = "nb:auto"

(
    RSHB_NAME,
    RSHB_PHONE,
    RSHB_BANK,
    RSHB_BANK_CUSTOM,
    RSHB_AMOUNT,
    RSHB_ACCOUNT,
    RSHB_SUMMARY,
    OZ_MENU,
    OZ_NAME,
    OZ_PHONE,
    OZ_BANK,
    OZ_BANK_CUSTOM,
    OZ_AMOUNT,
    OZ_SENDER,
    OZ_SUMMARY,
    OZ_EDIT_PDF,
    OZ_EDIT_AMOUNT,
    TB_MENU,
    TB_AMOUNT,
    TB_SENDER,
    TB_ACCOUNT,
    TB_RECEIVER,
    TB_PHONE,
    TB_BANK,
    TB_BANK_CUSTOM,
    TB_TIME,
    TB_TIME_INPUT,
    TB_SUMMARY,
    TB_EDIT_PDF,
    TB_EDIT_AMOUNT,
) = range(210, 240)

_BANKS = [
    "Т-Банк", "Сбербанк", "Альфа-Банк", "ВТБ", "Совкомбанк",
    "Озон Банк", "Газпромбанк", "Россельхозбанк", "Почта Банк", "МТС Банк",
    "Яндекс Банк", "ПСБ", "Райффайзенбанк",
]

_PREV = {
    RSHB_NAME: "main",
    RSHB_PHONE: RSHB_NAME,
    RSHB_BANK: RSHB_PHONE,
    RSHB_BANK_CUSTOM: RSHB_BANK,
    RSHB_AMOUNT: RSHB_BANK,
    RSHB_ACCOUNT: RSHB_AMOUNT,
    RSHB_SUMMARY: RSHB_ACCOUNT,
    OZ_MENU: "main",
    OZ_NAME: OZ_MENU,
    OZ_PHONE: OZ_NAME,
    OZ_BANK: OZ_PHONE,
    OZ_BANK_CUSTOM: OZ_BANK,
    OZ_AMOUNT: OZ_BANK,
    OZ_SENDER: OZ_AMOUNT,
    OZ_SUMMARY: OZ_SENDER,
    OZ_EDIT_PDF: OZ_MENU,
    OZ_EDIT_AMOUNT: OZ_EDIT_PDF,
    TB_MENU: "main",
    TB_AMOUNT: TB_MENU,
    TB_SENDER: TB_AMOUNT,
    TB_ACCOUNT: TB_SENDER,
    TB_RECEIVER: TB_ACCOUNT,
    TB_PHONE: TB_RECEIVER,
    TB_BANK: TB_PHONE,
    TB_BANK_CUSTOM: TB_BANK,
    TB_TIME: TB_BANK,
    TB_TIME_INPUT: TB_TIME,
    TB_SUMMARY: TB_TIME,
    TB_EDIT_PDF: TB_MENU,
    TB_EDIT_AMOUNT: TB_EDIT_PDF,
}

_api: dict = {}
_TEXT = filters.TEXT & ~filters.COMMAND


def bind(**funcs) -> None:
    _api.update(funcs)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _nav():
    return _api["nav"]()


def _markup(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows) + [_nav()])


def _pairs(labels_data: list, width: int = 2) -> list:
    buttons = [_btn(text, data) for text, data in labels_data]
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def _bank_rows(prefix: str) -> list:
    items = [(name, f"{prefix}:{i}") for i, name in enumerate(_BANKS)]
    items.append(("✏️ Свой банк", f"{prefix}:custom"))
    return _pairs(items, 2)


async def _ask(update, context, state: int, text: str, rows: list) -> int:
    context.user_data["current_step"] = state
    await _api["send"](update, context, text, reply_markup=_markup(rows))
    return state


async def _fail(update, context, state: int, text: str, rows: list) -> int:
    return await _ask(update, context, state, text, rows)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _yo(text: str) -> str:
    return (text or "").replace("Ё", "Е").replace("ё", "е")


def _receipt_name(text: str) -> str:
    cleaned = " ".join(_yo(text).split())
    parts = cleaned.split(" ")
    if len(parts) < 2:
        raise ValueError(
            "Не похоже на ФИО. Например: <code>Денис Викторович П</code> "
            "или <code>Иванов Иван Иванович</code>"
        )
    if len(parts) >= 3 and len(parts[2]) > 1 and parts[2].lower().endswith(
        ("вич", "вна", "ич", "ична")
    ):
        return f"{parts[1]} {parts[2]} {parts[0][0].upper()}"
    return cleaned.rstrip(".")


def _phone_digits(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError("Номер — 11 цифр с 7. Например: <code>79265487319</code>")
    return digits


def _rub(text: str) -> int:
    raw = (text or "").strip().replace(" ", "").replace("\u00a0", "").replace("\u20bd", "")
    raw = raw.replace("₽", "").replace("руб.", "").replace("руб", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", raw or ""):
        raise ValueError("Сумма от 1 до 999 999. Например: <code>14500</code>")
    amount = int(float(raw))
    if amount < 1 or amount > 999999:
        raise ValueError("Сумма от 1 до 999 999. Например: <code>14500</code>")
    return amount


def _four(text: str) -> str:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) != 4:
        raise ValueError("Нужны ровно 4 цифры. Например: <code>4821</code>")
    return digits


def _prepare_java() -> None:
    os.environ.setdefault("JZCOMP_DIR", str(REPO / "banks" / "rshb"))
    jre = REPO / "jre" / "bin" / "java"
    if jre.exists():
        os.environ.setdefault("JAVA_BIN", str(jre))


def _load(folder: str, name: str):
    folder_path = REPO / "banks" / folder
    if str(folder_path) not in sys.path:
        sys.path.insert(0, str(folder_path))
    import importlib
    return importlib.import_module(name)


def _check(mod, text: str, *args) -> None:
    bad = mod.check_text(text, *args) if args else mod.check_text(text)
    if bad:
        chars = "".join(sorted(set(bad)))
        raise ValueError(f"Этих символов в чеке нет: <code>{_esc(chars)}</code>")


_SUMMARY_STATES = {RSHB_SUMMARY, OZ_SUMMARY, TB_SUMMARY}


async def _go_summary_or(update, context, nxt: int):
    if update.callback_query is None:
        await _api["delete_user"](update, context)
    if context.user_data.pop("editing", False) or nxt in _SUMMARY_STATES:
        return await _summary(update, context)
    return await _prompt(update, context, nxt)


async def _summary(update, context) -> int:
    data = context.user_data
    scenario = data.get("scenario")
    if scenario == "rshb":
        state = RSHB_SUMMARY
        body = (
            "<b>Проверь данные — Россельхозбанк</b>\n\n"
            f"👤 Получатель: <b>{_esc(data.get('nb_name'))}</b>\n"
            f"📱 Телефон: <b>{_esc(data.get('nb_phone'))}</b>\n"
            f"🏦 Банк: <b>{_esc(data.get('nb_bank'))}</b>\n"
            f"💰 Сумма: <b>{data.get('nb_amount')} ₽</b>\n"
            f"💳 Счёт: <b>40817…{data.get('nb_account')}</b>"
        )
    elif scenario == "ozon":
        state = OZ_SUMMARY
        sender = data.get("nb_sender") or "как в чеке-доноре"
        body = (
            "<b>Проверь данные — Озон Банк</b>\n\n"
            "⚠️ Новый чек: ID операции фабричный, в базе банка его нет.\n\n"
            f"👤 Получатель: <b>{_esc(data.get('nb_name'))}</b>\n"
            f"📱 Телефон: <b>{_esc(data.get('nb_phone'))}</b>\n"
            f"🏦 Банк: <b>{_esc(data.get('nb_bank'))}</b>\n"
            f"💰 Сумма: <b>{data.get('nb_amount')} ₽</b>\n"
            f"👤 Отправитель: <b>{_esc(sender)}</b>"
        )
    else:
        state = TB_SUMMARY
        when = data.get("nb_dt")
        when_s = when.strftime("%d.%m.%Y %H:%M:%S") if isinstance(when, datetime) else "сейчас"
        account = data.get("nb_account") or "как в доноре"
        body = (
            "<b>Проверь данные — Т-Банк, СБП</b>\n\n"
            f"💰 Сумма: <b>{data.get('nb_amount')} ₽</b>\n"
            f"👤 Отправитель: <b>{_esc(data.get('nb_sender'))}</b>\n"
            f"💳 Счёт: <b>{_esc(account)}</b>\n"
            f"👤 Получатель: <b>{_esc(data.get('nb_name'))}</b>\n"
            f"📱 Телефон: <b>{_esc(data.get('nb_phone'))}</b>\n"
            f"🏦 Банк: <b>{_esc(data.get('nb_bank'))}</b>\n"
            f"🕒 Время: <b>{_esc(when_s)}</b>"
        )
    rows = [[_btn("✅ Сгенерировать", GEN_CB), _btn("✏️ Исправить", EDIT_CB)]]
    return await _ask(update, context, state, body, rows)


async def _prompt(update, context, state: int) -> int:
    if state == RSHB_NAME:
        return await _ask(
            update, context, state,
            "🌾 <b>Россельхозбанк, чек СБП</b>\n\n"
            "👤 <b>Получатель.</b>\n"
            "Нажмите, чтобы скопировать: <code>Денис Викторович П</code>",
            [],
        )
    if state == RSHB_PHONE:
        return await _ask(
            update, context, state,
            "📱 <b>Телефон получателя</b>\n"
            "Нажмите, чтобы скопировать: <code>79265487319</code>",
            [],
        )
    if state == RSHB_BANK:
        return await _ask(update, context, state, "🏦 <b>Банк получателя</b>", _bank_rows("rbank"))
    if state == RSHB_BANK_CUSTOM:
        return await _ask(
            update, context, state,
            "🏦 Название банка, до 40 символов.\nНапример: <code>Т-Банк</code>",
            [],
        )
    if state == RSHB_AMOUNT:
        return await _ask(
            update, context, state,
            "💰 <b>Сумма, целые рубли.</b>\nНапример: <code>14500</code>",
            [],
        )
    if state == RSHB_ACCOUNT:
        choices = [f"{random.randint(0, 9999):04d}" for _ in range(3)]
        return await _ask(
            update, context, state,
            "💳 <b>Последние 4 цифры счёта списания</b> (40817…XXXX).\n"
            "Кнопка или свои 4 цифры.",
            _pairs([(n, f"racc:{n}") for n in choices], 3),
        )
    if state == OZ_MENU:
        return await _ask(
            update, context, state,
            "🧾 <b>Озон Банк</b>\n\n"
            "✅ Для внешней проверки — «Изменить сумму настоящего чека»: "
            "ID операции останется настоящим.\n"
            "⚠️ «Новый чек» — фабричный: этого ID в базе Озона нет.",
            [
                [_btn("✏️ Изменить сумму настоящего чека", OZ_EDIT_CB)],
                [_btn("🧾 Новый чек", OZ_NEW_CB)],
            ],
        )
    if state == OZ_NAME:
        return await _ask(
            update, context, state,
            "🧾 <b>Озон, новый чек</b>\n\n"
            "👤 <b>Получатель.</b>\n"
            "Например: <code>Анна Ивановна К</code>",
            [],
        )
    if state == OZ_PHONE:
        return await _ask(
            update, context, state,
            "📱 <b>Телефон получателя</b>\nНапример: <code>+7 900 351-70-80</code>",
            [],
        )
    if state == OZ_BANK:
        return await _ask(update, context, state, "🏦 <b>Банк получателя</b>", _bank_rows("obank"))
    if state == OZ_BANK_CUSTOM:
        return await _ask(update, context, state, "🏦 Название банка. Например: <code>Сбербанк</code>", [])
    if state == OZ_AMOUNT:
        return await _ask(update, context, state, "💰 <b>Сумма, целые рубли.</b>\nНапример: <code>14500</code>", [])
    if state == OZ_SENDER:
        return await _ask(
            update, context, state,
            "👤 <b>Отправитель.</b> Можно оставить как в чеке-доноре.\n"
            "Или введите ФИО: <code>Иван Иванович И</code>",
            [[_btn("Оставить как в доноре", SKIP_CB)]],
        )
    if state == OZ_EDIT_PDF:
        return await _ask(
            update, context, state,
            "✏️ <b>Правка суммы Озона</b>\n\n"
            "Пришлите PDF настоящего чека Озон Банка. "
            "Поменяется только сумма, ID операции останется.",
            [],
        )
    if state == OZ_EDIT_AMOUNT:
        return await _ask(update, context, state, "💰 <b>Новая сумма, целые рубли.</b>\nНапример: <code>14500</code>", [])
    if state == TB_MENU:
        return await _ask(
            update, context, state,
            "💛 <b>Т-Банк</b>\n\n"
            "Новый чек СБП собирается по донору из новой папки.\n"
            "«Изменить сумму» — правка загруженного PDF, остальные поля не трогаем.",
            [
                [_btn("💸 Новый чек СБП", TB_NEW_CB)],
                [_btn("✏️ Изменить сумму настоящего чека", TB_EDIT_CB)],
            ],
        )
    if state == TB_AMOUNT:
        return await _ask(update, context, state, "💰 <b>Сумма, целые рубли.</b>\nНапример: <code>14500</code>", [])
    if state == TB_SENDER:
        return await _ask(
            update, context, state,
            "👤 <b>Отправитель.</b>\nНапример: <code>Иван Иванович И.</code>",
            [],
        )
    if state == TB_ACCOUNT:
        choices = [f"{random.randint(0, 9999):04d}" for _ in range(3)]
        return await _ask(
            update, context, state,
            "💳 <b>Последние 4 цифры счёта списания</b> или «Авто».",
            _pairs([(n, f"tacc:{n}") for n in choices], 3) + [[_btn("Авто", "tacc:auto")]],
        )
    if state == TB_RECEIVER:
        return await _ask(
            update, context, state,
            "👤 <b>Получатель.</b>\nНапример: <code>Анна Ивановна И.</code>",
            [],
        )
    if state == TB_PHONE:
        return await _ask(
            update, context, state,
            "📱 <b>Телефон получателя</b>\nНапример: <code>+7 900 000-00-00</code>",
            [],
        )
    if state == TB_BANK:
        return await _ask(update, context, state, "🏦 <b>Банк получателя</b>", _bank_rows("tbank"))
    if state == TB_BANK_CUSTOM:
        return await _ask(update, context, state, "🏦 Название банка. Например: <code>Сбербанк</code>", [])
    if state == TB_TIME:
        return await _ask(
            update, context, state,
            "🕒 <b>Время операции</b> (МСК).",
            [[_btn("Сейчас", NOW_CB)]],
        )
    if state == TB_TIME_INPUT:
        return await _ask(
            update, context, state,
            "🕒 Дата и время. Например: <code>11.11.2026 21:15</code>",
            [],
        )
    if state == TB_EDIT_PDF:
        return await _ask(
            update, context, state,
            "✏️ <b>Правка суммы Т-Банка</b>\n\nПришлите PDF чека Т-Банка.",
            [],
        )
    if state == TB_EDIT_AMOUNT:
        return await _ask(update, context, state, "💰 <b>Новая сумма, целые рубли.</b>\nНапример: <code>14500</code>", [])
    raise RuntimeError(f"unknown extra-bank step {state}")


def _first_step(scenario: str) -> int:
    return {
        "rshb": RSHB_NAME,
        "ozon": OZ_NAME,
        "tbank": TB_AMOUNT,
    }[scenario]


async def on_back(update, context) -> int:
    data = context.user_data
    if data.pop("editing", False) and data.get("scenario") in ("rshb", "ozon", "tbank"):
        return await _summary(update, context)
    current = data.get("current_step")
    prev = _PREV.get(current, "main")
    if prev == "main":
        return await _api["main"](update, context)
    return await _prompt(update, context, prev)


async def _answer(update) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer()


async def on_rshb_start(update, context) -> int:
    await _answer(update)
    _api["clear"](context)
    context.user_data["scenario"] = "rshb"
    return await _prompt(update, context, RSHB_NAME)


async def on_ozon_start(update, context) -> int:
    await _answer(update)
    _api["clear"](context)
    context.user_data["scenario"] = "ozon_menu"
    return await _prompt(update, context, OZ_MENU)


async def on_tbank_start(update, context) -> int:
    await _answer(update)
    _api["clear"](context)
    context.user_data["scenario"] = "tbank_menu"
    return await _prompt(update, context, TB_MENU)


async def on_oz_new(update, context) -> int:
    await _answer(update)
    context.user_data["scenario"] = "ozon"
    return await _prompt(update, context, OZ_NAME)


async def on_oz_edit(update, context) -> int:
    await _answer(update)
    context.user_data["scenario"] = "ozon_edit"
    return await _prompt(update, context, OZ_EDIT_PDF)


async def on_tb_new(update, context) -> int:
    await _answer(update)
    context.user_data["scenario"] = "tbank"
    return await _prompt(update, context, TB_AMOUNT)


async def on_tb_edit(update, context) -> int:
    await _answer(update)
    context.user_data["scenario"] = "tbank_edit"
    return await _prompt(update, context, TB_EDIT_PDF)


async def on_rshb_name(update, context) -> int:
    try:
        name = _receipt_name(update.effective_message.text)
        _check(_load("rshb", "rshb_generator"), name, "Regular")
    except ValueError as error:
        return await _fail(update, context, RSHB_NAME, f"❌ {error}", [])
    context.user_data["nb_name"] = name
    return await _go_summary_or(update, context, RSHB_PHONE)


async def on_rshb_phone(update, context) -> int:
    try:
        phone = _phone_digits(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, RSHB_PHONE, f"❌ {error}", [])
    context.user_data["nb_phone"] = phone
    return await _go_summary_or(update, context, RSHB_BANK)


async def on_rshb_bank(update, context) -> int:
    await _answer(update)
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _prompt(update, context, RSHB_BANK_CUSTOM)
    context.user_data["nb_bank"] = _BANKS[int(slug)]
    return await _go_summary_or(update, context, RSHB_AMOUNT)


async def on_rshb_bank_custom(update, context) -> int:
    bank = " ".join(_yo(update.effective_message.text or "").split())
    if not bank or len(bank) > 40:
        return await _fail(update, context, RSHB_BANK_CUSTOM, "❌ Банк — до 40 символов.", [])
    try:
        _check(_load("rshb", "rshb_generator"), bank, "Regular")
    except ValueError as error:
        return await _fail(update, context, RSHB_BANK_CUSTOM, f"❌ {error}", [])
    context.user_data["nb_bank"] = bank
    return await _go_summary_or(update, context, RSHB_AMOUNT)


async def on_rshb_amount(update, context) -> int:
    try:
        context.user_data["nb_amount"] = _rub(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, RSHB_AMOUNT, f"❌ {error}", [])
    return await _go_summary_or(update, context, RSHB_ACCOUNT)


async def on_rshb_account_btn(update, context) -> int:
    await _answer(update)
    context.user_data["nb_account"] = update.callback_query.data.split(":", 1)[1]
    return await _go_summary_or(update, context, RSHB_SUMMARY)


async def on_rshb_account_text(update, context) -> int:
    try:
        context.user_data["nb_account"] = _four(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, RSHB_ACCOUNT, f"❌ {error}", [])
    return await _go_summary_or(update, context, RSHB_SUMMARY)


async def on_oz_name(update, context) -> int:
    try:
        name = _receipt_name(update.effective_message.text)
        _check(_load("ozon", "ozon_generator"), name)
    except ValueError as error:
        return await _fail(update, context, OZ_NAME, f"❌ {error}", [])
    context.user_data["nb_name"] = name
    return await _go_summary_or(update, context, OZ_PHONE)


async def on_oz_phone(update, context) -> int:
    try:
        context.user_data["nb_phone"] = _phone_digits(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, OZ_PHONE, f"❌ {error}", [])
    return await _go_summary_or(update, context, OZ_BANK)


async def on_oz_bank(update, context) -> int:
    await _answer(update)
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _prompt(update, context, OZ_BANK_CUSTOM)
    context.user_data["nb_bank"] = _BANKS[int(slug)]
    return await _go_summary_or(update, context, OZ_AMOUNT)


async def on_oz_bank_custom(update, context) -> int:
    bank = " ".join(_yo(update.effective_message.text or "").split())
    if not bank or len(bank) > 40:
        return await _fail(update, context, OZ_BANK_CUSTOM, "❌ Банк — до 40 символов.", [])
    try:
        _check(_load("ozon", "ozon_generator"), bank)
    except ValueError as error:
        return await _fail(update, context, OZ_BANK_CUSTOM, f"❌ {error}", [])
    context.user_data["nb_bank"] = bank
    return await _go_summary_or(update, context, OZ_AMOUNT)


async def on_oz_amount(update, context) -> int:
    try:
        context.user_data["nb_amount"] = _rub(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, OZ_AMOUNT, f"❌ {error}", [])
    return await _go_summary_or(update, context, OZ_SENDER)


async def on_oz_sender_skip(update, context) -> int:
    await _answer(update)
    context.user_data["nb_sender"] = ""
    return await _go_summary_or(update, context, OZ_SUMMARY)


async def on_oz_sender(update, context) -> int:
    try:
        name = _receipt_name(update.effective_message.text)
        _check(_load("ozon", "ozon_generator"), name)
    except ValueError as error:
        return await _fail(update, context, OZ_SENDER, f"❌ {error}", [])
    context.user_data["nb_sender"] = name
    return await _go_summary_or(update, context, OZ_SUMMARY)


async def on_tb_amount(update, context) -> int:
    try:
        context.user_data["nb_amount"] = _rub(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, TB_AMOUNT, f"❌ {error}", [])
    return await _go_summary_or(update, context, TB_SENDER)


async def on_tb_sender(update, context) -> int:
    name = " ".join((update.effective_message.text or "").split())
    if len(name) < 3:
        return await _fail(update, context, TB_SENDER, "❌ Введите ФИО отправителя.", [])
    context.user_data["nb_sender"] = name if name.endswith(".") else name
    return await _go_summary_or(update, context, TB_ACCOUNT)


async def on_tb_account_btn(update, context) -> int:
    await _answer(update)
    data = update.callback_query.data
    context.user_data["nb_account"] = "" if data.endswith(":auto") else data.split(":", 1)[1]
    return await _go_summary_or(update, context, TB_RECEIVER)


async def on_tb_account_text(update, context) -> int:
    raw = (update.effective_message.text or "").strip().lower()
    if raw in ("авто", "auto", "-"):
        context.user_data["nb_account"] = ""
    else:
        try:
            context.user_data["nb_account"] = _four(raw)
        except ValueError as error:
            return await _fail(update, context, TB_ACCOUNT, f"❌ {error}", [])
    return await _go_summary_or(update, context, TB_RECEIVER)


async def on_tb_receiver(update, context) -> int:
    name = " ".join((update.effective_message.text or "").split())
    if len(name) < 3:
        return await _fail(update, context, TB_RECEIVER, "❌ Введите ФИО получателя.", [])
    context.user_data["nb_name"] = name
    return await _go_summary_or(update, context, TB_PHONE)


async def on_tb_phone(update, context) -> int:
    try:
        digits = _phone_digits(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, TB_PHONE, f"❌ {error}", [])
    context.user_data["nb_phone"] = f"8 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return await _go_summary_or(update, context, TB_BANK)


async def on_tb_bank(update, context) -> int:
    await _answer(update)
    slug = update.callback_query.data.split(":", 1)[1]
    if slug == "custom":
        return await _prompt(update, context, TB_BANK_CUSTOM)
    context.user_data["nb_bank"] = _BANKS[int(slug)]
    return await _go_summary_or(update, context, TB_TIME)


async def on_tb_bank_custom(update, context) -> int:
    bank = " ".join((update.effective_message.text or "").split())
    if not bank or len(bank) > 40:
        return await _fail(update, context, TB_BANK_CUSTOM, "❌ Банк — до 40 символов.", [])
    context.user_data["nb_bank"] = bank
    return await _go_summary_or(update, context, TB_TIME)


async def on_tb_now(update, context) -> int:
    await _answer(update)
    context.user_data["nb_dt"] = datetime.now()
    return await _go_summary_or(update, context, TB_SUMMARY)


async def on_tb_time_text(update, context) -> int:
    text = " ".join((update.effective_message.text or "").split())
    parsed = None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return await _fail(
            update, context, TB_TIME_INPUT,
            "❌ Дата как <code>11.11.2026 21:15</code>",
            [],
        )
    context.user_data["nb_dt"] = parsed
    return await _go_summary_or(update, context, TB_SUMMARY)


async def _save_pdf(update, context, key_path: str, key_dir: str) -> str | None:
    message = update.effective_message
    document = getattr(message, "document", None) if message else None
    name = (getattr(document, "file_name", None) or "")
    if document is None or not name.lower().endswith(".pdf"):
        return None
    import tempfile
    directory = Path(tempfile.mkdtemp(prefix="checketop_nb_"))
    local = directory / "donor.pdf"
    telegram_file = await update.get_bot().get_file(document.file_id)
    await telegram_file.download_to_drive(custom_path=str(local))
    context.user_data[key_path] = str(local)
    context.user_data[key_dir] = str(directory)
    return str(local)


def _cleanup_pdf(data: dict) -> None:
    path = data.get("nb_pdf")
    directory = data.get("nb_pdf_dir")
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    if directory:
        try:
            Path(directory).rmdir()
        except OSError:
            pass


async def on_oz_pdf(update, context) -> int:
    try:
        saved = await _save_pdf(update, context, "nb_pdf", "nb_pdf_dir")
    except Exception:
        logger.exception("ozon pdf download failed")
        saved = None
    if not saved:
        return await _fail(update, context, OZ_EDIT_PDF, "❌ Нужен PDF-файл чека Озона.", [])
    await _api["delete_user"](update, context)
    return await _prompt(update, context, OZ_EDIT_AMOUNT)


async def on_tb_pdf(update, context) -> int:
    try:
        saved = await _save_pdf(update, context, "nb_pdf", "nb_pdf_dir")
    except Exception:
        logger.exception("tbank pdf download failed")
        saved = None
    if not saved:
        return await _fail(update, context, TB_EDIT_PDF, "❌ Нужен PDF-файл чека Т-Банка.", [])
    await _api["delete_user"](update, context)
    return await _prompt(update, context, TB_EDIT_AMOUNT)


async def on_oz_edit_amount(update, context) -> int:
    try:
        amount = _rub(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, OZ_EDIT_AMOUNT, f"❌ {error}", [])
    context.user_data["nb_amount"] = amount
    return await _finish_edit(update, context, "ozon")


async def on_tb_edit_amount(update, context) -> int:
    try:
        amount = _rub(update.effective_message.text)
    except ValueError as error:
        return await _fail(update, context, TB_EDIT_AMOUNT, f"❌ {error}", [])
    context.user_data["nb_amount"] = amount
    return await _finish_edit(update, context, "tbank")


async def on_edit_restart(update, context) -> int:
    await _answer(update)
    context.user_data["editing"] = False
    scenario = context.user_data.get("scenario")
    return await _prompt(update, context, _first_step(scenario))


async def on_generate(update, context) -> int:
    await _answer(update)
    scenario = context.user_data.get("scenario")
    if scenario == "rshb":
        return await _finish_rshb(update, context)
    if scenario == "ozon":
        return await _finish_ozon(update, context)
    if scenario == "tbank":
        return await _finish_tbank(update, context)
    return await _summary(update, context)


def _gen_rshb(data: dict) -> bytes:
    _prepare_java()
    mod = _load("rshb", "rshb_generator")
    return mod.generate(
        data["nb_name"], data["nb_phone"], data["nb_bank"], data["nb_amount"],
        account=data.get("nb_account"),
    )


def _gen_ozon(data: dict) -> bytes:
    mod = _load("ozon", "ozon_generator")
    sender = data.get("nb_sender") or None
    return mod.generate(
        data["nb_name"], data["nb_phone"], data["nb_bank"], data["nb_amount"],
        sender=sender,
    )


def _gen_tbank(data: dict) -> bytes:
    lib = REPO / "banks" / "tbank"
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    from gen_tbank_receipt import generate_tbank_receipt

    when = data.get("nb_dt") or datetime.now()
    tail = data.get("nb_account") or ""
    account = f"40817810****{tail}" if tail else "408178100000****0000"
    donor = lib / "TBANK" / "tbank_sbp_verified_05052026_enriched.pdf"
    pdf, _name = generate_tbank_receipt(
        amount=int(data["nb_amount"]),
        sender_name=data.get("nb_sender") or "Иван Иванович И.",
        sender_account=account,
        recipient_name=data.get("nb_name") or "",
        recipient_phone=data.get("nb_phone") or "",
        recipient_bank=data.get("nb_bank") or "Сбербанк",
        operation_date=when.strftime("%d.%m.%Y"),
        operation_time=when.strftime("%H:%M:%S"),
        donor_path=donor,
        preserve_integrity=True,
    )
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("Т-Банк: собранный файл не похож на PDF")
    return pdf


def _edit_ozon(path: str, amount: int) -> bytes:
    _prepare_java()
    editor = _load("ozon", "ozon_editor")
    return editor.edit_amount(Path(path).read_bytes(), amount)


def _edit_tbank(path: str, amount: int) -> bytes:
    lib = REPO / "banks" / "tbank"
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    from tbank_check_service import patch_amount_only
    return patch_amount_only(path, amount, receipt_type="sbp")


async def _send_pdf(update, context, pdf: bytes, filename: str, caption: str, again: str) -> int:
    from telegram.ext import ConversationHandler

    await _api["drop_prompt"](update, context)
    bio = BytesIO(pdf)
    bio.name = filename
    markup = InlineKeyboardMarkup([
        [_btn("🔁 Ещё чек", again)],
        [_btn("🏠 Главное меню", _api["main_cb"])],
    ])
    await update.get_bot().send_document(
        chat_id=update.effective_chat.id,
        document=bio,
        filename=filename,
        caption=caption,
        reply_markup=markup,
    )
    _cleanup_pdf(context.user_data)
    _api["clear"](context)
    return ConversationHandler.END


async def _busy(update, context, state: int, producer, filename: str, caption: str, again: str) -> int:
    await _api["send"](update, context, "⏳ Собираю PDF…", reply_markup=None)
    try:
        pdf = await asyncio.get_running_loop().run_in_executor(None, producer)
    except Exception as error:
        logger.exception("extra bank generation failed")
        text = " ".join(str(error).split()) or error.__class__.__name__
        if len(text) > 300:
            text = text[:297] + "…"
        return await _fail(update, context, state, f"❌ Не получилось: <code>{_esc(text)}</code>", [])
    return await _send_pdf(update, context, pdf, filename, caption, again)


async def _finish_rshb(update, context) -> int:
    data = dict(context.user_data)
    stamp = datetime.now().strftime("%d-%m-%y_%H-%M")
    return await _busy(
        update, context, RSHB_SUMMARY,
        lambda: _gen_rshb(data),
        f"rshb_{stamp}.pdf",
        f"Россельхозбанк • {data.get('nb_amount')} ₽ • {data.get('nb_bank')}",
        RSHB_CB,
    )


async def _finish_ozon(update, context) -> int:
    data = dict(context.user_data)
    stamp = datetime.now().strftime("%d-%m-%y_%H-%M")
    return await _busy(
        update, context, OZ_SUMMARY,
        lambda: _gen_ozon(data),
        f"ozon_{stamp}.pdf",
        f"Озон Банк • {data.get('nb_amount')} ₽ • {data.get('nb_bank')}",
        OZON_CB,
    )


async def _finish_tbank(update, context) -> int:
    data = dict(context.user_data)
    stamp = datetime.now().strftime("%d-%m-%y_%H-%M")
    return await _busy(
        update, context, TB_SUMMARY,
        lambda: _gen_tbank(data),
        f"tbank_{stamp}.pdf",
        f"Т-Банк • {data.get('nb_amount')} ₽ • {data.get('nb_bank')}",
        TBANK_CB,
    )


async def _finish_edit(update, context, kind: str) -> int:
    data = dict(context.user_data)
    path = data.get("nb_pdf")
    amount = data.get("nb_amount")
    stamp = datetime.now().strftime("%d-%m-%y_%H-%M")
    if kind == "ozon":
        return await _busy(
            update, context, OZ_EDIT_AMOUNT,
            lambda: _edit_ozon(path, amount),
            f"ozon_edit_{stamp}.pdf",
            f"Озон Банк • сумма {amount} ₽ • ID операции прежний",
            OZON_CB,
        )
    return await _busy(
        update, context, TB_EDIT_AMOUNT,
        lambda: _edit_tbank(path, amount),
        f"tbank_edit_{stamp}.pdf",
        f"Т-Банк • сумма {amount} ₽",
        TBANK_CB,
    )


def menu_handlers():
    return [
        CallbackQueryHandler(on_rshb_start, pattern=f"^{RSHB_CB}$"),
        CallbackQueryHandler(on_ozon_start, pattern=f"^{OZON_CB}$"),
        CallbackQueryHandler(on_tbank_start, pattern=f"^{TBANK_CB}$"),
    ]


def entry_points():
    return menu_handlers() + [
        CallbackQueryHandler(on_oz_new, pattern=f"^{OZ_NEW_CB}$"),
        CallbackQueryHandler(on_oz_edit, pattern=f"^{OZ_EDIT_CB}$"),
        CallbackQueryHandler(on_tb_new, pattern=f"^{TB_NEW_CB}$"),
        CallbackQueryHandler(on_tb_edit, pattern=f"^{TB_EDIT_CB}$"),
    ]


def conversation_states(back):
    doc = filters.Document.ALL
    return {
        RSHB_NAME: [MessageHandler(_TEXT, on_rshb_name), back],
        RSHB_PHONE: [MessageHandler(_TEXT, on_rshb_phone), back],
        RSHB_BANK: [CallbackQueryHandler(on_rshb_bank, pattern=r"^rbank:"), back],
        RSHB_BANK_CUSTOM: [MessageHandler(_TEXT, on_rshb_bank_custom), back],
        RSHB_AMOUNT: [MessageHandler(_TEXT, on_rshb_amount), back],
        RSHB_ACCOUNT: [
            CallbackQueryHandler(on_rshb_account_btn, pattern=r"^racc:"),
            MessageHandler(_TEXT, on_rshb_account_text),
            back,
        ],
        RSHB_SUMMARY: [
            CallbackQueryHandler(on_generate, pattern=f"^{GEN_CB}$"),
            CallbackQueryHandler(on_edit_restart, pattern=f"^{EDIT_CB}$"),
            back,
        ],
        OZ_MENU: [
            CallbackQueryHandler(on_oz_new, pattern=f"^{OZ_NEW_CB}$"),
            CallbackQueryHandler(on_oz_edit, pattern=f"^{OZ_EDIT_CB}$"),
            back,
        ],
        OZ_NAME: [MessageHandler(_TEXT, on_oz_name), back],
        OZ_PHONE: [MessageHandler(_TEXT, on_oz_phone), back],
        OZ_BANK: [CallbackQueryHandler(on_oz_bank, pattern=r"^obank:"), back],
        OZ_BANK_CUSTOM: [MessageHandler(_TEXT, on_oz_bank_custom), back],
        OZ_AMOUNT: [MessageHandler(_TEXT, on_oz_amount), back],
        OZ_SENDER: [
            CallbackQueryHandler(on_oz_sender_skip, pattern=f"^{SKIP_CB}$"),
            MessageHandler(_TEXT, on_oz_sender),
            back,
        ],
        OZ_SUMMARY: [
            CallbackQueryHandler(on_generate, pattern=f"^{GEN_CB}$"),
            CallbackQueryHandler(on_edit_restart, pattern=f"^{EDIT_CB}$"),
            back,
        ],
        OZ_EDIT_PDF: [MessageHandler(doc, on_oz_pdf), MessageHandler(_TEXT, on_oz_pdf), back],
        OZ_EDIT_AMOUNT: [MessageHandler(_TEXT, on_oz_edit_amount), back],
        TB_MENU: [
            CallbackQueryHandler(on_tb_new, pattern=f"^{TB_NEW_CB}$"),
            CallbackQueryHandler(on_tb_edit, pattern=f"^{TB_EDIT_CB}$"),
            back,
        ],
        TB_AMOUNT: [MessageHandler(_TEXT, on_tb_amount), back],
        TB_SENDER: [MessageHandler(_TEXT, on_tb_sender), back],
        TB_ACCOUNT: [
            CallbackQueryHandler(on_tb_account_btn, pattern=r"^tacc:"),
            MessageHandler(_TEXT, on_tb_account_text),
            back,
        ],
        TB_RECEIVER: [MessageHandler(_TEXT, on_tb_receiver), back],
        TB_PHONE: [MessageHandler(_TEXT, on_tb_phone), back],
        TB_BANK: [CallbackQueryHandler(on_tb_bank, pattern=r"^tbank:"), back],
        TB_BANK_CUSTOM: [MessageHandler(_TEXT, on_tb_bank_custom), back],
        TB_TIME: [
            CallbackQueryHandler(on_tb_now, pattern=f"^{NOW_CB}$"),
            MessageHandler(_TEXT, on_tb_time_text),
            back,
        ],
        TB_TIME_INPUT: [MessageHandler(_TEXT, on_tb_time_text), back],
        TB_SUMMARY: [
            CallbackQueryHandler(on_generate, pattern=f"^{GEN_CB}$"),
            CallbackQueryHandler(on_edit_restart, pattern=f"^{EDIT_CB}$"),
            back,
        ],
        TB_EDIT_PDF: [MessageHandler(doc, on_tb_pdf), MessageHandler(_TEXT, on_tb_pdf), back],
        TB_EDIT_AMOUNT: [MessageHandler(_TEXT, on_tb_edit_amount), back],
    }
