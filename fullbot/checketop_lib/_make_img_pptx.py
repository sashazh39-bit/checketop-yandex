# -*- coding: utf-8 -*-
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch

# ── Шрифт ──────────────────────────────────────────────────────
plt.rcParams["font.family"] = "DejaVu Sans"

# ── Цвета ───────────────────────────────────────────────────────
C_BG        = "#0D1B2A"   # почти чёрный синий
C_PANEL     = "#162840"   # панели
C_ACCENT    = "#E8A01E"   # золото
C_BLUE2     = "#2E73C8"   # голубой акцент
C_WHITE     = "#FFFFFF"
C_LIGHTGRAY = "#D4DCE8"
C_TEXT      = "#EAF0FA"
C_SUB       = "#A8BDDA"

FIG_W, FIG_H = 16, 9   # дюймы

tmp_dir = r"c:\Users\drozd\Desktop\презентация_слайды"
os.makedirs(tmp_dir, exist_ok=True)
slide_paths = []

# ═══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

def new_fig():
    fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=C_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 16); ax.set_ylim(0, 9)
    ax.axis("off")
    ax.set_facecolor(C_BG)
    return fig, ax

def save(fig, idx):
    p = os.path.join(tmp_dir, f"slide_{idx:02d}.png")
    fig.savefig(p, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    slide_paths.append(p)

def bg_grid(ax):
    """Тонкая сетка на фоне."""
    for x in range(0, 17, 2):
        ax.axvline(x, color="#1A2E45", lw=0.4, zorder=0)
    for y in range(0, 10, 1):
        ax.axhline(y, color="#1A2E45", lw=0.4, zorder=0)

def gold_line(ax, y=7.55, x0=0.5, x1=15.5, lw=2.5):
    ax.plot([x0, x1], [y, y], color=C_ACCENT, lw=lw, zorder=5)

def header(ax, title, tag=""):
    """Заголовок слайда."""
    ax.add_patch(FancyBboxPatch((0, 8.0), 16, 1.0,
                                boxstyle="square,pad=0",
                                fc=C_PANEL, ec="none", zorder=2))
    gold_line(ax, y=8.0, x0=0, x1=16, lw=3)
    ax.text(0.4, 8.5, title, color=C_WHITE, fontsize=22,
            fontweight="bold", va="center", zorder=3)
    if tag:
        ax.text(15.6, 8.5, tag, color=C_ACCENT, fontsize=13,
                fontweight="bold", va="center", ha="right", zorder=3)

def footer(ax, note=""):
    ax.add_patch(mpatches.Rectangle((0, 0), 16, 0.42,
                                    fc=C_PANEL, ec="none", zorder=2))
    ax.text(0.4, 0.21,
            "Дроздов Д. · гр. 19СВС · Разработка технологического процесса "
            "на изготовление резервуаров",
            color=C_SUB, fontsize=8.5, va="center", zorder=3)
    if note:
        ax.text(15.6, 0.21, note, color=C_ACCENT, fontsize=9,
                va="center", ha="right", fontweight="bold", zorder=3)

def panel(ax, x, y, w, h, color=C_PANEL, alpha=1.0, radius=0.18):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle=f"round,pad=0,rounding_size={radius}",
                                fc=color, ec="none", alpha=alpha, zorder=3))

def badge(ax, x, y, text, color=C_BLUE2):
    ax.add_patch(FancyBboxPatch((x-0.05, y-0.22), 0.55, 0.4,
                                boxstyle="round,pad=0,rounding_size=0.1",
                                fc=color, ec="none", zorder=5))
    ax.text(x+0.23, y-0.02, text, color=C_WHITE, fontsize=11,
            ha="center", va="center", fontweight="bold", zorder=6)

