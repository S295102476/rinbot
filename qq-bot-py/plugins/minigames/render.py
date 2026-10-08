"""Local, persona-themed Pillow cards. Rendering never fetches remote assets."""
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import re
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

from plugins.minigame_registry import GAME_BY_ID, GAME_NAMES, GAME_REGISTRY

BG = (248, 246, 247)
INK = (39, 43, 55)
MUTED = (105, 114, 130)
ACCENT = (151, 54, 79)
THEMES = {
    "rin": {"name": "凛", "accent": ACCENT, "pale": (248, 236, 241), "bg": BG},
    "eres": {"name": "艾蕾", "accent": (153, 113, 35), "pale": (250, 243, 225), "bg": (250, 248, 242)},
    "ishtar": {"name": "伊什塔尔", "accent": (17, 123, 123), "pale": (229, 245, 242), "bg": (242, 249, 248)},
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GAME_ASSET_DIR = PROJECT_ROOT / "data" / "game"


@lru_cache(maxsize=32)
def _font(size, bold=False):
    choices = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for filename in choices:
        try:
            return ImageFont.truetype(filename, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text(draw, xy, text, size=24, color=INK, bold=False, width=None):
    """Single-line labels may elide; tutorial paragraphs use _paragraph instead."""
    font = _font(size, bold)
    text = str(text).replace("\n", " ").replace("\r", " ")
    if width:
        original = text
        while text and draw.textlength(text, font=font) > width:
            text = text[:-1]
        if text != original:
            while text and draw.textlength(text + "…", font=font) > width:
                text = text[:-1]
            text += "…"
    draw.text(xy, text, font=font, fill=color)


def _wrap(draw, text, width, size=23, bold=False):
    """Measure each line, retaining every character (including long tutorials)."""
    font = _font(size, bold)
    lines = []
    for paragraph in str(text).replace("\r", "").split("\n"):
        line = ""
        for char in paragraph:
            if line and draw.textlength(line + char, font=font) > width:
                if char in "，。！？；：、）》】」』,.!?;:)" and len(line) > 1:
                    # Keep closing punctuation with its preceding character,
                    # without exceeding the measured column width.
                    lines.append(line[:-1])
                    line = line[-1] + char
                else:
                    lines.append(line)
                    line = char
            else:
                line += char
        lines.append(line)
    return lines


def _paragraph(draw, xy, text, width, size=23, color=INK, bold=False, gap=10):
    lines = _wrap(draw, text, width, size, bold)
    for offset, line in enumerate(lines):
        draw.text((xy[0], xy[1] + offset * (size + gap)), line, font=_font(size, bold), fill=color)
    return len(lines) * (size + gap)


def _center(draw, xy, text, size=20, color=MUTED, bold=False):
    font = _font(size, bold)
    box = draw.textbbox((0, 0), str(text), font=font)
    draw.text((xy[0] - (box[2] - box[0]) / 2, xy[1] - (box[3] - box[1]) / 2 - box[1]), str(text), font=font, fill=color)


def _png(image):
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _local_image(source):
    """Decode bounded, local files or already downloaded bytes; no URL support."""
    try:
        if isinstance(source, bytes):
            if not source or len(source) > 8 * 1024 * 1024:
                return None
            source = BytesIO(source)
        else:
            path = Path(str(source))
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or path.stat().st_size > 8 * 1024 * 1024:
                return None
            source = path
        with Image.open(source) as picture:
            if picture.width * picture.height > 16_000_000:
                return None
            return picture.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _theme(persona):
    persona = persona if isinstance(persona, dict) else {"id": persona or "rin"}
    persona_id = persona.get("id") or "rin"
    theme = THEMES.get(persona_id, THEMES["rin"])
    return persona_id, persona.get("name") or theme["name"], theme


def _card(draw, bounds, *, fill="white", radius=24, outline=None):
    x1, y1, x2, y2 = bounds
    draw.rounded_rectangle((x1, y1 + 5, x2, y2 + 5), radius=radius, fill=(224, 227, 230))
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=outline, width=2)


def _avatar(image, draw, xy, source, theme, *, active=False, size=68):
    x, y = xy
    border = theme["accent"] if active else (210, 216, 223)
    draw.ellipse((x - 5, y - 5, x + size + 4, y + size + 4), fill="white", outline=border, width=3 if active else 2)
    picture = _local_image(source)
    if picture is not None:
        avatar = ImageOps.fit(picture, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size))
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        tile = Image.new("RGBA", (size, size), (239, 241, 245, 255))
        tile.alpha_composite(avatar)
        image.paste(tile.convert("RGB"), (x, y), mask)
    else:
        draw.ellipse((x, y, x + size - 1, y + size - 1), fill=(235, 239, 244))
        draw.ellipse((x + size * .36, y + size * .19, x + size * .64, y + size * .47), fill=(153, 165, 181))
        draw.rounded_rectangle((x + size * .21, y + size * .53, x + size * .79, y + size * .82), radius=10, fill=(153, 165, 181))


