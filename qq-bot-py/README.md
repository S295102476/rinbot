# RinBot 开发说明

项目介绍、功能和部署入口统一维护在[根 README](../README.md)，本文件记录开发与手动安装方式。默认部署建议使用 Docker Compose；手动运行需要自行准备 MySQL、Redis 和 MinIO。

## 目录与运行环境

| 目录 / 文件 | 用途 |
| --- | --- |
| `bot.py`、`runtime_config.py` | 启动、配置校验与功能加载 |
| `plugins/` | 机器人、Agent、控制台后端与小游戏 |
| `console/` | React / TypeScript 管理控制台 |
| `persona/` | 公开角色种子与公共知识 |
| `data/` | 随代码提供的小资源；运行数据不提交 |
| `tests/`、`tools/` | 后端测试与维护工具 |

Python 使用 **3.11**，前端构建使用 **Node.js 22** 和 npm。生产配置依照 `config.example.yaml`，前端依赖使用 `package-lock.json`，Python 使用 `requirements.lock.txt`。

## 手动安装

先准备可连接的 MySQL、Redis 与 MinIO，创建独立数据库和访问账号。以下命令从本目录运行：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt -r requirements-dev.txt
python -m playwright install chromium
cp config.example.yaml config.yaml
cp .env.example .env
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活环境，复制文件用 `Copy-Item`。系统还需要中文字体；Linux 可安装 Noto CJK 字体包，浏览器系统依赖可用 `python -m playwright install --with-deps chromium` 安装。

编辑 `config.yaml`：填写模型完整请求地址、密钥、名称、允许群、管理员，以及本机数据库 / Redis / MinIO 连接信息。模板中的 `mysql`、`redis`、`minio` 是 Compose 服务名，手动安装时改为实际地址。

在 `.env` 中设置 OneBot Token 和控制台认证变量。使用 `python tools/console_password.py` 按提示生成密码哈希并粘贴到 `AGENT_CONSOLE_PASSWORD_HASH`，OneBot Token 使用随机值；控制台会话由 Redis 存储。准确变量名以 `.env.example` 为准。

构建控制台并启动：

```bash
cd console
npm ci
npm run build
cd ..
python bot.py
```

服务监听、控制台和 OneBot 接入见[部署说明](../docs/deployment.md)。不要将自己的 `config.yaml`、`.env` 或数据库提交到仓库。

## 前端开发

在 `console/` 执行 `npm run dev`，开发地址为 `http://127.0.0.1:5173/admin/`。Vite 将管理 API 代理给本地 Bot；需要真正操作数据时先运行后端并完成本地配置。Playwright 用例使用受控的接口模拟，不能代替真实服务接入验收。

```bash
cd console
npm ci
npm run dev
```

## 验证改动

后端命令从 `qq-bot-py/` 执行：

```bash
python -m pytest tests -q
python tools/check_idioms.py
```

前端命令从 `qq-bot-py/console/` 执行：

```bash
npm run build
npx playwright install chromium
npm test
```

公开文件检查从仓库根目录执行：

```bash
python tools/check_public_release.py
```

测试优先使用临时数据库、内存存储和模拟服务，不连接原部署者的服务器。修改可选功能时至少验证开关关闭且无凭据时不影响默认启动。

## 修改人设

远坂凛兼容 `persona/*.md` 布局，艾蕾与伊什塔尔使用 `persona/profiles/<id>/`，公共知识放在 `persona/shared/`。角色注册信息位于 `persona/registry.yaml`。管理员可通过控制台编辑和回滚文档，详细目录规则见[人格说明](persona/profiles/README.md)。

Docker 中实际人设来自持久化卷，源码中的内容是首次初始化种子；修改种子不会自动覆盖线上人设。发布前确认私人关系档案未进入提交。

## 维护工具

棋类引擎、GBVSR 数据与 Core 是可选能力，使用方法见[扩展文档](../docs/extensions.md)。`tools/` 中历史数据抓取或诊断工具可能产生文件、联网或连接数据库，执行前阅读对应脚本参数与用途，不要把生成的数据误提交到公开仓库。
