#!/usr/bin/env python3
"""Alfa-Bank SBP receipt generator.

Generates a valid Alfa-Bank PDF receipt for СБП (Система Быстрых Платежей)
domestic transfers with arbitrary input data.

Fields supported:
    amount:           Transfer amount in RUB
    recipient:        Recipient name (Имя Отчество И.)
    phone:            Recipient phone +7 (XXX) XXX-XX-XX
    bank:             Recipient bank name
    operation_date:   DD.MM.YYYY or 'auto'
    operation_time:   HH:MM:SS or 'auto'
    account:          Sender account (20 digits)
    commission:       Commission in RUB (default 0)
    message:          Payment message (default 'Перевод денежных средств')
    receipt_number:   Operation ID override or 'auto'

Pipeline:
    1. Compute all fields (dates, op_id, sbp_id, formed_at)
    2. Find best SBP donor from СБП/ folder
    3. Extend font if any characters missing
    4. CID-patch all fields
    5. Set Document /ID (ID[0]==ID[1], lowercase, Oracle BI Publisher style)
    6. Return PDF bytes
"""
from __future__ import annotations

import os
import random
import re
import secrets
import sys
import string
import zlib
from datetime import date as _date_cls, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.resolve()
SBP_DONORS_DIR = ROOT / "СБП"

_MSK = timedelta(hours=3)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_rub(amount: int) -> str:
    """'3\xa0000\xa0RUR\xa0' — Oracle BI Publisher SBP format."""
    s = f"{amount:,}".replace(",", "\xa0")
    return f"{s}\xa0RUR\xa0"


def _fmt_phone(phone: str) -> str:
    """Normalise to +7\xa0(XXX)\xa0XXX-XX-XX format.

    Accepts: +79001234567, 89001234567, +7 900 123 45 67, etc.
    """
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        d = digits[1:]
        return f"+7\xa0({d[:3]})\xa0{d[3:6]}-{d[6:8]}-{d[8:10]}"
    # Already normalised or foreign — return with NBSP
    return phone.replace(" ", "\xa0")


def _fmt_datetime(date_str: str, time_str: str) -> str:
    """'08.04.2026\xa009:23:16\xa0мск\xa0' — note trailing NBSP."""
    return f"{date_str}\xa0{time_str}\xa0мск\xa0"


def _fmt_formed(date_str: str, time_str: str, offset_minutes: int = 0) -> str:
    """'08.04.2026\xa000:34\xa0мск' — no seconds, with offset.  Handles midnight wrap."""
    from datetime import date as _date, timedelta as _td
    h, m = int(time_str[:2]), int(time_str[3:5])
    total_minutes = h * 60 + m + offset_minutes
    extra_days = total_minutes // 1440
    total_minutes %= 1440
    out_date = date_str
    if extra_days:
        try:
            dd, mm, yyyy = (int(x) for x in date_str.split("."))
            new_d = _date(yyyy, mm, dd) + _td(days=extra_days)
            out_date = new_d.strftime("%d.%m.%Y")
        except (ValueError, OverflowError):
            pass
    return f"{out_date}\xa0{total_minutes // 60:02d}:{total_minutes % 60:02d}\xa0мск"


def _compute_filename_ts(formed_str: str) -> int:
    """Compute a 13-digit Unix epoch (ms) timestamp for AM_{ts}.pdf filenames.

    Genuine Alfa-Bank filenames are consistently 225-301 seconds BEFORE the
    'Сформирована' (date_formed) time shown inside the PDF.  We derive the
    timestamp from date_formed and subtract a random value in that range.

    formed_str: the already-formatted string like '08.04.2026\xa012:26\xa0мск'
    """
    clean = formed_str.replace("\xa0", " ").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", clean)
    if not m:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    dd, mm, yyyy, hh, mi = (int(x) for x in m.groups())
    formed_msk = datetime(yyyy, mm, dd, hh, mi, 0, tzinfo=timezone.utc)
    formed_utc = formed_msk - _MSK
    formed_ts_sec = int(formed_utc.timestamp())
    delta_sec = random.randint(225, 301)
    ms = random.randint(0, 999)
    return (formed_ts_sec - delta_sec) * 1000 + ms


def _fmt_text(text: str) -> str:
    """Replace plain spaces with NBSP (Oracle BI Publisher style)."""
    return text.replace(" ", "\xa0")


def _generate_operation_id(date_str: str, time_str: str = "12:00:00") -> str:
    """Generate SBP operation ID: C16{DDMMYY}{7-digit seq}.

    The 7-digit sequence is a daily counter that resets at midnight MSK.
    Derived from 40+ genuine intraday data points (Alfa-Bank SBP March 2026):

        00:32 (  32 min) →    14,659  (night baseline, ~400/min)
        05:13 ( 313 min) →   118,768  (pre-dawn ramp)
        07:26 ( 446 min) →   257,822  (morning rush ramp)
        08:20 ( 500 min) →   356,829  (morning peak begins)
        12:23 ( 743 min) → 1,021,840  (business-hours peak, ~2737/min)
        15:34 ( 934 min) → 1,479,778  (afternoon, extrapolated same rate)
        16:16 ( 976 min) → 1,600,906
        18:57 (1137 min) → 2,051,169  (end of peak)
        22:03 (1323 min) → 2,149,539  (evening wind-down, ~530/min)

    We use piecewise linear interpolation on cumulative seq anchored to the
    observed data, then add ±20% multiplicative noise.
    """
    parts = date_str.split(".")
    dd, mm = parts[0], parts[1]
    yy = parts[2][2:] if len(parts[2]) == 4 else parts[2]

    try:
        hh, mi = int(time_str[:2]), int(time_str[3:5])
    except Exception:
        hh, mi = 12, 0
    minutes = hh * 60 + mi

    # Cumulative seq anchored to real data (piecewise linear between anchors)
    # Each anchor: (minutes_from_midnight, cumulative_seq)
    ANCHORS = [
        (0,    0),
        (56,   22_389),
        (313,  118_768),
        (446,  257_822),
        (500,  356_829),
        (743,  1_021_840),
        (1080, 1_944_000),   # extrapolated: 356829 + (1080-500)*2737
        (1320, 2_071_000),   # evening wind-down: ~530/min after 18:00
        (1440, 2_095_000),   # late night: ~200/min after 22:00
    ]

    base = 0
    for i in range(len(ANCHORS) - 1):
        t0, s0 = ANCHORS[i]
        t1, s1 = ANCHORS[i + 1]
        if minutes <= t1:
            frac = (minutes - t0) / (t1 - t0)
            base = s0 + int(frac * (s1 - s0))
            break
    else:
        base = ANCHORS[-1][1]

    noise = random.uniform(0.80, 1.20)
    seq = max(8000, min(2_199_999, int(base * noise)))
    return f"C16{dd}{mm}{yy}{seq:07d}"


_SBP_SUFFIX_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Known p23 era transition dates.
# Format: (start_date, min_p23_int) — from start_date onwards p23 must be >= min_p23.
# When Alfa Bank rolls p23 to a new value, add a new entry here.
_P23_ERA_CUTOFFS: list[tuple[_date_cls, int]] = [
    (_date_cls(2026, 4, 10), 61),  # Apr 10 2026: p23 rolled from 60 → 61
    # (_date_cls(2026, X, Y), 62),  # add next cutoff here when discovered
]

# Legacy constants (kept for reference, formula was not reliably calibrated)
_P23_BASE   = 1685200931.0
_P23_PERIOD = 1459090.5


def _min_p23_for_date(yyyy: int, mm: int, dd: int) -> int:
    """Return the minimum valid p23 integer for a given operation date."""
    try:
        op_date = _date_cls(yyyy, mm, dd)
    except (ValueError, OverflowError):
        return 60
    min_p23 = 60
    for cutoff_date, min_val in _P23_ERA_CUTOFFS:
        if op_date >= cutoff_date:
            min_p23 = max(min_p23, min_val)
    return min_p23


def _extract_sbp_suffix(donor_sbp_id: str, op_date_str: str = "", op_time_str: str = "") -> str:
    """Return the 16-char SBP ID suffix verbatim from the donor.

    Analysis of genuine receipts shows the suffix is bank-specific and
    date-era-specific.  The safest approach is to always copy it verbatim
    from a date-matched donor (one selected from the same era).  Earlier
    attempts to reconstruct the suffix via formula produced incorrect results
    once the day-of-year exceeded 100.
    """
    return donor_sbp_id[16:] if len(donor_sbp_id) >= 32 else "0G10120011740501"