def _mascot(image, draw, persona_id, theme, *, bounds=(40, 156, 172, 222)):
    left, top, width, height = bounds
    picture = _local_image(GAME_ASSET_DIR / f"{persona_id}.png") if persona_id in THEMES else None
    if picture is not None:
        bounds = picture.getchannel("A").getbbox()
        if bounds:
            picture = picture.crop(bounds)
            picture = ImageOps.contain(picture, (width, height), method=Image.Resampling.LANCZOS)
            image.paste(picture, (left + (width - picture.width) // 2, top + (height - picture.height) // 2), picture)
            return
    radius = min(width * 58 / 172, height * 58 / 222)
    cx, cy = left + width * 84 / 172, top + height * 84 / 222
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=theme["pale"], outline=theme["accent"], width=3)
    _center(draw, (cx, cy), theme["name"], min(25, max(13, round(width * 25 / 172))), theme["accent"], True)


def _duration(seconds):
    seconds = max(1, int(seconds))
    return f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


def _menu_rows(game):
    title = GAME_NAMES[game]
    if game == "number":
        return [
            ("#猜数字 单人", "自己推理四位数字，10 分钟内猜中并计步"),
            ("#猜数字 抢答", "全群共同推理，15 分钟内首个 4A 获胜"),
            ("#猜 1234", "提交四位不重复数字，首位不能为 0"),
            ("#作答 1234", "兼容写法；日常聊天中的数字不参与猜题"),
            ("#题目", "查看最近猜测、A/B 反馈及剩余期限"),
            ("#结束游戏", "本人、抢答发起者或管理员可结束游戏"),
        ]
    if game == "idiom":
        return [
            ("#成语填空 单人", "自己挑战十题，整局最多 10 分钟"),
            ("#成语填空 抢答", "全群直接参与，率先答对十题获胜"),
            ("#作答 春暖花开 / 直接发成语", "填写完整四字成语，不只填缺少的字"),
            ("#题目", "查看当前题目、进度和本局比分"),
            ("#跳过", "仅单人可用；揭晓答案并记为未答对"),
            ("#结束游戏", "单人玩家、抢答发起者或管理员可结束"),
        ]
    example = {"gomoku": "H8", "tictactoe": "5", "xiangqi": "B3 E3", "go": "D4"}[game]
    rows = [
        (f"#{title} 对战", "和机器人开始一局，默认娱乐难度、你先手"),
        (f"#{title} 对战 认真 后手", "难度：娱乐 / 认真；顺序：先手 / 后手"),
        (f"#{title} 双人", "公开等待群友加入，也可加「后手」"),
        (f"#{title} @群友", "邀请一位群友，仅被邀请者可加入"),
        ("#加入游戏", "加入公开棋局，或接受发给你的邀请"),
        (f"#落子 {example}  /  直接发 {example}", "轮到你时落子，完整发送坐标或编号"),
        ("#棋盘  /  #悔棋", "查看当前对局，或申请撤销最后一轮"),
        ("#同意悔棋  /  #拒绝悔棋", "双人对手处理申请；60 秒内有效"),
        ("#认输  /  #结束游戏", "认输或取消对局，立即释放本群棋局"),
    ]
    if game == "xiangqi":
        rows[5] = ("#落子 炮二平五 / B3 E3", "中文棋谱或起点终点坐标；局内也可直接发送")
        rows.insert(-1, ("#求和 / #同意和棋 / #拒绝和棋", "群友对战可协商和棋；收到申请后由对手回应"))
    elif game == "go":
        rows[0] = ("#围棋 对战", "默认 9 路、娱乐难度、你执黑先手")
        rows[1] = ("#围棋 对战 19路 认真 后手", "棋盘：9 / 13 / 19 路；也可选择娱乐、先手")
        rows.insert(6, ("#停一手", "双方连续停一手，进入死子标记与数目确认"))
        rows.insert(-1, ("#标死 D4 / #取消死子 D4", "按连通棋串标记或恢复；修改后需要重新确认"))
        rows.insert(-1, ("#确认数目 / #继续对局", "双方确认后结算；有分歧可回到棋盘继续下"))
    return rows


def _command_section(draw, top, rows, theme, *, paint=True):
    heights = [max(len(_wrap(draw, left, 390, 21, True)), len(_wrap(draw, right, 382, 20))) * 29 + 22 for left, right in rows]
    bottom = top + 89 + sum(heights) + 22
    if paint:
        _card(draw, (32, top, 928, bottom))
        _text(draw, (56, top + 21), "开局与常用指令", 30, bold=True)
        y = top + 84
        for index, ((left, right), height) in enumerate(zip(rows, heights)):
            if index % 2 == 0:
                draw.rounded_rectangle((50, y, 910, y + height - 3), radius=12, fill=theme["pale"])
            _paragraph(draw, (67, y + 9), left, 390, 21, theme["accent"], True, 8)
            _paragraph(draw, (486, y + 9), right, 382, 20, MUTED, gap=9)
            y += height
    return bottom


def _example_board(draw, game, left, top, theme):
    if game == "xiangqi":
        draw.rounded_rectangle((left, top, left + 274, top + 265), radius=18, fill=(241, 218, 178))
        for index in range(5):
            x, y = left + 45 + index * 45, top + 40 + index * 45
            draw.line((x, top + 40, x, top + 220), fill=(149, 120, 81), width=1)
            draw.line((left + 45, y, left + 225, y), fill=(149, 120, 81), width=1)
            _center(draw, (x, top + 18), chr(65 + index), 18, INK)
            _center(draw, (left + 20, y), 5 - index, 18, INK)
        draw.ellipse((left + 73, top + 113, left + 107, top + 147), outline=theme["accent"], width=3)
        draw.line((left + 110, top + 130, left + 201, top + 130), fill=theme["accent"], width=3)
        draw.polygon(((left + 201, top + 130), (left + 190, top + 123), (left + 190, top + 137)), fill=theme["accent"])
        draw.ellipse((left + 207, top + 112, left + 243, top + 148), fill=(255, 243, 217), outline=(170, 42, 47), width=2)
        _center(draw, (left + 225, top + 130), "炮", 24, (170, 42, 47), True)
        _center(draw, (left + 137, top + 293), "B3 → E3 = 炮八平五示意", 18, theme["accent"], True)
        return
    if game == "go":
        draw.rounded_rectangle((left, top, left + 274, top + 265), radius=18, fill=(241, 218, 178))
        for index, column in enumerate("FGHJK"):
            x, y = left + 47 + index * 45, top + 48 + index * 45
            draw.line((x, top + 48, x, top + 228), fill=(149, 120, 81), width=1)
            draw.line((left + 47, y, left + 227, y), fill=(149, 120, 81), width=1)
            _center(draw, (x, top + 23), column, 18, INK)
            _center(draw, (left + 21, y), 5 - index, 18, INK)
        draw.ellipse((left + 120, top + 121, left + 154, top + 155), fill=(36, 42, 48))
        _center(draw, (left + 137, top + 293), "跳过 I 列，底部为第 1 行", 19, theme["accent"], True)
        return
    if game == "gomoku":
        step = 45
        draw.rounded_rectangle((left, top, left + 274, top + 265), radius=18, fill=(241, 218, 178))
        for index in range(5):
            x, y = left + 47 + index * step, top + 48 + index * step
            draw.line((x, top + 48, x, top + 228), fill=(163, 136, 98), width=1)
            draw.line((left + 47, y, left + 227, y), fill=(163, 136, 98), width=1)
            _center(draw, (x, top + 23), chr(70 + index), 18, MUTED)
            _center(draw, (left + 21, y), 6 + index, 18, MUTED)
        x, y = left + 137, top + 138
        draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=(36, 42, 48))
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(224, 84, 59))
        _center(draw, (left + 137, top + 293), "H8 = H 列、第 8 行", 21, theme["accent"], True)
    elif game == "tictactoe":
        for index in range(9):
            x, y = left + index % 3 * 88, top + index // 3 * 88
            draw.rounded_rectangle((x, y, x + 80, y + 80), radius=13, fill=theme["pale"], outline=(214, 220, 226))
            _center(draw, (x + 40, y + 40), index + 1, 30, theme["accent"], True)
        _center(draw, (left + 128, top + 293), "5 = B2 = 正中间", 21, theme["accent"], True)


def _tutorial_rules(game):
    if game == "xiangqi":
        return [
            ("红先黑后，轮流走子", "九路十行，红方在下、黑方在上。车走直线，马走日且不能蹩腿，炮吃子须隔一枚炮架；相不越河且不能塞眼，士与将帅不出九宫，兵卒过河后可横走但不能后退。"),
            ("坐标与中文棋谱", "列从左到右 A～I，行从下到上 1～10。发送 B3 E3 表示把 B3 的棋子移到 E3；也接受炮二平五、马八进七等中文棋谱。中文路数按各自执棋方的视角计算。"),
            ("将军、将死与困毙", "将帅不能照面，也不能让自己的将帅受攻击。被将军时必须应将；无合法着法时判负，包含将死和困毙。重复、长将与长捉按本小游戏固定规则裁定。"),
            ("胜负与和棋", "将死、困毙或对手认输可获胜；符合循环或限着条件时按规则结束。群友可用 #求和 发起和棋申请，等待对方 #同意和棋 或 #拒绝和棋。"),
        ]
    if game == "go":
        return [
            ("选棋盘与读坐标", "支持 9、13、19 路，默认 9 路；黑先白后。列从左到右且跳过 I，行从下到上；如 D4 表示第四列、从下数第四行。19 路最右列为 T，棋子下在空交点。"),
            ("气、提子与禁入点", "棋子相邻的空交点是气；同色棋子横竖相连组成棋串。无气的对方棋串被提走，不能自杀落子；劫与重复局面按本局规则限制。娱乐、认真两档均遵守相同规则。"),
            ("停一手与死子确认", "双方连续 #停一手 后进入数目阶段。建议就绪后，用 #标死 D4 标记整串死子，#取消死子 D4 恢复；调整后双方都需重新 #确认数目。人机对局有死活分歧时，请 #继续对局。"),
            ("数子计分与贴目", "按数子法计算活子与围住的空点，白方加本局贴目。提子数量仅作对局记录，不另加分。图中的试算结果要等双方确认后才结算；未围住的公共空点不算任何一方。"),
        ]
    if game == "number":
        return [
            ("四位数字，各不相同", "系统生成一个四位数。首位为 1～9，后面三位可以包含 0，但四个数字不能重复。发送 #猜 1234 进行猜测；也兼容 #作答 1234。"),
            ("A 和 B 怎么看", "A 表示数字和位置都正确；B 表示数字存在于答案中，但位置不正确。例如答案是 1234，猜 1320 会得到 1A2B：1 的位置正确，2 和 3 的位置相反，0 不在答案中。"),
            ("单人计步挑战", "只有发起者能猜，首轮成功展示后开始 10 分钟计时。每次有效猜测计一步，得到 4A 即完成挑战；没有跳过或每题倒计时，整局只有一个答案。"),
            ("全群公开抢答", "全群无需报名，每个人都能利用公开的猜测记录排除可能性。整局最多 15 分钟，第一个猜出 4A 的人获胜，不累积分数。只有发起者或管理员能结束整局。"),
        ]
    if game == "idiom":
        return [
            ("补全四字成语", "每题随机挖去两个字，例如「春□花□」。局内直接发送春暖花开，或发送 #作答 春暖花开；必须填写完整四字成语，错别字、同音字不算正确。原题认可的多个答案都可以得分。"),
            ("60 秒揭示一字", "本题展示 60 秒还未答对时，会把双空变成单空，但不会换题或扣分。此后继续作答，没有第二次提示倒计时；直到答对、单人跳过或整局时间结束。提示前原题认可的答案仍然有效。"),
            ("单人十题计时", "只有发起者能作答，逐题挑战十个不重复题目，整局最多 10 分钟。跳过会揭晓答案并进入下一题，记为未答对。最终显示正确数和总耗时，10/10 才算全部完成。"),
            ("群友公开抢答", "无需报名，群友可直接发送完整四字成语。第一位答对的人得一分并进入下一题；率先达到十分获胜。整局最多 15 分钟；不能跳过公共题目，仅发起者或管理员能结束整局。"),
            ("答错与公平计分", "直接发送成语答错时静默，使用 #作答 答错会明确提示；两者都不换题、不扣分。按机器人收到并处理消息的顺序判定；旧题迟到答案不计入新题。局外四字聊天不会被捕获。"),
        ]
    if game == "gomoku":
        return [
            ("自由规则", "15×15 棋盘，黑棋先走，双方轮流落一子。没有禁手，横、竖、两条斜线任一方向连成至少五子即获胜，长连也算胜。"),
            ("坐标怎么读", "列从左到右 A～O，行从上到下 1～15。例如 H8 是中央交点，直接发送 H8 或 #落子 H8；小写 h8 也可以。"),
            ("落子与平局", "棋子落在交点上，不能下在已有棋子的地方。轮到自己才能落子；棋盘填满且无人获胜时为平局。"),
        ]
    if game != "tictactoe":
        raise ValueError("Unsupported tutorial game")
    return [
        ("三子连线", "3×3 九宫格，先手使用 X，后手使用 O。双方轮流选择空格，横、竖或斜线连成三子即获胜。"),
        ("格子怎么选", "从左到右、从上到下编号 1～9，直接发送数字即可。也接受 A1～C3：字母表示列，数字表示行，例如 5 与 B2 都是正中间。"),
        ("落子与难度", "只能选择空格，轮到自己才能落子。九格填满且无人连线时为平局。娱乐档会赢棋、挡住直接威胁，但可能漏掉陷阱；认真档始终选择最优结果。"),
    ]


