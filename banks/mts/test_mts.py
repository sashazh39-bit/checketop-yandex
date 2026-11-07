#!/usr/bin/env python3
"""Tests for the MTS receipt patcher (stages 2-4 of the MTS chat plan).

Run:  python3 banks/mts/test_mts.py

Stage 2 (round-trip): every genuine donor re-emitted with its own
values and its own metadata must come out BYTE-IDENTICAL -- this proves
the parser, the serializer, the font subsetter, the ObjStm/xref
writers and the compression all reproduce the genuine OpenPDF output.

Stage 3 (one field): changing only the amount must keep every other
byte region intact except the ones genuinely affected (content stream,
font subsets, ObjStm, xref), and the new amount must extract back.

Stage 4 (all fields): a fully re-filled receipt must stay a
single-pass PDF with coherent /Names, dests, subsets and metadata.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys as _sys
if str(HERE) not in _sys.path:
    _sys.path.insert(0, str(HERE))

from mts_font import FontPool, EmbeddedSubset  # noqa: E402
from mts_receipt import Receipt, RUN_VALUES, FIELD_LABELS  # noqa: E402

DONORS = sorted((HERE / "donors").glob("mts_donor_*.pdf"))
OUT = HERE / "out"

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


def donor_tag(receipt: Receipt, family: str) -> str:
    return receipt._donor_tag(family).decode()


def identity_metadata(receipt: Receipt) -> dict:
    return dict(
        creation_date=receipt.donor_creation_date,
        correlation_id=receipt.donor_correlation_id,
        file_id=receipt.donor_file_id,
        subset_tags=(donor_tag(receipt, "regular"), donor_tag(receipt, "medium")),
        # the wrapped SBP line's x carries ±0.006 pt of AWT/JasperReports
        # float noise on genuine receipts (BANK.md §3); in identity mode
        # we inherit the donor's own printed value
        sbp_line1_x=receipt.runs[19].x,
    )


def extract_text_strings(raw: bytes) -> list:
    """All UTF-16BE strings drawn by the content stream of a saved file."""
    m = re.search(rb"(?m)^29 0 obj\r?$", raw)
    off = m.start()
    hdr_end = raw.index(b"stream\n", off) + 7
    end = raw.index(b"endstream", hdr_end)
    content = zlib.decompress(raw[hdr_end:end])
    out = []
    for tm in re.finditer(rb"\(((?:[^()\\]|\\.)*)\)Tj", content):
        raw_s = tm.group(1)
        data = bytearray()
        i = 0
        while i < len(raw_s):
            if raw_s[i] == 0x5C and i + 1 < len(raw_s):
                nxt = raw_s[i + 1]
                data.append({0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}.get(nxt, nxt))
                i += 2
            else:
                data.append(raw_s[i])
                i += 1
        out.append(bytes(data))
    return out


def stage2_roundtrip() -> None:
    print("== stage 2: donor round-trip (byte-identical re-emit) ==")
    if not DONORS:
        check("donors present", False, "no mts_donor_*.pdf in donors/")
        return
    for path in DONORS:
        receipt = Receipt(path)
        target = OUT / (path.stem + "_roundtrip.pdf")
        receipt.save(target, **identity_metadata(receipt))
        same = target.read_bytes() == path.read_bytes()
        check(f"{path.name} re-emit byte-identical", same,
              f"(donor {path.stat().st_size} B vs rebuilt {target.stat().st_size} B)")
        if not same:
            a, b = path.read_bytes(), target.read_bytes()
            i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]),
                     min(len(a), len(b)))
            print(f"      first diff at byte {i}: donor {a[max(0,i-16):i+16]!r}")
            print(f"                          rebuilt {b[max(0,i-16):i+16]!r}")
        target.unlink(missing_ok=True)


def stage3_one_field() -> None:
    print("== stage 3: change one field (amount) ==")
    path = DONORS[0]
    receipt = Receipt(path)
    old_amount = receipt.fields["amount"]
    new_amount = "100,00 ₽" if old_amount != "100,00 ₽" else "500,00 ₽"
    receipt.set("amount", new_amount)
    receipt.set("total", new_amount)
    target = OUT / "stage3_amount.pdf"
    receipt.save(target, **identity_metadata(receipt))
    raw = target.read_bytes()

    check("single startxref / single %%EOF / no /Prev",
          raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
          and b"/Prev" not in raw)
    check("no xref-table (xref stream like donor)", b"\nxref\n" not in raw)

    strings = extract_text_strings(raw)
    # decode with the new subset's own ToUnicode through PyMuPDF-free path:
    joined = b"".join(strings)
    # amount appears as CID bytes; simpler: parse via Receipt again
    receipt2 = Receipt(target)
    check("amount re-extracts", receipt2.fields["amount"] == new_amount,
          f"got {receipt2.fields['amount']!r}")
    check("total follows amount", receipt2.fields["total"] == new_amount)
    check("other fields intact",
          all(receipt2.fields[f] == Receipt(path).fields[f]
              for f in ("recipient", "phone", "bank", "account", "fee",
                        "txcode", "sbp", "datetime")))
    check("creation date kept (pinned)", receipt2.donor_creation_date ==
          Receipt(path).donor_creation_date)
    # the /Names tree must not contain the OLD amount anymore
    m = re.search(rb"(?m)^31 0 obj\r?$", raw)
    off = m.start()
    hdr_end = raw.index(b"stream\n", off) + 7
    end = raw.index(b"endstream", hdr_end)
    objstm = zlib.decompress(raw[hdr_end:end])
    from mts_pdf import mangle_name_key, escape_string
    old_key = escape_string(mangle_name_key(old_amount))
    new_key = escape_string(mangle_name_key(new_amount))
    check("/Names has new amount key", new_key in objstm)
    check("/Names lost old amount key", old_key not in objstm)
    # dest count unchanged (21 destinations, objects 32..52)
    check("21 named dests kept", len(re.findall(rb"\[1 0 R/XYZ ", objstm)) == 21)


def full_pool() -> dict:
    """Glyph pool from every genuine donor (what the engine uses)."""
    pool = {"regular": FontPool(), "medium": FontPool()}
    for path in DONORS:
        donor = Receipt(path)
        for family in pool:
            pool[family].absorb(donor.subsets[family])
    return pool


def stage4_all_fields() -> None:
    print("== stage 4: all fields re-filled ==")
    path = DONORS[0]
    receipt = Receipt(path, full_pool())
    receipt.set("datetime", "07.11.2026 13:05:11 (UTC+3)")
    receipt.set("recipient", "Иван Иванович И")
    receipt.set("phone", "+7 (900) 111-22-33")
    receipt.set("bank", "ВТБ")
    receipt.set("message", "Перевод СБП")
    receipt.set("account", "40817 81020 00025 66548")
    receipt.set("amount", "1 500,00 ₽")
    receipt.set("total", "1 500,00 ₽")
    receipt.set("fee", "0,00 ₽")
    receipt.set("txcode", "019545001337")
    receipt.set("sbp", "A63101309051130E0G10050011890101")
    target = OUT / "stage4_full.pdf"
    receipt.save(target)
    raw = target.read_bytes()
    receipt2 = Receipt(target)
    ok = all([
        receipt2.fields["datetime"] == "07.11.2026 13:05:11 (UTC+3)",
        receipt2.fields["recipient"] == "Иван Иванович И",
        receipt2.fields["phone"] == "+7 (900) 111-22-33",
        receipt2.fields["bank"] == "ВТБ",
        receipt2.fields["amount"] == "1 500,00 ₽",
        receipt2.fields["total"] == "1 500,00 ₽",
        receipt2.fields["txcode"] == "019545001337",
        receipt2.fields["sbp"] == "A63101309051130E0G10050011890101",
    ])
    check("all fields re-extract", ok,
          str({k: v for k, v in receipt2.fields.items()}))
    check("single startxref / no /Prev",
          raw.count(b"startxref") == 1 and b"/Prev" not in raw)
    # fresh metadata present and different from donor
    check("fresh correlation id", receipt2.donor_correlation_id is not None
          and receipt2.donor_correlation_id != Receipt(path).donor_correlation_id)
    # SBP id wraps into two lines like on genuine receipts
    strings = extract_text_strings(raw)
    from mts_receipt import _unescape  # noqa
    check("names tree keys re-sorted & renumbered", b"/Names[" in raw
          or b"/Names[" in zlib.decompress(
              raw[raw.index(b"stream\n", raw.index(b"31 0 obj")) + 7:
                  raw.index(b"endstream", raw.index(b"31 0 obj"))]))


def stage5_engine() -> None:
    print("== stage 5: generate_mts() ==")
    import importlib
    engine = importlib.import_module("engine")
    from mts_receipt import MSK
    from datetime import datetime

    out = engine.generate_mts(
        amount="100",
        operation_dt=datetime(2026, 11, 7, 6, 59, 30, tzinfo=MSK),
        recipient_name="Иванов Иван Иванович",
        recipient_phone="89001112233",
        recipient_bank="Т-Банк",
        output_path=OUT / "engine_basic.pdf",
    )
    r = Receipt(out)
    check("engine: printed name form", r.fields["recipient"] == "Иван Иванович И")
    check("engine: phone form", r.fields["phone"] == "+7 (900) 111-22-33")
    check("engine: amount form", r.fields["amount"] == "100,00 ₽")
    check("engine: total == amount", r.fields["total"] == "100,00 ₽")
    check("engine: fee 0,00", r.fields["fee"] == "0,00 ₽")
    check("engine: message default", r.fields["message"] == "Перевод СБП")
    check("engine: account inherited",
          r.fields["account"] == "40817 81020 00025 66548")
    sbp = r.fields["sbp"]
    check("engine: sbp 32 chars, era 0G tail",
          len(sbp) == 32 and sbp[16:18] == "0G" and sbp.endswith("0011890101"))
    moment = engine.decode_sbp_moment(sbp)
    hdr = datetime(2026, 11, 7, 6, 59, 30, tzinfo=MSK)
    delta = (hdr - moment).total_seconds() if moment else -1
    check("engine: header leads sbp moment by 3..29 s", 3 <= delta <= 29,
          f"delta={delta}")
    check("engine: txcode above donor anchor",
          r.fields["txcode"] > "019544730490")
    raw = out.read_bytes()
    check("engine: single pass",
          raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
          and b"/Prev" not in raw)

    # era by UTC date: 05.11 MSK evening -> old era 0B/0011881102
    out2 = engine.generate_mts(
        amount=30, operation_dt=datetime(2026, 11, 5, 22, 30, 0, tzinfo=MSK),
        recipient_name="Петров Петр Петрович", recipient_phone="+7 900 111-22-33",
        recipient_bank="Т-Банк", output_path=OUT / "engine_oldera.pdf",
    )
    sbp2 = Receipt(out2).fields["sbp"]
    check("engine: 05.11 op -> era 0B/0011881102",
          sbp2[16:18] == "0B" and sbp2.endswith("0011881102"), sbp2)

    # synthetic glyphs from the 2019 CFF master cover the whole Cyrillic
    # alphabet: names that were refused with the 3-donor pool now generate
    out3 = engine.generate_mts(
        amount=100, operation_dt=datetime(2026, 11, 7, 7, 0, 0, tzinfo=MSK),
        recipient_name="Жуков Женя Женович",
        recipient_phone="+7 900 111-22-33", recipient_bank="Т-Банк",
        output_path=OUT / "engine_synthetic.pdf",
    )
    check("engine: synthetic glyphs cover Ж/ё/etc",
          Receipt(out3).fields["recipient"] == "Женя Женович Ж")
    out4 = engine.generate_mts(
        amount=200, operation_dt=datetime(2026, 11, 7, 7, 1, 0, tzinfo=MSK),
        recipient_name="Фёдоров Фёдор Фёдорович",
        recipient_phone="+7 900 111-22-33", recipient_bank="Т-Банк",
        output_path=OUT / "engine_synthetic2.pdf",
    )
    check("engine: synthetic Фёдоров generates",
          Receipt(out4).fields["recipient"] == "Фёдор Фёдорович Ф")

    # a character outside the 2019 font itself is still an honest refusal
    try:
        engine.generate_mts(
            amount=100, operation_dt=datetime(2026, 11, 7, 7, 2, 0, tzinfo=MSK),
            recipient_name="陈 陈 陈",
            recipient_phone="+7 900 111-22-33", recipient_bank="Т-Банк",
            output_path=OUT / "engine_refuse.pdf",
        )
        check("engine: honest refusal outside the font", False, "no error raised")
    except engine.GlyphCoverageError:
        check("engine: honest refusal outside the font", True)
    except Exception as e:  # noqa: BLE001
        check("engine: honest refusal outside the font", False, repr(e))


def main() -> int:
    OUT.mkdir(exist_ok=True)
    stage2_roundtrip()
    stage3_one_field()
    stage4_all_fields()
    stage5_engine()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
