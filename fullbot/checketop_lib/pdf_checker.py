#!/usr/bin/env python3
"""Comprehensive PDF receipt validator for Alfa-Bank Oracle BI Publisher receipts.

Checks structural, stream-level, font, CMap, content, and logical properties
that a sophisticated external checker would verify.  Designed to catch every
known detection vector.

Usage:
    python pdf_checker.py <path_to_pdf> [--verbose] [--type sbp|transgran]
"""

from __future__ import annotations

import hashlib
import re
import struct
import sys
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_MSK = timedelta(hours=3)

# ── Known constants ──────────────────────────────────────────────────────────

EXPECTED_PRODUCER = b"Oracle BI Publisher 12.2.1.4.0"
EXPECTED_OBJECT_COUNT = 16
EXPECTED_XREF_HEADER = b"0 17"
EXPECTED_TJ_COUNT = 38
# Per receipt-type Tj counts: 3 header + 3 title + N_left×3 + 2 bottom + N_right×3
TJ_COUNT_BY_TYPE: dict[str, int] = {
    "sbp":       38,  # 5L + 5R
    "transgran": 35,  # 5L + 4R
    "alfa":      32,  # 4L + 4R
    "card":      32,  # 4L + 4R
    "alfa_card": 32,  # 4L + 4R
    "mobile":    32,  # 4L + 4R
}
ZLIB_LEVEL6_HEADER = bytes([0x78, 0x9C])
TTF_MAGIC = b"\x00\x01\x00\x00"

GENUINE_IMAGE_HASHES = {
    0: "30cead7b7731078d3482523f06b1decd75959506931ea5e4f33ffdd3ca9bb19a",
    1: "4b97c0d5ffaea51dfc0199c6a1c822328a0fc716603d1749c7ec936536b1f79a",
    2: "bfb9b68299256c95ad656e05d85d21b733286a9b24dc907a020587dab8249f9d",
}

TM_X_RANGE = (30.0, 570.0)
TM_Y_RANGE = (360.0, 800.0)

# ── Result collector ─────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self):
        self.passed: list[str] = []
        self.warnings: list[str] = []
        self.failed: list[str] = []

    def ok(self, msg: str):
        self.passed.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def fail(self, msg: str):
        self.failed.append(msg)

    @property
    def is_clean(self) -> bool:
        return len(self.failed) == 0

    def summary(self, verbose: bool = False) -> str:
        lines = []
        if verbose:
            for m in self.passed:
                lines.append(f"  [PASS] {m}")
        for m in self.warnings:
            lines.append(f"  [WARN] {m}")
        for m in self.failed:
            lines.append(f"  [FAIL] {m}")
        status = "CLEAN" if self.is_clean else "FAILED"
        header = (
            f"Result: {status}  "
            f"({len(self.passed)} passed, {len(self.warnings)} warnings, {len(self.failed)} failed)"
        )
        return header + "\n" + "\n".join(lines)


# ── Stream extraction ────────────────────────────────────────────────────────

_STREAM_RE = re.compile(
    rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", re.DOTALL
)


def _extract_streams(raw: bytes) -> list[dict]:
    """Extract all compressed streams with their metadata."""
    results = []
    for m in _STREAM_RE.finditer(raw):
        header = m.group(1) + m.group(3)
        slen = int(m.group(2))
        offset = m.end()
        data = raw[offset: offset + slen]
        entry: dict = {
            "header": header,
            "compressed": data,
            "length_declared": slen,
            "offset": offset,
            "decompressed": None,
        }
        if len(data) >= 2 and data[0] == 0x78:
            try:
                entry["decompressed"] = zlib.decompress(data)
            except zlib.error:
                pass
        results.append(entry)
    return results


# ── Individual checks ────────────────────────────────────────────────────────

def check_header(raw: bytes, r: CheckResult):
    if raw.startswith(b"%PDF-1.6"):
        r.ok("PDF header is 1.6")
    else:
        r.fail(f"PDF header is not 1.6: {raw[:20]}")


