# -*- coding: utf-8 -*-
"""Тесты yandex_statement_v2.py.

Доказывают три вещи отдельно:
1. synthetic-режим: текст, суммы, структура и пагинация 1/2/3 страницы
   соответствуют закону, измеренному на genuine-корпусе (9 выписок:
   6x1 стр., 2x2 стр., 1x3 стр.), а рендер непустой;
2. donor-template режим: выход байт-в-байт равен донору, кроме
   /CreationDate и trailer /ID (размер, текст, рендер попиксельно);
3. честность разделения: synthetic НЕ равен genuine байтово и помечен
   byte_faithful=False, а donor-template отказывается менять операции.

Запуск: py -3 -m pytest test_yandex_statement_v2.py -q
     или py -3 -m unittest test_yandex_statement_v2 -v
Донорские тесты пропускаются, если корпуса нет на диске.
Предполагается Windows + Arial (на Linux подставится DejaVu — метрики
переносов могут отличаться, счётчики операций по страницам тоже).
"""
import hashlib
import os
import re
import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yandex_statement_v2 as v2

MSK = v2.MSK
FIXED = dict(isx_no="202611070000001",
             creation_dt=datetime(2026, 11, 7, 3, 30, tzinfo=timezone.utc),
             doc_id="0123456789abcdef0123456789abcdef")


def norm(text):
    # PyMuPDF отдаёт пробелы как NBSP, а дефис Arial — как soft hyphen
    return (text or "").replace("\xa0", " ").replace("\xad", "-").replace("−", "–")


def make_op(day=14, hour=12, minute=22, amount="11", sender="Роман Андреевич В.",
            phone="+79118584552", bank="Озон Банк (Ozon)", outgoing=False,
            description="", month=10):
    return v2.Operation(
        datetime(2026, month, day, hour, minute, tzinfo=MSK), Decimal(amount),
        sender=sender, phone=phone, bank=bank, outgoing=outgoing,
        description=description)


def open_pdf(data):
    return fitz.open(stream=data, filetype="pdf")


def op_rows(page):
    """Число строк операций: даты dd.mm.yyyy в колонке даты операции (x~211)."""
    n = 0
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            txt = "".join(s["text"] for s in line["spans"])
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", txt.strip()) \
                    and abs(line["bbox"][0] - 211.22) < 4:
                n += 1
    return n


def span_y(page, needle, x=None):
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            if needle in norm("".join(s["text"] for s in line["spans"])) \
                    and (x is None or abs(line["bbox"][0] - x) < 4):
                return line["bbox"][1]
    return None


def ink_ratio(page, dpi=40):
    pix = page.get_pixmap(dpi=dpi)
    return sum(1 for b in pix.samples if b < 200) / max(1, len(pix.samples))


