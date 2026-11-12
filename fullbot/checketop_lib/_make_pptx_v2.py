# -*- coding: utf-8 -*-
import os, shutil
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg
from matplotlib.patches import FancyBboxPatch
from pptx import Presentation
from pptx.util import Emu

plt.rcParams["font.family"] = "DejaVu Sans"

IMG = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_pptx_preview"
OUT_DIR = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_slides_v2"
os.makedirs(OUT_DIR, exist_ok=True)

# Цвета как в примере — чисто, светло
C_BG       = "#FFFFFF"
C_HEADER   = "#1F3864"   # тёмно-синий (как тема Office «Синий»)
C_ACCENT   = "#2E75B6"   # голубой
C_TEXT     = "#1A1A1A"
C_WHITE    = "#FFFFFF"
C_SUBTEXT  = "#444444"
C_LINE     = "#2E75B6"

W, H = 16, 9
slide_paths = []

def new_fig():
    fig = plt.figure(figsize=(W, H), facecolor=C_BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W); ax.set_ylim(0, H)
    ax.axis("off")
    ax.set_facecolor(C_BG)
    return fig, ax

def save(fig, idx):
    p = os.path.join(OUT_DIR, f"slide_{idx:02d}.png")
    fig.savefig(p, dpi=150, bbox_inches="tight", pad_inches=0,
                facecolor=C_BG)
    plt.close(fig)
    slide_paths.append(p)

def header_strip(ax, title, subtitle=""):
    """Синяя полоса сверху с заголовком."""
    ax.add_patch(mpatches.Rectangle((0, H-1.35), W, 1.35,
                                    fc=C_HEADER, ec="none", zorder=2))
    ax.plot([0, W], [H-1.35, H-1.35], color=C_ACCENT, lw=4, zorder=3)
    ax.text(0.45, H-0.68, title,
            color=C_WHITE, fontsize=26, fontweight="bold",
            va="center", zorder=4)
    if subtitle:
        ax.text(0.45, H-1.1, subtitle,
                color="#A8C4E0", fontsize=13, va="center",
                style="italic", zorder=4)

def footer(ax, num=""):
    ax.add_patch(mpatches.Rectangle((0, 0), W, 0.38,
                                    fc=C_HEADER, ec="none", zorder=2))
    ax.text(0.4, 0.19,
            "Дроздов Д.А.  ·  группа 19СВС  ·  "
            "Разработка технологического процесса на изготовление резервуаров",
            color="#A8C4E0", fontsize=8.5, va="center", zorder=3)
    if num:
        ax.text(W-0.35, 0.19, num,
                color=C_WHITE, fontsize=9, va="center",
                ha="right", fontweight="bold", zorder=3)

def place_photo(ax, path, x=1.0, y=0.45, w=14.0, h=7.0):
    """Вставляет фото в указанную область."""
    try:
        img = mpimg.imread(path)
        ax.imshow(img, extent=[x, x+w, y, y+h],
                  aspect="auto", zorder=1, interpolation="lanczos")
    except Exception as e:
        ax.add_patch(mpatches.Rectangle((x, y), w, h,
                     fc="#CCCCCC", ec="none", zorder=1))
        ax.text(x+w/2, y+h/2, "фото", ha="center", va="center",
                color="#888888", fontsize=20, zorder=2)

def bullets(ax, items, x=0.5, y_start=7.4, dy=0.72,
            fsize=15, color=C_TEXT):
    for i, item in enumerate(items):
        yt = y_start - i*dy
        ax.plot([x+0.08], [yt-0.12], "s",
                color=C_ACCENT, markersize=7, zorder=4)
        ax.text(x+0.32, yt-0.05, item,
                color=color, fontsize=fsize, va="top",
                zorder=4, fontweight="bold" if i == 0 else "normal")

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 1 — ТИТУЛЬНЫЙ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()

# фото резервуаров — на весь фон, приглушённое
place_photo(ax, os.path.join(IMG,"image1.jpeg"), 0, 0, W, H)
# затемняющий оверлей
ax.add_patch(mpatches.Rectangle((0,0), W, H,
             fc="#000000", alpha=0.48, ec="none", zorder=1))

