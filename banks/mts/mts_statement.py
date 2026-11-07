#!/usr/bin/env python3
"""Account statement (счёт-выписка) for MTS Bank / Экси-Банк.

Same producer as the SBP and card receipts (OpenPDF 1.3.30.jaspersoft.2 /
dbo-print-forms, MTSSans v1.0).  A genuine 2-page A4 donor from
``donors_statement/`` is patched field by field and re-emitted as a
single-pass PDF (one xref stream, one ``%%EOF``, zlib level 6).

Object numbers and dest count follow the chosen donor: the layout is
not redrawn, so the posted-operation count must match a real template
(0 / 2 / 3 / 5 / 20).  Public entry: ``generate_mts_statement``.
"""
from __future__ import annotations

import re
import secrets
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from mts_pdf import fmt_double, escape_string, pdf_string, compress, mangle_name_key
from mts_font import (
    EmbeddedSubset, FontPool, FontCoverageError, OtfGlyphSource,
    build_subset, build_tounicode, build_w_array,
)
from mts_receipt import (  # noqa: E402
    Receipt, ReceiptError, MSK, _unescape, _TJ_RE, _TM_RE,
)
from mts_card_receipt import CardReceipt  # noqa: E402

__all__ = [
    "Statement", "StatementError", "StatementOp",
    "generate_mts_statement", "operation_from_receipt",
    "SUPPORTED_OP_COUNTS",
]

_HERE = Path(__file__).resolve().parent
STATEMENT_DONORS_DIR = _HERE / "donors_statement"
DEFAULT_OUT_DIR = _HERE / "out"

# Printed ``(UTC)`` clock on genuine statements is 12 hours behind the
# real UTC instant of the matching receipt (BANK.md).
_STATEMENT_CLOCK_LAG = timedelta(hours=12)

_PERIOD_RE = re.compile(
    r"^Операции за период с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})$"
)
_ISSUE_RE = re.compile(r"^Счет-выписка от (\d{2}\.\d{2}\.\d{4})$")
_CARD_RE = re.compile(r"^Номер карты: \(MIR\) (\d+)\*(\d+)$")
_ACCOUNT_RE = re.compile(r"^Номер счета: (\d{20})$")
_BALANCE_RE = re.compile(
    r"^Доступный остаток\* на (\d{2}\.\d{2}\.\d{4}): ([0-9.]+) RUR$"
)
_BLOCKED_HDR_RE = re.compile(
    r"^Заблокированные средства на (\d{2}\.\d{2}\.\d{4}):$"
)
_DT_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2}:\d{2}) \(UTC\)$")
_POSTED_AMT_RE = re.compile(r"^([+-])(\d+)\.(\d{2}) RUR$")
_BLOCKED_AMT_RE = re.compile(r"^(\d+)\.(\d{2}) RUR$")
_TOTAL_AMT_RE = re.compile(r"^([+-]) (\d+)\.(\d+) RUR$")
_ROW_NUM_RE = re.compile(r"^\d{1,2}$")

SUPPORTED_OP_COUNTS = (0, 2, 3, 5, 20)

_POSTED_TEMPLATES = {
    2: "Unknown (донор выписка мтс 3).pdf",
    3: "Unknown (донор выписка мтс) 2.pdf",
    5: "Unknown (донор выписка мтс).pdf",
    20: "Unknown (выписка донор мтс 7).pdf",
}

# Concatenated / HTML dest keys that Jasper stores instead of a run.
_DATE_HDR_DEST = "Дата совершения операции по карте"
_SUM_HDR_DEST = "Сумма операции "


class StatementError(ReceiptError):
    """Fatal problem with a statement donor or the requested fields."""


def _parse_objects(raw: bytes):
    objects: Dict[int, bytes] = {}
    stream_data: Dict[int, bytes] = {}
    physical: List[int] = []
    for m in re.finditer(rb"(?m)^(\d+) 0 obj\r?$", raw):
        num = int(m.group(1))
        end = raw.index(b"endobj\n", m.start()) + len(b"endobj\n")
        objects[num] = raw[m.start():end]
        physical.append(num)
        sidx = raw.find(b"stream\n", m.start(), end)
        if sidx != -1:
            send = raw.index(b"endstream", sidx)
            comp = raw[sidx + 7:send - 1]
            if b"/FlateDecode" in objects[num]:
                stream_data[num] = zlib.decompress(comp)
            else:
                stream_data[num] = comp
    return objects, stream_data, physical


def _parse_objstm(objects: Dict[int, bytes], stream_data: Dict[int, bytes]):
    for num, blob in objects.items():
        if b"/Type/ObjStm" not in blob:
            continue
        first = int(re.search(rb"/First (\d+)", blob).group(1))
        n = int(re.search(rb"/N (\d+)", blob).group(1))
        data = stream_data[num]
        index = data[:first].split()
        pairs = [(int(index[i]), int(index[i + 1])) for i in range(0, len(index), 2)]
        byoff = sorted(pairs, key=lambda p: p[1])
        members: Dict[int, bytes] = {}
        for i, (onum, off) in enumerate(byoff):
            stop = byoff[i + 1][1] if i + 1 < len(byoff) else len(data) - first
            members[onum] = data[first + off:first + stop].rstrip(b" ")
        return num, members, [p[0] for p in byoff], n
    raise StatementError("no ObjStm (not an MTS statement donor?)")


def _parse_pdf_names(body: bytes) -> List[bytes]:
    i = body.index(b"/Names[") + 7
    keys: List[bytes] = []
    while i < len(body) and body[i:i + 1] != b"]":
        if body[i:i + 1] == b"(":
            i += 1
            raw = bytearray()
            while i < len(body):
                if body[i] == 0x5C and i + 1 < len(body):
                    raw.append(0x5C)
                    raw.append(body[i + 1])
                    i += 2
                    continue
                if body[i] == 0x29:
                    i += 1
                    break
                raw.append(body[i])
                i += 1
            keys.append(_unescape(bytes(raw)))
        else:
            i += 1
    return keys


def _parse_xyz(body: bytes) -> Tuple[int, str, str]:
    m = re.match(
        rb"\[(\d+) 0 R/XYZ ([0-9.-]+) ([0-9.-]+) 0\]", body
    )
    if not m:
        raise StatementError(f"bad dest body {body!r}")
    return int(m.group(1)), m.group(2).decode("latin1"), m.group(3).decode("latin1")


