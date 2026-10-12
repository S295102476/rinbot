# Responses 接口接入

基础部署默认使用 `ai` 配置的 Chat Completions 接口。Responses 是可选接口，用于独立的 GPT 专家、管理员开发助手或已明确配置的聊天回退；不影响未启用这些能力的基础启动。

```yaml
ai:
  openai_responses:
    enabled: true
    base_url: https://api.example.com/v1
    api_key_env: OPENAI_RESPONSES_API_KEY
    model: your-responses-model
```

将示例地址和模型换成接口实际支持的值。密钥保存在项目私有 `.env` 中的 `OPENAI_RESPONSES_API_KEY`，或同名环境变量中；不要写入公开示例、聊天消息或日志。原生 Responses 功能及供应商工具是否可用，取决于所配置接口的兼容性。

## 验证接口

在 `qq-bot-py` 目录运行：

```sh
.venv/bin/python tools/test_responses_api.py
```

返回码 `0` 表示文字和联网搜索均通过，`1` 表示文字失败，`2` 表示仅联网搜索失败。需要联网搜索时，还应确认供应商确实支持相应工具；文字成功不代表所有可选能力可用。

## 群聊专家与开发助手

群聊专家使用独立的 `expert` 用途路由、群范围和调用配额；只接收问题与必要上下文，不获得生产文件访问权。配置方法见[自然语言小游戏与 GPT 专家](agent-tools-v2.md)。

管理员开发助手还需要启用 `features.development_agent`、`agent.dev.enabled` 和 `agent.model_routes.development.enabled`，填写管理员白名单，并准备隔离检查镜像。任务先在源码副本中修改和检查，经同一管理员审阅批准后才应用到生产文件，不自动重启、提交 Git 或部署。完整流程见[开发任务](developer-tasks.md)。

```text
#开发 分析签到偶尔签错人的原因并给出修复补丁
#agent status
#agent approve 任务编号
#agent reject 任务编号
```
