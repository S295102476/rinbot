# RinBot 新电脑使用说明

这是 Windows x64 开发工作台迁移包，包含最新代码、未提交修改、Git 仓库、私人配置、SFTP、VS Code、Git、Node.js 和活跃扩展。全部开发使用唯一 `workspace`：同一份代码既通过 SFTP 上传服务器，也提交 GitHub。原服务器上的 Bot 和其他服务继续运行。

## 最快开始

1. 推荐将整个压缩包解压到新电脑 `E:\qqbot`；没有 E 盘时可用 `D:\qqbot` 或其他本机短路径。
2. 进入解压后的 `QQBot-日期时间` 目录，双击 `Verify-Package.cmd`，确认传输完整。
3. 双击 `Open-QQBot.cmd`，用随包 VS Code 打开根 `workspace`。
4. 首次保存前检查 SFTP 连接和远端目录；启用 `uploadOnSave` 时保存即上传。
5. 编辑源码、使用 SFTP，并在同一个目录的源代码管理中提交 GitHub。

启动入口、Git 与 SFTP 的本地 `context` 使用相对路径，无需固定 E 盘。源码实际位于 `E:\qqbot\QQBot-日期时间\workspace`，改用其他盘符也一样可用。请保留完整包的目录结构。

编辑和 SFTP 无需先安装 Python。SFTP 需要网络，GitHub 或账号扩展首次使用可能需要重新登录。包内 Git、Node.js 只加入这个编辑器进程的 `PATH`，不会修改系统全局环境变量。

## 只维护一份代码

GitHub 和服务器上传都从 `workspace` 使用，不再有第二个公开工作目录。源码修改一次即可。根 `.git` 保留当前公开仓库历史和 GitHub 远端；私人 `config.yaml`、`.env`、SFTP、运行数据和可选 Core 由 `.gitignore` 隔离，保留在本地但不提交。

提交前检查 VS Code 暂存差异，不要用 `git add -f` 强制提交被忽略的私人文件。`gsuid_core_app` 存在时保留完整源码、数据和它自己的 Git，迁移副本里仅禁用 Core 的独立推送，主仓库 GitHub 推送正常。

请通过 `Open-QQBot.cmd` 打开根 `workspace`，让根 `.vscode/sftp.json` 的相对 `context` 正常对应 Bot 和 Core。SFTP 的忽略规则保持原样；被忽略的工具、文档或其他文件按需手动上传。

## 可选：本地 Python 与前端开发

只编辑或上传时不需要这一步。要运行 Python 或测试，先安装 Python 3.11 或更新版本，再双击 `Restore-Dev.cmd`，或在包目录的 PowerShell 执行：

```powershell
.\Restore-Dev.ps1
```

指定解释器并同时安装控制台开发依赖：

```powershell
.\Restore-Dev.ps1 -Python 'C:\Python311\python.exe' -Console
```

默认优先 `py -3.11`，否则检查系统 `python`。脚本创建 `workspace/qq-bot-py/.venv`，使用项目 `requirements.txt` 和存在时的包根 `python-requirements.lock.txt` 安装依赖，通常需要网络。已有可用环境会复用；已有失效环境请自行重命名后重试。在 VS Code 选择 `workspace/qq-bot-py/.venv/Scripts/python.exe`。

`-Console` 用包内 Node.js 执行 `npm ci`。已构建的 `console/dist` 已保留。脚本不启动 Bot、不恢复 Core 环境、不安装 Playwright 浏览器。完整本地运行还需要另行核对数据库、Redis、MinIO、QQ 客户端、代理和棋类引擎；服务器编辑和上传不依赖这些本地服务。

## 保管与校验

包内含 SFTP 密码、模型密钥和私人数据，只用于自己的 U 盘转移，不上传 GitHub 或公开分享。登录缓存、Windows 凭据库和 SSH 私钥不包含；账号需要重新登录时按提示操作。

`manifest.json` 记录版本和打包范围，`SHA256SUMS.csv` 记录每个文件的 SHA256。首次使用前验证；使用后代码、Git 和用户配置会改变，再次验证出现变化是正常的。保留目录结构和原电脑副本，直到新电脑编辑、SFTP 与 Git 操作确认正常。

建议至少 16 GB 的 NTFS 或 exFAT U 盘。包中包含 GBVSR 和存在时的 Core 本地资源，压缩包可能超过 FAT32 单文件 4 GB 限制。
