# 可选 gsuid_core 接入

RinBot 通过 OneBot 接收 QQ 消息，再将支持的游戏指令转发到独立运行的 gsuid_core。Core 和插件需要另行安装，本仓库只提供桥接代码；没有安装 Core 时，基础聊天和控制台仍可使用。

```text
OneBot v11 客户端
  └─ RinBot
       ├─ 主聊天、控制台和本地小游戏
       └─ gsuid_bridge → gsuid_core → 对应游戏插件
```

## 配置

在部署的 `config.yaml` 中同时打开功能开关与桥接开关，填写 Core 的实际 WebSocket 地址：

```yaml
features:
  gsuid: true
gsuid:
  enabled: true
  ws_url: ws://127.0.0.1:8765/ws/Nonebot
```

示例地址适用于两项服务运行在同一台宿主机；容器内的 `127.0.0.1` 是该容器自己，应改成容器可以访问的 Core 地址。不要将带凭据的 WebSocket URL 提交到 Git。

安装并启动 Core 与对应插件后重启 RinBot。常用前缀包括鸣潮的 `ww`、终末地的 `end` 或 `zmd`，实际可用功能取决于已安装插件及其配置。

## 转发与排查

桥接先处理匹配的游戏指令；收到 Core 的有效结果后发送到 QQ，避免聊天模型重复回答。不匹配的消息交由原有聊天路径处理；首条结果默认等待 30 秒，后续分批结果等待 2 秒，Core 不可用或等待超时会记录错误状态。

可依次检查日志中的连接成功提示、Core 插件加载情况，以及群内 `ww帮助` 或 `end帮助` 的实际结果。接口错误不会把供应商返回的私人内容直接发送到 QQ。

其他可选扩展参见[公开版扩展说明](../../docs/extensions.md)。
