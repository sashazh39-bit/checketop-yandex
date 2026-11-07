#!/usr/bin/env python3
"""Тесты бота ВБ Банка: только чек СБП, целые рубли, ошибки глифов/токенов."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yandex_bot as bot  # noqa: E402


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []
        self._next_message_id = 1000

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        pass

    async def send_document(self, chat_id, document, filename, caption, reply_markup=None):
        self.documents.append({"filename": filename, "caption": caption})


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.message_id = 777

    async def reply_text(self, text, **kwargs):
        pass


class FakeQuery:
    def __init__(self, data):
        self.data = data

    async def answer(self):
        pass


def fake_update(text=None, callback=None):
    return SimpleNamespace(
        effective_message=FakeMessage(text or ""),
        callback_query=FakeQuery(callback) if callback else None,
        effective_chat=SimpleNamespace(id=101),
        effective_user=SimpleNamespace(id=7),
    )


def fake_context(user_data=None):
    return SimpleNamespace(bot=FakeBot(), user_data=dict(user_data or {}))


def run(coro):
    return asyncio.run(coro)


FAKE_WB_ENGINE = '''
from pathlib import Path

def generate_wb(amount, operation_dt, recipient_name, recipient_phone,
                recipient_bank, output_path=None, **kwargs):
    assert "sender_name" not in kwargs
    assert "sender_phone" not in kwargs
    assert amount == "12 ₽", amount
    assert recipient_name == "Иванов Иван Иванович"
    assert recipient_phone == "+7 963 297-20-08"
    assert recipient_bank == "Сбербанк"
    Path(output_path).write_bytes(b"%PDF-1.5 fake-wb")
    return output_path
'''

BROKEN_WB_ENGINE = '''
class GlyphCoverageError(RuntimeError):
    pass
class NoUnusedDonorTokenError(RuntimeError):
    pass

def generate_wb(**kwargs):
    raise GlyphCoverageError("нет глифа 5 в Medium")
'''

TOKEN_WB_ENGINE = '''
class GlyphCoverageError(RuntimeError):
    pass
class NoUnusedDonorTokenError(RuntimeError):
    pass

def generate_wb(**kwargs):
    raise NoUnusedDonorTokenError("В пуле нет настоящих чеков ВБ Банка")
'''


class WbBotTest(unittest.TestCase):
    def test_menu_only_sbp(self):
        markup = bot._wb_menu_markup()
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("💸 Чек СБП", labels)
        self.assertTrue(all("карта" not in t.lower() for t in labels))
        self.assertTrue(all("выписка" not in t.lower() for t in labels))
        context = fake_context()
        state = run(bot.on_wb_menu(fake_update(callback="bank:wb"), context))
        self.assertEqual(state, bot.W_MENU)

    def test_whole_rubles_only(self):
        self.assertEqual(bot.parse_wb_amount("12"), "12 ₽")
        self.assertEqual(bot.parse_wb_amount("1000"), "1 000 ₽")
        with self.assertRaises(ValueError):
            bot.parse_wb_amount("12,50")

    def test_flow_and_fake_engine_ignores_sender(self):
        context = fake_context()
        state = run(bot.on_wb_sbp_start(fake_update(callback="wb:sbp"), context))
        self.assertEqual(state, bot.W_RECIPIENT_NAME)
        state = run(bot.on_w_recipient_name(fake_update(text="Иванов Иван Иванович"), context))
        state = run(bot.on_w_recipient_phone(fake_update(text="89632972008"), context))
        state = run(bot.on_w_bank_choice(fake_update(callback="wbank:sber"), context))
        state = run(bot.on_w_amount(fake_update(text="12"), context))
        self.assertEqual(context.user_data["amount"], "12 ₽")
        state = run(bot.on_w_time_choice(fake_update(callback="wtime:now"), context))
        self.assertEqual(state, bot.W_SUMMARY)
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_WB_ENGINE, encoding="utf-8")
            os.environ["WB_ENGINE_PATH"] = str(engine)
            bot._WB_ENGINE_CACHE.update(path=None, module=None)
            try:
                state = run(bot.on_w_generate(fake_update(callback="wgen:generate"), context))
            finally:
                os.environ.pop("WB_ENGINE_PATH", None)
                bot._WB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, -1)
        self.assertTrue(context.bot.documents[0]["filename"].endswith(".pdf"))

    def test_glyph_and_token_errors_do_not_crash(self):
        data = {
            "scenario": "wb",
            "recipient_name": "Иванов Иван Иванович",
            "recipient_phone": "+7 963 297-20-08",
            "recipient_bank": "Сбербанк",
            "amount": "12 ₽",
            "operation_dt": bot.parse_rocket_time("03.11.2026 14:20"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(BROKEN_WB_ENGINE, encoding="utf-8")
            os.environ["WB_ENGINE_PATH"] = str(engine)
            bot._WB_ENGINE_CACHE.update(path=None, module=None)
            try:
                context = fake_context(data)
                state = run(bot.on_w_generate(fake_update(callback="wgen:generate"), context))
            finally:
                os.environ.pop("WB_ENGINE_PATH", None)
                bot._WB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, bot.W_SUMMARY)
        self.assertTrue(any("Не получилось" in m for m in context.bot.messages))
        self.assertEqual(context.bot.documents, [])

        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(TOKEN_WB_ENGINE, encoding="utf-8")
            os.environ["WB_ENGINE_PATH"] = str(engine)
            bot._WB_ENGINE_CACHE.update(path=None, module=None)
            try:
                context = fake_context(data)
                state = run(bot.on_w_generate(fake_update(callback="wgen:generate"), context))
            finally:
                os.environ.pop("WB_ENGINE_PATH", None)
                bot._WB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, bot.W_SUMMARY)
        self.assertTrue(any("Не получилось" in m for m in context.bot.messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
