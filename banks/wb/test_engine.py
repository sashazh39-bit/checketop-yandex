#!/usr/bin/env python3
"""Tests for banks/wb — run with:

    python3 banks/wb/test_engine.py

Covers donor round-trip, SBP wrap 245 pt, generate_wb fields and
linked identifiers, one-time token registry, glyph-pool refusals.
Generations use temporary donor copies and a temporary registry so
the real pool is never consumed.  One sample is written to out/.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# HERE last so `import engine` is banks/wb/engine.py, not rocket's.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "banks" / "rocket"))
sys.path.insert(0, str(HERE))

import engine  # noqa: E402
import wb_font  # noqa: E402
import wb_receipt  # noqa: E402
from wb_receipt import MSK, Receipt  # noqa: E402

POOL = HERE / "donors"
DONORS = sorted(POOL.glob("wb_donor_*.pdf"))
OUT = HERE / "out"

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(
        f"  [{'PASS' if condition else 'FAIL'}] {label}"
        + (f" - {detail}" if detail and not condition else "")
    )
    if not condition:
        FAILURES += 1


def qpdf_clean(path) -> bool:
    if subprocess.run(["which", "qpdf"], capture_output=True).returncode != 0:
        return True
    result = subprocess.run(
        ["qpdf", "--check", str(path)], capture_output=True, text=True
    )
    return "No syntax or stream encoding errors" in result.stdout


def make_pool(tmp: Path, donors) -> tuple:
    pool = Path(tmp) / "donors"
    pool.mkdir(parents=True, exist_ok=True)
    for donor in donors:
        shutil.copy2(donor, pool / Path(donor).name)
    return pool, Path(tmp) / "spent_tokens.json"


def sample_kwargs(pool, registry, **overrides):
    kwargs = dict(
        amount="12 ₽",
        operation_dt=datetime(2026, 11, 7, 12, 15, 40, tzinfo=MSK),
        recipient_name="Жеребятьев Александр Евгеньевич",
        recipient_phone="89632972008",
        recipient_bank="Альфа-Банк",
        donor_pool_dir=pool,
        token_registry_path=registry,
        generated_at=datetime(2026, 11, 7, 12, 16, 0, tzinfo=MSK),
    )
    kwargs.update(overrides)
    return kwargs


def main() -> int:
    print("Stage 1: donors present")
    check("four WB donors", len(DONORS) == 4, str(len(DONORS)))
    names = {p.name for p in DONORS}
    check(
        "donor filenames",
        names == {f"wb_donor_{i}.pdf" for i in range(1, 5)},
        str(sorted(names)),
    )

    print("\nStage 2: byte-for-byte round-trip")
    for donor in DONORS:
        target = Path(tempfile.mkdtemp()) / "roundtrip.pdf"
        source = donor.read_bytes()
        Receipt(str(donor)).save(str(target))
        check(f"{donor.name} round-trip", target.read_bytes() == source)

    print("\nStage 2b: SBP wrap 245 pt reproduces donor line breaks")
    for donor in DONORS:
        receipt = Receipt(str(donor))
        sbp = receipt.fields["sbp_id"]
        line1, line2 = receipt.wrap_sbp(sbp)
        check(
            f"{donor.name} wrap concat",
            line1 + line2 == sbp,
            f"{line1!r}+{line2!r}",
        )
        check(
            f"{donor.name} wrap matches donor",
            line1 == receipt._fields["sbp_1"].text
            and line2 == receipt._fields["sbp_2"].text,
            f"got {line1!r}/{line2!r}",
        )
        check(f"{donor.name} SBP is 32 chars", len(sbp) == 32, sbp)

    print("\nStage 2c: printed time = SBP UTC + 1 s")
    for donor in DONORS:
        receipt = Receipt(str(donor))
        sbp = receipt.fields["sbp_id"]
        moment = engine.decode_sbp_moment(sbp)
        printed = datetime.strptime(
            receipt.fields["datetime"], "%d.%m.%Y %H:%M:%S"
        ).replace(tzinfo=MSK)
        check(
            f"{donor.name} SBP+1s",
            moment is not None and moment + timedelta(seconds=1) == printed,
            f"sbp={moment} printed={printed}",
        )
        check(
            f"{donor.name} serial inherited shape",
            sbp[16:18] == "0G" and sbp.endswith("0011890101"),
            sbp,
        )

    print("\nStage 3: change only the amount (native Medium digits)")
    with tempfile.TemporaryDirectory() as tmp:
        donor = POOL / "wb_donor_1.pdf"  # 13 ₽ → 11 ₽, both digits already present
        target = Path(tmp) / "amount.pdf"
        receipt = Receipt(str(donor))
        before = dict(receipt.fields)
        receipt.set_amount("11 ₽")
        receipt.save(str(target), mode="natural", reidentify=False)
        reread = Receipt(str(target))
        check("amount re-reads 11 ₽", reread.fields.get("amount") == "11 ₽",
              reread.fields.get("amount"))
        changed = {
            k for k in before
            if before[k] != reread.fields.get(k, "")
        }
        check("only the amount changed", changed <= {"amount"}, str(changed))
        raw = target.read_bytes()
        check(
            "natural save is single-pass",
            raw.count(b"startxref") == 1 and b"/Prev" not in raw,
        )

    print("\nStage 4: generate_wb — fields, SBP, receipt, token")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, DONORS)
        donor_tokens = {}
        for donor in DONORS:
            fields = Receipt(str(donor)).fields
            donor_tokens[engine.serial_token(fields["sbp_id"])] = fields["sbp_id"]
        before_spent = engine.load_spent_tokens(registry)
        out = Path(tmp) / "generated.pdf"
        result = engine.generate_wb(output_path=out, **sample_kwargs(pool, registry))
        check("returns the output path", Path(result) == out and out.is_file())

        reread = Receipt(str(out))
        fields = reread.fields
        wanted = {
            "datetime": "07.11.2026 12:15:40",
            "recipient": "Александр Евгеньевич Ж.",
            "phone": "+7 963 297-20-08",
            "bank": "Альфа-Банк",
            "type": "Перевод по СБП",
            "status": "Выполнено",
            "amount": "12 ₽",
        }
        mismatched = {
            k: (v, fields.get(k))
            for k, v in wanted.items()
            if fields.get(k) != v
        }
        check("all user fields reread", not mismatched, str(mismatched))
        check("no sender field", "sender" not in fields)

        sbp = fields["sbp_id"]
        spent_now = engine.load_spent_tokens(registry)
        new_tokens = spent_now - before_spent
        check("exactly one token spent", len(new_tokens) == 1, str(new_tokens))
        spent_token = next(iter(new_tokens)) if new_tokens else ""
        check(
            "sbp serial token is the spent donor token",
            sbp[11:22] == spent_token,
            sbp,
        )
        check(
            "sbp token inherited (not invented)",
            spent_token in donor_tokens,
            spent_token,
        )
        check("sbp era tail 0011890101", sbp.endswith("0011890101"), sbp)
        moment = engine.decode_sbp_moment(sbp)
        check(
            "sbp decodes to printed − 1 s",
            moment is not None
            and (moment + timedelta(seconds=1)).strftime("%d.%m.%Y %H:%M:%S")
            == "07.11.2026 12:15:40",
            str(moment),
        )
        check("first char inherited A/B", sbp[0] in "AB", sbp[0])
        check("sbp wrap still 32 chars", len(sbp) == 32, sbp)

        receipt_line = fields.get("receipt", "")
        check(
            "receipt line form",
            re.fullmatch(r"Квитанция № \d{10}", receipt_line) is not None,
            receipt_line,
        )
        number = receipt_line.split()[-1]
        check(
            "receipt digits ⊆ Bold pool 0125789",
            set(number) <= set("0125789"),
            number,
        )
        donor_numbers = {
            re.search(r"\d+", Receipt(str(d)).fields["receipt"]).group(0)
            for d in DONORS
        }
        check("receipt number is not copied from donor", number not in donor_numbers, number)

        raw = out.read_bytes()
        check(
            "single-pass file (1 startxref, no /Prev)",
            raw.count(b"startxref") == 1
            and raw.count(b"%%EOF") == 1
            and b"/Prev" not in raw,
        )
        check("qpdf clean", qpdf_clean(out))
        ids = re.findall(rb"/ID\s*\[<([0-9a-f]{32})><([0-9a-f]{32})>\]", raw)
        check(
            "fresh paired trailer /ID",
            bool(ids) and ids[-1][0] == ids[-1][1],
        )
        info_num = reread.pdf.resolve_ref(reread.pdf.trailer_dict, b"/Info")
        info = reread.pdf.obj(info_num).dict_bytes
        check(
            "CreationDate = generation moment UTC",
            b"/CreationDate(D:20261107091600Z)" in info,
            info.decode("latin-1"),
        )
        check("no ModDate", b"/ModDate" not in info)
        check(
            "Producer OpenPDF 1.3.32",
            b"OpenPDF 1.3.32" in info,
        )
        check(
            "Creator Jasper 7.0.3",
            b"JasperReports Library version 7.0.3" in info,
        )
        created = datetime(2026, 11, 7, 12, 16, tzinfo=MSK)
        check(
            "file mtime = CreationDate + 1s",
            abs(out.stat().st_mtime - (created.timestamp() + 1)) < 2,
            str(out.stat().st_mtime),
        )

        # Font subsets: used Regular/Medium/Bold chars ⊆ cmap, filled outlines
        # cover non-space used chars.
        exact_ok = True
        used_by_font = reread._scan_used_cids(reread._content)
        for slot in reread.fonts.values():
            used = used_by_font.get(slot.resource, set())
            if not used <= set(slot.cid_to_char):
                exact_ok = False
            ttf = reread.pdf.obj(slot.fontfile_obj).decoded_stream()
            for cid in used:
                char = slot.cid_to_char.get(cid, " ")
                if char in " \u00a0":
                    continue
                if not wb_font.glyph_blob(ttf, cid):
                    exact_ok = False
        check("font subsets cover used glyphs", exact_ok)

        default_out = engine.generate_wb(
            **sample_kwargs(pool, registry, output_path=None)
        )
        check(
            "default output name is <uuid4>.pdf",
            re.fullmatch(r"[0-9a-f-]{36}\.pdf", Path(default_out).name) is not None,
            str(default_out),
        )
        try:
            Path(default_out).unlink(missing_ok=True)
        except OSError:
            pass
        check(
            "two tokens spent after two gens",
            len(engine.load_spent_tokens(registry) - before_spent) == 2,
        )

    print("\nStage 5: amount inject (digit 2 into a 13 ₽ donor) and 1 000 grouping")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, DONORS)
        out = Path(tmp) / "twenty.pdf"
        engine.generate_wb(
            output_path=out,
            **sample_kwargs(pool, registry, amount=20, donor_path=pool / "wb_donor_1.pdf"),
        )
        reread = Receipt(str(out))
        check("injected Medium digit 2 → 20 ₽", reread.fields["amount"] == "20 ₽",
              reread.fields.get("amount"))

        out2 = Path(tmp) / "thousand.pdf"
        engine.generate_wb(
            output_path=out2,
            **sample_kwargs(pool, registry, amount=1000),
        )
        reread2 = Receipt(str(out2))
        check("thousands grouping 1 000 ₽", reread2.fields["amount"] == "1 000 ₽",
              reread2.fields.get("amount"))

    print("\nRejections")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, DONORS)
        before_spent = engine.load_spent_tokens(registry)
        try:
            engine.generate_wb(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, recipient_name="Ёлкина Анна Ивановна"),
            )
            refused = False
        except engine.GlyphCoverageError:
            refused = True
        check("rejects letter outside glyph pool (Ё)", refused)
        check(
            "glyph refusal does not spend the token",
            engine.load_spent_tokens(registry) == before_spent,
        )

        try:
            engine.generate_wb(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, amount="15 ₽"),
            )
            refused = False
        except engine.GlyphCoverageError:
            refused = True
        check("rejects Medium digit 5 (not in pool)", refused)

        try:
            engine.generate_wb(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, amount="12,50 ₽"),
            )
            refused = False
        except engine.ReceiptError:
            refused = True
        check("rejects kopecks", refused)

        try:
            engine.generate_wb(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, recipient_phone="123"),
            )
            refused = False
        except engine.ReceiptError:
            refused = True
        check("rejects bad phone", refused)
        check(
            "bad input does not spend the token",
            engine.load_spent_tokens(registry) == before_spent,
        )

        try:
            engine.generate_wb(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(
                    pool, registry, recipient_name="Константин Юрьевич К."
                ),
            )
            refused = False
        except engine.GlyphCoverageError:
            refused = True
        check("rejects Regular capital К (only in Bold)", refused)

    print("\nToken exhaustion")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, [POOL / "wb_donor_1.pdf"])
        engine.generate_wb(
            output_path=Path(tmp) / "one.pdf",
            **sample_kwargs(
                pool, registry, amount="13 ₽", donor_path=pool / "wb_donor_1.pdf"
            ),
        )
        try:
            engine.generate_wb(
                output_path=Path(tmp) / "two.pdf",
                **sample_kwargs(
                    pool, registry, amount="13 ₽", donor_path=pool / "wb_donor_1.pdf"
                ),
            )
            refused = False
        except engine.NoUnusedDonorTokenError:
            refused = True
        check("second use of the same donor token is refused", refused)

    print("\nSample → banks/wb/out/")
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, DONORS)
        sample = OUT / "sample.pdf"
        engine.generate_wb(
            output_path=sample,
            **sample_kwargs(pool, registry, amount=12),
        )
        check("sample written", sample.is_file() and sample.stat().st_size > 1000)

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
