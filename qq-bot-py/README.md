# RinBot 开发说明

项目介绍、截图与 Docker 部署统一放在[仓库首页](../README.md)。本文件说明源码开发、测试、提交和私人工作区迁移，避免维护两份功能介绍。

## 一份工作区完成开发和提交

仓库根目录包含 `.git/`、根 README、部署文件与 `qq-bot-py/` 源码。当前开发目录是 `E:\qqbot`；日常只修改这里，SFTP 上传和 GitHub 提交使用同一份文件。原来的独立公开目录仅作为旧备份，不再同步修改。

```text
qqbot/
  .git/                      GitHub 仓库历史
  .vscode/sftp.json           私人 SFTP 配置，Git 忽略
  README.md                  对外介绍
  docs/                      对外部署与配置说明
  qq-bot-py/
    bot.py / plugins/        后端源码
    persona/                 人格和公开示例
    config.example.yaml      公开配置模板
    config.yaml / .env       私人运行配置，Git 忽略
    console/                 React 控制台
    tests/                   离线测试
    tools/                   运维与迁移工具
    data/                    本地资源和运行数据
```

私人配置、密钥、SFTP、日志、数据库、依赖目录、控制台构建结果、迁移包、Core 本地目录和大图片缓存由 `.gitignore` 排除。必要的小资源与示例配置可以提交。编辑私人 `config.yaml` 只影响自己的运行；需要让其他安装者了解新字段时，同一次修改补充安全的 `config.example.yaml` 和文档，不复制自己的凭据。

在根目录审阅更改再提交：

```powershell
cd E:\qqbot
git status --short
git diff
git add README.md docs qq-bot-py
git diff --cached --stat
git diff --cached
git commit -m "Describe the change"
git push origin main
```

普通 `git add` 会尊重忽略规则；不要强制添加私人配置。SFTP 则可以上传 Git 忽略的运行配置，两者不需要两份源码。换电脑后 GitHub 凭据需要在新机登录一次，迁移包保留仓库与远端地址。

## 手动环境与后端

用户安装优先参考[Docker 部署](../docs/deployment.md)。手动开发需要 Python 3.11+、MySQL、Redis 与 MinIO；Linux 生产环境可沿用已有虚拟环境与服务。

在 `qq-bot-py/` 运行以下命令，先创建自己的配置，勿覆盖已有文件：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

配置参考为 `config.example.yaml`、`.env.example` 与[配置说明](../docs/configuration.md)。根据本机服务填写数据库、Redis、MinIO、模型、QQ 与管理认证；服务凭据和允许群都使用自己的值。示例空凭据不能直接运行。手动启动使用 `.\.venv\Scripts\python.exe bot.py`；Linux 在此目录运行 `bash start.sh`。

公开模板的聊天、摘要、情绪与表情分类共享自己配置的主模型；向量、专家与开发功能默认关闭。新版记忆的备份、迁移和测试群开启步骤见[Agent 升级](docs/agent-upgrade.md)。该文档中的 AG 分工是可选示例，不是基础部署依赖。

## 控制台开发

使用 Node.js，在 `console/` 执行：

```powershell
npm ci
npm run dev
```

开发服务器需要可用的后端管理员接口。上传服务器前执行 `npm run build`，将 `console/dist/` 通过 SFTP 更新；Docker 镜像会自动构建控制台。源码提交包括前端源文件与 `package-lock.json`，构建目录保持本地。

外观设置和自定义壁纸保存在访问站点的浏览器中，换电脑后重新选择即可。默认三种星空背景随前端构建提供。

## 检查

测试夹具读取公开模板，写入临时的假配置并使用测试账号，不读取自己的运行配置。先安装开发依赖再运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
cd console
npm run build
npm test
```

接口、对象存储与浏览器测试可以使用替身；它们不代表真实 QQ 或模型已经连通。上线仍需验证真实 @ 回复、图片、数据持久性与新功能的权限边界。仓库 GitHub 检查包含后端测试、前端构建、部署配置与敏感信息扫描。

## U 盘搬运私人工作区

从仓库根目录运行下面的打包工具。退出本地机器人／Core 进程，完成正在进行的 Git 操作后再打包；输出目录位于工作区之外：

```powershell
.\qq-bot-py\tools\migration\New-UsbPackage.ps1 -ProjectRoot 'E:\qqbot' -Destination 'E:\qqbot-migration'
```

包中保留这一份工作区、Git 历史、SFTP、私人配置、可携带的 VS Code／Git／Node.js 和当前扩展，依赖目录与临时输出不重复打包。使用生成的包内说明和校验文件搬运；包根的 `Open-QQBot.cmd` 可在新 Windows x64 电脑直接打开编辑工作区。只编辑和 SFTP 不需要恢复 Python；要在新机执行后端检查或运行时，再使用包中的环境恢复脚本。

迁移包包含私人配置，供自己用 U 盘搬运，不上传 GitHub。远端服务器的 MySQL、Redis、MinIO 和 QQ 客户端状态不在本地工作区；继续连接原服务器时保留原服务，新服务器迁移则另做数据备份与恢复。完整包结构、校验和新机步骤见[迁移工具说明](tools/migration/README.md)。
