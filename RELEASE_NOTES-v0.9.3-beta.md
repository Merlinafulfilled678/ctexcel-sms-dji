# v0.9.3-beta — Public installer preview

## 中文

这是首个面向公开仓库的单 EXE 安装器预览版。

### 已包含

- 离线检查或安装固定版本的 Python 3.14、PowerShell 7 和 Python wheel 依赖。
- 显式公开文件白名单；不包含 `config.json`、短信存档、Telegram 状态、日志或私人迁移数据。
- 安装包资源 SHA-256、自检和疑似 Telegram Token 扫描；随 Release 提供 `SHA256SUMS.txt`。
- 设备可后置：未插 DJI QDC507 也能完成基础安装。
- 安装目录可编辑或浏览选择；默认优先 `D:\CTExcel-SMS-DJI`，无合适 D 盘时自动回退到当前用户本地应用目录。
- 安装器声明 Windows DPI 感知，并在全部控件创建后统一按 DPI 缩放，避免高分辨率/高缩放屏幕上的位图模糊、留白异常和控件重叠。
- 本地首次设置页面；Telegram Token 只写不回显，配置接口带 CSRF 校验。
- 修复安装保留配置、短信存档和状态；不创建 Windows 开机启动。
- 卸载入口默认备份私人数据，二次确认后也可全部删除。

### 重要边界

- 公开 EXE **不包含 Quectel/DJI 驱动**。请从 Quectel、设备供应商或 Windows Update 等获授权来源取得 AT 串口驱动。
- 外层 EXE 暂无商业代码签名，Windows 可能显示“未知发布者”；请核对 `SHA256SUMS.txt`。
- 安装和配置不会发送短信或启用蜂窝数据。
- 首次真正启动并连接模块后，程序会先归档模块中的短信，确认落盘后删除模块原件。
- 发送普通短信可能收费；向 `888` 发送 `BAL` 是否免费以 CTExcel 实际账单为准。

### Beta 验收状态

- 已通过离线 Python 回归、PowerShell 语法、C# 编译、公开白名单、秘密扫描和 EXE 双重自检。
- 尚需在干净 Windows 11 电脑上完成首次安装、修复、卸载和 QDC507 实机验收后再发布 v1.0.0。

## English

This is the first single-file installer preview for the public repository.

It bundles the hash-pinned official Python and PowerShell installers plus the offline wheelhouse, while the project payload is created from an explicit public allowlist. Private configuration, SMS archives, Telegram state, logs, and migration data are excluded. The installer can complete its base installation without a connected device, provides a local write-only-token setup flow, preserves user data during repair, creates no Windows autostart entry, and includes a data-preserving uninstaller.

The installation directory is editable or selectable with a folder browser. It defaults to `D:\CTExcel-SMS-DJI` on a suitable fixed NTFS `D:` drive and otherwise falls back to the current user's local application directory.

The installer now declares Windows DPI awareness and scales all WinForms controls together after layout initialization, preventing bitmap blur, excessive blank space, and overlapping controls on high-resolution or high-scaling displays.

The public EXE does **not** redistribute the Quectel/DJI driver. Obtain the AT-port driver from an authorized source. The outer EXE is currently unsigned, so verify it against `SHA256SUMS.txt`. Offline regression, PowerShell parsing, C# compilation, payload scanning, and executable self-tests pass; clean Windows 11 and QDC507 acceptance remain required before v1.0.0.
