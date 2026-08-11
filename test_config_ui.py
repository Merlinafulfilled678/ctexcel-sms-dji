from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "static" / "index.html"


class ConfigUiTests(unittest.TestCase):
    def test_local_setup_ui_keeps_secrets_write_only(self) -> None:
        source = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="settingsButton"', source)
        self.assertIn('id="configDialog"', source)
        self.assertIn('id="telegramToken"', source)
        self.assertIn('type="password"', source)
        self.assertIn('autocomplete="new-password"', source)
        self.assertNotRegex(source, r'id="telegramToken"[^>]*\svalue=')
        self.assertIn('id="clearTelegramToken"', source)
        self.assertIn('api("/api/config")', source)
        self.assertIn('"X-CTExcel-CSRF": configCsrfToken', source)
        self.assertNotIn("innerHTML", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
