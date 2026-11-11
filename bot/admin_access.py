# -*- coding: utf-8 -*-
"""Две админ-кнопки из бота с Яндекс Диска: пользователи ЛК и создание доступа."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

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

ADM_LOGIN, ADM_PASS, ADM_DUR, ADM_GRANT, ADM_REVOKE = range(260, 265)
OTHER_CB = "menu_other"
ADMIN_CB = "admin"
_TEXT = filters.TEXT & ~filters.COMMAND
_api: dict = {}


def bind(**funcs) -> None:
    _api.update(funcs)


_STORE = Path(__file__).resolve().parents[1] / "bot_access.json"


def _load_store() -> dict:
    if _STORE.is_file():
        try:
            data = json.loads(_STORE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("users", [ADMIN_ID])
                data.setdefault("logs", [])
                data.setdefault("bindings", {})
                return data
        except Exception:
            logger.exception("bot_access.json")
    return {"users": [ADMIN_ID], "logs": [], "bindings": {}}


def _save_store(data: dict) -> None:
    _STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def has_access(user_id) -> bool:
    if user_id is None:
        return False
    return int(user_id) in set(_load_store().get("users") or [])


def _back_admin():
    return [_btn("⬅️ Управление", ADMIN_CB)]


def _back_other():
    return [_btn("⬅️ Другое", OTHER_CB)]


async def _show(update, context, text: str, rows: list):
    await _api["send"](update, context, text, reply_markup=InlineKeyboardMarkup(rows))


async def on_other(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    rows = [
        [_btn("🔍 Проверить чек", "oth:check"), _btn("📜 История", "oth:hist")],
        [_btn("📌 Шаблоны", "oth:tpl"), _btn("❓ Помощь", "oth:help")],
        [_btn("⚡ Быстрая генерация", "oth:fast"), _btn("📦 Пакет чеков", "oth:batch")],
        [_btn("📊 Статистика", "oth:stats"), _btn("🔑 API-токен", "oth:token")],
        [_btn("🌙 Тёмная тема", "oth:theme"), _btn("🔔 Уведомления", "oth:notif")],
        [_btn("⬅️ Главное меню", _api["main_cb"])],
    ]
    await _show(
        update, context,
        "📂 <b>Другое</b>\n\nИнструменты и удобства:",
        rows,
    )
    return ConversationHandler.END


async def on_admin(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        return ConversationHandler.END
    store = _load_store()
    text = (
        "🛠️ <b>Админ-панель</b>\n\n"
        f"👥 Пользователей бота: {len(store.get('users') or [])}\n"
        f"📄 Сгенерировано: {len(store.get('logs') or [])}"
    )
    rows = [
        [_btn("📋 Пользователи", "a_users")],
        [_btn("➕ Выдать доступ", "a_grant"), _btn("➖ Забрать", "a_revoke")],
        [_btn("📊 Логи", "a_logs"), _btn("📈 Статистика", "a_stats")],
        [_btn("👥 Пользователи", USERS_CB), _btn("➕ Создать доступ", NEW_CB)],
        [_btn("🔧 Фичи юзеров", "users_list")],
        [_btn("🔗 Привязки ЛК", "a_bind")],
        [_btn("⚓ Якоря номеров", "a_anchors")],
        [_btn("⬅️ Главная", _api["main_cb"])],
    ]
    await _show(update, context, text, rows)
    return ConversationHandler.END


async def on_bot_users(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        return ConversationHandler.END
    lines = []
    for uid in _load_store().get("users") or []:
        mark = "👑" if uid == ADMIN_ID else "👤"
        lines.append(f"{mark} <code>{uid}</code>")
    await _show(
        update, context,
        "👥 <b>Пользователи бота</b>\n\n" + ("\n".join(lines) or "пусто"),
        [_back_admin()],
    )
    return ConversationHandler.END


async def on_grant_start(update, context) -> int:
    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        from telegram.ext import ConversationHandler
        return ConversationHandler.END
    context.user_data["scenario"] = "admin_access"
    context.user_data["current_step"] = ADM_GRANT
    await _show(
        update, context,
        "➕ <b>Выдать доступ</b>\n\nВведи Telegram ID:",
        [_back_admin()],
    )
    return ADM_GRANT


async def on_revoke_start(update, context) -> int:
    await _answer(update)
    if not is_admin(getattr(update.effective_user, "id", None)):
        from telegram.ext import ConversationHandler
        return ConversationHandler.END
    context.user_data["scenario"] = "admin_access"
    context.user_data["current_step"] = ADM_REVOKE
    others = [str(u) for u in _load_store().get("users") or [] if u != ADMIN_ID]
    extra = ("\n".join(f"👤 <code>{u}</code>" for u in others) + "\n\n") if others else ""
    await _show(update, context, "➖ <b>Забрать доступ</b>\n\n" + extra + "Введи ID:", [_back_admin()])
    return ADM_REVOKE


async def on_grant_id(update, context) -> int:
    from telegram.ext import ConversationHandler

    raw = re.sub(r"\D", "", update.effective_message.text or "")
    if not raw:
        await _show(update, context, "❌ Введи числовой ID", [_back_admin()])
        return ADM_GRANT
    uid = int(raw)
    store = _load_store()
    users = list(store.get("users") or [])
    if uid not in users:
        users.append(uid)
    store["users"] = users
    _save_store(store)
    await _api["delete_user"](update, context)
    await _show(update, context, f"✅ Доступ выдан: <code>{uid}</code>", [_back_admin()])
    return ConversationHandler.END


async def on_revoke_id(update, context) -> int:
    from telegram.ext import ConversationHandler

    raw = re.sub(r"\D", "", update.effective_message.text or "")
    if not raw:
        await _show(update, context, "❌ Введи числовой ID", [_back_admin()])
        return ADM_REVOKE
    uid = int(raw)
    store = _load_store()
    users = [u for u in (store.get("users") or []) if u != uid and u != ADMIN_ID]
    if ADMIN_ID not in users:
        users.insert(0, ADMIN_ID)
    store["users"] = users
    _save_store(store)
    await _api["delete_user"](update, context)
    await _show(update, context, f"✅ Доступ снят: <code>{uid}</code>", [_back_admin()])
    return ConversationHandler.END


async def on_logs(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    logs = list(_load_store().get("logs") or [])[-10:]
    if not logs:
        text = "📭 Логов нет."
    else:
        lines = []
        for entry in reversed(logs):
            lines.append(
                f"👤 <code>{entry.get('uid')}</code> • {entry.get('time', '?')}\n"
                f"    {entry.get('name', '?')} • {entry.get('amount', '?')}₽ • {entry.get('bank', '?')}"
            )
        text = "📊 <b>Последние:</b>\n\n" + "\n\n".join(lines)
    await _show(update, context, text, [_back_admin()])
    return ConversationHandler.END


async def on_stats(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    logs = list(_load_store().get("logs") or [])
    by_user: dict[int, int] = {}
    for entry in logs:
        uid = int(entry.get("uid") or 0)
        by_user[uid] = by_user.get(uid, 0) + 1
    lines = [f"📈 <b>Всего:</b> {len(logs)}"]
    for uid, count in sorted(by_user.items(), key=lambda item: -item[1]):
        mark = "⭐" if uid == ADMIN_ID else "👤"
        lines.append(f"{mark} <code>{uid}</code>: {count}")
    await _show(update, context, "\n".join(lines), [_back_admin()])
    return ConversationHandler.END


async def on_other_stub(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    titles = {
        "oth:check": "🔍 Проверить чек",
        "oth:hist": "📜 История",
        "oth:tpl": "📌 Шаблоны",
        "oth:fast": "⚡ Быстрая генерация",
        "oth:batch": "📦 Пакет чеков",
        "oth:stats": "📊 Статистика",
        "oth:token": "🔑 API-токен",
        "oth:theme": "🌙 Тёмная тема",
        "oth:notif": "🔔 Уведомления",
        "users_list": "🔧 Фичи юзеров",
        "a_bind": "🔗 Привязки ЛК",
        "a_anchors": "⚓ Якоря номеров",
    }
    title = titles.get(update.callback_query.data, "Раздел")
    back = _back_other() if update.callback_query.data.startswith("oth:") else _back_admin()
    await _show(
        update, context,
        f"{title}\n\nКнопка на месте, как в том боте. Сам инструмент ещё переносится.",
        [back],
    )
    return ConversationHandler.END


async def on_help(update, context) -> int:
    from telegram.ext import ConversationHandler

    await _answer(update)
    await _show(
        update, context,
        "<b>❓ Помощь</b>\n\n"
        "Главное меню — банки. Внизу <b>📂 Другое</b> и, у админа, <b>🛠️ Управление</b>.\n"
        "Отмена любого шага — кнопка ❌ Отмена.",
        [_back_other()],
    )
    return ConversationHandler.END


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
        CallbackQueryHandler(on_other, pattern=f"^{OTHER_CB}$"),
        CallbackQueryHandler(on_admin, pattern=f"^{ADMIN_CB}$"),
        CallbackQueryHandler(on_users, pattern=f"^{USERS_CB}$"),
        CallbackQueryHandler(on_new, pattern=f"^{NEW_CB}$"),
        CallbackQueryHandler(on_ban, pattern=r"^adm:(ban|ub):"),
        CallbackQueryHandler(on_bot_users, pattern=r"^a_users$"),
        CallbackQueryHandler(on_grant_start, pattern=r"^a_grant$"),
        CallbackQueryHandler(on_revoke_start, pattern=r"^a_revoke$"),
        CallbackQueryHandler(on_logs, pattern=r"^a_logs$"),
        CallbackQueryHandler(on_stats, pattern=r"^a_stats$"),
        CallbackQueryHandler(on_help, pattern=r"^oth:help$"),
        CallbackQueryHandler(
            on_other_stub,
            pattern=r"^(oth:(check|hist|tpl|fast|batch|stats|token|theme|notif)|users_list|a_bind|a_anchors)$",
        ),
    ]


def conversation_states(back):
    return {
        ADM_LOGIN: [MessageHandler(_TEXT, on_login), back],
        ADM_PASS: [MessageHandler(_TEXT, on_password), back],
        ADM_DUR: [CallbackQueryHandler(on_create, pattern=r"^adm:c:"), back],
        ADM_GRANT: [MessageHandler(_TEXT, on_grant_id), back],
        ADM_REVOKE: [MessageHandler(_TEXT, on_revoke_id), back],
    }
