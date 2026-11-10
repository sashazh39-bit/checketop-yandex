#!/usr/bin/env python3
"""Каноническая схема координат ВТБ СБП.

Основа: layout_config.json (из scan_donors_align) + layout_overrides.json.
Гибрид: база из скана, ручные поправки в overrides.
"""
import json
import re
from pathlib import Path

_BASE_DIR = Path(__file__).parent
LAYOUT_CONFIG_PATH = _BASE_DIR / "layout_config.json"
LAYOUT_OVERRIDES_PATH = _BASE_DIR / "layout_overrides.json"


def load_layout_config() -> dict:
    """Загрузить конфиг: layout_config.json, поверх — layout_overrides.json."""
    config = {}
    if LAYOUT_CONFIG_PATH.exists():
        try:
            config = json.loads(LAYOUT_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    overrides = {}
    if LAYOUT_OVERRIDES_PATH.exists():
        try:
            overrides = json.loads(LAYOUT_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Merge: overrides применяются поверх config
    if op := overrides.get("pts"):
        pts = dict(config.get("pts", {}))
        for k, v in op.items():
            if not k.startswith("_"):
                pts[k] = v
        config["pts"] = pts
    if "wall" in overrides:
        config["wall"] = overrides["wall"]
    if "center_heading" in overrides:
        config["center_heading"] = overrides["center_heading"]
    if oy := overrides.get("y"):
        yc = dict(config.get("y", {}))
        for k, v in oy.items():
            if not k.startswith("_"):
                yc[k] = v
        config["y"] = yc
    return config

# Эталон отображения: чек 762×1514 px, отступ справа 70 px
REFERENCE_WIDTH_PX = 762
RIGHT_MARGIN_PX = 70
# (762-70)/762 ≈ 0.908 — доля ширины, на которой заканчивается последний символ
WALL_RATIO = (REFERENCE_WIDTH_PX - RIGHT_MARGIN_PX) / REFERENCE_WIDTH_PX

# Fallback при отсутствии MediaBox (медиана 163 доноров)
WALL = 257.0849


def get_page_width_pt(data: bytes) -> float | None:
    """Ширина страницы в pt из MediaBox. /MediaBox [x0 y0 x1 y1] → x1-x0."""
    m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+[\d.\-]+\s+([\d.\-]+)\s+[\d.\-]+", data)
    if m:
        try:
            return float(m.group(2)) - float(m.group(1))
        except ValueError:
            pass
    return None


def wall_from_fixed_margin(data: bytes) -> float | None:
    """WALL = позиция последнего символа: 70 px от правого края при Ш762.
    Возвращает None если MediaBox не найден."""
    w = get_page_width_pt(data)
    if w and w > 0:
        return w * WALL_RATIO
    return None


# Y координаты в stream — правый столбец, допуск ±0.5
Y_DATE = (275.25, 275.2, 274.5, 275.0)
Y_ACCOUNT = (251.25, 251.2, 250.5, 251.0)
Y_PAYER = (227.25, 227.2, 226.5, 227.0)
Y_RECIPIENT = (203.25, 203.2, 202.5, 203.0)
Y_MESSAGE = (263.25, 251.25, 237.75)  # Сообщение (если есть)
Y_PHONE = (179.25, 179.2, 178.5, 179.0)
Y_BANK = (155.25, 155.2, 154.5, 155.0)
Y_AMOUNT = 72.37499
Y_OPID = (297.43, 297.25, 297.4, 309.43, 309.4)
Y_CENTERED = (327.11, 327.11249, 327.2, 327.25, 327.43)  # ФИО под заголовком

# pts по полям: медиана по анализу 163 PDF (для fallback)
PTS_MEDIAN = {
    "date": 4.53,
    "payer": 4.66,
    "recipient": 4.66,
    "phone": 4.57,
    "bank": 4.92,
    "amount": 9.03,  # kern -11.11111, font 13.5
    "opid": 5.25,
    "account": 5.25,
}

# Fallback при отсутствии layout_config.json
SCAN_FIXED = {
    "wall": 257.0820,
    "pts_date": 4.50,
    "pts_account": 5.45,
    "pts_phone": 4.65,
    "pts_payer": 4.55,
    "pts_recipient": 4.55,
    "pts_amount": 6.2,
    "pts_bank": 4.92,
    "pts_opid": 5.25,
}


def get_layout_values() -> dict:
    """Единый источник: config + overrides, fallback → SCAN_FIXED, Y_*."""
    cfg = load_layout_config()
    wall = cfg.get("wall") or SCAN_FIXED["wall"]
    pts_cfg = cfg.get("pts", {})
    pts = dict(SCAN_FIXED)
    for k, v in pts_cfg.items():
        pts[k] = v
    center = cfg.get("center_heading")
    if center is None:
        center = (50 + wall) / 2
    def _to_tuple(v, default):
        if v is None:
            return default
        return tuple(v) if isinstance(v, list) else default

    y_cfg = cfg.get("y", {})
    y_vals = {
        "date": _to_tuple(y_cfg.get("date"), Y_DATE),
        "account": _to_tuple(y_cfg.get("account"), Y_ACCOUNT),
        "payer": _to_tuple(y_cfg.get("payer"), Y_PAYER),
        "recipient": _to_tuple(y_cfg.get("recipient"), Y_RECIPIENT),
        "message": _to_tuple(y_cfg.get("message"), Y_MESSAGE),
        "phone": _to_tuple(y_cfg.get("phone"), Y_PHONE),
        "bank": _to_tuple(y_cfg.get("bank"), Y_BANK),
        "opid": _to_tuple(y_cfg.get("opid"), Y_OPID),
        "centered": _to_tuple(y_cfg.get("centered"), Y_CENTERED),
        "amount": y_cfg.get("amount", Y_AMOUNT),
    }
    if isinstance(y_vals["amount"], list) and y_vals["amount"]:
        y_vals["amount"] = y_vals["amount"][0]
    elif not isinstance(y_vals["amount"], (int, float)):
        y_vals["amount"] = Y_AMOUNT
    y_tolerance = 0.15
    over = {}
    if LAYOUT_OVERRIDES_PATH.exists():
        try:
            over = json.loads(LAYOUT_OVERRIDES_PATH.read_text(encoding="utf-8"))
            y_tolerance = over.get("y_tolerance", y_tolerance)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "wall": wall,
        "center_heading": center,
        "pts": pts,
        "y": y_vals,
        "y_tolerance": y_tolerance,
    }


def get_pts_for_donor(pdf_path: Path) -> dict:
    """Извлечь pts из донора (wall, pts по полям). Fallback → PTS_MEDIAN."""
    try:
        from vtb_sber_reference import get_vtb_per_field_params
        p = get_vtb_per_field_params(pdf_path)
        out = dict(PTS_MEDIAN)
        out["wall"] = p.get("wall", WALL)
        for k, pk in [
            ("date", "pts_date"), ("payer", "pts_payer"), ("recipient", "pts_recipient"),
            ("phone", "pts_phone"), ("bank", "pts_bank"), ("amount", "pts_amount"),
            ("opid", "pts_opid"),
        ]:
            if p.get(pk) is not None:
                out[k] = p[pk]
        if p.get("pts_text") is not None:
            out["pts_text"] = p["pts_text"]
        return out
    except Exception:
        return dict(PTS_MEDIAN, wall=WALL)