@dataclass
class StatementRun:
    page_obj: int
    content_num: int
    font_res: str
    family: Optional[str]
    size: float
    x: str
    y: str
    raw: bytes
    fixed_a: bytes
    fixed_b: bytes
    text: str
    desired_text: Optional[str] = None
    dest_index: Optional[int] = None  # index into Statement._dests

    def __post_init__(self):
        if self.desired_text is None:
            self.desired_text = self.text

    @property
    def x_f(self) -> float:
        return float(self.x)

    @property
    def y_f(self) -> float:
        return float(self.y)


@dataclass
class StatementOp:
    kind: str  # "posted" | "blocked"
    index_run: StatementRun
    datetime_run: StatementRun
    amount_run: StatementRun
    desc_run: StatementRun
    page: int = 1


@dataclass
class _Dest:
    key: str                 # unicode (Java sort key)
    body: bytes              # ``[P 0 R/XYZ x y 0]``
    obj_num: int
    bound_run: Optional[StatementRun] = None


class Statement:
    """A genuine MTS statement donor opened for field-level patching."""

    def __init__(self, path: Union[str, Path],
                 pool: Optional[Dict[str, FontPool]] = None):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self.objects, self.stream_data, self.physical = _parse_objects(self.raw)
        (self.objstm_num, self.objstm_members,
         self.objstm_order, self.objstm_n) = _parse_objstm(
            self.objects, self.stream_data)
        self._parse_structure()
        self._parse_fonts()
        self.pool = pool or {}
        for family, subset in self.subsets.items():
            self.pool.setdefault(family, FontPool()).absorb(subset)
        self._parse_content()
        self._parse_dests()
        self._parse_info()
        self._classify_runs()

    # ------------------------------------------------------------------
    # structure
    # ------------------------------------------------------------------

    def _parse_structure(self) -> None:
        self.page_objs: List[int] = []
        self.content_nums: List[int] = []
        self.page_blobs: Dict[int, bytes] = {}
        for num, body in self.objstm_members.items():
            if b"/Type/Page" in body and b"/Contents" in body:
                content = int(re.search(rb"/Contents (\d+)", body).group(1))
                self.page_objs.append(num)
                self.content_nums.append(content)
                self.page_blobs[num] = body
        self.page_objs.sort()
        # keep contents in the same page-object order
        ordered = []
        for p in self.page_objs:
            c = int(re.search(rb"/Contents (\d+)", self.page_blobs[p]).group(1))
            ordered.append(c)
        self.content_nums = ordered
        if len(self.page_objs) != 2:
            raise StatementError(
                f"{self.path.name}: expected 2 pages, found {len(self.page_objs)}"
            )
        pages_num = None
        for num, body in self.objstm_members.items():
            if b"/Type/Pages" in body and b"/Kids" in body:
                pages_num = num
                break
        if pages_num is None:
            raise StatementError(f"{self.path.name}: no /Pages")
        self.pages_num = pages_num
        names_num = dests_wrap = None
        for num, body in self.objstm_members.items():
            if body.startswith(b"<</Names["):
                names_num = num
            elif body.startswith(b"<</Dests "):
                dests_wrap = num
        if names_num is None or dests_wrap is None:
            raise StatementError(f"{self.path.name}: /Names tree missing")
        self.names_num = names_num
        self.dests_wrap_num = dests_wrap
        self.catalog_num = next(
            n for n, b in self.objects.items() if b"/Type/Catalog" in b
        )
        self.info_num = next(
            n for n, b in self.objects.items() if b"/Author(MTS Bank)" in b
        )
        self.xref_num = next(
            n for n, b in self.objects.items() if b"/Type/XRef" in b
        )
        self.size = int(re.search(rb"/Size (\d+)", self.objects[self.xref_num]).group(1))
        first = re.search(rb"(?m)^\d+ 0 obj\r?$", self.raw)
        self.header = self.raw[: first.start()]

    def _parse_fonts(self) -> None:
        self.subsets: Dict[str, EmbeddedSubset] = {}
        self.font_type0: Dict[str, int] = {}
        self.font_fd: Dict[str, int] = {}
        self.font_cid: Dict[str, int] = {}
        self.font_file: Dict[str, int] = {}
        self.font_tounicode: Dict[str, int] = {}
        self.res_to_family: Dict[str, str] = {}
        type0_family: Dict[int, str] = {}
        for num, body in self.objstm_members.items():
            m = re.search(rb"/BaseFont/([A-Z]{6})\+(MTSSans-(Regular|Medium|Bold))", body)
            if not m or b"/Subtype/Type0" not in body:
                continue
            family = m.group(3).decode().lower()
            tu = int(re.search(rb"/ToUnicode (\d+)", body).group(1))
            cid = int(re.search(rb"/DescendantFonts\[(\d+)", body).group(1))
            cid_body = self.objstm_members[cid]
            fd = int(re.search(rb"/FontDescriptor (\d+)", cid_body).group(1))
            fd_body = self.objstm_members[fd]
            ff = int(re.search(rb"/FontFile2 (\d+)", fd_body).group(1))
            self.font_type0[family] = num
            self.font_fd[family] = fd
            self.font_cid[family] = cid
            self.font_file[family] = ff
            self.font_tounicode[family] = tu
            type0_family[num] = family
            self.subsets[family] = EmbeddedSubset(
                self.stream_data[ff],
                self.stream_data[tu].decode("latin1"),
            )
        if set(self.subsets) != {"regular", "medium", "bold"}:
            raise StatementError(
                f"{self.path.name}: expected Regular/Medium/Bold, got "
                f"{sorted(self.subsets)}"
            )
        for pobj in self.page_objs:
            blob = self.page_blobs[pobj]
            for res, ref in re.findall(rb"/(F\d) (\d+) 0 R", blob):
                fam = type0_family.get(int(ref))
                if fam:
                    self.res_to_family[res.decode()] = fam
        self._decoders: Dict[str, Dict[int, str]] = {}
        for res, fam in self.res_to_family.items():
            inv = {gid: ch for ch, gid in self.subsets[fam].char_to_gid.items()}
            self._decoders[res] = inv

    def _parse_content(self) -> None:
        self.runs: List[StatementRun] = []
        self.content_tails: Dict[int, bytes] = {}
        self.content_pieces: Dict[int, List[StatementRun]] = {}
        for pobj, cnum in zip(self.page_objs, self.content_nums):
            content = self.stream_data[cnum]
            matches = list(_TJ_RE.finditer(content))
            prev_end = 0
            font_res, size = "F1", 10.0
            page_runs: List[StatementRun] = []
            for m in matches:
                seg = content[prev_end:m.start()]
                for fm in re.finditer(rb"/(F\d) ([\d.]+) Tf", seg):
                    font_res = fm.group(1).decode()
                    size = float(fm.group(2))
                tm = None
                for tm_m in _TM_RE.finditer(seg):
                    tm = tm_m
                if tm is None:
                    run = StatementRun(
                        page_obj=pobj, content_num=cnum, font_res=font_res,
                        family=self.res_to_family.get(font_res),
                        size=size, x="", y="", raw=m.group(1),
                        fixed_a=seg, fixed_b=b"", text="",
                    )
                else:
                    run = StatementRun(
                        page_obj=pobj, content_num=cnum, font_res=font_res,
                        family=self.res_to_family.get(font_res),
                        size=size,
                        x=tm.group(1).decode("latin1"),
                        y=tm.group(2).decode("latin1"),
                        raw=m.group(1),
                        fixed_a=seg[: tm.start(1)],
                        fixed_b=seg[tm.end(1):],
                        text="",
                    )
                run.text = self._decode_run(run)
                run.desired_text = run.text
                page_runs.append(run)
                self.runs.append(run)
                prev_end = m.end()
            self.content_tails[cnum] = content[prev_end:]
            self.content_pieces[cnum] = page_runs

    def _decode_run(self, run: StatementRun) -> str:
        data = _unescape(run.raw)
        table = self._decoders.get(run.font_res, {})
        chars = []
        step = 2 if run.family else 1
        if not run.family:
            # Helvetica (WinAnsi) — used only for the empty page-anchor run
            return data.decode("latin1")
        for i in range(0, len(data), 2):
            cid = int.from_bytes(data[i:i + 2], "big")
            chars.append(table.get(cid, ""))
        return "".join(chars)

    def _parse_dests(self) -> None:
        names_body = self.objstm_members[self.names_num]
        mangled_keys = _parse_pdf_names(names_body)
        dest_nums = [
            n for n in self.objstm_order
            if n in self.objstm_members
            and self.objstm_members[n].startswith(b"[")
            and b"/XYZ" in self.objstm_members[n]
        ]
        if len(dest_nums) != len(mangled_keys):
            raise StatementError(
                f"{self.path.name}: dest/key count {len(dest_nums)}/"
                f"{len(mangled_keys)}"
            )
        self.dest_start = min(dest_nums) if dest_nums else 0
        candidates = self._static_key_candidates()
        self._dests: List[_Dest] = []
        used_runs = set()
        for i, (obj_num, mk) in enumerate(zip(dest_nums, mangled_keys)):
            body = self.objstm_members[obj_num]
            key = None
            bound = None
            for run in self.runs:
                if id(run) in used_runs or not run.text:
                    continue
                if mangle_name_key(run.text) == mk:
                    key = run.text
                    bound = run
                    run.dest_index = i
                    used_runs.add(id(run))
                    break
            if key is None:
                for cand in candidates:
                    if mangle_name_key(cand) == mk:
                        key = cand
                        break
            if key is None:
                raise StatementError(
                    f"{self.path.name}: cannot recover dest key {mk[:60]!r}"
                )
            self._dests.append(_Dest(key=key, body=body, obj_num=obj_num,
                                     bound_run=bound))

    def _static_key_candidates(self) -> List[str]:
        texts = [r.text for r in self.runs if r.text]
        addr = next((t for t in texts if t.startswith("www.exibank.ru")), "")
        phone = next((t for t in texts if t.startswith("8 800")), "")
        rus = next((t for t in texts if t.startswith("Российская")), "")
        lic = next((t for t in texts if t.startswith("Базовая")), "")
        disc = [t for t in texts if t.startswith("При несогласии")
                or t.startswith("с письменным")
                or t.startswith("следующего")
                or t.startswith("Банка о")]
        sig1 = next((t for t in texts if t.startswith("С уважением")), "")
        sig2 = next((t for t in texts if t.startswith("Служба поддержки")), "")
        sig3 = next((t for t in texts if "ЭКСИ-БАНК" in t), "")
        out = [
            "JR_PAGE_ANCHOR_0_1",
            "JR_PAGE_ANCHOR_0_2",
            _DATE_HDR_DEST,
            _SUM_HDR_DEST,
        ]
        if addr and phone and rus and lic:
            out.append(" \n(".join([addr, phone, rus, lic]))
        if sig1 and sig2 and sig3:
            out.append(f"{sig1} \n{sig2}\n{sig3.replace(chr(34), '&quot;')}")
        if len(disc) == 4:
            out.append(" ".join(disc))
        return out

    def _parse_info(self) -> None:
        blob = self.objects[self.info_num]
        dm = re.search(rb"/CreationDate\(([^)]*)\)", blob)
        sm = re.search(rb"/Subject\(CorrelationId:([0-9a-f]{16})\)", blob)
        self.donor_creation_date = dm.group(1).decode() if dm else None
        self.donor_correlation_id = sm.group(1).decode() if sm else None
        im = re.search(rb"/ID \[<([0-9a-f]{32})><([0-9a-f]{32})>\]", self.raw)
        self.donor_file_id = (im.group(1).decode(), im.group(2).decode()) if im else None

    def _donor_tag(self, family: str) -> bytes:
        body = self.objstm_members[self.font_fd[family]]
        m = re.search(rb"/FontName/([A-Z]{6})\+", body)
        if not m:
            raise StatementError(f"subset tag not found ({family})")
        return m.group(1)

    # ------------------------------------------------------------------
    # fields / operations
    # ------------------------------------------------------------------

    def _run_eq(self, pred) -> Optional[StatementRun]:
        for run in self.runs:
            if pred(run.text):
                return run
        return None

    def _classify_runs(self) -> None:
        self.field_runs: Dict[str, StatementRun] = {}
        holder = self._run_eq(lambda t: bool(re.fullmatch(
            r"[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+", t)))
        if holder:
            self.field_runs["holder"] = holder
        period = self._run_eq(lambda t: bool(_PERIOD_RE.match(t)))
        if period:
            self.field_runs["period"] = period
        issue = self._run_eq(lambda t: bool(_ISSUE_RE.match(t)))
        if issue:
            self.field_runs["issue"] = issue
        card = self._run_eq(lambda t: t.startswith("Номер карты:"))
        if card:
            self.field_runs["card"] = card
        acc = self._run_eq(lambda t: t.startswith("Номер счета:"))
        if acc:
            self.field_runs["account"] = acc
        bal = self._run_eq(lambda t: t.startswith("Доступный остаток* на"))
        if bal:
            self.field_runs["balance"] = bal
        inc = self._run_eq(lambda t: bool(_TOTAL_AMT_RE.match(t) and t.startswith("+")))
        exp = self._run_eq(lambda t: bool(_TOTAL_AMT_RE.match(t) and t.startswith("-")))
        if inc:
            self.field_runs["income_total"] = inc
        if exp:
            self.field_runs["expense_total"] = exp
        blocked_hdr = self._run_eq(lambda t: bool(_BLOCKED_HDR_RE.match(t)))
        if blocked_hdr:
            self.field_runs["blocked_header"] = blocked_hdr

        self.operations: List[StatementOp] = []
        self.blocked: List[StatementOp] = []
        # table rows: datetime run + neighbours on the same baseline
        by_key: Dict[Tuple[int, str], Dict[str, StatementRun]] = {}
        for run in self.runs:
            if not _DT_RE.match(run.text):
                continue
            key = (run.page_obj, run.y)
            group = {"datetime": run}
            for other in self.runs:
                if other.page_obj != run.page_obj or other.y != run.y:
                    continue
                if other is run:
                    continue
                if _POSTED_AMT_RE.match(other.text) or (
                        _BLOCKED_AMT_RE.match(other.text)
                        and not _TOTAL_AMT_RE.match(other.text)
                        and other.x_f > 150):
                    group["amount"] = other
                elif _ROW_NUM_RE.match(other.text) and other.x_f < 70:
                    group["index"] = other
                elif other.x_f >= 300 and other.family and other.size == 10:
                    group["desc"] = other
            by_key[key] = group
        rows = []
        for key, g in by_key.items():
            if not all(k in g for k in ("datetime", "amount", "desc", "index")):
                continue
            amt = g["amount"].text
            kind = "posted" if _POSTED_AMT_RE.match(amt) else "blocked"
            op = StatementOp(
                kind=kind,
                index_run=g["index"],
                datetime_run=g["datetime"],
                amount_run=g["amount"],
                desc_run=g["desc"],
                page=1 if g["datetime"].page_obj == self.page_objs[0] else 2,
            )
            rows.append(op)
        rows.sort(key=lambda o: (o.page, -o.datetime_run.y_f))
        # donor order is newest-first; keep stream/y order (already newest first
        # on genuine files because y decreases down the page).
        for op in rows:
            if op.kind == "posted":
                self.operations.append(op)
            else:
                self.blocked.append(op)

    @property
    def fields(self) -> Dict[str, str]:
        return {k: r.text for k, r in self.field_runs.items()}

    def set_run(self, run: StatementRun, text: str) -> None:
        run.desired_text = str(text)

    def set_field(self, name: str, text: str) -> None:
        if name not in self.field_runs:
            raise StatementError(f"statement has no field {name!r}")
        self.field_runs[name].desired_text = str(text)

    # ------------------------------------------------------------------
    # encode / save
    # ------------------------------------------------------------------

    def _encode(self, family: str, text: str) -> bytes:
        pool = self.pool[family]
        out = bytearray()
        for char in text:
            gid = pool.gid_of(char)
            if gid is None or gid not in pool.known_gids:
                raise FontCoverageError(
                    f"no glyph for {char!r} in the {family} statement pool"
                )
            out += gid.to_bytes(2, "big")
        return bytes(out)

    def _check_coverage(self) -> None:
        missing: Dict[str, List[str]] = {}
        for run in self.runs:
            if not run.family:
                continue
            for char in run.desired_text or "":
                if not self.pool[run.family].has_char(char):
                    missing.setdefault(run.family, []).append(char)
        if missing:
            detail = "; ".join(
                f"{fam}: {''.join(sorted(set(chars)))}"
                for fam, chars in missing.items()
            )
            raise FontCoverageError(
                f"glyphs outside the statement donor pool ({detail}); add a "
                "genuine MTS statement containing these characters to "
                "banks/mts/donors_statement/"
            )

    def _build_content(self, content_num: int) -> bytes:
        out = bytearray()
        for run in self.content_pieces[content_num]:
            out += run.fixed_a
            if not run.x:
                out += b"(" + run.raw + b")Tj"
                continue
            text = run.desired_text if run.desired_text is not None else run.text
            if text == run.text:
                out += run.x.encode("latin1")
                out += run.fixed_b + b"(" + run.raw + b")Tj"
                continue
            if not run.family:
                body = text.encode("latin1")
            else:
                body = self._encode(run.family, text)
            out += run.x.encode("latin1")
            out += run.fixed_b + b"(" + escape_string(body) + b")Tj"
        out += self.content_tails[content_num]
        return bytes(out)

    def _build_dests(self) -> List[_Dest]:
        entries: List[_Dest] = []
        for dest in self._dests:
            key = dest.key
            body = dest.body
            if dest.bound_run is not None:
                run = dest.bound_run
                key = run.desired_text if run.desired_text is not None else run.text
            entries.append(_Dest(key=key, body=body, obj_num=dest.obj_num,
                                 bound_run=dest.bound_run))
        keys = [e.key for e in entries]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise StatementError(
                f"duplicate /Names keys would make an invalid name tree: {dupes!r}"
            )
        entries.sort(key=lambda e: e.key)
        if len(entries) != len(self._dests):
            raise StatementError("dest count changed; pick another donor template")
        return entries

    def _build_objstm(self, dests: List[_Dest],
                      tags: Dict[str, bytes],
                      widths: Dict[str, bytes]) -> bytes:
        dest_by_new = {
            self.dest_start + i: dests[i] for i in range(len(dests))
        }
        members: List[Tuple[int, bytes]] = []
        for num in self.objstm_order:
            body = self.objstm_members[num]
            if num in dest_by_new:
                members.append((num, dest_by_new[num].body))
                continue
            family = None
            for fam, fd in self.font_fd.items():
                if num == fd:
                    family = fam
                    old = self._donor_tag(fam) + b"+MTSSans-" + fam.capitalize().encode()
                    new = tags[fam] + b"+MTSSans-" + fam.capitalize().encode()
                    members.append((num, body.replace(old, new)))
                    break
                if num == self.font_cid[fam]:
                    family = fam
                    tag = tags[fam]
                    # BaseFont + /W
                    body = re.sub(
                        rb"/BaseFont/[A-Z]{6}\+MTSSans-" + fam.capitalize().encode(),
                        b"/BaseFont/" + tag + b"+MTSSans-" + fam.capitalize().encode(),
                        body,
                    )
                    m = re.search(rb"/W \[", body)
                    if not m:
                        raise StatementError(f"CIDFont {fam} has no /W")
                    depth, i = 1, m.end()
                    while i < len(body) and depth:
                        if body[i:i + 1] == b"[":
                            depth += 1
                        elif body[i:i + 1] == b"]":
                            depth -= 1
                        i += 1
                    body = body[: m.start()] + b"/W " + widths[fam] + body[i:]
                    members.append((num, body))
                    break
                if num == self.font_type0[fam]:
                    tag = tags[fam]
                    body = re.sub(
                        rb"/BaseFont/[A-Z]{6}\+MTSSans-" + fam.capitalize().encode(),
                        b"/BaseFont/" + tag + b"+MTSSans-" + fam.capitalize().encode(),
                        body,
                    )
                    members.append((num, body))
                    break
            else:
                if num == self.names_num:
                    names = bytearray(b"<</Names[")
                    for i, dest in enumerate(dests):
                        names += pdf_string(mangle_name_key(dest.key))
                        names += f" {self.dest_start + i} 0 R".encode("latin1")
                    names += b"]>>"
                    members.append((num, bytes(names)))
                else:
                    members.append((num, body))
        index = bytearray()
        bodies = bytearray()
        for num, body in members:
            index += f"{num} {len(bodies)} ".encode("latin1")
            bodies += body + b" "
        payload = bytes(index) + bytes(bodies)
        head = (b"<</Filter/FlateDecode/Type/ObjStm/Length %d/First %d/N %d>>"
                % (len(compress(payload)), len(index), len(members)))
        return head + b"stream\n" + compress(payload) + b"\nendstream"

    def _metrics(self, family: str, text: str) -> List[Tuple[int, int, int]]:
        pool = self.pool[family]
        seen: Dict[int, Tuple[int, int, int]] = {}
        for char in text:
            gid = pool.gid_of(char)
            seen.setdefault(gid, (gid, pool.width_of_gid(gid), ord(char)))
        return sorted(seen.values())

    def save(self, output: Union[str, Path], *,
             creation_date: Optional[str] = None,
             correlation_id: Optional[str] = None,
             file_id: Optional[Tuple[str, str]] = None,
             subset_tags: Optional[Dict[str, str]] = None) -> Path:
        self._check_coverage()
        creation_date = creation_date or datetime.now(timezone.utc).strftime(
            "D:%Y%m%d%H%M%SZ")
        correlation_id = correlation_id or secrets.token_hex(8)
        file_id = file_id or (secrets.token_hex(16), secrets.token_hex(16))
        if subset_tags is None:
            subset_tags = {
                fam: "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                             for _ in range(6))
                for fam in ("regular", "medium", "bold")
            }
        tags = {fam: subset_tags[fam].encode("latin1")
                for fam in ("regular", "medium", "bold")}

        texts = {"regular": "", "medium": "", "bold": ""}
        for run in self.runs:
            if run.family:
                texts[run.family] += run.desired_text or ""
        fonts: Dict[str, bytes] = {}
        tounis: Dict[str, bytes] = {}
        widths: Dict[str, bytes] = {}
        for fam in ("regular", "medium", "bold"):
            gids = sorted({self.pool[fam].gid_of(c) for c in set(texts[fam])
                           if self.pool[fam].gid_of(c) is not None})
            fonts[fam] = build_subset(self.pool[fam], gids)
            met = self._metrics(fam, texts[fam])
            tounis[fam] = build_tounicode(met)
            widths[fam] = build_w_array(met)

        contents = {c: self._build_content(c) for c in self.content_nums}
        dests = self._build_dests()
        objstm = self._build_objstm(dests, tags, widths)

        info = (b"<</ModDate(" + creation_date.encode("latin1") +
                b")/Creator(dbo-print-forms)/CreationDate(" +
                creation_date.encode("latin1") +
                b")/Producer(OpenPDF 1.3.30.jaspersoft.2)/Subject(CorrelationId:" +
                correlation_id.encode("latin1") +
                b")/Author(MTS Bank)/Title(Printable document)>>")

        rebuilt: Dict[int, bytes] = {}
        for cnum, payload in contents.items():
            rebuilt[cnum] = (
                f"{cnum} 0 obj\n".encode()
                + b"<</Filter/FlateDecode/Length %d>>stream\n" % len(compress(payload))
                + compress(payload) + b"\nendstream\nendobj\n"
            )
        for fam in ("regular", "medium", "bold"):
            ff, tu = self.font_file[fam], self.font_tounicode[fam]
            font = fonts[fam]
            rebuilt[ff] = (
                f"{ff} 0 obj\n".encode()
                + b"<</Length1 %d/Filter/FlateDecode/Length %d>>stream\n"
                % (len(font), len(compress(font)))
                + compress(font) + b"\nendstream\nendobj\n"
            )
            rebuilt[tu] = (
                f"{tu} 0 obj\n".encode()
                + b"<</Filter/FlateDecode/Length %d>>stream\n" % len(compress(tounis[fam]))
                + compress(tounis[fam]) + b"\nendstream\nendobj\n"
            )
        rebuilt[self.info_num] = (
            f"{self.info_num} 0 obj\n".encode() + info + b"\nendobj\n"
        )
        rebuilt[self.objstm_num] = (
            f"{self.objstm_num} 0 obj\n".encode() + objstm + b"\nendobj\n"
        )

        body = bytearray(self.header)
        offsets: Dict[int, int] = {}
        for num in self.physical:
            if num == self.xref_num:
                continue
            blob = rebuilt.get(num, self.objects[num])
            offsets[num] = len(body)
            body += blob

        type2_index = {n: i for i, n in enumerate(self.objstm_order)}
        entries = bytearray()
        for num in range(0, self.size):
            if num in (0, self.xref_num):
                entries += b"\x00\x00\x00\xff\xff"
            elif num in type2_index:
                entries += (b"\x02" + self.objstm_num.to_bytes(2, "big")
                            + type2_index[num].to_bytes(2, "big"))
            else:
                entries += b"\x01" + offsets[num].to_bytes(2, "big") + b"\x00\x00"
        xref_raw = compress(bytes(entries))
        xref_dict = (
            b"<</Info %d 0 R/Filter/FlateDecode/Type/XRef/W[1 2 2]"
            b"/Index[0 %d]/ID [<%s><%s>]/Root %d 0 R/Length %d/Size %d>>"
            % (self.info_num, self.size,
               file_id[0].encode("latin1"), file_id[1].encode("latin1"),
               self.catalog_num, len(xref_raw), self.size)
        )
        startxref = len(body)
        body += (
            f"{self.xref_num} 0 obj\n".encode() + xref_dict + b"stream\n"
            + xref_raw + b"\nendstream\nendobj\nstartxref\n"
            + str(startxref).encode("latin1") + b"\n%%EOF\n"
        )
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(body)
        if (not raw.startswith(b"%PDF-")
                or raw.count(b"startxref") != 1
                or raw.count(b"%%EOF") != 1
                or b"/Prev" in raw):
            raise StatementError("natural save failed: not a single-pass PDF")
        output.write_bytes(raw)
        return output


