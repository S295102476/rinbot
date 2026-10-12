# 已知问题记录

> 迁移说明（2026-08-10）：下文 OpenClaw/GPT-5.5 内容是历史故障记录。当前活动代码使用通用 OpenAI Responses API 作为聊天兜底、搜索和文字翻译入口。

## AI 对话两级兜底失效

### 现象

```
[INFO]    调用主模型 gemini-3-flash (attempt 1)
[WARNING] 主模型超时（>40s），切换二级兜底 gpt-5.5
[INFO]    调用兜底模型 gpt-5.5
[DEBUG]   [ai_chat/fallback] 二级兜底失败: Expecting value: line 1 column 1 (char 0)
[WARNING] 二级兜底失败，切换三级兜底 deepseek-v4-flash
[INFO]    调用三级兜底模型 deepseek-v4-flash
```

### 根本原因

**OpenClaw 代理端只接受 base64 图片，拒绝 `host:9000` 端口的 MinIO 直链。**

群聊上下文（group context）里如果带有图片，代码会将 MinIO 对象通过 `_presign_group_img()` 生成临时直链，再下载转 base64。但以下两种情况会导致 `http://...9000/...` 原始 URL 漏进 messages 里：

1. `image_url` 字段存储的是完整 http 链接（以 `http` 开头），代码判断 `ref.startswith("http")` 后直接跳过转换，原始 URL 留在 content 里
2. presign 本身就会生成 `http://minio.example.com:9000/...` 格式的 URL —— 如果下载转 base64 成功还好，但若下载失败就会导致 content 里没有该图（数量减少），并不会插入原始 URL，所以这条路并不是问题

当 messages 里携带了 9000 端口的直链，OpenClaw 返回空 body（非 JSON），所有级别都走同一份 messages，因此一、二级都失败，最终降级到三级 DeepSeek（会调用 `_strip_images` 剔除所有图片，所以能成功）。

### 修复（2026-04-29）

**方案：MinIO `get_object()` 直读，不再走 presign HTTP URL。**

改动文件：
- [plugins/ai_chat.py](../plugins/ai_chat.py)
  - 删除 `_presign_group_img()`，改为 `_read_group_img_b64(obj_name)`：用 MinIO SDK 的 `get_object()` 直接拿字节流转 base64，全程不产生任何 HTTP 请求到 9000 端口
  - 群上下文图片加载从 `httpx.AsyncClient` 改为 `run_in_executor` 调用上述同步函数
  - 移除不再需要的 `timedelta` import
- [plugins/group_chat.py](../plugins/group_chat.py)
  - `group-images` bucket 清理周期从 24h 改为 **12h**（86400 → 43200 秒）

**原理**：MinIO Python SDK 的 `get_object()` 是内部 SDK 调用，走的是 MinIO 服务端 SDK 鉴权，不是外网 HTTP 直链，因此 OpenClaw 代理不会看到也不会拦截 9000 端口流量。

### 频发时间

- 2026-04-29 16:02 及多次上午时段
- 通常在群聊活跃、历史图片较多时触发

### 影响

主模型超时约 40s + 二级兜底约 10-13s 后才降到 GPT（三级），整体响应延迟约 **50-55 秒**，最终仍能回复但延迟极大。

### 当前兜底顺序（2026-04-29 调整）

| 级别 | 模型 | 说明 |
|------|------|------|
| 一级 | gemini-3-flash | 主模型，40s 超时 |
| 二级 | deepseek-v4-flash | 自动剥图（不支持多模态） |
| 三级 | gpt-5.5 | 最后兜底，支持多模态 |

---

## 其他 OpenClaw 限制

| 限制项 | 说明 |
|--------|------|
| 不接受 host:9000 直链 | 必须使用 base64 data URL 传图 |
| gpt-image-2 输入图 | 必须是真实 RGBA PNG，JPEG 字节流即使改扩展名也会 500 |
| gpt-image-2 大小限制 | 单图 < 4MB，推荐 512×512 压缩后约 300KB |
