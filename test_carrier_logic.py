from __future__ import annotations

import ast
import json
import math
import os
import re
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from carrier_profile import ACTIVE_CARRIER, parse_balance_amount
from modem_profile import diagnostic_commands, initialization_commands


BASE_DIR = Path(__file__).resolve().parent


def load_state_store_class() -> type:
    """Load the real StateStore implementation without constructing app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {"parse_iso_timestamp", "StateStore"}
    nodes = [tree.body[0]]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted
    )
    namespace: dict[str, Any] = {
        "ACTIVE_CARRIER": ACTIVE_CARRIER,
        "Any": Any,
        "Path": Path,
        "date": date,
        "datetime": datetime,
        "timezone": timezone,
        "json": json,
        "math": math,
        "os": os,
        "threading": threading,
        "balance_from_message": lambda message: None,
    }
    selected = ast.Module(body=nodes, type_ignores=[])
    exec(compile(selected, "app.py:selected", "exec"), namespace)
    return namespace["StateStore"]


def load_balance_from_message():
    """Load the real message-to-state adapter without constructing app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {"parse_iso_timestamp", "balance_from_message"}
    nodes = [tree.body[0]]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    )
    namespace: dict[str, Any] = {
        "ACTIVE_CARRIER": ACTIVE_CARRIER,
        "Any": Any,
        "datetime": datetime,
        "timezone": timezone,
        "parse_balance_amount": parse_balance_amount,
    }
    selected = ast.Module(body=nodes, type_ignores=[])
    exec(compile(selected, "app.py:selected", "exec"), namespace)
    return namespace["balance_from_message"]


def load_registration_state_parser():
    """Load the real pure registration parser without constructing app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {
        "registration_code_from_response",
        "registration_codes_from_responses",
        "registration_state_from_responses",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in wanted
    ]
    if not nodes:
        raise AssertionError("app.py 缺少 LTE/电路域统一注册状态解析 seam")
    namespace: dict[str, Any] = {"Any": Any, "re": __import__("re")}
    selected = ast.Module(body=[tree.body[0], *nodes], type_ignores=[])
    exec(compile(selected, "app.py:selected", "exec"), namespace)
    return namespace["registration_state_from_responses"]


def load_own_number_function():
    """Load the real private-config adapter without constructing app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_own_number"
    ]
    if not nodes:
        raise AssertionError("app.py 缺少本机号码私有配置 seam")
    namespace: dict[str, Any] = {"Path": Path, "json": json, "re": re}
    selected = ast.Module(body=[tree.body[0], *nodes], type_ignores=[])
    exec(compile(selected, "app.py:selected", "exec"), namespace)
    return namespace["load_own_number"]


