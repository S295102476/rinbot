import json
import httpx
import yaml
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import to_me
from nonebot.plugin import PluginMetadata
import redis as redis_lib

# 读取配置
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
rds = redis_lib.Redis(
    host=config["redis"]["host"],
    port=config["redis"]["port"],
    decode_responses=True
)

MAX_HISTORY = ai_cfg.get("max_history", 100)

# @机器人触发
ai_chat = on_message(rule=to_me(), priority=5, block=True)

@ai_chat.handle()
async def handle_chat(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    group_id = event.group_id
    # 去掉@部分，只取纯文本
    message = event.get_plaintext().strip()
    if not message:
        return

    # 判断群是否在白名单，决定是否注入群上下文
    enabled_groups = ai_cfg["group_chat"].get("enabled_groups", [])
    context_group_id = group_id if group_id in enabled_groups else None

    reply = await chat(user_id, message, context_group_id)
    await ai_chat.send(reply)


async def chat(user_id: int, user_message: str, group_id: int = None) -> str:
    key = f"ai:chat:{user_id}"
    raw_history = rds.lrange(key, 0, -1)
    history = [json.loads(item) for item in raw_history]

    # 判断是否有专属人设
    special_users = ai_cfg.get("special_users", {})
    prompt = ai_cfg["system_prompt"]
    if user_id in special_users:
        prompt = prompt + "\n" + special_users[user_id]

    messages = [{"role": "system", "content": prompt}]

    # 注入群聊上下文
    if group_id is not None:
        group_context = rds.lrange(f"ai:group:{group_id}", 0, -1)
        if group_context:
            context_text = "[最近的群聊记录，仅供参考]\n" + "\n".join(group_context)
            messages.append({"role": "user", "content": context_text})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近在聊的内容。"})

    # 加入历史记录
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            ai_cfg["api_url"],
            headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
            json={"model": ai_cfg["model"], "messages": messages}
        )
        reply = resp.json()["choices"][0]["message"]["content"].strip()

    # 存入 Redis
    rds.rpush(key, json.dumps({"role": "user", "content": user_message}, ensure_ascii=False))
    rds.rpush(key, json.dumps({"role": "assistant", "content": reply}, ensure_ascii=False))

    size = rds.llen(key)
    if size > MAX_HISTORY:
        rds.ltrim(key, size - MAX_HISTORY, -1)

    # 异步提取记忆（不等待结果）
    import asyncio
    asyncio.create_task(extract_memory(user_id, user_message))

    return reply


async def extract_memory(user_id: int, user_message: str):
    """异步提取记忆，总结用户说的话存入 Redis"""
    try:
        prompt = f"请把以下用户说的话总结成一句简短的第三人称陈述句（15字以内），只输出这句话，不要加任何多余内容。\n用户说：{user_message}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={"model": ai_cfg["model"], "messages": [{"role": "user", "content": prompt}]}
            )
            summary = resp.json()["choices"][0]["message"]["content"].strip()

        from datetime import datetime
        memory = json.dumps({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": summary
        }, ensure_ascii=False)

        key = f"ai:memory:{user_id}"
        rds.rpush(key, memory)
        size = rds.llen(key)
        if size > 100:
            rds.ltrim(key, size - 100, -1)
    except Exception:
        pass


# 清除历史
async def clear_history(user_id: int):
    rds.delete(f"ai:chat:{user_id}")