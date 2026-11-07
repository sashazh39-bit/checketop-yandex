#!/usr/bin/env python3
"""Tests for the MTS statement engine.

Run:  python3 banks/mts/test_statement.py

Stage 2 — genuine statement donors re-emitted unchanged are byte-identical.
Stage 3 — changing only one operation amount stays single-pass.
Stage 4/5 — generate_mts_statement() with manual operations, receipts, fill.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mts_statement import (  # noqa: E402
    Statement, generate_mts_statement, operation_from_receipt,
    SUPPORTED_OP_COUNTS, print_statement_clock, format_line_amount,
    format_total, StatementError,
)

MSK = timezone(timedelta(hours=3))
DONORS = sorted((HERE / "donors_statement").glob("*.pdf"))
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


def identity_metadata(stmt: Statement) -> dict:
    return dict(
        creation_date=stmt.donor_creation_date,
        correlation_id=stmt.donor_correlation_id,
        file_id=stmt.donor_file_id,
        subset_tags={fam: stmt._donor_tag(fam).decode()
                     for fam in ("regular", "medium", "bold")},
    )


def stage2_roundtrip() -> None:
    print("== stage 2: statement donor round-trip (byte-identical re-emit) ==")
    check("statement donors present", len(DONORS) == 11, str(len(DONORS)))
    counts = {}
    for path in DONORS:
        stmt = Statement(path)
        counts[path.name] = (len(stmt.operations), len(stmt.blocked))
        target = OUT / (path.stem[:40] + "_stmt_roundtrip.pdf")
        stmt.save(target, **identity_metadata(stmt))
        same = target.read_bytes() == path.read_bytes()
        check(f"{path.name} re-emit byte-identical "
              f"({len(stmt.operations)} posted, {len(stmt.blocked)} blocked)",
              same,
              f"donor {path.stat().st_size} B vs rebuilt {target.stat().st_size} B")
        target.unlink(missing_ok=True)
    check("templates cover 0/2/3/5/20 posted",
          {n for n, _ in counts.values()} >= set(SUPPORTED_OP_COUNTS),
          str(sorted({n for n, _ in counts.values()})))


def stage3_amount_only() -> None:
    print("== stage 3: change only one posted amount ==")
    path = HERE / "donors_statement" / "Unknown (донор выписка мтс) 2.pdf"
    stmt = Statement(path)
    check("3-op donor parsed", len(stmt.operations) == 3, str(len(stmt.operations)))
    slot = stmt.operations[0]
    old = slot.amount_run.text
    new = "-25.00 RUR" if old != "-25.00 RUR" else "-35.00 RUR"
    before = [op.desc_run.text for op in stmt.operations]
    stmt.set_run(slot.amount_run, new)
    target = OUT / "stmt_stage3_amount.pdf"
    stmt.save(target, **identity_metadata(stmt))
    raw = target.read_bytes()
    check("single startxref / single %%EOF / no /Prev",
          raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
          and b"/Prev" not in raw)
    reread = Statement(target)
    check("amount re-extracts", reread.operations[0].amount_run.text == new,
          reread.operations[0].amount_run.text)
    check("descriptions intact",
          [op.desc_run.text for op in reread.operations] == before)
    target.unlink(missing_ok=True)


def stage5_generate() -> None:
    print("== stage 4/5: generate_mts_statement() ==")
    out0 = generate_mts_statement(
        period_start="01.11.2026", period_end="07.11.2026",
        issue_date="07.11.2026",
        holder_name="Иванов Иван Иванович",
        account="40817810200002566548",
        card="220383*1735",
        opening_balance="20.22",
        operation_count=0, fill_random=False,
        output_path=OUT / "stmt_engine_0.pdf",
    )
    s0 = Statement(out0)
    check("0 ops: holder", s0.field_runs["holder"].text == "Иванов Иван Иванович")
    check("0 ops: period", "01.11.2026" in s0.field_runs["period"].text
          and "07.11.2026" in s0.field_runs["period"].text)
    check("0 ops: totals + 0.0 / - 0.0",
          s0.field_runs["income_total"].text == "+ 0.0 RUR"
          and s0.field_runs["expense_total"].text == "- 0.0 RUR",
          f"{s0.field_runs['income_total'].text} {s0.field_runs['expense_total'].text}")
    check("0 ops: no posted rows", len(s0.operations) == 0)
    check("0 ops: single pass",
          out0.read_bytes().count(b"startxref") == 1
          and b"/Prev" not in out0.read_bytes())

    out3 = generate_mts_statement(
        period_start="06.11.2026", period_end="07.11.2026",
        issue_date="07.11.2026",
        holder_name="Васильев Роман Андреевич",
        operations=[
            {"datetime": "07.11.2026 01:16:03", "amount": "-15",
             "description": "Никита Валерьевич У Переводы"},
            {"datetime": "07.11.2026 01:10:48", "amount": "-17",
             "description": "Ильяс Ильнурович Х Переводы"},
            {"datetime": "07.11.2026 01:09:28", "amount": "+52",
             "description": "Демид Андреевич В Переводы"},
        ],
        output_path=OUT / "stmt_engine_3.pdf",
    )
    s3 = Statement(out3)
    check("3 ops: posted count", len(s3.operations) == 3)
    check("3 ops: income + 52.0", s3.field_runs["income_total"].text == "+ 52.0 RUR",
          s3.field_runs["income_total"].text)
    check("3 ops: expense - 32.0", s3.field_runs["expense_total"].text == "- 32.0 RUR",
          s3.field_runs["expense_total"].text)
    clocks = [op.datetime_run.text for op in s3.operations]
    check("3 ops: UTC−12 clock of 07.11 01:16:03 MSK → 06.11 10:16:03 (UTC)",
          clocks[0] == "06.11.2026 10:16:03 (UTC)", clocks)
    check("3 ops: signed line amounts",
          [op.amount_run.text for op in s3.operations]
          == ["-15.00 RUR", "-17.00 RUR", "+52.00 RUR"])
    check("3 ops: single pass",
          out3.read_bytes().count(b"startxref") == 1
          and out3.read_bytes().count(b"%%EOF") == 1)

    sbp = HERE / "donors" / "mts_donor_1.pdf"
    card = HERE / "donors_card" / "Unknown карта (донор мтс).pdf"
    imported = operation_from_receipt(sbp)
    check("receipt import: SBP outgoing negative",
          imported["amount"] < 0 and "Никита" in imported["description"],
          str(imported))
    imported_c = operation_from_receipt(card)
    check("receipt import: card → Перевод с карты",
          imported_c["description"] == "Перевод с карты Переводы"
          and imported_c["amount"] == -imported_c["amount"].copy_abs(),
          str(imported_c))

    out2 = generate_mts_statement(
        period_start="01.11.2026", period_end="07.11.2026",
        issue_date="07.11.2026",
        holder_name="Васильев Роман Андреевич",
        receipt_paths=[sbp, card],
        output_path=OUT / "stmt_engine_receipts.pdf",
    )
    s2 = Statement(out2)
    check("receipts: 2 posted + 1 blocked (donor-3 template)",
          len(s2.operations) == 2 and len(s2.blocked) == 1,
          f"{len(s2.operations)}+{len(s2.blocked)}")
    descs = [op.desc_run.text for op in s2.operations]
    check("receipts: SBP + card descriptions",
          "Никита Валерьевич У Переводы" in descs
          and "Перевод с карты Переводы" in descs, str(descs))

    out5 = generate_mts_statement(
        period_start="31.10.2026", period_end="07.11.2026",
        issue_date="07.11.2026",
        holder_name="Васильев Роман Андреевич",
        operation_count=5, fill_random=True, random_seed=1,
        output_path=OUT / "stmt_engine_5fill.pdf",
    )
    s5 = Statement(out5)
    check("fill_random: 5 posted", len(s5.operations) == 5)
    check("fill_random: 1 blocked kept", len(s5.blocked) == 1)
    check("fill_random: single pass",
          out5.read_bytes().count(b"startxref") == 1)

    out20 = generate_mts_statement(
        period_start="31.10.2026", period_end="07.11.2026",
        issue_date="07.11.2026",
        holder_name="Васильев Роман Андреевич",
        operation_count=20, fill_random=True, random_seed=7,
        output_path=OUT / "stmt_engine_20fill.pdf",
    )
    s20 = Statement(out20)
    check("fill_random: 20 posted", len(s20.operations) == 20)
    check("fill_random: 20-op no blocked", len(s20.blocked) == 0)
    check("fill_random: 20-op single pass",
          out20.read_bytes().count(b"startxref") == 1
          and b"/Prev" not in out20.read_bytes())

    try:
        generate_mts_statement(
            "01.11.2026", "07.11.2026", "07.11.2026",
            "Иванов Иван Иванович",
            operation_count=4, fill_random=True,
            output_path=OUT / "stmt_engine_bad.pdf",
        )
        check("unsupported count 4 refused", False)
    except StatementError as exc:
        check("unsupported count 4 refused", "no MTS statement donor" in str(exc),
              str(exc))

    check("print_statement_clock helper",
          print_statement_clock(datetime(2026, 11, 7, 1, 16, 3, tzinfo=MSK))
          == "06.11.2026 10:16:03 (UTC)")
    check("format_line_amount +52", format_line_amount(52) == "+52.00 RUR")
    check("format_total zero expense still minus",
          format_total(0, sign="-") == "- 0.0 RUR")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    stage2_roundtrip()
    stage3_amount_only()
    stage5_generate()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
