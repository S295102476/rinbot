"""帮助菜单插件 — #指令一览 以合并转发方式分类展示所有功能"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

help_cmd = on_command("#指令一览", priority=2, block=True)

# 每个分类 (标题, 内容)
HELP_SECTIONS = [
    (
        "小游戏",
        "#小游戏　选择游戏\n#五子棋 / #井字棋 / #成语填空 / #猜数字　查看教程，不会开局\n#五子棋 对战 / #井字棋 对战　默认娱乐（可加：认真、后手）\n#五子棋 双人 / #井字棋 @群友　群友对战\n#加入游戏　加入棋局或接受邀请\n#落子 H8 / #落子 5　落子，局内玩家可直接发坐标\n#棋盘　查看棋局\n#悔棋 / #同意悔棋 / #拒绝悔棋\n#成语填空 单人　随机挖两空，10题/整局10分钟\n#成语填空 抢答　整局15分钟，全群先答对10题获胜\n局内直接发完整四字成语，或 #作答 完整成语\n裸答错静默，#作答答错有提示；均不换题\n60秒未答对只揭示一个字，继续当前题；局外四字聊天不捕获\n#题目 / #跳过（仅成语单人，揭晓答案后换题）\n#猜数字 单人 / #猜数字 抢答　四位不重复，首位非0\n#猜 1234　A=数字位置都对，B=数字对位置错；#题目看记录\n#认输 / #结束游戏　结束本局；抢答仅发起者或管理员可结束",
    ),
    (
        "象棋 / 围棋",
        "#象棋 / #围棋　查看教程，不会开局\n#象棋 对战 娱乐 / #象棋 对战 认真 后手\n#围棋 对战　默认9路娱乐，黑先白贴7.5点\n#围棋 对战 认真 19路 后手　也可选13路\n#象棋 双人 / #围棋 13路 @群友　群友对战\n#走棋 炮二平五 / #走棋 H3-E3　象棋，局内可直接发完整走法\n#落子 D4 / #停一手　围棋，坐标跳过I、行从下向上\n#求和 / #同意和棋 / #拒绝和棋　仅象棋双人，60秒有效\n围棋连续停一手后：#标死 D4 / #取消死子 D4 / #确认数目\n修改死子需要双方重新确认；有争议 #继续对局\n#棋盘 / #悔棋 / #同意悔棋 / #拒绝悔棋 / #认输 / #结束游戏\n本地引擎下棋，不调用聊天模型；共用每群一局，不计好感或积分",
    ),
    (
        "日常",
        "#签到　　每日签到。\n#娶群友　每日随机匹配一位群友,\n#好感度 / #查询好感度　查看本群好感度\n🦌　🦌一下\n引用消息后@机器人并发送“撤回”　尝试撤回该消息",
    ),
    (
        "搜索 / 翻译 / 记忆",
        "#搜索 <问题>　　　　　联网搜索，支持多轮对话\n#清除搜索　　　　　　重置搜索上下文\n#翻译 <内容>。<语言>　翻译（不写语言默认译中文）\n  例：#翻译 你好。日语\n#我的记忆　　查看 AI 记住的信息\n#清除历史　　管理员清除自己的对话历史\n#清除历史 <QQ号>　管理员清除指定用户的对话历史",
    ),
    (
        "图片生成 / 编辑",
        "#生图 <提示词>　另一种绘图，可附图或回复图作参考\n#图片生成 <描述>　文生图（冷却60秒）\n#图片编辑 <描述>　编辑图片，附图或回复图片+描述\n#手办化　　　　　将图片转为手办风格\n#美少女化　　　　将图片转为二次元动画美少女\n  - 直接发：取自己头像\n  - @某人：取对方头像\n  - 回复图片后发：取回复的图\n#高质量化　　　　增强图片清晰度（用法同上）",
    ),
    (
        "涩图 / Pixiv 排行",
        "#涩图　　　　　随机发送高质量插画\n#涩图 <标签>　按标签搜索（建议日文或英文标签）\n#p日榜 [日期]　Pixiv 日榜 Top10，日期格式：416 / 4-16 / 4月16日\n#p周榜　　　　Pixiv 周榜\n#p月榜　　　　Pixiv 月榜\n#p原创榜　　　原创周榜\n#p新人榜　　　新人周榜",
    ),
    (
        "搜图（暂时不可用）",
        "#搜图 <关键词>　Pixiv 关键词搜图\n#搜图 [附图]　　以图搜图（识别图片来源）\n#搜本子　[附图]　　以图搜本子（nhentai/ehentai/jmcomic）",
    ),
    (
        "杀戮尖塔2 百科（已暂停维护）",
        "#尖塔 <名称>　　　　　全局搜索卡牌/遗物/药水/怪物\n#尖塔 卡牌/遗物/药水/怪物/词条 <名称>　精确搜索\n#尖塔 今日挑战　　　查看当天每日挑战内容",
    ),
    (
        "游戏查询",
        "ww帮助　　鸣潮\nzzz帮助　 绝区零\nend帮助　 终末地\nnte帮助　 NTE 异环\n#GB使用率 [大师/S段]　GBVSR 角色使用率\n#GB 帮助　GBVSR 帧数查询说明",
    ),
    (
        "COC 跑团骰子",
        ".help　查看完整 COC 骰子指令",
    ),
    (
        "值班人设",
        "#值班表　查看本周排班\n#值班表 下周　查看下周排班",
    ),
    (
        "GBVSR 查询",
        "#GB使用率 [大师/S段]　GBVSR 角色使用率\n#GB 帮助　GBVSR 帧数查询说明",
    )
]

HELP_FALLBACK = "\n\n".join(f"[ {title} ]\n{content}" for title, content in HELP_SECTIONS)


def enabled_help_sections():
    from runtime_config import feature_enabled, load_config
    config = load_config()
    flags = {
        "小游戏": "minigames", "象棋 / 围棋": "minigames",
        "涩图 / Pixiv 排行": "pixiv", "游戏查询": "gsuid",
        "GBVSR 查询": "gbvsr", "值班人设": "persona",
    }
    sections = []
    for title, content in HELP_SECTIONS:
        if title in {"搜图（暂时不可用）", "杀戮尖塔2 百科（已暂停维护）"}:
            continue
        if title in flags and not feature_enabled(config, flags[title]):
            continue
        if title == "图片生成 / 编辑":
            if not (feature_enabled(config, "image_gen") or feature_enabled(config, "nai")):
                continue
            content += "\n绘图指令需配置对应的 Images 接口或 NovelAI Token。"
        if title == "搜索 / 翻译 / 记忆":
            if not feature_enabled(config, "chat"):
                continue
            if not feature_enabled(config, "web_search"):
                content = "\n".join(line for line in content.splitlines()
                                    if not line.startswith(("#搜索", "#清除搜索")))
        if title == "象棋 / 围棋":
            content += "\n人机模式需另行安装引擎；群友双人对战可直接使用。"
        if title == "游戏查询":
            content = "\n".join(line for line in content.splitlines() if not line.startswith("#GB"))
            content += "\n需要管理员接入 Core 并安装对应插件。"
        sections.append((title, content))
    return sections


@help_cmd.handle()
async def handle_help(bot: Bot, event: GroupMessageEvent):
    sections = enabled_help_sections()
    nodes = [
        {
            "type": "node",
            "data": {
                "name": title,
                "uin": str(bot.self_id),
                "content": content,
            },
        }
        for title, content in sections
    ]
    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception:
        await help_cmd.send("\n\n".join(f"[ {title} ]\n{content}" for title, content in sections))
