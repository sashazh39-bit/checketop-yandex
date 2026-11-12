# -*- coding: utf-8 -*-
"""Генератор исходящей СБП-квитанции Яндекс Банка (donor-template).

Byte-faithful пересборка по шести оригиналам ``Квитанция (47..52).pdf``
(OpenPDF 1.3.32 / JasperReports Library 6.21.2): скелет документа, порядок
и тела объектов, изображения, раскладка контентного потока, xref/trailer,
формат Info CreationDate и trailer /ID берутся из донора 47 (или явно
заданного донора), а динамические поля пересобираются заново:

  Исх. № / Дата, дата-время операции, отправитель (ФИО, телефон, банк),
  получатель (ФИО, телефон, банк), сообщение, сумма, комиссия,
  код авторизации, идентификатор операции в СБП.

Шрифтовые подмножества (YSText-Regular / YSText-Medium /
YSLogotype-Regular, Type0/CIDFontType2, Identity-H) пересобираются из
глифов донора и объединённого каталога ``_yandex_catalog.pkl``:
CID == GID, /W, CIDSet и ToUnicode строятся по фактически
отрисованным символам, случайный 6-буквенный subset-tag на фейс —
как в оригиналах. Все потоки сжимаются Java Deflater level 6
(JZComp/JZCompServer), потому что именно этот диалект (``78 9c``)
даёт байт-в-байт совпадающую перепаковку потоков оригиналов;
отката к Python zlib нет — без Java генератор честно падает.

Ограничения:
  * значения таблицы переносятся по пробелам в колонку 370 pt
    (200..570 pt при 10 pt): шаг строк внутри поля 11.72 pt, строки
    и разделители ниже сдвигаются на 11.00 pt за каждую лишнюю строку
    (подтверждено одним двухстрочным сообщением широкого корпуса,
    в шести первичных оригиналах переносов нет); одно слово шире
    370 pt — ValueError;
  * каталог доноров не содержит ``Ё``, ``Ъ``, ``ъ`` (в 118 genuine
    документах их нет); они доливаются из
    ``_yandex_missing_glyphs_candidate.pkl`` (официальный ys/4 того же
    билда, побайтно сверен с genuine-перекрытиями и банковской hmtx —
    см. ``_yandex_missing_glyphs_report.md``). Без файла кандидата
    ``Ё/Ъ/ъ``, а также ``! ? ; /`` и т. п. отклоняются (см. ``check_text``);
  * разделитель тысяч в сумме — NBSP (U+00A0, корпус октябрьских
    квитанций), для сумм < 1000 в оригиналах разделителя нет;
  * узел (поз. 15) SBP ID: по 6 образцам встречаются цифры и
    заглавные латиницы, пул принят ``[0-9A-Z]``; точный момент
    флипа эры B->G неизвестен (дефолт 2026-11-06T00:00Z).

Модуль никуда не деплоится сам по себе: рядом нужны файл-донор
(см. ``_find_template``) и желательно ``_yandex_catalog.pkl``.
"""
from __future__ import annotations

import os
import re
import zlib
import pickle
import struct
import secrets
import hashlib
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

_DIR = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.environ.get("YANDEX_CATALOG") or os.path.join(_DIR, "_yandex_catalog.pkl")
# Кандидат недостающих глифов (Ё/Ъ/ъ + латиница Medium) из официального
# ys/4 того же билда, что банковские сабсеты: каждый глиф сверен по
# SHA-256 и банковской hmtx (см. _yandex_missing_glyphs_report.md).
# Файл опционален: без него поведение прежнее (непокрытые символы —
# ValueError в check_text/generate). Лицензия ys/4 (name ID 13/14,
# web-only) — открытый юридический вопрос владельца, не технический.
_MISSING_PATH = os.environ.get("YANDEX_MISSING_GLYPHS") or os.path.join(
    _DIR, "_yandex_missing_glyphs_candidate.pkl")

_PROD = "OpenPDF 1.3.32"
_CREATOR = ("JasperReports Library version 6.21.2-"
            "8434a0bd7c3bbc37cbf916f2968d35e4b165821a")
_MSK = timezone(timedelta(hours=3))
_UTC = timezone.utc

PAGE_W, PAGE_H = 595, 842
_RIGHT_EDGE = 570.0
_VALUE_LEFT = 200.0
_HEADER_LEFT = 20.0

_FACES = ("YSText-Regular", "YSText-Medium", "YSLogotype-Regular")
_RES_OF_FACE = {"YSText-Regular": "F1", "YSText-Medium": "F2", "YSLogotype-Regular": "F3"}
_FACE_OF_RES = {v: k for k, v in _RES_OF_FACE.items()}
# face -> (Type0, FontFile2, FontDescriptor, CIDFont, ToUnicode, CIDSet)
_FONT_OBJS = {
    "YSText-Regular": (2, 11, 12, 13, 14, 10),
    "YSText-Medium": (3, 16, 17, 18, 19, 15),
    "YSLogotype-Regular": (4, 21, 22, 23, 24, 20),
}
_CONTENT_OBJ = 7
_INFO_OBJ = 28

# Tj-блоки контентного потока донора (по порядку Tj-операторов),
# которые являются динамическими значениями.
_DYN_IDX = {
    2: "isx", 3: "stmt_date", 5: "op_datetime", 7: "op_type", 9: "status",
    11: "sender_bank", 13: "sender_phone", 15: "sender_name",
    17: "recipient_phone", 19: "recipient_name", 21: "recipient_bank",
    23: "message", 25: "amount", 27: "commission", 29: "auth_code",
    31: "sbp_id",
}

_DEFAULT_SENDER_NAME = "Демид Андреевич В."
_DEFAULT_SENDER_PHONE = "+7 921 102-58-53"
_DEFAULT_SENDER_BANK = "Яндекс Банк"

# Закон SBP ID (НСПК, как в receipt_validator._parse_opid):
# [0]=A/B [1]=год-2020 [2:5]=DOY(UTC) [5:11]=HHMMSS(UTC) [11:14]=мс
# [14]=flag [15]=node [16]='0' [17]=route [18:22]=gateway [22:32]=suffix
# Точный флип эры у Яндекса неизвестен: B наблюдён 04.11 06:31Z,
# G — уже 06.11 19:57Z. Дефолт — середина наблюдаемого окна
# (как в спецификации v1), переопределяется параметром era_flip_utc.
_ERA_FLIP_UTC = datetime(2026, 11, 6, 0, 0, 0, tzinfo=_UTC)
_GW_A = ("1000", "1001", "1002", "1003", "1004", "1006", "1007", "1008", "1020")
_GW_B = ("1009", "1010", "1011", "1012", "1013", "1014", "1016", "1017", "1018")
_NODE_POOL = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_AUTH_POOL = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SBP_RE = re.compile(r"^[AB][0-9][0-9]{3}[0-9]{6}[0-9]{3}[0-9][0-9A-Z]0[GB][0-9]{4}[0-9]{10}$")


# ───────────────────────── Java Deflater (JZComp) ─────────────────────────
_JZ_LOCK = threading.RLock()
_JZ_PROC = None
_JZ_DIR = None


def _find_java():
    for cand in (os.environ.get("JAVA_BIN"),
                 r"C:\Users\nemor\AppData\Local\Programs\Microsoft\jdk-17.0.17.10-hotspot\bin\java.exe",
                 shutil.which("java"),
                 r"C:\Program Files (x86)\Common Files\Oracle\Java\java8path\java.exe",
                 "/usr/bin/java"):
        if cand and (os.path.exists(cand) or shutil.which(str(cand))):
            return cand
    return None


