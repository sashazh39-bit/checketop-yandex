# -*- coding: utf-8 -*-
"""Две админ-кнопки из бота с Яндекс Диска: пользователи ЛК и создание доступа."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger("admin_access")

ADMIN_ID = 1445265832
USERS_CB = "adm:users"
NEW_CB = "adm:new"
DURATIONS = (
    ("5h", "⏰ 5 часов"),
    ("1d", "📅 1 день"),
    ("3d", "📅 3 дня"),
    ("7d", "📅 7 дней"),
    ("30d", "📅 30 дней"),
)
DUR_TEXT = dict(DURATIONS)

ADM_LOGIN, ADM_PASS, ADM_DUR = range(260, 263)
_TEXT = filters.TEXT & ~filters.COMMAND
_api: dict = {}


def bind(**funcs) -> None:
    _api.update(funcs)


def is_admin(uid) -> bool:
    return uid == ADMIN_ID


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows) + [[_btn("🏠 Главное меню", _api["main_cb"])]])


def api_call(method: str, path: str, body=None):
    base = os.environ.get("API_BASE", "http://83.141.93.93:3012/api").rstrip("/")
    token = os.environ.get("INTERNAL_TOKEN", "").strip()
    if not token:
        return None, {"error": "На сервере бота не задан INTERNAL_TOKEN"}
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=payload, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode()[:300]
        try:
            return error.code, json.loads(raw)
        except Exception:
            return error.code, {"error": raw or error.reason}
    except Exception as error:
        return None, {"error": f"{type(error).__name__}: {error}"}


def _status(user: dict) -> str:
    if user.get("banned"):
        return "🚫 заблокирован"
    return "активен"


async def _answer(update) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer()


async def on_users(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        return ConversationHandler.END
    status, resp = api_call("GET", "/users/list")
    if status != 200:
        text = "❌ " + str((resp or {}).get("error") or "API недоступен")
        await _api["send"](update, context, text, reply_markup=_kb())
        return ConversationHandler.END
    users = (resp or {}).get("data") or []
    if not users:
        await _api["send"](
            update, context,
            "👥 <b>Пользователей ЛК нет.</b>",
            reply_markup=_kb([_btn("➕ Создать доступ", NEW_CB)]),
        )
        return ConversationHandler.END
    lines = []
    rows = []
    for user in users[:12]:
        lines.append(
            f"• <b>{user.get('login')}</b> [{user.get('label')}] — {_status(user)}"
        )
        label = str(user.get("label") or "")
        if user.get("banned"):
            rows.append([_btn(f"✅ Разбан {label}", f"adm:ub:{label}")])
        else:
            rows.append([_btn(f"🚫 Бан {label}", f"adm:ban:{label}")])
    await _api["send"](
        update, context,
        "👥 <b>Пользователи ЛК</b>\n\n" + "\n".join(lines),
        reply_markup=_kb(*rows, [_btn("➕ Создать доступ", NEW_CB)]),
    )
    return ConversationHandler.END


async def on_ban(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        return ConversationHandler.END
    label = update.callback_query.data.split(":", 2)[2]
    action = "unban" if update.callback_query.data.startswith("adm:ub:") else "ban"
    status, resp = api_call("POST", f"/users/{action}/{label}")
    if status == 200:
        text = f"{'✅ Разблокирован' if action == 'unban' else '🚫 Заблокирован'}: <b>{label}</b>"
    else:
        text = "❌ " + str((resp or {}).get("error") or "ошибка")
    await _api["send"](update, context, text, reply_markup=_kb([_btn("👥 Пользователи", USERS_CB)]))
    return ConversationHandler.END


async def on_new(update, context) -> int:
    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        from telegram.ext import ConversationHandler
        return ConversationHandler.END
    context.user_data["scenario"] = "admin_access"
    context.user_data["current_step"] = ADM_LOGIN
    await _api["send"](
        update, context,
        "👤 <b>Создание доступа в ЛК</b>\n\n"
        "Пришли <b>логин</b> (латиница и цифры, 3–32 символа).",
        reply_markup=InlineKeyboardMarkup([[_btn("❌ Отмена", "cancel")]]),
    )
    return ADM_LOGIN


async def on_login(update, context) -> int:
    login = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", login):
        await _api["send"](
            update, context,
            "❌ Логин: 3–32 символа, латиница, цифры, _ . -\nПришли ещё раз.",
            reply_markup=InlineKeyboardMarkup([[_btn("❌ Отмена", "cancel")]]),
        )
        return ADM_LOGIN
    await _api["delete_user"](update, context)
    context.user_data["new_login"] = login
    context.user_data["current_step"] = ADM_PASS
    await _api["send"](
        update, context,
        f"👤 Логин: <b>{login}</b>\n\n🔑 Пришли <b>пароль</b> (4–64 символа).",
        reply_markup=InlineKeyboardMarkup([[_btn("❌ Отмена", "cancel")]]),
    )
    return ADM_PASS


async def on_password(update, context) -> int:
    password = (update.effective_message.text or "").strip()
    if not 4 <= len(password) <= 64:
        await _api["send"](
            update, context,
            "❌ Пароль: 4–64 символа. Пришли ещё раз.",
            reply_markup=InlineKeyboardMarkup([[_btn("❌ Отмена", "cancel")]]),
        )
        return ADM_PASS
    await _api["delete_user"](update, context)
    context.user_data["new_pass"] = password
    context.user_data["current_step"] = ADM_DUR
    rows = [
        [_btn(DUR_TEXT["5h"], "adm:c:5h"), _btn(DUR_TEXT["1d"], "adm:c:1d")],
        [_btn(DUR_TEXT["3d"], "adm:c:3d"), _btn(DUR_TEXT["7d"], "adm:c:7d")],
        [_btn(DUR_TEXT["30d"], "adm:c:30d")],
    ]
    await _api["send"](
        update, context,
        "🔑 Пароль задан.\n\n⏰ <b>Срок доступа:</b>",
        reply_markup=InlineKeyboardMarkup(rows + [[_btn("❌ Отмена", "cancel")]]),
    )
    return ADM_DUR


async def on_create(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        return ConversationHandler.END
    duration = update.callback_query.data.split(":")[-1]
    status, resp = api_call("POST", "/users/create", {
        "login": context.user_data.get("new_login", ""),
        "password": context.user_data.get("new_pass", ""),
        "duration": duration,
    })
    if status == 200:
        label = resp.get("label")
        text = (
            "✅ <b>Пользователь создан</b>\n\n"
            f"👤 Логин: <code>{resp.get('login')}</code>\n"
            f"🔑 Пароль: <code>{resp.get('password')}</code>\n"
            f"📋 ID: <b>{label}</b>\n"
            f"⏰ Доступ: {DUR_TEXT.get(duration, duration)}\n\n"
            "Вход в админку — вкладка «Логин и пароль».\n"
            f"Его ЛК: <code>https://online.mtsdengi.ru/?lk={label}</code>"
        )
    else:
        text = "❌ " + str((resp or {}).get("error") or "ошибка")
    context.user_data.pop("new_login", None)
    context.user_data.pop("new_pass", None)
    await _api["send"](update, context, text, reply_markup=_kb([_btn("👥 Пользователи", USERS_CB)]))
    return ConversationHandler.END


def menu_handlers():
    return [
        CallbackQueryHandler(on_users, pattern=f"^{USERS_CB}$"),
        CallbackQueryHandler(on_new, pattern=f"^{NEW_CB}$"),
        CallbackQueryHandler(on_ban, pattern=r"^adm:(ban|ub):"),
    ]


def conversation_states(back):
    return {
        ADM_LOGIN: [MessageHandler(_TEXT, on_login), back],
        ADM_PASS: [MessageHandler(_TEXT, on_password), back],
        ADM_DUR: [CallbackQueryHandler(on_create, pattern=r"^adm:c:"), back],
    }
