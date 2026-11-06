#!/usr/bin/env python3
"""Regression checks for the Rocket receipt patcher.

Run with: python3 test_rocket.py [donor_dir]
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile

import patch_rocket
import rocket_pdf
import rocket_receipt

DONOR_DIR = "/Users/aleksandrzerebatav/Downloads"


def donor_paths(directory: str) -> list:
    """Genuine donors: banks/rocket/donors/ copies, or Downloads originals.

    The current corpus is ``receipt_rocket 18…37.pdf`` (Downloads) copied
    to ``donors/receipt_rocket_18…37.pdf``; older names are still tried.
    Listing ~/Downloads may be denied, so names are constructed, and a
    plain ``glob`` covers the in-repo copies.
    """
    candidates = []
    candidates.extend(sorted(glob.glob(os.path.join(directory, "receipt_rocket_*.pdf"))))
    candidates.extend(sorted(glob.glob(os.path.join(directory, "donors", "receipt_rocket_*.pdf"))))
    names = ["receipt_rocket.pdf"] + [
        f"receipt_rocket {i}.pdf" for i in range(2, 65)
    ]
    candidates.extend(os.path.join(directory, n) for n in names)
    seen = []
    for path in candidates:
        if os.path.exists(path) and path not in seen:
            seen.append(path)
    return seen


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    return condition


def main() -> int:
    directory = sys.argv[1] if len(sys.argv) > 1 else DONOR_DIR
    paths = donor_paths(directory)
    if not paths:
        print(f"no donor receipts found in {directory}", file=sys.stderr)
        return 1

    failures = 0

    print(f"Raw round-trip ({len(paths)} files)")
    for path in paths:
        source = open(path, "rb").read()
        pdf = rocket_pdf.PdfFile(source)
        # rebuild() may differ by \n before "stream" keyword (iText original format
        # uses >>stream, our serialiser uses >>\nstream for incremental updates).
        # Verify via incremental_save with no changes instead — must be verbatim.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
            tmp = tf.name
        try:
            pdf.incremental_save(tmp)
            saved = open(tmp, "rb").read()
        finally:
            os.unlink(tmp)
        if not check(os.path.basename(path), saved == source, "bytes differ"):
            failures += 1

    print("\nRound-trip through the receipt model")
    cache = patch_rocket.harvest(paths, verbose=False)
    for path in paths:
        source = open(path, "rb").read()
        receipt = rocket_receipt.Receipt(path, cache)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            receipt.save(handle.name)
            rebuilt = open(handle.name, "rb").read()
        os.unlink(handle.name)
        if not check(os.path.basename(path), rebuilt == source, "bytes differ"):
            failures += 1

    print("\nField parsing")
    for path in paths:
        receipt = rocket_receipt.Receipt(path, cache)
        fields = receipt.fields
        expected = {"date", "amount", "account", "status", "doc_number", "bank_from"}
        missing = expected - set(fields)
        if not check(
            f"{os.path.basename(path)} [{receipt.format}] {len(fields)} fields",
            not missing,
            f"missing {missing}",
        ):
            failures += 1
        if not check(
            f"{os.path.basename(path)} status is УСПЕШНО",
            fields.get("status") == "УСПЕШНО",
            repr(fields.get("status")),
        ):
            failures += 1

    print("\nPatch every writable field, then read it back")
    values = {
        "date": "06.10.2026 21:05 ПО МСК",
        "amount": "1 234 567 ₽",
        "account": "40817 81045 02313 09876",
        "doc_number": "M70123456789",
        "bank_to": "ПАО СБЕРБАНК",
        "status": "УСПЕШНО",
    }
    for path in paths:
        receipt = rocket_receipt.Receipt(path, cache)
        wanted = {k: v for k, v in values.items() if k in receipt.fields}
        for name, value in wanted.items():
            receipt.set(name, value)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            receipt.save(handle.name, mode="natural")
            target = handle.name

        reread = rocket_receipt.Receipt(target).fields
        mismatched = {k: (v, reread.get(k)) for k, v in wanted.items() if reread.get(k) != v}
        ok = check(
            f"{os.path.basename(path)} {len(wanted)} fields",
            not mismatched,
            str(mismatched),
        )
        failures += 0 if ok else 1

        text = subprocess.run(
            ["pdftotext", target, "-"], capture_output=True, text=True
        ).stdout
        extracted = all(v in text.replace("\n", " ") for v in wanted.values())
        if not check(f"{os.path.basename(path)} text layer", extracted):
            failures += 1

        if _has_qpdf():
            result = subprocess.run(
                ["qpdf", "--check", target], capture_output=True, text=True
            )
            clean = "No syntax or stream encoding errors" in result.stdout
            if not check(f"{os.path.basename(path)} qpdf", clean, result.stdout[-200:]):
                failures += 1

        raw = open(target, "rb").read()
        # Checker-facing files are single-pass: one xref, no incremental
        # tail (2 xref / 2 %%EOF reads as an after-the-fact edit).
        if not check(
            f"{os.path.basename(path)} single-pass format",
            raw.count(b"startxref") == 1 and raw.count(b"%%EOF") == 1
            and b"/XRef" not in raw,
            f"{len(raw)} bytes",
        ):
            failures += 1
        os.unlink(target)

    print("\nSame-size (in-place) save")
    orig_bytes = open(paths[0], "rb").read()
    orig_size = len(orig_bytes)
    samesize_cases = {
        "account": "40817 81045 02313 09876",
        "amount": "1 234 567 ₽",
        "date": "06.10.2026 21:05 ПО МСК",
    }
    r = rocket_receipt.Receipt(paths[0], cache)
    for name, value in samesize_cases.items():
        if name in r.fields:
            r.set(name, value)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        r.save(handle.name, mode="inplace")
        same = handle.name
    same_size = os.path.getsize(same)
    if not check(
        f"{os.path.basename(paths[0])} in-place keeps original size",
        same_size == orig_size,
        f"{same_size} != {orig_size}",
    ):
        failures += 1
    if _has_qpdf():
        result = subprocess.run(["qpdf", "--check", same], capture_output=True, text=True)
        clean = "No syntax or stream encoding errors" in result.stdout
        if not check(f"{os.path.basename(paths[0])} in-place qpdf", clean,
                     result.stdout[-200:]):
            failures += 1
    os.unlink(same)

    print("\nRejections")
    receipt = rocket_receipt.Receipt(paths[0], cache)
    for label, call in (
        ("unknown field", lambda: receipt.set("nope", "x")),
        ("non-digit account tail", lambda: receipt.set_account_tail("12ab")),
        ("oversized account tail", lambda: receipt.set_account_tail("1" * 24)),
        ("glyph not in donor pool", lambda: receipt.set("bank_to", "ЮЖНЫЙ")),
    ):
        try:
            call()
            raised = False
        except (rocket_receipt.ReceiptError, KeyError):
            raised = True
        if not check(f"rejects {label}", raised, "no error raised"):
            failures += 1

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


def _has_qpdf() -> bool:
    return (
        subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
