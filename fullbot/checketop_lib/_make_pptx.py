# -*- coding: utf-8 -*-
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

# ── Цветовая схема ──────────────────────────────────────────────
DARK_BLUE   = RGBColor(0x1B, 0x37, 0x6B)   # заголовки, фон шапки
MID_BLUE    = RGBColor(0x2E, 0x5F, 0xA3)   # акценты
LIGHT_GRAY  = RGBColor(0xF2, 0xF4, 0xF8)   # фон слайда
WHITE       = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT_GOLD = RGBColor(0xE8, 0xA0, 0x1E)   # разделительная линия
TEXT_DARK   = RGBColor(0x1A, 0x1A, 0x2E)

W = Inches(13.33)   # 16:9
H = Inches(7.5)

def new_prs():
    prs = Presentation()
    prs.slide_width  = W
    prs.slide_height = H
    return prs

def blank_slide(prs):
    blank_layout = prs.slide_layouts[6]   # полностью пустой макет
    return prs.slides.add_slide(blank_layout)

def fill_bg(slide, color):
    from pptx.oxml.ns import qn
    from lxml import etree
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color

def add_rect(slide, left, top, width, height, color, alpha=None):
    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape

def add_textbox(slide, left, top, width, height, text,
                font_name="Arial", font_size=18, bold=False,
                color=TEXT_DARK, align=PP_ALIGN.LEFT,
                word_wrap=True, italic=False):
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = word_wrap
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = font_name
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    return txBox

