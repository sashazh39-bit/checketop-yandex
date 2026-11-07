#!/usr/bin/env python3
"""Index untouched VTB PDFs into donor_db/catalog.json.

These files are layout/font/X references. Do not patch a file that already
went through Fraudex — its /ID and operation id are burned.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from vtb_card_receipt import extract_text, parse_objects, parse_w, stream_of

HERE = Path(__file__).resolve().parent
OUT = HERE / "donor_db"
OUT.mkdir(parents=True, exist_ok=True)

ROOTS = [
    HERE / "donors",
    Path("/Users/aleksandrzerebatav/Desktop/Проекты/чекетоп/база_чеков/vtb"),
]

# Confirmed verdicts only. Everything else is unknown.
KNOWN = {
    "full_alfa.pdf": "pass",
}


def kind_of(texts: list[str], page_h: str) -> str:
    joined = " ".join(texts)
    if "Исходящий перевод СБП" in joined:
        return "sbp"
    if "Перевод на карту" in joined:
        return "card_short" if page_h.startswith("432.75") else "card_full"
    return "other"


def field(texts: list[str], label: str) -> str | None:
    try:
        i = texts.index(label)
        return texts[i + 1]
    except (ValueError, IndexError):
        return None


def amount_x(cs: bytes) -> str | None:
    m = re.search(rb"1 0 0 1 ([\d.]+) 72.37499 Tm", cs)
    return m.group(1).decode() if m else None


def index_pdf(path: Path) -> dict | None:
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        return None
    objs = parse_objects(data)
    if 4 not in objs or 5 not in objs:
        return None
    texts = extract_text(data)
    if not texts:
        return None
    page = objs[4]
    mb = re.search(rb"/MediaBox \[([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)\]", page)
    page_w, page_h = (mb.group(3).decode(), mb.group(4).decode()) if mb else ("", "")
    font = None
    if 11 in objs:
        m = re.search(rb"/BaseFont /([A-Z]{6})\+", objs[11])
        if m:
            font = m.group(1).decode()
    idm = re.search(rb"/ID \[<([0-9A-Fa-f]+)>", data)
    cdm = re.search(rb"/CreationDate \(([^)]+)\)", objs.get(3, b""))
    cs = stream_of(objs[5])
    amt = next((t for t in texts if t.endswith("₽")), None)
    rec = {
        "path": str(path),
        "name": path.name,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "kind": kind_of(texts, page_h),
        "page_w": page_w,
        "page_h": page_h,
        "font": font,
        "id": idm.group(1).decode() if idm else None,
        "created": cdm.group(1).decode() if cdm else None,
        "amount": amt,
        "amount_x": amount_x(cs),
        "date": field(texts, "Дата операции"),
        "payer": field(texts, "Имя плательщика"),
        "payee": field(texts, "Получатель") or field(texts, "Карта получателя"),
        "account": field(texts, "Счет списания"),
        "bank": field(texts, "Банк получателя"),
        "sbp": field(texts, "ID операции в СБП"),
        "fraudex": KNOWN.get(path.name, "unknown"),
        "role": "layout",
    }
    if rec["kind"] == "sbp" and rec["sbp"] and rec.get("payee"):
        rec["sbp2"] = None
        for t in texts:
            if re.fullmatch(r"\d{7}", t):
                rec["sbp2"] = t
                break
    return rec


def main() -> None:
    rows = []
    seen = set()
    for root in ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.pdf")):
            try:
                rec = index_pdf(p)
            except Exception as e:
                print("ERR", p, e)
                continue
            if not rec:
                continue
            if rec["md5"] in seen:
                continue
            seen.add(rec["md5"])
            rows.append(rec)

    (OUT / "catalog.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    by_kind = defaultdict(int)
    amt_x = defaultdict(set)
    fonts = defaultdict(int)
    heights = defaultdict(int)
    for r in rows:
        by_kind[r["kind"]] += 1
        fonts[r.get("font") or "?"] += 1
        heights[r.get("page_h") or "?"] += 1
        if r["amount"] and r["amount_x"]:
            amt_x[r["amount"]].add(r["amount_x"])

    x_table = {a: sorted(xs) for a, xs in sorted(amt_x.items(), key=lambda kv: kv[0])}
    (OUT / "amount_x.json").write_text(
        json.dumps(x_table, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"indexed {len(rows)} unique PDFs → {OUT / 'catalog.json'}")
    print("kinds", dict(by_kind))
    print("page_h", dict(heights))
    print("unique amounts with X", len(x_table))
    print("fraudex labels", {k: sum(1 for r in rows if r["fraudex"] == k) for k in ("pass", "fail", "unknown")})
    conflict = {a: xs for a, xs in x_table.items() if len(xs) > 1}
    print("amount X conflicts", len(conflict))
    if conflict:
        for a, xs in list(conflict.items())[:8]:
            print(" ", a, xs)


if __name__ == "__main__":
    main()
