#!/usr/bin/env python3
"""Вертикальная стенка: последняя буква каждой строки касается одной линии.

WALL = max(x1) всех полей правой колонки.
PTS = (x1 - tm_x) / n — извлекается из исходного PDF для каждого поля (разный кернинг).
"""
from pathlib import Path
import re
import zlib

try:
    import fitz
except ImportError:
    fitz = None


def scan_vtb_unsupported_chars(pdf_path: str | Path) -> set[str]:
    """Если доступен PyMuPDF: извлекает текст правой колонки и возвращает неподдерживаемые символы."""
    if fitz is None:
        return set()
    from vtb_cmap import get_unsupported_chars
    path = Path(pdf_path)
    if not path.exists():
        return set()
    bad = set()
    try:
        doc = fitz.open(path)
        dt = doc[0].get_text("dict")
        doc.close()
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if not bb or bb[0] < 100 or bb[0] > 270:
                        continue
                    text = span.get("text", "")
                    for c in get_unsupported_chars(text):
                        bad.add(c)
    except Exception:
        pass
    return bad

FALLBACK_WALL = 257.1
FALLBACK_PTS_FONT9 = 5.0925
FALLBACK_PTS_AMOUNT = 6.447  # font 13.5, kern -11.11111


def _count_tj_glyphs(tj_bytes: bytes) -> int:
    for kern in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        if kern in tj_bytes:
            return tj_bytes.count(kern) + 1
    return 1


def _scan_stream_blocks(data: bytes) -> list[dict]:
    """Извлечь (y, tm_x, n) из content stream."""
    blocks = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue
        pat = rb'1\s+0\s+0\s+1\s+([\d.]+)\s+([\d.]+)\s+Tm\s*\r?\n[^\[]*\[([^\]]*)\]\s*TJ'
        for gm in re.finditer(pat, dec):
            x, y = float(gm.group(1)), float(gm.group(2))
            if x < 50:
                continue
            n = _count_tj_glyphs(gm.group(3))
            blocks.append({"tm_x": x, "y": y, "n": n})
        break
    return blocks


def get_vtb_params_from_stream(pdf_path: str | Path) -> dict:
    """Извлечь wall и pts_text из PDF: эталон Выполнено/*9426, блоки из stream.

    Возвращает: wall, pts_text (единый для даты/payer/recipient/phone/bank), stream_blocks.
    """
    result = {
        "wall": FALLBACK_WALL,
        "pts_text": 5.08,
        "stream_blocks": [],
    }
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        data = path.read_bytes()
        stream_blocks = _scan_stream_blocks(data)
        result["stream_blocks"] = stream_blocks

        doc = fitz.open(path)
        page = doc[0]
        page_height = page.rect.y1 - page.rect.y0
        dt = page.get_text("dict")
        doc.close()

        spans = _spans_with_bbox(dt)
        import re as _re
        def _is_ref(t):
            if "Выполнено" in t:
                return True
            if _re.search(r"\*\d{4}", t):
                return True  # *9426, *9483 и т.д.
            return False
        ref_spans = [s for s in spans if _is_ref(s["text"])]
        if not ref_spans:
            return result
        wall = max(s["x1"] for s in ref_spans)
        result["wall"] = wall

        Y_TOL = 8.0
        for ref in ref_spans:
            pdf_y = page_height - ref["y"]
            for sb in stream_blocks:
                if abs(sb["y"] - pdf_y) < Y_TOL and sb["n"] > 0:
                    pts = (wall - sb["tm_x"]) / sb["n"]
                    if 4.0 < pts < 7.0:
                        result["pts_text"] = pts
                        return result
    except Exception:
        pass
    return result


def _spans_with_bbox(dt: dict) -> list[dict]:
    """Список span с (text, x1, y_center) для правой колонки."""
    out = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bb = span.get("bbox")
                if not bb or bb[0] < 100 or bb[0] > 270 or bb[1] < 50 or bb[1] > 400:
                    continue
                text = span.get("text", "").strip()
                if not text:
                    continue
                y_center = (bb[1] + bb[3]) / 2
                out.append({"text": text, "x1": bb[2], "y": y_center})
    return out


