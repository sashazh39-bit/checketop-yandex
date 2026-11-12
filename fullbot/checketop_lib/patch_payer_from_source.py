#!/usr/bin/env python3
"""Патч 07-03-26_04-34: замена имени на «Арман Мелсикович Б.» (из эталона или жёстко).
Берёт имя из source (13-02-26_20-29 — копия.pdf), fallback — жёстко заданное.
"""
import re
import sys
import zlib
from pathlib import Path

PAYER_Y_SOURCE = 227.25
PAYER_Y_TARGET = 348.75
Y_TOL = 1.0

# Александр Евгеньевич Ж. (kern -16.66667) — в target
OLD_NAME = (
    b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 (\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
)
# «Имя плательщика» (ошибочно в правой колонке вместо имени) — kern -16.66667
OLD_LABEL_IMYA_16 = (
    b'(\x02\x1d)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02F)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02K)-16.66667 (\x02J)-16.66667 (\x02G)-16.66667 (\x02O)-16.66667 (\x02S)-16.66667 (\x02<)-16.66667 (\x02N)-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02[)'
)
# Вариант последнего глифа «а»: \x02\x40 вместо \x02[ (разные шрифты)
OLD_LABEL_IMYA_16_ALT = (
    b'(\x02\x1d)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02F)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02K)-16.66667 (\x02J)-16.66667 (\x02G)-16.66667 (\x02O)-16.66667 (\x02S)-16.66667 (\x02<)-16.66667 (\x02N)-16.66667 (\x02A)-16.66667 (\x02G)-16.66667 (\x02\x40)'
)
# Regex: «Имя плательщика» с любым kerning (fallback, если точное совпадение не сработало)
RE_IMYA_PLATELSHIKA = re.compile(
    rb'\(\x02\x1d\)-[\d.]+ \(\x02<\)-[\d.]+ \(\x02I\)-[\d.]+ \(\x02F\)-[\d.]+ \(\x00\x03\)-[\d.]+ '
    rb'\(\x02K\)-[\d.]+ \(\x02J\)-[\d.]+ \(\x02G\)-[\d.]+ \(\x02O\)-[\d.]+ \(\x02S\)-[\d.]+ \(\x02<\)-[\d.]+ \(\x02N\)-[\d.]+ \(\x02A\)-[\d.]+ \(\x02G\)-[\d.]+ \(\x02[\x40\x5b]\)'
)
# Андрей Максимович Р. (kern -16.66667) — иногда в доноре
OLD_ANDREI = (
    b'(\x02\x1c)-16.66667 (\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x02A)-16.66667 (\x02E)-16.66667 (\x00\x03)-16.66667 '
    b'(\x02\\()-16.66667 (\x02<)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02D)-16.66667 (\x02H)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x2c)-16.66667 (\x00\x11)'
)
# Арман Мелсикович Б. — М = Latin M (CID 0030), т.к. кирилл. 0228 нет в subset донора
NEW_PAYER_ARMAN = (
    b'(\x02\x1c)-16.66667 (\x02\x4c)-16.66667 (\x02\x48)-16.66667 (\x02\x3c)-16.66667 (\x02\x49)-16.66667 (\x00\x03)-16.66667 '
    b'(\x00\x30)-16.66667 (\x02\x41)-16.66667 (\x02\x47)-16.66667 (\x02\x4d)-16.66667 (\x02\x44)-16.66667 (\x02\x46)-16.66667 (\x02\x4a)-16.66667 (\x02\x3e)-16.66667 (\x02\x44)-16.66667 (\x02\x53)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)'
)


def _resolve_source(base: Path) -> Path:
    """Эталон: сначала «копия» (с правильным М), иначе обычный 13-02-26."""
    cheki = base / "чеки 07.03"
    # Сначала ищем копию (в ней правильное отображение М)
    for f in cheki.glob("13-02-26*копия*"):
        return f
    for name in ("13-02-26_20-29 — копия.pdf", "13-02-26_20-29\u00a0— копия.pdf", "13-02-26_20-29.pdf"):
        p = cheki / name
        if p.exists():
            return p
    return cheki / "13-02-26_20-29.pdf"


def extract_payer_tj(pdf_path: Path, payer_y: float) -> bytes | None:
    """Извлечь TJ плательщика из PDF (только правая колонка, x>100)."""
    data = pdf_path.read_bytes()
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
        for pat, grp in [
            (rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)', 7),
            (rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)', 6),
        ]:
            for mm in re.finditer(pat, dec):
                x, y = float(mm.group(2)), float(mm.group(4))
                if abs(y - payer_y) <= Y_TOL and x > 100:
                    return mm.group(grp)
    return None


def _extend_tounicode_m(data: bytearray) -> bytearray:
    """Добавить CID 0228 -> U+041C (М) в ToUnicode CMap, если отсутствует."""
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"beginbfrange" not in dec:
            continue
        # Пропустить, если маппинг 0228->041C уже есть
        if re.search(rb"<0228>\s*<0228>\s*<041[Cc]>", dec):
            continue
        # Добавить <0228><0228><041C> перед endbfrange
        new_entry = b"<0228> <0228> <041C>\r\n"
        insert_pos = dec.find(b"endbfrange")
        if insert_pos < 0:
            continue
        new_dec = dec[:insert_pos] + new_entry + dec[insert_pos:]
        # Увеличить счётчик beginbfrange
        count_m = re.search(rb"(\d+)\s+beginbfrange", new_dec)
        if count_m:
            old_n = int(count_m.group(1))
            new_dec = new_dec[: count_m.start(1)] + str(old_n + 1).encode() + new_dec[count_m.end(1) :]
        new_raw = zlib.compress(new_dec, 9)
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)
        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        data[len_num_start : len_num_start + len(old_len_str)] = new_len_str
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()
        print("[OK] CMap: добавлен CID 0228 (М) в ToUnicode")
        break
    return data


