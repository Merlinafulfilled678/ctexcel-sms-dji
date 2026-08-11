from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ModemProfile:
    key: str
    display_name: str
    port_markers: tuple[str, ...]
    usb_ids: frozenset[tuple[int, int]]
    sms_storage: str

    def status_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.display_name,
            "sms_storage": self.sms_storage,
            "ims_query_supported": True,
        }


@dataclass(frozen=True)
class PortMatch:
    device: str
    profile: ModemProfile


DJI_QDC507_PROFILE = ModemProfile(
    key="dji_qdc507",
    display_name="DJI 4G Module 1 / QDC507",
    port_markers=("Quectel USB AT Port",),
    # 2CA3:4006 is the untouched DJI identity. 2C7C:0125 is the Quectel
    # identity used by some community guides; both expose the same AT surface.
    usb_ids=frozenset({(0x2CA3, 0x4006), (0x2C7C, 0x0125)}),
    sms_storage="ME",
)

def _port_match_score(port: Any, profile: ModemProfile) -> int:
    description = str(getattr(port, "description", "") or "")
    if not any(marker.casefold() in description.casefold() for marker in profile.port_markers):
        return 0

    score = 50
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if isinstance(vid, int) and isinstance(pid, int) and (vid, pid) in profile.usb_ids:
        score += 100
    return score


def find_supported_at_port(ports: Iterable[Any]) -> PortMatch | None:
    """Return the strongest DJI QDC507 AT-port match without opening a port."""
    best: tuple[int, PortMatch] | None = None
    for port in ports:
        device = str(getattr(port, "device", "") or "").strip()
        if not device:
            continue
        score = _port_match_score(port, DJI_QDC507_PROFILE)
        if score <= 0:
            continue
        candidate = PortMatch(device=device, profile=DJI_QDC507_PROFILE)
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best is not None else None


def initialization_commands() -> tuple[str, ...]:
    """Build the non-data SMS setup for DJI QDC507.

    The QDC507 path deliberately does not change IMS, MBN, USB identity, or
    packet-data state. IMS was configured separately and is only queried by the
    application.
    """
    commands = ["ATE0", "AT+CMGF=1"]
    commands.extend(
        (
            'AT+CSCS="GSM"',
            "AT+CSDH=1",
            f'AT+CPMS="{DJI_QDC507_PROFILE.sms_storage}",'
            f'"{DJI_QDC507_PROFILE.sms_storage}",'
            f'"{DJI_QDC507_PROFILE.sms_storage}"',
            "AT+CNMI=2,1,0,0,0",
        )
    )
    return tuple(commands)


def diagnostic_commands() -> tuple[tuple[str, str], ...]:
    return (
        ("model", "AT+CGMM"),
        ("firmware", "AT+CGMR"),
        ("cnmi", "AT+CNMI?"),
        ("csms", "AT+CSMS?"),
        ("cgsms", "AT+CGSMS?"),
        ("ims", 'AT+QCFG="ims"'),
        ("ltesms_format", 'AT+QCFG="ltesms/format"'),
    )


def parse_cmti_events(text: str) -> list[tuple[str, int]]:
    return [
        (match.group("storage").upper(), int(match.group("index")))
        for match in re.finditer(
            r'^\s*\+CMTI:\s*"(?P<storage>[^"]+)",\s*(?P<index>\d+)\s*$',
            text,
            re.MULTILINE,
        )
    ]


def parse_quectel_ims(response: str | None) -> dict[str, bool] | None:
    if not response:
        return None
    match = re.search(r'\+QCFG:\s*"ims",\s*(\d+)\s*,\s*(\d+)', response)
    if match is None:
        return None
    return {
        "configured": int(match.group(1)) == 1,
        "registered": int(match.group(2)) == 1,
    }


def parse_wwan_state(output: str) -> str:
    """Parse ``pnputil /enum-devices /connected /class Net`` output."""
    blocks = re.split(r"(?:\r?\n){2,}", output)
    matching = [
        block
        for block in blocks
        if re.search(
            r"(?i)(VID_(?:2CA3|2C7C)|Quectel|Baiwang|DJI)",
            block,
        )
    ]
    if not matching:
        return "absent"

    statuses: list[str] = []
    for block in matching:
        match = re.search(
            r"(?im)^\s*(?:Status|状态)\s*:\s*(?P<status>.*?)\s*$",
            block,
        )
        if match is None:
            return "unknown"
        statuses.append(match.group("status"))

    if any(re.search(r"(?i)(Started|已启动|Problem|错误)", value) for value in statuses):
        return "enabled"
    if all(re.search(r"(?i)(Disabled|已禁用)", value) for value in statuses):
        return "disabled"
    return "unknown"
