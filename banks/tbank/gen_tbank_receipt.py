#!/usr/bin/env python3
"""T-Bank SBP receipt generator.

Takes user-supplied field values and patches a donor T-Bank PDF from the
TBANK/ folder, replacing amount, datetime, sender, recipient, phone, bank,
account, and SBP operation ID.

All coordinates are derived from the actual donor PDFs in TBANK/.
"""
from __future__ import annotations

import random
import re
import string
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
TBANK_DIR = BASE_DIR / "TBANK"

# Cache: pdf_path -> set of renderable unicode codepoints in F1 (regular) font.
_RENDERABLE_CACHE: dict[Path, set[int] | None] = {}
# Cache: pdf_path -> set of renderable unicode codepoints in F2 (medium/bold) font.
_RENDERABLE_F2_CACHE: dict[Path, set[int] | None] = {}
# Cache: pdf_path -> receipt type string ("sbp", "sbp_short", "sbp_long", ...)
_TYPE_CACHE: dict[Path, str] = {}

# '0'-'9' — digits that must be present in F2 for amount_bold to render correctly
_DIGIT_CODEPOINTS = frozenset(range(0x30, 0x3A))

# Donor types that contain bank/account/ident rows (full SBP layout)
_FULL_SBP_TYPES = frozenset({"sbp", "sbp_long"})


def _renderable_chars_for(donor_path: Path) -> set[int] | None:
    """Get renderable F1 codepoints for a donor (cached)."""
    cached = _RENDERABLE_CACHE.get(donor_path)
    if cached is not None or donor_path in _RENDERABLE_CACHE:
        return cached
    try:
        from tbank_check_service import get_renderable_chars
        chars = get_renderable_chars(donor_path.read_bytes(), "regular")
    except Exception:
        chars = None
    _RENDERABLE_CACHE[donor_path] = chars
    return chars


def _renderable_f2_chars_for(donor_path: Path) -> set[int] | None:
    """Get renderable F2 (medium/bold) codepoints for a donor (cached)."""
    if donor_path in _RENDERABLE_F2_CACHE:
        return _RENDERABLE_F2_CACHE[donor_path]
    try:
        from tbank_check_service import get_renderable_chars
        chars = get_renderable_chars(donor_path.read_bytes(), "medium")
    except Exception:
        chars = None
    _RENDERABLE_F2_CACHE[donor_path] = chars
    return chars


def _all_donors(*, integrity_only: bool = False) -> list[Path]:
    """All available T-Bank donor PDFs safe for external verification."""
    from tbank_check_service import donor_keywords_ok, _is_blocked_donor

    base: list[Path] = []
    for p in sorted(TBANK_DIR.glob("tbank_sbp_verified_*.pdf")):
        if "_enriched" in p.stem:
            continue
        if _is_blocked_donor(p) or not donor_keywords_ok(p):
            continue
        base.append(p)

    for p in sorted(TBANK_DIR.glob("tbank_sbp_*.pdf")):
        if "_enriched" in p.stem or p.name.startswith("tbank_sbp_verified_"):
            continue
        if _is_blocked_donor(p) or not donor_keywords_ok(p):
            continue
        base.append(p)

    allowed_stems = {p.stem for p in base}
    enriched = [
        p
        for p in sorted(TBANK_DIR.glob("*_enriched.pdf"))
        if p.stem.replace("_enriched", "") in allowed_stems
    ]

    seen: set[Path] = set()
    ordered: list[Path] = []
    for group in (base, enriched):
        for p in group:
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            ordered.append(p)
    return ordered


def _resolve_donor_path(donor_path: Path, *, preserve_integrity: bool = False) -> Path:
    """Prefer enriched donor variant so F2 digits render in amount_bold."""
    if "_enriched" in donor_path.stem:
        return donor_path

    enriched_path = donor_path.with_stem(donor_path.stem + "_enriched")
    if enriched_path.exists():
        return enriched_path

    try:
        from tbank_enrich_donor import enrich_pdf

        if enrich_pdf(donor_path, enriched_path):
            return enriched_path
    except Exception:
        pass

    return donor_path


def _verified_donors(candidates: list[Path]) -> list[Path]:
    """Keep only donors whose /Keywords metadata is safe for verification."""
    from tbank_check_service import donor_keywords_ok

    verified = [p for p in candidates if donor_keywords_ok(p)]
    return verified


