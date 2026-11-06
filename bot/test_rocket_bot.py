#!/usr/bin/env python3
"""Тесты бота Рокета: валидаторы, время, правка, выписка, вызовы движков.

    python3 bot/test_rocket_bot.py

Telegram и сеть не поднимаются: хендлеры гоняются на фейковых
Update/Context, а движки заменены фейковыми модулями на диске, чтобы
проверить, что бот вызывает их с нормализованными аргументами.
"""

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

import yandex_bot as bot  # noqa: E402  (тот же скрипт бота, что и у Яндекса)


# --------------------------------------------------------------------------
# Фейковый Telegram (ровно то, чем пользуются хендлеры)
# --------------------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.document = None

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


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


# --------------------------------------------------------------------------
# Валидаторы
# --------------------------------------------------------------------------


class RocketValidatorsTest(unittest.TestCase):
    def test_rocket_amount_whole_rubles_only(self):
        self.assertEqual(bot.parse_rocket_amount("10000"), "10 000 ₽")
        self.assertEqual(bot.parse_rocket_amount("1 000,00"), "1 000 ₽")
        self.assertEqual(bot.parse_rocket_amount("500"), "500 ₽")
        for bad in ("", "abc", "0", "-5", "100,50", "1 000,5"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_rocket_amount(bad)

    def test_rocket_time_full_and_time_only(self):
        now = datetime(2026, 11, 3, 9, 5, 44, tzinfo=bot.MSK)
        full = bot.parse_rocket_time("03.11.2026 16:26", now=now)
        self.assertEqual((full.day, full.hour, full.minute, full.second), (3, 16, 26, 0))
        only = bot.parse_rocket_time("16:26", now=now)
        self.assertEqual((only.date(), only.hour, only.minute, only.second),
                         (date(2026, 11, 3), 16, 26, 0))
        current = bot.rocket_current_time(now)
        self.assertEqual((current.hour, current.minute, current.second), (9, 5, 0))
        for bad in ("", "25:99", "03.11.2026", "вечером"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_rocket_time(bad, now=now)

    def test_card_full_and_mask(self):
        self.assertEqual(
            bot.parse_recipient_card("2200 1234 5678 9515"), "2200 **** **** 9515"
        )
        self.assertEqual(
            bot.parse_recipient_card("2200 **** **** 9515"), "2200 **** **** 9515"
        )
        self.assertEqual(bot.parse_recipient_card("2200123456789515"),
                         "2200 **** **** 9515")
        for bad in ("", "1234", "2200 **** 9515 1", "карта"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_recipient_card(bad)

    def test_account20_tail_and_mask(self):
        self.assertEqual(
            bot.parse_account20("40817 81045 02313 64892"), "40817810450231364892"
        )
        with self.assertRaises(ValueError):
            bot.parse_account20("12345")
        self.assertEqual(bot.parse_account_tail("12345"), "12345")
        with self.assertRaises(ValueError):
            bot.parse_account_tail("1234")
        self.assertEqual(bot.masked_account("40817810450231364892"), "…64892")
        self.assertEqual(bot.masked_account(None), "…64892")

    def test_random_account_by_formula(self):
        # Хвост донорского счёта воспроизводит сам донорский счёт.
        self.assertEqual(bot.make_rocket_account("64892"), "40817810450231364892")
        accounts = bot.random_rocket_accounts(3)
        self.assertEqual(len(set(accounts)), 3)
        for account in accounts:
            self.assertEqual(len(account), 20)
            self.assertTrue(account.startswith("40817810"), account)
            self.assertEqual(account[9:], "502313" + account[-5:], account)
            # ключ (9-я цифра) сходится с формулой
            self.assertEqual(bot.make_rocket_account(account[-5:]), account)

    def test_bank_options_labels_and_engine_values(self):
        sbp = {slug: (label, value) for slug, label, value in bot.ROCKET_SBP_BANK_OPTIONS}
        self.assertEqual(sbp["wb"][0], "Wildberries (Вайлдберриз Банк)")
        self.assertEqual(sbp["ozon"], ("Ozon Банк (Озон)", "Озон Банк"))
        for slug in ("psb", "otp", "mkb"):
            self.assertIn(slug, sbp)
        card = {slug: (label, value) for slug, label, value in bot.ROCKET_CARD_BANK_OPTIONS}
        self.assertIsNone(card["wb"][1])
        self.assertIn("Wildberries", card["wb"][0])

    def test_keyboards_two_per_row_and_copyable_examples(self):
        context = fake_context({"scenario": "rocket_sbp"})
        _text, markup = bot._rocket_prompt(context, bot.R_RECIPIENT_BANK)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        text, markup = bot._rocket_prompt(context, bot.R_AMOUNT)
        self.assertIn("<code>1000</code>", text)
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard for button in row
        ]
        self.assertFalse(any(str(cb).startswith("ramt:") for cb in callbacks))
        text, _markup = bot._rocket_prompt(context, bot.R_RECIPIENT_NAME)
        self.assertIn("<code>Иванов Иван Иванович</code>", text)
        self.assertNotIn("Подсказка", text)
        # три случайных счёта кнопками + по умолчанию/свой
        _text, markup = bot._rocket_prompt(context, bot.R_SENDER_ACCOUNT_CHOICE)
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard for button in row
        ]
        self.assertEqual(
            len([cb for cb in callbacks if str(cb).startswith("rsacc:rand")]), 3
        )
        self.assertEqual(len(context.user_data["sender_account_candidates"]), 3)

    def test_statement_validators(self):
        self.assertEqual(
            bot.parse_statement_period("16.10.2026–23.10.2026"),
            (date(2026, 10, 16), date(2026, 10, 23)),
        )
        with self.assertRaises(ValueError):
            bot.parse_statement_period("23.10.2026–16.10.2026")
        with self.assertRaises(ValueError):
            bot.parse_statement_period("октябрь")
        self.assertEqual(bot.parse_statement_date("03.11.2026"), date(2026, 11, 3))
        self.assertEqual(bot.parse_opening_balance("16,00"), "16.00")
        self.assertEqual(bot.parse_opening_balance("1 000,5"), "1000.50")
        with self.assertRaises(ValueError):
            bot.parse_opening_balance("-1")
        self.assertEqual(bot.parse_operation_count("10"), 10)
        for bad in ("-1", "11", "много"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_operation_count(bad)
        self.assertEqual(bot.format_money_display("1000.50"), "1 000,50")

    def test_statement_sources_and_upload_branch(self):
        data = {"stmt_operation_count": 6, "stmt_receipt_paths": ["a", "b"],
                "stmt_fill_random": True}
        self.assertIn("2 из загруженных чеков + 4 случайно", bot.statement_sources_text(data))
        self.assertEqual(bot.statement_sources_text(
            {"stmt_operation_count": 0, "stmt_receipt_paths": []}), "0 платежей")
        self.assertEqual(bot.statement_after_upload_action(2, 6), "shortage")
        self.assertEqual(bot.statement_after_upload_action(6, 6), "summary")
        self.assertEqual(bot.statement_after_upload_action(0, 0), "summary")

    def test_summaries_show_only_last5_of_account(self):
        data = {
            "scenario": "rocket_sbp", "operation_dt": datetime(2026, 11, 3, 16, 26, tzinfo=bot.MSK),
            "recipient_name": "Иванов Иван Иванович", "recipient_phone": "+7 900 351-70-80",
            "recipient_bank_label": "Сбербанк", "amount": "1 000 ₽",
            "sender_name": "Петров Пётр Петрович", "sender_phone": "+7 900 351-70-80",
            "sender_account": "40817810450231312345",
        }
        text = bot._rocket_summary_text(data)
        self.assertIn("…12345", text)
        self.assertNotIn("40817810450231312345", text)
        self.assertIn("ПОЛУЧАТЕЛЬ", text)
        self.assertIn("ОТПРАВИТЕЛЬ", text)
        self.assertIn("03.11.2026 16:26", text)


# --------------------------------------------------------------------------
# Хендлеры на фейковом Telegram: время, память, правка отправителя
# --------------------------------------------------------------------------


def sbp_data(**overrides):
    data = {
        "scenario": "rocket_sbp",
        "current_step": bot.R_SUMMARY,
        "recipient_name": "Иванов Иван Иванович",
        "recipient_phone": "+7 900 351-70-80",
        "recipient_bank": "Сбербанк",
        "recipient_bank_label": "Сбербанк",
        "amount": "1 000 ₽",
        "sender_name": "Жеребятьев Александр Евгеньевич",
        "sender_phone": "+7 963 297-20-08",
        "sender_account": None,
        "operation_dt": datetime(2026, 11, 3, 12, 0, tzinfo=bot.MSK),
    }
    data.update(overrides)
    return data


class RocketHandlersTest(unittest.TestCase):
    def test_time_step_now_and_change(self):
        context = fake_context(sbp_data(current_step=bot.R_TIME_CHOICE))
        state = run(bot.on_r_time_choice(fake_update(callback="rtime:now"), context))
        self.assertEqual(state, bot.R_SUMMARY)
        self.assertEqual(context.user_data["operation_dt"].second, 0)

        context = fake_context(sbp_data(current_step=bot.R_TIME_CHOICE))
        state = run(bot.on_r_time_choice(fake_update(callback="rtime:change"), context))
        self.assertEqual(state, bot.R_TIME_INPUT)
        state = run(bot.on_r_time_input(fake_update(text="16:26"), context))
        self.assertEqual(state, bot.R_SUMMARY)
        moment = context.user_data["operation_dt"]
        self.assertEqual((moment.hour, moment.minute, moment.second), (16, 26, 0))

        bad = fake_update(text="когда-нибудь")
        state = run(bot.on_r_time_input(bad, context))
        self.assertEqual(state, bot.R_TIME_INPUT)
        self.assertTrue(any("16:26" in m for m in context.bot.messages))

    def test_edit_sender_from_summary_returns_to_summary(self):
        context = fake_context(sbp_data())
        state = run(bot.on_r_summary_edit(fake_update(callback="rgen:edit"), context))
        self.assertEqual(state, bot.R_EDIT_SELECT)
        state = run(bot.on_r_edit_field(fake_update(callback="redit:sender_name"), context))
        self.assertEqual(state, bot.R_SENDER_NAME)
        self.assertTrue(context.user_data["editing"])
        state = run(bot.on_r_sender_name(fake_update(text="Петров Пётр Петрович"), context))
        self.assertEqual(state, bot.R_SUMMARY)
        self.assertEqual(context.user_data["sender_name"], "Петров Пётр Петрович")
        self.assertIn("Петров Пётр Петрович", context.bot.messages[-1])

    def test_edit_sender_account_from_summary(self):
        context = fake_context(sbp_data())
        run(bot.on_r_edit_field(fake_update(callback="redit:sender_account"), context))
        state = run(bot.on_r_sender_account_choice(
            fake_update(callback="rsacc:custom"), context))
        self.assertEqual(state, bot.R_SENDER_ACCOUNT_INPUT)
        state = run(bot.on_r_sender_account_input(
            fake_update(text="40817 81045 02313 99999"), context))
        self.assertEqual(state, bot.R_SUMMARY)
        self.assertEqual(context.user_data["sender_account"], "40817810450231399999")
        self.assertIn("…99999", context.bot.messages[-1])
        self.assertNotIn("40817810450231399999", context.bot.messages[-1])

    def test_choose_random_account_candidate(self):
        candidates = bot.random_rocket_accounts(3)
        context = fake_context(sbp_data(
            current_step=bot.R_SENDER_ACCOUNT_CHOICE,
            sender_account_candidates=candidates,
        ))
        state = run(bot.on_r_sender_account_choice(
            fake_update(callback="rsacc:rand1"), context))
        self.assertEqual(state, bot.R_TIME_CHOICE)
        self.assertEqual(context.user_data["sender_account"], candidates[1])

    def test_back_from_recipient_phone_returns_to_name(self):
        context = fake_context(sbp_data(current_step=bot.R_RECIPIENT_PHONE))
        state = run(bot.on_nav_back(fake_update(callback="nav:back"), context))
        self.assertEqual(state, bot.R_RECIPIENT_NAME)

    def test_card_wb_bank_button_and_custom_mean_no_bank_block(self):
        context = fake_context({"scenario": "rocket_card", "current_step": bot.R_RECIPIENT_BANK})
        state = run(bot.on_r_bank_choice(fake_update(callback="rbank:wb"), context))
        self.assertEqual(state, bot.R_AMOUNT)
        self.assertIsNone(context.user_data["recipient_bank"])
        self.assertIn("без банка", context.user_data["recipient_bank_label"])
        context = fake_context({"scenario": "rocket_card", "current_step": bot.R_RECIPIENT_BANK_CUSTOM})
        state = run(bot.on_r_bank_custom(fake_update(text="ВБ Банк"), context))
        self.assertEqual(state, bot.R_AMOUNT)
        self.assertIsNone(context.user_data["recipient_bank"])

    def test_memory_keeps_sender_and_recipient_after_clear(self):
        context = fake_context(sbp_data(sender_account="40817810450231399999"))
        bot._save_rocket_memory(context)
        bot._clear_draft(context)
        self.assertEqual(
            context.user_data["mem_rocket_sender"]["sender_name"],
            "Жеребятьев Александр Евгеньевич",
        )
        self.assertEqual(
            context.user_data["mem_rocket_recipient_sbp"]["recipient_bank"], "Сбербанк"
        )
        # Кнопка «оставить отправителя» применяет память и ведёт ко времени.
        context.user_data.update(sbp_data(scenario="rocket_sbp", sender_name=None))
        context.user_data.pop("sender_name")
        state = run(bot.on_r_mem_sender(fake_update(callback="rmem:sender"), context))
        self.assertEqual(state, bot.R_TIME_CHOICE)
        self.assertEqual(context.user_data["sender_account"], "40817810450231399999")


# --------------------------------------------------------------------------
# Выписка: подсчёт чеков и ветка «дополнить случайно»
# --------------------------------------------------------------------------


def stmt_data(**overrides):
    data = {
        "scenario": "rocket_statement",
        "current_step": bot.S_UPLOAD,
        "stmt_period_start": date(2026, 10, 16),
        "stmt_period_end": date(2026, 10, 23),
        "stmt_issue_date": date(2026, 11, 3),
        "stmt_holder_name": "Петров Иван Сергеевич",
        "stmt_phone": "+7 900 351-70-80",
        "stmt_account_mode": "keep",
        "stmt_opening_balance": "16.00",
        "stmt_operation_count": 6,
        "stmt_receipt_paths": ["/tmp/a.pdf", "/tmp/b.pdf"],
        "stmt_receipt_dates": ["2026-10-20", "2026-10-20"],
        "stmt_fill_random": False,
        "stmt_operation_date": None,
    }
    data.update(overrides)
    return data


class StatementHandlersTest(unittest.TestCase):
    def test_uploads_done_with_fewer_receipts_offers_shortage(self):
        context = fake_context(stmt_data())
        state = run(bot.on_s_uploads_done(fake_update(callback="stmt:uploads_done"), context))
        self.assertEqual(state, bot.S_SHORTAGE)
        self.assertIn("Загружено чеков: <b>2</b>", context.bot.messages[-1])

    def test_shortage_fill_random_uses_receipt_day(self):
        context = fake_context(stmt_data(current_step=bot.S_SHORTAGE))
        state = run(bot.on_s_shortage_fill(fake_update(callback="stmt:fill_random"), context))
        self.assertEqual(state, bot.S_OP_DAY_CHOICE)
        self.assertTrue(context.user_data["stmt_fill_random"])
        state = run(bot.on_s_day_receipt(fake_update(callback="stmt:day_receipt"), context))
        self.assertEqual(state, bot.S_SUMMARY)
        self.assertEqual(context.user_data["stmt_operation_date"], date(2026, 10, 20))
        self.assertIn("2 из загруженных чеков + 4 случайно", context.bot.messages[-1])

    def test_shortage_reduce_count_to_receipts(self):
        context = fake_context(stmt_data(current_step=bot.S_SHORTAGE))
        state = run(bot.on_s_shortage_reduce(fake_update(callback="stmt:reduce_count"), context))
        self.assertEqual(state, bot.S_SUMMARY)
        self.assertEqual(context.user_data["stmt_operation_count"], 2)
        self.assertFalse(context.user_data["stmt_fill_random"])

    def test_op_date_outside_period_does_not_advance(self):
        context = fake_context(stmt_data(stmt_operation_count=3, stmt_receipt_paths=[],
                                         stmt_receipt_dates=[], stmt_fill_random=True))
        update = fake_update(text="01.11.2026")
        state = run(bot.on_s_op_date_input(update, context))
        self.assertEqual(state, bot.S_OP_DATE_INPUT)
        self.assertTrue(any("вне периода" in m for m in context.bot.messages))


# --------------------------------------------------------------------------
# Вызовы фейковых движков с нормализованными аргументами
# --------------------------------------------------------------------------

FAKE_ROCKET_ENGINE = '''
from pathlib import Path

def generate_rocket(amount, operation_dt, sender_name, sender_phone,
                    recipient_name, recipient_phone, recipient_bank,
                    sender_account, output_path):
    assert amount == "10 000 ₽", amount
    assert operation_dt.second == 0 and operation_dt.tzinfo is not None
    assert sender_phone == "+7 900 351-70-80", sender_phone
    assert recipient_phone == "+7 963 297-20-08", recipient_phone
    assert recipient_bank == "Сбербанк", recipient_bank
    assert sender_account == "40817810450231399999", sender_account
    Path(output_path).write_bytes(b"%PDF-1.7 fake-rocket-sbp")
    return output_path

def generate_rocket_card(amount, operation_dt, sender_name, recipient_card,
                         recipient_bank, sender_account, output_path):
    assert amount == "3 750 ₽", amount
    assert recipient_card == "2200 **** **** 9515", recipient_card
    assert recipient_bank is None, recipient_bank  # ВБ Банк -> без блока
    assert sender_account is None, sender_account
    Path(output_path).write_bytes(b"%PDF-1.7 fake-rocket-card")
    return output_path
'''

FAKE_STATEMENT_ENGINE = '''
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

def operation_from_receipt(path, **kwargs):
    return {"kind": "sbp_out",
            "datetime": datetime(2026, 10, 20, 12, 30, tzinfo=ZoneInfo("Europe/Moscow"))}

def generate_rocket_statement(period_start, period_end, issue_date, holder_name,
                              account, account_tail, random_account_tail,
                              opening_balance, operation_count, operation_date,
                              receipt_paths, phone, fill_random, output_path):
    assert str(period_start) == "2026-10-16", period_start
    assert str(period_end) == "2026-10-23", period_end
    assert str(issue_date) == "2026-11-03", issue_date
    assert holder_name == "Петров Иван Сергеевич", holder_name
    assert account is None and account_tail == "12345", (account, account_tail)
    assert random_account_tail is False
    assert opening_balance == "16.00", opening_balance
    assert operation_count == 6, operation_count
    assert str(operation_date) == "2026-10-20", operation_date
    assert receipt_paths == ["/tmp/a.pdf"], receipt_paths
    assert phone == "+7 900 351-70-80", phone
    assert fill_random is True
    Path(output_path).write_bytes(b"%PDF-1.7 fake-rocket-statement")
    return output_path
'''

BROKEN_ROCKET_ENGINE = '''
def generate_rocket(**kwargs):
    raise RuntimeError("no donor glyph Ю")

def generate_rocket_card(**kwargs):
    raise RuntimeError("card engine boom")
'''


class RocketEngineCallTest(unittest.TestCase):
    def _write_engine(self, tmp, name, code):
        path = Path(tmp) / name
        path.write_text(code, encoding="utf-8")
        return path

    def test_sbp_engine_called_with_normalized_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._write_engine(tmp, "rocket_engine.py", FAKE_ROCKET_ENGINE)
            os.environ["ROCKET_ENGINE_PATH"] = str(engine)
            try:
                data = sbp_data(
                    amount=bot.parse_rocket_amount("10000"),
                    sender_phone=bot.parse_phone("8 900 351-70-80"),
                    recipient_phone=bot.parse_phone("89632972008"),
                    sender_account=bot.parse_account20("40817 81045 02313 99999"),
                    operation_dt=bot.parse_rocket_time("03.11.2026 16:26"),
                )
                data["amount"] = "10 000 ₽"
                out = Path(tmp) / "out.pdf"
                pdf = run(bot.generate_rocket_pdf(data, out))
                self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
            finally:
                os.environ.pop("ROCKET_ENGINE_PATH", None)

    def test_card_engine_called_with_normalized_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._write_engine(tmp, "rocket_engine.py", FAKE_ROCKET_ENGINE)
            os.environ["ROCKET_ENGINE_PATH"] = str(engine)
            try:
                data = {
                    "scenario": "rocket_card",
                    "amount": bot.parse_rocket_amount("3750"),
                    "operation_dt": bot.parse_rocket_time("03.11.2026 15:02"),
                    "sender_name": "Жеребятьев Александр Евгеньевич",
                    "recipient_card": bot.parse_recipient_card("2200 1234 5678 9515"),
                    "recipient_bank": None,  # ВБ Банк (в чеке без банка)
                    "sender_account": None,
                }
                out = Path(tmp) / "out.pdf"
                pdf = run(bot.generate_rocket_card_pdf(data, out))
                self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
                self.assertEqual(
                    bot.rocket_receipt_filename(data), "receipt_rocket.pdf"
                )
            finally:
                os.environ.pop("ROCKET_ENGINE_PATH", None)

    def test_statement_engine_and_receipt_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._write_engine(tmp, "rocket_statement.py", FAKE_STATEMENT_ENGINE)
            os.environ["ROCKET_STATEMENT_PATH"] = str(engine)
            try:
                operation = bot.import_receipt_operation("/tmp/whatever.pdf")
                self.assertEqual(bot.receipt_operation_date(operation), date(2026, 10, 20))
                data = stmt_data(
                    stmt_account_mode="tail", stmt_account_tail="12345",
                    stmt_receipt_paths=["/tmp/a.pdf"], stmt_fill_random=True,
                    stmt_operation_date=date(2026, 10, 20),
                )
                out = Path(tmp) / "out.pdf"
                pdf = run(bot.generate_rocket_statement_pdf(data, out))
                self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
                self.assertEqual(bot.rocket_statement_filename(data), "Документ.pdf")
            finally:
                os.environ.pop("ROCKET_STATEMENT_PATH", None)

    def test_broken_rocket_engine_raises_for_handler_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._write_engine(tmp, "rocket_engine.py", BROKEN_ROCKET_ENGINE)
            os.environ["ROCKET_ENGINE_PATH"] = str(engine)
            try:
                with self.assertRaisesRegex(RuntimeError, "no donor glyph"):
                    run(bot.generate_rocket_pdf(sbp_data(), Path(tmp) / "out.pdf"))
            finally:
                os.environ.pop("ROCKET_ENGINE_PATH", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
