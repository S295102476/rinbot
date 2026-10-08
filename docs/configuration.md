# 配置参考

Docker 初始化生成 `runtime/config.yaml`、`runtime/bot.env` 和根 `.env`。日常在控制台调整群策略、人格、表情与记忆；服务连接和可选能力在本地配置中修改。公开模板位于 [`qq-bot-py/config.example.yaml`](../qq-bot-py/config.example.yaml)，只包含空凭据与公共默认值。

## 基础配置

| 配置 | 用途 |
| --- | --- |
| `allowed_groups` | 允许使用机器人的群号列表，初始化必须填写至少一个 |
| `ai.api_url` | 完整 Chat Completions 请求 URL，包含 `/chat/completions` |
| `ai.api_key` | 自己的模型服务密钥 |
| `ai.model` | 服务支持的模型名 |
| `ai.protocol` | 明确指定 `chat_completions`（默认）或 `gemini_native` |
| `ai.proxy` | 模型请求代理，默认空 |
| `agent.group.default_mode` / `only_at` | 默认 `at` / `true`，新群仅 @ 回复 |
| `database` / `redis` / `meme.minio` | 数据库、缓存与对象存储连接，由初始化器生成 |

`ai.protocol` 由接口文档决定，不通过模型名猜测。普通 Chat Completions 服务可以使用各种兼容模型，API 地址和模型名均需由提供者确认。聊天、基础翻译、记忆摘要默认共用这组模型；接口支持图片输入时才能识图。

识图可以使用 `ai.vision` 覆盖主模型的连接与模型字段；没有覆盖时复用主配置。不要将文本模型配置为图片识别模型后期待自动拥有多模态能力。

## 功能开关

| `features` 字段 | 默认 | 说明 |
| --- | --- | --- |
| `chat`、`console`、`persona` | 开 | 聊天、Web 控制台、人格和值班 |
| `meme`、`sign_in`、`minigames` | 开 | 表情、签到、六种小游戏 |
| `web_search` | 关 | 联网搜索，需额外搜索后端 |
| `image_gen`、`nai` | 关 | 图片生成 / 编辑与 NovelAI |
| `pixiv`、`link_parser` | 关 | Pixiv 与链接解析 |
| `gbvsr`、`gsuid` | 关 | GBVSR 本地数据与 Core 桥接 |
| `board_engines` | 关 | 外部棋类引擎，仍需逐个配置 |
| `development_agent` | 关 | 管理员私聊开发能力 |

打开开关只是加载能力，不能代替安装资源或填写凭据。每个外部功能的最小步骤见[可选扩展](extensions.md)。改变 YAML 后执行 `docker compose up -d --force-recreate bot`。

## 群回复与工具

初始化器将允许群设置为 Agent 活动群，默认仅 @ 回复。控制台「群聊管理」可以按群选择回复模式和配额，「Agent 设置」管理全局选项。群级覆盖可能优先于全局设置，调整后用当前群实际消息验证。

`agent.group` 的冷却、决策并发、每日决策 / 回复限制和上下文长度决定模型调用频率。建议先保留默认值，确认实际费用与群活跃程度后再调整主动接话。`follow_up` 默认关闭。

模型调用工具依赖当前权限和可用服务；管理操作不能通过普通角色关系取得权限。开发 Agent 默认关闭，启用时仅允许配置的管理员，并保留代码修改审批。

## 人格与值班

`agent.persona.active_id` 默认 `eres`，可选 `rin`、`eres`、`ishtar`。公开角色文档位于 `persona/`，没有预设真实用户关系。管理员可通过控制台编辑人设及查看修订记录。

公开仓库不附带 `special_users.md`。确需维护经用户同意的互动偏好时，可在自己的私有角色目录创建该文件；它被 Git 忽略，缺少该文件不影响启动。用户关系不能赋予管理员权限。

`duty_roster.timezone` 默认 `Asia/Shanghai`，`auto_switch_hour` 默认 `2`，自动排班开关为 `duty_roster.enabled`。值班表图片来自 `data/dutyroster/`，控制台日历用于查看。切换作用于所有群，当前人格状态保存在持久化数据目录。

## 环境变量与管理认证

`runtime/bot.env` 对应手动部署的 `qq-bot-py/.env`：

| 变量 | 用途 |
| --- | --- |
| `HOST` / `PORT` | Bot 监听地址与端口，Docker 内使用 `0.0.0.0:8080` |
| `SUPERUSERS` | JSON 数组格式的管理员 QQ |
| `ONEBOT_ACCESS_TOKEN` | OneBot 客户端连接凭据 |
| `AGENT_CONSOLE_USERNAME` | 默认 `admin` |
| `AGENT_CONSOLE_PASSWORD_HASH` | scrypt 密码哈希，不是明文密码 |
| `AGENT_CONSOLE_SESSION_BACKEND` | 默认 `redis` |
| `AGENT_CONSOLE_ORIGINS` | 允许的完整浏览器来源，逗号分隔，不带 `/admin/` 路径 |

手动生成密码哈希使用 `python tools/console_password.py`，不要将密码放在命令行参数中。哈希含 `$`，环境文件中使用单引号保留字面值。

控制台会话存储在 Redis，认证配置缺失时拒绝登录；HTTPS 来源使用安全 Cookie。使用自己的域名时，将 `AGENT_CONSOLE_ORIGINS` 改为实际 `https://域名` 并配置反向代理转发。来源校验不能替代管理员密码。

根 `.env` 的 `RINBOT_BIND_HOST` 默认 `127.0.0.1`，`RINBOT_PORT` 默认 `8080`，其余字段是内部服务凭据。改动已有数据库 / 缓存密码需要同时更新实际服务账户和 Bot 配置，不要只改单侧文件。

## 图片和文件链接

签到、值班和棋盘等基础图片通过 OneBot 图片数据发送，不要求 QQ 客户端能够访问 Docker 内的 `minio:9000`。上传文件或提供下载链接的扩展需要 `meme.minio.public_endpoint` 等外部访问设置，填入客户端能访问的 S3 入口，勿使用 Compose 内部服务名。

代理为空时直连。配置了代理但模型或外部服务不可用时，先检查代理地址在容器内是否可达；容器中的 `127.0.0.1` 指容器本身。