# --------------------------------------------------------------------------
# amounts / dates / operations
# --------------------------------------------------------------------------

def parse_line_amount(text: str) -> Decimal:
    m = _POSTED_AMT_RE.match(text.strip())
    if m:
        value = Decimal(m.group(2) + "." + m.group(3))
        return value if m.group(1) == "+" else -value
    m = _BLOCKED_AMT_RE.match(text.strip())
    if m:
        return Decimal(m.group(1) + "." + m.group(2))
    raise StatementError(f"bad line amount {text!r}")


def format_line_amount(value, *, blocked: bool = False) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if blocked:
        return f"{amount:.2f} RUR"
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{abs(amount):.2f} RUR"


def format_total(value, *, sign: str) -> str:
    """Totals print as ``+ 52.0 RUR`` / ``- 0.0 RUR`` (sign is the field's)."""
    mag = abs(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if mag == mag.to_integral_value():
        body = f"{int(mag)}.0"
    else:
        body = f"{mag:.2f}".rstrip("0").rstrip(".")
        if "." not in body:
            body += ".0"
    return f"{sign} {body} RUR"


def format_balance(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f} RUR"


def _as_date(value, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(MSK).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise StatementError(f"cannot parse {name} {value!r}")


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(MSK) if value.tzinfo else value.replace(tzinfo=MSK)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 12, 0, tzinfo=MSK)
    text = str(value).strip()
    text = re.sub(r"\s*\(UTC(\+3)?\)$", "", text)
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=MSK)
        except ValueError:
            continue
    raise StatementError(f"cannot parse operation datetime {value!r}")


