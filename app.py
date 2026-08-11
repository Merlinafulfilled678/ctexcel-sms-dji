from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys

import serial
from flask import Flask, jsonify, request, send_from_directory
from serial.tools import list_ports
from carrier_profile import ACTIVE_CARRIER, parse_balance_amount
from modem_profile import (
    ModemProfile,
    PortMatch,
    diagnostic_commands,
    find_supported_at_port,
    initialization_commands,
    parse_cmti_events,
    parse_quectel_ims,
    parse_wwan_state,
)
from tg_bot import TelegramBot


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SERVICE_ROOT_FINGERPRINT = hashlib.sha256(
    str(BASE_DIR).casefold().encode("utf-8")
).hexdigest()[:32]
STATE_PATH = BASE_DIR / "state.json"
ARCHIVE_PATH = BASE_DIR / "archive.jsonl"


def load_own_number(config_path: Path) -> str:
    """Load the private local number without making it a carrier-wide source fact."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    carrier = raw.get("carrier")
    if not isinstance(carrier, dict):
        return ""
    value = str(carrier.get("own_number") or "").strip()
    return value if re.fullmatch(r"\+[1-9][0-9]{7,14}", value) else ""


OWN_NUMBER = load_own_number(CONFIG_PATH)
HOST = "127.0.0.1"
PORT = int(os.environ.get("SMS_TOOL_PORT", "7597"))
SUPPORTED_AT_PORT_HINT = "Quectel USB AT Port"
WWAN_CHECK_DEBOUNCE_SECONDS = 10.0  # 仅启动时和打开页面时实测;防抖避免多标签页重复触发
SMS_SUBMIT_TIMEOUT_SECONDS = 120.0
MODEM_REQUEST_TIMEOUT_SECONDS = 150.0
WWAN_CHECK_COMMAND = [
    "pnputil",
    "/enum-devices",
    "/connected",
    "/class",
    "Net",
]


class ATCommandError(RuntimeError):
    """An AT command completed with an error or did not complete in time."""


class ATTimeoutError(ATCommandError):
    """The modem did not finish an AT interaction before its deadline."""

    def __init__(self, message: str, response: str = "") -> None:
        super().__init__(message)
        self.response = response


@dataclass
class WorkRequest:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class ArchiveResult:
    persisted_ids: frozenset[str]
    error: str | None = None


def detect_wwan_state() -> str:
    """Perform one read-only WWAN PnP state measurement."""
    try:
        result = subprocess.run(
            WWAN_CHECK_COMMAND,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return parse_wwan_state(result.stdout)


GSM_BASIC_CODES = {
    "@": 0x00,
    "£": 0x01,
    "$": 0x02,
    "¥": 0x03,
    "è": 0x04,
    "é": 0x05,
    "ù": 0x06,
    "ì": 0x07,
    "ò": 0x08,
    "Ç": 0x09,
    "\n": 0x0A,
    "Ø": 0x0B,
    "ø": 0x0C,
    "\r": 0x0D,
    "Å": 0x0E,
    "å": 0x0F,
    "Δ": 0x10,
    "_": 0x11,
    "Φ": 0x12,
    "Γ": 0x13,
    "Λ": 0x14,
    "Ω": 0x15,
    "Π": 0x16,
    "Ψ": 0x17,
    "Σ": 0x18,
    "Θ": 0x19,
    "Ξ": 0x1A,
    "Æ": 0x1C,
    "æ": 0x1D,
    "ß": 0x1E,
    "É": 0x1F,
    "¤": 0x24,
    "¡": 0x40,
    "Ä": 0x5B,
    "Ö": 0x5C,
    "Ñ": 0x5D,
    "Ü": 0x5E,
    "§": 0x5F,
    "¿": 0x60,
    "ä": 0x7B,
    "ö": 0x7C,
    "ñ": 0x7D,
    "ü": 0x7E,
    "à": 0x7F,
}
for _code in range(0x20, 0x7B):
    _char = chr(_code)
    if _char not in "`{}[\\]~|" and _char not in GSM_BASIC_CODES:
        GSM_BASIC_CODES[_char] = _code

GSM_EXTENSION_CODES = {
    "\f": 0x0A,
    "^": 0x14,
    "{": 0x28,
    "}": 0x29,
    "\\": 0x2F,
    "[": 0x3C,
    "~": 0x3D,
    "]": 0x3E,
    "|": 0x40,
    "€": 0x65,
}
GSM_BASIC_CHARACTERS = {code: char for char, code in GSM_BASIC_CODES.items()}
GSM_EXTENSION_CHARACTERS = {code: char for char, code in GSM_EXTENSION_CODES.items()}


def is_gsm_text(text: str) -> bool:
    return all(char in GSM_BASIC_CODES or char in GSM_EXTENSION_CODES for char in text)


def encode_gsm_text(text: str) -> bytes:
    encoded = bytearray()
    for char in text:
        if char in GSM_BASIC_CODES:
            encoded.append(GSM_BASIC_CODES[char])
        else:
            encoded.extend((0x1B, GSM_EXTENSION_CODES[char]))
    return bytes(encoded)


def decode_gsm_bytes(value: bytes | bytearray) -> str:
    decoded: list[str] = []
    position = 0
    while position < len(value):
        code = value[position]
        if code == 0x1B and position + 1 < len(value):
            position += 1
            extension = value[position]
            decoded.append(GSM_EXTENSION_CHARACTERS.get(extension, "\ufffd"))
        else:
            decoded.append(GSM_BASIC_CHARACTERS.get(code, chr(code)))
        position += 1
    return "".join(decoded)


def parse_csv_fields(value: str) -> list[str]:
    try:
        return next(csv.reader([value], skipinitialspace=True))
    except (csv.Error, StopIteration):
        return []


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip(), 0)
    except (TypeError, ValueError):
        return None


def normalize_sim_time(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(
        r"(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})([+-])(\d{2})",
        value,
    )
    if not match:
        return value or datetime.now().astimezone().isoformat(timespec="seconds")

    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    offset_quarters = int(match.group(8))
    offset = timedelta(minutes=15 * offset_quarters)
    if match.group(7) == "-":
        offset = -offset
    try:
        parsed = datetime(
            2000 + year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone(offset),
        )
    except ValueError:
        return value
    return parsed.isoformat(timespec="seconds")


def decode_message_body(body: str, dcs: int | None) -> str:
    compact = "".join(body.split())
    is_hex_ucs2 = bool(compact) and len(compact) % 4 == 0 and bool(
        re.fullmatch(r"[0-9A-Fa-f]+", compact)
    )
    dcs_is_ucs2 = dcs is not None and (dcs & 0x0C) == 0x08
    if dcs_is_ucs2 or (dcs is None and is_hex_ucs2):
        try:
            return bytes.fromhex(compact).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            pass
    return body


def extract_code(body: str) -> str | None:
    keyword = (
        r"(?:验证码|校验码|动态码|verification\s*code|security\s*code|passcode|code|otp)"
    )
    preferred_after_keyword = re.search(
        keyword + r"[^0-9]{0,20}([0-9]{4,8})(?![0-9])",
        body,
        flags=re.IGNORECASE,
    )
    if preferred_after_keyword:
        return preferred_after_keyword.group(1)
    preferred_before_keyword = re.search(
        r"(?<![0-9])([0-9]{4,8})(?![0-9])[^0-9]{0,20}" + keyword,
        body,
        flags=re.IGNORECASE,
    )
    if preferred_before_keyword:
        return preferred_before_keyword.group(1)
    # 排除紧跟 -、:、/ 的数字段(日期时间的一部分,如 2026-07-18),避免误判为验证码
    fallback = re.search(r"(?<![0-9])([0-9]{4,8})(?![0-9])(?![-:/])", body)
    return fallback.group(1) if fallback else None


def parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def registration_code_from_response(response: str | None, prefix: str) -> int | None:
    if not response:
        return None
    match = re.search(
        rf"\+{prefix}:\s*(\d+)(?:\s*,\s*(\d+))?",
        response,
    )
    if match is None:
        return None
    return int(match.group(2) or match.group(1))


def registration_codes_from_responses(
    *, creg: str | None, cgreg: str | None, cereg: str | None
) -> dict[str, int | None]:
    return {
        "creg": registration_code_from_response(creg, "CREG"),
        "cgreg": registration_code_from_response(cgreg, "CGREG"),
        "cereg": registration_code_from_response(cereg, "CEREG"),
    }


def registration_state_from_responses(
    *, creg: str | None, cgreg: str | None, cereg: str | None
) -> tuple[bool, bool]:
    """Combine circuit, packet and LTE registration into one UI state."""
    codes = {
        code
        for code in registration_codes_from_responses(
            creg=creg, cgreg=cgreg, cereg=cereg
        ).values()
        if code is not None
    }
    return bool(codes & {1, 5, 6, 7}), bool(codes & {5, 7})


def balance_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Build balance state only from a verified inbound carrier reply."""
    if message.get("direction", "in") != "in":
        return None
    sender = str(message.get("sender") or "")
    amount = parse_balance_amount(sender, str(message.get("body") or ""))
    timestamp = str(message.get("time") or "")
    if amount is None or parse_iso_timestamp(timestamp) is None:
        return None
    return {
        "amount": amount,
        "source": "sms",
        "time": timestamp,
        "carrier": ACTIVE_CARRIER.key,
    }


