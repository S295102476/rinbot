import json
import time
import httpx
import yaml
import random
import redis as redis_lib
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
gc_cfg = ai_cfg["group_chat"]
rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

# 冷却时间记录
_last_reply_time: dict[int, float] = {}


def is_group_enabled(group_id: int) -> bool:
    return group_id in gc_cfg.get("enabled_groups", [])


def not_at_bot_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        return f"[CQ:at,qq={bot.self_id}]" not in event.raw_message
    return Rule(_rule)


group_listener = on_message(rule=not_at_bot_rule(), priority=10, block=False)

@group_listener.handle()
async def handle_group_msg(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    if not is_group_enabled(group_id):
        return

    plain = event.get_plaintext().strip()
    if not plain or plain.startswith("#"):
        return

    # 存入群聊上下文
    key = f"ai:group:{group_id}"
    entry = f"{event.user_id}: {plain}"
    rds.rpush(key, entry)
    size = rds.llen(key)
    context_size = gc_cfg.get("context_size", 50)
    if size > context_size:
        rds.ltrim(key, size - context_size, -1)

    # 判断是否触发被动回复
    if not should_trigger(group_id, plain):
        return

    reply = await passive_reply(group_id)
    if reply:
        await group_listener.send(reply)


def should_trigger(group_id: int, message: str) -> bool:
    cooldown = gc_cfg.get("cooldown_seconds", 10)
    last = _last_reply_time.get(group_id, 0)
    if time.time() - last < cooldown:
        return False

    rate = gc_cfg.get("passive_rate", 0.005)
    for kw in gc_cfg.get("keywords", []):
        if kw in message:
            rate = gc_cfg.get("keyword_rate", 0.1)
            break

    return random.random() < rate


async def passive_reply(group_id: int) -> str | None:
    key = f"ai:group:{group_id}"
    context = rds.lrange(key, 0, -1)
    if not context:
        return None

    system_content = ai_cfg["system_prompt"] + "\n\n【群聊模式】以下是群聊中其他人的对话记录，你可以自然地参与讨论，保持��短自然。"
    context_text = "[最近的群聊记录]\n" + "\n".join(context)

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": context_text}
    ]

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={"model": ai_cfg["model"], "messages": messages}
            )
            reply = resp.json()["choices"][0]["message"]["content"].strip()
            _last_reply_time[group_id] = time.time()
            return reply
    except Exception:
        return None