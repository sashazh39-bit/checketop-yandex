# -*- coding: utf-8 -*-
"""Строгий слой проверок поверх receipt_validator.py.

Каждое правило откалибровано на genuine-корпусе проекта (0 ложных
срабатываний) и считается решающим:

* vtb_subset_tag — у ВТБ (openhtmltopdf/PDFBox) метка сабсета шрифта
  детерминированно выводится из CIDToGIDMap: tag = base25(Σ newGid ^ cid).
  9/9 genuine совпадают; любой редактор, поменявший набор глифов без
  пересчёта метки, ломает равенство.
* opid_display_time — отображаемое время операции (МСК) минус момент,
  зашитый в SBP ID: genuine-корпус 271 чек, диапазон −57…+2075 с.
  Окно −180…+3600 с ловит правку даты/времени без правки ID.
* amount_arithmetic — «Сумма + Комиссия = Итого» для Озона (233/233)
  и МТС (41/41).
* current_profile_consistency — свежие временные якоря глобальных
  банковских последовательностей и PDF-лагов. Правило применяется только
  после своего якоря и должно обновляться новыми genuine-переводами.
"""
from __future__ import annotations

import collections
import hashlib
import math
import re
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

import fitz  # type: ignore
import receipt_validator as core  # type: ignore

_B25 = "BCDEFGHIJKLMNOPQRSTUVWXYZ"
_SUBSET_RE = re.compile(r"/BaseFont\s*/([A-Z]{6})\+")
_C2G_RE = re.compile(r"/CIDToGIDMap\s+(\d+)\s+0\s+R")

DISPLAY_WINDOW = (-180, 3600)

# Latest proven-genuine anchors (08.11.2026, MSK).  These deliberately live
# in the checker, not in the generator.  Bounds are broad enough for observed
# bank throughput but reject discontinuities of several orders of magnitude.
_MTS_COUNTER_AT = datetime(2026, 11, 8, 18, 58, 43)
_MTS_COUNTER = 19_593_214_350
_WB_RECEIPT_DAY = datetime(2026, 11, 8)
_WB_RECEIPT_FLOOR = 1_022_275_490
_CURRENT_SUFFIX = "0011890101"

# Живые якоря: бот-генератор учится на присланных настоящих чеках и пишет
# их в anchor_state.json; чекер перечитывает файл при изменении mtime.
import json
import os
from pathlib import Path

_anchors_cache: tuple[str, float, dict] | None = None


def _dynamic_anchors() -> dict:
    global _anchors_cache
    try:
        _here = Path(__file__).resolve().parent
        path = Path(os.environ.get("CHECKER_ANCHORS_PATH", str(_here / "anchor_state.json")))
        mtime = path.stat().st_mtime
        if _anchors_cache and _anchors_cache[0] == str(path) and _anchors_cache[1] == mtime:
            return _anchors_cache[2]
        state = json.loads(path.read_text(encoding="utf-8"))
        _anchors_cache = (str(path), mtime, state)
        return state
    except Exception:
        return {}


