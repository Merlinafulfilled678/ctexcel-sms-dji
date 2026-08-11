from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ServiceSms:
    """A carrier-defined SMS action that always requires caller confirmation."""

    key: str
    title: str
    recipient: str
    text: str
    preview_text: str


@dataclass(frozen=True)
class CarrierProfile:
    """Facts that vary when the physical SIM is moved to another carrier."""

    key: str
    name: str
    activity_window_days: int
    rate_note: str
    send_warning: str
    self_test: ServiceSms
    balance_query: ServiceSms

    def status_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "activity_window_days": self.activity_window_days,
            "rate_note": self.rate_note,
            # 已用本机真实 BAL 回复验证现金余额字段；套餐等字段仍保留原文展示。
            "balance_mode": "sms_query_auto_parse",
        }


LEGACY_CARRIER_KEY = "giffgaff"

_CTEXCEL_BALANCE_SENDERS = frozenset({"888", "ctexcel"})
_CTEXCEL_BALANCE_PATTERN = re.compile(
    r"您当前余额为\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)"
)


def parse_balance_amount(sender: str, body: str) -> float | None:
    """Return the verified CTExcel cash balance, never package allowances."""
    normalized_sender = sender.strip().lower().removeprefix("+")
    if normalized_sender not in _CTEXCEL_BALANCE_SENDERS:
        return None
    match = _CTEXCEL_BALANCE_PATTERN.search(body)
    if match is None:
        return None
    amount = float(match.group(1))
    return amount if 0 <= amount <= 1000 else None

ACTIVE_CARRIER = CarrierProfile(
    key="ctexcel",
    name="CTExcel",
    # 这是软件侧的保守短信活跃提醒，不代表 CTExcel 官方销号日期。
    activity_window_days=90,
    rate_note=(
        "CTExcel：中国大陆接收短信免费 · 发送参考 10p/条 · "
        "888 服务短信以实际账单为准 · 流量已禁用"
    ),
    send_warning="短信可能产生费用；888 服务短信是否免费以 CTExcel 实际账单为准。",
    self_test=ServiceSms(
        key="self_test",
        title="CTExcel BAL 收发自检",
        recipient="888",
        text="BAL",
        preview_text="BAL",
    ),
    balance_query=ServiceSms(
        key="balance_query",
        title="CTExcel 余额查询",
        recipient="888",
        text="BAL",
        preview_text="BAL",
    ),
)
