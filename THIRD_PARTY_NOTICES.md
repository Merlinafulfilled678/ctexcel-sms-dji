# 第三方声明

本项目自有源码采用 MIT License。下列项目、软件和商标不因此改用 MIT License。

## 参考资料

- [wlzh/dji-4g-vohive-mac](https://github.com/wlzh/dji-4g-vohive-mac) 提供 DJI 私有 USB 身份、Quectel AT 接口及 VoHive/Linux 部署经验，其文档声明采用 CC BY 4.0。本项目保留链接和致谢，没有复制或分发 VoHive 二进制。

## Python 依赖

- Flask、Werkzeug、Jinja2、itsdangerous、click、MarkupSafe：各项目自己的 BSD/MIT 类许可证。
- pyserial：BSD 许可证。
- requests：Apache License 2.0；其传递依赖使用各自许可证。

准确版本见 `requirements.txt` 与 `installer/assets/wheels/requirements-lock.txt`。使用或再分发 wheel 时，应保留 wheel 内的许可证元数据。

## 可选离线载荷

- Python 安装器：Python Software Foundation License。
- PowerShell 安装器：MIT License 及其第三方声明。
- Quectel/DJI 驱动：权利归对应厂商所有。

公开仓库不包含上述安装器、wheel、驱动或生成的迁移 EXE。构建者必须从授权来源自行取得载荷并遵守对应条款。

DJI、Quectel、CTExcel、Telegram、Python、PowerShell 及其他名称和商标归各自权利人所有。