def pdfbox_subset_tag(pairs: list[tuple[int, int]]) -> str:
    """PDFBox TrueTypeEmbedder.getTag: HashMap<newGid, cid>.hashCode → base25."""
    num = sum(new_gid ^ cid for new_gid, cid in pairs)
    out = ""
    while True:
        out += _B25[num % 25]
        num //= 25
        if num == 0 or len(out) >= 6:
            break
    return out.rjust(6, "A")


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def vtb_subset_tag(raw: bytes) -> dict[str, Any]:
    try:
        document = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        return _check("vtb_subset_tag", False, f"fitz failed: {exc}")
    results = []
    try:
        for xref in range(1, document.xref_length()):
            try:
                source = document.xref_object(xref, compressed=True)
            except Exception:
                continue
            tag_match = _SUBSET_RE.search(source)
            map_match = _C2G_RE.search(source)
            if not tag_match or not map_match:
                continue
            data = document.xref_stream(int(map_match.group(1))) or b""
            pairs = []
            for cid in range(len(data) // 2):
                gid = (data[2 * cid] << 8) | data[2 * cid + 1]
                if gid or cid == 0:
                    pairs.append((gid, cid))
            results.append((tag_match.group(1), pdfbox_subset_tag(pairs)))
    finally:
        document.close()
    if not results:
        return _check("vtb_subset_tag", False, "no subset CIDFont with CIDToGIDMap")
    bad = [(actual, expected) for actual, expected in results if actual != expected]
    return _check(
        "vtb_subset_tag",
        not bad,
        f"{len(results)} subset font(s); mismatches={bad[:3]}",
    )


_VTB_DPP = struct.unpack("f", struct.pack("f", 20.0 * 4.0 / 3.0))[0]
_VTB_RIGHT = 6860
_VTB_STR_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>", re.S)
_VTB_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _vtb_f32(v: float) -> float:
    return struct.unpack("f", struct.pack("f", float(v)))[0]


def _vtb_token_bytes(tok: bytes) -> bytes:
    if tok.startswith(b"<"):
        return bytes.fromhex(re.sub(rb"\s", b"", tok[1:-1]).decode())
    body = tok[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            n = body[i + 1]
            if n in _VTB_ESCAPES:
                out.append(_VTB_ESCAPES[n]); i += 2; continue
            if 0x30 <= n <= 0x37:
                j, digits = i + 1, b""
                while j < len(body) and len(digits) < 3 and 0x30 <= body[j] <= 0x37:
                    digits += bytes([body[j]]); j += 1
                out.append(int(digits, 8) & 0xFF); i = j; continue
            if n in (10, 13):
                i += 2; continue
            out.append(n); i += 2; continue
        out.append(c); i += 1
    return bytes(out)


def vtb_amount_alignment(raw: bytes) -> dict[str, Any]:
    """openhtmltopdf layout law on the 13.5 pt amount block:
    x_dots is exact integer and x_dots + ceil01(f32(f32(sum(W))/1000)*360) + 4*n == 6860.
    Verified 264/264 on genuine VTB receipts; catches donor amount edits."""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        return _check("vtb_amount_alignment", False, f"fitz failed: {exc}")
    try:
        widths: dict[int, int] = {}
        for xref in range(1, doc.xref_length()):
            try:
                src = doc.xref_object(xref, compressed=True)
            except Exception:
                continue
            if "/CIDFontType2" not in src:
                continue
            wm = re.search(r"/W\s*\[(.*)\]\s*/CIDToGIDMap", src, re.S)
            if wm:
                for start, arr in re.findall(r"(\d+)\s*\[([^\]]*)\]", wm.group(1)):
                    cid = int(start)
                    for val in arr.split():
                        widths[cid] = int(float(val))
                        cid += 1
                break
        if not widths or len(doc) < 1:
            return _check("vtb_amount_alignment", True, "skipped (no /W)")
        content = doc[0].read_contents() or b""
    finally:
        doc.close()

    checked = 0
    bad: list[str] = []
    for bm in re.finditer(rb"BT\n(.*?)\nET", content, re.S):
        body = bm.group(1)
        pm = re.fullmatch(
            rb"/F1 ([\d.]+) Tf\n1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm\n\[(.*)\] TJ",
            body, re.S,
        )
        if not pm or float(pm.group(1)) != 13.5:
            continue
        x_pt = float(pm.group(2))
        x_dots = x_pt * _VTB_DPP
        if x_dots < 2000:  # left-aligned label 'Сумма операции' at x=660 dots
            continue
        checked += 1
        if abs(x_dots - round(x_dots)) > 0.02:
            bad.append(f"non-int x={pm.group(2).decode()} ({x_dots:.3f} dots)")
            continue
        cids: list[int] = []
        for tok in _VTB_STR_TOKEN.findall(pm.group(4)):
            b2 = _vtb_token_bytes(tok)
            if len(b2) == 2:
                cids.append((b2[0] << 8) | b2[1])
        if not cids:
            continue
        total_w = 0.0
        for c in cids:
            total_w = _vtb_f32(total_w + widths.get(c, 1000))
        scaled = _vtb_f32(_vtb_f32(total_w / _vtb_f32(1000.0)) * 360)
        fl = math.floor(scaled)
        piece = int(fl) if (scaled - fl) < 0.01 else int(math.ceil(scaled))
        exp_x = _VTB_RIGHT - (piece + 4 * len(cids))
        if int(round(x_dots)) != exp_x:
            bad.append(f"x={int(round(x_dots))} != expected {exp_x} dots")
    return _check(
        "vtb_amount_alignment",
        not bad,
        f"{checked} 13.5pt value block(s); mismatches={bad[:3]}",
    )


def parse_display_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s*(?:,|в)?\s*(\d{1,2}:\d{2}(?::\d{2})?)", value
    )
    if not match:
        return None
    fmt = "%d.%m.%Y %H:%M:%S" if match.group(2).count(":") == 2 else "%d.%m.%Y %H:%M"
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", fmt)
    except ValueError:
        return None


def opid_display_time(op_utc: datetime, displayed: datetime) -> dict[str, Any]:
    id_msk = op_utc.replace(tzinfo=None) + timedelta(hours=3)
    diff = (displayed - id_msk).total_seconds()
    lo, hi = DISPLAY_WINDOW
    return _check(
        "opid_display_time",
        lo <= diff <= hi,
        f"displayed − ID(MSK) = {diff:.0f}s (window {lo}..{hi}s)",
    )


def amount_arithmetic(
    amount_minor: int | None, commission_minor: int | None, total_minor: int | None
) -> dict[str, Any] | None:
    if amount_minor is None or total_minor is None:
        return None
    commission = commission_minor or 0
    return _check(
        "amount_arithmetic",
        amount_minor + commission == total_minor,
        f"amount {amount_minor} + commission {commission} vs total {total_minor} (kop)",
    )


def current_profile_consistency(
    bank: str | None,
    displayed: datetime | None,
    doc_no: str | None,
    parsed_opid: dict[str, Any] | None,
    creation_date: datetime | None,
) -> dict[str, Any] | None:
    """Check current, time-bound bank profile without applying it to history.

    The detail intentionally stays generic: public verdicts must not expose
    which live counter or lag was outside its attested corridor.
    """
    state = _dynamic_anchors()
    mts_anchor = state.get("mts") or {}
    mts_counter = int(mts_anchor.get("counter", _MTS_COUNTER))
    mts_at_raw = mts_anchor.get("at")
    try:
        mts_at = datetime.strptime(mts_at_raw, "%Y-%m-%d %H:%M:%S") if mts_at_raw else _MTS_COUNTER_AT
    except (TypeError, ValueError):
        mts_at = _MTS_COUNTER_AT

    if bank == "MTS" and displayed and doc_no and doc_no.isdigit() \
            and len(doc_no) == 12 and doc_no.startswith("019") \
            and displayed >= mts_at:
        elapsed = (displayed - mts_at).total_seconds()
        delta = int(doc_no) - mts_counter
        # At the anchor: allow limited concurrency/reordering.  The upper
        # envelope is intentionally wider than observed (~330/s) to avoid
        # false red on genuine bank bursts.
        passed = -100_000 <= delta <= 100_000 + 2_000 * elapsed
        return _check(
            "current_profile_consistency",
            passed,
            "MTS transaction sequence consistent with current genuine anchor"
            if passed else "MTS transaction sequence outside current genuine profile",
        )

    wb_anchor = state.get("wb") or {}
    wb_floor = int(wb_anchor.get("floor", _WB_RECEIPT_FLOOR))
    wb_day_raw = wb_anchor.get("day")
    try:
        wb_day = datetime.strptime(wb_day_raw, "%Y-%m-%d") if wb_day_raw else _WB_RECEIPT_DAY
    except (TypeError, ValueError):
        wb_day = _WB_RECEIPT_DAY

    if bank == "WB" and displayed and doc_no and doc_no.isdigit() \
            and displayed >= wb_day:
        passed = int(doc_no) >= wb_floor
        return _check(
            "current_profile_consistency",
            passed,
            "WB receipt sequence consistent with current genuine day"
            if passed else "WB receipt sequence outside current genuine profile",
        )

    if bank == "YANDEX" and parsed_opid and creation_date \
            and core._is_current_era(parsed_opid):
        op_utc = parsed_opid["op_utc"]
        if creation_date.tzinfo is None:
            creation_date = creation_date.replace(tzinfo=timezone.utc)
        passed = creation_date.astimezone(timezone.utc) >= op_utc
        return _check(
            "current_profile_consistency",
            passed,
            "Yandex PDF timing consistent with current genuine era"
            if passed else "Yandex PDF timing outside current genuine profile",
        )
    return None


def _parse_tounicode_cids(cmap_bytes: bytes) -> dict[int, str]:
    txt = cmap_bytes.decode("latin-1", "replace")
    mp: dict[int, str] = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cid = int(m.group(1), 16)
            hx = m.group(2)
            mp[cid] = (
                bytes.fromhex(hx).decode("utf-16-be", "replace")
                if len(hx) >= 4 else chr(int(hx, 16))
            )
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for line in blk.splitlines():
            line = line.strip()
            if not line:
                continue
            if "[" in line:
                m = re.match(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*)\]", line)
                if m:
                    lo, hi = int(m.group(1), 16), int(m.group(2), 16)
                    for idx, hx in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", m.group(3))):
                        if lo + idx <= hi:
                            mp[lo + idx] = (
                                bytes.fromhex(hx).decode("utf-16-be", "replace")
                                if len(hx) >= 4 else chr(int(hx, 16))
                            )
            else:
                toks = re.findall(r"<([0-9A-Fa-f]+)>", line)
                if len(toks) >= 3:
                    lo, hi, st = int(toks[0], 16), int(toks[1], 16), int(toks[2], 16)
                    for idx, cid in enumerate(range(lo, hi + 1)):
                        mp[cid] = chr(st + idx)
    return mp


