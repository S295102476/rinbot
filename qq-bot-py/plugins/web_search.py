"""凛……搜/搜索/查/查找 — Gemini 联网搜索，结果渲染为图片卡发送

触发示例：
  凛帮我搜 <内容>
  凛帮我搜索一下 <内容>
  凛你去查找一下 <内容>
  凛，帮忙查 <内容>
支持附图或回复图一起搜索（图文多模态）。
"""

import io
import time
import base64
import re
import asyncio
from datetime import datetime

import httpx
import yaml
from PIL import Image, ImageDraw, ImageFont
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.rule import Rule
from nonebot.log import logger

from .provider_output import extract_gemini_text, strip_tool_activity

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_ai_cfg = _config["ai"]
_ws_cfg = _config.get("web_search", {})

_API_KEY: str = _ws_cfg.get("api_key") or ""
_PROXY: str | None = _ws_cfg.get("proxy") or _ai_cfg.get("proxy") or None
_MODEL: str = _ws_cfg.get("model", "gemini-3.5-flash")
_TIMEOUT: int = _ws_cfg.get("timeout", _ai_cfg.get("primary_timeout", 120))
_COOLDOWN: int = _ws_cfg.get("cooldown", 10)
_CARD_WIDTH: int = _ws_cfg.get("card_width", 1440)
_BODY_FONT_SIZE: int = _ws_cfg.get("body_font_size", 32)
_CARD_TITLE: str = _ws_cfg.get("title", "Gemini Web Search")
_THINKING_LEVEL: str = _ws_cfg.get("thinking_level", "minimal")
_MAX_IMAGES: int = _ws_cfg.get("max_images", 3)
_RETRY_COUNT: int = _ws_cfg.get("retry_count", 2)
_RETRY_BASE_DELAY: float = _ws_cfg.get("retry_base_delay", 2.0)
_CACHE_FALLBACK: bool = _ws_cfg.get("cache_fallback", True)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_CACHE_TTL_SECONDS = 3600

_SYSTEM_PROMPT = (
    "你是一个专业的搜索助手。请结合用户给出的文字、图片内容和联网搜索结果，用简体中文直接给出核心结论。"
    "输出必须是纯文本自然段，禁止 Markdown，禁止星号加粗，禁止标题，禁止项目符号，"
    "禁止代码块，禁止引用编号，禁止单独列出链接。"
    "回答要简洁、准确、客观。"
    "如果用户问图片是谁或是什么，优先说明对应对象和关键依据；如果用户问题目、报错或其他图片内容，直接解答问题。"
    "无法确认时请明确说无法确认，不要编造作者、角色、作品或来源。"
)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GENERATE_URL = _GEMINI_BASE + "/models/{model}:generateContent?key={key}"
_CACHE_URL = _GEMINI_BASE + "/cachedContents"

_cache_name: str = ""
_cache_expire_at: float = 0.0

# 群冷却 {group_id: last_ts}
_cooldown: dict[int, float] = {}

# 只有“凛 + 明确请求短语 + 搜/搜索/查/查找”才触发；请求短语必须
# 紧挨搜索动词，不能在普通句子中向后扫描“调查”“搜集”等词。
_SEARCH_TRIGGER_RE = re.compile(
    r"^\s*凛[\s,，。.!！?？~～、：:;；\-—_]*"
    r"(?P<prefix>"
    r"麻烦(?:你)?(?:帮我|帮忙)?|"
    r"请(?:你)?(?:帮我|帮忙)?|"
    r"能不能(?:帮我|帮忙)?|"
    r"可以(?:帮我|帮忙)?|"
    r"你(?:帮我|帮忙|来|去)?|"
    r"帮我|帮忙|给我|替我|来|去"
    r")?"
    r"[\s,，。.!！?？~～、：:;；\-—_]*"
    r"(?P<verb>搜索|搜(?!集)|查找|查)"
    r"(?P<suffix>找一下|一下|一哈|看看|下|找)?"
    r"[\s,，。.!！?？~～、：:;；\-—_]*"
    r"(?P<query>.*)$",
    re.DOTALL,
)

# ---------- 字体候选 ----------
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------- 触发规则 ----------
def _parse_search_trigger(text: str) -> str | None:
    match = _SEARCH_TRIGGER_RE.match(text or "")
    if not match:
        return None

    return (match.group("query") or "").strip()


def _strip_onebot_markup(text: str) -> str:
    return re.sub(r"\[(?:CQ:)?[a-zA-Z0-9_]+[^\]]*\]", "", text or "").strip()


