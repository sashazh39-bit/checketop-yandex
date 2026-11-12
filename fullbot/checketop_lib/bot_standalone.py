#!/usr/bin/env python3
"""Telegram-бот без внешних зависимостей (только stdlib).
Отправьте чек → выберите банк → введите "с какой на какую" → получите PDF.
"""
# Обход: прокси и SSL (корпоративные сети / Python на macOS)
import os as _os
import ssl
import sys
import urllib.request as _ur
from urllib.parse import urlparse
_ssl_ctx = ssl._create_unverified_context()
_proxy_url = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy") or _os.environ.get("HTTP_PROXY") or _os.environ.get("http_proxy")
_handlers = [_ur.HTTPSHandler(context=_ssl_ctx)]
if _proxy_url:
    _proxy = {"http": _proxy_url, "https": _proxy_url}
    _handlers.insert(0, _ur.ProxyHandler(_proxy))
    # Прокси с авторизацией user:pass@host:port
    try:
        p = urlparse(_proxy_url)
        if p.username and p.password:
            _pm = _ur.HTTPPasswordMgrWithDefaultRealm()
            _pm.add_password(None, f"{p.scheme}://{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}", p.username, p.password)
            _handlers.insert(1, _ur.ProxyBasicAuthHandler(_pm))
    except Exception:
        pass
_ur.install_opener(_ur.build_opener(*_handlers))

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

_BOT_DIR = Path(__file__).parent
_ADD_GLYPHS_SCRIPT = _BOT_DIR / "add_glyphs_to_13_03.py"

# Persistent storage: survives Render deploys when a Disk is mounted at /opt/render/project/data
# Locally defaults to the bot directory so nothing changes.
_PERSISTENT_DIR = Path(os.environ.get("PERSISTENT_DIR", str(_BOT_DIR)))
try:
    _PERSISTENT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    _PERSISTENT_DIR = _BOT_DIR

# Persistent offset — prevents replay of processed updates after restart
_OFFSET_FILE = _PERSISTENT_DIR / "bot_offset.txt"
# In-memory dedup: skip update_ids already handled (protects against replay/overlap).
_SEEN_UPDATE_IDS: set[int] = set()
_MAX_SEEN_UPDATE_IDS = 2000


def _load_offset() -> int:
    try:
        return int(_OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        _OFFSET_FILE.write_text(str(offset))
    except Exception:
        pass


def _already_handled_update(update_id: int) -> bool:
    """Return True if this update_id was already processed in this process."""
    if update_id in _SEEN_UPDATE_IDS:
        return True
    _SEEN_UPDATE_IDS.add(update_id)
    if len(_SEEN_UPDATE_IDS) > _MAX_SEEN_UPDATE_IDS:
        drop = sorted(_SEEN_UPDATE_IDS)[: _MAX_SEEN_UPDATE_IDS // 2]
        for uid in drop:
            _SEEN_UPDATE_IDS.discard(uid)
    return False


# Загрузка .env вручную (без python-dotenv)
try:
    env_path = Path(os.environ.get("BOT_ENV_FILE", str(Path(__file__).parent / ".env")))
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except Exception:
    pass

from pdf_patcher import patch_pdf_file, patch_amount as pdf_patch_amount, format_amount_display
from tbank_check_service import (
    patch_amount_only as tbank_patch_amount,
    patch_all_fields as tbank_patch_all_fields,
    extract_fields as tbank_extract_fields,
    detect_receipt_type as tbank_detect_type,
    is_tbank_pdf,
    RECEIPT_TYPES as TBANK_RECEIPT_TYPES,
    RECEIPT_TYPE_LABELS as TBANK_TYPE_LABELS,
)
from tbank_scratch_service import generate_from_scratch as tbank_generate_scratch
from alfa_scratch_service import generate_alfa_scratch, get_fields_for_scratch as alfa_get_fields_for_scratch
from vtb_scratch_service import (
    generate_vtb_sbp,
    generate_vtb_vtb,
    generate_from_values as vtb_generate_from_values,
    get_fields_for_scratch as vtb_get_fields_for_scratch,
    pick_sbp_template,
)
from tbank_statement_service import (
    patch_tbank_statement,
    extract_statement_fields as tbank_extract_statement,
    BASE_STATEMENT as TBANK_STATEMENT_TEMPLATE,
)
from tbank_cmap import (
    get_unsupported_chars as tbank_get_unsupported,
    format_unsupported_error as tbank_format_unsupported,
    extract_pdf_font_widths as tbank_extract_pdf_font_widths,
)
from alfa_transgran_patch import (
    patch_transgran,
    extract_transgran_fields,
    _mutate_doc_id,
    format_alfa_transgran_credited,
)
from vtb_transgran_patch import patch_vtb_transgran, extract_fields as extract_vtb_transgran_fields, parse_rate, format_credited, format_amount_rub
from vtb_patch_from_config import patch_from_values, patch_amount_only
from vtb_cmap import get_unsupported_chars, format_unsupported_error, suggest_replacement, FALLBACK_TIPS
from vtb_sber_reference import scan_vtb_unsupported_chars
from vtb_test_generator import update_creation_date
from receipt_db import (
    receipt_supports_chars,
    get_missing_chars_in_receipt,
    chars_from_text_fields,
    find_donor,
    get_bank_report,
    get_bank_counts,
    load_index,
    add_receipt_to_index,
    build_and_save,
    get_operation_id_from_pdf,
    COMMON_AMOUNTS,
    VTB_SUBTYPES,
)

BASE = "https://api.telegram.org/bot"

# --- SBP пул ---
try:
    from sbp_pool import SBPPool as _SBPPool
    _sbp_pool = _SBPPool(_PERSISTENT_DIR / "sbp_pool.json")
except Exception:
    _sbp_pool = None  # type: ignore

# --- Alfa SBP operation number persistent counter ---
_ALFA_OP_COUNTER_FILE = _PERSISTENT_DIR / "alfa_op_counter.json"


def _next_alfa_op_id(operation_date: str, operation_time: str = "12:00:00") -> str:
    """Return the next Alfa-Bank operation ID and persist the counter.

    The sequence number increments by a random delta in [1213, 1293] after each
    generation, resetting to a time-based estimate when the date changes.

    Format: C16 + DD + MM + YY + 7-digit seq  (total 16 chars).
    YY is the LAST two digits of the year (e.g. 2026 → "26").
    """
    import json as _json
    from gen_sbp_receipt import _generate_operation_id  # type: ignore

    try:
        state = _json.loads(_ALFA_OP_COUNTER_FILE.read_text()) if _ALFA_OP_COUNTER_FILE.exists() else {}
    except Exception:
        state = {}

    last_date = state.get("date", "")
    last_seq = state.get("seq", 0)

    today = operation_date if operation_date not in ("auto", "", None) else __import__("datetime").datetime.now().strftime("%d.%m.%Y")

    # Normalize single-digit hour: "1:13:23" → "01:13:23"
    if operation_time and operation_time not in ("auto", "", None):
        _tp = operation_time.split(":")
        if len(_tp) >= 2:
            try:
                operation_time = f"{int(_tp[0]):02d}:{_tp[1]}" + (f":{_tp[2]}" if len(_tp) >= 3 else ":00")
            except (ValueError, IndexError):
                pass

    if last_date != today or last_seq <= 0:
        seeded = _generate_operation_id(today, operation_time)
        try:
            last_seq = int(seeded[9:])
        except Exception:
            last_seq = 500_000
    else:
        delta = __import__("random").randint(1213, 1293)
        last_seq = min(last_seq + delta, 2_199_999)

    # Parse the date rigorously: always use DD/MM from positions 0:2 / 3:5 and
    # the LAST two chars of the year (works for both DD.MM.YYYY and DD.MM.YY).
    parts = today.split(".")
    try:
        dd = f"{int(parts[0]):02d}"
        mm = f"{int(parts[1]):02d}"
        yy_full = parts[2]
        yy = yy_full[-2:] if len(yy_full) >= 2 else yy_full.zfill(2)
    except (IndexError, ValueError):
        # Fallback: mirror gen_sbp_receipt's own generator so the ID is at
        # least internally consistent even if the date is malformed.
        return _generate_operation_id(today, operation_time)

    new_id = f"C16{dd}{mm}{yy}{last_seq:07d}"

    # Hard invariant: must be exactly 16 chars and match the bank regex, else
    # fall back to the vetted generator instead of emitting a broken ID.
    import re as _re
    if not _re.fullmatch(r"C16\d{13}", new_id):
        new_id = _generate_operation_id(today, operation_time)

    try:
        _ALFA_OP_COUNTER_FILE.write_text(_json.dumps({"date": today, "seq": last_seq}))
    except Exception:
        pass

    return new_id

VTB_UNSUPPORTED_NOTICE = (
    "⚠️ Нельзя использовать: ё, Ё, неразрывный дефис (‑). "
    "Замените на: е, Е, обычный дефис (-)."
)
USER_STATE: dict[int, dict] = {}

# Разрешённые user_id (через запятую в .env: ALLOWED_USER_IDS=123456,789012)
# NOTE: делаем устойчивый парсинг (запятые/пробелы/кавычки/лишний текст).
_ALLOWED_IDS: set[int] = set()
_raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
if _raw:
    for s in re.findall(r"\d+", _raw):
        _ALLOWED_IDS.add(int(s))

# Фолбэк для пользователя, у которого периодически теряется доступ из-за окружения.
_ALWAYS_ALLOWED_IDS: frozenset[int] = frozenset({1445265832, 7076663447, 8178442784, 6575861571})
_EFFECTIVE_ALLOWED_IDS: frozenset[int] = frozenset(_ALLOWED_IDS | set(_ALWAYS_ALLOWED_IDS))

ACCESS_DENIED_MSG = "🚫 Доступ запрещён. Бот доступен только ограниченному кругу пользователей."

# ---------------------------------------------------------------------------
# Временный доступ: выдаётся командой /grant без редеплоя
# Хранится в _PERSISTENT_DIR/temp_access.json: {uid_str: expiry_unix_timestamp}
# В памяти держим кеш — файл читается ОДИН раз при старте, потом только пишется.
# Это исключает I/O-сбои на Render.com при каждой проверке доступа.
# ---------------------------------------------------------------------------
_TEMP_ACCESS_FILE = _PERSISTENT_DIR / "temp_access.json"

# In-memory cache: uid → expiry timestamp. Заполняется при старте.
_TEMP_ACCESS_CACHE: dict[int, float] = {}


def _temp_access_load_from_file() -> None:
    """Загрузить гранты из файла в кеш (вызывается один раз при старте)."""
    global _TEMP_ACCESS_CACHE
    try:
        if _TEMP_ACCESS_FILE.exists():
            data = json.loads(_TEMP_ACCESS_FILE.read_text(encoding="utf-8"))
            _TEMP_ACCESS_CACHE = {int(k): float(v) for k, v in data.items()}
        else:
            _TEMP_ACCESS_CACHE = {}
    except Exception:
        _TEMP_ACCESS_CACHE = {}


def _temp_access_flush() -> None:
    """Сохранить текущий кеш в файл."""
    try:
        _PERSISTENT_DIR.mkdir(parents=True, exist_ok=True)
        _TEMP_ACCESS_FILE.write_text(
            json.dumps({str(k): v for k, v in _TEMP_ACCESS_CACHE.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# Загружаем при старте — единственное чтение файла
_temp_access_load_from_file()


def _load_temp_access() -> dict[int, float]:
    """Вернуть копию текущего кеша (без чтения файла)."""
    return dict(_TEMP_ACCESS_CACHE)


def _save_temp_access(grants: dict[int, float]) -> None:
    """Обновить кеш и записать в файл."""
    global _TEMP_ACCESS_CACHE
    _TEMP_ACCESS_CACHE = dict(grants)
    _temp_access_flush()


def _is_temp_allowed(uid: int) -> bool:
    """Проверить есть ли у пользователя действующий временный доступ (только кеш)."""
    expiry = _TEMP_ACCESS_CACHE.get(uid)
    if expiry is None:
        return False
    if time.time() > expiry:
        # Срок истёк — убрать из кеша и файла
        _TEMP_ACCESS_CACHE.pop(uid, None)
        _temp_access_flush()
        return False
    return True


def _is_allowed(uid: int) -> bool:
    """Проверить разрешён ли доступ (постоянный или временный)."""
    if not _EFFECTIVE_ALLOWED_IDS:
        return True
    if uid in _EFFECTIVE_ALLOWED_IDS:
        return True
    return _is_temp_allowed(uid)


def _parse_duration(s: str) -> int | None:
    """Парсить строку типа '24h', '7d', '30m' → секунды. None при ошибке."""
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(h|d|m|ч|д|м|мин|час|ден|дн)", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit in ("h", "ч", "час"):
        return n * 3600
    if unit in ("d", "д", "ден", "дн"):
        return n * 86400
    if unit in ("m", "м", "мин"):
        return n * 60
    return None

# Статистика PDF: счётчики только для этих пар (telegram user_id → подпись в отчёте).
STATS_TRACKED_USERS: tuple[tuple[int, str], ...] = (
    (1445265832, "диролЧ"),
    (7076663447, "ДмитрийЧ"),
    (8178442784, "Зимбамбву"),
)
STATS_IGNORE_USER_IDS: frozenset[int] = frozenset({
    1445265832,  # ДиролЧ — счётчики заморожены: ни генерация, ни редактирование чеков/выписок не учитываются.
})

STATS_PATH = _PERSISTENT_DIR / "bot_pdf_stats.json"


def _stats_load() -> dict:
    if not STATS_PATH.exists():
        return {"days": {}}
    try:
        raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"days": {}}
        days = raw.get("days")
        if not isinstance(days, dict):
            days = {}
        return {"days": dict(days)}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"days": {}}


def _stats_save(data: dict) -> None:
    try:
        STATS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _stats_user_cell_default() -> dict:
    return {
        "receipts": {"alfa": 0, "vtb": 0, "tbank": 0, "auto": 0},
        "statements": {"alfa": 0, "vtb": 0, "tbank": 0},
    }


def _stats_coerce_user_cell(raw) -> dict:
    """Привести ячейку пользователя к новой структуре (миграция со старых int)."""
    out = _stats_user_cell_default()
    if not isinstance(raw, dict):
        return out
    r = raw.get("receipts")
    if isinstance(r, dict):
        for k in ("alfa", "vtb", "tbank", "auto"):
            out["receipts"][k] = int(r.get(k, 0) or 0)
    elif isinstance(r, int) and r:
        out["receipts"]["auto"] = r
    s = raw.get("statements")
    if isinstance(s, dict):
        out["statements"]["alfa"] = int(s.get("alfa", 0) or 0)
        out["statements"]["vtb"] = int(s.get("vtb", 0) or 0)
        out["statements"]["tbank"] = int(s.get("tbank", 0) or 0)
        unk = s.get("unknown")
        if isinstance(unk, int) and unk:
            out["statements"]["unknown"] = unk
    elif isinstance(s, int) and s:
        out["statements"]["unknown"] = s
    return out


def stats_record_pdf(uid: int, kind: str, bank: str) -> None:
    """Учесть успешно выданный PDF: kind — receipt | statement; bank — alfa | vtb | auto."""
    if kind not in ("receipt", "statement"):
        return
    if uid in STATS_IGNORE_USER_IDS:
        return
    if uid not in {u for u, _ in STATS_TRACKED_USERS}:
        return
    b = (bank or "").strip().lower()
    if kind == "receipt":
        if b not in ("alfa", "vtb", "tbank", "auto"):
            b = "auto"
    else:
        if b not in ("alfa", "vtb", "tbank"):
            b = "alfa"
    d = _stats_load()
    uid_s = str(uid)
    day = datetime.now().strftime("%Y-%m-%d")
    d["days"].setdefault(day, {})
    cell = _stats_coerce_user_cell(d["days"][day].get(uid_s))
    if kind == "receipt":
        cell["receipts"][b] = int(cell["receipts"].get(b, 0)) + 1
    else:
        cell["statements"][b] = int(cell["statements"].get(b, 0)) + 1
    d["days"][day][uid_s] = cell
    _stats_save(d)


def _stats_format_user_block(display_name: str, u: dict) -> str:
    ra = int(u["receipts"]["alfa"])
    rv = int(u["receipts"]["vtb"])
    rt = int(u["receipts"].get("tbank", 0))
    rauto = int(u["receipts"]["auto"])
    sa = int(u["statements"]["alfa"])
    sv = int(u["statements"]["vtb"])
    st = int(u["statements"].get("tbank", 0))
    su = int(u["statements"].get("unknown", 0) or 0)
    chk = f"чеки — Альфа: {ra}, ВТБ: {rv}, ТБанк: {rt}"
    if rauto:
        chk += f", авто: {rauto}"
    stmt = f"выписки — Альфа: {sa}, ВТБ: {sv}, ТБанк: {st}"
    if su:
        stmt += f" (ещё {su} без банка в старом учёте)"
    return f"• {display_name}\n  {chk}\n  {stmt}"


def _stats_aggregate_all_days(uid_s: str) -> dict:
    """Суммарные счётчики пользователя по всем дням."""
    agg = _stats_user_cell_default()
    days = (_stats_load().get("days") or {})
    for _day, users in days.items():
        if not isinstance(users, dict):
            continue
        cell = users.get(uid_s)
        if not cell:
            continue
        c = _stats_coerce_user_cell(cell)
        for k in ("alfa", "vtb", "tbank", "auto"):
            agg["receipts"][k] = int(agg["receipts"].get(k, 0)) + int(
                c["receipts"].get(k, 0) or 0
            )
        for k in ("alfa", "vtb", "tbank"):
            agg["statements"][k] = int(agg["statements"].get(k, 0)) + int(
                c["statements"].get(k, 0) or 0
            )
        unk = c["statements"].get("unknown", 0) or 0
        if unk:
            agg["statements"]["unknown"] = int(
                agg["statements"].get("unknown", 0) or 0
            ) + int(unk)
    return agg


def stats_format_today() -> str:
    """Сводка за сегодня и за всё время: чеки и выписки с разбивкой по банкам."""
    day = datetime.now().strftime("%Y-%m-%d")
    today = (_stats_load().get("days") or {}).get(day) or {}
    lines = [f"📊 Статистика за {day}", ""]
    for uid, name in STATS_TRACKED_USERS:
        uid_s = str(uid)
        u = _stats_coerce_user_cell(today.get(uid_s))
        lines.append(_stats_format_user_block(name, u))
    lines.append("")
    lines.append("📈 За всё время")
    lines.append("")
    for uid, name in STATS_TRACKED_USERS:
        uid_s = str(uid)
        u = _stats_aggregate_all_days(uid_s)
        lines.append(_stats_format_user_block(name, u))
    return "\n".join(lines)


MAIN_MENU_TEXT = (
    "👋 Главная\n\n"
    "📁 Чек — загрузить PDF или сгенерировать чек."
)
MAIN_MENU_KB = [
    [{"text": "📁 Чек", "callback_data": "main_check"}],
]

CHANGELOG_TEXT = (
    "📝 Последние изменения\n\n"
    "16.04.2026\n\n"
    "• Альфа СБП — исправлена ошибка «No glyph source font» при генерации:\n"
    "  шрифт Tahoma теперь встроен в бот, MS Office на сервере не нужен.\n\n"
    "• Убрана надпись OnlyPDF везде — больше нет лишних предупреждений.\n"
    "  После генерации два чётких варианта:\n"
    "  «Прислать чек для нового SBP ID» или «Сохранить PDF».\n\n"
    "• Альфа СБП — новое поле в мастере генерации:\n"
    "  «Последние 4 цифры счёта списания» — бот сам строит корректный\n"
    "  20-значный счёт с правильной контрольной цифрой.\n\n"
    "• Номер операции теперь автоматически растёт на 1213–1293 после\n"
    "  каждой генерации и сохраняется между перезапусками сервера.\n\n"
    "• Т-Банк СБП — исправлен визуальный баг «000 ₽» вместо суммы:\n"
    "  в жирный шрифт добавлены все цифры 0–9 из обычного шрифта.\n\n"
    "• Т-Банк — поля с невидимыми символами теперь пропускаются;\n"
    "  бот предупреждает какие буквы отсутствуют в шрифте донора.\n\n"
    "• Т-Банк — нулевая дельта при патчинге: размер файла всегда\n"
    "  совпадает с донором, что улучшает прохождение проверок.\n\n"
    "• Добавлена команда /check для Т-Банк СБП.\n\n"
    "• Постоянное хранилище на Render.com: sbp_pool и статистика\n"
    "  теперь переживают редеплой."
)

ZAYAVKI_DIR = Path(__file__).parent / "заявки"
STATUS_LABELS = {"новый": "🆕 Новый", "в работе": "🔄 В работе", "оплачено": "💰 Оплачено"}
STATUS_SHORT = {"n": "новый", "w": "в работе", "o": "оплачено"}
STATUS_TO_SHORT = {"новый": "n", "в работе": "w", "оплачено": "o"}


def save_zayavka(uid: int, username: str, amount_from: int, amount_to: int, bank: str, pdf_name: str, description: str) -> Path | None:
    """Сохранить заявку в JSON (файл по дате). Возвращает путь к файлу или None."""
    try:
        ZAYAVKI_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        filepath = ZAYAVKI_DIR / f"{day}.json"
        data = []
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = []
        entry = {
            "id": f"{int(time.time() * 1000)}_{uid}",
            "user_id": uid,
            "username": username or "",
            "timestamp": datetime.now().isoformat(),
            "amount_from": amount_from,
            "amount_to": amount_to,
            "bank": bank,
            "pdf_name": pdf_name,
            "описание": description,
            "статус": "новый",
        }
        data.append(entry)
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return filepath
    except Exception as e:
        print(f"  ⚠️ Ошибка сохранения заявки: {e}")
        return None


def load_all_zayavki() -> list[dict]:
    """Загрузить все заявки из JSON (все файлы заявки/*.json), сортировка по дате (новые сверху)."""
    result = []
    if not ZAYAVKI_DIR.exists():
        return result
    for fp in sorted(ZAYAVKI_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for e in data:
                e.setdefault("статус", "новый")
                result.append(e)
        except (json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return result


def update_zayavka_status(zayavka_id: str, new_status: str) -> bool:
    """Обновить статус заявки по id. Вернуть True если успешно."""
    if not ZAYAVKI_DIR.exists():
        return False
    for fp in ZAYAVKI_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for i, e in enumerate(data):
                if e.get("id") == zayavka_id:
                    data[i]["статус"] = new_status
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def get_zayavka_by_id(zayavka_id: str) -> dict | None:
    """Найти заявку по id."""
    for z in load_all_zayavki():
        if z.get("id") == zayavka_id:
            return z
    return None


def build_zayavki_list(limit: int = 15) -> tuple[str, str]:
    """Список заявок: каждая — кнопка для открытия."""
    try:
        items = load_all_zayavki()
    except Exception as e:
        print(f"  ⚠️ Ошибка загрузки заявок: {e}")
        return "📋 Заявки:\n\nОшибка загрузки.", json.dumps({"inline_keyboard": []})
    if not items:
        return "📋 Заявки:\n\nНет заявок. Нажмите на заявку для просмотра.", json.dumps({"inline_keyboard": []})
    lines = [f"📋 Заявки (всего {len(items)})\n\nНажмите на заявку:"]
    keyboard = []
    for i, z in enumerate(items[:limit]):
        try:
            st = z.get("статус", "новый")
            am_from = int(z.get("amount_from") or 0)
            am_to = int(z.get("amount_to") or 0)
            ts = (z.get("timestamp") or "")[:10]
            lbl = STATUS_LABELS.get(st, st)
            btn_text = f"{i+1}. {format_amount_display(am_from)}→{format_amount_display(am_to)} ₽ · {ts} {lbl}"[:64]
            zid = z.get("id") or ""
            if not zid:
                continue
            cb = f"v_{zid}"[:64]
            keyboard.append([{"text": btn_text, "callback_data": cb}])
        except Exception as e:
            print(f"  ⚠️ Ошибка заявки {i}: {e}")
            continue
    return "\n".join(lines), json.dumps({"inline_keyboard": keyboard})


def build_zayavka_detail(zayavka_id: str) -> tuple[str, str] | None:
    """Карточка заявки с кнопками [В работе] [Оплачено] [Назад]."""
    z = get_zayavka_by_id(zayavka_id)
    if not z:
        return None
    st = z.get("статус", "новый")
    am_from = int(z.get("amount_from") or 0)
    am_to = int(z.get("amount_to") or 0)
    ts = (z.get("timestamp") or "")[:16].replace("T", " ")
    desc = z.get("описание") or "(без описания)"
    bank = z.get("bank", "")
    bank_name = {"alfa": "Альфа", "vtb": "ВТБ", "auto": "Авто"}.get(bank, bank)
    lbl = STATUS_LABELS.get(st, st)
    txt = (
        f"📋 Заявка\n\n"
        f"💰 {format_amount_display(am_from)} → {format_amount_display(am_to)} ₽\n"
        f"🏦 {bank_name} · {ts}\n"
        f"📌 {lbl}\n\n"
        f"📝 {desc}"
    )
    cb_w = f"st_{zayavka_id}_w"[:64]
    cb_o = f"st_{zayavka_id}_o"[:64]
    kb = {
        "inline_keyboard": [
            [
                {"text": "🔄 В работе", "callback_data": cb_w},
                {"text": "💰 Оплачено", "callback_data": cb_o},
            ],
            [{"text": "⬅️ К списку", "callback_data": "main_zayavki"}],
        ]
    }
    return txt, json.dumps(kb)


def tg_request(token: str, method: str, data: dict | None = None, files: dict | None = None) -> dict:
    url = f"{BASE}{token}/{method}"
    max_retries = 3
    last_err = None
    for attempt in range(max_retries):
        try:
            return _tg_request_once(url, data, files)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < max_retries - 1:
                delay = (attempt + 1) * 3
                print(f"  ⚠️ Сеть: {e}. Повтор через {delay} сек...")
                time.sleep(delay)
            else:
                raise
    raise last_err


def _tg_request_once(url: str, data: dict | None, files: dict | None) -> dict:
    if files:
        boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
        body = []
        if data:
            for k, v in data.items():
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fname, fbytes) in files.items():
            body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: application/pdf\r\n\r\n".encode())
            body.append(fbytes)
            body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())
        full_body = b"".join(body)
        req = urllib.request.Request(url, data=full_body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode())


def tg_get_file(token: str, file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < 2:
                print(f"  ⚠️ Загрузка файла: {e}. Повтор...")
                time.sleep(3)
            else:
                raise


def tg_get_file_path(token: str, file_id: str) -> str:
    r = tg_request(token, "getFile", {"file_id": file_id})
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile failed"))
    return r["result"]["file_path"]


def parse_amounts(text: str) -> tuple[int, int] | None:
    nums = re.findall(r"\d+", text.strip())
    if len(nums) >= 2:
        return int(nums[0]), int("".join(nums[1:]))
    return None




def _increment_filename(stem: str) -> str:
    """Увеличить последнюю цифру в имени файла на 1. check3 → check4, abc → abc2."""
    m = re.search(r'(\d+)(\D*)$', stem)
    if m:
        num = int(m.group(1)) + 1
        return stem[:m.start(1)] + str(num) + m.group(2)
    return stem + "2"


def scale_commission_rub(amount_from: int, amount_to: int, commission_from: int) -> int:
    """Пропорционально масштабировать комиссию при смене суммы перевода (руб., целые)."""
    if amount_from <= 0 or commission_from <= 0:
        return commission_from
    return max(0, int(round(commission_from * amount_to / amount_from)))


def extract_commission_from_pdf(pdf_path: str) -> int | None:
    """Извлечь комиссию из PDF-чека. Вернуть сумму в рублях или None."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return None
    patterns = [
        r'[Кк]омиссия[^\d]*?([\d\s]+)[,\.](\d{2})',
        r'[Кк]омиссия[^\d]*?([\d\s]+)\s*(?:₽|руб|RUR|RUB)',
        r'Commission[^\d]*?([\d\s]+)[,\.](\d{2})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1).replace(' ', '').replace('\xa0', '')
            try:
                val = int(raw)
                return val
            except ValueError:
                pass
    return None


def parse_auto_replacements(text: str) -> list[tuple[str, str]]:
    """Разобрать пары замен формата (старое = новое) или старое=новое."""
    pairs = re.findall(r'\(([^)]+?)\s*=\s*([^)]+?)\)', text)
    if pairs:
        return [(a.strip(), b.strip()) for a, b in pairs]
    # Fallback: строки вида ключ=значение
    result = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip()
            if k and v:
                result.append((k, v))
    return result


def _send_main_menu_button(token: str, chat_id: int, tg_req) -> None:
    """Отправить кнопку «Главное меню» после генерации."""
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "✅ Готово! Нажмите для возврата:",
        "reply_markup": json.dumps({
            "inline_keyboard": [[{"text": "🏠 Главное меню", "callback_data": "main_back"}]],
        }),
    })


def _do_amount_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч суммы (и опционально комиссии) и отправить PDF."""
    inp = state["file_path"]
    bank = state["bank"]
    amount_from = state.get("current_amount") or 0
    amount_to = state["amount_to"]
    out_stem = _increment_filename(Path(state["file_name"]).stem)
    out_name = out_stem + ".pdf"

    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        commission_from = state.get("current_commission") or 0
        commission_to = state.get("commission_to")
        transgran_scaled = False
        data = bytearray(Path(inp).read_bytes())

        if bank == "vtb":
            try:
                out_bytes = bytes(patch_amount_only(data, Path(inp), amount_to))
            except Exception:
                ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
                if not ok_sum or new_data is None:
                    try:
                        os.unlink(inp)
                    except OSError:
                        pass
                    del USER_STATE[uid]
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Сумма не найдена в чеке. {err_sum or ''}"})
                    _send_main_menu_button(token, chat_id, tg_req)
                    return
                out_bytes = new_data
        else:
            # Альфа трансгран: сумма + комиссия + UZS зачисления одним CID-патчем
            out_bytes = None
            try:
                from alfa_transgran_patch import is_alfa_transgran_receipt, patch_transgran_scale_amount
                pdf_blob = Path(inp).read_bytes()
                if bank in ("alfa", "auto") and is_alfa_transgran_receipt(pdf_blob):
                    ok_tg, _err_tg, data_tg = patch_transgran_scale_amount(pdf_blob, amount_to)
                    if ok_tg and data_tg:
                        out_bytes = data_tg
                        commission_to = None  # уже в PDF
                        transgran_scaled = True
            except Exception:
                pass

            if out_bytes is None:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path_tmp = tf.name
                ok, err = patch_pdf_file(inp, out_path_tmp, amount_from, amount_to, bank=bank)
                if not ok:
                    try:
                        os.unlink(inp)
                        os.unlink(out_path_tmp)
                    except OSError:
                        pass
                    del USER_STATE[uid]
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
                    _send_main_menu_button(token, chat_id, tg_req)
                    return
                out_bytes = Path(out_path_tmp).read_bytes()
                try:
                    os.unlink(out_path_tmp)
                except OSError:
                    pass

        # Комиссия
        if commission_to is not None and commission_from > 0:
            try:
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    comm_path = tf.name
                Path(comm_path).write_bytes(out_bytes)
                old_c = str(commission_from)
                new_c = str(commission_to)
                if patch_replacements(Path(comm_path), Path(comm_path), [(old_c, new_c)]):
                    out_bytes = Path(comm_path).read_bytes()
                try:
                    os.unlink(comm_path)
                except OSError:
                    pass
            except Exception:
                pass

        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]
        am_from_disp = format_amount_display(amount_from) if amount_from else "?"
        caption = f"✅ Готово: {am_from_disp} ₽ → {format_amount_display(amount_to)} ₽"
        if commission_to is not None and commission_from > 0:
            caption += f"\nКомиссия: {format_amount_display(commission_from)} → {format_amount_display(commission_to)} ₽"
        elif transgran_scaled and commission_from > 0 and amount_from:
            c_auto = scale_commission_rub(amount_from, amount_to, commission_from)
            caption += f"\nКомиссия: {format_amount_display(commission_from)} → {format_amount_display(c_auto)} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        stats_record_pdf(uid, "receipt", bank)
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        try:
            os.unlink(inp)
        except OSError:
            pass
        if uid in USER_STATE:
            del USER_STATE[uid]
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
        _send_main_menu_button(token, chat_id, tg_req)


def _format_alfa_card_commission(val: str) -> str:
    """Форматирует комиссию для замены в PDF: '729' → '729 RUR', '729,50' → '729,50 RUR'."""
    v = val.strip().replace(" ", "").replace("\xa0", "")
    v = v.replace(".", ",")
    if "," in v:
        parts = v.split(",", 1)
        whole = parts[0].lstrip("0") or "0"
        frac = (parts[1] + "00")[:2]
        if frac == "00":
            return f"{format_amount_display(int(whole))} RUR"
        return f"{format_amount_display(int(whole))},{frac} RUR"
    digits = re.sub(r"\D", "", v)
    if not digits:
        return "0 RUR"
    return f"{format_amount_display(int(digits))} RUR"


def _run_alfa_card_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Патч Альфа «по карте»: сумма + комиссия через CID-замены."""
    from cid_patch_amount import patch_replacements

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    inp = Path(state["file_path"])
    out_stem = _increment_filename(Path(state["file_name"]).stem)
    out_name = out_stem + ".pdf"

    send("⏳ Обрабатываю чек...")

    try:
        old_amt_raw = state.get("alfa_card_old_amount", "")
        old_comm_raw = state.get("alfa_card_old_commission", "")
        new_amt_val = state.get("alfa_card_new_amount")
        new_comm_val = state.get("alfa_card_new_commission")

        old_amt = old_amt_raw.replace("\xa0", " ").strip()
        old_comm = old_comm_raw.replace("\xa0", " ").strip()

        pairs: list[tuple[str, str]] = []

        if new_amt_val is not None:
            new_amt = f"{format_amount_display(new_amt_val)} RUR"
            if old_amt and old_amt != new_amt:
                pairs.append((old_amt, new_amt))

        if new_comm_val is not None:
            new_comm = _format_alfa_card_commission(new_comm_val)
            if old_comm and old_comm != new_comm:
                pairs.append((old_comm, new_comm))

        if not pairs:
            send("ℹ️ Нет изменений. Значения совпадают с текущими.")
            _send_main_menu_button(token, chat_id, tg_req)
            return

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out_path = tf.name

        ok = patch_replacements(inp, Path(out_path), pairs)
        if not ok:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            send("❌ Не удалось применить замены.")
            _send_main_menu_button(token, chat_id, tg_req)
            return

        pdf_bytes = Path(out_path).read_bytes()
        try:
            os.unlink(out_path)
        except OSError:
            pass
        try:
            os.unlink(str(inp))
        except OSError:
            pass
        del USER_STATE[uid]

        summary = []
        if new_amt_val is not None:
            summary.append(f"Сумма → {format_amount_display(new_amt_val)} ₽")
        if new_comm_val is not None:
            summary.append(f"Комиссия → {new_comm_val}")
        caption = "✅ Альфа по карте\n" + "\n".join(summary)
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
        stats_record_pdf(uid, "receipt", "alfa")
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        try:
            os.unlink(str(inp))
        except OSError:
            pass
        if uid in USER_STATE:
            del USER_STATE[uid]
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")
        _send_main_menu_button(token, chat_id, tg_req)


def run_check_caps(payer: str, recipient: str) -> tuple[bool, str]:
    """Проверка ФИО: получатся ли заглавные буквы. Возвращает (ok, output)."""
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    try:
        proc = subprocess.run(
            [os.sys.executable, str(_ADD_GLYPHS_SCRIPT), "--check-caps", "--payer", payer or "", "--recipient", recipient or ""],
            cwd=str(_BOT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        full = out or err or proc.stderr.decode(errors="replace") if proc.stderr else ""
        if proc.returncode != 0 and not full:
            full = f"Код выхода: {proc.returncode}"
        return proc.returncode == 0, full[:4000]  # лимит Telegram
    except subprocess.TimeoutExpired:
        return False, "Таймаут проверки (30 сек)"
    except Exception as e:
        return False, f"Ошибка: {e}"


def _decimal_safe_incs(hex_char: str) -> list[int]:
    """Инкременты 1..15, результат — цифра 0-9. Верификатор VTB требует decimal в pos=0."""
    base = int(hex_char.upper(), 16)
    return [i for i in range(1, 16) if (base + i) % 16 < 10]


def _change_one_char_in_id(data: bytearray) -> None:
    """Изменить ровно один символ в /ID. pos=0, результат 0-9 (как в add_glyphs — VTB верификатор)."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    hex1 = id_m.group(1).decode().upper()
    pos = 0
    incs = _decimal_safe_incs(hex1[pos])
    if not incs:
        return
    inc = incs[0]
    idx = "0123456789ABCDEF".find(hex1[pos])
    new_c = "0123456789ABCDEF"[(idx + inc) % 16]
    new1 = hex1[:pos] + new_c + hex1[pos + 1:]
    slot_len = id_m.end(1) - id_m.start(1)
    new_enc = new1.encode().ljust(slot_len)[:slot_len]
    data[id_m.start(1) : id_m.end(1)] = new_enc
    data[id_m.start(2) : id_m.end(2)] = new_enc


def run_sbp_generate_15_03(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация с шаблоном 15-03-26. Целостность: content-only если ФИО влезает, иначе add_glyphs + keep operation_id."""
    try:
        _tpl = pick_sbp_template()
    except FileNotFoundError as e:
        return False, None, str(e)
    _sbp = _tpl.parent
    req = {c for c in chars_from_text_fields(payer or "", recipient or "", "+7 (999) 000-00-00")}
    req.discard(" ")
    try:
        from receipt_db import get_receipt_chars, get_missing_chars_in_receipt, _normalize_char
    except ImportError:
        return False, None, ""
    req = {_normalize_char(c) for c in req}
    missing = get_missing_chars_in_receipt(_tpl, req) if req else set()
    if not date_str or date_str.strip().lower() in ("now", "сейчас"):
        dt = datetime.now()
        date_part = dt.strftime("%d.%m.%Y")
        time_part = dt.strftime("%H:%M")
    else:
        parts = date_str.strip().split(",")
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
    date_str_full = f"{date_part}, {time_part}"
    meta_date = datetime.strptime(date_str_full, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")
    phone = f"+7 ({__import__('random').randint(900, 999)}) {__import__('random').randint(100, 999)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}"
    if not missing:
        data = bytearray(_tpl.read_bytes())
        try:
            out = patch_from_values(
                data, _tpl,
                date_str=date_str_full, payer=payer or "Иван Иванович И.", recipient=recipient or "Иван Иванович И.",
                phone=phone, bank=bank or "Т-Банк", amount=amount, operation_id=None, keep_metadata=True,
                account_last4=account if account and re.match(r"^\d{4}$", account) else None,
            )
        except ValueError as e:
            return False, None, str(e)
        out_arr = bytearray(out)
        update_creation_date(out_arr, meta_date)
        _change_one_char_in_id(out_arr)
        return True, bytes(out_arr), ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(_BOT_DIR)) as tf:
        out_path = tf.name
    try:
        cmd = [
            os.sys.executable, str(_ADD_GLYPHS_SCRIPT),
            "--replace", "--hybrid-safe", "--target", str(_tpl), "--id-from", str(_tpl),
            "--keep-operation-id", "--payer", payer or "Иван Иванович И.", "--recipient", recipient or "Иван Иванович И.",
            "--bank", bank or "Т-Банк", "--amount", str(amount), "--date", date_part, "--time", time_part, "-o", out_path,
        ]
        if account and re.match(r"^\d{4}$", account):
            cmd.extend(["--account", account])
        proc = subprocess.run(cmd, cwd=str(_BOT_DIR), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, None, (proc.stderr or proc.stdout or "").strip()[:500]
        if Path(out_path).exists():
            return True, Path(out_path).read_bytes(), ""
        return False, None, "PDF не создан"
    except subprocess.TimeoutExpired:
        return False, None, "Таймаут"
    except Exception as e:
        return False, None, str(e)
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def run_sbp_generate_verified(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация чека БЕЗ модификации шрифта (content-only).
    Донор: bundled vtb_sbp_template.pdf или check (3).pdf.
    """
    try:
        _donor = pick_sbp_template()
    except FileNotFoundError as e:
        return False, None, str(e)
    try:
        from receipt_db import get_receipt_chars, _normalize_char
    except ImportError:
        return False, None, "receipt_db недоступен"
    req = {_normalize_char(c) for c in chars_from_text_fields(payer or "", recipient or "", f"+7 (999) 000-00-00")}
    donor_chars = get_receipt_chars(_donor)
    if req and not (req <= donor_chars):
        missing = req - donor_chars
        return False, None, f"В доноре нет букв: {''.join(sorted(missing))}"
    if not date_str or date_str.strip().lower() in ("now", "сейчас"):
        dt = datetime.now()
        date_part = dt.strftime("%d.%m.%Y")
        time_part = dt.strftime("%H:%M")
    else:
        parts = date_str.strip().split(",")
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
    date_str_full = f"{date_part}, {time_part}"
    meta_date = datetime.strptime(date_str_full, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")
    phone = f"+7 ({__import__('random').randint(900, 999)}) {__import__('random').randint(100, 999)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}"
    data = bytearray(_donor.read_bytes())
    try:
        out = patch_from_values(
            data,
            _donor,
            date_str=date_str_full,
            payer=payer or "Иван Иванович И.",
            recipient=recipient or "Иван Иванович И.",
            phone=phone,
            bank=bank or "Т-Банк",
            amount=amount,
            operation_id=None,
            keep_metadata=True,
            account_last4=account if account and re.match(r"^\d{4}$", account) else None,
        )
    except ValueError as e:
        return False, None, str(e)
    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)
    _change_one_char_in_id(out_arr)
    return True, bytes(out_arr), ""


def run_sbp_generate(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация чека ВТБ СБП из встроенного шаблона (перевод не нужен)."""
    try:
        pdf_bytes = generate_vtb_sbp(
            payer=payer,
            recipient=recipient,
            amount=amount,
            date_str=date_str,
            bank=bank,
            account=account,
            operation_id=operation_id,
        )
        return True, pdf_bytes, ""
    except (ValueError, FileNotFoundError) as e:
        err = str(e)
    except Exception as e:
        err = str(e)
    ok, pdf_bytes, err2 = run_sbp_generate_15_03(payer, recipient, amount, date_str, bank, account)
    if ok and pdf_bytes:
        return True, pdf_bytes, ""
    ok, pdf_bytes, err2 = run_sbp_generate_verified(payer, recipient, amount, date_str, bank, account)
    if ok and pdf_bytes:
        return True, pdf_bytes, ""
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, None, err or err2 or f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(_BOT_DIR)) as tf:
        out_path = tf.name
    try:
        if not date_str or date_str.strip().lower() in ("now", "сейчас"):
            dt = datetime.now()
            date_part = dt.strftime("%d.%m.%Y")
            time_part = dt.strftime("%H:%M")
        else:
            # "13.03.2026, 20:04" или "13.03.2026"
            parts = date_str.strip().split(",")
            date_part = parts[0].strip()
            time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
        # 16-03-26 — актуальный шаблон ID (15-03-26 исчерпан: все 9 decimal-слотов израсходованы)
        # Каждый шаблон даёт ровно 9 уникальных decimal-ID (pos0, incs 1..15 дающих 0-9)
        _template_id = str(_BOT_DIR / "база_чеков" / "vtb" / "СБП" / "16-03-26_00-00.pdf")
        _template_exists = Path(_template_id).exists()
        cmd = [
            os.sys.executable, str(_ADD_GLYPHS_SCRIPT),
            "--replace", "--hybrid-safe", "--auto-base",
            "--payer", payer or "Иван Иванович И.",
            "--recipient", recipient or "Иван Иванович И.",
            "--bank", bank or "Т-Банк",
            "--amount", str(amount),
            "--date", date_part,
            "--time", time_part,
            "-o", out_path,
        ]
        if _template_exists:
            cmd.extend(["--id-from", _template_id])
        if account and re.match(r"^\d{4}$", account):
            cmd.extend(["--account", account])
        if operation_id:
            cmd.extend(["--operation-id", operation_id])
        proc = subprocess.run(cmd, cwd=str(_BOT_DIR), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            return False, None, err or f"Код выхода: {proc.returncode}"
        if not Path(out_path).exists():
            return False, None, "PDF не создан"
        pdf_bytes = Path(out_path).read_bytes()
        return True, pdf_bytes, ""
    except subprocess.TimeoutExpired:
        return False, None, "Таймаут генерации (60 сек)"
    except Exception as e:
        return False, None, str(e)
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


# ── TBank check helpers ───────────────────────────────────────────────

TBANK_FIELD_LABELS = {
    "datetime": "Дата и время",
    "amount_bold": "Сумма (жирная, с пробелом перед ₽)",
    "amount_small": "Сумма (мелкая, с пробелом перед ₽)",
    "type_label": "Тип перевода",
    "status": "Статус",
    "commission": "Комиссия",
    "sender": "Отправитель",
    "phone": "Телефон получателя",
    "receiver": "Получатель",
    "bank": "Банк получателя",
    "account": "Счёт списания",
    "ident": "Идентификатор",
    "card_to": "Карта получателя",
    "credited_amt": "Сумма зачисления",
}


def _tbank_send_next_field(token: str, chat_id: int, state: dict) -> None:
    """Send the next field prompt in TBank full-field mode."""
    idx = state["tbank_field_idx"]
    keys = state["tbank_field_keys"]
    if idx >= len(keys):
        return
    key = keys[idx]
    current_val = state["tbank_fields"].get(key, "—")
    label = TBANK_FIELD_LABELS.get(key, key)
    tg_request_fn = state.get("_tg_req")

    msg = (
        f"🅃 ТБанк — все поля ({idx + 1}/{len(keys)})\n\n"
        f"**{label}**\n"
        f"Текущее: {current_val}\n\n"
        "Введите новое значение или нажмите «Оставить»:"
    )
    kb = [[{"text": "✅ Оставить", "callback_data": "tbank_keep_field"}]]

    tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": msg,
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


def _run_tbank_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Execute the full TBank patch with all collected changes."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        changes = state.get("tbank_changes", {})
        if not changes:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "ℹ️ Нет изменений. Чек не изменён."})
            return
        rtype = state.get("tbank_type", "sbp")
        pdf_bytes = tbank_patch_all_fields(state["file_path"], changes, rtype)
        fname = state.get("file_name", "receipt.pdf")
        out_name = f"tbank_{rtype}_{fname}"

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "tbank")
    except ValueError as e:
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ {e}\n\nПопробуйте другое значение или вернитесь в главное меню.",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "🔄 Попробовать снова", "callback_data": "bank_tbank_full"}],
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ]}),
        })
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
    finally:
        if uid in USER_STATE:
            if "file_path" in USER_STATE[uid]:
                try:
                    os.unlink(USER_STATE[uid]["file_path"])
                except OSError:
                    pass
            del USER_STATE[uid]


def _run_tbank_amount_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Execute the TBank amount-only patch."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        new_amount = state.get("tbank_new_amount", 0)
        rtype = state.get("tbank_type", "sbp")
        pdf_bytes = tbank_patch_amount(state["file_path"], new_amount, rtype)
        fname = state.get("file_name", "receipt.pdf")
        out_name = f"tbank_{rtype}_{fname}"

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "tbank")

        # Оставляем оригинал живым — предлагаем изменить снова или завершить
        from tbank_check_service import _format_amount_str
        amt_disp = _format_amount_str(new_amount)
        state["awaiting"] = None
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Готово! Сумма изменена на {amt_disp} ₽.\n\nХочешь изменить ещё раз?",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "🔄 Изменить сумму снова", "callback_data": "tbank_amount_again"}],
                    [{"text": "✅ Готово, закрыть", "callback_data": "tbank_done"}],
                ],
            }),
        })
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
        if uid in USER_STATE:
            if "file_path" in USER_STATE[uid]:
                try:
                    os.unlink(USER_STATE[uid]["file_path"])
                except OSError:
                    pass
            del USER_STATE[uid]


def _tbank_f2_digit_hint(pdf_path: str) -> str:
    """Подсказка: какие цифры реально отрисуются в шрифте F2 (жирная сумма) в этом PDF."""
    p = Path(pdf_path)
    if not p.exists():
        return ""
    try:
        from tbank_check_service import get_renderable_chars
        from tbank_cmap import MEDIUM_UNI_TO_CID
        pdf_bytes = p.read_bytes()
        f2_renderable = get_renderable_chars(pdf_bytes, "medium")
        if f2_renderable is None:
            return ""
        missing = [str(d) for d in range(10) if MEDIUM_UNI_TO_CID.get(0x30 + d) not in f2_renderable]
    except Exception:
        return ""
    if not missing:
        return ""
    return (
        "⚠️ В жирной сумме («Итого») этого чека отсутствуют цифры: "
        + ", ".join(missing)
        + ".\nОни будут невидимы. Загрузи обогащённый чек или используй /check для генерации с нуля.\n\n"
    )


def _tbank_merge_account_last4(current: str, digits: str) -> str:
    """Подставить последние 4 цифры в строку счёта (маска ****1234)."""
    d = re.sub(r"\D", "", (digits or "").strip())
    if len(d) != 4:
        raise ValueError("Введите ровно 4 цифры (последние разряды счёта).")
    cur = (current or "").strip()
    if not cur:
        return f"****{d}"
    if re.search(r"\d{4}\s*$", cur):
        return re.sub(r"\d{4}(\s*)$", d + r"\1", cur)
    return cur + d


def _tbank_format_credited_foreign(val: float) -> str:
    """Формат суммы зачисления в валюте для поля credited_amt."""
    if val != val or val in (float("inf"), float("-inf")):  # NaN
        raise ValueError("Некорректный расчёт зачисления.")
    s = f"{val:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _run_tbank_amount_account_patch(
    token: str, uid: int, chat_id: int, state: dict, tg_req
) -> None:
    """Патч суммы (жирная + мелкая) и последних 4 цифр счёта."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        amt = float(state.get("tbank_new_amount", 0))
        rtype = state.get("tbank_type", "sbp")
        last4 = state.get("tbank_account_last4", "")
        fields = state.get("tbank_fields") or {}
        current_acct = fields.get("account", "")
        new_acct = _tbank_merge_account_last4(current_acct, last4)

        from tbank_check_service import _format_amount_str

        amount_str = _format_amount_str(amt) + " "
        changes = {
            "amount_bold": amount_str,
            "amount_small": amount_str,
            "account": new_acct,
        }
        pdf_bytes = tbank_patch_all_fields(state["file_path"], changes, rtype)
        fname = state.get("file_name", "receipt.pdf")
        out_name = f"tbank_{rtype}_{fname}"

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "tbank")
    except ValueError as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
    finally:
        if uid in USER_STATE:
            if "file_path" in USER_STATE[uid]:
                try:
                    os.unlink(USER_STATE[uid]["file_path"])
                except OSError:
                    pass
            del USER_STATE[uid]


def _run_tbank_transgran_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Трансгран Т-Банк: сумма RUB + курс → зачисление в валюте."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        amt = float(state["tbank_tg_amount"])
        rate = float(state["tbank_tg_rate"])
        credited = amt * rate
        credited_str = _tbank_format_credited_foreign(credited)

        from tbank_check_service import _format_amount_str

        amount_str = _format_amount_str(amt) + " "
        changes = {
            "amount_bold": amount_str,
            "amount_small": amount_str,
            "credited_amt": credited_str,
        }
        pdf_bytes = tbank_patch_all_fields(
            state["file_path"], changes, "transgran"
        )
        fname = state.get("file_name", "receipt.pdf")
        out_name = f"tbank_transgran_{fname}"

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "tbank")
        try:
            am_int = int(round(amt))
        except (TypeError, ValueError):
            am_int = 0
        tg_req(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✅ Сумма: {format_amount_display(am_int)} ₽\n"
                    f"Курс (ед. валюты за 1 ₽): {rate}\n"
                    f"Зачисление: {credited_str}"
                ),
            },
        )
    except ValueError as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
    finally:
        if uid in USER_STATE:
            if "file_path" in USER_STATE[uid]:
                try:
                    os.unlink(USER_STATE[uid]["file_path"])
                except OSError:
                    pass
            del USER_STATE[uid]


def _tbank_send_scratch_field(token: str, chat_id: int, state: dict) -> None:
    """Send the next field prompt for TBank scratch generation."""
    idx = state["tbank_scratch_idx"]
    fields = state["tbank_scratch_fields"]
    if idx >= len(fields):
        return
    field = fields[idx]
    label = TBANK_FIELD_LABELS.get(field["key"], field.get("label", field["key"]))
    default = field.get("default", "—")
    msg = (
        f"🅃 ТБанк — генерация ({idx + 1}/{len(fields)})\n\n"
        f"**{label}**\n"
        f"По умолчанию: {default}\n\n"
        "Введите значение или нажмите «Оставить»:"
    )
    kb = [[{"text": "✅ Оставить", "callback_data": "tbank_scratch_keep"}]]
    tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": msg,
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


def _run_tbank_scratch_gen(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Execute TBank scratch generation with collected values."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Генерирую чек..."})
    try:
        rtype = state.get("tbank_type", "sbp")
        values = state.get("tbank_scratch_values", {})
        fields = state.get("tbank_scratch_fields", [])
        for field in fields:
            if field["key"] not in values and field.get("default"):
                values[field["key"]] = field["default"]

        pdf_bytes = tbank_generate_scratch(values, rtype)
        out_name = f"tbank_{rtype}_scratch.pdf"

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "tbank")
    except ValueError as e:
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ {e}\n\nПопробуйте другое значение или вернитесь в главное меню.",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ]}),
        })
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
    finally:
        if uid in USER_STATE:
            del USER_STATE[uid]


