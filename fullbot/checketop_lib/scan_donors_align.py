#!/usr/bin/env python3
"""Сканировать всех доноров и вычислить фиксированные координаты.

Сохраняет layout_config.json (база из скана) и выводит сводку.
Ручные поправки — в layout_overrides.json (применяются поверх).

Использование: python3 scan_donors_align.py
             python3 scan_donors_align.py --no-write  # только вывод, без записи
"""
from pathlib import Path
import json
import statistics
import argparse

from receipt_db import RECEIPT_BASE
from vtb_sber_reference import get_field_align_raw


def _med(arr: list, default: float) -> float:
    return statistics.median(arr) if arr else default


def _unique_rounded(arr: list[float], digits: int = 2) -> list[float]:
    """Уникальные значения, округлённые (для Y — допуск совпадения)."""
    rounded = [round(v, digits) for v in arr]
    return sorted(set(rounded))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="Не писать layout_config.json")
    args = ap.parse_args()

    base = RECEIPT_BASE
    donors_dir = base / "vtb" / "СБП"
    if not donors_dir.exists():
        donors_dir = Path(__file__).parent / "база_чеков" / "vtb" / "СБП"
    if not donors_dir.exists():
        print("Папка доноров не найдена:", donors_dir)
        return 1

    pdfs = list(donors_dir.glob("*.pdf"))
    print(f"Сканирую {len(pdfs)} доноров в {donors_dir.name}/")

    x1_static = {"date": [], "account": [], "phone": []}
    pts_all = {
        "date": [], "account": [], "phone": [],
        "payer": [], "recipient": [],
        "amount": [], "bank": [], "opid": [],
    }
    y_all = {
        "date": [], "account": [], "phone": [],
        "payer": [], "recipient": [],
        "amount": [], "bank": [], "opid": [], "centered": [],
    }
    center_headings: list[float] = []

    for p in sorted(pdfs):
        raw = get_field_align_raw(p)
        for k in ("date", "account", "phone"):
            if v := raw["x1"].get(k):
                x1_static[k].append(v)
            if v := raw["pts"].get(k):
                if 3.5 < v < 7.0:
                    pts_all[k].append(v)
            if v := raw.get("y", {}).get(k):
                y_all[k].append(v)
        for k in ("payer", "recipient"):
            if v := raw["pts"].get(k):
                if 4.0 < v < 7.0:
                    pts_all[k].append(v)
            if v := raw.get("y", {}).get(k):
                y_all[k].append(v)
        for k in ("amount", "bank", "opid"):
            if v := raw["pts"].get(k):
                if 3.0 < v < 12.0:
                    pts_all[k].append(v)
            if v := raw.get("y", {}).get(k):
                y_all[k].append(v)
        if ch := raw.get("center_heading"):
            center_headings.append(ch)

    wall_candidates = []
    for k in ("date", "account", "phone"):
        wall_candidates.extend(x1_static[k])
    fixed_wall = _med(wall_candidates, 257.1)

    config = {
        "wall": round(fixed_wall, 4),
        "center_heading": round(_med(center_headings, 188.5), 4),
        "pts": {
            "date": round(_med(pts_all["date"], 4.55), 4),
            "account": round(_med(pts_all["account"], 5.25), 4),
            "phone": round(_med(pts_all["phone"], 4.57), 4),
            "payer": round(_med(pts_all["payer"], 4.66), 4),
            "recipient": round(_med(pts_all["recipient"], 4.66), 4),
            "amount": round(_med(pts_all["amount"], 6.45), 4),
            "bank": round(_med(pts_all["bank"], 4.92), 4),
            "opid": round(_med(pts_all["opid"], 5.25), 4),
        },
        "y": {
            "date": _unique_rounded(y_all["date"])[:6] or [275.25],
            "account": _unique_rounded(y_all["account"])[:6] or [251.25],
            "payer": _unique_rounded(y_all["payer"])[:6] or [227.25],
            "recipient": _unique_rounded(y_all["recipient"])[:6] or [203.25],
            "phone": _unique_rounded(y_all["phone"])[:6] or [179.25],
            "bank": _unique_rounded(y_all["bank"])[:6] or [155.25],
            "amount": round(_med(y_all["amount"], 72.375), 5) if y_all["amount"] else 72.37499,
            "opid": _unique_rounded(y_all["opid"])[:6] or [297.43],
            "centered": _unique_rounded(y_all.get("centered", []))[:6] or [327.11, 327.25],
        },
    }
    config["_meta"] = {
        "donors_scanned": len(pdfs),
        "source": "scan_donors_align.py",
    }

    print("\n--- layout_config.json (база из скана) ---")
    print(f"  wall = {config['wall']}")
    print(f"  center_heading = {config['center_heading']}")
    for k, v in config["pts"].items():
        n = len(pts_all[k])
        print(f"  pts.{k} = {v}  (n={n})")
    for k in ("date", "account", "payer", "recipient", "phone"):
        print(f"  y.{k} = {config['y'][k][:3]}...")

    if not args.no_write:
        out_path = Path(__file__).parent / "layout_config.json"
        out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nЗаписано: {out_path}")

        overrides_path = Path(__file__).parent / "layout_overrides.json"
        if not overrides_path.exists():
            overrides_path.write_text(
                json.dumps({"pts": {}, "y_tolerance": 0.15, "_comment": "Ручные поправки поверх layout_config.json"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Создан пустой: {overrides_path}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