def check_eof(raw: bytes, r: CheckResult):
    tail = raw[-10:]
    if tail.rstrip().endswith(b"%%EOF"):
        r.ok("%%EOF present at end")
    else:
        r.fail(f"Missing %%EOF at end: {tail!r}")
    if raw.endswith(b"%%EOF\r\n"):
        r.ok("%%EOF followed by CRLF")
    elif raw.endswith(b"%%EOF\n"):
        r.warn("%%EOF followed by LF only (expected CRLF)")
    else:
        r.warn(f"Unexpected bytes after %%EOF: {raw[-6:]!r}")


def check_object_count(raw: bytes, r: CheckResult):
    objs = re.findall(rb"\d+ \d+ obj", raw)
    if len(objs) == EXPECTED_OBJECT_COUNT:
        r.ok(f"Object count = {EXPECTED_OBJECT_COUNT}")
    else:
        r.fail(f"Object count = {len(objs)}, expected {EXPECTED_OBJECT_COUNT}")


def check_xref(raw: bytes, r: CheckResult):
    xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n", raw)
    if not xref_m:
        # Oracle BI Publisher often uses a compact xref (just "xref\r\nNNNNN\r\n%%EOF")
        compact_m = re.search(rb"xref\r?\n(\d+)\r?\n%%EOF", raw)
        if compact_m:
            r.ok("Compact xref format (Oracle BI Publisher style)")
        else:
            r.fail("No xref table found")
        return
    header = xref_m.group(1) + b" " + xref_m.group(2)
    if header == EXPECTED_XREF_HEADER:
        r.ok(f"xref header: {header.decode()}")
    else:
        r.warn(f"xref header: {header.decode()}, expected {EXPECTED_XREF_HEADER.decode()}")


def check_startxref(raw: bytes, r: CheckResult):
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n%%EOF", raw)
    if sxref_m:
        r.ok("startxref present before %%EOF")
    else:
        r.warn("startxref not found in expected position")


def check_producer(raw: bytes, r: CheckResult):
    prod_m = re.search(rb"/Producer\s*\(([^)]+)\)", raw)
    if not prod_m:
        r.fail("No /Producer in metadata")
        return
    producer = prod_m.group(1)
    if producer == EXPECTED_PRODUCER:
        r.ok(f"Producer: {producer.decode()}")
    else:
        r.fail(f"Producer mismatch: {producer.decode()}")


def check_info_minimal(raw: bytes, r: CheckResult):
    """Genuine Oracle BI Publisher PDFs have /Info with only /Producer."""
    info_m = re.search(
        rb"(\d+)\s+0\s+obj\r?\n<<\s*/Producer\s*\([^)]+\)\s*>>\s*\r?\nendobj",
        raw,
    )
    if info_m:
        r.ok("/Info contains only /Producer (Oracle style)")
    else:
        extra_keys = []
        for key in [b"/Author", b"/Title", b"/Subject", b"/Creator", b"/ModDate", b"/CreationDate"]:
            # Only count if it's inside the Info dict object
            if re.search(rb"obj\r?\n<<[^>]*" + re.escape(key), raw):
                extra_keys.append(key.decode())
        if extra_keys:
            r.warn(f"/Info has extra keys: {', '.join(extra_keys)}")
        else:
            r.ok("/Info metadata appears minimal")


def check_document_id(raw: bytes, r: CheckResult):
    id_m = re.search(rb"/ID\s*\[<([0-9a-fA-F]+)><([0-9a-fA-F]+)>\]", raw)
    if not id_m:
        r.fail("No /ID array found")
        return
    id0 = id_m.group(1).decode()
    id1 = id_m.group(2).decode()

    if id0 == id1:
        r.ok("ID[0] == ID[1]")
    else:
        r.fail(f"ID[0] != ID[1]: {id0} vs {id1}")

    if id0 == id0.lower():
        r.ok("ID hex is lowercase")
    else:
        r.fail(f"ID hex contains uppercase: {id0}")

    if len(id0) == 32:
        r.ok("ID length = 32 hex chars (16 bytes)")
    else:
        r.fail(f"ID length = {len(id0)}, expected 32")


