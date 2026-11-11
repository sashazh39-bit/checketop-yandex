#!/usr/bin/env python3
"""Тесты бота Яндекса: валидаторы, время, правка из сводки, вызов движка.

    python3 bot/test_yandex_bot.py

Telegram и сеть не поднимаются: хендлеры гоняются на фейковых
Update/Context, а движок заменён фейковым модулем на диске, чтобы
проверить, что бот вызывает ``generate_yandex(...)`` по контракту из
PROJECT.md с нормализованными аргументами.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yandex_bot as bot  # noqa: E402


# --------------------------------------------------------------------------
# Фейковый Telegram (ровно то, чем пользуются хендлеры)
# --------------------------------------------------------------------------


class FakeTelegramFile:
    def __init__(self, source):
        self.source = source

    async def download_to_drive(self, custom_path):
        shutil.copyfile(self.source, custom_path)


class FakeBot:
    def __init__(self):
        self.messages = []
        self.markups = []
        self.documents = []
        self.deleted = []
        self.edited = []
        self._next_message_id = 1000
        self.file_source = None  # что «прислал» пользователь документом

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)
        self.markups.append(reply_markup)
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        # Редактирование на месте: текст виден как последнее сообщение.
        self.messages.append(text)
        self.markups.append(reply_markup)
        self.edited.append(message_id)
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def send_document(self, chat_id, document, filename, caption, reply_markup=None):
        self.documents.append({"filename": filename, "caption": caption})

    async def get_file(self, file_id):
        return FakeTelegramFile(self.file_source)


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.document = None
        self.message_id = 777

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data = data

    async def answer(self):
        pass


def fake_update(text=None, callback=None, document_name=None):
    message = FakeMessage(text or "")
    if document_name is not None:
        message.document = SimpleNamespace(
            file_name=document_name, file_id="fake-file-id-123456"
        )
    return SimpleNamespace(
        effective_message=message,
        callback_query=FakeQuery(callback) if callback else None,
        effective_chat=SimpleNamespace(id=101),
        effective_user=SimpleNamespace(id=7),
    )


def fake_context(user_data=None):
    return SimpleNamespace(bot=FakeBot(), user_data=dict(user_data or {}))


def run(coro):
    return asyncio.run(coro)


FAKE_ENGINE = '''
from pathlib import Path

def generate_yandex(amount, operation_dt, sender_name, sender_phone,
                    recipient_name, recipient_phone, recipient_bank,
                    output_path):
    assert amount == "10 000,00 ₽", amount
    assert sender_phone == "+7 900 351-70-80", sender_phone
    assert recipient_phone == "+7 963 297-20-08", recipient_phone
    assert recipient_bank == "Сбербанк", recipient_bank
    Path(output_path).write_bytes(b"%PDF-1.5 fake-receipt")
    return output_path
'''

BROKEN_ENGINE = '''
def generate_yandex(**kwargs):
    raise RuntimeError("engine boom")
'''

TEMPLATE_FAKE_ENGINE = '''
import os
from pathlib import Path

def generate_yandex(amount, operation_dt, sender_name, sender_phone,
                    recipient_name, recipient_phone, recipient_bank,
                    output_path, donor_path=None):
    assert donor_path, "template mode must pass donor_path"
    assert Path(donor_path).is_file(), donor_path
    capture = os.environ.get("FAKE_DONOR_CAPTURE")
    if capture:
        Path(capture).write_text(str(donor_path), encoding="utf-8")
    Path(output_path).write_bytes(b"%PDF-1.5 fake-template-receipt")
    return output_path
'''

FRESH_REFUSAL_ENGINE = '''
class NoUnusedDonorTokenError(RuntimeError):
    pass

def generate_yandex(**kwargs):
    raise NoUnusedDonorTokenError(
        "Нет свободного токена: все настоящие чеки Яндекса в пуле уже использованы"
    )
'''

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DONOR = REPO_ROOT / "banks" / "yandex" / "donors" / "yandex_fresh.pdf"
REAL_DONOR_TOKEN = "1596N0B1014"  # токен (поз. 12-22) квитанции yandex_fresh.pdf


def sample_data(**overrides) -> dict:
    data = {
        "scenario": "yandex",
        "current_step": bot.CONFIRM,
        "amount": "10 000,00 ₽",
        "operation_dt": datetime(2026, 11, 3, 14, 20, 37, tzinfo=bot.MSK),
        "sender_name": "Иванов Иван Иванович",
        "sender_phone": "+7 900 351-70-80",
        "recipient_name": "Петрова Анна Ивановна",
        "recipient_phone": "+7 963 297-20-08",
        "recipient_bank": "Сбербанк",
    }
    data.update(overrides)
    return data


class AllowlistTest(unittest.TestCase):
    def test_only_two_ids_are_allowed(self):
        self.assertTrue(bot.is_allowed_user(1445265832))
        self.assertFalse(bot.is_allowed_user(7076663447))
        self.assertFalse(bot.is_allowed_user(7))
        self.assertFalse(bot.is_allowed_user(None))

    def test_stranger_is_stopped(self):
        update = fake_update(text="/start")
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.restrict_user(update, fake_context()))
        self.assertEqual(update.effective_message.replies, [])

    def test_allowed_user_passes(self):
        update = fake_update(text="/start")
        update.effective_user = SimpleNamespace(id=1445265832)
        run(bot.restrict_user(update, fake_context()))
        self.assertEqual(update.effective_message.replies, [])


class ValidatorsTest(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(bot.parse_amount("10000"), "10 000,00 ₽")
        self.assertEqual(bot.parse_amount("10 000,5"), "10 000,50 ₽")
        self.assertEqual(bot.parse_amount("2500.00 ₽"), "2 500,00 ₽")
        for bad in ("", "abc", "0", "-100", "10,000.00.1", "1 00"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_amount(bad)

    def test_datetime(self):
        moment = bot.parse_datetime("03.11.2026 14:20")
        self.assertEqual((moment.day, moment.hour, moment.minute, moment.second), (3, 14, 20, 0))
        self.assertEqual(moment.utcoffset().total_seconds(), 3 * 3600)
        fixed_now = datetime(2026, 11, 3, 9, 5, 44, tzinfo=bot.MSK)
        now = bot.parse_datetime("сейчас", now=fixed_now)
        # Текущее время идёт в движок с секундами, без округления до минут.
        self.assertEqual((now.hour, now.minute, now.second), (9, 5, 44))
        with_seconds = bot.parse_datetime("03.11.2026 14:20:37")
        self.assertEqual(with_seconds.second, 37)
        only_time = bot.parse_datetime("16:26", now=fixed_now)
        self.assertEqual((only_time.day, only_time.hour, only_time.minute, only_time.second),
                         (3, 16, 26, 0))
        for bad in ("", "03.11.2026", "32.13.2026 25:99", "завтра"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_datetime(bad, now=fixed_now)

    def test_name(self):
        self.assertEqual(
            bot.parse_name("  Иванов   Иван Иванович "),
            "Иванов Иван Иванович",
        )
        for bad in ("", "Иванов", "Иванов Иван123", "Иванов !"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_name(bad)

    def test_phone(self):
        self.assertEqual(
            bot.parse_phone("8 (900) 351-70-80"), "+7 900 351-70-80"
        )
        self.assertEqual(
            bot.parse_phone("+79632972008"), "+7 963 297-20-08"
        )
        for bad in ("", "12345", "+1 202 555 0100 1", "телефон"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_phone(bad)

    def test_bank(self):
        self.assertEqual(bot.parse_bank(" Сбербанк "), "Сбербанк")
        for bad in ("", "С", "!!!", "x" * 65):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_bank(bad)

    def test_bank_options_as_printed(self):
        labels = [label for _slug, label in bot.YANDEX_BANK_OPTIONS]
        for label in ("Сбербанк", "Т-Банк", "ВТБ", "Альфа-Банк", "ПСБ",
                      "Озон Банк (Ozon)", "Wildberries (Вайлдберриз Банк)",
                      "Рокетбанк"):
            self.assertIn(label, labels)

    def test_receipt_filename_is_uuid_pdf(self):
        name = bot.receipt_filename(sample_data())
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f-]{36}\.pdf", name), name)

    def test_keyboards_two_per_row_and_copyable_examples(self):
        context = fake_context({"scenario": "yandex"})
        _text, markup = bot._prompt(bot.RECIPIENT_BANK, context)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        text, markup = bot._prompt(bot.AMOUNT, context)
        self.assertIn("<code>17 350,00 ₽</code>", text)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        text, _markup = bot._prompt(bot.RECIPIENT_NAME, context)
        self.assertIn("<code>Иванов Иван Иванович</code>", text)
        self.assertNotIn("Подсказка", text)

    def test_summary_two_blocks_and_engine_seconds(self):
        text = bot._summary_text(sample_data())
        self.assertIn("ПОЛУЧАТЕЛЬ", text)
        self.assertIn("ОТПРАВИТЕЛЬ", text)
        self.assertIn("14:20:37", text)
        no_seconds = bot._summary_text(
            sample_data(operation_dt=datetime(2026, 11, 3, 14, 20, tzinfo=bot.MSK))
        )
        self.assertIn("секунды — движка", no_seconds)


# --------------------------------------------------------------------------
# Хендлеры на фейковом Telegram: поток, время, память, правка из сводки
# --------------------------------------------------------------------------


class YandexHandlersTest(unittest.TestCase):
    def test_mode_choice_then_normal_flow_to_summary(self):
        context = fake_context()
        state = run(bot.choose_yandex(fake_update(callback="bank:yandex"), context))
        self.assertEqual(state, bot.Y_MENU)
        state = run(bot.on_yandex_doc_choice(fake_update(callback=bot.Y_DOC_CHECK_CB), context))
        self.assertEqual(state, bot.Y_MODE)
        self.assertIn("Как создать чек?", context.bot.messages[-1])
        markup = context.bot.markups[-1]
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard for button in row
        }
        self.assertIn(bot.Y_MODE_NORMAL_CB, callbacks)
        self.assertIn(bot.Y_MODE_TEMPLATE_CB, callbacks)
        self.assertIn(bot.BACK_CB, callbacks)
        self.assertIn(bot.CANCEL_CB, callbacks)
        labels = {
            button.text
            for row in markup.inline_keyboard for button in row
        }
        self.assertIn("📝 без PDF", labels)
        self.assertIn("📄 с PDF", labels)
        state = run(bot.on_y_mode_choice(fake_update(callback=bot.Y_MODE_NORMAL_CB), context))
        self.assertEqual(state, bot.RECIPIENT_NAME)
        self.assertEqual(context.user_data["yandex_mode"], "normal")
        state = run(bot.on_recipient_name(fake_update(text="Петрова Анна Ивановна"), context))
        self.assertEqual(state, bot.RECIPIENT_PHONE)
        state = run(bot.on_recipient_phone(fake_update(text="8 963 297-20-08"), context))
        self.assertEqual(state, bot.RECIPIENT_BANK)
        self.assertEqual(context.user_data["recipient_phone"], "+7 963 297-20-08")
        state = run(bot.on_y_bank_choice(fake_update(callback="ybank:ozon"), context))
        self.assertEqual(state, bot.AMOUNT)
        self.assertEqual(context.user_data["recipient_bank"], "Озон Банк (Ozon)")
        state = run(bot.on_amount(fake_update(text="17350"), context))
        self.assertEqual(state, bot.SENDER_NAME)
        self.assertEqual(context.user_data["amount"], "17 350,00 ₽")
        state = run(bot.on_sender_name(fake_update(text="Иванов Иван Иванович"), context))
        self.assertEqual(state, bot.SENDER_PHONE)
        state = run(bot.on_sender_phone(fake_update(text="+79003517080"), context))
        self.assertEqual(state, bot.Y_TIME_CHOICE)
        state = run(bot.on_y_time_choice(fake_update(callback="ytime:change"), context))
        self.assertEqual(state, bot.DATETIME)
        state = run(bot.on_datetime_text(fake_update(text="03.11.2026 14:20"), context))
        self.assertEqual(state, bot.CONFIRM)
        summary = context.bot.messages[-1]
        self.assertIn("ПОЛУЧАТЕЛЬ", summary)
        self.assertIn("ОТПРАВИТЕЛЬ", summary)
        self.assertIn("Озон Банк (Ozon)", summary)
        # Память для следующих чеков уже сохранена показом сводки.
        self.assertEqual(context.user_data["mem_yandex_sender"]["sender_name"],
                         "Иванов Иван Иванович")
        self.assertEqual(context.user_data["mem_yandex_recipient"]["recipient_bank"],
                         "Озон Банк (Ozon)")

    def test_time_step_now_keeps_engine_seconds(self):
        context = fake_context(sample_data(current_step=bot.Y_TIME_CHOICE))
        state = run(bot.on_y_time_choice(fake_update(callback="ytime:now"), context))
        self.assertEqual(state, bot.CONFIRM)
        self.assertIsInstance(context.user_data["operation_dt"], datetime)

    def test_invalid_input_does_not_advance(self):
        context = fake_context({"scenario": "yandex", "current_step": bot.RECIPIENT_PHONE})
        update = fake_update(text="12345")
        state = run(bot.on_recipient_phone(update, context))
        self.assertEqual(state, bot.RECIPIENT_PHONE)
        self.assertTrue(any("⚠️" in m for m in context.bot.messages))
        self.assertNotIn("recipient_phone", context.user_data)

    def test_back_from_recipient_phone_returns_to_name(self):
        context = fake_context(sample_data(current_step=bot.RECIPIENT_PHONE))
        state = run(bot.on_nav_back(fake_update(callback="nav:back"), context))
        self.assertEqual(state, bot.RECIPIENT_NAME)

    def test_edit_sender_from_summary_returns_to_summary(self):
        context = fake_context(sample_data())
        state = run(bot.on_confirm_edit(fake_update(callback="confirm:edit"), context))
        self.assertEqual(state, bot.EDIT_SELECT)
        state = run(bot.on_edit_field(fake_update(callback="edit:sender_name"), context))
        self.assertEqual(state, bot.SENDER_NAME)
        self.assertTrue(context.user_data["editing"])
        state = run(bot.on_sender_name(fake_update(text="Сидоров Сидор Сидорович"), context))
        self.assertEqual(state, bot.CONFIRM)
        self.assertEqual(context.user_data["sender_name"], "Сидоров Сидор Сидорович")
        self.assertIn("Сидоров Сидор Сидорович", context.bot.messages[-1])

    def test_edit_recipient_bank_and_time_from_summary(self):
        context = fake_context(sample_data())
        run(bot.on_edit_field(fake_update(callback="edit:recipient_bank"), context))
        state = run(bot.on_y_bank_choice(fake_update(callback="ybank:wb"), context))
        self.assertEqual(state, bot.CONFIRM)
        self.assertEqual(context.user_data["recipient_bank"],
                         "Wildberries (Вайлдберриз Банк)")
        run(bot.on_edit_field(fake_update(callback="edit:datetime"), context))
        self.assertEqual(context.user_data["current_step"], bot.Y_TIME_CHOICE)
        state = run(bot.on_y_time_choice(fake_update(callback="ytime:change"), context))
        self.assertEqual(state, bot.DATETIME)
        state = run(bot.on_datetime_text(fake_update(text="16:26"), context))
        self.assertEqual(state, bot.CONFIRM)
        self.assertEqual((context.user_data["operation_dt"].hour,
                          context.user_data["operation_dt"].minute), (16, 26))

    def test_memory_buttons_apply_saved_people(self):
        context = fake_context(sample_data())
        bot._save_yandex_memory(context)
        bot._clear_draft(context)
        self.assertIn("mem_yandex_sender", context.user_data)
        context.user_data.update({"scenario": "yandex", "current_step": bot.SENDER_NAME})
        state = run(bot.on_y_mem_sender(fake_update(callback="ymem:sender"), context))
        self.assertEqual(state, bot.Y_TIME_CHOICE)
        self.assertEqual(context.user_data["sender_name"], "Иванов Иван Иванович")
        context.user_data.update({"scenario": "yandex", "current_step": bot.RECIPIENT_NAME})
        state = run(bot.on_y_mem_recipient(fake_update(callback="ymem:recipient"), context))
        self.assertEqual(state, bot.AMOUNT)
        self.assertEqual(context.user_data["recipient_bank"], "Сбербанк")

    def test_success_generation_sends_pdf_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_ENGINE, encoding="utf-8")
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            try:
                context = fake_context(sample_data())
                state = run(bot.on_confirm_yes(fake_update(callback="confirm:yes"), context))
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)
        self.assertEqual(state, -1)  # ConversationHandler.END
        self.assertEqual(len(context.bot.documents), 1)
        document = context.bot.documents[0]
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f-]{36}\.pdf", document["filename"]))
        self.assertIn("Чек готов", document["caption"])
        self.assertIn("Сбербанк", document["caption"])

    def test_engine_error_returns_to_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(BROKEN_ENGINE, encoding="utf-8")
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            try:
                context = fake_context(sample_data())
                state = run(bot.on_confirm_yes(fake_update(callback="confirm:yes"), context))
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)
        self.assertEqual(state, bot.CONFIRM)
        self.assertTrue(any("Не получилось: engine boom" in m for m in context.bot.messages))


class YandexTemplateModeTest(unittest.TestCase):
    def _context_for_upload(self, source):
        context = fake_context(
            {"scenario": "yandex", "yandex_mode": "template",
             "current_step": bot.Y_TEMPLATE_UPLOAD}
        )
        context.bot.file_source = source
        return context

    def _upload_real_donor(self, context, spent=()):
        with mock.patch.object(
            bot, "load_yandex_spent_tokens", return_value=set(spent)
        ):
            return run(
                bot.on_y_template_document(
                    fake_update(document_name="квитанция.pdf"), context
                )
            )

    def test_template_warnings_pure(self):
        sbp_now = "A" + "6" * 10 + "12345678901" + bot.YANDEX_SBP_CURRENT_TAIL
        self.assertEqual(len(sbp_now), 32)
        self.assertEqual(bot.yandex_template_warnings(sbp_now, set()), [])
        self.assertEqual(
            bot.yandex_template_warnings(sbp_now, {"12345678901"}), ["spent"]
        )
        sbp_old = sbp_now[:22] + "0011850501"
        self.assertEqual(bot.yandex_template_warnings(sbp_old, set()), ["era"])
        text = bot.yandex_template_warning_text(["spent", "era"])
        self.assertIn("уже использовался", text)
        self.assertIn("может не пройти проверку", text)

    def test_template_mode_entry(self):
        context = fake_context({"scenario": "yandex", "current_step": bot.Y_MODE})
        state = run(
            bot.on_y_mode_choice(fake_update(callback=bot.Y_MODE_TEMPLATE_CB), context)
        )
        self.assertEqual(state, bot.Y_TEMPLATE_UPLOAD)
        self.assertEqual(context.user_data["yandex_mode"], "template")
        self.assertIn("квитанции Яндекс Банка", context.bot.messages[-1])

    def test_valid_template_warns_era_then_generates_with_donor_path(self):
        self.assertTrue(REAL_DONOR.is_file())
        context = self._context_for_upload(REAL_DONOR)
        # Сентябрьский донор: токен не потрачен, эпоха чужая -> предупреждение.
        state = self._upload_real_donor(context)
        self.assertEqual(state, bot.Y_TEMPLATE_WARN)
        self.assertIn("чужой эпохи", context.bot.messages[-1])
        template_path = Path(context.user_data["y_template_path"])
        self.assertTrue(template_path.is_file())
        self.assertEqual(context.user_data["y_template_token"], REAL_DONOR_TOKEN)

        state = run(
            bot.on_y_template_proceed(
                fake_update(callback=bot.Y_TEMPLATE_PROCEED_CB), context
            )
        )
        self.assertEqual(state, bot.RECIPIENT_NAME)
        self.assertIn("Шаблон принят", context.bot.messages[-1])
        self.assertIn("09.09.2026 в 14:20", context.bot.messages[-1])
        self.assertIn("2 500,00 ₽", context.bot.messages[-1])

        context.user_data.update(sample_data(
            yandex_mode="template",
            y_template_path=str(template_path),
            y_template_dir=str(template_path.parent),
            y_template_datetime=context.user_data["y_template_datetime"],
            y_template_amount=context.user_data["y_template_amount"],
        ))
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(TEMPLATE_FAKE_ENGINE, encoding="utf-8")
            capture = Path(tmp) / "donor_path.txt"
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            os.environ["FAKE_DONOR_CAPTURE"] = str(capture)
            try:
                state = run(bot.on_confirm_yes(fake_update(callback="confirm:yes"), context))
                captured_donor = capture.read_text(encoding="utf-8")
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)
                os.environ.pop("FAKE_DONOR_CAPTURE", None)
        self.assertEqual(state, -1)
        self.assertEqual(captured_donor, str(template_path))
        self.assertEqual(len(context.bot.documents), 1)
        # Шаблон одноразовый: после генерации временный файл и папка удалены.
        self.assertFalse(template_path.exists())
        self.assertFalse(template_path.parent.exists())

    def test_non_yandex_pdf_rejected_and_returns_to_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            garbage = Path(tmp) / "not-yandex.pdf"
            garbage.write_bytes(b"%PDF-1.4 definitely not a Yandex receipt")
            context = self._context_for_upload(garbage)
            dirs_before = set(bot.yandex_template_root().glob("tpl_*")) \
                if bot.yandex_template_root().exists() else set()
            state = run(
                bot.on_y_template_document(
                    fake_update(document_name="not-yandex.pdf"), context
                )
            )
            dirs_after = set(bot.yandex_template_root().glob("tpl_*")) \
                if bot.yandex_template_root().exists() else set()
        self.assertEqual(state, bot.Y_MODE)
        self.assertTrue(
            any("не валидная квитанция" in m for m in context.bot.messages)
        )
        self.assertNotIn("y_template_path", context.user_data)
        # Временный файл невалидного шаблона не остаётся на диске.
        self.assertEqual(dirs_after - dirs_before, set())

    def test_spent_token_gives_warning(self):
        context = self._context_for_upload(REAL_DONOR)
        state = self._upload_real_donor(context, spent={REAL_DONOR_TOKEN})
        try:
            self.assertEqual(state, bot.Y_TEMPLATE_WARN)
            warning = context.bot.messages[-1]
            self.assertIn("уже использовался", warning)
            self.assertIn("как повтор", warning)
            markup = context.bot.markups[-1]
            callbacks = {
                button.callback_data
                for row in markup.inline_keyboard for button in row
            }
            self.assertIn(bot.Y_TEMPLATE_PROCEED_CB, callbacks)
            self.assertIn(bot.CANCEL_CB, callbacks)
        finally:
            bot._cleanup_yandex_template_files(dict(context.user_data))

    def test_template_deleted_after_cancel(self):
        context = self._context_for_upload(REAL_DONOR)
        state = self._upload_real_donor(context)
        self.assertEqual(state, bot.Y_TEMPLATE_WARN)
        run(bot.on_y_template_proceed(fake_update(callback=bot.Y_TEMPLATE_PROCEED_CB), context))
        template_path = Path(context.user_data["y_template_path"])
        self.assertTrue(template_path.is_file())
        state = run(bot.cancel(fake_update(callback="cancel"), context))
        self.assertEqual(state, -1)
        self.assertFalse(template_path.exists())
        self.assertNotIn("y_template_path", context.user_data)

    def test_stale_template_timeout_cleanup(self):
        root = bot.yandex_template_root()
        root.mkdir(parents=True, exist_ok=True)
        stale = Path(tempfile.mkdtemp(prefix="tpl_stale_", dir=str(root)))
        (stale / "template.pdf").write_bytes(b"%PDF- stale")
        fresh = Path(tempfile.mkdtemp(prefix="tpl_fresh_", dir=str(root)))
        (fresh / "template.pdf").write_bytes(b"%PDF- fresh")
        old = time.time() - bot.YANDEX_TEMPLATE_TTL_SECONDS - 60
        os.utime(stale, (old, old))
        try:
            bot._cleanup_stale_yandex_templates()
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
        finally:
            bot._cleanup_yandex_template_files(
                {"y_template_path": str(fresh / "template.pdf"),
                 "y_template_dir": str(fresh)}
            )

    def test_normal_fresh_donor_refusal_returns_to_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FRESH_REFUSAL_ENGINE, encoding="utf-8")
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            try:
                context = fake_context(sample_data(yandex_mode="normal"))
                state = run(bot.on_confirm_yes(fake_update(callback="confirm:yes"), context))
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)
        self.assertEqual(state, bot.Y_MODE)
        self.assertTrue(
            any("свежий настоящий чек Яндекса за последние дни" in m
                for m in context.bot.messages)
        )


class CleanChatTest(unittest.TestCase):
    def test_answer_deletes_user_message_and_edits_bot_message_in_place(self):
        context = fake_context()
        run(bot.choose_yandex(fake_update(callback="bank:yandex"), context))
        mode_message_id = context.user_data["_bot_prompt_id"]
        run(bot.on_y_mode_choice(fake_update(callback=bot.Y_MODE_NORMAL_CB), context))
        # Вопрос выбора режима не удалён, а отредактирован на месте.
        self.assertIn(mode_message_id, context.bot.edited)
        self.assertNotIn(mode_message_id, context.bot.deleted)
        prompt_message_id = context.user_data["_bot_prompt_id"]
        state = run(
            bot.on_recipient_name(fake_update(text="Петрова Анна Ивановна"), context)
        )
        self.assertEqual(state, bot.RECIPIENT_PHONE)
        # Ответ пользователя (777) удалён; вопрос бота правится, не удаляется.
        self.assertIn(777, context.bot.deleted)
        self.assertIn(prompt_message_id, context.bot.edited)
        self.assertNotIn(prompt_message_id, context.bot.deleted)

    def test_clear_draft_keeps_bot_message_id_for_editing(self):
        context = fake_context({"scenario": "yandex", "_bot_prompt_id": 4242})
        bot._clear_draft(context)
        self.assertEqual(context.user_data.get("_bot_prompt_id"), 4242)

    def test_invalid_answer_repeats_question_in_one_message(self):
        context = fake_context(
            {"scenario": "yandex", "current_step": bot.RECIPIENT_PHONE}
        )
        state = run(bot.on_recipient_phone(fake_update(text="12345"), context))
        self.assertEqual(state, bot.RECIPIENT_PHONE)
        self.assertIn(777, context.bot.deleted)
        last = context.bot.messages[-1]
        self.assertIn("⚠️", last)
        self.assertIn("Телефон получателя", last)


FAKE_YANDEX_STATEMENT_ENGINE = '''
from pathlib import Path

def generate_yandex_statement(period_start, period_end, holder_name,
                              holder_birth, holder_passport, opening_balance,
                              operations, contract_date, contract_number,
                              issue_date, output_path, generated_at):
    assert (period_start, period_end) == ("03.11.2026", "05.11.2026"), (period_start, period_end)
    assert holder_name == "Васенин Иван Андреевич", holder_name
    assert holder_birth == "12.03.1995", holder_birth
    assert holder_passport == "4510 123456", holder_passport
    assert opening_balance == "100.00", opening_balance
    assert contract_date == "03.11.2026", contract_date
    assert contract_number is None, contract_number
    assert issue_date == "05.11.2026", issue_date
    assert generated_at.tzinfo is not None and generated_at.hour >= 0
    assert operations[0]["description"] == (
        "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк"
    ), operations
    assert operations[0]["amount"] == "+1 500,50", operations
    Path(output_path).write_bytes(b"%PDF-1.5 fake-yandex-statement")
    return output_path
'''

BROKEN_YANDEX_STATEMENT_ENGINE = '''
class StatementError(RuntimeError):
    pass

def generate_yandex_statement(**kwargs):
    raise StatementError("no donor glyph for 'Ю' in YSText-Medium")
'''


def ystmt_data(**overrides) -> dict:
    from datetime import date as _date

    data = {
        "scenario": "yandex_statement",
        "current_step": bot.YS_SUMMARY,
        "ystmt_period_start": _date(2026, 11, 3),
        "ystmt_period_end": _date(2026, 11, 5),
        "ystmt_issue_date": _date(2026, 11, 5),
        "ystmt_holder_name": "Васенин Иван Андреевич",
        "ystmt_holder_birth": _date(1995, 3, 12),
        "ystmt_holder_passport": "4510 123456",
        "ystmt_contract_date": _date(2026, 11, 3),
        "ystmt_contract_number": None,
        "ystmt_opening_balance": "100.00",
        "ystmt_ops": [
            {
                "datetime": "03.11.2026 10:15",
                "description": "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
                "amount": "+1 500,50",
            },
            {
                "datetime": "04.11.2026 12:40",
                "description": "Исходящий перевод СБП, Анна Ивановна К., +7 911 222-33-44, Т-Банк",
                "amount": "-250,00",
            },
        ],
        "ystmt_draft": None,
    }
    data.update(overrides)
    return data


class YandexStatementValidatorsTest(unittest.TestCase):
    def test_period_birth_passport_contract(self):
        self.assertEqual(bot.parse_passport("4510123456"), "4510 123456")
        self.assertEqual(bot.parse_passport("4510 123456"), "4510 123456")
        for bad in ("123", "4510 12345", ""):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_passport(bad)
        born = bot.parse_birth_date("12.03.1995")
        self.assertEqual((born.year, born.month, born.day), (1995, 3, 12))
        for bad in ("32.13.1995", "12.03.1890", "завтра", ""):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_birth_date(bad)
        self.assertEqual(
            bot.parse_contract_number("Э20261103102094982"), "Э20261103102094982"
        )
        with self.assertRaises(ValueError):
            bot.parse_contract_number("20261103102094982")

    def test_op_datetime_inside_and_outside_period(self):
        from datetime import date as _date

        start, end = _date(2026, 11, 3), _date(2026, 11, 5)
        moment = bot.parse_ystmt_op_datetime("04.11.2026 12:40", start, end)
        self.assertEqual((moment.day, moment.hour, moment.minute), (4, 12, 40))
        only = bot.parse_ystmt_op_datetime("12:40", start, start)
        self.assertEqual((only.day, only.hour), (3, 12))
        with self.assertRaises(ValueError):
            bot.parse_ystmt_op_datetime("12:40", start, end,
                                         now=datetime(2026, 11, 10, 9, 0, tzinfo=bot.MSK))
        for bad in ("06.11.2026 10:00", "02.11.2026 10:00", "когда"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_ystmt_op_datetime(bad, start, end)

    def test_sbp_description_and_person_short(self):
        self.assertEqual(bot.format_person_short("Иванов Иван Петрович"), "Иван Петрович И.")
        self.assertEqual(bot.format_person_short("Иван Петрович И."), "Иван Петрович И.")
        description = bot.build_ystmt_sbp_description(
            "in", "Сидоров Иван Петрович", "+7 900 111-22-33", "Сбербанк"
        )
        self.assertEqual(
            description,
            "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
        )
        out = bot.build_ystmt_sbp_description(
            "out", "Ким Анна Ивановна", "+7 911 222-33-44", "Т-Банк"
        )
        self.assertTrue(out.startswith("Исходящий перевод СБП, Анна Ивановна К.,"))

    def test_totals_like_engine(self):
        income, expense, closing = bot.ystmt_totals(
            ystmt_data()["ystmt_ops"], "100.00"
        )
        self.assertEqual(str(income), "1500.50")
        self.assertEqual(str(expense), "250.00")
        self.assertEqual(str(closing), "1350.50")
        income, expense, closing = bot.ystmt_totals([], "100.00")
        self.assertEqual((str(income), str(expense), str(closing)), ("0", "0", "100.00"))

    def test_bulk_operations_parse_and_reject(self):
        from datetime import date as _date

        ops = bot.parse_ystmt_bulk_operations(
            "04.11.2026 12:40 | Оплата услуг | -250,00\n"
            "03.11.2026 10:15 | Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк | +1 500,50",
            _date(2026, 11, 3), _date(2026, 11, 5),
        )
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0]["amount"], "-250,00")
        with self.assertRaises(ValueError):
            bot.parse_ystmt_bulk_operations(
                "09.11.2026 12:40 | вне периода | -250,00",
                _date(2026, 11, 3), _date(2026, 11, 5),
            )
        with self.assertRaises(ValueError):
            bot.parse_ystmt_bulk_operations(
                "04.11.2026 12:40 | без знака | 250,00",
                _date(2026, 11, 3), _date(2026, 11, 5),
            )


class YandexStatementHandlersTest(unittest.TestCase):
    def _to_ops_editor(self):
        context = fake_context()
        state = run(bot.choose_yandex(fake_update(callback="bank:yandex"), context))
        self.assertEqual(state, bot.Y_MENU)
        state = run(bot.on_yandex_doc_choice(fake_update(callback=bot.Y_DOC_STMT_CB), context))
        self.assertEqual(state, bot.YS_PERIOD)
        state = run(bot.on_ystmt_period(fake_update(text="03.11.2026–05.11.2026"), context))
        self.assertEqual(state, bot.YS_ISSUE_CHOICE)
        state = run(bot.on_ystmt_issue_choice(fake_update(callback="ysissue:custom"), context))
        self.assertEqual(state, bot.YS_ISSUE_INPUT)
        state = run(bot.on_ystmt_issue_input(fake_update(text="05.11.2026"), context))
        self.assertEqual(state, bot.YS_HOLDER_NAME)
        state = run(bot.on_ystmt_holder_name(fake_update(text="Васенин Иван Андреевич"), context))
        self.assertEqual(state, bot.YS_HOLDER_BIRTH)
        state = run(bot.on_ystmt_holder_birth(fake_update(text="12.03.1995"), context))
        self.assertEqual(state, bot.YS_HOLDER_PASSPORT)
        state = run(bot.on_ystmt_holder_passport(fake_update(text="4510123456"), context))
        self.assertEqual(state, bot.YS_CONTRACT_DATE_CHOICE)
        self.assertEqual(context.user_data["ystmt_holder_passport"], "4510 123456")
        state = run(bot.on_ystmt_contract_date_choice(fake_update(callback="yscontract:start"), context))
        self.assertEqual(state, bot.YS_CONTRACT_NUMBER_CHOICE)
        state = run(bot.on_ystmt_contract_number_choice(fake_update(callback="ysnum:auto"), context))
        self.assertEqual(state, bot.YS_OPENING)
        state = run(bot.on_ystmt_opening(fake_update(text="100,00"), context))
        self.assertEqual(state, bot.YS_OPS_EDITOR)
        return context

    def test_empty_operations_go_to_summary(self):
        context = self._to_ops_editor()
        state = run(bot.on_ystmt_ops_editor_choice(fake_update(callback="ysops:none"), context))
        self.assertEqual(state, bot.YS_SUMMARY)
        summary = context.bot.messages[-1]
        self.assertIn("Без операций", summary)
        self.assertIn("Исходящий остаток: <b>100,00 ₽</b>", summary)

    def test_add_sbp_operation_and_delete_it(self):
        context = self._to_ops_editor()
        state = run(bot.on_ystmt_ops_editor_choice(fake_update(callback="ysops:add"), context))
        self.assertEqual(state, bot.YS_OP_TYPE)
        state = run(bot.on_ystmt_op_type(fake_update(callback="ysop:in"), context))
        self.assertEqual(state, bot.YS_OP_NAME)
        state = run(bot.on_ystmt_op_name(fake_update(text="Сидоров Иван Петрович"), context))
        self.assertEqual(state, bot.YS_OP_PHONE)
        state = run(bot.on_ystmt_op_phone(fake_update(text="8 900 111-22-33"), context))
        self.assertEqual(state, bot.YS_OP_BANK)
        state = run(bot.on_ystmt_op_bank_choice(fake_update(callback="ysbank:sber"), context))
        self.assertEqual(state, bot.YS_OP_DT_CHOICE)
        state = run(bot.on_ystmt_op_dt_choice(fake_update(callback="ysdt:input"), context))
        self.assertEqual(state, bot.YS_OP_DT_INPUT)
        # вне периода не продвигает
        state = run(bot.on_ystmt_op_dt_input(fake_update(text="09.11.2026 10:00"), context))
        self.assertEqual(state, bot.YS_OP_DT_INPUT)
        self.assertEqual(len(context.user_data.get("ystmt_ops") or []), 0)
        state = run(bot.on_ystmt_op_dt_input(fake_update(text="03.11.2026 10:15"), context))
        self.assertEqual(state, bot.YS_OP_AMOUNT)
        state = run(bot.on_ystmt_op_amount(fake_update(text="1 500,50"), context))
        self.assertEqual(state, bot.YS_OPS_EDITOR)
        ops = context.user_data["ystmt_ops"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(
            ops[0]["description"],
            "Входящий перевод СБП, Иван Петрович С., +7 900 111-22-33, Сбербанк",
        )
        self.assertEqual(ops[0]["amount"], "+1 500,50")
        self.assertIn("Приход: +1 500,50 ₽", context.bot.messages[-1])
        # удаление
        state = run(bot.on_ystmt_ops_editor_choice(fake_update(callback="ysops:delete"), context))
        self.assertEqual(state, bot.YS_OP_DELETE)
        state = run(bot.on_ystmt_op_delete_choice(fake_update(callback="ysdel:0"), context))
        self.assertEqual(state, bot.YS_OPS_EDITOR)
        self.assertEqual(context.user_data["ystmt_ops"], [])

    def test_edit_fields_from_summary(self):
        context = fake_context(ystmt_data())
        state = run(bot.on_ystmt_summary_edit(fake_update(callback="ysgen:edit"), context))
        self.assertEqual(state, bot.YS_EDIT_SELECT)
        state = run(bot.on_ystmt_edit_field(fake_update(callback="ysedit:holder_name"), context))
        self.assertEqual(state, bot.YS_HOLDER_NAME)
        state = run(bot.on_ystmt_holder_name(fake_update(text="Васенин Иван Андреевич"), context))
        self.assertEqual(state, bot.YS_SUMMARY)
        # операции из сводки правятся через редактор
        state = run(bot.on_ystmt_edit_field(fake_update(callback="ysedit:operations"), context))
        self.assertEqual(state, bot.YS_OPS_EDITOR)
        state = run(bot.on_ystmt_ops_editor_choice(fake_update(callback="ysops:done"), context))
        self.assertEqual(state, bot.YS_SUMMARY)
        summary = context.bot.messages[-1]
        self.assertIn("Приход: <b>+1 500,50 ₽</b>", summary)
        self.assertIn("Исходящий остаток: <b>1 350,50 ₽</b>", summary)

    def test_generate_with_fake_statement_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "statement.py"
            engine.write_text(FAKE_YANDEX_STATEMENT_ENGINE, encoding="utf-8")
            os.environ["YANDEX_STATEMENT_PATH"] = str(engine)
            try:
                context = fake_context(ystmt_data())
                state = run(bot.on_ystmt_generate(fake_update(callback="ysgen:generate"), context))
            finally:
                os.environ.pop("YANDEX_STATEMENT_PATH", None)
        self.assertEqual(state, -1)
        self.assertEqual(len(context.bot.documents), 1)
        doc = context.bot.documents[0]
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f-]{36}\.pdf", doc["filename"]))
        self.assertIn("Выписка готова", doc["caption"])

    def test_statement_engine_error_returns_to_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "statement.py"
            engine.write_text(BROKEN_YANDEX_STATEMENT_ENGINE, encoding="utf-8")
            os.environ["YANDEX_STATEMENT_PATH"] = str(engine)
            try:
                context = fake_context(ystmt_data())
                state = run(bot.on_ystmt_generate(fake_update(callback="ysgen:generate"), context))
            finally:
                os.environ.pop("YANDEX_STATEMENT_PATH", None)
        self.assertEqual(state, bot.YS_SUMMARY)
        self.assertTrue(
            any("Не получилось" in m and "глиф" in m for m in context.bot.messages)
        )
        self.assertEqual(context.bot.documents, [])

    def test_explain_errors(self):
        self.assertIn("одну страницу", bot.explain_ystmt_error(
            RuntimeError("operations do not fit one page")
        ))
        self.assertIn("владельцу", bot.explain_ystmt_error(
            RuntimeError("no statement donor in banks/yandex/donors_statement/")
        ))


class EngineCallTest(unittest.TestCase):
    def _run_with_engine(self, engine_code: str):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(engine_code, encoding="utf-8")
            out = Path(tmp) / "out.pdf"
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            try:
                pdf_path = asyncio.run(bot.generate_pdf(sample_data(), out))
                return pdf_path.read_bytes()
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)

    def test_engine_is_called_and_pdf_returned(self):
        self.assertTrue(self._run_with_engine(FAKE_ENGINE).startswith(b"%PDF-"))

    def test_template_mode_passes_donor_path_to_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(TEMPLATE_FAKE_ENGINE, encoding="utf-8")
            capture = Path(tmp) / "donor_path.txt"
            out = Path(tmp) / "out.pdf"
            os.environ["YANDEX_ENGINE_PATH"] = str(engine)
            donor = Path(tmp) / "user-template.pdf"
            donor.write_bytes(b"%PDF-1.5 user template")
            os.environ["FAKE_DONOR_CAPTURE"] = str(capture)
            try:
                data = sample_data(
                    yandex_mode="template", y_template_path=str(donor)
                )
                pdf_path = asyncio.run(bot.generate_pdf(data, out))
                self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-"))
                self.assertEqual(capture.read_text(encoding="utf-8"), str(donor))
            finally:
                os.environ.pop("YANDEX_ENGINE_PATH", None)
                os.environ.pop("FAKE_DONOR_CAPTURE", None)

    def test_broken_engine_raises_for_handler_to_report(self):
        with self.assertRaisesRegex(RuntimeError, "engine boom"):
            self._run_with_engine(BROKEN_ENGINE)

    def test_missing_engine_raises_clear_error(self):
        os.environ["YANDEX_ENGINE_PATH"] = "/nonexistent/engine.py"
        try:
            with self.assertRaisesRegex(FileNotFoundError, "Движок Яндекса не найден"):
                asyncio.run(
                    bot.generate_pdf(
                        sample_data(), Path(tempfile.gettempdir()) / "x.pdf"
                    )
                )
        finally:
            os.environ.pop("YANDEX_ENGINE_PATH", None)


FAKE_MTS_ENGINE = '''
from pathlib import Path

def generate_mts(amount, operation_dt, recipient_name, recipient_phone,
                 recipient_bank, output_path, sender_account=None, message=None,
                 donor_path=None, pool=None, generated_at=None):
    assert amount == "1 500,00 ₽", amount
    assert operation_dt.second == 0 and operation_dt.tzinfo is not None
    assert recipient_name == "Петрова Анна Ивановна", recipient_name
    assert recipient_phone == "+7 963 297-20-08", recipient_phone
    assert recipient_bank == "Сбербанк", recipient_bank
    assert sender_account is None, sender_account
    assert message is None, message
    assert donor_path is None
    Path(output_path).write_bytes(b"%PDF-1.5 fake-mts")
    return output_path
'''

BROKEN_MTS_ENGINE = '''
class GlyphCoverageError(RuntimeError):
    pass

def generate_mts(**kwargs):
    raise GlyphCoverageError("Буквы вне пула глифов настоящих чеков МТС: Ю")
'''


class MtsBotTest(unittest.TestCase):
    def test_message_and_menu(self):
        self.assertEqual(bot.parse_mts_message("  Перевод   СБП "), "Перевод СБП")
        with self.assertRaises(ValueError):
            bot.parse_mts_message("hello")
        markup = bot._main_menu_markup()
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🔴 МТС", labels)
        self.assertIn("🟡 Яндекс чек", labels)
        self.assertIn("🚀 Рокетбанк", labels)
        self.assertIn("🔵 ВТБ", labels)
        self.assertIn("🟣 ВБ Банк", labels)
        self.assertIn("🔴 Альфа", labels)
        self.assertIn("🌾 Россельхозбанк", labels)
        self.assertIn("🧾 Озон Банк", labels)
        self.assertIn("💛 Т-Банк", labels)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        self.assertEqual(bot.mts_receipt_filename({}), "Unknown.pdf")
        mts_menu = bot._mts_menu_markup()
        mts_labels = [button.text for row in mts_menu.inline_keyboard for button in row]
        self.assertIn("💸 Перевод по СБП", mts_labels)
        self.assertIn("💳 Перевод по карте", mts_labels)
        self.assertIn("📄 Выписка", mts_labels)

    def test_flow_to_summary_and_edit(self):
        context = fake_context()
        state = run(bot.on_mts_start(fake_update(callback="bank:mts"), context))
        self.assertEqual(state, bot.M_MENU)
        state = run(bot.on_mts_sbp_start(fake_update(callback="mts:sbp"), context))
        self.assertEqual(state, bot.M_RECIPIENT_NAME)
        state = run(bot.on_mts_name(fake_update(text="Петрова Анна Ивановна"), context))
        self.assertEqual(state, bot.M_RECIPIENT_PHONE)
        state = run(bot.on_mts_phone(fake_update(text="89632972008"), context))
        self.assertEqual(state, bot.M_RECIPIENT_BANK)
        self.assertEqual(context.user_data["recipient_phone"], "+7 963 297-20-08")
        state = run(bot.on_mts_bank_choice(fake_update(callback="mbank:sber"), context))
        self.assertEqual(state, bot.M_AMOUNT)
        state = run(bot.on_mts_amount(fake_update(text="1500"), context))
        self.assertEqual(state, bot.M_ACCOUNT_CHOICE)
        self.assertEqual(context.user_data["amount"], "1 500,00 ₽")
        state = run(bot.on_mts_account_choice(fake_update(callback="macc:keep"), context))
        self.assertEqual(state, bot.M_MESSAGE_CHOICE)
        self.assertIsNone(context.user_data["sender_account"])
        state = run(bot.on_mts_message_choice(fake_update(callback="mmsg:default"), context))
        self.assertEqual(state, bot.M_TIME_CHOICE)
        self.assertIsNone(context.user_data["mts_message"])
        state = run(bot.on_mts_time_choice(fake_update(callback="mtime:now"), context))
        self.assertEqual(state, bot.M_SUMMARY)
        self.assertEqual(context.user_data["operation_dt"].second, 0)
        summary = context.bot.messages[-1]
        self.assertIn("ПОЛУЧАТЕЛЬ", summary)
        self.assertIn("Перевод СБП", summary)
        self.assertIn("0,00 ₽", summary)
        state = run(bot.on_mts_summary_edit(fake_update(callback="mgen:edit"), context))
        self.assertEqual(state, bot.M_EDIT_SELECT)
        state = run(bot.on_mts_edit_field(fake_update(callback="medit:amount"), context))
        self.assertEqual(state, bot.M_AMOUNT)
        state = run(bot.on_mts_amount(fake_update(text="248,50"), context))
        self.assertEqual(state, bot.M_SUMMARY)
        self.assertEqual(context.user_data["amount"], "248,50 ₽")

    def test_back_and_memory(self):
        context = fake_context({
            "scenario": "mts",
            "current_step": bot.M_RECIPIENT_PHONE,
            "recipient_name": "Петрова Анна Ивановна",
        })
        state = run(bot.on_nav_back(fake_update(callback="nav:back"), context))
        self.assertEqual(state, bot.M_RECIPIENT_NAME)
        bot._save_mts_memory(context)
        context.user_data.update({
            "recipient_name": "Петрова Анна Ивановна",
            "recipient_phone": "+7 963 297-20-08",
            "recipient_bank": "Сбербанк",
        })
        bot._save_mts_memory(context)
        bot._clear_draft(context)
        self.assertIn("mem_mts_recipient", context.user_data)
        context.user_data["scenario"] = "mts"
        state = run(bot.on_mts_mem_recipient(fake_update(callback="mmem:recipient"), context))
        self.assertEqual(state, bot.M_AMOUNT)
        self.assertEqual(context.user_data["recipient_bank"], "Сбербанк")

    def test_generate_with_fake_engine_and_glyph_error(self):
        data = {
            "scenario": "mts",
            "current_step": bot.M_SUMMARY,
            "recipient_name": "Петрова Анна Ивановна",
            "recipient_phone": "+7 963 297-20-08",
            "recipient_bank": "Сбербанк",
            "amount": "1 500,00 ₽",
            "sender_account": None,
            "mts_message": None,
            "operation_dt": bot.parse_rocket_time("03.11.2026 14:20"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_MTS_ENGINE, encoding="utf-8")
            os.environ["MTS_ENGINE_PATH"] = str(engine)
            bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
            try:
                context = fake_context(data)
                state = run(bot.on_mts_generate(fake_update(callback="mgen:generate"), context))
            finally:
                os.environ.pop("MTS_ENGINE_PATH", None)
                bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
        self.assertEqual(state, -1)
        self.assertEqual(context.bot.documents[0]["filename"], "Unknown.pdf")
        self.assertIn("Чек готов", context.bot.documents[0]["caption"])

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(BROKEN_MTS_ENGINE, encoding="utf-8")
            os.environ["MTS_ENGINE_PATH"] = str(engine)
            bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
            try:
                context = fake_context(dict(data))
                state = run(bot.on_mts_generate(fake_update(callback="mgen:generate"), context))
            finally:
                os.environ.pop("MTS_ENGINE_PATH", None)
                bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
        self.assertEqual(state, bot.M_SUMMARY)
        self.assertTrue(any("нужен новый чек владельца" in m for m in context.bot.messages))
        self.assertEqual(context.bot.documents, [])

    def test_mts_card_flow_and_fake_engine(self):
        self.assertEqual(bot.parse_mts_card("8429"), "8429")
        self.assertEqual(bot.parse_mts_card("2200 1234 5678 8429"), "2200123456788429")
        with self.assertRaises(ValueError):
            bot.parse_mts_card("12")
        context = fake_context()
        state = run(bot.on_mts_card_start(fake_update(callback="mts:card"), context))
        self.assertEqual(state, bot.MC_RECIPIENT_CARD)
        self.assertEqual(context.user_data["scenario"], "mts_card")
        state = run(bot.on_mc_recipient_card(fake_update(text="8429"), context))
        self.assertEqual(state, bot.MC_AMOUNT)
        state = run(bot.on_mc_amount(fake_update(text="225,50"), context))
        self.assertEqual(state, bot.MC_SENDER_CARD_CHOICE)
        self.assertEqual(context.user_data["amount"], "225,50 ₽")
        state = run(bot.on_mc_sender_card_choice(fake_update(callback="mccard:skip"), context))
        self.assertEqual(state, bot.MC_ACCOUNT_CHOICE)
        state = run(bot.on_mc_account_choice(fake_update(callback="mcacc:keep"), context))
        self.assertEqual(state, bot.MC_TIME_CHOICE)
        state = run(bot.on_mc_time_choice(fake_update(callback="mctime:now"), context))
        self.assertEqual(state, bot.MC_SUMMARY)
        self.assertIn("0,00 ₽", context.bot.messages[-1])
        data = dict(context.user_data)
        fake = '''
from pathlib import Path
def generate_mts(*args, **kwargs):
    raise AssertionError("SBP must not be called")
def generate_mts_card(amount, operation_dt, recipient_card, output_path=None,
                      sender_card=None, sender_account=None, **kwargs):
    assert amount == "225,50 ₽", amount
    assert recipient_card == "8429", recipient_card
    assert sender_card is None
    assert sender_account is None
    Path(output_path).write_bytes(b"%PDF-1.5 fake-mts-card")
    return output_path
'''
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(fake, encoding="utf-8")
            os.environ["MTS_ENGINE_PATH"] = str(engine)
            bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
            try:
                context = fake_context(data)
                state = run(bot.on_mc_generate(fake_update(callback="mcgen:generate"), context))
            finally:
                os.environ.pop("MTS_ENGINE_PATH", None)
                bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
        self.assertEqual(state, -1)
        self.assertEqual(context.bot.documents[0]["filename"], "Unknown.pdf")

        broken = '''
class GlyphCoverageError(RuntimeError):
    pass
def generate_mts(**kwargs):
    pass
def generate_mts_card(**kwargs):
    raise GlyphCoverageError("нет глифа")
'''
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(broken, encoding="utf-8")
            os.environ["MTS_ENGINE_PATH"] = str(engine)
            bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
            try:
                context = fake_context(data)
                state = run(bot.on_mc_generate(fake_update(callback="mcgen:generate"), context))
            finally:
                os.environ.pop("MTS_ENGINE_PATH", None)
                bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
        self.assertEqual(state, bot.MC_SUMMARY)
        self.assertTrue(any("Не получилось" in m for m in context.bot.messages))

    def test_mts_statement_counts_and_manual_ops(self):
        self.assertEqual(bot.parse_stmt_operation_count("20", "mts"), 20)
        for bad in ("1", "4", "10", "6"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_stmt_operation_count(bad, "mts")
        context = fake_context()
        state = run(bot.on_mts_stmt_start(fake_update(callback="mts:stmt"), context))
        self.assertEqual(state, bot.S_PERIOD)
        self.assertEqual(context.user_data["scenario"], "mts_statement")
        state = run(bot.on_s_period(fake_update(text="01.11.2026–07.11.2026"), context))
        self.assertEqual(state, bot.S_ISSUE_CHOICE)
        state = run(bot.on_s_issue_choice(fake_update(callback="sissue:today"), context))
        self.assertEqual(state, bot.S_HOLDER)
        state = run(bot.on_s_holder(fake_update(text="Иванов Иван Иванович"), context))
        self.assertEqual(state, bot.S_ACCOUNT_CHOICE)
        _text, markup = bot._stmt_prompt(context, bot.S_OP_COUNT_CHOICE)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels.count("0") + labels.count("2") + labels.count("3")
                         + labels.count("5") + labels.count("20"), 5)
        self.assertNotIn("Другое (0–10)", " ".join(labels))
        context.user_data.update({
            "stmt_period_start": date(2026, 11, 1),
            "stmt_period_end": date(2026, 11, 7),
            "stmt_issue_date": date(2026, 11, 7),
            "stmt_holder_name": "Иванов Иван Иванович",
            "stmt_opening_balance": "16.00",
            "stmt_operation_count": 2,
        })
        state = run(bot.on_s_source_choice(fake_update(callback="ssource:manual"), context))
        self.assertEqual(state, bot.S_OPS_EDITOR)
        state = run(bot.on_s_op_kind(fake_update(callback="sopk:sbp_out"), context))
        self.assertEqual(state, bot.S_OP_AMOUNT)
        state = run(bot.on_s_op_amount(fake_update(text="1500"), context))
        self.assertEqual(state, bot.S_OP_DT_CHOICE)
        context.user_data["stmt_period_start"] = date(2026, 11, 4)
        context.user_data["stmt_period_end"] = date(2026, 11, 4)
        state = run(bot.on_s_op_dt_input(fake_update(text="04.11.2026 12:40"), context))
        self.assertEqual(state, bot.S_OP_DETAIL)
        state = run(bot.on_s_op_detail_text(fake_update(text="Петрова Анна Ивановна"), context))
        self.assertEqual(state, bot.S_OPS_EDITOR)
        self.assertEqual(len(context.user_data["stmt_ops"]), 1)
        self.assertEqual(context.user_data["stmt_ops"][0]["kind"], "sbp_out")
        self.assertEqual(context.user_data["stmt_ops"][0]["recipient"], "Петрова Анна Ивановна")
        state = run(bot.on_s_ops_editor_choice(fake_update(callback="sops:done"), context))
        self.assertEqual(state, bot.S_OPS_EDITOR)
        self.assertTrue(any("нужно ровно" in m for m in context.bot.messages))
        fake = '''
from pathlib import Path
def generate_mts(**kwargs):
    pass
def generate_mts_statement(period_start, period_end, issue_date, holder_name,
                           account=None, card=None, opening_balance=None,
                           operations=None, operation_count=None,
                           receipt_paths=None, fill_random=True, output_path=None, **kwargs):
    assert fill_random is False
    assert operation_count == 2
    assert len(operations) == 2
    assert operations[0]["kind"] == "sbp_out"
    Path(output_path).write_bytes(b"%PDF-1.5 fake-mts-stmt")
    return output_path
'''
        context.user_data["stmt_ops"].append({
            "kind": "sbp_in",
            "datetime": bot.parse_ystmt_op_datetime(
                "04.11.2026 13:00", date(2026, 11, 4), date(2026, 11, 4)
            ),
            "amount": "10.00",
            "recipient": "Иванов Иван Иванович",
        })
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(fake, encoding="utf-8")
            os.environ["MTS_ENGINE_PATH"] = str(engine)
            bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
            try:
                state = run(bot.on_s_generate(fake_update(callback="sgen:generate"), context))
            finally:
                os.environ.pop("MTS_ENGINE_PATH", None)
                bot._MTS_ENGINE_CACHE.update(path=None, module=None, pool=None)
        self.assertEqual(state, -1)
        self.assertTrue(context.bot.documents)


FAKE_ALFA_PATCHER = '''
from pathlib import Path

def patch_pdf_file(input_path, output_path, amount_from, amount_to, bank="auto"):
    assert bank == "alfa", bank
    assert amount_from == 10 and amount_to == 5000, (amount_from, amount_to)
    assert Path(input_path).is_file()
    Path(output_path).write_bytes(b"%PDF-1.5 fake-alfa")
    return True, None
'''


class AlfaBotTest(unittest.TestCase):
    def test_amount_pair(self):
        self.assertEqual(bot.parse_alfa_amounts("10 5000"), (10, 5000))
        self.assertEqual(bot.parse_alfa_amounts("10 5 000"), (10, 5000))
        with self.assertRaises(ValueError):
            bot.parse_alfa_amounts("5000")

    def test_upload_then_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            engine = Path(tmp) / "pdf_patcher.py"
            engine.write_text(FAKE_ALFA_PATCHER, encoding="utf-8")
            os.environ["ALFA_PATCHER_PATH"] = str(engine)
            bot._ALFA_PATCHER_CACHE.update(path=None, module=None)
            try:
                context = fake_context()
                context.bot.file_source = source
                state = run(bot.on_alfa_start(fake_update(callback="bank:alfa"), context))
                self.assertEqual(state, bot.A_UPLOAD)
                state = run(bot.on_alfa_document(
                    fake_update(document_name="чек.pdf"), context
                ))
                self.assertEqual(state, bot.A_AMOUNT)
                self.assertTrue(Path(context.user_data["alfa_pdf_path"]).is_file())
                state = run(bot.on_alfa_amounts(fake_update(text="10 5 000"), context))
            finally:
                os.environ.pop("ALFA_PATCHER_PATH", None)
                bot._ALFA_PATCHER_CACHE.update(path=None, module=None)
        self.assertEqual(state, -1)
        self.assertEqual(context.bot.documents[0]["filename"], "чек_5000.pdf")
        self.assertIn("10 ₽ → 5000 ₽", context.bot.documents[0]["caption"])
        self.assertNotIn("alfa_pdf_path", context.user_data)

    def test_patcher_module_loads(self):
        bot._ALFA_PATCHER_CACHE.update(path=None, module=None)
        module = bot.load_alfa_patcher()
        self.assertTrue(callable(module.patch_pdf_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)