class TestSyntheticTextAndMath(unittest.TestCase):
    def test_one_page_mirrors_genuine_33c8(self):
        """2 операции, как в genuine 33c8...: 96 + 11 + 2 = 109, приход +13."""
        meta = {}
        data = v2.generate_statement(
            [make_op(amount="11"), make_op(hour=12, minute=27, amount="2")],
            opening_balance=96, period_from=date(2026, 10, 13),
            period_to=date(2026, 10, 14), statement_date=date(2026, 11, 7),
            meta_out=meta, **FIXED)
        self.assertEqual(meta["mode"], "synthetic")
        self.assertFalse(meta["byte_faithful"])
        doc = open_pdf(data)
        self.assertEqual(doc.page_count, 1)
        t = norm(doc[0].get_text())
        for needle in ("Выписка по договору", "Исх. № 202611070000001",
                       "Дата 07.11.2026", "Хабибуллин Ильяс Ильнурович",
                       "Выписка по Договору за период с 13.10.2026 по 14.10.2026",
                       "Входящий остаток на 13.10.2026", "96,00 ₽",
                       "Входящий перевод СБП, Роман", "+7 911 858-45-52",
                       "Озон Банк (Ozon)", "14.10.2026", "в 12:22", "в 12:27",
                       "+11,00 ₽", "+2,00 ₽",
                       "Исходящий остаток за 14.10.2026", "109,00 ₽",
                       "Всего расходных операций", "0,00 ₽",
                       "Всего приходных операций", "+13,00 ₽",
                       "Страница 1 из 1", "С уважением,", "T.C. Чиличихина",
                       "Яндекс Банк", "welcome@bank.yandex.ru"):
            self.assertIn(needle, t, needle)
        self.assertNotIn("Продолжение на следующей странице", t)
        self.assertEqual(op_rows(doc[0]), 2)
        doc.close()

    def test_anchors_match_genuine_coordinates(self):
        data = v2.generate_statement([make_op()], opening_balance=0, **FIXED)
        doc = open_pdf(data)
        self.assertAlmostEqual(span_y(doc[0], "Описание операции"), 208.42, delta=3.0)
        self.assertAlmostEqual(span_y(doc[0], "14.10.2026", x=211.22), 246.78, delta=3.0)
        self.assertAlmostEqual(span_y(doc[0], "Входящий остаток"), 185.92, delta=3.0)
        self.assertEqual(doc[0].rect, fitz.Rect(0, 0, 595, 842))
        doc.close()

    def test_mixed_totals_and_closing(self):
        ops = [make_op(amount="100"), make_op(amount="-30", outgoing=True),
               make_op(amount="-20.50", outgoing=True)]
        meta = {}
        data = v2.generate_statement(ops, opening_balance="10", meta_out=meta, **FIXED)
        doc = open_pdf(data)
        t = norm(doc[0].get_text())
        self.assertIn("–50,50 ₽", t)          # всего расходных
        self.assertIn("+100,00 ₽", t)         # всего приходных
        self.assertIn("59,50 ₽", t)           # исходящий: 10+100-30-20.50
        self.assertEqual((meta["total_in"], meta["total_out"]), ("100.00", "-50.50"))
        doc.close()

    def test_thousands_grouping(self):
        data = v2.generate_statement([make_op(amount="1234.5")], **FIXED)
        doc = open_pdf(data)
        self.assertIn("+1 234,50 ₽", norm(doc[0].get_text()))
        doc.close()

    def test_zero_operations_genuine_layout(self):
        meta = {}
        data = v2.generate_statement([], opening_balance="79",
                                     period_from=date(2026, 10, 5),
                                     period_to=date(2026, 10, 5),
                                     meta_out=meta, **FIXED)
        doc = open_pdf(data)
        self.assertEqual(doc.page_count, 1)
        t = norm(doc[0].get_text())
        self.assertNotIn("Описание операции", t)  # как в genuine 7abdb...
        self.assertIn("Исходящий остаток за 05.10.2026", t)
        self.assertAlmostEqual(span_y(doc[0], "Исходящий остаток"), 213.92, delta=3.0)
        doc.close()

    def test_validation(self):
        with self.assertRaises(ValueError):
            v2.generate_statement([], period_from=date(2026, 10, 9),
                                  period_to=date(2026, 10, 1))
        with self.assertRaises(ValueError):
            v2.generate_statement([], doc_id="xyz")
        with self.assertRaises(v2.DonorTemplateError):
            v2.generate([make_op()], mode="donor-template", donor_path="x.pdf")
        with self.assertRaises(ValueError):
            v2.generate([], mode="unknown-mode")

    def test_check_text_cyrillic_covered(self):
        self.assertEqual(v2.check_text("Привет, ₽ 123"), [])


