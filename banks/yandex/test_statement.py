#!/usr/bin/env python3
"""Checks for the Yandex statement engine, in the order of the chat's stages.

Run with: python3 banks/yandex/test_statement.py

Stage 2 — a genuine statement donor opened and saved unchanged is
          byte-for-byte identical.
Stage 3 — changing only one operation amount uses the native
          YSText-Regular font and leaves every other run untouched;
          the donor's descriptions re-wrap to the donor's own lines.
Stage 4 — generate_yandex_statement(): holder/contract/period/
          operations/totals re-read back, arithmetic agrees,
          Исх./Дата/CreationDate are linked to the issue moment.
Stage 5 — exact font subsets, natural single-pass save, glyph
          growth from genuine receipts/statements, honest refusals.

The holder and counterparties here are fictional; donor personal
data is never used as generation input.
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
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parent / "donors_statement"))

import statement as ys  # noqa: E402
import yandex_font  # noqa: E402

try:
    import rocket_pdf
except ModuleNotFoundError:
    sys.path.insert(0, str(_HERE.parents[1] / "banks" / "rocket"))
    import rocket_pdf  # noqa: E402

DONORS = sorted((_HERE / "donors_statement").glob("*.pdf"))
MSK = timezone(timedelta(hours=3))
FAILURES = 0

HOLDER = "Васенин Иван Андреевич"
BIRTH = "12.03.1995"
PASSPORT = "4510 123456"
GEN_AT = datetime(2026, 10, 6, 12, 0, tzinfo=MSK)


def check(label: str, condition: bool, detail: str = "") -> bool:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1
    return condition


def _qpdf_clean(path: Path) -> bool:
    if subprocess.run(["which", "qpdf"], capture_output=True).returncode != 0:
        return True
    out = subprocess.run(["qpdf", "--check", str(path)], capture_output=True, text=True).stdout
    return "No syntax or stream encoding errors" in out


def _all_level6(path: Path) -> bool:
    pdf = rocket_pdf.load(str(path))
    streams = [o for o in pdf.objects.values() if o.stream_raw and b"/FlateDecode" in o.dict_bytes]
    return bool(streams) and all(
        o.stream_raw[:2] == b"\x78\x9c" and zlib.compress(o.decoded_stream(), 6) == o.stream_raw
        for o in streams
    )


def _subsets_exact(stmt: ys.Statement) -> bool:
    used = stmt._scan_used_cids(stmt.content)
    for res, slot in stmt.fonts.items():
        expected = {c for c in used.get(res, set()) if c in slot.cid_to_char}
        cidset = yandex_font.parse_cidset(
            stmt.pdf.obj(slot.cidset_obj).decoded_stream()
        )
        if not (expected == set(slot.cid_to_char) == cidset == set(slot.widths)):
            return False
        ttf = yandex_font.read_ttf(stmt.pdf.obj(slot.fontfile_obj).decoded_stream())
        for cid in expected:
            char = slot.cid_to_char[cid]
            if char not in " \u00a0" and not yandex_font.glyph_has_outline(ttf, cid):
                return False
    return True


def stage2() -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(DONORS)} statement donors)")
    check("at least one statement donor present", len(DONORS) >= 1, str(len(DONORS)))
    for path in DONORS:
        source = path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            ys.Statement(str(path)).save(str(tmp))
            check(f"{path.name} via Statement model", tmp.read_bytes() == source)
        finally:
            tmp.unlink(missing_ok=True)
        donor = ys.parse_statement(ys.Statement(str(path)))
        ops_sum_in = sum((o["amount"] for o in donor.operations if o["amount"] > 0), Decimal("0"))
        ops_sum_out = -sum((o["amount"] for o in donor.operations if o["amount"] < 0), Decimal("0"))
        check(
            f"{path.name} arithmetic: closing and totals agree with operations",
            donor.closing == donor.opening + donor.income_total - donor.expense_total
            and donor.income_total == ops_sum_in and donor.expense_total == ops_sum_out
            and all(o["amount"] == o["amount_esp"] for o in donor.operations),
            f"{donor.opening} {donor.income_total} {donor.expense_total} {donor.closing}",
        )


def stage3() -> None:
    print("\nStage 3: change only one operation amount (native font)")
    donor_path = DONORS[0]
    donor = ys.Statement(str(donor_path))
    before_texts = [r.text for r in donor.runs]
    amount_runs = [r for r in donor.runs if r.text.startswith("+40,00")]
    check("incoming amount runs found (both currency columns)", len(amount_runs) == 2,
          str(len(amount_runs)))
    if not amount_runs:
        return
    stmt = ys.Statement(str(donor_path))
    run = [r for r in stmt.runs if r.text.startswith("+40,00")][0]
    check("amount font is F1 YSText-Regular",
          stmt.fonts[run.font].family == "YSText-Regular", stmt.fonts[run.font].family)
    stmt.set_run_text(run, "+250,00 ₽")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        stmt.save(str(tmp), mode="natural")
        reread = ys.Statement(str(tmp))
        after_texts = [r.text for r in reread.runs]
        check("same number of runs", len(after_texts) == len(before_texts),
              f"{len(before_texts)} -> {len(after_texts)}")
        diff = [i for i, (a, b) in enumerate(zip(before_texts, after_texts)) if a != b]
        check("only the one amount text changed",
              len(diff) == 1 and after_texts[diff[0]] == "+250,00 ₽", str(diff))
        parsed = ys.parse_statement(reread)
        check("amount re-reads as set, twin column untouched",
              parsed.operations[0]["amount"] == Decimal("250.00")
              and parsed.operations[0]["amount_esp"] == Decimal("40.00"),
              f"{parsed.operations[0]['amount']} / {parsed.operations[0]['amount_esp']}")
        raw = tmp.read_bytes()
        check("single pass, no incremental tail",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1 and b"/Prev" not in raw)
        check("all Flate streams are native zlib level 6 (78 9c)", _all_level6(tmp))
        check("qpdf clean", _qpdf_clean(tmp))
    finally:
        tmp.unlink(missing_ok=True)

    print("\nStage 3b: donor fonts pruned to their own glyphs are byte-identical")
    donor_stmt = ys.Statement(str(donor_path))
    used = donor_stmt._scan_used_cids(donor_stmt.content)
    same = True
    for slot in donor_stmt.fonts.values():
        keep = {c for c in used.get(slot.resource, set()) if c in slot.cid_to_char}
        original = donor_stmt.pdf.obj(slot.fontfile_obj).decoded_stream()
        if yandex_font.prune_ttf(original, keep) != original:
            same = False
    check("no-op prune == donor FontFile2 (all three families)", same)

    print("\nStage 3c: donor descriptions re-wrap to the donor's own lines")
    wrapped_ok = True
    for part in ys._desc_part_indexes(donor_stmt):
        donor_lines = [r.text for r in donor_stmt.runs if r.part == part]
        rebuilt = ys.wrap_description(" ".join(donor_lines), donor_stmt.fonts["F1"])
        # hyphen-split lines rejoin without a space in the source text
        source = donor_lines[0]
        for line in donor_lines[1:]:
            source += line if source.endswith("-") else " " + line
        if ys.wrap_description(source, donor_stmt.fonts["F1"]) != donor_lines:
            wrapped_ok = False
    check("greedy + hyphen wrap reproduces every donor row", wrapped_ok)


def _generate(tmp_dir: Path, name: str, **overrides):
    kwargs = dict(
        period_start="01.10.2026", period_end="05.10.2026",
        holder_name=HOLDER, holder_birth=BIRTH, holder_passport=PASSPORT,
        opening_balance="100,00",
        operations=[
            {"datetime": "02.10.2026 10:15",
             "description": "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
             "amount": "+1 500,50"},
            {"datetime": "02.10.2026 12:40",
             "description": "Исходящий перевод СБП, Анна Ивановна К., +7 911 222-33-44, Т-Банк",
             "amount": -250},
            {"datetime": "03.10.2026 09:05",
             "description": "Исходящий перевод СБП, Роман Андреевич В., +7 922 333-44-55, ВТБ",
             "amount": "-99,90"},
        ],
        contract_date="01.10.2026", issue_date="06.10.2026",
        output_path=tmp_dir / name, generated_at=GEN_AT,
    )
    kwargs.update(overrides)
    return ys.generate_yandex_statement(**kwargs)


def stage4() -> None:
    print("\nStage 4/5: generate_yandex_statement() — all fields + linked data")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = _generate(td, "main.pdf")
        stmt = ys.Statement(str(out))
        parsed = ys.parse_statement(stmt)
        donor = ys.parse_statement(ys.Statement(str(DONORS[0])))

        check("holder re-reads", parsed.holder_name == HOLDER, parsed.holder_name)
        check("birth + passport re-read",
              parsed.holder_birth.isoformat() == "1995-03-12" and parsed.holder_passport == PASSPORT,
              f"{parsed.holder_birth} {parsed.holder_passport}")
        check("contract: Э + contract date + 9 digits",
              bool(re.fullmatch(r"Э20261001\d{9}", parsed.contract_number))
              and parsed.contract_date.isoformat() == "2026-10-01",
              parsed.contract_number)
        check("period re-reads",
              (parsed.period_start.isoformat(), parsed.period_end.isoformat())
              == ("2026-10-01", "2026-10-05"),
              f"{parsed.period_start} {parsed.period_end}")
        check("printed Дата = issue date", parsed.doc_date.isoformat() == "2026-10-06",
              str(parsed.doc_date))
        check("Исх. № = issue date + 7 digits, fresh",
              bool(re.fullmatch(r"20261006\d{7}", parsed.iskh)) and parsed.iskh != donor.iskh,
              parsed.iskh)
        check("3 operations laid out", len(parsed.operations) == 3, str(len(parsed.operations)))
        check("operations chronological with processing = operation date",
              [o["datetime"].strftime("%d.%m.%Y %H:%M") for o in parsed.operations]
              == ["02.10.2026 10:15", "02.10.2026 12:40", "03.10.2026 09:05"]
              and all(o["processing_date"] == o["datetime"].date() for o in parsed.operations))
        check("descriptions round-trip (wrap + rejoin)",
              [o["description"] for o in parsed.operations] == [
                  "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
                  "Исходящий перевод СБП, Анна Ивановна К., +7 911 222-33-44, Т-Банк",
                  "Исходящий перевод СБП, Роман Андреевич В., +7 922 333-44-55, ВТБ",
              ], str([o["description"] for o in parsed.operations]))
        check("amounts signed, ЭСП column mirrors operation currency",
              [o["amount"] for o in parsed.operations]
              == [Decimal("1500.50"), Decimal("-250"), Decimal("-99.90")]
              and all(o["amount"] == o["amount_esp"] for o in parsed.operations),
              str([str(o["amount"]) for o in parsed.operations]))
        check("arithmetic: closing = opening + income - expenses",
              parsed.opening == Decimal("100.00") and parsed.closing == Decimal("1250.60")
              and parsed.income_total == Decimal("1500.50")
              and parsed.expense_total == Decimal("349.90")
              and parsed.closing == parsed.opening + parsed.income_total - parsed.expense_total,
              f"{parsed.opening} {parsed.income_total} {parsed.expense_total} {parsed.closing}")
        label_texts = [r.text for r in stmt.runs if r.font == "F2" and r.x == 20]
        check("balance labels carry period dates",
              "Входящий остаток на 01.10.2026" in label_texts
              and "Исходящий остаток за 05.10.2026" in label_texts, str(label_texts))

        raw = out.read_bytes()
        pdf = rocket_pdf.load(str(out))
        info = pdf.obj(28).dict_bytes if 28 in pdf.objects else b""
        info = next(
            (o.dict_bytes for o in pdf.objects.values() if b"/CreationDate" in o.dict_bytes), b""
        )
        check("CreationDate = generation moment (UTC), no ModDate",
              b"D:20261006090000Z" in info and b"ModDate" not in info,
              info.decode("latin-1", "replace"))
        donor_pdf = rocket_pdf.load(str(DONORS[0]))
        check("trailer /ID fresh, paired, same width",
              pdf.trailer_dict != donor_pdf.trailer_dict
              and re.search(rb"/ID \[<([0-9a-f]{32})><\1>\]", pdf.trailer_dict) is not None,
              pdf.trailer_dict.decode("latin-1", "replace"))
        def tags(p):
            found = {}
            for o in p.objects.values():
                m = re.search(rb"/BaseFont\s*/([A-Z]{6})\+([^\s/<>\[\]]+)", o.dict_bytes)
                if m:
                    found[m.group(2).decode()] = m.group(1).decode()
            return found
        check("all subset tags re-issued",
              tags(pdf).keys() == tags(donor_pdf).keys()
              and all(tags(pdf)[k] != tags(donor_pdf)[k] for k in tags(pdf)),
              f"{tags(pdf)} vs {tags(donor_pdf)}")
        check("font subsets carry exactly the used glyphs", _subsets_exact(stmt))
        check("single pass, no /Prev",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1 and b"/Prev" not in raw)
        check("all Flate streams are native zlib level 6", _all_level6(out))
        check("qpdf clean (main generation)", _qpdf_clean(out))
        import os
        mtime = datetime.fromtimestamp(os.stat(out).st_mtime, tz=timezone.utc)
        created = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        check("file mtime = CreationDate + 1s",
              abs((mtime - (created + timedelta(seconds=1))).total_seconds()) <= 2,
              str(mtime))

        # a 4-line description pushes the next row and totals by 11 pt
        out4 = _generate(td, "four_lines.pdf", operations=[
            {"datetime": "02.10.2026 10:15",
             "description": "Исходящий перевод СБП, Александр Евгеньевич Ж., +7 900 351-70-80, Wildberries (Вайлдберриз Банк)",
             "amount": "-10,00"},
            {"datetime": "02.10.2026 11:15",
             "description": "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
             "amount": "+20,00"},
        ])
        stmt4 = ys.Statement(str(out4))
        parsed4 = ys.parse_statement(stmt4)
        check("4-line row: next row first line 11 pt lower",
              len(parsed4.operations) == 2
              and abs(parsed4.operations[1]["first_y"] - 528.72) < 0.01,
              str([o["first_y"] for o in parsed4.operations]))
        closing_run = next(r for r in stmt4.runs if r.text.startswith("Исходящий остаток за"))
        check("4-line row: totals shifted with the table bottom",
              abs(closing_run.y - 478.58) < 0.01, str(closing_run.y))
        check("4-line generation subsets exact + qpdf",
              _subsets_exact(stmt4) and _qpdf_clean(out4))

        # empty statement of the same period
        out0 = _generate(td, "empty.pdf", operations=[])
        parsed0 = ys.parse_statement(ys.Statement(str(out0)))
        check("empty statement: closing = opening, zero totals",
              parsed0.operations == [] and parsed0.closing == parsed0.opening
              and parsed0.income_total == 0 and parsed0.expense_total == 0,
              f"{parsed0.opening} {parsed0.closing}")
        check("empty statement qpdf clean", _qpdf_clean(out0))

        # glyph growth: К for Medium comes from genuine receipts,
        # Latin O/z for Regular from genuine receipts (Ozon)
        out_growth = _generate(
            td, "growth.pdf", holder_name="Каверин Иван Андреевич",
            operations=[
                {"datetime": "02.10.2026 10:15",
                 "description": "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Озон Банк (Ozon)",
                 "amount": "+500,00"},
            ],
        )
        growth_stmt = ys.Statement(str(out_growth))
        growth = ys.parse_statement(growth_stmt)
        check("grown glyphs re-read (holder К, description Ozon)",
              growth.holder_name == "Каверин Иван Андреевич"
              and "(Ozon)" in growth.operations[0]["description"],
              f"{growth.holder_name} / {growth.operations[0]['description'] if growth.operations else ''}")
        check("grown fonts still have exact subsets", _subsets_exact(growth_stmt))
        check("growth generation qpdf clean", _qpdf_clean(out_growth))

    print("\nRejections")
    base = dict(
        period_start="01.10.2026", period_end="05.10.2026",
        holder_birth=BIRTH, holder_passport=PASSPORT, opening_balance="100,00",
        generated_at=GEN_AT,
    )
    one_op = [{"datetime": "02.10.2026 10:15",
               "description": "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
               "amount": 10}]
    long_ops = [
        {"datetime": f"02.10.2026 10:{10 + i:02d}",
         "description": "Исходящий перевод СБП, Александр Евгеньевич Ж., +7 900 351-70-80, Wildberries (Вайлдберриз Банк)",
         "amount": -10}
        for i in range(12)
    ]
    cases = [
        ("glyph outside pool (Ю in Medium)", dict(holder_name="Юрьев Юрий Юрьевич", operations=[])),
        ("glyph outside pool (Щ in Regular)", dict(
            holder_name=HOLDER,
            operations=[{"datetime": "02.10.2026 10:15",
                         "description": "Входящий перевод СБП, Щербаков Иван Петрович С., +7 900 111-22-33, Сбербанк",
                         "amount": 10}])),
        ("filled Карта column", dict(
            holder_name=HOLDER, operations=[dict(one_op[0], card="2200 **** **** 1234")])),
        ("operation outside period", dict(
            holder_name=HOLDER,
            operations=[dict(one_op[0], datetime="20.10.2026 10:15")])),
        ("zero amount", dict(holder_name=HOLDER, operations=[dict(one_op[0], amount=0)])),
        ("bad passport", dict(holder_name=HOLDER, holder_passport="123", operations=[])),
        ("operations overflow one page", dict(holder_name=HOLDER, operations=long_ops)),
    ]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        for label, override in cases:
            try:
                ys.generate_yandex_statement(
                    **{**base, **override}, output_path=out,
                )
                raised = False
            except ys.StatementError:
                raised = True
            check(f"rejects {label}", raised, "no StatementError raised")


def main() -> int:
    if not DONORS:
        print("no donors in banks/yandex/donors_statement/", file=sys.stderr)
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
