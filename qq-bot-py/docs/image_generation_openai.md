# 图片生成接口

## 指令

- `#图片生成 <描述>`（或 `#生成图片`）：调用 OpenAI Images 兼容接口生成图片
- `#图片编辑 <描述>`（或 `#编辑图片`）：附带当前图片、回复图片或 `@用户` 头像后调用编辑接口
- `#生图 <描述>`（以及 `#nai`）：继续使用 NovelAI，不受本配置影响

前两个指令会把生成结果和本次耗时放在同一条消息中，图片下方显示例如
`耗时 60.2s`。

## 配置

`config.yaml` 的 `image_gen.openai` 控制前两个指令：

```yaml
image_gen:
  openai:
    base_url: "https://vip.auto-code.net"
    api_url: "https://vip.auto-code.net/v1"
    api_key_env: "OPENAI_RESPONSES_API_KEY"
    model: "gpt-image-2.5-sunburst"
    edit_model: "gpt-image-2.5-sunburst"
    fallback_model: "gpt-image-2"
    fallback_edit_model: "gpt-image-2"
```

`api_url` 是实际 Images API 根路径，插件会请求：

- `{api_url}/images/generations`
- `{api_url}/images/edits`

如果中转站把 `/v1` 放在别的位置，只修改 `api_url`，不需要改代码。API Key
优先从 `api_key_env` 指定的环境变量读取，也支持项目根目录的 `.env`。

OpenAI Images 返回的 `b64_json` 和带鉴权下载的 `url` 两种图片结果都可以处理；
编辑上传前会统一转换为小尺寸 RGBA PNG，减少请求体积。

## 降级重试

每次最多调用两次模型：先调用 `gpt-image-2.5-sunburst`，遇到 HTTP 502/503/504、
请求超时或未返回有效图片时，第二次改用 `gpt-image-2`。第二次失败就结束，
不会再调用主模型，也不会叠加原来的 `edit_retries` 重试循环。

兜底沿用同一个 `api_url` 和 API Key，保留提示词、尺寸及编辑参考图。
上述兜底配置都有默认值，现有配置无需修改；设为空字符串可禁用对应兜底。
参数、鉴权、限流和审核拒绝不会触发降级。原图下载失败或 QQ 发送失败也不会重跑生图。
日志会记录每次实际使用的模型和尝试次数，图片下方的耗时包含两次尝试。
