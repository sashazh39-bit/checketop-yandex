# -*- coding: utf-8 -*-
"""Озон Банк — UX-слой: preview, edit-single-field, отдельный отправитель.

Этот модуль — ЧИСТОЕ ядро (stdlib only, без aiogram, без I/O, без импорта
генераторов). Он НЕ подключён к ``mts_bot.py``: владелец бота встраивает его
одним патчем из ``_ozon_bot_patch_candidate.md``.

Что закрывает (аудит текущего Ozon-флоу в ``mts_bot.py``)
---------------------------------------------------------
1. Имя: старый ``bank_name()`` не ставил точку после инициала фамилии и не
   нормализовал регистр (``иван иванович иванов`` -> ``иван ильнурович Х``
   без точки). Здесь ``normalize_person_name`` всегда даёт
   ``Имя Отчество Ф.`` с нормальным регистром.
2. Отправитель: ``_ozon_finish`` не передавал ``sender=`` в
   ``ozon_generator.generate``, поэтому в чек попадал отправитель донора
   (чужой человек). Здесь ``sender`` — обязательное отдельное поле.
3. Preview: сейчас после суммы сразу генерация. Здесь после заполнения —
   экран «Проверь данные» с правкой ОДНОГО поля.
4. Сумма/телефон: старый ввод делал ``re.sub(r"\\D", "", text)``, поэтому
   ``14500,50`` превращался в ``1450050``, а ``1.5`` — в ``15``. Здесь
   копейки != ``,00`` — понятная ошибка, а не молчаливая порча суммы.
   Телефон дополнительно принимает 10 цифр без ``7``/``8``.
5. Сохранение при ошибке: текущий финиш при исключении генератора делает
   ``sessions.pop(uid)`` — все поля теряются. Здесь ошибка валидации и
   ошибка генерации сохраняют черновик и возвращают в preview.
6. ``ozon_edit``: сейчас достаточно раскладки 86 объектов. Здесь перед
   правкой — проверка признаков genuine Ozon (маркеры шрифта/Chromium/
   Skia, раскладка через инъекцию, извлекаемый SBP ID), отказ известным
   сгенерированным PDF и только поле ``amount``.

Честное ограничение genuine-проверки
------------------------------------
``ozon_generator`` специально делает byte-faithful реплику genuine Skia PDF,
поэтому по одним байтовым маркерам отличить «настоящий» чек от реплики
этого же проекта нельзя. Ядро требует все локальные признаки genuine и
даёт владельцу бота обязательный крючок ``known_generated`` (сверка хэша/
SBP ID с журналом собственных генераций бота). Без этого крючка обещать
«только genuine» нельзя — патч-кандидат это прямо фиксирует.

Контракт генератора (уже есть в ``ozon_generator.generate``)::

    generate(recipient: str, phone: str, bank: str, amount: int,
             dt=None, sender: str | None = None,
             meta_out: dict | None = None) -> bytes  # PDF

Контракт редактора (уже есть в ``ozon_editor``)::

    edit_amount(pdf_bytes: bytes, new_amount: int) -> bytes
    _ensure_supported_layout(pdf_bytes: bytes) -> None  # ValueError если нет
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field as _dc_field

__all__ = [
    "FEATURE_OZON_V2", "ozon_v2_enabled",
    "FIELD_ORDER", "FieldDef", "FIELDS",
    "Draft", "new_draft", "validate_field", "apply_text_input",
    "start_edit", "cancel_edit", "missing_fields", "next_input_step",
    "is_complete", "apply_generation_error", "clear_generation_error",
    "normalize_person_name", "format_phone_ru", "parse_amount_rub",
    "CB_PREFIX", "scenario_kb_spec", "preview_kb_spec", "parse_callback",
    "preview_text", "GeneratorBlocked", "build_generator_call",
    "history_entry", "edit_history_entry", "log_label", "user_history",
    "history_text", "friendly_error",
    "EDITABLE_EDIT_FIELDS", "EditCheck", "check_edit_donor",
    "EditDraft", "apply_edit_amount", "apply_edit_error",
    "apply_edit_success", "ensure_editable_field",
]

# ── Feature flag ─────────────────────────────────────────────────────────────
#: Имя per-user фичи. В патче бота: добавить в FEATURE_FLAGS (мастер, True)
#: и в PER_USER_FEATURES + FEATURE_LABELS. Выдача — существующий /grant.
FEATURE_OZON_V2 = "ozon_v2"


def ozon_v2_enabled(global_flags: dict, per_user_flags: dict,
                    is_admin: bool) -> bool:
    """Зеркало `feat(uid, name)` из mts_bot.py, но pure (без глобалов бота)."""
    if not (global_flags or {}).get(FEATURE_OZON_V2, False):
        return False
    if is_admin:
        return True
    return bool((per_user_flags or {}).get(FEATURE_OZON_V2, False))


# ── Поля ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldDef:
    code: str              # ключ в Draft.fields и короткий код callback'а
    label: str             # общая подпись
    prompt: str            # что просим ввести на шаге ввода
    example: str


# Порядок сохраняет первые шаги текущего флоу (name -> phone -> bank),
# добавляет ОТДЕЛЬНОГО отправителя и оставляет сумму последней перед preview.
FIELD_ORDER: tuple[str, ...] = ("name", "phone", "bank", "sender", "amount")

FIELDS: dict[str, FieldDef] = {
    "name": FieldDef(
        "name", "Получатель", "ФИО получателя (полностью или Имя Отчество Ф.)",
        "Иван Иванович Иванов"),
    "phone": FieldDef(
        "phone", "Телефон", "Телефон получателя",
        "+79161234567 или 9161234567"),
    "bank": FieldDef(
        "bank", "Банк", "Банк получателя", "Т-Банк"),
    "sender": FieldDef(
        "sender", "Отправитель",
        "ФИО отправителя — отдельный человек, не получатель",
        "Пётр Петрович Петров"),
    "amount": FieldDef(
        "amount", "Сумма", "Сумма перевода (рубли, целое число)", "14500"),
}


# ── Имя: «Имя Отчество Ф.» с нормальным регистром ────────────────────────────
_PATR_RE = re.compile(r"(вич|вна|ич|ична|инична|оглы|улы|кызы)$", re.IGNORECASE)
_NAME_WORD_RE = re.compile(r"^[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*$")


def _title_word(word: str) -> str:
    """``иВАН`` -> ``Иван``; ``АННА-МАРИЯ`` -> ``Анна-Мария``; ``Ёлкин`` -> ``Ёлкин``."""
    parts = []
    for part in word.split("-"):
        if not part:
            continue
        parts.append(part[:1].upper() + part[1:].lower())
    return "-".join(parts)


def normalize_person_name(raw) -> str | None:
    """Нормализация ФИО в формат чека ``Имя Отчество Ф.`` или None.

    Принимает:
      * ``Имя Отчество Фамилия``   -> ``Имя Отчество Ф.``
      * ``Фамилия Имя Отчество``   -> ``Имя Отчество Ф.`` (отчество по суффиксу)
      * ``Имя Отчество Ф.`` / ``Имя Отчество Ф`` -> ``Имя Отчество Ф.``
      * ``Имя Фамилия``            -> ``Имя Ф.`` (как старый bot-контракт:
        первое слово — имя, второе — фамилия)

    Регистр каждого слова нормализуется (``ИВАН``/``иван`` -> ``Иван``),
    двойные фамилии через дефис сохраняются в инициал по первой части.
    Одно слово, цифры, эмодзи и >3 слов не угадываются — возвращается None,
    а вызывающий показывает понятную ошибку из ``validate_field``.
    """
    text = " ".join(str(raw or "").strip().split())
    if not text:
        return None
    words = text.split(" ")
    if len(words) == 1 or len(words) > 3:
        return None
    # Инициал вида «Ф.» / «Ф» в конце — отделить точку до проверок.
    words = [w[:-1] if len(w) == 2 and w.endswith(".") else w for w in words]
    for w in words:
        if len(w) == 1:
            if not re.match(r"^[A-Za-zА-Яа-яЁё]$", w):
                return None
            continue
        if not _NAME_WORD_RE.match(w):
            return None

    def _initial(surname: str) -> str:
        return surname[:1].upper() + "."

    if len(words) == 2:
        first, second = words
        if len(second) == 1:                      # Имя Ф
            return f"{_title_word(first)} {_initial(second)}"
        if len(first) == 1:                       # Ф Имя — не угадываем, но
            return None                           # честно просим полную форму
        # Имя Фамилия
        return f"{_title_word(first)} {_initial(second)}"

    a, b, c = words
    if len(c) == 1:                               # уже Имя Отчество Ф
        return f"{_title_word(a)} {_title_word(b)} {_initial(c)}"
    if len(a) == 1 or len(b) == 1:                # инициал не в конце — не угадываем
        return None
    if _PATR_RE.search(b):                        # Имя Отчество Фамилия
        return f"{_title_word(a)} {_title_word(b)} {_initial(c)}"
    if _PATR_RE.search(c):                        # Фамилия Имя Отчество
        return f"{_title_word(b)} {_title_word(c)} {_initial(a)}"
    # Ни одно слово не похоже на отчество — не угадываем порядок.
    return None


# ── Телефон и сумма: строгий, но дружелюбный парсинг ─────────────────────────
def format_phone_ru(raw) -> str | None:
    """``+7 (XXX) XXX-XX-XX`` или None. Принимает 10 цифр, а также 11 с 7/8.

    Старый ``mts_bot.format_phone`` отклонял 10-значный ``9161234567``,
    хотя ``ozon_generator.format_phone`` его умеет. Здесь оба формата равны.
    Буквы, добавочные номера и чужие коды стран — не угадываются.
    """
    text = str(raw or "").strip()
    if not text or re.search(r"[A-Za-zА-Яа-яЁё]", text):
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return (f"+7 ({digits[1:4]}) {digits[4:7]}-"
            f"{digits[7:9]}-{digits[9:11]}")


_AMOUNT_SUFFIX_RE = re.compile(r"\s*(?:₽|руб\.?|р\.?|rub)\s*$", re.IGNORECASE)


def parse_amount_rub(raw) -> int | None:
    """Целые рубли из ввода юзера или None (ошибку формулирует validate_field).

    Принимает ``14500``, ``14 500``, ``14\\u00a0500``, ``14500 ₽``,
    ``14500,00``/``14500.00`` (ноль копеек). Отклоняет копейки != 0
    (старый баг: ``14500,50`` -> ``1450050``), знаки, буквы и пустой ввод.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw == int(raw) else None
    text = str(raw or "").strip()
    if not text:
        return None
    text = _AMOUNT_SUFFIX_RE.sub("", text).strip()
    if not text or not re.fullmatch(r"[0-9\s\u00a0\u2009.,]+", text):
        return None
    m = re.fullmatch(r"(.+?)[.,](\d{1,2})", text.replace("\u00a0", " ")
                     .replace("\u2009", " "))
    if m:  # копейки допустимы только как ,00 / .00 / ,0 / .0
        if int(m.group(2)) != 0:
            return None
        text = m.group(1)
    elif "," in text or "." in text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    return int(digits)