def _rules_section(draw, top, game, theme, *, paint=True):
    blocks = _tutorial_rules(game)
    if game in {"idiom", "number"}:
        heights = [37 + len(_wrap(draw, text, 820, 21)) * 31 + 24 for _, text in blocks]
        bottom = top + 94 + sum(heights) + 12
        if paint:
            _card(draw, (32, top, 928, bottom))
            _text(draw, (56, top + 21), "规则与答题教程", 30, bold=True)
            y = top + 91
            for (title, text), height in zip(blocks, heights):
                _text(draw, (68, y), title, 24, theme["accent"], True)
                _paragraph(draw, (68, y + 37), text, 820, 21, MUTED)
                y += height
        return bottom
    heights = [35 + len(_wrap(draw, text, 470, 21)) * 31 + 20 for _, text in blocks]
    content_height = max(338, sum(heights))
    bottom = top + 91 + content_height + 18
    if paint:
        _card(draw, (32, top, 928, bottom))
        _text(draw, (56, top + 21), "规则与落子教程", 30, bold=True)
        _example_board(draw, game, 70, top + 105, theme)
        y = top + 89
        for (title, text), height in zip(blocks, heights):
            _text(draw, (402, y), title, 24, theme["accent"], True)
            _paragraph(draw, (402, y + 37), text, 470, 21, MUTED)
            y += height
    return bottom


def _tips_section(draw, top, text, theme, *, paint=True):
    height = 75 + len(_wrap(draw, text, 830, 21)) * 31 + 25
    if paint:
        _card(draw, (32, top, 928, top + height), fill=theme["pale"])
        _text(draw, (57, top + 19), "对局小贴士", 27, bold=True)
        _paragraph(draw, (57, top + 70), text, 830, 21, MUTED)
    return top + height


def render_menu(persona=None, game="", active_game=None, wait_seconds=120, idle_seconds=600, *,
                idiom_enabled=True, number_enabled=True, xiangqi_enabled=True, go_enabled=True):
    """Selection or tutorial card. It never creates, modifies, or resumes a game."""
    if game and game not in GAME_NAMES:
        raise ValueError("Unsupported tutorial game")
    persona_id, name, theme = _theme(persona)
    measure = ImageDraw.Draw(Image.new("RGB", (960, 1)))
    wait, idle = _duration(wait_seconds), _duration(idle_seconds)
    active = bool(active_game)
    enabled = {"idiom": idiom_enabled, "number": number_enabled,
               "xiangqi": xiangqi_enabled, "go": go_enabled}
    entries = [entry for entry in GAME_REGISTRY if enabled.get(entry.id, True)]
    selection_tips_top = 409 + len(entries) * 169 + 16
    tips = (f"每群同时一盘；等待加入 {wait}，连续 {idle}没有成功落子会自动取消。\n"
            "每人每局最多成功悔棋 3 次。人机撤销上一轮；双人仅撤销自己刚下的最后一步，对手需在 60 秒内同意。\n"
            "查看菜单、棋盘或发送非法落子不会延长对局。参与者可结束游戏，群管理员可强制结束。")
    if game == "idiom":
        tips = ("每群同时一局，与其他小游戏共用名额。答题不影响好感度或签到积分。\n"
                "本题成功展示 60 秒后仅揭示一个字，不换题。发送失败可用 #题目 重试；查看题目不重置整局计时。\n"
                "单人仅本人、抢答全群可直接发完整四字成语作答。裸答错静默，#作答 答错有提示；局外四字聊天不捕获。")
    elif game == "number":
        tips = ("每群同时一局，与其他小游戏共用名额。猜数字不影响好感度或签到积分。\n"
                "首次成功展示后开始计时；发送失败用 #题目 重试。查看记录不延长期限，重启不重置计时。\n"
                "猜错会给出 A/B 提示，所有有效猜测都会计步；没有跳过功能。结束后才揭晓答案。")
    if game and not enabled.get(game, True):
        tips = f"本群暂未启用{GAME_NAMES[game]}，以下教程仅供了解玩法。\n" + tips
    if active:
        ongoing_quiz = isinstance(active_game, dict) and active_game.get("game") in {"idiom", "number"}
        tips += ("\n本群已有答题游戏：发送 #题目 返回。打开教程不会重置计时。" if ongoing_quiz
                 else "\n本群已有棋局：发送 #棋盘 返回对局。打开教程不会重置棋盘。")
    if game:
        rows = _menu_rows(game)
        commands_bottom = _command_section(measure, 404, rows, theme, paint=False)
        rules_bottom = _rules_section(measure, commands_bottom + 24, game, theme, paint=False)
        bottom = _tips_section(measure, rules_bottom + 24, tips, theme, paint=False)
    else:
        tips = ("先打开对应菜单查看规则，再选择人机、双人或答题模式开局。\n"
                f"每群同时一局；棋类等待加入 {wait}，连续 {idle}没有成功落子自动取消。\n"
                "游戏不影响好感度或签到积分，进度自动保存，重启后计时不会重置。")
        if active:
            is_quiz = isinstance(active_game, dict) and active_game.get("game") in {"idiom", "number"}
            tips += "\n本群已有游戏，发送 " + ("#题目" if is_quiz else "#棋盘") + " 查看。打开菜单不会重置对局。"
        bottom = _tips_section(measure, selection_tips_top, tips, theme, paint=False)
    image = Image.new("RGB", (960, bottom + 68), theme["bg"])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 137), fill=theme["accent"])
    _text(draw, (38, 22), GAME_NAMES[game] if game else "小游戏合集", 44, "white", True)
    subtitle = ("四位不重复数字 · 1A2B 推理 / 全群抢答" if game == "number" else
                "补全四字成语 · 单人计时 / 全群抢答" if game == "idiom" else
                "先看看规则，再选人机或邀请群友开局" if game else
                f"{len(entries)} 款轻量小游戏 · 选择玩法 · 自动保存进度")
    _text(draw, (40, 86), subtitle, 24, "white")
    _mascot(image, draw, persona_id, theme)
    _card(draw, (233, 172, 924, 363), outline=theme["accent"])
    draw.polygon(((233, 219), (212, 237), (233, 255)), fill="white")
    draw.line((233, 219, 212, 237, 233, 255), fill=theme["accent"], width=2)
    label = name
    label_width = min(590, int(draw.textlength(label, font=_font(22, True))) + 32)
    draw.rounded_rectangle((256, 190, 256 + label_width, 230), radius=18, fill=theme["accent"])
    _text(draw, (272, 194), label, 22, "white", True, width=label_width - 32)
    greetings = {"rin": "规则先看清楚，准备好了就来一局吧。", "eres": "不用着急，我会陪你慢慢熟悉规则的。", "ishtar": "选好你的战场，来和女神较量一下吧！"}
    _paragraph(draw, (257, 248), greetings.get(persona_id, "先看规则，准备好了再来一局吧。"), 632, 24, INK, True)
    example = ("#猜数字 单人  /  #猜数字 抢答" if game == "number" else
               "#成语填空 单人  /  #成语填空 抢答" if game == "idiom" else
               f"#{GAME_NAMES[game]} 对战  /  #{GAME_NAMES[game]} @群友" if game else
               "发送 #游戏名，进入对应菜单")
    _paragraph(draw, (257, 303), example, 632, 21, theme["accent"])
    if game:
        _command_section(draw, 404, rows, theme)
        _rules_section(draw, commands_bottom + 24, game, theme)
        _tips_section(draw, rules_bottom + 24, tips, theme)
    else:
        for index, entry in enumerate(entries):
            y = 409 + index * 169
            _card(draw, (32, y, 928, y + 145))
            draw.rounded_rectangle((55, y + 28, 143, y + 116), radius=22, fill=theme["accent"])
            _center(draw, (99, y + 72), entry.short, 39, "white", True)
            _text(draw, (170, y + 25), entry.name, 29, bold=True)
            _text(draw, (170, y + 77), entry.description, 22, MUTED)
            _text(draw, (736, y + 49), f"#{entry.name}", 25, theme["accent"], True)
        _tips_section(draw, selection_tips_top, tips, theme)
    _text(draw, (39, bottom + 23), "菜单仅供查看，不会开局 · 阅读教程后发送对应开局指令开始游玩", 20, MUTED)
    return _png(image)


