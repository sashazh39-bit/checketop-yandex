#!/usr/bin/env python3
"""Тесты бота ВТБ: меню, СБП/карта/выписка, фейковые generate_*."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
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


FAKE_VTB_ENGINE = '''
from pathlib import Path

def generate_vtb(amount, operation_dt, sender_name, sender_phone,
                 recipient_name, recipient_phone, recipient_bank,
                 output_path=None, sender_account=None, message=None, **kwargs):
    assert amount == "5 000,00 ₽", amount
    assert sender_phone == "+7 900 351-70-80"
    assert recipient_phone == "+7 963 297-20-08"
    assert recipient_bank == "Сбербанк"
    Path(output_path).write_bytes(b"%PDF-1.4 fake-vtb-sbp")
    return output_path

def generate_vtb_card(amount, operation_dt, sender_name, recipient_card,
                      recipient_bank=None, output_path=None,
                      sender_account=None, payer_card=None, fee=None, **kwargs):
    assert amount == "1 500,00 ₽", amount
    assert recipient_card == "2200270011114432"
    assert fee is None
    Path(output_path).write_bytes(b"%PDF-1.4 fake-vtb-card")
    return output_path

def generate_vtb_statement(period_start, period_end, holder_name, account=None,
                           opening_balance=None, operations=None,
                           operation_count=None, receipt_paths=None,
                           fill_random=True, issue_date=None, output_path=None, **kwargs):
    assert fill_random is False
    assert operation_count == 5
    assert len(operations) == 5
    assert operations[0]["kind"] == "sbp_out"
    Path(output_path).write_bytes(b"%PDF-1.5 fake-vtb-stmt")
    return output_path
'''

BROKEN_VTB_ENGINE = '''
def generate_vtb(**kwargs):
    raise RuntimeError("vtb boom")
def generate_vtb_card(**kwargs):
    raise RuntimeError("vtb card boom")
def generate_vtb_statement(**kwargs):
    raise RuntimeError("vtb stmt boom")
'''


class VtbBotTest(unittest.TestCase):
    def test_menu_like_rocket(self):
        markup = bot._vtb_menu_markup()
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("💸 Перевод по СБП", labels)
        self.assertIn("💳 Перевод по карте", labels)
        self.assertIn("📄 Выписка", labels)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        context = fake_context()
        state = run(bot.on_vtb_menu(fake_update(callback="bank:vtb"), context))
        self.assertEqual(state, bot.V_MENU)

    def test_sbp_flow_and_fake_engine(self):
        context = fake_context()
        state = run(bot.on_vtb_sbp_start(fake_update(callback="vtb:sbp"), context))
        self.assertEqual(state, bot.V_RECIPIENT_NAME)
        state = run(bot.on_v_recipient_name(fake_update(text="Петрова Анна Ивановна"), context))
        state = run(bot.on_v_recipient_phone(fake_update(text="89632972008"), context))
        state = run(bot.on_v_bank_choice(fake_update(callback="vbank:sber"), context))
        state = run(bot.on_v_amount(fake_update(text="5000"), context))
        self.assertEqual(context.user_data["amount"], "5 000,00 ₽")
        state = run(bot.on_v_sender_name(fake_update(text="Иванов Иван Иванович"), context))
        self.assertEqual(state, bot.V_SENDER_PHONE)
        state = run(bot.on_v_sender_phone(fake_update(text="89003517080"), context))
        state = run(bot.on_v_account_choice(fake_update(callback="vacc:keep"), context))
        state = run(bot.on_v_message_choice(fake_update(callback="vmsg:none"), context))
        state = run(bot.on_v_time_choice(fake_update(callback="vtime:now"), context))
        self.assertEqual(state, bot.V_SUMMARY)
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_VTB_ENGINE, encoding="utf-8")
            os.environ["VTB_ENGINE_PATH"] = str(engine)
            bot._VTB_ENGINE_CACHE.update(path=None, module=None)
            try:
                state = run(bot.on_v_generate(fake_update(callback="vgen:generate"), context))
            finally:
                os.environ.pop("VTB_ENGINE_PATH", None)
                bot._VTB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, -1)
        self.assertTrue(context.bot.documents[0]["filename"].startswith("vtb_sbp_"))

    def test_card_shows_fee_99_and_calls_generate_vtb_card(self):
        context = fake_context()
        state = run(bot.on_vtb_card_start(fake_update(callback="vtb:card"), context))
        self.assertEqual(state, bot.V_RECIPIENT_CARD)
        state = run(bot.on_v_recipient_card(fake_update(text="2200270011114432"), context))
        state = run(bot.on_v_bank_choice(fake_update(callback="vbank:sber"), context))
        state = run(bot.on_v_amount(fake_update(text="1500"), context))
        state = run(bot.on_v_sender_name(fake_update(text="Иванов Иван Иванович"), context))
        self.assertEqual(state, bot.V_SENDER_ACCOUNT_CHOICE)
        state = run(bot.on_v_account_choice(fake_update(callback="vacc:keep"), context))
        state = run(bot.on_v_payer_card_choice(fake_update(callback="vpcard:skip"), context))
        state = run(bot.on_v_time_choice(fake_update(callback="vtime:now"), context))
        self.assertEqual(state, bot.V_SUMMARY)
        self.assertIn("99 ₽", context.bot.messages[-1])
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_VTB_ENGINE, encoding="utf-8")
            os.environ["VTB_ENGINE_PATH"] = str(engine)
            bot._VTB_ENGINE_CACHE.update(path=None, module=None)
            try:
                state = run(bot.on_v_generate(fake_update(callback="vgen:generate"), context))
            finally:
                os.environ.pop("VTB_ENGINE_PATH", None)
                bot._VTB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, -1)
        self.assertTrue(context.bot.documents)

    def test_engine_error_returns_to_summary(self):
        data = {
            "scenario": "vtb_sbp",
            "recipient_name": "Петрова Анна Ивановна",
            "recipient_phone": "+7 963 297-20-08",
            "recipient_bank": "Сбербанк",
            "amount": "5 000,00 ₽",
            "sender_name": "Иванов Иван Иванович",
            "sender_phone": "+7 900 351-70-80",
            "sender_account": None,
            "operation_dt": bot.parse_rocket_time("03.11.2026 14:20"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(BROKEN_VTB_ENGINE, encoding="utf-8")
            os.environ["VTB_ENGINE_PATH"] = str(engine)
            bot._VTB_ENGINE_CACHE.update(path=None, module=None)
            try:
                context = fake_context(data)
                state = run(bot.on_v_generate(fake_update(callback="vgen:generate"), context))
            finally:
                os.environ.pop("VTB_ENGINE_PATH", None)
                bot._VTB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, bot.V_SUMMARY)
        self.assertTrue(any("Не получилось" in m for m in context.bot.messages))
        self.assertEqual(context.bot.documents, [])

    def test_statement_layouts_and_manual_ops(self):
        self.assertEqual(bot.parse_stmt_operation_count("15", "vtb"), 15)
        for bad in ("1", "6", "10"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_stmt_operation_count(bad, "vtb")
        context = fake_context()
        state = run(bot.on_vtb_stmt_start(fake_update(callback="vtb:stmt"), context))
        self.assertEqual(state, bot.S_PERIOD)
        _text, markup = bot._stmt_prompt(context, bot.S_OP_COUNT_CHOICE)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("0", labels)
        self.assertIn("5", labels)
        self.assertIn("15", labels)
        self.assertNotIn("Другое (0–10)", " ".join(labels))
        context.user_data.update({
            "stmt_period_start": date(2026, 11, 1),
            "stmt_period_end": date(2026, 11, 7),
            "stmt_issue_date": date(2026, 11, 7),
            "stmt_holder_name": "Иванов Иван Иванович",
            "stmt_opening_balance": "16.00",
            "stmt_ops": [],
        })
        state = run(bot.on_s_source_choice(fake_update(callback="ssource:manual"), context))
        self.assertEqual(state, bot.S_OPS_EDITOR)
        state = run(bot.on_s_op_kind(fake_update(callback="sopk:sbp_out"), context))
        state = run(bot.on_s_op_amount(fake_update(text="1000"), context))
        state = run(bot.on_s_op_dt_input(fake_update(text="04.11.2026 12:40"), context))
        state = run(bot.on_s_op_detail_text(fake_update(text="Переводы через СБП."), context))
        self.assertEqual(len(context.user_data["stmt_ops"]), 1)
        state = run(bot.on_s_ops_editor_choice(fake_update(callback="sops:done"), context))
        self.assertEqual(state, bot.S_OPS_EDITOR)
        for i in range(4):
            context.user_data["stmt_ops"].append({
                "kind": "sbp_out",
                "datetime": datetime(2026, 11, 4, 12, i, tzinfo=bot.MSK),
                "amount": "10.00",
                "description": "Переводы через СБП.",
                "direction": "expense",
            })
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            engine.write_text(FAKE_VTB_ENGINE, encoding="utf-8")
            os.environ["VTB_ENGINE_PATH"] = str(engine)
            bot._VTB_ENGINE_CACHE.update(path=None, module=None)
            try:
                state = run(bot.on_s_generate(fake_update(callback="sgen:generate"), context))
            finally:
                os.environ.pop("VTB_ENGINE_PATH", None)
                bot._VTB_ENGINE_CACHE.update(path=None, module=None)
        self.assertEqual(state, -1)
        self.assertTrue(context.bot.documents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