def check_streams_level6(streams: list[dict], r: CheckResult):
    """Verify all compressed streams are exactly recompressible at zlib level 6."""
    for i, s in enumerate(streams):
        data = s["compressed"]
        if len(data) < 2 or data[0] != 0x78:
            continue
        if data[:2] != ZLIB_LEVEL6_HEADER:
            r.fail(
                f"Stream[{i}] zlib header = {data[:2].hex()}, "
                f"expected {ZLIB_LEVEL6_HEADER.hex()} (level 6)"
            )
        dec = s.get("decompressed")
        if dec is None:
            r.fail(f"Stream[{i}] failed to decompress")
            continue
        recompressed = zlib.compress(dec, 6)
        if recompressed == data:
            r.ok(f"Stream[{i}] exactly recompressible at level 6")
        else:
            r.fail(
                f"Stream[{i}] recompress mismatch "
                f"(orig {len(data)} vs recomp {len(recompressed)})"
            )


def check_stream_count(streams: list[dict], r: CheckResult):
    if len(streams) == 6:
        r.ok("Stream count = 6")
    else:
        r.fail(f"Stream count = {len(streams)}, expected 6")


def check_image_hashes(streams: list[dict], r: CheckResult):
    for idx, expected_hash in GENUINE_IMAGE_HASHES.items():
        if idx >= len(streams):
            r.fail(f"Stream[{idx}] (image) missing")
            continue
        dec = streams[idx].get("decompressed")
        if dec is None:
            r.fail(f"Stream[{idx}] (image) could not decompress")
            continue
        actual = hashlib.sha256(dec).hexdigest()
        if actual == expected_hash:
            r.ok(f"Stream[{idx}] image hash matches")
        else:
            r.fail(
                f"Stream[{idx}] image hash mismatch: "
                f"{actual[:16]}... vs {expected_hash[:16]}..."
            )


def check_font(streams: list[dict], raw: bytes, r: CheckResult):
    if len(streams) < 5:
        r.fail("Not enough streams for font check")
        return
    font_dec = streams[4].get("decompressed")
    if font_dec is None:
        r.fail("Font stream could not decompress")
        return

    if font_dec[:4] == TTF_MAGIC:
        r.ok("Font stream is valid TTF")
    else:
        r.fail(f"Font stream invalid magic: {font_dec[:4].hex()}")

    # Check /BaseFont tag
    bf_m = re.search(rb"/BaseFont\s*/([A-Z]{6}\+Tahoma)", raw)
    if bf_m:
        tag = bf_m.group(1).decode()
        prefix = tag.split("+")[0]
        if len(prefix) == 6 and prefix.isalpha() and prefix.isupper():
            r.ok(f"BaseFont tag valid: {tag}")
        else:
            r.fail(f"BaseFont prefix invalid: {tag}")
    else:
        bf_any = re.search(rb"/BaseFont\s*/(\S+)", raw)
        if bf_any:
            r.fail(f"BaseFont not Tahoma subset: {bf_any.group(1).decode()}")
        else:
            r.fail("No /BaseFont found")

    # Check /Length1 matches decompressed font size
    l1_m = re.search(rb"/Length1\s+(\d+)", raw)
    if l1_m:
        length1 = int(l1_m.group(1))
        if length1 == len(font_dec):
            r.ok(f"/Length1 = {length1} matches font size")
        else:
            r.fail(f"/Length1 = {length1}, font size = {len(font_dec)}")
    else:
        r.fail("No /Length1 found")