# ── Черновик (сессия одного пользователя) ────────────────────────────────────
STEP_PREVIEW = "preview"


@dataclass
class Draft:
    """Состояние флоу нового чека. Сериализуется в plain dict для sessions."""

    fields: dict = _dc_field(default_factory=dict)
    step: str = "name"             # <field> | edit:<field> | preview
    editing: str | None = None     # поле в режиме edit-single-field
    last_error: str | None = None  # понятная ошибка генерации для preview

    def to_session(self) -> dict:
        return {
            "fields": dict(self.fields),
            "step": self.step,
            "editing": self.editing,
            "last_error": self.last_error,
        }

    @classmethod
    def from_session(cls, raw: dict | None) -> "Draft":
        raw = raw or {}
        fields = {k: v for k, v in (raw.get("fields") or {}).items()
                  if k in FIELDS}
        step = raw.get("step") or "name"
        editing = raw.get("editing") if raw.get("editing") in FIELDS else None
        last_error = raw.get("last_error")
        return cls(fields=fields, step=step, editing=editing,
                   last_error=str(last_error) if last_error else None)

    def get(self, code: str, default=None):
        return self.fields.get(code, default)


def new_draft() -> Draft:
    d = Draft()
    d.step = next_input_step(d) or STEP_PREVIEW
    return d


