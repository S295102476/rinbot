# 变更日志

## 2025-05（回滚）— 关闭滚动摘要，缩减群聊上下文

### 问题记录（5.25 停机排查）

**现象：**
- 5.23 切换到 `gemini-3.5-flash` 原生 API + 滚动摘要方案后，两天内 API 请求数达 1447 条
- 对比：4.30 起使用 API 的前 23 天总请求仅 1523 条，即两天 ≈ 23 天的请求量（约 10 倍）
- Google AI Studio 账单从 ~$0.7/天 飙升到 ~$7/天

**根本原因分析：**

1. **摘要请求数量过多（主因）**
   - `_generate_group_summary` 每群每 20 条新消息触发一次 API 调用
   - 多个活跃群同时累计，每天额外产生约 650+ 次摘要调用
   - 旧方案无摘要，仅有用户主动 @机器人 时才触发，每天约 70 次

2. **单次摘要 thinking tokens 失控（次因）**
   - `gemini-3.5-flash` 原生 API 默认开启 thinking 模式
   - 每次摘要调用：input ~2070 tokens + thinking ~1033 tokens + output ~134 tokens = **3237 tokens**
   - thinking 占单次消耗的 **87%**，而实际有用输出只有 134 tokens
   - 主对话也受影响：input ~2121 + thinking ~1079 + output ~21 = 3221 tokens/次

3. **摘要方案本身引入的路径问题**
   - `_generate_group_summary` 使用 `ai_cfg["api_url"]`（OpenAI compat 层）直接调用主模型
   - 该路径绕过了 `call_gemini_native` 里的 `generationConfig`，导致无法受 `thinkingBudget` 约束
   - 即便 `call_gemini_native` 里设置了 `thinkingBudget: 512`，摘要路径的 thinking 仍完全无限制

**结论：** 摘要方案的设计目标（减少群聊上下文 payload）是正确的，但引入的额外请求次数远超节省的量，在 `gemini-3.5-flash` 原生 API 开启 thinking 后成本急剧放大，得不偿失。

---

### 回滚变更（5.25）

#### 1. 关闭滚动摘要（`plugins/group_chat.py`）
- 删除每 20 条消息触发 `_generate_group_summary` 的计数逻辑
- `_generate_group_summary` 函数本体保留，不再被调用
- 消除每天 ~650 次额外 API 调用

#### 2. 恢复原始群聊上下文（`plugins/ai_chat.py` — `_get_group_context()`）
- **旧方案（2025-05 前）**：100 条原始记录
- **已废弃的摘要方案**：Redis 摘要 + 10 条原始记录
- **当前方案（本次）**：直接取 **40 条**原始记录，无需 Redis
- 移除函数内 Redis 客户端初始化及摘要读取代码

#### 3. 关闭主对话 thinking（`plugins/gemini_native.py`）
- `thinkingConfig.thinkingBudget`：`512` → `0`
- `maxOutputTokens`：`800` → `400`（不再需要为 thinking 预留空间）
- 角色扮演回复上限 20 字，无需模型推理，thinking 无实际价值

### 预期恢复效果
| 指标 | 摘要方案（5.23–5.25） | 回滚后（本次）|
|------|----------------------|--------------|
| 每日 API 请求次数 | ~720 次 | ~70–100 次（仅 @触发） |
| 单次主对话 tokens | ~3221（含 thinking 1079） | ~1000–1500（无 thinking） |
| 单次摘要 tokens | ~3237（含 thinking 1033） | 0（无摘要） |
| 每日预估费用 | ~$7 | ~$0.7–1.0 |
| 群聊上下文质量 | 摘要200字 + 10条新消息 | 40条原始消息 |

### 待验证（5.26 观察一天）
- Google AI Studio 日志条数是否回落到 ~70–100 次/天
- `thoughtsTokenCount` 是否在响应中消失（thinkingBudget=0 生效确认）
- 对话响应时间是否可接受（40条原始记录 payload 比 100条小得多，预计 15–25s）

