# -*- coding: utf-8 -*-
"""
Презентация — только фотографии (без текста).
9 слайдов по теме аннотации. Каждый слайд = одна картинка на весь экран.
"""
from pptx import Presentation
from pptx.util import Emu
import os

IMG = r"c:\Users\drozd\Downloads\чекетоп_backup_21_05_2026\_pptx_preview"

# Привязка фото к слайдам аннотации:
#   1. Титул / приветствие          → готовые резервуары зимой
#   2. Резервуары и их назначение   → чертёж, проектирование
#   3. Выбор способа сварки         → сварщик на резервуаре
#   4. Подготовка металла           → зачистка / подготовка кромок
#   5. Сборка и сварочные работы    → монтаж корпуса резервуара
#   6. Контроль качества            → работники проверяют резервуар
#   7. Антикоррозионная защита      → покраска резервуара
#   8. Техника безопасности         → сварщик (вид сбоку, крупно)
#   9. Заключение                   → готовые резервуары (красивый вид)
slides_photos = [
    "image1.jpeg",   # 1 — титул
    "image2.jpeg",   # 2 — проектирование / назначение
    "image7.jpeg",   # 3 — выбор сварки
    "image3.jpeg",   # 4 — подготовка металла
    "image4.jpeg",   # 5 — сборка и сварка
    "image6.jpeg",   # 6 — контроль качества
    "image5.jpeg",   # 7 — антикоррозионная защита
    "image7.jpeg",   # 8 — техника безопасности (сварщик)
    "image1.jpeg",   # 9 — заключение (красивый снимок резервуаров)
]

prs = Presentation()
prs.slide_width  = Emu(9144000)   # 16:9  (33.87 см)
prs.slide_height = Emu(5143500)   # 16:9  (19.05 см)

blank_layout = prs.slide_layouts[6]  # без заполнителей

for photo in slides_photos:
    path = os.path.join(IMG, photo)
    slide = prs.slides.add_slide(blank_layout)
    slide.shapes.add_picture(path, 0, 0,
                             prs.slide_width, prs.slide_height)

out = r"c:\Users\drozd\Desktop\все\презентация.pptx"
prs.save(out)
print(f"Готово: {out}  ({len(slides_photos)} слайдов)")