class TestSyntheticPagination(unittest.TestCase):
    def test_two_pages_totals_spill_like_genuine(self):
        """8 операций (как оба genuine 2-стр. донора): операции и исходящий
        на стр.1, итоги целиком вынесены на стр.2 без заголовка таблицы."""
        meta = {}
        ops = [make_op(day=14 + (i % 4), minute=i, amount="10") for i in range(8)]
        data = v2.generate_statement(ops, opening_balance=96, meta_out=meta, **FIXED)
        doc = open_pdf(data)
        self.assertEqual(doc.page_count, 2)
        self.assertEqual(op_rows(doc[0]), 8)
        t1, t2 = norm(doc[0].get_text()), norm(doc[1].get_text())
        self.assertIn("Исходящий остаток", t1)
        self.assertNotIn("Всего расходных", t1)
        self.assertIn("Страница 1 из 2", t1)
        self.assertIn("Продолжение на следующей странице", t1)
        self.assertNotIn("С уважением,", t1)
        self.assertEqual(op_rows(doc[1]), 0)
        self.assertNotIn("Описание операции", t2)
        self.assertIn("Всего расходных операций", t2)
        self.assertIn("Страница 2 из 2", t2)
        self.assertIn("С уважением,", t2)
        doc.close()

    def test_three_pages_mirrors_genuine_b34c(self):
        """21 операция, 9-я — короткая 2-строчная (как genuine b34c...):
        стр.1 = 9 оп., стр.2 = 12 оп. + исходящий, стр.3 = только итоги."""
        ops = []
        for i in range(21):
            if i == 8:
                ops.append(make_op(day=6, hour=18, minute=51, amount="-10",
                                   outgoing=True,
                                   description="Внутрибанковский перевод, Николай К."))
            else:
                ops.append(make_op(day=4 + (i % 5), minute=i, amount="10",
                                   sender="Никита Валерьевич У.",
                                   phone="+79931349557"))
        meta = {}
        data = v2.generate_statement(ops, opening_balance=0, meta_out=meta, **FIXED)
        self.assertEqual(meta["ops_per_page"], [9, 12, 0])
        doc = open_pdf(data)
        self.assertEqual(doc.page_count, 3)
        self.assertEqual([op_rows(doc[i]) for i in range(3)], [9, 12, 0])
        t1, t2, t3 = (norm(doc[i].get_text()) for i in range(3))
        self.assertIn("Продолжение на следующей странице", t1)
        self.assertIn("Описание операции", t2)  # заголовок повторяется
        self.assertAlmostEqual(span_y(doc[1], "Описание операции"), 24.42, delta=3.0)
        self.assertIn("Исходящий остаток", t2)
        self.assertNotIn("Всего расходных", t2)
        self.assertIn("Всего приходных операций", t3)
        self.assertIn("Страница 3 из 3", t3)
        self.assertIn("С уважением,", t3)
        self.assertNotIn("Продолжение", t3)
        doc.close()

    def test_three_pages_uniform_descriptions(self):
        """21 одинаковая 3-строчная операция: [8, 12, 1], итоги на стр.3
        вместе с последней операцией (экстраполированная ветка модели)."""
        meta = {}
        ops = [make_op(minute=i, amount="10") for i in range(21)]
        data = v2.generate_statement(ops, opening_balance=0, meta_out=meta, **FIXED)
        self.assertEqual(meta["ops_per_page"], [8, 12, 1])
        doc = open_pdf(data)
        self.assertEqual(doc.page_count, 3)
        self.assertIn("Всего расходных", norm(doc[2].get_text()))
        doc.close()

    def test_every_operation_present_once(self):
        ops = [make_op(day=10 + i, hour=10, minute=i, amount=str(i + 1))
               for i in range(5)]
        data = v2.generate_statement(ops, **FIXED)
        doc = open_pdf(data)
        t = norm("".join(doc[i].get_text() for i in range(doc.page_count)))
        for i in range(5):
            self.assertGreaterEqual(t.count(f"{10 + i}.10.2026"), 2)  # операция + обработка
            self.assertIn(f"в 10:0{i}", t)
        doc.close()


class TestSyntheticStructureRender(unittest.TestCase):
    def setUp(self):
        self.data = v2.generate_statement(
            [make_op(amount=str(i + 1)) for i in range(10)], **FIXED)

    def test_metadata_and_id(self):
        doc = open_pdf(self.data)
        self.assertEqual(doc.metadata["producer"], v2.GENUINE_PRODUCER)
        self.assertEqual(doc.metadata["creator"], v2.GENUINE_CREATOR)
        self.assertEqual(doc.metadata["creationDate"], "D:20261107033000Z")
        self.assertFalse(doc.is_repaired)
        self.assertFalse(doc.needs_pass)
        doc.close()
        m = re.search(rb"/ID\s*\[<([0-9a-f]{32})><([0-9a-f]{32})>\]", self.data)
        self.assertIsNotNone(m, "trailer /ID не записан")
        self.assertEqual(m.group(1), m.group(2))
        self.assertEqual(m.group(1).decode(), FIXED["doc_id"])

    def test_deterministic_with_fixed_params(self):
        again = v2.generate_statement(
            [make_op(amount=str(i + 1)) for i in range(10)], **FIXED)
        self.assertEqual(hashlib.sha256(self.data).hexdigest(),
                         hashlib.sha256(again).hexdigest())

    def test_fonts_embedded_and_render_not_blank(self):
        doc = open_pdf(self.data)
        for i in range(doc.page_count):
            fonts = doc.get_page_fonts(i)
            self.assertTrue(fonts, f"стр.{i + 1}: нет шрифтов")
            self.assertTrue(all(f[1] == "ttf" for f in fonts))
            self.assertGreater(ink_ratio(doc[i]), 0.005,
                               f"стр.{i + 1}: пустой рендер")
        doc.close()

    def test_synthetic_is_not_genuine_bytes(self):
        donor = v2.find_donor(1)
        if not donor:
            self.skipTest("genuine-корпус недоступен")
        with open(donor, "rb") as f:
            genuine = f.read()
        self.assertNotEqual(hashlib.md5(self.data).hexdigest(),
                            hashlib.md5(genuine).hexdigest())


    def test_signature_image_from_donor_optional(self):
        donor = v2.find_donor(1)
        if not donor:
            self.skipTest("genuine-корпус недоступен")
        meta = {}
        data = v2.generate_statement([make_op()], signature_donor=donor,
                                     meta_out=meta, **FIXED)
        self.assertTrue(meta["signature_image"])
        doc = open_pdf(data)
        self.assertTrue(doc[0].get_images(), "картинка подписи не вставлена")
        doc.close()


