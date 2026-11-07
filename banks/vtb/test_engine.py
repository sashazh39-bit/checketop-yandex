#!/usr/bin/env python3
"""Checks for the VTB SBP engine.

Run with: python3 test_engine.py
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
DONORS = sorted((_HERE / "donors_sbp").glob("*.pdf"))
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
    streams = list(re.finditer(rb"/Filter /FlateDecode\n>>\nstream\r\n", data))
    if not streams:
        streams = list(re.finditer(rb"/Filter /FlateDecode\n>>\r?\nstream\r?\n", data))
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
    print(f"\nStage 2: byte-for-byte round-trip ({len(DONORS)} SBP donors)")
    check("7 SBP donors present", len(DONORS) == 7, str(len(DONORS)))
    catalog = vtb_receipt.load_catalog()
    for path in DONORS:
        source = path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            receipt = vtb_receipt.Receipt(str(path), catalog)
            is_sbp = receipt.format == vtb_receipt.FORMAT_SBP
            receipt.save(str(tmp))
            via_model = tmp.read_bytes() == source
        finally:
            tmp.unlink(missing_ok=True)
        check(path.name, via_model and is_sbp, f"model={via_model} sbp={is_sbp}")
        check(f"{path.name} producer/title",
              b"openhtmltopdf.com" in source and b"Che" not in source[:80])
        rec = vtb_receipt.Receipt(str(path), catalog)
        check(f"{path.name} title Исходящий перевод СБП",
              rec.printed_fields.get("title") == "Исходящий перевод СБП")


def stage3_amount_only() -> None:
    print("\nStage 3: change only the amount (native SFPro)")
    donor_path = _HERE / "donors_sbp" / "vtb_sbp_02_5000.pdf"
    catalog = vtb_receipt.load_catalog()
    donor = vtb_receipt.Receipt(str(donor_path), catalog)
    before = dict(donor.printed_fields)
    receipt = vtb_receipt.Receipt(str(donor_path), catalog)
    receipt.set("amount", "1 234 ₽")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        receipt.save(str(tmp))
        reread = vtb_receipt.Receipt(str(tmp))
        after = dict(reread.printed_fields)
        changed = {k for k in before if after.get(k) != before[k]}
        check("amount re-reads as set", after.get("amount") == "1 234 ₽", after.get("amount", ""))
        check("only the amount changed", changed == {"amount"}, str(changed))
        raw = tmp.read_bytes()
        check("single-pass classic xref, no /XRef",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
              and b"/XRef" not in raw and raw.startswith(b"%PDF-1.4"),
              f"{len(raw)} vs {donor_path.stat().st_size}")
        check("Flate streams are zlib level 6 (78 9c)", _all_level6(tmp))
        if _has_qpdf():
            out = subprocess.run(["qpdf", "--check", str(tmp)],
                                 capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in out, out[-200:])
    finally:
        tmp.unlink(missing_ok=True)


def stage5_generate() -> None:
    print("\nStage 4/5: generate_vtb() — all fields + linked SBP id")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = td / "generated.pdf"
        result = engine.generate_vtb(
            amount="10 000",
            operation_dt=datetime(2026, 11, 3, 14, 25, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+7 900 351-70-80",
            recipient_name="Петров Иван Сергеевич",
            recipient_phone="+7 (963) 297-20-08",
            recipient_bank="Сбербанк",
            output_path=out,
            creation_delta_minutes=5,
        )
        check("returns the output path", Path(result) == out and out.is_file())
        receipt = vtb_receipt.Receipt(str(out))
        f = receipt.printed_fields
        check("date", f.get("date") == "03.11.2026, 14:25", f.get("date", ""))
        check("amount", f.get("amount") == "10 000 ₽", f.get("amount", ""))
        check("recipient / headline",
              f.get("recipient") == "Иван Сергеевич П."
              and f.get("headline") == "Иван Сергеевич П.",
              f.get("recipient", ""))
        check("sender", f.get("sender") == "Александр Евгеньевич Ж.", f.get("sender", ""))
        check("phone U+2011", f.get("phone_to") == "+7 (963) 297\u201120\u201108",
              f.get("phone_to", ""))
        check("bank", f.get("bank_to") == "Сбербанк", f.get("bank_to", ""))
        check("title", f.get("title") == "Исходящий перевод СБП")
        check("status", f.get("status") == "Выполнено")

        moment = engine.decode_sbp_moment(f["sbp_id"])
        check("sbp_id is 32 chars", len(f["sbp_id"]) == 32, f.get("sbp_id", ""))
        check("sbp_id decodes to operation (UTC, inherited seconds)",
              moment is not None and moment.replace(second=0) == datetime(2026, 11, 3, 14, 25, tzinfo=MSK),
              str(moment))

        raw = out.read_bytes()
        check("producer openhtmltopdf.com, title Чек-PDF",
              b"openhtmltopdf.com" in raw
              and b"/Title <FEFF04270435043A002D005000440046>" in raw)
        check("CreationDate = operation +5 min",
              b"/CreationDate (D:202611031430" in raw, raw[raw.find(b"CreationDate"):raw.find(b"CreationDate")+60] if b"CreationDate" in raw else b"")
        check("single-pass, zlib-6",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
              and b"/XRef" not in raw and _all_level6(out))
        if _has_qpdf():
            q = subprocess.run(["qpdf", "--check", str(out)],
                               capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in q, q[-200:])

        out2 = td / "kopecks.pdf"
        engine.generate_vtb(
            amount="12 995.74",
            operation_dt="20.10.2026 20:29",
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+79003517080",
            recipient_name="Жеребятьев Александр Евгеньевич",
            recipient_phone="+79632972008",
            recipient_bank="Т-Банк",
            output_path=out2,
        )
        f2 = vtb_receipt.Receipt(str(out2)).printed_fields
        check("kopecks with dot, T‑Банк alias",
              f2["amount"] == "12 995.74 ₽" and "Т" in f2["bank_to"],
              f"{f2.get('amount')} {f2.get('bank_to')}")

        out3 = td / "msg.pdf"
        engine.generate_vtb(
            amount=19,
            operation_dt="07.11.2026 11:10",
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+79003517080",
            recipient_name="Петров Иван Сергеевич",
            recipient_phone="+79632972008",
            recipient_bank="Сбербанк",
            message="ЮЖНЙЦУ",
            output_path=out3,
        )
        f3 = vtb_receipt.Receipt(str(out3)).printed_fields
        check("short message donor", f3.get("message") == "ЮЖНЙЦУ", f3.get("message", ""))

        out4 = td / "wb.pdf"
        engine.generate_vtb(
            amount=18,
            operation_dt="07.11.2026 11:12",
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+79003517080",
            recipient_name="Жеребятьев Александр Евгеньевич",
            recipient_phone="+79003517080",
            recipient_bank="Wildberries",
            output_path=out4,
        )
        f4 = vtb_receipt.Receipt(str(out4)).printed_fields
        check("Wildberries two-line bank",
              "Wildberries" in f4.get("bank_to", "") and "Банк" in f4.get("bank_to", ""),
              f4.get("bank_to", ""))

    print("\nRejections")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        base = dict(
            amount=10,
            operation_dt=datetime(2026, 11, 3, 14, 25, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+7 900 351-70-80",
            recipient_name="Петров Иван Сергеевич",
            recipient_phone="+7 963 297-20-08",
            recipient_bank="Сбербанк",
            output_path=out,
        )
        for label, override in (
            ("bad phone", {"recipient_phone": "123"}),
            ("glyph outside donor pool", {"recipient_name": "Qqq Qqq Q."}),
            ("zero amount", {"amount": 0}),
        ):
            try:
                engine.generate_vtb(**{**base, **override})
                raised = False
            except Exception:  # noqa: BLE001
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def main() -> int:
    if not DONORS:
        print("no donors in banks/vtb/donors_sbp/", file=sys.stderr)
        return 1
    print(vtb_receipt.load_catalog().summary())
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
