from __future__ import annotations

import ast
import csv
import hashlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modem_profile import (
    DJI_QDC507_PROFILE,
    diagnostic_commands,
    find_supported_at_port,
    initialization_commands,
    parse_cmti_events,
    parse_quectel_ims,
    parse_wwan_state,
)


BASE_DIR = Path(__file__).resolve().parent


def load_stored_sms_parser() -> Any:
    """Load the real parser without constructing Flask or serial globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {
        "parse_csv_fields",
        "parse_int",
        "normalize_sim_time",
        "decode_message_body",
        "extract_code",
        "message_id",
        "parse_message_response",
    }
    nodes = [tree.body[0]]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "csv": csv,
        "datetime": datetime,
        "hashlib": hashlib,
        "re": re,
        "timedelta": timedelta,
        "timezone": timezone,
    }
    selected = ast.Module(body=nodes, type_ignores=[])
    exec(compile(selected, "app.py:selected-stored-sms", "exec"), namespace)
    return namespace["parse_message_response"]


class ModemProfileTests(unittest.TestCase):
    def test_dji_port_is_preferred_by_usb_identity_and_description(self) -> None:
        ports = [
            SimpleNamespace(
                device="COM9",
                description="Bluetooth Serial Port",
                vid=None,
                pid=None,
            ),
            SimpleNamespace(
                device="COM42",
                description="Quectel USB AT Port",
                vid=0x2CA3,
                pid=0x4006,
            ),
        ]

        match = find_supported_at_port(ports)

        self.assertIsNotNone(match)
        self.assertEqual(match.device, "COM42")
        self.assertEqual(match.profile, DJI_QDC507_PROFILE)

    def test_non_dji_at_port_is_rejected(self) -> None:
        ports = [
            SimpleNamespace(
                device="COM11",
                description="Generic USB AT Port",
                vid=0x1234,
                pid=0x5678,
            )
        ]

        match = find_supported_at_port(ports)

        self.assertIsNone(match)

    def test_qdc_initialization_uses_me_without_mutating_ims_or_mbn(self) -> None:
        commands = initialization_commands()

        self.assertIn('AT+CPMS="ME","ME","ME"', commands)
        self.assertIn("AT+CNMI=2,1,0,0,0", commands)
        self.assertNotIn("AT+CGSMS=2", commands)
        self.assertFalse(any("QMBNCFG" in command for command in commands))
        self.assertFalse(any('QCFG="ims",' in command for command in commands))

    def test_cmti_parser_accepts_me_and_mt(self) -> None:
        events = parse_cmti_events('+CMTI: "ME",2\r\n+CMTI: "MT",9')

        self.assertEqual(events, [("ME", 2), ("MT", 9)])

    def test_quectel_ims_requires_configured_and_registered_flags(self) -> None:
        self.assertEqual(
            parse_quectel_ims('+QCFG: "ims",1,1\r\nOK'),
            {"configured": True, "registered": True},
        )
        self.assertEqual(
            parse_quectel_ims('+QCFG: "ims",1,0\r\nOK'),
            {"configured": True, "registered": False},
        )

    def test_qdc_diagnostics_are_read_only_and_include_ims(self) -> None:
        commands = {command for _, command in diagnostic_commands()}

        self.assertIn('AT+QCFG="ims"', commands)
        self.assertIn('AT+QCFG="ltesms/format"', commands)
        forbidden = ("NETOPEN", "CGACT", "QIACT", "QMBNCFG=", 'QCFG="ims",')
        self.assertFalse(
            any(token in command for command in commands for token in forbidden)
        )

    def test_wwan_probe_uses_non_admin_pnputil_net_class_query(self) -> None:
        source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="app.py")
        command = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "WWAN_CHECK_COMMAND"
                for target in node.targets
            ):
                command = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(command)
        self.assertEqual(
            command,
            ["pnputil", "/enum-devices", "/connected", "/class", "Net"],
        )

    def test_wwan_parser_only_flags_matching_cellular_net_devices(self) -> None:
        unrelated = (
            "Instance ID: PCI\\VEN_10EC&DEV_8168\n"
            "Device Description: Realtek Ethernet\n"
            "Class Name: Net\n"
            "Status: Started\n"
        )
        enabled = (
            "Instance ID: USB\\VID_2CA3&PID_4006&MI_04\n"
            "Device Description: Quectel WWAN Adapter\n"
            "Class Name: Net\n"
            "Status: Started\n"
        )
        disabled = enabled.replace("Status: Started", "Status: Disabled")

        self.assertEqual(parse_wwan_state(unrelated), "absent")
        self.assertEqual(parse_wwan_state(enabled), "enabled")
        self.assertEqual(parse_wwan_state(disabled), "disabled")


class QdcStoredSmsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parse_message_response = staticmethod(load_stored_sms_parser())

    def test_me_cmti_does_not_pollute_ucs2_cmgr_body(self) -> None:
        body = "验证码已到达，请勿泄露。"
        encoded_body = body.encode("utf-16-be").hex().upper()
        response = (
            '+CMGR: "REC UNREAD","CTExcel",,"26/08/07,12:00:00+32",'
            '145,17,0,8,"service",145,24\r\n'
            f"{encoded_body}\r\n"
            '+CMTI: "ME",3\r\n'
            "OK\r\n"
        )

        messages = self.parse_message_response(response, cmgr_index=2)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["index"], 2)
        self.assertEqual(messages[0]["body"], body)
        self.assertEqual(messages[0]["code"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
