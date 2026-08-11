# Quectel AT 串口驱动

本公开仓库不分发 Quectel/DJI 驱动二进制。请通过 Quectel 官方渠道、设备供应商或 Windows Update 获取与你的系统和模块匹配的驱动，并自行确认许可条款。

本项目只需要把 DJI QDC507 的 `USB\VID_2CA3&PID_4006&MI_02` 绑定为 `Quectel USB AT Port`。不要批量处理整个 USB 复合设备，也不要为本工具安装 WWAN、NDIS 或蜂窝数据网卡。

如果你依法取得了经过验证的私有离线驱动包，可将其放在：

```text
drivers/Quectel-Ports-30.0.65.2/
```

该目录已被 `.gitignore` 排除，不会进入公开提交。`tools/Bind-DjiAtPort.ps1` 仅适用于项目中预期的已核验文件结构；安装驱动会修改系统设备绑定，需要管理员权限。日常检查请优先使用 `-Mode ListOnly`。
