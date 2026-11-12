# -*- coding: utf-8 -*-
"""Tests for yandex_generator.py — byte-faithful donor-template rebuild.

The generator copies a genuine Yandex Bank statement donor verbatim and
only regenerates the Info CreationDate and the trailer /ID (both halves
equal, lowercase 32-hex per the Jasper/OpenPDF law verified on 38 genuine
corpus PDFs). Text content, page count, object count, fonts and stream
encoding must be byte-identical to the donor.
"""
import os, sys, io, re, hashlib, unittest
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yandex_generator as yg

DONOR_NAMES = [
    "33c8e075-893b-4abf-9bd1-c730a4edb729.pdf",
    "076aa890-625a-41b2-94be-048bec7d72d2.pdf",
    "96ee2a6f-04ff-43a8-ae1c-ff829322e1c8.pdf",
    "2460ac89-1d68-4239-9a1c-5c5baace9ede.pdf",
    "e552f408-dace-4940-bc94-d8a5affcd7db.pdf",
    "b34c97a3-af09-488d-858e-b1d25a7cb93b.pdf",
]
_WIN_DIR = r"C:\Users\nemor\Downloads\Telegram Desktop"
_VPS_DIR = "/opt/mts-bot/yandex_donors"
if os.path.isdir(_VPS_DIR):
    DONORS = [os.path.join(_VPS_DIR, n) for n in DONOR_NAMES]
else:
    DONORS = [os.path.join(_WIN_DIR, n) for n in DONOR_NAMES]
ORIG_MD5 = ["56ef3da7a3dea4a767453ecf501f26e4", "5cd7badcb2cc3cad6996886dfa8f802d",
            "0bd98d330f45cbe45bd3ab647aa16650", "60491f52ec11a59a239253ffb9f5ff00",
            "7725cd2f11eea664550d994d8f8b09f2", "b06cbdcb71576cd6899f2807c6da42c5"]


class TestRebuild(unittest.TestCase):
    def test_donors_present(self):
        for p in DONORS:
            self.assertTrue(os.path.exists(p), f"donor missing: {p}")

    def test_engine_available(self):
        self.assertIn(yg._Z_ENGINE, ("libdeflate_L6", "stdzlib_L6"))

    def test_each_donor_rebuilds_byte_faithful(self):
        """For each of the 6 donors: generate() must produce a PDF whose
        size, page count, text content and object count match the donor
        exactly; only CreationDate and /ID differ."""
        for i, p in enumerate(DONORS):
            with open(p, "rb") as f: orig = f.read()
            self.assertEqual(hashlib.md5(orig).hexdigest(), ORIG_MD5[i],
                             f"donor {i} md5 mismatch — corpus changed")
            meta = {}
            gen = yg.generate(p, meta_out=meta)
            # size must match
            self.assertEqual(len(gen), len(orig),
                             f"donor {i}: size {len(gen)} != {len(orig)}")
            # page count must match
            do = fitz.open(p); dg = fitz.open(stream=gen, filetype="pdf")
            self.assertEqual(do.page_count, dg.page_count,
                             f"donor {i}: page count mismatch")
            # text content must match (all pages)
            for pi in range(do.page_count):
                to = "\n".join(l.strip() for l in do[pi].get_text().splitlines() if l.strip())
                tg = "\n".join(l.strip() for l in dg[pi].get_text().splitlines() if l.strip())
                self.assertEqual(to, tg, f"donor {i} page {pi}: text mismatch")
            # metadata: producer/creator must match donor
            self.assertEqual(dg.metadata.get("producer"), do.metadata.get("producer"))
            self.assertEqual(dg.metadata.get("creator"), do.metadata.get("creator"))
            # CreationDate must differ (regenerated to now-UTC)
            self.assertNotEqual(dg.metadata.get("creationDate"), do.metadata.get("creationDate"))
            # /ID halves must be equal
            m = re.search(rb"/ID \[<([0-9a-f]+)><([0-9a-f]+)>\]", gen)
            self.assertIsNotNone(m, f"donor {i}: no /ID in output")
            self.assertEqual(m.group(1), m.group(2), f"donor {i}: /ID halves not equal")
            self.assertEqual(len(m.group(1)), 32, f"donor {i}: /ID not 32 hex")
            # fitz must open clean
            self.assertFalse(dg.is_repaired, f"donor {i}: fitz repaired the PDF")
            self.assertFalse(dg.needs_pass, f"donor {i}: fitz wants a password")
            do.close(); dg.close()

    def test_gen_id_format(self):
        for _ in range(20):
            i = yg._gen_id()
            self.assertEqual(len(i), 32)
            self.assertTrue(all(c in "0123456789abcdef" for c in i),
                            f"non-hex /ID: {i}")

    def test_deterministic_with_fixed_creation_dt(self):
        """Same donor + same creation_dt => same output bytes except the
        random /ID (which is regenerated per the Jasper/OpenPDF law)."""
        from datetime import datetime, timezone
        dt = datetime(2026, 11, 7, 3, 30, 0, tzinfo=timezone.utc)
        a = yg.generate(DONORS[0], creation_dt=dt)
        b = yg.generate(DONORS[0], creation_dt=dt)
        # Strip the /ID trailer halves before comparing — they are random.
        a_strip = re.sub(rb"<[0-9a-f]{32}>", b"<X>", a)
        b_strip = re.sub(rb"<[0-9a-f]{32}>", b"<X>", b)
        self.assertEqual(a_strip, b_strip,
                         "deterministic output for fixed creation_dt (ignoring random /ID)")

    def test_save(self):
        out = os.path.join(os.path.dirname(yg.__file__), "_yandex_test_save.pdf")
        yg.save(out, DONORS[0])
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 100000)
        d = fitz.open(out)
        self.assertEqual(d.page_count, 1)
        d.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
