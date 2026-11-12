# -*- coding: utf-8 -*-
"""Тесты yandex_receipt_generator.py — donor-template квитанции СБП.

Ключевой критерий: реплика каждого из 6 оригиналов «Квитанция (47..52)»
с полями, CreationDate, /ID и subset-тегами самого оригинала должна
быть БАЙТ-В-БАЙТ равна оригиналу (и на своём шаблоне, и на шаблоне 47).
Дальше — случайные генерации: поля, законы SBP ID / Исх. № / /ID,
receipt_validator (низкоуровневые проверки) и рендер через fitz.

receipt_validator не знает банк YANDEX (в _BANK_FP его нет), поэтому
validate() и для оригиналов, и для генераций возвращает bank=None —
тесты фиксируют это равенство поведения и проверяют остальные законы
через низкоуровневые функции валидатора, а не правят валидатор.
"""
import hashlib
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yandex_receipt_generator as yrg
import receipt_validator as rv

MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

_WIN_DIR = r"C:\Users\nemor\Downloads\Telegram Desktop"
_CANDIDATE_DIRS = [
    _WIN_DIR,
    os.path.dirname(os.path.abspath(__file__)),
    "/opt/mts-bot/yandex_donors",
    "/opt/mts-bot",
]


def _donor_path(n):
    for d in _CANDIDATE_DIRS:
        p = os.path.join(d, f"Квитанция ({n}).pdf")
        if os.path.exists(p):
            return p
        p2 = os.path.join(d, "yandex_receipt_donor.pdf")
        if n == 47 and os.path.exists(p2):
            return p2
    return os.path.join(_WIN_DIR, f"Квитанция ({n}).pdf")


DONORS = [_donor_path(n) for n in range(47, 53)]
ORIG_MD5 = [
    "7f94857134d6a37b0a38a17c5a3ad6af",
    "036ac9920c306056f00340bf5eb99120",
    "d515734818161c1c2e796f1d9c182c43",
    "ae484ebceb10a52a11763593644c0982",
    "bb5424c3ae66e750881c50a29ed83a10",
    "ca09e55a49503c653c26d639c5a94319",
]
ORIG_SHA256 = [
    "5284c2613597cd6a86dbe188b54d9adcab7d6b0098008f23811d938d7f25eb09",
    "841ff268c9b121df917d547bf49fc4db46477de2cdb1cba80ba3271fdd7d7d66",
    "808bbabe557303b6280e0d0379cd28c9957aac9a10cdba41e6971d2de829a6cb",
    "b6688337697da5a41969f0baf8528c49fa11821547548b67cd17b46d77ed214a",
    "ec5cb64b66d4b6cd1eb9f175a99b26ef6f567c5fc9dd40cc45d09efd314c7966",
    "358ab2ab1909d46bfad2f561700130184ad2f4e4d681a1f8a0f6d86df9866a4a",
]

LABELS = [
    "Дата и время операции МСК", "Тип операции", "Статус",
    "Банк отправителя", "Телефон отправителя", "ФИО отправителя",
    "Телефон получателя", "ФИО получателя", "Банк получателя",
    "Сообщение отправителя", "Сумма операции", "Комиссия",
    "Код авторизации", "Идентификатор операции в СБП",
]


def extract_fields(pdf_path_or_bytes):
    if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(pdf_path_or_bytes), filetype="pdf")
        raw = bytes(pdf_path_or_bytes)
    else:
        doc = fitz.open(pdf_path_or_bytes)
        with open(pdf_path_or_bytes, "rb") as fh:
            raw = fh.read()
    txt = doc[0].get_text()
    meta = dict(doc.metadata)
    spans = []
    for block in doc[0].get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans.extend(line["spans"])
    page_count = doc.page_count
    rect = doc[0].rect
    fonts = [(f[3], f[4]) for f in doc[0].get_fonts()]
    doc.close()
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    out = {}
    for i, line in enumerate(lines):
        if line in LABELS and i + 1 < len(lines):
            out[line] = lines[i + 1]
    out["isx"] = re.search(r"Исх\. № (\d+)", txt).group(1)
    out["stmt_date"] = re.search(r"Дата (\d{2}\.\d{2}\.\d{4})", txt).group(1)
    tags = {}
    for tag, face in re.findall(
            rb"/BaseFont\s*/([A-Z]{6})\+(YSText-Regular|YSText-Medium|YSLogotype-Regular)", raw):
        tags[face.decode()] = tag.decode()
    m = re.search(rb"/ID \[<([0-9a-f]+)><([0-9a-f]+)>\]", raw)
    out.update({"text": txt, "meta": meta, "spans": spans, "tags": tags,
                "doc_id": m.group(1).decode() if m else None,
                "page_count": page_count, "rect": rect, "fonts": fonts,
                "raw": raw})
    return out