# синяя плашка заголовка
ax.add_patch(FancyBboxPatch((0.6, 3.5), 14.7, 3.8,
             boxstyle="round,pad=0,rounding_size=0.2",
             fc=C_HEADER, alpha=0.92, ec="none", zorder=2))
ax.plot([0.6, 15.3], [6.95, 6.95], color=C_ACCENT, lw=3, zorder=3)
ax.plot([0.6, 15.3], [3.55, 3.55], color=C_ACCENT, lw=3, zorder=3)

ax.text(8.0, 6.55,
        "ДИПЛОМНАЯ РАБОТА",
        color=C_ACCENT, fontsize=14, fontweight="bold",
        ha="center", va="center", zorder=4)

ax.text(8.0, 5.6,
        "Разработка технологического процесса",
        color=C_WHITE, fontsize=27, fontweight="bold",
        ha="center", va="center", zorder=4)
ax.text(8.0, 4.95,
        "на изготовление резервуаров",
        color=C_WHITE, fontsize=27, fontweight="bold",
        ha="center", va="center", zorder=4)

ax.text(8.0, 4.15,
        "Студент: Дроздов Дмитрий Александрович   ·   Группа 19СВС   ·   2026 г.",
        color="#A8C4E0", fontsize=13,
        ha="center", va="center", zorder=4)

ax.text(8.0, 0.19,
        "Специальность: Сварщик (ручная и частично механизированная сварка)",
        color="#A8C4E0", fontsize=9, ha="center", va="center", zorder=3)
ax.add_patch(mpatches.Rectangle((0,0), W, 0.38,
             fc=C_HEADER, alpha=0.9, ec="none", zorder=2))

save(fig, 1)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 2 — РЕЗЕРВУАРЫ: НАЗНАЧЕНИЕ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Резервуары и их назначение", "Объект разработки")
footer(ax, "2 / 9")

place_photo(ax, os.path.join(IMG,"image1.jpeg"), 7.5, 0.45, 8.3, 7.45)

# затемнение только под фото для читаемости
ax.add_patch(mpatches.Rectangle((7.5, 0.45), 8.3, 7.45,
             fc="#000000", alpha=0.08, ec="none", zorder=2))

# текст слева
lines = [
    "Крупные ёмкости для хранения",
    "жидкостей и газов",
    "",
    "Применение:",
    "· нефтегазовая промышленность",
    "· химическое производство",
    "· пищевая промышленность",
    "· водоснабжение и энергетика",
    "· системы пожарной безопасности",
    "",
    "Материал: углеродистая и",
    "низколегированная сталь",
]
for i, line in enumerate(lines):
    y = 7.55 - i*0.55
    if line == "":
        continue
    bold = line in ("Применение:", "Крупные ёмкости для хранения",)
    col  = C_ACCENT if line == "Применение:" else C_TEXT
    ax.text(0.5, y, line, color=col, fontsize=14,
            va="top", fontweight="bold" if bold else "normal", zorder=4)

save(fig, 2)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 3 — ВЫБОР СПОСОБА СВАРКИ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Выбор способа сварки и материалов", "Раздел 1")
footer(ax, "3 / 9")

place_photo(ax, os.path.join(IMG,"image7.jpeg"), 7.8, 0.45, 7.95, 7.45)
ax.add_patch(mpatches.Rectangle((7.8, 0.45), 7.95, 7.45,
             fc="#000000", alpha=0.12, ec="none", zorder=2))

rows = [
    ("Основной метод:",    "Полуавтоматическая сварка MAG"),
    ("Защитный газ:",      "Углекислый газ (CO₂)"),
    ("Проволока:",         "Св-08Г2С"),
    ("Доп. метод:",        "Аргонодуговая TIG — ответственные швы"),
    ("Труднодост. места:", "Ручная дуговая MMA"),
]
y0 = 7.55
for label, val in rows:
    ax.text(0.5, y0, label, color=C_ACCENT, fontsize=13,
            va="top", fontweight="bold", zorder=4)
    ax.text(3.5, y0, val, color=C_TEXT, fontsize=13,
            va="top", zorder=4)
    ax.plot([0.5, 7.4], [y0-0.55, y0-0.55],
            color="#DDDDDD", lw=0.8, zorder=3)
    y0 -= 0.72

