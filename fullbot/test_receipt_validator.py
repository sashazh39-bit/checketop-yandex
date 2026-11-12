# -*- coding: utf-8 -*-
"""test_receipt_validator.py - 24 cases: 5 valid per bank + 4 corrupted per bank.

Run on the VPS (where generators + donors live):
    /opt/mts-bot/venv/bin/python -m pytest test_receipt_validator.py -v
or:
    /opt/mts-bot/venv/bin/python -m unittest test_receipt_validator -v

Valid cases (should pass=True): freshly generated receipts per bank.
Corrupted cases (should pass=False with the right diagnosis):
  - corrupt /ID (flip a hex byte in a trailer /ID half)
  - foreign deflate (recompress a stream with zlib level 9 -> header 78 da)
  - wrong bank (rename Producer so detection picks the wrong bank)
  - broken xref (clobber the xref free entry)
"""
import os, re, sys, unittest, zlib, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MTS_DONOR_SBP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "donors", "file25.pdf"))
os.environ.setdefault("JZCOMP_DIR", os.path.dirname(os.path.abspath(__file__)))

import receipt_validator as rv
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))
_NOW = datetime.now(MSK).replace(tzinfo=None, microsecond=0)


def _gen_mts():
    import mts_generator as m
    return m.generate("Иван Иванович И", "+7 999 123-45-67", "Т-Банк", 2500, dt=_NOW)


def _gen_wb():
    import wb_generator as w
    return w.generate("Иван Иванович И", "+7 999 123-45-67", "Альфа-Банк", 2500, dt=_NOW)


def _gen_vtb():
    import vtb_generator as v
    return v.generate("Михаил Иванович Х.", "Иван Иванович И", "+7 999 123-45-67", "Т-Банк", 2500, dt=_NOW)


def _gen_rshb():
    import rshb_generator as r
    return r.generate("Иван Иванович И", "+7 999 123-45-67", "Т-Банк", 2500, dt=_NOW)


def _gen_ozon():
    import ozon_generator as o
    return o.generate("Иван Иванович И", "+7 999 123-45-67", "Т-Банк", 2500, dt=_NOW)


# ── corruption helpers ───────────────────────────────────────────────
def corrupt_id(pdf: bytes) -> bytes:
    """Flip one hex char in the FIRST /ID half so halves-equal check breaks
    (for WB/VTB/RSHB) or the ID becomes malformed."""
    m = re.search(rb"/ID\s*\[<([0-9a-fA-F]{32})>", pdf)
    if not m:
        # Ozon has no /ID - inject a fake one to break the 'id_absent' check
        return pdf.replace(b"trailer\n<</Size", b"trailer\n<</ID [<aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa><aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa>]/Size", 1)
    pos = m.start(1) + 5  # mid-first-half
    b = bytearray(pdf)
    b[pos] = (b[pos] + 1) % 256
    return bytes(b)


def corrupt_deflate(pdf: bytes) -> bytes:
    """Recompress the first FlateDecode stream with zlib level 9 (header 78 da)
    so the 78 9c header check fails (foreign deflate dialect)."""
    m = re.search(rb"(?m)^(\d+)\s+0\s+obj", pdf)
    # find first stream with /FlateDecode
    for mm in re.finditer(rb"(?m)^(\d+)\s+0\s+obj\r?\n", pdf):
        start = mm.end(); end = pdf.find(b"endobj", start)
        if end < 0: continue
        body = pdf[start:end]
        if b"/FlateDecode" not in body: continue
        si = body.find(b"stream")
        s = si + 6
        if body[s:s+2] == b"\r\n": s += 2
        elif body[s:s+1] == b"\n": s += 1
        lm = re.search(rb"/Length\s+(\d+)", body[:si])
        if not lm: continue
        declen = int(lm.group(1))
        data = body[s:s+declen]
        try:
            raw = zlib.decompress(data)
        except Exception:
            continue
        new = zlib.compress(raw, 9)  # 78 da header
        # patch /Length and replace stream
        new_body = body[:si].replace(rb"/Length " + str(declen).encode(), rb"/Length " + str(len(new)).encode())
        new_body += b"stream\n" + new + b"\nendstream"
        return pdf[:mm.start()] + mm.group(0) + new_body + pdf[end:]
    return pdf


def corrupt_bank(pdf: bytes) -> bytes:
    """Replace Producer with a foreign string so bank detection fails."""
    return re.sub(rb"/Producer\s*\(([^)]*)\)", rb"/Producer(FakeProducer 0.0)", pdf, count=1)


def corrupt_xref(pdf: bytes) -> bytes:
    """Clobber the xref free entry so xref_table check fails."""
    return re.sub(rb"0000000000\s+65535\s+f", b"XXXXXXXXXX 65535 f ", pdf, count=1)