def get_vtb_per_field_params(pdf_path: str | Path) -> dict:
    """Извлекает WALL и pts для каждого заменяемого поля из исходного PDF.

    pts = (x1 - tm_x) / n, где x1 из PyMuPDF bbox, tm_x,n из stream.
    Возвращает: wall, pts_date, pts_payer, pts_recipient, pts_amount
    """
    result = {
        "wall": FALLBACK_WALL,
        "center_heading": (50 + FALLBACK_WALL) / 2,
        "pts_date": 4.55,
        "pts_payer": 4.66,
        "pts_recipient": 4.66,
        "pts_amount": FALLBACK_PTS_AMOUNT,
        "pts_phone": 4.57,
        "pts_bank": 5.09,
    }
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        data = path.read_bytes()
        stream_blocks = _scan_stream_blocks(data)
        doc = fitz.open(path)
        page = doc[0]
        rect = page.rect
        page_height = rect.y1 - rect.y0
        dt = page.get_text("dict")
        spans = _spans_with_bbox(dt)
        doc.close()

        # Fitz bbox: топ-левый origin, y вниз. PDF stream: низ-левый, y вверх.
        # pdf_y = page_height - fitz_y
        Y_TOL = 5.0

        def find_pts(search_words: list[str]) -> float | None:
            span = None
            for s in spans:
                if any(w in s["text"] for w in search_words):
                    span = s
                    break
            if not span:
                return None
            pdf_y = page_height - span["y"]
            best = None
            best_dy = 999
            for sb in stream_blocks:
                dy = abs(sb["y"] - pdf_y)
                if dy < best_dy and dy < Y_TOL:
                    best_dy = dy
                    best = sb
            if best and best["n"] > 0:
                return (span["x1"] - best["tm_x"]) / best["n"]
            return None

        # WALL = правый край эталонных полей (Выполнено, *XXXX)
        def _is_ref(t):
            if "Выполнено" in t:
                return True
            if re.search(r"\*\d{4}", t):
                return True
            return False
        ref_x1 = [s["x1"] for s in spans if _is_ref(s["text"])]
        result["wall"] = max(ref_x1) if ref_x1 else max(s["x1"] for s in spans)
        # Центр заголовка "Исходящий перевод СБП" для ФИО под ним
        all_spans = []
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if bb and span.get("text"):
                        all_spans.append({"text": span["text"], "bbox": bb})
        for s in all_spans:
            if "Исходящий" in s["text"] or "перевод СБП" in s["text"]:
                result["center_heading"] = (s["bbox"][0] + s["bbox"][2]) / 2
                break
        else:
            result["center_heading"] = (50 + result["wall"]) / 2  # fallback

        if pts := find_pts(["04:47", "09.03", "03.202", "08:52", "21:36"]):
            result["pts_date"] = pts
        if pts := find_pts(["Александр", "Евгений", "Евгеньевич", "Юрьевич", "Константинович", "Даниил"]):
            result["pts_payer"] = pts
        if pts := find_pts(["Ефим", "Антонович", "Юлия", "Константиновна", "Алла", "Дмитриевна"]):
            result["pts_recipient"] = pts
        if pts := find_pts(["1 000", "000 ₽", "₽"]):
            result["pts_amount"] = pts
        if pts := find_pts(["+7 (906)", "+7 (903)", "+7 (935)", "236-86", "236‑86", "903-66", "247-22"]):
            result["pts_phone"] = pts
        if pts := find_pts(["Сбербанк", "Альфа", "ВТБ"]):
            result["pts_bank"] = pts
        if pts := find_pts(["B606", "A606"]):
            result["pts_opid"] = pts
        stream_params = get_vtb_params_from_stream(path)
        result["pts_text"] = stream_params.get("pts_text", 5.08)
    except Exception:
        pass
    return result