def _alfa_send_scratch_field(token: str, chat_id: int, state: dict) -> None:
    """Send the next field prompt for Alfa scratch generation."""
    idx = state["alfa_scratch_idx"]
    fields = state["alfa_scratch_fields"]
    if idx >= len(fields):
        return
    field = fields[idx]
    label = field.get("label", field["key"])
    default = field.get("default", "—") or "—"
    prompt = field.get("prompt", label)
    msg = (
        f"🏦 Альфа — генерация ({idx + 1}/{len(fields)})\n\n"
        f"**{prompt}**\n"
        f"По умолчанию: {default}\n\n"
        "Введите значение или нажмите «Оставить»:"
    )
    kb = [
        [{"text": "✅ Оставить", "callback_data": "alfa_scratch_keep"}],
        [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
    ]
    tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": msg,
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


def _run_alfa_scratch_gen(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Execute Alfa scratch generation with collected values."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Генерирую чек Альфа..."})
    try:
        rtype = state.get("alfa_scratch_type", "sbp")
        values = state.get("alfa_scratch_values", {})
        fields = state.get("alfa_scratch_fields", [])
        for field in fields:
            if field["key"] not in values and field.get("default"):
                values[field["key"]] = field["default"]

        pdf_bytes = generate_alfa_scratch(rtype, values)
        out_name = f"alfa_{rtype}_scratch.pdf"

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp = tf.name

        with open(tmp, "rb") as f:
            boundary = "----FormBoundary" + os.urandom(8).hex()
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{out_name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode() + f.read() + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                f"{BASE}{token}/sendDocument",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(req, timeout=30)

        os.unlink(tmp)
        stats_record_pdf(uid, "receipt", "alfa_scratch")
    except ValueError as e:
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ {e}\n\nПопробуйте другое значение или вернитесь в главное меню.",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ]}),
        })
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
    finally:
        if uid in USER_STATE:
            del USER_STATE[uid]


def _vtb_full_validate_text(text: str, field_name: str) -> tuple[str | None, str | None]:
    """Валидация текста. Вернуть (None, None) если ок; (err_msg, suggested) при ошибке.
    suggested — вариант с заменой ё→е, ‑→- (если применим)."""
    bad = get_unsupported_chars(text)
    if not bad:
        return None, None
    suggested = suggest_replacement(text)
    return format_unsupported_error(bad, field_name), suggested


