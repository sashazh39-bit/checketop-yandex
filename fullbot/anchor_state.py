# -*- coding: utf-8 -*-
"""Общий профиль «живых» якорей чекер ↔ генератор (anchor_state.json).

Поддерживает ВСЕ 6 банков проекта (МТС, WB, Озон, ВТБ, Яндекс, РСХБ):
админ кидает настоящий PDF-чек любого из этих банков в бота-генератора
(или жмёт «Обновить якорь») — learn_from_pdf() обновляет:
  • счётчики транзакций МТС (019...0) и квитанций WB (102...);
  • живой лаг CreationDate Яндекса;
  • пул якорей SBP ID Озона (ozon_anchors.txt);
  • свежие узлы/шлюзы/флаги/эру НСПК (route + 10-значный suffix) из
    настоящего 32-символьного SBP ID любого из 6 банков в anchor_state.json,
    который читают и генераторы, и чекер.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MSK = timezone(timedelta(hours=3))
_HERE = Path(__file__).resolve().parent
STATE_PATH = os.environ.get("ANCHOR_STATE", str(_HERE / "anchor_state.json"))

_OPID_RE = re.compile(r"[AB][0-9A-Z]{31}")
_CD_RE = re.compile(rb"/CreationDate\s*\(([^)]*)\)")
_GW_A = {"1000", "1001", "1002", "1003", "1004", "1006", "1007", "1008", "1020"}
_GW_B = {"1009", "1010", "1011", "1012", "1013", "1014", "1016", "1017", "1018"}


def _parse_op_id(op_id: str | None) -> dict | None:
    if not op_id or len(op_id) != 32 or op_id[0] not in "AB":
        return None
    try:
        year = 2020 + int(op_id[1])
        doy = int(op_id[2:5])
        hh, mm, ss = int(op_id[5:7]), int(op_id[7:9]), int(op_id[9:11])
        ms = int(op_id[11:14])
        if not (1 <= doy <= 366 and hh < 24 and mm < 60 and ss < 60):
            return None
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        op_utc = base.replace(hour=hh, minute=mm, second=ss) + timedelta(milliseconds=ms)
        return {
            "prefix": op_id[0],
            "op_utc": op_utc,
            "flag": op_id[14],
            "node": op_id[15],
            "tail": op_id[16],
            "route": op_id[17],
            "gw": op_id[18:22],
            "suffix": op_id[22:32],
        }
    except (ValueError, IndexError):
        return None


def _valid_nspk_binding(parsed: dict | None) -> bool:
    """Связка prefix↔gateway НСПК (защита от обучения на грязном файле)."""
    if not parsed or parsed.get("tail") != "0":
        return False
    if not (parsed["node"].isalnum() and parsed["route"].isalpha() and parsed["suffix"].isdigit()):
        return False
    pool = _GW_A if parsed["prefix"] == "A" else _GW_B
    return parsed["gw"] in pool


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
        sign = 1 if tz_match.group(1).startswith("+") else -1
        offset = sign * (abs(int(tz_match.group(1))) * 60 + int(tz_match.group(2) or 0)) * 60
    return datetime(*map(int, m.groups()), tzinfo=timezone(timedelta(seconds=offset)))


def _detect_bank(raw: bytes, text: str) -> str | None:
    try:
        import receipt_validator as rv
        bank, *_ = rv._detect_bank(raw)
        if bank:
            return bank
    except Exception:
        pass
    if "Квитанция №" in text or "Вайлдберриз" in text:
        return "WB"
    if "Код транзакции" in text and "(UTC+3)" in text:
        return "MTS"
    if "Яндекс Банк" in text:
        return "YANDEX"
    if "ОЗОН БАНК" in text or "Ozon Банка" in text:
        return "OZON"
    if "Банк ВТБ (ПАО)" in text:
        return "VTB"
    if "Россельхозбанк" in text:
        return "RSHB"
    return None


def extract_profile(pdf_bytes: bytes) -> dict:
    """Вытащить якорные поля из оригинального PDF любого из 6 банков."""
    import fitz  # ленивый импорт: генераторы не обязаны зависеть от fitz

    raw = bytes(pdf_bytes)
    with fitz.open(stream=raw, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    compact = re.sub(r"\s+", "", text)
    op_match = _OPID_RE.search(compact)
    op_id = op_match.group(0) if op_match else None
    parsed = _parse_op_id(op_id)
    cd = _creation_date(raw)
    bank = _detect_bank(raw, text)

    out: dict[str, Any] = {
        "op_id": op_id,
        "parsed": parsed,
        "creation": cd,
        "bank": bank,
        "num": None,
        "dt": None,
    }

    # 1. WB: «Квитанция № …» + время ПЕЧАТИ (CreationDate).
    m = re.search(r"Квитанция\s*№\s*(\d{7,12})", text)
    if m and (bank in (None, "WB")):
        if cd is None:
            raise ValueError("WB: нет /CreationDate")
        d = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}(?::\d{2})?)", text)
        displayed = None
        if d:
            fmt = "%d.%m.%Y %H:%M:%S" if d.group(2).count(":") == 2 else "%d.%m.%Y %H:%M"
            displayed = datetime.strptime(d.group(0), fmt)
        out.update(kind="WB", num=int(m.group(1)),
                   dt=cd.astimezone(MSK).replace(tzinfo=None), displayed=displayed)
        return out

    # 2. МТС СБП: 12-значный «Код транзакции 019...0» + время операции (UTC+3).
    # Карточные коды 631... относятся к другой серии и намеренно отклоняются,
    # чтобы никогда не засорить СБП-якорь.
    m = re.search(r"Код\s+транзакции[\s:：]*(\d{12})", text)
    if m and (bank in (None, "MTS")):
        code = m.group(1)
        if not code.startswith("019"):
            raise ValueError(
                f"МТС: код {code} из карточной серии (для якоря СБП нужен чек с кодом 019…)"
            )
        d = re.search(
            r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s*\(UTC\s*\+\s*3\)", text
        )
        if not d:
            raise ValueError("МТС: не нашёл дату «(UTC+3)»")
        dt = datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)),
                      int(d.group(4)), int(d.group(5)), int(d.group(6)))
        out.update(kind="MTS", num=int(code), tr=code, dt=dt, displayed=dt)
        return out

    # 3. Яндекс: «Исх. № …» + лаг CreationDate относительно SBP ID.
    m = re.search(r"Исх\.\s*№\s*(\d{15})", text)
    if m and (bank == "YANDEX" or "Яндекс" in text):
        if not (parsed and cd):
            raise ValueError("Яндекс: нет SBP ID или CreationDate")
        lag = (cd - parsed["op_utc"]).total_seconds()
        out.update(kind="YANDEX", num=int(m.group(1)),
                   dt=cd.astimezone(MSK).replace(tzinfo=None), cd_lag=lag)
        return out

    # 4. Озон / ВТБ / РСХБ — любой СБП-чек с валидным 32-символьным SBP ID.
    if bank in ("OZON", "VTB", "RSHB") and parsed and _valid_nspk_binding(parsed):
        dt_msk = (cd.astimezone(MSK).replace(tzinfo=None)
                  if cd else (parsed["op_utc"] + timedelta(hours=3)).replace(tzinfo=None))
        lag = (cd - parsed["op_utc"]).total_seconds() if cd else None
        out.update(kind=bank, num=op_id, dt=dt_msk, cd_lag=lag)
        return out

    raise ValueError("чек без якорных полей (нужен СБП-чек МТС/WB/Озон/ВТБ/Яндекс/РСХБ)")


def load_state(path: str | Path | None = None) -> dict:
    try:
        return json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def live_sbp_profile(bank: str, path: str | Path | None = None) -> dict:
    """Свежий выученный профиль SBP ID для генератора банка (или {}).

    Узлы/шлюзы/флаги берутся по конкретному банку, а глобальная эра НСПК
    (route + 10-значный suffix) при необходимости подтягивается из самого
    свежего чека любого банка (nspk_current), т.к. НСПК крутит суффикс
    синхронно для всех банков.
    """
    st = load_state(path)
    cur = dict((st.get("sbp") or {}).get(bank.upper()) or {})
    glob = st.get("nspk_current") or {}
    if glob.get("route") and glob.get("suffix"):
        if not cur.get("latest_utc") or (glob.get("latest_utc") or "") > (cur.get("latest_utc") or ""):
            cur["route"] = glob["route"]
            cur["suffix"] = glob["suffix"]
    return cur


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


def _learn_sbp_fields(state: dict, bank: str, op_id: str | None,
                      parsed: dict | None, cd: datetime | None) -> list[str]:
    """Сохранить свежие route/suffix/node/gateway/lag из настоящего SBP ID."""
    if not (op_id and parsed and _valid_nspk_binding(parsed)):
        return []
    notes: list[str] = []
    # 1) глобальный список живых эр НСПК для чекера (на случай новой эры после G/0011890101)
    pair = [parsed["route"], parsed["suffix"]]
    eras = list(state.get("sbp_eras") or [])
    if pair not in eras:
        eras.append(pair)
        state["sbp_eras"] = eras[-16:]
        notes.append(f"НСПК эра {pair[0]}/{pair[1]}")

    # 2) глобальный текущий релиз НСПК (единый для всех 6 банков)
    op_iso = parsed["op_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
    sprint = int(parsed["suffix"][2:6]) if len(parsed["suffix"]) == 10 and parsed["suffix"].isdigit() else 0
    glob = dict(state.get("nspk_current") or {})
    if sprint >= 1189 and op_iso >= (glob.get("latest_utc") or ""):
        if (glob.get("route"), glob.get("suffix")) != (parsed["route"], parsed["suffix"]):
            notes.append(f"глобальный релиз НСПК → {parsed['route']}/{parsed['suffix']}")
        state["nspk_current"] = {
            "route": parsed["route"],
            "suffix": parsed["suffix"],
            "latest_utc": op_iso,
        }

    # 3) по-банковый профиль для генераторов (только относительно свежие ID)
    sbp_all = dict(state.get("sbp") or {})
    cur = dict(sbp_all.get(bank) or {})
    prev_era = (cur.get("route"), cur.get("suffix"))
    new_era = (parsed["route"], parsed["suffix"])
    prev_latest = cur.get("latest_utc") or ""

    # Не перетираем актуальную эру более старым архивным чеком.
    if not prev_latest and sprint < 1189 and op_iso < "2026-11-06T22:00:00Z":
        return notes
    if prev_latest and op_iso < prev_latest and new_era != prev_era:
        return notes

    changed = False
    if new_era != prev_era:
        # Банк перешёл на новую эру НСПК — начинаем свежие пулы этой эры.
        cur = {
            "route": parsed["route"],
            "suffix": parsed["suffix"],
            "nodes": [parsed["node"]],
            "gw_a": [parsed["gw"]] if parsed["prefix"] == "A" else [],
            "gw_b": [parsed["gw"]] if parsed["prefix"] == "B" else [],
            "flags": [parsed["flag"]],
            "samples": 1,
            "latest_utc": op_iso,
            "last_op_id": op_id,
        }
        changed = True
        notes.append(f"{bank} переход эры → {parsed['route']}/{parsed['suffix']}")
    else:
        nodes = list(cur.get("nodes") or [])
        if parsed["node"] not in nodes:
            nodes.append(parsed["node"])
            cur["nodes"] = sorted(nodes)
            changed = True
            notes.append(f"{bank} новый узел {parsed['node']}")
        gw_key = "gw_a" if parsed["prefix"] == "A" else "gw_b"
        gws = list(cur.get(gw_key) or [])
        if parsed["gw"] not in gws:
            gws.append(parsed["gw"])
            cur[gw_key] = sorted(gws)
            changed = True
            notes.append(f"{bank} шлюз {parsed['prefix']}→{parsed['gw']}")
        flags = list(cur.get("flags") or [])
        if parsed["flag"] not in flags:
            flags.append(parsed["flag"])
            cur["flags"] = sorted(flags)
            changed = True
        if cur.get("last_op_id") != op_id:
            cur["samples"] = int(cur.get("samples", 0)) + 1
            cur["last_op_id"] = op_id
            if op_iso >= prev_latest:
                cur["latest_utc"] = op_iso
            changed = True

    if cd is not None:
        lag = (cd - parsed["op_utc"]).total_seconds()
        if 0 <= lag <= 3600:
            lag_i = int(round(lag))
            cur["lag_min"] = min(int(cur.get("lag_min", lag_i)), lag_i)
            cur["lag_max"] = max(int(cur.get("lag_max", lag_i)), lag_i)

    if changed:
        cur["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sbp_all[bank] = cur
        state["sbp"] = sbp_all
    return notes


def learn_from_pdf(pdf_bytes: bytes, *, state_path: str | Path | None = None,
                   register_generator_anchor: bool = True) -> dict:
    """Обучиться на настоящем чеке любого из 6 банков: обновить state + генераторы.

    register_generator_anchor=False — обновить только anchor_state.json
    (когда якорь генератора уже добавлен потоком /anchor, чтобы не задваиваться).
    """
    path = Path(state_path or STATE_PATH)
    profile = extract_profile(pdf_bytes)
    state = load_state(path)
    learned: list[str] = []
    state_dirty = False
    kind = profile["kind"]

    if kind == "MTS":
        num_str = f"{int(profile['num']):012d}"
        if num_str.startswith("019"):
            current = (state.get("mts") or {}).get("counter")
            if current is None or not f"{int(current):012d}".startswith("019") or profile["num"] > int(current):
                state["mts"] = {"counter": int(profile["num"]),
                                "at": profile["dt"].strftime("%Y-%m-%d %H:%M:%S")}
                state_dirty = True
                learned.append(f"МТС код {num_str} @ {profile['dt']:%d.%m %H:%M:%S}")
            if register_generator_anchor:
                import mts_generator
                ok, text = mts_generator.add_anchor(profile["dt"], profile["num"], note="auto-anchor")
                if ok or "уже есть" not in str(text):
                    learned.append(f"генератор МТС: {'+' if ok else '⚠️'} {text}")
    elif kind == "WB":
        current = (state.get("wb") or {}).get("floor")
        floor = profile["num"] - 19  # небольшой буфер: никогда не режем ниже реального
        if current is None or floor > int(current):
            state["wb"] = {"floor": floor, "receipt": profile["num"],
                           "day": profile["dt"].strftime("%Y-%m-%d"),
                           "at": profile["dt"].strftime("%Y-%m-%d %H:%M:%S")}
            state_dirty = True
            learned.append(f"WB квитанция {profile['num']} @ {profile['dt']:%d.%m %H:%M:%S}")
        if register_generator_anchor:
            import wb_generator
            ok, text = wb_generator.add_anchor(profile["dt"], profile["num"], note="auto-anchor")
            if ok or "уже есть" not in str(text):
                learned.append(f"генератор WB: {'+' if ok else '⚠️'} {text}")
    elif kind == "YANDEX":
        lag = profile["cd_lag"]
        if 0 <= lag <= 86400:
            ya = dict(state.get("yandex") or {})
            lag_i = int(lag)
            ya["lag_min"] = min(int(ya.get("lag_min", lag_i)), lag_i)
            ya["lag_max"] = max(int(ya.get("lag_max", lag_i)), lag_i)
            ya["samples"] = int(ya.get("samples", 0)) + 1
            ya["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state["yandex"] = ya
            state_dirty = True
            learned.append(f"Яндекс CD-lag {lag:.0f}с (пул {ya['lag_min']}..{ya['lag_max']}с)")
    elif kind == "OZON":
        if register_generator_anchor and profile.get("op_id") and profile.get("parsed"):
            try:
                import ozon_anchors
                id_sec = profile["parsed"]["op_utc"].replace(microsecond=0, tzinfo=None)
                cd_utc = (profile["creation"].astimezone(timezone.utc).replace(tzinfo=None)
                          if profile.get("creation") else None)
                ok, text = ozon_anchors.add_anchor(
                    profile["op_id"], id_sec, cd_utc, note="auto-anchor"
                )
                if ok:
                    learned.append(f"Озон якорь {profile['op_id'][:16]}… (+ozon_anchors.txt)")
                elif "уже есть" not in str(text):
                    learned.append(f"Озон якорь: ⚠️ {text}")
            except Exception as exc:
                learned.append(f"Озон якорь: ⚠️ {exc}")

    # Для ВСЕХ 6 банков учим живой SBP-профиль (эра/узлы/шлюзы/лаг).
    prev_last_op = ((state.get("sbp") or {}).get(kind) or {}).get("last_op_id")
    sbp_notes = _learn_sbp_fields(
        state, kind, profile.get("op_id"), profile.get("parsed"), profile.get("creation")
    )
    if sbp_notes:
        state_dirty = True
        learned.extend(sbp_notes)
    elif profile.get("op_id") and profile["op_id"] != prev_last_op \
            and ((state.get("sbp") or {}).get(kind) or {}).get("last_op_id") == profile["op_id"]:
        state_dirty = True
        if kind in ("VTB", "RSHB"):
            p = profile["parsed"]
            learned.append(
                f"{kind} SBP-якорь учтён ({p['route']}/{p['suffix']}, узел {p['node']}, шлюз {p['gw']})"
            )

    if state_dirty:
        _save_state(state, path)
    return {"kind": kind, "num": profile["num"], "learned": learned, "state": state}