def bullet_list(ax, items, x=0.5, y_start=7.5, dy=0.62,
                fsize=13.5, indent_color=C_BLUE2):
    for i, item in enumerate(items):
        yt = y_start - i * dy
        if isinstance(item, tuple):
            ind, text = item
        else:
            ind, text = 0, item
        lx = x + ind * 0.55
        dot_color = C_ACCENT if ind == 0 else indent_color
        dot = "▶" if ind == 0 else "–"
        ax.text(lx, yt, dot, color=dot_color,
                fontsize=fsize - (1 if ind else 0),
                va="top", fontweight="bold", zorder=4)
        ax.text(lx + 0.45, yt, text,
                color=C_TEXT if ind == 0 else C_SUB,
                fontsize=fsize - (1 if ind else 0),
                va="top", wrap=True, zorder=4,
                fontweight="bold" if ind == 0 else "normal")

def two_cols(ax, left_title, left_items, right_title, right_items,
             fsize=13.0):
    # левая панель
    panel(ax, 0.3, 0.5, 7.55, 7.35)
    ax.text(0.7, 7.6, left_title, color=C_ACCENT, fontsize=14,
            fontweight="bold", va="top", zorder=5)
    gold_line(ax, y=7.3, x0=0.7, x1=7.6, lw=1.5)
    for i, item in enumerate(left_items):
        yt = 6.95 - i * 0.75
        if isinstance(item, tuple):
            ind, text = item
        else:
            ind, text = 0, item
        lx = 0.7 + ind * 0.4
        dot = "▶" if ind == 0 else "–"
        ax.text(lx, yt, dot, color=C_ACCENT if ind == 0 else C_BLUE2,
                fontsize=fsize - 1, va="top", fontweight="bold", zorder=5)
        ax.text(lx + 0.38, yt, text,
                color=C_TEXT if ind == 0 else C_SUB,
                fontsize=fsize - 1, va="top",
                fontweight="bold" if ind == 0 else "normal", zorder=5)
    # правая панель
    panel(ax, 8.15, 0.5, 7.55, 7.35)
    ax.text(8.55, 7.6, right_title, color=C_ACCENT, fontsize=14,
            fontweight="bold", va="top", zorder=5)
    gold_line(ax, y=7.3, x0=8.55, x1=15.4, lw=1.5)
    for i, item in enumerate(right_items):
        yt = 6.95 - i * 0.75
        if isinstance(item, tuple):
            ind, text = item
        else:
            ind, text = 0, item
        lx = 8.55 + ind * 0.4
        dot = "▶" if ind == 0 else "–"
        ax.text(lx, yt, dot, color=C_ACCENT if ind == 0 else C_BLUE2,
                fontsize=fsize - 1, va="top", fontweight="bold", zorder=5)
        ax.text(lx + 0.38, yt, text,
                color=C_TEXT if ind == 0 else C_SUB,
                fontsize=fsize - 1, va="top",
                fontweight="bold" if ind == 0 else "normal", zorder=5)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 1 — ТИТУЛЬНЫЙ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)

# декоративные круги-"портал"
for r, alpha in [(4.5, 0.06), (3.6, 0.09), (2.7, 0.13), (1.8, 0.18)]:
    ax.add_patch(plt.Circle((13.5, 4.5), r, color=C_BLUE2,
                             fill=False, lw=1.5, alpha=alpha, zorder=1))
# "трубки" — декоративные линии
for xi, col in [(12.2, C_ACCENT), (13.7, C_BLUE2), (15.1, C_ACCENT)]:
    ax.plot([xi, xi], [0.5, 8.5], color=col, lw=1.2, alpha=0.18, zorder=1)

# цветная шапка
ax.add_patch(mpatches.Rectangle((0, 5.8), 10.5, 3.2,
                                 fc=C_PANEL, ec="none", zorder=2))
gold_line(ax, y=5.8, x0=0, x1=10.5, lw=3)

ax.text(0.5, 8.6, "Д И П Л О М Н А Я   Р А Б О Т А", color=C_ACCENT,
        fontsize=14, fontweight="bold", va="top", zorder=4)