def _run_vtb_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч ВТБ и отправить PDF."""
    inp = state["file_path"]
    amount_from = state.get("vtb_amount_from") or state.get("vtb_amount")
    amount_to = state.get("vtb_amount")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    required_chars = chars_from_text_fields(
        state.get("vtb_payer") or "",
        state.get("vtb_recipient") or "",
        state.get("vtb_phone") or "",
        state.get("vtb_bank") or "",
    )
    if required_chars and not receipt_supports_chars(inp, required_chars):
        missing = get_missing_chars_in_receipt(inp, required_chars)
        miss_txt = f"Не хватает букв: «{'» «'.join(sorted(missing))}»" if missing else ""
        send(f"❌ Имя недоступно. В вашем чеке отсутствуют нужные буквы.\n{miss_txt}\nПопробуйте «Сгенерировать» — бот найдёт чек с подходящими символами.")
        return

    try:
        data = bytearray(Path(inp).read_bytes())
        # Для ВТБ сначала используем точное выравнивание суммы по wall из patch_amount_only.
        try:
            data = bytearray(patch_amount_only(data, Path(inp), amount_to))
        except Exception:
            ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
            if not ok_sum or new_data is None:
                send(f"❌ Сумма не найдена.\nПроверь: в чеке должна быть сумма {format_amount_display(amount_from)} ₽. {err_sum or ''}")
                return
            data = bytearray(new_data)
        # Остальные поля (дата, ФИО, телефон, банк) — amount=None, сумму уже поменяли
        out_bytes = patch_from_values(
            data,
            Path(inp),
            date_str=state.get("vtb_date", "now"),
            payer=state.get("vtb_payer"),
            recipient=state.get("vtb_recipient"),
            phone=state.get("vtb_phone"),
            bank=state.get("vtb_bank"),
            amount=None,
            account=None,
        )
        out_name = Path(state["file_name"]).stem + f"_{format_amount_display(state['vtb_amount']).replace(' ', '_')}.pdf"
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]
        am_from = state.get("vtb_amount_from") or state.get("vtb_amount")
        caption = f"✅ Готово: {format_amount_display(am_from)} ₽ → {format_amount_display(state['vtb_amount'])} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        stats_record_pdf(uid, "receipt", "vtb")
        _send_main_menu_button(token, chat_id, tg_req)
    except ValueError as e:
        send(f"❌ Ошибка: {e}")
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _vtb_full_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Отправить следующий шаг. Для payer/recipient/phone/bank — с кнопкой «Оставить текущим»."""
    KEEP_PROMPTS = {
        "vtb_payer": ("3️⃣ Плательщик (ФИО):", "vtb_keep_payer"),
        "vtb_recipient": ("4️⃣ Получатель (ФИО):", "vtb_keep_recipient"),
        "vtb_phone": ("5️⃣ Телефон получателя:", "vtb_keep_phone"),
        "vtb_bank": ("6️⃣ Банк получателя:", "vtb_keep_bank"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _gen_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «Сгенерировать»."""
    KEEP_PROMPTS = {
        "gen_payer": ("1️⃣ Плательщик (ФИО):", "gen_keep_payer"),
        "gen_recipient": ("2️⃣ Получатель (ФИО):", "gen_keep_recipient"),
        "gen_phone": ("5️⃣ Телефон получателя:", "gen_keep_phone"),
        "gen_bank": ("6️⃣ Банк получателя:", "gen_keep_bank"),
        "gen_account": ("6️⃣ Номер счёта (4 цифры, напр. 9426):", "gen_keep_account"),
        "gen_operation_id": ("7️⃣ ID операции (B606...):", "gen_keep_opid"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить пустым", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _run_alfa_karta_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать квитанцию Альфа «карта на карту» из шаблона шаблоны/alfa_karta.pdf."""
    from alfa_karta_service import patch_alfa_karta, template_exists

    if not template_exists():
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Шаблон Альфа-карта не найден (шаблоны/alfa_karta.pdf)."})
        if uid in USER_STATE:
            del USER_STATE[uid]
        _send_main_menu_button(token, chat_id, tg_req)
        return

    card = state.get("ak_new_card", "")
    amount = int(state.get("ak_amount", 0))
    comm = state.get("ak_commission")
    comm_show_00 = bool(state.get("ak_commission_show_00"))
    ak_formed_line = state.get("ak_formed_line")
    ak_transfer_line = state.get("ak_transfer_line")
    if uid in USER_STATE:
        del USER_STATE[uid]

    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Генерирую квитанцию..."})
    try:
        ok, err, pdf_bytes = patch_alfa_karta(
            new_recipient_card=card,
            new_amount_rub=amount,
            new_commission_rub=comm,
            commission_show_zero_kopeks=comm_show_00,
            new_formed_at=ak_formed_line,
            new_transfer_at=ak_transfer_line,
        )
    except Exception as e:
        ok, err, pdf_bytes = False, str(e), None

    if not ok or not pdf_bytes:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err or 'патч PDF'}"})
        _send_main_menu_button(token, chat_id, tg_req)
        return

    try:
        _pdf_ba = bytearray(pdf_bytes)
        _mutate_doc_id(_pdf_ba)
        pdf_bytes = bytes(_pdf_ba)
        out_name = f"alfa_karta_{format_amount_display(amount).replace(' ', '_')}.pdf"
        cap = f"✅ Альфа-карта: {format_amount_display(amount)} ₽"
        if comm is not None:
            _c = int(round(float(comm) * 100))
            _w, _f = _c // 100, _c % 100
            if _f:
                cap += f", комиссия {_w},{_f:02d} ₽"
            elif comm_show_00:
                cap += f", комиссия {_w},00 ₽"
            else:
                cap += f", комиссия {_w} ₽"
        else:
            cap += " (комиссия как в шаблоне)"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": cap}, files={"document": (out_name, pdf_bytes)})
        stats_record_pdf(uid, "receipt", "alfa")
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Не удалось отправить PDF: {e}"})
    _send_main_menu_button(token, chat_id, tg_req)


def _handle_alfa_karta_input(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Пошаговый ввод: карта → сумма → комиссия → время «Сформирована» → время перевода."""
    from alfa_karta_service import (
        commission_user_typed_zero_kopeks,
        parse_alfa_karta_formed_input,
        parse_alfa_karta_transfer_input,
    )

    state = USER_STATE.get(uid)
    if not state:
        return
    aw = state.get("awaiting", "")
    t = text.strip()

    if aw == "ak_recipient_card":
        card = re.sub(r"\s+", "", t).replace("\u00a0", "")
        if len(card) < 12 or "*" not in card:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите маску карты, например: 220220******5047"})
            return
        state["ak_new_card"] = card
        state["awaiting"] = "ak_amount"
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "💰 Сумма перевода (руб., целое число, например 10000):"})
        return

    if aw == "ak_amount":
        nums = re.findall(r"\d+", t)
        if not nums:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите сумму числом."})
            return
        amount = int("".join(nums))
        if amount <= 0:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Сумма должна быть больше 0."})
            return
        state["ak_amount"] = amount
        state["awaiting"] = "ak_commission"
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "💳 Комиссия (руб.: 62.79, 150 или 244,00 — если указали ,00, в PDF будет ,00). Отправьте — чтобы оставить как в шаблоне:",
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "— Как в шаблоне", "callback_data": "ak_keep_commission"}]]}),
        })
        return

    if aw == "ak_commission":
        if t in ("-", "—", "пропустить", "skip"):
            state["ak_commission"] = None
            state.pop("ak_commission_show_00", None)
        else:
            try:
                state["ak_commission"] = float(t.replace(",", ".").replace(" ", ""))
            except ValueError:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Неверное число. Пример: 62.79"})
                return
            state["ak_commission_show_00"] = commission_user_typed_zero_kopeks(t)
        state["awaiting"] = "ak_formed"
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                "🕐 Строка «Сформирована» (без секунд).\n"
                "Формат: `ДД.ММ.ГГГГ ЧЧ:ММ` — мск подставится само.\n"
                "Отправьте «—» чтобы оставить как в шаблоне."
            ),
            "parse_mode": "Markdown",
        })
        return

    if aw == "ak_formed":
        try:
            state["ak_formed_line"] = parse_alfa_karta_formed_input(t)
        except ValueError as e:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
            return
        state["awaiting"] = "ak_transfer"
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                "🕑 Дата и время перевода (со секундами).\n"
                "Формат: `ДД.ММ.ГГГГ ЧЧ:ММ:СС`\n"
                "«—» — оставить как в шаблоне."
            ),
            "parse_mode": "Markdown",
        })
        return

    if aw == "ak_transfer":
        try:
            state["ak_transfer_line"] = parse_alfa_karta_transfer_input(t)
        except ValueError as e:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
            return
        _run_alfa_karta_generate(token, uid, chat_id, state, tg_req)
        return


def _run_gen_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать чек ВТБ→ВТБ из встроенного шаблона (перевод не нужен)."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    amount_to = state["gen_amount"]
    try:
        out_bytes = generate_vtb_vtb(
            payer=state.get("gen_payer") or "Иван Иванович И.",
            recipient=state.get("gen_recipient") or "Иван Иванович И.",
            amount=amount_to,
            date_str=state.get("gen_date", "now"),
            bank=state.get("gen_bank") or "ВТБ",
            phone=state.get("gen_phone"),
            operation_id=state.get("gen_operation_id"),
        )
        out_name = f"vtb_vtb_{format_amount_display(amount_to).replace(' ', '_')}.pdf"
        del USER_STATE[uid]
        caption = f"✅ Сгенерировано: {format_amount_display(amount_to)} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        stats_record_pdf(uid, "receipt", "vtb")
        op_id = state.get("gen_operation_id") or get_operation_id_from_pdf(out_bytes)
        if op_id:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"📋 ID операции (скопируйте при необходимости):\n{op_id}"})
        _send_main_menu_button(token, chat_id, tg_req)
    except ValueError as e:
        send(f"❌ {e}")
        if uid in USER_STATE:
            del USER_STATE[uid]
    except FileNotFoundError as e:
        send(f"❌ {e}")
        if uid in USER_STATE:
            del USER_STATE[uid]
    except Exception as e:
        send(f"❌ Ошибка: {e}")
        if uid in USER_STATE and state.get("gen_bank_type"):
            del USER_STATE[uid]


def _do_gen_fio_check_and_continue(token: str, uid: int, chat_id: int, state: dict, send, next_step) -> None:
    """Проверка ФИО (run_check_caps) и переход к настройке (gen_amount)."""
    payer = state.get("gen_payer") or ""
    recipient = state.get("gen_recipient") or ""
    send("⏳ Проверяю буквы...")
    ok, output = run_check_caps(payer, recipient)
    send("📋 Результат проверки ФИО:\n\n" + (output or "(пусто)"))
    next_step("gen_amount", "Настройка.\n\n1️⃣ Сумма (например 10000):")


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После банка: для СБП — gen_account, для ВТБ на ВТБ — gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Или оставить из шаблона:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")


def _run_sbp_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Генерация чека СБП через add_glyphs_to_13_03.py."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})
    ok, pdf_bytes, err = run_sbp_generate(
        payer=state.get("gen_payer") or "Иван Иванович И.",
        recipient=state.get("gen_recipient") or "Иван Иванович И.",
        amount=state["gen_amount"],
        date_str=state.get("gen_date", "now"),
        bank=state.get("gen_bank") or "Т-Банк",
        account=state.get("gen_account"),
        operation_id=state.get("gen_operation_id"),
    )
    if not ok or not pdf_bytes:
        send(f"❌ Ошибка генерации: {err or 'PDF не создан'}")
        if uid in USER_STATE:
            del USER_STATE[uid]
        return
    out_name = f"чек_{format_amount_display(state['gen_amount']).replace(' ', '_')}.pdf"
    del USER_STATE[uid]
    caption = f"✅ Сгенерировано: {format_amount_display(state['gen_amount'])} ₽"
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
    stats_record_pdf(uid, "receipt", "vtb")
    _send_main_menu_button(token, chat_id, tg_req)


def _handle_gen_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода режима «Сгенерировать»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _gen_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting in ("gen_type", "gen_subtype", "gen_bank") and state.get("gen_bank_type") is None:
        if uid in USER_STATE:
            del USER_STATE[uid]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "Выберите тип перевода и банк кнопками выше или вернитесь в главное меню.",
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🏠 Главное меню", "callback_data": "main_back"}]]}),
        })
        return

    if awaiting == "gen_amount":
        nums = re.findall(r"\d+", text.strip())
        if not nums:
            send("❌ Введите сумму (например: 50000) или две: с какой на какую (10 1000)")
            return
        if len(nums) == 1:
            state["gen_amount"] = int(nums[0])
            state["gen_amount_from"] = None
        else:
            state["gen_amount_from"] = int(nums[0])
            state["gen_amount"] = int("".join(nums[1:]))
        if state["gen_amount"] <= 0:
            send("❌ Сумма должна быть больше 0.")
            return
        next_step("gen_date", "2️⃣ Дата (дд.мм.гггг чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "gen_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["gen_date"] = "now"
        else:
            state["gen_date"] = text.strip()
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            next_step("gen_phone", "5️⃣ Телефон получателя:")
        else:
            next_step("gen_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "gen_payer":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_payer"] = None
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["gen_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["gen_payer"] = t
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "gen_recipient":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_recipient"] = None
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["gen_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                if state.get("gen_vtb_subtype") == "vtb_sbp":
                    _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
                else:
                    next_step("gen_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["gen_recipient"] = t
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "gen_operation_id":
        t = text.strip().replace(" ", "").replace("\n", "")
        if not t or t.lower() in ("пропустить", "-", "=", "оставить", "оставить пустым"):
            state["gen_operation_id"] = None
            send("⏳ Генерирую чек...")
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _run_sbp_generate(token, uid, chat_id, state, tg_req)
            else:
                _run_gen_patch(token, uid, chat_id, state, tg_req)
            return
        if not re.match(r"^[AB]606[\dA-Fa-f]{15,30}$", t):
            send("❌ Формат: B606... или A606... (цифры и A-F, 20-32 символа)")
            return
        state["gen_operation_id"] = t
        send("⏳ Генерирую чек...")
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            _run_sbp_generate(token, uid, chat_id, state, tg_req)
        else:
            _run_gen_patch(token, uid, chat_id, state, tg_req)

    elif awaiting == "gen_phone":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_phone"] = None
            next_step("gen_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["gen_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("gen_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["gen_phone"] = t
            next_step("gen_bank", "6️⃣ Банк получателя:")

    elif awaiting == "gen_bank":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_bank"] = None
            _gen_next_after_bank(state, next_step)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["gen_bank"] = suggested
                send(f"✅ Применена замена: {suggested}")
                _gen_next_after_bank(state, next_step)
            else:
                send(err)
                return
        else:
            state["gen_bank"] = t
            _gen_next_after_bank(state, next_step)

    elif awaiting == "gen_account":
        t = text.strip()
        if t.lower() in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_account"] = None
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
            return
        if re.match(r"^\d{4}$", t):
            state["gen_account"] = t
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
        else:
            send("❌ Номер счёта — 4 цифры (например 9426)")


# ── Выписка: пошаговый визард ──────────────────────────────────────────────

def _sw_send_step(token: str, chat_id: int, state: dict, tg_req) -> None:
    """Send the current wizard step to the user."""
    from statement_wizard import (
        get_current_field, format_step_message, get_step_keyboard,
        format_preview, get_preview_keyboard,
    )
    field = get_current_field(state)
    if field is None:
        txt = format_preview(state)
        kb = get_preview_keyboard("sw")
        tg_req(token, "sendMessage", {
            "chat_id": chat_id, "text": txt,
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return
    current_val = state.get("current_values", {}).get(field["key"])
    msg = format_step_message(field, current_val, changes=state.get("changes", {}))
    kb = get_step_keyboard(field, current_val, "sw")

    # Auto-fill button for operation description field
    fkey = field["key"]
    if fkey.endswith("_описание"):
        fkey_parts = fkey.split("_")  # ["op", N, "описание"]
        if len(fkey_parts) >= 2:
            op_idx = fkey_parts[1]
            cur = state.get("current_values", {})
            chg = state.get("changes", {})
            old_op_num = cur.get(f"op_{op_idx}_номер_операции", "")
            new_op_num = chg.get(f"op_{op_idx}_номер_операции", old_op_num)
            old_phone = cur.get(f"op_{op_idx}_телефон", "")
            new_phone = chg.get(f"op_{op_idx}_телефон", old_phone)
            if current_val and (old_op_num != new_op_num or old_phone != new_phone):
                flat = current_val.replace("\n", "")
                auto = flat.replace(old_op_num, new_op_num) if old_op_num else flat
                old_phone_flat = old_phone.replace("\n", "")
                new_phone_flat = new_phone.replace("\n", "")
                if old_phone_flat:
                    auto = auto.replace(old_phone_flat, new_phone_flat)
                # Re-insert line break: last 2 digits of phone go to next line (PDF layout)
                if new_phone_flat and len(new_phone_flat) >= 2 and new_phone_flat in auto:
                    auto = auto.replace(
                        new_phone_flat,
                        new_phone_flat[:-2] + "\n" + new_phone_flat[-2:],
                    )
                if auto != flat:
                    state["_auto_desc"] = auto
                    kb.insert(0, [{"text": "✅ Подставить автозначение", "callback_data": "sw_auto_desc"}])

    tg_req(token, "sendMessage", {
        "chat_id": chat_id, "text": msg,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


def _sw_handle_text(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Handle text input in wizard mode: apply value to current field, advance."""
    from statement_wizard import (
        get_current_field, advance_field, validate_input, validate_account_number,
    )
    state = USER_STATE.get(uid)
    if not state:
        return
    field = get_current_field(state)
    if field is None:
        if "=" in text:
            parts = text.strip().split("\n")
            applied = []
            for line in parts:
                if "=" not in line:
                    continue
                old, _, new = line.partition("=")
                old, new = old.strip(), new.strip()
                if old and new:
                    state.setdefault("raw_replacements", []).append((old, new))
                    applied.append(f"{old} → {new}")
            if applied:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Добавлены замены:\n" + "\n".join(applied)})
        return
    value = text.strip()
    if not value:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Введите значение или нажмите кнопку."})
        return

    available = None
    pdf_path = state.get("file_path")
    if pdf_path:
        try:
            from vyписка_service import get_available_chars
            available = get_available_chars(Path(pdf_path))
        except (ImportError, Exception):
            pass
    if not available and state.get("bank") == "alfa":
        try:
            from alfa_statement_service import get_available_chars as alfa_chars
            available = alfa_chars()
        except (ImportError, Exception):
            pass

    if state.get("bank") == "tbank":
        bad_chars = tbank_get_unsupported(value, "regular")
        if bad_chars:
            tg_req(token, "sendMessage", {
                "chat_id": chat_id,
                "text": tbank_format_unsupported(bad_chars) + "\nПопробуйте другое значение или нажмите «Пропустить».",
                "reply_markup": json.dumps({"inline_keyboard": [
                    [{"text": "⏭ Пропустить", "callback_data": "sw_skip"}],
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ]}),
            })
            return

    ok, warning = validate_input(field, value, available)
    if not ok:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": warning + "\nПопробуйте ещё раз или нажмите «Пропустить»."})
        return

    if field.get("suggest_fio") and state.get("bank") == "alfa" and "\n" not in value:
        parts = value.split()
        if len(parts) >= 3:
            value = f"{parts[0]} {parts[1]}\n{' '.join(parts[2:])}"
    state.setdefault("changes", {})[field["key"]] = value
    advance_field(state)
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_handle_callback(token: str, uid: int, q: dict, tg_req) -> None:
    """Handle callback queries for the statement wizard (sw_*)."""
    from statement_wizard import (
        get_current_field, advance_field, suggest_fio, suggest_address,
        jump_to_block, format_preview, get_preview_keyboard,
    )
    data = q["data"]
    chat_id = q["message"]["chat"]["id"]
    state = USER_STATE.get(uid)
    if not state:
        tg_req(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
        return

    if data == "sw_keep" or data == "sw_skip":
        advance_field(state)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_auto_desc":
        auto = state.pop("_auto_desc", None)
        field = get_current_field(state)
        if field and auto:
            state.setdefault("changes", {})[field["key"]] = auto
        advance_field(state)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_suggest_fio":
        field = get_current_field(state)
        if not field:
            return
        current = state.get("current_values", {}).get(field["key"], "")
        is_multiline = state.get("bank") == "alfa"
        suggestions = suggest_fio(current, multiline=is_multiline)
        if not suggestions:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Не удалось подобрать ФИО. Введите вручную."})
            return
        state["_suggestions"] = suggestions
        kb = []
        for i, s in enumerate(suggestions):
            display = s.replace("\n", " ")
            kb.append([{"text": f"✅ {display}", "callback_data": f"sw_pick_{i}"}])
        kb.append([{"text": "✏️ Ввести вручную", "callback_data": "sw_manual"}])
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"💡 Предложения ФИО:\n\nТекущее: {current.replace(chr(10), ' ')}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return

    if data == "sw_suggest_addr":
        field = get_current_field(state)
        if not field:
            return
        current_addr = state.get("current_values", {}).get(field["key"], "")
        bank_name = state.get("bank", "alfa")
        suggestions = suggest_address(current_addr, bank=bank_name)
        if not suggestions:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Не удалось подобрать адрес. Введите вручную."})
            return
        state["_suggestions"] = suggestions
        kb = []
        for i, s in enumerate(suggestions):
            display = s.replace("\n", ", ")[:60]
            kb.append([{"text": f"✅ {display}", "callback_data": f"sw_pick_{i}"}])
        kb.append([{"text": "✏️ Ввести вручную", "callback_data": "sw_manual"}])
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"💡 Предложения адреса:\n\nТекущий: {current_addr}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return

    if data.startswith("sw_pick_"):
        idx_str = data[8:]
        suggestions = state.get("_suggestions", [])
        try:
            idx = int(idx_str)
            picked = suggestions[idx] if idx < len(suggestions) else idx_str
        except (ValueError, IndexError):
            picked = idx_str
        field = get_current_field(state)
        if field:
            state.setdefault("changes", {})[field["key"]] = picked
        state.pop("_suggestions", None)
        advance_field(state)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_manual":
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "✏️ Введите значение:"})
        return

    if data.startswith("sw_edit_b"):
        block_num = int(data[-1])
        jump_to_block(state, block_num)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_generate":
        _sw_generate(token, uid, chat_id, state, tg_req)
        return


def _parse_float_safe(val_str) -> float:
    s = str(val_str).replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        if s.rindex(",") < s.rindex("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _apply_alfa_block3_calc(state, changes, current, out_path,
                            sum_operations_expense, sum_operations_income_alfa,
                            calc_alfa_block3, format_amount_rur, patch_replacements):
    """Apply Block 3 auto-calculation for Alfa statements."""
    from statement_wizard import _parse_amount
    expenses_str = changes.get("расходы")
    income_str = changes.get("поступления")
    if expenses_str:
        expenses = _parse_amount(expenses_str)
    else:
        expenses = sum_operations_expense(state)
    if income_str:
        income = _parse_amount(income_str)
    else:
        income = sum_operations_income_alfa(state)
    bal = _parse_float_safe(changes.get("текущий_баланс", current.get("текущий_баланс", "0")))
    calc = calc_alfa_block3(bal, expenses, income)
    bal_pairs = []
    seen_bal: set[str] = set()
    for calc_key in ("входящий_остаток", "расходы", "поступления", "исходящий_остаток", "платежный_лимит", "текущий_баланс"):
        old_v = current.get(calc_key)
        new_v = format_amount_rur(calc[calc_key])
        if old_v and old_v != new_v:
            pair_key = old_v + " RUR"
            if pair_key not in seen_bal:
                seen_bal.add(pair_key)
                bal_pairs.append((pair_key, new_v + " RUR"))
    if bal_pairs:
        patch_replacements(Path(out_path), Path(out_path), bal_pairs)


def _finalize_statement_pdf_bytes(
    pdf_bytes: bytes,
    *,
    formation_date_ddmmyyyy: str | None = None,
) -> bytes:
    """После CID-патча: ModDate + минимальная правка /ID (как task2 / CHECK_VERIFICATION_RULES).

    Полная подмена обоих /ID на MD5 ломала ту же проверку, что и у чеков.
    """
    import hashlib
    import tempfile
    from pathlib import Path

    from patch_id import patch_document_id_one_nibble, patch_moddate

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        path = Path(tf.name)
    try:
        if formation_date_ddmmyyyy and re.match(
            r"^\d{2}\.\d{2}\.\d{4}$", formation_date_ddmmyyyy.strip()
        ):
            patch_moddate(path, formation_date_ddmmyyyy.strip())
        pos = hashlib.md5(pdf_bytes).digest()[0] % 32
        patch_document_id_one_nibble(path, which=2, pos_from_end=min(pos, 31))
        return path.read_bytes()
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _sw_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Generate statement PDF from wizard state."""
    from statement_wizard import (
        build_replacement_pairs, sum_operations_expense, sum_operations_income,
        sum_operations_income_alfa,
        calc_alfa_block3, calc_vtb_block3, format_amount_rur, format_amount_rub,
    )
    bank = state.get("bank", "alfa")
    mode = state.get("mode", "")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Генерирую выписку..."})

    changes = state.get("changes", {})
    current = state.get("current_values", {})
    raw_extra = state.get("raw_replacements", [])

    if bank == "alfa":
        if mode == "sw_alfa_edit":
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            if not replacement_pairs:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "ℹ️ Нет изменений — файл не модифицирован."})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                _send_main_menu_button(token, chat_id, tg_req)
                return
            in_path = Path(state["file_path"])
            try:
                from alfa_statement_service import (
                    apply_deferred_op_c_replacements,
                    split_alfa_pairs_defer_global_op_c,
                )
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                first_pairs, deferred_op_c = split_alfa_pairs_defer_global_op_c(
                    replacement_pairs
                )
                if first_pairs:
                    patch_replacements(in_path, Path(out_path), first_pairs)
                else:
                    import shutil
                    shutil.copy2(in_path, out_path)
                _apply_alfa_block3_calc(state, changes, current, out_path,
                                        sum_operations_expense, sum_operations_income_alfa,
                                        calc_alfa_block3, format_amount_rur, patch_replacements)
                apply_deferred_op_c_replacements(Path(out_path), deferred_op_c)
                try:
                    from alfa_statement_service import adjust_amount_tm_positions
                    adjust_amount_tm_positions(Path(out_path))
                except Exception:
                    pass
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка патчинга: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            try:
                os.unlink(state["file_path"])
            except OSError:
                pass
            out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
        else:
            try:
                from alfa_statement_service import BASE_PDF
                from cid_patch_amount import patch_replacements
                if not BASE_PDF.exists():
                    tg_req(
                        token,
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": "❌ Шаблон Альфа не найден: выписка_жуков_177414_СБП_одна_операция.pdf (в папке бота или ~/Downloads/). Пересборка: python3 build_statement_zhukov_177414.py",
                        },
                    )
                    if uid in USER_STATE:
                        del USER_STATE[uid]
                    return
                from alfa_statement_service import (
                    apply_deferred_op_c_replacements,
                    split_alfa_pairs_defer_global_op_c,
                )

                replacement_pairs = build_replacement_pairs(state) + raw_extra
                first_pairs, deferred_op_c = split_alfa_pairs_defer_global_op_c(
                    replacement_pairs
                )
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if first_pairs:
                    patch_replacements(BASE_PDF, Path(out_path), first_pairs)
                else:
                    import shutil
                    shutil.copy2(BASE_PDF, out_path)

                _apply_alfa_block3_calc(state, changes, current, out_path,
                                        sum_operations_expense, sum_operations_income_alfa,
                                        calc_alfa_block3, format_amount_rur, patch_replacements)
                apply_deferred_op_c_replacements(Path(out_path), deferred_op_c)
                try:
                    from alfa_statement_service import adjust_amount_tm_positions
                    adjust_amount_tm_positions(Path(out_path))
                except Exception:
                    pass

                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            out_name = "выписка_альфа.pdf"
    elif bank == "vtb":
        if mode == "sw_vtb_edit":
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            in_path = Path(state["file_path"])
            try:
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if replacement_pairs:
                    patch_replacements(in_path, Path(out_path), replacement_pairs)
                else:
                    import shutil
                    shutil.copy2(in_path, out_path)
                expenses = sum_operations_expense(state)
                income = sum_operations_income(state)
                bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
                try:
                    bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
                except ValueError:
                    bal = 0.0
                calc = calc_vtb_block3(bal, expenses, income)
                bal_pairs = []
                for calc_key, fmt_key in [("баланс_начало", "баланс_начало"), ("расходные_операции", "расходные_операции"), ("баланс_конец", "баланс_конец")]:
                    old_v = current.get(fmt_key)
                    new_v = format_amount_rub(calc[calc_key])
                    if old_v and old_v != new_v:
                        bal_pairs.append((old_v, new_v))
                if bal_pairs:
                    patch_replacements(Path(out_path), Path(out_path), bal_pairs)
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            try:
                os.unlink(state["file_path"])
            except OSError:
                pass
            out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
        else:
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            try:
                from vyписка_service import BASE_STATEMENT
                from cid_patch_amount import patch_replacements
                if not BASE_STATEMENT.exists():
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Шаблон ВТБ не найден (база_выписок/vtb_template.pdf)"})
                    if uid in USER_STATE:
                        del USER_STATE[uid]
                    return
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if replacement_pairs:
                    patch_replacements(BASE_STATEMENT, Path(out_path), replacement_pairs)
                else:
                    import shutil
                    shutil.copy2(BASE_STATEMENT, out_path)
                expenses = sum_operations_expense(state)
                income = sum_operations_income(state)
                bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
                try:
                    bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
                except ValueError:
                    bal = 0.0
                calc = calc_vtb_block3(bal, expenses, income)
                bal_pairs = []
                for calc_key in ("баланс_начало", "расходные_операции", "баланс_конец"):
                    old_v = current.get(calc_key)
                    new_v = format_amount_rub(calc[calc_key])
                    if old_v and old_v != new_v:
                        bal_pairs.append((old_v, new_v))
                if bal_pairs:
                    patch_replacements(Path(out_path), Path(out_path), bal_pairs)
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            out_name = "выписка_втб.pdf"

    elif bank == "tbank":
        tbank_changes = {}
        field_map = {
            "фио": "fio",
            "дата_формирования": "date_form",
            "исх_номер": "ish_number",
            "дата_договора": "contract_date",
            "номер_договора": "contract_number",
            "номер_счета": "account_number",
            "op_card_дата": "op1_date",
            "op_card_время": "op1_time",
            "op_card_время_списания": "op1_list_time",
            "op_card_сумма": "op1_amount",
            "op_card_номер": "op1_number",
            "op_sbp_дата": "op2_date",
            "op_sbp_время": "op2_time",
            "op_sbp_время_списания": "op2_list_time",
            "op_sbp_сумма": "op2_amount",
            "op_sbp_телефон": "op2_phone",
            "op_sbp_номер": "op2_number",
            "op_deposit_дата": "op3_date",
            "op_deposit_время": "op3_time",
            "op_deposit_время_списания": "op3_list_time",
            "op_deposit_сумма": "op3_amount",
            "op_deposit_номер": "op3_number",
            "пополнения": "total_deposit",
            "расходы": "total_expense",
        }
        for wiz_key, svc_key in field_map.items():
            val = changes.get(wiz_key)
            if val:
                tbank_changes[svc_key] = val
        try:
            base_pdf = state.get("file_path") if mode == "sw_tbank_edit" else None
            pdf_bytes = patch_tbank_statement(
                tbank_changes,
                base_pdf=base_pdf or str(TBANK_STATEMENT_TEMPLATE),
            )
        except Exception as e:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
            if uid in USER_STATE:
                del USER_STATE[uid]
            return
        if state.get("file_path"):
            try:
                os.unlink(state["file_path"])
            except OSError:
                pass
        out_name = "выписка_тбанк.pdf"

    form_date = None
    if bank == "alfa":
        form_date = changes.get("дата_формирования") or current.get(
            "дата_формирования"
        )
    if bank != "tbank":
        pdf_bytes = _finalize_statement_pdf_bytes(
            pdf_bytes, formation_date_ddmmyyyy=form_date
        )

    if uid in USER_STATE:
        del USER_STATE[uid]
    bank_label = {"alfa": "Альфа-Банк", "vtb": "ВТБ", "tbank": "ТБанк"}.get(bank, bank)
    caption = f"✅ Выписка {bank_label} готова"
    n_changes = len(changes)
    if n_changes:
        caption += f" ({n_changes} изм.)"
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption},
           files={"document": (out_name, pdf_bytes)})
    stats_record_pdf(uid, "statement", bank)
    _send_main_menu_button(token, chat_id, tg_req)


def _sw_start_alfa_edit_after_upload(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads Alfa statement PDF: scan and start wizard."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    block1 = scan_alfa_block1(Path(path))
    block2_ops = scan_alfa_block2(Path(path))
    block3 = scan_alfa_block3(Path(path))
    state = init_wizard_state("alfa", "sw_alfa_edit", block1, block2_ops, file_path=path, file_name=fname)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Выписка загружена.\n\n📋 Найдено полей: {len(block1)}\n📋 Операций: {len(block2_ops)}\n\nНачинаем пошаговое редактирование:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_alfa_from_check(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads check PDF for Alfa statement: extract and start wizard."""
    from statement_wizard import init_wizard_state
    from receipt_extractor import extract_from_receipt
    from alfa_statement_service import BASE_PDF
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    extracted = extract_from_receipt(Path(path))
    try:
        os.unlink(path)
    except OSError:
        pass
    block1 = scan_alfa_block1(BASE_PDF)
    block2_ops = scan_alfa_block2(BASE_PDF)
    block3 = scan_alfa_block3(BASE_PDF)
    if extracted.get("date"):
        for op in block2_ops:
            op["дата"] = extracted["date"]
    if extracted.get("operation_id"):
        for op in block2_ops:
            op["номер_операции"] = extracted["operation_id"]
    if extracted.get("phone_recipient"):
        for op in block2_ops:
            op["телефон"] = extracted["phone_recipient"]
    if extracted.get("amount"):
        for op in block2_ops:
            op["сумма"] = str(extracted["amount"])
    state = init_wizard_state("alfa", "sw_alfa_check", block1, block2_ops)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    parts = ["📎 Данные извлечены из чека:\n"]
    for k, v in extracted.items():
        if v:
            parts.append(f"  {k}: {v}")
    parts.append("\nНачинаем пошаговое редактирование выписки:")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(parts)})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_alfa_new(token: str, uid: int, chat_id: int, tg_req) -> None:
    """Start Alfa statement from scratch by scanning the Alfa template."""
    from statement_wizard import init_wizard_state
    from alfa_statement_service import BASE_PDF
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    block1 = scan_alfa_block1(BASE_PDF)
    block2_ops = scan_alfa_block2(BASE_PDF)
    block3 = scan_alfa_block3(BASE_PDF)
    state = init_wizard_state("alfa", "sw_alfa_new", block1, block2_ops)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "🔧 Создание выписки Альфа с нуля\n\nЗаполните все поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_edit_after_upload(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads VTB statement PDF: scan and start wizard."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_vtb_block1, scan_vtb_block2
    block1 = scan_vtb_block1(Path(path))
    block2_ops = scan_vtb_block2(Path(path))
    state = init_wizard_state("vtb", "sw_vtb_edit", block1, block2_ops, file_path=path, file_name=fname)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Выписка ВТБ загружена.\n\n📋 Найдено полей: {len(block1)}\n📋 Операций: {len(block2_ops)}\n\nНачинаем редактирование:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_from_check(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads check PDF for VTB statement: extract and start wizard."""
    from statement_wizard import init_wizard_state
    from receipt_extractor import extract_from_receipt
    from vyписка_service import scan_vtb_block1, scan_vtb_block2, BASE_STATEMENT
    extracted = extract_from_receipt(Path(path))
    try:
        os.unlink(path)
    except OSError:
        pass
    block1 = scan_vtb_block1(BASE_STATEMENT)
    block2_ops = scan_vtb_block2(BASE_STATEMENT)
    if extracted.get("date"):
        for op in block2_ops:
            op["дата"] = extracted["date"]
    if extracted.get("amount"):
        for op in block2_ops:
            op["сумма"] = str(extracted["amount"])
            op["сумма_зачисление"] = str(extracted["amount"])
    if extracted.get("fio_recipient"):
        desc_parts = []
        desc_parts.append("Переводы через СБП.")
        desc_parts.append(f"Перевод денежных средств. {extracted['fio_recipient']}.")
        for op in block2_ops:
            op["описание"] = " ".join(desc_parts)
    state = init_wizard_state("vtb", "sw_vtb_check", block1, block2_ops)
    USER_STATE[uid] = state
    parts = ["📎 Данные из чека для выписки ВТБ:\n"]
    for k, v in extracted.items():
        if v:
            parts.append(f"  {k}: {v}")
    parts.append("\nПошаговое редактирование:")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(parts)})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_new(token: str, uid: int, chat_id: int, tg_req) -> None:
    """Start VTB statement from scratch by scanning the VTB template."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_vtb_block1, scan_vtb_block2, BASE_STATEMENT
    block1 = scan_vtb_block1(BASE_STATEMENT)
    block2_ops = scan_vtb_block2(BASE_STATEMENT)
    state = init_wizard_state("vtb", "sw_vtb_new", block1, block2_ops)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "🔧 Создание выписки ВТБ с нуля\n\nЗаполните все поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_tbank_new(token: str, uid: int, chat_id: int, tg_req) -> None:
    """Start TBank statement from scratch using the TBank template."""
    from statement_wizard import build_field_sequence
    fields = tbank_extract_statement(TBANK_STATEMENT_TEMPLATE)
    current_values = dict(fields)
    state = {
        "mode": "sw_tbank_new",
        "bank": "tbank",
        "fields": build_field_sequence("tbank"),
        "field_idx": 0,
        "current_values": current_values,
        "changes": {},
        "operations": [],
    }
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "🔧 Создание выписки ТБанк с нуля\n\nЗаполните поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_tbank_edit_after_upload(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """Start TBank statement editing after user uploads their own statement PDF."""
    from statement_wizard import build_field_sequence
    fields = tbank_extract_statement(path)
    current_values = dict(fields)
    state = {
        "mode": "sw_tbank_edit",
        "bank": "tbank",
        "fields": build_field_sequence("tbank"),
        "field_idx": 0,
        "current_values": current_values,
        "changes": {},
        "operations": [],
        "file_path": path,
        "file_name": fname,
    }
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "✏️ Выписка ТБанк загружена.\n\nРедактируйте поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_tbank_from_check(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """Start TBank statement from a receipt PDF — extract check data into statement fields."""
    from statement_wizard import build_field_sequence
    try:
        rtype = tbank_detect_type(path)
        check_fields = tbank_extract_fields(path, rtype)
    except Exception:
        check_fields = {}

    base_fields = tbank_extract_statement(TBANK_STATEMENT_TEMPLATE)
    if check_fields.get("datetime"):
        parts = check_fields["datetime"].split()
        if parts:
            base_fields["date_form"] = parts[0]
            base_fields["op1_date"] = parts[0]
            base_fields["op2_date"] = parts[0]
            base_fields["op3_date"] = parts[0]
        if len(parts) > 1:
            base_fields["op1_time"] = parts[-1]

    if check_fields.get("amount_small"):
        amt = check_fields["amount_small"].strip()
        if rtype == "sbp":
            base_fields["op2_amount"] = f"-{amt}"
        elif rtype == "card":
            base_fields["op1_amount"] = f"-{amt}"

    if check_fields.get("phone"):
        base_fields["op2_phone"] = check_fields["phone"].replace(" ", "").replace("(", "").replace(")", "").replace("-", "")

    current_values = dict(base_fields)
    state = {
        "mode": "sw_tbank_check",
        "bank": "tbank",
        "fields": build_field_sequence("tbank"),
        "field_idx": 0,
        "current_values": current_values,
        "changes": {},
        "operations": [],
        "file_path": path,
        "file_name": fname,
    }
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "📄 Данные из чека перенесены в выписку ТБанк.\n\nРедактируйте поля:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _handle_vtb_full_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для ВТБ «Все поля»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _vtb_full_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting == "vtb_amount":
        parsed = parse_amounts(text)
        if not parsed:
            send("❌ Введите две суммы: с какой на какую (например: 10 1000 или 50 50000)")
            return
        amount_from, amount_to = parsed
        if amount_from <= 0 or amount_to <= 0:
            send("❌ Суммы должны быть больше 0.")
            return
        state["vtb_amount_from"] = amount_from
        state["vtb_amount"] = amount_to
        next_step("vtb_date", "2️⃣ Дата (дд.мм.гггг, чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "vtb_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["vtb_date"] = "now"
        else:
            state["vtb_date"] = text.strip()
        next_step("vtb_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "vtb_payer":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_payer"] = None
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["vtb_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["vtb_payer"] = t
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "vtb_recipient":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_recipient"] = None
            next_step("vtb_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["vtb_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_recipient"] = t
            next_step("vtb_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "vtb_phone":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_phone"] = None
            next_step("vtb_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["vtb_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("vtb_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_phone"] = t
            next_step("vtb_bank", "6️⃣ Банк получателя:")

    elif awaiting == "vtb_bank":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_bank"] = None
            send("⏳ Обрабатываю чек...")
            _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["vtb_bank"] = suggested
                send(f"✅ Применена замена: {suggested}\n⏳ Обрабатываю чек...")
                _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
                return
            send(err)
            return
        state["vtb_bank"] = t
        send("⏳ Обрабатываю чек...")
        _run_vtb_full_patch(token, uid, chat_id, state, tg_req)


def _alfa_tg_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «Альфа Трансгран». Поля с кнопкой «Оставить текущим»."""
    KEEP_PROMPTS = {
        "at_amount": ("1️⃣ Сумма перевода (сейчас: {amount}):\nВведите новую сумму, напр. 3 036 RUR", "at_keep_amount"),
        "at_commission": ("2️⃣ Комиссия (сейчас: {commission}):\nВведите новую, напр. 50 RUR\n(комиссия не включается в зачисление)", "at_keep_commission"),
        "at_rate": ("3️⃣ Курс конвертации (сейчас: {rate}):\nВведите новый, напр. 1 RUR = 0.1130 TJS\n✅ Зачисление в валюте (TJS/UZS/…) считается автоматически: сумма × курс", "at_keep_rate"),
        "at_phone": ("4️⃣ Телефон (сейчас: {phone}):", "at_keep_phone"),
        "at_name": ("5️⃣ Получатель (сейчас: {name}):", "at_keep_name"),
        "at_operation_id": ("6️⃣ Номер операции (сейчас: {operation_id}):", "at_keep_opid"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        tmpl, cb = KEEP_PROMPTS[aw]
        fields = state.get("at_fields", {})
        p = tmpl.format(**{k: fields.get(k, '—') for k in ['amount', 'commission', 'rate', 'credited', 'phone', 'name', 'operation_id']})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _run_alfa_transgran_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч трансграничного чека Альфа-Банка и отправить PDF."""
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    try:
        pdf_data = Path(inp).read_bytes()
        fields = state.get("at_fields", {})

        kwargs = {}
        for key, label in [
            ("at_amount", "amount"),
            ("at_commission", "commission"),
            ("at_rate", "rate"),
            ("at_credited", "credited"),
            ("at_phone", "phone"),
            ("at_name", "name"),
            ("at_operation_id", "operation_id"),
        ]:
            new_val = state.get(key)
            if new_val:
                old_val = fields.get(label, "")
                if old_val:
                    kwargs[label] = f"{old_val}={new_val}"

        if not kwargs:
            send("❌ Нет замен — все поля оставлены текущими.")
            return

        ok, err, new_data = patch_transgran(pdf_data, **kwargs)
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]

        if not ok or new_data is None:
            send(f"❌ Ошибка: {err}")
            return

        out_name = Path(state["file_name"]).stem + "_transgran.pdf"
        changes = [f"• {k}" for k in kwargs]
        caption = f"✅ Трансгран готов\n" + "\n".join(changes)
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, new_data)})
        stats_record_pdf(uid, "receipt", "alfa")
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _handle_alfa_sbp_input(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Пошаговый ввод полей Альфа СБП (все поля)."""
    state = USER_STATE[uid]
    step = state.get("alfa_sbp_step", 0)
    fields = state.setdefault("alfa_sbp_fields", {})
    field_list = state.get("_alfa_sbp_field_list", [
        ("amount", "💰 Сумма перевода (число, например: 5000)"),
        ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
        ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
        ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
        ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
        ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
    ])

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    if step >= len(field_list):
        return

    field_key, _ = field_list[step]
    t = text.strip()

    if t != "-":
        if field_key == "amount":
            nums = re.findall(r"\d+", t)
            if not nums:
                send("❌ Введите число (например: 5000)")
                return
            fields["amount"] = int("".join(nums))
        elif field_key == "account":
            digits = re.findall(r"\d+", t)
            last4 = "".join(digits)
            if len(last4) < 4:
                send("❌ Нужно ровно 4 цифры (например: 1234)")
                return
            last4 = last4[-4:]
            fields["account_last4"] = last4
        else:
            # Check for unsupported chars, but continue anyway with warning
            from vtb_cmap import get_unsupported_chars as _get_bad
            bad = _get_bad(t)
            if bad:
                bad_str = ", ".join(f"«{c}»" for c in sorted(bad))
                send(f"⚠️ Символы недоступны: {bad_str}. Поле будет пропущено. Продолжаем.")
                # Don't save the value, continue to next step
            else:
                fields[field_key] = t

    step += 1
    state["alfa_sbp_step"] = step

    if step < len(field_list):
        _, prompt_next = field_list[step]
        curr_key = field_list[step][0]
        curr_val = state.get("alfa_sbp_current", {}).get(curr_key)
        keep_hint = f"\n📌 Текущее: {curr_val}" if curr_val else ""
        kb = [[{"text": f"⏭ Пропустить", "callback_data": f"alfa_sbp_skip_{step}"}]]
        if curr_val:
            kb[0].insert(0, {"text": f"📌 Оставить: {str(curr_val)[:20]}", "callback_data": f"alfa_sbp_keep_{step}"})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Принято.\n\nШаг {step + 1}/{len(field_list)}: {prompt_next}{keep_hint}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
    else:
        summary_lines = []
        for fk, label in field_list:
            val = fields.get(fk) or fields.get("account_last4" if fk == "account" else fk)
            summary_lines.append(f"  {label.split('(')[0].strip()}: {val or '(без изменений)'}")
        summary = "\n".join(summary_lines)
        send(f"📋 Параметры:\n{summary}\n\n⏳ Генерирую PDF...")
        _run_alfa_sbp_full_patch(token, uid, chat_id, state, tg_req)


def _run_alfa_sbp_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Применяет все замены к Альфа СБП чеку (CID + zero-delta для счёта)."""
    import shutil

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    fields = state.get("alfa_sbp_fields", {})
    inp_path = Path(state["file_path"])

    try:
        data = inp_path.read_bytes()

        uni_to_cid = {}
        cid_to_uni = {}
        for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
            raw = data[m.end(): m.end() + int(m.group(2))]
            try:
                dec = zlib.decompress(raw)
            except zlib.error:
                continue
            if b'beginbfchar' in dec:
                for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                    cid = int(mm.group(1), 16)
                    uni = int(mm.group(2), 16)
                    cid_to_uni[cid] = chr(uni)
                    uni_to_cid[chr(uni)] = mm.group(1).decode().upper().zfill(4)
                break
            if b'beginbfrange' in dec:
                for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                    s = int(mm.group(1), 16)
                    e = int(mm.group(2), 16)
                    u = int(mm.group(3), 16)
                    for i in range(e - s + 1):
                        cid_to_uni[s + i] = chr(u + i)
                        uni_to_cid[chr(u + i)] = f'{s + i:04X}'
                break

        current_texts = []
        for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
            sl = int(m.group(2))
            ss = m.end()
            try:
                dec = zlib.decompress(data[ss:ss + sl])
            except zlib.error:
                continue
            if b'BT' not in dec:
                continue
            for tj in re.finditer(rb'<([0-9A-Fa-f]+)>\s*Tj', dec):
                hexstr = tj.group(1).decode()
                txt = ''
                for i in range(0, len(hexstr), 4):
                    cid = int(hexstr[i:i + 4], 16)
                    txt += cid_to_uni.get(cid, '?')
                current_texts.append(txt)
            break

        def find_text(pattern):
            for t in current_texts:
                if re.search(pattern, t.replace('\xa0', ' ')):
                    return t
            return None

        current_amount_text = find_text(r'\d+\s*RUR')
        current_datetime_text = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}')
        current_date_formed = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+мск')
        current_recipient = None
        current_phone = None
        current_bank = None
        current_account = None

        label_order = [t.replace('\xa0', ' ').strip() for t in current_texts]
        for i, tc in enumerate(label_order):
            if tc == 'Получатель' and i + 1 < len(label_order):
                current_recipient = current_texts[i + 1]
            elif 'телефона получателя' in tc and i + 1 < len(label_order):
                current_phone = current_texts[i + 1]
            elif tc == 'Банк получателя' and i + 1 < len(label_order):
                current_bank = current_texts[i + 1]
            elif 'Счёт списания' in tc and i + 1 < len(label_order):
                current_account = current_texts[i + 1]

        replacements = []

        if "amount" in fields and current_amount_text:
            old_amt = current_amount_text.replace('\xa0', ' ').strip()
            new_amt_num = fields["amount"]
            new_amt_str = f"{new_amt_num:,}".replace(",", "\xa0") + "\xa0RUR\xa0"
            replacements.append((old_amt, new_amt_str))

        if "date_time" in fields:
            new_dt = fields["date_time"]
            if current_datetime_text:
                old_dt = current_datetime_text.replace('\xa0', ' ').strip()
                new_dt_clean = new_dt.replace(' ', '\xa0')
                if not new_dt_clean.endswith('\xa0'):
                    new_dt_clean += '\xa0'
                replacements.append((old_dt, new_dt_clean))
            if current_date_formed:
                old_df = current_date_formed.replace('\xa0', ' ').strip()
                dt_parts = new_dt.split()
                if len(dt_parts) >= 2:
                    formed = dt_parts[0] + '\xa0' + dt_parts[1][:5] + '\xa0мск'
                    replacements.append((old_df, formed))

        if "recipient" in fields and current_recipient:
            old_r = current_recipient.replace('\xa0', ' ').strip()
            new_r = fields["recipient"].replace(' ', '\xa0')
            if old_r.endswith(' '):
                new_r += '\xa0'
            replacements.append((old_r, new_r))

        if "phone" in fields and current_phone:
            old_p = current_phone.replace('\xa0', ' ').strip()
            new_p = fields["phone"].replace(' ', '\xa0')
            replacements.append((old_p, new_p))

        if "bank" in fields and current_bank:
            old_b = current_bank.replace('\xa0', ' ').strip()
            new_b = fields["bank"].replace(' ', '\xa0')
            replacements.append((old_b, new_b))

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
            out_path = out_fp.name

        applied_text = False
        if replacements:
            try:
                from cid_patch_amount import patch_replacements
                text_reps = [(old.replace('\xa0', ' '), new.replace('\xa0', ' '))
                             for old, new in replacements if old.replace('\xa0', ' ') != new.replace('\xa0', ' ')]
                if text_reps:
                    applied_text = patch_replacements(inp_path, Path(out_path), text_reps)
            except Exception as e:
                send(f"⚠️ Ошибка текстовых замен: {e}")

        if not applied_text:
            shutil.copy2(str(inp_path), out_path)

        if "account_last4" in fields:
            try:
                from patch_account_last4 import patch_account_last4 as do_patch_account

                acct_20 = None
                if current_account:
                    digits_found = re.findall(r'\d+', current_account.replace('\xa0', ''))
                    full = ''.join(digits_found)
                    if len(full) == 20:
                        acct_20 = full

                if not acct_20:
                    acct_20 = "40817810980480002476"

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name

                ok = do_patch_account(
                    input_pdf=out_path,
                    output_pdf=tmp_path,
                    old_account=acct_20,
                    new_last4=fields["account_last4"],
                )
                if ok:
                    shutil.move(tmp_path, out_path)
                else:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    send("⚠️ Не удалось заменить счёт (возможно, номер не найден в PDF)")
            except Exception as e:
                send(f"⚠️ Ошибка замены счёта: {e}")

        try:
            os.unlink(state["file_path"])
        except OSError:
            pass
        del USER_STATE[uid]

        out_name = Path(state.get("file_name", "чек.pdf")).stem + "_patched.pdf"
        with open(out_path, "rb") as f:
            out_bytes = f.read()

        caption_parts = []
        if "amount" in fields:
            caption_parts.append(f"Сумма: {fields['amount']} RUR")
        if "account_last4" in fields:
            caption_parts.append(f"Счёт: ****{fields['account_last4']}")
        if "recipient" in fields:
            caption_parts.append(f"Получатель: {fields['recipient']}")
        caption = "✅ Готово! " + ", ".join(caption_parts) if caption_parts else "✅ Готово!"

        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        stats_record_pdf(uid, "receipt", "alfa")

        try:
            os.unlink(out_path)
        except OSError:
            pass

        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        import traceback
        traceback.print_exc()
        send(f"❌ Ошибка: {e}\n\nПопробуйте снова или отправьте чек заново.")
        try:
            os.unlink(state.get("file_path", ""))
        except OSError:
            pass
        if uid in USER_STATE:
            del USER_STATE[uid]


# ---------------------------------------------------------------------------
# Новые генераторы: поля wizard и вспомогательные функции
# ---------------------------------------------------------------------------

_NEW_GEN_WIZARD_FIELDS: dict[str, list[tuple[str, str]]] = {
    "alfa_sbp": [
        ("amount",           "💰 Сумма перевода (число, например: 5000)"),
        ("recipient_name",   "👤 Получатель (например: Виктория Игоревна С.)"),
        ("recipient_phone",  "📱 Телефон получателя (например: +7(900)351-70-80)"),
        ("recipient_bank",   "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
        ("account_last4",    "💳 Последние 4 цифры счёта списания (например: 2476, или - для авто)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
        ("formed_time",      "Сформирована (ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ — обязательно, например: 14:35 или 29.05.2026 14:35, не позднее +5 мин от времени перевода)"),
        ("sbp_id",           "🔑 SBP ID (32 символа из реального чека, например: A6119154524577180B10030011750703, или - для авто)"),
    ],
    "alfa_card": [
        ("amount",           "💰 Сумма перевода (число, например: 5000)"),
        ("commission",       "💰 Комиссия (например: 49,33 или 0, или - для 0)"),
        ("sender_card",      "💳 Карта отправителя (маска 220432******9136, или 16 цифр, или последние 4)"),
        ("recipient_card",   "💳 Карта получателя (маска 220432******9136, или 16 цифр, или последние 4)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
        ("formed_time",      "Сформирована (ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ — обязательно, не позднее +5 мин от времени перевода)"),
    ],
    "alfa_transgran": [
        ("amount",              "💰 Сумма в рублях (число, например: 5000)"),
        ("recipient_name",      "👤 Получатель (например: Rahimov A.)"),
        ("recipient_phone",     "📱 Телефон получателя (например: +992901234567)"),
        ("credited_currency",   "💱 Валюта зачисления (TJS, UZS, AMD — или - для TJS)"),
        ("amount_int",          "💰 Сумма зачисления (или - для авто расчёта)"),
        ("operation_date",      "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",      "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
    ],
    "gpb_sbp": [
        ("amount",           "💰 Сумма перевода (число, например: 10000)"),
        ("sender_name",      "👤 Отправитель ЗАГЛАВНЫМИ (например: ДАНИЛ АЛЕКСАНДРОВИЧ С.)"),
        ("sender_card",      "💳 Последние 4 цифры карты отправителя"),
        ("recipient_name",   "👤 Получатель (например: Байжигит Максатбекович М.)"),
        ("recipient_phone",  "📱 Телефон получателя (например: +7(915)333-60-13)"),
        ("recipient_bank",   "🏦 Банк получателя (например: ВТБ, Сбербанк)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
    ],
    "vtb_sbp": [
        ("amount",           "💰 Сумма перевода (число, например: 5000)"),
        ("recipient_name",   "👤 Получатель (например: Виктория Игоревна С.)"),
        ("recipient_phone",  "📱 Телефон получателя (например: +7(900)351-70-80)"),
        ("recipient_bank",   "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
        ("account_last4",    "💳 Последние 4 цифры счёта списания (например: 2476, или - для авто)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
        ("formed_time",      "Сформирована (ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ — или - для авто)"),
        ("sbp_id",           "🔑 SBP ID (32 символа, или - для авто)"),
    ],
    "vtb_vtb": [
        ("amount",           "💰 Сумма перевода (число, например: 5000)"),
        ("recipient_name",   "👤 Получатель (например: Виктория Игоревна С.)"),
        ("recipient_phone",  "📱 Телефон получателя (например: +7(900)351-70-80)"),
        ("recipient_bank",   "🏦 Банк получателя (например: ВТБ, или - для авто)"),
        ("account_last4",    "💳 Последние 4 цифры счёта списания (например: 2476, или - для авто)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
        ("formed_time",      "Сформирована (ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ — или - для авто)"),
        ("operation_id",     "🔑 ID операции (или - для авто)"),
    ],
    "tbank_sbp": [
        ("amount",           "💰 Сумма перевода (число, например: 5000)"),
        ("sender_name",      "👤 Отправитель (например: Иван Иванович И.)"),
        ("sender_account",   "💳 Последние 4 цифры счёта списания (например: 7645, или - для авто)"),
        ("recipient_name",   "👤 Получатель (например: Анна Ивановна И.)"),
        ("recipient_phone",  "📱 Телефон получателя (например: +7(900)000-00-00)"),
        ("recipient_bank",   "🏦 Банк получателя (например: Сбербанк, ВТБ, Альфа)"),
        ("operation_date",   "📅 Дата (ДД.ММ.ГГГГ, или - для авто)"),
        ("operation_time",   "🕐 Время (ЧЧ:ММ:СС, или - для авто)"),
        ("spb_number",       "🔑 SBP ID (из реального чека, или - для авто)"),
    ],
}

_NEW_GEN_MODE_LABELS = {
    "alfa_sbp":       "Альфа СБП",
    "alfa_card":      "Альфа карта-на-карту",
    "alfa_transgran": "Альфа трансгран",
    "gpb_sbp":        "Газпромбанк СБП",
    "vtb_sbp":        "ВТБ СБП",
    "vtb_vtb":        "ВТБ→ВТБ",
    "tbank_sbp":      "Т-Банк СБП",
}


def _extract_sbp_id_from_pdf(pdf_bytes: bytes) -> str | None:
    """Извлечь SBP ID (32 alphanumeric символа) из текста PDF."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        matches = re.findall(r"\b[A-Za-z0-9]{32}\b", text)
        return matches[0] if matches else None
    except Exception:
        return None


def _msk_datetime_from_sbp_id(sbp_id: str, reference_date: str | None = None) -> tuple[str, str] | None:
    """Restore the MSK (date, time) pair that is encoded inside the SBP ID.

    Real Alfa-Bank SBP IDs encode the transaction instant to the second, so if
    we inject a donor's SBP ID into a user-facing receipt we MUST display the
    same instant, otherwise the time inside the ID disagrees with the "Дата и
    время перевода" field and the receipt fails verification.

    Layout of an SBP ID (32 chars):
      0       : 'A' or 'B'
      1-2     : era counter
      3-4     : day of year (UTC) mod 100
      5-6     : UTC hour
      7-8     : UTC minute
      9-10    : UTC second
      11-14   : seq
      15      : check char
      16-31   : suffix

    `reference_date` (DD.MM.YYYY) is used to resolve the year and the
    hundreds-of-day — without it we assume the current year in MSK.
    """
    if not sbp_id or len(sbp_id) < 32 or sbp_id[0] not in ("A", "B"):
        return None
    try:
        day_mod = int(sbp_id[3:5])
        utc_h = int(sbp_id[5:7])
        utc_m = int(sbp_id[7:9])
        utc_s = int(sbp_id[9:11])
    except ValueError:
        return None
    if not (0 <= utc_h < 24 and 0 <= utc_m < 60 and 0 <= utc_s < 60):
        return None

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    if reference_date and reference_date not in ("auto", "", None):
        try:
            dd, mm, yyyy = (int(x) for x in reference_date.split("."))
            ref_year = yyyy
            # The digits show the day of year mod 100 — reconstruct full DoY
            # by taking whichever multiple-of-100 offset lands closest to the
            # reference date in that year.
            ref_doy = _dt(ref_year, mm, dd).timetuple().tm_yday
        except Exception:
            ref_year = _dt.now(_tz.utc).year
            ref_doy = _dt.now(_tz.utc).timetuple().tm_yday
    else:
        now_utc = _dt.now(_tz.utc)
        ref_year = now_utc.year
        ref_doy = now_utc.timetuple().tm_yday

    candidates = [day_mod + 100 * k for k in range(0, 4)]
    candidates = [d for d in candidates if 1 <= d <= 366]
    if not candidates:
        return None
    doy = min(candidates, key=lambda d: abs(d - ref_doy))

    try:
        utc_dt = _dt(ref_year, 1, 1, utc_h, utc_m, utc_s, tzinfo=_tz.utc) + _td(days=doy - 1)
    except (ValueError, OverflowError):
        return None
    msk_dt = utc_dt + _td(hours=3)
    return msk_dt.strftime("%d.%m.%Y"), msk_dt.strftime("%H:%M:%S")


def _align_fields_to_sbp_id(fields: dict, sbp_id: str | None) -> dict:
    """Return a copy of `fields` with operation_date/operation_time rewritten
    so that they agree with the time encoded in `sbp_id`.

    Called before passing `sbp_id_override` into a generator, so no matter how
    the override is produced (pool entry, extracted from user PDF, etc.) the
    receipt is self-consistent.
    """
    if not sbp_id or len(sbp_id) < 32:
        return fields
    ref = fields.get("operation_date") or None
    resolved = _msk_datetime_from_sbp_id(sbp_id, reference_date=ref)
    if not resolved:
        return fields
    new_date, new_time = resolved
    aligned = dict(fields)
    aligned["operation_date"] = new_date
    aligned["operation_time"] = new_time
    return aligned


def _validate_generated_alfa_sbp(pdf_bytes: bytes, expected_sbp_id: str | None = None) -> list[str]:
    """Post-generation sanity check.

    Returns a list of human-readable problem descriptions. Empty list → PDF
    looks internally consistent. Used to refuse obviously broken output
    instead of sending it to the user.
    """
    problems: list[str] = []
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = doc[0].get_text() if doc.page_count else ""
        doc.close()
    except ImportError:
        return []  # fitz not installed — skip semantic check, allow PDF through
    except Exception as e:
        return [f"не смог прочитать PDF: {e}"]

    m_dt = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s*мск", text)
    m_op = re.search(r"C16\d{13}\b", text)
    m_sbp = re.search(r"\b[AB]\d{2}[A-Z0-9]{29}\b", text)

    if not m_dt:
        problems.append("не нашёл 'Дата и время перевода' в формате DD.MM.YYYY HH:MM:SS")
    if not m_op:
        problems.append("не нашёл номер операции в формате C16 + 13 цифр")
    if not m_sbp:
        problems.append("не нашёл СБП ID (32 символа, начинается с A/B)")

    if m_dt and m_op:
        date_str = m_dt.group(1)
        yy_doc = date_str.split(".")[2][-2:]
        yy_op = m_op.group(0)[7:9]
        if yy_doc != yy_op:
            problems.append(
                f"год в номере операции (YY={yy_op}) не совпадает с годом в 'Дата и время перевода' (YY={yy_doc})"
            )

    if m_dt and m_sbp:
        date_str, time_str = m_dt.group(1), m_dt.group(2)
        sbp = m_sbp.group(0)
        if expected_sbp_id and sbp != expected_sbp_id:
            problems.append(f"СБП ID в PDF ({sbp}) не совпадает с ожидаемым ({expected_sbp_id})")
        encoded = _msk_datetime_from_sbp_id(sbp, reference_date=date_str)
        if encoded:
            enc_date, enc_time = encoded
            if enc_date != date_str or enc_time != time_str:
                problems.append(
                    "время в СБП ID ("
                    f"{enc_date} {enc_time}) не совпадает с временем перевода ({date_str} {time_str})"
                )

    return problems


def _gen_gpb(fields: dict) -> tuple[bytes, str]:
    from gen_gpb_receipt import generate_gpb_receipt
    amount = int(re.sub(r"\D", "", fields.get("amount", "1000")) or "1000")
    raw_card = fields.get("sender_card", "8527")
    sender_card = re.sub(r"[^\d]", "", raw_card)[-4:] or "8527"
    return generate_gpb_receipt(
        amount=amount,
        sender_name=fields.get("sender_name") or "ИВАН ИВАНОВИЧ И.",
        sender_card=sender_card,
        recipient_name=fields.get("recipient_name") or "Анна Ивановна И.",
        recipient_phone=fields.get("recipient_phone") or "+7(900)000-00-00",
        recipient_bank=fields.get("recipient_bank") or "Сбербанк",
        operation_date=fields.get("operation_date", "auto"),
        operation_time=fields.get("operation_time", "auto"),
        sbp_number=fields.get("sbp_number", "auto"),
    )


def _gen_alfa_sbp(fields: dict, sbp_id_override: str | None = None) -> tuple[bytes, str]:
    import importlib, gen_sbp_receipt as _gsr_mod
    importlib.reload(_gsr_mod)
    generate_sbp_receipt = _gsr_mod.generate_sbp_receipt
    random_account_with_last4 = _gsr_mod.random_account_with_last4

    # If the form contains an explicit SBP ID, prefer it over the pool entry.
    form_sbp = (fields.get("sbp_id") or "").strip()
    if len(form_sbp) == 32 and form_sbp[0] in ("A", "B"):
        sbp_id_override = form_sbp

    # When an external SBP ID is injected, realign date/time to the instant
    # encoded inside the ID — real SBP IDs are bound to the transaction time
    # to the second, so any drift is a verification-killer.
    fields = _align_fields_to_sbp_id(fields, sbp_id_override)

    amount = int(re.sub(r"\D", "", fields.get("amount", "1000")) or "1000")
    op_date = fields.get("operation_date", "auto")
    op_time = fields.get("operation_time", "auto")

    account: str | None = None
    raw_last4 = fields.get("account_last4") or fields.get("account") or ""
    raw_last4 = str(raw_last4).strip()
    if raw_last4 and raw_last4 not in ("auto", "авто", "-", ""):
        digits = re.sub(r"\D", "", raw_last4)
        if digits:
            account = random_account_with_last4(digits[-4:])

    receipt_number = _next_alfa_op_id(op_date, op_time)

    # Locate Tahoma font for font surgery (adds missing Cyrillic glyphs if needed)
    _bot_dir = Path(__file__).parent
    _tahoma = _bot_dir / "fonts" / "tahoma.ttf"
    _glyph_src = str(_tahoma) if _tahoma.exists() else None

    raw_formed = (fields.get("formed_time") or "").strip()
    if not raw_formed or raw_formed in ("-", "—", "авто", "auto"):
        raise ValueError(
            "❌ Поле «Сформирована» обязательно.\n"
            "Введите время в формате ЧЧ:ММ (например: 14:35) "
            "или ДД.ММ.ГГГГ ЧЧ:ММ (например: 29.05.2026 14:35)."
        )
    formed_time_val = raw_formed

    result = generate_sbp_receipt(
        amount=amount,
        recipient=fields.get("recipient_name") or "Виктория Игоревна С",
        phone=fields.get("recipient_phone") or "+7(900)000-00-00",
        bank=fields.get("recipient_bank") or "Сбербанк",
        operation_date=op_date,
        operation_time=op_time,
        account=account,
        message=fields.get("message") or "Перевод денежных средств",
        receipt_number=receipt_number,
        sbp_id_override=sbp_id_override,
        glyph_source=_glyph_src,
        formed_time=formed_time_val,
    )
    pdf_bytes, filename = result[0], result[1]

    # Self-check: refuse to hand over an internally inconsistent PDF.
    problems = _validate_generated_alfa_sbp(pdf_bytes, expected_sbp_id=sbp_id_override)
    if problems:
        raise RuntimeError(
            "Сгенерированный чек не прошёл внутреннюю проверку:\n- "
            + "\n- ".join(problems)
        )

    return pdf_bytes, filename


def _sanitize_card_input(raw: str, default: str = "9999") -> str:
    """Prepare a card string for generate_card_receipt._fmt_card_mask.

    Accepts:
      - Full mask: '220432******9136'  → passed as-is (BIN + stars preserved)
      - 16-digit number: '2204320000009136' → passed as-is (_fmt_card_mask strips middle)
      - Last 4 digits: '9136'          → passed as-is (_fmt_card_mask picks random BIN)
    """
    raw = (raw or "").strip()
    if "*" in raw:
        return raw  # already a proper mask, e.g. 220432******9136
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 16:
        return digits  # full 16-digit number
    if len(digits) >= 4:
        return digits[-4:]  # last 4 digits
    return default


def _gen_alfa_card(fields: dict) -> tuple[bytes, str]:
    import importlib, sys
    import gen_card_receipt as _gcr_mod
    importlib.reload(_gcr_mod)
    generate_card_receipt = _gcr_mod.generate_card_receipt
    amount = int(re.sub(r"\D", "", fields.get("amount", "1000")) or "1000")
    sender_card = _sanitize_card_input(fields.get("sender_card", "9999"), "9999")
    recipient_card = _sanitize_card_input(fields.get("recipient_card", "1234"), "1234")
    # Commission: user may enter "49,33" or "49" or "-" for auto (supports kopecks)
    raw_comm = (fields.get("commission") or "").strip()
    if raw_comm in ("-", "—", "auto", "авто", ""):
        commission_rub = 0.0
    else:
        try:
            commission_rub = float(raw_comm.replace(",", "."))
        except (ValueError, TypeError):
            commission_rub = 0.0

    # formed_time is mandatory
    raw_formed = (fields.get("formed_time") or "").strip()
    if not raw_formed or raw_formed in ("-", "—", "авто", "auto"):
        raise ValueError(
            "❌ Поле «Сформирована» обязательно.\n"
            "Введите время в формате ЧЧ:ММ (например: 01:53) "
            "или ДД.ММ.ГГГГ ЧЧ:ММ.\nДолжно быть позже времени перевода."
        )


    return generate_card_receipt(
        amount=amount,
        sender_card=sender_card,
        recipient_card=recipient_card,
        operation_date=fields.get("operation_date", "auto"),
        operation_time=fields.get("operation_time", "auto"),
        commission=commission_rub,
        formed_time=raw_formed,
    )


def _gen_alfa_transgran(fields: dict) -> tuple[bytes, str]:
    from gen_tajik_receipt import generate_tajik_receipt
    amount = int(re.sub(r"\D", "", fields.get("amount", "1000")) or "1000")
    commission = int(re.sub(r"\D", "", fields.get("commission", "0")) or "0")
    credited_currency = (fields.get("credited_currency") or "TJS").strip().upper() or "TJS"
    if credited_currency in ("-", "авто", "auto"):
        credited_currency = "TJS"
    amount_int_str = fields.get("amount_int", "")
    if amount_int_str and amount_int_str not in ("-", "auto", "авто"):
        amount_credited: int | None = int(re.sub(r"\D", "", amount_int_str) or "0") or None
    else:
        amount_credited = None
    account = fields.get("account") or None
    if account in ("auto", "авто", "-", ""):
        account = None
    pdf_bytes = generate_tajik_receipt(
        amount=amount,
        recipient_name=fields.get("recipient_name") or "Rahimov A.",
        recipient_phone=fields.get("recipient_phone") or "+992900000000",
        credited_currency=credited_currency,
        amount_credited=amount_credited,
        operation_date=fields.get("operation_date", "auto"),
        operation_time=fields.get("operation_time", "auto"),
        receipt_number=fields.get("receipt_number", "auto"),
        commission=commission,
        account=account,
    )
    fname = f"AM_{int(time.time() * 1000)}.pdf"
    return pdf_bytes, fname


_TBANK_AUTO_VALS = frozenset({"auto", "авто", "-", "—", "–", ".", ""})


def _tbank_field(val, default: str) -> str:
    """Return val if it's a real value, otherwise return default.
    Handles None and all common 'auto'/'skip' markers.
    """
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in _TBANK_AUTO_VALS:
        return default
    return s


def _gen_tbank_sbp(fields: dict) -> tuple[bytes, str]:
    """Сгенерировать чек Т-Бank СБП. Возвращает (pdf_bytes, filename).

    По умолчанию preserve_integrity=True: только content-stream патч,
    без изменения шрифтов и метаданных — для «Целостность PDF: Да».
    """
    from gen_tbank_receipt import generate_tbank_receipt

    amount_raw = fields.get("amount", "1000")
    amount_int = int(re.sub(r"\D", "", str(amount_raw)) or "1000")

    raw_account = _tbank_field(fields.get("sender_account"), "")
    if not raw_account:
        sender_account = "408178100000****0000"
    else:
        digits = re.sub(r"\D", "", raw_account)
        if len(digits) == 4:
            sender_account = f"408178100000****{digits}"
        elif len(digits) >= 4:
            sender_account = digits[:20].ljust(20, "0")
        else:
            sender_account = "408178100000****0000"

    pdf_bytes, filename = generate_tbank_receipt(
        amount=amount_int,
        sender_name=_tbank_field(fields.get("sender_name"), "Иван Иванович И."),
        sender_account=sender_account,
        recipient_name=_tbank_field(fields.get("recipient_name"), "Анна Ивановна И."),
        recipient_phone=_tbank_field(fields.get("recipient_phone"), "+7(900)000-00-00"),
        recipient_bank=_tbank_field(fields.get("recipient_bank"), "Сбербанк"),
        operation_date=fields.get("operation_date", "auto"),
        operation_time=fields.get("operation_time", "auto"),
        spb_number=fields.get("spb_number", "auto"),
        receipt_number=fields.get("receipt_number", "auto"),
        # donor_path=None → generate_tbank_receipt выберет подходящий донор сам
    )
    return pdf_bytes, filename


def _new_gen_send_next_field(token: str, chat_id: int, state: dict) -> None:
    """Отправить следующий вопрос wizard нового генератора."""
    mode = state["new_gen_mode"]
    step = state["new_gen_step"]
    fields_list = _NEW_GEN_WIZARD_FIELDS[mode]
    total = len(fields_list)
    _, prompt = fields_list[step]
    tg_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"Шаг {step + 1}/{total}: {prompt}\n\nОтправьте «-» чтобы оставить автоматически.",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "main_back"}]]}),
    })


