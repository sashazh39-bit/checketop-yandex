# -*- coding: utf-8 -*-
"""
Презентация: титульник (картинка) + 8 тематических фото без текста.
Каждое фото — точно по теме слайда аннотации.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg
from matplotlib.patches import FancyBboxPatch
from pptx import Presentation
from pptx.util import Emu

plt.rcParams["font.family"] = "DejaVu Sans"

OLD = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_pptx_preview"   # фото из примера
NEW = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_slides_photos"  # скачанные
OUT_DIR = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_slides_final"
os.makedirs(OUT_DIR, exist_ok=True)

def p(folder, name):
    return os.path.join(folder, name)

# ───────────────────────────────────────────────────────────────
#  Слайд 1 — ТИТУЛЬНИК (рисуем как картинку)
# ───────────────────────────────────────────────────────────────
W, H = 16, 9
fig = plt.figure(figsize=(W, H), facecolor="#0D1B2A")
ax  = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W); ax.set_ylim(0, H)
ax.axis("off")
ax.set_facecolor("#0D1B2A")

# Фоновое фото
img = mpimg.imread(p(OLD, "image1.jpeg"))
ax.imshow(img, extent=[0, W, 0, H], aspect="auto", zorder=1, alpha=0.35)

# Центральная плашка
ax.add_patch(FancyBboxPatch((0.7, 2.5), 14.6, 4.8,
    boxstyle="round,pad=0,rounding_size=0.25",
    fc="#0D1B2A", alpha=0.88, ec="#2E75B6", lw=2.5, zorder=2))
ax.plot([0.7, 15.3], [6.95, 6.95], color="#2E75B6", lw=2.5, zorder=3)
ax.plot([0.7, 15.3], [2.55, 2.55], color="#2E75B6", lw=2.5, zorder=3)

ax.text(8.0, 6.57,
        "ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ  "
        "КАЛИНИНГРАДСКОЙ ОБЛАСТИ",
        color="#7AAFD4", fontsize=10, ha="center", va="center",
        zorder=4)
ax.text(8.0, 6.2,
        "ПРОФЕССИОНАЛЬНАЯ ОБРАЗОВАТЕЛЬНАЯ ОРГАНИЗАЦИЯ",
        color="#7AAFD4", fontsize=10, ha="center", va="center",
        zorder=4)

# Логотип/название «Технологический колледж»
logo_path = r"C:\Users\drozd\.cursor\projects\c-Users-drozd-Downloads-backup-21-05-2026\assets\c__Users_drozd_AppData_Roaming_Cursor_User_workspaceStorage_empty-window_images_image-2712fe25-26d1-4c2c-8cce-53ee17cd8d8d.png"
logo = mpimg.imread(logo_path)
# Размещаем по центру под «организация», масштабируем по высоте ~0.5 дюйма
logo_h, logo_w = logo.shape[:2]
aspect = logo_w / logo_h
display_h = 0.52
display_w = display_h * aspect
cx = 8.0
x0 = cx - display_w / 2
ax.imshow(logo, extent=[x0, x0+display_w, 5.65, 5.65+display_h],
          aspect="auto", zorder=4)

ax.text(8.0, 5.45,
        "ДИПЛОМНАЯ РАБОТА",
        color="#2E75B6", fontsize=15, fontweight="bold",
        ha="center", va="center", zorder=4)

ax.text(8.0, 4.75,
        "Разработка технологического процесса",
        color="#FFFFFF", fontsize=30, fontweight="bold",
        ha="center", va="center", zorder=4)
ax.text(8.0, 4.1,
        "на изготовление резервуаров",
        color="#FFFFFF", fontsize=30, fontweight="bold",
        ha="center", va="center", zorder=4)

ax.plot([3.5, 12.5], [3.65, 3.65], color="#2E75B6", lw=0.8,
        alpha=0.6, zorder=3)

ax.text(8.0, 3.32,
        "Студент: Дроздов Дмитрий Александрович",
        color="#C8DCF0", fontsize=14, ha="center", va="center", zorder=4)
ax.text(8.0, 2.9,
        "Группа: 19СВС   ·   Специальность: 22.02.06 Сварочное производство   ·   2026 г.",
        color="#C8DCF0", fontsize=12, ha="center", va="center", zorder=4)

title_path = os.path.join(OUT_DIR, "slide_01_title.png")
fig.savefig(title_path, dpi=150, bbox_inches="tight",
            pad_inches=0, facecolor="#0D1B2A")
plt.close(fig)
print("OK title slide")

# ───────────────────────────────────────────────────────────────
#  Слайды 2–9 — ТОЛЬКО ФОТО, без текста
#  Каждое фото подобрано точно по теме слайда аннотации
# ───────────────────────────────────────────────────────────────
content_slides = [
    # (2) Назначение резервуаров — готовые резервуары на объекте
    p(OLD, "image1.jpeg"),
    # (3) Выбор способа сварки MAG / CO2 — дуга MAG с искрами
    p(NEW, "mag_welding.jpg"),
    # (4) Подготовка металла: резка, кромки — ЧПУ плазменная резка
    p(NEW, "plasma_cutting.jpg"),
    # (5) Сборка и сварочные работы — монтаж корпуса резервуара
    p(OLD, "image4.jpeg"),
    # (6) Контроль качества (УЗК) — специалист с ультразвуковым дефектоскопом
    p(NEW, "ultrasonic_test.jpg"),
    # (7) Антикоррозионная защита — рабочий наносит покрытие на резервуар
    p(OLD, "image5.jpeg"),
    # (8) Техника безопасности — сварщик в полной СИЗ (маска Speedglas)
    p(NEW, "tig_welding.jpg"),
    # (9) Заключение — сварщик на большом резервуаре
    p(OLD, "image7.jpeg"),
]

# ───────────────────────────────────────────────────────────────
#  Сборка PPTX
# ───────────────────────────────────────────────────────────────
prs = Presentation()
prs.slide_width  = Emu(9144000)
prs.slide_height = Emu(5143500)
blank = prs.slide_layouts[6]

# Слайд 1 — титульник
slide = prs.slides.add_slide(blank)
slide.shapes.add_picture(title_path, 0, 0,
                         prs.slide_width, prs.slide_height)

# Слайды 2–9 — фото
for photo in content_slides:
    slide = prs.slides.add_slide(blank)
    slide.shapes.add_picture(photo, 0, 0,
                             prs.slide_width, prs.slide_height)
    print(f"  added: {os.path.basename(photo)}")

out = r"c:\Users\drozd\Desktop\все\презентация.pptx"
prs.save(out)
print(f"\nГотово: {out}  ({len(prs.slides)} слайдов)")