ax.text(0.5, y0-0.1,
        "Электрод подаётся автоматически.\n"
        "Газ защищает ванну от воздуха.\n"
        "Шов — ровный, плотный, без пор.",
        color=C_SUBTEXT, fontsize=12, va="top", zorder=4,
        linespacing=1.5)

save(fig, 3)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 4 — ПОДГОТОВКА МЕТАЛЛА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Подготовка металла", "Раздел 2 — этап 1")
footer(ax, "4 / 9")

place_photo(ax, os.path.join(IMG,"image3.jpeg"), 8.2, 0.45, 7.55, 7.45)
ax.add_patch(mpatches.Rectangle((8.2, 0.45), 7.55, 7.45,
             fc="#000000", alpha=0.1, ec="none", zorder=2))

steps = [
    "1.  Входной контроль металла",
    "2.  Очистка от ржавчины, масла, окалины",
    "3.  Разметка по чертежам",
    "4.  Резка: механическая / газовая / плазма",
    "5.  Обработка кромок — разделка под сварку",
    "6.  Вальцовка листов в обечайки",
    "7.  Формирование днища и крыши",
    "8.  Сборка на стендах, прихватки",
]
for i, s in enumerate(steps):
    y = 7.5 - i*0.82
    ax.add_patch(FancyBboxPatch((0.4, y-0.52), 7.5, 0.6,
                 boxstyle="round,pad=0,rounding_size=0.1",
                 fc="#EBF3FB", ec=C_ACCENT, lw=0.6, zorder=3))
    ax.text(0.65, y-0.18, s, color=C_TEXT, fontsize=12.5,
            va="center", zorder=4)

save(fig, 4)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 5 — СБОРКА И СВАРКА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Сборка и сварка резервуара", "Раздел 2 — этапы 2–3")
footer(ax, "5 / 9")

place_photo(ax, os.path.join(IMG,"image4.jpeg"), 0, 0.38, W, H-0.38)
ax.add_patch(mpatches.Rectangle((0, 0.38), W, H-0.38,
             fc="#000000", alpha=0.52, ec="none", zorder=2))

cards = [
    (0.4,  4.8, "Продольные швы",
     "Соединяют листы по\nвысоте стенки корпуса"),
    (4.3,  4.8, "Кольцевые швы",
     "Соединяют пояса обечаек,\nвоспринимают давление"),
    (8.2,  4.8, "Многопроходная сварка",
     "1-й проход — корень шва,\nследующие — толщина"),
    (12.1, 4.8, "Против деформаций",
     "Симметричная укладка\nи обратноступенчатый метод"),
]
for x, y, title, desc in cards:
    ax.add_patch(FancyBboxPatch((x, y-1.55), 3.7, 2.65,
                 boxstyle="round,pad=0,rounding_size=0.15",
                 fc=C_HEADER, alpha=0.88, ec=C_ACCENT, lw=1.2, zorder=3))
    ax.plot([x+0.1, x+3.6], [y+0.98, y+0.98],
            color=C_ACCENT, lw=2, zorder=4)
    ax.text(x+1.85, y+0.72, title, color=C_WHITE, fontsize=13,
            fontweight="bold", ha="center", va="center", zorder=5)
    ax.text(x+1.85, y-0.45, desc, color="#C8DCF0", fontsize=11.5,
            ha="center", va="center", zorder=5, linespacing=1.4)

save(fig, 5)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 6 — КОНТРОЛЬ КАЧЕСТВА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Контроль качества готового изделия", "Раздел 3")
footer(ax, "6 / 9")

place_photo(ax, os.path.join(IMG,"image6.jpeg"), 8.5, 0.45, 7.4, 7.45)
ax.add_patch(mpatches.Rectangle((8.5, 0.45), 7.4, 7.45,
             fc="#000000", alpha=0.1, ec="none", zorder=2))