def _new_gen_send_form(token: str, chat_id: int, state: dict, tg_req=None) -> None:
    """Отправить форму со всеми полями сразу — пользователь заполняет и присылает одним сообщением."""
    if tg_req is None:
        tg_req = tg_request
    mode = state["new_gen_mode"]
    fields_list = _NEW_GEN_WIZARD_FIELDS[mode]
    label = _NEW_GEN_MODE_LABELS.get(mode, mode)
    lines = []
    for _, prompt in fields_list:
        # Берём только часть до скобки как метку поля
        label_part = prompt.split("(")[0].strip().lstrip("💰👤📱🏦💳📅🕐💱🔑 ")
        lines.append(f"{label_part}: ")
    form_text = (
        f"✨ {label} — заполните все поля и отправьте одним сообщением:\n\n"
        + "\n".join(lines)
        + "\n\n💡 Для авто-значений напишите «-» после двоеточия."
    )
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": form_text,
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "main_back"}]]}),
    })
    state["awaiting"] = "new_gen_form"


_VTB_FORM_LABELS: dict[str, str] = {
    "сумма перевода": "amount",
    "сумма": "amount",
    "получатель": "recipient_name",
    "плательщик": "gen_payer",
    "телефон получателя": "recipient_phone",
    "телефон": "recipient_phone",
    "банк получателя": "recipient_bank",
    "банк": "recipient_bank",
    "последние 4 цифры счёта списания": "account_last4",
    "последние 4 цифры счёта": "account_last4",
    "дата": "operation_date",
    "время": "operation_time",
    "сформирована": "formed_time",
    "sbp id": "sbp_id",
    "id операции": "operation_id",
    "сообщение": "message",
}