def render_board(state, avatars=None):
    game, board = state["game"], state["board"]
    if game in {"xiangqi", "go"}:
        return render_strategy_board(state, avatars)
    if game not in GAME_BY_ID or GAME_BY_ID[game].kind != "board":
        raise ValueError("Unsupported board game")
    n = 15 if game == "gomoku" else 3
    if len(board) != n * n:
        raise ValueError("Invalid board size")
    theme = THEMES.get(state.get("persona_id"), THEMES["rin"])
    accent = theme["accent"]
    image = Image.new("RGB", (960, 1100), theme["bg"])
    draw = ImageDraw.Draw(image)
    _text(draw, (36, 18), GAME_NAMES[game], 36, bold=True)
    difficulty_labels = {"casual": "娱乐人机", "serious": "认真人机", "easy": "旧版简单", "normal": "旧版普通"}
    mode = difficulty_labels.get(state.get("difficulty"), "人机对战") if state.get("mode") == "bot" else "群友对战"
    draw.rounded_rectangle((763, 24, 924, 65), radius=18, fill=theme["pale"])
    _center(draw, (843, 44), mode, 22, accent, True)
    avatars = avatars or {}
    for side, x in [(1, 36), (2, 488)]:
        player = state["players"][str(side)]
        name = player.get("name") or "等待加入"
        player_id = player.get("id")
        bot = player_id == state.get("bot_id")
        active = state.get("status") == "active" and state.get("turn") == side
        source = state.get("persona_avatar", "") if bot else avatars.get(player_id, avatars.get(str(player_id), b""))
        draw.rounded_rectangle((x - 8, 76, x + 428, 166), radius=18, fill="white")
        _avatar(image, draw, (x + 5, 86), source, theme, active=active)
        _text(draw, (x + 90, 84), name, 24, bold=True, width=324)
        if n == 15:
            draw.ellipse((x + 91, 128, x + 106, 143), fill=(35, 39, 46) if side == 1 else "white", outline=(100, 106, 117))
        else:
            _center(draw, (x + 100, 137), "X" if side == 1 else "O", 20, accent if side == 1 else (25, 147, 149), True)
        label = ("黑棋 · 先手" if side == 1 else "白棋 · 后手") if n == 15 else ("X · 先手" if side == 1 else "O · 后手")
        _text(draw, (x + 117, 122), label, 18, MUTED)
        if active:
            _text(draw, (x + 275, 122), "当前回合", 18, accent, True)
    status, winner = state["status"], state.get("winner", 0)
    if status == "waiting":
        subtitle = "等待指定对手加入" if state.get("invitee") else "等待群友加入 · #加入游戏"
    elif status == "ended":
        subtitle = f"{state['players'][str(winner)]['name']} 获胜" if winner in (1, 2) else "平局" if winner == -1 else "棋局已取消"
    else:
        subtitle = f"轮到 {state['players'][str(state['turn'])]['name']}"
    _text(draw, (36, 176), subtitle, 24, accent, bold=True, width=890)
    points = {}
    if n == 15:
        draw.rounded_rectangle((36, 222, 924, 1032), radius=24, fill=(239, 214, 177))
        left, top, step = 130, 272, 50
        end_x, end_y = left + 14 * step, top + 14 * step
        for i in range(15):
            x, y = left + i * step, top + i * step
            draw.line((x, top, x, end_y), fill=(149, 120, 81), width=1)
            draw.line((left, y, end_x, y), fill=(149, 120, 81), width=1)
            _center(draw, (x, top - 27), chr(65 + i), 20, INK)
            _center(draw, (x, end_y + 27), chr(65 + i), 20, INK)
            _center(draw, (left - 34, y), i + 1, 20, INK)
            _center(draw, (end_x + 34, y), i + 1, 20, INK)
        for r, c in [(3, 3), (3, 11), (7, 7), (11, 3), (11, 11)]:
            x, y = left + c * step, top + r * step
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(106, 79, 46))
        for i, piece in enumerate(board):
            x, y = left + i % 15 * step, top + i // 15 * step
            points[i] = (x, y)
            if piece:
                draw.ellipse((x - 21, y - 19, x + 23, y + 25), fill=(189, 164, 125))
                draw.ellipse((x - 22, y - 22, x + 22, y + 22), fill=(33, 37, 44) if piece == 1 else (253, 252, 246), outline=(84, 76, 65), width=1)
    else:
        left, top, step = 150, 272, 220
        for i, piece in enumerate(board):
            x, y = left + i % 3 * step, top + i // 3 * step
            points[i] = (x + 105, y + 105)
            draw.rounded_rectangle((x, y, x + 210, y + 210), radius=24, fill="white", outline=(216, 221, 234), width=3)
            if piece == 1:
                draw.line((x + 59, y + 59, x + 151, y + 151), fill=accent, width=14)
                draw.line((x + 151, y + 59, x + 59, y + 151), fill=accent, width=14)
            elif piece == 2:
                draw.ellipse((x + 52, y + 52, x + 158, y + 158), outline=(25, 147, 149), width=14)
            else:
                _center(draw, (x + 105, y + 105), i + 1, 48, (164, 174, 194))
        _text(draw, (160, 969), "1 2 3 / 4 5 6 / 7 8 9 · 也可发送 A1～C3", 25, MUTED)
    moves = state.get("moves", [])
    if moves:
        x, y = points[moves[-1]["index"]]
        if n == 15:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(224, 84, 59))
        else:
            draw.rounded_rectangle((x - 104, y - 104, x + 104, y + 104), radius=24, outline=(224, 84, 59), width=5)
    line = state.get("winning_line", [])
    if len(line) >= (5 if n == 15 else 3):
        draw.line((*points[line[0]], *points[line[-1]]), fill=(223, 79, 57), width=6)
    _text(draw, (40, 1053), "红色标记为最近落子 · #棋盘 查看 · #认输 / #结束游戏", 21, MUTED)
    return _png(image)