ax.text(0.5, 7.85,
        "РАЗРАБОТКА ТЕХНОЛОГИЧЕСКОГО\nПРОЦЕССА НА ИЗГОТОВЛЕНИЕ\nРЕЗЕРВУАРОВ",
        color=C_WHITE, fontsize=24, fontweight="bold",
        va="top", zorder=4, linespacing=1.35)

# карточка студента
panel(ax, 0, 0.45, 11.0, 5.2, color="#0F2035")
gold_line(ax, y=5.5, x0=0.5, x1=10.5, lw=1.5)

for label, val, yy in [
    ("Студент:",        "Дроздов Дмитрий Александрович",     5.1),
    ("Группа:",         "19СВС",                              4.5),
    ("Специальность:",  "Сварщик (ручная и частично",         3.9),
    ("",                "механизированная сварка)",           3.4),
    ("Год:",            "2026",                               2.8),
]:
    if label:
        ax.text(0.6, yy, label, color=C_SUB, fontsize=13, va="top",
                fontweight="bold", zorder=4)
    ax.text(3.8, yy, val, color=C_TEXT, fontsize=13, va="top", zorder=4)

footer(ax)
save(fig, 1)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 2 — АКТУАЛЬНОСТЬ И ЦЕЛЬ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Актуальность темы и цель работы", "Слайд 2")

panel(ax, 0.3, 0.5, 7.55, 7.35)
ax.text(0.7, 7.55, "Актуальность", color=C_ACCENT,
        fontsize=14, fontweight="bold", va="top", zorder=5)
gold_line(ax, y=7.28, x0=0.7, x1=7.55, lw=1.5)

act = [
    "Резервуары применяются повсеместно:\nнефтегаз, химия, пищепром, водоснабжение",
    "Дефект шва → утечка, коррозия,\nавария, угроза жизни и экологии",
    "Нужен системный техпроцесс:\nкаждая операция контролируется",
    "Современное производство требует\nрасчёта режимов, а не работы «на глаз»",
]
for i, t in enumerate(act):
    y = 6.9 - i * 1.38
    panel(ax, 0.5, y - 0.9, 7.1, 1.1, color="#0F2A44", radius=0.15)
    ax.text(0.85, y - 0.1, t, color=C_TEXT, fontsize=12.5,
            va="top", zorder=5, linespacing=1.3)

panel(ax, 8.15, 0.5, 7.55, 7.35)
ax.text(8.55, 7.55, "Цель и задачи", color=C_ACCENT,
        fontsize=14, fontweight="bold", va="top", zorder=5)
gold_line(ax, y=7.28, x0=8.55, x1=15.4, lw=1.5)

tasks = [
    ("Цель", "Разработать технологический\nпроцесс изготовления резервуаров"),
    ("З1", "Классификация и назначение"),
    ("З2", "Материалы и оборудование"),
    ("З3", "Расчёт режимов сварки"),
    ("З4", "Последовательность операций"),
    ("З5", "Методы контроля качества"),
    ("З6", "Требования охраны труда"),
]
for i, (num, text) in enumerate(tasks):
    y = 6.9 - i * 0.95
    badge_color = C_ACCENT if num == "Цель" else C_BLUE2
    ax.add_patch(FancyBboxPatch((8.45, y - 0.35), 0.85, 0.46,
                                boxstyle="round,pad=0,rounding_size=0.1",
                                fc=badge_color, ec="none", zorder=5))
    ax.text(8.87, y - 0.12, num, color=C_BG if num == "Цель" else C_WHITE,
            fontsize=9.5, ha="center", va="center", fontweight="bold", zorder=6)
    ax.text(9.55, y - 0.12, text, color=C_TEXT if num == "Цель" else C_SUB,
            fontsize=12, va="center",
            fontweight="bold" if num == "Цель" else "normal", zorder=5)