def _parse_vtb_form(text: str, mode: str) -> dict | None:
    """Parse VTB form — supports alfa-style and legacy labels (Плательщик, Сумма, …)."""
    result: dict[str, str] = {}
    for line in (l.strip() for l in text.splitlines() if l.strip()):
        if ":" not in line:
            continue
        line_label, _, line_val = line.partition(":")
        key = _VTB_FORM_LABELS.get(line_label.strip().lower())
        if not key:
            continue
        val = line_val.strip()
        result[key] = "auto" if val in ("-", "—", "") else val

    if result.get("gen_recipient") and not result.get("recipient_name"):
        result["recipient_name"] = result["gen_recipient"]
    if result.get("gen_amount") and not result.get("amount"):
        result["amount"] = result["gen_amount"]
    if result.get("gen_date") and not result.get("operation_date"):
        result["operation_date"] = result["gen_date"]
    if result.get("gen_phone") and not result.get("recipient_phone"):
        result["recipient_phone"] = result["gen_phone"]
    if result.get("gen_bank") and not result.get("recipient_bank"):
        result["recipient_bank"] = result["gen_bank"]
    if result.get("gen_account") and not result.get("account_last4"):
        result["account_last4"] = result["gen_account"]
    if result.get("gen_operation_id") and not result.get("sbp_id") and not result.get("operation_id"):
        if mode == "vtb_vtb":
            result["operation_id"] = result["gen_operation_id"]
        else:
            result["sbp_id"] = result["gen_operation_id"]

    if not result.get("amount") or not result.get("recipient_name"):
        return None
    return result


def _parse_gen_form(text: str, fields_list: list, mode: str | None = None) -> dict | None:
    """Парсит ответ пользователя на форму. Возвращает {key: value} или None если не удалось."""
    if mode in ("vtb_sbp", "vtb_vtb"):
        parsed = _parse_vtb_form(text, mode)
        if parsed:
            return parsed

    result = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for key, prompt in fields_list:
        label_part = prompt.split("(")[0].strip().lstrip("💰👤📱🏦💳📅🕐💱🔑 ")
        matched = False
        for line in lines:
            if ":" not in line:
                continue
            line_label, _, line_val = line.partition(":")
            if line_label.strip().lower() == label_part.strip().lower():
                val = line_val.strip()
                result[key] = "auto" if val in ("-", "—", "") else val
                matched = True
                break
        if not matched:
            return None
    return result if result else None


def _run_new_gen(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Запустить генерацию после сбора всех полей."""
    mode = state["new_gen_mode"]
    fields = state["new_gen_values"]
    label = _NEW_GEN_MODE_LABELS.get(mode, mode)

    # Заменяем "-" на "auto" для дат/времени и авто-полей
    for k in ("operation_date", "operation_time", "credited_currency", "amount_int",
              "gen_date", "gen_phone", "gen_bank", "gen_account", "gen_operation_id",
              "gen_payer", "recipient_phone", "recipient_bank", "account_last4", "sbp_id",
              "formed_time", "operation_id"):
        if fields.get(k) in ("-", "—", "авто"):
            fields[k] = "auto"

    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"⏳ Генерирую {label}..."})

    try:
        if mode in ("vtb_sbp", "vtb_vtb"):
            try:
                pdf_bytes = vtb_generate_from_values(mode, fields)
            except ValueError as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
                return
            except FileNotFoundError as e:
                if uid in USER_STATE:
                    del USER_STATE[uid]
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ {e}"})
                return
            amount = int(str(fields.get("amount") or fields.get("gen_amount") or "0").replace(" ", ""))
            out_name = f"vtb_{mode}_{format_amount_display(amount).replace(' ', '_')}.pdf"
            del USER_STATE[uid]
            caption = f"✅ {label} | {format_amount_display(amount)} ₽"
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
            stats_record_pdf(uid, "receipt", "vtb")
            _send_main_menu_button(token, chat_id, tg_req)
            return

        if mode == "gpb_sbp":
            pdf_bytes, filename = _gen_gpb(fields)
            del USER_STATE[uid]
            caption = f"✅ Газпромбанк СБП | {fields.get('amount', '?')} руб."
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (filename, pdf_bytes)})
            _send_main_menu_button(token, chat_id, tg_req)
            return

        if mode == "tbank_sbp":
            pdf_bytes, filename = _gen_tbank_sbp(fields)
            del USER_STATE[uid]
            caption = f"✅ Т-Банк СБП | {fields.get('amount', '?')} руб."
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (filename, pdf_bytes)})
            _send_main_menu_button(token, chat_id, tg_req)
            return

        # Для Альфа: сначала проверяем SBP ID из формы, затем пул
        sbp_id_override = None
        used_pool_id = False
        if mode == "alfa_sbp":
            form_sbp = (fields.get("sbp_id") or "").strip()
            if len(form_sbp) == 32 and form_sbp[0] in ("A", "B"):
                # Пользователь вставил реальный SBP ID прямо в форму
                sbp_id_override = form_sbp
                used_pool_id = True  # тоже считается «готов сразу»
            elif _sbp_pool is not None:
                entry = _sbp_pool.consume()
                if entry:
                    sbp_id_override = entry["id"]
                    used_pool_id = True

        if mode == "alfa_sbp":
            pdf_bytes, filename = _gen_alfa_sbp(fields, sbp_id_override=sbp_id_override)
        elif mode == "alfa_card":
            pdf_bytes, filename = _gen_alfa_card(fields)
        elif mode == "alfa_transgran":
            pdf_bytes, filename = _gen_alfa_transgran(fields)
        else:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Неизвестный режим."})
            return

        # Если использован пул (alfa_sbp) или это карта/трансгран — отправляем сразу.
        # Меню «Прислать чек для нового SBP ID» относится только к alfa_sbp без пула,
        # для карты/трансграна оно бессмысленно (нет SBP ID для подстановки).
        if used_pool_id or mode in ("alfa_card", "alfa_transgran"):
            del USER_STATE[uid]
            caption = f"✅ {label} | {fields.get('amount', '?')} руб."
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (filename, pdf_bytes)})
            _send_main_menu_button(token, chat_id, tg_req)
            return

        # Иначе (только alfa_sbp без пула) — сохраняем PDF и предлагаем варианты
        USER_STATE[uid] = {
            "awaiting": "new_gen_pending",
            "new_gen_mode": mode,
            "new_gen_values": fields,
            "new_gen_saved_fields": fields,
            "new_gen_saved_mode": mode,
            "new_gen_pdf_bytes": pdf_bytes,
            "new_gen_pdf_filename": filename,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"✅ Чек {label} готов!\n\n"
                "Выберите действие:"
            ),
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "📎 Прислать чек для нового SBP ID", "callback_data": "new_gen_sbp_extract"}],
                [{"text": "💾 Сохранить PDF", "callback_data": "new_gen_save_pdf"}],
                [{"text": "❌ Отмена", "callback_data": "main_back"}],
            ]}),
        })

    except ValueError as e:
        # Validation error (e.g. missing formed_time) — keep state so user can retry
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": str(e)})
    except FileNotFoundError as e:
        if uid in USER_STATE:
            del USER_STATE[uid]
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Не найдены донорские файлы: {e}"})
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        if uid in USER_STATE:
            del USER_STATE[uid]
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка генерации: {e}"})
        _send_main_menu_button(token, chat_id, tg_req)


_CHECK_MODES = {
    "gpb_sbp":        "Газпромбанк СБП",
    "gpb":            "Газпромбанк СБП",
    "alfa_sbp":       "Альфа-Банк СБП",
    "alfa":           "Альфа-Банк СБП",
    "alfa_card":      "Альфа карта-на-карту",
    "card":           "Альфа карта-на-карту",
    "alfa_transgran": "Альфа Трансгран",
    "transgran":      "Альфа Трансгран",
    "tbank_sbp":      "Т-Банк СБП",
    "tbank":          "Т-Банк СБП",
}

_CHECK_HELP = """\
✨ Команда /check — генерация чека с нуля

Просто скопируйте нужный шаблон, замените данные и отправьте.
auto = бот подставит значение сам (дату, время, номер).

━━━━━━━━━━━━━━━━━━━━━
🔵 ГАЗПРОМБАНК — перевод по СБП
━━━━━━━━━━━━━━━━━━━━━
/check
mode: gpb_sbp
amount: 15000
sender_name: Данил Александрович С.
sender_card: 8527
recipient_name: Иван Сергеевич К.
recipient_phone: +7(915)348-60-40
recipient_bank: Сбербанк
operation_date: auto
operation_time: auto

  sender_card — последние 4 цифры карты отправителя
  operation_date — дата чека (формат 25.04.2026) или auto

━━━━━━━━━━━━━━━━━━━━━
🏦 АЛЬФА-БАНК — перевод по СБП
━━━━━━━━━━━━━━━━━━━━━
/check
mode: alfa_sbp
amount: 3500
sender_name: Демид Андреевич В.
sender_card: ** ** **** 3664
recipient_name: Иван Сергеевич К.
recipient_phone: +7(996)321-64-57
recipient_bank: Сбербанк
spb_number: auto
operation_date: auto
operation_time: auto

  spb_number — уникальный номер операции СБП.
    auto = берётся из пула или генерируется.

━━━━━━━━━━━━━━━━━━━━━
🏦 АЛЬФА-БАНК — перевод карта на карту
━━━━━━━━━━━━━━━━━━━━━
/check
mode: alfa_card
amount: 5000
sender_card: 9876
recipient_card: 1234
operation_date: auto
operation_time: auto

  sender_card / recipient_card — последние 4 цифры карт

━━━━━━━━━━━━━━━━━━━━━
🏦 АЛЬФА-БАНК — перевод в Таджикистан
━━━━━━━━━━━━━━━━━━━━━
/check
mode: alfa_transgran
amount: 3000
recipient_name: Rahimov A.
recipient_phone: +992938999964
credited_currency: TJS
amount_int: 354
operation_date: auto
operation_time: auto

  credited_currency — валюта получателя (TJS, UZS и т.д.)
  amount_int — сумма в валюте получателя

━━━━━━━━━━━━━━━━━━━━━
🅃 Т-БАНК — перевод по СБП
━━━━━━━━━━━━━━━━━━━━━
/check
mode: tbank_sbp
amount: 15000
sender_name: Егор Гладышев
sender_account: 408178102001****1274
recipient_name: Евгения К.
recipient_phone: +7 (908) 540-55-34
recipient_bank: Сбербанк
spb_number: auto
operation_date: auto
operation_time: auto

  sender_account — номер счёта в формате 408...****XXXX
  spb_number — ID операции СБП, auto = сгенерировать\
"""


def _parse_check_fields(text: str) -> dict[str, str]:
    """Parse /check multi-line format. Strips 'auto // comment' to just 'auto'."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("/check"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace("-", "_")
        val = val.strip()
        if "//" in val:
            val = val[: val.index("//")].strip()
        fields[key] = val
    return fields


def _handle_check_command(token: str, chat_id: int, text: str, tg_req) -> None:
    """Handle /check command: generate receipts for all supported modes."""

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    # Show help when called with no arguments
    body = text.strip()
    lines_with_content = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("/check")]
    if not lines_with_content:
        send(_CHECK_HELP)
        return

    fields = _parse_check_fields(text)
    mode = fields.pop("mode", "alfa_sbp").lower().replace(" ", "_")

    if mode not in _CHECK_MODES:
        send(
            "❌ Неверный режим. Доступные:\n"
            "  mode: gpb_sbp\n"
            "  mode: alfa_sbp\n"
            "  mode: alfa_card\n"
            "  mode: alfa_transgran\n"
            "  mode: tbank_sbp\n\n"
            "Отправьте /check без параметров, чтобы увидеть примеры."
        )
        return

    label = _CHECK_MODES[mode]
    send(f"⏳ Генерирую чек ({label})...")

    try:
        if mode in ("gpb_sbp", "gpb"):
            pdf_bytes, filename = _gen_gpb(fields)

        elif mode in ("alfa_sbp", "alfa"):
            spb_raw = fields.get("spb_number", "auto")
            sbp_override: str | None = None
            if spb_raw not in ("", "auto"):
                sbp_override = spb_raw
            elif _sbp_pool is not None:
                entry = _sbp_pool.consume()
                sbp_override = entry["id"] if entry else None
            pdf_bytes, filename = _gen_alfa_sbp(fields, sbp_id_override=sbp_override)

        elif mode in ("alfa_card", "card"):
            pdf_bytes, filename = _gen_alfa_card(fields)

        elif mode in ("alfa_transgran", "transgran"):
            pdf_bytes, filename = _gen_alfa_transgran(fields)

        elif mode in ("tbank_sbp", "tbank"):
            from gen_tbank_receipt import generate_tbank_receipt, get_missing_chars, _find_donor
            # Always run enrich_all — it skips already-enriched files, but will
            # create enriched versions of any new fresh donors added to TBANK/.
            try:
                from tbank_enrich_donor import enrich_all, TBANK_DIR as _TBANK_DIR
                enrich_all(_TBANK_DIR)
            except Exception:
                pass
            amount_raw = fields.get("amount", "1000")
            amount_int = int(re.sub(r"\D", "", amount_raw) or "1000")
            raw_account = fields.get("sender_account", "408178100000****0000")
            donor = _find_donor(prefer_enriched=True)
            # Warn if some name characters can't render
            text_fields = {
                "sender_name": fields.get("sender_name") or "",
                "recipient_name": fields.get("recipient_name") or "",
                "recipient_bank": fields.get("recipient_bank") or "",
            }
            missing_ch = get_missing_chars(donor, text_fields)
            pdf_bytes, filename = generate_tbank_receipt(
                amount=amount_int,
                sender_name=fields.get("sender_name") or "Иван Иванович И.",
                sender_account=raw_account if raw_account not in ("", "auto") else "408178100000****0000",
                recipient_name=fields.get("recipient_name") or "Анна Ивановна И.",
                recipient_phone=fields.get("recipient_phone") or "+7(900)000-00-00",
                recipient_bank=fields.get("recipient_bank") or "Сбербанк",
                operation_date=fields.get("operation_date", "auto"),
                operation_time=fields.get("operation_time", "auto"),
                spb_number=fields.get("spb_number", "auto"),
                receipt_number=fields.get("receipt_number", "auto"),
                donor_path=donor,
            )
            if missing_ch:
                send(
                    f"⚠️ В чеке отсутствуют глифы для символов: {' '.join(missing_ch)}\n"
                    f"Эти буквы будут невидимы в PDF. "
                    f"Используй имена из букв: А Б Д И К О П С Т У (заглавные)."
                )

        else:
            send("❌ Режим не поддерживается.")
            return

    except Exception as exc:
        send(f"❌ Ошибка генерации: {exc}")
        return

    caption = f"✅ {label} | {fields.get('amount', '?')} руб."

    tg_req(token, "sendDocument", {
        "chat_id": chat_id,
        "caption": caption,
    }, files={"document": (filename, pdf_bytes)})


def _handle_alfa_transgran_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для Альфа Трансгран."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str = ""):
        _alfa_tg_send_next(state, aw, prompt, chat_id, token, tg_req)

    skip_words = ("оставить", "текущим", "оставить текущим", "пропустить", "-", "=")

    # Примечание: at_credited рассчитывается автоматически (сумма × курс) — шаг пропускается
    FLOW = [
        ("at_amount",       "at_commission"),
        ("at_commission",   "at_rate"),
        ("at_rate",         "at_phone"),   # at_credited вычисляется автоматически
        ("at_phone",        "at_name"),
        ("at_name",         "at_operation_id"),
        ("at_operation_id", None),
    ]

    for step_aw, next_aw in FLOW:
        if awaiting != step_aw:
            continue
        t = text.strip()
        if t.lower() in skip_words:
            state[step_aw] = None
        else:
            state[step_aw] = t
        fields = state.get("at_fields", {})
        # После новой суммы — зачисление в валюте (TJS, UZS, …) по курсу из чека
        if step_aw == "at_amount" and state.get("at_amount"):
            try:
                rate_str = state.get("at_rate") or fields.get("rate", "")
                amount_str = state["at_amount"]
                if rate_str and amount_str:
                    pr = parse_rate(rate_str)
                    if pr:
                        rate_val, currency = pr
                        from decimal import Decimal

                        amount_num = Decimal(
                            re.sub(r"[^\d.,]", "", amount_str.replace(",", ".").replace("\xa0", ""))
                        )
                        credited_val = amount_num * rate_val
                        state["at_credited"] = format_alfa_transgran_credited(credited_val, currency)
                        send(
                            f"✅ Зачисление: {state['at_credited']} "
                            f"(сумма × курс; в PDF при патче подставится эта строка)"
                        )
            except Exception:
                pass
        # После ввода курса — автоматически вычислить зачисление
        if step_aw == "at_rate" and state.get("at_rate"):
            try:
                rate_str = state["at_rate"]
                amount_str = state.get("at_amount") or fields.get("amount", "")
                rate_val, currency = parse_rate(rate_str)
                from decimal import Decimal

                amount_num = Decimal(re.sub(r"[^\d.,]", "", amount_str.replace(",", ".").replace("\xa0", "")))
                credited_val = amount_num * rate_val
                state["at_credited"] = format_alfa_transgran_credited(credited_val, currency)
                send(f"✅ Зачисление авто: {state['at_credited']} (сумма × курс, без комиссии)")
            except Exception:
                state["at_credited"] = None
        if next_aw:
            next_step(next_aw)
        else:
            send("⏳ Обрабатываю чек...")
            _run_alfa_transgran_patch(token, uid, chat_id, state, tg_req)
        return


