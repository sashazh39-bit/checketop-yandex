# -*- coding: utf-8 -*-
"""Общий профиль «живых» якорей чекер ↔ генератор (anchor_state.json).

Бот-генератор вызывает learn_from_pdf() на присланных настоящих чеках —
счётчики МТС/WB и лаг CreationDate Яндекса обновляются без правки кода.
Бот-чекер читает тот же файл (CHECKER_ANCHORS_PATH) и применяет свежие
якоря в current_profile_consistency.

Файл — единственное место связи: генератор пишет, чекер читает. Эра и
пулы ID остаются в коде (меняются редко и требуют ревью), счётчики и
лаг — в файле (меняются каждый день).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
STATE_PATH = os.environ.get("ANCHOR_STATE", "/opt/mts-bot/anchor_state.json")

_OPID_RE = re.compile(r"[AB][0-9A-Z]{31}")
_CD_RE = re.compile(rb"/CreationDate\s*\(([^)]*)\)")


def _parse_op_id(op_id: str | None) -> dict | None:
    if not op_id or len(op_id) != 32 or op_id[0] not in "AB":
        return None
    try:
        year = 2020 + int(op_id[1])
        doy = int(op_id[2:5])
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        op_utc = base.replace(
            hour=int(op_id[5:7]), minute=int(op_id[7:9]), second=int(op_id[9:11])
        ) + timedelta(milliseconds=int(op_id[11:14]))
        return {"op_utc": op_utc, "suffix": op_id[22:32]}
    except (ValueError, IndexError):
        return None


def _creation_date(raw: bytes) -> datetime | None:
    match = _CD_RE.search(raw)
    if not match:
        return None
    value = match.group(1).decode("latin-1", "replace")
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", value)
    if not m:
        return None
    offset = 0
    tz_match = re.search(r"([+\-]\d{2})'?(\d{2})?'?$", value)
    if tz_match:
        sign = 1 if tz_match.group(1) == "+" else -1
        offset = sign * (abs(int(tz_match.group(1))) * 60 + int(tz_match.group(2))) * 60
    return datetime(*map(int, m.groups()), tzinfo=timezone(timedelta(seconds=offset)))


def extract_profile(pdf_bytes: bytes) -> dict:
    """Вытащить из чека то, по чему живут якоря. ValueError — если не наш формат."""
    import fitz  # ленивый импорт: генераторы не обязаны зависеть от fitz

    raw = bytes(pdf_bytes)
    with fitz.open(stream=raw, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    compact = re.sub(r"\s+", "", text)
    op_match = _OPID_RE.search(compact)
    op_id = op_match.group(0) if op_match else None
    parsed = _parse_op_id(op_id)
    cd = _creation_date(raw)

    out: dict = {"op_id": op_id, "parsed": parsed, "creation": cd}

    # WB: «Квитанция № …» + время ПЕЧАТИ (CreationDate), не время операции.
    m = re.search(r"Квитанция\s*№\s*(\d{7,12})", text)
    if m:
        if cd is None:
            raise ValueError("WB: нет /CreationDate")
        d = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}(?::\d{2})?)", text)
        displayed = None
        if d:
            fmt = "%d.%m.%Y %H:%M:%S" if d.group(2).count(":") == 2 else "%d.%m.%Y %H:%M"
            displayed = datetime.strptime(d.group(0), fmt)
        out.update(kind="WB", num=int(m.group(1)), dt=cd.astimezone(MSK).replace(tzinfo=None),
                   displayed=displayed)
        return out

    # МТС: «Код транзакции …» + время операции на чеке.
    m = re.search(r"Код\s+транзакции[\s:：]*(\d{12})", text)
    if m:
        d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s*\(UTC\s*\+\s*3\)", text)
        if not d:
            raise ValueError("МТС: не нашёл дату «(UTC+3)»")
        dt = datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)),
                      int(d.group(4)), int(d.group(5)), int(d.group(6)))
        out.update(kind="MTS", num=int(m.group(1)), dt=dt, displayed=dt)
        return out

    # Яндекс: «Исх. № …»; якорь — лаг CreationDate относительно момента в SBP ID.
    m = re.search(r"Исх\.\s*№\s*(\d{15})", text)
    if m and "Яндекс" in text:
        if not (parsed and cd):
            raise ValueError("Яндекс: нет SBP ID или CreationDate")
        lag = (cd - parsed["op_utc"]).total_seconds()
        out.update(kind="YANDEX", num=int(m.group(1)), dt=cd.astimezone(MSK).replace(tzinfo=None),
                   cd_lag=lag)
        return out

    raise ValueError("не WB/МТС/Яндекс-чек (нет якорных полей)")


def load_state(path: str | Path | None = None) -> dict:
    try:
        return json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict, path: str | Path) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="anchor_state.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def learn_from_pdf(pdf_bytes: bytes, *, state_path: str | Path | None = None,
                   register_generator_anchor: bool = True) -> dict:
    """Обучиться на настоящем чеке: обновить state + (опц.) якорь генератора.

    register_generator_anchor=False — только state (когда якорь генератора
    уже добавлен потоком /anchor, чтобы не задваиваться).
    """
    path = Path(state_path or STATE_PATH)
    profile = extract_profile(pdf_bytes)
    state = load_state(path)
    learned = []
    kind = profile["kind"]

    if kind == "MTS":
        current = (state.get("mts") or {}).get("counter")
        if current is None or profile["num"] > int(current):
            state["mts"] = {"counter": profile["num"],
                            "at": profile["dt"].strftime("%Y-%m-%d %H:%M:%S")}
            learned.append(f"МТС код {profile['num']} @ {profile['dt']:%d.%m %H:%M:%S}")
        if register_generator_anchor:
            import mts_generator
            ok, text = mts_generator.add_anchor(profile["dt"], profile["num"], note="auto-anchor")
            learned.append(f"генератор: {'+' if ok else '—'} {text}")
    elif kind == "WB":
        current = (state.get("wb") or {}).get("floor")
        floor = profile["num"] - 19  # небольшой буфер: никогда не режем ниже реального
        if current is None or floor > int(current):
            state["wb"] = {"floor": floor, "receipt": profile["num"],
                           "day": profile["dt"].strftime("%Y-%m-%d"),
                           "at": profile["dt"].strftime("%Y-%m-%d %H:%M:%S")}
            learned.append(f"WB квитанция {profile['num']} @ {profile['dt']:%d.%m %H:%M:%S}")
        if register_generator_anchor:
            import wb_generator
            ok, text = wb_generator.add_anchor(profile["dt"], profile["num"], note="auto-anchor")
            learned.append(f"генератор: {'+' if ok else '—'} {text}")
    elif kind == "YANDEX":
        lag = profile["cd_lag"]
        if 0 <= lag <= 86400:
            ya = state.get("yandex") or {}
            lag_i = int(lag)
            ya["lag_min"] = min(int(ya.get("lag_min", lag_i)), lag_i)
            ya["lag_max"] = max(int(ya.get("lag_max", lag_i)), lag_i)
            ya["samples"] = int(ya.get("samples", 0)) + 1
            ya["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state["yandex"] = ya
            learned.append(f"Яндекс CD-lag {lag:.0f}с (пул {ya['lag_min']}..{ya['lag_max']}с)")

    if learned:
        _save_state(state, path)
    return {"kind": kind, "num": profile["num"], "learned": learned, "state": state}
