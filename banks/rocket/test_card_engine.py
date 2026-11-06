#!/usr/bin/env python3
"""Checks for the Rocket card-transfer engine, in the order of the chat's stages.

Run with: python3 test_card_engine.py

Stage 2 — a genuine card donor opened and saved unchanged is
          byte-for-byte identical (donors 43-45 in donors_card/).
Stage 3 — changing only the amount uses the native RocketSans-Wide
          font and leaves every other field untouched; a field font
          assembled for another donor's exact text is byte-identical
          to that donor's own font (CFF + ToUnicode + widths).
Stage 4/5 — generate_rocket_card() rewrites all fields plus the linked
          dates/numbers/IDs in a single-pass file (single xref, no
          incremental-update tail, native zlib level 6 streams).
"""

from __future__ import annotations

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
CARD_DONORS = sorted((_HERE / "donors_card").glob("receipt_rocket_*.pdf"))
SBP_DONORS = sorted((_HERE / "donors").glob("receipt_rocket_*.pdf"))

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


def _pool(cache_paths):
    return patch_rocket.harvest([str(p) for p in cache_paths], verbose=False)


def stage2_roundtrip(cache) -> None:
    print(f"\nStage 2: byte-for-byte round-trip ({len(CARD_DONORS)} card donors)")
    check("card donors 43-45 present", len(CARD_DONORS) == 3, str(len(CARD_DONORS)))
    for path in CARD_DONORS:
        source = path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            rocket_pdf.PdfFile(source).incremental_save(str(tmp))
            low_level = tmp.read_bytes() == source
            receipt = rocket_receipt.Receipt(str(path), cache)
            is_card = receipt.format == rocket_receipt.FORMAT_CARD
            receipt.save(str(tmp), mode="incremental")
            via_model = tmp.read_bytes() == source
        finally:
            tmp.unlink(missing_ok=True)
        check(path.name, low_level and via_model and is_card,
              f"low={low_level} model={via_model} card={is_card}")


def stage3_amount_only(cache) -> None:
    print("\nStage 3: change only the amount (native font)")
    donor_path = _HERE / "donors_card" / "receipt_rocket_44.pdf"
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

    print("\nStage 3b: font assembled for another donor's text is byte-identical")
    src_path = _HERE / "donors_card" / "receipt_rocket_43.pdf"
    tgt = rocket_receipt.Receipt(
        str(_HERE / "donors_card" / "receipt_rocket_44.pdf"))
    for field in ("date", "bank_to", "card", "doc_number", "amount"):
        r = rocket_receipt.Receipt(str(src_path), cache)
        r.minimal_glyphs = True
        r.set(field, tgt.fields[field])
        slot = r.fonts[r._fields[field].font]
        tslot = tgt.fonts[tgt._fields[field].font]
        same = (
            r.pdf.obj(slot.fontfile_obj).decoded_stream()
            == tgt.pdf.obj(tslot.fontfile_obj).decoded_stream()
            and r.pdf.obj(slot.tounicode_obj).decoded_stream()
            == tgt.pdf.obj(tslot.tounicode_obj).decoded_stream()
            and slot.widths == tslot.widths
            and slot.cid_to_char == tslot.cid_to_char
        )
        check(f"font for {field} == donor 44's own font", same)