def adapt_kerning(tj: bytes, target_kern: bytes) -> bytes:
    """Адаптировать kerning под target (-16.66667)."""
    for old in (b"8.33333", b"21.42857"):
        if b"-" + old in tj:
            return tj.replace(b"-" + old, b"-" + target_kern)
    return tj


def main():
    base = Path(__file__).parent
    source = _resolve_source(base)
    target = base / "donors" / "07-03-26_04-34 (1).pdf"
    out = base / "чеки 08.03" / "07-03-26_04-34.pdf"

    if len(sys.argv) >= 3:
        source = Path(sys.argv[1])
        target = Path(sys.argv[2])
    if len(sys.argv) >= 4:
        out = Path(sys.argv[3])

    if not target.exists():
        print(f"[ERROR] Target не найден: {target}")
        sys.exit(1)

    # Копируем имя плательщика из эталона (CID как есть), fallback — жёстко заданное
    new_tj = NEW_PAYER_ARMAN
    if source.exists():
        source_tj = extract_payer_tj(source, PAYER_Y_SOURCE)
        if source_tj:
            new_tj = adapt_kerning(source_tj, b"16.66667")
            if new_tj.startswith(b"[") and b"] TJ" in new_tj:
                new_tj = new_tj[1 : new_tj.index(b"] TJ")]
            # М: кирилл. 0228 нет в subset донора — заменяем на Latin M (CID 0030), визуально идентична
            new_tj = new_tj.replace(b"(\x02\x28)", b"(\x00\x30)").replace(b"(\x02\\(", b"(\x00\x30)")
            # Проверка: должно быть «Арман» (021c 024c 0248...)
            has_arman = (b"(\x02\x4c)" in new_tj or b"(\x02L)" in new_tj) and (
                b"(\x02\x48)" in new_tj or b"(\x02H)" in new_tj
            )
            if not has_arman:
                new_tj = NEW_PAYER_ARMAN
        else:
            new_tj = NEW_PAYER_ARMAN
    else:
        print(f"[WARN] Source не найден, используем жёстко заданное имя: {source}")

    data = bytearray(target.read_bytes())
    # Добавить CID 0228 (М) в ToUnicode CMap, если отсутствует — иначе буква М не отображается
    data = _extend_tounicode_m(data)
    mods = []

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue

        def repl_tj(match):
            x, y = float(match.group(2)), float(match.group(4))
            if abs(y - PAYER_Y_TARGET) > Y_TOL or x < 100:
                return match.group(0)
            tj_grp = match.lastindex  # pat: 7, pat2: 6
            tj_block = match.group(tj_grp)
            idx = tj_block.find(b"]")
            if idx < 0:
                return match.group(0)
            tj_inner = tj_block[1:idx]
            new_inner = None
            if OLD_NAME in tj_inner:
                new_inner = tj_inner.replace(OLD_NAME, new_tj)
            elif OLD_ANDREI in tj_inner:
                new_inner = tj_inner.replace(OLD_ANDREI, new_tj)
            elif OLD_LABEL_IMYA_16 in tj_inner:
                new_inner = tj_inner.replace(OLD_LABEL_IMYA_16, new_tj)
            elif OLD_LABEL_IMYA_16_ALT in tj_inner:
                new_inner = tj_inner.replace(OLD_LABEL_IMYA_16_ALT, new_tj)
            elif RE_IMYA_PLATELSHIKA.search(tj_inner):
                new_inner = RE_IMYA_PLATELSHIKA.sub(new_tj, tj_inner)
            if new_inner is None:
                return match.group(0)
            new_tj_block = b"[" + new_inner + tj_block[idx:]
            # pat: grp6 между Tm и TJ, grp7=TJ. pat2: grp6=TJ. Не дублируем старый TJ.
            prefix = match.group(1) + match.group(2) + match.group(3) + match.group(4) + match.group(5)
            if tj_grp == 7:
                prefix += match.group(6)
            return prefix + new_tj_block

        pat = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)([^\[]*?)(\[[^\]]*\]\s*TJ)'
        new_dec = re.sub(pat, repl_tj, dec)
        pat2 = rb'(1\s+0\s+0\s+1\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm\s*\r?\n)(\[[^\]]*\]\s*TJ)'
        if new_dec == dec:
            new_dec = re.sub(pat2, repl_tj, dec)
        # Fallback: глобальная замена «Имя плательщика» в stream (если позиционная не сработала)
        if new_dec == dec and (OLD_LABEL_IMYA_16 in dec or OLD_LABEL_IMYA_16_ALT in dec or RE_IMYA_PLATELSHIKA.search(dec)):
            new_dec = dec
            for old in (OLD_LABEL_IMYA_16, OLD_LABEL_IMYA_16_ALT):
                if old in new_dec:
                    new_dec = new_dec.replace(old, new_tj)
            if RE_IMYA_PLATELSHIKA.search(new_dec):
                new_dec = RE_IMYA_PLATELSHIKA.sub(new_tj, new_dec)
        if new_dec == dec:
            continue
        new_raw = zlib.compress(new_dec, 9)
        mods.append((stream_start, stream_len, len_num_start, new_raw))

    if not mods:
        print("[ERROR] Не найдено поле плательщика в target.")
        sys.exit(1)

    mods.sort(key=lambda x: x[0], reverse=True)
    for stream_start, stream_len, len_num_start, new_raw in mods:
        delta = len(new_raw) - stream_len
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        if len(new_len_str) != len(old_len_str):
            delta += len(new_len_str) - len(old_len_str)

        data = data[:stream_start] + new_raw + data[stream_start + stream_len :]
        num_end = len_num_start + len(old_len_str)
        data[len_num_start:num_end] = new_len_str

        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)

        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
            pos = startxref_m.start(1)
            old_pos = int(startxref_m.group(1))
            data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print("[OK] Имя плательщика -> Арман Мелсикович Б.")
    print(f"[OK] Сохранено: {out}")


if __name__ == "__main__":
    main()
