# 部署与维护

默认路径使用 Linux x64 + Docker Compose v2。以下命令从仓库根目录运行，初始化步骤见[首页](../README.md#快速部署)。

## 初始化与存储

```bash
docker compose run --rm init
docker compose up -d --build
docker compose ps
```

初始化器询问模型完整请求 URL、API Key、模型名、管理员 QQ、允许群列表和控制台密码，生成以下本地文件。已存在配置时停止，不静默覆盖。

| 文件 | 用途 |
| --- | --- |
| `.env` | Compose 内部服务凭据、监听地址与端口 |
| `runtime/config.yaml` | Bot 业务配置，挂载到容器内 `qq-bot-py/config.yaml` |
| `runtime/bot.env` | OneBot Token、控制台密码哈希等运行环境变量 |

四个常驻服务为 `bot`、`mysql`、`redis`、`minio`，`init` 是一次性工具服务。数据保存于 `bot_data`、`bot_persona`、`mysql_data`、`redis_data`、`minio_data` 命名卷，实际卷名带 Compose 项目前缀。

MinIO 社区版现由上游仅提供源码。Compose 会自动使用 Go 1.24.8 构建固定版本 `RELEASE.2025-10-15T17-29-55Z`，无需手动安装 Go；首次构建需要下载 Go 依赖并编译，因此会比后续启动更久。此版本和构建方式依据 [MinIO 官方发布说明](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)，不依赖已经无法拉取的旧公共 MinIO 镜像。

`bot_data` 保存运行状态和本地资源，`bot_persona` 保存已编辑人格；初次启动从镜像种子资源初始化，重建镜像后继续使用已有卷。更改仓库中的人格种子不会自动覆盖已运行的人格文档。

## 接入 OneBot v11

本仓库运行 OneBot v11 适配器，不附带 QQ 登录实现。在你自己的 OneBot 客户端完成登录并添加反向 WebSocket：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

Access Token 填写 `runtime/bot.env` 中 `ONEBOT_ACCESS_TOKEN` 的值，不带变量名和引号。`127.0.0.1` 仅适用于客户端与 Docker 发布端口位于同一台宿主机。

- **客户端在另一容器**：容器内的 `127.0.0.1` 指该容器本身。将客户端加入 RinBot 的 Compose 网络后使用 `ws://bot:8080/onebot/v11/ws`，或者按客户端网络方案访问宿主机。
- **客户端在其他机器**：优先通过私有网络或 WebSocket HTTPS 反代连接。只暴露 OneBot 的路径，保留 Token，控制台继续使用 SSH 转发或单独的受保护入口。
- **QQ 已连接但群里无响应**：确认群号已加入允许列表，发送明确 @；查看 `docker compose logs --tail=100 bot`，再检查模型配置。

根 `.env` 的 `RINBOT_BIND_HOST` 默认 `127.0.0.1`，`RINBOT_PORT` 默认 `8080`。需要在受控局域网监听时设为宿主机实际局域网地址并重建 Bot；修改为所有地址会同时暴露 Bot 与管理入口，访问范围由宿主机网络控制。

## 远程访问控制台

### SSH 转发

在自己的电脑执行，将 `user@your-server` 替换为服务器登录信息：

```bash
ssh -N -L 8080:127.0.0.1:8080 user@your-server
```

保持该连接，打开 `http://127.0.0.1:8080/admin/`。如果电脑的 8080 已被占用，使用 `-L 18080:127.0.0.1:8080` 并访问本地 18080。

### HTTPS 反代

已有 HTTPS 反向代理时，给 RinBot 分配独立域名，将 `/admin/`、`/api/admin/` 与 OneBot WebSocket 路径代理至 `127.0.0.1:8080`，保留 Host、客户端协议信息和 WebSocket Upgrade。不要把控制台放到额外的路径前缀下。

例如在已配置证书的 Nginx HTTPS `server` 中添加：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
}
```

控制台 Cookie 和来源校验配置需要与实际 HTTPS 域名一致，具体字段见[配置参考](configuration.md)。代理仅开放自己需要的路径与来源。

## 验证部署

1. `docker compose ps` 显示四个服务运行，数据库、缓存和存储就绪，Bot 没有持续重启。
2. 控制台使用 `admin` 和初始化密码登录；进入群聊管理，确认允许群与默认仅 @ 策略。
3. OneBot 客户端连接成功；群里发送 `@机器人 你好` 并收到模型回复。
4. 发送 `#签到`、`#值班表`，确认图片能在 QQ 中显示。
5. 修改一项群设置，然后 `docker compose restart bot`，确认设置、人格与业务数据仍在。
6. 不设置外部扩展凭据时，基础功能持续运行。不要将接口探活成功等同于真实 QQ 接入验收。

部署自动化使用模拟 OneBot 与模型验证接入、发送和持久化路径。真实 QQ 登录、群内 @ 回复及客户端图片显示仍需要按上述步骤确认；首版当前验证状态见[更新说明](releases/first-public-release.md#验收记录)。

## 备份与更新

需要一起备份根 `.env`、`runtime/`、五个数据卷。配置含凭据，数据库含用户业务数据与记忆，不应提交到 GitHub。可先 `docker compose stop`，使用 Docker 卷备份工具复制一致状态；数据库也可以在维护流程中使用 MySQL 原生逻辑备份。

更新前记录当前提交并完成备份：

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

正常 `up`、`restart` 和 `down` 保留命名卷。不要使用 `docker compose down -v` 做普通更新，它会删除卷。升级后检查日志、登录、一次 QQ 回复与图片功能；数据结构变更应按对应 Release 说明操作。

如果旧克隆无法快进更新，请先阅读[历史清理与旧版本迁移](releases/first-public-release.md)，不要直接把旧历史重新推回远端。

## 常见排查

| 现象 | 检查 |
| --- | --- |
| 初始化拒绝继续 | 已存在配置；先确认是否已有部署，不要为了重跑初始化删除正在使用的文件 |
| 模型请求 404 | `ai.api_url` 是否为完整 `/chat/completions` 请求地址 |
| 模型请求 401 / 403 | API Key、服务访问策略和模型权限 |
| 图片无法识别 | 模型是否支持图片输入，以及代理是否允许图片数据 |
| 控制台 503 / 无法登录 | Redis、控制台认证环境变量、时钟与实际访问来源 |
| 其他容器连不上 Bot | `localhost` 的指向和 Docker 网络；参照 OneBot 接入说明 |
| 群设置改了却未生效 | 群设置是否覆盖全局、当前群是否允许、是否被配额或冷却限制 |
| 引擎或游戏数据不存在 | 先按扩展文档安装；不要仅打开功能开关 |

提交问题前，截取必要的错误类型和步骤，并移除日志中的账号、聊天内容和凭据。
