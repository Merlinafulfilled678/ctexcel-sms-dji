# DJI QDC507 短信工具迁移到新电脑

> 公开源码说明：仓库不包含私人迁移 EXE、Python/PowerShell 离线安装包、wheel 或 Quectel 驱动。下述单 EXE 路线只适用于维护者在本地依法取得并核验全部载荷后构建的私人迁移包；任何包含 `config.json`、短信归档或 Telegram 状态的产物都不得公开分享。

适用范围：把同一块已配置好的 DJI QDC507、同一张 CTExcel SIM 和本工程目录迁移到 Windows 11 x64 新电脑。

当前迁移包保留“仅短信、无 WWAN”边界，不安装移动数据网卡，不发测试短信，也不会自动修改模块的 MBN、IMS 或 USB 身份。

## 推荐流程：只迁移一个私人 EXE

真正迁出前，在旧电脑当前工程目录双击 `生成迁移安装包.bat`：

1. 构建器先验证官方 Python/PowerShell 载荷、14 个离线 wheel、Microsoft WHCP 驱动和当前私人数据文件；
2. 它确认 7597 上运行的确实是 `ctexcel + dji_qdc507`，只停止这个工程的精确 Python 进程；
3. 服务停止且端口释放后，才快照最新的 `config.json`、`archive.jsonl`、`state.json` 和 `tg_state.json`；
4. 输出 `dist\CTExcel-SMS-DJI-Migration-日期时间.exe`，并对成品做资源解包、哈希、数字签名和 ZIP 路径自检；
5. 构建结束后旧电脑服务保持停止。实际迁移前不要再启动它，否则 EXE 中的状态会落后。

把这个 EXE 私下复制到新电脑即可，不需要再复制工程文件夹。插入同一块 DJI QDC507，等待 USB 枚举后双击 EXE并允许管理员确认。安装器会依次：

- 检查 Windows x64、7597 端口、唯一 DJI `MI_02` 以及无 WWAN 安全状态；
- 检查 PowerShell 7 和 Python 3.14 x64，缺少时从 EXE 内置的官方文件静默安装到标准系统位置；
- 使用内置 wheel 完全离线安装锁定依赖，明确禁止 pip 联网；
- 校验并只绑定唯一 DJI `MI_02` 的 Quectel AT 驱动；
- 优先把短信工具安装到 NTFS 固定盘 `D:\CTExcel-SMS-DJI`，并为当前用户补齐归档写入权限；D 盘不满足条件时安装到当前用户本地应用目录；
- 运行 `Test-NewPcReadiness.ps1` 最终只读验收，只有 `FAIL=0` 才报告完成；
- 视情况创建桌面手动启动快捷方式，但不自动启动应用，也不创建任何开机启动项。

失败窗口会指出失败步骤、子进程退出码和 `%TEMP%\CTExcel-SMS-DJI-install-*.log`。修正原因后可直接重跑：已标记的目标安装会更新程序文件，同时保留新电脑上已有的四个私人配置/状态文件；若目标目录不是本安装器创建的，它会拒绝覆盖。

这个 EXE 嵌入真实 Telegram token、owner 绑定和短信历史，不能上传公共仓库、网盘公开链接或发给他人。由于当前没有商业代码签名证书，最外层自制 EXE 的 UAC 可能显示“未知发布者”；内嵌 Python、PowerShell 和 Quectel 驱动仍分别通过固定官方 SHA-256 与有效数字签名校验。

## 迁移包包含什么

- 路径无关的 Python/Flask 短信应用；
- 官方 Python 3.14.5 x64 安装器、PowerShell 7.6.4 x64 MSI；
- 三个顶层 Python 包及其 11 个传递依赖的完整离线 wheel 锁和哈希清单；
- 已核验 Microsoft WHCP 签名的 Quectel Ports `30.0.65.2` x64 驱动；
- 动态识别新电脑 DJI `MI_02` Instance ID 和 COM 号的工具；
- `tools/Test-NewPcReadiness.ps1` 只读验收脚本；
- 当前目录中的本地归档、余额状态、Telegram owner、update offset 和推送去重状态。

模块侧的 `ROW_Generic_3GPP`、MBN AutoSel 和 IMS 强制设置保存在物理模块中，会随模块迁移。应用只读 IMS，不会在新电脑首次启动时重复修改。

下面的一至七节保留为手工迁移和故障回退流程；正常迁移优先使用上面的单 EXE。

## 一、旧电脑迁出前

1. 不要在复制期间继续运行短信工具。先通过 `/api/status` 核对监听 7597 的确实是 `ctexcel + dji_qdc507`，再停止该精确 PID。
2. 确认 7597 已释放，且 `pythonw` 实例不再占用 AT 串口。
3. 完整复制整个工程目录，不要只复制 `.py` 文件。
4. 必须包含：
   - `archive.jsonl`
   - `state.json`
   - `tg_state.json`
   - `config.json`
   - `drivers/`
   - `static/`
   - `tools/`
5. `config.json` 含 Telegram token 和 owner 标识；迁移包只能私下保存，不得上传公共仓库或发给他人。
6. 旧电脑上的服务必须保持停止。两个电脑同时使用同一个 Bot token 做 long polling 会争抢 Telegram update。

如果迁移期间模块收到短信，新电脑首次正常启动会读取模块 `ME`，先写入本地归档成功，再删除模块原件。

## 二、新电脑安装基础环境