def _vtb_tg_send_next(state: dict, aw: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «ВТБ Трансгран». Поля с кнопкой «Оставить текущим»."""
    fields = state.get("vt_fields", {})
    KEEP_PROMPTS = {
        "vt_amount": ("1️⃣ Сумма операции (сейчас: {amount}):\nВведите новую сумму числом (напр. 10000)", "vt_keep_amount"),
        "vt_phone": ("2️⃣ Телефон получателя (сейчас: {phone}):\nВведите новый номер", "vt_keep_phone"),
        "vt_date": ("3️⃣ Дата и время (сейчас: {date}):\nВведите новые, напр. 18.03.2026, 02:44", "vt_keep_date"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        tmpl, cb = KEEP_PROMPTS[aw]
        p = tmpl.format(**{k: fields.get(k, '—') for k in ['amount', 'phone', 'date']})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })


def _run_vtb_transgran_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч трансграничного чека ВТБ и отправить PDF."""
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    try:
        pdf_data = Path(inp).read_bytes()
        fields = state.get("vt_fields", {})

        kwargs: dict = {}
        new_amount = state.get("vt_amount")
        new_phone = state.get("vt_phone")
        new_date = state.get("vt_date")

        if new_amount:
            try:
                kwargs["amount"] = int(new_amount.replace(" ", "").replace("₽", "").strip())
            except ValueError:
                send(f"❌ Неверный формат суммы: {new_amount}")
                return

        if new_phone:
            kwargs["phone"] = new_phone.strip()

        if new_date:
            kwargs["date"] = new_date.strip()

        if not kwargs:
            send("❌ Нет замен — все поля оставлены текущими.")
            return

        if "amount" not in kwargs:
            old_amt = fields.get("amount", "")
            m = re.search(r'([\d\s]+)', old_amt.replace('\xa0', ' '))
            if m:
                kwargs["amount"] = int(m.group(1).replace(" ", ""))

        ok, info, new_data = patch_vtb_transgran(pdf_data, **kwargs)
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]

        if not ok or new_data is None:
            send(f"❌ Ошибка: {info}")
            return

        out_name = Path(state["file_name"]).stem + "_transgran.pdf"

        rate_str = fields.get("rate", "")
        parsed = parse_rate(rate_str) if rate_str else None
        summary_lines = []
        if "amount" in kwargs:
            summary_lines.append(f"• Сумма: {format_amount_rub(kwargs['amount'])}")
            if parsed:
                from decimal import Decimal
                rate_val, currency = parsed
                credited_val = Decimal(kwargs["amount"]) * rate_val
                summary_lines.append(f"• Зачисление: {format_credited(credited_val, currency)} (авто)")
        if new_phone:
            summary_lines.append(f"• Телефон: {new_phone}")
        if new_date:
            summary_lines.append(f"• Дата: {new_date}")

        caption = "✅ ВТБ Трансгран готов\n" + "\n".join(summary_lines)
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, new_data)})
        stats_record_pdf(uid, "receipt", "vtb")
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _handle_vtb_transgran_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для ВТБ Трансгран."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    skip_words = ("оставить", "текущим", "оставить текущим", "пропустить", "-", "=")

    FLOW = [
        ("vt_amount", "vt_phone"),
        ("vt_phone",  "vt_date"),
        ("vt_date",   None),
    ]

    for step_aw, next_aw in FLOW:
        if awaiting != step_aw:
            continue
        t = text.strip()
        if t.lower() in skip_words:
            state[step_aw] = None
        else:
            state[step_aw] = t
        if next_aw:
            _vtb_tg_send_next(state, next_aw, chat_id, token, tg_req)
        else:
            send("⏳ Обрабатываю чек...")
            _run_vtb_transgran_patch(token, uid, chat_id, state, tg_req)
        return




