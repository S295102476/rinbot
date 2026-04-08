import json
import yaml
import redis as redis_lib
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.message import MessageSegment

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

memory_cmd = on_command("#我的记忆", priority=5, block=True)
clear_memory_cmd = on_command("#清除记忆", priority=5, block=True)
clear_history_cmd = on_command("#清除历史", priority=5, block=True)
clear_search_cmd = on_command("#清除搜索", priority=5, block=True)

@memory_cmd.handle()
async def handle_memory(bot: Bot, event: GroupMessageEvent):
    user_id = event.user_id
    raw = rds.lrange(f"ai:memory:{user_id}", 0, -1)
    if not raw:
        await memory_cmd.send("🧠 暂无记忆记录")
        return

    # 构建合并转发消息
    messages = []
    for i, item in enumerate(reversed(raw)):
        data = json.loads(item)
        content = f"{i + 1}. {data['content']}（更新时间：{data['time']}）"
        messages.append({
            "type": "node",
            "data": {
                "name": "记忆系统",
                "uin": str(bot.self_id),
                "content": content
            }
        })

    await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=messages)


@clear_memory_cmd.handle()
async def handle_clear_memory(event: GroupMessageEvent):
    rds.delete(f"ai:memory:{event.user_id}")
    await clear_memory_cmd.send("已清除你的所有记忆！")


@clear_history_cmd.handle()
async def handle_clear_history(event: GroupMessageEvent):
    rds.delete(f"ai:chat:{event.user_id}")
    await clear_history_cmd.send("已清除你的对话记录！")


@clear_search_cmd.handle()
async def handle_clear_search(event: GroupMessageEvent):
    rds.delete(f"ai:search:{event.user_id}")
    await clear_search_cmd.send("已清除你的搜索记录！")