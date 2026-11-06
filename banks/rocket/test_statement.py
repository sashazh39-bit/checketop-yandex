#!/usr/bin/env python3
"""Checks for the Rocket statement engine, in the order of the chat's stages.

Run with: python3 test_statement.py

Stage 2 — a genuine statement donor opened and saved unchanged is
          byte-for-byte identical (all 8 donors).
Stage 3 — changing only one operation amount uses the native
          IBM-Semi-Bold font, leaves every other text run untouched,
          and the font subset built for a donor's own text is
          byte-identical to that donor's own font.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import rocket_statement as rs  # noqa: E402

DONORS = sorted((_HERE / "donors_statement").glob("statement_rocket_*.pdf"))
FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1
    return condition


def stage2() -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(DONORS)} statement donors)")
    check("donors 11-18 present", len(DONORS) == 8, str(len(DONORS)))
    for path in DONORS:
        source = path.read_bytes()
        check(f"{path.name} low-level PdfFile", rs.StatementPdf(source).rebuild() == source)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            rs.Statement(str(path)).save(str(tmp))
            check(f"{path.name} via Statement model", tmp.read_bytes() == source)
        finally:
            tmp.unlink(missing_ok=True)


def _all_level9(path: Path) -> bool:
    import zlib

    pdf = rs.StatementPdf(path.read_bytes())
    streams = [o for o in pdf.streams.values() if o.is_flate and b"/Image" not in o.dict_bytes]
    return bool(streams) and all(
        o.raw[:2] == b"\x78\xda" and zlib.compress(o.decoded, 9) == o.raw for o in streams
    )


def stage3() -> None:
    print("\nStage 3: change only one operation amount (native font)")
    donor_path = _HERE / "donors_statement" / "statement_rocket_15.pdf"
    donor = rs.Statement(str(donor_path))
    amount_runs = [r for r in donor.runs if r.text.startswith("+100,00")]
    check("one incoming amount run found", len(amount_runs) == 1, str([r.text for r in donor.runs if "100" in r.text]))
    if not amount_runs:
        return
    before_texts = [r.text for r in donor.runs]

    stmt = rs.Statement(str(donor_path))
    run = [r for r in stmt.runs if r.text.startswith("+100,00")][0]
    check("amount font is IBM (SBKLGE)", stmt.fonts[run.font].basefont == "IBM-Semi-Bold", stmt.fonts[run.font].basefont)
    stmt.set_run_text(run, "+250,00\u00a0RUR", right_align=True)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        stmt.save(str(tmp))
        reread = rs.Statement(str(tmp))
        changed = [(a.text, b.text) for a, b in zip(
            rs.Statement(str(tmp)).runs, rs.Statement(str(donor_path)).runs)]
        after_texts = [r.text for r in reread.runs]
        diff = [i for i, (a, b) in enumerate(zip(before_texts, after_texts)) if a != b]
        check("amount re-reads as set", after_texts[diff[0]] == "+250,00\u00a0RUR" if diff else False,
              str([after_texts[i] for i in diff]))
        check("only the amount text changed", len(diff) == 1 and before_texts[diff[0]].startswith("+100,00"), str(diff))
        raw = tmp.read_bytes()
        check("single xref stream, no incremental tail",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1 and b"/Prev" not in raw,
              f"{len(raw)} vs {donor_path.stat().st_size}")
        check("all non-image Flate streams are native zlib level 9 (78 da)", _all_level9(tmp))
        if subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0:
            out = subprocess.run(["qpdf", "--check", str(tmp)], capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in out, out[-200:])
    finally:
        tmp.unlink(missing_ok=True)

    print("\nStage 3b: font built for a donor's own text is byte-identical")
    stmt = rs.Statement(str(donor_path))
    stmt.generation_dt = stmt.font_modified(next(iter(stmt.fonts.values())))
    # Force font rebuild with the same character sets / same timestamp:
    run = [r for r in stmt.runs if r.text.startswith("+100,00")][0]
    stmt.set_run_text(run, "+100,00\u00a0RUR", right_align=True)
    run.desired_text = run.text  # no textual change, but save() must still rebuild fonts
    # mark a textual no-op change so save() goes through the rebuild path
    run.desired_text = run.text.replace("100", "100")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        # generation_dt alone must trigger the rebuild path
        stmt.save(str(tmp))
        orig = rs.StatementPdf(donor_path.read_bytes())
        new = rs.StatementPdf(tmp.read_bytes())
        for slot in stmt.fonts.values():
            pass
        donor_stmt = rs.Statement(str(donor_path))
        same_fonts = True
        for res, slot in donor_stmt.fonts.items():
            if orig.stream(slot.fontfile_num).decoded != new.stream(slot.fontfile_num).decoded:
                same_fonts = False
            if orig.stream(slot.tounicode_num).decoded != new.stream(slot.tounicode_num).decoded:
                same_fonts = False
            if orig.comp_body(slot.cidfont_num) != new.comp_body(slot.cidfont_num):
                same_fonts = False
        check("font files + ToUnicode + /W byte-identical to donor's own", same_fonts)
    finally:
        tmp.unlink(missing_ok=True)


def stage3c_font_injection() -> None:
    print("\nStage 3c: glyph injection into a donor font is byte-identical")
    target_path = _HERE / "donors_statement" / "statement_rocket_15.pdf"
    source_path = _HERE / "donors_statement" / "statement_rocket_18.pdf"
    target = rs.Statement(str(target_path))
    stmt = rs.Statement(str(source_path))
    stmt.generation_dt = stmt.font_modified(next(iter(stmt.fonts.values())))
    # rebuild 18's fonts to 15's character sets (15's own timestamp is
    # not used; the font must equal 15's except head.modified/checksum,
    # so set the generation moment to 15's font moment first)
    target_stmt = rs.Statement(str(target_path))
    stmt.generation_dt = target_stmt.font_modified(next(iter(target_stmt.fonts.values())))
    ok = True
    for res, slot in stmt.fonts.items():
        entries = list(target_stmt.fonts[res].cid_to_char.items())
        stmt._rebuild_font(slot, entries)
        if stmt.pdf.stream(slot.fontfile_num).decoded != target_stmt.pdf.stream(target_stmt.fonts[res].fontfile_num).decoded:
            ok = False
    check("18 + injected glyphs == 15's own fonts (both families)", ok)


def _subsets_exact(stmt) -> bool:
    used = {}
    for run in stmt.runs:
        used.setdefault(run.font, set()).update(run.text)
    for res, chars in used.items():
        slot = stmt.fonts[res]
        if set(slot.cid_to_char.values()) != chars:
            return False
    return True


def stage4() -> None:
    print("\nStage 4/5: generate_rocket_statement() — all fields + linked data")
    from datetime import datetime, timezone, timedelta
    from decimal import Decimal
    import re as _re

    msk = timezone(timedelta(hours=3))
    gen_at = datetime(2026, 11, 3, 15, 30, tzinfo=msk)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # target statement from the chat: period 16-23.10, 6 operations
        out = rs.generate_rocket_statement(
            period_start="16.10.2026", period_end="23.10.2026", issue_date="03.11.2026",
            holder_name="Жеребятьев Александр Евгеньевич",
            account="40817810450231364892", opening_balance="16,00",
            operation_count=6, operation_date="20.10.2026",
            output_path=td / "six.pdf", generated_at=gen_at, random_seed=42,
        )
        stmt = rs.Statement(str(out))
        parsed = rs.parse_statement(stmt)
        f = {
            "period": parsed.period_runs[0].text,
            "issue": parsed.issue_runs[0].text,
            "fio": parsed.fio_run.text,
            "account": parsed.account_run.text,
        }
        check("period run", f["period"] == "С 16.10.2026 ПО 23.10.2026", f["period"])
        check("issue date run", f["issue"] == "03.11.2026", f["issue"])
        check("holder run", f["fio"] == "ЖЕРЕБЯТЬЕВ АЛЕКСАНДР ЕВГЕНЬЕВИЧ", f["fio"])
        check("account run + key", "40817810450231364892" in f["account"] and rs.check_account("40817810450231364892"), f["account"])
        ops = [op for day in parsed.days for op in day.ops]
        check("6 operations laid out", len(ops) == 6, str(len(ops)))
        opening = rs.parse_money(parsed.header_values["opening"].text)
        income = rs.parse_money(parsed.header_values["income"].text)
        expenses = rs.parse_money(parsed.header_values["expenses"].text)
        closing = rs.parse_money(parsed.header_values["closing"].text)
        check("header identity closing=opening+income-expenses",
              closing == opening + income - expenses, f"{opening} {income} {expenses} {closing}")
        check("header totals = printed operations",
              income == sum((o.amount for o in ops if o.amount > 0), Decimal("0"))
              and expenses == -sum((o.amount for o in ops if o.amount < 0), Decimal("0")))
        check("day balance = opening",
              rs.parse_money(parsed.days[0].balance_value_run.text) == opening)
        codes = [int(o.code) for o in ops]
        check("operation codes strictly grow with time", codes == sorted(codes) and len(set(codes)) == len(codes), str(codes))
        sbp_ok = True
        for op in ops:
            m = _re.search(r"\b[AB][0-9A-Z]{31}\b", op.description)
            if "СБП" in op.description and not m:
                sbp_ok = False
        check("every SBP description carries a 32-char SBP id", sbp_ok)
        check("font subsets carry exactly the document's characters", _subsets_exact(stmt))
        raw = out.read_bytes()
        pdf6 = rs.StatementPdf(raw)
        info_body = pdf6.comp_body(int(pdf6.info_ref.split()[0]))
        check("no CreationDate/ModDate, no /ID (statement donors have none)",
              b"CreationDate" not in raw and b"/ID" not in pdf6.streams[pdf6.xref_num].dict_bytes
              and b"WeasyPrint 66.0" in info_body, info_body.decode("latin-1", "replace"))
        check("fonts head.modified = generation moment (both fonts)",
              all(abs((stmt.font_modified(slot) - gen_at.astimezone(timezone.utc)).total_seconds()) < 2 for slot in stmt.fonts.values()),
              str([stmt.font_modified(s) for s in stmt.fonts.values()]))
        check("all non-image Flate streams are native zlib level 9", _all_level9(out))
        if subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0:
            q = subprocess.run(["qpdf", "--check", str(out)], capture_output=True, text=True).stdout
            check("qpdf clean (6 ops)", "No syntax or stream encoding errors" in q, q[-200:])

        # one operation imported from a genuine SBP receipt
        out1 = rs.generate_rocket_statement(
            period_start="01.11.2026", period_end="03.11.2026", issue_date="03.11.2026",
            holder_name="Жеребятьев Александр Евгеньевич", opening_balance="100,00",
            receipt_paths=[str(_HERE / "donors" / "receipt_rocket_42.pdf")],
            operation_count=1, output_path=td / "one.pdf", generated_at=gen_at, random_seed=2,
        )
        p1 = rs.parse_statement(rs.Statement(str(out1)))
        ops1 = [op for day in p1.days for op in day.ops]
        check("receipt op: code = receipt doc number without M",
              len(ops1) == 1 and ops1[0].code == "73551331569", str([o.code for o in ops1]))
        check("receipt op: amount signed, SBP id from the receipt",
              len(ops1) == 1 and ops1[0].amount == Decimal("-17")
              and "A63070342357510K0B10030011881102" in ops1[0].description,
              ops1[0].description if ops1 else "")

        # random last-5 account tail -> new key everywhere
        out2 = rs.generate_rocket_statement(
            period_start="16.10.2026", period_end="23.10.2026", issue_date="03.11.2026",
            holder_name="Жеребятьев Александр Евгеньевич", account_tail="12345",
            opening_balance="100,00", operation_count=2, operation_date="21.10.2026",
            output_path=td / "tail.pdf", generated_at=gen_at, random_seed=7,
        )
        p2 = rs.parse_statement(rs.Statement(str(out2)))
        acc = _re.search(r"\d{20}", p2.account_run.text).group(0)
        check("account tail changed, key recomputed", acc.endswith("12345") and acc != "40817810450231364892" and rs.check_account(acc), acc)
        check("new account in every operation description",
              all(acc in op.description for op in [o for d in p2.days for o in d.ops]
                  if op.description.startswith(("ПЕРЕВОД СОГЛАСНО", "ВНЕШНИЙ"))))

        # holder must differ from the donor's for checking (outline too)
        out3 = rs.generate_rocket_statement(
            period_start="16.10.2026", period_end="23.10.2026", issue_date="03.11.2026",
            holder_name="Петров Иван Сергеевич", opening_balance="16,00",
            operation_count=2, operation_date="20.10.2026",
            output_path=td / "petrov.pdf", generated_at=gen_at, random_seed=11,
        )
        p3 = rs.parse_statement(rs.Statement(str(out3)))
        pdf3 = rs.StatementPdf(out3.read_bytes())
        import re as _re2
        titles = []
        for _num, _body in pdf3.comp.items():
            _m = _re2.search(rb"/Title <([0-9a-fA-F]+)>", _body)
            if _m:
                try:
                    titles.append(bytes.fromhex(_m.group(1).decode()).decode("utf-16-be").lstrip("\ufeff"))
                except Exception:
                    pass
        check("different holder in print + outline",
              p3.fio_run.text == "ПЕТРОВ ИВАН СЕРГЕЕВИЧ" and "Петров Иван Сергеевич" in titles
              and not any("ЖЕРЕБЯТЬЕВ" in t.upper() for t in titles), str(titles))

    print("\nRejections")
    base = dict(
        period_start="16.10.2026", period_end="23.10.2026", issue_date="03.11.2026",
        holder_name="Жеребятьев Александр Евгеньевич", opening_balance="16,00",
    )
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        for label, override in (
            ("operation outside period", {"operations": [{"kind": "sbp_out", "datetime": "20.11.2026 10:00", "amount": 10}], "operation_count": 1}),
            ("more than 10 operations", {"operation_count": 11}),
            ("operations on several days", {"operations": [
                {"kind": "sbp_out", "datetime": "20.10.2026 10:00", "amount": 10},
                {"kind": "sbp_out", "datetime": "21.10.2026 10:00", "amount": 10},
            ], "operation_count": 2}),
            ("glyph outside donor pool (Ю in Loos)", {"holder_name": "Юрьев Юрий Юрьевич", "operation_count": 0}),
        ):
            try:
                rs.generate_rocket_statement(**{**base, **override}, output_path=out, random_seed=1)
                raised = False
            except Exception:  # noqa: BLE001 – any honest rejection counts
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def main() -> int:
    if not DONORS:
        print("no donors in banks/rocket/donors_statement/", file=sys.stderr)
        return 1
    stage2()
    stage3()
    stage3c_font_injection()
    stage4()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