def missing_fields(draft: Draft) -> list[str]:
    return [c for c in FIELD_ORDER
            if draft.fields.get(c) in (None, "", 0)]


def is_complete(draft: Draft) -> bool:
    return not missing_fields(draft)


def next_input_step(draft: Draft) -> str | None:
    miss = missing_fields(draft)
    return miss[0] if miss else None


# ── Валидаторы (pure; check_text инъектируется из генератора владельцем) ────
def _norm_spaces(raw) -> str:
    return " ".join(str(raw or "").strip().split())


def validate_field(code: str, raw, check_text=None):
    """(value, error). value — нормализованное значение для Draft.fields.

    check_text: необязательный callable(text)->list[str] недоступных символов
    (в боте — ozon_generator.check_text). Ядро само каталог не грузит.
    При ошибке возвращается (None, человеческий текст с примером) — вызывающий
    обязан сохранить старое значение поля (см. apply_text_input).
    """
    if code not in FIELDS:
        return None, f"Неизвестное поле: {code}"

    if code in ("name", "sender"):
        value = normalize_person_name(raw if isinstance(raw, str)
                                      else ("" if raw is None else str(raw)))
        if value is None:
            return None, ("Не понял ФИО. Нужно имя, отчество и фамилия "
                          "полностью или в виде «Имя Отчество Ф.». "
                          "Пример: Иван Иванович Иванов")
        who = "получателя" if code == "name" else "отправителя"
        if len(value) > 60:
            return None, f"Слишком длинное ФИО {who} (макс. 60 символов)."
    elif code == "phone":
        value = format_phone_ru(raw if isinstance(raw, str)
                                else ("" if raw is None else str(raw)))
        if value is None:
            return None, ("Не могу распознать номер. Введи 10 цифр или номер "
                          "с +7/8. Пример: +79161234567")
        return value, None
    elif code == "bank":
        value = _norm_spaces(raw)
        if not value:
            return None, "Банк не может быть пустым. Пример: Т-Банк"
        if len(value) > 40:
            return None, "Слишком длинное название банка (макс. 40 символов)."
    elif code == "amount":
        value = parse_amount_rub(raw)
        if value is None:
            return None, ("Сумма — целое число рублей без копеек. "
                          "Пример: 14500")
        if value <= 0 or value > 999_999:
            return None, "Введи сумму от 1 до 999 999."
        return value, None
    else:  # pragma: no cover — FIELD_ORDER и FIELDS синхронизированы тестами
        value = _norm_spaces(raw)

    if check_text is not None:
        bad = check_text(value)
        if bad:
            uniq = "".join(sorted(set(bad)))
            return None, (f"Символы {uniq} недоступны в шрифте Озон Банка. "
                          "Попробуй другой вариант написания.")
    return value, None