def get_field_align_raw(pdf_path: str | Path) -> dict:
    """Сырые данные для alignment: x1 и pts по полям.
    date, account, phone — статичные поля; payer, recipient — ФИО.
    """
    result = {"path": str(pdf_path), "x1": {}, "pts": {}}
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        data = path.read_bytes()
        stream_blocks = _scan_stream_blocks(data)
        doc = fitz.open(path)
        page = doc[0]
        page_height = page.rect.y1 - page.rect.y0
        dt = page.get_text("dict")
        spans = _spans_with_bbox(dt)
        doc.close()

        Y_TOL = 5.0

        def find_x1_pts_y(search_words: list[str], x1_min: float = 200.0) -> tuple[float | None, float | None, float | None]:
            """Только правый столбец: x1 > x1_min (иначе ловит левые подписи «Банк ВТБ»)."""
            span = None
            for s in spans:
                if s.get("x1", 0) < x1_min:
                    continue
                if any(w in s["text"] for w in search_words):
                    span = s
                    break
            if not span:
                return None, None, None
            x1 = span["x1"]
            pdf_y = page_height - span["y"]
            best = None
            best_dy = 999
            for sb in stream_blocks:
                dy = abs(sb["y"] - pdf_y)
                if dy < best_dy and dy < Y_TOL:
                    best_dy = dy
                    best = sb
            if best and best["n"] > 0:
                return x1, (x1 - best["tm_x"]) / best["n"], best["y"]
            return x1, None, best["y"] if best else None

        def find_by_regex(pat: str, x1_min: float = 200.0) -> tuple[float | None, float | None, float | None]:
            for s in spans:
                if s.get("x1", 0) < x1_min:
                    continue
                if re.search(pat, s["text"]):
                    x1 = s["x1"]
                    pdf_y = page_height - s["y"]
                    best = None
                    best_dy = 999
                    for sb in stream_blocks:
                        dy = abs(sb["y"] - pdf_y)
                        if dy < best_dy and dy < Y_TOL:
                            best_dy = dy
                            best = sb
                    if best and best["n"] > 0:
                        return x1, (x1 - best["tm_x"]) / best["n"], best["y"]
                    return x1, None, best["y"] if best else None
            return None, None, None

        result["y"] = {}
        result["center_heading"] = None

        for name, words in [
            ("date", ["04:47", "09.03", "03.202", "08:52", "21:36", "02.202", "01.202", "16:44", "09.2025", "03.2026"]),
            ("phone", ["+7 (906)", "+7 (903)", "+7 (935)", "+7 (931)", "+7 (994)", "236-86", "236‑86", "605‑91"]),
        ]:
            x1, pts, y = find_x1_pts_y(words)
            if x1 is not None:
                result["x1"][name] = x1
            if pts is not None:
                result["pts"][name] = pts
            if y is not None:
                result["y"][name] = y

        x1_acc, pts_acc, y_acc = find_by_regex(r"\*\d{4}")
        if x1_acc is not None:
            result["x1"]["account"] = x1_acc
        if pts_acc is not None:
            result["pts"]["account"] = pts_acc
        if y_acc is not None:
            result["y"]["account"] = y_acc

        for name, words in [
            ("payer", ["Александр", "Евгений", "Евгеньевич", "Юрьевич", "Константинович", "Даниил", "Юлия", "Елена", "Арман", "Алан", "Петрович"]),
            ("recipient", ["Анна", "Петрова", "Артем", "Егорович", "Ефим", "Антонович", "Юлия", "Константиновна", "Алла", "Дмитриевна", "Максим", "Андреевич", "Дмитрий", "Сергеевич", "Роман"]),
        ]:
            x1, pts, y = find_x1_pts_y(words)
            if x1 is not None:
                result["x1"][name] = x1
            if pts is not None:
                result["pts"][name] = pts
            if y is not None:
                result["y"][name] = y

        for name, words in [
            ("amount", ["1 000", "000 ₽", "₽", "10 000", "100 ₽", "180 ₽"]),
            ("bank", ["Сбербанк", "Альфа", "ВТБ", "Т‑Банк", "Т-Банк", "Совкомбанк"]),
            ("message", ["ЙЦУКЕ", "Перевод денежных", "СМИТЬЪБЮ"]),
        ]:
            x1, pts, y = find_x1_pts_y(words)
            if pts is not None:
                result["pts"][name] = pts
            if y is not None:
                result["y"][name] = y

        x1_op, pts_op, y_op = find_by_regex(r"[AB]\d{4}[0-9A-Fa-f]{10,}")
        if pts_op is not None:
            result["pts"]["opid"] = pts_op
        if y_op is not None:
            result["y"]["opid"] = y_op

        x1_done, _, y_done = find_x1_pts_y(["Выполнено"], x1_min=200.0)
        if y_done is not None:
            result["y"]["done"] = y_done

        all_spans = []
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if bb and span.get("text"):
                        all_spans.append({"text": span["text"], "bbox": bb})
        for s in all_spans:
            if "Исходящий" in s["text"] or "перевод СБП" in s["text"]:
                result["center_heading"] = (s["bbox"][0] + s["bbox"][2]) / 2
                break
    except Exception:
        pass
    return result