footer(ax, "2 / 13")
save(fig, 2)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 3 — КЛАССИФИКАЦИЯ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Резервуары: назначение и классификация", "Слайд 3")

two_cols(ax,
    "По конструкции и расположению",
    [
        "Вертикальные цилиндрические —\nнаиболее распространены, большие объёмы",
        "Горизонтальные — ограниченный объём,\nподземная установка",
        "Сферические — газы под давлением,\nравномерное распределение напряжений",
        "Наземные — простой доступ и контроль",
        "Подземные — защита от t° и ударов",
    ],
    "По давлению и назначению",
    [
        "Атмосферные — вода, нефть,\nбольшинство жидкостей",
        "Под давлением — сжиженные газы,\nлетучие вещества",
        "Товарные — хранение готовой\nпродукции перед отгрузкой",
        "Технологические — смешивание,\nнагрев, отстаивание",
        "Аварийно-резервные — запасы\nна случай нештатных ситуаций",
    ],
    fsize=12.5,
)

footer(ax, "3 / 13")
save(fig, 3)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 4 — МАТЕРИАЛЫ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Основные материалы для изготовления резервуаров", "Слайд 4")

mats = [
    (C_ACCENT,  "УГЛЕРОДИСТАЯ\nСТАЛЬ",
     "Самый распространённый материал. Высокая\nпрочность, хорошая свариваемость, "
     "доступность.\nНефть, вода, нефтепродукты.\nТребуется антикоррозионная защита."),
    (C_BLUE2,   "НИЗКО-\nЛЕГИРОВАННАЯ",
     "Повышенная прочность и ударная вязкость.\nЛучше сваривается при низких "
     "температурах.\nРекомендуется для ответственных конструкций."),
    ("#2EAA78",  "НЕРЖАВЕЮЩАЯ\nСТАЛЬ",
     "Агрессивные среды, пищевая промышленность.\nКоррозионная стойкость, "
     "гигиеничность.\nВысокая стоимость. Сложнее в сварке."),
    ("#8E44AD",  "АЛЮМИНИЙ",
     "Лёгкость, стойкость к коррозии.\nРабота при низких температурах.\n"
     "Меньшая прочность по сравнению со сталью."),
]
for i, (col, name, desc) in enumerate(mats):
    x0 = 0.3 + i * 3.9
    panel(ax, x0, 0.5, 3.65, 7.35, color=C_PANEL)
    # цветная плашка-заголовок
    ax.add_patch(FancyBboxPatch((x0, 5.9), 3.65, 1.95,
                                boxstyle="round,pad=0,rounding_size=0.18",
                                fc=col, ec="none", alpha=0.85, zorder=4))
    ax.text(x0 + 1.82, 6.95, name, color=C_WHITE,
            fontsize=13.5, fontweight="bold", ha="center", va="center",
            zorder=5, linespacing=1.3)
    ax.text(x0 + 0.2, 5.6, desc, color=C_TEXT, fontsize=11.5,
            va="top", zorder=4, linespacing=1.35)

ax.text(8.0, 0.72,
        "Требования: соответствие стандартам по хим. составу, прочности, "
        "свариваемости, ударной вязкости",
        color=C_ACCENT, fontsize=11, ha="center", va="bottom",
        fontweight="bold", zorder=4)

footer(ax, "4 / 13")
save(fig, 4)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 5 — СВАРОЧНОЕ ОБОРУДОВАНИЕ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Сварочное оборудование и методы сварки", "Слайд 5")

