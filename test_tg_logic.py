from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from carrier_profile import ACTIVE_CARRIER
from tg_bot import (
    MAX_PENDING_MESSAGES,
    TELEGRAM_COMMANDS,
    TelegramBot,
    TelegramRequestError,
)


class FakeResponse:
    def __init__(self, result: Any = True, status_code: int = 200) -> None:
        self.status_code = status_code
        self._result = result

    def json(self) -> dict[str, Any]:
        return {"ok": True, "result": self._result}


class FakeSession:
    def __init__(self) -> None:
        self.proxies: dict[str, str] = {}
        self.trust_env = True
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.failure: Exception | None = None

    def post(self, url: str, *, json: dict[str, Any], timeout: Any) -> FakeResponse:
        self.calls.append((url.rsplit("/", 1)[-1], dict(json), timeout))
        if self.failure:
            raise self.failure
        if url.endswith("/getUpdates"):
            return FakeResponse([])
        return FakeResponse(True)

    def close(self) -> None:
        pass


class TelegramBotLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_path = self.base / "config.json"
        self.state_path = self.base / "tg_state.json"
        self.archive_path = self.base / "archive.jsonl"
        self.log_path = self.base / "app.log"
        self.config_path.write_text(
            json.dumps(
                {
                    "telegram": {
                        "token": "测试token",
                        "chat_id": None,
                        "proxy": "http://127.0.0.1:7897",
                    },
                    "alerts": {"low_balance_gbp": 2.0, "keepalive_warn_days": 14},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.archive_path.write_text(
            json.dumps(
                {
                    "id": "历史短信",
                    "sender": "CTExcel",
                    "time": "2026-07-18T08:00:00+01:00",
                    "body": "旧短信",
                    "direction": "in",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.session = FakeSession()
        self.bots: list[TelegramBot] = []
        self.sent_sms: list[tuple[str, str]] = []
        self.status = {
            "connected": True,
            "port": "COM4",
            "signal": 20,
            "registered": True,
            "roaming": True,
            "operator": "中国移动",
            "storage_used": 1,
            "storage_total": 10,
            "balance": {"amount": 14.1, "time": "2026-07-18T08:00:00+01:00"},
            "keepalive_days_left": 178,
            "wwan": {"state": "disabled", "checked_at": None},
        }
        self.balance = 14.1

    def tearDown(self) -> None:
        for bot in self.bots:
            for handler in list(bot.logger.handlers):
                bot.logger.removeHandler(handler)
                handler.close()
        self.temp_dir.cleanup()

    def make_bot(
        self,
        *,
        message_provider: Any | None = None,
    ) -> TelegramBot:
        def send_sms(recipient: str, text: str) -> dict[str, Any]:
            self.sent_sms.append((recipient, text))
            return {"ok": True}

        def set_balance(amount: float) -> None:
            if not 0 <= amount <= 1000:
                raise ValueError("余额必须是 0 到 1000 之间的数字")
            self.balance = amount

        bot = TelegramBot(
            config_path=self.config_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            log_path=self.log_path,
            send_sms=send_sms,
            status_provider=lambda: dict(self.status),
            balance_setter=set_balance,
            message_provider=message_provider,
            session=self.session,
        )
        self.bots.append(bot)
        return bot

    @staticmethod
    def private_message(update_id: int, user_id: int, text: str) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False},
                "text": text,
            },
        }

    def test_first_private_message_binds_owner_and_ignores_others(self) -> None:
        bot = self.make_bot()
        bot._handle_update(self.private_message(1, 12345, "/status"))
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["telegram"]["chat_id"], 12345)
        queued_after_owner = len(bot._outbox)
        bot._handle_update(self.private_message(2, 99999, "/help"))
        self.assertEqual(len(bot._outbox), queued_after_owner)

    def test_archive_id_is_persisted_only_after_successful_push(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        bot.notify_incoming_sms(
            {
                "id": "新短信",
                "sender": "Telegram",
                "time": "2026-07-20T06:39:20+01:00",
                "body": "Telegram code: 24227",
                "code": "24227",
                "direction": "in",
            }
        )
        self.assertNotIn("新短信", bot._pushed_archive_ids)
        bot._flush_outbox()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("新短信", state["pushed_message_ids"])
        sent_payload = next(call[1] for call in self.session.calls if call[0] == "sendMessage")
        self.assertIn("<code>24227</code>", sent_payload["text"])

    def test_multipart_sms_is_pushed_once_after_logical_assembly(self) -> None:
        logical_messages: list[dict[str, Any]] = []
        bot = self.make_bot(message_provider=lambda: list(logical_messages))
        bot.chat_id = 12345
        source_ids = ["part-4", "part-3", "part-2", "part-1"]
        for source_id in source_ids:
            bot.notify_incoming_sms(
                {
                    "id": source_id,
                    "sender": "CTExcel",
                    "time": "2026-07-24T16:33:50+01:00",
                    "body": "一个物理分段",
                    "direction": "in",
                }
            )
        self.assertEqual(len(bot._outbox), 0)

        logical_messages.append(
            {
                "id": "logical-message",
                "sender": "CTExcel",
                "time": "2026-07-24T16:33:50+01:00",
                "body": "Hey, complete multipart message. Cheers.",
                "code": None,
                "direction": "in",
                "_source_ids": list(reversed(source_ids)),
            }
        )
        bot._queue_due_incoming(force=True)

        self.assertEqual(len(bot._outbox), 1)
        self.assertIn("Hey, complete multipart message. Cheers.", bot._outbox[0].payload["text"])
        bot._flush_outbox()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("logical-message", state["pushed_message_ids"])
        for source_id in source_ids:
            self.assertIn(source_id, state["pushed_message_ids"])

    def test_existing_pushed_parts_migrate_without_resending_logical_sms(self) -> None:
        source_ids = ["old-part-1", "old-part-2", "old-part-3"]
        self.state_path.write_text(
            json.dumps(
                {
                    "pushed_message_ids": source_ids,
                    "last_update_id": 10,
                    "alerts": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logical = {
            "id": "old-logical-message",
            "sender": "CTExcel",
            "time": "2026-07-22T14:27:37+01:00",
            "body": "完整的旧长短信",
            "direction": "in",
            "_source_ids": source_ids,
        }

        bot = self.make_bot(message_provider=lambda: [logical])

        self.assertEqual(len(bot._outbox), 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("old-logical-message", state["pushed_message_ids"])

    def test_network_failure_keeps_message_in_memory_queue(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        bot._queue_text("待补发")
        self.session.failure = OSError("代理未启动")
        with self.assertRaises(TelegramRequestError):
            bot._flush_outbox()
        self.assertEqual(len(bot._outbox), 1)

    def test_pending_queue_drops_oldest_at_fixed_limit(self) -> None:
        bot = self.make_bot()
        for index in range(MAX_PENDING_MESSAGES + 1):
            bot._queue_text(f"消息 {index}")
        self.assertEqual(len(bot._outbox), MAX_PENDING_MESSAGES)
        self.assertEqual(bot._outbox[0].payload["text"], "消息 1")

    def test_send_requires_inline_confirmation(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        bot._handle_update(self.private_message(1, 12345, "/send +8613800000000 中文测试"))
        self.assertEqual(self.sent_sms, [])
        preview = bot._outbox[-1]
        callback_data = preview.payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        callback = {
            "id": "callback-1",
            "from": {"id": 12345, "is_bot": False},
            "message": {
                "message_id": 88,
                "chat": {"id": 12345, "type": "private"},
            },
            "data": callback_data,
        }
        bot._handle_callback(callback)
        self.assertEqual(self.sent_sms, [("+8613800000000", "中文测试")])

    def test_query_balance_and_hidden_test_alias_require_confirmation(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        cases = [
            ("/test", ACTIVE_CARRIER.balance_query),
            ("/querybalance", ACTIVE_CARRIER.balance_query),
        ]
        for index, (command, action) in enumerate(cases, start=1):
            bot._handle_update(self.private_message(index, 12345, command))
            self.assertEqual(self.sent_sms, [])
            preview = bot._outbox[-1]
            self.assertIn(action.title, preview.payload["text"])
            self.assertIn(action.preview_text, preview.payload["text"])
            callback_data = preview.payload["reply_markup"]["inline_keyboard"][0][0][
                "callback_data"
            ]
            bot._handle_callback(
                {
                    "id": f"service-callback-{index}",
                    "from": {"id": 12345, "is_bot": False},
                    "message": {
                        "message_id": 100 + index,
                        "chat": {"id": 12345, "type": "private"},
                    },
                    "data": callback_data,
                }
            )
            self.assertEqual(self.sent_sms[-1], (action.recipient, action.text))
            self.sent_sms.clear()

    def test_help_lists_clear_balance_commands_without_test_alias(self) -> None:
        bot = self.make_bot()
        help_text = bot._help_text()
        self.assertNotIn("/test", help_text)
        self.assertIn("/querybalance", help_text)
        self.assertIn("/balance", help_text)
        self.assertIn("/setbalance", help_text)
        self.assertIn("CTExcel", help_text)
        self.assertIn("DJI QDC507", help_text)

    def test_command_menu_registers_all_commands_once(self) -> None:
        bot = self.make_bot()
        bot._ensure_commands_registered()
        bot._ensure_commands_registered()

        menu_calls = [call for call in self.session.calls if call[0] == "setMyCommands"]
        self.assertEqual(len(menu_calls), 1)
        commands = menu_calls[0][1]["commands"]
        self.assertEqual(commands, [dict(item) for item in TELEGRAM_COMMANDS])
        self.assertEqual(
            [item["command"] for item in commands],
            [
                "status",
                "balance",
                "querybalance",
                "history",
                "send",
                "help",
            ],
        )

    def test_balance_and_history_commands_use_injected_storage_logic(self) -> None:
        with self.archive_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": "发件记录",
                        "sender": "发往 +8613800000000",
                        "time": "2026-07-20T14:00:00+08:00",
                        "body": "测试发送",
                        "direction": "out",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        bot = self.make_bot()
        bot.chat_id = 12345
        bot._handle_update(self.private_message(1, 12345, "/balance"))
        self.assertIn("当前余额", bot._outbox[-1].payload["text"])
        self.assertEqual(self.balance, 14.1)
        bot._handle_update(self.private_message(2, 12345, "/balance 14.40"))
        self.assertIn("/setbalance 14.40", bot._outbox[-1].payload["text"])
        self.assertEqual(self.balance, 14.1)
        bot._handle_update(self.private_message(3, 12345, "/setbalance 14.40"))
        self.assertEqual(self.balance, 14.4)
        bot._handle_update(self.private_message(4, 12345, "/history 2"))
        history = bot._outbox[-1].payload["text"]
        self.assertIn("[发送] 发往 +8613800000000", history)
        self.assertIn("[接收] CTExcel", history)

    def test_missing_token_disables_bot_without_network_calls(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["telegram"]["token"] = ""
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        bot = self.make_bot()
        self.assertFalse(bot.enabled)
        bot.notify_incoming_sms(
            {"id": "不会推送", "direction": "in", "body": "测试"}
        )
        self.assertEqual(len(bot._outbox), 0)
        self.assertEqual(self.session.calls, [])

    def test_explicit_enable_supports_direct_connection_and_disable_wins(self) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["telegram"]["enabled"] = True
        config["telegram"]["proxy"] = ""
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        direct_bot = self.make_bot()
        self.assertTrue(direct_bot.enabled)
        self.assertEqual(self.session.proxies, {})

        config["telegram"]["enabled"] = False
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        disabled_bot = self.make_bot()
        self.assertFalse(disabled_bot.enabled)

    def test_alerts_are_transition_and_daily_limited(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        bot._check_alerts()
        bot._outbox.clear()
        self.status["wwan"] = {"state": "enabled", "checked_at": None}
        self.status["balance"] = {"amount": 1.5, "time": "2026-07-20T10:00:00+08:00"}
        self.status["keepalive_days_left"] = 14
        bot._check_alerts()
        first_count = len(bot._outbox)
        self.assertEqual(first_count, 3)
        bot._check_alerts()
        self.assertEqual(len(bot._outbox), first_count)

    def test_storage_alerts_warn_escalate_and_recover_without_duplicates(self) -> None:
        bot = self.make_bot()
        bot.chat_id = 12345
        self.status["storage_used"] = 0
        self.status["storage_total"] = 23

        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 0)
        self.assertEqual(bot._state["alerts"]["last_storage_level"], "normal")

        self.status["storage_used"] = 18
        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 1)
        self.assertIn("预警", bot._outbox[-1].payload["text"])
        self.assertIn("18/23", bot._outbox[-1].payload["text"])

        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 1)

        self.status["storage_used"] = 21
        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 2)
        self.assertIn("紧急告警", bot._outbox[-1].payload["text"])
        self.assertIn("21/23", bot._outbox[-1].payload["text"])

        self.status["storage_used"] = 20
        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 2)
        self.assertEqual(bot._state["alerts"]["last_storage_level"], "warning")

        self.status["storage_used"] = 0
        bot._check_alerts()
        self.assertEqual(len(bot._outbox), 3)
        self.assertIn("已恢复", bot._outbox[-1].payload["text"])
        self.assertIn("0/23", bot._outbox[-1].payload["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