GO_COLUMNS = "ABCDEFGHJKLMNOPQRST"
XIANGQI_GLYPHS = {"R": "车", "N": "马", "B": "相", "A": "仕", "K": "帅", "C": "炮", "P": "兵",
                  "r": "车", "n": "马", "b": "象", "a": "士", "k": "将", "c": "炮", "p": "卒",
                  "H": "马", "E": "相", "h": "马", "e": "象"}
BOARD_WOOD = (239, 214, 177)
BOARD_LINE = (142, 109, 71)
LAST_MOVE = (211, 89, 40)
DEAD_MARK = (187, 45, 64)
SUGGESTED_MARK = (117, 76, 169)


def _strategy_geometry(game, size=9):
    """Board indices have row zero at the bottom in both new games."""
    if game == "xiangqi":
        return 960, 120, 470, 90, 9, 10
    width = 1200 if size == 19 else 960
    extent = 972 if size == 19 else 720
    return width, (width - extent) // 2, 470, extent / (size - 1), size, size


def _strategy_points(game, size=9):
    _, left, top, step, columns, rows = _strategy_geometry(game, size)
    return {index: (round(left + (index % columns) * step),
                    round(top + (rows - 1 - index // columns) * step))
            for index in range(columns * rows)}


def _xiangqi_move_squares(move):
    match = re.fullmatch(r"([a-i])(10|[1-9])([a-i])(10|[1-9])", str(move).lower())
    if not match:
        return None
    first_file, first_rank, last_file, last_rank = match.groups()
    return ((int(first_rank) - 1) * 9 + ord(first_file) - ord("a"),
            (int(last_rank) - 1) * 9 + ord(last_file) - ord("a"))


def _strategy_move_label(state):
    moves = state.get("moves") or []
    if not moves:
        return "红方先行" if state["game"] == "xiangqi" else "黑方先行"
    last = moves[-1]
    move = last.get("move", last.get("index"))
    side = int(last.get("side") or 1)
    if state["game"] == "xiangqi":
        squares = _xiangqi_move_squares(move)
        if squares:
            labels = [f"{chr(65 + square % 9)}{square // 9 + 1}" for square in squares]
            notation = last.get("notation") or last.get("san")
            return ("红" if side == 1 else "黑") + "方上步：" + " → ".join(labels) + (f" · {notation}" if notation else "")
    elif str(move).lower() == "pass":
        return ("黑" if side == 1 else "白") + "方上步：停一手"
    else:
        try:
            index, size = int(move), int(state.get("board_size", 9))
            if 0 <= index < size * size:
                return ("黑" if side == 1 else "白") + f"方上步：{GO_COLUMNS[index % size]}{index // size + 1}"
        except (ValueError, TypeError):
            pass
    return f"已行 {len(moves)} 手"


def _strategy_status(state):
    status, players, winner = state.get("status"), state.get("players") or {}, state.get("winner")
    if status == "waiting":
        return "等待指定对手加入" if state.get("invitee") else "等待群友加入 · #加入游戏"
    if status == "ended":
        if winner in (1, 2):
            return f"{players.get(str(winner), {}).get('name') or '玩家'} 获胜"
        return "和棋" if winner == -1 else "本局已结束"
    if state.get("phase") == "scoring":
        return "数目确认 · 请检查死子与地盘"
    name = players.get(str(state.get("turn", 1)), {}).get("name") or "玩家"
    return ("将军！请应将 · " if state.get("in_check") else "轮到 " ) + name


def _strategy_header(image, draw, state, avatars, theme):
    width = image.width
    game, persona_id = state["game"], state.get("persona_id") or "rin"
    title = GAME_NAMES.get(game, "中国象棋" if game == "xiangqi" else "围棋")
    if game == "go":
        title += f" · {state.get('board_size', 9)} 路"
    draw.rectangle((0, 0, width, 96), fill=theme["accent"])
    _text(draw, (36, 18), title, 38, "white", True)
    mode = ({"casual": "娱乐人机", "serious": "认真人机"}.get(state.get("difficulty"), "人机对战")
            if state.get("mode") == "bot" else "群友对战")
    _text(draw, (width - 185, 32), mode, 24, "white", True)
    card_width = (width - 88) // 2
    scoring = state.get("phase") == "scoring"
    for side, x in ((1, 36), (2, width // 2 + 8)):
        player = (state.get("players") or {}).get(str(side), {})
        player_id = player.get("id")
        bot = state.get("bot_id") is not None and str(player_id) == str(state["bot_id"])
        active = state.get("status") == "active" and not scoring and state.get("turn") == side
        source = state.get("persona_avatar", "") if bot else avatars.get(player_id, avatars.get(str(player_id), b""))
        _card(draw, (x, 118, x + card_width, 226), radius=20)
        _avatar(image, draw, (x + 14, 137), source, theme, active=active, size=68)
        _text(draw, (x + 101, 132), player.get("name") or "等待加入", 25, bold=True, width=card_width - 119)
        label = (("红方 · 先手" if side == 1 else "黑方 · 后手") if game == "xiangqi" else
                 ("黑棋 · 先手" if side == 1 else "白棋 · 后手"))
        _text(draw, (x + 102, 177), label, 20, DEAD_MARK if game == "xiangqi" and side == 1 else MUTED)
        if active:
            _text(draw, (x + card_width - 103, 179), "当前回合", 18, theme["accent"], True)
        elif scoring and str(player_id) in {str(uid) for uid in (state.get("scoring") or {}).get("confirmed", [])}:
            _text(draw, (x + card_width - 85, 179), "已确认", 18, theme["accent"], True)
    _mascot(image, draw, persona_id, theme, bounds=(36, 242, 106, 135))
    _card(draw, (161, 250, width - 36, 372), outline=theme["accent"])
    _text(draw, (181, 265), _strategy_status(state), 27, theme["accent"], True, width=width - 239)
    _text(draw, (182, 316), _strategy_move_label(state), 22, MUTED, width=width - 240)


def _xiangqi_grid(draw, points):
    for row in range(10):
        draw.line((*points[row * 9], *points[row * 9 + 8]), fill=BOARD_LINE, width=2)
    for column in range(9):
        if column in (0, 8):
            draw.line((*points[column], *points[81 + column]), fill=BOARD_LINE, width=2)
        else:
            draw.line((*points[column], *points[36 + column]), fill=BOARD_LINE, width=2)
            draw.line((*points[45 + column], *points[81 + column]), fill=BOARD_LINE, width=2)
        _center(draw, (points[column][0], points[81][1] - 56), chr(65 + column), 22, INK, True)
        _center(draw, (points[column][0], points[0][1] + 54), chr(65 + column), 22, INK, True)
    for row in range(10):
        y = points[row * 9][1]
        _center(draw, (65, y), row + 1, 23, INK)
        _center(draw, (895, y), row + 1, 23, INK)
    for base in (0, 63):
        draw.line((*points[base + 3], *points[base + 23]), fill=BOARD_LINE, width=2)
        draw.line((*points[base + 5], *points[base + 21]), fill=BOARD_LINE, width=2)
    river_y = (points[36][1] + points[45][1]) / 2
    _center(draw, (300, river_y), "楚 河", 34, BOARD_LINE)
    _center(draw, (660, river_y), "汉 界", 34, BOARD_LINE)
    for index in (19, 25, 64, 70, 27, 29, 31, 33, 35, 54, 56, 58, 60, 62):
        x, y = points[index]
        for dx in (-1, 1):
            if (index % 9 == 0 and dx < 0) or (index % 9 == 8 and dx > 0):
                continue
            for dy in (-1, 1):
                draw.line((x + dx * 7, y + dy * 18, x + dx * 7, y + dy * 7, x + dx * 18, y + dy * 7), fill=BOARD_LINE, width=2)


def _paint_xiangqi(draw, state, points):
    _xiangqi_grid(draw, points)
    moves = state.get("moves") or []
    recent = _xiangqi_move_squares(moves[-1].get("move")) if moves else None
    if recent:
        x, y = points[recent[0]]
        draw.ellipse((x - 35, y - 35, x + 35, y + 35), outline=LAST_MOVE, width=4)
        _center(draw, (x, y), "起", 22, LAST_MOVE)
    for index, piece in enumerate(state["board"]):
        if piece == ".":
            continue
        x, y = points[index]
        color = (176, 43, 43) if piece.isupper() else (38, 42, 44)
        draw.ellipse((x - 35, y - 32, x + 36, y + 39), fill=(192, 163, 117))
        draw.ellipse((x - 35, y - 35, x + 35, y + 35), fill=(255, 240, 204), outline=color, width=2)
        draw.ellipse((x - 29, y - 29, x + 29, y + 29), outline=color, width=1)
        _center(draw, (x, y - 1), XIANGQI_GLYPHS[piece], 41, color, True)
    if recent:
        x, y = points[recent[1]]
        draw.rounded_rectangle((x - 41, y - 41, x + 41, y + 41), radius=16, outline=LAST_MOVE, width=4)


def _go_star_points(size):
    if size == 9:
        return ((2, 2), (2, 6), (4, 4), (6, 2), (6, 6))
    positions = (3, size // 2, size - 4)
    return tuple((row, column) for row in positions for column in positions)


def _paint_go(draw, state, points):
    size = int(state.get("board_size", 9))
    width, left, top, step, _, _ = _strategy_geometry("go", size)
    right, bottom = points[size - 1][0], points[0][1]
    for index in range(size):
        x, y = points[index][0], points[index * size][1]
        draw.line((x, top, x, bottom), fill=BOARD_LINE, width=2)
        draw.line((left, y, right, y), fill=BOARD_LINE, width=2)
        _center(draw, (x, top - 53), GO_COLUMNS[index], 23, INK, True)
        _center(draw, (x, bottom + 53), GO_COLUMNS[index], 23, INK, True)
        _center(draw, (left - 55, y), index + 1, 23, INK)
        _center(draw, (right + 55, y), index + 1, 23, INK)
    for row, column in _go_star_points(size):
        x, y = points[row * size + column]
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=BOARD_LINE)
    radius = min(38, int(step * .43))
    for index, piece in enumerate(state["board"]):
        if not piece:
            continue
        x, y = points[index]
        draw.ellipse((x - radius + 2, y - radius + 4, x + radius + 2, y + radius + 4), fill=(188, 162, 124))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=(33, 38, 44) if piece == 1 else (255, 254, 246), outline=(82, 74, 62), width=1)
    scoring = state.get("scoring") or {}
    scoring_visible = state.get("phase") == "scoring" or state.get("status") == "ended"
    if scoring_visible:
        score = scoring.get("score") or {}
        dead = set(scoring.get("dead") or [])
        for field, color in (("black_territory", (37, 42, 48)), ("white_territory", (255, 253, 243))):
            for index in score.get(field) or []:
                if index not in points or (state["board"][index] and index not in dead):
                    continue
                x, y = points[index]
                draw.rectangle((x - 7, y - 7, x + 7, y + 7), fill=color, outline=(87, 78, 66), width=1)
        for index in score.get("neutral") or []:
            if index in points and not state["board"][index]:
                x, y = points[index]
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(185, 126, 55))
        for index in (scoring.get("suggested") or []):
            if index in points and state["board"][index] and index not in dead:
                x, y = points[index]
                for start in (0, 90, 180, 270):
                    draw.arc((x - radius, y - radius, x + radius, y + radius), start, start + 55, fill=SUGGESTED_MARK, width=4)
        for index in dead:
            if index in points and state["board"][index]:
                x, y = points[index]
                mark = max(10, int(radius * .6))
                draw.line((x - mark, y - mark, x + mark, y + mark), fill=DEAD_MARK, width=4)
                draw.line((x - mark, y + mark, x + mark, y - mark), fill=DEAD_MARK, width=4)
    moves = state.get("moves") or []
    if moves and not scoring_visible:
        move = moves[-1].get("move", moves[-1].get("index"))
        try:
            index = int(move)
        except (ValueError, TypeError):
            index = -1
        if index in points:
            x, y = points[index]
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=LAST_MOVE)


def _strategy_details(state):
    if state["game"] == "xiangqi":
        lines = ["橙圈为上步起点，橙框为终点 · 红方在下，坐标从下向上读"]
        if state.get("status") == "active":
            lines += ["#落子 炮二平五 / #落子 H3 E3 · #棋盘 查看 · #悔棋",
                      "#求和 · #认输 · #结束游戏"]
        elif state.get("status") == "waiting":
            lines += ["#加入游戏 接受邀请或加入 · #结束游戏 取消等待"]
        else:
            reason = {"checkmate": "将死", "stalemate": "困毙", "resign": "认输",
                      "resignation": "认输", "resigned": "认输", "repetition": "循环局面裁定",
                      "draw": "和棋", "agreement": "双方同意和棋", "agreed_draw": "双方同意和棋"}.get(state.get("end_reason"))
            lines += ([f"结束原因：{reason}"] if reason else []) + ["#象棋 查看玩法，选择人机或群友重新开局"]
        return lines
    size = int(state.get("board_size", 9))
    captures = state.get("captures") or {}
    komi = state.get("komi", 7.5)
    lines = [f"{size} 路 · 白贴 {komi:g} · 黑提 {captures.get('1', 0)} / 白提 {captures.get('2', 0)}（提子不另计分）"]
    scoring = state.get("scoring") or {}
    if state.get("phase") == "scoring" or (state.get("status") == "ended" and scoring.get("score")):
        lines += ["红叉 = 已标死子 · 黑白小方块 = 地盘 · 金点 = 公共点"]
        if scoring.get("suggested") and set(scoring["suggested"]) - set(scoring.get("dead") or []):
            lines += ["紫圈 = 建议死子，请核对后用 #标死 标记"]
        score = scoring.get("score") or {}
        if score:
            finalized = state.get("status") == "ended" and state.get("end_reason") == "scored"
            prefix = "最终计分" if finalized else "结束时试算（未结算）" if state.get("status") == "ended" else "当前试算"
            lines += [f"{prefix}：黑 {score.get('black', 0):g} · 白 {score.get('white', 0):g}（含贴目）"]
        if state.get("status") != "ended":
            if scoring.get("source") == "pending":
                lines += ["正在准备死子建议，暂不能标记或确认数目",
                          "稍后 #棋盘 查看建议，或 #继续对局 继续落子"]
                return lines
            confirmed = {str(uid) for uid in scoring.get("confirmed", [])}
            players = state.get("players") or {}
            done = [f"{'黑' if side == 1 else '白'}方{'已确认' if str(players.get(str(side), {}).get('id')) in confirmed else '待确认'}" for side in (1, 2)]
            lines += [" · ".join(done) + " · 修改标记后需双方重新确认",
                      "#标死 D4 / #取消死子 D4 · #确认数目 · #继续对局"]
    elif state.get("status") == "waiting":
        lines += ["#加入游戏 加入 · #结束游戏 取消等待"]
    elif state.get("status") == "active":
        lines += ["橙点为最近落子 · 列跳过 I，底部为第 1 行",
                  "#落子 D4 / 直接发 D4 · #停一手 · #悔棋",
                  "双方连续停一手后确认数目 · #认输 / #结束游戏"]
    else:
        lines += ["#围棋 查看玩法，选择人机或群友重新开局"]
    return lines


def render_strategy_board(state, avatars=None):
    """Render the saved position only; never infer life/death or game results."""
    game = state.get("game")
    board = state.get("board") or []
    size = int(state.get("board_size", 9))
    if game == "xiangqi":
        if len(board) != 90 or any(piece != "." and piece not in XIANGQI_GLYPHS for piece in board):
            raise ValueError("Invalid xiangqi board")
    elif game == "go":
        if size not in (9, 13, 19) or len(board) != size * size or any(piece not in (0, 1, 2) for piece in board):
            raise ValueError("Invalid go board")
    else:
        raise ValueError("Unsupported strategy board")
    _, _, theme = _theme({"id": state.get("persona_id")})
    width, _, _, _, _, _ = _strategy_geometry(game, size)
    points = _strategy_points(game, size)
    board_bottom = points[0][1] + 75
    details = _strategy_details(state)
    measure = ImageDraw.Draw(Image.new("RGB", (width, 1)))
    heights = [len(_wrap(measure, line, width - 112, 23)) * 35 + 13 for line in details]
    footer_top = board_bottom + 25
    image = Image.new("RGB", (width, footer_top + sum(heights) + 54), theme["bg"])
    draw = ImageDraw.Draw(image)
    _strategy_header(image, draw, state, avatars or {}, theme)
    draw.rounded_rectangle((36, 386, width - 36, board_bottom), radius=25, fill=BOARD_WOOD)
    if game == "xiangqi":
        _paint_xiangqi(draw, state, points)
    else:
        _paint_go(draw, state, points)
    _card(draw, (36, footer_top, width - 36, image.height - 25), fill=theme["pale"])
    y = footer_top + 19
    for line, height in zip(details, heights):
        _paragraph(draw, (56, y), line, width - 112, 23, MUTED, gap=12)
        y += height
    return _png(image)


def _elapsed_label(seconds):
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} 分 {rest:02d} 秒" if minutes else f"{rest} 秒"


def _end_reason_label(reason):
    return {
        "cancelled": "游戏已取消", "cancel": "游戏已取消",
        "timeout": "整局时间已到", "expired": "整局时间已到",
        "time_limit": "整局时间已到", "deadline": "整局时间已到",
        "undelivered": "题目长时间未能展示，本局结束",
        "pool_exhausted": "本局可用题目已用完",
        "bank_unavailable": "题库暂不可用，本局结束",
    }.get(reason, "本局进度已保存")


def render_idiom(state):
    """Render only public quiz state; current accepted answers are never read."""
    if state.get("game") != "idiom":
        raise ValueError("Unsupported quiz game")
    persona_id, name, theme = _theme({"id": state.get("persona_id"),
                                     "name": state.get("bot_name") or state.get("persona_name")})
    solo = state.get("mode") == "solo"
    ended = state.get("status") == "ended"
    question_no = int(state.get("question_no", 1))
    question = state.get("question") or {}
    now = state.get("display_now", state.get("now", time.time()))
    finish = state.get("finished_at") or now
    elapsed = max(0, finish - state["started_at"]) if state.get("started_at") else 0
    players = state.get("players") or {}
    scores = sorted((state.get("scores") or {}).items(), key=lambda pair: (-int(pair[1]), str(pair[0])))
    winner_id = state.get("winner_id")
    winner = players.get(str(winner_id), {}).get("name", str(winner_id)) if winner_id else ""
    correct, skipped, timed_out = (int(state.get(key, 0)) for key in ("correct_count", "skipped_count", "timed_out_count"))
    if ended:
        headline = ("十题全部完成！" if solo and correct == 10 else
                    "本局挑战结束" if solo else f"{winner} 获胜" if winner else "本局结束 · 未决出胜者")
    else:
        headline = f"第 {question_no} / {int(state.get('question_limit') or 10)} 题" if solo else f"第 {question_no} 题 · 率先十分获胜"
    last = state.get("last_result") or {}
    # Even a malformed state must not reveal the still-active question.
    reveal_last = ended or int(last.get("question_no", question_no)) < question_no
    result_lines = []
    if last and reveal_last:
        kind = last.get("kind")
        action = f"{last.get('name') or '玩家'} 答对了" if kind == "correct" else "已跳过" if kind == "skipped" else "本题超时"
        answers = last.get("answers") or []
        if isinstance(answers, str):
            answers = [answers]
        revealed = " / ".join(str(answer) for answer in answers)
        result_lines.append(f"第 {last.get('question_no', max(1, question_no - 1))} 题：{action}")
        if revealed:
            result_lines.append(f"答案：{revealed}")
    if solo:
        score_lines = [f"正确 {correct} / 10    跳过 {skipped}" + (f"    超时未答 {timed_out}" if timed_out else ""),
                       f"总耗时：{_elapsed_label(elapsed)} · 最多 10 分钟" if state.get("started_at") else "首题成功展示后开始计时，整局最多 10 分钟"]
    else:
        score_lines = [f"{rank}. {players.get(str(uid), {}).get('name') or uid}    {int(score)} 分"
                       for rank, (uid, score) in enumerate(scores[:8], 1)] or ["暂时无人得分，发送 #作答 完整成语 参与抢答"]
        if len(scores) > 8:
            score_lines.append(f"还有 {len(scores) - 8} 位群友参与，本图展示前八名")
        score_lines.append(f"整局耗时：{_elapsed_label(elapsed)} · 最多 15 分钟" if state.get("started_at") else "首题成功展示后开始计时，整局最多 15 分钟")
    if not ended and state.get("deadline"):
        score_lines.append(f"整局截止：{datetime.fromtimestamp(float(state['deadline']), timezone(timedelta(hours=8))).strftime('%H:%M:%S')}（北京时间）")
    measure = ImageDraw.Draw(Image.new("RGB", (960, 1)))
    result_height = 34 + sum(len(_wrap(measure, line, 820, 22)) * 32 + 8 for line in result_lines) if result_lines else 0
    score_top = 750 + result_height + (22 if result_lines else 0)
    score_height = 83 + sum(len(_wrap(measure, line, 818, 23)) * 33 + 14 for line in score_lines)
    footer_top = score_top + score_height + 30
    image = Image.new("RGB", (960, footer_top + 106), theme["bg"])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 137), fill=theme["accent"])
    _text(draw, (38, 22), "成语填空", 44, "white", True)
    _text(draw, (40, 86), "单人十题计时" if solo else "全群公开抢答 · 首个十分获胜", 24, "white")
    _mascot(image, draw, persona_id, theme)
    _card(draw, (233, 172, 924, 363), outline=theme["accent"])
    draw.polygon(((233, 219), (212, 237), (233, 255)), fill="white")
    draw.line((233, 219, 212, 237, 233, 255), fill=theme["accent"], width=2)
    label_width = min(590, int(draw.textlength(name, font=_font(22, True))) + 32)
    draw.rounded_rectangle((256, 190, 256 + label_width, 230), radius=18, fill=theme["accent"])
    _text(draw, (272, 194), name, 22, "white", True, width=label_width - 32)
    greeting = ("认真想一想，把缺少的字补全吧。" if solo else "看准这一题，谁会第一个答出来呢？") if not ended else "这一局结束啦，看看你的表现吧。"
    _paragraph(draw, (257, 248), greeting, 632, 24, INK, True)
    _text(draw, (257, 308), "直接发完整成语，或使用 #作答 春暖花开" if not ended else "#成语填空 查看玩法，再开一局", 21, theme["accent"], width=620)
    _card(draw, (32, 409, 928, 720))
    _text(draw, (60, 430), headline, 29, theme["accent"], True, width=840)
    if not ended:
        original_mask = str(question.get("mask") or "□□□□")[:4]
        mask = str(state.get("hint_mask") or original_mask)[:4]
        for index, character in enumerate(mask):
            x = 214 + index * 137
            draw.rounded_rectangle((x, 492, x + 122, 614), radius=20, fill=theme["pale"], outline=theme["accent"], width=2 if character == "□" else 1)
            _center(draw, (x + 61, 553), character, 58, theme["accent"] if character == "□" else INK, True)
        due = state.get("question_deadline")
        hinted = state.get("hinted_at") is not None or bool(state.get("hint_mask"))
        if hinted:
            timer = "本题已是单空，继续作答" if original_mask.count("□") <= 1 else "已揭示一字，继续作答"
        elif due:
            timer = f"60 秒后揭示一字 · 提示时间 {datetime.fromtimestamp(float(due), timezone(timedelta(hours=8))).strftime('%H:%M:%S')}"
        else:
            timer = "成功展示后开始计时 · 60 秒后揭示一字"
        _center(draw, (480, 654), timer, 23, MUTED)
        _center(draw, (480, 693), "#题目 查看进度" + ("    #跳过 揭晓并进入下一题" if solo else "    答错不扣分，抢答不可跳过"), 20, MUTED)
    else:
        summary = (f"答对 {correct} / 10" if solo else "首个十分获胜" if winner else "无人达到十分")
        _center(draw, (480, 550), summary, 45, theme["accent"], True)
        reason = state.get("end_reason", "")
        detail = _end_reason_label(reason)
        _center(draw, (480, 632), detail, 24, MUTED)
    if result_lines:
        _card(draw, (32, 750, 928, 750 + result_height), fill=theme["pale"])
        y = 766
        for line in result_lines:
            y += _paragraph(draw, (68, y), line, 820, 22, theme["accent"]) + 8
    _card(draw, (32, score_top, 928, score_top + score_height))
    _text(draw, (60, score_top + 20), "挑战统计" if solo else "本局比分", 29, bold=True)
    y = score_top + 77
    for line in score_lines:
        y += _paragraph(draw, (70, y), line, 818, 23, MUTED) + 14
    _paragraph(draw, (40, footer_top), "局内可直接发完整四字成语 · 局外聊天不捕获\n裸答错静默，#作答 答错有提示 · 不影响好感度和签到积分", 880, 20, MUTED)
    return _png(image)