def modem_initialization_commands() -> set[str]:
    """Return literal AT commands used by the real modem initialization path."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ModemWorker":
            for member in node.body:
                if (
                    isinstance(member, ast.FunctionDef)
                    and member.name == "_initialize_modem"
                ):
                    return {
                        value.value
                        for value in ast.walk(member)
                        if isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value.startswith("AT")
                    }
    raise AssertionError("app.py 缺少 ModemWorker._initialize_modem")


def module_constant(name: str) -> Any:
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"app.py 缺少模块常量 {name}")


def modem_method(name: str) -> ast.FunctionDef:
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ModemWorker":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == name:
                    return member
    raise AssertionError(f"app.py 缺少 ModemWorker.{name}")


class CarrierStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state_store_class = load_state_store_class()
        cls.balance_from_message = staticmethod(load_balance_from_message())
        cls.load_own_number = staticmethod(load_own_number_function())

    def test_own_number_is_loaded_only_from_private_config(self) -> None:
        self.assertFalse(hasattr(ACTIVE_CARRIER, "own_number"))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"carrier": {"own_number": "+447700900123"}}),
                encoding="utf-8",
            )
            self.assertEqual(self.load_own_number(path), "+447700900123")

            path.write_text(
                json.dumps({"carrier": {"own_number": "not-a-number"}}),
                encoding="utf-8",
            )
            self.assertEqual(self.load_own_number(path), "")

            path.write_text("not json", encoding="utf-8")
            self.assertEqual(self.load_own_number(path), "")

    def test_untagged_balance_and_activity_are_hidden_without_deleting_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "last_outbound": date.today().isoformat(),
                        "balance": {
                            "amount": 12.34,
                            "source": "manual",
                            "time": datetime.now().astimezone().isoformat(
                                timespec="seconds"
                            ),
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self.state_store_class(path)

            self.assertIsNone(store.balance_snapshot())
            self.assertIsNone(store.keepalive_days_left())

            store.record_manual_balance(1.25)
            store.record_outbound()

            balance = store.balance_snapshot()
            self.assertIsNotNone(balance)
            self.assertEqual(balance["amount"], 1.25)
            self.assertEqual(balance["carrier"], ACTIVE_CARRIER.key)
            self.assertEqual(
                store.keepalive_days_left(), ACTIVE_CARRIER.activity_window_days
            )

    def test_real_ctexcel_bal_reply_parses_only_cash_balance(self) -> None:
        body = (
            "您当前余额为 £12.34\n"
            "套餐含CN15GB_365DAYS 有效期至 01/01/2030 00:00, 你还有:\n"
            " - 9.87 GB 中国流量"
        )

        self.assertEqual(parse_balance_amount("888", body), 12.34)
        self.assertEqual(parse_balance_amount("+888", body), 12.34)
        self.assertEqual(parse_balance_amount("CTExcel", body), 12.34)
        self.assertIsNone(parse_balance_amount("other", body))
        self.assertIsNone(
            parse_balance_amount(
                "888",
                "套餐含CN15GB_365DAYS，你还有 9.87 GB 中国流量",
            )
        )

        balance = self.balance_from_message(
            {
                "direction": "in",
                "sender": "888",
                "body": body,
                "time": "2026-08-05T12:00:00+08:00",
            }
        )
        self.assertEqual(balance["amount"], 12.34)
        self.assertEqual(balance["source"], "sms")
        self.assertEqual(balance["carrier"], ACTIVE_CARRIER.key)

        self.assertIsNone(
            self.balance_from_message(
                {
                    "direction": "out",
                    "sender": "888",
                    "body": body,
                    "time": "2026-08-05T12:00:00+08:00",
                }
            )
        )

    def test_ctexcel_service_sms_actions_are_unified_on_bal(self) -> None:
        self.assertEqual(ACTIVE_CARRIER.self_test.recipient, "888")
        self.assertEqual(ACTIVE_CARRIER.self_test.text, "BAL")
        self.assertEqual(ACTIVE_CARRIER.self_test.preview_text, "BAL")
        self.assertEqual(
            ACTIVE_CARRIER.self_test.text,
            ACTIVE_CARRIER.balance_query.text,
        )

    def test_lte_roaming_registration_overrides_denied_circuit_domain(self) -> None:
        parse_registration = load_registration_state_parser()
        registered, roaming = parse_registration(
            creg="+CREG: 0,3\r\n\r\nOK",
            cgreg="+CGREG: 0,5\r\n\r\nOK",
            cereg="+CEREG: 0,5\r\n\r\nOK",
        )
        self.assertTrue(registered)
        self.assertTrue(roaming)
        self.assertEqual(
            parse_registration(
                creg="+CREG: 0,1\r\n\r\nOK",
                cgreg=None,
                cereg=None,
            ),
            (True, False),
        )
        self.assertEqual(
            parse_registration(
                creg="+CREG: 0,3\r\n\r\nOK",
                cgreg="+CGREG: 0,3\r\n\r\nOK",
                cereg="+CEREG: 0,3\r\n\r\nOK",
            ),
            (False, False),
        )

    def test_qdc_initialization_omits_packet_domain_sms_preference(self) -> None:
        self.assertNotIn("AT+CGSMS=2", initialization_commands())

    def test_modem_initialization_caches_safe_sms_diagnostics(self) -> None:
        commands = {command for _, command in diagnostic_commands()}
        self.assertTrue(
            {"AT+CGMM", "AT+CGMR", "AT+CNMI?", "AT+CSMS?", "AT+CGSMS?"}
            <= commands
        )

    def test_sms_submit_waits_for_slow_roaming_confirmation(self) -> None:
        self.assertGreaterEqual(module_constant("SMS_SUBMIT_TIMEOUT_SECONDS"), 120.0)
        self.assertGreater(
            module_constant("MODEM_REQUEST_TIMEOUT_SECONDS"),
            module_constant("SMS_SUBMIT_TIMEOUT_SECONDS"),
        )
        send_method = modem_method("_send_sms")
        self.assertTrue(
            any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "_read_response"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "SMS_SUBMIT_TIMEOUT_SECONDS"
                for call in ast.walk(send_method)
                if isinstance(call, ast.Call)
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
