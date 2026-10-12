# RinBot 开发电脑 U 盘迁移

当前项目使用一个工作目录：在同一份源码上编辑、通过 SFTP 上传服务器，再提交 GitHub。私人 `config.yaml`、SFTP 配置、运行数据和可选 Core 保存在本地，由 `.gitignore` 排除。迁移包只有 `workspace`，不再创建需要手动同步的公开工作目录。

这是 Windows x64 开发工作台迁移，服务器上的 Bot、QQ 客户端、MySQL、Redis、MinIO 和 Core 服务继续运行。

## 在当前电脑打包

先保存编辑器文件，并退出本机 Bot 或 Core 进程。SQLite 数据库及其 `-wal`、`-shm` 文件需要在写入停止后复制；脚本会核对复制前后的数据库哈希。远端服务器不需要停止。

在唯一工作目录根运行：

```powershell
.\qq-bot-py\tools\migration\New-UsbPackage.ps1 -Destination 'E:\qqbot-migration'
```

也可以指定其他工作目录或 U 盘位置：

```powershell
.\qq-bot-py\tools\migration\New-UsbPackage.ps1 -ProjectRoot 'E:\qqbot' -Destination 'F:\QQBOT-Migration'
```

命令提示符入口为 `qq-bot-py\tools\migration\New-UsbPackage.cmd`，参数相同。旧的 `-PublicProjectRoot` 参数会明确报错，请直接使用唯一工作目录。

脚本保留源码、未提交修改、根仓库 Git 元数据、私人配置、全部本地业务资源和已构建的 `console/dist`。GBVSR 大图片、Core 源码及 Core 本地数据在源目录存在时一起复制。Core 是可选目录，不存在也能打包。根仓库继续使用 GitHub 远端；Core 的独立 Git 推送仅在迁移副本中禁用，避免把私人 Core 修改误推到上游。

排除 `.venv`、`node_modules`、日志、可重建缓存、`.agents`、`task-exports`、工作目录根的 `rinbot-*.zip*` 更新包，以及 Git 临时 `index.lock`。主仓库还存在 `index.lock` 时拒绝打包；脚本不删除原目录文件。

随包复制当前 VS Code、Git for Windows、Node.js 与全部活跃 VS Code 扩展；用户偏好只复制 `settings.json`、`keybindings.json` 和 `snippets`。不复制账号登录缓存、Windows 凭据库或个人 SSH 私钥。GitHub 和需要账号的扩展在新电脑可能需要重新登录。

脚本生成独立的新目录，不覆盖旧迁移包。结束前检查便携工具可运行、扩展清单一致、私人配置哈希一致，并生成每个文件的 `SHA256SUMS.csv`。标有 `INCOMPLETE.txt` 的目录尚未完成，不要使用。

建议 U 盘至少 16 GB，使用 NTFS 或 exFAT；完整包可能超过 FAT32 的单文件 4 GB 限制。将新包放在工作目录之外，避免递归复制。打包目录可整体转移，也可以另行压缩为支持 ZIP64 的 ZIP 文件。

## 包中内容

```text
QQBot-日期时间/
  Open-QQBot.cmd
  Verify-Package.cmd
  Verify-Package.ps1
  Restore-Dev.cmd
  Restore-Dev.ps1
  README.md
  manifest.json
  SHA256SUMS.csv
  python-requirements.lock.txt  # 源电脑存在可用 .venv 时生成
  workspace/
    .git/
    .vscode/                  # 私人 SFTP 配置
    qq-bot-py/
    gsuid_core_app/            # 可选私人 Core 副本
  apps/
    vscode/
    git/
    node/
  extensions/
  vscode-user/User/
```

## 新电脑继续工作

推荐把整个迁移包解压到新电脑的 `E:\qqbot`，以便保留原来的盘符习惯；实际源码在 `E:\qqbot\QQBot-日期时间\workspace`。没有 E 盘也可以使用 `D:\qqbot` 或其他本机短路径。启动入口、Git 和 SFTP 的本地 `context` 使用相对路径，不依赖固定盘符。不要把 `workspace` 单独移走，要保留整个包的目录结构。

先双击 `Verify-Package.cmd`，验证传输完整；再双击 `Open-QQBot.cmd`。入口用随包 VS Code 打开根 `workspace`，让该编辑器进程使用包内 Git 和 Node.js，不更改系统全局环境变量。编辑和 SFTP 不需要先安装 Python。

根 `.vscode/sftp.json` 保持原来的连接与相对 `context`；打开根目录可同时管理 Bot 和存在时的 Core。第一次保存前核对远端目录与连接，配置可能启用了 `uploadOnSave`。SFTP 需要网络，GitHub 推送可能需要登录一次。SFTP 自己的忽略规则仍生效，例如被忽略的工具文件需要手动上传。

GitHub 也使用这个根 `workspace`。在 VS Code 源代码管理中检查差异、暂存源码、提交并推送即可，同一份修改无需复制到另一个目录。私人配置和运行数据应始终保持被 Git 忽略，不要用 `git add -f` 强制提交。Core 的私人修改不属于主仓库发布内容。

整个迁移包含 SFTP 密码、模型密钥和私人数据，只用于自己的 U 盘转移，不上传 GitHub 或公开分享。账号登录缓存未迁移；以后使用 SSH 密钥认证时需单独处理密钥，并调整 SFTP 配置。

## 可选：恢复本地开发依赖

仅编辑代码和 SFTP 上传时跳过此步骤。需要运行 Python 或测试时，先安装 Python 3.11 或更新版本，再在包目录运行：

```powershell
.\Restore-Dev.ps1
```

指定解释器，同时安装控制台开发依赖：

```powershell
.\Restore-Dev.ps1 -Python 'C:\Python311\python.exe' -Console
```

脚本优先使用 `py -3.11`，否则检查系统 `python` 不低于 3.11。在 `workspace/qq-bot-py/.venv` 新建或复用可运行环境，同次 pip 安装使用项目 `requirements.txt` 和存在时的 `python-requirements.lock.txt`，通常需要网络。失效的旧环境会报错，需要自行重命名后重试。

`-Console` 用包内 Node.js 在 `workspace/qq-bot-py/console` 执行 `npm ci`。脚本不启动 Bot、不恢复 Core 环境、不安装 Playwright 浏览器。若要在新电脑运行完整 Bot，需要另外核对数据库、QQ 连接、对象存储、代理、浏览器和棋类引擎；这不影响继续编辑和向现有服务器上传。
