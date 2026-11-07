#!/usr/bin/env python3
"""Checks for the VTB account statement engine.

Run with: python3 test_statement.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import engine  # noqa: E402  — vtb engine, imported before rocket is on sys.path
import vtb_statement as vs  # noqa: E402

DONORS = sorted((_HERE / "donors_statement").glob("*.pdf"))
MSK = timezone(timedelta(hours=3))
FAILURES = 0
GEN_AT = datetime(2026, 11, 7, 12, 0, tzinfo=MSK)
HOLDER = "Жеребятьев Александр Евгеньевич"


def check(label: str, condition: bool, detail: str = "") -> bool:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1
    return condition


def _all_level6(path: Path) -> bool:
    pdf = __import__("rocket_pdf").load(str(path))
    streams = [
        o for o in pdf.objects.values()
        if o.stream_raw and b"/FlateDecode" in o.dict_bytes
    ]
    return bool(streams) and all(
        o.stream_raw[:2] == b"\x78\x9c"
        and zlib.compress(o.decoded_stream(), 6) == o.stream_raw
        for o in streams
    )


def stage2() -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(DONORS)} statement donors)")
    check("3 statement donors present", len(DONORS) == 3, str(len(DONORS)))
    expected = {
        "vtb_statement_01_empty.pdf": 0,
        "vtb_statement_02_ops_1p.pdf": 5,
        "vtb_statement_03_ops_2p.pdf": 15,
    }
    for path in DONORS:
        source = path.read_bytes()
        check(f"{path.name} OpenPDF 1.3.32 / Jasper 6.21.4 / >>stream",
              b"OpenPDF 1.3.32" in source
              and b"JasperReports Library version 6.21.4" in source
              and source.count(b">>stream\n") >= 5
              and source.count(b">>\nstream") == 0)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            vs.Statement(str(path)).save(str(tmp))
            check(f"{path.name} via Statement model", tmp.read_bytes() == source)
        finally:
            tmp.unlink(missing_ok=True)
        parsed = vs.parse_statement(vs.Statement(str(path)))
        want = expected.get(path.name)
        check(f"{path.name} {want} operations, newest first",
              len(parsed.operations) == want
              and parsed.account == "40817810590049129426"
              and parsed.holder == HOLDER,
              f"{len(parsed.operations)} ops holder={parsed.holder!r}")
        if parsed.operations:
            dates = [o.op_date for o in parsed.operations]
            check(f"{path.name} newest-first dates", dates == sorted(dates, reverse=True),
                  str(dates))
            check(f"{path.name} arithmetic",
                  parsed.closing == parsed.opening + parsed.income - parsed.expenses
                  and parsed.income == sum((o.amount for o in parsed.operations if o.amount > 0), Decimal("0"))
                  and parsed.expenses == -sum((o.amount for o in parsed.operations if o.amount < 0), Decimal("0")))


def stage3() -> None:
    print("\nStage 3: change only one operation amount (native ArialMT)")
    donor_path = _HERE / "donors_statement" / "vtb_statement_02_ops_1p.pdf"
    donor = vs.Statement(str(donor_path))
    parsed = vs.parse_statement(donor)
    slot = parsed.slots[0]
    before = [r.text for r in donor.runs]
    stmt = vs.Statement(str(donor_path))
    parsed2 = vs.parse_statement(stmt)
    stmt.set_run_text(parsed2.slots[0].amount_run, "-25.00 RUB", right_edge=228.0)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        stmt.save(str(tmp))
        reread = vs.parse_statement(vs.Statement(str(tmp)))
        check("amount re-reads as set", reread.operations[0].amount == Decimal("-25.00"),
              str(reread.operations[0].amount))
        after = [r.text for r in vs.Statement(str(tmp)).runs]
        diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        check("only the amount text changed",
              len(diff) == 1 and before[diff[0]].endswith("RUB"),
              str([(before[i], after[i]) for i in diff]))
        raw = tmp.read_bytes()
        check("classic xref, >>stream, zlib-6",
              raw.count(b"startxref") == 1 and b">>stream\n" in raw
              and b">>\nstream" not in raw and _all_level6(tmp))
        if subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0:
            out = subprocess.run(["qpdf", "--check", str(tmp)],
                                 capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in out, out[-200:])
    finally:
        tmp.unlink(missing_ok=True)


def stage4() -> None:
    print("\nStage 4/5: generate_vtb_statement() — header, ops, receipts, layouts")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = vs.generate_vtb_statement(
            period_start="01.11.2026", period_end="07.11.2026",
            holder_name=HOLDER, operation_count=5, fill_random=True,
            random_seed=1, output_path=td / "five.pdf", generated_at=GEN_AT,
        )
        parsed = vs.parse_statement(vs.Statement(str(out)))
        check("1-page / 5 ops", parsed.page_count == 1 and len(parsed.operations) == 5,
              f"{parsed.page_count}p {len(parsed.operations)}ops")
        check("period / holder / account",
              parsed.period_start.strftime("%d.%m.%Y") == "01.11.2026"
              and parsed.period_end.strftime("%d.%m.%Y") == "07.11.2026"
              and parsed.holder == HOLDER
              and parsed.account == "40817810590049129426")
        check("header arithmetic = operations",
              parsed.closing == parsed.opening + parsed.income - parsed.expenses
              and parsed.income == sum((o.amount for o in parsed.operations if o.amount > 0), Decimal("0"))
              and parsed.expenses == -sum((o.amount for o in parsed.operations if o.amount < 0), Decimal("0")),
              f"{parsed.opening} {parsed.income} {parsed.expenses} {parsed.closing}")
        check("newest-first",
              [o.op_date for o in parsed.operations]
              == sorted((o.op_date for o in parsed.operations), reverse=True))
        raw = out.read_bytes()
        check("CreationDate = generated_at, fresh /ID, OpenPDF",
              b"/CreationDate(D:20261107120000+03'00')" in raw
              and b"OpenPDF 1.3.32" in raw
              and b"JasperReports Library version 6.21.4" in raw)
        check(">>stream not >>\\nstream, zlib-6",
              raw.count(b">>stream\n") >= 5 and b">>\nstream" not in raw
              and _all_level6(out))
        if subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0:
            q = subprocess.run(["qpdf", "--check", str(out)],
                               capture_output=True, text=True).stdout
            check("qpdf clean (5 ops)", "No syntax or stream encoding errors" in q, q[-200:])

        empty = vs.generate_vtb_statement(
            period_start="05.11.2026", period_end="06.11.2026",
            holder_name="Петров Иван Сергеевич",
            operation_count=0, output_path=td / "empty.pdf", generated_at=GEN_AT,
        )
        pe = vs.parse_statement(vs.Statement(str(empty)))
        check("empty 1 page, 0 ops, holder patched, glyph 3 injected",
              pe.page_count == 1 and pe.operations == []
              and pe.holder == "Петров Иван Сергеевич"
              and pe.period_start.day == 5,
              pe.holder)

        two = vs.generate_vtb_statement(
            period_start="09.10.2026", period_end="28.10.2026",
            holder_name=HOLDER, operation_count=15, fill_random=True,
            random_seed=2, output_path=td / "fifteen.pdf", generated_at=GEN_AT,
        )
        p15 = vs.parse_statement(vs.Statement(str(two)))
        check("2-page / 15 ops", p15.page_count == 2 and len(p15.operations) == 15)
        check("15-op arithmetic",
              p15.closing == p15.opening + p15.income - p15.expenses)
        check("card slots kept on 2-page donor",
              any(o.kind == "card" for o in p15.operations))

        rcp = vs.generate_vtb_statement(
            period_start="01.11.2026", period_end="07.11.2026",
            holder_name=HOLDER,
            receipt_paths=[str(_HERE / "donors_sbp" / "vtb_sbp_03_13_ozon.pdf")],
            fill_random=True, random_seed=3,
            output_path=td / "from_rcp.pdf", generated_at=GEN_AT,
        )
        pr = vs.parse_statement(vs.Statement(str(rcp)))
        sbp = [o for o in pr.operations if o.amount == Decimal("-13.00")]
        check("SBP receipt becomes a −13.00 RUB operation",
              bool(sbp) and "ЯН" in sbp[0].counterparty,
              str([(o.amount, o.counterparty) for o in pr.operations]))

        card_rcp = vs.generate_vtb_statement(
            period_start="01.11.2026", period_end="07.11.2026",
            holder_name=HOLDER,
            receipt_paths=[str(_HERE / "donors_card" / "vtb_card_01_17_sovcom_4432.pdf")],
            fill_random=True, random_seed=4,
            output_path=td / "from_card.pdf", generated_at=GEN_AT,
        )
        pc = vs.parse_statement(vs.Statement(str(card_rcp)))
        check("card receipt uses 2-page layout, debit amount+99",
              pc.page_count == 2
              and any(o.amount == Decimal("-116.00") and o.kind == "card" for o in pc.operations),
              str([(o.kind, o.amount) for o in pc.operations if o.kind == "card"][:3]))

        manual = vs.generate_vtb_statement(
            period_start="01.11.2026", period_end="07.11.2026",
            holder_name=HOLDER,
            operations=[
                {"datetime": "07.11.2026", "amount": 15, "direction": "расход",
                 "kind": "sbp_out", "counterparty": "Иванов Иван Иванович"},
                {"datetime": "06.11.2026", "amount": 1000, "direction": "приход",
                 "kind": "internal", "counterparty": "Петров Петр Петрович"},
                {"datetime": "05.11.2026", "amount": 200, "direction": "расход",
                 "kind": "internal"},
                {"datetime": "04.11.2026", "amount": 50, "direction": "расход",
                 "kind": "sbp_out", "counterparty": "Сидоров Сидор Сидорович"},
                {"datetime": "02.11.2026", "amount": 3000, "direction": "приход",
                 "kind": "salary"},
            ],
            fill_random=False, output_path=td / "manual.pdf", generated_at=GEN_AT,
        )
        pm = vs.parse_statement(vs.Statement(str(manual)))
        check("manual 5 ops fill_random=False",
              len(pm.operations) == 5 and pm.page_count == 1
              and pm.operations[0].amount == Decimal("-15.00")
              and "ИВАНОВ" in pm.operations[0].counterparty,
              str([(o.amount, o.counterparty) for o in pm.operations]))

    print("\nRejections")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        base = dict(
            period_start="01.11.2026", period_end="07.11.2026",
            holder_name=HOLDER, output_path=out, generated_at=GEN_AT,
        )
        for label, override in (
            ("fill_random=False with 3 ops", {"operation_count": 3, "fill_random": False}),
            ("16 operations", {"operation_count": 16}),
            ("operation outside period", {
                "operations": [{"datetime": "20.12.2026", "amount": 10, "direction": "расход"}],
                "fill_random": False, "operation_count": 5,
            }),
            ("glyph outside Arial pool (Q)", {"holder_name": "Quinn Quinn Quinn", "operation_count": 0}),
        ):
            try:
                vs.generate_vtb_statement(**{**base, **override})
                raised = False
            except Exception:  # noqa: BLE001
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def main() -> int:
    if not DONORS:
        print("no donors in banks/vtb/donors_statement/", file=sys.stderr)
        return 1
    stage2()
    stage3()
    stage4()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