def check_cmap(streams: list[dict], r: CheckResult) -> Optional[dict]:
    """Validate CMap structure and return cid->unicode mapping."""
    if len(streams) < 6:
        r.fail("Not enough streams for CMap check")
        return None
    cmap_dec = streams[5].get("decompressed")
    if cmap_dec is None:
        r.fail("CMap stream could not decompress")
        return None

    cmap_text = cmap_dec.decode("latin-1")

    # Oracle registry
    if "Registry (Oracle)" in cmap_text and "Ordering(UCS)" in cmap_text:
        r.ok("CMap has Oracle Registry/Ordering")
    elif "Registry (Oracle)" in cmap_text:
        r.ok("CMap has Oracle Registry")
    else:
        r.fail("CMap missing Oracle Registry")

    # beginbfchar (not bfrange)
    if "beginbfchar" in cmap_text:
        r.ok("CMap uses beginbfchar")
    else:
        r.fail("CMap missing beginbfchar")
    if "beginbfrange" in cmap_text:
        r.warn("CMap contains bfrange (Oracle uses only bfchar)")

    # Parse entries
    bfchar_count_m = re.search(r"(\d+)\s+beginbfchar", cmap_text)
    if not bfchar_count_m:
        r.fail("Cannot parse bfchar count")
        return None
    declared_count = int(bfchar_count_m.group(1))

    # Only count entries inside beginbfchar...endbfchar block
    block_m = re.search(
        r"beginbfchar\r?\n(.*?)endbfchar", cmap_text, re.DOTALL
    )
    if block_m:
        entries = re.findall(
            r"<([0-9A-Fa-f]{4})>\s+<([0-9A-Fa-f]{4})>", block_m.group(1)
        )
    else:
        entries = []

    actual_count = len(entries)

    if actual_count == declared_count:
        r.ok(f"CMap entry count = {declared_count} (matches header)")
    else:
        r.fail(
            f"CMap declares {declared_count} entries but has {actual_count}"
        )

    cmap = {}
    for cid_hex, uni_hex in entries:
        cid = int(cid_hex, 16)
        uni = int(uni_hex, 16)
        cmap[cid] = uni

    return cmap