def _donor_priority(d: Path) -> tuple[int, str]:
    """Lower tuple sorts earlier.

    Priority tiers (ascending = lower priority number = better):
      0 – verified non-enriched donors (font unchanged = cleanest)
           UUID+10817/9686 come before md5+991 within this tier
      1 – verified enriched donors (font has injected digit glyphs)
      2 – unverified non-enriched
      3 – unverified enriched / unknown
    """
    from tbank_check_service import _parse_keywords

    is_verified = d.name.startswith("tbank_sbp_verified_")
    is_enriched = "_enriched" in d.stem

    if is_enriched:
        base = 1 if is_verified else 3
    else:
        base = 0 if is_verified else 2

    # Within tier 0 prefer UUID over md5, and newer date over older
    sub = 0
    try:
        parsed = _parse_keywords(d.read_bytes())
        if parsed:
            _, hash_part, suffix = parsed
            if "-" in hash_part and suffix in ("10817", "9686"):
                sub = 0  # UUID best
            else:
                sub = 1  # md5 second
    except Exception:
        sub = 9

    # Sort newest-first within same (base, sub): negate name (~ sorts Z before A)
    name_key = ("~" + d.stem) if is_verified else d.name
    return base, sub, name_key


def _probe_padding(donor_path: Path, changes: dict[str, str]) -> int:
    """Return estimated padding bytes needed for zero-delta level-6 recompression.

    0 = perfect fit (no padding), low = good donor, high = donor will need
    large padding and likely fail external integrity checks.
    Returns 9999 on error.
    """
    import zlib
    try:
        from tbank_check_service import (
            patch_all_fields, detect_receipt_type, _find_page_stream,
        )
        rt = detect_receipt_type(donor_path)
        pdf = patch_all_fields(donor_path, changes, rt)
        _, st, slen, dec = _find_page_stream(pdf)
        target = slen
        base = dec.rstrip(b"\n\r \t") + b"\n"
        for pad_n in range(0, 9):
            for pc in (b"\n", b" "):
                data = base if pad_n == 0 else base + pc * pad_n
                for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED, zlib.Z_RLE):
                    for mem in (8, 9, 7):
                        co = zlib.compressobj(6, zlib.DEFLATED, 15, mem, strategy)
                        c = co.compress(data) + co.flush()
                        if len(c) == target:
                            return pad_n
                if pad_n == 0:
                    break
        return 9999
    except Exception:
        return 9999