def run_bot(token: str) -> None:
    offset = _load_offset()
    print("Бот запущен (без зависимостей)...")

    _ALLOWED_UPDATES = ["message", "callback_query"]

    while True:
        try:
            # timeout 25 — стандартный long-poll; urlopen timeout в tg_request = 35
            r = tg_request(token, "getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": _ALLOWED_UPDATES,
            })
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            err_msg = body or e.reason
            if e.code == 400:
                # НЕ сбрасываем offset в 0 — иначе старые апдейты проигрываются заново.
                # Сбрасываем только если ошибка явно о webhook-конфликте.
                print(f"⚠️ getUpdates 400: {err_msg[:120]}")
                if "webhook" in err_msg.lower():
                    print("   Webhook-конфликт — удаляем webhook и продолжаем.")
                    try:
                        tg_request(token, "deleteWebhook", {"drop_pending_updates": False})
                    except Exception:
                        pass
            elif e.code == 409:
                print("❌ Conflict 409: другой экземпляр бота уже запущен. Выход.")
                sys.exit(0)
            else:
                print(f"Ошибка getUpdates HTTP {e.code}: {err_msg[:80]}")
            time.sleep(5)
            continue
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        if not r.get("ok"):
            print("Ответ API:", r)
            time.sleep(5)
            continue

        for upd in r.get("result", []):
            update_id = upd["update_id"]
            offset = update_id + 1
            _save_offset(offset)
            if _already_handled_update(update_id):
                continue
            try:

                if "message" in upd:
                    msg = upd["message"]
                    uid = msg["from"]["id"]
                    chat_id = msg["chat"]["id"]
                    if not _is_allowed(uid):
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": ACCESS_DENIED_MSG})
                        continue
                    text = msg.get("text", "").strip()

                    if text == "/start":
                        if uid in USER_STATE:
                            if "file_path" in USER_STATE[uid]:
                                try:
                                    os.unlink(USER_STATE[uid]["file_path"])
                                except OSError:
                                    pass
                            del USER_STATE[uid]
                        tg_request(token, "sendMessage", {
                            "chat_id": msg["chat"]["id"],
                            "text": MAIN_MENU_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                        })
                        continue

                    _parts = text.split()
                    cmd0 = _parts[0].split("@", 1)[0] if _parts else ""

                    # --- /check: генерация чека (gpb_sbp / alfa_sbp / alfa_card / alfa_transgran) ---
                    if cmd0 == "/check":
                        _handle_check_command(token, chat_id, text, tg_request)
                        continue

                    # --- /add_sbp: добавить SBP ID в пул ---
                    if cmd0 == "/add_sbp":
                        if _sbp_pool is None:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ SBP пул недоступен."})
                            continue
                        body = text[len("/add_sbp"):].strip()
                        if not body:
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": "Отправьте SBP ID (по одному на строку или через пробел):\n/add_sbp A61051018121260A0B10040011740901\n\nКаждый ID — ровно 32 символа.",
                            })
                            continue
                        added, skipped = _sbp_pool.add_bulk(body)
                        total, used, avail = _sbp_pool.status()
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": f"✅ Добавлено: {added} | Пропущено: {skipped}\n\n📊 Пул: всего {total} | использовано {used} | доступно {avail}",
                        })
                        continue

                    # --- /pool: статус SBP пула ---
                    if cmd0 == "/pool":
                        if _sbp_pool is None:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ SBP пул недоступен."})
                            continue
                        total, used, avail = _sbp_pool.status()
                        entries = _sbp_pool.list_available()
                        lines = [
                            f"📊 Пул SBP ID",
                            f"Всего: {total} | Использовано: {used} | Доступно: {avail}",
                        ]
                        if entries:
                            lines.append("\n🟢 Доступные (первые 10):")
                            for e in entries[:10]:
                                bank = e.get("bank", "")
                                date = e.get("date", "")
                                lines.append(f"  {e['id']}  {bank} {date}".strip())
                        else:
                            lines.append("\n⚠️ Пул пуст. Добавьте: /add_sbp AAAA...")
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(lines)})
                        continue

                    # --- /sbp: удобное меню управления пулом SBP ID ---
                    if cmd0 == "/sbp":
                        if _sbp_pool is None:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ SBP пул недоступен."})
                            continue
                        total, used, avail = _sbp_pool.status()
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": (
                                f"📋 Пул SBP ID\n\n"
                                f"Всего: {total} | Использовано: {used} | Доступно: {avail}"
                            ),
                            "reply_markup": json.dumps({"inline_keyboard": [
                                [{"text": "➕ Добавить ID текстом", "callback_data": "sbp_add_text"}],
                                [{"text": "📄 Добавить из чека (PDF)", "callback_data": "sbp_add_pdf"}],
                                [{"text": "📋 Список доступных", "callback_data": "sbp_list"}],
                                [{"text": "🗑 Очистить использованные", "callback_data": "sbp_clear_used"}],
                            ]}),
                        })
                        continue

                    if cmd0 in ("/statistika", "/stats"):
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": stats_format_today(),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Главное меню", "callback_data": "main_back"}]]}),
                        })
                        continue

                    if text == "/main":
                        txt, km = build_zayavki_list()
                        kb = json.loads(km)
                        kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": txt, "reply_markup": json.dumps(kb)})
                        continue

                    # --- /grant: выдать временный доступ (только для постоянных админов) ---
                    if cmd0 == "/grant":
                        if uid not in _ALWAYS_ALLOWED_IDS:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "🚫 Только администратор может выдавать доступ."})
                            continue
                        parts = _parts[1:]  # [uid, duration]
                        if len(parts) < 2:
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": (
                                    "📋 Использование:\n"
                                    "/grant <user_id> <срок>\n\n"
                                    "Примеры:\n"
                                    "/grant 7041518561 24h\n"
                                    "/grant 7041518561 7d\n"
                                    "/grant 7041518561 30m\n\n"
                                    "Единицы: h/ч — часы, d/д — дни, m/м — минуты"
                                ),
                            })
                            continue
                        try:
                            target_uid = int(re.sub(r"\D", "", parts[0]))
                        except (ValueError, IndexError):
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Неверный user_id."})
                            continue
                        secs = _parse_duration(parts[1])
                        if secs is None:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Неверный срок. Используйте: 24h, 7d, 30m."})
                            continue
                        grants = _load_temp_access()
                        expiry = time.time() + secs
                        grants[target_uid] = expiry
                        _save_temp_access(grants)
                        exp_str = datetime.fromtimestamp(expiry).strftime("%d.%m.%Y %H:%M")
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": f"✅ Доступ выдан!\n\nUser ID: `{target_uid}`\nДо: {exp_str}",
                            "parse_mode": "Markdown",
                        })
                        continue

                    # --- /revoke: отозвать доступ ---
                    if cmd0 == "/revoke":
                        if uid not in _ALWAYS_ALLOWED_IDS:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "🚫 Только администратор может отзывать доступ."})
                            continue
                        if len(_parts) < 2:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "Использование: /revoke <user_id>"})
                            continue
                        try:
                            target_uid = int(re.sub(r"\D", "", _parts[1]))
                        except (ValueError, IndexError):
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Неверный user_id."})
                            continue
                        grants = _load_temp_access()
                        if target_uid in grants:
                            del grants[target_uid]
                            _save_temp_access(grants)
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Доступ для `{target_uid}` отозван.", "parse_mode": "Markdown"})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"⚠️ У `{target_uid}` нет временного доступа.", "parse_mode": "Markdown"})
                        continue

                    # --- /access: список выданных доступов ---
                    if cmd0 == "/access":
                        if uid not in _ALWAYS_ALLOWED_IDS:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "🚫 Только администратор."})
                            continue
                        grants = _load_temp_access()
                        now = time.time()
                        # Убираем истёкшие
                        expired = [k for k, v in grants.items() if v <= now]
                        for k in expired:
                            del grants[k]
                        if expired:
                            _save_temp_access(grants)
                        if not grants:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "📋 Активных временных доступов нет."})
                            continue
                        lines = ["📋 Активные временные доступы:\n"]
                        for tuid, exp in sorted(grants.items(), key=lambda x: x[1]):
                            exp_str = datetime.fromtimestamp(exp).strftime("%d.%m.%Y %H:%M")
                            remaining = int(exp - now)
                            if remaining >= 86400:
                                rem_str = f"{remaining // 86400}д {(remaining % 86400) // 3600}ч"
                            elif remaining >= 3600:
                                rem_str = f"{remaining // 3600}ч {(remaining % 3600) // 60}м"
                            else:
                                rem_str = f"{remaining // 60}м"
                            lines.append(f"• `{tuid}` — до {exp_str} (осталось {rem_str})")
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"})
                        continue


                    if "document" in msg:
                        doc = msg["document"]
                        fname = doc.get("file_name", "")
                        if not fname.lower().endswith(".pdf"):
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Отправьте PDF-файл."})
                            continue
                        state = USER_STATE.get(uid, {})
                        mode = state.get("mode", "")
                        if mode in ("sw_alfa_edit", "sw_vtb_edit", "sw_tbank_edit") and state.get("step") == "upload":
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю выписку..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            try:
                                if mode == "sw_alfa_edit":
                                    _sw_start_alfa_edit_after_upload(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                elif mode == "sw_tbank_edit":
                                    _sw_start_tbank_edit_after_upload(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                else:
                                    _sw_start_vtb_edit_after_upload(token, uid, msg["chat"]["id"], path, fname, tg_request)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка сканирования: {e}"})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                            continue
                        if mode in ("sw_alfa_check", "sw_vtb_check", "sw_tbank_check") and state.get("step") == "upload":
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            try:
                                if mode == "sw_alfa_check":
                                    _sw_start_alfa_from_check(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                elif mode == "sw_tbank_check":
                                    _sw_start_tbank_from_check(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                else:
                                    _sw_start_vtb_from_check(token, uid, msg["chat"]["id"], path, fname, tg_request)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                if uid in USER_STATE:
                                    del USER_STATE[uid]
                            continue
                        aw = state.get("awaiting", "")
                        if aw.startswith("db_add_"):
                            bank = aw.replace("db_add_", "")
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю и добавляю в базу..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            if add_receipt_to_index(path, bank):
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                del USER_STATE[uid]
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": f"✅ Чек добавлен в базу ({'СБП' if bank == 'vtb_sbp' else 'ВТБ на ВТБ' if bank == 'vtb_vtb_vtb' else 'Альфа'})",
                                    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                                })
                            else:
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Не удалось добавить (возможно пустой PDF или ошибка)"})
                            continue
                        # SBP пул: пользователь прислал PDF для извлечения SBP ID в пул
                        if USER_STATE.get(uid, {}).get("awaiting") == "sbp_add_pdf_input":
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                                del USER_STATE[uid]
                                continue
                            sbp_id = _extract_sbp_id_from_pdf(pdf_data)
                            del USER_STATE[uid]
                            if not sbp_id:
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": "❌ SBP ID не найден в этом PDF.\n\nПопробуйте другой СБП-чек или добавьте ID вручную через /sbp.",
                                })
                            elif _sbp_pool is not None:
                                added = _sbp_pool.add(sbp_id)
                                total, used, avail = _sbp_pool.status()
                                status = "✅ Добавлен" if added else "⚠️ Уже есть в пуле"
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": (
                                        f"{status}: `{sbp_id}`\n\n"
                                        f"📊 Пул: всего {total} | использовано {used} | доступно {avail}"
                                    ),
                                    "parse_mode": "Markdown",
                                })
                            else:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Пул недоступен."})
                            continue

                        # Новый генератор: пользователь прислал реальный чек для извлечения SBP ID
                        if USER_STATE.get(uid, {}).get("awaiting") == "new_gen_sbp_receipt":
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Извлекаю SBP ID из чека..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                                continue
                            sbp_id = _extract_sbp_id_from_pdf(pdf_data)
                            if not sbp_id:
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": "❌ SBP ID не найден в этом PDF.\n\nПопробуйте другой чек (СБП-перевод любого банка) или сохраните PDF как есть.",
                                    "reply_markup": json.dumps({"inline_keyboard": [
                                        [{"text": "💾 Сохранить PDF", "callback_data": "new_gen_save_pdf"}],
                                        [{"text": "❌ Отмена", "callback_data": "main_back"}],
                                    ]}),
                                })
                                continue
                            tg_request(token, "sendMessage", {
                                "chat_id": msg["chat"]["id"],
                                "text": f"✅ SBP ID найден: `{sbp_id}`\n⏳ Пересобираю чек...",
                                "parse_mode": "Markdown",
                            })
                            state = USER_STATE[uid]
                            saved_fields = state.get("new_gen_saved_fields") or state.get("new_gen_values", {})
                            saved_mode = state.get("new_gen_saved_mode") or state.get("new_gen_mode", "alfa_sbp")
                            label = _NEW_GEN_MODE_LABELS.get(saved_mode, saved_mode)
                            try:
                                if saved_mode == "tbank_sbp":
                                    # Для T-Bank подставляем SBP ID в поле spb_number и регенерируем
                                    saved_fields = dict(saved_fields)
                                    saved_fields["spb_number"] = sbp_id
                                    pdf_bytes, filename = _gen_tbank_sbp(saved_fields)
                                else:
                                    pdf_bytes, filename = _gen_alfa_sbp(saved_fields, sbp_id_override=sbp_id)
                                del USER_STATE[uid]
                                caption = f"✅ {label} | {saved_fields.get('amount', '?')} руб. | SBP ID вставлен"
                                tg_request(token, "sendDocument", {"chat_id": msg["chat"]["id"], "caption": caption}, files={"document": (filename, pdf_bytes)})
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка пересборки: {e}"})
                            _send_main_menu_button(token, chat_id, tg_request)
                            continue

                        _aw_pdf = (USER_STATE.get(uid, {}).get("awaiting") or "")
                        if (
                            "gen_bank_type" in USER_STATE.get(uid, {})
                            or "gen_transfer_type" in USER_STATE.get(uid, {})
                            or _aw_pdf.startswith("ak_")
                            or _aw_pdf in ("new_gen_field", "new_gen_form")
                        ):
                            tg_request(token, "sendMessage", {
                                "chat_id": msg["chat"]["id"],
                                "text": "В режиме генерации чек не нужен. Продолжайте ввод выше или нажмите /start.",
                            })
                            continue
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                        try:
                            fp = tg_get_file_path(token, doc["file_id"])
                            pdf_data = tg_get_file(token, fp)
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            tf.write(pdf_data)
                            path = tf.name
                        old = USER_STATE.get(uid, {})
                        if "file_path" in old and old["file_path"] != path:
                            try:
                                os.unlink(old["file_path"])
                            except OSError:
                                pass
                        USER_STATE[uid] = {"file_path": path, "file_name": fname}

                        # Авто-детект Т-Банка: сразу предложить смену суммы
                        _auto_tbank = False
                        try:
                            if is_tbank_pdf(path):
                                _rtype = tbank_detect_type(path)
                                _fields = tbank_extract_fields(path, _rtype)
                                _amt = (_fields.get("amount_small") or _fields.get("amount_bold") or "?").strip()
                                _rtype_label = TBANK_TYPE_LABELS.get(_rtype, _rtype)
                                USER_STATE[uid]["tbank_type"] = _rtype
                                USER_STATE[uid]["bank"] = "tbank"
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": (
                                        f"🅃 Чек Т-Банка получен\n"
                                        f"Тип: {_rtype_label}\n"
                                        f"Текущая сумма: {_amt} ₽\n\n"
                                        "Что сделать?"
                                    ),
                                    "reply_markup": json.dumps({
                                        "inline_keyboard": [
                                            [{"text": "💰 Изменить сумму", "callback_data": "bank_tbank_amount"}],
                                            [{"text": "📋 Все поля", "callback_data": "bank_tbank_full"}],
                                            [{"text": "⚙️ Другой банк", "callback_data": "bank_manual_select"}],
                                            [{"text": "❌ Отмена", "callback_data": "cancel"}],
                                        ],
                                    }),
                                })
                                _auto_tbank = True
                        except Exception:
                            pass

                        if not _auto_tbank:
                            tg_request(token, "sendMessage", {
                                "chat_id": msg["chat"]["id"],
                                "text": "📎 Чек получен. Выберите банк:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [
                                            {"text": "🏦 Альфа", "callback_data": "bank_alfa_sub"},
                                            {"text": "🏛 ВТБ", "callback_data": "bank_vtb_sub"},
                                            {"text": "🅃 ТБанк", "callback_data": "bank_tbank_sub"},
                                        ],
                                        [{"text": "🔍 Авто — любой банк", "callback_data": "bank_auto_free"}],
                                        [{"text": "❌ Отмена", "callback_data": "cancel"}],
                                    ],
                                }),
                            })
                        continue

                    # Альфа СБП все поля: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("mode") == "alfa_sbp_full":
                        _handle_alfa_sbp_input(token, uid, chat_id, text, tg_request)
                        continue

                    # Пошаговый визард выписки (sw_alfa_*, sw_vtb_*)
                    if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("sw_"):
                        _sw_handle_text(token, uid, chat_id, text, tg_request)
                        continue

                    # Альфа Трансгран: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("at_"):
                        _handle_alfa_transgran_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # ВТБ Трансгран: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("vt_"):
                        _handle_vtb_transgran_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # ВТБ «Все поля»: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("vtb_"):
                        _handle_vtb_full_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # ТБанк трансгран: шаг 2 — курс
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_tg_rate":
                        try:
                            rate = float(
                                text.replace(",", ".").replace(" ", "").strip()
                            )
                        except ValueError:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите курс числом. Пример: 0.1130"})
                            continue
                        if rate <= 0:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Курс должен быть больше 0."})
                            continue
                        USER_STATE[uid]["tbank_tg_rate"] = rate
                        _run_tbank_transgran_patch(
                            token, uid, chat_id, USER_STATE[uid], tg_request
                        )
                        continue

                    # ТБанк трансгран: шаг 1 — сумма
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_tg_amount":
                        try:
                            amt = float(text.replace(",", ".").replace(" ", ""))
                        except ValueError:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите число. Пример: 5000"})
                            continue
                        if amt <= 0:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Сумма должна быть больше 0."})
                            continue
                        USER_STATE[uid]["tbank_tg_amount"] = amt
                        USER_STATE[uid]["awaiting"] = "tbank_tg_rate"
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": (
                                "Шаг 2/2: введите **курс** — сколько единиц валюты за **1 ₽** "
                                "(например: `0.1130`).\n\n"
                                "Зачисление = сумма × курс."
                            ),
                            "parse_mode": "Markdown",
                        })
                        continue

                    # ТБанк: сумма + 4 цифры счёта — шаг 2
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_ac_last4":
                        t = text.strip()
                        d = re.sub(r"\D", "", t)
                        if len(d) != 4:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Нужно ровно 4 цифры. Пример: 7645"})
                            continue
                        USER_STATE[uid]["tbank_account_last4"] = d
                        _run_tbank_amount_account_patch(
                            token, uid, chat_id, USER_STATE[uid], tg_request
                        )
                        continue

                    # ТБанк: сумма + 4 цифры счёта — шаг 1
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_ac_amount":
                        try:
                            amt = float(text.replace(",", ".").replace(" ", ""))
                        except ValueError:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите число. Пример: 15000"})
                            continue
                        if amt <= 0:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Сумма должна быть больше 0."})
                            continue
                        USER_STATE[uid]["tbank_new_amount"] = amt
                        USER_STATE[uid]["awaiting"] = "tbank_ac_last4"
                        tg_request(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": "Шаг 2/2: введите **последние 4 цифры** номера счёта списания (как в чеке).",
                            "parse_mode": "Markdown",
                        })
                        continue

                    # ТБанк: ввод суммы
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_new_amount":
                        try:
                            amt = float(text.replace(",", ".").replace(" ", ""))
                        except ValueError:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите число. Пример: 15000"})
                            continue
                        USER_STATE[uid]["tbank_new_amount"] = amt
                        _run_tbank_amount_patch(token, uid, chat_id, USER_STATE[uid], tg_request)
                        continue

                    # ТБанк «Все поля»: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_full_field":
                        state = USER_STATE[uid]
                        idx = state["tbank_field_idx"]
                        keys = state["tbank_field_keys"]
                        key = keys[idx]
                        bad = tbank_get_unsupported(text, "regular")
                        if bad:
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": tbank_format_unsupported(bad) + "\n\nПопробуйте другое значение или нажмите «Оставить».",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "✅ Оставить", "callback_data": "tbank_keep_field"}],
                                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                ]}),
                            })
                            continue
                        state["tbank_changes"][key] = text
                        state["tbank_field_idx"] += 1
                        if state["tbank_field_idx"] >= len(keys):
                            _run_tbank_full_patch(token, uid, chat_id, state, tg_request)
                        else:
                            _tbank_send_next_field(token, chat_id, state)
                        continue

                    # ТБанк генерация с нуля: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_scratch_field":
                        state = USER_STATE[uid]
                        idx = state["tbank_scratch_idx"]
                        fields_list = state["tbank_scratch_fields"]
                        key = fields_list[idx]["key"]
                        bad = tbank_get_unsupported(text, "regular")
                        if bad:
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": tbank_format_unsupported(bad) + "\n\nПопробуйте другое значение или нажмите «Оставить».",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "✅ Оставить", "callback_data": "tbank_scratch_keep"}],
                                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                ]}),
                            })
                            continue
                        state["tbank_scratch_values"][key] = text
                        state["tbank_scratch_idx"] += 1
                        if state["tbank_scratch_idx"] >= len(fields_list):
                            _run_tbank_scratch_gen(token, uid, chat_id, state, tg_request)
                        else:
                            _tbank_send_scratch_field(token, chat_id, state)
                        continue

                    # SBP пул: добавление через текст
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "sbp_add_text_input":
                        if _sbp_pool is not None:
                            added, skipped = _sbp_pool.add_bulk(text)
                            total, used, avail = _sbp_pool.status()
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": (
                                    f"✅ Добавлено: {added} | Пропущено (дубли): {skipped}\n\n"
                                    f"📊 Пул: всего {total} | использовано {used} | доступно {avail}"
                                ),
                            })
                        del USER_STATE[uid]
                        continue

                    # Новые генераторы: форма (все поля сразу)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "new_gen_form":
                        state = USER_STATE[uid]
                        # Защита от двойного запуска: пока идёт генерация — игнорируем
                        if state.get("new_gen_generating"):
                            continue
                        mode = state["new_gen_mode"]
                        fields_list = _NEW_GEN_WIZARD_FIELDS.get(mode, [])
                        parsed = _parse_gen_form(text, fields_list, mode=mode)
                        if parsed is None:
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": (
                                    "❌ Не удалось распознать форму.\n\n"
                                    "Скопируйте шаблон выше и заполните каждое поле после двоеточия. "
                                    "Для авто-значений напишите «-»."
                                ),
                            })
                            _new_gen_send_form(token, chat_id, state, tg_request)
                        else:
                            state["new_gen_values"] = parsed
                            state["new_gen_generating"] = True
                            _run_new_gen(token, uid, chat_id, state, tg_request)
                        continue

                    # Альфа генерация с нуля: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_scratch_field":
                        state = USER_STATE[uid]
                        idx = state["alfa_scratch_idx"]
                        fields_list = state["alfa_scratch_fields"]
                        key = fields_list[idx]["key"]
                        state["alfa_scratch_values"][key] = text
                        state["alfa_scratch_idx"] += 1
                        if state["alfa_scratch_idx"] >= len(fields_list):
                            _run_alfa_scratch_gen(token, uid, chat_id, state, tg_request)
                        else:
                            _alfa_send_scratch_field(token, chat_id, state)
                        continue

                    # Альфа-карта: генерация из шаблона (карта / сумма / комиссия)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("ak_"):
                        _handle_alfa_karta_input(token, uid, chat_id, text, tg_request)
                        continue

                    # Режим «Сгенерировать»: по gen_bank_type (надёжнее, чем только awaiting)
                    if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                        _handle_gen_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # Авто-режим: пользователь вводит пары замен (старое = новое)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "auto_replacements":
                        state = USER_STATE[uid]
                        pairs = parse_auto_replacements(text)
                        if not pairs:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Не найдено пар замен.\nФормат: (старое = новое)\nНапример: (10 RUR = 10 000 RUR)"})
                            continue
                        inp = state["file_path"]
                        out_stem = _increment_filename(Path(state["file_name"]).stem)
                        out_name = out_stem + ".pdf"
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"⏳ Применяю {len(pairs)} замен(ы)..."})
                        try:
                            from cid_patch_amount import patch_replacements
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                out_path = tf.name
                            ok = patch_replacements(Path(inp), Path(out_path), pairs)
                            try:
                                os.unlink(inp)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                            if ok:
                                with open(out_path, "rb") as f:
                                    pdf_bytes = f.read()
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                summary = "\n".join(f"  {a} → {b}" for a, b in pairs[:5])
                                if len(pairs) > 5:
                                    summary += f"\n  ... и ещё {len(pairs)-5}"
                                caption = f"✅ Авто-замены:\n{summary}\n\n⚠️ Режим Авто не гарантирует прохождение проверки."
                                tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
                                stats_record_pdf(uid, "receipt", "auto")
                            else:
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Не удалось применить замены. Проверьте что значения совпадают с текстом в PDF."})
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                        _send_main_menu_button(token, chat_id, tg_request)
                        continue

                    # Альфа по карте: ввод суммы (шаг 1)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_card_amount":
                        state = USER_STATE[uid]
                        t = text.strip()
                        if t == "-":
                            state["alfa_card_new_amount"] = None
                        else:
                            nums = re.findall(r"\d+", t)
                            if not nums:
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите число (например: 40000)"})
                                continue
                            state["alfa_card_new_amount"] = int("".join(nums))
                        old_comm = state.get("alfa_card_old_commission", "")
                        comm_display = old_comm.replace("\xa0", " ").replace("RUR", "₽").strip() if old_comm else "нет"
                        if old_comm:
                            state["awaiting"] = "alfa_card_commission"
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": (
                                    "✅ Принято.\n\n"
                                    f"📌 Текущая комиссия: {comm_display}\n\n"
                                    "Шаг 2/2: введите новую комиссию (например: 729 или 729,50)\n"
                                    "Отправьте - чтобы оставить без изменений."
                                ),
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⏭ Оставить", "callback_data": "alfa_card_skip_comm"}]]}),
                            })
                        else:
                            state["alfa_card_new_commission"] = None
                            _run_alfa_card_patch(token, uid, chat_id, state, tg_request)
                        continue

                    # Альфа по карте: ввод комиссии (шаг 2)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_card_commission":
                        state = USER_STATE[uid]
                        t = text.strip()
                        if t in ("-", "—", "пропустить", "skip"):
                            state["alfa_card_new_commission"] = None
                        else:
                            t_clean = t.replace(" ", "").replace("\xa0", "")
                            if not re.match(r"^\d+([.,]\d{1,2})?$", t_clean):
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите число (например: 729 или 729,50)"})
                                continue
                            state["alfa_card_new_commission"] = t_clean.replace(".", ",")
                        _run_alfa_card_patch(token, uid, chat_id, state, tg_request)
                        continue

                    # Новый ввод суммы: только новое значение (auto-detect текущего)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_formed_input":
                        state = USER_STATE[uid]
                        inp = state.get("file_path", "")
                        t = text.strip()

                        def send_formed(txt):
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": txt, "parse_mode": "HTML"})

                        # Read current formed string from PDF
                        _formed_old = None
                        try:
                            import fitz as _fitz
                            _doc = _fitz.open(inp)
                            _txt_pdf = _doc[0].get_text() if _doc.page_count else ""
                            _doc.close()
                            _fm = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})\s+мск", _txt_pdf)
                            if _fm:
                                _formed_old = f"{_fm.group(1)} {_fm.group(2)} мск"
                        except Exception:
                            pass

                        if not _formed_old:
                            send_formed("❌ Не удалось прочитать текущее время «Сформирована» из PDF.")
                            del USER_STATE[uid]
                            _send_main_menu_button(token, chat_id, tg_request)
                            continue

                        # Parse new time from user input
                        _new_formed = None
                        _t_clean = t.replace(".", ".").strip()
                        # DD.MM.YYYY HH:MM
                        _m_full = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})$", _t_clean)
                        # HH:MM only
                        _m_time = re.match(r"(\d{1,2}:\d{2})$", _t_clean)

                        if _m_full:
                            _date_part = _m_full.group(1)
                            _time_part = _m_full.group(2)
                            _hh, _mm = _time_part.split(":")
                            _new_formed = f"{_date_part} {int(_hh):02d}:{_mm} мск"
                        elif _m_time:
                            _date_part = _formed_old.split()[0]  # keep date from current
                            _hh, _mm = _m_time.group(1).split(":")
                            _new_formed = f"{_date_part} {int(_hh):02d}:{_mm} мск"
                        else:
                            send_formed("❌ Формат не распознан.\nПримеры: <code>17:48</code> или <code>28.05.2026 17:48</code>")
                            continue

                        try:
                            from pathlib import Path as _Path
                            from cid_patch_amount import patch_replacements as _patch
                            from gen_sbp_receipt import _trim_cmap_in_pdf as _trim
                            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                            import random as _rnd, shutil as _sh

                            _src = _Path(inp)
                            _tmp = _src.with_suffix(".tmp_formed.pdf")

                            ok = _patch(_src, _tmp, [(_formed_old, _new_formed)])
                            if not ok:
                                send_formed(f"❌ Не удалось заменить «{_formed_old}» — строка не найдена в PDF.")
                                _tmp.unlink(missing_ok=True)
                                continue

                            _data = _trim(_tmp.read_bytes())
                            _tmp.unlink(missing_ok=True)

                            # Compute new filename from new formed time
                            _fm2 = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", _new_formed)
                            if _fm2:
                                _dd2, _mm2, _yyyy2, _hh2, _mi2 = (int(x) for x in _fm2.groups())
                                _MSK_OFF = _td(hours=3)
                                _formed_utc = _dt(_yyyy2, _mm2, _dd2, _hh2, _mi2, 0, tzinfo=_tz.utc) - _MSK_OFF
                                _offset = _rnd.randint(225, 295)
                                _ts_ms = int((_formed_utc.timestamp() - _offset) * 1000) + _rnd.randint(0, 999)
                                _new_fname = f"AM_{_ts_ms}.pdf"
                            else:
                                _new_fname = _src.name  # fallback keep original name

                            _dst = _src.parent / _new_fname
                            _dst.write_bytes(_data)
                            if _src != _dst:
                                _src.unlink(missing_ok=True)

                            state["file_path"] = str(_dst)
                            state["file_name"] = _new_fname
                            state.pop("awaiting", None)

                            send_formed(f"✅ «Сформирована» изменена:\n<b>{_formed_old}</b> → <b>{_new_formed}</b>")
                            tg_request(token, "sendDocument", {
                                "chat_id": chat_id,
                                "caption": f"📄 {_new_fname}",
                            }, files={"document": (_new_fname, _data)})
                            _send_main_menu_button(token, chat_id, tg_request)

                        except Exception as _e:
                            import traceback as _tb; _tb.print_exc()
                            send_formed(f"❌ Ошибка: {_e}")
                        continue

                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_new_only":
                        state = USER_STATE[uid]
                        nums = re.findall(r"\d+", text.strip())
                        if not nums:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите новую сумму числом (например: 5000)"})
                            continue
                        amount_to = int("".join(nums))
                        if amount_to <= 0:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Сумма должна быть больше 0."})
                            continue
                        state["amount_to"] = amount_to
                        commission = state.get("current_commission")
                        amt_from = state.get("current_amount") or 0
                        if commission and commission > 0 and amt_from > 0:
                            state["commission_to"] = scale_commission_rub(amt_from, amount_to, commission)
                            _do_amount_patch(token, uid, chat_id, state, tg_request)
                        elif commission and commission > 0:
                            state["awaiting"] = "amount_commission"
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": f"💳 Обнаружена комиссия: {format_amount_display(commission)} ₽\nВведите новую комиссию или — чтобы пропустить:",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "— Пропустить", "callback_data": "amount_skip_commission"}]]}),
                            })
                        else:
                            _do_amount_patch(token, uid, chat_id, state, tg_request)
                        continue

                    # Ввод новой комиссии
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_commission":
                        state = USER_STATE[uid]
                        t = text.strip()
                        if t in ("-", "—", "пропустить", "skip"):
                            state["commission_to"] = None
                        else:
                            nums = re.findall(r"\d+", t)
                            state["commission_to"] = int("".join(nums)) if nums else None
                        _do_amount_patch(token, uid, chat_id, state, tg_request)
                        continue

                    if uid in USER_STATE and "bank" in USER_STATE[uid]:
                        # Fallback: legacy "FROM TO" format (shouldn't normally reach here)
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите новую сумму (одно число, например: 5000)."})
                        continue

                    if uid in USER_STATE:
                        _aw = USER_STATE[uid].get("awaiting") or ""
                        if _aw.startswith("new_gen_"):
                            # Ждём нажатия кнопки — текстовые сообщения игнорируем
                            pass
                        elif "gen_bank_type" in USER_STATE[uid] or "gen_transfer_type" in USER_STATE[uid] or _aw.startswith("ak_"):
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Продолжайте ввод выше или нажмите кнопки."})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала выберите банк (кнопками выше)."})
                    else:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала отправьте чек и выберите банк."})

                elif "callback_query" in upd:
                    q = upd["callback_query"]
                    uid = q["from"]["id"]
                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"]})
                    if not _is_allowed(uid):
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": ACCESS_DENIED_MSG})
                        continue
                    if q["data"] == "main_check":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📁 Чек\n\nЗагрузите PDF или сгенерируйте из базы:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "📄 Загрузить чек", "callback_data": "main_new"},
                                        {"text": "✨ Сгенерировать чек", "callback_data": "main_generate"},
                                    ],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "main_generate":
                        USER_STATE[uid] = {}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✨ Сгенерировать чек\n\nВыберите банк:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "🏦 Альфа-Банк", "callback_data": "new_gen_alfa_menu"}],
                                    [{"text": "🏛 ВТБ", "callback_data": "gen_bank_menu_vtb"}],
                                    [{"text": "🅃 Т-Банк", "callback_data": "gen_type_tbank"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_check"}],
                                ],
                            }),
                        })
                        continue

                    if q["data"] == "gen_bank_menu_vtb":
                        _cur_aw = USER_STATE.get(uid, {}).get("awaiting", "")
                        if _cur_aw in ("new_gen_form", "new_gen_field", "new_gen_pending", "new_gen_sbp_receipt",
                                       "alfa_scratch_field", "tbank_scratch_field"):
                            tg_request(token, "answerCallbackQuery", {
                                "callback_query_id": q["id"],
                                "text": "⚠️ Вы в процессе другой операции. Завершите её или нажмите Отмена.",
                                "show_alert": True,
                            })
                            continue
                        USER_STATE[uid] = {"awaiting": "gen_type", "gen_transfer_type": None, "gen_bank_type": None}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🏛 ВТБ — выберите тип:\n\n"
                                "🔧 Генерация из встроенного шаблона — реальный перевод не нужен."
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📱 ВТБ СБП", "callback_data": "new_gen_start_vtb_sbp"}],
                                    [{"text": "💳 ВТБ на ВТБ", "callback_data": "new_gen_start_vtb_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_type_alfa_scratch":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Альфа — генерация с нуля\n\nВыберите тип чека:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📱 СБП", "callback_data": "gen_alfa_sbp"}],
                                    [{"text": "🏦 Альфа-альфа", "callback_data": "gen_alfa_alfa"}],
                                    [{"text": "💳 Карта", "callback_data": "gen_alfa_card"}],
                                    [{"text": "🌍 Трансгран", "callback_data": "gen_alfa_transgran"}],
                                    [{"text": "📱 Моб. связь", "callback_data": "gen_alfa_mobile"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue

                    if q["data"] in ("gen_alfa_sbp", "gen_alfa_alfa", "gen_alfa_card", "gen_alfa_transgran", "gen_alfa_mobile"):
                        rtype = q["data"].replace("gen_alfa_", "")
                        scratch_fields = alfa_get_fields_for_scratch(rtype)
                        USER_STATE[uid] = {
                            "awaiting": "alfa_scratch_field",
                            "alfa_scratch_type": rtype,
                            "alfa_scratch_fields": scratch_fields,
                            "alfa_scratch_idx": 0,
                            "alfa_scratch_values": {},
                        }
                        _alfa_send_scratch_field(token, q["message"]["chat"]["id"], USER_STATE[uid])
                        continue

                    if q["data"] == "alfa_scratch_keep":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_scratch_field":
                            state = USER_STATE[uid]
                            state["alfa_scratch_idx"] += 1
                            if state["alfa_scratch_idx"] >= len(state["alfa_scratch_fields"]):
                                _run_alfa_scratch_gen(token, uid, q["message"]["chat"]["id"], state, tg_request)
                            else:
                                _alfa_send_scratch_field(token, q["message"]["chat"]["id"], state)
                        continue

                    if q["data"] == "gen_type_tbank":
                        if uid in USER_STATE:
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🅃 Т-Банк — выберите тип:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📱 СБП", "callback_data": "new_gen_start_tbank_sbp"}],
                                    [{"text": "💳 По карте", "callback_data": "gen_tbank_card"}],
                                    [{"text": "🌍 Трансгран", "callback_data": "gen_tbank_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue

                    if q["data"] == "gen_tbank_sbp":
                        # Legacy callback → redirect на новый flow с формой как у Альфы
                        cur = USER_STATE.get(uid, {})
                        if cur.get("awaiting") == "new_gen_form" and cur.get("new_gen_mode") == "tbank_sbp":
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": "tbank_sbp",
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue

                    if q["data"] in ("gen_tbank_card", "gen_tbank_transgran"):
                        rtype = q["data"].replace("gen_tbank_", "")
                        from tbank_scratch_service import get_fields_for_scratch
                        scratch_fields = get_fields_for_scratch(rtype)
                        USER_STATE[uid] = {
                            "awaiting": "tbank_scratch_field",
                            "tbank_type": rtype,
                            "tbank_scratch_fields": scratch_fields,
                            "tbank_scratch_idx": 0,
                            "tbank_scratch_values": {},
                        }
                        _tbank_send_scratch_field(token, q["message"]["chat"]["id"], USER_STATE[uid])
                        continue

                    if q["data"] == "tbank_scratch_keep":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_scratch_field":
                            state = USER_STATE[uid]
                            state["tbank_scratch_idx"] += 1
                            if state["tbank_scratch_idx"] >= len(state["tbank_scratch_fields"]):
                                _run_tbank_scratch_gen(token, uid, q["message"]["chat"]["id"], state, tg_request)
                            else:
                                _tbank_send_scratch_field(token, q["message"]["chat"]["id"], state)
                        continue

                    if q["data"] in ("new_gen_start_vtb_sbp", "new_gen_start_vtb_vtb"):
                        mode = "vtb_sbp" if q["data"] == "new_gen_start_vtb_sbp" else "vtb_vtb"
                        cur = USER_STATE.get(uid, {})
                        if cur.get("awaiting") == "new_gen_form" and cur.get("new_gen_mode") == mode:
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": mode,
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue

                    if q["data"] == "gen_subtype_sbp":
                        _cur_aw = USER_STATE.get(uid, {}).get("awaiting", "")
                        if _cur_aw in ("new_gen_form", "new_gen_field", "new_gen_pending", "new_gen_sbp_receipt",
                                       "alfa_scratch_field", "tbank_scratch_field", "vtb_scratch_field"):
                            tg_request(token, "answerCallbackQuery", {
                                "callback_query_id": q["id"],
                                "text": "⚠️ Вы в процессе другой операции. Завершите её или нажмите Отмена.",
                                "show_alert": True,
                            })
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": "vtb_sbp",
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue
                    if q["data"] == "gen_subtype_vtb_vtb":
                        _cur_aw = USER_STATE.get(uid, {}).get("awaiting", "")
                        if _cur_aw in ("new_gen_form", "new_gen_field", "new_gen_pending", "new_gen_sbp_receipt",
                                       "alfa_scratch_field", "tbank_scratch_field", "vtb_scratch_field"):
                            tg_request(token, "answerCallbackQuery", {
                                "callback_query_id": q["id"],
                                "text": "⚠️ Вы в процессе другой операции. Завершите её или нажмите Отмена.",
                                "show_alert": True,
                            })
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": "vtb_vtb",
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue
                    if q["data"] == "gen_type_card":
                        from alfa_karta_service import template_exists
                        cid, mid = q["message"]["chat"]["id"], q["message"]["message_id"]
                        if not template_exists():
                            tg_request(token, "editMessageText", {
                                "chat_id": cid,
                                "message_id": mid,
                                "text": "❌ Шаблон не найден.\n\nПоложите PDF в папку бота:\n`шаблоны/alfa_karta.pdf`",
                                "parse_mode": "Markdown",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_generate"}]]}),
                            })
                            continue
                        USER_STATE[uid] = {"awaiting": "ak_recipient_card"}
                        tg_request(token, "editMessageText", {
                            "chat_id": cid,
                            "message_id": mid,
                            "text": (
                                "💳 Альфа-карта\n\n"
                                "Квитанция «перевод с карты на карту» (шаблон Альфа-Банка).\n\n"
                                "1️⃣ Введите номер карты получателя (маска), например:\n"
                                "`220220******5047`"
                            ),
                            "parse_mode": "Markdown",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_generate"}]]}),
                        })
                        continue
                    if q["data"] == "gen_type_transgran":
                        if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "Пока доступен только перевод по СБП.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue

                    # --- Новые генераторы: Альфа-Банк и Газпромбанк ---
                    if q["data"] == "new_gen_alfa_menu":
                        USER_STATE[uid] = {}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Альфа-Банк — выберите тип:",
                            "reply_markup": json.dumps({"inline_keyboard": [
                                [{"text": "📱 СБП перевод", "callback_data": "new_gen_start_alfa_sbp"}],
                                [{"text": "💳 Карта на карту", "callback_data": "new_gen_start_alfa_card"}],
                                [{"text": "🌍 Трансгран (Таджикистан)", "callback_data": "new_gen_start_alfa_transgran"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                            ]}),
                        })
                        continue

                    if q["data"] == "new_gen_gpb_menu":
                        USER_STATE[uid] = {}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🔵 Газпромбанк — выберите тип:",
                            "reply_markup": json.dumps({"inline_keyboard": [
                                [{"text": "📱 СБП перевод", "callback_data": "new_gen_start_gpb_sbp"}],
                                [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                            ]}),
                        })
                        continue

                    if q["data"] in ("new_gen_start_alfa_sbp", "new_gen_start_alfa_card",
                                     "new_gen_start_alfa_transgran", "new_gen_start_gpb_sbp",
                                     "new_gen_start_tbank_sbp"):
                        mode = q["data"].replace("new_gen_start_", "")
                        cur = USER_STATE.get(uid, {})
                        # Двойной клик: форма уже отправлена для этого же режима — игнорируем
                        if cur.get("awaiting") == "new_gen_form" and cur.get("new_gen_mode") == mode:
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": mode,
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue

                    if q["data"] in ("new_gen_save_pdf", "new_gen_onlypdf"):
                        state = USER_STATE.get(uid, {})
                        pdf_bytes = state.get("new_gen_pdf_bytes")
                        if not pdf_bytes:
                            # Двойной клик или сессия устарела — тихо игнорируем
                            continue
                        filename = state.get("new_gen_pdf_filename", "receipt.pdf")
                        amount = (state.get("new_gen_values") or state.get("new_gen_saved_fields", {})).get("amount", "?")
                        mode = state.get("new_gen_mode", "")
                        label = _NEW_GEN_MODE_LABELS.get(mode, mode)
                        if uid in USER_STATE:
                            del USER_STATE[uid]
                        caption = f"✅ {label} | {amount} руб."
                        tg_request(token, "sendDocument", {"chat_id": q["message"]["chat"]["id"], "caption": caption}, files={"document": (filename, pdf_bytes)})
                        _send_main_menu_button(token, q["message"]["chat"]["id"], tg_request)
                        continue

                    if q["data"] == "new_gen_sbp_extract":
                        state = USER_STATE.get(uid, {})
                        if not state.get("new_gen_pdf_bytes"):
                            tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": "❌ Сессия истекла. Начните заново."})
                            continue
                        state["awaiting"] = "new_gen_sbp_receipt"
                        tg_request(token, "sendMessage", {
                            "chat_id": q["message"]["chat"]["id"],
                            "text": (
                                "📎 Пришлите любой реальный СБП-чек (любого банка).\n\n"
                                "Бот извлечёт из него SBP ID и пересоберёт ваш чек с этим ID.\n"
                                "Чек не должен быть ранее проверен в системе."
                            ),
                        })
                        continue
                    # --- конец новых генераторов ---

                    # --- SBP пул: callback-меню ---
                    if q["data"] == "sbp_add_text":
                        if _sbp_pool is None:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Пул недоступен"})
                            continue
                        USER_STATE[uid] = {"awaiting": "sbp_add_text_input"}
                        tg_request(token, "sendMessage", {
                            "chat_id": q["message"]["chat"]["id"],
                            "text": (
                                "Отправьте один или несколько SBP ID (по одному на строку).\n\n"
                                "Пример:\n"
                                "A61051018121260A0B10040011740901\n"
                                "A61061126522550G0B100600117\n\n"
                                "Каждый ID — ровно 32 символа (буквы и цифры)."
                            ),
                        })
                        continue

                    if q["data"] == "sbp_add_pdf":
                        if _sbp_pool is None:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Пул недоступен"})
                            continue
                        USER_STATE[uid] = {"awaiting": "sbp_add_pdf_input"}
                        tg_request(token, "sendMessage", {
                            "chat_id": q["message"]["chat"]["id"],
                            "text": "Отправьте PDF любого СБП-чека. Бот извлечёт из него SBP ID и добавит в пул.",
                        })
                        continue

                    if q["data"] == "sbp_list":
                        if _sbp_pool is None:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Пул недоступен"})
                            continue
                        entries = _sbp_pool.list_available()
                        total, used, avail = _sbp_pool.status()
                        if not entries:
                            txt = f"📋 Доступных SBP ID: 0 (всего {total}, использовано {used})\n\nДобавьте через /sbp"
                        else:
                            lines = [f"📋 Доступные SBP ID ({avail} из {total}):"]
                            for e in entries[:20]:
                                bank = e.get("bank", "")
                                date = e.get("date", "")
                                lines.append(f"  `{e['id']}`  {bank} {date}".strip())
                            if avail > 20:
                                lines.append(f"  ... ещё {avail - 20}")
                            txt = "\n".join(lines)
                        tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": txt, "parse_mode": "Markdown"})
                        continue

                    if q["data"] == "sbp_clear_used":
                        if _sbp_pool is None:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Пул недоступен"})
                            continue
                        removed = _sbp_pool.clear_used()
                        total, used, avail = _sbp_pool.status()
                        tg_request(token, "sendMessage", {
                            "chat_id": q["message"]["chat"]["id"],
                            "text": f"🗑 Удалено использованных: {removed}\n\nОсталось доступных: {avail}",
                        })
                        continue

                    if q["data"] == "ak_keep_commission":
                        if uid not in USER_STATE or USER_STATE[uid].get("awaiting") != "ak_commission":
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Не тот шаг"})
                            continue
                        st = USER_STATE[uid]
                        st["ak_commission"] = None
                        st.pop("ak_commission_show_00", None)
                        st["awaiting"] = "ak_formed"
                        tg_request(token, "sendMessage", {
                            "chat_id": q["message"]["chat"]["id"],
                            "text": (
                                "🕐 Строка «Сформирована» (без секунд).\n"
                                "Формат: `ДД.ММ.ГГГГ ЧЧ:ММ` — мск подставится само.\n"
                                "Отправьте «—» чтобы оставить как в шаблоне."
                            ),
                            "parse_mode": "Markdown",
                        })
                        continue
                    if q["data"] == "main_db":
                        counts = get_bank_counts()
                        n_sbp, n_vtb, n_alfa = counts["vtb_sbp"], counts["vtb_vtb_vtb"], counts["alfa"]
                        # Подсчёт шаблонов выписок
                        stmt_base = _BOT_DIR / "база_выписок"
                        n_alfa_stmt = len(list(stmt_base.glob("*.pdf"))) if stmt_base.exists() else 0
                        vtb_tpl = stmt_base / "vtb_template.pdf"
                        n_vtb_stmt = 1 if vtb_tpl.exists() else 0
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                f"📂 База\n\n"
                                f"Чеки:\n"
                                f"  СБП: {n_sbp} | ВТБ→ВТБ: {n_vtb} | Альфа: {n_alfa}\n\n"
                                f"Шаблоны выписок:\n"
                                f"  Альфа: {n_alfa_stmt} | ВТБ: {n_vtb_stmt}\n\n"
                                "Чтобы добавить чек: выберите тип, затем загрузите PDF.\n"
                                "Если добавили файлы вручную — нажмите «Обновить индекс»."
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📋 СБП", "callback_data": "db_list_vtb_sbp"}, {"text": "➕ СБП", "callback_data": "db_add_vtb_sbp"}],
                                    [{"text": "📋 ВТБ→ВТБ", "callback_data": "db_list_vtb_vtb"}, {"text": "➕ ВТБ→ВТБ", "callback_data": "db_add_vtb_vtb"}],
                                    [{"text": "📋 Альфа", "callback_data": "db_list_alfa"}, {"text": "➕ Альфа", "callback_data": "db_add_alfa"}],
                                    [{"text": "🔄 Обновить индекс", "callback_data": "db_rebuild"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "db_rebuild":
                        try:
                            idx = build_and_save()
                            n_sbp = len(idx.get("vtb_sbp", {}))
                            n_vtb = len(idx.get("vtb_vtb_vtb", {}))
                            n_alfa = len(idx.get("alfa", {}))
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"✅ Индекс обновлён\n\nСБП: {n_sbp} | ВТБ на ВТБ: {n_vtb} | Альфа: {n_alfa}",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"⚠️ Ошибка: {e}",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                            })
                        continue
                    if q["data"] in ("db_add_vtb_sbp", "db_add_vtb_vtb", "db_add_alfa"):
                        if "vtb_sbp" in q["data"]:
                            bank = "vtb_sbp"
                        elif "vtb_vtb" in q["data"]:
                            bank = "vtb_vtb_vtb"
                        else:
                            bank = "alfa"
                        USER_STATE[uid] = {"awaiting": f"db_add_{bank}"}
                        lbl = {"vtb_sbp": "СБП", "vtb_vtb_vtb": "ВТБ на ВТБ", "alfa": "Альфа"}.get(bank, bank)
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"➕ Отправьте PDF-чек для добавления в базу ({lbl})",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_vtb_sbp":
                        idx = load_index()
                        items = list(idx.get("vtb_sbp", {}).keys())
                        lines = ["📋 Чеки СБП:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_vtb_vtb":
                        idx = load_index()
                        items = list(idx.get("vtb_vtb_vtb", {}).keys())
                        lines = ["📋 Чеки ВТБ на ВТБ:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_alfa":
                        idx = load_index()
                        items = list(idx.get("alfa", {}).keys())
                        lines = ["📋 Чеки Альфа:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "gen_bank_vtb":
                        prev = USER_STATE.get(uid, {})
                        raw_subtype = prev.get("gen_vtb_subtype", "vtb_sbp")
                        # Нормализуем: "vtb_vtb_vtb" → "vtb_vtb"
                        mode = "vtb_vtb" if "vtb_vtb" in raw_subtype else "vtb_sbp"
                        # Двойной клик: форма уже отправлена для этого режима — игнорируем
                        if prev.get("awaiting") == "new_gen_form" and prev.get("new_gen_mode") == mode:
                            continue
                        USER_STATE[uid] = {
                            "awaiting": "new_gen_form",
                            "new_gen_mode": mode,
                            "new_gen_values": {},
                        }
                        _new_gen_send_form(token, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue
                    if q["data"] in ("gen_keep_payer", "gen_keep_recipient", "gen_keep_phone", "gen_keep_bank", "gen_keep_account", "gen_keep_opid"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        subtype = state.get("gen_vtb_subtype", "vtb_sbp")
                        keep_map = {
                            "gen_keep_payer": ("gen_payer", "gen_recipient", "4️⃣ Получатель (ФИО):"),
                            "gen_keep_recipient": ("gen_recipient", "gen_phone" if subtype != "vtb_sbp" else None, "5️⃣ Телефон:" if subtype != "vtb_sbp" else ""),
                            "gen_keep_phone": ("gen_phone", "gen_bank", "6️⃣ Банк получателя:"),
                            "gen_keep_bank": ("gen_bank", "gen_account" if subtype == "vtb_sbp" else "gen_operation_id", "6️⃣ Счёт (4 цифры):" if subtype == "vtb_sbp" else "7️⃣ ID операции:"),
                            "gen_keep_account": ("gen_account", "gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:"),
                            "gen_keep_opid": ("gen_operation_id", None, ""),
                        }
                        field_key, next_aw, _ = keep_map[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Пусто"})
                        if next_aw:
                            _gen_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Генерирую чек..."})
                            if state.get("gen_vtb_subtype") == "vtb_sbp":
                                _run_sbp_generate(token, uid, cid, state, tg_request)
                            else:
                                _run_gen_patch(token, uid, cid, state, tg_request)
                        continue
                    if q["data"] == "main_new":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "📄 Загрузить чек\n\n"
                                "Отправьте PDF-чек — бот определит сумму автоматически.\n\n"
                                "После загрузки выберите банк и режим замены."
                            ),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_check"}]]}),
                        })
                        continue
                    if q["data"] == "main_stmt":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📋 Создать выписку\n\nВыберите банк:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏦 Альфа", "callback_data": "stmt_alfa_menu"},
                                        {"text": "🏛 ВТБ", "callback_data": "stmt_vtb_menu"},
                                        {"text": "🅃 ТБанк", "callback_data": "stmt_tbank_menu"},
                                    ],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "stmt_alfa_menu":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Выписка Альфа-Банк\n\nВыберите вариант:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_alfa_edit"}],
                                    [{"text": "📄 Выписка по чеку", "callback_data": "stmt_alfa_from_check"}],
                                    [{"text": "🔧 Создание с нуля", "callback_data": "stmt_alfa_new"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "stmt_vtb_menu":
                        vtb_tpl = _BOT_DIR / "база_выписок" / "vtb_template.pdf"
                        if not vtb_tpl.exists():
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 Выписка ВТБ\n\n⚠️ Шаблон выписки ВТБ не найден.\n\nДобавьте файл vtb_template.pdf в папку база_выписок/ и обновите индекс в разделе «База».",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                ]}),
                            })
                        else:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 Выписка ВТБ\n\nВыберите вариант:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_vtb_edit"}],
                                        [{"text": "📄 Выписка по чеку", "callback_data": "stmt_vtb_from_check"}],
                                        [{"text": "🔧 Создание с нуля", "callback_data": "stmt_vtb_new"}],
                                        [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                    ],
                                }),
                            })
                        continue
                    if q["data"] == "stmt_vtb_edit":
                        USER_STATE[uid] = {"mode": "sw_vtb_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование выписки ВТБ\n\nОтправьте PDF-файл вашей выписки ВТБ.\n\nБот отсканирует все поля и предложит пошаговое редактирование.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_vtb_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_vtb_from_check":
                        USER_STATE[uid] = {"mode": "sw_vtb_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка ВТБ по чеку\n\nОтправьте PDF-файл чека — данные будут перенесены в выписку ВТБ.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_vtb_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_vtb_new":
                        _sw_start_vtb_new(token, uid, q["message"]["chat"]["id"], tg_request)
                        continue
                    if q["data"] == "stmt_alfa_edit":
                        USER_STATE[uid] = {"mode": "sw_alfa_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование выписки Альфа\n\nОтправьте PDF-файл вашей выписки.\n\nБот отсканирует все 3 блока и предложит пошаговое редактирование каждого поля.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_alfa_from_check":
                        USER_STATE[uid] = {"mode": "sw_alfa_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка по чеку (Альфа)\n\nОтправьте PDF-файл чека — данные будут автоматически перенесены в выписку Альфа-Банка.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_alfa_new":
                        _sw_start_alfa_new(token, uid, q["message"]["chat"]["id"], tg_request)
                        continue

                    # ── TBank Statement menu ──
                    if q["data"] == "stmt_tbank_menu":
                        tpl_exists = TBANK_STATEMENT_TEMPLATE.exists()
                        if not tpl_exists:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🅃 Выписка ТБанк\n\n⚠️ Шаблон выписки ТБанк не найден.\n\nДобавьте файл «Справка о движении средств.pdf» в папку проекта или ~/Downloads/.",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                ]}),
                            })
                        else:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🅃 Выписка ТБанк\n\nВыберите вариант:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_tbank_edit"}],
                                        [{"text": "📄 Выписка по чеку", "callback_data": "stmt_tbank_from_check"}],
                                        [{"text": "🔧 Создание с нуля", "callback_data": "stmt_tbank_new"}],
                                        [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                    ],
                                }),
                            })
                        continue

                    if q["data"] == "stmt_tbank_edit":
                        USER_STATE[uid] = {"mode": "sw_tbank_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование выписки ТБанк\n\nОтправьте PDF-файл вашей выписки ТБанк.\n\nБот отсканирует все поля и предложит пошаговое редактирование.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_tbank_menu"}]]}),
                        })
                        continue

                    if q["data"] == "stmt_tbank_from_check":
                        USER_STATE[uid] = {"mode": "sw_tbank_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка ТБанк по чеку\n\nОтправьте PDF-файл чека — данные будут перенесены в выписку ТБанк.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_tbank_menu"}]]}),
                        })
                        continue

                    if q["data"] == "stmt_tbank_new":
                        _sw_start_tbank_new(token, uid, q["message"]["chat"]["id"], tg_request)
                        continue

                    if q["data"] in ("stmt_edit", "stmt_receipt"):
                        USER_STATE[uid] = {"mode": "sw_alfa_edit" if q["data"] == "stmt_edit" else "sw_alfa_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Отправьте PDF-файл.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"].startswith("sw_"):
                        _sw_handle_callback(token, uid, q, tg_request)
                        continue
                    if q["data"] == "main_back":
                        if uid in USER_STATE:
                            if "file_path" in USER_STATE[uid]:
                                try:
                                    os.unlink(USER_STATE[uid]["file_path"])
                                except OSError:
                                    pass
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": MAIN_MENU_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                        })
                        continue
                    if q["data"] == "main_changelog":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": CHANGELOG_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "main_stats":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": stats_format_today(),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "main_zayavki":
                        try:
                            txt, km = build_zayavki_list()
                            kb = json.loads(km)
                            kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": txt,
                                "reply_markup": json.dumps(kb),
                            })
                        except Exception as e:
                            print(f"  ⚠️ main_zayavki: {e}")
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "⚠️ Ошибка обновления. Попробуйте ещё раз.",
                            })
                        continue
                    if q["data"].startswith("v_"):
                        try:
                            zid = q["data"][2:] if len(q["data"]) > 2 else ""
                            if zid and (detail := build_zayavka_detail(zid)):
                                txt, km = detail
                                tg_request(token, "editMessageText", {
                                    "chat_id": q["message"]["chat"]["id"],
                                    "message_id": q["message"]["message_id"],
                                    "text": txt,
                                    "reply_markup": km,
                                })
                            else:
                                tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Заявка не найдена"})
                        except Exception as e:
                            print(f"  ⚠️ v_ callback: {e}")
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                        continue
                    if q["data"].startswith("st_"):
                        try:
                            parts = q["data"].split("_")
                            if len(parts) >= 3:
                                zid = "_".join(parts[1:-1])
                                short = parts[-1]
                                new_st = STATUS_SHORT.get(short)
                                if new_st and update_zayavka_status(zid, new_st):
                                    if detail := build_zayavka_detail(zid):
                                        txt, km = detail
                                        tg_request(token, "editMessageText", {
                                            "chat_id": q["message"]["chat"]["id"],
                                            "message_id": q["message"]["message_id"],
                                            "text": txt,
                                            "reply_markup": km,
                                        })
                                    else:
                                        txt, km = build_zayavki_list()
                                        kb = json.loads(km)
                                        kb["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "main_back"}])
                                        tg_request(token, "editMessageText", {
                                            "chat_id": q["message"]["chat"]["id"],
                                            "message_id": q["message"]["message_id"],
                                            "text": txt,
                                            "reply_markup": json.dumps(kb),
                                        })
                                else:
                                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Не удалось"})
                        except Exception as e:
                            print(f"  ⚠️ st_ callback: {e}")
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                        continue
                    if q["data"] == "cancel":
                        if uid in USER_STATE and "file_path" in USER_STATE[uid]:
                            try:
                                os.unlink(USER_STATE[uid]["file_path"])
                            except OSError:
                                pass
                            del USER_STATE[uid]
                        main_text = (
                            "👋 Главное меню.\n\n"
                            "📋 Отправьте PDF-чек или /start для начала."
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": main_text,
                            "reply_markup": json.dumps({"inline_keyboard": []}),
                        })
                        continue
                    if q["data"] == "vtb_mode_amount":
                        # redirect to new flow
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏛 ВТБ — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "vtb_mode_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["vtb_mode"] = "full"
                        USER_STATE[uid]["awaiting"] = "vtb_amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        amt_hint = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n" if current_amt else ""
                        txt = (
                            "🏛 ВТБ — все поля\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            f"{amt_hint}"
                            "1️⃣ Сумма: с какой на какую менять (например: 10 1000 или 50 50000)"
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": txt,
                        })
                        continue
                    if q["data"] in ("vtb_keep_payer", "vtb_keep_recipient", "vtb_keep_phone", "vtb_keep_bank"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid, mid = q["message"]["chat"]["id"], q["message"]["message_id"]
                        keep_map = {
                            "vtb_keep_payer": ("vtb_payer", "vtb_recipient", "4️⃣ Получатель (ФИО):"),
                            "vtb_keep_recipient": ("vtb_recipient", "vtb_phone", "5️⃣ Телефон получателя:"),
                            "vtb_keep_phone": ("vtb_phone", "vtb_bank", "6️⃣ Банк получателя:"),
                            "vtb_keep_bank": ("vtb_bank", None, ""),
                        }
                        field_key, next_aw, _ = keep_map[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _vtb_full_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_vtb_full_patch(token, uid, cid, state, tg_request)
                        continue
                    # ВТБ Трансгран: callback handlers
                    if q["data"] == "bank_vtb_transgran":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        state = USER_STATE[uid]
                        state["bank"] = "vtb_transgran"
                        try:
                            pdf_data = Path(state["file_path"]).read_bytes()
                            fields = extract_vtb_transgran_fields(pdf_data)
                        except Exception:
                            fields = {}
                        state["vt_fields"] = fields
                        field_lines = []
                        for label, key in [("Сумма операции", "amount"), ("Курс обмена", "rate"),
                                           ("Зачисление", "credited"), ("Телефон", "phone"),
                                           ("Дата", "date"), ("Получатель", "name"),
                                           ("Банк", "bank"), ("Страна", "country")]:
                            field_lines.append(f"  {label}: {fields.get(key, '—')}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🌍 ВТБ Трансгран (UZS)\n\n"
                                "Текущие поля чека:\n" + "\n".join(field_lines) + "\n\n"
                                "Зачисление считается автоматически (сумма × курс).\n"
                                "Имя получателя не меняется.\n\n"
                                "Введите новые значения пошагово."
                            ),
                        })
                        _vtb_tg_send_next(state, "vt_amount", q["message"]["chat"]["id"], token, tg_request)
                        continue
                    if q["data"] in ("vt_keep_amount", "vt_keep_phone", "vt_keep_date"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        keep_flow = {
                            "vt_keep_amount": ("vt_amount", "vt_phone"),
                            "vt_keep_phone":  ("vt_phone",  "vt_date"),
                            "vt_keep_date":   ("vt_date",   None),
                        }
                        field_key, next_aw = keep_flow[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _vtb_tg_send_next(state, next_aw, cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_vtb_transgran_patch(token, uid, cid, state, tg_request)
                        continue
                    # Альфа СБП: пропустить/оставить текущий шаг
                    if q["data"].startswith("alfa_sbp_skip_") or q["data"].startswith("alfa_sbp_keep_"):
                        if uid not in USER_STATE or USER_STATE[uid].get("mode") != "alfa_sbp_full":
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        is_keep = q["data"].startswith("alfa_sbp_keep_")
                        step_idx = int(q["data"].split("_")[-1])
                        field_list = state.get("_alfa_sbp_field_list", [])
                        if step_idx < len(field_list):
                            field_key = field_list[step_idx][0]
                            if is_keep:
                                curr_val = state.get("alfa_sbp_current", {}).get(field_key)
                                if curr_val:
                                    fields = state.setdefault("alfa_sbp_fields", {})
                                    if field_key == "account":
                                        fields["account_last4"] = str(curr_val)[-4:]
                                    else:
                                        fields[field_key] = curr_val
                        # Advance step
                        state["alfa_sbp_step"] = step_idx + 1
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅"})
                        if step_idx + 1 < len(field_list):
                            _, prompt_next = field_list[step_idx + 1]
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": f"Шаг {step_idx+2}/{len(field_list)}: {prompt_next}\nОтправьте - чтобы пропустить."})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Генерирую PDF..."})
                            _run_alfa_sbp_full_patch(token, uid, cid, state, tg_request)
                        continue
                    # Альфа СБП все поля: старый callback (для совместимости)
                    if q["data"] == "bank_alfa_sbp_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["mode"] = "alfa_sbp_full"
                        USER_STATE[uid]["alfa_sbp_step"] = 0
                        USER_STATE[uid]["alfa_sbp_fields"] = {}
                        _ALFA_SBP_FIELDS = [
                            ("amount", "💰 Сумма перевода (число, например: 5000)"),
                            ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
                            ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
                            ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
                            ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
                            ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
                        ]
                        USER_STATE[uid]["_alfa_sbp_field_list"] = _ALFA_SBP_FIELDS
                        _, prompt = _ALFA_SBP_FIELDS[0]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🏦 Альфа — все поля\n\n"
                                "Введите новые значения пошагово.\n"
                                "Отправьте - чтобы оставить поле без изменений.\n\n"
                                f"Шаг 1/{len(_ALFA_SBP_FIELDS)}: {prompt}"
                            ),
                        })
                        continue

                    # Альфа Трансгран: callback handlers
                    if q["data"] == "bank_alfa_transgran":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        state = USER_STATE[uid]
                        state["bank"] = "alfa_transgran"
                        try:
                            pdf_data = Path(state["file_path"]).read_bytes()
                            fields = extract_transgran_fields(pdf_data)
                        except Exception:
                            fields = {}
                        state["at_fields"] = fields
                        field_lines = []
                        for label, key in [("Сумма перевода", "amount"), ("Комиссия", "commission"),
                                           ("Курс", "rate"), ("Зачисление", "credited"),
                                           ("Телефон", "phone"), ("Получатель", "name"),
                                           ("Номер операции", "operation_id")]:
                            field_lines.append(f"  {label}: {fields.get(key, '—')}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🌐 Альфа Трансгран\n\n"
                                "Текущие поля чека:\n" + "\n".join(field_lines) + "\n\n"
                                "Введите новые значения пошагово.\n"
                                "Для каждого поля можно «Оставить текущим»."
                            ),
                        })
                        _alfa_tg_send_next(state, "at_amount", "", q["message"]["chat"]["id"], token, tg_request)
                        continue
                    if q["data"] in ("at_keep_amount", "at_keep_commission", "at_keep_rate", "at_keep_credited", "at_keep_phone", "at_keep_name", "at_keep_opid"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        # at_credited теперь вычисляется автоматически — пропускаем этот шаг
                        keep_flow = {
                            "at_keep_amount": ("at_amount", "at_commission"),
                            "at_keep_commission": ("at_commission", "at_rate"),
                            "at_keep_rate": ("at_rate", "at_phone"),  # пропускаем at_credited
                            "at_keep_credited": ("at_credited", "at_phone"),
                            "at_keep_phone": ("at_phone", "at_name"),
                            "at_keep_name": ("at_name", "at_operation_id"),
                            "at_keep_opid": ("at_operation_id", None),
                        }
                        field_key, next_aw = keep_flow[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _alfa_tg_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_alfa_transgran_patch(token, uid, cid, state, tg_request)
                        continue
                    # Новые обработчики банка
                    if q["data"] == "bank_alfa_sub":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Альфа Чек\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "bank_alfa_amount"}],
                                    [{"text": "💳 По карте", "callback_data": "bank_alfa_card"}],
                                    [{"text": "📋 Все поля (СБП)", "callback_data": "bank_alfa_full"}],
                                    [{"text": "🕐 Сформирована", "callback_data": "bank_alfa_formed"}],
                                    [{"text": "🌐 Трансгран", "callback_data": "bank_alfa_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "bank_vtb_sub":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        scan_tip = ""
                        try:
                            bad = scan_vtb_unsupported_chars(USER_STATE[uid]["file_path"])
                            if bad:
                                tips = [f"«{c}» → «{FALLBACK_TIPS.get(c, '?')}»" if c in FALLBACK_TIPS else f"«{c}»" for c in sorted(bad)]
                                scan_tip = f"\n\n⚠️ В чеке найдены буквы для замены: {', '.join(tips)}"
                        except Exception:
                            pass
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏛 ВТБ{scan_tip}\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "bank_vtb_amount"}],
                                    [{"text": "📋 Все поля", "callback_data": "bank_vtb_full"}],
                                    [{"text": "🌍 Трансгран", "callback_data": "bank_vtb_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue
                    # ── ТБанк sub-menu ──
                    if q["data"] == "bank_tbank_sub":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🅃 ТБанк\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "bank_tbank_amount"}],
                                    [{"text": "💰 Сумма + 4 цифры счёта", "callback_data": "bank_tbank_amount_account"}],
                                    [{"text": "📋 Все поля", "callback_data": "bank_tbank_full"}],
                                    [{"text": "🌍 Трансгран", "callback_data": "bank_tbank_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue

                    if q["data"] == "bank_tbank_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден."})
                            continue
                        USER_STATE[uid]["bank"] = "tbank"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            rtype = tbank_detect_type(inp)
                            fields = tbank_extract_fields(inp, rtype)
                            old_amt = fields.get("amount_small", "?").strip()
                            USER_STATE[uid]["tbank_type"] = rtype
                            USER_STATE[uid]["awaiting"] = "tbank_new_amount"
                            hint = _tbank_f2_digit_hint(inp)
                            rtype_label = TBANK_TYPE_LABELS.get(rtype, rtype.upper())
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    f"🅃 ТБанк — смена суммы\n"
                                    f"Тип: {rtype_label}\n"
                                    f"Текущая сумма: {old_amt} ₽\n\n"
                                    f"{hint}"
                                    "Введите новую сумму (число):"
                                ),
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"❌ Ошибка чтения чека: {e}",
                            })
                        continue

                    if q["data"] == "bank_tbank_amount_account":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден."})
                            continue
                        USER_STATE[uid]["bank"] = "tbank"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            rtype = tbank_detect_type(inp)
                            if rtype != "sbp":
                                tg_request(token, "editMessageText", {
                                    "chat_id": q["message"]["chat"]["id"],
                                    "message_id": q["message"]["message_id"],
                                    "text": (
                                        "❌ Режим «сумма + 4 цифры счёта» только для чека СБП "
                                        "(есть поле «Счёт списания»).\n\n"
                                        "Выберите другой режим или отправьте чек СБП."
                                    ),
                                    "reply_markup": json.dumps({"inline_keyboard": [
                                        [{"text": "⬅️ Назад к ТБанк", "callback_data": "bank_tbank_sub"}],
                                    ]}),
                                })
                                continue
                            fields = tbank_extract_fields(inp, rtype)
                            old_amt = fields.get("amount_small", "?").strip()
                            old_acct = fields.get("account", "—")
                            USER_STATE[uid]["tbank_type"] = rtype
                            USER_STATE[uid]["tbank_fields"] = fields
                            USER_STATE[uid]["awaiting"] = "tbank_ac_amount"
                            hint = _tbank_f2_digit_hint(inp)
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    "🅃 ТБанк — сумма и последние 4 цифры счёта\n\n"
                                    f"{hint}"
                                    f"Текущая сумма: {old_amt}\n"
                                    f"Текущий счёт: {old_acct}\n\n"
                                    "Шаг 1/2: введите новую сумму (число):"
                                ),
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"❌ Ошибка чтения чека: {e}",
                            })
                        continue

                    if q["data"] == "bank_tbank_transgran":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден."})
                            continue
                        USER_STATE[uid]["bank"] = "tbank"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            rtype = tbank_detect_type(inp)
                            if rtype != "transgran":
                                tg_request(token, "editMessageText", {
                                    "chat_id": q["message"]["chat"]["id"],
                                    "message_id": q["message"]["message_id"],
                                    "text": (
                                        "❌ Трансгран доступен только для чека типа «Трансгран» "
                                        "(по высоте страницы).\n\n"
                                        "Отправьте соответствующий PDF или выберите другой режим."
                                    ),
                                    "reply_markup": json.dumps({"inline_keyboard": [
                                        [{"text": "⬅️ Назад к ТБанк", "callback_data": "bank_tbank_sub"}],
                                    ]}),
                                })
                                continue
                            fields = tbank_extract_fields(inp, rtype)
                            old_amt = fields.get("amount_small", "?").strip()
                            old_cred = fields.get("credited_amt", "—")
                            USER_STATE[uid]["tbank_type"] = "transgran"
                            USER_STATE[uid]["tbank_fields"] = fields
                            USER_STATE[uid]["awaiting"] = "tbank_tg_amount"
                            hint = _tbank_f2_digit_hint(inp)
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    "🅃 ТБанк — трансгран\n\n"
                                    "Зачисление в валюте считается: **сумма в ₽ × курс** "
                                    "(курс — сколько единиц валюты за 1 ₽, напр. 0.1130).\n\n"
                                    f"{hint}"
                                    f"Текущая сумма: {old_amt}\n"
                                    f"Текущее зачисление: {old_cred}\n\n"
                                    "Шаг 1/2: введите новую сумму в ₽ (число):"
                                ),
                                "parse_mode": "Markdown",
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"❌ Ошибка: {e}",
                            })
                        continue

                    if q["data"] == "bank_tbank_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден."})
                            continue
                        USER_STATE[uid]["bank"] = "tbank"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            rtype = tbank_detect_type(inp)
                            fields = tbank_extract_fields(inp, rtype)
                            USER_STATE[uid]["tbank_type"] = rtype
                            USER_STATE[uid]["tbank_fields"] = fields
                            USER_STATE[uid]["tbank_changes"] = {}
                            field_keys = list(fields.keys())
                            USER_STATE[uid]["tbank_field_keys"] = field_keys
                            USER_STATE[uid]["tbank_field_idx"] = 0
                            USER_STATE[uid]["awaiting"] = "tbank_full_field"
                            _tbank_send_next_field(token, q["message"]["chat"]["id"], USER_STATE[uid])
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"❌ Ошибка: {e}",
                            })
                        continue

                    if q["data"] == "tbank_keep_field":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "tbank_full_field":
                            state = USER_STATE[uid]
                            state["tbank_field_idx"] += 1
                            if state["tbank_field_idx"] >= len(state["tbank_field_keys"]):
                                _run_tbank_full_patch(token, uid, q["message"]["chat"]["id"], state, tg_request)
                            else:
                                _tbank_send_next_field(token, q["message"]["chat"]["id"], state)
                        continue

                    if q["data"] == "tbank_amount_again":
                        cid = q["message"]["chat"]["id"]
                        if uid not in USER_STATE or "file_path" not in USER_STATE.get(uid, {}):
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        state = USER_STATE[uid]
                        inp = state["file_path"]
                        try:
                            rtype = state.get("tbank_type") or tbank_detect_type(inp)
                            state["tbank_type"] = rtype
                            state["awaiting"] = "tbank_new_amount"
                            hint = _tbank_f2_digit_hint(inp)
                            last_amt = state.get("tbank_new_amount")
                            last_disp = f" (прошлый раз: {int(last_amt):,} ₽)".replace(",", " ") if last_amt else ""
                            rtype_label = TBANK_TYPE_LABELS.get(rtype, rtype.upper())
                            tg_request(token, "sendMessage", {
                                "chat_id": cid,
                                "text": (
                                    f"🅃 ТБанк — смена суммы\n"
                                    f"Тип: {rtype_label}{last_disp}\n\n"
                                    f"{hint}"
                                    "Введите новую сумму (число):"
                                ),
                            })
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": f"❌ Ошибка: {e}"})
                        continue

                    if q["data"] == "tbank_done":
                        cid = q["message"]["chat"]["id"]
                        if uid in USER_STATE:
                            fp = USER_STATE[uid].get("file_path")
                            if fp:
                                try:
                                    os.unlink(fp)
                                except OSError:
                                    pass
                            del USER_STATE[uid]
                        tg_request(token, "sendMessage", {
                            "chat_id": cid,
                            "text": "✅ Готово. Для нового чека — отправьте PDF.",
                        })
                        continue

                    if q["data"] == "bank_manual_select":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📎 Выберите банк:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏦 Альфа", "callback_data": "bank_alfa_sub"},
                                        {"text": "🏛 ВТБ", "callback_data": "bank_vtb_sub"},
                                        {"text": "🅃 ТБанк", "callback_data": "bank_tbank_sub"},
                                    ],
                                    [{"text": "🔍 Авто — любой банк", "callback_data": "bank_auto_free"}],
                                    [{"text": "❌ Отмена", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue

                    if q["data"] == "bank_auto_free":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "auto"
                        USER_STATE[uid]["awaiting"] = "auto_replacements"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🔍 Авто — любой банк\n\n"
                                "Введите замены в формате:\n"
                                "(старое = новое)\n\n"
                                "Пример:\n(10 RUR = 10 000 RUR)\n(01.01.2025 = 20.03.2026)\n\n"
                                "⚠️ Авто-режим не гарантирует прохождение проверки."
                            ),
                        })
                        continue
                    if q["data"] == "bank_alfa_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "alfa"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        current_comm = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = current_comm
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏦 Альфа — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "bank_alfa_formed":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        inp = USER_STATE[uid]["file_path"]
                        # Read current formed time from PDF
                        _formed_cur = "?"
                        try:
                            import fitz as _fitz
                            _doc = _fitz.open(inp)
                            _txt = _doc[0].get_text() if _doc.page_count else ""
                            _doc.close()
                            import re as _re
                            _m = _re.search(r"Сформирована\s*\n?\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+мск)", _txt)
                            if _m:
                                _formed_cur = _m.group(1).strip()
                        except Exception:
                            pass
                        USER_STATE[uid]["awaiting"] = "alfa_formed_input"
                        USER_STATE[uid]["bank"] = "alfa"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                f"🕐 Изменить «Сформирована»\n\n"
                                f"Текущее: {_formed_cur}\n\n"
                                "Введите новое время:\n"
                                "• <b>ЧЧ:ММ</b> — только время (дата сохранится)\n"
                                "• <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> — дата и время"
                            ),
                            "parse_mode": "HTML",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "bank_alfa_sub"}]]}),
                        })
                        continue

                    if q["data"] == "bank_alfa_card":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "alfa"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from alfa_karta_service import extract_alfa_karta_fields
                            fields = extract_alfa_karta_fields(inp)
                            amt_raw = fields.get("amount_raw", "")
                            comm_raw = fields.get("commission_raw", "")
                            if not amt_raw:
                                tg_request(token, "editMessageText", {
                                    "chat_id": q["message"]["chat"]["id"],
                                    "message_id": q["message"]["message_id"],
                                    "text": "❌ Не удалось прочитать сумму из чека. Убедитесь что это квитанция Альфа «по карте».",
                                    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "bank_alfa_sub"}]]}),
                                })
                                continue
                            amt_display = amt_raw.replace("\xa0", " ").replace("RUR", "₽").strip()
                            comm_display = comm_raw.replace("\xa0", " ").replace("RUR", "₽").strip() if comm_raw else "нет"
                            USER_STATE[uid]["alfa_card_old_amount"] = amt_raw
                            USER_STATE[uid]["alfa_card_old_commission"] = comm_raw
                            USER_STATE[uid]["awaiting"] = "alfa_card_amount"
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    "💳 Альфа — по карте\n\n"
                                    f"📌 Текущая сумма: {amt_display}\n"
                                    f"📌 Текущая комиссия: {comm_display}\n\n"
                                    "Шаг 1/2: введите новую сумму (число, например: 40000)\n"
                                    "Отправьте - чтобы оставить без изменений."
                                ),
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"❌ Ошибка чтения чека: {e}",
                            })
                        continue
                    if q["data"] == "bank_vtb_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        current_comm = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = current_comm
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏛 ВТБ — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "bank_alfa_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        # Same as old bank_alfa_sbp_full
                        USER_STATE[uid]["mode"] = "alfa_sbp_full"
                        USER_STATE[uid]["alfa_sbp_step"] = 0
                        USER_STATE[uid]["alfa_sbp_fields"] = {}
                        _ALFA_SBP_FIELDS = [
                            ("amount", "💰 Сумма перевода (число, например: 5000)"),
                            ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
                            ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
                            ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
                            ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
                            ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
                        ]
                        USER_STATE[uid]["_alfa_sbp_field_list"] = _ALFA_SBP_FIELDS
                        _, prompt = _ALFA_SBP_FIELDS[0]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🏦 Альфа — все поля\n\n"
                                "Введите новые значения пошагово.\n"
                                "Отправьте - чтобы оставить поле без изменений.\n\n"
                                f"Шаг 1/{len(_ALFA_SBP_FIELDS)}: {prompt}"
                            ),
                        })
                        continue
                    if q["data"] == "bank_vtb_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "full"
                        USER_STATE[uid]["awaiting"] = "vtb_amount"
                        # Extract current values
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        amt_hint = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n" if current_amt else ""
                        txt = (
                            "🏛 ВТБ — все поля\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            f"{amt_hint}"
                            "1️⃣ Сумма: с какой на какую (например: 10 1000)"
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": txt,
                        })
                        continue
                    if q["data"] == "amount_skip_commission":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_commission":
                            USER_STATE[uid]["commission_to"] = None
                            _do_amount_patch(token, uid, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue
                    if q["data"] == "alfa_card_skip_comm":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "alfa_card_commission":
                            USER_STATE[uid]["alfa_card_new_commission"] = None
                            _run_alfa_card_patch(token, uid, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue

                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                        continue
                    # Legacy bank callbacks (for backwards compat)
                    bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_auto": "auto"}
                    if q["data"] in bank_map:
                        bank = bank_map[q["data"]]
                        USER_STATE[uid]["bank"] = bank
                        if bank == "vtb":
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 ВТБ\n\nВыберите режим:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "💰 Только сумма", "callback_data": "bank_vtb_amount"}],
                                        [{"text": "📋 Все поля", "callback_data": "bank_vtb_full"}],
                                        [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                    ],
                                }),
                            })
                        else:
                            # Old alfa/auto: trigger amount_new_only flow
                            inp = USER_STATE[uid]["file_path"]
                            try:
                                from receipt_db import get_receipt_amount
                                current_amt = get_receipt_amount(Path(inp))
                            except Exception:
                                current_amt = None
                            USER_STATE[uid]["current_amount"] = current_amt
                            USER_STATE[uid]["current_commission"] = extract_commission_from_pdf(inp)
                            USER_STATE[uid]["awaiting"] = "amount_new_only"
                            amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"Введите новую сумму:\n\n{amt_txt}",
                            })
                        continue
                    continue
            except Exception as _upd_err:
                import traceback
                print(f"❌ Ошибка обработки апдейта {upd.get('update_id')}: {_upd_err}", flush=True)
                traceback.print_exc()