# ── FSM: ввод и edit-single-field ─────────────────────────────────────────────
def apply_text_input(draft: Draft, raw, check_text=None):
    """Принять текст на текущем шаге. Возвращает (draft, error|None).

    Обычный ввод: заполняет поле и двигается к следующему пустому / preview.
    Edit-single-field (draft.editing задан): меняет ТОЛЬКО это поле и
    возвращается в preview; остальные поля не трогаются. При ошибке
    валидации старое значение и текущий шаг сохраняются — юзер правит
    только ошибочный ввод, а не весь флоу.
    """
    code = draft.editing or (
        draft.step if draft.step in FIELDS else None)
    if code is None:
        return draft, "Сейчас ввод не ожидается — нажми кнопку на экране."
    value, err = validate_field(code, raw, check_text=check_text)
    if err:
        return draft, err
    draft.fields[code] = value
    draft.last_error = None
    if draft.editing is not None:
        draft.editing = None
        draft.step = STEP_PREVIEW
    else:
        draft.step = next_input_step(draft) or STEP_PREVIEW
    return draft, None


def start_edit(draft: Draft, code: str) -> Draft:
    if code not in FIELDS:
        raise ValueError(f"unknown ozon field: {code!r}")
    draft.editing = code
    draft.step = f"edit:{code}"
    return draft


