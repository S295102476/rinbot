# JM 番号搜索功能

## 功能说明

用户在群内发送 `jm + 纯数字`（如 `jm445566`），bot 自动调用 18comic 搜索接口，返回对应 album 页面链接。

## 触发方式

- **格式**：消息完整内容为 `jmXXXXXX`（不区分大小写，X 为纯数字）
- **示例**：`jm445566` → bot 回复 `https://18comic.vip/album/246522/`

## 搜索逻辑

1. 提取消息中的数字部分，构造搜索词 `jm{number}`
2. GET `https://18comic.vip/search/photos?search_query=jm{number}&search-type=photos&main_tag=0`
3. 若被重定向到 `/album/XXXXXX/`，直接取重定向后的 URL
4. 否则在搜索结果 HTML 中正则匹配第一个 `href="/album/XXXXXX"` 链接
5. 找到则回复完整链接，未找到则提示"未找到"

## 配置项（config.yaml）

```yaml
jm_search:
  allowed_groups: [987654321]  # 允许使用的群，空列表=所有群
```

## 文件

- `plugins/jm_search.py` — 插件主体
- `config.yaml` — `jm_search.allowed_groups` 白名单配置

## 限制

- 仅在 `allowed_groups` 列出的群内响应
- 不使用代理（直连）
- 请求超时 15 秒
- 优先级 4，`block=True`（不触发后续 AI 回复）
