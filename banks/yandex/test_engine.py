#!/usr/bin/env python3
"""Tests for banks/yandex/engine.py — run with:

    python3 banks/yandex/test_engine.py

Mirrors the Rocket engine tests: donor round-trip, one field, full
generation (every field re-reads, SBP decodes to the operation moment,
font subsets are exact, one startxref, qpdf clean), one-time spent
token registry and honest rejections.  All engine generations run on
*temporary copies* of donors with a temporary registry, so the real
pool and the real spent-token registry are never consumed by tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))

import engine  # noqa: E402
import patch_yandex  # noqa: E402
import yandex_font  # noqa: E402
import yandex_receipt  # noqa: E402

POOL = HERE / "donors"
MAIN_DONOR = POOL / "yandex_fresh.pdf"
SECOND_DONOR = POOL / "yandex_test.pdf"
SPENT_DONOR = POOL / "390ab9c7-e994-4ecb-b4da-51d3e81939dc.pdf"

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1


def normalize(text: str) -> str:
    return (text or "").replace("\u00a0", " ")


def qpdf_clean(path) -> bool:
    if subprocess.run(["which", "qpdf"], capture_output=True).returncode != 0:
        return True
    result = subprocess.run(["qpdf", "--check", str(path)], capture_output=True, text=True)
    return "No syntax or stream encoding errors" in result.stdout


def make_pool(tmp: Path, donors) -> tuple:
    pool = Path(tmp) / "donors"
    pool.mkdir(parents=True, exist_ok=True)
    for donor in donors:
        shutil.copy2(donor, pool / Path(donor).name)
    return pool, Path(tmp) / "spent_tokens.json"


def sample_kwargs(pool, registry, **overrides):
    kwargs = dict(
        amount="17 350,50 ₽",
        operation_dt=datetime(2026, 11, 3, 14, 25, tzinfo=yandex_receipt.MSK),
        sender_name="Иванов Иван Иванович",
        sender_phone="89161234567",
        recipient_name="Петрова Анна Ивановна",
        recipient_phone="+7 925 765-43-21",
        recipient_bank="Т-Банк",
        donor_pool_dir=pool,
        token_registry_path=registry,
        generated_at=datetime(2026, 11, 3, 14, 28, tzinfo=yandex_receipt.MSK),
    )
    kwargs.update(overrides)
    return kwargs


def main() -> int:
    print("Stage 2: byte-for-byte round-trip")
    check("pool donors present", MAIN_DONOR.is_file() and SECOND_DONOR.is_file())
    check("pool has genuine donors", len(list(POOL.glob("*.pdf"))) >= 30,
          str(len(list(POOL.glob("*.pdf")))))
    seeded = engine.load_spent_tokens()
    check(
        "04.11 spent tokens seeded",
        {"6381I0B1013", "906260B1008", "3380O0B1017", "1720E0B1007", "9446L0B1006"} <= seeded,
        str(sorted(seeded)),
    )
    check("clean donor token not seeded", not any(t.startswith("33340") for t in seeded))

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "roundtrip.pdf"
        source = MAIN_DONOR.read_bytes()
        yandex_receipt.Receipt(str(MAIN_DONOR)).save(str(target))
        check("round-trip byte-identical", target.read_bytes() == source)

    print("\nStage 3: change only the amount (native font)")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "amount.pdf"
        receipt = yandex_receipt.Receipt(str(MAIN_DONOR))
        before = dict(receipt.fields)
        receipt.set_amount("1 234 567,89 ₽")
        receipt.save(str(target))
        reread = yandex_receipt.Receipt(str(target))
        check("amount re-reads", normalize(reread.amount) == "1 234 567,89 ₽", reread.amount)
        changed = {k for k in before if normalize(before[k]) != normalize(reread.fields.get(k, ""))}
        check("only the amount changed", changed <= {"amount"}, str(changed))
        check("amount qpdf clean", qpdf_clean(target))

    print("\nStage 4/5: generate_yandex() — all fields + linked IDs + one-time token")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, [MAIN_DONOR, SECOND_DONOR])
        donor_tokens = {}
        for donor in (MAIN_DONOR, SECOND_DONOR):
            fields = yandex_receipt.Receipt(str(donor)).fields
            donor_tokens[engine.serial_token(fields["sbp_id"])] = fields["sbp_id"]
        before_spent = engine.load_spent_tokens(registry)
        out = Path(tmp) / "generated.pdf"
        result = engine.generate_yandex(output_path=out, **sample_kwargs(pool, registry))
        check("returns the output path", Path(result) == out and out.is_file())

        reread = yandex_receipt.Receipt(str(out))
        fields = reread.fields
        wanted = {
            "datetime": "03.11.2026 в 14:25",
            "doc_date": "Дата 03.11.2026",
            "phone_from": "+7 916 123-45-67",
            "sender": "Иван Иванович И.",
            "phone_to": "+7 925 765-43-21",
            "recipient": "Анна Ивановна П.",
            "bank_to": "Т-Банк",
            "fee": "—",
            "message": "—",
        }
        mismatched = {k: (v, fields.get(k)) for k, v in wanted.items() if normalize(fields.get(k, "")) != v}
        check("all user fields reread", not mismatched, str(mismatched))
        check("amount with kopecks reread", normalize(fields.get("amount", "")) == "17 350,50 ₽",
              fields.get("amount", ""))
        check("iskh is op-date + 7 digits",
              re.fullmatch(r"Исх. № 20261103\d{7}", fields["iskh"]) is not None, fields["iskh"])

        sbp = fields["sbp_id"]
        spent_now = engine.load_spent_tokens(registry)
        new_tokens = spent_now - before_spent
        check("exactly one token spent", len(new_tokens) == 1, str(new_tokens))
        spent_token = next(iter(new_tokens)) if new_tokens else ""
        check("sbp serial token is the spent donor token", sbp[11:22] == spent_token, sbp)
        check("sbp token inherited (not invented)", spent_token in donor_tokens, spent_token)
        check("sbp era tail", sbp.endswith("0011881102"), sbp)
        moment = patch_yandex.parse_sbp_moment(sbp)
        check(
            "sbp decodes to operation moment (donor seconds inherited)",
            moment is not None and moment.strftime("%d.%m.%Y %H:%M:%S") == "03.11.2026 14:25:37",
            str(moment),
        )
        auth = fields["auth"]
        digits = sum(c.isdigit() for c in auth)
        check("auth code format", re.fullmatch(r"[ABCFGIJKLMNOPRTVYZ0-9]{6}", auth) is not None and 1 <= digits <= 5, auth)

        raw = out.read_bytes()
        check("single-pass file (1 startxref, no /Prev)",
              raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1 and b"/Prev" not in raw)
        check("qpdf clean", qpdf_clean(out))
        ids = re.findall(rb"/ID\s*\[<([0-9a-f]{32})><([0-9a-f]{32})>\]", raw)
        donor_raw = MAIN_DONOR.read_bytes()
        donor_id = re.search(rb"/ID\s*\[<([0-9a-f]+)>", donor_raw).group(1)
        check("fresh paired trailer /ID", bool(ids) and ids[-1][0] == ids[-1][1] and ids[-1][0] != donor_id)
        info = reread.pdf.obj(reread.pdf.resolve_ref(reread.pdf.trailer_dict, b"/Info"))
        check("CreationDate = generation moment",
              b"/CreationDate(D:20261103112800Z)" in info.dict_bytes,
              info.dict_bytes.decode("latin-1"))
        created = datetime(2026, 11, 3, 14, 28, tzinfo=yandex_receipt.MSK)
        check("file mtime = CreationDate + 1s",
              abs(out.stat().st_mtime - (created.timestamp() + 1)) < 2,
              str(out.stat().st_mtime))

        # Font subsets are exactly the glyphs the new text uses.
        import fitz

        with fitz.open(out) as doc:
            spans = [
                span
                for block in doc[0].get_text("dict")["blocks"]
                if block.get("type") == 0
                for line in block["lines"]
                for span in line["spans"]
            ]
        exact_ok = True
        for slot in reread.fonts.values():
            used_chars = {c for span in spans if span["font"] == slot.family for c in span["text"]}
            if set(slot.cid_to_char.values()) != used_chars:
                exact_ok = False
            ttf = yandex_font.read_ttf(reread.pdf.obj(slot.fontfile_obj).decoded_stream())
            filled = {ch for cid, ch in slot.cid_to_char.items() if yandex_font.glyph_has_outline(ttf, cid)}
            if filled != {c for c in used_chars if c not in " \u00a0"}:
                exact_ok = False
        check("font subsets are exact", exact_ok)

        # Default output name is a genuine-style <uuid4>.pdf; this also
        # consumes the second donor of the temporary pool.
        default_out = engine.generate_yandex(
            **sample_kwargs(pool, registry, output_path=None)
        )
        check("default output name is <uuid4>.pdf",
              re.fullmatch(r"[0-9a-f-]{36}\.pdf", Path(default_out).name) is not None,
              str(default_out))
        try:
            Path(default_out).unlink(missing_ok=True)
            Path(default_out).parent.rmdir()
        except OSError:
            pass
        check("both temp donors spent", len(engine.load_spent_tokens(registry) - before_spent) == 2)

        try:
            engine.generate_yandex(output_path=Path(tmp) / "third.pdf", **sample_kwargs(pool, registry))
            refused = False
        except engine.NoUnusedDonorTokenError:
            refused = True
        check("third generation refused: no unused token", refused)

    print("\nRejections")
    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, [SPENT_DONOR])
        try:
            engine.generate_yandex(output_path=Path(tmp) / "x.pdf", **sample_kwargs(pool, registry))
            refused = False
        except engine.NoUnusedDonorTokenError:
            refused = True
        check("seeded spent donor (390ab9c7) is never reused", refused)

    with tempfile.TemporaryDirectory() as tmp:
        pool, registry = make_pool(tmp, [MAIN_DONOR])
        before_spent = engine.load_spent_tokens(registry)
        try:
            engine.generate_yandex(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, recipient_name="Ёлкина Анна Ивановна"),
            )
            refused = False
        except engine.GlyphCoverageError:
            refused = True
        check("rejects letter outside glyph pool (Ё)", refused)
        check("glyph refusal does not spend the token",
              engine.load_spent_tokens(registry) == before_spent)

        try:
            engine.generate_yandex(
                output_path=Path(tmp) / "x.pdf",
                **sample_kwargs(pool, registry, recipient_phone="123"),
            )
            refused = False
        except yandex_receipt.ReceiptError:
            refused = True
        check("rejects bad phone", refused)
        check("bad input does not spend the token",
              engine.load_spent_tokens(registry) == before_spent)

        # A token spent by a *failed* generation stays spent.
        spent_before = engine.load_spent_tokens(registry)
        original_save = yandex_receipt.Receipt.save

        def broken_save(self, *args, **kwargs):
            raise yandex_receipt.ReceiptError("simulated save failure")

        yandex_receipt.Receipt.save = broken_save
        try:
            engine.generate_yandex(output_path=Path(tmp) / "x.pdf", **sample_kwargs(pool, registry))
            refused = False
        except yandex_receipt.ReceiptError:
            refused = True
        finally:
            yandex_receipt.Receipt.save = original_save
        check("failed generation raises", refused)
        check("failed generation keeps the token spent",
              len(engine.load_spent_tokens(registry) - spent_before) == 1)

    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
