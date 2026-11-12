# -*- coding: utf-8 -*-
"""Smoke-тесты перестроенного меню mts_bot.py (unittest, без сети/Telegram).

Запуск: py -m unittest test_mts_bot_menu -v
Проверяет только построение клавиатур и сохранность существующих
callback/flow/step (по исходнику) — генераторы и Telegram не дёргаются.
"""
import pathlib
import unittest

import mts_bot as bot

SRC = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
ADMIN = bot.ADMIN_ID
USER = 123456001
USER_GRANTED = 123456002

BANK_KEYS = ["mts", "wb", "vtb", "alfa", "tbank", "rocket", "rshb", "ozon", "yandex"]
FLOW_CALLBACKS = {
    "new", "new_card", "new_stmt", "new_wb", "new_vtb", "new_rshb",
    "new_ozon", "ozon_edit", "new_yandex", "new_yrcpt",
}
OTHER_CALLBACKS = {
    "check_pdf", "history", "templates", "help", "fast_gen", "batch",
    "stats_btn", "api_token", "dark_theme", "notifications", "lk_bind",
}


def flat(markup):
    return [(b.text, b.callback_data)
            for row in markup.inline_keyboard for b in row]


def cbs(markup):
    return {d for _t, d in flat(markup)}


class MenuSmoke(unittest.TestCase):
    def setUp(self):
        self._flags = dict(bot.FEATURE_FLAGS)
        self._users = list(bot.data["users"])
        for uid in (USER, USER_GRANTED):
            if uid not in bot.data["users"]:
                bot.data["users"].append(uid)
        ff = bot.data.setdefault("feature_flags", {})
        ff[str(USER)] = {}
        ff[str(USER_GRANTED)] = {name: True for name in bot.PER_USER_FEATURES}
        bot.data.setdefault("lk_bindings", {})

    def tearDown(self):
        bot.FEATURE_FLAGS.clear()
        bot.FEATURE_FLAGS.update(self._flags)
        bot.data["users"] = self._users

    # ── корень ──
    def test_root_admin_only_banks_other_admin(self):
        got = cbs(bot.main_menu(ADMIN))
        expect_banks = {f"bankmenu:{k}" for k in BANK_KEYS}
        self.assertTrue(expect_banks <= got)
        self.assertIn("menu_other", got)
        self.assertIn("admin", got)
        # в корне нет ни flow-кнопок, ни небанковских кнопок «Другого»
        self.assertFalse(got & FLOW_CALLBACKS, got & FLOW_CALLBACKS)
        self.assertFalse(got & OTHER_CALLBACKS, got & OTHER_CALLBACKS)

    def test_root_nonadmin_no_admin(self):
        got = cbs(bot.main_menu(USER))
        self.assertEqual({f"bankmenu:{k}" for k in BANK_KEYS} | {"menu_other", "noop"},
                         got)
        self.assertNotIn("admin", got)
        self.assertFalse(got & FLOW_CALLBACKS)
        self.assertFalse(got & OTHER_CALLBACKS)

    def test_root_plain_menu_rich_off(self):
        bot.FEATURE_FLAGS["rich_menu"] = False
        got = cbs(bot.main_menu(ADMIN))
        self.assertNotIn("noop", got)  # без заголовков секций
        self.assertIn("admin", got)
        self.assertIn("menu_other", got)
        self.assertTrue({f"bankmenu:{k}" for k in BANK_KEYS} <= got)
        self.assertFalse(got & FLOW_CALLBACKS)

    # ── подменю банков ──
    def test_bank_menus(self):
        expect = {
            "mts": {"new", "new_card", "new_stmt", "home"},
            "wb": {"new_wb", "home"},
            # merge чекетоп: ВТБ СБП теперь на движке чекетоп (ck_vtb_sbp),
            # карточный ВТБ — прежний (new_vtbcard)
            "vtb": {"ck_vtb_sbp", "new_vtbcard", "home"},
            "alfa": {"ck_alfa_sbp", "home"},
            "tbank": {"ck_tbank_sbp", "home"},
            "rocket": {"rocket:sbp", "rocket:card", "rocket:stmt", "home", "cancel"},
            "rshb": {"new_rshb", "home"},
            # без флага ozon_v2 — старый flow (rollback)
            "ozon": {"new_ozon", "ozon_edit", "home"},
            # выписка + отдельная кнопка «Квитанция СБП»
            "yandex": {"new_yandex", "new_yrcpt", "home"},
        }
        for key, want in expect.items():
            with self.subTest(bank=key):
                self.assertEqual(want, cbs(bot.bank_menu(key, USER)))
                if key != "ozon":  # у админа Озон — v2, см. ниже
                    self.assertEqual(want, cbs(bot.bank_menu(key, ADMIN)))

    def test_ozon_menu_v2_for_flagged_users(self):
        # ozon_v2: сценарий ozon_ux (новый чек / правка / история)
        want = {"oz:new", "oz:edit", "oz:hist", "cancel", "home"}
        self.assertEqual(want, cbs(bot.bank_menu("ozon", ADMIN)))
        self.assertEqual(want, cbs(bot.bank_menu("ozon", USER_GRANTED)))
        # без флага — байт-в-байт старый набор кнопок
        self.assertEqual({"new_ozon", "ozon_edit", "home"},
                         cbs(bot.bank_menu("ozon", USER)))

    # ── «Другое» ──
    def test_other_admin_has_all_features(self):
        got = cbs(bot.other_menu(ADMIN))
        self.assertTrue((OTHER_CALLBACKS - {"lk_bind"}) <= got, got)
        self.assertIn("home", got)
        self.assertNotIn("lk_bind", got)   # у админа в «Другом» нет привязки ЛК
        self.assertNotIn("admin", got)     # «Управление» — только в корне
        self.assertFalse(got & FLOW_CALLBACKS)

    def test_other_nonadmin_without_grants(self):
        got = cbs(bot.other_menu(USER))
        # глобальные удобства видны, per-user фичи — нет
        self.assertTrue({"history", "templates", "help", "lk_bind", "home"} <= got)
        self.assertFalse(got & {"check_pdf", "fast_gen", "batch", "stats_btn",
                                "api_token", "dark_theme", "notifications"})

    def test_other_nonadmin_with_grants(self):
        got = cbs(bot.other_menu(USER_GRANTED))
        self.assertTrue(OTHER_CALLBACKS <= got, got)
        self.assertIn("home", got)

    def test_feature_flags_off(self):
        bot.FEATURE_FLAGS["history_btn"] = False
        bot.FEATURE_FLAGS["templates"] = False
        bot.FEATURE_FLAGS["help_cmd"] = False
        bot.FEATURE_FLAGS["fast_gen"] = False  # мастер-флаг режет даже админа
        got = cbs(bot.other_menu(ADMIN))
        self.assertFalse(got & {"history", "templates", "help", "fast_gen"})
        self.assertIn("check_pdf", got)  # остальные фичи не тронуты

    # ── сохранность существующих callback/flow/step и фикса Озона ──
    def test_existing_callbacks_preserved(self):
        for cb in sorted(FLOW_CALLBACKS | OTHER_CALLBACKS |
                         {"admin", "home", "cancel", "noop", "tpl_save",
                          "api_token_new", "users_list"}):
            with self.subTest(callback=cb):
                self.assertIn(f'F.data == "{cb}"', SRC)
        self.assertIn('F.data.startswith("bankmenu:")', SRC)
        self.assertIn('F.data == "menu_other"', SRC)
        self.assertIn('F.data.startswith("bank:")', SRC)  # СБП-выбор банка цел

    def test_existing_steps_preserved(self):
        for step in ("name", "wb_name", "vtb_name", "rshb_name", "ozon_name",
                     "ozon_edit_pdf", "yandex_name", "st_name", "card_form",
                     "check_pdf_wait", "batch_count",
                     # новые flow: ozon v2 и квитанция Яндекс
                     "ozon_flow", "ozon_edit_flow", "ozon_edit_v2_amount",
                     "yrcpt_sender", "yrcpt_sender_phone", "yrcpt_name",
                     "yrcpt_phone", "yrcpt_bank", "yrcpt_amount",
                     "yrcpt_message", "yrcpt_preview"):
            with self.subTest(step=step):
                self.assertIn(f'"{step}"', SRC)

    def test_new_flow_callbacks_wired(self):
        self.assertIn('F.data == "new_yrcpt"', SRC)
        self.assertIn('F.data == "yrcpt_go"', SRC)
        self.assertIn('F.data == "yrcpt_msg_skip"', SRC)
        self.assertIn('F.data.startswith("yrcpt_bank:")', SRC)
        self.assertIn('F.data.startswith("oz:")', SRC)
        self.assertIn('"ozon_v2"', SRC)
        # квитанция не смешана с выпиской: отдельный генератор/loader
        self.assertIn("yandex_receipt_generator", SRC)
        self.assertIn("def yrcptgen", SRC)

    def test_ozon_cart_emoji_fixed(self):
        self.assertNotIn("f6d2", SRC)
        self.assertNotIn("\U0001f6d2", SRC)  # 🛒
        self.assertIn("Озон", SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