def message_id(sender: str, timestamp: str, body: str) -> str:
    raw = f"{sender}\0{timestamp}\0{body}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def parse_direct_message(header: str, body: str) -> dict[str, Any] | None:
    """Parse one text-mode +CMT unsolicited message delivered without SIM storage."""
    match = re.match(r"^\s*\+CMT:\s*(.*)$", header)
    if match is None:
        return None
    fields = parse_csv_fields(match.group(1))
    if not fields:
        return None
    sender = fields[0].strip()
    timestamp = fields[2].strip() if len(fields) > 2 else ""
    dcs = parse_int(fields[6]) if len(fields) > 6 else None
    decoded_body = decode_message_body(body, dcs)
    normalized_sender = sender or "未知发件人"
    normalized_time = normalize_sim_time(timestamp)
    return {
        "index": None,
        "sender": normalized_sender,
        "time": normalized_time,
        "body": decoded_body,
        "code": extract_code(decoded_body),
        "id": message_id(normalized_sender, normalized_time, decoded_body),
        "direction": "in",
        "on_sim": False,
    }


class DirectSmsUrcAssembler:
    """Join a +CMT header with its following text-mode body line across reads."""

    def __init__(self) -> None:
        self._pending_header: str | None = None

    def reset(self) -> None:
        self._pending_header = None

    def feed(self, text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for line in text.replace("\r", "").split("\n"):
            if re.match(r"^\s*\+CMT:\s*", line):
                self._pending_header = line.strip()
                continue
            if self._pending_header is None:
                continue
            if line in {"OK", "ERROR"} or re.match(
                r"^\+(?:CMS|CME) ERROR:", line
            ):
                self._pending_header = None
                continue
            message = parse_direct_message(self._pending_header, line)
            self._pending_header = None
            if message is not None:
                messages.append(message)
        return messages


def parse_message_response(
    response: str, *, cmgr_index: int | None = None
) -> list[dict[str, Any]]:
    """Parse CMGL/CMGR text-mode responses with CSDH extended headers."""
    lines = response.replace("\r", "").split("\n")
    messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_lines: list[str] = []

    def finish_current() -> None:
        nonlocal current, body_lines
        if current is None:
            return
        body = "\n".join(body_lines).strip("\n")
        body = decode_message_body(body, current.pop("_dcs", None))
        current["body"] = body
        current["code"] = extract_code(body)
        current["id"] = message_id(current["sender"], current["time"], body)
        messages.append(current)
        current = None
        body_lines = []

    for position, line in enumerate(lines):
        cmgl = re.match(r"^\+CMGL:\s*(.*)$", line)
        cmgr = re.match(r"^\+CMGR:\s*(.*)$", line)
        if cmgl or cmgr:
            finish_current()
            fields = parse_csv_fields((cmgl or cmgr).group(1))
            if cmgl:
                index = parse_int(fields[0]) if len(fields) > 0 else None
                status = fields[1].strip() if len(fields) > 1 else ""
                sender = fields[2].strip() if len(fields) > 2 else ""
                timestamp = fields[4].strip() if len(fields) > 4 else ""
                dcs = parse_int(fields[8]) if len(fields) > 8 else None
            else:
                index = cmgr_index
                status = fields[0].strip() if len(fields) > 0 else ""
                sender = fields[1].strip() if len(fields) > 1 else ""
                timestamp = fields[3].strip() if len(fields) > 3 else ""
                dcs = parse_int(fields[7]) if len(fields) > 7 else None
            current = {
                "index": index,
                "sender": sender or "未知发件人",
                "time": normalize_sim_time(timestamp),
                "_dcs": dcs,
                "direction": "out" if status.upper().startswith("STO") else "in",
                "on_sim": True,
            }
            continue

        if current is None:
            continue
        remaining = lines[position + 1 :]
        is_final_result = line in {"OK", "ERROR"} and not any(
            item.strip() for item in remaining
        )
        if is_final_result or re.match(r"^\+(?:CMS|CME) ERROR:", line):
            continue
        if re.match(r'^\+CMTI:\s*"[^"]+",\s*\d+', line):
            continue
        body_lines.append(line)

    finish_current()
    return messages


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_locked(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)

    @staticmethod
    def _normalize_balance(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        amount = value.get("amount")
        source = value.get("source")
        timestamp = value.get("time")
        carrier = value.get("carrier")
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or not 0 <= float(amount) <= 1000
            or source not in {"sms", "manual"}
            or parse_iso_timestamp(timestamp) is None
            or not isinstance(carrier, str)
            or not carrier
        ):
            return None
        return {
            "amount": float(amount),
            "source": source,
            "time": timestamp,
            "carrier": carrier,
        }

    def _record_balance_if_newer(self, candidate: dict[str, Any]) -> bool:
        candidate_time = parse_iso_timestamp(candidate["time"])
        if candidate_time is None:
            return False
        with self._lock:
            current = self._normalize_balance(self._state.get("balance"))
            current_time = parse_iso_timestamp(current["time"]) if current else None
            if current_time is not None and candidate_time < current_time:
                return False
            self._state["balance"] = dict(candidate)
            self._save_locked()
        return True

    def record_sms_balance(self, message: dict[str, Any]) -> bool:
        candidate = balance_from_message(message)
        return bool(candidate and self._record_balance_if_newer(candidate))

    def reconcile_balance(self, messages: list[dict[str, Any]]) -> bool:
        candidates = [
            candidate
            for message in messages
            if (candidate := balance_from_message(message)) is not None
        ]
        if not candidates:
            return False
        newest = max(candidates, key=lambda item: parse_iso_timestamp(item["time"]))
        return self._record_balance_if_newer(newest)

    def record_manual_balance(self, amount: float) -> None:
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or not 0 <= float(amount) <= 1000
        ):
            raise ValueError("余额必须是 0 到 1000 之间的数字")
        with self._lock:
            self._state["balance"] = {
                "amount": float(amount),
                "source": "manual",
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "carrier": ACTIVE_CARRIER.key,
            }
            self._save_locked()

    def balance_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            balance = self._normalize_balance(self._state.get("balance"))
            if not balance or balance.get("carrier") != ACTIVE_CARRIER.key:
                return None
            return dict(balance)

    def record_outbound(self) -> None:
        with self._lock:
            self._state["last_outbound"] = date.today().isoformat()
            self._state["last_outbound_carrier"] = ACTIVE_CARRIER.key
            self._save_locked()

    def keepalive_days_left(self) -> int | None:
        with self._lock:
            value = self._state.get("last_outbound")
            carrier = self._state.get("last_outbound_carrier")
        if carrier != ACTIVE_CARRIER.key:
            return None
        if not isinstance(value, str):
            return None
        try:
            last_outbound = date.fromisoformat(value[:10])
        except ValueError:
            return None
        return ACTIVE_CARRIER.activity_window_days - (date.today() - last_outbound).days


