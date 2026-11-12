#!/usr/bin/env python3
"""Сводка по bot_usage.jsonl за последние N суток.

Пример:
    python3 usage_report.py 2
    python3 usage_report.py 7
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot_usage_log import usage_log_path


def _parse_ts(raw: str) -> datetime:
    s = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Отчёт по журналу использования бота")
    parser.add_argument(
        "days",
        nargs="?",
        type=float,
        default=2.0,
        help="За сколько суток от текущего момента (UTC), по умолчанию 2",
    )
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        default=None,
        help="Путь к JSONL (по умолчанию как у бота: BOT_USAGE_LOG_PATH или bot_usage.jsonl)",
    )
    args = parser.parse_args()
    path = args.file or usage_log_path()
    if not path.is_file():
        print(f"Файл журнала не найден: {path}")
        print("События появятся после работы бота с включённой записью.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    events: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                ts = _parse_ts(row["ts"])
                if ts >= cutoff:
                    events.append(row)
            except (json.JSONDecodeError, KeyError, ValueError):
                bad += 1

    print(f"Период: последние {args.days} суток (UTC), с {cutoff.isoformat()}")
    print(f"Файл: {path}")
    print(f"Событий в периоде: {len(events)}")
    if bad:
        print(f"Пропущено битых строк: {bad}")
    print()

    if not events:
        return

    by_user: dict[int, int] = defaultdict(int)
    by_event = Counter()
    for row in events:
        by_user[int(row["user_id"])] += 1
        by_event[row["event"]] += 1

    print("По пользователям (user_id → число событий):")
    for uid, n in sorted(by_user.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {uid}: {n}")
    print()

    print("По типам событий:")
    for ev, n in by_event.most_common():
        print(f"  {ev}: {n}")
    print()

    print("Хронология (последние 30 записей в периоде):")
    tail = events[-30:]
    for row in tail:
        d = row.get("detail") or {}
        extra = f" {d}" if d else ""
        print(f"  {row['ts']} | {row['user_id']} | {row['event']}{extra}")


if __name__ == "__main__":
    main()
