"""DJI QDC507 只读 AT 诊断。

严格约束：
- 只发查询命令，绝不发送短信、绝不写 NV、绝不发数据业务指令
- 不改变模块任何持久设置，不切 MBN/IMS/USB 身份
- 不输出号码、验证码或短信正文
"""
from __future__ import annotations

import re
import sys
import time

import serial
from serial.tools import list_ports
from modem_profile import find_supported_at_port, parse_cmti_events

# (命令, 说明)。全部为查询/只读形式。
COMMANDS: list[tuple[str, str]] = [
    ("AT", "串口连通性"),
    ("AT+CPIN?", "SIM 卡状态"),
    ("AT+CGMM", "模块型号"),
    ("AT+CGMR", "固件版本"),
    ('AT+QCFG="usbcfg"', "QDC USB 组合（不支持时返回 ERROR）"),
    ("AT+CSQ", "信号强度"),
    ("AT+COPS?", "当前运营商与接入技术"),
    ("AT+CREG?", "电路域注册"),
    ("AT+CGREG?", "分组域注册"),
    ("AT+CEREG?", "EPS/LTE 注册"),
    ("AT+CSCA?", "短信中心地址 SMSC"),
    ("AT+CSMS?", "短信服务类型"),
    ("AT+CNMI?", "新短信通知设置"),
    ("AT+CGSMS?", "短信服务域选择"),
    ("AT+CMGF?", "短信格式"),
    ("AT+CPMS?", "短信存储用量"),
    ('AT+QCFG="ims"', "★QDC IMS 配置与会话状态"),
    ('AT+QCFG="ltesms/format"', "QDC LTE 短信格式"),
    ('AT+QMBNCFG="AutoSel"', "QDC MBN 自动选择状态"),
    ('AT+QMBNCFG="List"', "QDC MBN 列表与当前激活项"),
]

MASK_PATTERN = re.compile(r"\+?\d{7,}")


def mask_numbers(text: str) -> str:
    """把长号码遮蔽成 前4位+***+位数,保留判断国家码的能力。"""

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        digits = value.lstrip("+")
        prefix = value[:5] if value.startswith("+") else value[:4]
        return f"{prefix}***({len(digits)}位)"

    return MASK_PATTERN.sub(replace, text)


def find_port():
    return find_supported_at_port(list_ports.comports())


def read_reply(handle: serial.Serial, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while time.monotonic() < deadline:
        waiting = handle.in_waiting
        chunk = handle.read(waiting if waiting > 0 else 1)
        if chunk:
            buffer.extend(chunk)
            text = buffer.decode("latin-1")
            if re.search(r"(?:^|\r?\n)(?:OK|ERROR|\+CM[ES] ERROR:[^\r\n]*)\r?\n?$", text):
                break
    return buffer.decode("latin-1")


def clean(response: str, command: str) -> str:
    lines = []
    for line in response.replace("\r", "").split("\n"):
        stripped = line.strip()
        if not stripped or stripped == command:
            continue
        lines.append(stripped)
    return " | ".join(lines) or "(无响应)"


def main() -> int:
    match = find_port()
    if match is None:
        print("未找到 DJI QDC507 的 Quectel AT 口，请确认设备已插好且 SMS 工具已停止。")
        return 1
    port = match.device
    print(f"模块：{match.profile.display_name}\nAT 口：{port}\n")
    try:
        handle = serial.Serial(
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
    except (serial.SerialException, OSError) as exc:
        print(f"打开串口失败（可能仍被其他程序占用）：{exc}")
        return 1

    with handle:
        # 与 QDC507 成功接收探针及 app.py 保持相同控制线状态。
        handle.dtr = False
        handle.rts = False
        time.sleep(0.3)
        handle.reset_input_buffer()
        for command, description in COMMANDS:
            try:
                handle.write(command.encode("ascii") + b"\r")
                handle.flush()
                reply = read_reply(handle, 8.0 if "QMBNCFG" in command else 4.0)
            except (serial.SerialException, OSError) as exc:
                print(f"{command:22} {description}\n    串口异常：{exc}")
                break
            print(f"{command:22} {description}")
            print(f"    {mask_numbers(clean(reply, command))}")
        # 被动监听 20 秒,看是否有任何 URC 自发到达
        print("\n--- 被动监听 20 秒（不发任何命令，看有无自发 URC）---")
        handle.timeout = 1.0
        idle = bytearray()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            chunk = handle.read(handle.in_waiting or 1)
            if chunk:
                idle.extend(chunk)
        if idle:
            idle_text = idle.decode("latin-1")
            cmti_count = len(parse_cmti_events(idle_text))
            direct_count = len(re.findall(r"(?m)^\s*\+CMT:\s*", idle_text))
            print(
                f"    收到 {len(idle)} 字节；CMTI={cmti_count}；"
                f"CMT={direct_count}；内容已隐藏"
            )
        else:
            print("    收到 0 字节（无自发 URC）")
    print("\n完成。本次全部为只读查询，未改变模块任何设置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
