from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app as app_module
from config_store import ConfigStore


class ConfigApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ctexcel-config-api-")
        self.config_path = Path(self.temporary.name) / "config.json"
        self.original_store = app_module.config_store
        self.original_runtime_started = app_module.runtime_started
        self.original_telegram_bot = app_module.telegram_bot
        self.original_bot_factory = app_module.create_telegram_bot
        app_module.config_store = ConfigStore(self.config_path)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module.config_store = self.original_store
        app_module.runtime_started = self.original_runtime_started
        app_module.telegram_bot = self.original_telegram_bot
        app_module.create_telegram_bot = self.original_bot_factory
        self.temporary.cleanup()

    def test_get_is_redacted_and_post_requires_csrf_token(self) -> None:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["csrf_token"])
        self.assertNotIn("token", body["config"]["telegram"])
        self.assertNotIn("chat_id", body["config"]["telegram"])

        rejected = self.client.post(
            "/api/config",
            json={"carrier": {"own_number": "+447700900123"}},
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertFalse(self.config_path.exists())

    def test_post_persists_secret_without_echoing_it(self) -> None:
        csrf_token = self.client.get("/api/config").get_json()["csrf_token"]
        secret = "123456:never-echo-this"
        response = self.client.post(
            "/api/config",
            headers={"X-CTExcel-CSRF": csrf_token},
            json={
                "carrier": {"own_number": "+447700900123"},
                "telegram": {
                    "enabled": True,
                    "token": secret,
                    "proxy": "http://127.0.0.1:7897",
                },
                "alerts": {"low_balance_gbp": 2.0, "keepalive_warn_days": 14},
            },
        )
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn(secret, text)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["config"]["telegram"]["token_configured"])
        self.assertTrue(body["restart_required"])
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))["telegram"][
                "token"
            ],
            secret,
        )

        reread = self.client.get("/api/config").get_data(as_text=True)
        self.assertNotIn(secret, reread)

    def test_first_enable_starts_a_fresh_bot_without_restarting_the_app(self) -> None:
        class FakeBot:
            enabled = True
            ident = None

            def __init__(self) -> None:
                self.started = False

            def is_alive(self) -> bool:
                return False

            def start(self) -> None:
                self.started = True

        replacement = FakeBot()
        app_module.runtime_started = True
        app_module.create_telegram_bot = lambda: replacement
        csrf_token = self.client.get("/api/config").get_json()["csrf_token"]
        response = self.client.post(
            "/api/config",
            headers={"X-CTExcel-CSRF": csrf_token},
            json={
                "telegram": {
                    "enabled": True,
                    "token": "123456:private-token",
                    "proxy": "",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["restart_required"])
        self.assertIs(app_module.telegram_bot, replacement)
        self.assertTrue(replacement.started)


if __name__ == "__main__":
    unittest.main(verbosity=2)
