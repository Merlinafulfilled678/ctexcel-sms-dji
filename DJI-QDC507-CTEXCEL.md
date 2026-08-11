# DJI QDC507 + CTExcel 短信开发与验收记录

最后更新：2026-08-07

## 结论

可行。大疆 4G 模块一代已在 Windows 上通过 AT 串口驻留中国电信 LTE，并在 ROW MBN 下建立 IMS 会话；用户触发 CTExcel 验证码后，短信已进入 QDC507 的 `ME` 存储并可通过 `CMGR` 读取。原 SIM7600CE-T 的失败点是没有建立同等 IMS 短信通道，而不是 Web、Telegram 或正文解析。

入站底层验收、应用受控启动和出站均已完成：模块现存短信能够逐条归档，后续新短信能够实时进入 Web 与 Telegram；经用户明确授权的一次 `BAL` 查询取得真实 `+CMGS`，官方回复分段被合并并自动更新余额。CTExcel 在 QDC507 上的收发 SMS over IMS 已实测可用。

## Windows 驱动与 USB 边界

- 设备保持 DJI 原始 USB 身份 `VID_2CA3&PID_4006`，没有执行社区教程中的永久 VID/PID 改写。
- 只把 `MI_02` 精确绑定为 `Quectel USB AT Port`；COM 号由 Windows 动态分配，代码不得写死。
- 使用 Microsoft Update Catalog 中的 Quectel Ports `30.0.65.2` 驱动，`qcser.cat` 为 Microsoft WHCP 签名；驱动已进入 Driver Store。
- 已从当前实测工作的 Driver Store 导出完整 x64 驱动到 `drivers/Quectel-Ports-30.0.65.2`，记录三个 SHA-256，并让迁移安装脚本在列出候选或安装前校验哈希和签名。新电脑的 Instance ID、`oemXX.inf` 和 COM 号均动态发现，不沿用当前机器值。
- 下载 CAB 的 SHA-256 为 `3068B59C215D6EFB9955F68CA16ED3E3474D49C7BFF2DC42C661AA8FF4D358E4`。
- 其他 DJI 接口没有强行套用串口或网卡驱动；Windows 当前没有 DJI/Quectel WWAN 网络适配器。
- `tools/Bind-DjiAtPort.ps1` 只允许列出设备，或精确处理 `VID_2CA3&PID_4006&MI_02`，避免误绑其他接口。