def cancel_edit(draft: Draft) -> Draft:
    draft.editing = None
    draft.step = STEP_PREVIEW if is_complete(draft) else (
        next_input_step(draft) or STEP_PREVIEW)
    return draft


def apply_generation_error(draft: Draft, exc_or_text) -> Draft:
    """Ошибка генерации: поля НЕ теряются, юзер возвращается в preview.

    Текущий ``_ozon_finish`` при исключении удаляет сессию целиком.
    Владелец бота вместо этого вызывает эту функцию, показывает
    ``draft.last_error`` в preview и даёт исправить одно поле/повторить.
    """
    draft.last_error = friendly_error(exc_or_text)
    draft.editing = None
    draft.step = STEP_PREVIEW
    return draft


def clear_generation_error(draft: Draft) -> Draft:
    draft.last_error = None
    return draft


# ── Клавиатуры как данные (бот маппит их в свой kb()) ─────────────────────────
# Callback namespace "oz:" — не пересекается с существующими
# new_ozon / ozon_bank: / ozon_bank_custom / ozon_edit. Все <= 64 байт.
CB_PREFIX = "oz"


def _cb(*parts: str) -> str:
    data = ":".join((CB_PREFIX, *parts))
    assert len(data.encode("utf-8")) <= 64, data
    return data


def scenario_kb_spec():
    """Стартовый экран v2: новый чек и правка genuine-чека — разные сценарии."""
    return [
        [("🛒 Новый чек Озон", _cb("new")), ("✏️ Изменить сумму", _cb("edit"))],
        [("📜 История", _cb("hist"))],
        [("❌ Отмена", "cancel")],
    ]


def preview_kb_spec(draft: Draft, can_generate: bool = True):
    rows = []
    for i in range(0, len(FIELD_ORDER), 2):
        pair = FIELD_ORDER[i:i + 2]
        rows.append([(f"✏️ {FIELDS[c].label}", _cb("e", c)) for c in pair])
    if can_generate:
        rows.append([("✅ Сгенерировать", _cb("go"))])
    else:
        rows.append([("⏳ Заполни все поля", _cb("blocked"))])
    rows.append([("📜 История", _cb("hist"))])
    rows.append([("❌ Отмена", "cancel")])
    return rows


