from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent


def load_message_store_class() -> type:
    """Load the real MessageStore implementation without starting app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {
        "parse_iso_timestamp",
        "extract_code",
        "message_id",
        "MessageStore",
    }
    nodes = [tree.body[0]]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and node.name in wanted
    )
    namespace: dict[str, Any] = {
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
        "threading": threading,
        "datetime": datetime,
        "timezone": timezone,
        "Path": Path,
        "Any": Any,
    }
    selected = ast.Module(body=nodes, type_ignores=[])
    exec(compile(selected, "app.py:selected", "exec"), namespace)
    return namespace["MessageStore"]


def load_direct_sms_helpers() -> tuple[Any, type]:
    """Load the real +CMT parser/assembler without constructing app globals."""
    source = (BASE_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")
    wanted = {
        "parse_csv_fields",
        "parse_int",
        "normalize_sim_time",
        "decode_message_body",
        "extract_code",
        "message_id",
        "parse_direct_message",
        "DirectSmsUrcAssembler",
    }
    nodes = [tree.body[0]]
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted
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
    exec(compile(selected, "app.py:selected-direct-sms", "exec"), namespace)
    return namespace["parse_direct_message"], namespace["DirectSmsUrcAssembler"]


class LongSmsAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.message_store_class = load_message_store_class()

    def setUp(self) -> None:
        self.store = self.message_store_class.__new__(self.message_store_class)

    @staticmethod
    def segment(
        item_id: str,
        body: str,
        sms_time: str,
        received_at: str,
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "sender": "carrier",
            "time": sms_time,
            "body": body,
            "code": None,
            "direction": "in",
            "on_sim": False,
            "received_at": received_at,
        }

    def test_reverse_arrival_segments_are_reassembled_in_content_order(self) -> None:
        first = "Hey, welcome abroad. Make sure your account can use the"
        second = " internet. Calls and texts are available. For m" + "x" * 70
        third = "ore information, check your account and monit" + "y" * 70
        fourth = "or usage. Cheers and safe travels." + "z" * 70

        # The modem delivered this real-world pattern tail-first. Three segments
        # also share one SMS timestamp, so a hash-ID tiebreaker cannot order them.
        arrival_order = [
            self.segment(
                "10000000000000000000",
                fourth,
                "2026-07-24T16:33:49+01:00",
                "2026-07-24T23:33:57+08:00",
            ),
            self.segment(
                "20000000000000000000",
                third,
                "2026-07-24T16:33:50+01:00",
                "2026-07-24T23:33:59+08:00",
            ),
            self.segment(
                "30000000000000000000",
                second,
                "2026-07-24T16:33:50+01:00",
                "2026-07-24T23:34:00+08:00",
            ),
            self.segment(
                "40000000000000000000",
                first,
                "2026-07-24T16:33:50+01:00",
                "2026-07-24T23:34:01+08:00",
            ),
        ]

        merged = self.store._merge_long_segments(arrival_order)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["body"], first + second + third + fourth)
        self.assertEqual(
            merged[0]["_source_ids"],
            [
                "40000000000000000000",
                "30000000000000000000",
                "20000000000000000000",
                "10000000000000000000",
            ],
        )


class DirectSmsUrcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        parser, cls.assembler_class = load_direct_sms_helpers()
        cls.parse_direct_message = staticmethod(parser)

    def test_text_mode_cmt_is_parsed_as_non_sim_inbound_message(self) -> None:
        header = (
            '+CMT: "CTExcel",,"26/08/05,12:34:56+32",145,17,0,0,'
            '"+447785016005",145,19'
        )
        message = self.parse_direct_message(header, "Your code is 123456")

        self.assertIsNotNone(message)
        self.assertEqual(message["sender"], "CTExcel")
        self.assertEqual(message["time"], "2026-08-05T12:34:56+08:00")
        self.assertEqual(message["body"], "Your code is 123456")
        self.assertEqual(message["code"], "123456")
        self.assertIsNone(message["index"])
        self.assertFalse(message["on_sim"])
        self.assertEqual(message["direction"], "in")

    def test_cmt_assembler_keeps_header_until_body_arrives(self) -> None:
        assembler = self.assembler_class()
        header = (
            '+CMT: "+447700900123",,"26/08/05,12:35:00+32",145,17,0,8,'
            '"+447785016005",145,3'
        )

        self.assertEqual(assembler.feed(header), [])
        messages = assembler.feed("9A8C8BC17801")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "验证码")


if __name__ == "__main__":
    unittest.main(verbosity=2)