class TestWrap(unittest.TestCase):
    def test_genuine_description_wraps_to_three_lines(self):
        lines = v2.wrap_description(
            "Входящий перевод СБП, Роман Андреевич В., "
            "+7 911 858-45-52, Озон Банк (Ozon)")
        self.assertEqual(len(lines), 3)
        self.assertEqual(" ".join(lines),
                         "Входящий перевод СБП, Роман Андреевич В., "
                         "+7 911 858-45-52, Озон Банк (Ozon)")

    def test_short_description_two_lines(self):
        self.assertEqual(
            len(v2.wrap_description("Внутрибанковский перевод, Николай К.")), 2)


@unittest.skipUnless(v2.find_donor(1), "genuine-корпус недоступен")
class TestDonorTemplate(unittest.TestCase):
    def _check_donor(self, pages):
        path = v2.find_donor(pages)
        self.assertIsNotNone(path, f"нет донора на {pages} стр.")
        with open(path, "rb") as f:
            orig = f.read()
        meta = {}
        out = v2.generate_from_donor(
            path, creation_dt=datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc),
            doc_id="a" * 32, meta_out=meta)
        self.assertEqual(meta["mode"], "donor-template")
        self.assertEqual(len(out), len(orig), "размер изменился")
        do, dg = fitz.open(path), open_pdf(out)
        self.assertEqual(dg.page_count, pages)
        for i in range(pages):
            self.assertEqual(norm(do[i].get_text()), norm(dg[i].get_text()),
                             f"стр.{i + 1}: текст изменился")
            po = do[i].get_pixmap(dpi=50)
            pg = dg[i].get_pixmap(dpi=50)
            self.assertEqual(hashlib.md5(po.samples).hexdigest(),
                             hashlib.md5(pg.samples).hexdigest(),
                             f"стр.{i + 1}: рендер изменился")
        self.assertEqual(dg.metadata["creationDate"], "D:20261108000000Z")
        self.assertEqual(dg.metadata["producer"], do.metadata["producer"])
        do.close(); dg.close()
        # байты равны всюду, кроме CreationDate и /ID
        def masked(b):
            b = re.sub(rb"D:\d{14}Z", b"D:XXXXXXXXXXXXXXZ", b)
            return re.sub(rb"<[0-9a-f]{32}>", b"<X>", b)
        self.assertEqual(masked(orig), masked(out))

    def test_donor_1_page(self):
        self._check_donor(1)

    def test_donor_2_pages(self):
        self._check_donor(2)

    def test_donor_3_pages(self):
        self._check_donor(3)

    def test_donor_rejects_non_genuine(self):
        fake = v2.generate_statement([make_op()], **FIXED)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_v2_fake_donor.pdf")
        try:
            with open(path, "wb") as f:
                f.write(fake)
            # synthetic помечен producer как genuine, но /ID и структура свои;
            # проверка донора смотрит producer+creator+заголовок, поэтому
            # дополнительно убеждаемся, что чужой PDF без Jasper-метаданных
            # отклоняется:
            doc = fitz.open()
            doc.new_page().insert_text((72, 72), "hello")
            bad = os.path.join(os.path.dirname(path), "_v2_bad_donor.pdf")
            doc.save(bad); doc.close()
            with self.assertRaises(v2.DonorTemplateError):
                v2.generate_from_donor(bad)
            os.remove(bad)
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