def _find_donor(
    prefer_enriched: bool = False,
    text_fields: Optional[dict[str, str]] = None,
    require_full_sbp: bool = True,
    preserve_integrity: bool = False,
) -> Path:
    """Pick an SBP donor from TBANK/.

    If text_fields is provided, only donors whose F1 font can render ALL
    characters are eligible. Among eligible donors, fresh (tbank_sbp_*) are
    preferred over enriched legacy ones — fresh donors have current metadata
    and pass strict external verification more reliably.

    If require_full_sbp=True (default), only donors with full SBP layout
    (bank/account/ident rows) are considered — i.e. type "sbp" or "sbp_long".
    Donors with SBP_SHORT layout (type "sbp_short") are excluded because they
    lack bank/account rows that the user's form provides.
    """
    from tbank_check_service import detect_receipt_type as _detect_type

    def _donor_type(p: Path) -> str:
        if p not in _TYPE_CACHE:
            try:
                _TYPE_CACHE[p] = _detect_type(p)
            except Exception:
                _TYPE_CACHE[p] = "sbp"
        return _TYPE_CACHE[p]

    all_donors = _all_donors(integrity_only=preserve_integrity)
    if not all_donors:
        raise FileNotFoundError(
            "Не найдены донорские PDF для Т-Банка. "
            "Положите файлы в папку TBANK/ проекта."
        )

    safe_donors = _verified_donors(all_donors)
    if safe_donors:
        all_donors = safe_donors

    # Filter out SBP_SHORT donors when full layout is required
    if require_full_sbp:
        full_donors = [d for d in all_donors if _donor_type(d) in _FULL_SBP_TYPES]
        if full_donors:
            all_donors = full_donors

    # Characters that are NOT required to have glyphs in the F1 font.
    # '*' appears only in masked account numbers ("****1234") and is
    # ornamental — a missing glyph shows a blank box, which is acceptable.
    _F1_SKIP_CHARS = frozenset("* ")

    needed: set[int] = set()
    if text_fields:
        for val in text_fields.values():
            for ch in str(val):
                if ch not in _F1_SKIP_CHARS:
                    needed.add(ord(ch))

    if not needed:
        pool = sorted(all_donors, key=_donor_priority)
        if prefer_enriched:
            enr = [p for p in pool if "_enriched" in p.stem]
            if enr:
                return random.choice(enr)
        top_score = _donor_priority(pool[0])[0]
        best = [p for p in pool if _donor_priority(p)[0] == top_score]
        return random.choice(best)

    eligible_fresh: list[Path] = []
    eligible_enriched: list[Path] = []
    eligible_legacy: list[Path] = []
    best_partial: tuple[int, Path] | None = None

    # Build set of all donor paths for quick lookup
    all_donors_set = set(all_donors)

    for d in all_donors:
        chars = _renderable_chars_for(d)
        if chars is None:
            covered = needed
        else:
            covered = needed & chars
            if covered != needed:
                missing_count = len(needed - chars)
                if best_partial is None or missing_count < best_partial[0]:
                    best_partial = (missing_count, d)
                continue

        # F1 coverage OK — now check F2 (bold/medium) digit coverage.
        # Build set of digits actually needed for the bold amount.
        needed_digits: set[int] = set()
        if text_fields:
            for key in ("amount_bold", "amount_small"):
                for ch in str(text_fields.get(key, "")):
                    if ch.isdigit():
                        needed_digits.add(ord(ch))

        f2_chars = _renderable_f2_chars_for(d)
        if f2_chars is not None and needed_digits and not needed_digits.issubset(f2_chars):
            # F2 lacks some needed digits → swap to enriched donor.
            if "_enriched" not in d.stem:
                enriched_candidate = d.with_stem(d.stem + "_enriched")
                if enriched_candidate.exists() or enriched_candidate in all_donors_set:
                    d = enriched_candidate
                else:
                    try:
                        from tbank_enrich_donor import enrich_pdf

                        if enrich_pdf(d, enriched_candidate):
                            d = enriched_candidate
                            all_donors_set.add(enriched_candidate.resolve())
                        else:
                            continue
                    except Exception:
                        continue
            else:
                continue

        # Sanity-check: if fitz decodes any span into Latin Extended chars
        # (U+0100-U+03FF: Latin Extended-A/B, Greek, Combining Diacritics),
        # the font has wrong ToUnicode mappings that will produce garbage text.
        # We only block that specific problematic range; everything else is OK
        # (ASCII, Cyrillic, symbols like ©, №, «», —, etc. are all allowed).
        try:
            import fitz as _fitz
            _doc = _fitz.open(d)
            _ok = True
            for _blk in _doc[0].get_text("dict").get("blocks", []):
                for _ln in _blk.get("lines", []):
                    for _sp in _ln.get("spans", []):
                        for _ch in _sp.get("text", ""):
                            cp = ord(_ch)
                            # Block only Latin Extended / Greek / Combining ranges
                            # which indicate corrupted Cyrillic font mappings
                            if 0x0100 <= cp <= 0x03FF:
                                _ok = False
                                break
                        if not _ok:
                            break
                    if not _ok:
                        break
                if not _ok:
                    break
            _doc.close()
            if not _ok:
                continue  # skip this donor — bad font mappings
        except Exception:
            pass

        if "_enriched" in d.stem:
            eligible_enriched.append(d)
        elif d.name.startswith("tbank_sbp_verified_"):
            eligible_fresh.append(d)
        elif d.name.startswith("tbank_sbp_"):
            eligible_fresh.append(d)
        else:
            eligible_legacy.append(d)

    if eligible_fresh or eligible_enriched or eligible_legacy:
        pool = eligible_fresh + eligible_enriched + eligible_legacy
        top_score = min(_donor_priority(p)[0] for p in pool)
        best = [p for p in pool if _donor_priority(p)[0] == top_score]

        # In preserve_integrity mode: pick the donor that needs the LEAST
        # zero-delta padding (large padding leaves detectable trailing bytes).
        if preserve_integrity and text_fields and len(best) > 1:
            probed = [(p, _probe_padding(p, text_fields)) for p in best]
            min_pad = min(pad for _, pad in probed)
            best = [p for p, pad in probed if pad == min_pad]

        verified_best = [p for p in best if p.name.startswith("tbank_sbp_verified_")]
        if verified_best:
            return random.choice(verified_best)
        fresh_best = [
            p
            for p in best
            if p.name.startswith("tbank_sbp_")
            and not p.name.startswith("tbank_sbp_verified_")
        ]
        if fresh_best:
            return random.choice(fresh_best)
        if eligible_enriched:
            enr_best = [p for p in best if "_enriched" in p.stem]
            if enr_best:
                return random.choice(enr_best)
        if eligible_legacy:
            legacy_best = [
                p for p in best
                if p not in eligible_fresh and p not in eligible_enriched
            ]
            if legacy_best:
                return random.choice(legacy_best)
        return random.choice(best)
    if best_partial is not None:
        return best_partial[1]
    return random.choice(all_donors)


def _format_amount(amount: int) -> str:
    """Format integer amount: 15000 → '15 000 ', 20 → '20 '."""
    s = f"{amount:,}".replace(",", " ")
    return s + " "