VTB_GEN_VERSION = "2-bundled-templates"


def _check_vtb_templates_on_startup() -> None:
    """Log VTB template availability at bot start (verify deploy)."""
    try:
        sbp = pick_sbp_template()
        print(f"✅ VTB СБП шаблон ({VTB_GEN_VERSION}): {sbp.name}", flush=True)
    except FileNotFoundError as e:
        print(f"❌ VTB СБП шаблон НЕ НАЙДЕН ({VTB_GEN_VERSION}): {e}", flush=True)
        print("   Положите vtb_sbp_template.pdf в корень проекта и перезапустите бота.", flush=True)
    try:
        from vtb_scratch_service import pick_vtb_vtb_template
        vtb = pick_vtb_vtb_template()
        print(f"✅ VTB→ВТБ шаблон: {vtb.name}", flush=True)
    except Exception as e:
        print(f"⚠️ VTB→ВТБ шаблон: {e}", flush=True)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    _check_vtb_templates_on_startup()
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN (в .env или export)")
        return
    if _proxy_url:
        _masked = _proxy_url.split("@")[-1] if "@" in _proxy_url else _proxy_url[:50]
        print("🔒 Прокси:", _masked)
    # Проверка токена и сброс webhook (иначе getUpdates даёт 400)
    try:
        r = tg_request(token, "getMe")
        if r.get("ok"):
            print("✅ Бот:", r["result"].get("username", "?"))
            if _EFFECTIVE_ALLOWED_IDS:
                print("🔐 Доступ: только", len(_EFFECTIVE_ALLOWED_IDS), "пользовател(ей)")
            dw = tg_request(token, "deleteWebhook", {"drop_pending_updates": True})
            if not dw.get("ok"):
                print("⚠️ deleteWebhook:", dw.get("description", dw))
            run_bot(token)
        else:
            print("❌ Токен неверный:", r.get("description"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Токен неверный (401). Сделай в @BotFather:")
            print("   /mybots → выбери бота → API Token → Revoke current token")
            print("   Обнови .env с новым токеном")
        else:
            print("❌ Ошибка HTTP:", e.code, e.reason)


if __name__ == "__main__":
    main()