methods = [
    ("#2E75B6", "ВИК",  "Визуальный и измерительный —\nнаружные дефекты, геометрия"),
    ("#1F6B42", "УЗК",  "Ультразвуковой контроль —\nдефекты внутри шва"),
    ("#8B1A1A", "РК",   "Радиографический (рентген) —\nструктура соединения"),
    ("#7B4FA6", "КК",   "Капиллярный —\nповерхностные микротрещины"),
    ("#B06000", "ГИ",   "Гидравлическое испытание —\nпрочность и герметичность"),
]
y0 = 7.5
for col, abbr, desc in methods:
    ax.add_patch(FancyBboxPatch((0.4, y0-0.64), 7.7, 0.72,
                 boxstyle="round,pad=0,rounding_size=0.1",
                 fc="#F4F8FC", ec=col, lw=1.2, zorder=3))
    ax.add_patch(FancyBboxPatch((0.4, y0-0.64), 1.1, 0.72,
                 boxstyle="round,pad=0,rounding_size=0.08",
                 fc=col, ec="none", zorder=4))
    ax.text(0.95, y0-0.28, abbr, color=C_WHITE, fontsize=11,
            ha="center", va="center", fontweight="bold", zorder=5)
    ax.text(1.7, y0-0.26, desc, color=C_TEXT, fontsize=12,
            va="center", zorder=4, linespacing=1.2)
    y0 -= 0.86

save(fig, 6)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 7 — АНТИКОРРОЗИОННАЯ ЗАЩИТА
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Антикоррозионная защита и монтаж", "Раздел 4")
footer(ax, "7 / 9")

place_photo(ax, os.path.join(IMG,"image5.jpeg"), 7.6, 0.45, 8.1, 7.45)
ax.add_patch(mpatches.Rectangle((7.6, 0.45), 8.1, 7.45,
             fc="#000000", alpha=0.08, ec="none", zorder=2))

items = [
    ("Наружное покрытие",
     "Грунтовка + лакокрасочный состав.\nЗащита от влаги и атмосферы."),
    ("Внутреннее покрытие",
     "Для агрессивных сред — специальные\nхимически стойкие составы, футеровка."),
    ("Технологические элементы",
     "Люки, патрубки, запорная арматура,\nдыхательные клапаны."),
    ("Эксплуатационные элементы",
     "Лестницы, площадки обслуживания,\nограждения."),
    ("Финальный контроль",
     "Проверка комплектности по проекту.\nОформление акта приёмки."),
]
y0 = 7.55
for title, desc in items:
    ax.text(0.5, y0, title, color=C_ACCENT, fontsize=13,
            va="top", fontweight="bold", zorder=4)
    ax.text(0.5, y0-0.42, desc, color=C_SUBTEXT, fontsize=12,
            va="top", zorder=4, linespacing=1.3)
    ax.plot([0.5, 7.1], [y0-1.05, y0-1.05],
            color="#DDDDDD", lw=0.8, zorder=3)
    y0 -= 1.32

save(fig, 7)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 8 — ТЕХНИКА БЕЗОПАСНОСТИ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()
header_strip(ax, "Техника безопасности", "Раздел 5")
footer(ax, "8 / 9")

place_photo(ax, os.path.join(IMG,"image7.jpeg"), 0, 0.38, W, H-0.38)
ax.add_patch(mpatches.Rectangle((0, 0.38), W, H-0.38,
             fc="#000000", alpha=0.55, ec="none", zorder=2))

dangers = [
    ("#E8A01E", "Электрический\nток",
     "Заземление оборудования,\nпроверка изоляции"),
    ("#2E75B6", "Излучение\nдуги",
     "Маска со светофильтром,\nогнестойкая одежда"),
    ("#C0392B", "Вредные\nгазы",
     "Принудительная\nвентиляция"),
    ("#2EAA78", "Пожар\nи взрыв",
     "Очистка места,\nогнетушители рядом"),
]
for i, (col, danger, protect) in enumerate(dangers):
    x0 = 0.4 + i*3.9
    ax.add_patch(FancyBboxPatch((x0, 3.55), 3.6, 4.15,
                 boxstyle="round,pad=0,rounding_size=0.15",
                 fc=C_HEADER, alpha=0.88, ec=col, lw=2, zorder=3))
    ax.add_patch(FancyBboxPatch((x0+0.15, 6.8), 3.3, 0.75,
                 boxstyle="round,pad=0,rounding_size=0.1",
                 fc=col, alpha=0.9, ec="none", zorder=4))
    ax.text(x0+1.8, 7.2, danger, color=C_WHITE, fontsize=12,
            ha="center", va="center", fontweight="bold",
            linespacing=1.2, zorder=5)
    ax.text(x0+1.8, 5.4, protect, color="#C8DCF0", fontsize=11.5,
            ha="center", va="center", linespacing=1.4, zorder=4)

