#!/usr/bin/env python3
"""Checks for the VTB «Перевод на карту» engine (new openhtmltopdf donors).

Run with: python3 test_card_engine.py

The old ``vtb_card_receipt.py`` rebuilds a different layout and is not
exercised here.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import engine  # noqa: E402
import vtb_receipt  # noqa: E402

MSK = timezone(timedelta(hours=3))
CARD_DONORS = sorted((_HERE / "donors_card").glob("*.pdf"))
FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1
    return condition


def _has_qpdf() -> bool:
    return subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0


def _all_level6(path: Path) -> bool:
    data = path.read_bytes()
    ok = False
    for match in re.finditer(rb"stream\r\n", data):
        start = match.end()
        end = data.find(b"\r\nendstream", start)
        if end < 0:
            continue
        raw = data[start:end]
        if raw[:2] != b"\x78\x9c":
            return False
        try:
            if zlib.compress(zlib.decompress(raw), 6) != raw:
                return False
            ok = True
        except zlib.error:
            return False
    return ok


def stage2_roundtrip() -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(CARD_DONORS)} card donors)")
    check("3 card donors present", len(CARD_DONORS) == 3, str(len(CARD_DONORS)))
    catalog = vtb_receipt.load_catalog()
    for path in CARD_DONORS:
        source = path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            receipt = vtb_receipt.Receipt(str(path), catalog)
            is_card = receipt.format == vtb_receipt.FORMAT_CARD
            receipt.save(str(tmp))
            via_model = tmp.read_bytes() == source
        finally:
            tmp.unlink(missing_ok=True)
        f = vtb_receipt.Receipt(str(path), catalog).printed_fields
        check(path.name, via_model and is_card,
              f"model={via_model} card={is_card}")
        check(f"{path.name} heading Перевод на карту",
              f.get("title") == "Перевод на карту")
        check(f"{path.name} commission 99 ₽", f.get("fee") == "99 ₽", f.get("fee", ""))


def stage3_amount_only() -> None:
    print("\nStage 3: change only the amount")
    donor_path = CARD_DONORS[0]
    catalog = vtb_receipt.load_catalog()
    donor = vtb_receipt.Receipt(str(donor_path), catalog)
    before = dict(donor.printed_fields)
    receipt = vtb_receipt.Receipt(str(donor_path), catalog)
    receipt.set("amount", "50 ₽")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        receipt.save(str(tmp))
        after = dict(vtb_receipt.Receipt(str(tmp)).printed_fields)
        changed = {k for k in before if after.get(k) != before[k]}
        check("amount re-reads as set", after.get("amount") == "50 ₽", after.get("amount", ""))
        check("only the amount changed", changed == {"amount"}, str(changed))
        raw = tmp.read_bytes()
        check("single-pass, zlib-6, no /XRef",
              raw.count(b"startxref") == 1 and b"/XRef" not in raw and _all_level6(tmp))
        if _has_qpdf():
            out = subprocess.run(["qpdf", "--check", str(tmp)],
                                 capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in out, out[-200:])
    finally:
        tmp.unlink(missing_ok=True)


def stage5_generate() -> None:
    print("\nStage 4/5: generate_vtb_card() — amount + 99 ₽ commission")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = td / "card.pdf"
        result = engine.generate_vtb_card(
            amount=17,
            operation_dt=datetime(2026, 11, 7, 12, 0, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="220027******4432",
            recipient_bank="Совкомбанк",
            output_path=out,
            creation_delta_minutes=5,
        )
        check("returns the output path", Path(result) == out and out.is_file())
        f = vtb_receipt.Receipt(str(out)).printed_fields
        check("title", f.get("title") == "Перевод на карту")
        check("amount", f.get("amount") == "17 ₽", f.get("amount", ""))
        check("fee always 99 ₽", f.get("fee") == "99 ₽", f.get("fee", ""))
        check("total = amount + 99", f.get("total") == "116 ₽", f.get("total", ""))
        check("payee card mask", f.get("card") == "2200 27** **** 4432", f.get("card", ""))
        check("bank", f.get("bank_to") == "Совкомбанк", f.get("bank_to", ""))
        check("sender", f.get("sender") == "Александр Евгеньевич Ж.")
        raw = out.read_bytes()
        check("openhtmltopdf / Чек-PDF / zlib-6",
              b"openhtmltopdf.com" in raw and raw.count(b"startxref") == 1
              and b"/XRef" not in raw and _all_level6(out))
        if _has_qpdf():
            q = subprocess.run(["qpdf", "--check", str(out)],
                               capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in q, q[-200:])

        out2 = td / "tbank.pdf"
        engine.generate_vtb_card(
            amount=19,
            operation_dt="07.11.2026 11:16",
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="220039******9515",
            recipient_bank="Т-Банк",
            output_path=out2,
        )
        f2 = vtb_receipt.Receipt(str(out2)).printed_fields
        check("T‑Банк card donor + total 118 ₽",
              f2["total"] == "118 ₽" and "Т" in f2["bank_to"]
              and f2["card"] == "2200 39** **** 9515",
              f"{f2.get('total')} {f2.get('bank_to')} {f2.get('card')}")

    print("\nRejections")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        base = dict(
            amount=17,
            operation_dt="07.11.2026 12:00",
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="220027******4432",
            recipient_bank="Совкомбанк",
            output_path=out,
        )
        for label, override in (
            ("fee other than 99 ₽", {"fee": "50"}),
            ("bad card", {"recipient_card": "1234"}),
            ("SBP donor is not a card layout", {
                "donor_path": str(sorted((_HERE / "donors_sbp").glob("*.pdf"))[0])
            }),
        ):
            try:
                engine.generate_vtb_card(**{**base, **override})
                raised = False
            except Exception:  # noqa: BLE001
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def main() -> int:
    if not CARD_DONORS:
        print("no donors in banks/vtb/donors_card/", file=sys.stderr)
        return 1
    stage2_roundtrip()
    stage3_amount_only()
    stage5_generate()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