def parse_callback(data: str):
    """'oz:e:amount' -> ('edit', 'amount'); чужие callback'и -> (None, None)."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != CB_PREFIX:
        return None, None
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    mapping = {"e": "edit", "go": "generate", "hist": "history",
               "blocked": "blocked", "pv": "preview", "new": "new",
               "edit": "edit_start"}
    return mapping.get(action, action), arg


# ── Preview ───────────────────────────────────────────────────────────────────
def _fmt_amount(value) -> str:
    try:
        return f"{int(value):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def preview_text(draft: Draft) -> str:
    """HTML-текст экрана «Проверь данные» (совместим с parse_mode=HTML бота)."""
    f = draft.fields
    lines = [
        "🛒 <b>Озон Банк — проверь данные</b>",
        "",
        f"👤 <b>Получатель:</b> {html.escape(str(f.get('name') or '—'))}",
        f"📱 <b>Телефон:</b> {html.escape(str(f.get('phone') or '—'))}",
        f"🏦 <b>Банк:</b> {html.escape(str(f.get('bank') or '—'))}",
        f"📤 <b>Отправитель:</b> {html.escape(str(f.get('sender') or '—'))}",
        f"💰 <b>Сумма:</b> {_fmt_amount(f.get('amount'))}",
    ]
    miss = missing_fields(draft)
    if miss:
        labels = ", ".join(FIELDS[c].label for c in miss)
        lines += ["", f"⚠️ Не заполнено: {html.escape(labels)}"]
    else:
        lines += ["", "Нажми ✏️, чтобы исправить одно поле, "
                      "или ✅ — сгенерировать."]
    if draft.last_error:
        lines += ["", f"❌ <b>Не получилось:</b> "
                      f"{html.escape(str(draft.last_error))}",
                  "Данные сохранены — исправь поле или нажми ✅ ещё раз."]
    return "\n".join(lines)


# ── Генератор: аргументы адаптера ─────────────────────────────────────────────
class GeneratorBlocked(RuntimeError):
    """Черновик неполон — генератору нечего собирать."""


def build_generator_call(draft: Draft):
    """((recipient, phone, bank, amount), {"sender": sender}) для generate().

    Порядок позиционных аргументов и имя kwarg сверены с сигнатурой
    ``ozon_generator.generate(recipient, phone, bank, amount, dt=None,
    sender=None, ...)``. Отправитель передаётся ВСЕГДА явно: значение
    по умолчанию у генератора — отправитель донора, что для бота
    недопустимо как тихий фолбэк.
    """
    miss = missing_fields(draft)
    if miss:
        raise GeneratorBlocked(
            "Не заполнены поля: " + ", ".join(FIELDS[c].label for c in miss))
    args = (draft.fields["name"], draft.fields["phone"],
            draft.fields["bank"], int(draft.fields["amount"]))
    return args, {"sender": draft.fields["sender"]}


# ── Понятные ошибки ───────────────────────────────────────────────────────────
def friendly_error(exc_or_text) -> str:
    """Человеческий текст ошибки без жаргона, трейсов и обрывков исключений.

    Внутренние детали (MCID, glyph, xref, donor) юзеру не помогают —
    ему нужны причина простыми словами и следующий шаг. Полный текст
    исключения владелец бота пишет в лог, а не в сообщение юзеру.
    """
    text = str(getattr(exc_or_text, "args", [exc_or_text])[0]
               if isinstance(exc_or_text, BaseException) else exc_or_text or "")
    low = text.lower()
    if "неподдерживаем" in low or "unsupported" in low:
        return ("Этот вариант чека изменить нельзя (другая раскладка PDF). "
                "Пришли чек, сформированный приложением Озон Банка, "
                "или сделай новый чек.")
    if "glyph" in low or "lacks" in low or "не покрыт" in low \
            or "no ozon donor" in low or "not covered" in low:
        return ("В шрифте чека нет нужных символов для этих данных. "
                "Исправь подсвеченное поле в preview и попробуй ещё раз.")
    if "sbp" in low or "id операц" in low:
        return ("Не нашёл ID операции СБП в этом PDF. Пришли настоящий "
                "чек Озон Банка целиком, не скриншот и не обрезок.")
    if "pdf" in low and ("not" in low or "не " in low):
        return "Это не PDF чека Озон Банка. Пришли файл PDF из приложения банка."
    if "amount" in low or "сумм" in low:
        return "Не понял сумму. Введи целое число рублей, например: 14500"
    if "phone" in low or "телефон" in low:
        return ("Не могу распознать номер. Введи 10 цифр или номер с +7/8. "
                "Пример: +79161234567")
    return ("Что-то пошло не так при создании чека. Данные сохранены — "
            "проверь поля в preview и нажми ✅ ещё раз. Если повторится, "
            "пришли другой вариант данных.")


# ── ozon_edit: только genuine Ozon, только сумма ──────────────────────────────
EDITABLE_EDIT_FIELDS: tuple[str, ...] = ("amount",)

_GENUINE_MARKERS = (b"GTEestiProDisplay", b"Chromium", b"Skia/PDF m105")
_SBP_ID_RE = re.compile(r"[AB]\d{14}[\dA-Z]0[GB]\d{14}")
_EDIT_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class EditCheck:
    ok: bool
    code: str            # ok|not_pdf|too_large|not_ozon|unsupported|no_sbp_id|known_generated
    user_error: str | None = None


def ensure_editable_field(code: str) -> str:
    """Guard: редактор суммы не должен «случайно» научиться править ID/имя."""
    if code not in EDITABLE_EDIT_FIELDS:
        raise ValueError(
            f"ozon_edit правит только сумму; поле {code!r} запрещено: "
            "ID операции и остальные поля чека остаются нетронутыми")
    return code


def check_edit_donor(pdf_bytes, *, layout_ok: bool = True,
                     extracted_text: str | None = None,
                     known_generated: bool = False,
                     max_bytes: int = _EDIT_MAX_BYTES) -> EditCheck:
    """Локальная проверка, что PDF похож на genuine чек Озон Банка.

    layout_ok / extracted_text вычисляет владелец бота своими модулями
    (``ozon_editor._ensure_supported_layout`` и pypdf-extract) и передаёт
    сюда — ядро PDF не парсит и генераторы не импортирует. Порядок
    проверок важен: каждая даёт СВОЮ понятную ошибку, а не общий «ошибка».

    known_generated=True (хэш/SBP ID найден в журнале генераций самого
    бота) — обязательный отказ: реплику этого же проекта по байтам
    от genuine не отличить (см. docstring модуля).
    """
    raw = bytes(pdf_bytes or b"")
    if not raw.startswith(b"%PDF"):
        return EditCheck(False, "not_pdf",
                         "Это не PDF. Пришли файл чека Озон Банка в формате PDF.")
    if len(raw) > max_bytes:
        return EditCheck(False, "too_large",
                         f"Файл больше {max_bytes // (1024 * 1024)} МБ. "
                         "Пришли PDF чека как есть из приложения банка.")
    if known_generated:
        return EditCheck(False, "known_generated",
                         "Этот PDF создан ботом, а не приложением Озон Банка. "
                         "Изменяю только настоящие чеки банка.")
    missing = [m.decode("latin-1") for m in _GENUINE_MARKERS if m not in raw]
    if missing:
        return EditCheck(False, "not_ozon",
                         "Не вижу признаков настоящего чека Озон Банка. "
                         "Пришли PDF, сформированный приложением Озон Банка.")
    if not layout_ok:
        return EditCheck(False, "unsupported",
                         "Этот вариант чека изменить нельзя (другая раскладка PDF, "
                         "например с полем «Сообщение»). ID и шрифты в нём "
                         "трогать не буду — пришли чек без доп. полей "
                         "или сделай новый.")
    if extracted_text is not None and not _SBP_ID_RE.search(extracted_text):
        return EditCheck(False, "no_sbp_id",
                         "Не нашёл ID операции СБП в этом PDF. Пришли настоящий "
                         "чек Озон Банка целиком, не скриншот и не обрезок.")
    return EditCheck(True, "ok", None)


@dataclass
class EditDraft:
    """Состояние правки суммы genuine-чека. Байты PDF в dict НЕ кладём —
    их владелец бота хранит отдельным ключом сессии (см. патч-кандидат);
    здесь только флаг наличия, чтобы ядро могло проверять переходы."""

    amount: int | None = None
    step: str = "wait_pdf"         # wait_pdf | amount | preview_edit | done
    pdf_present: bool = False
    last_error: str | None = None

    def to_session(self) -> dict:
        return {"amount": self.amount, "step": self.step,
                "pdf_present": self.pdf_present,
                "last_error": self.last_error}

    @classmethod
    def from_session(cls, raw: dict | None) -> "EditDraft":
        raw = raw or {}
        step = raw.get("step") or "wait_pdf"
        if step not in ("wait_pdf", "amount", "preview_edit", "done"):
            step = "wait_pdf"
        amt = raw.get("amount")
        return cls(amount=int(amt) if isinstance(amt, int) else None,
                   step=step, pdf_present=bool(raw.get("pdf_present")),
                   last_error=str(raw["last_error"]) if raw.get("last_error")
                   else None)


def apply_edit_amount(draft: EditDraft, raw):
    """Новая сумма для ozon_edit. При ошибке старая сумма и PDF сохраняются."""
    if not draft.pdf_present:
        return draft, ("PDF чека не загружен. Начни заново кнопкой "
                       "«✏️ Изменить сумму» и пришли настоящий чек Озон Банка.")
    value, err = validate_field("amount", raw)
    if err:
        return draft, err
    draft.amount = value
    draft.last_error = None
    draft.step = "preview_edit"
    return draft, None


def apply_edit_error(draft: EditDraft, exc_or_text) -> EditDraft:
    """Ошибка редактора: PDF и сумма сохраняются, можно ввести другую сумму."""
    draft.last_error = friendly_error(exc_or_text)
    draft.step = "amount"
    return draft


def apply_edit_success(draft: EditDraft) -> EditDraft:
    """Успех: PDF больше не нужен — владелец бота удаляет байты из сессии."""
    draft.step = "done"
    draft.pdf_present = False
    draft.amount = None
    draft.last_error = None
    return draft


# ── История (совместима с data["logs"] бота) ──────────────────────────────────
def history_entry(draft: Draft, meta: dict | None, time_str: str,
                  uid: int) -> dict:
    """Запись лога нового чека. type остаётся ``ozon`` — совместимо с
    текущими ``_log_label``/статистикой бота; sender пишется отдельным
    ключом и старые читатели его просто игнорируют."""
    meta = meta or {}
    return {
        "uid": uid,
        "type": "ozon",
        "name": draft.fields.get("name", ""),
        "sender": draft.fields.get("sender", ""),
        "phone": draft.fields.get("phone", ""),
        "bank": draft.fields.get("bank", ""),
        "amount": draft.fields.get("amount", 0),
        "op_id": meta.get("op_id") or "",
        "time": time_str,
    }


def edit_history_entry(amount: int, time_str: str, uid: int) -> dict:
    """Запись лога правки суммы genuine-чека (текущий type ``ozon_edit``)."""
    return {"uid": uid, "type": "ozon_edit", "amount": int(amount),
            "time": time_str}


def log_label(entry: dict) -> str:
    t = entry.get("type")
    if t == "ozon_edit":
        return f"✏️ Озон правка • {entry.get('amount', '?')}₽"
    if t == "ozon":
        return (f"🧾 Озон • {entry.get('name', '?')} • "
                f"{entry.get('amount', '?')}₽ • {entry.get('bank', '?')}")
    return f"🧾 {entry.get('name', '?')} • {entry.get('amount', '?')}₽"


def _entry_uid(entry: dict):
    return entry.get("uid", entry.get("user_id", 0))


def user_history(logs, uid: int, limit: int = 10) -> list:
    out = []
    for e in reversed(list(logs or [])):
        if _entry_uid(e) != uid:
            continue
        if str(e.get("type", "")) not in ("ozon", "ozon_edit"):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def history_text(logs, uid: int, limit: int = 10) -> str:
    items = user_history(logs, uid, limit=limit)
    title = "📜 <b>История — Озон Банк</b>"
    if not items:
        return title + "\n\nПока пусто."
    lines = [title, ""]
    for i, e in enumerate(items, 1):
        lines.append(f"<b>{i}.</b> {html.escape(log_label(e))}\n"
                     f"   🕐 {html.escape(str(e.get('time', '?')))}")
    return "\n\n".join(lines)