ax.add_patch(FancyBboxPatch((0.4, 0.5), 15.2, 2.85,
             boxstyle="round,pad=0,rounding_size=0.12",
             fc="#3D0000", alpha=0.85, ec="#C0392B", lw=1.5, zorder=3))
ax.text(0.8, 3.2, "Работа внутри резервуара — особые требования:",
        color="#FF8080", fontsize=13, fontweight="bold",
        va="top", zorder=4)
ax.text(0.8, 2.65,
        "Работы выполняются только по наряду-допуску  ·  "
        "Снаружи дежурит наблюдающий\n"
        "Принудительная вентиляция  ·  "
        "Анализ воздуха на взрывоопасные газы  ·  "
        "Страховочные приспособления",
        color="#FFD0D0", fontsize=12, va="top", zorder=4,
        linespacing=1.5)

save(fig, 8)

# ═══════════════════════════════════════════════════════════════
#  СЛАЙД 9 — ЗАКЛЮЧЕНИЕ
# ═══════════════════════════════════════════════════════════════
fig, ax = new_fig()

place_photo(ax, os.path.join(IMG,"image4.jpeg"), 0, 0, W, H)
ax.add_patch(mpatches.Rectangle((0,0), W, H,
             fc="#000000", alpha=0.60, ec="none", zorder=1))

ax.add_patch(FancyBboxPatch((0.5, 1.5), 15.0, 5.7,
             boxstyle="round,pad=0,rounding_size=0.2",
             fc=C_HEADER, alpha=0.90, ec=C_ACCENT, lw=2, zorder=2))
ax.plot([0.5, 15.5], [6.85, 6.85], color=C_ACCENT, lw=3, zorder=3)

ax.text(8.0, 7.15, "ЗАКЛЮЧЕНИЕ", color=C_WHITE, fontsize=14,
        fontweight="bold", ha="center", va="center", zorder=3)

conclusions = [
    "Разработан полный технологический процесс изготовления резервуаров",
    "Обоснован выбор способа сварки (MAG) и сварочных материалов",
    "Определена рациональная последовательность сборочно-сварочных операций",
    "Разработана система контроля качества (ВИК, УЗК, РК, КК, гидроиспытание)",
    "Рассмотрены требования техники безопасности при сварке резервуаров",
    "Разработанный техпроцесс может применяться на реальном производстве",
]
for i, c in enumerate(conclusions):
    y = 6.35 - i*0.72
    ax.plot([0.85], [y-0.08], "D", color=C_ACCENT,
            markersize=6, zorder=4)
    ax.text(1.2, y-0.05, c, color=C_WHITE, fontsize=13,
            va="center", zorder=4)

ax.text(8.0, 1.05, "Благодарю за внимание!  Готов ответить на вопросы.",
        color=C_ACCENT, fontsize=16, fontweight="bold",
        ha="center", va="center", zorder=3)
ax.add_patch(mpatches.Rectangle((0,0), W, 0.38,
             fc=C_HEADER, alpha=0.9, ec="none", zorder=2))
ax.text(8.0, 0.19,
        "Дроздов Д.А.  ·  группа 19СВС  ·  2026",
        color="#A8C4E0", fontsize=9, ha="center", va="center", zorder=3)

save(fig, 9)

# ═══════════════════════════════════════════════════════════════
#  СБОРКА PPTX
# ═══════════════════════════════════════════════════════════════
prs = Presentation()
prs.slide_width  = Emu(9144000)
prs.slide_height = Emu(5143500)

for path in slide_paths:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(path, 0, 0,
                             prs.slide_width, prs.slide_height)

out = r"c:\Users\drozd\Desktop\Дроздов_резервуары_презентация.pptx"
prs.save(out)
print(f"Saved: {out}  ({len(slide_paths)} слайдов)")
