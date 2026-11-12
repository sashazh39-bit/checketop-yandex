#!/usr/bin/env python3
"""PDF structural validation for Oracle BI Publisher receipts.

Checks:
  1. xref offsets: every 'n' entry points to the correct byte offset.
  2. /Length entries: each compressed stream's declared length matches actual bytes.
  3. startxref: points to the actual xref table position.
  4. Text extraction: PyMuPDF can open and extract text without errors.
  5. Glyph coverage: all expected text strings are present in extracted text.
  6. Document /ID: both IDs are equal (Oracle BI Publisher standard).
  7. Producer: Oracle BI Publisher 12.2.1.4.0.

Usage:
    from pdf_validate import validate_pdf, ValidationResult

    result = validate_pdf(pdf_bytes, expected_texts=["Bobomurodov Kh.", "354 TJS"])
    if not result.ok:
        print(result.errors)
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"[ERROR] {e}")
        for w in self.warnings:
            lines.append(f"[WARN]  {w}")
        for i in self.info:
            lines.append(f"[OK]    {i}")
        return "\n".join(lines) if lines else "[OK] All checks passed."


def _check_xref(data: bytes) -> list[str]:
    """Verify that each 'n' xref entry offset points to an actual object."""
    errors: list[str] = []
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
    )
    if not xref_m:
        errors.append("xref table not found")
        return errors

    entries_raw = xref_m.group(3)
    base = int(xref_m.group(1))
    count = int(xref_m.group(2))

    entry_list = re.findall(rb"(\d{10})\s+(\d{5})\s+([nf])", entries_raw)
    if len(entry_list) != count:
        errors.append(f"xref entry count mismatch: declared {count}, found {len(entry_list)}")

    for i, (offset_b, gen_b, flag_b) in enumerate(entry_list):
        obj_num = base + i
        offset = int(offset_b)
        flag = flag_b.decode()
        if flag == "f":
            continue
        if offset == 0:
            errors.append(f"obj {obj_num}: offset is 0 for 'n' entry")
            continue
        if offset >= len(data):
            errors.append(f"obj {obj_num}: offset {offset} beyond file size {len(data)}")
            continue
        chunk = data[offset: offset + 20]
        expected = f"{obj_num} ".encode()
        if not chunk.startswith(expected):
            errors.append(
                f"obj {obj_num}: offset {offset} points to {chunk[:20]!r}, "
                f"expected '{obj_num} ...' obj header"
            )
    return errors


def _check_lengths(data: bytes) -> list[str]:
    """Verify each zlib-compressed stream's /Length matches actual compressed bytes."""
    errors: list[str] = []
    for m in re.finditer(
        rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL
    ):
        declared_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + declared_len > len(data):
            errors.append(
                f"stream at offset {stream_start}: declared length {declared_len} "
                f"exceeds file size"
            )
            continue
        raw = data[stream_start: stream_start + declared_len]
        try:
            zlib.decompress(raw)
        except zlib.error:
            # Not a zlib stream — could be image data; skip length check
            pass
        # Verify endstream marker follows declared length
        endstream = data[stream_start + declared_len: stream_start + declared_len + 20]
        if b"endstream" not in endstream and b"\nendstream" not in endstream:
            # Sometimes there's \r\n padding; search a small window
            window = data[stream_start + declared_len: stream_start + declared_len + 30]
            if b"endstream" not in window:
                errors.append(
                    f"stream at offset {stream_start}: 'endstream' not found after "
                    f"declared length {declared_len}"
                )
    return errors


def _check_startxref(data: bytes) -> list[str]:
    """Verify startxref points to the xref table."""
    errors: list[str] = []
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if not sxref_m:
        errors.append("startxref not found")
        return errors
    pos = int(sxref_m.group(1))
    if pos >= len(data):
        errors.append(f"startxref {pos} beyond file size {len(data)}")
        return errors
    chunk = data[pos: pos + 10]
    if not chunk.startswith(b"xref"):
        errors.append(
            f"startxref {pos} does not point to 'xref' (found {chunk[:10]!r})"
        )
    return errors


def _check_doc_id(data: bytes) -> tuple[list[str], list[str]]:
    """Check Document /ID: both entries should be equal (Oracle BI Publisher)."""
    errors: list[str] = []
    warnings: list[str] = []
    m = re.search(rb"/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]", data)
    if not m:
        warnings.append("/ID not found in PDF — may be missing trailer dict")
        return errors, warnings
    id1, id2 = m.group(1), m.group(2)
    if id1 != id2:
        errors.append(
            f"Document /ID mismatch: ID[0]={id1.decode()[:8]}... != "
            f"ID[1]={id2.decode()[:8]}... "
            "(Oracle BI Publisher always sets ID[0] == ID[1])"
        )
    return errors, warnings


