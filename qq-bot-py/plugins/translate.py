"""翻译插件 — #翻译 触发，使用 GPT-5.5 进行单轮翻译"""

import re
import httpx
import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.log import logger

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
_translate_cfg = ai_cfg.get("translate", {})
_TRANSLATE_MODEL = _translate_cfg.get("model", "gpt-5.5")
_API_URL = ai_cfg["api_url"]
_API_KEY = ai_cfg["api_key"]

_SYSTEM_PROMPT = (
    "你是一位专业翻译。用户会发来一段文本，可能在末尾或句中注明目标语言"
    "（如「日语」「英语」「翻译成法语」等）。"
    "请识别目标语言并给出翻译结果。"
    "若未注明目标语言，默认翻译为中文（原文若已是中文则翻译为英文）。"
    "只输出翻译结果，不要解释，不要加任何前缀或多余说明。"
)

translate_cmd = on_command("#翻译", priority=5, block=True)


@translate_cmd.handle()
async def handle_translate(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await translate_cmd.send("用法：#翻译 <内容>。<目标语言>\n示例：#翻译 你好。日语")
        return
    await translate_cmd.send(await _do_translate(text))


async def _do_translate(text: str) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                _API_URL,
                headers={"Authorization": f"Bearer {_API_KEY}"},
                json={"model": _TRANSLATE_MODEL, "messages": messages},
            )
            data = resp.json()
            if "error" in data:
                logger.warning(f"[translate] API 报错: {data['error']}")
                return f"翻译失败：{data['error'].get('message', '未知错误')}"
            result = data["choices"][0]["message"]["content"].strip()
            result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
            return result
    except Exception as e:
        logger.warning(f"[translate] 请求异常: {e}")
        return "翻译服务暂时不可用，请稍后再试。"