def _find_jzdir():
    global _JZ_DIR
    if _JZ_DIR:
        return _JZ_DIR
    here = _DIR
    for d in (os.environ.get("JZCOMP_DIR"), here, os.getcwd(),
              os.path.join(here, "retranslator-mts", "pdf-service"),
              "/opt/mts-bot"):
        if d and (os.path.exists(os.path.join(d, "JZComp.class")) or
                  os.path.exists(os.path.join(d, "JZCompServer.class"))):
            _JZ_DIR = d
            return d
    return None


def _read_exact(fh, n):
    out = bytearray()
    while len(out) < n:
        chunk = fh.read(n - len(out))
        if not chunk:
            raise EOFError("JZCompServer closed stdout")
        out += chunk
    return bytes(out)


def _deflate_via_server(data: bytes) -> bytes:
    global _JZ_PROC
    jzdir = _find_jzdir()
    java = _find_java()
    if not jzdir or not java or not os.path.exists(os.path.join(jzdir, "JZCompServer.class")):
        raise RuntimeError("JZCompServer.class/java unavailable")
    with _JZ_LOCK:
        if _JZ_PROC is None or _JZ_PROC.poll() is not None:
            proc = subprocess.Popen([java, "-cp", jzdir, "JZCompServer"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            try:
                n = struct.unpack(">I", _read_exact(proc.stdout, 4))[0]
                if n != 4 or _read_exact(proc.stdout, 4) != b"JZOK":
                    raise RuntimeError("JZCompServer handshake failed")
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise
            _JZ_PROC = proc
        try:
            _JZ_PROC.stdin.write(struct.pack(">II", len(data), 6))
            _JZ_PROC.stdin.write(data)
            _JZ_PROC.stdin.flush()
            n = struct.unpack(">I", _read_exact(_JZ_PROC.stdout, 4))[0]
            return _read_exact(_JZ_PROC.stdout, n)
        except Exception:
            try:
                _JZ_PROC.kill()
            except Exception:
                pass
            _JZ_PROC = None
            raise


def _deflate_via_cli(data: bytes, tag: str) -> bytes:
    jzdir = _find_jzdir()
    java = _find_java()
    if not jzdir or not java or not os.path.exists(os.path.join(jzdir, "JZComp.class")):
        raise RuntimeError("JZComp.class/java unavailable")
    fd_in, path_in = tempfile.mkstemp(prefix=f"yr_{tag}_", suffix=".bin")
    path_out = path_in + ".out"
    try:
        with os.fdopen(fd_in, "wb") as fh:
            fh.write(data)
        subprocess.check_call([java, "-cp", jzdir, "JZComp", path_in, path_out],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(path_out, "rb") as fh:
            return fh.read()
    finally:
        for p in (path_in, path_out):
            try:
                os.unlink(p)
            except OSError:
                pass


def _deflate(data: bytes, tag: str = "s") -> bytes:
    """Java Deflater level 6; без него — RuntimeError, не zlib."""
    try:
        return _deflate_via_server(data)
    except Exception:
        pass
    try:
        return _deflate_via_cli(data, tag)
    except Exception as e:
        raise RuntimeError(
            "Java Deflater (JZComp) недоступен; отказ от Python zlib, "
            "потому что он не воспроизводит deflate-диалект оригиналов"
        ) from e


# ───────────────────────── низкоуровневый разбор PDF ─────────────────────
_OBJ_RE = re.compile(rb"(?m)^(\d+) 0 obj\r?\n")


def _obj_bodies(raw: bytes):
    objs, order = {}, []
    for m in _OBJ_RE.finditer(raw):
        num = int(m.group(1))
        start = m.end()
        end = raw.index(b"endobj", start)
        objs[num] = raw[start:end].rstrip(b"\r\n")
        order.append(num)
    return objs, order


def _stream_parts(body: bytes):
    si = body.index(b"stream")
    header = body[:si]
    s = si + 6
    if body[s:s + 2] == b"\r\n":
        s += 2
    elif body[s:s + 1] in (b"\n", b"\r"):
        s += 1
    lm = re.search(rb"/Length (\d+)", header)
    if lm:
        data = body[s:s + int(lm.group(1))]
    else:
        e = body.rindex(b"endstream")
        data = body[s:e].rstrip(b"\r\n")
    return header, data


def _stream_data(body: bytes) -> bytes:
    return _stream_parts(body)[1]


def _decompressed(body: bytes) -> bytes:
    return zlib.decompress(_stream_data(body))


def _parse_tounicode(data: bytes) -> dict:
    txt = data.decode("latin-1")
    mp = {}
    for sec in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sec):
            try:
                mp[int(a, 16)] = bytes.fromhex(b).decode("utf-16-be")
            except Exception:
                pass
    for sec in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", sec):
            lo, hi, dst = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
            if dst.startswith("["):
                vals = re.findall(r"<([0-9A-Fa-f]+)>", dst)
                for i, v in enumerate(vals):
                    if lo + i <= hi:
                        try:
                            mp[lo + i] = bytes.fromhex(v).decode("utf-16-be")
                        except Exception:
                            pass
            else:
                start = int(dst[1:-1], 16)
                for i, cid in enumerate(range(lo, hi + 1)):
                    if 0 <= start + i <= 0x10FFFF:
                        mp[cid] = chr(start + i)
    return mp


def _parse_w(body: bytes) -> dict:
    m = re.search(rb"/W\s*\[", body)
    if not m:
        return {}
    start = body.index(b"[", m.start())
    depth, end = 0, start
    for j in range(start, len(body)):
        if body[j:j + 1] == b"[":
            depth += 1
        elif body[j:j + 1] == b"]":
            depth -= 1
            if depth == 0:
                end = j
                break
    toks = re.findall(rb"\[|\]|-?\d+", body[start + 1:end])
    out, i = {}, 0
    # токены вида: c [w w ...] | c c2 w
    while i < len(toks):
        c = int(toks[i]); i += 1
        if i < len(toks) and toks[i] == b"[":
            i += 1
            widths = []
            while i < len(toks) and toks[i] != b"]":
                widths.append(int(toks[i])); i += 1
            i += 1  # ']'
            for k, w in enumerate(widths):
                out[c + k] = w
        elif i + 1 < len(toks):
            c2, w = int(toks[i]), int(toks[i + 1]); i += 2
            for cid in range(c, c2 + 1):
                out[cid] = w
    return out


def _sfnt_tables(ttf: bytes) -> dict:
    if len(ttf) < 12:
        return {}
    n = struct.unpack(">H", ttf[4:6])[0]
    tabs = {}
    for i in range(n):
        o = 12 + i * 16
        tag = ttf[o:o + 4].decode("latin-1")
        _ck, off, ln = struct.unpack(">III", ttf[o + 4:o + 16])
        tabs[tag] = ttf[off:off + ln]
    return tabs


def _glyphs_by_gid(ttf: bytes, tabs: dict) -> dict:
    if "glyf" not in tabs or "loca" not in tabs or "head" not in tabs:
        return {}
    head = tabs["head"]
    fmt = struct.unpack(">h", head[50:52])[0] if len(head) >= 54 else 0
    loca, glyf = tabs["loca"], tabs["glyf"]
    offs = []
    if fmt == 0:
        cnt = len(loca) // 2
        offs = [v * 2 for v in struct.unpack(f">{cnt}H", loca[:cnt * 2])]
    else:
        cnt = len(loca) // 4
        offs = list(struct.unpack(f">{cnt}I", loca[:cnt * 4]))
    out = {}
    for gid in range(len(offs) - 1):
        g = glyf[offs[gid]:offs[gid + 1]]
        if g:
            out[gid] = g
    return out


# ───────────────────────── шаблон-донор ──────────────────────────────────
def _find_template(explicit=None):
    cands = []
    if explicit:
        cands.append(explicit)
    for env in ("YANDEX_RECEIPT_DONOR", "YANDEX_RECEIPT_TEMPLATE"):
        if os.environ.get(env):
            cands.append(os.environ[env])
    cands += [
        os.path.join(_DIR, "yandex_receipt_donor.pdf"),
        os.path.join(_DIR, "yandex_donor_receipt.pdf"),
        os.path.join(_DIR, "Квитанция (47).pdf"),
        os.path.join(os.path.expanduser("~"), "Downloads", "Telegram Desktop", "Квитанция (47).pdf"),
        r"C:\Users\nemor\Downloads\Telegram Desktop\Квитанция (47).pdf",
        "/opt/mts-bot/yandex_receipt_donor.pdf",
        "/opt/mts-bot/yandex_donors/yandex_receipt_donor.pdf",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError(
        "не найден донор квитанции Яндекс Банка: положите «Квитанция (47).pdf» "
        "рядом с модулем как yandex_receipt_donor.pdf, задайте "
        "YANDEX_RECEIPT_DONOR или передайте template_path=")


class _Template:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.raw = fh.read()
        self.md5 = hashlib.md5(self.raw).hexdigest()
        self.sha256 = hashlib.sha256(self.raw).hexdigest()
        self.objs, self.order = _obj_bodies(self.raw)
        if sorted(self.objs) != list(range(1, 29)):
            raise ValueError(f"донор {path}: ожидался скелет из 28 объектов, "
                             f"получены {sorted(self.objs)}")
        self.content = _decompressed(self.objs[_CONTENT_OBJ])
        # cid->char по фейсам донора
        self.cid2char = {}
        for face, (_t0, _ff, _fd, _cf, tu_obj, _cs) in _FONT_OBJS.items():
            mp = _parse_tounicode(_decompressed(self.objs[tu_obj]))
            # каталог/донор используют одиночные символы; многосимвольные
            # значения ToUnicode для кодирования текста не подходят
            self.cid2char[face] = {cid: ch for cid, ch in mp.items() if len(ch) == 1}
        self.donor_faces = {}
        for face, (_t0, ff, _fd, cf, _tu, _cs) in _FONT_OBJS.items():
            ttf = _decompressed(self.objs[ff])
            tabs = _sfnt_tables(ttf)
            self.donor_faces[face] = {
                "ttf": ttf, "tables": tabs,
                "glyph_by_gid": _glyphs_by_gid(ttf, tabs),
                "char_to_cid": {ch: cid for cid, ch in self.cid2char[face].items()},
                "widths": _parse_w(self.objs[cf]),
                "num_glyphs": struct.unpack(">H", tabs["maxp"][4:6])[0] if "maxp" in tabs else 0,
            }


_TPL_CACHE = {}


def _load_template(path=None):
    p = _find_template(path)
    if p not in _TPL_CACHE:
        _TPL_CACHE[p] = _Template(p)
    return _TPL_CACHE[p]


# ───────────────────────── каталог глифов ────────────────────────────────
_CATALOG_UNION = None


def _catalog_union():
    """face -> {char_to_cid, widths, glyph_by_gid} по _yandex_catalog.pkl."""
    global _CATALOG_UNION
    if _CATALOG_UNION is not None:
        return _CATALOG_UNION
    union = {f: {"char_to_cid": {}, "widths": {}, "glyph_by_gid": {}} for f in _FACES}
    try:
        with open(_CATALOG_PATH, "rb") as fh:
            cat = pickle.load(fh)
    except Exception:
        _CATALOG_UNION = union
        return union
    for name, blk in cat.items():
        if not isinstance(blk, dict) or "glyphs" not in blk:
            continue
        face = str(name).split("@")[0]
        if face not in union:
            continue
        u = union[face]
        tabs = blk.get("tables") or {}
        if "glyf" in tabs and "loca" in tabs and "head" in tabs:
            # пул глифов по GID из таблиц блока (включая компоненты)
            try:
                ng = blk.get("num_glyphs") or 0
                loca, glyf = tabs["loca"], tabs["glyf"]
                if ng and len(loca) >= 2 * (ng + 1):
                    offs = [v * 2 for v in struct.unpack(f">{ng + 1}H", loca[:2 * (ng + 1)])]
                    for gid in range(ng):
                        g = glyf[offs[gid]:offs[gid + 1]]
                        if g and gid not in u["glyph_by_gid"]:
                            u["glyph_by_gid"][gid] = g
            except Exception:
                pass
        for cid, ent in (blk.get("glyphs") or {}).items():
            try:
                ch = ent.get("char")
                if not ch or len(ch) != 1 or ch == "\x00":
                    continue
                cid = int(cid)
                if ch not in u["char_to_cid"]:
                    u["char_to_cid"][ch] = cid
                if ent.get("width") is not None:
                    u["widths"].setdefault(cid, int(ent["width"]))
                if ent.get("glyf"):
                    u["glyph_by_gid"].setdefault(cid, ent["glyf"])
            except Exception:
                continue
    _merge_missing_glyphs(union)
    _CATALOG_UNION = union
    return union


def _merge_missing_glyphs(union):
    """Долить в union глифы из _yandex_missing_glyphs_candidate.pkl.

    Только отсутствующие символы (genuine-байты никогда не перетираются);
    каждый глиф обязан совпасть по SHA-256 из записи кандидата, а его
    ширина — с банковской hmtx на том же CID (поле bank_hmtx_at_cid).
    Несовпадение = запись пропускается, а не «почти тот же шрифт»."""
    try:
        with open(_MISSING_PATH, "rb") as fh:
            cand = pickle.load(fh)
    except Exception:
        return
    for face, entries in (cand.get("faces") or {}).items():
        if face not in union or not isinstance(entries, dict):
            continue
        u = union[face]
        for ch, ent in entries.items():
            try:
                if not ch or len(ch) != 1 or ch in u["char_to_cid"]:
                    continue  # уже есть genuine/каталог — не трогаем
                cid = int(ent["cid"])
                if cid in u["widths"] or cid in u["glyph_by_gid"]:
                    # CID занят другим символом/глифом — конфликт, пропуск
                    if u["glyph_by_gid"].get(cid) not in (None, ent.get("glyph_bytes")):
                        continue
                raw = ent.get("glyph_bytes") or b""
                if not raw or hashlib.sha256(raw).hexdigest() != ent.get("glyph_sha256"):
                    continue
                w = ent.get("width")
                w = int(w[0]) if isinstance(w, (list, tuple)) else int(w)
                bank_w = ent.get("bank_hmtx_at_cid")
                if bank_w is not None and int(bank_w) != w:
                    continue
                u["char_to_cid"][ch] = cid
                u["widths"][cid] = w
                u["glyph_by_gid"][cid] = raw
            except Exception:
                continue


_FACES_CACHE = {}


def _load_faces(tpl: _Template):
    key = tpl.path
    if key in _FACES_CACHE:
        return _FACES_CACHE[key]
    union = _catalog_union()
    faces = {}
    for face in _FACES:
        base = tpl.donor_faces[face]
        char_to_cid = dict(base["char_to_cid"])
        widths = dict(base["widths"])
        glyph_by_gid = dict(base["glyph_by_gid"])
        u = union.get(face) or {"char_to_cid": {}, "widths": {}, "glyph_by_gid": {}}
        for ch, cid in u["char_to_cid"].items():
            char_to_cid.setdefault(ch, cid)
        for cid, w in u["widths"].items():
            widths.setdefault(cid, w)
        for gid, g in u["glyph_by_gid"].items():
            glyph_by_gid.setdefault(gid, g)
        faces[face] = {
            "tables": base["tables"], "num_glyphs": base["num_glyphs"],
            "char_to_cid": char_to_cid, "widths": widths,
            "glyph_by_gid": glyph_by_gid,
        }
    _FACES_CACHE[key] = faces
    return faces


def check_text(text, face="YSText-Regular"):
    """Символы ``text``, не покрытые каталогом/донором для ``face``."""
    if face not in _FACES:
        # допускаем короткие имена как в других генераторах проекта
        face = {"Regular": "YSText-Regular", "Medium": "YSText-Medium",
                "Logotype": "YSLogotype-Regular"}.get(face, face)
    try:
        tpl = _load_template()
        cov = _load_faces(tpl)[face]["char_to_cid"]
    except Exception:
        cov = _catalog_union().get(face, {}).get("char_to_cid", {})
    return [c for c in (text or "") if c not in cov]


# ───────────────────────── построение шрифтов ────────────────────────────
def _cksum(b: bytes) -> int:
    if len(b) % 4:
        b += b"\x00" * (4 - len(b) % 4)
    return sum(struct.unpack(f">{len(b) // 4}I", b)) & 0xFFFFFFFF


def _components_of(data: bytes):
    if not data or len(data) < 10:
        return []
    if struct.unpack(">h", data[0:2])[0] >= 0:
        return []
    comps, off = [], 10
    while off + 4 <= len(data):
        flags, gid = struct.unpack(">HH", data[off:off + 4])
        off += 4
        off += 4 if flags & 0x0001 else 2
        if flags & 0x0008:
            off += 2
        elif flags & 0x0040:
            off += 4
        elif flags & 0x0080:
            off += 8
        comps.append(gid)
        if not (flags & 0x0020):
            break
    return comps


def _build_ttf(face_data: dict, used_cids) -> bytes:
    num_g = int(face_data["num_glyphs"])
    glyphs = {}
    pool = face_data["glyph_by_gid"]
    if 0 in pool:
        glyphs[0] = pool[0]
    for cid in used_cids:
        if cid in pool:
            glyphs[int(cid)] = pool[int(cid)]
    queue = list(glyphs.values())
    while queue:
        gdata = queue.pop()
        for comp in _components_of(gdata):
            if comp not in glyphs and comp in pool:
                glyphs[comp] = pool[comp]
                queue.append(pool[comp])
    out_glyf = bytearray()
    loca = []
    for gid in range(num_g):
        loca.append(len(out_glyf))
        chunk = glyphs.get(gid, b"")
        if len(chunk) % 2:
            chunk += b"\x00"
        out_glyf += chunk
    loca.append(len(out_glyf))
    if any(off % 2 or off // 2 > 0xFFFF for off in loca):
        raise ValueError("glyf слишком велик для short loca донора")
    loca_b = struct.pack(f">{len(loca)}H", *[off // 2 for off in loca])
    tables = dict(face_data["tables"])
    tables["glyf"] = bytes(out_glyf)
    tables["loca"] = loca_b
    tags = sorted(tables.keys())
    n = len(tags)
    pow2 = 1 << (n.bit_length() - 1)
    header = struct.pack(">IHHHH", 0x00010000, n, 16 * pow2,
                         pow2.bit_length() - 1, 16 * n - 16 * pow2)
    pos = 12 + 16 * n
    built = []
    for tag in tags:
        if pos % 4:
            pos += 4 - (pos % 4)
        built.append((tag, pos, tables[tag]))
        pos += len(tables[tag])
    if pos % 4:
        pos += 4 - (pos % 4)
    font = bytearray(pos)
    font[:12] = header
    for di, (tag, off, blob) in enumerate(built):
        o = 12 + di * 16
        font[o:o + 4] = tag.encode("latin-1")
        calc_blob = blob
        if tag == "head":
            hz = bytearray(blob)
            hz[8:12] = b"\x00\x00\x00\x00"
            calc_blob = bytes(hz)
        struct.pack_into(">III", font, o + 4, _cksum(calc_blob), off, len(blob))
    for tag, off, blob in built:
        font[off:off + len(blob)] = blob
    return bytes(font)


def _build_tounicode(pairs) -> bytes:
    lines = [f"<{c:04x}><{c:04x}><{ord(ch):04x}>".encode()
             for c, ch in sorted(pairs.items()) if len(ch) == 1]
    # OpenPDF пишет не более 100 записей на секцию beginbfrange
    # (донор 49: 106 использованных CID = секции 100 + 6).
    sections = []
    for i in range(0, len(lines), 100):
        chunk = lines[i:i + 100]
        sections.append(f"{len(chunk)} beginbfrange\n".encode()
                        + b"\n".join(chunk) + b"\nendbfrange\n")
    return (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CIDSystemInfo\n"
        b"<< /Registry (TTX+0)\n"
        b"/Ordering (T42UV)\n"
        b"/Supplement 0\n"
        b">> def\n"
        b"/CMapName /TTX+0 def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n"
        b"<0000><FFFF>\n"
        b"endcodespacerange\n"
        + b"".join(sections)
        + b"endcmap\n"
        b"CMapName currentdict /CMap defineresource pop\n"
        b"end end\n"
    )


def _build_w(cid_width: dict) -> bytes:
    groups = []
    for c in sorted(cid_width):
        w = cid_width[c]
        if groups and groups[-1][0] + len(groups[-1][1]) == c:
            groups[-1][1].append(str(w))
        else:
            groups.append((c, [str(w)]))
    return "".join(f"{s}[{' '.join(ws)}]" for s, ws in groups).encode()


def _build_cidset(cids) -> bytes:
    mx = max(cids) if cids else 0
    bits = bytearray(mx // 8 + 1)
    for c in cids:
        bits[c // 8] |= 1 << (7 - (c % 8))
    return bytes(bits)


# ───────────────────────── контентный поток ──────────────────────────────
_UNESC_NAMED = {ord("b"): 0x08, ord("t"): 0x09, ord("n"): 0x0A,
                ord("f"): 0x0C, ord("r"): 0x0D}


def _unesc(raw: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(raw):
        if raw[i:i + 1] == b"\\" and i + 1 < len(raw):
            out.append(_UNESC_NAMED.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _esc(cid_bytes: bytes) -> bytes:
    return (cid_bytes.replace(b"\\", b"\\\\")
            .replace(b"\x08", b"\\b").replace(b"\x09", b"\\t")
            .replace(b"\x0a", b"\\n").replace(b"\x0c", b"\\f")
            .replace(b"\x0d", b"\\r")
            .replace(b"(", b"\\(").replace(b")", b"\\)"))


_STR_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)")
_TF_RE = re.compile(rb"/(F\d+) ([\d.]+) Tf")
_TJ_STR_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)Tj")
_TM_RE = re.compile(rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm")


def _decode_payload(raw: bytes, face: str, tpl: _Template) -> str:
    data = _unesc(raw)
    mp = tpl.cid2char.get(face, {})
    return "".join(mp.get(struct.unpack(">H", data[i:i + 2])[0], "?")
                   for i in range(0, len(data) - 1, 2))


def _shown_strings(tpl: _Template):
    """Все строковые литералы потока по порядку с текущим фейсом.

    Возвращает list[dict] с позицией литерала, ресурсом шрифта и —
    для Tj-операторов — их порядковым номером Tj-индексом."""
    content = tpl.content
    events = []
    for m in _TF_RE.finditer(content):
        events.append((m.start(), "tf", m.group(1).decode()))
    tj_pos = {}
    for idx, m in enumerate(_TJ_STR_RE.finditer(content)):
        tj_pos[m.start()] = idx
    for m in _STR_RE.finditer(content):
        events.append((m.start(), "str", m))
    events.sort(key=lambda e: e[0])
    out, cur_res = [], "F1"
    for pos, kind, val in events:
        if kind == "tf":
            cur_res = val
        else:
            out.append({"pos": pos, "match": val, "res": cur_res,
                        "tj_idx": tj_pos.get(pos)})
    return out


def _text_width_units(text: str, face: str, faces) -> int:
    fd = faces[face]
    total = 0
    for ch in text:
        cid = fd["char_to_cid"].get(ch)
        if cid is None:
            raise ValueError(f"символ {ch!r} не покрыт шрифтом {face} "
                             f"(check_text: {check_text(ch, face)})")
        total += int(fd["widths"].get(cid, 1000))
    return total


def _fmt_coord(v: float) -> str:
    s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


# строки таблицы: (подпись Tj, значение Tj) — Tj-индексы 4..31 парами
_ROW_KEYS = ["op_datetime", "op_type", "status", "sender_bank",
             "sender_phone", "sender_name", "recipient_phone",
             "recipient_name", "recipient_bank", "message", "amount",
             "commission", "auth_code", "sbp_id"]
_ROW_OF_KEY = {k: i for i, k in enumerate(_ROW_KEYS)}
_SEP_RE = re.compile(rb"(20|200) (\d+(?:\.\d+)?) m\n(200|570) (\d+(?:\.\d+)?) l\nS")
_WRAP_STEP = 11.72   # шаг базовых линий внутри поля (НАБЛЮДЕНО, n=1)
_ROW_SHIFT = 11.00   # сдвиг строк/разделителей ниже поля (НАБЛЮДЕНО, n=1)


def _wrap_value(text: str, faces, max_units=37000):
    """Перенос по пробелам в колонку 370 pt (F1 10 pt), как Jasper.

    Одно слово шире колонки — ValueError (поведение подлинника
    неизвестно, угадывать не будем)."""
    words = text.split(" ")
    lines, cur = [], ""
    for word in words:
        if _text_width_units(word, "YSText-Regular", faces) > max_units:
            raise ValueError(f"слово шире колонки значения (370 pt): {word!r}")
        cand = word if not cur else cur + " " + word
        if _text_width_units(cand, "YSText-Regular", faces) <= max_units:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines


def _rebuild_content(tpl: _Template, faces, values: dict):
    """Пересобрать поток: динамика, перенос значений, сдвиги строк.

    Возвращает (content_bytes, used_chars_by_face, lines_by_key).
    При однострочных значениях выход побайтно равен потоку донора
    при тех же значениях (реплики 47–52)."""
    shown = _shown_strings(tpl)
    # --- перенос значений табличных строк ---
    lines_by_key = {}
    for key, text in values.items():
        if key in _ROW_OF_KEY:
            lines_by_key[key] = _wrap_value(text, faces)
        else:  # шапка (Исх. № / Дата) не переносится
            units = _text_width_units(text, "YSText-Regular", faces)
            if _RIGHT_EDGE - units * 10 / 1000.0 < _HEADER_LEFT - 1e-9:
                raise ValueError(f"значение поля {key} слишком широкое: {text!r}")
            lines_by_key[key] = [text]
    extra_by_row = [len(lines_by_key[k]) - 1 for k in _ROW_KEYS]
    cum_before, acc = [], 0.0
    for e in extra_by_row:
        cum_before.append(acc)
        acc += e * _ROW_SHIFT
    if 440 + acc > 630:
        raise ValueError("таблица после переносов не помещается на страницу "
                         "(низ таблицы ниже 630 pt по верху)")
    # --- использованные символы: статика донора (с подменой динамики) ---
    used = {f: set() for f in _FACES}
    dyn_by_pos = {}
    for item in shown:
        face = _FACE_OF_RES.get(item["res"], "YSText-Regular")
        idx = item["tj_idx"]
        if idx is not None and idx in _DYN_IDX:
            key = _DYN_IDX[idx]
            dyn_by_pos[item["pos"]] = (idx, key)
            texts = lines_by_key[key]
        else:
            texts = [_decode_payload(item["match"].group(1), face, tpl)]
        for text in texts:
            for ch in text:
                if ch != "\x00":
                    used[face].add(ch)
    # --- кодирование строк динамических значений (все F1, 10 pt) ---
    fd = faces["YSText-Regular"]

    def encode_line(text):
        units = _text_width_units(text, "YSText-Regular", faces)
        x = _RIGHT_EDGE - units * 10 / 1000.0
        cid_bytes = b"".join(struct.pack(">H", fd["char_to_cid"][c]) for c in text)
        return x, _esc(cid_bytes)

    encoded = {key: [encode_line(t) for t in lines]
               for key, lines in lines_by_key.items()}
    # --- события правок потока: Tj-блоки и разделители ---
    events = []  # (start, kind, data)
    for item in shown:
        if item["tj_idx"] is None:
            continue
        idx = item["tj_idx"]
        row = (idx - 4) // 2 if 4 <= idx <= 31 else None
        shift = cum_before[row] if row is not None else 0.0
        m = item["match"]
        tm = None
        for tm in _TM_RE.finditer(tpl.content, 0, m.start()):
            pass
        if tm is None:
            continue
        if item["pos"] in dyn_by_pos:
            key = dyn_by_pos[item["pos"]][1]
            events.append((tm.start(), "tj", (item, tm, key, shift, row)))
        elif shift:
            events.append((tm.start(), "tj", (item, tm, None, shift, row)))
    seps = list(_SEP_RE.finditer(tpl.content))
    if len(seps) != 28:
        raise ValueError(f"в доноре ожидалось 28 разделителей, найдено {len(seps)}")
    for si, sm in enumerate(seps):
        row = si // 2
        y0 = float(sm.group(2))
        y1 = y0 - cum_before[row] - extra_by_row[row] * _ROW_SHIFT
        if abs(y1 - y0) > 1e-9:
            events.append((sm.start(), "sep", (sm, y1)))
    events.sort(key=lambda e: e[0])
    # --- сшивка ---
    out, cursor = [], 0
    for _start, kind, data in events:
        if kind == "sep":
            sm, y1 = data
            out.append(tpl.content[cursor:sm.start()])
            out.append(b"%s %s m\n%s %s l\nS" % (
                sm.group(1), _fmt_coord(y1).encode(),
                sm.group(3), _fmt_coord(y1).encode()))
            cursor = sm.end()
            continue
        item, tm, key, shift, row = data
        m = item["match"]
        payload_start, payload_end = m.start(1), m.end(1)
        orig_y = float(tm.group(2))
        new_y = orig_y - shift
        if key is None:  # статическая подпись сдвинутой строки
            x_s, payload = tm.group(1), tpl.content[payload_start:payload_end]
            extra_lines = []
        else:
            (x0, payload), *rest = encoded[key]
            x_s = _fmt_coord(x0).encode()
            extra_lines = rest
        out.append(tpl.content[cursor:tm.start(1)])
        out.append(x_s if isinstance(x_s, bytes) else x_s.encode())
        out.append(tpl.content[tm.end(1):tm.start(2)])
        out.append(_fmt_coord(new_y).encode())
        out.append(tpl.content[tm.end(2):payload_start])
        out.append(payload)
        cursor = payload_end
        if extra_lines:
            marker = b")Tj\n0 g"
            ins = tpl.content.find(marker, payload_end)
            if ins < 0:
                raise ValueError("не найден конец BT-блока значения для переноса")
            ins_end = ins + len(marker)
            out.append(tpl.content[cursor:ins_end])
            for j, (lx, lpayload) in enumerate(extra_lines, start=1):
                ly = orig_y - shift - _WRAP_STEP * j
                out.append(f"\n1 0 0 1 {_fmt_coord(lx)} {_fmt_coord(ly)} Tm\n"
                           f"0 0 0 rg\n(".encode() + lpayload + b")Tj\n0 g")
            cursor = ins_end
    out.append(tpl.content[cursor:])
    return b"".join(out), used, lines_by_key


# ───────────────────────── форматирование полей ──────────────────────────
def format_phone(raw) -> str:
    d = "".join(c for c in str(raw or "") if c.isdigit())
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    if len(d) != 10:
        raise ValueError(f"телефон должен содержать 10 цифр (без +7): {raw!r}")
    return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"


def _to_decimal(amount) -> Decimal:
    if isinstance(amount, Decimal):
        return amount
    if isinstance(amount, (int, float)):
        return Decimal(str(amount))
    s = str(amount).strip().replace("\u00a0", "").replace(" ", "")
    s = s.replace("₽", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"не удалось разобрать сумму: {amount!r}") from e


def format_amount(amount) -> str:
    """``470,00 ₽``; тысячи — NBSP, как в Jasper-формате выписок."""
    v = _to_decimal(amount).quantize(Decimal("0.01"))
    sign = "-" if v < 0 else ""
    v = abs(v)
    rub = int(v)
    kop = int((v - rub) * 100)
    grouped = f"{rub:,}".replace(",", "\u00a0")
    return f"{sign}{grouped},{kop:02d} ₽"


def _fmt_money_or_dash(value, what="комиссия") -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float, Decimal)):
        return "—" if _to_decimal(value) == 0 else format_amount(value)
    s = str(value).strip()
    if not s or s in ("-", "–", "—", "0", "0,00", "0.00"):
        return "—"
    # числовая строка — нормализовать в денежный формат
    try:
        return format_amount(s)
    except ValueError:
        return s


def _parse_dt(value, *, what="дата операции"):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        dt = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y в %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y",
                    "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"не удалось разобрать {what}: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MSK)
    return dt


def _parse_creation(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        m = re.fullmatch(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z", s)
        if m:
            dt = datetime(*[int(x) for x in m.groups()], tzinfo=_UTC)
        else:
            dt = _parse_dt(s, what="CreationDate")
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def _creation_str(dt_utc: datetime) -> str:
    return dt_utc.astimezone(_UTC).strftime("D:%Y%m%d%H%M%SZ")


# ───────────────────────── SBP ID / Исх. № / auth code ───────────────────
def _resolve_flip(era_flip_utc=None) -> datetime:
    if era_flip_utc is None:
        return _ERA_FLIP_UTC
    dt = _parse_creation(era_flip_utc)
    if dt is None:
        raise ValueError(f"не удалось разобрать era_flip_utc: {era_flip_utc!r}")
    return dt


def _era_route_suffix(op_utc: datetime, era_flip_utc=None):
    if op_utc >= _resolve_flip(era_flip_utc):
        return "G", "0011890101"
    return "B", "0011881102"


def parse_sbp_id(sbp_id: str) -> dict:
    s = str(sbp_id or "").strip()
    if not _SBP_RE.match(s) or len(s) != 32:
        raise ValueError(f"SBP ID должен быть 32 символа [AB]...: {sbp_id!r}")
    year = 2020 + int(s[1])
    doy = int(s[2:5])
    base = datetime(year, 1, 1, tzinfo=_UTC) + timedelta(days=doy - 1)
    op_utc = base.replace(hour=int(s[5:7]), minute=int(s[7:9]), second=int(s[9:11]))
    return {"prefix": s[0], "op_utc": op_utc, "ms": int(s[11:14]),
            "flag": s[14], "node": s[15], "route": s[17],
            "gateway": s[18:22], "suffix": s[22:32], "raw": s}


def _validate_sbp_id(sbp_id: str, op_utc: datetime, era_flip_utc=None):
    p = parse_sbp_id(sbp_id)
    if p["op_utc"].replace(second=0) != op_utc.replace(second=0, microsecond=0):
        raise ValueError(f"SBP ID кодирует {p['op_utc'].isoformat()}, "
                         f"а дата операции {op_utc.isoformat()} (минуты не совпадают)")
    route, suffix = _era_route_suffix(op_utc, era_flip_utc)
    if (p["route"], p["suffix"]) != (route, suffix):
        raise ValueError(f"SBP ID route/suffix {p['route']}/{p['suffix']} "
                         f"не соответствует эре операции ({route}/{suffix})")
    if route == "G" and p["flag"] != "0":
        raise ValueError("SBP ID новой эры (route G) должен иметь flag=0")
    pool = _GW_A if p["prefix"] == "A" else _GW_B
    if p["gateway"] not in pool:
        raise ValueError(f"gateway {p['gateway']} недопустим для префикса {p['prefix']}")
    return p


def make_sbp_id(op_dt_msk: datetime, *, rng_second: bool = False,
                era_flip_utc=None) -> str:
    op_utc = op_dt_msk.astimezone(_UTC)
    if rng_second:
        op_utc = op_utc.replace(second=secrets.randbelow(60), microsecond=0)
    route, suffix = _era_route_suffix(op_utc, era_flip_utc)
    # G-эра: flag=0 (5/5). B-эра: единственный образец flag=1,
    # распределение неизвестно — ставим '1' с низкой уверенностью.
    flag = "0" if route == "G" else "1"
    prefix = secrets.choice("AB")
    gateway = secrets.choice(_GW_A if prefix == "A" else _GW_B)
    doy = op_utc.timetuple().tm_yday
    # ms: exclude triple repdigits like the other banks (genuine corpus
    # shows ~none; a disputed checker marker, costless to avoid).
    ms = secrets.randbelow(1000)
    while len(set(f"{ms:03d}")) == 1:
        ms = secrets.randbelow(1000)
    return (f"{prefix}{str(op_utc.year)[-1]}{doy:03d}"
            f"{op_utc.hour:02d}{op_utc.minute:02d}{op_utc.second:02d}"
            f"{ms:03d}{flag}{secrets.choice(_NODE_POOL)}0"
            f"{route}{gateway}{suffix}")


# для совместимости имён с исследованиями законов ID в проекте
make_op_id = make_sbp_id


def _make_isx_no(stmt_date_msk: datetime) -> str:
    return stmt_date_msk.strftime("%Y%m%d") + f"{secrets.randbelow(10 ** 7):07d}"


def _make_auth_code() -> str:
    return "".join(secrets.choice(_AUTH_POOL) for _ in range(6))


def _subset_tags_from(value):
    if value is None:
        return {f: "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
                for f in _FACES}
    if isinstance(value, dict):
        out = {}
        for f in _FACES:
            tag = value.get(f) or value.get(_RES_OF_FACE[f])
            if not tag or not re.fullmatch(r"[A-Z]{6}", str(tag)):
                raise ValueError(f"subset tag для {f} должен быть [A-Z]{{6}}: {tag!r}")
            out[f] = str(tag)
        return out
    seq = list(value)
    if len(seq) != 3 or any(not re.fullmatch(r"[A-Z]{6}", str(t)) for t in seq):
        raise ValueError("subset_tags: ожидается dict или 3 тега [A-Z]{6} "
                         "(Regular, Medium, Logotype)")
    return dict(zip(_FACES, [str(t) for t in seq]))


# ───────────────────────── главный генератор ─────────────────────────────
def generate(sender=None, sender_phone=None, recipient=None,
             recipient_phone=None, recipient_bank=None, amount=None,
             op_dt=None, *,
             sender_name=None, recipient_name=None, dt=None,
             sender_bank=_DEFAULT_SENDER_BANK,
             message=None,
             commission=None,
             auth_code=None,
             sbp_id=None,
             isx_no=None,
             receipt_dt=None,
             creation_dt=None,
             doc_id=None,
             subset_tags=None,
             _subset_tags=None,
             template_path=None,
             era_flip_utc=None,
             op_type="Исходящий перевод СБП",
             status="Выполнено",
             meta_out=None) -> bytes:
    """Собрать исходящую СБП-квитанцию Яндекс Банка и вернуть PDF bytes.

    Основная сигнатура (по спецификации)::

        generate(sender, sender_phone, recipient, recipient_phone,
                 recipient_bank, amount, op_dt, *, message=None,
                 commission=None, auth_code=None, sbp_id=None,
                 receipt_dt=None, isx_no=None, doc_id=None, meta_out=None)

    ``op_dt`` — дата/время операции (МСК; naive трактуется как МСК).
    ``receipt_dt`` — момент формирования: из него выводятся Дата
    документа, префикс Исх. № и CreationDate (UTC); naive — как МСК.
    Алиасы: ``sender_name``/``recipient_name``, ``dt`` (= op_dt),
    ``creation_dt`` (= receipt_dt, naive — как UTC),
    ``subset_tags``/``_subset_tags``. Если задан ``sbp_id``, он должен
    кодировать ту же минуту операции (UTC). Для полностью
    детерминированной реплики оригинала передайте также ``isx_no``,
    ``auth_code``, ``doc_id`` и теги донора.
    """
    # --- разрешение алиасов ---
    if sender_name is None:
        sender_name = sender if sender is not None else _DEFAULT_SENDER_NAME
    if recipient_name is None:
        recipient_name = recipient
    if sender_phone is None:
        sender_phone = _DEFAULT_SENDER_PHONE
    if op_dt is None:
        op_dt = dt
    if recipient_name is None or recipient_phone is None or recipient_bank is None \
            or amount is None:
        raise TypeError("generate: нужны recipient, recipient_phone, "
                        "recipient_bank и amount")
    flip = _resolve_flip(era_flip_utc)
    auth_code_generated = auth_code is None
    tpl = _load_template(template_path)
    faces = _load_faces(tpl)

    # --- операция / SBP ID ---
    op_dt = _parse_dt(op_dt, what="дата операции") if op_dt is not None else None
    sbp_parsed = None
    if sbp_id is not None:
        sbp_parsed = parse_sbp_id(str(sbp_id).strip().upper())
    if op_dt is None:
        if sbp_parsed is not None:
            op_dt = sbp_parsed["op_utc"].astimezone(_MSK)
        else:
            op_dt = datetime.now(_MSK)
    op_dt = op_dt.astimezone(_MSK)
    op_utc = op_dt.astimezone(_UTC)
    minute_precision = (op_dt.second == 0 and op_dt.microsecond == 0)
    if sbp_id is None:
        sbp_id = make_sbp_id(op_dt, rng_second=minute_precision, era_flip_utc=flip)
        sbp_parsed = parse_sbp_id(sbp_id)
    else:
        sbp_id = str(sbp_id).strip().upper()
        sbp_parsed = _validate_sbp_id(sbp_id, op_utc, flip)
    sbp_op_utc = sbp_parsed["op_utc"]

    # --- CreationDate / Дата / Исх. № ---
    creation_utc = None
    if receipt_dt is not None:
        creation_utc = _parse_dt(receipt_dt, what="receipt_dt").astimezone(_UTC)
    if creation_dt is not None:
        cd_utc = _parse_creation(creation_dt)
        if creation_utc is not None and cd_utc != creation_utc:
            raise ValueError("receipt_dt и creation_dt задают разные моменты")
        creation_utc = cd_utc
    if creation_utc is None:
        creation_utc = datetime.now(_UTC).replace(microsecond=0)
        # Current-era genuine Yandex PDFs are always created after the SBP-ID
        # instant (08.11 corpus min +46s). Never emit a same-second or earlier
        # CreationDate for a minute-precision operation.
        if creation_utc < sbp_op_utc + timedelta(seconds=60):
            creation_utc = sbp_op_utc + timedelta(seconds=60 + secrets.randbelow(600))
    if creation_utc < sbp_op_utc + timedelta(seconds=60):
        raise ValueError("CreationDate слишком близко к моменту операции в SBP ID (current genuine min +60s)")
    creation_str = _creation_str(creation_utc)
    stmt_dt_msk = creation_utc.astimezone(_MSK)
    stmt_date_s = stmt_dt_msk.strftime("%d.%m.%Y")
    if isx_no is None:
        isx_no = _make_isx_no(stmt_dt_msk)
    else:
        isx_no = str(isx_no).strip()
        if not re.fullmatch(r"\d{15}", isx_no):
            raise ValueError(f"Исх. № должен быть 15 цифр: {isx_no!r}")
        if isx_no[:8] != stmt_dt_msk.strftime("%Y%m%d"):
            raise ValueError(f"Исх. № {isx_no} не начинается с даты формирования "
                             f"{stmt_dt_msk.strftime('%Y%m%d')} (CreationDate по МСК)")

    # --- код авторизации / /ID / subset tags ---
    if auth_code is None:
        auth_code = _make_auth_code()
    else:
        auth_code = str(auth_code).strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{6}", auth_code):
            raise ValueError(f"код авторизации должен быть [0-9A-Z]{{6}}: {auth_code!r}")
    if doc_id is None:
        doc_id = secrets.token_hex(16)
    else:
        doc_id = str(doc_id).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", doc_id):
            raise ValueError(f"doc_id (/ID) должен быть 32 hex-символа: {doc_id!r}")
    tags = _subset_tags_from(subset_tags if subset_tags is not None else _subset_tags)

    # --- текстовые поля ---
    def _clean(v, what):
        s = " ".join(str(v or "").split())
        if not s:
            raise ValueError(f"пустое поле: {what}")
        return s

    sender_name_f = _clean(sender_name, "ФИО отправителя")
    recipient_name_f = _clean(recipient_name, "ФИО получателя")
    sender_bank_f = _clean(sender_bank, "банк отправителя")
    recipient_bank_f = _clean(recipient_bank, "банк получателя")
    sender_phone_f = format_phone(sender_phone)
    recipient_phone_f = format_phone(recipient_phone)
    msg = "" if message is None else " ".join(str(message).split())
    message_f = "—" if msg in ("", "-", "–") else msg
    amount_dec = _to_decimal(amount)
    if amount_dec < 0:
        raise ValueError(f"сумма исходящего перевода не может быть отрицательной: {amount!r}")
    amount_f = format_amount(amount_dec)
    commission_f = _fmt_money_or_dash(commission)

    values = {
        "isx": f"Исх. № {isx_no}",
        "stmt_date": f"Дата {stmt_date_s}",
        "op_datetime": f"{op_dt.strftime('%d.%m.%Y')} в {op_dt.strftime('%H:%M')}",
        "op_type": _clean(op_type, "тип операции"),
        "status": _clean(status, "статус"),
        "sender_bank": sender_bank_f,
        "sender_phone": sender_phone_f,
        "sender_name": sender_name_f,
        "recipient_phone": recipient_phone_f,
        "recipient_name": recipient_name_f,
        "recipient_bank": recipient_bank_f,
        "message": message_f,
        "amount": amount_f,
        "commission": commission_f,
        "auth_code": auth_code,
        "sbp_id": sbp_id,
    }
    # покрытие шрифтом проверяем до сборки (громкая ошибка, не мусор в PDF)
    for key, text in values.items():
        missing = check_text(text, "YSText-Regular")
        if missing:
            raise ValueError(f"поле {key}: символы вне каталога YSText-Regular: "
                             f"{missing} в {text!r}")

    content, used, lines_by_key = _rebuild_content(tpl, faces, values)

    # --- объекты ---
    objs = dict(tpl.objs)
    objs[_CONTENT_OBJ] = (b"<</Filter/FlateDecode/Length "
                          + str(len(_c := _deflate(content, "cnt"))).encode()
                          + b">>stream\n" + _c + b"\nendstream")
    for face in _FACES:
        t0, ff, fd_obj, cf, tu_obj, cs_obj = _FONT_OBJS[face]
        fd_data = faces[face]
        used_cids = sorted(fd_data["char_to_cid"][c] for c in used[face])
        pairs = {cid: ch for ch, cid in fd_data["char_to_cid"].items()
                 if ch in used[face]}
        ttf = _build_ttf(fd_data, used_cids)
        comp_ff = _deflate(ttf, "ff")
        objs[ff] = (f"<</Length1 {len(ttf)}/Filter/FlateDecode/Length {len(comp_ff)}>>stream\n".encode()
                    + comp_ff + b"\nendstream")
        tu_b = _build_tounicode(pairs)
        comp_tu = _deflate(tu_b, "tu")
        objs[tu_obj] = (f"<</Filter/FlateDecode/Length {len(comp_tu)}>>stream\n".encode()
                        + comp_tu + b"\nendstream")
        cs_b = _build_cidset(used_cids)
        comp_cs = _deflate(cs_b, "cs")
        objs[cs_obj] = (f"<</Filter/FlateDecode/Length {len(comp_cs)}>>stream\n".encode()
                        + comp_cs + b"\nendstream")
        tag = tags[face]
        fd_b = objs[fd_obj].decode("latin-1")
        fd_b = re.sub(r"/FontName\s*/[A-Z]{6}\+" + re.escape(face),
                      f"/FontName/{tag}+" + face, fd_b)
        objs[fd_obj] = fd_b.encode("latin-1")
        cf_b = objs[cf].decode("latin-1")
        cf_b = re.sub(r"/BaseFont\s*/[A-Z]{6}\+" + re.escape(face),
                      f"/BaseFont/{tag}+" + face, cf_b)
        wm = re.search(r"/W\s*\[", cf_b)
        if not wm:
            raise ValueError(f"в доноре нет /W для {face}")
        bstart = cf_b.index("[", wm.start())
        depth, bend = 0, bstart
        for j in range(bstart, len(cf_b)):
            if cf_b[j] == "[":
                depth += 1
            elif cf_b[j] == "]":
                depth -= 1
                if depth == 0:
                    bend = j
                    break
        cid_width = {cid: int(fd_data["widths"][cid]) for cid in used_cids}
        cf_b = cf_b[:wm.start()] + "/W [" + _build_w(cid_width).decode("latin-1") + "]" + cf_b[bend + 1:]
        objs[cf] = cf_b.encode("latin-1")
        t0_b = objs[t0].decode("latin-1")
        t0_b = re.sub(r"/BaseFont\s*/[A-Z]{6}\+" + re.escape(face),
                      f"/BaseFont/{tag}+" + face, t0_b)
        objs[t0] = t0_b.encode("latin-1")
    objs[_INFO_OBJ] = (f"<</Creator({_CREATOR})/CreationDate({creation_str})"
                       f"/Producer({_PROD})>>").encode("latin-1")

    # --- сборка / xref / trailer ---
    pdf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in tpl.order:
        offsets[num] = len(pdf)
        pdf += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(pdf)
    size = max(tpl.objs) + 1
    pdf += f"xref\n0 {size}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for num in range(1, size):
        pdf += f"{offsets[num]:010d} 00000 n \n".encode()
    pdf += (f"trailer\n<</Info 28 0 R/ID [<{doc_id}><{doc_id}>]/Root 27 0 R/Size {size}>>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()

    if meta_out is not None:
        meta_out.clear()
        meta_out.update({
            "recipient_name": recipient_name_f,
            "recipient_phone": recipient_phone_f,
            "phone_fmt": recipient_phone_f,
            "recipient_bank": recipient_bank_f,
            "bank": recipient_bank_f,
            "sender_name": sender_name_f,
            "sender_phone": sender_phone_f,
            "sender_bank": sender_bank_f,
            "message": message_f,
            "amount": amount_dec,
            "amount_fmt": amount_f,
            "commission": commission_f,
            "auth_code": auth_code,
            "auth_code_generated": auth_code_generated,
            "sbp_id": sbp_id,
            "op_id": sbp_id,
            "sbp_op_utc": sbp_op_utc,
            "isx_no": isx_no,
            "receipt_no": isx_no,
            "op_dt": op_dt,
            "dt": op_dt,
            "creation": creation_str,
            "creation_dt": creation_utc,
            "receipt_dt": creation_utc.astimezone(_MSK),
            "stmt_date": stmt_date_s,
            "doc_date": stmt_date_s,
            "commission_fmt": commission_f,
            "sender_phone_fmt": sender_phone_f,
            "recipient_phone_fmt": recipient_phone_f,
            "message_lines": lines_by_key.get("message", [message_f]),
            "lines_by_key": lines_by_key,
            "era_flip_utc": flip.isoformat(),
            "era_flip_warning": "флип эры SBP по умолчанию внутри наблюдаемого "
                                "окна 04.11–06.11.2026, точный момент неизвестен",
            "id": (doc_id, doc_id),
            "doc_id": doc_id,
            "subset_tags": dict(tags),
            "template": os.path.basename(tpl.path),
            "template_md5": tpl.md5,
            "donor": "yandex_receipt",
        })
    return bytes(pdf)


# удобные алиасы в стиле соседних модулей проекта
generate_receipt = generate
generate_yandex_receipt = generate


def save(path, *args, **kwargs) -> str:
    pdf = generate(*args, **kwargs)
    with open(path, "wb") as fh:
        fh.write(pdf)
    return str(path)


__all__ = ["generate", "generate_receipt", "generate_yandex_receipt", "save",
           "check_text", "format_amount", "format_phone", "make_sbp_id",
           "make_op_id", "parse_sbp_id"]
