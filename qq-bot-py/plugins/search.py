import json
import httpx
import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message
import redis as redis_lib

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

search_cmd = on_command("#搜索", priority=5, block=True)

@search_cmd.handle()
async def handle_search(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    keyword = args.extract_plain_text().strip()
    if not keyword:
        await search_cmd.send("请输入搜索内容，例如：#搜索 Java是什么")
        return

    reply = await search(event.user_id, keyword)
    await search_cmd.send(reply)


async def search(user_id: int, user_message: str) -> str:
    key = f"ai:search:{user_id}"
    raw_history = rds.lrange(key, 0, -1)
    history = [json.loads(item) for item in raw_history]

    messages = [{"role": "system", "content": ai_cfg["search_system_prompt"]}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={"model": ai_cfg["model"], "messages": messages}
            )
            reply = resp.json()["choices"][0]["message"]["content"].strip()

        rds.rpush(key, json.dumps({"role": "user", "content": user_message}, ensure_ascii=False))
        rds.rpush(key, json.dumps({"role": "assistant", "content": reply}, ensure_ascii=False))
        size = rds.llen(key)
        if size > 100:
            rds.ltrim(key, size - 100, -1)

        return reply
    except Exception as e:
        return f"搜索出错了：{e}"