def replica_kwargs(f):
    dt = datetime.strptime(f["Дата и время операции МСК"], "%d.%m.%Y в %H:%M")
    dt = dt.replace(tzinfo=MSK)
    creation = datetime.strptime(f["meta"]["creationDate"], "D:%Y%m%d%H%M%SZ")
    creation = creation.replace(tzinfo=UTC)
    amount = f["Сумма операции"].replace("\u00a0", " ").replace(" ₽", "").strip()
    return dict(
        recipient_name=f["ФИО получателя"],
        recipient_phone=f["Телефон получателя"],
        recipient_bank=f["Банк получателя"],
        amount=amount, dt=dt,
        sender_name=f["ФИО отправителя"],
        sender_phone=f["Телефон отправителя"],
        sender_bank=f["Банк отправителя"],
        message="" if f["Сообщение отправителя"] == "—" else f["Сообщение отправителя"],
        commission=None if f["Комиссия"] == "—" else f["Комиссия"],
        auth_code=f["Код авторизации"],
        sbp_id=f["Идентификатор операции в СБП"],
        isx_no=f["isx"], creation_dt=creation, doc_id=f["doc_id"],
        subset_tags=f["tags"],
    )


def assert_sbp_law(tc, sbp, op_dt_msk):
    p = yrg.parse_sbp_id(sbp)
    tc.assertEqual(len(sbp), 32)
    op_utc = op_dt_msk.astimezone(UTC)
    tc.assertEqual(p["op_utc"].replace(second=0), op_utc.replace(second=0, microsecond=0))
    route, suffix = yrg._era_route_suffix(op_utc)
    tc.assertEqual((p["route"], p["suffix"]), (route, suffix))
    if route == "G":
        tc.assertEqual(p["flag"], "0")
    pool = rv._GW_A if p["prefix"] == "A" else rv._GW_B
    tc.assertIn(p["gateway"], pool)
    tc.assertEqual(sbp[16], "0")
    vp = rv._parse_opid(sbp)
    tc.assertIsNotNone(vp)
    tc.assertEqual(vp["gw"], p["gateway"])
    tc.assertEqual(vp["suffix"], p["suffix"])
    return p


def assert_validator_level(tc, pdf_bytes, sbp_expected=None):
    raw = bytes(pdf_bytes)
    res = rv.validate(raw)
    tc.assertIsNone(res["bank"])  # как и для genuine-оригиналов
    tc.assertTrue(rv._xref_ok(raw))
    tid = rv._trailer_id(raw)
    tc.assertIsNotNone(tid)
    tc.assertEqual(tid[0], tid[1])
    tc.assertRegex(tid[0], r"^[0-9a-f]{32}$")
    info = rv._info_dict(raw)
    tc.assertEqual(info.get("/Producer"), yrg._PROD)
    tc.assertEqual(info.get("/Creator"), yrg._CREATOR)
    tc.assertRegex(info.get("/CreationDate", ""), r"^D:\d{14}Z$")
    streams = rv._flate_streams(raw)
    tc.assertGreater(len(streams), 0)
    tc.assertTrue(all(s[2] for s in streams))
    tc.assertTrue(all(s[1] == b"\x78\x9c" for s in streams),
                  f"заголовки потоков: {sorted(set(s[1].hex() for s in streams))}")
    tc.assertTrue(all(s[3] is None or abs(s[3] - s[4]) <= 2 for s in streams))
    fonts = rv._font_objects(raw)
    tc.assertTrue(any(f["has_fontfile2"] for f in fonts))
    tc.assertTrue(any(f["has_cidset"] for f in fonts))
    tc.assertTrue(any(f["has_tounicode"] for f in fonts))
    tc.assertTrue(any(f["has_w"] for f in fonts))
    if sbp_expected:
        doc = fitz.open(stream=raw, filetype="pdf")
        text = "".join(p.get_text() for p in doc)
        doc.close()
        tc.assertEqual(rv._extract_opid(text), sbp_expected)