def _generate_sbp_id(
    date_str: str,
    time_str: str,
    donor_sbp_id: str = "",
    prefix: str | None = None,
    check_char: str | None = None,
    p23: str | None = None,
) -> str:
    """Generate a 32-char Alfa-Bank SBP transaction ID that matches the real format.

    Real structure (confirmed across 100+ genuine receipts):
      pos 1     : [A|B]  — Alfa-Bank prefix letter
      pos 2-3   : 2-digit bank/era counter — extracted from donor (e.g. "60", "61")
      pos 4-5   : day of year (UTC) % 100, zero-padded
      pos 6-7   : UTC hour, zero-padded
      pos 8-9   : UTC minute, zero-padded
      pos 10-11 : UTC second, zero-padded
      pos 12-15 : 4-digit sequence (random within observed range)
      pos 16    : check character — uppercase letter (~60%) or digit (~40%)
      pos 17-32 : bank-specific suffix (copied verbatim from donor)

    p23 must be extracted from the donor PDF (donor_sbp_id[1:3]).  The value
    changed from "60" (Apr 8-9 2026) to "61" (Apr 10+ 2026) and will change
    again in the future.  Never hardcode it.
    """
    # Convert MSK time to UTC (MSK = UTC+3)
    parts = date_str.split(".")
    dd, mm, yyyy = int(parts[0]), int(parts[1]), int(parts[2])
    hh, mi, ss = int(time_str[:2]), int(time_str[3:5]), int(time_str[6:8])
    msk_dt = datetime(yyyy, mm, dd, hh, mi, ss, tzinfo=timezone.utc) - _MSK
    utc_dt = msk_dt  # after subtracting MSK offset it is UTC

    day_of_year = utc_dt.timetuple().tm_yday % 100
    utc_h = utc_dt.hour
    utc_m = utc_dt.minute
    utc_s = utc_dt.second

    seq = random.randint(1000, 9999)
    if prefix is None:
        prefix = random.choice("AB")
    if check_char is None:
        check_char = (
            random.choice(string.ascii_uppercase)
            if random.random() < 0.6
            else str(random.randint(0, 9))
        )
    # p23: take from donor. If not provided, extract from donor_sbp_id.
    if p23 is None:
        p23 = donor_sbp_id[1:3] if len(donor_sbp_id) >= 3 else "61"
    # Enforce minimum p23 for the operation date — prevents stale era from old donors.
    try:
        min_p23 = _min_p23_for_date(yyyy, mm, dd)
        if int(p23) < min_p23:
            print(
                f"[WARN] Upgrading stale p23={p23} → {min_p23:02d} "
                f"for date {dd:02d}.{mm:02d}.{yyyy} "
                f"(donor suffix reused from older era)"
            )
            p23 = f"{min_p23:02d}"
    except (ValueError, TypeError):
        pass
    suffix = _extract_sbp_suffix(donor_sbp_id)

    return (
        f"{prefix}{p23}{day_of_year:02d}{utc_h:02d}{utc_m:02d}{utc_s:02d}"
        f"{seq:04d}{check_char}{suffix}"
    )


# ---------------------------------------------------------------------------
# CMap / donor helpers (shared logic with gen_tajik_receipt.py)
# ---------------------------------------------------------------------------

def _trim_cmap_in_pdf(pdf_bytes: bytes) -> bytes:
    """Remove CMap entries for CIDs that are never used in the content stream.

    Genuine Oracle BI Publisher PDFs have a tight CMap with zero unused entries.
    After font surgery the CMap may contain extra glyphs. This function strips
    them so the output is indistinguishable from a genuine receipt.
    """
    STREAM_RE = re.compile(
        rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", re.DOTALL
    )
    streams = []
    for m in STREAM_RE.finditer(pdf_bytes):
        slen = int(m.group(2))
        start = m.end()
        raw = pdf_bytes[start: start + slen]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            dec = None
        streams.append({
            "dict_start": m.start(),
            "stream_start": start,
            "stream_len": slen,
            "len_num_start": m.start(2),
            "raw": raw,
            "dec": dec,
        })

    if len(streams) < 6:
        return pdf_bytes  # unexpected structure

    # --- Collect used CIDs from content stream (index 3) ---
    content = streams[3]["dec"]
    if not content:
        return pdf_bytes
    used_cids: set[int] = set()
    for tj_hex in re.findall(rb"<([0-9A-Fa-f]+)>\s*Tj", content):
        s = tj_hex.decode()
        for i in range(0, len(s), 4):
            used_cids.add(int(s[i: i + 4], 16))

    # --- Parse CMap stream (index 5) ---
    cmap_info = streams[5]
    cmap_dec = cmap_info["dec"]
    if not cmap_dec:
        return pdf_bytes
    cmap_text = cmap_dec.decode("latin-1")

    block_m = re.search(r"beginbfchar\r?\n(.*?)endbfchar", cmap_text, re.DOTALL)
    if not block_m:
        return pdf_bytes
    entries = re.findall(r"<([0-9A-Fa-f]{4})>\s+<([0-9A-Fa-f]{4})>", block_m.group(1))

    # Only keep entries whose CID is actually used
    kept = [(c, u) for c, u in entries if int(c, 16) in used_cids]
    if len(kept) == len(entries):
        return pdf_bytes  # nothing to trim

    # --- Rebuild CMap text ---
    new_block_body = "\r\n".join(f"<{c}> <{u}>" for c, u in kept) + "\r\n"
    new_cmap_text = cmap_text
    # Replace entry count
    new_cmap_text = re.sub(
        r"\d+(?=\s+beginbfchar)", str(len(kept)), new_cmap_text, count=1
    )
    # Replace block body
    new_cmap_text = new_cmap_text.replace(block_m.group(1), new_block_body, 1)
    new_cmap_bytes = new_cmap_text.encode("latin-1")
    new_raw = zlib.compress(new_cmap_bytes, 6)

    print(f"[TRIM CMAP] {len(entries)} → {len(kept)} entries "
          f"({len(cmap_info['raw'])} → {len(new_raw)} bytes)")

    # --- Splice new stream into PDF bytes ---
    delta = len(new_raw) - cmap_info["stream_len"]
    data = bytearray(pdf_bytes)

    # Replace stream data
    s_start = cmap_info["stream_start"]
    data[s_start: s_start + cmap_info["stream_len"]] = new_raw

    # Update /Length value
    old_len_str = str(cmap_info["stream_len"]).encode()
    new_len_str = str(len(new_raw)).encode()
    ln_start = cmap_info["len_num_start"]
    # Recompute after splice (positions shift if len string changes length)
    # Find fresh after splice (delta not yet applied to data, so use bytearray as-is)
    len_delta = len(new_len_str) - len(old_len_str)
    data[ln_start: ln_start + len(old_len_str)] = new_len_str
    delta += len_delta

    if delta == 0:
        return bytes(data)

    # Adjust for len_delta in positions
    effective_stream_start = s_start  # before splice; xref entries after this shift

    # Update xref table
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)",
        bytes(data),
    )
    if xref_m:
        entries_ba = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries_ba):
            offset = int(em.group(1))
            if offset > effective_stream_start:
                new_offset = offset + delta
                entries_ba[em.start(1): em.start(1) + 10] = f"{new_offset:010d}".encode()
        pos = xref_m.start(3)
        data[pos: pos + len(xref_m.group(3))] = bytes(entries_ba)

    # Update startxref
    sxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", bytes(data))
    if sxref_m:
        old_xref_pos = int(sxref_m.group(1))
        if effective_stream_start < old_xref_pos:
            new_xref_pos = old_xref_pos + delta
            pos = sxref_m.start(1)
            data[pos: pos + len(str(old_xref_pos))] = str(new_xref_pos).encode()

    return bytes(data)


# ---------------------------------------------------------------------------

def _cmap_from_pdf(pdf_bytes: bytes) -> dict[int, str]:
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        raw = pdf_bytes[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        uni_to_cid: dict[int, str] = {}
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
            if uni_to_cid:
                return uni_to_cid
        if b"beginbfrange" in dec:
            for mm in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec
            ):
                s = int(mm.group(1), 16)
                e = int(mm.group(2), 16)
                u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    uni_to_cid[u + i] = f"{s + i:04X}"
            if uni_to_cid:
                return uni_to_cid
    return {}


def _chars_missing(cmap: dict[int, str], text: str) -> set[str]:
    missing: set[str] = set()
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp not in cmap and cp != 0xFFFF:
            missing.add(ch)
    return missing