two_cols(ax,
    "Способы сварки",
    [
        "Полуавтомат MIG/MAG (135/136):\nосновной метод, длинные швы, высокая\nпроизводительность, стабильная дуга",
        "Аргонодуговая TIG (141):\nответственные швы, корневые проходы,\nминимум дефектов",
        "Ручная дуговая MMA (111):\nприхватки, труднодоступные места,\nремонт — универсальна",
        "Автоматическая под флюсом:\nкрупносерийное производство",
    ],
    "Оборудование и инструменты",
    [
        "Инверторные источники питания —\nстабильность дуги, точная регулировка",
        "Механизмы подачи проволоки,\nгорелки, редукторы, баллоны с CO₂",
        "Шлифмашины, зачистные круги —\nподготовка кромок",
        "Сборочные стенды, струбцины,\nкондукторы, шаблоны",
        "Контрольно-измерительный\nинструмент: линейки, уровни, шаблоны",
    ],
    fsize=12.5,
)

footer(ax, "5 / 13")
save(fig, 5)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 6 — ПОДГОТОВКА МЕТАЛЛА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Этап 1: Подготовка металла и заготовительные работы", "Слайд 6")

steps = [
    (C_ACCENT,  "1", "Приёмка металла",
     "Проверка соответствия стандартам и проектной документации"),
    (C_BLUE2,   "2", "Очистка поверхности",
     "Удаление ржавчины, окалины, масла, влаги — загрязнения вызывают поры и непровары"),
    ("#2EAA78",  "3", "Разметка и резка",
     "Механическая, газовая или плазменная резка по чертежам и техкартам"),
    ("#C0392B",  "4", "Обработка кромок",
     "Удаление заусенцев, разделка под сварку, фаски для полного провара"),
    ("#8E44AD",  "5", "Формирование деталей",
     "Вальцовка обечаек, формирование днища и крыши на спецоборудовании"),
    ("#1A8FAA",  "6", "Прихватки",
     "Временная фиксация деталей короткими швами — сохранение геометрии"),
]

for i, (col, num, title, desc) in enumerate(steps):
    row, col_n = divmod(i, 3)
    x0 = 0.3 + col_n * 5.25
    y0 = 4.2 - row * 3.5
    panel(ax, x0, y0, 5.0, 3.3, color=C_PANEL)
    ax.add_patch(FancyBboxPatch((x0, y0 + 2.58), 5.0, 0.72,
                                boxstyle="round,pad=0,rounding_size=0.15",
                                fc=col, ec="none", zorder=4))
    ax.text(x0 + 0.42, y0 + 2.94, num, color=C_WHITE,
            fontsize=18, fontweight="bold", va="center", ha="center", zorder=5)
    ax.text(x0 + 0.9, y0 + 2.94, title, color=C_WHITE,
            fontsize=13, fontweight="bold", va="center", zorder=5)
    ax.text(x0 + 0.25, y0 + 2.3, desc, color=C_TEXT,
            fontsize=11.5, va="top", zorder=4, linespacing=1.3)

footer(ax, "6 / 13")
save(fig, 6)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 7 — СБОРКА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Этап 2: Сборка конструкции резервуара", "Слайд 7")

bullet_list(ax, [
    "Установка деталей в проектное положение на сборочных стендах со струбцинами, "
    "шаблонами и кондукторами",
    (1, "Любые отклонения по размерам создают дополнительные напряжения при эксплуатации"),
    "Порядок сборки: обечайки корпуса → днище → кровля → патрубки → усиливающие элементы",
    "Продольные швы соединяют листы по высоте — воспринимают нагрузки от давления",
    "Кольцевые швы соединяют пояса обечаек — несут напряжения от гидростатического давления",
    (1, "Многопроходная сварка: 1-й проход — провар корня, последующие — толщина и прочность"),
    "Снижение деформаций: симметричная последовательность швов, обратноступенчатый метод",
    (1, "Коробление и остаточные напряжения — главные враги правильной геометрии корпуса"),
], fsize=13.5)

footer(ax, "7 / 13")
save(fig, 7)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 8 — РЕЖИМЫ СВАРКИ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Расчёт параметров режима сварки", "Слайд 8")

