"""图片生成插件 — #图片生成 <描述>

使用 chatgpt2api 代理，调用 gpt-image-1 生成图片。
"""
import base64
import time

import httpx
import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

img_gen_cfg = config.get("image_gen", {})
API_URL = img_gen_cfg.get("api_url", "https://api.example.com/v1")
API_KEY = img_gen_cfg.get("api_key", "")
MODEL = img_gen_cfg.get("model", "gpt-image-1")
SIZE = img_gen_cfg.get("size", "1024x1024")
QUALITY = img_gen_cfg.get("quality", "low")
COOLDOWN = img_gen_cfg.get("cooldown", 60)
TIMEOUT = img_gen_cfg.get("timeout", 120)
STYLE_SUFFIX = img_gen_cfg.get("style_suffix", "")
ALLOWED_GROUPS: set[int] = set(img_gen_cfg.get("allowed_groups", []))

# 群冷却记录 {group_id: last_call_timestamp}
_cooldown: dict[int, float] = {}

image_gen_cmd = on_command("#图片生成", priority=5, block=True)


@image_gen_cmd.handle()
async def handle_image_gen(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    # 群白名单检查（空列表=全部允许）
    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        return

    prompt = args.extract_plain_text().strip()
    if not prompt:
        return

    # 冷却检查
    now = time.time()
    if group_id in _cooldown and now - _cooldown[group_id] < COOLDOWN:
        return

    _cooldown[group_id] = now

    await bot.send(event, "正在生成图片，请稍候…")

    full_prompt = f"{prompt}{STYLE_SUFFIX}" if STYLE_SUFFIX else prompt
    t_start = time.time()
    logger.info(f"[image_gen] 开始生成 | group={group_id} model={MODEL} size={SIZE} quality={QUALITY} prompt={prompt[:50]!r}")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{API_URL}/images/generations",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "prompt": full_prompt,
                    "n": 1,
                    "size": SIZE,
                    "quality": QUALITY,
                    "response_format": "b64_json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        b64 = data["data"][0]["b64_json"]
        img_bytes = base64.b64decode(b64)
        elapsed = time.time() - t_start
        await bot.send(event, MessageSegment.image(img_bytes))
        logger.info(f"[image_gen] 生成成功 | group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r}")

    except httpx.TimeoutException:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.warning(
            f"[image_gen] 请求超时（>{TIMEOUT}s，已等待 {elapsed:.1f}s）| "
            f"group={group_id} model={MODEL} prompt={prompt[:50]!r} — 冷却已重置"
        )
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_gen] HTTP {e.response.status_code} 错误 | "
            f"group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r} | "
            f"响应: {e.response.text[:200]}"
        )
    except Exception as e:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_gen] 生成异常 {type(e).__name__} | "
            f"group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r} | {e}"
        )