def check_content(
    streams: list[dict], cmap: Optional[dict], r: CheckResult, receipt_type: str = "sbp"
):
    if len(streams) < 4:
        r.fail("Not enough streams for content check")
        return
    content_dec = streams[3].get("decompressed")
    if content_dec is None:
        r.fail("Content stream could not decompress")
        return

    # Tj count
    tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
    expected_count = TJ_COUNT_BY_TYPE.get(receipt_type, EXPECTED_TJ_COUNT)
    if len(tjs) == expected_count:
        r.ok(f"Tj count = {expected_count}")
    else:
        r.fail(f"Tj count = {len(tjs)}, expected {expected_count}")

    # Tm coordinate ranges
    tms = re.findall(rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm", content_dec)
    if tms:
        x_vals = [float(x) for x, y in tms]
        y_vals = [float(y) for x, y in tms]
        x_ok = TM_X_RANGE[0] <= min(x_vals) and max(x_vals) <= TM_X_RANGE[1]
        y_ok = TM_Y_RANGE[0] <= min(y_vals) and max(y_vals) <= TM_Y_RANGE[1]
        if x_ok and y_ok:
            r.ok(
                f"Tm coordinates in range "
                f"(X: {min(x_vals):.1f}-{max(x_vals):.1f}, "
                f"Y: {min(y_vals):.1f}-{max(y_vals):.1f})"
            )
        else:
            r.fail(
                f"Tm coordinates out of range "
                f"(X: {min(x_vals):.1f}-{max(x_vals):.1f}, "
                f"Y: {min(y_vals):.1f}-{max(y_vals):.1f})"
            )

    # Font reference
    tf_m = re.search(rb"/(\w+)\s+[\d.]+\s+Tf", content_dec)
    if tf_m:
        r.ok(f"Font reference in content: /{tf_m.group(1).decode()}")
    else:
        r.warn("No Tf command found in content")

    # CMap unused entry check
    if cmap and tjs:
        used_cids: set[int] = set()
        for tj_hex in tjs:
            hex_str = tj_hex.decode()
            for i in range(0, len(hex_str), 4):
                cid = int(hex_str[i: i + 4], 16)
                used_cids.add(cid)

        all_cmap_cids = set(cmap.keys())
        unused = all_cmap_cids - used_cids - {0}  # CID 0 (.notdef) excluded
        if len(unused) == 0:
            r.ok("CMap: 0 unused entries (all CIDs used in content)")
        else:
            unused_unis = [f"U+{cmap[c]:04X}" for c in sorted(unused)]
            r.fail(
                f"CMap: {len(unused)} unused entries: {', '.join(unused_unis[:10])}"
                + ("..." if len(unused_unis) > 10 else "")
            )


def check_logic_sbp(
    streams: list[dict], cmap: Optional[dict], r: CheckResult
):
    """SBP-specific logical checks on decoded text fields.

    Genuine Oracle BI Publisher SBP receipts always have exactly 38 Tj
    commands in a fixed order.  The 4 variable data fields at positions
    8 (amount), 14 (datetime), 17 (operation_id), 20 (recipient) always
    end with NBSP (U+00A0).  The fields at positions 25 (phone), 31
    (account), 34 (sbp_id) must NOT end with NBSP.
    """
    if not cmap or len(streams) < 4:
        return
    content_dec = streams[3].get("decompressed")
    if content_dec is None:
        return

    cid_to_chr = {cid: chr(uni) for cid, uni in cmap.items()}
    tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
    decoded: list[str] = []
    for tj_hex in tjs:
        hex_str = tj_hex.decode()
        chars = []
        for i in range(0, len(hex_str), 4):
            cid = int(hex_str[i: i + 4], 16)
            chars.append(cid_to_chr.get(cid, "?"))
        decoded.append("".join(chars))

    def _raw(idx: int) -> str:
        return decoded[idx] if idx < len(decoded) else ""

    def _clean(idx: int) -> str:
        return _raw(idx).replace("\xa0", " ").strip()

    # ── Amount (Tj[8]) ─────────────────────────────────────────────────────
    amount_raw = _raw(8)
    amount_clean = _clean(8)
    if "RUR" in amount_clean or "RUB" in amount_clean:
        r.ok(f"Amount field found: {amount_clean}")
    else:
        # fall back to scan
        for line in decoded:
            stripped = line.replace("\xa0", " ").strip()
            if "RUR" in stripped or "RUB" in stripped:
                amount_raw = line
                amount_clean = stripped
                r.warn(f"Amount not at Tj[8]; found at other position: {stripped}")
                break
        else:
            r.warn("No RUR/RUB amount line found in decoded text")

    if amount_raw.endswith("\xa0"):
        r.ok("Amount ends with NBSP (genuine pattern)")
    else:
        r.fail(
            f"Amount does NOT end with NBSP: {repr(amount_raw[-5:])}"
            " -- genuine Oracle PDFs always have trailing NBSP after amount"
        )

    # ── Date/time (Tj[14]) ─────────────────────────────────────────────────
    dt_raw = _raw(14)
    dt_clean = _clean(14)
    if re.search(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}", dt_clean):
        r.ok(f"Date/time format valid: {dt_clean}")
        if dt_raw.endswith("\xa0"):
            r.ok("Date/time ends with NBSP (genuine pattern)")
        else:
            r.fail(
                f"Date/time does NOT end with NBSP: {repr(dt_raw[-5:])}"
            )
    else:
        r.warn(f"Date/time at Tj[14] not in expected format: {dt_clean!r}")

    # ── Operation ID (Tj[17]) ──────────────────────────────────────────────
    op_raw = _raw(17)
    op_clean = _clean(17)
    if re.match(r"C16\d+$", op_clean):
        r.ok(f"Operation ID starts with C16: {op_clean}")
        if op_raw.endswith("\xa0"):
            r.ok("Operation ID ends with NBSP (genuine pattern)")
        else:
            r.fail(
                f"Operation ID does NOT end with NBSP: {repr(op_raw[-5:])}"
            )
    else:
        r.warn(f"Operation ID at Tj[17] unexpected: {op_clean!r}")

    # ── Recipient (Tj[20]) ─────────────────────────────────────────────────
    recip_raw = _raw(20)
    recip_clean = _clean(20)
    if recip_clean:
        r.ok(f"Recipient field: {recip_clean}")
        if recip_raw.endswith("\xa0"):
            r.ok("Recipient ends with NBSP (genuine pattern)")
        else:
            r.fail(
                f"Recipient does NOT end with NBSP: {repr(recip_raw[-5:])}"
            )

    # ── Phone (Tj[25]) -- must NOT end with NBSP ───────────────────────────
    phone_raw = _raw(25)
    phone_clean = _clean(25)
    if re.search(r"\+7\s*\(\d{3}\)\s*\d{3}-\d{2}-\d{2}", phone_clean):
        r.ok(f"Phone format valid: {phone_clean}")
    else:
        r.warn(f"Phone at Tj[25] unexpected format: {phone_clean!r}")
    if not phone_raw.endswith("\xa0"):
        r.ok("Phone correctly has no trailing NBSP")
    else:
        r.fail("Phone ends with NBSP (should not for genuine PDFs)")

    # ── Account (Tj[31]) -- must NOT end with NBSP ─────────────────────────
    acc_raw = _raw(31)
    acc_clean = _clean(31)
    if re.match(r"\d{20}$", acc_clean):
        r.ok(f"Account format valid: {acc_clean}")
    else:
        r.warn(f"Account at Tj[31] unexpected: {acc_clean!r}")
    if not acc_raw.endswith("\xa0"):
        r.ok("Account correctly has no trailing NBSP")
    else:
        r.fail("Account ends with NBSP (should not for genuine PDFs)")

    # ── SBP ID (Tj[34]) -- must NOT end with NBSP ─────────────────────────
    sbp_raw = _raw(34)
    sbp_clean = _clean(34)
    # p23 (positions 2-3) is a rolling era counter, not a fixed "60".
    # Accept any two digits for p23.
    if re.match(r"[AB]\d{2}[0-9A-Za-z]{29}$", sbp_clean):
        r.ok(f"SBP ID valid format: {sbp_clean}")
    else:
        r.warn(f"SBP ID at Tj[34] unexpected: {sbp_clean!r}")
    if not sbp_raw.endswith("\xa0"):
        r.ok("SBP ID correctly has no trailing NBSP")
    else:
        r.fail("SBP ID ends with NBSP (should not for genuine PDFs)")

    # ── SBP ID p23 era check ──────────────────────────────────────────────
    # p23 = sbp_id[1:3].  It is a rolling era counter that Alfa increments
    # periodically.  Known transitions:
    #   p23=60 → valid up to Apr 9, 2026
    #   p23=61 → valid from Apr 10, 2026
    # New transitions are added to _P23_ERA_CUTOFFS_CHK as they are observed.
    # The check is numeric: p23 must be >= the minimum required for op_date.
    _P23_ERA_CUTOFFS_CHK: list[tuple] = [
        # (start_date, min_p23_int)
        (datetime(2026, 4, 10).date(), 61),
        # Add future cutoffs here when discovered
    ]

    if len(sbp_clean) >= 3:
        p23 = sbp_clean[1:3]
        dt_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", _clean(14))
        if dt_m:
            try:
                from datetime import date as _date
                op_date = _date(int(dt_m.group(3)), int(dt_m.group(2)), int(dt_m.group(1)))
                min_p23 = 60
                for cutoff_date, min_val in _P23_ERA_CUTOFFS_CHK:
                    if op_date >= cutoff_date:
                        min_p23 = max(min_p23, min_val)
                try:
                    p23_int = int(p23)
                    if p23_int < min_p23:
                        r.fail(
                            f"SBP ID p23='{p23}' is stale for operation date {op_date} "
                            f"(minimum required: '{min_p23:02d}')"
                        )
                    else:
                        r.ok(f"SBP ID p23='{p23}' is valid for operation date {op_date}")
                except ValueError:
                    r.warn(f"SBP ID p23='{p23}' is not numeric — cannot validate era")
            except (ValueError, ImportError):
                pass


def check_logic_transgran(
    streams: list[dict], cmap: Optional[dict], r: CheckResult
):
    """Transgran-specific logical checks."""
    if not cmap or len(streams) < 4:
        return
    content_dec = streams[3].get("decompressed")
    if content_dec is None:
        return

    cid_to_chr = {cid: chr(uni) for cid, uni in cmap.items()}
    tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
    decoded: list[str] = []
    for tj_hex in tjs:
        hex_str = tj_hex.decode()
        chars = []
        for i in range(0, len(hex_str), 4):
            cid = int(hex_str[i: i + 4], 16)
            chars.append(cid_to_chr.get(cid, "?"))
        decoded.append("".join(chars))

    # Operation ID (C82...)
    for line in decoded:
        stripped = line.replace("\xa0", " ").strip()
        if re.match(r"C82\d+", stripped):
            r.ok(f"Transgran operation ID: {stripped}")
            break
    else:
        r.warn("No C82 operation ID found (transgran)")

    # Currency
    found_tjs = False
    for line in decoded:
        stripped = line.replace("\xa0", " ").strip()
        if "TJS" in stripped:
            found_tjs = True
            r.ok(f"TJS currency found: {stripped}")
            break
    if not found_tjs:
        r.warn("No TJS currency found (transgran)")


def check_logic_generic(
    streams: list[dict], cmap: Optional[dict], r: CheckResult, receipt_type: str
):
    """Generic logic checks for alfa/card/mobile receipt types (4L+4R layout).

    Validates the amount field at Tj[8] and checks that the title matches the
    expected type keyword.  Field-position checks beyond Tj[8] are skipped
    because each type has a different field layout.
    """
    if not cmap or len(streams) < 4:
        return
    content_dec = streams[3].get("decompressed")
    if content_dec is None:
        return

    cid_to_chr = {cid: chr(uni) for cid, uni in cmap.items()}
    tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
    decoded: list[str] = []
    for tj_hex in tjs:
        hex_str = tj_hex.decode()
        chars = []
        for i in range(0, len(hex_str), 4):
            cid = int(hex_str[i: i + 4], 16)
            chars.append(cid_to_chr.get(cid, "?"))
        decoded.append("".join(chars))

    def _raw(idx: int) -> str:
        return decoded[idx] if idx < len(decoded) else ""

    def _clean(idx: int) -> str:
        return _raw(idx).replace("\xa0", " ").strip()

    # Amount at Tj[8] (same position for all 4L+ receipt types)
    amount_raw = _raw(8)
    amount_clean = _clean(8)
    if "RUR" in amount_clean or "RUB" in amount_clean:
        r.ok(f"Amount field found: {amount_clean}")
    else:
        for line in decoded:
            stripped = line.replace("\xa0", " ").strip()
            if "RUR" in stripped or "RUB" in stripped:
                amount_raw = line
                r.warn(f"Amount not at Tj[8]; found elsewhere: {stripped}")
                break
        else:
            r.warn("No RUR/RUB amount line found in decoded text")

    if amount_raw.endswith("\xa0"):
        r.ok(f"Amount ({receipt_type}) ends with NBSP (genuine pattern)")
    else:
        r.fail(
            f"Amount does NOT end with NBSP: {repr(amount_raw[-5:])}"
            " -- genuine Oracle PDFs always have trailing NBSP after amount"
        )

    # Title at Tj[4] — check it's non-empty
    title_clean = _clean(4)
    if title_clean:
        r.ok(f"Title ({receipt_type}) at Tj[4]: {title_clean[:40]}")
    else:
        r.warn(f"Title at Tj[4] is empty for {receipt_type}")


def check_filename_formed(
    pdf_path: Path,
    streams: list[dict],
    cmap: Optional[dict],
    r: CheckResult,
):
    """Validate that the filename timestamp correlates with date_formed inside the PDF.

    Genuine Alfa-Bank receipts follow the pattern AM_{13-digit-ms-epoch}.pdf where
    the epoch is consistently 225-301 seconds BEFORE the 'Сформирована' time shown
    inside the PDF.  We accept 180-360 seconds as a generous margin.
    """
    fn = pdf_path.name
    fn_m = re.match(r"AM_(\d{13})\.pdf$", fn)
    if not fn_m:
        r.warn(f"Filename does not match AM_{{13-digit}}.pdf pattern: {fn!r}")
        return

    fn_ts_ms = int(fn_m.group(1))
    fn_ts_sec = fn_ts_ms / 1000
    r.ok(f"Filename matches AM_{{13-digit}}.pdf pattern: {fn}")

    # Extract date_formed from Tj[2]
    if not cmap or len(streams) < 4:
        r.warn("Cannot check filename timing: no CMap or content stream")
        return

    content_dec = streams[3].get("decompressed")
    if content_dec is None:
        return

    cid_to_chr = {cid: chr(uni) for cid, uni in cmap.items()}
    tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
    decoded = []
    for tj_hex in tjs:
        hex_str = tj_hex.decode()
        chars = []
        for i in range(0, len(hex_str), 4):
            cid = int(hex_str[i: i + 4], 16)
            chars.append(cid_to_chr.get(cid, "?"))
        decoded.append("".join(chars))

    if len(decoded) < 3:
        r.warn("Cannot check filename timing: fewer than 3 Tj entries")
        return

    formed_raw = decoded[2].replace("\xa0", " ").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", formed_raw)
    if not m:
        r.warn(f"date_formed at Tj[2] not parseable: {formed_raw!r}")
        return

    dd, mm, yyyy, hh, mi = (int(x) for x in m.groups())
    formed_msk = datetime(yyyy, mm, dd, hh, mi, 0, tzinfo=timezone.utc)
    formed_utc = formed_msk - _MSK
    formed_ts_sec = formed_utc.timestamp()

    # delta = formed_ts_sec - fn_ts_sec  (positive = filename is before formed)
    delta = formed_ts_sec - fn_ts_sec
    r.ok(f"Filename timestamp: {fn_ts_sec:.0f} | date_formed MSK: {formed_raw} | delta: {delta:.0f}s")

    if 180 <= delta <= 360:
        r.ok(f"Filename-to-formed delta {delta:.0f}s is in genuine range (180-360s)")
    elif delta < 0:
        r.fail(
            f"Filename is AFTER date_formed by {-delta:.0f}s — "
            "genuine files always have filename timestamp before date_formed"
        )
    else:
        r.fail(
            f"Filename-to-formed delta {delta:.0f}s is outside genuine range "
            f"(180-360s). Genuine receipts: 225-301s."
        )


# ── Main validator ───────────────────────────────────────────────────────────

def validate_pdf(
    pdf_path: str | Path,
    verbose: bool = False,
    receipt_type: str = "auto",
) -> CheckResult:
    """Run all checks on a PDF file.

    receipt_type: 'sbp', 'transgran', or 'auto' (detect from content).
    """
    pdf_path = Path(pdf_path)
    r = CheckResult()

    if not pdf_path.exists():
        r.fail(f"File not found: {pdf_path}")
        return r

    raw = pdf_path.read_bytes()
    r.ok(f"File size: {len(raw)} bytes")

    # Structural
    check_header(raw, r)
    check_eof(raw, r)
    check_object_count(raw, r)
    check_xref(raw, r)
    check_startxref(raw, r)

    # Metadata
    check_producer(raw, r)
    check_info_minimal(raw, r)
    check_document_id(raw, r)

    # Streams
    streams = _extract_streams(raw)
    check_stream_count(streams, r)
    check_streams_level6(streams, r)
    check_image_hashes(streams, r)

    # Font
    check_font(streams, raw, r)

    # CMap
    cmap = check_cmap(streams, r)

    # Auto-detect receipt type (before content check so correct Tj count is used)
    if receipt_type == "auto" and cmap and len(streams) >= 4:
        content_dec = streams[3].get("decompressed")
        if content_dec:
            cid_to_chr = {cid: chr(uni) for cid, uni in cmap.items()}
            tjs = re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content_dec)
            full_text = ""
            for tj_hex in tjs:
                hex_str = tj_hex.decode()
                for i in range(0, len(hex_str), 4):
                    cid = int(hex_str[i: i + 4], 16)
                    full_text += cid_to_chr.get(cid, "?")
            if "TJS" in full_text or "C82" in full_text or "рубеж" in full_text:
                receipt_type = "transgran"
            elif "карт" in full_text.lower() and "авторизации" in full_text.lower():
                receipt_type = "card"
            elif "мобильной" in full_text.lower() or "билайн" in full_text.lower() or "МТС" in full_text:
                receipt_type = "mobile"
            elif "клиенту" in full_text.lower() and "Альфа" in full_text:
                receipt_type = "alfa"
            else:
                receipt_type = "sbp"
        if receipt_type == "auto":
            receipt_type = "sbp"

    # Content
    check_content(streams, cmap, r, receipt_type)

    # Logic
    if receipt_type == "sbp":
        check_logic_sbp(streams, cmap, r)
    elif receipt_type == "transgran":
        check_logic_transgran(streams, cmap, r)
    elif receipt_type in ("alfa", "card", "mobile"):
        check_logic_generic(streams, cmap, r, receipt_type)

    # Filename timing correlation
    check_filename_formed(pdf_path, streams, cmap, r)

    return r


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate Alfa-Bank PDF receipt")
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--type", "-t", choices=["sbp", "transgran", "auto"], default="auto")
    args = parser.parse_args()

    result = validate_pdf(args.pdf, verbose=args.verbose, receipt_type=args.type)
    print(result.summary(verbose=args.verbose))
    sys.exit(0 if result.is_clean else 1)


if __name__ == "__main__":
    main()
