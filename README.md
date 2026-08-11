# CTExcel SMS Tool / 短信工具（DJI QDC507）

[中文](#中文) | [English](#english)

<a id="中文"></a>

## 中文

一个运行在 Windows 本机的短信收发工具，专用于大疆 4G 模块一代 QDC507，通过 Quectel AT 串口管理 CTExcel 英国 SIM。

Flask 后端只监听 `127.0.0.1:7597`，前端无外部 CDN。短信归档、余额和配置默认保存在项目目录；Telegram Bot 为可选功能。

> 本项目是社区工具，与 DJI、Quectel、CTExcel、Telegram 无隶属或背书关系。短信资费、漫游政策和运营商兼容性可能变化，实际费用与服务状态以运营商为准。

### 功能

- 自动发现 `Quectel USB AT Port`，不写死 COM 号。
- 接收 `+CMTI`、`+CMT` 与存储新增短信，支持 UCS2 中文和验证码识别。
- 新短信先以 UTF-8 写入 `archive.jsonl`，确认落盘后才删除模块原件。
- 合并相邻长短信分段，包括已验证的“尾段先到”场景。
- Web 页面查看收件箱、已发送、余额、IMS、注册、信号和模块存储。
- 向 `888` 发送 `BAL` 查询 CTExcel 余额；仅解析明确的现金余额字段，不根据发送记录推算扣费。
- 可选 Telegram Bot：状态、历史、余额、二次确认发送、主动告警和重启去重。
- 只读检测 DJI/Quectel 蜂窝网卡；应用不会建立移动数据连接。

### 安全边界

- 应用不会发出 `NETOPEN`、`CGACT` 等数据业务 AT 指令。
- QDC507 路径只读 IMS/MBN 状态，不自动改 MBN、IMS 或 USB 身份。
- Web 与 Telegram 发送都要求二次确认；发送短信可能产生费用。
- 自动化测试不会打开串口、发送短信、安装驱动或修改网卡。
- 本项目不创建启动文件夹、Run 注册表项或计划任务，保持手动启动。
- 首次连接设备后会归档模块中的已有短信，并在每条短信成功落盘后删除模块原件。若要先保留原件，请先使用只读诊断工具检查。

### 支持环境

- Windows 11 x64
- PowerShell 7
- Python 3.14
- DJI QDC507：`MI_02` 已绑定为 `Quectel USB AT Port`

公开仓库不包含 Quectel 驱动、Python/PowerShell 安装包、wheel 或私人迁移 EXE。驱动获取与绑定边界见 [drivers/README.md](drivers/README.md)。

### 安装

设备不需要在安装 Python 依赖时连接；真正运行和收发短信时才需要插入模块与 SIM。

```powershell
git clone https://github.com/ywang3129-cell/ctexcel-sms-dji.git
Set-Location -LiteralPath .\ctexcel-sms-dji
Copy-Item -LiteralPath .\config.example.json -Destination .\config.json
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --requirement .\requirements.txt
```

在本地编辑 `config.json`：

- `carrier.own_number`：可选，使用完整 E.164 号码；用于界面显示以及排除向本机发送的活跃记录。
- `telegram.token`：可选，BotFather token；不要提交或粘贴到 issue。
- `telegram.chat_id`：可保持 `null`，第一条私聊会绑定唯一 owner。
- `telegram.proxy`：可选 HTTP 代理，例如 `http://127.0.0.1:7897`。

`config.json`、短信归档、余额状态、Telegram 状态和日志都已被 `.gitignore` 排除。

### 启动

调试启动：

```powershell
.\.venv\Scripts\python.exe .\app.py
```

日常也可以双击 `启动短信工具.bat`。浏览器打开：

```text
http://127.0.0.1:7597/
```

程序每 3 秒重新探测 AT 端口。同一时间只能有一个程序占用该串口，请先关闭 PuTTY、Gammu 或其他串口工具。

### CTExcel 与 IMS

在已验证的中国电信漫游场景中，CTExcel 短信依赖可用的 IMS/SMS over IMS 通道。仅看到 LTE/EPS 注册并不代表短信可用；QDC507 还应报告 IMS 会话已注册，并最终观察到真实的 `+CMTI/+CMT` 或 `+CMGS`。

应用不会替用户切换运营商 profile。相关只读诊断、受控配置和回退说明见：

- [DJI-QDC507-CTEXCEL.md](DJI-QDC507-CTEXCEL.md)

### Telegram 命令

- `/status`：查看设备、IMS、WWAN、余额和模块存储状态。
- `/balance`：只读取本地余额，不发短信。
- `/querybalance`：确认后向 `888` 发送 `BAL`。
- `/send 号码 内容`：预览并二次确认后发送。
- `/history [n]`：查看最近收发记录。
- `/setbalance 金额`：手动校准本地余额，不发短信。
- `/help`、`/start`：帮助。

旧 `/test` 仅作为 `/querybalance` 的兼容别名，不出现在命令菜单。

### 本地数据

- `config.json`：私人号码、Telegram 凭据、owner 和告警阈值。
- `archive.jsonl`：唯一可靠的短信历史，每行一条 JSON。
- `state.json`：按运营商保存的余额和短信活跃参考。
- `tg_state.json`：Telegram offset、推送去重与告警节流。
- `app.log`：后台启动日志。

这些文件均不得提交。迁移前停止旧电脑服务，避免两个实例同时使用同一个 Telegram Bot。完整说明见 [MIGRATION.md](MIGRATION.md)。

### 测试

以下测试全部为离线测试：

```powershell
python -m py_compile app.py modem_profile.py tg_bot.py carrier_profile.py diag_readonly.py
python test_carrier_logic.py
python test_long_sms_logic.py
python test_tg_logic.py
python test_dji_qdc507_logic.py
python test_migration_assets.py
python test_installer_assets.py
```

公开源码检出中没有第三方二进制时，相关哈希测试会明确标记为跳过；如果本地放入了完整离线载荷，测试会校验其固定 SHA-256。

### 与上游资料的关系

[wlzh/dji-4g-vohive-mac](https://github.com/wlzh/dji-4g-vohive-mac) 提供了 DJI 私有 USB 身份、Quectel AT 接口和 VoHive/Linux 部署经验。本项目采用了其中的硬件识别知识，但没有复制或运行 VoHive 二进制，也不要求永久改写 USB VID/PID。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

### 许可证

项目自有源码采用 [MIT License](LICENSE)。第三方软件、驱动与商标仍受各自许可证和权利人条款约束。

---

<a id="english"></a>

## English

A local Windows application for sending and receiving SMS messages, built specifically for the first-generation DJI 4G module (QDC507). It manages a CTExcel UK SIM through the Quectel AT serial port.

The Flask backend listens only on `127.0.0.1:7597`, and the web interface uses no external CDN. SMS archives, balance records, and configuration are stored in the project directory by default. Telegram Bot integration is optional.

> This is a community project and is not affiliated with or endorsed by DJI, Quectel, CTExcel, or Telegram. SMS rates, roaming policies, and carrier compatibility may change. Actual charges and service availability are determined by the relevant carrier.

### Features

- Automatically discovers a `Quectel USB AT Port`; no COM number is hard-coded.
- Receives `+CMTI`, `+CMT`, and newly stored messages, with UCS2 Chinese decoding and verification-code detection.
- Writes each new SMS to `archive.jsonl` in UTF-8 before deleting the original from the module.
- Reassembles adjacent multipart messages, including the verified case where the final segment arrives first.
- Provides a local web interface for inbox, sent messages, balance, IMS, registration, signal, and module-storage status.
- Queries the CTExcel balance by sending `BAL` to `888`; only an explicit cash-balance field is parsed, and charges are never estimated from local send history.
- Offers an optional Telegram Bot for status, history, balance, confirmed sending, proactive alerts, and restart-safe deduplication.
- Detects DJI/Quectel cellular network adapters in read-only mode; the application does not establish a mobile-data connection.

### Safety boundaries

- The application never issues mobile-data AT commands such as `NETOPEN` or `CGACT`.
- On QDC507, it only reads IMS/MBN status and does not automatically change the MBN profile, IMS setting, or USB identity.
- Sending from either the web interface or Telegram requires a second confirmation; SMS charges may apply.
- Automated tests do not open serial ports, send SMS messages, install drivers, or change network-adapter state.
- The project does not create Startup-folder shortcuts, Run registry entries, or scheduled tasks. Startup remains manual.
- On the first device connection, existing messages on the module are archived and each original is deleted only after it has been written successfully. Use a read-only diagnostic tool first if you need to preserve the originals before running the application.

### Supported environment

- Windows 11 x64
- PowerShell 7
- Python 3.14
- DJI QDC507 with `MI_02` bound as `Quectel USB AT Port`

The public repository does not include Quectel drivers, Python/PowerShell installers, wheels, or a private migration EXE. See [drivers/README.md](drivers/README.md) for driver-acquisition and binding boundaries.

### Installation

The module does not need to be connected while installing Python dependencies. Connect the module and SIM only when you are ready to run the application and send or receive SMS messages.

```powershell
git clone https://github.com/ywang3129-cell/ctexcel-sms-dji.git
Set-Location -LiteralPath .\ctexcel-sms-dji
Copy-Item -LiteralPath .\config.example.json -Destination .\config.json
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --requirement .\requirements.txt
```

Edit `config.json` locally:

- `carrier.own_number`: optional; use the full E.164 number. It is used for display and to exclude messages sent to this SIM from the activity reference.
- `telegram.token`: optional BotFather token. Never commit it or paste it into an issue.
- `telegram.chat_id`: may remain `null`; the first private chat binds the sole owner.
- `telegram.proxy`: optional HTTP proxy, for example `http://127.0.0.1:7897`.

`config.json`, SMS archives, balance state, Telegram state, and logs are excluded by `.gitignore`.

### Running

For a foreground/debug run:

```powershell
.\.venv\Scripts\python.exe .\app.py
```

For normal use, you can also double-click `启动短信工具.bat`. Then open:

```text
http://127.0.0.1:7597/
```

The application rescans for the AT port every three seconds. Only one process can own the serial port at a time, so close PuTTY, Gammu, or any other serial-port tool first.

### CTExcel and IMS

In the validated CTExcel roaming scenario on China Telecom in mainland China, SMS depends on a working IMS/SMS-over-IMS channel. LTE/EPS registration alone does not prove that SMS is available. The QDC507 should also report an active IMS session, followed by real `+CMTI`/`+CMT` events for inbound SMS or `+CMGS` for outbound SMS.

The application does not switch the carrier profile for you. See the following notes for read-only diagnostics, controlled configuration, and rollback guidance:

- [DJI-QDC507-CTEXCEL.md](DJI-QDC507-CTEXCEL.md)

### Telegram commands

- `/status`: show device, IMS, WWAN, balance, and module-storage status.
- `/balance`: read the locally stored balance without sending an SMS.
- `/querybalance`: after confirmation, send `BAL` to `888`.
- `/send NUMBER MESSAGE`: preview and send after a second confirmation.
- `/history [n]`: show recent incoming and outgoing messages.
- `/setbalance AMOUNT`: manually correct the local balance without sending an SMS.
- `/help`, `/start`: show help.

The legacy `/test` command remains a hidden alias for `/querybalance` and is not included in the command menu.

### Local data

- `config.json`: private phone number, Telegram credentials, owner, and alert thresholds.
- `archive.jsonl`: the authoritative SMS history, with one JSON object per line.
- `state.json`: carrier-scoped balance and SMS-activity reference.
- `tg_state.json`: Telegram update offset, notification deduplication, and alert throttling.
- `app.log`: log file used for background startup.

Never commit these files. Stop the service on the old computer before migrating so that two instances do not use the same Telegram Bot at the same time. See [MIGRATION.md](MIGRATION.md) for the complete procedure.

### Tests

All commands below are offline tests:

```powershell
python -m py_compile app.py modem_profile.py tg_bot.py carrier_profile.py diag_readonly.py
python test_carrier_logic.py
python test_long_sms_logic.py
python test_tg_logic.py
python test_dji_qdc507_logic.py
python test_migration_assets.py
python test_installer_assets.py
```

When a public source checkout does not contain third-party binaries, the related hash checks explicitly report them as skipped. If the complete offline payload is placed into a local checkout, the tests verify its pinned SHA-256 hashes.

### Relationship to upstream work

[wlzh/dji-4g-vohive-mac](https://github.com/wlzh/dji-4g-vohive-mac) documents the DJI-specific USB identity, Quectel AT interface, and VoHive/Linux deployment experience. This project uses that hardware-identification knowledge, but it neither copies nor runs the VoHive binary and does not require permanently rewriting the USB VID/PID. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.

### License

Original source code in this project is licensed under the [MIT License](LICENSE). Third-party software, drivers, and trademarks remain subject to their respective licenses and rights holders.
