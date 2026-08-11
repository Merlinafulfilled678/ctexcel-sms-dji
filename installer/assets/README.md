# 单 EXE 安装器的离线载荷

本目录只用于构建私人迁移安装包，不会被复制进短信工具的安装目录。

公开仓库只保留版本、来源、哈希和锁文件；`.exe`、`.msi` 与 `.whl` 二进制均被 `.gitignore` 排除。构建者必须自行从官方来源取得载荷并完成哈希与签名验证，不得从不可信镜像补齐。

固定载荷：

- Python 3.14.5 Windows x64 传统安装器
  - 来源：`https://www.python.org/ftp/python/3.14.5/python-3.14.5-amd64.exe`
  - SHA-256：`F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D`
  - 构建时必须验证签名者包含 `Python Software Foundation`
- PowerShell 7.6.4 Windows x64 MSI
  - 来源：`https://github.com/PowerShell/PowerShell/releases/download/v7.6.4/PowerShell-7.6.4-win-x64.msi`
  - SHA-256：`D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0`
  - 构建时必须验证签名者包含 `Microsoft Corporation`
- `wheels/`：Python 3.14 x64 离线 wheel 及完整版本锁。

最终 EXE 会嵌入上述载荷、驱动和构建瞬间的 `config.json`、短信归档及 Telegram 去重状态。它含私密凭据和历史，只能私下保存，不能上传或分享。

本工程没有商业代码签名证书，所以自制的最外层迁移 EXE 会显示“未知发布者”；内嵌的 Python、PowerShell 和 Quectel 驱动仍分别校验官方哈希及有效签名。