rows = [
    ("Параметр",           "Значение",           "Обоснование"),
    ("Способ сварки",      "Полуавтомат MAG",    "Высокая производительность, длинные швы"),
    ("Сварочная проволока","Св-08Г2С  ∅ 1,2 мм", "Устойчивая дуга, хорошее формирование шва"),
    ("Толщина металла",    "~ 8 мм",             "Стенки корпуса резервуара"),
    ("Сварочный ток",      "~ 200 А",            "Достаточный провар стенки 8 мм"),
    ("Напряжение дуги",    "~ 28 В",             "Стабильность процесса, нормальная ширина шва"),
    ("Скорость сварки",    "~ 30 м/ч",           "Качественный шов, снижение деформаций"),
    ("Расход CO₂",         "~ 14 л/мин",         "Надёжная защита ванны от окисления"),
    ("Полярность",         "Постоянный, обратная","Глубокий провар, минимум разбрызгивания"),
]

col_x = [0.4, 5.0, 8.65]
col_w = [4.4, 3.45, 7.0]
row_h = 0.72
y_top = 7.68

for r_i, row_data in enumerate(rows):
    y = y_top - r_i * row_h
    is_hdr = (r_i == 0)
    bg = DARK_BLUE = "#0D1B2A" if is_hdr else (C_PANEL if r_i % 2 else "#0F2035")
    for c_i, (text, cx, cw) in enumerate(zip(row_data, col_x, col_w)):
        ax.add_patch(mpatches.FancyBboxPatch((cx, y - row_h + 0.04), cw - 0.08, row_h - 0.05,
                                              boxstyle="round,pad=0,rounding_size=0.08",
                                              fc=bg, ec="none", zorder=3))
        col_clr = C_ACCENT if (is_hdr or (c_i == 1 and not is_hdr)) else C_TEXT
        fw = "bold" if (is_hdr or c_i == 1) else "normal"
        ax.text(cx + 0.18, y - row_h / 2, text,
                color=col_clr, fontsize=12.5 if is_hdr else 12,
                va="center", fontweight=fw, zorder=4)

footer(ax, "8 / 13")
save(fig, 8)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 9 — СВАРКА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Этап 3: Выполнение сварочных работ", "Слайд 9")

bullet_list(ax, [
    "Сварка — ключевой этап: от прочности швов зависит герметичность "
    "и безопасность всего резервуара",
    "Продольные швы корпуса выполняются в первую очередь, "
    "затем кольцевые соединения обечаек",
    (1, "1-й проход — провар корня шва; последующие — толщина и прочность соединения"),
    "Применяемые соединения: стыковые (листы корпуса), угловые и тавровые "
    "(патрубки, усилители, опоры)",
    "CO₂ защищает ванну от кислорода → снижается пористость, "
    "улучшаются механические свойства шва",
    "Постоянный ток обратной полярности: ток от изделия к электроду → "
    "глубокий провар, меньше брызг",
    (1, "Квалификация сварщика: допуск к ответственным конструкциям обязателен"),
    "После сварки — зачистка шлака, брызг и неровностей; "
    "плавный переход к основному металлу",
], fsize=13.5)

footer(ax, "9 / 13")
save(fig, 9)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 10 — КОНТРОЛЬ КАЧЕСТВА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Контроль качества готового изделия", "Слайд 10")

methods = [
    (C_ACCENT,  "ВИК",    "Визуально-измерительный контроль",
     "Трещины, подрезы, наплывы,\nпрожоги, геометрия швов"),
    (C_BLUE2,   "УЗК",    "Ультразвуковой контроль",
     "Внутренние дефекты: непровары,\nпоры, трещины, шлак"),
    ("#2EAA78",  "РК",     "Радиографический контроль",
     "Просвечивание рентгеном,\nобъективная оценка структуры"),
    ("#C0392B",  "КК",     "Капиллярный контроль",
     "Поверхностные микротрещины,\nневидимые при осмотре"),
]

