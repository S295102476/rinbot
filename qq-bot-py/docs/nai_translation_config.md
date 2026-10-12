# NAI 提示词翻译配置

NAI 中文提示词翻译使用 Antigravity；`ai.openai_responses` 仅用于开发 Agent
和聊天末级兜底。
`nai` 节不再维护单独的 `trans_api_url`、`trans_api_key` 或 `trans_model`。

```yaml
ai:
  openai_responses:
    model: "gpt-5.6-sol"
    base_url: "https://vip.auto-code.net"
    api_key_env: "OPENAI_RESPONSES_API_KEY"
    api_backend: "responses"
    timeout: 120
```

验证普通文本调用：

```bash
.venv/bin/python tools/test_responses_api.py --skip-search
```

翻译失败时，NAI 会继续使用用户原始提示词，不影响生图请求。