这条 Windows 路线借鉴了 [wlzh/dji-4g-vohive-mac](https://github.com/wlzh/dji-4g-vohive-mac) 对 DJI 私有 USB 身份和 Quectel AT 接口的说明，但没有部署 VoHive、Linux VM 或仓库内二进制。

## 模块基线

现场读取到的非敏感状态：

| 项目 | 结果 |
|---|---|
| 模块 | Baiwang QDC507 |
| 固件 | `QDC507GLEFM21_01.001.02.003` |
| SIM | READY |
| 访问网 | `CHN-CT`，LTE |
| 注册域 | `CREG=3`、`CGREG=5`、`CEREG=5` |
| 信号 | 多次约 27–29/31 |
| 初始 MBN | `ROW_Generic_3GPP`，AutoSel=1 |
| 初始 IMS | `AT+QCFG="ims"` 返回 `0,0` |
| LTE SMS 格式 | `AT+QCFG="ltesms/format"` 返回 GSM format (`1`) |
| 短信存储 | `ME`，容量 23 |

`CEREG/CGREG=5` 只证明 EPS/分组域漫游注册。CTExcel 入站是否可用仍必须看 IMS 会话和真实 `+CMTI/+CMT` 或存储新增。

## MBN 实验与回退

在用户明确授权后，曾选择固件内置 `VoLTE_OPNMKT_CT`：

1. 关闭 MBN AutoSel；
2. 选择 CT profile；
3. 重启模块。

CT profile 显示 selected/activated，但注册阶段出现 `+EMM ERROR: 19` 和 `+ESM ERROR: 1,33`，`CEREG` 长时间停留在搜索状态，IMS 仍为 `0,0`。脚本随后自动回退并验证：

- `ROW_Generic_3GPP` selected/activated；
- AutoSel=1；
- `CGREG/CEREG=5`；
- `CHN-CT` LTE 恢复。

因此，当前可用组合不是 CT MBN，而是 ROW MBN + 强制 IMS。

## ROW + IMS 成功路径

在用户再次明确授权后执行 `AT+QCFG="ims",1` 并重启。模块先显示 `1,0`，约一分钟后变为 `1,1`；同时保持 ROW MBN、AutoSel=1、中国电信 LTE 和 EPS 漫游注册。

`1,1` 在本项目中解释为：应用侧强制 IMS 已启用，IMS 会话可用。该设置保存在模块侧，应用启动时只读查询，不会重复写入。

如需恢复 MBN 默认 IMS 行为，必须先停止短信应用并获得明确授权，然后使用：

```powershell
pwsh -NoProfile -File .\tools\Set-DjiCarrierProfile.ps1 -Mode RestoreIms
```

当前不要执行恢复，因为 CTExcel 入站正依赖已验证的 `1,1` 状态。

## 入站短信验收

使用隐私安全探针完成以下闭环：

1. 打开 AT 口并设置 TEXT 模式、`CSDH=1`、`CPMS=ME`、`CNMI=2,1`；
2. 立即确认 IMS=`1,1`、`CEREG` 已注册；
3. 用户从外部触发 CTExcel 短信/验证码；
4. 观察到 `+CMTI: "ME",index` 或 `ME` 计数增加；
5. `CMGR` 返回完整 11 字段头、DCS=8 和有效 UCS2 十六进制正文。

本轮 `ME` 计数从 0 增加到 3；用户明确触发的后一次验证码使计数从 2 增加到 3，新索引可读取。探针从不输出发件号码、验证码或非预期正文，也没有删除模块内的三条短信。

这已经证明 CTExcel → 中国电信漫游 → IMS → QDC507 → AT/`ME` 的入站链路成立。

## 应用适配

DJI 版本保留旧工具的单串口线程、UTF-8 JSONL 归档、验证码识别、Web 和 Telegram 功能，并增加：

- 识别 `Quectel USB AT Port` 与 DJI/Quectel USB 身份；
- QDC507 使用 `ME`，SIM7600 继续使用 `SM`；
- `+CMTI` 接受任意合法存储名并携带 storage/index；
- QDC507 DCS=8 的 UCS2 正文回归测试；
- 每 5 秒只读更新 IMS 会话状态，只有 EPS + IMS 同时成立才显示“IMS 短信在线”；
- QDC507 初始化不写 `CGSMS=2`，也不写 IMS、MBN、USB 身份或数据连接状态；
- WWAN 检测限制在 Windows `Net` 类，未安装的 DJI 接口不会被误报为启用网卡。

应用首次真实启动已按设计完成：读取模块中的现存短信，逐条写入有效 JSONL 后删除模块原件。停止应用后再次独立读取模块，确认 IMS/EPS 仍在线且存储已清理。随后又以 `pythonw`、绝对 `app.py` 路径和临时端口验证无窗口启动：应用通过动态发现连接 AT 端口、识别 `dji_qdc507`、报告短信就绪，并在测试后停止和释放端口。启动脚本的既有实例判断也已收紧，旧 CTExcel/SIM7600 服务会被判定为端口冲突，不会被误认成 DJI 版。

经用户授权完成生产切换后，旧 SIM7600 服务停止，DJI 服务使用默认本地端口运行；没有创建启动文件夹、Run 注册表或计划任务。私人 Telegram 配置和去重状态在停止旧实例后迁移，并把已有归档 ID 纳入历史基线，避免迁移时补发旧短信。Bot API 与无隐私测试消息均通过，应用日志没有启动失败或 traceback。

生产实例持续运行期间，用户触发的新验证码成为独立入站记录，逻辑消息与原始归档一致；模块原件在落盘后被清理，Telegram 只推送新增逻辑短信且无通信错误。该结果完成了实时 `+CMTI → CMGR → 先归档 → 删除模块原件 → Web/API → Telegram` 链路验收。

用户确认费用并授权后，应用只调用一次 `888` 余额查询。模块返回真实 `+CMGS`，没有 `+CMS/+CME ERROR`；官方回复的多个物理分段被还原为一条逻辑消息。严格解析器只从已验证的“您当前余额为 £金额”字段取值，以 `source=sms`、`carrier=ctexcel` 写入 `state.json`。浏览器余额卡片与 Telegram 推送均通过验收，模块原件在归档后清理。

后续运维加固在不触发短信的前提下完成：Telegram Bot 启动时注册 `/status`、`/balance`、`/querybalance`、`/history`、`/send`、`/help` 共 6 项常用菜单命令，帮助标题统一为 DJI QDC507。重复的 `/test` 只保留为 `/querybalance` 的隐藏兼容别名；`/balance` 只读本地余额，手动校准改为不占菜单的 `/setbalance 金额`。模块 `ME` 达到 18/23 时预警、21/23 时紧急告警，降回正常区间后通知恢复，且同一级别只提醒一次。用户明确选择不配置 Windows 开机启动，服务继续通过 `启动短信工具.bat` 手动启动。

## 工具与安全用法

只读状态：

```powershell
pwsh -NoProfile -File .\tools\Read-DjiModemState.ps1
```

查看 profile/IMS 状态（只读模式）：

```powershell
pwsh -NoProfile -File .\tools\Set-DjiCarrierProfile.ps1 -Mode Status
```

隐私安全查看存储元数据：

```powershell
pwsh -NoProfile -File .\tools\Inspect-DjiStoredSms.ps1
```

任何 `ApplyCt`、`RollbackRow`、`ForceIms`、`RestoreIms` 模式都会改变模块配置或重启模块，必须先停止应用、确认 AT 口和当前状态，并获得用户明确授权。

## 验收结论与边界

- CTExcel 入站、出站 `+CMGS`、官方回复、余额自动解析、Web 显示和 Telegram 推送均已实测可用；
- 出站现场证据来自 `888` 的 `BAL` 服务动作。任意其他号码仍沿用相同 `CMGS` 路径，但不会为了重复证明而自动产生测试短信；
- 软件只能验证 `+CMGS` 和余额回复，无法独立读取运营商计费记录。本次免费结论来自用户对当前 `888` 服务的确认。