def assert_render_common(tc, pdf_bytes, f_expected=None):
    doc = fitz.open(stream=bytes(pdf_bytes), filetype="pdf")
    tc.assertEqual(doc.page_count, 1)
    tc.assertFalse(doc.is_repaired)
    tc.assertFalse(doc.needs_pass)
    tc.assertAlmostEqual(doc[0].rect.width, 595, delta=1)
    tc.assertAlmostEqual(doc[0].rect.height, 842, delta=1)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
    tc.assertGreater(pix.width, 1000)
    samples = pix.samples  # один раз: свойство строит bytes при каждом доступе
    nonwhite = sum(1 for i in range(0, len(samples), 97) if samples[i] != 255)
    tc.assertGreater(nonwhite, 100, "рендер выглядит пустым")
    fonts = {f[3].split("+", 1)[-1] for f in doc[0].get_fonts()}
    tc.assertEqual(fonts, {"YSText-Regular", "YSText-Medium", "YSLogotype-Regular"})
    spans = []
    for block in doc[0].get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans.extend(line["spans"])
    doc.close()
    if f_expected:
        by_text = {s["text"]: s for s in spans}
        for text in f_expected:
            tc.assertIn(text, by_text, f"нет спана {text!r}")
            tc.assertAlmostEqual(by_text[text]["bbox"][2], 570.0, delta=0.6,
                                 msg=f"правый край {text!r}: {by_text[text]['bbox']}")
    return spans