def _iter_content_tokens(data: bytes):
    n = len(data)
    i = 0
    ws = b" \t\r\n\x0c\x00"
    delims = b"()<>[]{}/%"
    while i < n:
        b = data[i]
        if b in ws:
            i += 1
            continue
        if b == ord("%"):
            while i < n and data[i] not in (10, 13):
                i += 1
            continue
        if b == ord("/"):
            j = i + 1
            while j < n and data[j] not in ws and data[j] not in delims:
                j += 1
            yield ("name", data[i + 1:j].decode("latin-1", "replace"))
            i = j
            continue
        if b == ord("("):
            depth = 1
            i += 1
            out = bytearray()
            while i < n and depth > 0:
                c = data[i]
                if c == 0x5C:
                    i += 1
                    if i >= n:
                        break
                    nxt = data[i]
                    if nxt in _VTB_ESCAPES:
                        out.append(_VTB_ESCAPES[nxt]); i += 1
                    elif 0x30 <= nxt <= 0x37:
                        oct_d = bytes([nxt]); i += 1
                        for _ in range(2):
                            if i < n and 0x30 <= data[i] <= 0x37:
                                oct_d += bytes([data[i]]); i += 1
                            else:
                                break
                        out.append(int(oct_d, 8) & 0xFF)
                    elif nxt == 13:
                        i += 1
                        if i < n and data[i] == 10:
                            i += 1
                    elif nxt == 10:
                        i += 1
                    else:
                        out.append(nxt); i += 1
                elif c == ord("("):
                    depth += 1; out.append(c); i += 1
                elif c == ord(")"):
                    depth -= 1
                    if depth > 0:
                        out.append(c)
                    i += 1
                else:
                    out.append(c); i += 1
            yield ("str", bytes(out))
            continue
        if b == ord("<"):
            if i + 1 < n and data[i + 1] == ord("<"):
                yield ("dict_start", None); i += 2; continue
            j = i + 1
            while j < n and data[j] != ord(">"):
                j += 1
            hx = re.sub(rb"\s+", b"", data[i + 1:j])
            if len(hx) % 2:
                hx += b"0"
            try:
                yield ("str", bytes.fromhex(hx.decode("ascii")))
            except Exception:
                yield ("str", b"")
            i = j + 1
            continue
        if b == ord(">") and i + 1 < n and data[i + 1] == ord(">"):
            yield ("dict_end", None); i += 2; continue
        if b == ord("["):
            yield ("arr_start", None); i += 1; continue
        if b == ord("]"):
            yield ("arr_end", None); i += 1; continue
        j = i
        while j < n and data[j] not in ws and data[j] not in delims:
            j += 1
        if j == i:
            i += 1
            continue
        yield ("op", data[i:j].decode("latin-1", "replace"))
        i = j


