# -*- coding: utf-8 -*-
"""Tests for the Yandex generator (stable module).

Verifies fitz.get_text() contains the requested fields, Исх. № law,
metadata, embedded fonts, and valid 1-page A4 layout.
"""
import os, re, sys
from datetime import datetime, timedelta, timezone
import fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yandex_gen_stable as yg

MSK = timezone(timedelta(hours=3))

def _norm(text):
    """Normalise PyMuPDF text-layer substitutions for custom TTF fonts:
    U+00A0 nbsp -> space, U+00AD soft hyphen -> hyphen."""
    return (text or "").replace("\xa0", " ").replace("\xad", "-")

def _gen(recipient, phone, bank, amount, dt=None):
    meta = {}
    pdf = yg.generate(recipient, phone, bank, amount, dt=dt, meta_out=meta)
    doc = fitz.open(stream=pdf, filetype="pdf")
    txt = _norm(doc[0].get_text())
    m = doc.metadata
    n = doc.page_count
    r = doc[0].rect
    doc.close()
    return txt, meta, m, n, r

def test_text_contains_params():
    txt, meta, m, n, r = _gen("Роман Андреевич В.", "+79118584552",
                              "Озон Банк (Ozon)", 11)
    assert "Роман" in txt, "recipient first name missing"
    assert "Андреевич В." in txt, f"recipient rest missing"
    assert "+7 911 858-45-52" in txt, f"phone missing: {txt[:400]!r}"
    assert "Озон Банк (Ozon)" in txt, "bank missing"
    assert "+11,00 ₽" in txt, f"amount missing: {txt[:400]!r}"
    assert "Выписка по договору" in txt, "title missing"
    assert "АО «Яндекс Банк»" in txt, "bank header missing"
    assert "Э20260929767871183" in txt, "contract no missing"
    print("  OK test_text_contains_params")

def test_isx_no_law():
    for _ in range(20):
        meta = {}
        yg.generate("Ильяс Ильнурович Х.", "+79677399849",
                    "МТС Деньги (ЭКСИ Банк)", 16, meta_out=meta)
        isx = meta["isx_no"]
        assert re.fullmatch(r"\d{15}", isx), f"isx_no not 15 digits: {isx}"
        d = datetime.now(MSK)
        assert isx.startswith(d.strftime("%Y%m%d")), \
            f"isx_no date prefix mismatch: {isx} vs {d.strftime('%Y%m%d')}"
    print("  OK test_isx_no_law (20 runs)")

def test_metadata():
    txt, meta, m, n, r = _gen("Александр Евгеньевич Ж.", "+79003517080",
                              "Альфа-Банк", 100)
    assert m["producer"] == "OpenPDF 1.3.32", m["producer"]
    assert m["creator"].startswith("JasperReports Library version 6.21.2"), m["creator"]
    assert re.fullmatch(r"D:\d{14}Z", m["creationDate"]), m["creationDate"]
    cd = datetime.strptime(m["creationDate"], "D:%Y%m%d%H%M%SZ")
    cd = cd.replace(tzinfo=timezone.utc)
    assert cd <= datetime.now(timezone.utc) + timedelta(seconds=5), cd
    print("  OK test_metadata")

def test_fonts_embedded():
    meta = {}
    pdf = yg.generate("Никита Валерьевич У.", "+79931349557", "ВТБ", 10, meta_out=meta)
    doc = fitz.open(stream=pdf, filetype="pdf")
    has_type0 = has_cid = has_ff2 = False
    for x in range(1, doc.xref_length()):
        o = doc.xref_object(x, compressed=False)
        if "/Subtype /Type0" in o: has_type0 = True
        if "/Subtype /CIDFontType2" in o: has_cid = True
        if "/FontFile2" in o: has_ff2 = True
    doc.close()
    assert has_type0, "no Type0 font embedded"
    assert has_cid, "no CIDFontType2 embedded"
    assert has_ff2, "no FontFile2 stream embedded"
    print("  OK test_fonts_embedded")

def test_valid_single_page():
    txt, meta, m, n, r = _gen("Семён Евгеньевич О.", "+79826389563", "ЮМани", 13)
    assert n == 1, f"expected 1 page, got {n}"
    assert abs(r.width - 595.32) < 5 and abs(r.height - 841.92) < 5, (r.width, r.height)
    print("  OK test_valid_single_page")

def test_amount_formatting():
    txt, meta, m, n, r = _gen("Тест Тестович Т.", "+79998887766", "Сбербанк", 219)
    assert "+219,00 ₽" in txt, txt[:300]
    txt2, _, _, _, _ = _gen("Тест Тестович Т.", "+79998887766", "Сбербанк", -16)
    assert "–16,00 ₽" in txt2, txt2[:300]
    print("  OK test_amount_formatting")

def test_meta_out_populated():
    meta = {}
    yg.generate("Роман Андреевич В.", "+79118584552", "Озон Банк (Ozon)", 11, meta_out=meta)
    for k in ("isx_no", "creation", "op_dt", "op_id", "amount_fmt",
              "phone_fmt", "period_from", "period_to", "stmt_date", "id", "donor"):
        assert k in meta, f"meta missing {k}"
    assert meta["phone_fmt"] == "+7 911 858-45-52", meta["phone_fmt"]
    assert meta["amount_fmt"] == "+11,00 ₽", meta["amount_fmt"]
    assert meta["donor"] == "yandex"
    assert len(meta["id"][0]) == 32 and len(meta["id"][1]) == 32
    print("  OK test_meta_out_populated")

def test_deterministic_isx_prefix():
    m1, m2 = {}, {}
    yg.generate("А Б", "+70000000001", "Сбербанк", 1, meta_out=m1)
    yg.generate("В Г", "+70000000002", "ВТБ", 2, meta_out=m2)
    assert m1["isx_no"][:8] == m2["isx_no"][:8], \
        f"date prefix differs: {m1['isx_no']} vs {m2['isx_no']}"
    assert m1["isx_no"] != m2["isx_no"], "isx_no must be unique"
    print("  OK test_deterministic_isx_prefix")

def test_byte_compare_smoke():
    """E2E criterion: fitz.get_text() contains all requested fields."""
    txt, meta, m, n, r = _gen("Роман Андреевич В.", "+79118584552",
                              "Озон Банк (Ozon)", 11)
    expected = ["Роман", "Андреевич В.", "+7 911 858-45-52",
                "Озон Банк (Ozon)", "+11,00 ₽", "Выписка по договору",
                "АО «Яндекс Банк»", "Э20260929767871183",
                "T.C. Чиличихина", "Страница 1 из 1"]
    for e in expected:
        assert e in txt, f"expected {e!r} in text, got: {txt[:500]!r}"
    print("  OK test_byte_compare_smoke")

def run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try: t()
        except AssertionError as e:
            failed += 1; print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1; print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'-'*50}")
    print(f"YANDEX GENERATOR TESTS: {len(tests)-failed}/{len(tests)} passed")
    if failed:
        print(f"  {failed} FAILED"); sys.exit(1)
    print("  ALL PASSED")

if __name__ == "__main__":
    run_all()