class TestDonorReplicas(unittest.TestCase):
    def test_donors_present_and_hashes(self):
        for i, p in enumerate(DONORS):
            self.assertTrue(os.path.exists(p), f"donor missing: {p}")
            with open(p, "rb") as fh:
                raw = fh.read()
            self.assertEqual(hashlib.md5(raw).hexdigest(), ORIG_MD5[i])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ORIG_SHA256[i])

    def test_each_original_replica_byte_identical_own_template(self):
        for i, p in enumerate(DONORS):
            with self.subTest(donor=i + 47):
                f = extract_fields(p)
                gen = yrg.generate(**replica_kwargs(f), template_path=p)
                self.assertEqual(gen, f["raw"],
                                 f"реплика {i + 47} не байт-в-байт (md5 "
                                 f"{hashlib.md5(gen).hexdigest()} != {ORIG_MD5[i]})")

    def test_each_original_replica_byte_identical_on_template47(self):
        for i, p in enumerate(DONORS):
            with self.subTest(donor=i + 47):
                f = extract_fields(p)
                gen = yrg.generate(**replica_kwargs(f), template_path=DONORS[0])
                self.assertEqual(gen, f["raw"])

    def test_replica_render_pixel_identical(self):
        for p in DONORS:
            f = extract_fields(p)
            gen = yrg.generate(**replica_kwargs(f), template_path=p)
            d_o = fitz.open(p)
            d_g = fitz.open(stream=gen, filetype="pdf")
            pix_o = d_o[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            pix_g = d_g[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            self.assertEqual(pix_g.samples, pix_o.samples)
            self.assertEqual(d_g[0].get_text(), d_o[0].get_text())
            d_o.close(); d_g.close()


class TestRandomGenerations(unittest.TestCase):
    CASES = [
        dict(recipient_name="Иван Иванович И.", recipient_phone="+79118584552",
             recipient_bank="Озон Банк (Ozon)", amount=470,
             sender_name="Демид Андреевич В.", sender_phone="+79211025853",
             message="", commission=None),
        dict(recipient_name="Мария Сергеевна П.", recipient_phone="89110001122",
             recipient_bank="Сбербанк", amount="12,50",
             sender_name="Анна Павловна С.", sender_phone="9211025853",
             message="Заказ 123 спасибо", commission=0),
        dict(recipient_name="Семён Андреевич К.", recipient_phone="+7 999 123-45-67",
             recipient_bank="Альфа-Банк", amount=1000,
             sender_name="Пётр Николаевич В.", sender_phone="+7 911 000-11-22",
             message="Оплата услуг", commission="10,00"),
        dict(recipient_name="Anna Ivanovna S.", recipient_phone="79003517080",
             recipient_bank="Wildberries (Вайлдберриз Банк)", amount=123456.78,
             sender_name="Демид Андреевич В.", sender_phone="+79211025853",
             message="Test payment 42", commission=25.5),
        dict(recipient_name="Роман Андреевич В.", recipient_phone="+79118584552",
             recipient_bank="МТС Деньги (ЭКСИ Банк)", amount=100,
             sender_name="Ильяс Ильнурович Х.", sender_phone="+79677399849",
             message="Й й Ц ц У у К к Е е Н н", commission=None),
    ]

    def _run_case(self, case):
        now_msk = datetime.now(MSK).replace(second=0, microsecond=0)
        op_dt = now_msk - timedelta(minutes=17)
        meta = {}
        pdf = yrg.generate(dt=op_dt, meta_out=meta, **case)
        f = extract_fields(pdf)
        self.assertEqual(f["ФИО получателя"], case["recipient_name"])
        self.assertEqual(f["Телефон получателя"], yrg.format_phone(case["recipient_phone"]))
        self.assertEqual(f["Банк получателя"], case["recipient_bank"])
        self.assertEqual(f["ФИО отправителя"], case["sender_name"])
        self.assertEqual(f["Телефон отправителя"], yrg.format_phone(case["sender_phone"]))
        self.assertEqual(f["Банк отправителя"], "Яндекс Банк")
        exp_msg = "—" if not case["message"] else case["message"]
        self.assertEqual(f["Сообщение отправителя"], exp_msg)
        self.assertEqual(f["Сумма операции"].replace("\u00a0", " "),
                         yrg.format_amount(case["amount"]).replace("\u00a0", " "))
        self.assertEqual(f["Комиссия"], yrg._fmt_money_or_dash(case["commission"]))
        self.assertEqual(f["Код авторизации"], meta["auth_code"])
        self.assertRegex(meta["auth_code"], r"^[0-9A-Z]{6}$")
        self.assertEqual(f["Идентификатор операции в СБП"], meta["sbp_id"])
        self.assertEqual(f["isx"], meta["isx_no"])
        self.assertEqual(f["stmt_date"], meta["stmt_date"])
        self.assertEqual(f["Дата и время операции МСК"],
                         f"{op_dt.strftime('%d.%m.%Y')} в {op_dt.strftime('%H:%M')}")
        self.assertRegex(meta["isx_no"], r"^\d{15}$")
        self.assertTrue(meta["isx_no"].startswith(
            meta["creation_dt"].astimezone(MSK).strftime("%Y%m%d")))
        assert_sbp_law(self, meta["sbp_id"], op_dt)
        cd = datetime.strptime(f["meta"]["creationDate"], "D:%Y%m%d%H%M%SZ").replace(tzinfo=UTC)
        self.assertGreaterEqual(cd, yrg.parse_sbp_id(meta["sbp_id"])["op_utc"])
        self.assertEqual(f["meta"]["producer"], yrg._PROD)
        self.assertEqual(f["meta"]["creator"], yrg._CREATOR)
        self.assertEqual(f["doc_id"], meta["doc_id"])
        for face, tag in f["tags"].items():
            self.assertRegex(tag, r"^[A-Z]{6}$")
        assert_validator_level(self, pdf, sbp_expected=meta["sbp_id"])
        assert_render_common(self, pdf, f_expected=[
            f["ФИО получателя"], f["Банк получателя"], f["Сумма операции"],
            f["Идентификатор операции в СБП"], f"Исх. № {meta['isx_no']}",
        ])
        return pdf, meta

    def test_random_cases_fields_and_laws(self):
        for i, case in enumerate(self.CASES):
            with self.subTest(case=i):
                self._run_case(case)

    def test_validator_behaviour_same_as_originals(self):
        with open(DONORS[0], "rb") as fh:
            orig = rv.validate(fh.read())
        meta = {}
        pdf = yrg.generate("Демид Андреевич В.", "+79211025853",
                           "Иван Иванович И.", "+79118584552", "Сбербанк", 10,
                           datetime.now(MSK) - timedelta(minutes=3), meta_out=meta)
        gen = rv.validate(pdf)
        self.assertIsNone(orig["bank"])
        self.assertEqual(gen["bank"], orig["bank"])
        self.assertEqual([c["name"] for c in gen["checks"]],
                         [c["name"] for c in orig["checks"]])

    def test_uniqueness_of_random_identifiers(self):
        isxs, ids, sbps, auths = set(), set(), set(), set()
        op_dt = datetime.now(MSK).replace(second=0, microsecond=0) - timedelta(minutes=5)
        for _ in range(20):
            meta = {}
            yrg.generate("Демид Андреевич В.", "+79211025853",
                         "Иван Иванович И.", "+79118584552", "ВТБ", 10,
                         op_dt, meta_out=meta)
            isxs.add(meta["isx_no"]); ids.add(meta["doc_id"])
            sbps.add(meta["sbp_id"]); auths.add(meta["auth_code"])
            assert_sbp_law(self, meta["sbp_id"], op_dt)
        self.assertEqual(len(isxs), 20)
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(sbps), 20)
        self.assertGreaterEqual(len(auths), 18)

    def test_deterministic_when_all_random_inputs_fixed(self):
        f = extract_fields(DONORS[0])
        kwargs = replica_kwargs(f)
        a = yrg.generate(**kwargs, template_path=DONORS[0])
        b = yrg.generate(**kwargs, template_path=DONORS[0])
        self.assertEqual(a, b)
        c = yrg.generate(**kwargs, template_path=DONORS[1])
        self.assertEqual(a, c)

    def test_spec_positional_signature_and_receipt_dt(self):
        op_dt = datetime.now(MSK).replace(second=0, microsecond=0) - timedelta(minutes=9)
        receipt_dt = datetime.now(MSK)
        meta = {}
        pdf = yrg.generate("Демид Андреевич В.", "+79211025853",
                           "Иван Иванович И.", "+79118584552",
                           "Сбербанк", 100, op_dt,
                           receipt_dt=receipt_dt, meta_out=meta)
        self.assertEqual(meta["doc_date"], receipt_dt.strftime("%d.%m.%Y"))
        self.assertEqual(meta["creation"],
                         receipt_dt.astimezone(UTC).strftime("D:%Y%m%d%H%M%SZ"))
        self.assertTrue(meta["isx_no"].startswith(receipt_dt.strftime("%Y%m%d")))
        self.assertTrue(meta["auth_code_generated"])
        self.assertEqual(meta["sender_phone_fmt"], "+7 921 102-58-53")
        self.assertEqual(meta["recipient_phone_fmt"], "+7 911 858-45-52")
        self.assertEqual(meta["donor"], "yandex_receipt")
        assert_validator_level(self, pdf, sbp_expected=meta["sbp_id"])

    def test_long_message_wraps_like_genuine(self):
        # сообщение из широкого корпуса d646eb15…: 2 строки, вторая VBNM
        msg = ("q w e r t y u i o p a s d f g h j k l z x c v b n m "
               "Q W E R T Y U I O P A S D F G H J K L Z X C VBNM")
        meta = {}
        pdf = yrg.generate("Демид Андреевич В.", "+79211025853",
                           "Никита Валерьевич У.", "+79931349557",
                           "Озон Банк (Ozon)", 10,
                           datetime.now(MSK) - timedelta(minutes=4),
                           message=msg, meta_out=meta)
        self.assertEqual(len(meta["message_lines"]), 2)
        self.assertEqual(meta["message_lines"][1], "VBNM")
        self.assertTrue(meta["message_lines"][0].endswith("C"))
        f = extract_fields(pdf)
        by_text = {s["text"]: s for s in f["spans"]}
        self.assertIn("VBNM", by_text)
        # вторая строка: TOP 334.5 (обычная строка 322.78 + 11.72)
        self.assertAlmostEqual(by_text["VBNM"]["bbox"][1], 334.5, delta=0.1)
        self.assertAlmostEqual(by_text["VBNM"]["bbox"][2], 570.0, delta=0.6)
        # строки ниже сдвинуты на +11.00 по TOP
        self.assertAlmostEqual(by_text["Сумма операции"]["bbox"][1], 358.78, delta=0.1)
        self.assertAlmostEqual(by_text["Идентификатор операции в СБП"]["bbox"][1],
                               433.78, delta=0.1)
        # подвал не сдвигается
        self.assertAlmostEqual(by_text["С уважением,"]["bbox"][1], 720.78, delta=0.1)
        assert_validator_level(self, pdf, sbp_expected=meta["sbp_id"])
        assert_render_common(self, pdf)

    def test_save(self):
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_yandex_receipt_test_save.pdf")
        try:
            yrg.save(out, "Демид Андреевич В.", "+79211025853",
                     "Иван Иванович И.", "+79118584552", "ВТБ", 10,
                     datetime.now(MSK) - timedelta(minutes=1))
            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 100000)
            doc = fitz.open(out)
            self.assertEqual(doc.page_count, 1)
            doc.close()
        finally:
            if os.path.exists(out):
                os.unlink(out)