---

## 2025-05 — Gemini 原生层 + 滚动摘要 性能优化

### 背景
AI 对话响应时间长达 40–50 秒，主要瓶颈：
- `_get_group_context` 每次从 DB 拉取 100 条原始消息，payload 庞大
- Persona 文档（`persona/*.md`）每次请求都以文字形式注入，重复计费 token
- vision 模型旧版本 `gemini-3.1-pro-preview` 较慢

### 变更摘要

#### 1. 原生 Gemini REST API 层（`plugins/gemini_native.py` — 新增）
- 使用 Gemini Files API 上传 `persona/*.md` 一次，获取 `file_uri`，后续请求以 `fileData` 引用
- 文件 TTL 48h，后台任务每 44h 自动刷新
- 消息格式转换：OpenAI `messages[]` → Gemini `contents[]` + `systemInstruction`
- 流式解析 SSE（`streamGenerateContent?alt=sse`），支持 base64 图片内联
- 上传失败时优雅降级为文字注入（与旧行为相同）

#### 2. `_call_primary()` 路由分派（`plugins/ai_chat.py`）
- 当 `MODEL.startswith("gemini")` 时，绕过 OpenAI compat 路径，直接调用 `call_gemini_native()`
- 非 Gemini 模型（DeepSeek、GPT 等）保持原有 OpenAI compat 流式路径不变

#### 3. 启动时上传 persona 文件（`plugins/ai_chat.py`）
- `_create_chat_tables()` 启动钩子中，当主模型是 Gemini 时异步触发 `upload_persona_files()` 和 `start_refresh_task()`

#### 4. 群聊滚动摘要（`plugins/group_chat.py`）
- 每群每 20 条新消息触发异步任务 `_generate_group_summary(group_id)`
- 调用主模型（Gemini Flash）将最近 50 条记录压缩为 ≤200 字摘要
- 摘要写入 Redis key `group_summary:{group_id}`，TTL 4 小时
- 计数器 key `group_summary_counter:{group_id}`，触发后重置

#### 5. 群聊上下文优化（`plugins/ai_chat.py` — `_get_group_context()`）
- **之前**：每次拉取 100 条原始 DB 记录注入 prompt
- **之后**：从 Redis 读取摘要 + 仅取最近 10 条原始记录
- 摘要与原始消息格式：
  ```
  [群聊近况]
  <Redis 摘要，≤200字>

  [最近消息]
  <最近10条原始对话>
  ```
- 图片引用保持最新 5 张不变

#### 6. 模型版本更新（`config.yaml`）
| 配置项 | 旧值 | 新值 |
|--------|------|------|
| `ai.model` | `gemini-3-flash-preview` | `gemini-3.5-flash` |
| `ai.vision.model` | `gemini-3.1-pro-preview` | `gemini-3.5-flash` |
| `meme.gemini.detector_model` | `gemini-3-flash-preview` | `gemini-3.5-flash` |
| `meme.gemini.classifier_model` | — | `gemini-3.1-pro-preview`（不变） |

### 预期效果
| 指标 | 优化前 | 预期优化后 |
|------|--------|-----------|
| 主模型首包延迟 | ~20–25s | ~5–10s |
| `chat()` 总耗时 | ~28–35s | ~8–15s |
| `chat_with_vision()` 总耗时 | ~40–50s | ~15–25s |
| Persona token 消耗 | 每次重复 ~2000 token | 0（Files API 引用） |
| 群聊上下文 payload | ~100条 × 平均100字 | 200字摘要 + 10条最新 |

### 已知限制
- Gemini Files API：免费额度每日上传次数有限，File 最大 2GB，`TYPE=text/plain` 适用 Markdown
- 滚动摘要依赖主模型可用性；若主模型宕机，摘要不会更新（旧摘要仍可用，TTL 4h 后清空回退为纯原始消息）
- `_generate_group_summary` 目前使用 OpenAI compat 接口调用主模型，若主模型切换为原生 Gemini 可进一步优化