WALL_SOURCES = {
    "sber": "Сбербанк",
    "phone": "+7 (906)",  # Телефон — обычно самый правый
    "done": "Выполнено",
    "star": "*9426",
    "max": "max",  # максимум из всех
}


def get_vtb_alignment_params(
    pdf_path: str | Path, wall_source: str = "sber"
) -> dict:
    """WALL и PTS. wall_source: sber|phone|done|star|max."""
    result = {
        "wall": FALLBACK_WALL,
        "pts_font9": FALLBACK_PTS_FONT9,
        "pts_font13_5": FALLBACK_PTS_FONT9 * 13.5 / 9,
    }
    result["ref_right"] = result["wall"]
    result["pts_payer"] = result["pts_recipient"] = result["pts_font9"]
    result["pts_amount"] = result["pts_font13_5"]
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        doc = fitz.open(path)
        page = doc[0]
        dt = page.get_text("dict")
        pts9 = FALLBACK_PTS_FONT9
        sber = page.search_for("Сбербанк")
        if sber:
            r = sber[0]
            pts9 = (r.x1 - r.x0) / 8
            result["pts_font9"] = result["pts_payer"] = result["pts_recipient"] = pts9
            result["pts_font13_5"] = result["pts_amount"] = pts9 * 13.5 / 9

        # Собираем x1 всех полей правого столбца
        x1_list = []
        for b in dt.get("blocks", []):
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    bb = s.get("bbox")
                    if bb and 100 < bb[0] < 260 and 100 < bb[1] < 380:
                        x1_list.append(bb[2])

        wall = FALLBACK_WALL
        if wall_source == "sber" and sber:
            wall = float(sber[0].x1)
        elif wall_source == "phone":
            r = page.search_for("236-86-13")
            if not r:
                r = page.search_for("+7 ")
            if r:
                wall = float(r[0].x1)
            elif x1_list:
                wall = max(x1_list)
        elif wall_source == "done":
            r = page.search_for("Выполнено")
            if r:
                wall = float(r[0].x1)
        elif wall_source == "star":
            r = page.search_for("*9426")
            if r:
                wall = float(r[0].x1)
        elif wall_source == "max" and x1_list:
            wall = max(x1_list)

        result["wall"] = result["ref_right"] = wall
        doc.close()
    except Exception:
        pass
    return result


def get_sber_right_edge(pdf_path: str | Path) -> float:
    return get_vtb_alignment_params(pdf_path)["wall"]


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "Тест ВТБ/09-03-26_03-47_1.pdf"
    params = get_vtb_alignment_params(p)
    print(f"WALL = {params['wall']:.4f}")
    print(f"pts_font9 = {params['pts_font9']:.4f}")
    print(f"pts_font13_5 = {params['pts_font13_5']:.4f}")