_GENUINE_CACHE: dict[Path, bool] = {}


def _is_genuine_pdf(pdf_path: Path) -> bool:
    """Content-based genuineness test: every compressed stream must be exactly
    reproducible at zlib level 6.  Genuine Oracle BI Publisher PDFs always
    satisfy this; previously-patched PDFs (level 9 content, modified fonts) do
    not."""
    if pdf_path in _GENUINE_CACHE:
        return _GENUINE_CACHE[pdf_path]
    try:
        raw = pdf_path.read_bytes()
    except OSError:
        _GENUINE_CACHE[pdf_path] = False
        return False
    for m in re.finditer(
        rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", raw, re.DOTALL
    ):
        slen = int(m.group(2))
        data = raw[m.end(): m.end() + slen]
        if len(data) < 2 or data[0] != 0x78:
            continue
        try:
            dec = zlib.decompress(data)
            if zlib.compress(dec, 6) != data:
                _GENUINE_CACHE[pdf_path] = False
                return False
        except zlib.error:
            _GENUINE_CACHE[pdf_path] = False
            return False
    _GENUINE_CACHE[pdf_path] = True
    return True


def _find_best_donor(
    all_text: str,
    *,
    _cache: dict[Path, dict[int, str]] | None = None,
) -> tuple[Optional[Path], int]:
    """Find the donor whose CMap best covers *all_text*.

    Returns (path, missing_count).  missing_count == 0 means full coverage
    (no font surgery needed).
    """
    if not SBP_DONORS_DIR.exists():
        return None, -1
    required: set[int] = set()
    for ch in all_text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        required.add(cp)

    best_path: Optional[Path] = None
    best_missing = len(required) + 1
    for pdf_path in sorted(SBP_DONORS_DIR.glob("*.pdf")):
        if not _is_genuine_pdf(pdf_path):
            continue
        if _cache is not None and pdf_path in _cache:
            cmap = _cache[pdf_path]
        else:
            try:
                raw = pdf_path.read_bytes()
            except OSError:
                continue
            cmap = _cmap_from_pdf(raw)
            if not cmap:
                continue
            if _cache is not None:
                _cache[pdf_path] = cmap
        missing = len(required - set(cmap.keys()))
        if missing < best_missing:
            best_missing = missing
            best_path = pdf_path
            if missing == 0:
                break
    return best_path, best_missing


def _decode_stream_texts(pdf_bytes: bytes, cmap: dict[int, str]) -> list[str]:
    cid_to_chr = {cid: chr(uni) for uni, cid in cmap.items()}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        slen = int(m.group(2))
        start = m.end()
        raw = pdf_bytes[start: start + slen]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tj" not in dec:
            continue
        texts: list[str] = []
        for tj in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", dec):
            hexdata = tj.group(1).decode().upper()
            chars = []
            for i in range(0, len(hexdata), 4):
                code = hexdata[i: i + 4]
                chars.append(cid_to_chr.get(code, "?"))
            texts.append("".join(chars))
        return texts
    return []


def _extract_donor_fields(pdf_bytes: bytes, cmap: dict[int, str]) -> dict[str, str]:
    texts = _decode_stream_texts(pdf_bytes, cmap)
    labels = [t.replace("\xa0", " ").strip() for t in texts]

    def _next_val(i: int) -> str:
        j = i + 1
        while j < len(labels) and labels[j] == "":
            j += 1
        return texts[j] if j < len(texts) else ""

    fields: dict[str, str] = {}
    for i, lbl in enumerate(labels):
        if re.search(r"\d+\.\d+\.\d{4}\s+\d{2}:\d{2}\s+мск", lbl):
            fields["date_formed"] = texts[i]
        elif lbl == "Сумма перевода":
            fields["amount"] = _next_val(i)
        elif lbl == "Комиссия":
            fields["commission"] = _next_val(i)
        elif "Дата и время" in lbl:
            fields["date_time"] = _next_val(i)
        elif lbl == "Номер операции":
            fields["operation_id"] = _next_val(i)
        elif lbl == "Получатель":
            fields["recipient"] = _next_val(i)
        elif "телефона получателя" in lbl:
            fields["phone"] = _next_val(i)
        elif "Банк получателя" in lbl:
            fields["bank"] = _next_val(i)
        elif "Счёт списания" in lbl or "Счет списания" in lbl:
            fields["account"] = _next_val(i)
        elif "Идентификатор" in lbl:
            fields["sbp_id"] = _next_val(i)
        elif "Сообщение" in lbl:
            fields["message"] = _next_val(i)
    return fields


