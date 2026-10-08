"""原生 Gemini REST API 客户端（Context Caching 版本）

功能：
1. 启动时将 systemInstruction (persona) 创建为 cachedContent，TTL 1h，55min 自动续期
2. 将 OpenAI 格式 messages 转换为 Gemini 原生 contents 格式
3. 流式调用 streamGenerateContent，请求携带 cachedContent 名，命中缓存 token 折扣 75%

降级策略：
- 创建 cache 失败 → systemInstruction 文字正常随每次请求发送（无缓存折扣但功能正常）
- 请求失败 → 抛出异常由 ai_chat._call_primary 的调用方兜底
"""

import asyncio
import json
import pathlib
import time

import httpx
import yaml
from nonebot.log import logger

from .provider_output import extract_gemini_text
from .persona_manager import get_persona_content

# ---------- 配置读取 ----------
with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_ai_cfg = _config["ai"]
_API_KEY: str = _ai_cfg["api_key"]
_PRIMARY_PROXY: str | None = _ai_cfg.get("proxy") or None
_PRIMARY_TIMEOUT: int = _ai_cfg.get("primary_timeout", 40)
_PERSONA_DIR: str = _ai_cfg.get("persona_dir", "persona")

_GEMINI_BASE = str(_ai_cfg.get("gemini_base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
_GEMINI_GENERATE_URL = _GEMINI_BASE + "/models/{model}:generateContent?key={key}"
_GEMINI_CACHE_URL = _GEMINI_BASE + "/cachedContents"

# Context Cache TTL：1小时，55分钟时续期
_CACHE_TTL_SECONDS = 3600
_CACHE_RENEW_BEFORE = 300   # 过期前 5 分钟续期

# ---------- 全局 cache 状态 ----------
_CACHED_CONTENT_NAME: str = ""          # "cachedContents/xxxxx"
_CACHED_CONTENT_EXPIRE_AT: float = 0.0  # 过期时间戳
_CACHED_MODEL: str = ""                 # cache 绑定的模型名（含 models/ 前缀）

# ---------- 降级用 persona 文字 ----------
_PERSONA_TEXT_FALLBACK: str = ""


def _load_persona_text() -> str:
    """读取当前人设和共享文档，供 cache 或降级注入使用。"""
    return get_persona_content()


def _make_transport() -> httpx.AsyncHTTPTransport:
    return (
        httpx.AsyncHTTPTransport(proxy=_PRIMARY_PROXY, http2=False)
        if _PRIMARY_PROXY
        else httpx.AsyncHTTPTransport(http2=False)
    )


async def create_cached_content(model: str) -> bool:
    """
    为指定模型创建 Context Cache，内容为 persona systemInstruction。
    成功返回 True，失败返回 False（降级为每次随请求发送 systemInstruction）。
    """
    global _CACHED_CONTENT_NAME, _CACHED_CONTENT_EXPIRE_AT, _CACHED_MODEL, _PERSONA_TEXT_FALLBACK

    # 先清空，避免失败后残留过期 name 被误用
    _CACHED_CONTENT_NAME = ""
    _CACHED_CONTENT_EXPIRE_AT = 0.0

    _PERSONA_TEXT_FALLBACK = _load_persona_text()
    if not _PERSONA_TEXT_FALLBACK:
        logger.warning("[gemini_native] persona 文字为空，跳过 cache 创建")
        return False

    # Gemini cachedContents 要求模型名带 "models/" 前缀
    model_name = model if model.startswith("models/") else f"models/{model}"

    payload = {
        "model": model_name,
        "systemInstruction": {
            "parts": [{"text": _PERSONA_TEXT_FALLBACK}]
        },
        # tools 必须在 cache 里声明，带 cachedContent 的请求不能再单独带 tools
        "tools": [{"googleSearch": {}}],
        "ttl": f"{_CACHE_TTL_SECONDS}s",
    }

    try:
        async with httpx.AsyncClient(
            timeout=30, mounts={"https://": _make_transport()}
        ) as client:
            resp = await client.post(
                _GEMINI_CACHE_URL,
                params={"key": _API_KEY},
                json=payload,
            )
        if resp.status_code != 200:
            logger.warning(
                f"[gemini_native] 创建 cache 失败 HTTP {resp.status_code}"
            )
            return False

        data = resp.json()
        name = data.get("name", "")
        if not name:
            logger.warning("[gemini_native] cache 响应无 name")
            return False

        _CACHED_CONTENT_NAME = name
        _CACHED_MODEL = model_name
        _CACHED_CONTENT_EXPIRE_AT = time.time() + _CACHE_TTL_SECONDS
        token_count = data.get("usageMetadata", {}).get("totalTokenCount", "?")
        logger.info(
            f"[gemini_native] Context Cache 已创建: {name} | {token_count} tokens | TTL {_CACHE_TTL_SECONDS}s"
        )
        return True

    except Exception as e:
        logger.warning(f"[gemini_native] 创建 cache 异常: {type(e).__name__}")
        return False


async def _renew_cached_content() -> bool:
    """续期已有 cache（PATCH 更新 TTL），避免过期。"""
    global _CACHED_CONTENT_EXPIRE_AT

    if not _CACHED_CONTENT_NAME:
        return False

    try:
        async with httpx.AsyncClient(
            timeout=15, mounts={"https://": _make_transport()}
        ) as client:
            resp = await client.patch(
                f"{_GEMINI_BASE}/{_CACHED_CONTENT_NAME}",
                params={"key": _API_KEY, "updateMask": "ttl"},
                json={"ttl": f"{_CACHE_TTL_SECONDS}s"},
            )
        if resp.status_code != 200:
            logger.warning(f"[gemini_native] cache 续期失败 HTTP {resp.status_code}")
            return False

        _CACHED_CONTENT_EXPIRE_AT = time.time() + _CACHE_TTL_SECONDS
        logger.info(f"[gemini_native] cache 续期成功: {_CACHED_CONTENT_NAME}")
        return True

    except Exception as e:
        logger.warning(f"[gemini_native] cache 续期异常: {type(e).__name__}")
        return False


async def _cache_keepalive_loop(model: str):
    """后台循环：在 cache 过期前 5 分钟续期，失败则重新创建。"""
    # 等待 create_cached_content() 完成（upload_persona_files 以 create_task 异步启动，
    # 此时 _CACHED_CONTENT_EXPIRE_AT 可能还是 0.0，需等其写入后再计算 sleep）
    for _ in range(60):  # 最多等 60s
        if _CACHED_CONTENT_EXPIRE_AT > 0:
            break
        await asyncio.sleep(1)

    while True:
        # 等到距过期还剩 _CACHE_RENEW_BEFORE 秒时唤醒
        sleep_sec = max(60, _CACHED_CONTENT_EXPIRE_AT - time.time() - _CACHE_RENEW_BEFORE)
        await asyncio.sleep(sleep_sec)

        logger.info("[gemini_native] cache keepalive 触发，尝试续期...")
        ok = await _renew_cached_content()
        if not ok:
            logger.info("[gemini_native] 续期失败，重新创建 cache...")
            await create_cached_content(model)


def start_refresh_task(model: str = ""):
    """启动 cache keepalive 后台任务（在 on_startup 里调用）。"""
    asyncio.create_task(_cache_keepalive_loop(model or _ai_cfg.get("model", "")))


# 保留旧名称兼容 ai_chat.py 中的调用
async def upload_persona_files(model: str = "") -> list[str]:
    """兼容旧接口：实际执行 create_cached_content。返回空列表（cache 方案不用 file_uri）。"""
    await create_cached_content(model or _ai_cfg.get("model", ""))
    return []


# ---------- 格式转换 ----------

def _openai_messages_to_gemini(
    messages: list[dict],
) -> tuple[dict | None, list[dict]]:
    """
    将 OpenAI 格式 messages 转换为 Gemini 原生格式。

    返回 (system_instruction, contents)：
    - system_instruction: {"parts": [{"text": "..."}]}（或 None，cache 命中时为 None）
    - contents: [{"role": "user"|"model", "parts": [...]}]

    规则：
    - role=system → system_instruction（仅 cache 降级时使用）
    - role=user   → role=user
    - role=assistant → role=model
    - image_url base64 → inlineData
    """
    system_parts: list[dict] = []
    contents: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append({"text": content})
            continue

        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict] = []

        if isinstance(content, str):
            if content.strip():
                parts.append({"text": content})
        elif isinstance(content, list):
            for item in content:
                t = item.get("type", "")
                if t == "text":
                    text_val = item.get("text", "").strip()
                    if text_val:
                        parts.append({"text": text_val})
                elif t == "image_url":
                    url_val = item.get("image_url", {}).get("url", "")
                    if url_val.startswith("data:"):
                        try:
                            header, b64data = url_val.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                        except Exception:
                            pass
                    elif url_val.startswith("http"):
                        parts.append({"text": f"[图片链接：{url_val}]"})
                elif t == "file":
                    file_uri_val = item.get("file", {}).get("uri", "")
                    if file_uri_val:
                        parts.append({"fileData": {"fileUri": file_uri_val}})

        if not parts:
            continue

        # 相邻同 role 合并（Gemini 要求 user/model 严格交替）
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


# ---------- 原生调用 ----------

async def call_gemini_native(model: str, messages: list[dict]) -> str:
    """
    使用原生 Gemini REST API 发送对话请求（流式 SSE）。

    - cache 有效：请求携带 cachedContent，不附带 systemInstruction（省 token）
    - cache 无效/过期：降级为随请求发送 systemInstruction 文字
    """
    global _CACHED_CONTENT_NAME, _CACHED_CONTENT_EXPIRE_AT

    # 检查 cache 是否接近过期或不存在
    cache_valid = bool(_CACHED_CONTENT_NAME) and (
        time.time() < _CACHED_CONTENT_EXPIRE_AT - 30
    )
    if not cache_valid:
        logger.info("[gemini_native] cache 无效或过期，重新创建...")
        await create_cached_content(model)
        cache_valid = bool(_CACHED_CONTENT_NAME)

    system_instruction, contents = _openai_messages_to_gemini(messages)

    if not contents:
        raise RuntimeError("[gemini_native] contents 为空，无法发送请求")

    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 1600,  # thinking + 实际输出共享 budget；thinking 最多约 1200，留 400 给回复
            "thinkingConfig": {"thinkingBudget": 1024},  # thinking 上限放宽，避免强制截断后泄漏到输出
        },
        # googleSearch 已在 cache 创建时声明，带 cachedContent 请求不能重复带 tools
    }

    if cache_valid:
        # cache 命中：不发 systemInstruction，改用 cachedContent 引用
        payload["cachedContent"] = _CACHED_CONTENT_NAME
        cache_label = f"cache={_CACHED_CONTENT_NAME}"
        # 动态 system_instruction（QQ/昵称等）无法放入 cachedContent，注入到第一条 user 消息前
        if system_instruction:
            sys_text = "\n".join(
                p.get("text", "") for p in system_instruction.get("parts", []) if "text" in p
            ).strip()
            if sys_text and contents:
                for i, turn in enumerate(contents):
                    if turn["role"] == "user":
                        contents[i]["parts"].insert(0, {"text": sys_text + "\n\n"})
                        break
    else:
        # 降级：cache 创建失败，直接带完整 systemInstruction（persona + system_prompt）+ 动态 QQ 注入
        sys_parts: list[dict] = []
        if _PERSONA_TEXT_FALLBACK:
            sys_parts.append({"text": _PERSONA_TEXT_FALLBACK})
        if system_instruction:
            dyn_text = "\n".join(
                p.get("text", "") for p in system_instruction.get("parts", []) if "text" in p
            ).strip()
            if dyn_text:
                sys_parts.append({"text": dyn_text})
        if sys_parts:
            payload["systemInstruction"] = {"parts": sys_parts}
        payload["tools"] = [{"googleSearch": {}}]
        cache_label = "cache=MISS(fallback)"

    # 使用非流式 generateContent，避免 SSE 长连接被代理截断
    url = _GEMINI_GENERATE_URL.format(model=model, key=_API_KEY)
    transport = _make_transport()

    async with httpx.AsyncClient(
        timeout=_PRIMARY_TIMEOUT,
        mounts={"https://": transport},
    ) as client:
        logger.info(f"[gemini_native] 调用原生 Gemini: {model} | {cache_label}")
        _tp = time.time()
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"[gemini_native] HTTP {resp.status_code}")
        data = resp.json()
        answer = extract_gemini_text(data, skip_citation_parts=True)
        logger.debug(f"[gemini_native] 完成 {time.time()-_tp:.2f}s，共 {len(answer)} 字")
        return answer

def _extract_text(obj: dict, ref: list):
    """从 Gemini generateContent 的 JSON 里提取文字到 ref[0]。
    兼容旧调用；只保留最终回答，不包含 thought、搜索过程或引用标记 part。
    """
    ref[0] += extract_gemini_text(obj, skip_citation_parts=True)
