# 可选扩展

基础部署不需要本页的服务。所有开关位于 `runtime/config.yaml` 的 `features`；填写服务参数后再开启对应能力并重建 Bot。请使用自己的接口、凭据与数据。

## 联网搜索

`features.web_search: true` 加载搜索能力。当前有两种独立接入路径：

- `#搜索`：优先使用明确开启且凭据完整的 `ai.antigravity` 搜索接口；否则需要 `web_search` 中单独配置的 Gemini 搜索密钥与模型。Antigravity 接入需填写接口地址、密钥、模型与明确协议。普通 Chat Completions 接口不会因此自动获得联网能力。
- 自然语言搜索卡片：在 `web_search` 配置支持 Google 搜索工具的 Gemini 密钥、模型和可选代理。这个后端使用自己的服务接口，与普通聊天密钥分开。

缺少对应搜索后端时保持关闭，Google 搜索不借用主聊天密钥。历史 `ai.search.provider` 字段不再决定搜索路由。搜索生成文件的能力还需要客户端可访问的文件外链设置，参见[图片与文件链接](configuration.md#图片和文件链接)。

## 绘图与图片编辑

`features.image_gen: true` 启用 `#图片生成`、`#图片编辑` 等入口，配置 `image_gen.openai.api_url`、`api_key`、`model`，按服务能力配置 `edit_model`。这里的 `api_url` 为 Images API **基础地址**，例如 `https://api.example.com/v1`；插件在后面追加 `/images/generations` 或 `/images/edits`，与聊天的完整请求 URL 不同。

接口需要兼容图片生成 JSON、图片编辑 multipart 输入及相应响应格式。不同服务可用的模型、大小和费用以其说明为准。

NovelAI 通过 `features.nai: true` 单独启用，至少设置 `nai.token`，可配置代理和每日限额；中文提示词翻译复用已配置模型。不要把两种绘图服务的凭据混用。

## Pixiv 与链接解析

Pixiv 使用 `features.pixiv: true`，在 `setu` 配置 `pixiv_refresh_token`、必要的 `pixiv_api_proxy`、管理员与每日限额。外部平台接口和网络变化会影响排行榜与插画下载。

链接解析使用 `features.link_parser: true`，依赖目标平台与对应解析库。仅在自己的网络可达、确实需要时开启。搜图与搜本子当前不纳入可用功能，不建议通过填写历史配置尝试启用。

## 棋类引擎

基础井字棋、五子棋娱乐玩法、成语与猜数字不需要外部引擎。象棋 / 围棋双人规则也不需要 AI 模型，**人机对战**分别需要 Fairy-Stockfish、KataGo；五子棋高级难度需要 Rapfi。

Linux x64 可用现有安装器安装已固定版本的象棋 / 围棋引擎。默认 Docker 中执行：

```bash
docker compose exec bot python tools/install_board_engines.py --game all
```

安装器把文件写入持久化 `data/engines/`，不会修改 YAML 或自动重启。按 `qq-bot-py/docs/minigames_phase3.example.yaml` 将对应路径合并到 `runtime/config.yaml`，打开 `features.board_engines` 和各游戏 `engine.enabled`。

```bash
docker compose exec bot python tools/check_board_engines.py --verify-files --self-test
docker compose up -d --force-recreate bot
```

安装器目前面向 Linux x64 CPU 引擎；手动部署在 `qq-bot-py/` 执行相同 Python 工具。不同 CPU 需要匹配指令集，诊断工具会检查启动与资源配置。Rapfi 需自行取得合适发行包，填写 `minigames.rapfi.executable` 和 `config_path`，打开 `minigames.rapfi.enabled`，再运行 `python tools/check_rapfi.py --self-test`。

引擎与模型文件保持其上游许可，不提交至 RinBot 源码仓库。

## GBVSR 数据

`features.gbvsr: true` 加载帧数与使用率入口。本仓库不包含原部署中约 4 GB 的图片；首次使用先通过现有工具抓取并导入自己的数据。

先选择一个角色验证下载与导入，再按需要抓取全部：

```bash
docker compose exec bot python tools/gbvsr_fetch_frame.py Katalina
docker compose exec bot python tools/gbvsr_import_frame_db.py
```

确认可以发送 `#GB 帮助` 并查询该角色后，可将抓取参数换成 `all`。工具访问 Dustloop API、生成本地 JSON 与图片，导入工具写入当前部署的 MySQL；根据网络需要添加 `--proxy`。下载数据的权利与使用条件以来源为准，失败角色会显示 `SKIP`，需检查结果，不能只看进程退出码。

角色概览使用 `gbvsr_fetch_overview.py` / `gbvsr_import_overview_db.py`。版本更新、中文注释与翻译维护工具位于 `qq-bot-py/tools/`，执行前阅读参数。缺少数据时先补齐，不将旧私有数据库作为公开安装前提。

## Core 游戏查询

Core 是另行部署的服务，主仓库不包含 Core 源码、游戏插件、Cookie 或账号数据。按照其上游说明安装 Core 及自己需要的插件，然后在 RinBot 配置：

```yaml
features:
  gsuid: true
gsuid:
  enabled: true
  ws_url: ws://your-core:8765/ws/Nonebot
```

`your-core` 需要是 **Bot 容器可访问**的 Core 地址。同机但不在同一容器时不能直接使用 `127.0.0.1`。配置后重建 Bot，检查桥接日志，再发送实际插件支持的帮助指令。

RinBot 负责消息转发；游戏范围、账号绑定、插件权限和服务稳定性由实际 Core 部署决定。原本机 Core 目录与修改可继续私有保留，不应提交进本仓库。

## 私聊开发 Agent

默认关闭。需要时明确开启 `features.development_agent` 与 `agent.dev.enabled`，填写 `agent.dev.admin_users`、工作区和沙箱相关配置。它用于受控源码读取、检查和修改提案；管理员通过 `#agent approve <编号>` / `#agent reject <编号>` 审批，不能作为公开群聊的任意 Shell 接口。

先在专门开发实例验证，保持权限限制与审批，再接入自己的工作区。默认 Docker 部署的镜像代码不作为宿主机项目编辑入口。