def print_statement_clock(op_msk: datetime) -> str:
    """Genuine DBO clock: label ``(UTC)``, value = real UTC − 12 h."""
    printed = op_msk.astimezone(timezone.utc) - _STATEMENT_CLOCK_LAG
    return printed.strftime("%d.%m.%Y %H:%M:%S (UTC)")


def format_holder(value: str) -> str:
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if len(tokens) < 2:
        raise StatementError(f"holder_name needs at least two words, got {value!r}")

    def title(token: str) -> str:
        return "-".join(part.capitalize() for part in token.split("-"))

    return " ".join(title(t) for t in tokens)


def format_card_line(value: str) -> str:
    text = str(value or "").strip()
    m = re.search(r"(\d{4,6})\D*(\d{4})\s*$", text.replace(" ", ""))
    if "*" in text:
        bits = re.findall(r"\d+", text)
        if len(bits) >= 2:
            return f"Номер карты: (MIR) {bits[0]}*{bits[-1]}"
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        return f"Номер карты: (MIR) {digits[:6]}*{digits[-4:]}"
    if len(digits) == 4:
        return f"Номер карты: (MIR) 220383*{digits}"
    raise StatementError(f"cannot parse card {value!r}")


def format_account_line(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 20:
        raise StatementError(f"account must have 20 digits, got {value!r}")
    return f"Номер счета: {digits}"


def _parse_money(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    text = re.sub(r"(?i)\s*(₽|руб\.?|rur|rub)\s*$", "", text)
    text = re.sub(r"[\s  ]", "", text).replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StatementError(f"cannot parse amount {value!r}") from None


def operation_from_receipt(path, *, kind: Optional[str] = None) -> dict:
    """One statement operation taken from a genuine MTS receipt PDF.

    SBP receipt → outgoing (or ``kind='sbp_in'``) ``{recipient} Переводы``;
    card receipt → ``Перевод с карты Переводы``.  Amounts of outgoing
    operations are negative.
    """
    path = Path(path)
    try:
        receipt = Receipt(path)
        fmt = "sbp"
    except Exception:  # noqa: BLE001
        receipt = CardReceipt(path)
        fmt = "card"
    stamp = receipt.fields["datetime"]
    dt = _as_datetime(stamp)
    amount = _parse_money(receipt.fields["amount"])
    if fmt == "card":
        return {
            "kind": "card",
            "datetime": dt,
            "amount": -abs(amount),
            "description": "Перевод с карты Переводы",
            "_receipt": str(path),
        }
    direction = kind or "sbp_out"
    if direction == "sbp_in":
        amount = abs(amount)
    else:
        amount = -abs(amount)
    return {
        "kind": direction,
        "datetime": dt,
        "amount": amount,
        "description": f"{receipt.fields['recipient']} Переводы",
        "_receipt": str(path),
    }


def _prepare_operation(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise StatementError(f"operation must be a dict, got {spec!r}")
    spec = dict(spec)
    if spec.get("receipt_path"):
        imported = operation_from_receipt(
            spec["receipt_path"], kind=spec.get("kind"))
        spec = {**imported, **{k: v for k, v in spec.items()
                               if k not in ("receipt_path",)}}
    kind = spec.get("kind", "posted")
    if kind in ("blocked",):
        dt = _as_datetime(spec.get("datetime") or spec.get("date") or spec.get("dt"))
        amount = abs(_parse_money(spec.get("amount", "0")))
        desc = spec.get("description") or "Красное&Белое Супермаркеты"
        return {"kind": "blocked", "dt": dt, "amount": amount,
                "description": desc, "_explicit": True}
    if kind not in ("sbp_out", "sbp_in", "card", "posted"):
        raise StatementError(f"unknown operation kind {kind!r}")
    dt = _as_datetime(spec.get("datetime") or spec.get("date") or spec.get("dt"))
    amount = _parse_money(spec.get("amount", "0"))
    if amount == 0:
        raise StatementError("operation amount is zero")
    if kind in ("sbp_out", "card"):
        amount = -abs(amount)
    elif kind == "sbp_in":
        amount = abs(amount)
    # kind "posted" keeps the caller's sign
    desc = spec.get("description")
    if not desc:
        if kind == "card":
            desc = "Перевод с карты Переводы"
        else:
            person = spec.get("recipient") or spec.get("person") or spec.get("name")
            if not person:
                raise StatementError(
                    "operation needs description= or recipient=/person="
                )
            from engine import _format_person  # noqa: WPS433
            desc = f"{_format_person(person)} Переводы"
    return {
        "kind": "posted",
        "dt": dt,
        "amount": amount,
        "description": desc,
        "_explicit": True,
    }


def _donor_operation_pool() -> List[dict]:
    pool: List[dict] = []
    for path in sorted(STATEMENT_DONORS_DIR.glob("*.pdf")):
        try:
            stmt = Statement(path)
        except Exception:  # noqa: BLE001
            continue
        for op in stmt.operations:
            pool.append({
                "kind": "posted",
                "amount": parse_line_amount(op.amount_run.text),
                "description": op.desc_run.text,
                "clock": op.datetime_run.text,
            })
    return pool


def _uniquify_posted(ops: List[dict], pool: List[dict], rng) -> None:
    """Keep dest keys unique among random-fill ops (explicit values stay)."""
    pool_descs = list(dict.fromkeys(o["description"] for o in pool if o.get("description")))
    used_desc, used_amt, used_clock = set(), set(), set()
    for op in ops:
        if op.get("_explicit"):
            used_desc.add(op["description"])
            used_amt.add(format_line_amount(op["amount"]))
            used_clock.add(print_statement_clock(op["dt"]))
    for op in ops:
        if op.get("_explicit"):
            continue
        desc = op["description"]
        if desc in used_desc:
            alt = [d for d in pool_descs if d not in used_desc]
            desc = rng.choice(alt) if alt else desc + "."
            op["description"] = desc
        used_desc.add(op["description"])
        amt_text = format_line_amount(op["amount"])
        nudge = Decimal("0.01") if op["amount"] >= 0 else Decimal("-0.01")
        guard = 0
        while amt_text in used_amt and guard < 10_000:
            op["amount"] += nudge
            amt_text = format_line_amount(op["amount"])
            guard += 1
        used_amt.add(amt_text)
        clock = print_statement_clock(op["dt"])
        while clock in used_clock:
            op["dt"] = op["dt"] + timedelta(seconds=1)
            clock = print_statement_clock(op["dt"])
        used_clock.add(clock)


def _pick_template(posted_count: int, account: Optional[str],
                   donor_path: Optional[Union[str, Path]]) -> Path:
    if donor_path is not None:
        path = Path(donor_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
    if posted_count not in SUPPORTED_OP_COUNTS:
        raise StatementError(
            f"no MTS statement donor for {posted_count} posted operations "
            f"(supported: {', '.join(map(str, SUPPORTED_OP_COUNTS))})"
        )
    if posted_count > 0:
        path = STATEMENT_DONORS_DIR / _POSTED_TEMPLATES[posted_count]
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
    digits = re.sub(r"\D", "", str(account or ""))
    empties = []
    for path in sorted(STATEMENT_DONORS_DIR.glob("*.pdf")):
        try:
            stmt = Statement(path)
        except Exception:  # noqa: BLE001
            continue
        if stmt.operations:
            continue
        acc_run = stmt.field_runs.get("account")
        acc = acc_run.text if acc_run else ""
        empties.append((path, acc))
    if not empties:
        raise StatementError("no empty MTS statement donor")
    if len(digits) == 20:
        for path, acc in empties:
            if digits in acc:
                return path
    return empties[0][0]


def build_statement_pool(
    extra_paths: Optional[List[Path]] = None,
) -> Dict[str, FontPool]:
    """Regular/Medium: statement TTF first, then receipts, then CFF gaps.

    Bold exists only on statements (no public Bold OTF).
    """
    from engine import (  # noqa: WPS433
        build_pool, _donor_paths, _card_donor_paths, FONTS_DIR,
    )
    pool = {"regular": FontPool(), "medium": FontPool(), "bold": FontPool()}
    for path in sorted(STATEMENT_DONORS_DIR.glob("*.pdf")):
        try:
            Statement(path, pool)
        except Exception:  # noqa: BLE001
            continue
    extra = list(_donor_paths()) + list(_card_donor_paths())
    if extra_paths:
        extra.extend(extra_paths)
    receipts = build_pool(extra, synthetic=False)
    for fam in ("regular", "medium"):
        pool[fam].fill_from(receipts[fam])
        otf = FONTS_DIR / f"MTSSans-{fam.capitalize()}.otf"
        if otf.is_file():
            pool[fam].absorb_synthetic(OtfGlyphSource(otf))
    return pool


def generate_mts_statement(
    period_start,
    period_end,
    issue_date,
    holder_name: str,
    account: Optional[str] = None,
    *,
    card: Optional[str] = None,
    opening_balance=None,
    operations: Optional[list] = None,
    operation_count: Optional[int] = None,
    receipt_paths: Optional[list] = None,
    blocked: Optional[list] = None,
    donor_path: Optional[Union[str, Path]] = None,
    output_path: Optional[Union[str, Path]] = None,
    generated_at: Optional[datetime] = None,
    random_seed: Optional[int] = None,
    fill_random: bool = True,
    pool: Optional[Dict[str, FontPool]] = None,
) -> Path:
    """Generate an MTS account statement PDF and return its path.

    Operations come from any mix of:

    * ``operations=[{{"datetime", "amount", "description"|recipient, ...}}]``
    * ``receipt_paths=[...]`` — genuine MTS SBP and/or card receipts
    * ``fill_random=True`` + ``operation_count`` — remaining slots from
      the donor operation pool

    The posted count must match a genuine template (0, 2, 3, 5 or 20).
    Templates with extra blocked rows (2 and 5 posted) keep one blocked
    row; pass ``blocked=[...]`` to replace it.
    """
    import random as _random

    rng = (_random.Random(random_seed) if random_seed is not None
           else _random.Random(secrets.randbits(64)))

    p_start = _as_date(period_start, name="period_start")
    p_end = _as_date(period_end, name="period_end")
    issue = _as_date(issue_date, name="issue_date")
    if p_end < p_start:
        raise StatementError("period_end is before period_start")
    holder = format_holder(holder_name)

    explicit: List[dict] = []
    for spec in operations or []:
        explicit.append(_prepare_operation(spec))
    for path in receipt_paths or []:
        explicit.append(_prepare_operation(operation_from_receipt(path)))

    posted_explicit = [o for o in explicit if o["kind"] == "posted"]
    blocked_explicit = [o for o in explicit if o["kind"] == "blocked"]
    for spec in blocked or []:
        blocked_explicit.append(_prepare_operation(
            {**spec, "kind": "blocked"} if spec.get("kind") != "blocked"
            else spec
        ))

    total_count = (operation_count if operation_count is not None
                   else len(posted_explicit))
    if total_count < len(posted_explicit):
        raise StatementError(
            f"operation_count={total_count} is smaller than the "
            f"{len(posted_explicit)} operations given"
        )
    fill_count = total_count - len(posted_explicit)
    if fill_count and not fill_random:
        raise StatementError(
            "operation_count exceeds given operations and fill_random=False"
        )

    donor_ops = _donor_operation_pool()
    fills: List[dict] = []
    for _ in range(fill_count):
        sample = rng.choice(donor_ops) if donor_ops else {
            "amount": Decimal("10.00"),
            "description": "Перевод СБП Переводы",
        }
        minute = rng.randrange(6 * 60, 22 * 60)
        day_span = (p_end - p_start).days
        day = p_start + timedelta(days=rng.randrange(day_span + 1) if day_span else 0)
        dt = datetime(day.year, day.month, day.day,
                      minute // 60, minute % 60,
                      rng.randrange(60), tzinfo=MSK)
        fills.append({
            "kind": "posted",
            "dt": dt,
            "amount": sample["amount"],
            "description": sample["description"],
            "_explicit": False,
        })

    all_posted = sorted(posted_explicit + fills, key=lambda o: o["dt"], reverse=True)
    _uniquify_posted(all_posted, donor_ops, rng)
    for op in all_posted:
        if not (p_start <= op["dt"].date() <= p_end):
            raise StatementError(
                f"operation {op['dt']:%d.%m.%Y} is outside the period "
                f"{p_start:%d.%m.%Y}-{p_end:%d.%m.%Y}"
            )

    template = _pick_template(len(all_posted), account, donor_path)
    pool = pool or build_statement_pool()
    stmt = Statement(template, pool)

    if len(stmt.operations) != len(all_posted):
        raise StatementError(
            f"template {template.name} has {len(stmt.operations)} posted "
            f"operations, need {len(all_posted)}"
        )

    # blocked slots
    if blocked_explicit and len(blocked_explicit) != len(stmt.blocked):
        raise StatementError(
            f"template has {len(stmt.blocked)} blocked rows, "
            f"got {len(blocked_explicit)}"
        )
    blocked_ops = list(blocked_explicit)
    if not blocked_ops:
        blocked_ops = []
        for slot in stmt.blocked:
            blocked_ops.append({
                "kind": "blocked",
                "dt": datetime(issue.year, issue.month, issue.day,
                               12, 0, 0, tzinfo=MSK),
                "amount": parse_line_amount(slot.amount_run.text)
                if _BLOCKED_AMT_RE.match(slot.amount_run.text)
                else Decimal(slot.amount_run.text.split()[0]),
                "description": slot.desc_run.text,
            })

    # header
    stmt.set_field("holder", holder)
    stmt.set_field("period",
                   f"Операции за период с {p_start:%d.%m.%Y} по {p_end:%d.%m.%Y}")
    stmt.set_field("issue", f"Счет-выписка от {issue:%d.%m.%Y}")
    if "blocked_header" in stmt.field_runs:
        stmt.set_field("blocked_header",
                       f"Заблокированные средства на {issue:%d.%m.%Y}:")

    if card is not None:
        stmt.set_field("card", format_card_line(card))
    if account is not None:
        stmt.set_field("account", format_account_line(account))

    income = sum((op["amount"] for op in all_posted if op["amount"] > 0), Decimal("0"))
    expense = -sum((op["amount"] for op in all_posted if op["amount"] < 0), Decimal("0"))
    stmt.set_field("income_total", format_total(income, sign="+"))
    stmt.set_field("expense_total", format_total(expense, sign="-"))

    if opening_balance is not None:
        bal = _parse_money(opening_balance)
        stmt.set_field(
            "balance",
            f"Доступный остаток* на {issue:%d.%m.%Y}: {format_balance(bal)}",
        )
    else:
        # keep donor remainder, retarget the date
        old = stmt.field_runs["balance"].text
        m = _BALANCE_RE.match(old)
        rest = m.group(2) if m else "0.00"
        stmt.set_field(
            "balance",
            f"Доступный остаток* на {issue:%d.%m.%Y}: {rest} RUR",
        )

    for slot, op in zip(stmt.operations, all_posted):
        if op["dt"].second == 0 and op["dt"].microsecond == 0:
            m = _DT_RE.match(slot.datetime_run.text)
            if m:
                op["dt"] = op["dt"].replace(second=int(m.group(2).split(":")[2]))
        stmt.set_run(slot.datetime_run, print_statement_clock(op["dt"]))
        stmt.set_run(slot.amount_run, format_line_amount(op["amount"]))
        stmt.set_run(slot.desc_run, op["description"])

    for slot, op in zip(stmt.blocked, blocked_ops):
        stmt.set_run(slot.datetime_run, print_statement_clock(op["dt"]))
        stmt.set_run(slot.amount_run, format_line_amount(op["amount"], blocked=True))
        stmt.set_run(slot.desc_run, op["description"])

    if output_path is None:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        output = DEFAULT_OUT_DIR / (
            f"mts_statement_{p_start:%Y%m%d}_{p_end:%Y%m%d}_{len(all_posted)}ops.pdf"
        )
    else:
        output = Path(output_path).expanduser()
    if output.resolve() == stmt.path.resolve():
        raise StatementError("output_path must not overwrite the donor")
    output.parent.mkdir(parents=True, exist_ok=True)

    gen = generated_at or datetime.now(timezone.utc)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    stmt.save(output, creation_date=gen.astimezone(timezone.utc).strftime(
        "D:%Y%m%d%H%M%SZ"))

    raw = output.read_bytes() if output.is_file() else b""
    if (not raw.startswith(b"%PDF-")
            or raw.count(b"startxref") != 1
            or raw.count(b"%%EOF") != 1
            or b"/Prev" in raw):
        output.unlink(missing_ok=True)
        raise StatementError("natural save failed: generated file is not a "
                             "single-pass PDF")
    return output