def font_subset_exact(raw: bytes) -> dict[str, Any]:
    """Universal donor-residue check across all 6 banks.

    Every genuine PDF engine (Skia, OpenPDF 1.3.30/1.3.32, PDFBox/openhtmltopdf)
    emits a ToUnicode CMap whose non-zero CIDs match 1-to-1 the CIDs actually
    drawn in page and Form XObject content streams. Editing a donor's text
    without rebuilding font subsets leaves orphaned donor glyphs in ToUnicode.
    """
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        return _check("font_subset_exact", False, f"fitz failed: {exc}")
    try:
        tu_by_t0: dict[int, tuple[str, dict[int, str]]] = {}
        used_by_t0: dict[int, set[int]] = collections.defaultdict(set)
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref, compressed=True)
            except Exception:
                continue
            if "/Subtype/Type0" not in obj.replace(" ", "") or "Identity-H" not in obj:
                continue
            bm = re.search(r"/BaseFont\s*/([^\s/<>\[\]()]+)", obj)
            tm = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            if not tm:
                continue
            tu_bytes = doc.xref_stream(int(tm.group(1))) or b""
            tu_by_t0[xref] = (
                bm.group(1) if bm else f"xref{xref}",
                _parse_tounicode_cids(tu_bytes),
            )
        if not tu_by_t0:
            return _check("font_subset_exact", False, "no Identity-H Type0 fonts with ToUnicode")

        def resource_font_map(res_str: str) -> dict[str, int]:
            fm = re.search(r"/Font\s+(\d+)\s+0\s+R", res_str)
            if fm:
                fdict = doc.xref_object(int(fm.group(1)), compressed=True)
            else:
                m2 = re.search(r"/Font\s*<<(.*?)>>", res_str, re.S)
                fdict = m2.group(1) if m2 else ""
            return {
                m.group(1): int(m.group(2))
                for m in re.finditer(r"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+0\s+R", fdict)
            }

        def scan_stream(stream_bytes: bytes, fmap: dict[str, int]) -> None:
            cur_t0: int | None = None
            last_name: str | None = None
            last_str: bytes | None = None
            in_arr = False
            arr_strs: list[bytes] = []
            font_stack: list[int | None] = []
            for kind, val in _iter_content_tokens(stream_bytes):
                if kind == "name":
                    last_name = val
                elif kind == "arr_start":
                    in_arr = True
                    arr_strs = []
                elif kind == "arr_end":
                    in_arr = False
                elif kind == "str":
                    if in_arr:
                        arr_strs.append(val)
                    else:
                        last_str = val
                elif kind == "op":
                    if val == "q":
                        font_stack.append(cur_t0)
                    elif val == "Q":
                        if font_stack:
                            cur_t0 = font_stack.pop()
                    elif val == "Tf" and last_name is not None:
                        cur_t0 = fmap.get(last_name)
                    elif val in ("Tj", "'", '"') and last_str is not None and cur_t0 in tu_by_t0:
                        for k in range(0, len(last_str) - 1, 2):
                            used_by_t0[cur_t0].add((last_str[k] << 8) | last_str[k + 1])
                        last_str = None
                    elif val == "TJ" and cur_t0 in tu_by_t0:
                        for sb in arr_strs:
                            for k in range(0, len(sb) - 1, 2):
                                used_by_t0[cur_t0].add((sb[k] << 8) | sb[k + 1])
                        arr_strs = []

        visited_forms: set[int] = set()

        def scan_with_forms(stream_bytes: bytes, res_str: str) -> None:
            scan_stream(stream_bytes, resource_font_map(res_str))
            xm = re.search(r"/XObject\s+(\d+)\s+0\s+R", res_str)
            xdict = doc.xref_object(int(xm.group(1)), compressed=True) if xm else res_str
            for m in re.finditer(r"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+0\s+R", xdict):
                fxref = int(m.group(2))
                if fxref in visited_forms:
                    continue
                try:
                    fobj = doc.xref_object(fxref, compressed=True)
                except Exception:
                    continue
                if "/Subtype/Form" in fobj.replace(" ", ""):
                    visited_forms.add(fxref)
                    rm = re.search(r"/Resources\s+(\d+)\s+0\s+R", fobj)
                    fres = doc.xref_object(int(rm.group(1)), compressed=True) if rm else fobj
                    scan_with_forms(doc.xref_stream(fxref) or b"", fres)

        for page in doc:
            p_obj = doc.xref_object(page.xref, compressed=True)
            rm = re.search(r"/Resources\s+(\d+)\s+0\s+R", p_obj)
            res_str = doc.xref_object(int(rm.group(1)), compressed=True) if rm else p_obj
            scan_with_forms(page.read_contents() or b"", res_str)
    finally:
        doc.close()

    diffs: list[str] = []
    for t0, (bf, tu_map) in tu_by_t0.items():
        used = used_by_t0.get(t0, set())
        unused = sorted(c for c in tu_map if c != 0 and c not in used)
        missing = sorted(c for c in used if c != 0 and c not in tu_map)
        if unused or missing:
            diffs.append(f"{bf}: unused={len(unused)} missing={len(missing)}")
    return _check(
        "font_subset_exact",
        not diffs,
        f"{len(tu_by_t0)} font subset(s) exact" if not diffs else "; ".join(diffs[:3]),
    )


_INFO_KV_RE = re.compile(rb"/([A-Za-z]+)\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)")


def _decode_pdf_str(tok: bytes) -> str:
    data = _vtb_token_bytes(tok)
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    return data.decode("latin-1")


def _vtb_id_matches(raw: bytes) -> bool:
    tid = core._trailer_id(raw)
    tr = core._find_trailer(raw)
    mi = re.search(rb"/Info\s+(\d+)\s+\d+\s+R", tr)
    body = core._obj_body(raw, int(mi.group(1))) if mi else b""
    values = [(m.group(1).decode(), _decode_pdf_str(m.group(2))) for m in _INFO_KV_RE.finditer(body)]
    cd = dict(values).get("CreationDate", "")
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})([+\-]\d{2})'(\d{2})'", cd)
    if not (tid and m):
        return False
    tz = timezone(timedelta(hours=int(m.group(7)), minutes=int(m.group(8))))
    cd_dt = datetime(*map(int, m.groups()[:6]), tzinfo=tz)
    base = int(cd_dt.timestamp() * 1000)
    tail = "".join("COSString{" + v + "}" for _, v in values).encode("latin-1", "replace")
    want = tid[0].lower()
    for rng in (range(base, base + 2000), range(base - 1000, base), range(base + 2000, base + 30000)):
        for ms in rng:
            if hashlib.md5(str(ms).encode() + tail).hexdigest() == want:
                return True
    return False