安装以下 x64 环境，并确保命令进入 PATH：

- Windows 11 x64；
- PowerShell 7（`pwsh`）；
- Python 3.14，包含 `python.exe` 和 `pythonw.exe`。

在工程目录打开 PowerShell 7：

```powershell
python --version
pwsh --version
python -m pip install --requirement .\requirements.txt
```

生产环境实测版本是 Python `3.14.5`、Flask `3.1.3`、pyserial `3.5`、requests `2.34.2`。

## 三、只安装 DJI AT 串口驱动

插入 DJI QDC507 并等待 Windows 完成枚举。首次迁移时，`MI_02` 可能还没有 COM 号，这是正常现象。

先在普通 PowerShell 7 中做只读候选检查：

```powershell
pwsh -NoProfile -File .\tools\Bind-DjiAtPort.ps1 -Mode ListOnly
```

脚本必须同时满足以下条件才会继续：

- 只找到一个连接的 `VID_2CA3&PID_4006&MI_02`；
- 驱动文件 SHA-256 全部匹配；
- `qcser.cat` 为有效 Microsoft WHCP 签名；
- 只找到一个 Quectel Incorporated `30.0.65.2` 的 `Quectel USB AT Port` 候选。

确认输出为 `Result=LIST_ONLY_OK` 后，以管理员身份打开 PowerShell 7，在工程目录运行：

```powershell
pwsh -NoProfile -File .\tools\Bind-DjiAtPort.ps1 -Mode Install
```

`Install` 只处理精确的 `MI_02`。不要给其他 MI 接口绑定驱动，也不要安装 WWAN/NDIS 网卡。若提示需要重启或重新枚举，先拔插模块；没有必要恢复 SIM7600 的 USB PID，也不要改 DJI 原始 VID/PID。

驱动安装属于新电脑首次部署中唯一需要管理员权限的步骤。脚本的 `ListOnly` 路径已在当前电脑用随包驱动实测；`Install` 路径仍应在第一次新电脑迁移时按本说明现场验收，不把“脚本可运行”当成“驱动已成功绑定”。

## 四、运行只读迁移验收

在启动短信应用前运行：

```powershell
pwsh -NoProfile -File .\tools\Test-NewPcReadiness.ps1
```

它只读取环境并检查：

- Windows/PowerShell/Python 版本；
- Python 依赖；
- 项目数据文件；
- 驱动哈希和签名；
- DJI `MI_02`、当前动态 COM 号和已安装驱动版本；
- Windows 中没有 DJI/Quectel/SimTech 蜂窝网卡；
- 7597 空闲或已由正确的 DJI 服务占用；
- Telegram 配置存在，且只检测代理端口是否可连接，不显示 token、owner 或短信内容。

只有 `SUMMARY` 中 `FAIL=0` 才进入下一步。`WARN` 允许 Web/SMS 工作，但应阅读具体提示，例如 Telegram 本机代理尚未启动。

仅检查软件文件、暂时不插模块时可以使用：

```powershell
pwsh -NoProfile -File .\tools\Test-NewPcReadiness.ps1 -SkipDevice
```

## 五、启动和验收

1. 确保旧电脑服务仍处于停止状态。
2. 如果需要 Telegram，先启动新电脑上 `config.json` 指定的本机代理；代理地址不同则只在新电脑私下修改配置，不要在聊天或日志中粘贴 token。
3. 双击 `启动短信工具.bat`。
4. 浏览器打开 `http://127.0.0.1:7597/` 后核对：
   - modem=`dji_qdc507`
   - connected=true
   - sms_ready=true
   - IMS registered=true
   - WWAN absent
5. 首次只做状态和历史检查，不主动发送短信。
6. 若需最终入站验收，先记录归档数量，再由用户自行触发一条验证码；不要由脚本自动发送测试短信。

COM 号在新电脑上变化没有关系，应用和更新后的 PowerShell 工具都会动态识别 `Quectel USB AT Port`。仍可显式传入 `-PortName COMx`，但不再依赖当前电脑的 COM14。

## 六、常见问题

### Telegram 不工作但网页和短信正常

复制的 `config.json` 使用本机代理。新电脑没有启动相同代理，或代理端口不同，Telegram 会退避重试；Web、串口收取和本地归档不受影响。

### `MI_02` 存在但没有 COM 号

说明 Quectel AT 驱动尚未绑定。运行 `Bind-DjiAtPort.ps1 -Mode ListOnly`，核对后再在管理员 PowerShell 7 中执行 `-Mode Install`。

### IMS 不再是 registered

先停止应用并运行只读状态脚本：

```powershell
pwsh -NoProfile -File .\tools\Read-DjiModemState.ps1
```

不要直接执行 `ForceIms`、切换 MBN 或改 USB 身份。模块配置变更仍必须另行确认并保留回退路径。

### 7597 被占用

启动脚本会拒绝把其他服务误认为 DJI 版。先查清精确 PID 和程序身份，不要批量结束所有 Python 进程。

## 七、回退边界

- 新电脑迁移失败时，旧电脑目录和历史数据保持原样；
- 不要同时重新启动新旧两个 Telegram Bot 实例；
- 不自动删除新电脑 Driver Store 中的驱动，避免误伤其他 Quectel 设备；
- 如需回退驱动，先在设备管理器核对精确 `MI_02` 和当前 `oemXX.inf`，再单独制定操作；
- 不删除 `archive.jsonl`，模块清空也不能替代本地归档备份。
