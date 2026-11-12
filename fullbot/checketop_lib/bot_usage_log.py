"""Журнал действий пользователей — только допись в файл на диске (не хранится в памяти бота).

Путь к файлу: переменная окружения BOT_USAGE_LOG_PATH или bot_usage.jsonl в корне проекта.
Файл в .gitignore (не в репозитории).

Отчёт за последние N дней:
    python3 usage_report.py 2

Либо откройте bot_usage.jsonl и вставьте фрагмент / прикрепите файл в чат с ассистентом.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_DEFAULT_PATH = Path(__file__).resolve().parent / "bot_usage.jsonl"


def usage_log_path() -> Path:
    p = os.environ.get("BOT_USAGE_LOG_PATH", "").strip()
    return Path(p).expanduser() if p else _DEFAULT_PATH


def log_usage(user_id: int | None, event: str, **detail: Any) -> None:
    """Одна строка JSON в конец файла. Ошибки записи игнорируются, чтобы не ломать бота."""
    if user_id is None:
        return
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "event": event,
    }
    extra = {k: v for k, v in detail.items() if v is not None}
    if extra:
        row["detail"] = extra
    line = json.dumps(row, ensure_ascii=False) + "\n"
    path = usage_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass
