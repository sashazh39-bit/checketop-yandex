#!/usr/bin/env python3
"""Regression checks for the Yandex donor-preserving PDF layer.

Run with:

    python3 banks/yandex/test_yandex.py [donor.pdf ...]

With no arguments the three donors named in ``banks/yandex/BANK.md``
are used (``yandex_fresh.pdf``, ``yandex_10000.pdf`` and
``yandex_test.pdf`` in the repository root).

The central check is deliberately strict: a donor is loaded with
``yandex_pdf`` and saved again, and the saved file must be *byte for
byte* identical to the original.  A single differing byte fails the
test.  Structural checks additionally verify that the low-level
parser really understands the file (objects, streams, Type0 fonts,
ToUnicode maps, content-stream text), so the round-trip cannot pass
by accident on an unparsed copy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yandex_pdf  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DONORS = [
    REPO_ROOT / "yandex_fresh.pdf",
    REPO_ROOT / "yandex_10000.pdf",
    REPO_ROOT / "yandex_test.pdf",
]

EXPECTED_FONTS = {
    "F1": "YSText-Regular",
    "F2": "YSText-Medium",
    "F3": "YSLogotype-Regular",
}


def donor_paths(argv: list) -> list:
    if not argv:
        return list(DEFAULT_DONORS)
    paths = []
    for item in argv:
        path = Path(item).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("yandex_*.pdf")))
        else:
            paths.append(path)
    return paths


def first_diff(a: bytes, b: bytes):
    """Offset of the first differing byte, or None when identical."""
    limit = min(len(a), len(b))
    for index in range(limit):
        if a[index] != b[index]:
            return index
    if len(a) != len(b):
        return limit
    return None


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    return condition


def main() -> int:
    paths = donor_paths(sys.argv[1:])
    failures = 0

    if not check(f"at least 2 donors given ({len(paths)} found)", len(paths) >= 2):
        failures += 1
    for path in paths:
        if not check(f"donor exists: {path}", path.exists()):
            failures += 1
    if failures:
        print(f"\n{failures} check(s) FAILED")
        return 1

    print(f"Raw round-trip ({len(paths)} donors): load -> save -> compare bytes")
    for path in paths:
        source = path.read_bytes()
        pdf = yandex_pdf.load(path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as handle:
            tmp = handle.name
        try:
            pdf.save(tmp)
            saved = Path(tmp).read_bytes()
        finally:
            os.unlink(tmp)

        diff = first_diff(saved, source)
        if not check(
            f"{path.name} byte-identical ({len(source)} bytes, sha256={pdf.sha256[:12]}...)",
            diff is None,
            f"first differing byte at offset {diff}",
        ):
            failures += 1

        # The comparison itself must notice even a single flipped byte:
        # otherwise the byte-identical check above would be meaningless.
        mutated = bytearray(saved)
        mutated[len(mutated) // 2] ^= 0xFF
        if not check(
            f"{path.name} one-byte difference is detected",
            first_diff(bytes(mutated), source) is not None,
        ):
            failures += 1

    print("\nStructure of the original Yandex export")
    for path in paths:
        name = path.name
        source = path.read_bytes()
        pdf = yandex_pdf.load(path)
        fonts = pdf.fonts()
        runs = pdf.text_runs()
        text = pdf.extracted_text()

        checks = [
            ("PDF 1.5 header", source.startswith(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")),
            ("28 objects (1..28)", sorted(pdf.objects) == list(range(1, 29))),
            (
                "single classic xref, no incremental update",
                source.count(b"startxref") == 1 and source.count(b"%%EOF") == 1,
            ),
            ("no xref/object streams", b"/ObjStm" not in source and b"/XRef" not in source),
            (
                "producer is the original JasperReports/OpenPDF",
                "OpenPDF" in pdf.info.get("Producer", "")
                and "JasperReports" in pdf.info.get("Creator", ""),
            ),
            (
                "page is 595x842 with a content stream",
                b"/MediaBox[0 0 595 842]" in pdf.page_object().dict_bytes
                and pdf.content_stream().has_stream,
            ),
            (
                "F1/F2/F3 are the Type0 YSText/YSLogotype fonts",
                {res: f.font_name for res, f in fonts.items()} == EXPECTED_FONTS
                and all(f.encoding == "Identity-H" for f in fonts.values()),
            ),
            (
                "every font has descendant, FontFile2, CIDSet and ToUnicode",
                all(
                    f.descendant_obj and f.font_file_obj and f.cidset_obj and f.tounicode
                    for f in fonts.values()
                ),
            ),
            (
                "embedded font programs are TrueType (FontFile2)",
                all(
                    pdf.obj(f.font_file_obj).decoded_stream().startswith(b"\x00\x01\x00\x00")
                    for f in fonts.values()
                    if f.font_file_obj
                ),
            ),
            (
                "all streams inflate cleanly",
                all(
                    pdf.obj(n).decoded_stream() is not None
                    for n in pdf.order
                    if pdf.obj(n).has_stream
                ),
            ),
            (
                "content text decodes through ToUnicode with no unknown CIDs",
                runs and all("\ufffd" not in run.text for run in runs),
            ),
            (
                "receipt labels are readable (Квитанция / Сумма операции)",
                "Квитанция" in text and "Сумма операции" in text and "Яндекс Банк" in text,
            ),
        ]
        for label, condition in checks:
            if not check(f"{name}: {label}", bool(condition)):
                failures += 1

        if _has_qpdf():
            result = subprocess.run(
                ["qpdf", "--check", str(path)], capture_output=True, text=True
            )
            clean = "No syntax or stream encoding errors" in result.stdout
            if not check(f"{name}: qpdf --check", clean, result.stdout[-200:]):
                failures += 1

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


def _has_qpdf() -> bool:
    return subprocess.run(["which", "qpdf"], capture_output=True).returncode == 0


if __name__ == "__main__":
    raise SystemExit(main())