# ─────────────────────────────────────────────────────────────────────
class TestValid(unittest.TestCase):
    """5 freshly generated receipts - must pass=True."""

    def test_mts_valid(self):
        r = rv.validate(_gen_mts())
        self.assertTrue(r["passed"], f"failed: {[c['name'] for c in r['checks'] if not c['passed']]}")
        self.assertEqual(r["bank"], "MTS")

    def test_wb_valid(self):
        r = rv.validate(_gen_wb())
        self.assertTrue(r["passed"], f"failed: {[c['name'] for c in r['checks'] if not c['passed']]}")
        self.assertEqual(r["bank"], "WB")

    def test_vtb_valid(self):
        r = rv.validate(_gen_vtb())
        self.assertTrue(r["passed"], f"failed: {[c['name'] for c in r['checks'] if not c['passed']]}")
        self.assertEqual(r["bank"], "VTB")

    def test_rshb_valid(self):
        r = rv.validate(_gen_rshb())
        self.assertTrue(r["passed"], f"failed: {[c['name'] for c in r['checks'] if not c['passed']]}")
        self.assertEqual(r["bank"], "RSHB")

    def test_ozon_valid(self):
        r = rv.validate(_gen_ozon())
        self.assertTrue(r["passed"], f"failed: {[c['name'] for c in r['checks'] if not c['passed']]}")
        self.assertEqual(r["bank"], "OZON")


class TestCorruptID(unittest.TestCase):
    """/ID corruption - must pass=False (except Ozon where /ID is absent)."""

    def test_wb_corrupt_id(self):
        r = rv.validate(corrupt_id(_gen_wb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("id_halves_equal", names)

    def test_rshb_corrupt_id(self):
        r = rv.validate(corrupt_id(_gen_rshb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("id_halves_equal", names)

    def test_vtb_corrupt_id(self):
        r = rv.validate(corrupt_id(_gen_vtb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("id_halves_equal", names)

    def test_ozon_inject_id(self):
        # Ozon must NOT have /ID; injecting one breaks id_absent
        r = rv.validate(corrupt_id(_gen_ozon()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("id_absent", names)


class TestCorruptDeflate(unittest.TestCase):
    """Foreign deflate (zlib L9, header 78 da) - must pass=False."""

    def test_wb_foreign_deflate(self):
        r = rv.validate(corrupt_deflate(_gen_wb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertTrue(any("flate" in n for n in names), f"expected flate fail, got {names}")

    def test_rshb_foreign_deflate(self):
        r = rv.validate(corrupt_deflate(_gen_rshb()))
        self.assertFalse(r["passed"])

    def test_vtb_foreign_deflate(self):
        r = rv.validate(corrupt_deflate(_gen_vtb()))
        self.assertFalse(r["passed"])

    def test_mts_foreign_deflate(self):
        r = rv.validate(corrupt_deflate(_gen_mts()))
        self.assertFalse(r["passed"])


class TestWrongBank(unittest.TestCase):
    """Producer replaced with foreign string - bank detection fails."""

    def test_wb_wrong_bank(self):
        r = rv.validate(corrupt_bank(_gen_wb()))
        self.assertFalse(r["passed"])
        # producer check must fail even if font-fallback still names the bank
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("producer", names)

    def test_mts_wrong_bank(self):
        r = rv.validate(corrupt_bank(_gen_mts()))
        self.assertFalse(r["passed"])

    def test_vtb_wrong_bank(self):
        r = rv.validate(corrupt_bank(_gen_vtb()))
        self.assertFalse(r["passed"])

    def test_ozon_wrong_bank(self):
        r = rv.validate(corrupt_bank(_gen_ozon()))
        self.assertFalse(r["passed"])


class TestCorruptXref(unittest.TestCase):
    """Broken xref free entry - must pass=False (banks with classic xref)."""

    def test_wb_corrupt_xref(self):
        r = rv.validate(corrupt_xref(_gen_wb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("xref_table", names)

    def test_rshb_corrupt_xref(self):
        r = rv.validate(corrupt_xref(_gen_rshb()))
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("xref_table", names)

    def test_vtb_corrupt_xref(self):
        r = rv.validate(corrupt_xref(_gen_vtb()))
        self.assertFalse(r["passed"])

    def test_ozon_corrupt_xref(self):
        r = rv.validate(corrupt_xref(_gen_ozon()))
        self.assertFalse(r["passed"])


class TestNotPdf(unittest.TestCase):
    def test_empty(self):
        r = rv.validate(b"")
        self.assertFalse(r["passed"])

    def test_not_pdf(self):
        r = rv.validate(b"hello world not a pdf")
        self.assertFalse(r["passed"])
        names = [c["name"] for c in r["checks"] if not c["passed"]]
        self.assertIn("pdf_header", names)


if __name__ == "__main__":
    unittest.main()
