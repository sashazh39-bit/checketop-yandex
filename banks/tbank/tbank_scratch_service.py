#!/usr/bin/env python3
"""T-Bank receipt generation from scratch using a donor PDF.

Takes a template receipt PDF, replaces ALL text fields with user-provided values
while preserving images, fonts, page structure, and metadata integrity.

F2 (bold amount) field: only modified if the new text can be encoded
in the Medium font subset. Otherwise the original value is kept.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from tbank_cmap import (
    get_unsupported_chars,
    format_unsupported_error,
    extract_pdf_font_widths,
)
from tbank_check_service import (
    TEMPLATES,
    _pick_template,
    SBP_FIELDS,
    CARD_FIELDS,
    TRANSGRAN_FIELDS,
    FIELDS_BY_TYPE,
    _find_page_stream,
    _recompress_zero_delta,
    _update_keywords,
    _update_dates,
    _update_doc_id,
    _replace_all_fields_in_stream,
    _format_amount_str,
    get_renderable_chars,
)


SBP_DEFAULTS = {
    "datetime": "23.03.2026  14:31:16",
    "amount_bold": "10 ",
    "type_label": "",
    "status": "",
    "amount_small": "10 ",
    "commission": "",
    "sender": "",
    "phone": "+7 (911) 858-45-52",
    "receiver": "",
    "bank": "",
    "account": "408178108000****7645",
    "ident": "A60821131169401K0G100300117",
}

CARD_DEFAULTS = {
    "datetime": "23.03.2026  14:32:35",
    "amount_bold": "10 ",
    "status": "",
    "amount_small": "10 ",
    "sender": "",
    "card_to": "220015******2794",
}

TRANSGRAN_DEFAULTS = {
    "datetime": "23.03.2026  14:40:46",
    "amount_bold": "10 ",
    "status": "",
    "amount_small": "10 ",
    "commission": "",
    "sender": "",
    "phone": "+(992) 933-550-187",
    "receiver": "",
    "credited_amt": "1.13",
}

DEFAULTS_BY_TYPE = {
    "sbp": SBP_DEFAULTS,
    "card": CARD_DEFAULTS,
    "transgran": TRANSGRAN_DEFAULTS,
}


def get_fields_for_scratch(receipt_type: str) -> list[dict]:
    """Return field definitions with labels for the wizard UI."""
    fields = FIELDS_BY_TYPE.get(receipt_type, SBP_FIELDS)
    defaults = DEFAULTS_BY_TYPE.get(receipt_type, SBP_DEFAULTS)
    result = []
    for f in fields:
        result.append({**f, "default": defaults.get(f["key"], "")})
    return result


def validate_scratch_fields(
    values: dict[str, str], receipt_type: str
) -> Optional[str]:
    """Validate all field values can be encoded. Returns error message or None."""
    for key, val in values.items():
        if not val:
            continue
        bad = get_unsupported_chars(val, "regular")
        if bad:
            return format_unsupported_error(bad)
    return None


def generate_from_scratch(
    values: dict[str, str],
    receipt_type: str = "sbp",
    output_path: Optional[str | Path] = None,
    template_path: Optional[str | Path] = None,
) -> bytes:
    """Generate a T-Bank receipt from scratch by replacing all fields in a donor."""
    donor = Path(template_path) if template_path else _pick_template(receipt_type)
    if not donor or not donor.exists():
        raise FileNotFoundError(
            f"Template for {receipt_type} not found at {donor}"
        )

    pdf_bytes = donor.read_bytes()
    f1_widths, f2_widths = extract_pdf_font_widths(pdf_bytes)
    len_num_start, stream_start, stream_len, decompressed = _find_page_stream(
        pdf_bytes
    )

    fields = FIELDS_BY_TYPE.get(receipt_type, SBP_FIELDS)

    for key, val in values.items():
        if not val:
            continue
        bad = get_unsupported_chars(val, "regular")
        if bad:
            raise ValueError(format_unsupported_error(bad))

    f1_renderable = get_renderable_chars(pdf_bytes, "regular")
    f2_renderable = get_renderable_chars(pdf_bytes, "medium")

    new_stream = _replace_all_fields_in_stream(
        decompressed, fields, values, f1_widths, f2_widths,
        f1_renderable=f1_renderable, f2_renderable=f2_renderable,
    )

    pdf_bytes = _recompress_zero_delta(
        pdf_bytes, stream_start, stream_len, new_stream
    )
    from datetime import datetime as _dt
    now = _dt.now()
    pdf_bytes = _update_keywords(pdf_bytes, now)
    pdf_bytes = _update_dates(pdf_bytes, now)
    pdf_bytes = _update_doc_id(pdf_bytes)

    if output_path:
        Path(output_path).write_bytes(pdf_bytes)
    return pdf_bytes
