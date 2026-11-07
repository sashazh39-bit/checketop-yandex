#!/usr/bin/env python3
"""Tests for the MTS card-transfer engine.

Run:  python3 banks/mts/test_card_engine.py

Stage 2 — genuine card donors re-emitted unchanged are byte-identical.
Stage 3 — changing only the amount keeps other fields and stays single-pass.
Stage 4/5 — generate_mts_card() rewrites all fields in a single-pass file.
"""
from __future__ import annotations

import re
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mts_card_receipt import CardReceipt  # noqa: E402
from mts_receipt import Receipt  # noqa: E402
import engine  # noqa: E402

MSK = timezone(timedelta(hours=3))
CARD_DONORS = sorted((HERE / "donors_card").glob("*.pdf"))
OUT = HERE / "out"

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")
    return ok


def donor_tag(receipt: CardReceipt, family: str) -> str:
    return receipt._donor_tag(family).decode()


def identity_metadata(receipt: CardReceipt) -> dict:
    return dict(
        creation_date=receipt.donor_creation_date,
        correlation_id=receipt.donor_correlation_id,
        file_id=receipt.donor_file_id,
        subset_tags=(donor_tag(receipt, "regular"), donor_tag(receipt, "medium")),
    )


def stage2_roundtrip() -> None:
    print("== stage 2: card donor round-trip (byte-identical re-emit) ==")
    check("card donors present", len(CARD_DONORS) >= 2, str(len(CARD_DONORS)))
    for path in CARD_DONORS:
        receipt = CardReceipt(path)
        target = OUT / (path.stem + "_card_roundtrip.pdf")
        receipt.save(target, **identity_metadata(receipt))
        same = target.read_bytes() == path.read_bytes()
        check(f"{path.name} re-emit byte-identical", same,
              f"(donor {path.stat().st_size} B vs rebuilt {target.stat().st_size} B)")
        if not same:
            a, b = path.read_bytes(), target.read_bytes()
            i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]),
                     min(len(a), len(b)))
            print(f"      first diff at byte {i}: donor {a[max(0, i-16):i+16]!r}")
            print(f"                          rebuilt {b[max(0, i-16):i+16]!r}")
        target.unlink(missing_ok=True)


def stage3_amount_only() -> None:
    print("== stage 3: change only the amount ==")
    path = CARD_DONORS[0]
    receipt = CardReceipt(path)
    old_amount = receipt.fields["amount"]
    new_amount = "100,00 ₽" if old_amount != "100,00 ₽" else "500,00 ₽"
    before = dict(receipt.fields)
    receipt.set("amount", new_amount)
    receipt.set("total", new_amount)
    target = OUT / "card_stage3_amount.pdf"
    receipt.save(target, **identity_metadata(receipt))
    raw = target.read_bytes()
    check("single startxref / single %%EOF / no /Prev",
          raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
          and b"/Prev" not in raw)
    reread = CardReceipt(target)
    check("amount re-extracts", reread.fields["amount"] == new_amount,
          f"got {reread.fields['amount']!r}")
    check("total follows amount", reread.fields["total"] == new_amount)
    intact = all(reread.fields[f] == before[f]
                 for f in ("debit_card", "credit_card", "account", "fee",
                           "txcode", "datetime"))
    check("other fields intact", intact, str(reread.fields))
    target.unlink(missing_ok=True)


def stage5_generate() -> None:
    print("== stage 4/5: generate_mts_card() ==")
    out = engine.generate_mts_card(
        amount="225",
        operation_dt=datetime(2026, 11, 2, 0, 52, 20, tzinfo=MSK),
        recipient_card="2200 1234 5678 8429",
        sender_card="7826",
        sender_account="00000000700030617378",
        output_path=OUT / "engine_card.pdf",
    )
    r = CardReceipt(out)
    check("engine: debit mask", r.fields["debit_card"] == "•• 7826")
    check("engine: credit mask", r.fields["credit_card"] == "•• 8429")
    check("engine: amount form", r.fields["amount"] == "225,00 ₽")
    check("engine: total == amount", r.fields["total"] == "225,00 ₽")
    check("engine: fee 0,00", r.fields["fee"] == "0,00 ₽")
    check("engine: account groups",
          r.fields["account"] == "00000 00070 00306 17378")
    check("engine: datetime kept seconds",
          r.fields["datetime"] == "02.11.2026 00:52:20 (UTC+3)")
    check("engine: txcode 12 digits, date prefix 6305 (UTC doy of 01.11 21:52)",
          bool(re.fullmatch(r"6305\d{8}", r.fields["txcode"])),
          r.fields["txcode"])
    raw = out.read_bytes()
    check("engine: single pass",
          raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
          and b"/Prev" not in raw)
    # SBP parser must refuse a card receipt (different run count)
    try:
        Receipt(out)
        check("SBP Receipt refuses card layout", False, "no error")
    except Exception:  # noqa: BLE001
        check("SBP Receipt refuses card layout", True)

    out2 = engine.generate_mts_card(
        amount=133,
        operation_dt=datetime(2026, 11, 7, 12, 0, tzinfo=MSK),  # seconds inherited
        recipient_card="•• 9999",
        output_path=OUT / "engine_card_inherit.pdf",
    )
    r2 = CardReceipt(out2)
    check("engine: inherited seconds from donor",
          r2.fields["datetime"].endswith("(UTC+3)")
          and r2.fields["datetime"].startswith("07.11.2026 12:00:"),
          r2.fields["datetime"])
    check("engine: 07.11 noon UTC doy prefix 6311",
          r2.fields["txcode"].startswith("6311"), r2.fields["txcode"])


def main() -> int:
    OUT.mkdir(exist_ok=True)
    stage2_roundtrip()
    stage3_amount_only()
    stage5_generate()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
