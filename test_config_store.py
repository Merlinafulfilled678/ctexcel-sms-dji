from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config_store import ConfigStore, ConfigValidationError


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ctexcel-config-")
        self.config_path = Path(self.temporary.name) / "config.json"
        self.store = ConfigStore(self.config_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_update_writes_valid_config_but_only_returns_redacted_values(self) -> None:
        public = self.store.update_public(
            {
                "carrier": {"own_number": "+447700900123"},
                "telegram": {
                    "enabled": True,
                    "token": "123456:private-token",
                    "proxy": "http://127.0.0.1:7897",
                },
                "alerts": {"low_balance_gbp": 2.5, "keepalive_warn_days": 14},
            }
        )

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["telegram"]["token"], "123456:private-token")
        self.assertIsNone(written["telegram"]["chat_id"])
        self.assertTrue(written["telegram"]["enabled"])
        self.assertNotIn("token", public["telegram"])
        self.assertNotIn("chat_id", public["telegram"])
        self.assertTrue(public["telegram"]["token_configured"])
        self.assertFalse(public["telegram"]["chat_id_configured"])
        self.assertNotIn("private-token", json.dumps(public, ensure_ascii=False))

    def test_omitted_token_preserves_it_and_replacing_token_clears_owner_binding(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "carrier": {"own_number": ""},
                    "telegram": {
                        "enabled": True,
                        "token": "old-token",
                        "chat_id": 12345,
                        "proxy": "http://localhost:7897",
                    },
                    "alerts": {"low_balance_gbp": 2.0, "keepalive_warn_days": 14},
                }
            ),
            encoding="utf-8",
        )
        self.store.update_public(
            {
                "telegram": {
                    "enabled": True,
                    "proxy": "http://localhost:7897",
                }
            }
        )
        self.assertEqual(self.store.read_runtime()["telegram"]["token"], "old-token")
        self.assertEqual(self.store.read_runtime()["telegram"]["chat_id"], 12345)

        self.store.update_public({"telegram": {"token": "new-token"}})
        runtime = self.store.read_runtime()
        self.assertEqual(runtime["telegram"]["token"], "new-token")
        self.assertIsNone(runtime["telegram"]["chat_id"])

    def test_invalid_update_is_rejected_without_changing_the_file(self) -> None:
        self.store.update_public({"carrier": {"own_number": "+447700900123"}})
        before = self.config_path.read_bytes()
        with self.assertRaises(ConfigValidationError) as raised:
            self.store.update_public(
                {
                    "carrier": {"own_number": "not-a-phone-number"},
                    "telegram": {"proxy": "https://example.com:443"},
                    "alerts": {"keepalive_warn_days": 90},
                }
            )
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(
            set(raised.exception.errors),
            {
                "carrier.own_number",
                "telegram.proxy",
                "alerts.keepalive_warn_days",
            },
        )
        self.assertNotIn("not-a-phone-number", str(raised.exception))
        self.assertFalse(self.config_path.with_suffix(".json.tmp").exists())

    def test_missing_file_has_safe_public_defaults(self) -> None:
        public = self.store.read_public()
        self.assertEqual(public["carrier"]["own_number"], "")
        self.assertFalse(public["telegram"]["enabled"])
        self.assertFalse(public["telegram"]["token_configured"])
        self.assertEqual(public["telegram"]["proxy"], "")
        self.assertEqual(public["alerts"]["low_balance_gbp"], 2.0)
        self.assertEqual(public["alerts"]["keepalive_warn_days"], 14)

    def test_owner_binding_preserves_other_fields_and_stays_redacted(self) -> None:
        self.store.update_public(
            {
                "carrier": {"own_number": "+447700900123"},
                "telegram": {"enabled": True, "token": "private-token"},
            }
        )
        self.store.bind_telegram_owner(12345)
        runtime = self.store.read_runtime()
        self.assertEqual(runtime["telegram"]["chat_id"], 12345)
        self.assertEqual(runtime["telegram"]["token"], "private-token")
        self.assertEqual(runtime["carrier"]["own_number"], "+447700900123")
        self.assertNotIn("chat_id", self.store.read_public()["telegram"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