def _set_doc_id_equal(pdf_bytes: bytes) -> bytes:
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf_bytes)
    if not m:
        return pdf_bytes
    old1, old2 = m.group(1), m.group(2)
    target_len = len(old1)
    # Oracle BI Publisher: always lowercase, ID[0] == ID[1]
    new_hex = secrets.token_hex(target_len // 2).lower().encode()
    pdf_bytes = pdf_bytes.replace(b"<" + old1 + b">", b"<" + new_hex + b">", 1)
    pdf_bytes = pdf_bytes.replace(b"<" + old2 + b">", b"<" + new_hex + b">", 1)
    return pdf_bytes


# ---------------------------------------------------------------------------
# Minimal-change mode: keep SBP ID & operation ID verbatim, swap personal data
# ---------------------------------------------------------------------------

def generate_minimal_change(
    donor_path: "str | Path",
    new_recipient: str,
    new_phone: str,
    new_account: str | None = None,
    output_path: "str | Path | None" = None,
    change_recipient: bool = True,
    change_phone: bool = True,
    change_account: bool = True,
    change_doc_id: bool = False,
) -> "tuple[bytes, str]":
    """Replace only recipient/phone/account in a genuine donor PDF.

    All 'transaction identity' fields are kept verbatim from the donor:
      - SBP ID (32-char)
      - Operation ID (C16...)
      - Amount, commission
      - Date/time, Сформирована date
      - Bank name
      - Message

    Only the personal-data fields are replaced:
      - Recipient name   (must use chars already in donor CMap)
      - Phone number     (digits/+/( ) only — always available)
      - Sender account   (optional; 20-digit Alfa-Bank account)

    This tests whether the external checker's "✅ Альфа Банк" result is
    triggered purely by a live SBP ID lookup (ignoring personal fields).

    Parameters
    ----------
    donor_path:     Path to a genuine Alfa-Bank SBP receipt PDF.
    new_recipient:  New recipient full name (Cyrillic + space).
    new_phone:      New phone, e.g. '+7 (910) 123-45-67'.
    new_account:    Optional new 20-digit sender account.
    output_path:    Output file path or directory.  Returns bytes only if None.

    Returns
    -------
    (pdf_bytes, canonical_filename)
    """
    donor_file = Path(donor_path)
    pdf_bytes = donor_file.read_bytes()
    print(f"[MINIMAL] Donor: {donor_file.name}")

    cmap = _cmap_from_pdf(pdf_bytes)
    donor_fields = _extract_donor_fields(pdf_bytes, cmap)
    print(f"[MINIMAL] Donor fields: {list(donor_fields.keys())}")

    # Validate that the new recipient uses only chars already in the CMap
    avail_cps = set(cmap.keys())
    missing = {ch for ch in new_recipient.replace(" ", "\xa0") if ord(ch) >= 0x20 and ord(ch) not in avail_cps}
    if missing:
        raise ValueError(
            f"New recipient contains chars not in donor CMap: {sorted(missing)}. "
            "Choose a recipient with only characters already used in the donor."
        )

    # Build replacement list (only personal fields, controlled by flags)
    replacements: list[tuple[str, str]] = []

    def _add_min(old_key: str, new_val: str) -> None:
        old = donor_fields.get(old_key, "")
        old_clean = old.replace("\xa0", " ").strip()
        new_clean = new_val.replace("\xa0", " ").strip()
        if old and old_clean != new_clean:
            if old.endswith("\xa0"):
                new_clean += " "
            replacements.append((old_clean, new_clean))

    if change_recipient:
        _add_min("recipient", _fmt_text(new_recipient))
    if change_phone:
        _add_min("phone", _fmt_phone(new_phone))
    if change_account and new_account is not None:
        _add_min("account", new_account)

    print(f"[MINIMAL] Replacements ({len(replacements)}):")
    for old, new in replacements:
        print(f"  {old!r} → {new!r}")

    if replacements:
        from cid_patch_amount import patch_replacements
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            in_path = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            out_path = Path(f.name)

        try:
            ok = patch_replacements(in_path, out_path, replacements)
            if not ok:
                raise RuntimeError("CID replacement failed — check donor compatibility.")
            pdf_bytes = out_path.read_bytes()
        finally:
            for p in (in_path, out_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # Keep Document /ID equal (ID[0] == ID[1]) — optionally regenerate
    if change_doc_id:
        pdf_bytes = _set_doc_id_equal(pdf_bytes)

    # Canonical filename derived from donor's date_formed (225-301s before)
    donor_formed = donor_fields.get("date_formed", "")
    filename_ts = _compute_filename_ts(donor_formed) if donor_formed else int(datetime.now(timezone.utc).timestamp() * 1000)
    canonical_filename = f"AM_{filename_ts}.pdf"

    # Post-generation validation — only check fields we actually changed
    from pdf_validate import validate_pdf
    expected = []
    if change_recipient:
        expected.append(new_recipient.replace("\xa0", " ").strip())
    if change_phone:
        expected.append(_fmt_phone(new_phone).replace("\xa0", " ").strip())
    if not expected:
        # fallback: verify the donor's own recipient is present
        expected.append(donor_fields.get("recipient", "").replace("\xa0", " ").strip())
    result = validate_pdf(pdf_bytes, expected)
    for line in result.info:
        print(f"[VALIDATE] {line}")
    for line in result.warnings:
        print(f"[VALIDATE WARN] {line}")
    if result.errors:
        for line in result.errors:
            print(f"[VALIDATE ERROR] {line}")
        raise RuntimeError(f"Minimal-change PDF failed validation: {result.errors[0]}")

    if output_path is not None:
        out = Path(output_path)
        if out.is_dir():
            out = out / canonical_filename
        else:
            out = out.parent / canonical_filename
        out.write_bytes(pdf_bytes)
        print(f"[OK] Written: {out} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes, canonical_filename


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_sbp_receipt(
    *,
    amount: int,
    recipient: str,
    phone: str,
    bank: str,
    operation_date: str = "auto",
    operation_time: str = "auto",
    account: str | None = None,
    commission: int = 0,
    message: str = "Перевод денежных средств",
    receipt_number: str = "auto",
    donor_path: str | Path | None = None,
    output_path: str | Path | None = None,
    glyph_source: str | None = None,
    sbp_prefix: str | None = None,
    sbp_p23: str | None = None,
    sbp_check: str | None = None,
    sbp_id_override: str | None = None,
    formed_time: str | None = None,
) -> tuple[bytes, str]:
    """Generate an Alfa-Bank SBP receipt PDF.

    Parameters
    ----------
    amount:           Transfer amount in RUB.
    recipient:        Recipient name, e.g. 'Виктория Игоревна С'.
    phone:            Recipient phone, e.g. '+79091234567' or '+7 (909) 123-45-67'.
    bank:             Recipient bank name, e.g. 'Сбербанк'.
    operation_date:   'DD.MM.YYYY' or 'auto'.
    operation_time:   'HH:MM:SS' or 'auto'.
    account:          Sender account (20 digits). Keeps donor value if None.
    commission:       Commission in RUB, default 0.
    message:          Payment message. Default 'Перевод денежных средств'.
    receipt_number:   Operation ID or 'auto'.
    donor_path:       Explicit donor PDF. Auto-selected if None.
    output_path:      Output file path. Returns bytes only if None.
    glyph_source:     Path to full Tahoma TTF for font surgery.
    sbp_id_override:  If provided, use this exact SBP ID instead of generating one.
                      Must be 32 chars. Useful for injecting a real SBP ID from a
                      fresh donor to test cross-validation by the external checker.

    Returns
    -------
    PDF bytes of the generated receipt.
    """
    # --- Resolve date / time ---
    if operation_date in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_date = now_msk.strftime("%d.%m.%Y")
    if operation_time in ("auto", "", None):
        now_msk = datetime.now(timezone.utc) + _MSK
        operation_time = now_msk.strftime("%H:%M:%S")
    else:
        # Normalize single-digit hours: "1:13:23" → "01:13:23"
        _tp = operation_time.split(":")
        if len(_tp) >= 2:
            operation_time = f"{int(_tp[0]):02d}:{_tp[1]}" + (f":{_tp[2]}" if len(_tp) >= 3 else ":00")

    # --- Align date/time to an injected SBP ID ---
    # A real Alfa-Bank SBP ID encodes the transaction instant to the second
    # (day-of-year mod 100, UTC hour/minute/second at positions 4-11). When a
    # caller forces an external ID (from pool or extracted from a donor PDF),
    # we MUST display the same instant, otherwise the receipt contradicts
    # itself and fails verification.
    if sbp_id_override and len(sbp_id_override) >= 32 and sbp_id_override[0] in ("A", "B"):
        try:
            _id_doy_mod = int(sbp_id_override[3:5])
            _id_utc_h = int(sbp_id_override[5:7])
            _id_utc_m = int(sbp_id_override[7:9])
            _id_utc_s = int(sbp_id_override[9:11])
        except ValueError:
            _id_doy_mod = _id_utc_h = _id_utc_m = _id_utc_s = -1
        if (
            0 <= _id_utc_h < 24 and 0 <= _id_utc_m < 60 and 0 <= _id_utc_s < 60
            and 0 <= _id_doy_mod < 100
        ):
            from datetime import timedelta as _td
            try:
                _dd, _mm, _yyyy = (int(x) for x in operation_date.split("."))
                _ref_doy = datetime(_yyyy, _mm, _dd).timetuple().tm_yday
            except Exception:
                _now = datetime.now(timezone.utc) + _MSK
                _yyyy = _now.year
                _ref_doy = _now.timetuple().tm_yday
            _cands = [_id_doy_mod + 100 * k for k in range(0, 4) if 1 <= _id_doy_mod + 100 * k <= 366]
            if _cands:
                _doy = min(_cands, key=lambda d: abs(d - _ref_doy))
                try:
                    _utc_dt = (
                        datetime(_yyyy, 1, 1, _id_utc_h, _id_utc_m, _id_utc_s, tzinfo=timezone.utc)
                        + _td(days=_doy - 1)
                    )
                    _msk_dt = _utc_dt + _MSK
                    operation_date = _msk_dt.strftime("%d.%m.%Y")
                    operation_time = _msk_dt.strftime("%H:%M:%S")
                    print(f"[INFO] Date/time aligned to SBP ID: {operation_date} {operation_time}")
                except (ValueError, OverflowError):
                    pass

    # --- Derived fields ---
    # Genuine receipts show the PDF being opened/downloaded anywhere from
    # immediately after the transaction to several hours later. Allow 0-360 min.
    # Real Alfa SBP receipts: "Сформирована" is within 0-12 minutes of the transfer.
    # Checker flags PDFs where gap > ~15 min even on genuine receipts.
    # Observed safe range from passing real PDFs: 0-12 min.
    if formed_time and formed_time not in ("auto", "авто", "-", ""):
        # Use explicitly provided formed time (DD.MM.YYYY HH:MM or just HH:MM)
        _ft = formed_time.strip()
        # If only time HH:MM provided, combine with operation_date
        if re.match(r"^\d{1,2}:\d{2}$", _ft):
            _ft = f"{operation_date}\xa0{int(_ft[:2]):02d}:{_ft[-2:]}\xa0мск"
        elif re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}$", _ft):
            _parts = _ft.split()
            _ft = f"{_parts[0]}\xa0{_parts[1]}\xa0мск"
        new_date_formed = _ft
        formed_offset = 0  # used only for _compute_filename_ts fallback
    else:
        formed_offset = 2
    if not formed_time or formed_time in ("auto", "авто", "-", ""):
        new_date_formed = _fmt_formed(operation_date, operation_time, offset_minutes=formed_offset)
    new_date_time = _fmt_datetime(operation_date, operation_time)
    new_amount = _fmt_rub(amount)
    new_commission = _fmt_rub(commission)
    new_phone = _fmt_phone(phone)
    new_recipient = _fmt_text(recipient)
    new_bank = _fmt_text(bank)
    new_message = _fmt_text(message)

    if receipt_number in ("auto", "", None) or "-" in receipt_number:
        new_operation_id = _generate_operation_id(operation_date, operation_time)
    else:
        new_operation_id = receipt_number

    _sbp_prefix = sbp_prefix or random.choice("AB")
    _sbp_p23 = sbp_p23  # may be None; _generate_sbp_id will extract from donor

    # --- Collect text WITHOUT sbp_check for initial donor search ---
    # We pick sbp_check AFTER loading the donor CMap so we can choose a char
    # that is already in the font — this avoids font surgery for just one char.
    _text_without_check = "".join(filter(None, [
        new_amount, new_commission, new_date_time, new_date_formed,
        new_operation_id,
        new_recipient, new_phone, new_bank, new_message,
        account or "",
        sbp_id_override or _sbp_prefix,
    ]))

    # --- Find / load donor ---
    if donor_path is not None:
        donor_file = Path(donor_path)
    else:
        donor_file, _missing_n = _find_best_donor(_text_without_check)
        if donor_file is None:
            raise FileNotFoundError(
                f"No SBP donor PDFs found in {SBP_DONORS_DIR}. "
                "Add real Alfa-Bank SBP receipts to that folder."
            )

    # --- Pick sbp_check from donor CMap to avoid introducing new characters ---
    _donor_cmap_for_check = _cmap_from_pdf(donor_file.read_bytes())
    _avail_cps_for_check = set(_donor_cmap_for_check.keys())
    if sbp_check is not None:
        _sbp_check = sbp_check
    else:
        # Prefer chars already in the donor CMap: uppercase letters then digits
        _check_pool = [
            c for c in string.ascii_uppercase if ord(c) in _avail_cps_for_check
        ] or [
            c for c in string.digits if ord(c) in _avail_cps_for_check
        ] or list(string.ascii_uppercase)  # last resort — may need font surgery
        _sbp_check = random.choice(_check_pool)

    # Final coverage text including the chosen sbp_check
    all_new_text_base = "".join(filter(None, [
        new_amount, new_commission, new_date_time, new_date_formed,
        new_operation_id,
        new_recipient, new_phone, new_bank, new_message,
        account or "",
        sbp_id_override if sbp_id_override else (_sbp_prefix + _sbp_check),
    ]))

    pdf_bytes = donor_file.read_bytes()
    print(f"[INFO] Donor: {donor_file.name}")

    # --- Font surgery if needed ---
    cmap = _cmap_from_pdf(pdf_bytes)
    missing = _chars_missing(cmap, all_new_text_base)
    if missing:
        # Check if fonttools is available before attempting surgery
        _fonttools_ok = False
        try:
            import importlib
            importlib.import_module("fontTools.ttLib")
            _fonttools_ok = True
        except ImportError:
            pass

        if _fonttools_ok:
            print(f"[INFO] Missing chars: {sorted(missing)} — running font surgery...")
            from font_extend import extend_font_in_pdf
            pdf_bytes, cmap = extend_font_in_pdf(
                pdf_bytes, all_new_text_base, glyph_source=glyph_source
            )
            still_missing = _chars_missing(cmap, all_new_text_base)
            if still_missing:
                raise RuntimeError(f"Font surgery failed — still missing: {sorted(still_missing)}")
            print("[INFO] Font surgery complete.")
        else:
            # fonttools not installed — try to install it on-the-fly, then retry
            print(f"[WARN] fonttools not available — attempting on-the-fly install ...")
            try:
                import subprocess as _sp
                _sp.check_call(
                    [sys.executable, "-m", "pip", "install", "fonttools>=4.43.0", "--quiet"],
                    stdout=_sp.DEVNULL,
                )
                _fonttools_ok = True
                print("[INFO] fonttools installed OK — retrying font surgery")
            except Exception as _pip_err:
                print(f"[WARN] pip install fonttools failed: {_pip_err}")

            if _fonttools_ok:
                from font_extend import extend_font_in_pdf
                pdf_bytes, cmap = extend_font_in_pdf(
                    pdf_bytes, all_new_text_base, glyph_source=glyph_source
                )
                still_missing = _chars_missing(cmap, all_new_text_base)
                if still_missing:
                    raise RuntimeError(f"Font surgery failed — still missing: {sorted(still_missing)}")
                print("[INFO] Font surgery complete (after on-the-fly install).")
            else:
                # Last resort: find zero-missing donor
                perfect_donor, perfect_missing = _find_best_donor(all_new_text_base)
                if perfect_donor is not None and perfect_missing == 0:
                    donor_file = perfect_donor
                    pdf_bytes = donor_file.read_bytes()
                    cmap = _cmap_from_pdf(pdf_bytes)
                    print(f"[INFO] Switched to zero-missing donor: {donor_file.name}")
                else:
                    missing_chars = sorted(missing)
                    raise RuntimeError(
                        f"Символы {missing_chars} отсутствуют во всех донорских PDF. "
                        "Попробуйте другое имя получателя или банк."
                    )
    else:
        print("[INFO] All characters already in font.")

    # --- Extract donor field values ---
    donor_fields = _extract_donor_fields(pdf_bytes, cmap)
    print(f"[INFO] Donor fields: {list(donor_fields.keys())}")

    # --- Generate SBP ID now that we have the donor's SBP ID for suffix extraction ---
    donor_sbp_id = donor_fields.get("sbp_id", "").replace("\xa0", "").strip()
    if sbp_id_override and len(sbp_id_override) == 32:
        new_sbp_id = sbp_id_override
        print(f"[INFO] SBP ID override: {new_sbp_id}")
    else:
        new_sbp_id = _generate_sbp_id(
            operation_date, operation_time, donor_sbp_id,
            prefix=_sbp_prefix, check_char=_sbp_check,
            p23=_sbp_p23,  # extracted from donor; None → auto-extract from donor_sbp_id
        )
        print(f"[INFO] Generated SBP ID: {new_sbp_id}")

    # --- Build replacement pairs ---
    replacements: list[tuple[str, str]] = []

    def _add(old_key: str, new_val: str) -> None:
        old = donor_fields.get(old_key, "")
        old_clean = old.replace("\xa0", " ").strip()
        new_clean = new_val.replace("\xa0", " ").strip()
        if old and old_clean != new_clean:
            # Preserve trailing NBSP: if the donor field ended with NBSP,
            # the new value must also end with a space (which maps to the
            # NBSP CID in the Oracle font).  This is how genuine Oracle BI
            # Publisher PDFs always encode amount, datetime, op_id, recipient.
            if old.endswith("\xa0"):
                new_clean += " "
            replacements.append((old_clean, new_clean))

    _add("amount", new_amount)
    _add("commission", new_commission)
    _add("date_time", new_date_time)
    _add("date_formed", new_date_formed)
    _add("operation_id", new_operation_id)
    _add("sbp_id", new_sbp_id)
    _add("recipient", new_recipient)
    _add("phone", new_phone)
    _add("bank", new_bank)
    _add("message", new_message)
    if account is not None:
        _add("account", account)

    print(f"[INFO] Replacements ({len(replacements)}):")
    for old, new in replacements:
        print(f"  {old!r} → {new!r}")

    if replacements:
        from cid_patch_amount import patch_replacements
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            in_path = Path(f.name)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            out_path = Path(f.name)

        try:
            ok = patch_replacements(in_path, out_path, replacements)
            if not ok:
                raise RuntimeError("CID replacement failed — check donor compatibility.")
            pdf_bytes = out_path.read_bytes()
        finally:
            for p in (in_path, out_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # --- Set Document /ID ---
    pdf_bytes = _set_doc_id_equal(pdf_bytes)

    # --- Trim CMap: remove unused entries so checker sees zero unused ---
    pdf_bytes = _trim_cmap_in_pdf(pdf_bytes)

    # --- Post-generation validation ---
    from pdf_validate import validate_pdf
    expected = [
        new_recipient.replace("\xa0", " ").strip(),
        new_phone.replace("\xa0", " ").strip(),
        new_amount.replace("\xa0", " ").strip(),
    ]
    result = validate_pdf(pdf_bytes, expected)
    for line in result.info:
        print(f"[VALIDATE] {line}")
    for line in result.warnings:
        print(f"[VALIDATE WARN] {line}")
    if result.errors:
        for line in result.errors:
            print(f"[VALIDATE ERROR] {line}")
        raise RuntimeError(f"Generated PDF failed validation: {result.errors[0]}")

    # --- Semantic self-consistency check ---
    # Catch the exact bug class that bit us once: year in operation ID not
    # matching year in "Дата и время перевода", or SBP ID encoding a time
    # different from the one shown. If any of these misalign, refuse to emit.
    try:
        import fitz as _fitz
        _doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
        _text = _doc[0].get_text() if _doc.page_count else ""
        _doc.close()
    except Exception:
        _text = ""

    if _text:
        _sem_errors: list[str] = []
        # Prefer the transaction datetime ("перевода") over the formed datetime ("формирования")
        # to avoid the case where _fmt_formed crosses UTC midnight due to formed_offset.
        _m_dt = re.search(r"перевода[^\d]{0,30}(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s*мск", _text)
        if not _m_dt:
            _m_dt = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s*мск", _text)
        _m_op = re.search(r"C16\d{13}\b", _text)
        _m_sbp = re.search(r"\b[AB]\d{2}[A-Z0-9]{29}\b", _text)

        if _m_dt and _m_op:
            _yy_doc = _m_dt.group(1).split(".")[2][-2:]
            _yy_op = _m_op.group(0)[7:9]
            if _yy_doc != _yy_op:
                _sem_errors.append(
                    f"operation id year YY={_yy_op} ≠ transaction date year YY={_yy_doc} "
                    f"(op={_m_op.group(0)}, date={_m_dt.group(1)})"
                )
        if _m_dt and _m_sbp:
            _sbp = _m_sbp.group(0)
            try:
                _sbp_h = int(_sbp[5:7]); _sbp_m = int(_sbp[7:9]); _sbp_s = int(_sbp[9:11])
                from datetime import timedelta as _td2
                _d, _mo, _y = (int(x) for x in _m_dt.group(1).split("."))
                _th, _tm, _ts = (int(x) for x in _m_dt.group(2).split(":"))
                _msk_dt = datetime(_y, _mo, _d, _th, _tm, _ts, tzinfo=timezone.utc)
                _utc_dt = _msk_dt - _MSK
                if (_utc_dt.hour, _utc_dt.minute, _utc_dt.second) != (_sbp_h, _sbp_m, _sbp_s):
                    _sem_errors.append(
                        f"SBP ID encodes UTC {_sbp_h:02d}:{_sbp_m:02d}:{_sbp_s:02d} "
                        f"but receipt shows {_m_dt.group(2)} МСК "
                        f"(= UTC {_utc_dt.hour:02d}:{_utc_dt.minute:02d}:{_utc_dt.second:02d})"
                    )
                _sbp_doy_mod = int(_sbp[3:5])
                _doc_doy = _utc_dt.timetuple().tm_yday  # SBP ID encodes UTC day
                _doc_doy_mod = _doc_doy % 100
                # Allow ±1 day: MSK = UTC+3, so times 00:00-02:59 MSK are UTC previous day.
                # A real SBP ID from UTC day N is valid on MSK day N+1 morning.
                _allowed_doy_mods = (_doc_doy_mod, (_doc_doy - 1) % 100)
                if _sbp_doy_mod not in _allowed_doy_mods:
                    _sem_errors.append(
                        f"SBP ID day-of-year mod100={_sbp_doy_mod} ≠ receipt doy mod100={_doc_doy_mod}"
                    )
            except ValueError:
                pass
        if sbp_id_override and _m_sbp and _m_sbp.group(0) != sbp_id_override:
            _sem_errors.append(
                f"embedded SBP ID ({_m_sbp.group(0)}) ≠ requested override ({sbp_id_override})"
            )

        if _sem_errors:
            for _e in _sem_errors:
                print(f"[VALIDATE ERROR] {_e}")
            raise RuntimeError(
                "Generated PDF is internally inconsistent:\n  - "
                + "\n  - ".join(_sem_errors)
            )

    # --- Compute canonical filename (AM_{13-digit-ms-timestamp}.pdf) ---
    # Genuine Alfa-Bank filenames are 225-301 seconds before the date_formed
    # shown inside the PDF.  Derive it so external checkers find a realistic
    # filename ↔ formed-time correlation.
    filename_ts = _compute_filename_ts(new_date_formed)
    canonical_filename = f"AM_{filename_ts}.pdf"

    # --- Write output ---
    if output_path is not None:
        out = Path(output_path)
        if out.is_dir():
            out = out / canonical_filename
        Path(out).write_bytes(pdf_bytes)
        print(f"[OK] Written: {out} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes, canonical_filename


# ---------------------------------------------------------------------------
# Random realistic data generators — donor-aware ("zero-surgery") approach
# ---------------------------------------------------------------------------

_RUSSIAN_FEMALE_NAMES = [
    "Анастасия", "Наталья", "Екатерина", "Ольга", "Татьяна",
    "Ирина", "Елена", "Светлана", "Виктория", "Юлия",
    "Марина", "Александра", "Мария", "Надежда", "Дарья",
    "Жанна", "Злата", "Элина", "Эмилия", "Олеся",
    "Вероника", "Маргарита", "Алина", "Диана", "Евгения",
]
_RUSSIAN_MALE_NAMES = [
    "Александр", "Дмитрий", "Сергей", "Андрей", "Алексей",
    "Михаил", "Иван", "Николай", "Владимир", "Артём",
    "Максим", "Евгений", "Роман", "Денис", "Антон",
    "Захар", "Эдуард", "Олег", "Виктор", "Тимофей",
    "Марат", "Егор", "Валерий", "Демид", "Абдельрахман",
]
_RUSSIAN_PATRONYMICS_F = [
    "Александровна", "Сергеевна", "Игоревна", "Дмитриевна", "Андреевна",
    "Николаевна", "Владимировна", "Михайловна", "Алексеевна", "Павловна",
    "Захаровна", "Эдуардовна", "Олеговна", "Валерьевна", "Евгеньевна",
]
_RUSSIAN_PATRONYMICS_M = [
    "Александрович", "Сергеевич", "Игоревич", "Дмитриевич", "Андреевич",
    "Николаевич", "Владимирович", "Михайлович", "Алексеевич", "Павлович",
    "Захарович", "Эдуардович", "Олегович", "Валерьевич", "Евгеньевич",
]
_SBP_BANKS = [
    "Сбербанк", "Т-Банк", "ВТБ", "Газпромбанк",
    "Альфа-Банк", "Россельхозбанк", "Почта Банк",
    "Озон Банк (Ozon)", "МТС Банк", "Совкомбанк",
]

_FIXED_LABEL_TEXTS = [
    "Сформирована", "Квитанция о переводе по СБП ",
    "Сумма перевода", "RUR ", "Комиссия",
    "Дата и время перевода", "мск ",
    "Номер операции", "Получатель",
    "Номер телефона получателя", "Банк получателя",
    "Счёт списания", "Идентификатор операции в СБП",
    "Сообщение получателю", "Перевод денежных средств",
    "+7 ()", "0123456789", ".:- ", "C16",
]

_FIXED_CPS: set[int] = set()
for _lbl in _FIXED_LABEL_TEXTS:
    for _ch in _lbl:
        _cp = ord(_ch)
        if _cp == 0x20:
            _cp = 0xA0
        if _cp >= 0x20:
            _FIXED_CPS.add(_cp)


def _text_cps(text: str) -> set[int]:
    """Return the set of codepoints a text uses (space→NBSP normalised)."""
    out: set[int] = set()
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp >= 0x20:
            out.add(cp)
    return out


def _can_render(text: str, avail_cps: set[int]) -> bool:
    """True if every character of *text* is in *avail_cps*."""
    for ch in text:
        cp = ord(ch)
        if cp == 0x20:
            cp = 0xA0
        if cp >= 0x20 and cp not in avail_cps:
            return False
    return True


def _load_donor_info(pdf_path: Path) -> dict | None:
    """Load a donor PDF and return its CMap, fields, and available codepoints."""
    try:
        raw = pdf_path.read_bytes()
    except OSError:
        return None
    cmap = _cmap_from_pdf(raw)
    if not cmap:
        return None
    avail_cps = set(cmap.keys())
    fields = _extract_donor_fields(raw, cmap)

    donor_recipient = fields.get("recipient", "").replace("\xa0", " ").strip()
    donor_bank = fields.get("bank", "").replace("\xa0", " ").strip()
    donor_sbp_id = fields.get("sbp_id", "").replace("\xa0", "").strip()
    donor_op_id = fields.get("operation_id", "").replace("\xa0", "").strip()

    recipient_cps = _text_cps(donor_recipient)
    bank_cps = _text_cps(donor_bank)
    unique_var_upper = {
        cp for cp in (recipient_cps - _FIXED_CPS - bank_cps)
        if 0x0410 <= cp <= 0x042F or cp == 0x0401
    }

    sbp_prefix = donor_sbp_id[0] if donor_sbp_id else "B"
    sbp_p23 = donor_sbp_id[1:3] if len(donor_sbp_id) >= 3 else "61"
    sbp_check = donor_sbp_id[15] if len(donor_sbp_id) > 15 else "0"
    avail_latin_upper = {
        chr(cp) for cp in avail_cps if 0x41 <= cp <= 0x5A
    }

    return {
        "path": pdf_path,
        "raw": raw,
        "cmap": cmap,
        "avail_cps": avail_cps,
        "fields": fields,
        "recipient": donor_recipient,
        "bank": donor_bank,
        "sbp_id": donor_sbp_id,
        "operation_id": donor_op_id,
        "sbp_prefix": sbp_prefix,
        "sbp_p23": sbp_p23,
        "sbp_check": sbp_check,
        "unique_var_upper": unique_var_upper,
        "avail_latin_upper": avail_latin_upper,
    }


def _craft_recipient_for_donor(donor_info: dict) -> str:
    """Craft a random recipient name that reuses ALL of the donor's unique
    variable characters (both upper and lower case), so that zero CMap
    entries become unused after text replacement."""
    avail = donor_info["avail_cps"]
    must_use_upper = donor_info["unique_var_upper"]

    donor_recipient = donor_info["recipient"]
    donor_bank = donor_info["bank"]
    bank_cps = _text_cps(donor_bank)
    recipient_cps = _text_cps(donor_recipient)
    must_use_lower = {
        cp for cp in (recipient_cps - _FIXED_CPS - bank_cps)
        if 0x0430 <= cp <= 0x044F or cp == 0x0451
    }

    pools = [
        (_RUSSIAN_FEMALE_NAMES, _RUSSIAN_PATRONYMICS_F),
        (_RUSSIAN_MALE_NAMES, _RUSSIAN_PATRONYMICS_M),
    ]
    gender_renderable: list[tuple[list[str], list[str]]] = []
    for names, patrs in pools:
        rn = [n for n in names if _can_render(n, avail)]
        rp = [p for p in patrs if _can_render(p, avail)]
        if rn and rp:
            gender_renderable.append((rn, rp))
    if not gender_renderable:
        all_n = _RUSSIAN_FEMALE_NAMES + _RUSSIAN_MALE_NAMES
        all_p = _RUSSIAN_PATRONYMICS_F + _RUSSIAN_PATRONYMICS_M
        return random.choice(all_n) + " " + random.choice(all_p) + " А"

    avail_upper_cyrillic = {
        chr(cp) for cp in avail if 0x0410 <= cp <= 0x042F or cp == 0x0401
    }

    best_combo: tuple[str, str, str] | None = None
    best_waste = 999

    for _ in range(500):
        renderable_names, renderable_patrs = random.choice(gender_renderable)
        name = random.choice(renderable_names)
        patr = random.choice(renderable_patrs)

        name_upper = {ord(name[0])} if name else set()
        patr_upper = {ord(patr[0])} if patr else set()
        combo_cps = _text_cps(name) | _text_cps(patr)

        remaining_upper = must_use_upper - name_upper - patr_upper
        initial_candidates = [chr(cp) for cp in remaining_upper
                              if chr(cp) in avail_upper_cyrillic]
        if not initial_candidates:
            initial_candidates = [chr(cp) for cp in must_use_upper
                                  if chr(cp) in avail_upper_cyrillic]
        if not initial_candidates:
            spare = avail_upper_cyrillic - set("БДИКНПС")
            initial_candidates = list(spare) if spare else list(avail_upper_cyrillic)

        initial = random.choice(initial_candidates)

        used_upper = name_upper | patr_upper | {ord(initial)}
        upper_waste = len(must_use_upper - used_upper)
        lower_waste = len(must_use_lower - combo_cps)
        waste = upper_waste + lower_waste

        if waste < best_waste:
            best_waste = waste
            best_combo = (name, patr, initial)
            if waste == 0:
                break

    if best_combo is None:
        fb_names, fb_patrs = random.choice(gender_renderable)
        name = random.choice(fb_names)
        patr = random.choice(fb_patrs)
        initial = random.choice(list(avail_upper_cyrillic)) if avail_upper_cyrillic else "А"
        best_combo = (name, patr, initial)

    return f"{best_combo[0]} {best_combo[1]} {best_combo[2]}"


def random_phone() -> str:
    """Generate a plausible Russian mobile number."""
    prefix = random.choice([
        "900", "901", "902", "903", "904", "905", "906", "909",
        "910", "911", "912", "913", "914", "915", "916", "917", "918", "919",
        "920", "921", "922", "923", "924", "925", "926", "927", "928", "929",
        "930", "931", "932", "933", "934", "936", "937", "938", "939",
        "950", "951", "952", "953", "958", "960", "961", "962", "963",
        "964", "965", "966", "967", "968", "969",
        "977", "978", "980", "981", "982", "983", "984", "985", "986",
        "988", "989", "990", "991", "992", "993", "994", "995", "996", "999",
    ])
    num = "".join(str(random.randint(0, 9)) for _ in range(7))
    return f"+7{prefix}{num}"


def random_account() -> str:
    """Generate a valid Alfa-Bank 20-digit account with correct control key.

    Genuine Alfa-Bank SBP receipts consistently use branch code 8048
    (main Moscow office of Alfa-Bank).  Random branch codes are easily
    distinguishable from real Alfa-Bank accounts.
    """
    from patch_account_last4 import _check_account
    bik = "044525593"  # Alfa-Bank Moscow
    # Fixed branch: 8048 (confirmed from all genuine Alfa-Bank receipts)
    # Account structure: 40817810 [key] 8048 [7 random digits]
    branch = "8048"
    tail7 = "".join(str(random.randint(0, 9)) for _ in range(7))
    prefix8 = "40817810"
    for key in range(10):
        candidate = prefix8 + str(key) + branch + tail7
        if _check_account(bik, candidate):
            return candidate
    return "40817810980480002476"


def random_account_with_last4(last4: str) -> str:
    """Generate a valid Alfa-Bank 20-digit account ending in specific last 4 digits.

    Account structure: 40817810 [key] 8048 [3 random digits] [last4]
    Total: 8 + 1 + 4 + 3 + 4 = 20 digits.
    """
    from patch_account_last4 import _check_account
    bik = "044525593"
    last4 = last4.strip()[-4:].zfill(4)
    branch = "8048"
    prefix8 = "40817810"
    for _ in range(50):
        mid3 = "".join(str(random.randint(0, 9)) for _ in range(3))
        for key in range(10):
            candidate = prefix8 + str(key) + branch + mid3 + last4
            if _check_account(bik, candidate):
                return candidate
    # Fallback: just use random_account() if checksum can't be satisfied
    return random_account()


def generate_random_sbp(output_path: str | Path | None = None) -> tuple[bytes, str]:
    """Generate a fully random but realistic SBP receipt.

    Uses the "zero-surgery" approach: picks a donor first, then generates all
    text using only the characters already in the donor's CMap.  This keeps
    the font and CMap streams byte-identical to the genuine donor PDF.

    Returns (pdf_bytes, canonical_filename) where canonical_filename is the
    AM_{ts}.pdf name derived from the date_formed, matching genuine PDF naming.
    """
    # --- Pick a random donor and load its metadata ---
    if not SBP_DONORS_DIR.exists():
        raise FileNotFoundError(f"No SBP donor directory: {SBP_DONORS_DIR}")
    donor_candidates = [
        p for p in sorted(SBP_DONORS_DIR.glob("*.pdf"))
        if _is_genuine_pdf(p)
    ]
    if not donor_candidates:
        raise FileNotFoundError(f"No valid donor PDFs in {SBP_DONORS_DIR}")

    _PHONE_FORMAT_CPS = {ord(c) for c in "+()"}

    # Determine the target date era for donor selection.
    # We generate a date 0-3 days ago, so compute a representative target.
    _now_msk_for_sort = datetime.now(timezone.utc) + _MSK
    _target_ts = _now_msk_for_sort.timestamp()

    def _donor_date_ts(info: dict) -> float:
        """Return the Unix timestamp of the donor's operation date (for sorting)."""
        op_id = info.get("operation_id", "")
        # C16DDMMYYNNNNNNN — positions 3-8 encode DDMMYY
        if len(op_id) >= 9 and op_id[:3] == "C16":
            try:
                dd = int(op_id[3:5])
                mm = int(op_id[5:7])
                yy = int(op_id[7:9])
                yyyy = 2000 + yy
                return datetime(yyyy, mm, dd, tzinfo=timezone.utc).timestamp()
            except (ValueError, OverflowError):
                pass
        return 0.0

    # Sort: donors closest in date to the target date come first, then shuffle
    # within each bucket so we don't always pick the same file.
    def _donor_sort_key(p: Path) -> tuple:
        info = _load_donor_info(p)
        if not info:
            return (9999999999.0,)
        dist = abs(_donor_date_ts(info) - _target_ts)
        return (dist,)

    # Build sorted candidate list with a two-tier sort:
    # 1. Donors whose date is within 30 days of today (preferred)
    # 2. Remaining donors
    _THIRTY_DAYS = 30 * 86400
    preferred = [p for p in donor_candidates
                 if abs((_donor_date_ts(_load_donor_info(p) or {}) or 0) - _target_ts) <= _THIRTY_DAYS]
    fallback = [p for p in donor_candidates if p not in preferred]
    random.shuffle(preferred)
    random.shuffle(fallback)
    ordered_candidates = preferred + fallback

    # Compute minimum p23 for today's date to filter out stale donors.
    _now_for_p23 = datetime.now(timezone.utc) + _MSK
    _today_min_p23 = _min_p23_for_date(_now_for_p23.year, _now_for_p23.month, _now_for_p23.day)

    donor_info: dict | None = None
    _stale_p23_skipped: list[str] = []
    for cand in ordered_candidates:
        info = _load_donor_info(cand)
        if not info or not info["recipient"] or not info["bank"]:
            continue
        if not _PHONE_FORMAT_CPS.issubset(info["avail_cps"]):
            continue
        # Only accept donors with standard C16 operation ID prefix.
        # C42 and other non-standard prefixes indicate unusual receipt types
        # that may be flagged by external checkers when combined with C16 output.
        if not info.get("operation_id", "").startswith("C16"):
            print(f"[RANDOM SBP] Skipping {cand.name}: non-C16 op_id {info.get('operation_id', '?')[:6]}")
            continue
        # Skip donors with stale p23 — their SBP ID era is outdated.
        # _generate_sbp_id will upgrade p23 automatically, but the suffix
        # from an old-era donor may also be era-specific; prefer fresh donors.
        donor_p23_str = info.get("sbp_p23", "")
        try:
            if int(donor_p23_str) < _today_min_p23:
                _stale_p23_skipped.append(f"{cand.name}(p23={donor_p23_str})")
                continue
        except (ValueError, TypeError):
            pass
        donor_info = info
        break
    if donor_info is None:
        # Fallback: no fresh-era donor found — use any valid donor.
        # p23 will be automatically upgraded in _generate_sbp_id.
        if _stale_p23_skipped:
            print(
                f"[RANDOM SBP] WARNING: all donors have stale p23<{_today_min_p23:02d}. "
                f"Skipped: {', '.join(_stale_p23_skipped[:5])}. "
                f"Please add fresh receipts to {SBP_DONORS_DIR}."
            )
        for cand in ordered_candidates:
            info = _load_donor_info(cand)
            if not info or not info["recipient"] or not info["bank"]:
                continue
            if not _PHONE_FORMAT_CPS.issubset(info["avail_cps"]):
                continue
            if not info.get("operation_id", "").startswith("C16"):
                continue
            donor_info = info
            break
    if donor_info is None:
        raise RuntimeError("Could not load any valid donor PDF")

    print(f"[RANDOM SBP] Selected donor: {donor_info['path'].name}")
    print(f"[RANDOM SBP] Donor bank: {donor_info['bank']!r}, "
          f"recipient: {donor_info['recipient']!r}")

    # --- Generate amount ---
    amount = random.choice([
        random.randint(100, 999),
        random.randint(1000, 9999),
        random.randint(10000, 50000),
    ])
    amount = round(amount / 50) * 50 or 100

    # --- Keep donor bank (zero waste for bank chars) ---
    bank = donor_info["bank"]

    # --- Craft recipient to reuse donor's unique variable chars ---
    recipient = _craft_recipient_for_donor(donor_info)

    phone = random_phone()
    account = random_account()

    # Use today's date (0 days ago) — fresh receipts pass the age check.
    # With date-matched April 13+ donors the suffix is copied verbatim so
    # there are no new Latin characters to worry about.
    hour   = random.randint(8, 21)
    minute = random.randint(0, 59)
    second = random.randint(0, 59)

    now_msk = datetime.now(timezone.utc) + _MSK
    op_date = now_msk.strftime("%d.%m.%Y")
    op_time = f"{hour:02d}:{minute:02d}:{second:02d}"

    # Reuse the donor's exact SBP prefix letter, p23, and check character
    # so no CMap entries become unused from SBP ID changes.
    sbp_prefix = donor_info["sbp_prefix"]
    sbp_p23    = donor_info["sbp_p23"]
    sbp_check  = donor_info["sbp_check"]

    # Verify the suffix chars from the donor are all present in the donor CMap.
    donor_sbp_id   = donor_info.get("sbp_id", "")
    donor_suffix   = donor_sbp_id[16:] if len(donor_sbp_id) >= 32 else ""
    donor_avail    = donor_info["avail_cps"]
    suffix_new_chars = {c for c in (sbp_prefix + sbp_check + donor_suffix)
                        if 'A' <= c <= 'Z' and ord(c) not in donor_avail}
    if suffix_new_chars:
        print(f"[RANDOM SBP] WARNING: suffix/prefix/check chars not in donor CMap: "
              f"{sorted(suffix_new_chars)} — font surgery will be triggered")

    print(f"[RANDOM SBP] amount={amount} RUB, recipient={recipient!r}, "
          f"phone={phone}, bank={bank!r}, date={op_date} {op_time}")
    print(f"[RANDOM SBP] p23={sbp_p23}, prefix={sbp_prefix}, check={sbp_check}, "
          f"suffix={donor_suffix}")

    # Pass output_path=None so generate_sbp_receipt doesn't write yet; we
    # then write to the canonical AM_{ts}.pdf name ourselves.
    pdf_bytes, canonical_filename = generate_sbp_receipt(
        amount=amount,
        recipient=recipient,
        phone=phone,
        bank=bank,
        account=account,
        operation_date=op_date,
        operation_time=op_time,
        donor_path=donor_info["path"],
        output_path=None,
        sbp_prefix=sbp_prefix,
        sbp_p23=sbp_p23,
        sbp_check=sbp_check,
    )

    if output_path is not None:
        out = Path(output_path)
        if out.is_dir():
            out = out / canonical_filename
        else:
            # Replace any non-canonical name with the proper AM_{ts}.pdf name
            # in the same directory, so the file is named correctly.
            out = out.parent / canonical_filename
        out.write_bytes(pdf_bytes)
        print(f"[OK] Written: {out} ({len(pdf_bytes):,} bytes)")

    return pdf_bytes, canonical_filename


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate Alfa-Bank SBP receipt PDF.")
    parser.add_argument("--amount", type=int, help="Transfer amount in RUB")
    parser.add_argument("--recipient", help="Recipient name")
    parser.add_argument("--phone", help="Recipient phone")
    parser.add_argument("--bank", help="Recipient bank name")
    parser.add_argument("--operation-date", default="auto")
    parser.add_argument("--operation-time", default="auto")
    parser.add_argument("--account", default=None)
    parser.add_argument("--commission", type=int, default=0)
    parser.add_argument("--message", default="Перевод денежных средств")
    parser.add_argument("--receipt-number", default="auto")
    parser.add_argument("--donor", default=None)
    parser.add_argument("--random", action="store_true", help="Generate with random data")
    parser.add_argument("--output", "-o", default="receipt_sbp.pdf")
    args = parser.parse_args()

    try:
        if args.random:
            _, name = generate_random_sbp(output_path=Path(args.output).parent if args.output != "receipt_sbp.pdf" else Path("."))
            print(f"[DONE] Canonical filename: {name}")
        else:
            if not all([args.amount, args.recipient, args.phone, args.bank]):
                parser.error("--amount, --recipient, --phone, --bank are required (or use --random)")
            pdf_bytes, name = generate_sbp_receipt(
                amount=args.amount,
                recipient=args.recipient,
                phone=args.phone,
                bank=args.bank,
                operation_date=args.operation_date,
                operation_time=args.operation_time,
                account=args.account,
                commission=args.commission,
                message=args.message,
                receipt_number=args.receipt_number,
                donor_path=args.donor,
                output_path=args.output,
            )
            print(f"[DONE] Canonical filename: {name}")
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