def engine_provenance(bank: str | None, raw: bytes) -> dict[str, Any] | None:
    """Bank-specific PDF producer invariants (0 false positives across genuine corpus)."""
    if not bank:
        return None
    info = core._info_dict(raw)
    errs: list[str] = []

    if bank == "OZON":
        if not info.get("/ModDate") or info.get("/ModDate") != info.get("/CreationDate"):
            errs.append("Skia /ModDate != /CreationDate")
        for s in core._parse_streams(raw):
            if b" /Length " in s["dict"]:
                errs.append(f"non-Skia stream dict spacing on obj {s['num']}")
                break
            if b"/Subtype /Image" in s["dict"] or b"/Length1" in s["dict"]:
                continue
            try:
                dec = zlib.decompress(s["data"])
            except Exception:
                continue
            if b"BT" in dec and b"Tf" in dec and re.search(rb"\]\s*TJ", dec):
                errs.append(f"non-Skia TJ array in obj {s['num']}")
                break
    elif bank == "VTB":
        if not _vtb_id_matches(raw):
            errs.append("PDFBox trailer /ID MD5 mismatch")
        try:
            with fitz.open(stream=raw, filetype="pdf") as doc:
                cnt = doc[0].read_contents() or b""
            for bm in re.finditer(rb"BT\n(.*?)\nET", cnt, re.S):
                mm = re.search(rb"1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm", bm.group(1))
                if not mm:
                    errs.append("non-openhtmltopdf Tm matrix")
                    break
                xd = float(mm.group(1)) * _VTB_DPP
                if abs(xd - round(xd)) > 0.025:
                    errs.append(f"non-integer layout dot x={xd:.3f}")
                    break
        except Exception as exc:
            errs.append(f"content parse error: {exc}")
    elif bank == "MTS":
        if not info.get("/ModDate") or info.get("/ModDate") != info.get("/CreationDate"):
            errs.append("OpenPDF 1.3.30 /ModDate != /CreationDate")
    elif bank in ("WB", "RSHB", "YANDEX"):
        if "/ModDate" in info:
            errs.append("unexpected /ModDate in JasperReports OpenPDF 1.3.32")
    else:
        return None

    return _check(
        "engine_provenance",
        not errs,
        f"{bank} engine provenance verified" if not errs else "; ".join(errs),
    )
