#!/usr/bin/env python3
"""Low-level PDF primitives for MTS receipts, byte-compatible with the
genuine producer (OpenPDF 1.3.30.jaspersoft.2, see banks/mts/BANK.md).

Every helper here reproduces one exact behaviour of the Java original:

* ``fmt_double``    -- ``ByteBuffer.formatDouble`` (0.000015..32767 branch:
                       round half-up to 2 decimals, trim trailing zeros);
* ``escape_string`` -- ``PdfContentByte.escapeString`` (only \\r \\n \\t \\b \\f
                       become letter escapes, ``(`` ``)`` ``\\`` get a
                       backslash, every other byte is written raw);
* ``compress``      -- Java ``Deflater`` default level == ``zlib.compress(., 6)``;
* ``pdf_string``    -- literal ``(`` ... ``)`` string from raw bytes.

These were verified byte-for-byte against the genuine donors (BANK.md).
"""
from __future__ import annotations

import zlib

__all__ = ["fmt_double", "escape_string", "pdf_string", "compress", "mangle_name_key"]


def fmt_double(value: float) -> str:
    """iText ``ByteBuffer.formatDouble`` for the ranges used on the receipt.

    Java: values with |d| < 0.000015 print as ``0``; d < 1 rounds to 5
    decimals; 1 <= d <= 32767 gets ``d += 0.005`` and is truncated to an
    integer of hundredths, then printed with trailing zeros trimmed.
    The receipt only carries coordinates in the 20..730 range, so the
    middle branch is what matters (e.g. 189.604 -> ``189.6``,
    246.88 -> ``246.88``, 355.0 -> ``355``).
    """
    d = float(value)
    negative = d < 0
    if abs(d) < 0.000015:
        return "0"
    if negative:
        d = -d
    if d < 1.0:
        d += 0.000005
        if d >= 1:
            return "-1" if negative else "1"
        v = int(d * 100000)
        digits = f"{v:05d}".rstrip("0") or "0"
        return ("-" if negative else "") + "0." + digits
    elif d <= 32767:
        d += 0.005
        v = int(d * 100)
        whole, frac = divmod(v, 100)
        if frac == 0:
            return ("-" if negative else "") + str(whole)
        text = ("-" if negative else "") + f"{whole}.{frac:02d}"
        return text.rstrip("0")
    # outside receipt ranges; plain fallback (never hit by the template)
    text = repr(round(d, 6))
    return ("-" if negative else "") + text


_ESCAPES = {
    0x0D: b"\\r",
    0x0A: b"\\n",
    0x09: b"\\t",
    0x08: b"\\b",
    0x0C: b"\\f",
}


def escape_string(raw: bytes) -> bytes:
    """``PdfContentByte.escapeString`` -- body of a literal string.

    Only CR/LF/TAB/BS/FF are letter-escaped and ``( ) \\`` backslash-escaped;
    control bytes such as 0x11/0x1F (low bytes of Cyrillic capitals in the
    mangled /Names keys) are written RAW, exactly as OpenPDF does.
    """
    out = bytearray()
    for byte in raw:
        if byte in _ESCAPES:
            out += _ESCAPES[byte]
        elif byte in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
            out.append(byte)
        else:
            out.append(byte)
    return bytes(out)


def pdf_string(raw: bytes) -> bytes:
    """A literal PDF string ``(`` ... ``)`` from raw bytes."""
    return b"(" + escape_string(raw) + b")"


def compress(data: bytes) -> bytes:
    """Java ``Deflater`` default (level 6) == Python zlib level 6.

    Verified: recompressing every stream of the genuine donors with
    ``zlib.compress(data, 6)`` reproduces the stored bytes exactly
    (32/32 streams, BANK.md).
    """
    return zlib.compress(data, 6)


def mangle_name_key(text: str) -> bytes:
    """OpenPDF's broken non-ASCII string serialisation used for the
    /Names tree keys: every char is written as its low byte
    (``PdfEncodings.convertToBytes(text, null)`` = ``char & 0xFF``).

    This is why genuine receipts carry ``(0,00 \\xbd)`` for ``0,00 ₽``
    (U+20BD -> 0xBD) and control bytes for Cyrillic capitals
    (``Б`` U+0411 -> 0x11) in the name tree (BANK.md).
    """
    return bytes(ord(char) & 0xFF for char in text)