def stage5_generate() -> None:
    print("\nStage 4/5: generate_rocket_card() — all fields + linked IDs")
    template = _HERE / "donors_card" / "receipt_rocket_44.pdf"
    donor_receipt, _, donor_id = info_of(template)
    donor = dict(donor_receipt.fields)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "card.pdf"
        result = engine.generate_rocket_card(
            amount="12 500,00 ₽",
            operation_dt=datetime(2026, 11, 3, 15, 2, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="2200 1234 5678 4831",
            recipient_bank="Росбанк",
            output_path=out,
            creation_delta_minutes=5,
        )
        check("returns the output path", Path(result) == out and out.is_file())

        receipt, info, id_match = info_of(out)
        f = receipt.fields
        check("layout is card, no SBP fields",
              receipt.format == rocket_receipt.FORMAT_CARD
              and "sbp_id" not in f and "recipient" not in f
              and "phone_to" not in f and "phone_from" not in f,
              ", ".join(sorted(f)))
        expected = {
            "date": "03.11.2026 15:02 ПО МСК",
            "amount": "12 500 ₽",
            "card": "2200 **** **** 4831",
            "bank_to": "РОСБАНК",
            "sender": "АЛЕКСАНДР ЕВГЕНЬЕВИЧ Ж.",
            "account": donor["account"],
            "fee": "БЕЗ КОМИССИИ",
            "direction": "ИСХОДЯЩИЙ",
            "status": "УСПЕШНО",
            "bank_from": "РОКЕТ",
        }
        for name, want in expected.items():
            check(f"field {name}", f.get(name) == want,
                  f"{f.get(name)!r} != {want!r}")

        check("doc_number format", bool(re.fullmatch(r"M7\d{10}", f["doc_number"])),
              f["doc_number"])
        check("doc_number is new", f["doc_number"] != donor["doc_number"])
        doc_value = int(f["doc_number"][1:])
        check("doc_number matches operation date (joint corpus model)",
              73_540_000_000 < doc_value < 73_580_000_000, f["doc_number"])
        check("font subsets carry exactly their line's glyphs",
              _subsets_exact(receipt))

        check("CreationDate = operation +5 min (explicit delta)",
              "(D:20261103120700Z)" in info, info)
        check("ModDate == CreationDate", info.count("20261103120700Z") == 2, info)
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
                  datetime(2026, 11, 3, 12, 7, 1, tzinfo=timezone.utc).timestamp()) < 2,
              str(out.stat().st_mtime))

        text = subprocess.run(["pdftotext", str(out), "-"],
                              capture_output=True, text=True).stdout
        check("pdftotext sees new values",
              all(v in text for v in ("12 500 ₽", "2200 **** **** 4831",
                                      "РОСБАНК", f["doc_number"])))
        if _has_qpdf():
            q = subprocess.run(["qpdf", "--check", str(out)],
                               capture_output=True, text=True).stdout
            check("qpdf clean", "No syntax or stream encoding errors" in q, q[-200:])

        # No-bank layout: WB (donor 45's bank) omits the whole block.
        out2 = Path(td) / "card_nobank.pdf"
        engine.generate_rocket_card(
            amount=890,
            operation_dt="03.11.2026 13:26",
            sender_name="АЛЕКСАНДР ЕВГЕНЬЕВИЧ Ж.",
            recipient_card="2204 **** **** 6108",
            recipient_bank="ВБ Банк",
            output_path=out2,
        )
        r2 = rocket_receipt.Receipt(str(out2))
        nobank_template = _HERE / "donors_card" / "receipt_rocket_45.pdf"
        check("WB -> no bank block, no-bank template size",
              "bank_to" not in r2.fields
              and r2.fields["card"] == "2204 **** **** 6108"
              and r2.fields["amount"] == "890 ₽"
              and abs(out2.stat().st_size - nobank_template.stat().st_size) < 4096,
              str(r2.fields))
        text2 = subprocess.run(["pdftotext", str(out2), "-"],
                               capture_output=True, text=True).stdout
        check("pdftotext has no БАНК ПОЛУЧАТЕЛЯ block",
              "БАНК ПОЛУЧАТЕЛЯ" not in text2, text2[:200])

        # Short bank forms + masked-card input pass through.
        out3 = Path(td) / "card_alfa.pdf"
        engine.generate_rocket_card(
            amount=500,
            operation_dt="03.11.2026 09:05",
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="2200 **** 7640",
            recipient_bank="Альфа-Банк",
            sender_account="40817 81045 02313 09876",
            output_path=out3,
        )
        f3 = rocket_receipt.Receipt(str(out3)).fields
        check("masked card + short bank form + account",
              f3["card"] == "2200 **** **** 7640"
              and f3["bank_to"] == "АЛЬФА-БАНК"
              and f3["account"] == "40817 81045 02313 09876",
              str(f3))

    print("\nRejections")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.pdf"
        base = dict(
            operation_dt=datetime(2026, 11, 3, 15, 2, tzinfo=MSK),
            sender_name="Жеребятьев Александр Евгеньевич",
            recipient_card="2200 1234 5678 4831",
            recipient_bank="Росбанк",
            output_path=out,
        )
        for label, override in (
            ("kopecks (no donor format)", {"amount": "100,50 ₽"}),
            ("bad card", {"recipient_card": "123"}),
            ("glyph outside donor pool", {"sender_name": "Юрьев Юрий Юрьевич"}),
            ("bank on no-bank donor", {
                "donor_path": str(_HERE / "donors_card" / "receipt_rocket_45.pdf"),
            }),
            ("no bank on with-bank donor", {
                "recipient_bank": None,
                "donor_path": str(_HERE / "donors_card" / "receipt_rocket_44.pdf"),
            }),
            ("SBP donor as card template", {
                "donor_path": str(_HERE / "donors" / "receipt_rocket_42.pdf"),
            }),
        ):
            try:
                engine.generate_rocket_card(**{**base, **override})
                raised = False
            except Exception:  # noqa: BLE001 - any honest rejection counts
                raised = True
            check(f"rejects {label}", raised, "no error raised")


def _subsets_exact(receipt) -> bool:
    for run in receipt._fields.values():
        slot = receipt.fonts[run.font]
        if set(slot.cid_to_char.values()) != set(run.text):
            return False
    return True


def _has_qpdf() -> bool:
    return subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0


def _all_level6(path: Path) -> bool:
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
    if not CARD_DONORS:
        print("no donors in banks/rocket/donors_card/", file=sys.stderr)
        return 1
    cache = _pool(CARD_DONORS + SBP_DONORS)
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
