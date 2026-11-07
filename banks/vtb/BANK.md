# ВТБ — три типа документов

Доноры патчатся, PDF с чистого листа не перерисовывается. Старый
`vtb_card_receipt.py` — **другой** тип документа (свой CID/TTF-rebuild);
новые чеки ВТБ — `openhtmltopdf.com`, title `Чек-PDF`. Не смешивать.

Выход: `banks/vtb/out/`.

## Три PDF-стека

| Тип | Функция | Стек | Доноры |
|---|---|---|---|
| Чек СБП | `generate_vtb(...)` | openhtmltopdf / PDFBox 1.4, xref `n\r\n`, `stream\r\n`, zlib-6, SFProDisplay-Regular Identity-H | `donors_sbp/` (7 шт.) |
| Чек «Перевод на карту» | `generate_vtb_card(...)` | тот же openhtmltopdf, заголовок «Перевод на карту», комиссия **всегда 99 ₽** | `donors_card/` (3 шт.) |
| Выписка по счёту | `generate_vtb_statement(...)` | JasperReports 6.21.4 / OpenPDF 1.3.32, PDF-1.5, xref `n \n`, `>>stream\n`, zlib-6, ArialMT Identity-H CID=GID TTX+0 | `donors_statement/` (3 шт.) |

Контракт публичных функций — как у Рокета (`generate_rocket` /
`generate_rocket_card` / `generate_rocket_statement`).

```python
generate_vtb(
    amount, operation_dt,
    sender_name, sender_phone,      # phone принят для паритета, на чеке СБП не печатается
    recipient_name, recipient_phone, recipient_bank,
    output_path=None, *,
    donor_path=None, glyph_donor_paths=None,
    sender_account=None, message=None, sbp_id=None,
    creation_delta_minutes=None,
) -> Path

generate_vtb_card(
    amount, operation_dt, sender_name, recipient_card,
    recipient_bank=None, output_path=None, *,
    donor_path=None, glyph_donor_paths=None,
    sender_account=None, payer_card=None, fee=None,   # fee только 99 ₽
    creation_delta_minutes=None,
) -> Path

generate_vtb_statement(
    period_start, period_end, holder_name, account=None, *,
    opening_balance=None,
    operations=None,                 # [{datetime, amount, description, counterparty, direction, kind}]
    operation_count=None,
    receipt_paths=None,              # чеки СБП / карты ВТБ
    donor_path=None, glyph_donor_paths=None,
    output_path=None, generated_at=None, random_seed=None,
    fill_random=True, issue_date=None,
) -> Path
```

## Чеки СБП (`donors_sbp/`)

- Producer `openhtmltopdf.com`, title `Чек-PDF`, «Исходящий перевод СБП», «Банк ВТБ (ПАО)».
- Сумма: пробел тысяч, **точка** копеек только если ненулевые (`12 995.74 ₽` / `5 000 ₽`).
- Телефон `+7 (XXX) XXX‑XX‑XX` (U+2011). Имя `Имя Отчество Ф.`.
- ID СБП 32 символа, перенос 25+7. UTC-заголовок как у Рокета, серийник (поз. 12–32) наследуется от донора.
- Раскладки: без сообщения → 01–04; короткое сообщение → 05; длинное (глиф-пул) → 06; двухстрочный банк Wildberries → 07.

## Чеки по карте (`donors_card/`)

- Тот же стек, заголовок «Перевод на карту».
- Комиссия **всегда 99 ₽**; «Сумма с комиссией» = сумма + 99. Иной `fee=` — отказ.
- Карта получателя `2200 27** **** 4432`. Не путать со старым `vtb_card_receipt.py`.

## Выписка (`donors_statement/`)

- Title `Выписка по счёту`, счёт по умолчанию `40817810590049129426`, держатель донора «Жеребятьев Александр Евгеньевич».
- Ключ счёта **не вычисляется** — передавайте 20 цифр как есть или оставьте донорский.
- Деньги `X.00 RUB` без группировки. Правые края: шапка 290/567, операции 67.50 / 132.50 / 228 / 308 / 388 / 574.
- Операции сверху вниз — **сначала новые**. Время не печатается.
- Шаблоны: 0 операций → 1 пустая стр.; 1–5 → 1 стр. (5 строк); 6–15 или чек по карте → 2 стр. (15 строк). `fill_random=False` требует ровно 0 / 5 / 15.
- Описание не перевёрстывается: число строк слота донора сохраняется. Helvetica `()Tj` не трогаем.
- `rocket_pdf.rebuild()` не используется (`>>\nstream`); запись — copy-through / inplace / refit с `>>stream\n`.

Операции:

1. ручные `operations=[...]` (дата, сумма, описание, контрагент, приход/расход);
2. `receipt_paths` из чеков СБП (исходящий, сумма как есть) и карты (списание = сумма + 99 ₽);
3. `fill_random` + `operation_count` — даты/суммы из донорского пула, тексты слотов сохраняются.

## Глифы

- Чеки: SFPro из объединения `donors_sbp/` + `donors_card/` (донор 06 — полный кириллический пул). Символа нет — отказ, не дорисовываем.
- Выписка: ArialMT, CID=GID, пустой донор без многих букв (нет «3», заглавных Ж–Я кроме части) — недостающее **инжектится** из 02/03. Латиница вне `BNRU` / чужие символы — отказ.

## Что работает

- `python3 test_engine.py` / `test_card_engine.py` / `test_statement.py`
- Неизменённый round-trip донора = исходные байты
- Генерация СБП, карты (99 ₽), выписки 0 / 5 / 15 операций

## Ограничения

- Комиссия карты только 99 ₽ (так на всех трёх донорах).
- Выписка 1 vs 2 страницы — только готовые донорские раскладки, третья страница не рисуется.
- Сообщение СБП длиннее слота донора 05 уходит на донор 06; Wildberries только на доноре 07.
- Старый `vtb_card_receipt.py` / `donors/` / `donor_db/` не используются новыми движками.

## Бот

Подключён в `bot/yandex_bot.py`. После [ВТБ]: [Перевод по СБП]
(`generate_vtb`) / [Перевод по карте] (`generate_vtb_card`, комиссия
всегда 99 ₽ — в сводке показывается, `fee` не спрашиваем) /
[Выписка] (`generate_vtb_statement`, счёт по умолчанию донорский,
ключ не пересчитывается). Раскладка выписки 0 / 5 / 15; источник как
у Рокета плюс [✍️ Настроить].