def render_number(state):
    """Public 1A2B feedback; never access the secret while a round is active."""
    if state.get("game") != "number":
        raise ValueError("Unsupported number game")
    persona_id, name, theme = _theme({"id": state.get("persona_id"),
                                     "name": state.get("bot_name") or state.get("persona_name")})
    solo = state.get("mode") == "solo"
    ended = state.get("status") == "ended"
    limit_minutes = 10 if solo else 15
    players = state.get("players") or {}
    host_id = str(state.get("host_id", ""))
    host_name = players.get(host_id, {}).get("name") or "发起者"
    winner_id = state.get("winner_id")
    winner_name = players.get(str(winner_id), {}).get("name") or str(winner_id) if winner_id else ""
    now = state.get("display_now", state.get("now", time.time()))
    elapsed = max(0, (state.get("finished_at") or now) - state["started_at"]) if state.get("started_at") else 0
    attempts = (state.get("attempts") or [])[-12:]
    total_attempts = int(state.get("total_attempts", len(attempts)))
    personal_attempts = int((state.get("attempt_counts") or {}).get(host_id, total_attempts))
    due = state.get("deadline")
    measure = ImageDraw.Draw(Image.new("RGB", (960, 1)))
    rows = list(reversed(attempts))
    history_top = 765
    history_height = 134 + (len(rows) * 57 if rows else 75)
    stats_top = history_top + history_height + 25
    stats_lines = [f"本局总猜测：{total_attempts} 次" + (f" · 你的步数：{personal_attempts}" if solo else " · 首个 4A 获胜"),
                   f"耗时：{_elapsed_label(elapsed)}" if state.get("started_at") else f"成功展示后开始 {limit_minutes} 分钟计时"]
    if ended:
        stats_lines.append(_end_reason_label(state.get("end_reason", "")))
    else:
        stats_lines.append("A = 数字和位置都对；B = 数字对，但位置不对")
    stats_height = 88 + sum(len(_wrap(measure, line, 820, 22)) * 32 + 9 for line in stats_lines)
    footer_top = stats_top + stats_height + 25
    image = Image.new("RGB", (960, footer_top + 95), theme["bg"])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 137), fill=theme["accent"])
    _text(draw, (38, 22), "猜数字", 44, "white", True)
    _text(draw, (40, 86), "单人计步 · 10 分钟挑战" if solo else "全群公开抢答 · 15 分钟内首个 4A 获胜", 24, "white")
    _mascot(image, draw, persona_id, theme)
    _card(draw, (233, 172, 924, 363), outline=theme["accent"])
    draw.polygon(((233, 219), (212, 237), (233, 255)), fill="white")
    draw.line((233, 219, 212, 237, 233, 255), fill=theme["accent"], width=2)
    label_width = min(590, int(draw.textlength(name, font=_font(22, True))) + 32)
    draw.rounded_rectangle((256, 190, 256 + label_width, 230), radius=18, fill=theme["accent"])
    _text(draw, (272, 194), name, 22, "white", True, width=label_width - 32)
    greeting = "结合每一步线索，找出藏起来的数字吧。" if not ended else "答案揭晓啦，来看看这次的推理过程。"
    _paragraph(draw, (257, 248), greeting, 632, 24, INK, True)
    _text(draw, (257, 308), "发送 #猜 1234，每个数字不能重复" if not ended else "#猜数字 查看玩法，再来挑战一局", 21, theme["accent"], width=620)
    _card(draw, (32, 409, 928, 734))
    headline = (f"{winner_name} 猜中了！" if winner_id else "本局结束 · 未猜出答案") if ended else f"{host_name} 的单人挑战" if solo else "全群一起推理，谁先猜出 4A？"
    _text(draw, (60, 430), headline, 29, theme["accent"], True, width=838)
    # This access is deliberately confined to a finished state.
    digits = str(state.get("secret") or "□□□□")[:4] if ended else "□□□□"
    for index, character in enumerate(digits):
        x = 214 + index * 137
        draw.rounded_rectangle((x, 492, x + 122, 614), radius=20, fill=theme["pale"], outline=theme["accent"], width=2)
        _center(draw, (x + 61, 553), character, 58, theme["accent"], True)
    if ended:
        _center(draw, (480, 653), "以上为本局答案", 24, MUTED)
    else:
        timer = f"本局截止：{datetime.fromtimestamp(float(due), timezone(timedelta(hours=8))).strftime('%H:%M:%S')}（北京时间）" if due else f"整局 {limit_minutes} 分钟 · 成功展示后开始计时"
        _center(draw, (480, 651), timer, 23, MUTED)
    _center(draw, (480, 701), "四位数字各不相同 · 首位非 0 · 其他位置可以有 0", 22, MUTED)
    _card(draw, (32, history_top, 928, history_top + history_height))
    _text(draw, (60, history_top + 20), "最近猜测", 29, bold=True)
    _text(draw, (595, history_top + 27), "最新在上 · 最多显示 12 次", 20, MUTED)
    for x, label in ((65, "步数"), (165, "玩家"), (503, "猜测"), (741, "反馈")):
        _text(draw, (x, history_top + 80), label, 21, MUTED, True)
    y = history_top + 122
    if not rows:
        _paragraph(draw, (66, y + 6), "还没有猜测记录，发送 #猜 1234 开始推理。", 820, 23, MUTED)
    for index, attempt in enumerate(rows):
        if index % 2 == 0:
            draw.rounded_rectangle((50, y, 910, y + 52), radius=12, fill=theme["pale"])
        _text(draw, (68, y + 9), attempt.get("number", max(1, total_attempts - index)), 22, MUTED, width=85)
        player_name = attempt.get("name") or players.get(str(attempt.get("user_id")), {}).get("name") or "玩家"
        _text(draw, (165, y + 9), player_name, 22, INK, width=307)
        _text(draw, (503, y + 6), attempt.get("guess", ""), 27, INK, True, width=177)
        _text(draw, (735, y + 7), f"{int(attempt.get('a', 0))}A{int(attempt.get('b', 0))}B", 26, theme["accent"], True, width=160)
        y += 57
    _card(draw, (32, stats_top, 928, stats_top + stats_height), fill=theme["pale"])
    _text(draw, (60, stats_top + 20), "本局统计" if ended else "推理提示", 29, bold=True)
    y = stats_top + 80
    for line in stats_lines:
        y += _paragraph(draw, (68, y), line, 820, 22, MUTED) + 9
    _paragraph(draw, (40, footer_top), "#题目 查看猜测记录 · #结束游戏 结束本局\n一群同时一局 · 普通数字聊天不参与猜测 · 不影响好感度和签到积分", 880, 20, MUTED)
    return _png(image)
