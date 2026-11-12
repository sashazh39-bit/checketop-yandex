#!/usr/bin/env python3
"""Generic step-by-step wizard engine for statement editing.

Drives a field-by-field flow for both Alfa and VTB bank statements.
Each field is shown one at a time with an 'Оставить' (keep) button.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any

SURNAMES_BY_LETTER: dict[str, list[str]] = {
    "А": ["Александров", "Андреев", "Алексеев", "Антонов", "Артемов", "Афанасьев",
          "Абрамов", "Аксенов", "Астахов", "Авдеев", "Аникин", "Архипов"],
    "Б": ["Борисов", "Белов", "Богданов", "Баранов", "Беляков", "Бирюков",
          "Быков", "Бобров", "Буров", "Блинов", "Бурцев", "Батуров"],
    "В": ["Васильев", "Волков", "Виноградов", "Власов", "Воробьев", "Воронов",
          "Вишняков", "Воронцов", "Веселов", "Ветров", "Вавилов", "Вершинин"],
    "Г": ["Григорьев", "Горбунов", "Голубев", "Громов", "Герасимов", "Гаврилов",
          "Гончаров", "Горшков", "Гуляев", "Гордеев", "Гребнев", "Гуров"],
    "Д": ["Дмитриев", "Данилов", "Давыдов", "Дорофеев", "Дьяконов", "Демидов",
          "Денисов", "Дроздов", "Дубов", "Дегтярев", "Долгов", "Дудин"],
    "Е": ["Егоров", "Ершов", "Елисеев", "Ефремов", "Евдокимов", "Емельянов",
          "Еремин", "Есин", "Евсеев", "Еланцев", "Евтушенко", "Есипов"],
    "Ж": ["Жуков", "Журавлев", "Жданов", "Жаров", "Житков", "Жилин",
          "Жигалов", "Жириков", "Женихов", "Жестков"],
    "З": ["Захаров", "Зайцев", "Зимин", "Зотов", "Зуев", "Зиновьев",
          "Зеленов", "Зубов", "Зыков", "Золотарев", "Зыков", "Землянов"],
    "И": ["Иванов", "Ильин", "Исаев", "Игнатов", "Исаков", "Ильюшин",
          "Ильченко", "Инюшин", "Исупов", "Иноземцев", "Ипатов", "Ильницкий"],
    "К": ["Козлов", "Кузнецов", "Киселев", "Комаров", "Королев", "Калашников",
          "Карпов", "Кириллов", "Климов", "Куликов", "Кондратьев", "Крылов"],
    "Л": ["Лебедев", "Лазарев", "Логинов", "Лукин", "Литвинов", "Лаврентьев",
          "Лавров", "Леонов", "Лосев", "Лыков", "Лопатин", "Луговой"],
    "М": ["Михайлов", "Морозов", "Макаров", "Максимов", "Мельников", "Марков",
          "Медведев", "Миронов", "Матвеев", "Малинин", "Мухин", "Мишин"],
    "Н": ["Николаев", "Никитин", "Новиков", "Наумов", "Назаров", "Некрасов",
          "Носов", "Нечаев", "Нагорный", "Никифоров", "Нестеров", "Нарышкин"],
    "О": ["Орлов", "Осипов", "Овчинников", "Олейников", "Островский", "Одинцов",
          "Овсянников", "Орехов", "Орлов", "Огурцов", "Онищенко", "Охримович"],
    "П": ["Петров", "Павлов", "Попов", "Поляков", "Пономарев", "Панов",
          "Прокофьев", "Путилов", "Плотников", "Просвиров", "Першин", "Пивоваров"],
    "Р": ["Романов", "Рыбаков", "Родионов", "Рогов", "Русаков", "Рябов",
          "Рожков", "Рубцов", "Резников", "Рыжов", "Ростовцев", "Рахманов"],
    "С": ["Сергеев", "Смирнов", "Соколов", "Степанов", "Суворов", "Соболев",
          "Сидоров", "Савельев", "Семенов", "Самойлов", "Строев", "Сафонов"],
    "Т": ["Тихонов", "Тарасов", "Титов", "Трофимов", "Туманов", "Третьяков",
          "Тимофеев", "Токарев", "Тюрин", "Тихомиров", "Тарханов", "Толстых"],
    "У": ["Устинов", "Уваров", "Ушаков", "Ульянов", "Удальцов", "Утюжников",
          "Угаров", "Усов", "Умников", "Усманов"],
    "Ф": ["Федоров", "Филиппов", "Фролов", "Фомин", "Фадеев", "Фокин",
          "Фурсов", "Фетисов", "Фомичев", "Фурманов", "Филатов", "Федченко"],
    "Х": ["Харитонов", "Хохлов", "Хорошев", "Хомяков", "Худяков", "Храмов",
          "Хачатурян", "Хомутов", "Ходаков", "Хлебников"],
    "Ц": ["Цветков", "Царев", "Цыганов", "Целиков", "Цапков", "Цыплаков",
          "Цирков", "Цыбулин"],
    "Ч": ["Чернов", "Чистяков", "Чуйков", "Чехов", "Чуприн", "Черкасов",
          "Черников", "Чесноков", "Чирков", "Чубаров", "Черепанов", "Чухнов"],
    "Ш": ["Шаров", "Шилов", "Шестаков", "Широков", "Шмелев", "Шведов",
          "Шувалов", "Шишкин", "Шаталов", "Шорохов", "Шубин", "Шестопалов"],
    "Щ": ["Щербаков", "Щукин", "Щеглов", "Щепкин", "Щетинин",
          "Щелоков", "Щербинин", "Щипцов"],
    "Э": ["Эльдаров", "Эдуардов", "Элькин", "Эрастов", "Эгоров", "Эйхман"],
    "Ю": ["Юдин", "Юрьев", "Юсупов", "Южаков", "Юхнов",
          "Юматов", "Юнаков", "Юхимчук", "Юров"],
    "Я": ["Яковлев", "Яшин", "Ярцев", "Яблоков", "Ященко",
          "Якушев", "Янковский", "Яровой", "Ярославцев", "Яхнов", "Яценко"],
}

NAMES_BY_LETTER: dict[str, list[str]] = {
    "А": ["Алексей", "Андрей", "Артем", "Антон", "Арсений", "Александр"],
    "Б": ["Борис", "Богдан", "Булат", "Бронислав", "Борислав"],
    "В": ["Виктор", "Владимир", "Валерий", "Вадим", "Василий", "Вячеслав"],
    "Г": ["Григорий", "Георгий", "Глеб", "Геннадий", "Герман"],
    "Д": ["Дмитрий", "Данил", "Денис", "Давид", "Добрыня"],
    "Е": ["Евгений", "Егор", "Елисей", "Ефим", "Емельян"],
    "Ж": ["Ждан", "Жаргал", "Жером"],
    "З": ["Захар", "Зиновий", "Зураб"],
    "И": ["Иван", "Игорь", "Илья", "Ильяс", "Искандер"],
    "К": ["Кирилл", "Константин", "Клим", "Кузьма", "Карим"],
    "Л": ["Леонид", "Лев", "Лука", "Лаврентий", "Леонард"],
    "М": ["Михаил", "Максим", "Марк", "Матвей", "Мирослав"],
    "Н": ["Николай", "Никита", "Назар", "Нестор", "Наум"],
    "О": ["Олег", "Остап", "Оскар", "Орест", "Онуфрий"],
    "П": ["Павел", "Петр", "Платон", "Прохор", "Потап"],
    "Р": ["Роман", "Руслан", "Ростислав", "Родион", "Рустам"],
    "С": ["Сергей", "Степан", "Семен", "Святослав", "Савелий"],
    "Т": ["Тимур", "Тимофей", "Тихон", "Тарас", "Тобиас"],
    "У": ["Устин", "Ульян", "Умар"],
    "Ф": ["Федор", "Филипп", "Фарид", "Фома", "Фарух"],
    "Х": ["Харитон", "Хасан", "Хамид"],
    "Ц": ["Цезарь", "Цыган"],
    "Ч": ["Чеслав", "Чингиз"],
    "Ш": ["Шамиль", "Шахрияр"],
    "Щ": ["Щукин"],
    "Э": ["Эдуард", "Эмиль", "Эльдар", "Эрнест", "Элькин"],
    "Ю": ["Юрий", "Юсуф", "Юлиан", "Юстин"],
    "Я": ["Яков", "Ярослав", "Ян", "Якуб"],
}

PATRONYMICS_BY_LETTER: dict[str, list[str]] = {
    "А": ["Алексеевич", "Андреевич", "Артемович", "Антонович", "Александрович"],
    "Б": ["Борисович", "Богданович", "Булатович", "Брониславович"],
    "В": ["Викторович", "Владимирович", "Валерьевич", "Вадимович", "Васильевич"],
    "Г": ["Григорьевич", "Георгиевич", "Глебович", "Геннадьевич", "Германович"],
    "Д": ["Дмитриевич", "Данилович", "Денисович", "Давидович"],
    "Е": ["Евгеньевич", "Егорович", "Елисеевич", "Ефимович"],
    "Ж": ["Жоресович", "Жданович"],
    "З": ["Захарович", "Зиновьевич", "Зурабович"],
    "И": ["Иванович", "Игоревич", "Ильич", "Ильясович"],
    "К": ["Кириллович", "Константинович", "Климович", "Кузьмич"],
    "Л": ["Леонидович", "Львович", "Лукич", "Лаврентьевич"],
    "М": ["Михайлович", "Максимович", "Маркович", "Матвеевич"],
    "Н": ["Николаевич", "Никитич", "Назарович", "Несторович"],
    "О": ["Олегович", "Остапович", "Оскарович"],
    "П": ["Павлович", "Петрович", "Платонович", "Прохорович"],
    "Р": ["Романович", "Русланович", "Ростиславович", "Родионович"],
    "С": ["Сергеевич", "Степанович", "Семенович", "Святославович"],
    "Т": ["Тимурович", "Тимофеевич", "Тихонович", "Тарасович"],
    "У": ["Устинович", "Ульянович", "Умарович"],
    "Ф": ["Федорович", "Филиппович", "Фаридович", "Фомич"],
    "Х": ["Харитонович", "Хасанович", "Хамидович"],
    "Ц": ["Цезаревич", "Царевич"],
    "Ч": ["Чеславович", "Чингизович"],
    "Ш": ["Шамильевич", "Шахриярович"],
    "Щ": ["Щукинович", "Щербакович"],
    "Э": ["Эдуардович", "Эмильевич", "Эльдарович", "Эрнестович"],
    "Ю": ["Юрьевич", "Юсуфович", "Юлианович"],
    "Я": ["Яковлевич", "Ярославович", "Янович"],
}

ADDRESS_DATABASE: list[dict[str, str]] = [
    {"индекс": "101000", "регион": "Московская", "город": "Москва", "улица": "Мясницкая", "дом": "15", "кв": "42"},
    {"индекс": "190000", "регион": "Ленинградская", "город": "Санкт-Петербург", "улица": "Невский проспект", "дом": "28", "кв": "15"},
    {"индекс": "620000", "регион": "Свердловская", "город": "Екатеринбург", "улица": "Ленина", "дом": "24А", "кв": "89"},
    {"индекс": "630099", "регион": "Новосибирская", "город": "Новосибирск", "улица": "Красный проспект", "дом": "65", "кв": "33"},
    {"индекс": "420015", "регион": "Татарстан", "город": "Казань", "улица": "Баумана", "дом": "36", "кв": "7"},
    {"индекс": "603005", "регион": "Нижегородская", "город": "Нижний Новгород", "улица": "Большая Покровская", "дом": "18", "кв": "56"},
    {"индекс": "443010", "регион": "Самарская", "город": "Самара", "улица": "Куйбышева", "дом": "95", "кв": "21"},
    {"индекс": "644099", "регион": "Омская", "город": "Омск", "улица": "Ленина", "дом": "12", "кв": "88"},
    {"индекс": "454091", "регион": "Челябинская", "город": "Челябинск", "улица": "Кирова", "дом": "167", "кв": "14"},
    {"индекс": "344006", "регион": "Ростовская", "город": "Ростов-на-Дону", "улица": "Большая Садовая", "дом": "47", "кв": "31"},
    {"индекс": "450076", "регион": "Башкортостан", "город": "Уфа", "улица": "Ленина", "дом": "14", "кв": "62"},
    {"индекс": "400005", "регион": "Волгоградская", "город": "Волгоград", "улица": "Мира", "дом": "22", "кв": "9"},
    {"индекс": "614000", "регион": "Пермская", "город": "Пермь", "улица": "Ленина", "дом": "51", "кв": "73"},
    {"индекс": "350000", "регион": "Краснодарская", "город": "Краснодар", "улица": "Красная", "дом": "109", "кв": "45"},
    {"индекс": "410000", "регион": "Саратовская", "город": "Саратов", "улица": "Московская", "дом": "34", "кв": "18"},
    {"индекс": "394018", "регион": "Воронежская", "город": "Воронеж", "улица": "Плехановская", "дом": "53", "кв": "27"},
    {"индекс": "300041", "регион": "Тульская", "город": "Тула", "улица": "Советская", "дом": "17", "кв": "5"},
    {"индекс": "236000", "регион": "Калининградская", "город": "Калининград", "улица": "Ленинский проспект", "дом": "83", "кв": "11"},
    {"индекс": "238750", "регион": "Калининградская", "город": "Советск", "улица": "Каштановая", "дом": "8В", "кв": "78"},
    {"индекс": "238340", "регион": "Калининградская", "город": "Светлый", "улица": "Калининградская", "дом": "2А", "кв": "34"},
    {"индекс": "238401", "регион": "Калининградская", "город": "Славск", "улица": "Советская", "дом": "12В", "кв": "56"},
    {"индекс": "660049", "регион": "Красноярская", "город": "Красноярск", "улица": "Мира", "дом": "94", "кв": "37"},
    {"индекс": "680000", "регион": "Хабаровская", "город": "Хабаровск", "улица": "Муравьева-Амурского", "дом": "15", "кв": "8"},
    {"индекс": "690091", "регион": "Приморская", "город": "Владивосток", "улица": "Светланская", "дом": "29", "кв": "65"},
    {"индекс": "664003", "регион": "Иркутская", "город": "Иркутск", "улица": "Карла Маркса", "дом": "13", "кв": "19"},
    {"индекс": "170100", "регион": "Тверская", "город": "Тверь", "улица": "Советская", "дом": "23", "кв": "41"},
    {"индекс": "150000", "регион": "Ярославская", "город": "Ярославль", "улица": "Свободы", "дом": "46", "кв": "12"},
    {"индекс": "160000", "регион": "Вологодская", "город": "Вологда", "улица": "Мира", "дом": "6", "кв": "3"},
    {"индекс": "214000", "регион": "Смоленская", "город": "Смоленск", "улица": "Ленина", "дом": "10", "кв": "22"},
    {"индекс": "460000", "регион": "Оренбургская", "город": "Оренбург", "улица": "Советская", "дом": "32", "кв": "57"},
    {"индекс": "440000", "регион": "Пензенская", "город": "Пенза", "улица": "Московская", "дом": "78", "кв": "4"},
    {"индекс": "432017", "регион": "Ульяновская", "город": "Ульяновск", "улица": "Гончарова", "дом": "40", "кв": "16"},
    {"индекс": "410031", "регион": "Саратовская", "город": "Балаково", "улица": "Набережная", "дом": "21", "кв": "93"},
    {"индекс": "305000", "регион": "Курская", "город": "Курск", "улица": "Ленина", "дом": "11", "кв": "44"},
    {"индекс": "360000", "регион": "Кабардино-Балкарская", "город": "Нальчик", "улица": "Ленина", "дом": "27", "кв": "8"},
    {"индекс": "185035", "регион": "Карельская", "город": "Петрозаводск", "улица": "Ленина", "дом": "33", "кв": "71"},
    {"индекс": "248000", "регион": "Калужская", "город": "Калуга", "улица": "Кирова", "дом": "55", "кв": "29"},
    {"индекс": "241050", "регион": "Брянская", "город": "Брянск", "улица": "Ленина", "дом": "19", "кв": "63"},
    {"индекс": "153000", "регион": "Ивановская", "город": "Иваново", "улица": "Ленина", "дом": "47", "кв": "10"},
    {"индекс": "600000", "регион": "Владимирская", "город": "Владимир", "улица": "Большая Московская", "дом": "56", "кв": "38"},
    {"индекс": "390000", "регион": "Рязанская", "город": "Рязань", "улица": "Почтовая", "дом": "36", "кв": "51"},
    {"индекс": "428000", "регион": "Чувашская", "город": "Чебоксары", "улица": "Ленинградская", "дом": "17", "кв": "24"},
    {"индекс": "362040", "регион": "Северо-Осетинская", "город": "Владикавказ", "улица": "Мира", "дом": "8", "кв": "35"},
]

# ── Field definitions ────────────────────────────────────────────────────

ALFA_BLOCK1_FIELDS = [
    {"key": "номер_счета", "label": "📋 Номер счёта", "block": 1, "validate": "account"},
    {"key": "дата_открытия", "label": "📅 Дата открытия счёта", "block": 1},
    {"key": "валюта", "label": "💱 Валюта счёта", "block": 1},
    {"key": "тип_счета", "label": "📝 Тип счёта", "block": 1},
    {"key": "дата_формирования", "label": "📅 Дата формирования выписки", "block": 1, "unique_replace": True},
    {"key": "клиент", "label": "👤 ФИО клиента (Фамилия Имя Отчество)", "block": 1, "suggest_fio": True},
    {"key": "адрес", "label": "🏠 Адрес регистрации", "block": 1, "suggest_address": True},
    {"key": "индекс", "label": "📮 Индекс", "block": 1},
    {"key": "город", "label": "🏙️ Город", "block": 1},
    {"key": "улица", "label": "🛤️ Улица", "block": 1},
    {"key": "дом_кв", "label": "🏠 Дом, квартира", "block": 1},
]

ALFA_OP_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции", "block": 2},
    {"key": "номер_операции", "label": "🔢 Номер операции", "block": 2},
    {"key": "телефон", "label": "📱 Телефон/карта", "block": 2},
    {"key": "сумма", "label": "💰 Сумма операции", "block": 2},
    {"key": "описание", "label": "📝 Описание операции", "block": 2},
]

ALFA_BLOCK3_FIELDS = [
    {"key": "период_с", "label": "📅 Период с", "block": 3},
    {"key": "период_по", "label": "📅 Период по", "block": 3},
    {"key": "текущий_баланс", "label": "💰 Текущий баланс (исходящий остаток = платежный лимит = баланс)", "block": 3},
    {"key": "расходы", "label": "💸 Расходы (авто из операций, можно изменить)", "block": 3},
    {"key": "поступления", "label": "💰 Поступления (авто из операций, можно изменить)", "block": 3},
]

VTB_BLOCK1_FIELDS = [
    {"key": "фио", "label": "👤 ФИО клиента", "block": 1, "suggest_fio": True},
    {"key": "номер_счета", "label": "📋 Номер счёта", "block": 1, "validate": "account"},
    {"key": "период_start", "label": "📅 Начало периода", "block": 1},
    {"key": "период_end", "label": "📅 Конец периода", "block": 1},
]

VTB_OP_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции", "block": 2},
    {"key": "время", "label": "🕐 Время операции", "block": 2},
    {"key": "сумма", "label": "💰 Сумма (списание)", "block": 2},
    {"key": "сумма_зачисление", "label": "💰 Сумма (зачисление)", "block": 2},
    {"key": "комиссия", "label": "💰 Комиссия", "block": 2},
    {"key": "описание", "label": "📝 Описание", "block": 2},
]

VTB_BLOCK3_FIELDS = [
    {"key": "баланс_начало", "label": "💰 Баланс на начало периода", "block": 3},
]

# ── TBank field definitions ──────────────────────────────────────────────

TBANK_BLOCK1_FIELDS = [
    {"key": "фио", "label": "👤 ФИО клиента", "block": 1, "suggest_fio": True},
    {"key": "дата_формирования", "label": "📅 Дата формирования", "block": 1},
    {"key": "исх_номер", "label": "🔢 Исх. №", "block": 1},
]

TBANK_BLOCK2_FIELDS = [
    {"key": "дата_договора", "label": "📅 Дата заключения договора", "block": 2},
    {"key": "номер_договора", "label": "🔢 Номер договора", "block": 2},
    {"key": "номер_счета", "label": "📋 Номер лицевого счёта", "block": 2, "validate": "account"},
]

TBANK_OP_CARD_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции (ЛИНИЯ ПЕРЕВОД ПО КАРТЕ)", "block": 3},
    {"key": "время", "label": "🕐 Время операции", "block": 3},
    {"key": "время_списания", "label": "🕐 Время списания", "block": 3},
    {"key": "сумма", "label": "💰 Сумма (напр. -10.00 )", "block": 3},
    {"key": "номер", "label": "🔢 Номер", "block": 3},
]

TBANK_OP_SBP_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции (ЛИНИЯ ПЕРЕВОД СБП)", "block": 3},
    {"key": "время", "label": "🕐 Время операции", "block": 3},
    {"key": "время_списания", "label": "🕐 Время списания", "block": 3},
    {"key": "сумма", "label": "💰 Сумма (напр. -10.00 )", "block": 3},
    {"key": "телефон", "label": "📱 Телефон (напр. +79118584552)", "block": 3},
    {"key": "номер", "label": "🔢 Номер", "block": 3},
]

TBANK_OP_DEPOSIT_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции (ПОПОЛНЕНИЕ)", "block": 3},
    {"key": "время", "label": "🕐 Время операции", "block": 3},
    {"key": "время_списания", "label": "🕐 Время списания", "block": 3},
    {"key": "сумма", "label": "💰 Сумма (напр. +130.00 )", "block": 3},
    {"key": "номер", "label": "🔢 Номер", "block": 3},
]

TBANK_BLOCK4_FIELDS = [
    {"key": "пополнения", "label": "💰 Пополнения (авто из операций, можно изменить)", "block": 4},
    {"key": "расходы", "label": "💸 Расходы (авто из операций, можно изменить)", "block": 4},
]


# ── Wizard state helpers ─────────────────────────────────────────────────

def build_field_sequence(bank: str, operations: list[dict] | None = None) -> list[dict]:
    """Build flat field list: Block1 + per-operation Block2 + Block3."""
    if bank == "alfa":
        fields = list(ALFA_BLOCK1_FIELDS)
        for i, op in enumerate(operations or []):
            for f in ALFA_OP_FIELDS:
                fields.append({
                    **f,
                    "key": f"op_{i}_{f['key']}",
                    "label": f"{f['label']} (оп. {i + 1})",
                    "op_index": i,
                    "op_field": f["key"],
                })
        fields.extend(ALFA_BLOCK3_FIELDS)
    elif bank == "tbank":
        fields = list(TBANK_BLOCK1_FIELDS)
        fields.extend(TBANK_BLOCK2_FIELDS)
        # Three fixed operation types
        op_field_sets = [
            ("op_card", TBANK_OP_CARD_FIELDS, "Перевод по карте"),
            ("op_sbp", TBANK_OP_SBP_FIELDS, "Перевод СБП"),
            ("op_deposit", TBANK_OP_DEPOSIT_FIELDS, "Пополнение"),
        ]
        for prefix, op_fields, op_name in op_field_sets:
            for f in op_fields:
                fields.append({
                    **f,
                    "key": f"{prefix}_{f['key']}",
                    "label": f"{f['label']}",
                    "op_name": op_name,
                })
        fields.extend(TBANK_BLOCK4_FIELDS)
    else:
        fields = list(VTB_BLOCK1_FIELDS)
        for i, op in enumerate(operations or []):
            for f in VTB_OP_FIELDS:
                fields.append({
                    **f,
                    "key": f"op_{i}_{f['key']}",
                    "label": f"{f['label']} (оп. {i + 1})",
                    "op_index": i,
                    "op_field": f["key"],
                })
        fields.extend(VTB_BLOCK3_FIELDS)
    return fields


def init_wizard_state(
    bank: str,
    mode: str,
    scanned_block1: dict,
    scanned_ops: list[dict],
    file_path: str | None = None,
    file_name: str | None = None,
) -> dict:
    """Create initial USER_STATE for a statement wizard."""
    fields = build_field_sequence(bank, scanned_ops)
    current_values = {}
    for k, v in scanned_block1.items():
        current_values[k] = v
    for i, op in enumerate(scanned_ops):
        for k, v in op.items():
            current_values[f"op_{i}_{k}"] = v
    return {
        "mode": mode,
        "bank": bank,
        "fields": fields,
        "field_idx": 0,
        "current_values": current_values,
        "changes": {},
        "operations": scanned_ops,
        "file_path": file_path,
        "file_name": file_name,
    }


def get_current_field(state: dict) -> dict | None:
    """Return the field dict for the current step, or None if done."""
    idx = state.get("field_idx", 0)
    fields = state.get("fields", [])
    if idx >= len(fields):
        return None
    return fields[idx]


def advance_field(state: dict) -> dict | None:
    """Move to next field. Return new field or None if wizard complete."""
    state["field_idx"] = state.get("field_idx", 0) + 1
    return get_current_field(state)


def format_step_message(field: dict, current_value: str | None, changes: dict | None = None) -> str:
    """Format the prompt message for a wizard step.

    For Block 2 operation fields, includes a summary of values already
    entered for the same operation so the user can cross-check.
    """
    block = field.get("block", 0)
    block_names = {1: "Информация о счёте", 2: "Операции", 3: "Баланс счёта"}
    header = f"📋 Блок {block} — {block_names.get(block, '')}\n\n"
    label = field["label"]

    context = ""
    key = field.get("key", "")
    if changes and key.startswith("op_") and block == 2:
        key_parts = key.split("_")
        if len(key_parts) >= 2:
            op_idx = key_parts[1]
            field_labels = {
                "дата": "Дата",
                "номер_операции": "Номер операции",
                "телефон": "Телефон",
                "сумма": "Сумма",
                "описание": "Описание",
            }
            entered_lines = []
            for sub_key, sub_label in field_labels.items():
                full_key = f"op_{op_idx}_{sub_key}"
                if full_key == key:
                    continue
                val = changes.get(full_key)
                if val:
                    short = val.replace("\n", " ")[:50]
                    entered_lines.append(f"  {sub_label}: {short}")
            if entered_lines:
                op_num = int(op_idx) + 1
                context = f"\n\n📌 Уже введено для Оп.{op_num}:\n" + "\n".join(entered_lines)

    if current_value:
        msg = f"{header}{label}{context}\n\nТекущее значение:\n`{current_value}`\n\nВведите новое значение или нажмите «Оставить»:"
    else:
        msg = f"{header}{label}{context}\n\nЗначение не найдено.\n\nВведите значение или нажмите «Пропустить»:"
    return msg


def get_step_keyboard(field: dict, current_value: str | None, callback_prefix: str = "sw") -> list[list[dict]]:
    """Build inline keyboard for a wizard step."""
    kb: list[list[dict]] = []
    if current_value:
        display = current_value[:40] + ("..." if len(current_value) > 40 else "")
        kb.append([{"text": f"📌 Оставить: {display}", "callback_data": f"{callback_prefix}_keep"}])
    else:
        kb.append([{"text": "⏭ Пропустить", "callback_data": f"{callback_prefix}_skip"}])

    if field.get("suggest_fio") and current_value:
        kb.append([{"text": "💡 Предложить замену ФИО", "callback_data": f"{callback_prefix}_suggest_fio"}])
    if field.get("suggest_address") and current_value:
        kb.append([{"text": "💡 Предложить замену адреса", "callback_data": f"{callback_prefix}_suggest_addr"}])
    return kb


def validate_account_number(old_val: str, new_val: str) -> tuple[bool, str]:
    """Validate account number structure. If only last 4 digits changed, check basic structure."""
    new_clean = re.sub(r"\s", "", new_val)
    if not re.match(r"^\d{20}$", new_clean):
        return False, "Номер счёта должен содержать 20 цифр."
    if old_val and new_clean[:16] == old_val[:16]:
        if not new_clean.startswith("408"):
            return False, "Номер счёта физ. лица должен начинаться с 408."
    return True, ""


def validate_input(field: dict, value: str, available_chars: set[str] | None = None) -> tuple[bool, str]:
    """Validate user input for a field."""
    if field.get("validate") == "account":
        current = field.get("_current", "")
        ok, msg = validate_account_number(current, value)
        if not ok:
            return False, msg

    if available_chars is not None:
        missing = []
        seen = set()
        for ch in value:
            if ch not in available_chars and not ch.isspace() and ch not in seen:
                missing.append(ch)
                seen.add(ch)
        if missing:
            return False, f"⚠️ Недоступные символы: {''.join(missing)}. Попробуйте другое значение или введите в формате старое=новое."
    return True, ""


def suggest_fio(current_fio: str, available_chars: set[str] | None = None, multiline: bool = False) -> list[str]:
    """Suggest replacement FIO based on first letters and available chars (up to 10).

    If multiline=True, format as "Фамилия Имя\\nОтчество" for PDF line-break matching.
    """
    flat = current_fio.replace("\n", " ")
    parts = flat.split()
    if not parts:
        return ["Иванов Иван\nИванович" if multiline else "Иванов Иван Иванович"]

    l_s = parts[0][0].upper() if parts[0] else "И"
    l_n = parts[1][0].upper() if len(parts) > 1 and parts[1] else "И"
    l_p = parts[2][0].upper() if len(parts) > 2 and parts[2] else "И"

    surnames = SURNAMES_BY_LETTER.get(l_s, ["Иванов"])
    names = NAMES_BY_LETTER.get(l_n, ["Иван"])
    patronymics = PATRONYMICS_BY_LETTER.get(l_p, ["Иванович"])

    def _fmt(s, n, p):
        return f"{s} {n}\n{p}" if multiline else f"{s} {n} {p}"

    seen: set[str] = set()
    candidates: list[str] = []
    for s in surnames:
        for n in names:
            for p in patronymics:
                c = _fmt(s, n, p)
                if c not in seen:
                    seen.add(c)
                    candidates.append(c)

    max_results = 10

    if available_chars:
        def fits(text: str) -> bool:
            return all(ch in available_chars or ch.isspace() or ch == "\n" for ch in text)
        filtered = [c for c in candidates if fits(c)]
        if len(filtered) >= max_results:
            return filtered[:max_results]
        for sl in "ИПВМКСАТНГБДЕРОЖЗЛУФЯЮ":
            if len(filtered) >= max_results:
                break
            for nl in "ИПВМКСАТНГБДЕРОЖЗЛУФЯЮ":
                if len(filtered) >= max_results:
                    break
                for s in SURNAMES_BY_LETTER.get(sl, []):
                    for n in NAMES_BY_LETTER.get(nl, []):
                        for p in PATRONYMICS_BY_LETTER.get(nl, []):
                            cand = _fmt(s, n, p)
                            if cand not in seen and fits(cand):
                                seen.add(cand)
                                filtered.append(cand)
                                if len(filtered) >= max_results:
                                    return filtered
        return filtered[:max_results]
    return candidates[:max_results]


def _format_alfa_address(entry: dict[str, str]) -> str:
    """Format an ADDRESS_DATABASE entry into Alfa statement address format.

    Trailing spaces before \\n match the actual PDF template layout.
    When the street name is longer than 8 characters, 'д.' moves to the
    house/apt line so the street line does not overflow the text box.
    """
    street = entry['улица']
    if len(street) > 8:
        return (
            f"{entry['индекс']}, РОССИЯ, \n"
            f"{entry['регион']} область, \n"
            f"ОБЛАСТЬ {entry['регион']}, \n"
            f"{entry['город']}, УЛИЦА {street}, \n"
            f"д. {entry['дом']}, кв. {entry['кв']}"
        )
    return (
        f"{entry['индекс']}, РОССИЯ, \n"
        f"{entry['регион']} область, \n"
        f"ОБЛАСТЬ {entry['регион']}, \n"
        f"{entry['город']}, УЛИЦА {street}, д. \n"
        f"{entry['дом']}, кв. {entry['кв']}"
    )


def _format_vtb_address(entry: dict[str, str]) -> str:
    """Format an ADDRESS_DATABASE entry for VTB statement style (single line)."""
    return f"{entry['индекс']}, РОССИЯ, {entry['регион']} обл., {entry['город']}, ул. {entry['улица']}, д. {entry['дом']}, кв. {entry['кв']}"


def suggest_address(current_address: str, available_chars: set[str] | None = None, bank: str = "alfa") -> list[str]:
    """Suggest replacement addresses from the real address database (up to 5)."""
    city_m = re.search(r"ОБЛАСТЬ\s+\S+,\s*\n?\s*(\S+)", current_address)
    if not city_m:
        city_m = re.search(r",\s*([А-ЯЁ][а-яё]+(?:-[а-яё]+)?(?:-[А-ЯЁа-яё]+)?)\s*,", current_address)
    old_city = city_m.group(1).rstrip(",") if city_m else ""
    first_letter = old_city[0].upper() if old_city else "М"

    fmt = _format_alfa_address if bank == "alfa" else _format_vtb_address
    max_results = 5

    by_letter = [e for e in ADDRESS_DATABASE if e["город"][0].upper() == first_letter]
    rest = [e for e in ADDRESS_DATABASE if e["город"][0].upper() != first_letter]

    candidates: list[str] = []
    for entry in by_letter + rest:
        addr = fmt(entry)
        candidates.append(addr)
        if len(candidates) >= len(ADDRESS_DATABASE):
            break

    if available_chars:
        safe = set("0123456789,.\n ")
        def fits(t: str) -> bool:
            return all(ch in available_chars or ch in safe for ch in t)
        filtered = [c for c in candidates if fits(c)]
        if len(filtered) >= max_results:
            return filtered[:max_results]
        for entry in ADDRESS_DATABASE:
            addr = fmt(entry)
            if fits(addr) and addr not in filtered:
                filtered.append(addr)
                if len(filtered) >= max_results:
                    return filtered
        return filtered[:max_results]
    return candidates[:max_results]


# ── Block 3 auto-calculations ────────────────────────────────────────────

def calc_alfa_block3(
    текущий_баланс: float,
    расходы: float,
    поступления: float,
) -> dict[str, float]:
    """Calculate Alfa Block 3 derived values.

    входящий_остаток = текущий_баланс + расходы - поступления
    исходящий_остаток = платежный_лимит = текущий_баланс
    """
    return {
        "входящий_остаток": текущий_баланс + расходы - поступления,
        "поступления": поступления,
        "расходы": расходы,
        "исходящий_остаток": текущий_баланс,
        "платежный_лимит": текущий_баланс,
        "текущий_баланс": текущий_баланс,
    }


def calc_vtb_block3(
    баланс_начало: float,
    расходы: float,
    поступления: float,
) -> dict[str, float]:
    """Calculate VTB Block 3 derived values.

    баланс_конец = баланс_начало - расходы
    """
    return {
        "баланс_начало": баланс_начало,
        "поступления": поступления,
        "расходные_операции": расходы,
        "баланс_конец": баланс_начало - расходы,
    }


def _parse_amount(val_str: str) -> float:
    s = str(val_str).replace(" ", "").replace("\xa0", "").lstrip("-")
    if "," in s and "." in s:
        if s.rindex(",") < s.rindex("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def sum_operations_expense(state: dict) -> float:
    """Sum up expense operation amounts (тип == 'расход') for Block 3."""
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    ops = state.get("operations", [])
    total = 0.0
    for i, op in enumerate(ops):
        typ_key = f"op_{i}_тип"
        typ = current.get(typ_key, op.get("тип", "расход"))
        if typ != "расход":
            continue
        key = f"op_{i}_сумма"
        val_str = changes.get(key, current.get(key, op.get("сумма", "0")))
        total += _parse_amount(val_str)
    return total


def sum_operations_income_alfa(state: dict) -> float:
    """Sum up income operation amounts (тип == 'приход') for Alfa Block 3."""
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    ops = state.get("operations", [])
    total = 0.0
    for i, op in enumerate(ops):
        typ_key = f"op_{i}_тип"
        typ = current.get(typ_key, op.get("тип", "приход"))
        if typ != "приход":
            continue
        key = f"op_{i}_сумма"
        val_str = changes.get(key, current.get(key, op.get("сумма", "0")))
        total += _parse_amount(val_str)
    return total


def sum_operations_income(state: dict) -> float:
    """Sum up income operations (VTB зачисление field)."""
    changes = state.get("changes", {})
    ops = state.get("operations", [])
    total = 0.0
    for i, op in enumerate(ops):
        key = f"op_{i}_сумма_зачисление"
        val_str = changes.get(key, op.get("сумма_зачисление", "0"))
        total += _parse_amount(val_str)
    return total


def format_amount_rur(val: float) -> str:
    """Format as Alfa-style: '19 121,65'."""
    integer = int(abs(val))
    frac = abs(val) - integer
    cents = round(frac * 100)
    s = f"{integer:,}".replace(",", " ")
    result = f"{s},{cents:02d}"
    if val < 0:
        result = "-" + result
    return result


def format_amount_rub(val: float) -> str:
    """Format as VTB-style: '39,862.00'."""
    integer = int(abs(val))
    frac = abs(val) - integer
    cents = round(frac * 100)
    s = f"{integer:,}"
    result = f"{s}.{cents:02d}"
    if val < 0:
        result = "-" + result
    return result


# ── Preview formatting ───────────────────────────────────────────────────

def format_preview(state: dict) -> str:
    """Format a preview of all 3 blocks with current/changed values."""
    bank = state.get("bank", "alfa")
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    ops = state.get("operations", [])

    def val(key: str) -> str:
        return changes.get(key, current.get(key, "—"))

    lines = ["🏦 Предпросмотр выписки\n"]

    if bank == "alfa":
        lines.append("📋 Блок 1 — Информация о счёте")
        lines.append(f"  Номер счёта: {val('номер_счета')}")
        lines.append(f"  Дата открытия: {val('дата_открытия')}")
        lines.append(f"  Валюта: {val('валюта')}")
        lines.append(f"  Тип счёта: {val('тип_счета')}")
        lines.append(f"  Дата формирования: {val('дата_формирования')}")
        lines.append(f"  Клиент: {val('клиент')}")
        lines.append(f"  Адрес: {val('адрес')}")
        lines.append("")
        lines.append("📋 Блок 2 — Операции")
        for i, op in enumerate(ops):
            d = val(f"op_{i}_дата")
            n = val(f"op_{i}_номер_операции")
            p = val(f"op_{i}_телефон")
            s = val(f"op_{i}_сумма")
            lines.append(f"  Оп.{i+1}: {d} | {n} | {p} | {s} RUR")
        lines.append("")
        expenses_str = changes.get("расходы")
        income_str_user = changes.get("поступления")
        if expenses_str:
            expenses = _parse_amount(expenses_str)
        else:
            expenses = sum_operations_expense(state)
        if income_str_user:
            income = _parse_amount(income_str_user)
        else:
            income_raw = current.get("поступления", "0")
            income = _parse_amount(income_raw)
        bal_str = changes.get("текущий_баланс", current.get("текущий_баланс", "0"))
        try:
            bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
        except ValueError:
            bal = 0.0
        calc = calc_alfa_block3(bal, expenses, income)
        lines.append("📋 Блок 3 — Баланс счёта")
        lines.append(f"  Период: {val('период_с')} — {val('период_по')}")
        lines.append(f"  Входящий остаток: {format_amount_rur(calc['входящий_остаток'])} RUR")
        lines.append(f"  Поступления: {format_amount_rur(calc['поступления'])} RUR")
        lines.append(f"  Расходы: {format_amount_rur(calc['расходы'])} RUR")
        lines.append(f"  Исходящий остаток: {format_amount_rur(calc['исходящий_остаток'])} RUR")
        lines.append(f"  Платежный лимит: {format_amount_rur(calc['платежный_лимит'])} RUR")
        lines.append(f"  Текущий баланс: {format_amount_rur(calc['текущий_баланс'])} RUR")
    else:
        lines.append("📋 Блок 1 — Информация о счёте")
        lines.append(f"  ФИО: {val('фио')}")
        lines.append(f"  Номер счёта: {val('номер_счета')}")
        lines.append(f"  Период: {val('период_start')} — {val('период_end')}")
        lines.append("")
        lines.append("📋 Блок 2 — Операции")
        for i, op in enumerate(ops):
            d = val(f"op_{i}_дата")
            t = val(f"op_{i}_время")
            s = val(f"op_{i}_сумма")
            desc = val(f"op_{i}_описание")
            lines.append(f"  Оп.{i+1}: {d} {t} | {s} RUB | {desc[:30]}")
        lines.append("")
        expenses = sum_operations_expense(state)
        income = sum_operations_income(state)
        bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
        try:
            bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
        except ValueError:
            bal = 0.0
        calc = calc_vtb_block3(bal, expenses, income)
        lines.append("📋 Блок 3 — Баланс счёта")
        lines.append(f"  Баланс на начало: {format_amount_rub(calc['баланс_начало'])} RUB")
        lines.append(f"  Поступления: {format_amount_rub(calc['поступления'])} RUB")
        lines.append(f"  Расходные операции: {format_amount_rub(calc['расходные_операции'])} RUB")
        lines.append(f"  Баланс на конец: {format_amount_rub(calc['баланс_конец'])} RUB")

    return "\n".join(lines)


def get_preview_keyboard(callback_prefix: str = "sw") -> list[list[dict]]:
    """Keyboard for the preview screen."""
    return [
        [{"text": "✅ Сформировать PDF", "callback_data": f"{callback_prefix}_generate"}],
        [{"text": "✏️ Редактировать Блок 1", "callback_data": f"{callback_prefix}_edit_b1"},
         {"text": "✏️ Блок 2", "callback_data": f"{callback_prefix}_edit_b2"},
         {"text": "✏️ Блок 3", "callback_data": f"{callback_prefix}_edit_b3"}],
        [{"text": "⬅️ Главное меню", "callback_data": "main_back"}],
    ]


def jump_to_block(state: dict, block_num: int) -> dict | None:
    """Jump wizard to the first field of a given block number."""
    fields = state.get("fields", [])
    for i, f in enumerate(fields):
        if f.get("block") == block_num:
            state["field_idx"] = i
            return f
    return None


# ── Replacement pair builder ─────────────────────────────────────────────

_DATE_CONTEXT_KEYS = {"период_с", "период_по", "дата_формирования", "дата_открытия"}
_BLOCK3_AUTO_KEYS = {"входящий_остаток", "расходы", "поступления", "исходящий_остаток", "платежный_лимит"}


def build_replacement_pairs(state: dict) -> list[tuple[str, str]]:
    """Build (old_value, new_value) pairs from wizard changes for CID patching.

    Handles dates with context-aware patterns to avoid collisions when
    multiple fields share the same original date string.
    Block 3 auto-calculated fields are skipped here (handled separately).
    """
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    bank = state.get("bank", "alfa")
    pairs: list[tuple[str, str]] = []
    seen_old: set[str] = set()

    if bank == "alfa":
        old_start = current.get("период_с", "")
        old_end = current.get("период_по", "")
        new_start = changes.get("период_с", old_start)
        new_end = changes.get("период_по", old_end)
        if old_start and old_end and (new_start != old_start or new_end != old_end):
            old_period = f"За период с {old_start} по {old_end}"
            new_period = f"За период с {new_start} по {new_end}"
            pairs.append((old_period, new_period))
            seen_old.add(old_start)
            if old_end != old_start:
                seen_old.add(old_end)

        if "дата_открытия" in changes:
            old_do = current.get("дата_открытия", "")
            new_do = changes["дата_открытия"]
            if old_do and old_do != new_do:
                old_ctx = f"счета\n{old_do}"
                new_ctx = f"счета\n{new_do}"
                pairs.append((old_ctx, new_ctx))
                seen_old.add(old_do)

        if "дата_формирования" in changes:
            old_df = current.get("дата_формирования", "")
            new_df = changes["дата_формирования"]
            if old_df and old_df != new_df:
                old_ctx = f"выписки\n{old_df}"
                new_ctx = f"выписки\n{new_df}"
                pairs.append((old_ctx, new_ctx))
                seen_old.add(old_df)

    for key, new_val in changes.items():
        if key in _DATE_CONTEXT_KEYS or key in _BLOCK3_AUTO_KEYS:
            continue
        if key == "текущий_баланс":
            continue
        old_val = current.get(key)
        if old_val and old_val != new_val:
            old_s = str(old_val)
            new_s = str(new_val)
            if old_s in seen_old:
                if not (key.startswith("op_") and key.endswith("_дата")):
                    continue
            seen_old.add(old_s)
            pairs.append((old_s, new_s))

    # Protect formation date: if user keeps it at its original value but an op date
    # shares that same original value and IS being changed, the raw op-date pair will
    # stomp on the formation date too.  Add a restore pair that runs after the raw pair.
    if bank == "alfa":
        old_df = current.get("дата_формирования", "")
        user_df = changes.get("дата_формирования", old_df)
        if old_df and old_df == user_df:
            for key, new_val in changes.items():
                if key.startswith("op_") and key.endswith("_дата"):
                    if current.get(key) == old_df and new_val != old_df:
                        pairs.append((f"выписки\n{new_val}", f"выписки\n{old_df}"))
                        break

    return pairs