class MessageStore:
    def __init__(self, archive_path: Path) -> None:
        self.archive_path = archive_path
        self._lock = threading.Lock()
        self._archive: list[dict[str, Any]] = []
        self._archive_ids: set[str] = set()
        self._sim_messages: dict[int, dict[str, Any]] = {}
        self._load_archive()

    def _load_archive(self) -> None:
        try:
            with self.archive_path.open("r", encoding="utf-8") as handle:
                for archive_order, line in enumerate(handle, start=1):
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    sender = str(item.get("sender") or "未知发件人")
                    timestamp = str(item.get("time") or "")
                    body = str(item.get("body") or "")
                    item_id = str(item.get("id") or message_id(sender, timestamp, body))
                    if item_id in self._archive_ids:
                        continue
                    normalized = {
                        "id": item_id,
                        "sender": sender,
                        "time": timestamp,
                        "body": body,
                        "code": item.get("code") or extract_code(body),
                        "direction": "out" if item.get("direction") == "out" else "in",
                        "received_at": item.get("received_at"),
                        "_archive_order": archive_order,
                    }
                    self._archive.append(normalized)
                    self._archive_ids.add(item_id)
        except OSError:
            return

    def record(self, messages: list[dict[str, Any]]) -> ArchiveResult:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            persisted_ids = {
                str(message["id"])
                for message in messages
                if str(message["id"]) in self._archive_ids
            }
            new_items: list[dict[str, Any]] = []
            queued_ids: set[str] = set()
            for message in messages:
                item_id = str(message["id"])
                if item_id in self._archive_ids or item_id in queued_ids:
                    continue
                item = {
                    "id": item_id,
                    "sender": message["sender"],
                    "time": message["time"],
                    "body": message["body"],
                    "code": message.get("code"),
                    "direction": message.get("direction") or "in",
                    "received_at": now,
                    "_archive_order": len(self._archive) + len(new_items) + 1,
                }
                new_items.append(item)
                queued_ids.add(item_id)

            if not new_items:
                return ArchiveResult(frozenset(persisted_ids))
            try:
                handle = self.archive_path.open("a", encoding="utf-8", newline="\n")
            except OSError as exc:
                return ArchiveResult(frozenset(persisted_ids), str(exc))

            error: str | None = None
            with handle:
                for item in new_items:
                    try:
                        persisted_item = {
                            key: value
                            for key, value in item.items()
                            if not key.startswith("_")
                        }
                        handle.write(
                            json.dumps(persisted_item, ensure_ascii=False) + "\n"
                        )
                        handle.flush()
                        os.fsync(handle.fileno())
                    except OSError as exc:
                        error = str(exc)
                        break
                    self._archive.append(item)
                    self._archive_ids.add(item["id"])
                    persisted_ids.add(item["id"])
            return ArchiveResult(frozenset(persisted_ids), error)

    def record_outbound(self, recipient: str, text: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        item = {
            "id": message_id(f"out:{recipient}", now, text),
            "sender": f"发往 {recipient}",
            "time": now,
            "body": text,
            "code": None,
            "direction": "out",
            "received_at": now,
        }
        result = self.record([item])
        if result.error:
            raise OSError(result.error)

    def archive_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._archive]

    def replace_sim_messages(self, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._sim_messages = {
                int(message["index"]): dict(message)
                for message in messages
                if message.get("index") is not None
            }

    def set_sim_message(self, message: dict[str, Any]) -> None:
        index = message.get("index")
        if index is None:
            return
        with self._lock:
            self._sim_messages[int(index)] = dict(message)

    def clear_sim_messages(self) -> None:
        with self._lock:
            self._sim_messages.clear()

    def remove_sim_message(self, index: int) -> None:
        with self._lock:
            self._sim_messages.pop(index, None)

    def sim_message_count(self) -> int:
        with self._lock:
            return len(self._sim_messages)

    @staticmethod
    def _sort_value(message: dict[str, Any]) -> tuple[bool, float, str]:
        timestamp = parse_iso_timestamp(message.get("time"))
        return (
            timestamp is None,
            timestamp.timestamp() if timestamp is not None else 0.0,
            str(message.get("id") or ""),
        )

    @staticmethod
    def _seconds_apart(first: str, second: str) -> float | None:
        first_time = parse_iso_timestamp(first)
        second_time = parse_iso_timestamp(second)
        if first_time is None or second_time is None:
            return None
        return abs((second_time - first_time).total_seconds())

    @staticmethod
    def _delivery_sort_value(message: dict[str, Any]) -> tuple[int, float, str]:
        """Order physical segments by observed delivery, newest first later.

        QDC507 can deliver concatenated SMS parts tail-first. The archive
        position is the most reliable ordering evidence; received_at is the
        fallback for records without it.
        """
        archive_order = message.get("_archive_order")
        if isinstance(archive_order, int):
            return (2, float(archive_order), str(message.get("id") or ""))
        received_at = parse_iso_timestamp(message.get("received_at"))
        if received_at is not None:
            return (1, received_at.timestamp(), str(message.get("id") or ""))
        timestamp = parse_iso_timestamp(message.get("time"))
        return (
            0,
            timestamp.timestamp() if timestamp is not None else 0.0,
            str(message.get("id") or ""),
        )

    def _merge_long_segments(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        groups: list[list[dict[str, Any]]] = []
        for message in sorted(messages, key=self._sort_value):
            item = dict(message)
            item["_source_ids"] = [str(item.get("id") or "")]
            if not groups:
                groups.append([item])
                continue
            previous = groups[-1][-1]
            distance = self._seconds_apart(previous["time"], message["time"])
            looks_segmented = len(previous["body"]) >= 60 or len(message["body"]) >= 60
            if (
                previous.get("direction") != "out"
                and message.get("direction") != "out"
                and previous["sender"] == message["sender"]
                and distance is not None
                and distance <= 10
                and looks_segmented
            ):
                groups[-1].append(item)
            else:
                groups.append([item])

        merged: list[dict[str, Any]] = []
        for group in groups:
            if len(group) == 1:
                merged.append(group[0])
                continue

            # QDC507 can deliver multipart pieces tail-first. Reverse observed
            # delivery order instead of using the content hash as a same-second
            # tiebreaker.
            ordered = sorted(group, key=self._delivery_sort_value, reverse=True)
            combined = dict(ordered[0])
            combined["body"] = "".join(str(item["body"]) for item in ordered)
            combined["code"] = extract_code(combined["body"])
            combined["on_sim"] = any(bool(item.get("on_sim")) for item in ordered)
            combined["_source_ids"] = [
                source_id
                for item in ordered
                for source_id in item.get("_source_ids", [])
                if source_id
            ]
            combined["id"] = message_id(
                combined["sender"], combined["time"], combined["body"]
            )
            merged.append(combined)
        return merged

    def _snapshot(self, *, include_source_ids: bool) -> list[dict[str, Any]]:
        with self._lock:
            combined: dict[str, dict[str, Any]] = {}
            for archived in self._archive:
                item = {
                    "id": archived["id"],
                    "sender": archived["sender"],
                    "time": archived["time"],
                    "body": archived["body"],
                    "code": archived.get("code"),
                    "direction": archived.get("direction") or "in",
                    "on_sim": False,
                    "received_at": archived.get("received_at"),
                    "_archive_order": archived.get("_archive_order"),
                }
                combined[item["id"]] = item
            for sim_message in self._sim_messages.values():
                archived = combined.get(sim_message["id"], {})
                item = {
                    "id": sim_message["id"],
                    "sender": sim_message["sender"],
                    "time": sim_message["time"],
                    "body": sim_message["body"],
                    "code": sim_message.get("code"),
                    "direction": sim_message.get("direction") or "in",
                    "on_sim": True,
                    "received_at": archived.get("received_at"),
                    "_archive_order": archived.get("_archive_order"),
                }
                combined[item["id"]] = item
        messages = self._merge_long_segments(list(combined.values()))
        for message in messages:
            timestamp = parse_iso_timestamp(message.get("time"))
            message["epoch"] = timestamp.timestamp() if timestamp is not None else None
            message.pop("received_at", None)
            message.pop("_archive_order", None)
            if not include_source_ids:
                message.pop("_source_ids", None)
        return sorted(
            messages,
            key=lambda message: (
                message["epoch"] is None,
                -message["epoch"] if message["epoch"] is not None else 0.0,
                str(message.get("id") or ""),
            ),
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return self._snapshot(include_source_ids=False)

    def notification_snapshot(self) -> list[dict[str, Any]]:
        """Return logical messages plus physical source IDs for push deduping."""
        return self._snapshot(include_source_ids=True)


class WwanStatusMonitor(threading.Thread):
    """Measure WWAN state independently from HTTP and serial-port work."""

    def __init__(self) -> None:
        super().__init__(name="sms-wwan-monitor", daemon=True)
        self._stop_event = threading.Event()
        self._check_request = threading.Event()
        self._status_lock = threading.Lock()
        self._last_check = 0.0
        self._status: dict[str, str | None] = {
            "state": "unknown",
            "checked_at": None,
        }

    def stop(self) -> None:
        self._stop_event.set()
        self._check_request.set()

    def request_check(self) -> None:
        """非阻塞地请求一次实测(页面打开时调用),由监测线程执行。"""
        self._check_request.set()

    def status_snapshot(self) -> dict[str, str | None]:
        with self._status_lock:
            return dict(self._status)

    def _perform_check(self) -> None:
        try:
            state = detect_wwan_state()
            checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
            with self._status_lock:
                self._status = {"state": state, "checked_at": checked_at}
        except Exception:
            # A failed status check must never stop this thread or affect SMS work.
            try:
                checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
                with self._status_lock:
                    self._status = {"state": "unknown", "checked_at": checked_at}
            except Exception:
                pass
        self._last_check = time.monotonic()

    def run(self) -> None:
        # 仅在启动时和每次打开页面时实测,平时不做周期轮询
        self._perform_check()
        while not self._stop_event.is_set():
            self._check_request.wait()
            if self._stop_event.is_set():
                break
            self._check_request.clear()
            if time.monotonic() - self._last_check >= WWAN_CHECK_DEBOUNCE_SECONDS:
                self._perform_check()


class ModemWorker(threading.Thread):
    """The only thread allowed to discover, open, read, or write the serial port."""

    def __init__(self, state_store: StateStore, message_store: MessageStore) -> None:
        super().__init__(name="sms-serial-worker", daemon=True)
        self.state_store = state_store
        self.message_store = message_store
        self._requests: queue.Queue[WorkRequest] = queue.Queue(maxsize=64)
        self._stop_event = threading.Event()
        self._serial: serial.Serial | None = None
        self._modem_profile: ModemProfile | None = None
        self._pending_cmti: queue.SimpleQueue[tuple[str, int]] = queue.SimpleQueue()
        self._pending_direct_sms: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._direct_sms_assembler = DirectSmsUrcAssembler()
        self._idle_buffer = bytearray()
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "connected": False,
            "port": None,
            "signal": None,
            "registered": False,
            "roaming": False,
            "sms_ready": None,
            "operator": "",
            "storage_used": None,
            "storage_total": None,
            "storage_name": None,
            "modem": None,
            "diagnostics": {
                "model": None,
                "firmware": None,
                "cnmi": None,
                "csms": None,
                "cgsms": None,
                "ims": None,
                "ltesms_format": None,
                "registration_domains": {
                    "creg": None,
                    "cgreg": None,
                    "cereg": None,
                },
                "errors": {},
            },
            "sms_submit": {
                "state": "idle",
                "elapsed_seconds": None,
                "result": None,
                "completed_at": None,
            },
            "error": "正在等待设备接入",
        }
        self._next_status_refresh = 0.0

    def submit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        timeout: float = MODEM_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if not self.is_alive():
            return {"ok": False, "error": "短信服务尚未启动"}
        item = WorkRequest(kind=kind, payload=payload or {})
        try:
            self._requests.put(item, timeout=1.0)
        except queue.Full:
            return {"ok": False, "error": "操作队列已满，请稍后再试"}
        if not item.done.wait(timeout):
            return {"ok": False, "error": "设备操作超时"}
        return item.result or {"ok": False, "error": "设备未返回结果"}

    def stop(self) -> None:
        self._stop_event.set()

    def status_snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            status = dict(self._status)
            for key in ("diagnostics", "sms_submit"):
                if isinstance(status.get(key), dict):
                    status[key] = dict(status[key])
            diagnostics = status.get("diagnostics")
            if isinstance(diagnostics, dict):
                for key in ("registration_domains", "errors", "ims"):
                    if isinstance(diagnostics.get(key), dict):
                        diagnostics[key] = dict(diagnostics[key])
        status["own_number"] = OWN_NUMBER
        status["carrier"] = ACTIVE_CARRIER.status_metadata()
        status["keepalive_days_left"] = self.state_store.keepalive_days_left()
        status["balance"] = self.state_store.balance_snapshot()
        return status

    def _set_status(self, **values: Any) -> None:
        with self._status_lock:
            self._status.update(values)

    def _find_at_port(self) -> PortMatch | None:
        # Port discovery also lives here so HTTP threads never touch serial APIs.
        return find_supported_at_port(list_ports.comports())

    def _read_response(self, timeout: float, expect_prompt: bool = False) -> str:
        if self._serial is None:
            raise serial.SerialException("串口未连接")
        deadline = time.monotonic() + timeout
        received = bytearray()
        while time.monotonic() < deadline and not self._stop_event.is_set():
            waiting = self._serial.in_waiting
            chunk = self._serial.read(waiting if waiting > 0 else 1)
            if not chunk:
                continue
            received.extend(chunk)
            text = decode_gsm_bytes(received)
            if expect_prompt and re.search(r"(?:^|\r?\n)>\s*$", text):
                self._capture_sms_urcs(text)
                return text
            if re.search(
                r"(?:^|\r?\n)(?:OK|ERROR|\+CMS ERROR:.*|\+CME ERROR:.*)\r?\n?$",
                text,
            ):
                self._capture_sms_urcs(text)
                return text
        text = decode_gsm_bytes(received)
        self._capture_sms_urcs(text)
        raise ATTimeoutError("等待模块响应超时", response=text)

    @staticmethod
    def _raise_for_at_error(response: str) -> None:
        match = re.search(r"(?:^|\r?\n)((?:\+CMS|\+CME) ERROR:[^\r\n]*|ERROR)(?:\r?\n|$)", response)
        if match:
            raise ATCommandError(match.group(1).strip())

    def _command(
        self, command: str, *, timeout: float = 3.0, expect_prompt: bool = False
    ) -> str:
        if self._serial is None:
            raise serial.SerialException("串口未连接")
        self._serial.write(command.encode("ascii") + b"\r")
        self._serial.flush()
        response = self._read_response(timeout, expect_prompt=expect_prompt)
        self._raise_for_at_error(response)
        if expect_prompt:
            if not re.search(r"(?:^|\r?\n)>\s*$", response):
                raise ATCommandError("模块未返回短信输入提示符")
        elif not re.search(r"(?:^|\r?\n)OK(?:\r?\n|$)", response):
            raise ATCommandError("模块未确认 AT 命令")
        return response

    def _capture_sms_urcs(self, text: str) -> None:
        for storage, index in parse_cmti_events(text):
            self._pending_cmti.put((storage, index))
        for message in self._direct_sms_assembler.feed(text):
            self._pending_direct_sms.put(message)

    @staticmethod
    def _diagnostic_value(response: str) -> str | None:
        values = []
        for line in response.replace("\r", "").split("\n"):
            stripped = line.strip()
            if not stripped or stripped in {"OK", "ERROR"} or stripped.startswith("AT+"):
                continue
            values.append(stripped)
        return " | ".join(values) or None

    def _archive_and_cleanup(self, messages: list[dict[str, Any]]) -> str | None:
        inbound = [message for message in messages if message.get("direction", "in") == "in"]
        if not inbound:
            return None

        result = self.message_store.record(inbound)
        errors: list[str] = []
        if result.error:
            errors.append(f"短信归档失败：{result.error}")

        for message in inbound:
            item_id = str(message.get("id") or "")
            if item_id not in result.persisted_ids:
                continue
            try:
                self.state_store.record_sms_balance(message)
            except OSError as exc:
                errors.append(f"余额状态保存失败：{exc}")

            # 归档成功后仅把消息交给 Telegram 内存队列；任何异常都不得影响串口清理。
            try:
                telegram_bot.notify_incoming_sms(message)
            except Exception as exc:
                print(f"Telegram 新短信回调失败：{exc}", file=sys.stderr)

            index = message.get("index")
            if index is None:
                continue
            sim_index = int(index)
            try:
                self._command(f"AT+CMGD={sim_index}")
            except ATTimeoutError:
                raise
            except ATCommandError as exc:
                errors.append(f"自动清理 SIM 位置 {sim_index} 失败：{exc}")
                continue
            self.message_store.remove_sim_message(sim_index)
            self._set_status(storage_used=self.message_store.sim_message_count())

        error = "；".join(errors) if errors else None
        if error:
            self._set_status(error=error)
        return error

    def _initialize_modem(self) -> str | None:
        profile = self._modem_profile
        if profile is None:
            raise ATCommandError("尚未识别模块类型")

        for command in initialization_commands():
            self._command(command)

        diagnostics: dict[str, Any] = {
            "model": None,
            "firmware": None,
            "cnmi": None,
            "csms": None,
            "cgsms": None,
            "ims": None,
            "ltesms_format": None,
            "registration_domains": {
                "creg": None,
                "cgreg": None,
                "cereg": None,
            },
            "errors": {},
        }
        for key, command in diagnostic_commands():
            try:
                response = self._command(command)
                diagnostics[key] = (
                    parse_quectel_ims(response)
                    if key == "ims"
                    else self._diagnostic_value(response)
                )
            except ATTimeoutError:
                raise
            except ATCommandError as exc:
                diagnostics["errors"][key] = str(exc)
        ims = diagnostics.get("ims")
        ims_registered = bool(isinstance(ims, dict) and ims.get("registered"))
        self._set_status(
            diagnostics=diagnostics,
            sms_ready=ims_registered,
        )

        response = self._command('AT+CMGL="ALL"')
        messages = parse_message_response(response)
        self.message_store.replace_sim_messages(messages)
        self._set_status(storage_used=self.message_store.sim_message_count())
        errors: list[str] = []
        archive_error = self._archive_and_cleanup(messages)
        if archive_error:
            errors.append(archive_error)
        if not ims_registered:
            errors.append("IMS 尚未注册，CTExcel 短信承载不可用")
        return "；".join(errors) if errors else None

    def _try_connect(self) -> None:
        match = self._find_at_port()
        if match is None:
            self._set_status(
                connected=False,
                port=None,
                signal=None,
                registered=False,
                roaming=False,
                sms_ready=None,
                operator="",
                storage_used=None,
                storage_name=None,
                modem=None,
                error=f"未检测到 {SUPPORTED_AT_PORT_HINT}",
            )
            return

        port = match.device
        self._modem_profile = match.profile
        self._set_status(port=port, error=f"正在初始化 {port}")
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=3.0,
                rtscts=False,
                dsrdtr=False,
            )
            # Match the control-line state used by the successful QDC507 probe.
            self._serial.dtr = False
            self._serial.rts = False
            self._idle_buffer.clear()
            self._direct_sms_assembler.reset()
            self._set_status(
                modem=match.profile.status_metadata(),
                storage_name=match.profile.sms_storage,
            )
            initialization_error = self._initialize_modem()
            self._set_status(connected=True, port=port, error=None)
            self._refresh_status()
            if initialization_error:
                self._set_status(error=initialization_error)
            self._next_status_refresh = time.monotonic() + 5.0
        except (serial.SerialException, OSError, ATCommandError) as exc:
            self._disconnect(f"无法连接 {port}：{exc}")

    def _disconnect(self, error: str) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except (serial.SerialException, OSError):
                pass
        self._serial = None
        self._modem_profile = None
        self._idle_buffer.clear()
        self._direct_sms_assembler.reset()
        self._set_status(
            connected=False,
            port=None,
            signal=None,
            registered=False,
            roaming=False,
            sms_ready=None,
            operator="",
            storage_used=None,
            storage_name=None,
            modem=None,
            error=error,
        )

    def _query_status_command(self, command: str) -> str | None:
        try:
            return self._command(command)
        except ATTimeoutError:
            raise
        except ATCommandError as exc:
            self._set_status(error=f"{command} 失败：{exc}")
            return None

    def _refresh_status(self) -> None:
        csq = self._query_status_command("AT+CSQ")
        creg = self._query_status_command("AT+CREG?")
        cgreg = self._query_status_command("AT+CGREG?")
        cereg = self._query_status_command("AT+CEREG?")
        cops = self._query_status_command("AT+COPS?")
        cpms = self._query_status_command("AT+CPMS?")
        ims_response = self._query_status_command('AT+QCFG="ims"')

        updates: dict[str, Any] = {"connected": True}
        if csq:
            match = re.search(r"\+CSQ:\s*(\d+)\s*,", csq)
            if match:
                signal = int(match.group(1))
                updates["signal"] = None if signal == 99 else max(0, min(31, signal))
        updates["registered"], updates["roaming"] = (
            registration_state_from_responses(
                creg=creg,
                cgreg=cgreg,
                cereg=cereg,
            )
        )
        with self._status_lock:
            diagnostics = dict(self._status.get("diagnostics") or {})
        diagnostics["registration_domains"] = registration_codes_from_responses(
            creg=creg,
            cgreg=cgreg,
            cereg=cereg,
        )
        diagnostics["ims"] = parse_quectel_ims(ims_response)
        updates["sms_ready"] = bool(
            updates["registered"]
            and isinstance(diagnostics["ims"], dict)
            and diagnostics["ims"].get("registered")
        )
        updates["diagnostics"] = diagnostics
        if cops:
            match = re.search(r'\+COPS:\s*\d+\s*,\s*\d+\s*,\s*"([^"]*)"', cops)
            if match:
                updates["operator"] = match.group(1)
        if cpms:
            match = re.search(r'\+CPMS:\s*"[^"]+"\s*,\s*(\d+)\s*,\s*(\d+)', cpms)
            if not match:
                match = re.search(r"\+CPMS:\s*(\d+)\s*,\s*(\d+)", cpms)
            if match:
                updates["storage_used"] = int(match.group(1))
                updates["storage_total"] = int(match.group(2))
        required_responses = [csq, creg, cgreg, cereg, cops, cpms]
        required_responses.append(ims_response)
        if all(value is not None for value in required_responses):
            updates["error"] = None
        self._set_status(**updates)

    def _read_idle_once(self) -> None:
        if self._serial is None:
            return
        waiting = self._serial.in_waiting
        chunk = self._serial.read(waiting if waiting > 0 else 1)
        if not chunk:
            return
        self._idle_buffer.extend(chunk)
        while b"\n" in self._idle_buffer:
            raw_line, _, rest = self._idle_buffer.partition(b"\n")
            self._idle_buffer = bytearray(rest)
            line = decode_gsm_bytes(raw_line.rstrip(b"\r"))
            self._capture_sms_urcs(line)

    def _process_one_direct_sms(self) -> bool:
        try:
            message = self._pending_direct_sms.get_nowait()
        except queue.Empty:
            return False
        self._archive_and_cleanup([message])
        return True

    def _process_one_cmti(self) -> bool:
        try:
            storage, index = self._pending_cmti.get_nowait()
        except queue.Empty:
            return False
        try:
            profile = self._modem_profile
            if profile is None:
                raise ATCommandError("尚未识别 DJI QDC507")
            if storage not in {profile.sms_storage, "MT"}:
                raise ATCommandError(
                    f"收到未配置存储 {storage} 的短信通知，当前为 {profile.sms_storage}"
                )
            response = self._command(f"AT+CMGR={index}")
            messages = parse_message_response(response, cmgr_index=index)
            if not messages:
                raise ATCommandError(f"未能解析模块存储位置 {index} 的短信")
            for message in messages:
                self.message_store.set_sim_message(message)
            self._set_status(storage_used=self.message_store.sim_message_count())
            self._archive_and_cleanup(messages)
        except ATTimeoutError:
            raise
        except ATCommandError as exc:
            self._set_status(error=f"读取新短信失败：{exc}")
        return True

    @staticmethod
    def _sms_submit_result(response: str) -> str | None:
        for pattern in (
            r"(?:^|\r?\n)(\+CMGS:\s*\d+)(?:\r?\n|$)",
            r"(?:^|\r?\n)((?:\+CMS|\+CME) ERROR:[^\r\n]*)(?:\r?\n|$)",
            r"(?:^|\r?\n)(ERROR)(?:\r?\n|$)",
        ):
            match = re.search(pattern, response)
            if match:
                return match.group(1).strip()
        return None

    def _send_sms(self, recipient: str, text: str) -> None:
        if self._serial is None:
            raise serial.SerialException("设备未连接")
        use_ucs2 = not is_gsm_text(text)
        operation_started = time.monotonic()
        submit_started: float | None = None
        submit_state = "preparing"
        submit_result: str | None = None
        primary_error: Exception | None = None
        switched_charset = False
        self._set_status(
            sms_submit={
                "state": submit_state,
                "elapsed_seconds": 0.0,
                "result": None,
                "completed_at": None,
            }
        )
        try:
            if use_ucs2:
                self._command('AT+CSCS="UCS2"')
                switched_charset = True
                self._command("AT+CSMP=17,167,0,8")
                encoded_recipient = recipient.encode("utf-16-be").hex().upper()
                payload = text.encode("utf-16-be").hex().upper().encode("ascii")
            else:
                encoded_recipient = recipient
                payload = encode_gsm_text(text)

            self._command(
                f'AT+CMGS="{encoded_recipient}"', timeout=3.0, expect_prompt=True
            )
            self._serial.write(payload + b"\x1a")
            self._serial.flush()
            submit_started = time.monotonic()
            submit_state = "waiting"
            self._set_status(
                sms_submit={
                    "state": submit_state,
                    "elapsed_seconds": 0.0,
                    "result": None,
                    "completed_at": None,
                }
            )
            try:
                response = self._read_response(SMS_SUBMIT_TIMEOUT_SECONDS)
            except ATTimeoutError as exc:
                submit_result = self._sms_submit_result(exc.response)
                raise ATTimeoutError(
                    f"等待短信提交结果 {SMS_SUBMIT_TIMEOUT_SECONDS:.0f} 秒仍无响应"
                    "（未收到完整的 +CMGS/OK 或 +CMS ERROR）",
                    response=exc.response,
                ) from exc
            submit_result = self._sms_submit_result(response)
            self._raise_for_at_error(response)
            if not re.search(r"(?:^|\r?\n)\+CMGS:\s*\d+", response):
                raise ATCommandError("模块未返回 +CMGS 发送确认")
            submit_state = "success"
        except (ATCommandError, serial.SerialException, OSError) as exc:
            primary_error = exc
            submit_state = "timeout" if isinstance(exc, ATTimeoutError) else "error"
            if submit_result is None:
                submit_result = str(exc)
        finally:
            restore_error: Exception | None = None
            if switched_charset and self._serial is not None:
                try:
                    self._command('AT+CSCS="GSM"')
                    self._command("AT+CSMP=17,167,0,0")
                except (ATCommandError, serial.SerialException, OSError) as exc:
                    restore_error = exc

            elapsed_from = submit_started or operation_started
            elapsed_seconds = round(time.monotonic() - elapsed_from, 1)
            status_result = submit_result
            if restore_error is not None:
                restore_detail = f"恢复 GSM 模式失败：{restore_error}"
                status_result = (
                    f"{status_result}；{restore_detail}"
                    if status_result
                    else restore_detail
                )
            completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._set_status(
                sms_submit={
                    "state": submit_state,
                    "elapsed_seconds": elapsed_seconds,
                    "result": status_result,
                    "completed_at": completed_at,
                }
            )
            print(
                "短信提交诊断："
                f"state={submit_state}; elapsed={elapsed_seconds:.1f}s; "
                f"result={status_result or '无'}"
            )

            if primary_error is not None:
                detail = str(primary_error)
                if restore_error is not None:
                    detail += f"（恢复 GSM 模式也失败：{restore_error}）"
                if isinstance(primary_error, (serial.SerialException, OSError)) or isinstance(
                    restore_error, (serial.SerialException, OSError)
                ):
                    raise serial.SerialException(detail) from primary_error
                if isinstance(primary_error, ATTimeoutError) or isinstance(
                    restore_error, ATTimeoutError
                ):
                    response = (
                        primary_error.response
                        if isinstance(primary_error, ATTimeoutError)
                        else ""
                    )
                    raise ATTimeoutError(detail, response=response) from primary_error
                raise ATCommandError(detail) from primary_error
            if restore_error is not None:
                detail = f"短信已发送，但恢复 GSM 模式失败：{restore_error}"
                if isinstance(restore_error, (serial.SerialException, OSError)):
                    raise serial.SerialException(detail) from restore_error
                if isinstance(restore_error, ATTimeoutError):
                    raise ATTimeoutError(
                        detail, response=restore_error.response
                    ) from restore_error
                raise ATCommandError(detail) from restore_error

        # 发给自己不作为运营商活动记录；CTExcel 固定服务号码会正常记录。
        if recipient != OWN_NUMBER:
            self.state_store.record_outbound()
        self.message_store.record_outbound(recipient, text)

    def _handle_request(self, item: WorkRequest) -> None:
        try:
            if self._serial is None:
                item.result = {"ok": False, "error": "设备未连接"}
            elif item.kind == "send":
                self._send_sms(str(item.payload["to"]), str(item.payload["text"]))
                item.result = {"ok": True}
            elif item.kind == "carrier_test":
                action = ACTIVE_CARRIER.self_test
                self._send_sms(action.recipient, action.text)
                item.result = {"ok": True}
            elif item.kind == "balance_query":
                action = ACTIVE_CARRIER.balance_query
                self._send_sms(action.recipient, action.text)
                item.result = {"ok": True}
            elif item.kind == "clear":
                self._command("AT+CMGD=,4")
                self.message_store.clear_sim_messages()
                self._set_status(storage_used=0)
                item.result = {"ok": True}
            else:
                item.result = {"ok": False, "error": "未知操作"}
        except ATTimeoutError as exc:
            item.result = {"ok": False, "error": str(exc)}
            self._disconnect(f"设备响应超时：{exc}")
        except ATCommandError as exc:
            item.result = {"ok": False, "error": str(exc)}
        except (serial.SerialException, OSError) as exc:
            item.result = {"ok": False, "error": f"串口异常：{exc}"}
            self._disconnect(f"设备连接已中断：{exc}")
        finally:
            item.done.set()

    def _process_one_request(self) -> bool:
        try:
            item = self._requests.get_nowait()
        except queue.Empty:
            return False
        self._handle_request(item)
        return True

    def _reject_pending_requests(self, error: str) -> None:
        while True:
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                return
            item.result = {"ok": False, "error": error}
            item.done.set()

    def run(self) -> None:
        next_probe = 0.0
        try:
            while not self._stop_event.is_set():
                if self._serial is None:
                    self._reject_pending_requests("设备未连接")
                    if time.monotonic() >= next_probe:
                        try:
                            self._try_connect()
                        except Exception as exc:  # Keep the reconnect loop alive.
                            self._disconnect(f"设备初始化异常：{exc}")
                        next_probe = time.monotonic() + 3.0
                    self._stop_event.wait(0.1)
                    continue

                try:
                    if self._process_one_direct_sms():
                        continue
                    if self._process_one_cmti():
                        continue
                    if self._process_one_request():
                        continue
                    if time.monotonic() >= self._next_status_refresh:
                        self._refresh_status()
                        self._next_status_refresh = time.monotonic() + 5.0
                        continue
                    self._read_idle_once()
                except ATTimeoutError as exc:
                    self._disconnect(f"设备响应超时：{exc}")
                    next_probe = time.monotonic() + 3.0
                except (serial.SerialException, OSError) as exc:
                    self._disconnect(f"设备连接已中断：{exc}")
                    next_probe = time.monotonic() + 3.0
                except Exception as exc:  # A malformed modem response must not kill the worker.
                    self._disconnect(f"串口工作线程异常：{exc}")
                    next_probe = time.monotonic() + 3.0
        finally:
            self._reject_pending_requests("短信服务已停止")
            if self._serial is not None:
                try:
                    self._serial.close()
                except (serial.SerialException, OSError):
                    pass
                self._serial = None


state_store = StateStore(STATE_PATH)
message_store = MessageStore(ARCHIVE_PATH)
state_store.reconcile_balance(message_store.archive_snapshot())
modem_worker = ModemWorker(state_store, message_store)
wwan_monitor = WwanStatusMonitor()


def telegram_status_snapshot() -> dict[str, Any]:
    status = modem_worker.status_snapshot()
    status["wwan"] = wwan_monitor.status_snapshot()
    return status


telegram_bot = TelegramBot(
    config_path=CONFIG_PATH,
    state_path=BASE_DIR / "tg_state.json",
    archive_path=ARCHIVE_PATH,
    log_path=BASE_DIR / "app.log",
    send_sms=lambda recipient, text: modem_worker.submit(
        "send", {"to": recipient, "text": text}
    ),
    status_provider=telegram_status_snapshot,
    balance_setter=state_store.record_manual_balance,
    message_provider=message_store.notification_snapshot,
)

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.json.ensure_ascii = False


@app.get("/")
def index() -> Any:
    # 每次打开页面视为"点开软件",触发一次 WWAN 实测(非阻塞,带防抖)
    wwan_monitor.request_check()
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def api_status() -> Any:
    status = modem_worker.status_snapshot()
    status["wwan"] = wwan_monitor.status_snapshot()
    status["service_instance"] = {
        "entrypoint": "app.py",
        "root_fingerprint": SERVICE_ROOT_FINGERPRINT,
    }
    return jsonify(status)


@app.post("/api/balance")
def api_balance() -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="请求内容必须是 JSON"), 400
    amount = payload.get("amount")
    try:
        state_store.record_manual_balance(amount)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except OSError as exc:
        return jsonify(ok=False, error=f"余额保存失败：{exc}"), 500
    return jsonify(ok=True)


