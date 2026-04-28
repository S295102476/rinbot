"""帮助菜单插件 — #帮助 查看机器人所有功能"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, Message

help_cmd = on_command("#指令一览", priority=2, block=True)

HELP_TEXT = """ 指令一览

━━ 日常功能 ━━
#签到　　　　每日签到，生成专属卡片
#娶群友　　　每日随机匹配一位群友

━━ 搜索 & 翻译 & 生成 ━━
#搜索 <内容>　　　　　  AI 联网搜索
#翻译 <内容>。<语言>　  AI 翻译（不写语言默认译中文）
#搜图 <关键词>　　　　  搜索 Pixiv 插画
#搜图 [图片]　　　　　  以图搜图
#图片生成 <描述>　　　  AI 生成图片（冷却60秒）
#图片编辑 <描述>　　　  AI 编辑图片（附图或回复图+描述）

━━ 涩图 & 排行 ━━
#涩图　　　　随机搜索涩图
#涩图 <标签>　按标签搜索（最好日，英）
#p日榜 [日期]　Pixiv 日榜 Top10
#p周榜　　　　Pixiv 周榜 Top10
#p月榜　　　　Pixiv 月榜 Top10
#p原创榜　　　Pixiv 原创周榜
#p新人榜　　　Pixiv 新人周榜

━━ 杀戮尖塔2 ━━
#尖塔 卡牌 <名称>　搜索卡牌
#尖塔 遗物 <名称>　搜索遗物
#尖塔 药水 <名称>　搜索药水
#尖塔 怪物 <名称>　搜索怪物
#尖塔 词条 <名称>　搜索每日挑战词条
#尖塔 <名称>　　　 全局搜索
#尖塔 今日挑战　　 查看每日挑战

━━ COC 跑团骰子 ━━
.r <表达式>　　掷骰（如 .r 3d6+2）
.r N#表达式　多连骰（如 .r 3#d6）
.rd[面数]　　 快速掷骰（.rd .rd6 .rd100）
.ra <属性> [值] 属性检定
.rb/.rp [N]　 奖励骰/惩罚骰
.rh [表达式]　暗骰（私聊发送）
.coc [N]　　 快速建卡
.name <名字>  设置角色名
.st <属性 值> 设置属性（可批量）
.st show　　 查看角色卡
.del <属性>　删除某属性
.clr　　　　 清空角色卡
.hp+N / .hp-N 修改 HP
.mp+N / .mp-N 修改 MP
.san+N / .san-N 修改 SAN
.sc <成功/失败> 理智检定
.en <属性>　 成长检定
.ti / .li　　随机疯狂症状
.jrrp　　　　今日人品
.help　　　　COC 骰子帮助

━━ 游戏查询 ━━
ww帮助　　鸣潮相关指令
sr帮助　　崩坏：星穹铁道
zzz帮助　 绝区零
ark帮助　 明日方舟
end帮助 终末地

━━ 记忆管理 ━━
#我的记忆　　查看 AI 记住的内容
#清除记忆　　清除 AI 记忆
#清除历史　　清除对话历史
#清除搜索　　清除搜索上下文
"""


@help_cmd.handle()
async def handle_help(bot: Bot, event: GroupMessageEvent):
    await help_cmd.send(HELP_TEXT)
