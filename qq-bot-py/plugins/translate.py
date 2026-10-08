"""翻译插件 — 文字使用 Responses API，图片 OCR/翻译使用官方 Gemini Vision。"""

import base64
import re
import httpx
import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.log import logger
from .translation_routing import select_translation_provider

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
_VISION_CFG = ai_cfg.get("vision", {})
_VISION_URL = _VISION_CFG.get("api_url", ai_cfg["api_url"])
_VISION_KEY = _VISION_CFG.get("api_key", ai_cfg["api_key"])
_VISION_MODEL = _VISION_CFG.get("model", ai_cfg["model"])
_VISION_PROXY = ai_cfg.get("proxy", "") or None

_SYSTEM_PROMPT = (
    "你是一位专业翻译。用户会发来一段文本或图片，可能在末尾或句中注明目标语言"
    "（如「日语」「英语」「翻译成法语」等）。"
    "若有图片，请识别图片中所有文字并翻译；若同时有文本，文本为翻译指令或目标语言说明。"
    "请识别目标语言并给出翻译结果。"
    "若未注明目标语言，默认翻译为中文（原文若已是中文则翻译为英文）。"
    "只输出翻译结果，不要解释，不要加任何前缀或多余说明。"
)

translate_cmd = on_command("#翻译", priority=5, block=True)


async def _extract_image(event: GroupMessageEvent) -> bytes | None:
    """从当前消息或被回复消息中提取第一张图片字节，失败返回 None。"""
    sources = list(event.message)
    if event.reply:
        sources = sources + list(event.reply.message)
    for seg in sources:
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file", "")
            if url and url.startswith("http"):
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        r = await client.get(url)
                    if r.status_code == 200:
                        return r.content
                except Exception as e:
                    logger.warning(f"[translate] 下载图片失败: {e}")
    return None


@translate_cmd.handle()
async def handle_translate(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    img_bytes = await _extract_image(event)

    if not text and img_bytes is None:
        await translate_cmd.send(
            "用法：\n"
            "  #翻译 <文字>          → 翻译文字\n"
            "  #翻译 <文字> + 附图   → 翻译图片中的文字（文字可指定目标语言）\n"
            "  #翻译 + 附图          → 自动识别并翻译图片文字\n"
            "示例：#翻译 你好。日语"
        )
        return

    await translate_cmd.send(await _do_translate(text, img_bytes))


async def _do_translate(text: str, img_bytes: bytes | None = None) -> str:
    # 构建 user content：有图片时用 vision 数组格式
    if img_bytes is not None:
        b64 = base64.b64encode(img_bytes).decode()
        # 简单判断图片格式
        mime = "image/png" if img_bytes[:4] == b"\x89PNG" else "image/jpeg"
        user_content: list | str = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": text if text else "请翻译图片中的所有文字"},
        ]
    else:
        user_content = text

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        from .ai_chat import _call_primary
        result = await _call_primary(messages, timeout=60)
        return re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
    except Exception as e:
        logger.warning(f"[translate] 请求异常: {e}")
        return "翻译服务暂时不可用，请稍后再试。"