class TestValidationErrors(unittest.TestCase):
    BASE = dict(recipient_name="Иван Иванович И.", recipient_phone="+79118584552",
                recipient_bank="Сбербанк", amount=10)

    def test_check_text_coverage(self):
        self.assertEqual(yrg.check_text("Иван 123"), [])
        self.assertEqual(yrg.check_text("Артём"), [])  # строчная ё есть
        # Ё/Ъ/ъ долиты из _yandex_missing_glyphs_candidate.pkl (ys/4,
        # тот же билд, что банковские сабсеты; SHA/hmtx сверены при
        # загрузке каталога) — раньше здесь был отказ ValueError.
        self.assertEqual(yrg.check_text("Ёлка"), [])
        self.assertEqual(yrg.check_text("объект"), [])
        self.assertEqual(yrg.check_text("Ъ Ъъ"), [])
        self.assertIn("!", yrg.check_text("Привет!"))
        self.assertIn(";", yrg.check_text("Банк;"))

    def test_uncovered_char_rejected(self):
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "recipient_name": "Иван! И."})
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "recipient_bank": "Банк!"})

    def test_yo_names_generate_and_extract(self):
        # Падавший кейс стабильности: ФИО с Ё/Ъ/ъ обязаны генерироваться
        # и извлекаться обратно, с полными низкоуровневыми проверками.
        meta = {}
        pdf = yrg.generate(
            **{**self.BASE, "recipient_name": "Артём Ёлкин Ё.",
               "recipient_bank": "Сбербанк", "amount": 1000},
            sender_name="Объект Ъъ Ъ.",
            dt=datetime.now(MSK) - timedelta(minutes=2), meta_out=meta)
        f = extract_fields(pdf)
        self.assertEqual(f["ФИО получателя"], "Артём Ёлкин Ё.")
        self.assertEqual(f["ФИО отправителя"], "Объект Ъъ Ъ.")
        self.assertEqual(f["Сумма операции"].replace("\u00a0", " "),
                         "1 000,00 ₽")
        assert_validator_level(self, pdf, sbp_expected=meta["sbp_id"])
        assert_render_common(self, pdf)

    def test_bad_phone_rejected(self):
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "recipient_phone": "123"})

    def test_bad_amount_rejected(self):
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "amount": -1})
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "amount": "abc"})

    def test_amount_zero_accepted(self):
        meta = {}
        pdf = yrg.generate(**{**self.BASE, "amount": 0},
                           dt=datetime.now(MSK) - timedelta(minutes=2),
                           meta_out=meta)
        self.assertEqual(meta["amount_fmt"], "0,00 ₽")
        self.assertEqual(extract_fields(pdf)["Сумма операции"], "0,00 ₽")

    def test_too_wide_message_rejected(self):
        with self.assertRaises(ValueError):
            yrg.generate(**{**self.BASE, "message": "А" * 80})

    def test_sbp_mismatch_rejected(self):
        f = extract_fields(DONORS[0])
        kwargs = replica_kwargs(f)
        kwargs["dt"] = kwargs["dt"] + timedelta(days=1)
        with self.assertRaises(ValueError):
            yrg.generate(**kwargs, template_path=DONORS[0])

    def test_isx_date_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            yrg.generate(**self.BASE, isx_no="200001011234567",
                         creation_dt=datetime(2026, 11, 7, tzinfo=UTC),
                         dt=datetime(2026, 11, 7, 6, 0, tzinfo=MSK))

    def test_format_helpers(self):
        self.assertEqual(yrg.format_phone("89118584552"), "+7 911 858-45-52")
        self.assertEqual(yrg.format_phone("+7 911 858-45-52"), "+7 911 858-45-52")
        self.assertEqual(yrg.format_amount(470), "470,00 ₽")
        self.assertEqual(yrg.format_amount("12,5"), "12,50 ₽")
        self.assertEqual(yrg.format_amount(1234567.89),
                         "1\u00a0234\u00a0567,89 ₽")
        p = yrg.make_sbp_id(datetime(2026, 11, 7, 6, 49, 54, tzinfo=MSK))
        parsed = yrg.parse_sbp_id(p)
        self.assertEqual(parsed["op_utc"], datetime(2026, 11, 7, 3, 49, 54, tzinfo=UTC))
        self.assertEqual((parsed["route"], parsed["suffix"]), ("G", "0011890101"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