@app.get("/api/messages")
def api_messages() -> Any:
    return jsonify(message_store.snapshot())


@app.post("/api/send")
def api_send() -> Any:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="请求内容必须是 JSON"), 400
    recipient = payload.get("to")
    text = payload.get("text")
    if not isinstance(recipient, str) or not re.fullmatch(r"\+?[0-9]{3,20}", recipient.strip()):
        return jsonify(ok=False, error="请输入有效的收件人号码"), 400
    if not isinstance(text, str) or not text:
        return jsonify(ok=False, error="短信内容不能为空"), 400
    if len(text) > 2000:
        return jsonify(ok=False, error="短信内容过长"), 400
    result = modem_worker.submit("send", {"to": recipient.strip(), "text": text})
    return jsonify(result), (200 if result.get("ok") else 503)


@app.post("/api/clear")
def api_clear() -> Any:
    result = modem_worker.submit("clear")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.post("/api/carrier-test")
def api_carrier_test() -> Any:
    result = modem_worker.submit("carrier_test")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.post("/api/balance-query")
def api_balance_query() -> Any:
    result = modem_worker.submit("balance_query")
    return jsonify(result), (200 if result.get("ok") else 503)


def port_is_available() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main() -> None:
    # pythonw 无控制台运行时 stdout/stderr 为 None,Flask 写日志会崩溃,重定向到文件
    if sys.stdout is None or sys.stderr is None:
        log_handle = open(BASE_DIR / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log_handle
        sys.stderr = sys.stderr or log_handle
    if not port_is_available():
        return
    modem_worker.start()
    try:
        wwan_monitor.start()
    except Exception as exc:
        print(f"WWAN status monitor failed to start: {exc}", file=sys.stderr)
    if telegram_bot.enabled:
        try:
            telegram_bot.start()
        except Exception as exc:
            print(f"Telegram Bot 启动失败：{exc}", file=sys.stderr)
    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
    finally:
        telegram_bot.stop()
        wwan_monitor.stop()
        modem_worker.stop()
        if telegram_bot.is_alive():
            telegram_bot.join(timeout=35.0)
        if wwan_monitor.is_alive():
            wwan_monitor.join(timeout=11.0)
        modem_worker.join(timeout=35.0)


if __name__ == "__main__":
    main()
