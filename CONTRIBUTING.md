# 贡献指南

欢迎提交问题和改进。开始前请先阅读 [README.md](README.md) 的安全边界。

## 开发约束

- 运行环境是 Windows，脚本使用 PowerShell 7 原生语法。
- 不引入 Flask、pyserial、requests 之外的新运行时依赖。
- 不写死 COM 号、Windows 设备实例 ID、`oemXX.inf` 或本机路径。
- 不发出 `NETOPEN`、`CGACT` 等移动数据业务指令。
- QDC507 应用路径只读 IMS/MBN 状态；真实配置变更必须由用户明确授权。
- 任何真实发送路径都必须保留二次确认。
- 新短信必须先成功写入本地归档，再删除模块原件。
- 不提交第三方驱动、安装器二进制、私人运行数据或真实号码。

## 验证

提交前运行：

```powershell
python -m py_compile app.py modem_profile.py tg_bot.py carrier_profile.py diag_readonly.py
python test_carrier_logic.py
python test_long_sms_logic.py
python test_tg_logic.py
python test_dji_qdc507_logic.py
python test_migration_assets.py
python test_installer_assets.py
```

测试默认必须离线，不连接串口、不发送短信、不安装驱动、不改变网卡。需要真实硬件验收时，请在 PR 中清楚列出设备、SIM、授权范围、费用风险和回退方案，不要附带私人日志或短信正文。