def _auto_datetime(
    operation_date: str, operation_time: str
) -> tuple[str, str]:
    """Return (date_str, time_str), substituting 'auto' with current values."""
    now = datetime.now()
    if operation_date in ("", "auto", "авто"):
        operation_date = now.strftime("%d.%m.%Y")
    if operation_time in ("", "auto", "авто"):
        operation_time = now.strftime("%H:%M:%S")
    else:
        # Normalize single-digit hours: "1:13:23" → "01:13:23"
        _tp = operation_time.split(":")
        if len(_tp) >= 2:
            operation_time = f"{int(_tp[0]):02d}:{_tp[1]}" + (f":{_tp[2]}" if len(_tp) >= 3 else ":00")
    return operation_date, operation_time


def _gen_receipt_number() -> str:
    """Generate a receipt number like '1-119-177-831-062'."""
    parts = [str(random.randint(1, 9))] + [
        f"{random.randint(0, 999):03d}" for _ in range(4)
    ]
    return "-".join(parts)


def _gen_sbp_operation_id() -> str:
    """Generate a T-Bank style SBP operation ID like 'A61061126522550G0B100600117'."""
    chars = string.ascii_uppercase + string.digits
    return "A" + "".join(random.choices(chars, k=26))


def get_missing_chars(donor_path: Path, text_fields: dict[str, str]) -> list[str]:
    """Return list of characters in text_fields that lack glyph outlines in the donor's F1 font.

    Used to warn the user which characters will render as invisible.
    """
    from tbank_check_service import get_renderable_chars
    pdf_bytes = donor_path.read_bytes()
    f1_renderable = get_renderable_chars(pdf_bytes, "regular")
    if f1_renderable is None:
        return []
    missing: set[str] = set()
    for val in text_fields.values():
        for ch in val:
            if ch != " " and ord(ch) not in f1_renderable:
                missing.add(ch)
    return sorted(missing)


def generate_tbank_receipt(
    amount: int,
    sender_name: str,
    sender_account: str,
    recipient_name: str,
    recipient_phone: str,
    recipient_bank: str,
    operation_date: str = "auto",
    operation_time: str = "auto",
    spb_number: str = "auto",
    receipt_number: str = "auto",
    donor_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    preserve_integrity: bool = True,
) -> tuple[bytes, str]:
    """Generate a T-Bank SBP receipt PDF by patching a donor.

    When preserve_integrity=True (default), only the page content stream is
    patched with zero-delta recompression. /Keywords, dates and /ID stay
    untouched — required for external «Целостность PDF: Да» checks.

    Returns (pdf_bytes, filename).
    """
    from tbank_check_service import (
        patch_all_fields,
        detect_receipt_type,
        _update_doc_id,
        _update_dates,
        _update_keywords,
        _update_keywords_ts_only,
    )

    operation_date, operation_time = _auto_datetime(operation_date, operation_time)

    if spb_number in ("", "auto", "авто"):
        spb_number = _gen_sbp_operation_id()

    if receipt_number in ("", "auto", "авто"):
        receipt_number = _gen_receipt_number()

    # Format datetime string as shown in T-Bank receipts (double-space between date and time)
    datetime_str = f"{operation_date}  {operation_time}"

    # Amount string (bold uses same formatted value)
    amount_str = _format_amount(amount)

    # Build changes dict mapping field keys to new values
    changes: dict[str, str] = {
        "datetime": datetime_str,
        "amount_bold": amount_str,
        "amount_small": amount_str,
        "sender": sender_name,
        "phone": recipient_phone,
        "receiver": recipient_name,
        "bank": recipient_bank,
        "account": sender_account,
    }

    if donor_path is None:
        donor_path = _find_donor(
            text_fields=changes,
            preserve_integrity=preserve_integrity,
        )

    donor_path = _resolve_donor_path(
        donor_path, preserve_integrity=preserve_integrity
    )

    receipt_type = detect_receipt_type(donor_path)

    pdf_bytes = patch_all_fields(donor_path, changes, receipt_type)

    try:
        dt = datetime.strptime(
            f"{operation_date} {operation_time}", "%d.%m.%Y %H:%M:%S"
        )
    except ValueError:
        dt = datetime.now()

    if preserve_integrity:
        # Sync only the timestamp in /Keywords so it matches the content date.
        # Hash and suffix are kept from the donor — the file structure (size,
        # offsets, /ID, CreationDate) stays byte-identical to the original.
        pdf_bytes = _update_keywords_ts_only(pdf_bytes, dt)
    else:
        pdf_bytes = _update_keywords(pdf_bytes, dt)
        pdf_bytes = _update_dates(pdf_bytes, dt)
        pdf_bytes = _update_doc_id(pdf_bytes)

    # Build filename
    date_tag = operation_date.replace(".", "")
    filename = f"Tbank_SBP_{date_tag}_{amount}.pdf"

    if output_path:
        output_path = Path(output_path)
        output_path.write_bytes(pdf_bytes)

    return pdf_bytes, filename