def _extract_search_query(event: GroupMessageEvent) -> str | None:
    plain = event.get_plaintext().strip()
    query = _parse_search_trigger(plain)
    if query is not None:
        return query

    raw = _strip_onebot_markup(getattr(event, "raw_message", "") or "")
    query = _parse_search_trigger(raw)
    if query is not None:
        return query

    return None


def _web_search_rule() -> Rule:
    async def _rule(event: GroupMessageEvent) -> bool:
        return _extract_search_query(event) is not None
    return Rule(_rule)


# ---------- 文本清理与图片渲染 ----------
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CITATION = re.compile(r"\s*\[\d+(?:[,\s-]*\d+)*\]")
_RAW_URL = re.compile(r"https?://\S+")
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n?|\n?```")


def _clean_answer(text: str) -> str:
    """把模型输出整理成适合卡片展示的纯文本。"""
    text = strip_tool_activity(text)
    text = _FENCE.sub("", text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = _RAW_URL.sub("", text)
    text = _CITATION.sub("", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?!\*)", r"\1", text, flags=re.DOTALL)

    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-+*•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = line.replace("*", "").strip()
        if line:
            cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    if not text:
        return 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _wrap_text_by_pixel(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """按实际像素宽度换行，避免中英文混排被裁剪。"""
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)
    lines: list[str] = []

    for para in text.split("\n"):
        para = para.strip()
        if not para:
            lines.append("")
            continue

        current = ""
        for ch in para:
            candidate = current + ch
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current.rstrip())
                current = ch.lstrip()
            else:
                current = candidate

        if current:
            lines.append(current.rstrip())

    return lines


def _format_tokens(token_count: int) -> str:
    if token_count >= 1000:
        return f"{token_count / 1000:.1f}k"
    return str(token_count)


def _render_card(query: str, answer: str, model: str, elapsed: float, token_count: int) -> bytes:
    """渲染搜索结果为白底图片卡，返回 PNG bytes。"""

    W = max(900, int(_CARD_WIDTH))
    PAD_X = 52
    PAD_TOP = 44
    PAD_BOTTOM = 46
    HEADER_GAP = 34
    BADGE_GAP = 22
    DIVIDER_GAP = 24
    BODY_GAP = 36

    font_title = _get_font(30)
    font_meta = _get_font(25)
    font_badge = _get_font(23)
    font_body = _get_font(max(24, int(_BODY_FONT_SIZE)))
    font_footer = _get_font(28)

    # 颜色
    C_BG = (255, 255, 255)
    C_TEXT = (18, 25, 37)
    C_MUTED = (130, 142, 158)
    C_BORDER = (226, 232, 240)
    C_BADGE_BG = (238, 242, 247)
    C_BADGE_TEXT = (77, 91, 108)

    max_text_width = W - PAD_X * 2
    answer = _clean_answer(answer)
    wrapped_lines = _wrap_text_by_pixel(answer, font_body, max_text_width)
    body_line_h = int(max(38, _BODY_FONT_SIZE * 1.58))
    paragraph_gap = int(body_line_h * 0.45)

    line_heights = [paragraph_gap if line == "" else body_line_h for line in wrapped_lines]
    body_h = sum(line_heights)
    footer_text = f"(耗时: {elapsed:.1f}s, tokens: {_format_tokens(token_count)})"
    footer_h = int(_BODY_FONT_SIZE * 1.55)

    total_h = (
        PAD_TOP
        + 36
        + HEADER_GAP
        + 34
        + BADGE_GAP
        + 1
        + DIVIDER_GAP
        + body_h
        + BODY_GAP
        + footer_h
        + PAD_BOTTOM
    )

    img = Image.new("RGB", (W, total_h), C_BG)
    draw = ImageDraw.Draw(img)

    # Header
    time_str = datetime.now().strftime("%H:%M:%S")
    elapsed_str = f"{elapsed:.1f}s"
    title_x = PAD_X
    title_y = PAD_TOP
    draw.text((title_x, title_y), _CARD_TITLE, font=font_title, fill=C_TEXT)
    title_w = _text_width(draw, _CARD_TITLE, font_title)
    meta_x = title_x + title_w + 24
    draw.text((meta_x, title_y + 1), time_str, font=font_meta, fill=C_MUTED)
    time_w = _text_width(draw, time_str, font_meta)
    divider_x = meta_x + time_w + 20
    draw.line([divider_x, title_y - 8, divider_x, title_y + 39], fill=C_BORDER, width=2)
    draw.text((divider_x + 18, title_y + 1), elapsed_str, font=font_meta, fill=C_MUTED)

    # Model badge
    badge_y = title_y + 36 + HEADER_GAP
    badge_text = model
    badge_w = _text_width(draw, badge_text, font_badge) + 24
    badge_h = 34
    draw.rounded_rectangle([PAD_X, badge_y, PAD_X + badge_w, badge_y + badge_h], radius=6, fill=C_BADGE_BG)
    draw.text((PAD_X + 12, badge_y + 3), badge_text, font=font_badge, fill=C_BADGE_TEXT)

    divider_y = badge_y + badge_h + BADGE_GAP
    draw.line([PAD_X, divider_y, W - PAD_X, divider_y], fill=C_BORDER, width=2)

    # Body
    y = divider_y + DIVIDER_GAP
    for line, line_h in zip(wrapped_lines, line_heights):
        if line:
            draw.text((PAD_X, y), line, font=font_body, fill=C_TEXT)
        y += line_h

    # Footer meta
    y += BODY_GAP
    draw.text((PAD_X, y), footer_text, font=font_footer, fill=C_TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------- Gemini 调用 ----------
def _make_transport() -> httpx.AsyncHTTPTransport:
    return (
        httpx.AsyncHTTPTransport(proxy=_PROXY, http2=False)
        if _PROXY
        else httpx.AsyncHTTPTransport(http2=False)
    )


def _model_name(model: str) -> str:
    return model if model.startswith("models/") else f"models/{model}"


async def _ensure_search_cache() -> str:
    """创建/复用 web_search 专用 cachedContent，避免复用聊天人格 cache。"""
    global _cache_name, _cache_expire_at

    if _cache_name and time.time() < _cache_expire_at - 60:
        return _cache_name

    payload = {
        "model": _model_name(_MODEL),
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "tools": [{"googleSearch": {}}],
        "ttl": f"{_CACHE_TTL_SECONDS}s",
    }

    async with httpx.AsyncClient(
        timeout=30,
        mounts={"https://": _make_transport()},
    ) as client:
        resp = await client.post(_CACHE_URL, params={"key": _API_KEY}, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini cache HTTP {resp.status_code}")

    data = resp.json()
    name = data.get("name", "")
    if not name:
        raise RuntimeError("Gemini cache response missing name")

    _cache_name = name
    _cache_expire_at = time.time() + _CACHE_TTL_SECONDS
    logger.info(f"[web_search] 搜索 cache 已创建: {name}")
    return name


def _detect_image_mime(data: bytes, header_mime: str = "") -> str | None:
    header_mime = header_mime.split(";", 1)[0].strip().lower()
    if header_mime.startswith("image/"):
        return header_mime
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _download_image_inline(url: str) -> tuple[str, str] | None:
    """下载原图字节并转为 Gemini inlineData，不做缩放、压缩或格式转换。"""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"[web_search] 下载图片失败 HTTP {resp.status_code}: {url[:120]}")
                return None

        data = resp.content
        if len(data) < 500:
            logger.warning(f"[web_search] 图片内容过小，跳过: {len(data)} bytes")
            return None

        mime = _detect_image_mime(data, resp.headers.get("content-type", ""))
        if not mime:
            logger.warning(f"[web_search] 无法识别图片格式，跳过: {url[:120]}")
            return None

        encoded = base64.b64encode(data).decode("ascii")
        logger.debug(f"[web_search] 原图已下载: mime={mime} size={len(data)//1024}KB")
        return mime, encoded
    except Exception as e:
        logger.warning(f"[web_search] 下载图片失败: {type(e).__name__}: {e}")
        return None


async def _call_gemini_search(query: str, images: list[tuple[str, str]]) -> tuple[str, int]:
    """
    调用 Gemini 原生 API，启用 googleSearch 工具。
    返回 (回答文本, token 数)。
    """
    parts: list[dict] = [{"text": query}]
    for mime, data in images:
        parts.append({
            "inlineData": {
                "mimeType": mime,
                "data": data,
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "thinkingConfig": {
                "thinkingLevel": _THINKING_LEVEL,
            },
        },
        "systemInstruction": {
            "parts": [{"text": _SYSTEM_PROMPT}]
        }
    }

    url = _GENERATE_URL.format(model=_MODEL, key=_API_KEY)
    transport = _make_transport()

    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        mounts={"https://": transport},
    ) as client:
        for attempt in range(_RETRY_COUNT + 1):
            try:
                resp = await client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < _RETRY_COUNT:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[web_search] Gemini 请求异常，{delay:.1f}s 后重试 "
                        f"{attempt + 1}/{_RETRY_COUNT}: {type(e).__name__}"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            if resp.status_code == 200:
                break

            if resp.status_code in _RETRY_STATUS and attempt < _RETRY_COUNT:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"[web_search] Gemini HTTP {resp.status_code}，{delay:.1f}s 后重试 "
                    f"{attempt + 1}/{_RETRY_COUNT}"
                )
                await asyncio.sleep(delay)
                continue

            raise RuntimeError(f"Gemini API HTTP {resp.status_code}")

    data = resp.json()
    text = extract_gemini_text(data)

    token_count = data.get("usageMetadata", {}).get("totalTokenCount", 0)
    return text.strip(), token_count


async def _call_gemini_cached_search(query: str, images: list[tuple[str, str]]) -> tuple[str, int]:
    """
    使用 web_search 专用 cachedContent 做同模型兜底。
    仍然是 Gemini 视觉 + googleSearch，不切到 DeepSeek/GPT。
    """
    cache_name = await _ensure_search_cache()

    parts: list[dict] = [{"text": query}]
    for mime, data in images:
        parts.append({
            "inlineData": {
                "mimeType": mime,
                "data": data,
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "cachedContent": cache_name,
        "generationConfig": {
            "maxOutputTokens": 2048,
            "thinkingConfig": {
                "thinkingLevel": _THINKING_LEVEL,
            },
        },
    }

    url = _GENERATE_URL.format(model=_MODEL, key=_API_KEY)
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        mounts={"https://": _make_transport()},
    ) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini cache generate HTTP {resp.status_code}")

    data = resp.json()
    text = extract_gemini_text(data)

    token_count = data.get("usageMetadata", {}).get("totalTokenCount", 0)
    return text.strip(), token_count


# ---------- Handler ----------
web_search_cmd = on_message(rule=_web_search_rule(), priority=4, block=True)


@web_search_cmd.handle()
async def handle_web_search(bot: Bot, event: GroupMessageEvent):
    if not _API_KEY or not _MODEL:
        await bot.send(event, "联网搜索尚未配置，请管理员设置 web_search.api_key 和 web_search.model。")
        return
    group_id = event.group_id

    # 冷却
    now = time.time()
    if group_id in _cooldown and now - _cooldown[group_id] < _COOLDOWN:
        return
    _cooldown[group_id] = now

    # 提取查询词
    query = _extract_search_query(event)
    if query is None:
        return

    # 收集图片 URL（当前消息 + 回复消息）
    img_urls: list[str] = []
    for seg in event.message:
        if seg.type == "image" and seg.data.get("sub_type", 0) != 1:
            url = seg.data.get("url") or seg.data.get("file", "")
            if url:
                img_urls.append(url)
    if event.reply:
        for seg in event.reply.message:
            if seg.type == "image" and seg.data.get("sub_type", 0) != 1:
                url = seg.data.get("url") or seg.data.get("file", "")
                if url:
                    img_urls.append(url)

    if not query and not img_urls:
        logger.info(f"[web_search] 空查询且无图片，静默跳过 | group={group_id}")
        return

    if not query:
        query = "请描述这张图片的内容"

    images: list[tuple[str, str]] = []
    if img_urls:
        for url in img_urls[:_MAX_IMAGES]:
            image = await _download_image_inline(url)
            if image:
                images.append(image)
        if not images:
            logger.warning(f"[web_search] 图片全部下载失败，静默跳过 | group={group_id} urls={len(img_urls)}")
            _cooldown.pop(group_id, None)
            return

    if not query and not images:
        logger.info(f"[web_search] 空查询且无可用图片，静默跳过 | group={group_id}")
        _cooldown.pop(group_id, None)
        return

    t_start = time.time()
    logger.info(
        f"[web_search] 搜索 | group={group_id} query={query[:60]!r} "
        f"imgs={len(images)}"
    )

    try:
        try:
            answer, token_count = await _call_gemini_search(query, images)
        except Exception as primary_err:
            if not _CACHE_FALLBACK:
                raise
            logger.warning(
                f"[web_search] 直连 Gemini 失败，尝试 cache 路径兜底 | "
                f"{type(primary_err).__name__}"
            )
            answer, token_count = await _call_gemini_cached_search(query, images)

        elapsed = time.time() - t_start
        answer = _clean_answer(answer)

        if not answer:
            logger.info(f"[web_search] 空结果，静默跳过 | group={group_id} 耗时={elapsed:.1f}s")
            return

        # 渲染图片卡
        card_bytes = _render_card(query, answer, _MODEL, elapsed, token_count)
        await bot.send(event, MessageSegment.image(card_bytes))
        logger.info(f"[web_search] 完成 | group={group_id} 耗时={elapsed:.1f}s tokens={token_count}")

    except Exception as e:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.warning(
            f"[web_search] 搜索失败 | group={group_id} 耗时={elapsed:.1f}s | "
            f"{type(e).__name__}"
        )
