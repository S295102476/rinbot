# AI 文件生成功能

## 功能说明

用户在群内 `@机器人` 发送包含文件生成意图的消息时，机器人会调用 DeepSeek AI 生成对应文件内容，并通过 `upload_group_file` API 将文件上传至群文件，而不是以纯文字方式返回。

**示例触发语句：**
- `@bot 创建一个用于展会的公司介绍网页，设计简洁大方，完成后直接发送html文件给我`
- `@bot 帮我写一个Python爬虫脚本，发送py文件`
- `@bot 生成文件，整理一份公司员工名单模板（csv）`

---

## 改动文件

### `plugins/ai_chat.py`

#### 新增 import（第 2-4 行）

```python
import os
import re
import tempfile
from datetime import datetime  # 从 extract_memory 内部提升至顶层
```

原来仅有 `import json / httpx / yaml`，新增了 `os`、`re`、`tempfile` 和顶层 `datetime` import。

---

#### 新增：文件关键词表 `_FILE_KEYWORDS`（第 27-36 行）

```python
_FILE_KEYWORDS: list[tuple[list[str], str]] = [
    (["html", "网页", "前端页面", "h5"], "html"),
    (["python", ".py", "py文件", "python文件"], "py"),
    (["markdown", ".md", "md文件"], "md"),
    (["json文件", ".json"], "json"),
    (["csv文件", ".csv"], "csv"),
    (["txt文件", ".txt", "文本文件"], "txt"),
    (["代码文件", "发送文件", "生成文件", "给我文件", "直接发送"], None),  # 后缀延迟判断
]
```

定义了关键词到文件后缀的映射表。最后一组通用意图词不绑定具体后缀，默认降为 `txt`。

---

#### 新增函数 `_detect_file_request()`（第 39-58 行）

```python
def _detect_file_request(message: str) -> str | None:
```

- 遍历 `_FILE_KEYWORDS`，在消息中匹配关键词
- 若有命中，返回对应后缀字符串（`"html"`、`"py"` 等）
- 若无任何命中，返回 `None`
- 通用意图词命中但无具体后缀时，默认返回 `"txt"`

---

#### 修改函数 `handle_chat()`（第 75-84 行）

原来直接调用 `chat()`，现在插入文件检测分支：

```python
# 检测是否为文件生成请求
file_ext = _detect_file_request(message)
if file_ext:
    await _handle_file_request(bot, event, message, file_ext)
    return

reply = await chat(user_id, message, context_group_id)
```

命中文件请求后直接调用 `_handle_file_request()` 并 `return`，跳过普通对话流程（不写入 Redis 历史，不触发记忆提取）。

---

#### 新增函数 `_handle_file_request()`（第 88-148 行）

```python
async def _handle_file_request(bot: Bot, event: GroupMessageEvent, message: str, ext: str):
```

**流程：**

1. **发送等待提示**：`正在生成文件，请稍等……`
2. **构造专用 system prompt**：要求 AI 只输出纯文件内容，不包含任何 markdown 代码块或说明
3. **调用 DeepSeek API**（timeout=120s），获取文件内容
4. **剥离 markdown 代码块**（用 `re.sub` 去除 ` ``` ` 包裹，防止 AI 不遵守 prompt 时的情况）
5. **写入临时文件**：路径为 `tempfile.gettempdir()/ai_output_{timestamp}.{ext}`
6. **调用 `bot.call_api("upload_group_file", ...)`** 上传到群文件
7. **发送成功提示**：`✅ 文件已生成，请在群文件中查看：{filename}`
8. **降级处理**（上传失败时）：将文件内容截断至 1800 字以文字形式发送，提示用户无法上传
9. **清理临时文件**：`finally` 块中 `os.remove(tmp_path)`

---

## 注意事项

1. **机器人需是群管理员** 或群开启了"允许任何人上传文件"，否则 `upload_group_file` 会失败，触发降级逻辑
2. **文件请求不写入对话历史**，不会污染 AI 的普通聊天上下文
3. **临时文件路径**：使用操作系统的临时目录，上传完毕立即删除
4. **关键词误触发**：包含 "html" 的普通问题可能被误识别为文件请求，可按需调整 `_FILE_KEYWORDS` 中的关键词

---

## 关键词触发表

| 关键词 | 生成文件类型 |
|--------|-------------|
| `html`、`网页`、`前端页面`、`h5` | `.html` |
| `python`、`.py`、`py文件`、`python文件` | `.py` |
| `markdown`、`.md`、`md文件` | `.md` |
| `json文件`、`.json` | `.json` |
| `csv文件`、`.csv` | `.csv` |
| `txt文件`、`.txt`、`文本文件` | `.txt` |
| `代码文件`、`发送文件`、`生成文件`、`给我文件`、`直接发送` | `.txt`（默认） |