for i, (col, abbr, full, desc) in enumerate(methods):
    x0 = 0.3 + i * 3.85
    panel(ax, x0, 3.7, 3.65, 4.1, color=C_PANEL)
    ax.add_patch(plt.Circle((x0 + 1.82, 6.8), 0.9,
                             color=col, zorder=4, alpha=0.9))
    ax.text(x0 + 1.82, 6.8, abbr, color=C_WHITE,
            fontsize=16, fontweight="bold", ha="center", va="center", zorder=5)
    ax.text(x0 + 1.82, 5.9, full, color=C_TEXT,
            fontsize=11.5, fontweight="bold", ha="center", va="top",
            zorder=4, linespacing=1.25)
    ax.text(x0 + 0.22, 5.35, desc, color=C_SUB,
            fontsize=11, va="top", zorder=4, linespacing=1.3)

# Нижняя строка — испытания
panel(ax, 0.3, 0.5, 7.5, 3.05, color=C_PANEL)
ax.text(0.7, 3.3, "Гидравлическое испытание", color=C_ACCENT,
        fontsize=13, fontweight="bold", va="top", zorder=5)
ax.text(0.7, 2.85,
        "Заполнение водой под расчётным давлением.\n"
        "Проверка: отсутствие протечек, деформаций, падения давления.",
        color=C_TEXT, fontsize=11.5, va="top", zorder=4, linespacing=1.3)

panel(ax, 8.1, 0.5, 7.5, 3.05, color=C_PANEL)
ax.text(8.5, 3.3, "Пневматическое испытание", color=C_ACCENT,
        fontsize=13, fontweight="bold", va="top", zorder=5)
ax.text(8.5, 2.85,
        "Сжатый воздух + мыльный раствор на швах.\n"
        "Строгое соблюдение правил безопасности.",
        color=C_TEXT, fontsize=11.5, va="top", zorder=4, linespacing=1.3)

footer(ax, "10 / 13")
save(fig, 10)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 11 — АНТИКОРРОЗИОННАЯ ЗАЩИТА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Антикоррозионная защита и монтаж элементов", "Слайд 11")

bullet_list(ax, [
    "Резервуары эксплуатируются на открытом воздухе или контактируют "
    "с агрессивными веществами — антикоррозионная защита обязательна",
    "Наружная защита: очистка → грунтовка → лакокрасочные или "
    "антикоррозионные покрытия",
    (1, "Нефтепродукты и химия — эпоксидные составы, футеровка с хим. стойкостью"),
    "Монтаж технологических элементов:\nлюки, патрубки, запорная арматура, "
    "дыхательные клапаны",
    "Монтаж эксплуатационных элементов:\nлестницы, площадки обслуживания, "
    "ограждения, перила",
    "Проверка комплектности по проектной документации до ввода в эксплуатацию",
    (1, "Составление акта приёмки и технической документации — обязательный финал"),
], fsize=13.5)

footer(ax, "11 / 13")
save(fig, 11)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 12 — ТЕХНИКА БЕЗОПАСНОСТИ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Техника безопасности при сварке резервуаров", "Слайд 12")

dangers = [
    (C_ACCENT,  "~",   "Электрический\nток",    "Заземление оборудования,\nпроверка изоляции"),
    (C_BLUE2,   "UV",  "УФ и ИК\nизлучение",    "Маска со светофильтром,\nогнестойкая одежда"),
    ("#C0392B",  "GAS", "Вредные\nгазы/аэрозоли","Принудительная\nвентиляция"),
    ("#2EAA78",  "!",   "Пожар\nи взрыв",        "Очистка рабочего места,\nсредства пожаротушения"),
]

