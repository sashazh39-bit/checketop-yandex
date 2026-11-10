#!/usr/bin/env python3
"""Checks for the Rocket engine, in the order of the chat's stages.

Run with: python3 test_engine.py

Stage 2 — a genuine donor opened and saved unchanged is byte-for-byte
          identical (all 20 donors).
Stage 3 — changing only the amount uses the native RocketSans-Wide
          font and leaves every other field untouched.
Stage 4/5 — generate_rocket() rewrites all fields plus the linked
          dates/numbers/IDs, at exactly the donor's byte size
          (single xref, no incremental-update tail).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import engine  # noqa: E402
import patch_rocket  # noqa: E402
import rocket_pdf  # noqa: E402
import rocket_receipt  # noqa: E402

MSK = timezone(timedelta(hours=3))
DONORS = sorted((_HERE / "donors").glob("receipt_rocket_*.pdf"))

FAILURES = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global FAILURES
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES += 1
    return condition


def info_of(path: Path):
    receipt = rocket_receipt.Receipt(str(path))
    num = receipt.pdf.resolve_ref(receipt.pdf.trailer_dict, b"/Info")
    info = receipt.pdf.obj(num).dict_bytes.decode("latin-1") if num else ""
    match = re.search(r"/ID\s*\[<([0-9a-fA-F]+)><([0-9a-fA-F]+)>",
                      receipt.pdf.trailer_dict.decode("latin-1"))
    return receipt, info, match


def stage2_roundtrip(cache) -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(DONORS)} donors)")
    check("donors 18-59 present", len(DONORS) == 26, str(len(DONORS)))
    for path in DONORS:
        source = path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            rocket_pdf.PdfFile(source).incremental_save(str(tmp))
            low_level = tmp.read_bytes() == source
            receipt = rocket_receipt.Receipt(str(path), cache)
            receipt.save(str(tmp), mode="incremental")
            via_model = tmp.read_bytes() == source
        finally:
            tmp.unlink(missing_ok=True)
        check(path.name, low_level and via_model,
              f"low={low_level} model={via_model}")


def stage3_amount_only(cache) -> None:
    print("\nStage 3: change only the amount (native font)")
    donor_path = _HERE / "donors" / "receipt_rocket_37.pdf"
    donor = rocket_receipt.Receipt(str(donor_path), cache)
    before = dict(donor.fields)
    amount_run = donor._fields["amount"]
    check("amount font is RocketSans-Wide",
          donor.fonts[amount_run.font].family == "RocketSans-Wide",
          donor.fonts[amount_run.font].family)

    receipt = rocket_receipt.Receipt(str(donor_path), cache)
    receipt.minimal_glyphs = True
    receipt.set("amount", "1 234 567 ₽")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        receipt.save(str(tmp), mode="natural")
        reread = rocket_receipt.Receipt(str(tmp))
        changed = {k for k in before if reread.fields.get(k) != before[k]}
        check("amount re-reads as set", reread.fields["amount"] == "1 234 567 ₽",
              reread.fields.get("amount", ""))
        check("only the amount changed", changed == {"amount"}, str(changed))
        raw = tmp.read_bytes()
        check("single-pass file, no incremental tail / padding",
              abs(len(raw) - donor_path.stat().st_size) < 4096
              and raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
              and b"/XRef" not in raw,
              f"{len(raw)} vs {donor_path.stat().st_size}")
        check("all streams are native zlib level 6 (78 9c)",
              _all_level6(tmp))
        if _has_qpdf():
            out = subprocess.run(["qpdf", "--check", str(tmp)],
                                 capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in out,
                  out[-200:])
    finally:
        tmp.unlink(missing_ok=True)


def stage5_generate() -> None:
    print("\nStage 4/5: generate_rocket() — all fields + linked IDs")
    template = _HERE / "donors" / "receipt_rocket_42.pdf"
    donor_receipt, _, donor_id = info_of(template)
    donor = dict(donor_receipt.fields)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "generated.pdf"
        result = engine.generate_rocket(
            amount="10 000,00 ₽",
            operation_dt=datetime(2026, 11, 3, 14, 25, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="8 900 351-70-80",
            recipient_name="Ковалёв Иван Петрович",
            recipient_phone="+7 (963) 297-20-08",
            recipient_bank="Сбербанк",
            output_path=out,
            donor_path=template,
            creation_delta_minutes=5,
        )
        check("returns the output path", Path(result) == out and out.is_file())

        receipt, info, id_match = info_of(out)
        f = receipt.fields
        expected = {
            "date": "03.11.2026 14:25 ПО МСК",
            "amount": "10 000 ₽",
            "recipient": "ИВАН ПЕТРОВИЧ К",
            "phone_to": "+7 963 297-20-08",
            "bank_to": "ПАО СБЕРБАНК",
            "sender": "АЛЕКСАНДР ЕВГЕНЬЕВИЧ Ж.",
            "phone_from": "+7 900 351-70-80",
            "account": donor["account"],
            "fee": "БЕЗ КОМИССИИ",
            "status": "УСПЕШНО",
            "bank_from": "РОКЕТ",
        }
        for name, want in expected.items():
            check(f"field {name}", f.get(name) == want,
                  f"{f.get(name)!r} != {want!r}")

        # SBP id decodes back to the operation moment (donor seconds 35
        # are inherited because operation_dt has second == 0).
        moment = engine._decode_sbp_moment(f["sbp_id"])
        want_moment = datetime(2026, 11, 3, 14, 25, 35, tzinfo=MSK)
        check("sbp_id encodes operation (UTC)", moment == want_moment,
              f"{moment} != {want_moment}")
        check("sbp_id is new", f["sbp_id"] != donor["sbp_id"])
        check("sbp_id per-position alphabets (pos 15/17 are '0')",
              bool(re.fullmatch(
                  r"[AB]\d\d{3}\d{6}\d{3}0[134679BCKN]0[BG]10\d{2}\d{10}",
                  f["sbp_id"])),
              f["sbp_id"])
        check("sbp_id current era profile (0B/1004/0011881102)",
              f["sbp_id"][16:18] == "0B"
              and f["sbp_id"][18:22] == "1004"
              and f["sbp_id"][22:] == "0011881102", f["sbp_id"])
        check("sbp_id segment 19-22 is Rocket-observed",
              f["sbp_id"][18:22] in engine._SBP_SEGMENTS, f["sbp_id"])

        check("doc_number format", bool(re.fullmatch(r"M7\d{10}", f["doc_number"])),
              f["doc_number"])
        check("doc_number is new", f["doc_number"] != donor["doc_number"])
        doc_value = int(f["doc_number"][1:])
        check("doc_number matches operation date (corpus counter model)",
              73_520_000_000 < doc_value < 73_620_000_000, f["doc_number"])
        check("font subsets carry exactly their line's glyphs",
              _subsets_exact(receipt))

        check("CreationDate = operation +5 min (explicit delta)",
              "(D:20261103113035Z)" in info, info)
        check("ModDate == CreationDate", info.count("20261103113035Z") == 2, info)
        check("trailer /ID new, paired, 128 hex",
              bool(id_match) and id_match.group(1) == id_match.group(2)
              and len(id_match.group(1)) == 128
              and (not donor_id or id_match.group(1) != donor_id.group(1)),
              info)

        raw = out.read_bytes()
        donor_tags = set(re.findall(rb"/BaseFont/([A-Z]{6})\+",
                                    template.read_bytes()))
        gen_tags = set(re.findall(rb"/BaseFont/([A-Z]{6})\+", raw))
        check("subset tags are freshly generated (not donor's)",
              bool(gen_tags) and not (gen_tags & donor_tags),
              f"{len(gen_tags)} tags, overlap {gen_tags & donor_tags}")
        check("single-pass file, no incremental tail / padding",
              abs(len(raw) - template.stat().st_size) < 4096
              and raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
              and b"/XRef" not in raw,
              f"{len(raw)} vs {template.stat().st_size}")
        check("all streams are native zlib level 6 (78 9c)",
              _all_level6(out))
        check("filesystem mtime = CreationDate +1s",
              abs(out.stat().st_mtime -
                  datetime(2026, 11, 3, 11, 30, 36, tzinfo=timezone.utc).timestamp()) < 2,
              str(out.stat().st_mtime))

        text = subprocess.run(["pdftotext", str(out), "-"],
                              capture_output=True, text=True).stdout
        check("pdftotext sees new values",
              all(v in text for v in ("10 000 ₽", "ИВАН ПЕТРОВИЧ К", "ПАО СБЕРБАНК",
                                      f["sbp_id"], f["doc_number"])))
        if _has_qpdf():
            q = subprocess.run(["qpdf", "--check", str(out)],
                               capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in q, q[-200:])

        # Already-formatted names / known bank aliases pass through.
        out2 = Path(td) / "generated2.pdf"
        engine.generate_rocket(
            amount=500,
            operation_dt="03.11.2026 09:05",
            sender_name="АЛЕКСАНДР ЕВГЕНЬЕВИЧ Ж.",
            sender_phone="+7 963 297-20-08",
            recipient_name="РОМАН АНДРЕЕВИЧ В",
            recipient_phone="89118584552",
            recipient_bank="Т-Банк",
            sender_account="40817 81045 02313 09876",
            output_path=out2,
        )
        f2 = rocket_receipt.Receipt(str(out2)).fields
        check("rocket-form names + bank alias + account",
              f2["sender"] == "АЛЕКСАНДР ЕВГЕНЬЕВИЧ Ж."
              and f2["recipient"] == "РОМАН АНДРЕЕВИЧ В"
              and f2["bank_to"] == 'АО "ТБАНК"'
              and f2["amount"] == "500 ₽"
              and f2["account"] == "40817 81045 02313 09876",
              str(f2))

        out3 = Path(td) / "era1011.pdf"
        engine.generate_rocket(
            amount="10 ₽",
            operation_dt=datetime(2026, 11, 10, 22, 10, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+7 963 297-20-08",
            recipient_name="Ян Евгеньевич Ш",
            recipient_phone="+7 911 857-53-91",
            recipient_bank="Сбербанк",
            output_path=out3,
        )
        sbp3 = rocket_receipt.Receipt(str(out3)).fields["sbp_id"]
        check("10.11 SBP era 0B / 1006|1007|1011 / 0011890301",
              sbp3[16:18] == "0B"
              and sbp3[18:22] in {"1006", "1007", "1011"}
              and sbp3[22:] == "0011890301"
              and sbp3[14] == "0",
              sbp3)

    print("\nRejections")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        base = dict(
            operation_dt=datetime(2026, 11, 3, 14, 25, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            sender_phone="+7 900 351-70-80",
            recipient_name="Ковалёв Иван Петрович",
            recipient_phone="+7 963 297-20-08",
            recipient_bank="Сбербанк",
            output_path=out,
        )
        for label, override in (
            ("kopecks (no donor format)", {"amount": "100,50 ₽"}),
            ("bad phone", {"recipient_phone": "123"}),
            ("glyph outside donor pool", {"recipient_name": "Юрьев Юрий Юрьевич"}),
        ):
            try:
                engine.generate_rocket(**{**base, **override})
                raised = False
            except Exception:  # noqa: BLE001 - any honest rejection counts
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def _subsets_exact(receipt) -> bool:
    """Every field font subset holds exactly the glyphs of its line,
    as in genuine donors (no leftover donor characters, none missing)."""
    for run in receipt._fields.values():
        slot = receipt.fonts[run.font]
        if set(slot.cid_to_char.values()) != set(run.text):
            return False
    return True


def _has_qpdf() -> bool:
    return subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0


def _all_level6(path: Path) -> bool:
    """Every Flate stream is zlib level 6, as in every genuine donor."""
    import zlib

    pdf = rocket_pdf.PdfFile(path.read_bytes())
    streams = [o for o in pdf.objects.values()
               if o.has_stream and b"/FlateDecode" in o.dict_bytes]
    return bool(streams) and all(
        o.stream_raw[:2] == b"\x78\x9c"
        and zlib.compress(o.decoded_stream(), 6) == o.stream_raw
        for o in streams
    )


def main() -> int:
    if not DONORS:
        print("no donors in banks/rocket/donors/", file=sys.stderr)
        return 1
    cache = patch_rocket.harvest([str(p) for p in DONORS], verbose=False)
    print(cache.summary())
    stage2_roundtrip(cache)
    stage3_amount_only(cache)
    stage5_generate()
    print()
    if FAILURES:
        print(f"{FAILURES} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
