from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_CONFIG: dict[str, Any] = {
    "carrier": {"own_number": ""},
    "telegram": {
        "enabled": False,
        "token": "",
        "chat_id": None,
        "proxy": "",
    },
    "alerts": {
        "low_balance_gbp": 2.0,
        "keepalive_warn_days": 14,
    },
}


class ConfigValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("配置字段无效：" + "、".join(sorted(self.errors)))


class ConfigStore:
    """Validate, redact and atomically persist the local private configuration."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _valid_own_number(value: str) -> bool:
        return not value or re.fullmatch(r"\+[1-9][0-9]{7,14}", value) is not None

    @staticmethod
    def _valid_loopback_proxy(value: str) -> bool:
        if not value:
            return True
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"}:
                return False
            if not parsed.hostname or parsed.username or parsed.password:
                return False
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                return False
            _ = parsed.port
            hostname = parsed.hostname.casefold()
            if hostname == "localhost":
                return True
            return ipaddress.ip_address(hostname).is_loopback
        except (ValueError, UnicodeError):
            return False

    @staticmethod
    def _finite_number(value: Any, default: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        number = float(value)
        return number if math.isfinite(number) else default

    def _read_raw(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        carrier = raw.get("carrier")
        carrier = carrier if isinstance(carrier, dict) else {}
        own_number = str(carrier.get("own_number") or "").strip()
        if not self._valid_own_number(own_number):
            own_number = ""

        telegram = raw.get("telegram")
        telegram = telegram if isinstance(telegram, dict) else {}
        token = str(telegram.get("token") or "").strip()
        chat_id = telegram.get("chat_id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
            chat_id = None
        proxy = str(telegram.get("proxy") or "").strip()
        if not self._valid_loopback_proxy(proxy):
            proxy = ""
        enabled_value = telegram.get("enabled")
        enabled = enabled_value if isinstance(enabled_value, bool) else bool(token)

        alerts = raw.get("alerts")
        alerts = alerts if isinstance(alerts, dict) else {}
        low_balance = self._finite_number(alerts.get("low_balance_gbp"), 2.0)
        if not 0 <= low_balance <= 1000:
            low_balance = 2.0
        keepalive = alerts.get("keepalive_warn_days")
        if isinstance(keepalive, bool) or not isinstance(keepalive, int):
            keepalive = 14
        if not 1 <= keepalive <= 89:
            keepalive = 14

        return {
            "carrier": {"own_number": own_number},
            "telegram": {
                "enabled": enabled,
                "token": token,
                "chat_id": chat_id,
                "proxy": proxy,
            },
            "alerts": {
                "low_balance_gbp": low_balance,
                "keepalive_warn_days": keepalive,
            },
        }

    def read_runtime(self) -> dict[str, Any]:
        with self._lock:
            return self._normalize(self._read_raw())

    @staticmethod
    def _redact(runtime: dict[str, Any]) -> dict[str, Any]:
        telegram = runtime["telegram"]
        return {
            "carrier": dict(runtime["carrier"]),
            "telegram": {
                "enabled": telegram["enabled"],
                "token_configured": bool(telegram["token"]),
                "chat_id_configured": telegram["chat_id"] is not None,
                "proxy": telegram["proxy"],
            },
            "alerts": dict(runtime["alerts"]),
        }

    def read_public(self) -> dict[str, Any]:
        with self._lock:
            return self._redact(self._normalize(self._read_raw()))

    @staticmethod
    def _reject_unknown_fields(
        value: dict[str, Any], allowed: set[str], prefix: str, errors: dict[str, str]
    ) -> None:
        for name in value.keys() - allowed:
            errors[f"{prefix}.{name}" if prefix else name] = "不支持的字段"

    def _write_locked(self, runtime: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(runtime, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def bind_telegram_owner(self, chat_id: int) -> dict[str, Any]:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ConfigValidationError({"telegram.chat_id": "owner ID 无效"})
        with self._lock:
            runtime = self._normalize(self._read_raw())
            runtime["telegram"]["chat_id"] = chat_id
            self._write_locked(runtime)
            return self._redact(runtime)

    def update_public(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ConfigValidationError({"request": "请求内容必须是 JSON 对象"})

        with self._lock:
            current = self._normalize(self._read_raw())
            updated = {
                "carrier": dict(current["carrier"]),
                "telegram": dict(current["telegram"]),
                "alerts": dict(current["alerts"]),
            }
            errors: dict[str, str] = {}
            self._reject_unknown_fields(
                payload, {"carrier", "telegram", "alerts"}, "", errors
            )

            carrier = payload.get("carrier")
            if carrier is not None:
                if not isinstance(carrier, dict):
                    errors["carrier"] = "必须是对象"
                else:
                    self._reject_unknown_fields(
                        carrier, {"own_number"}, "carrier", errors
                    )
                    if "own_number" in carrier:
                        own_number = carrier["own_number"]
                        if not isinstance(own_number, str) or not self._valid_own_number(
                            own_number.strip()
                        ):
                            errors["carrier.own_number"] = "必须为空或 E.164 号码"
                        else:
                            updated["carrier"]["own_number"] = own_number.strip()

            telegram = payload.get("telegram")
            if telegram is not None:
                if not isinstance(telegram, dict):
                    errors["telegram"] = "必须是对象"
                else:
                    self._reject_unknown_fields(
                        telegram, {"enabled", "token", "proxy"}, "telegram", errors
                    )
                    if "enabled" in telegram:
                        enabled = telegram["enabled"]
                        if not isinstance(enabled, bool):
                            errors["telegram.enabled"] = "必须是布尔值"
                        else:
                            updated["telegram"]["enabled"] = enabled
                    if "proxy" in telegram:
                        proxy = telegram["proxy"]
                        if not isinstance(proxy, str) or not self._valid_loopback_proxy(
                            proxy.strip()
                        ):
                            errors["telegram.proxy"] = "必须为空或本机 HTTP(S) 代理"
                        else:
                            updated["telegram"]["proxy"] = proxy.strip()
                    if "token" in telegram:
                        token = telegram["token"]
                        if (
                            not isinstance(token, str)
                            or len(token.strip()) > 256
                            or any(char.isspace() for char in token.strip())
                        ):
                            errors["telegram.token"] = "Token 格式无效"
                        else:
                            token = token.strip()
                            if token != updated["telegram"]["token"]:
                                updated["telegram"]["chat_id"] = None
                            updated["telegram"]["token"] = token

            alerts = payload.get("alerts")
            if alerts is not None:
                if not isinstance(alerts, dict):
                    errors["alerts"] = "必须是对象"
                else:
                    self._reject_unknown_fields(
                        alerts,
                        {"low_balance_gbp", "keepalive_warn_days"},
                        "alerts",
                        errors,
                    )
                    if "low_balance_gbp" in alerts:
                        amount = alerts["low_balance_gbp"]
                        if (
                            isinstance(amount, bool)
                            or not isinstance(amount, (int, float))
                            or not math.isfinite(float(amount))
                            or not 0 <= float(amount) <= 1000
                        ):
                            errors["alerts.low_balance_gbp"] = "必须是 0 到 1000"
                        else:
                            updated["alerts"]["low_balance_gbp"] = float(amount)
                    if "keepalive_warn_days" in alerts:
                        days = alerts["keepalive_warn_days"]
                        if (
                            isinstance(days, bool)
                            or not isinstance(days, int)
                            or not 1 <= days <= 89
                        ):
                            errors["alerts.keepalive_warn_days"] = "必须是 1 到 89"
                        else:
                            updated["alerts"]["keepalive_warn_days"] = days

            if updated["telegram"]["enabled"] and not updated["telegram"]["token"]:
                errors["telegram.token"] = "启用 Telegram 时必须填写 Token"
            if errors:
                raise ConfigValidationError(errors)

            self._write_locked(updated)
            return self._redact(updated)
