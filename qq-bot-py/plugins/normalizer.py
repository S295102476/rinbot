"""消息预处理：将全角＃统一为半角#，并去除#与命令之间的空格
例：＃帮助 → #帮助，# 签到 → #签到，＃ 涩图 → #涩图
"""
import re
from nonebot.message import event_preprocessor
from nonebot.adapters.onebot.v11 import MessageEvent


@event_preprocessor
async def normalize_command_prefix(event: MessageEvent):
    """全角→半角，去除 # 后多余空格"""
    if not event.message:
        return
    seg = event.message[0]
    if seg.type != "text":
        return
    text = seg.data.get("text", "")
    new_text = text.replace("＃", "#")          # 全角 → 半角
    new_text = re.sub(r"^#\s+", "#", new_text)  # # 签到 → #签到
    if new_text != text:
        seg.data["text"] = new_text