def _check_text_extraction(data: bytes, expected_texts: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Extract text with PyMuPDF and verify expected strings are present."""
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    try:
        import fitz
    except ImportError:
        warnings.append("PyMuPDF not installed — skipping text extraction check")
        return errors, warnings, info

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        errors.append(f"PyMuPDF failed to open PDF: {e}")
        return errors, warnings, info

    try:
        if doc.page_count == 0:
            errors.append("PDF has 0 pages")
            return errors, warnings, info

        full_text = "".join(page.get_text() for page in doc)
        info.append(f"Text extraction OK ({len(full_text)} chars, {doc.page_count} page(s))")

        for expected in expected_texts:
            clean = expected.replace("\xa0", " ").strip()
            extracted_clean = full_text.replace("\xa0", " ")
            if clean and clean not in extracted_clean:
                errors.append(f"Expected text not found: {clean!r}")
            elif clean:
                info.append(f"Found: {clean!r}")
    finally:
        doc.close()

    return errors, warnings, info


def _check_producer(data: bytes) -> list[str]:
    """Verify producer is Oracle BI Publisher."""
    warnings: list[str] = []
    m = re.search(rb"/Producer\s*\(([^)]+)\)", data)
    if not m:
        warnings.append("Producer not found in PDF metadata")
    elif b"Oracle BI Publisher" not in m.group(1):
        warnings.append(
            f"Producer is not Oracle BI Publisher: {m.group(1).decode('latin1')!r}"
        )
    return warnings


def validate_pdf(
    pdf_bytes: bytes,
    expected_texts: Optional[list[str]] = None,
    *,
    check_xref: bool = True,
    check_lengths: bool = True,
    check_startxref: bool = True,
    check_id: bool = True,
    check_producer: bool = True,
    check_text: bool = True,
) -> ValidationResult:
    """Run all structural and content checks on a PDF.

    Parameters
    ----------
    pdf_bytes:       Raw PDF bytes to validate.
    expected_texts:  List of strings that must appear in the extracted text
                     (NBSP-normalised comparison).

    Returns
    -------
    ValidationResult with .ok, .errors, .warnings, .info.
    """
    all_errors: list[str] = []
    all_warnings: list[str] = []
    all_info: list[str] = []

    if check_xref:
        errs = _check_xref(pdf_bytes)
        all_errors.extend(errs)
        if not errs:
            all_info.append("xref offsets OK")

    if check_lengths:
        errs = _check_lengths(pdf_bytes)
        all_errors.extend(errs)
        if not errs:
            all_info.append("/Length values OK")

    if check_startxref:
        errs = _check_startxref(pdf_bytes)
        all_errors.extend(errs)
        if not errs:
            all_info.append("startxref OK")

    if check_id:
        errs, warns = _check_doc_id(pdf_bytes)
        all_errors.extend(errs)
        all_warnings.extend(warns)
        if not errs and not warns:
            all_info.append("Document /ID OK (ID[0] == ID[1])")

    if check_producer:
        warns = _check_producer(pdf_bytes)
        all_warnings.extend(warns)
        if not warns:
            all_info.append("Producer: Oracle BI Publisher OK")

    if check_text and expected_texts:
        errs, warns, info = _check_text_extraction(pdf_bytes, expected_texts)
        all_errors.extend(errs)
        all_warnings.extend(warns)
        all_info.extend(info)

    return ValidationResult(
        ok=len(all_errors) == 0,
        errors=all_errors,
        warnings=all_warnings,
        info=all_info,
    )


def validate_pdf_file(
    pdf_path: str | Path,
    expected_texts: Optional[list[str]] = None,
    **kwargs,
) -> ValidationResult:
    """Validate a PDF file on disk."""
    data = Path(pdf_path).read_bytes()
    return validate_pdf(data, expected_texts, **kwargs)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 pdf_validate.py receipt.pdf [expected_text ...]")
        sys.exit(1)

    path = Path(sys.argv[1])
    texts = sys.argv[2:]
    result = validate_pdf_file(path, texts or None)
    print(result)
    sys.exit(0 if result.ok else 1)