for i, (col, icon, danger, protect) in enumerate(dangers):
    x0 = 0.3 + i * 3.85
    panel(ax, x0, 3.8, 3.65, 4.0, color=C_PANEL)
    ax.add_patch(FancyBboxPatch((x0 + 0.2, 6.5), 3.25, 1.1,
                                boxstyle="round,pad=0,rounding_size=0.15",
                                fc=col, ec="none", alpha=0.85, zorder=4))
    ax.text(x0 + 1.82, 7.1, icon + "  " + danger,
            color=C_WHITE, fontsize=13, fontweight="bold",
            ha="center", va="center", zorder=5, linespacing=1.2)
    ax.text(x0 + 0.35, 6.2, protect, color=C_TEXT,
            fontsize=12.5, va="top", zorder=4, linespacing=1.35,
            fontweight="bold")

# нижняя плашка — работа внутри резервуара
panel(ax, 0.3, 0.5, 15.4, 3.15, color="#1A0A08")
ax.add_patch(FancyBboxPatch((0.3, 3.25), 15.4, 0.06,
                             boxstyle="square,pad=0",
                             fc="#C0392B", ec="none", zorder=4))
ax.text(0.7, 3.4, "⚠  РАБОТА ВНУТРИ РЕЗЕРВУАРА",
        color="#C0392B", fontsize=14, fontweight="bold", va="top", zorder=5)
ax.text(0.7, 2.9,
        "Очистка и проветривание ёмкости · Анализ воздуха на взрывоопасные вещества · "
        "Наряд-допуск\n"
        "Наблюдающий снаружи · Принудительная вентиляция · Страховочные "
        "приспособления · Диэлектрические средства",
        color=C_TEXT, fontsize=12, va="top", zorder=4, linespacing=1.4)

footer(ax, "12 / 13")
save(fig, 12)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 13 — ЗАКЛЮЧЕНИЕ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
bg_grid(ax)
header(ax, "Заключение", "Слайд 13")

conclusions = [
    (C_ACCENT,  "01", "Разработан полный технологический\nпроцесс изготовления резервуаров",
     "Подготовка металла → сборка → сварка → контроль → испытания → защита"),
    (C_BLUE2,   "02", "Подобраны параметры\nполуавтоматической сварки",
     "I = 200 А  ·  U = 28 В  ·  v = 30 м/ч  ·  CO₂ = 14 л/мин  ·  Св-08Г2С ∅1,2 мм"),
    ("#2EAA78",  "03", "Качество обеспечивается\nкомплексным подходом",
     "Правильный материал · точная сборка · режимы сварки · контроль швов"),
    ("#8E44AD",  "04", "Соблюдение охраны труда\nснижает риски",
     "Допуск, СИЗ, вентиляция, наряд-допуск, наблюдающий при работах в ёмкости"),
]

for i, (col, num, title, sub) in enumerate(conclusions):
    row, cn = divmod(i, 2)
    x0 = 0.3 + cn * 7.85
    y0 = 4.1 - row * 3.4
    panel(ax, x0, y0, 7.55, 3.15, color=C_PANEL)
    ax.add_patch(plt.Circle((x0 + 0.7, y0 + 2.5), 0.52,
                             color=col, zorder=4))
    ax.text(x0 + 0.7, y0 + 2.5, num, color=C_WHITE,
            fontsize=14, fontweight="bold",
            ha="center", va="center", zorder=5)
    ax.text(x0 + 1.45, y0 + 2.3, title,
            color=C_TEXT, fontsize=13, fontweight="bold",
            va="top", zorder=4, linespacing=1.3)
    ax.text(x0 + 0.3, y0 + 0.25, sub,
            color=C_SUB, fontsize=11.5, va="bottom", zorder=4, linespacing=1.3)

ax.text(8.0, 0.62,
        "Доклад окончен · Благодарю за внимание · Готов ответить на вопросы",
        color=C_ACCENT, fontsize=14, fontweight="bold",
        ha="center", va="bottom", zorder=4)

footer(ax, "13 / 13")
save(fig, 13)

print(f"Готово! Сохранено {len(slide_paths)} слайдов:")
for p in slide_paths:
    print(" ", p)