def add_paragraph(tf, text, font_name="Arial", font_size=16,
                  bold=False, color=TEXT_DARK, align=PP_ALIGN.LEFT,
                  space_before=Pt(4), italic=False):
    from pptx.util import Pt as Ptx
    p = tf.add_paragraph()
    p.alignment = align
    p.space_before = space_before
    run = p.add_run()
    run.text = text
    run.font.name = font_name
    run.font.size = Ptx(font_size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    return p

def header_bar(slide, title_text, subtitle_text=""):
    """Тёмно-синяя полоса сверху с заголовком."""
    add_rect(slide, 0, 0, W, Inches(1.45), DARK_BLUE)
    add_rect(slide, 0, Inches(1.45), W, Inches(0.06), ACCENT_GOLD)
    add_textbox(slide, Inches(0.35), Inches(0.18),
                Inches(12.6), Inches(0.85),
                title_text,
                font_name="Arial", font_size=28, bold=True,
                color=WHITE, align=PP_ALIGN.LEFT)
    if subtitle_text:
        add_textbox(slide, Inches(0.35), Inches(0.9),
                    Inches(12.6), Inches(0.5),
                    subtitle_text,
                    font_name="Arial", font_size=15,
                    color=ACCENT_GOLD, align=PP_ALIGN.LEFT, italic=True)

def footer_bar(slide, num_text=""):
    add_rect(slide, 0, H - Inches(0.42), W, Inches(0.42), DARK_BLUE)
    add_textbox(slide, Inches(0.3), H - Inches(0.4),
                Inches(10), Inches(0.38),
                "Дроздов Д. · группа 19СВС · Разработка технологического процесса "
                "на изготовление резервуаров",
                font_name="Arial", font_size=9, color=WHITE)
    if num_text:
        add_textbox(slide, W - Inches(1.1), H - Inches(0.4),
                    Inches(0.9), Inches(0.38),
                    num_text,
                    font_name="Arial", font_size=9, color=WHITE,
                    align=PP_ALIGN.RIGHT)

def bullet_box(slide, items, left=Inches(0.4), top=Inches(1.6),
               width=Inches(12.5), height=Inches(5.6),
               font_size=17, color=TEXT_DARK):
    """Добавляет текстовое поле с маркированным списком."""
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = True
    first = True
    for item in items:
        if first:
            p = tf.paragraphs[0]
            first = False
        else:
            p = tf.add_paragraph()
        p.space_before = Pt(5)
        if isinstance(item, tuple):
            indent, text = item
        else:
            indent, text = 0, item
        run = p.add_run()
        bullet = "▪ " if indent == 0 else "   – "
        run.text = bullet + text
        run.font.name = "Arial"
        run.font.size = Pt(font_size if indent == 0 else font_size - 1)
        run.font.color.rgb = color if indent == 0 else MID_BLUE
        run.font.bold = (indent == 0)

def two_column_box(slide, left_items, right_items,
                   left_title="", right_title="",
                   font_size=16):
    col_w = Inches(6.1)
    gap = Inches(0.55)
    top = Inches(1.65)
    h = Inches(5.5)

    for col_idx, (ctitle, items) in enumerate(
        [(left_title, left_items), (right_title, right_items)]
    ):
        left = Inches(0.35) + col_idx * (col_w + gap)
        if ctitle:
            tb = slide.shapes.add_textbox(left, top, col_w, Inches(0.45))
            tf = tb.text_frame
            p = tf.paragraphs[0]
            run = p.add_run()
            run.text = ctitle
            run.font.name = "Arial"
            run.font.size = Pt(15)
            run.font.bold = True
            run.font.color.rgb = MID_BLUE
            item_top = top + Inches(0.5)
        else:
            item_top = top

        txBox = slide.shapes.add_textbox(left, item_top, col_w, h - Inches(0.5))
        tf = txBox.text_frame
        tf.word_wrap = True
        first = True
        for item in items:
            if first:
                p = tf.paragraphs[0]
                first = False
            else:
                p = tf.add_paragraph()
            p.space_before = Pt(5)
            run = p.add_run()
            if isinstance(item, tuple):
                ind, text = item
                run.text = ("▪ " if ind == 0 else "  – ") + text
                run.font.bold = (ind == 0)
                run.font.color.rgb = TEXT_DARK if ind == 0 else MID_BLUE
            else:
                run.text = "▪ " + item
                run.font.bold = True
                run.font.color.rgb = TEXT_DARK
            run.font.name = "Arial"
            run.font.size = Pt(font_size)

# ══════════════════════════════════════════════════════════════════
#  СЛАЙДЫ
# ══════════════════════════════════════════════════════════════════
prs = new_prs()

# ── Слайд 1: Титульный ───────────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
add_rect(sl, 0, 0, W, H * 0.52, DARK_BLUE)
add_rect(sl, 0, H * 0.52, W, Inches(0.07), ACCENT_GOLD)

add_textbox(sl, Inches(0.5), Inches(0.3),
            Inches(12.3), Inches(0.6),
            "ГАПОУ  ·  Группа 19СВС",
            font_name="Arial", font_size=14, color=ACCENT_GOLD, italic=True)

add_textbox(sl, Inches(0.5), Inches(0.9),
            Inches(12.3), Inches(1.4),
            "РАЗРАБОТКА ТЕХНОЛОГИЧЕСКОГО ПРОЦЕССА\nНА ИЗГОТОВЛЕНИЕ РЕЗЕРВУАРОВ",
            font_name="Arial", font_size=30, bold=True, color=WHITE)

add_textbox(sl, Inches(0.5), Inches(2.4),
            Inches(12.3), Inches(0.65),
            "Дипломная работа",
            font_name="Arial", font_size=18, color=WHITE, italic=True)

add_textbox(sl, Inches(0.5), Inches(4.4),
            Inches(12.3), Inches(0.5),
            "Студент:  Дроздов Дмитрий Александрович,  группа 19СВС",
            font_name="Arial", font_size=15, color=TEXT_DARK)
add_textbox(sl, Inches(0.5), Inches(4.9),
            Inches(12.3), Inches(0.5),
            "Специальность:  Сварщик (ручная и частично механизированная сварка (наплавка))",
            font_name="Arial", font_size=14, color=TEXT_DARK)
add_textbox(sl, Inches(0.5), Inches(5.4),
            Inches(12.3), Inches(0.5),
            "2026 г.",
            font_name="Arial", font_size=14, color=TEXT_DARK)

footer_bar(sl)

# ── Слайд 2: Актуальность и цель ─────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Актуальность темы и цель работы", "Слайд 2")
footer_bar(sl, "2 / 13")

two_column_box(sl,
    left_title="Актуальность",
    left_items=[
        "Резервуары применяются повсеместно: нефтегаз, химия, пищевая промышленность, "
        "водоснабжение, энергетика",
        "Дефект сварного шва → утечка, коррозия, авария, угроза жизни и экологии",
        "Требуется системный технологический процесс, обеспечивающий стабильное "
        "качество на каждом этапе",
        "Современное производство требует расчёта режимов, а не сварки «на глаз»",
    ],
    right_title="Цель и задачи",
    right_items=[
        "Цель — разработать технологический процесс изготовления резервуаров",
        (1, "Рассмотреть классификацию и назначение"),
        (1, "Изучить материалы и оборудование"),
        (1, "Рассчитать режимы сварки"),
        (1, "Описать последовательность операций"),
        (1, "Методы контроля качества"),
        (1, "Требования охраны труда"),
    ],
    font_size=15,
)

# ── Слайд 3: Классификация резервуаров ───────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Резервуары: назначение и классификация", "Слайд 3")
footer_bar(sl, "3 / 13")

two_column_box(sl,
    left_title="По конструкции",
    left_items=[
        "Вертикальные цилиндрические — наиболее распространены, большие объёмы жидкостей",
        "Горизонтальные — ограниченные объёмы, подземная установка",
        "Сферические — хранение газов под давлением, равномерное распределение напряжений",
    ],
    right_title="По расположению и давлению",
    right_items=[
        "Наземные — простое обслуживание и контроль",
        "Подземные — защита от температуры и механических воздействий",
        "Атмосферные — вода, нефтепродукты, большинство жидкостей",
        "Под давлением — сжиженные газы, летучие вещества, усиленная конструкция",
        "По назначению: товарные, технологические, аварийно-резервные",
    ],
    font_size=15,
)

# ── Слайд 4: Материалы ───────────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Основные материалы для изготовления резервуаров", "Слайд 4")
footer_bar(sl, "4 / 13")

bullet_box(sl, [
    "Углеродистая сталь — самый распространённый материал; высокая прочность, доступность, "
    "хорошая свариваемость; применяется для хранения нефти, воды и нефтепродуктов",
    (1, "Недостаток: подвержена коррозии → требуется защитное покрытие"),
    "Низколегированная сталь — повышенная прочность, лучшая ударная вязкость, "
    "улучшенная свариваемость по сравнению с углеродистой",
    "Нержавеющая сталь — для агрессивных сред и пищевой промышленности; "
    "коррозионная стойкость, гигиеничность; более высокая стоимость",
    "Алюминий — лёгкость, стойкость к коррозии, работа при низких температурах; "
    "применяется при ограниченных нагрузках",
    "Требования к металлу: соответствие стандартам по химсоставу, механическим "
    "свойствам, ударной вязкости и свариваемости; очистка перед производством",
], font_size=16)

# ── Слайд 5: Сварочное оборудование ─────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Сварочное оборудование и методы сварки", "Слайд 5")
footer_bar(sl, "5 / 13")

two_column_box(sl,
    left_title="Способы сварки",
    left_items=[
        "Полуавтоматическая MIG/MAG (135/136) — основной метод; высокая "
        "производительность, длинные непрерывные швы, стабильность дуги",
        "Аргонодуговая TIG (141) — ответственные швы, корневые проходы, "
        "тонкий металл; минимум дефектов",
        "Ручная дуговая MMA (111) — прихватки, труднодоступные места, ремонт; "
        "универсальна",
        "Автоматическая под флюсом — крупносерийное производство, длинные швы",
    ],
    right_title="Оборудование",
    right_items=[
        "Инверторные источники питания — стабильность дуги, точная регулировка, "
        "экономия электроэнергии",
        "Механизмы подачи проволоки, горелки, редукторы, баллоны с CO₂",
        "Шлифмашины, зачистные круги, металлические щётки — подготовка кромок",
        "Сборочные стенды, струбцины, кондукторы, шаблоны — точность сборки",
        "Контрольно-измерительный инструмент: линейки, шаблоны, уровни, рулетки",
    ],
    font_size=15,
)

# ── Слайд 6: Подготовка металла ──────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Этап 1: Подготовка металла и заготовительные работы", "Слайд 6")
footer_bar(sl, "6 / 13")

bullet_box(sl, [
    "Приёмка листового металла, проверка соответствия стандартам и проектной документации",
    "Очистка поверхностей от ржавчины, окалины, масла, влаги и загрязнений "
    "(загрязнения — причина пор, непроваров, трещин в швах)",
    "Разметка заготовок по чертежам и технологическим картам",
    "Резка металла: механическая, газовая или плазменная — выбор зависит от "
    "толщины листа и требуемой точности",
    "Обработка кромок: удаление заусенцев, разделка под сварку, выполнение фасок "
    "для обеспечения полного провара",
    "Вальцовка листов в обечайки цилиндрической формы корпуса резервуара",
    "Формирование элементов днища и крыши с применением специальных приспособлений",
    "Прихватки — временная фиксация деталей короткими швами для сохранения "
    "геометрии при основной сварке",
], font_size=16)

# ── Слайд 7: Сборка ──────────────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Этап 2: Сборка конструкции", "Слайд 7")
footer_bar(sl, "7 / 13")

bullet_box(sl, [
    "Установка деталей в проектное положение на сборочных стендах с применением "
    "струбцин, шаблонов и кондукторов",
    "Строгий контроль зазоров, соосности, формы корпуса, смещения листов и перекосов — "
    "любые отклонения создают дополнительные напряжения в эксплуатации",
    "Последовательность сборки: обечайки корпуса → днище → кровля → патрубки → "
    "усиливающие элементы",
    "Продольные швы соединяют листы по высоте резервуара, воспринимают нагрузки "
    "от давления",
    "Кольцевые швы соединяют пояса обечаек между собой — несут значительные "
    "напряжения от гидростатического давления жидкости",
    "Для снижения деформаций при сварке: симметричная последовательность наложения "
    "швов, обратноступенчатый метод, многопроходная сварка",
], font_size=16)

# ── Слайд 8: Режимы сварки ───────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Расчёт параметров режима сварки", "Слайд 8")
footer_bar(sl, "8 / 13")

# таблица-имитация через прямоугольники и текст
rows = [
    ("Параметр",              "Значение",          "Обоснование"),
    ("Способ сварки",         "Полуавтомат (MAG)",  "Высокая производительность, длинные швы"),
    ("Проволока",             "Св-08Г2С  Ø 1,2 мм","Хорошая свариваемость, устойчивая дуга"),
    ("Толщина металла",       "~8 мм",             "Стенки корпуса резервуара"),
    ("Сварочный ток",         "~200 А",            "Достаточный провар стенки 8 мм"),
    ("Напряжение дуги",       "~28 В",             "Стабильность процесса, ширина шва"),
    ("Скорость сварки",       "~30 м/ч",           "Качественный шов, снижение деформаций"),
    ("Расход CO₂",            "~14 л/мин",         "Надёжная защита ванны от окисления"),
    ("Полярность тока",       "Постоянный, обратная","Глубокий провар, минимум брызг"),
]
col_w = [Inches(3.5), Inches(2.8), Inches(5.8)]
col_x = [Inches(0.35), Inches(3.85), Inches(6.65)]
row_h = Inches(0.42)
top0  = Inches(1.65)

for r_i, row in enumerate(rows):
    y = top0 + r_i * row_h
    bg_color = DARK_BLUE if r_i == 0 else (LIGHT_GRAY if r_i % 2 == 0 else WHITE)
    txt_color = WHITE if r_i == 0 else TEXT_DARK
    fs = 13 if r_i == 0 else 13
    bd = True if r_i == 0 else False
    for c_i, cell_text in enumerate(row):
        add_rect(sl, col_x[c_i], y, col_w[c_i], row_h, bg_color)
        add_textbox(sl, col_x[c_i] + Inches(0.07), y + Inches(0.07),
                    col_w[c_i] - Inches(0.1), row_h - Inches(0.05),
                    cell_text,
                    font_name="Arial", font_size=fs, bold=bd,
                    color=txt_color, align=PP_ALIGN.LEFT)

# ── Слайд 9: Сварка ──────────────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Этап 3: Выполнение сварочных работ", "Слайд 9")
footer_bar(sl, "9 / 13")

bullet_box(sl, [
    "Сварка — ключевой этап изготовления: от прочности швов зависит "
    "герметичность и безопасность всего резервуара",
    "Первый проход обеспечивает провар корня шва; последующие формируют "
    "необходимую толщину и прочность соединения",
    "Применяются соединения: стыковые (основные листы корпуса), угловые и "
    "тавровые (патрубки, усилители, опоры)",
    "Углекислый газ предотвращает контакт расплавленного металла с воздухом → "
    "снижается пористость, улучшаются механические свойства шва",
    "Режим обратной полярности: ток течёт от изделия к электроду → "
    "глубокий провар, меньше разбрызгивание",
    "После сварки — зачистка шлака, брызг, неровностей; визуальная оценка "
    "ровности и плавности переходов",
    "Квалификация сварщика: допуск к ответственным конструкциям обязателен",
], font_size=16)

# ── Слайд 10: Контроль качества ──────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Контроль качества готового изделия", "Слайд 10")
footer_bar(sl, "10 / 13")

two_column_box(sl,
    left_title="Методы неразрушающего контроля",
    left_items=[
        "ВИК (визуально-измерительный контроль) — трещины, подрезы, наплывы, "
        "прожоги, несоответствие геометрии",
        "Ультразвуковой контроль (УЗК) — выявление внутренних дефектов: "
        "непроваров, пор, трещин, шлаковых включений",
        "Радиографический контроль — просвечивание швов рентгеном, объективная "
        "оценка структуры соединения",
        "Капиллярный метод — поверхностные трещины и микродефекты, "
        "невидимые при осмотре",
    ],
    right_title="Испытания и финальный контроль",
    right_items=[
        "Гидравлическое испытание — заполнение водой под расчётным давлением; "
        "проверка отсутствия протечек и деформаций",
        "Пневматическое испытание — сжатый воздух + мыльный раствор; "
        "строгое соблюдение безопасности",
        "Контроль антикоррозионного покрытия — равномерность, отсутствие "
        "отслоений и непрокрашенных участков",
        "Проверка комплектности: люки, патрубки, арматура, лестницы, "
        "клапаны; соответствие проектной документации",
        "Составление технической документации и акта приёмки",
    ],
    font_size=14,
)

# ── Слайд 11: Антикоррозионная защита ───────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Антикоррозионная защита и монтаж элементов", "Слайд 11")
footer_bar(sl, "11 / 13")

bullet_box(sl, [
    "Резервуары эксплуатируются на открытом воздухе или контактируют с агрессивными "
    "веществами → антикоррозионная защита обязательна",
    "Наружная защита: очистка поверхности, нанесение грунтовки, лакокрасочных "
    "покрытий или специальных антикоррозионных составов",
    "Внутренняя защита: для нефтепродуктов и химических веществ — специальные "
    "покрытия, эпоксидные материалы, футеровка с повышенной химической стойкостью",
    "Установка технологических элементов: люки для осмотра и обслуживания, "
    "патрубки для подключения трубопроводов, запорная арматура, дыхательные клапаны",
    "Монтаж лестниц, площадок обслуживания, ограждений, перил — требования охраны "
    "труда при эксплуатации",
    "Проверка правильности установки всех элементов по проектной документации "
    "перед вводом резервуара в эксплуатацию",
], font_size=16)

# ── Слайд 12: Техника безопасности ──────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Техника безопасности при сварке резервуаров", "Слайд 12")
footer_bar(sl, "12 / 13")

two_column_box(sl,
    left_title="Основные опасные факторы",
    left_items=[
        "Поражение электрическим током — неисправность оборудования, повреждение изоляции",
        "УФ и ИК-излучение дуги — ожоги кожи и глаз без защиты",
        "Вредные газы и сварочные аэрозоли — отравление, болезни дыхательной системы",
        "Пожар и взрыв — искры, брызги металла, горючие вещества рядом с рабочим местом",
        "Работа внутри резервуара — накопление газов, ограниченное пространство",
    ],
    right_title="Обязательные меры защиты",
    right_items=[
        "Обучение, инструктаж, медосмотр, допуск к ответственным конструкциям",
        "Заземление оборудования, проверка кабелей и изоляции перед работой",
        "Маска со светофильтром, огнестойкая спецодежда, перчатки, защитная обувь",
        "Вентиляция, очистка рабочего места от горючих материалов, средства "
        "пожаротушения рядом",
        "Работа внутри по наряду-допуску: наблюдающий снаружи, принудительная "
        "вентиляция, анализ воздуха перед началом",
    ],
    font_size=14,
)

# ── Слайд 13: Заключение ─────────────────────────────────────────
sl = blank_slide(prs)
fill_bg(sl, LIGHT_GRAY)
header_bar(sl, "Заключение", "Слайд 13")
footer_bar(sl, "13 / 13")

bullet_box(sl, [
    "Разработан полный технологический процесс изготовления резервуаров: "
    "от подготовки металла до испытаний и антикоррозионной защиты",
    "Подобраны параметры полуавтоматической сварки в CO₂: "
    "I = 200 А · U = 28 В · v = 30 м/ч · расход газа 14 л/мин · проволока Св-08Г2С ∅1,2 мм",
    "Качество резервуара обеспечивается комплексом мер: правильный материал, "
    "точная сборка, соблюдение режимов сварки, контроль швов и испытания",
    "Применение методов неразрушающего контроля (ВИК, УЗК, рентген) позволяет "
    "выявить дефекты до ввода резервуара в эксплуатацию",
    "Соблюдение требований охраны труда снижает риск травматизма, пожара и "
    "профессиональных заболеваний",
    "Практическая значимость: разработанный техпроцесс может применяться при "
    "организации производства и подготовке технологической документации",
], font_size=17)

out = r"c:\Users\drozd\Desktop\Дроздов_резервуары_презентация.pptx"
prs.save(out)
print("Saved:", out)
