"""COC 克苏鲁的呼唤 跑团骰子插件

指令前缀: .
支持: .r .rd .ra .rh .rb .rp .coc .st .sc .en .ti .li .setcoc .jrrp .help
"""

import hashlib
import json
import random
import re
from datetime import date

import yaml
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.rule import Rule
from sqlalchemy import BigInteger, Integer, String, JSON, SmallInteger, select

from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, engine, get_session

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# ---------- ORM ----------


class CocCharacter(Base):
    __tablename__ = "coc_characters"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), default="")
    attributes: Mapped[dict | None] = mapped_column(JSON, default=None)
    san: Mapped[int] = mapped_column(Integer, default=0)
    hp: Mapped[int] = mapped_column(Integer, default=0)
    mp: Mapped[int] = mapped_column(Integer, default=0)
    coc_rule: Mapped[int] = mapped_column(SmallInteger, default=5)


# ---------- 建表 ----------
from nonebot import get_driver


@get_driver().on_startup
async def _create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ============ 骰子解析 ============

def _roll(n: int, m: int) -> list[int]:
    """掷 n 个 m 面骰"""
    return [random.randint(1, m) for _ in range(n)]


def _eval_dice_expr(expr: str) -> tuple[int, str]:
    """
    解析并计算骰子表达式, 如 3d6+2, d100, 2d10kh1
    返回 (总值, 过程描述)
    """
    expr = expr.strip().lower()
    if not expr:
        expr = "d100"

    # 匹配 NdM 模式
    pattern = re.compile(r"(\d*)d(\d+)(?:(kh|kl)(\d+))?")

    def replacer(m):
        n = int(m.group(1)) if m.group(1) else 1
        sides = int(m.group(2))
        n = min(n, 100)  # 防滥用
        sides = min(sides, 10000)
        rolls = _roll(n, sides)
        keep_mode = m.group(3)
        keep_n = int(m.group(4)) if m.group(4) else n

        if keep_mode == "kh":
            kept = sorted(rolls, reverse=True)[:keep_n]
        elif keep_mode == "kl":
            kept = sorted(rolls)[:keep_n]
        else:
            kept = rolls

        detail = f"[{'+'.join(str(r) for r in rolls)}]"
        if keep_mode:
            detail += f"→{'+'.join(str(r) for r in kept)}"
        return str(sum(kept)), detail

    details = []
    result_expr = expr

    for m in pattern.finditer(expr):
        val, detail = replacer(m)
        details.append(detail)
        result_expr = result_expr.replace(m.group(0), val, 1)

    # 安全计算剩余的加减乘除
    allowed = set("0123456789+-*/(). ")
    if all(c in allowed for c in result_expr):
        try:
            total = int(eval(result_expr))  # noqa: S307 - input is sanitized
        except Exception:
            total = 0
    else:
        total = 0

    desc = " ".join(details) if details else ""
    return total, desc


# ============ COC 判定规则 ============

COC_RULES = {
    0: "规则0: 出1大成功; 不满50出96-100大失败, 满50出101大失败",
    1: "规则1: 出1-5且≤成功率为大成功; 出96-100且>成功率为大失败",
    2: "规则2: 出1-5且≤成功率/5为大成功; 出96-100为大失败",
    3: "规则3: 出1-5为大成功; 出96-100为大失败",
    4: "规则4: 出1-5且≤成功率/10为大成功; 出≥96+成功率/10为大失败",
    5: "规则5: 出1-2且<成功率/5为大成功; 出96-100且≥96+成功率/10为大失败",
}


def _check_result(roll_val: int, skill_val: int, rule: int = 5) -> str:
    """根据规则判定检定结果"""
    is_critical = False  # 大成功
    is_fumble = False    # 大失败

    if rule == 0:
        is_critical = roll_val == 1
        if skill_val < 50:
            is_fumble = roll_val >= 96
        else:
            is_fumble = roll_val > 100
    elif rule == 1:
        is_critical = roll_val <= 5 and roll_val <= skill_val
        is_fumble = roll_val >= 96 and roll_val > skill_val
    elif rule == 2:
        is_critical = roll_val <= 5 and roll_val <= skill_val // 5
        is_fumble = roll_val >= 96
    elif rule == 3:
        is_critical = roll_val <= 5
        is_fumble = roll_val >= 96
    elif rule == 4:
        is_critical = roll_val <= 5 and roll_val <= skill_val // 10
        is_fumble = roll_val >= 96 + skill_val // 10
    else:  # rule 5 (默认)
        is_critical = roll_val <= 2 and roll_val < skill_val // 5
        is_fumble = roll_val >= 96 and roll_val >= 96 + skill_val // 10

    if is_critical:
        return "🎉 大成功！"
    if is_fumble:
        return "💀 大失败！"
    if roll_val <= skill_val // 5:
        return "✨ 极难成功"
    if roll_val <= skill_val // 2:
        return "🌟 困难成功"
    if roll_val <= skill_val:
        return "✅ 成功"
    return "❌ 失败"


# ============ COC 建卡 ============

_MAIN_ATTRS = ["力量", "体质", "体型", "敏捷", "外貌", "智力", "意志"]

def _gen_coc_character() -> dict:
    attrs = {}
    for a in _MAIN_ATTRS:
        attrs[a] = sum(_roll(3, 6)) * 5
    attrs["教育"] = (sum(_roll(2, 6)) + 6) * 5
    attrs["幸运"] = sum(_roll(3, 6)) * 5
    # 派生属性
    attrs["HP"] = (attrs["体质"] + attrs["体型"]) // 10
    attrs["MP"] = attrs["意志"] // 5
    attrs["SAN"] = attrs["意志"]
    # 伤害加值
    db_val = attrs["力量"] + attrs["体型"]
    if db_val <= 64:
        attrs["DB"] = "-2"
    elif db_val <= 84:
        attrs["DB"] = "-1"
    elif db_val <= 124:
        attrs["DB"] = "0"
    elif db_val <= 164:
        attrs["DB"] = "+1d4"
    elif db_val <= 204:
        attrs["DB"] = "+1d6"
    else:
        attrs["DB"] = "+2d6"
    return attrs


def _format_attrs(attrs: dict) -> str:
    lines = []
    for a in _MAIN_ATTRS:
        if a in attrs:
            lines.append(f"{a}:{attrs[a]}")
    for a in ["教育", "幸运"]:
        if a in attrs:
            lines.append(f"{a}:{attrs[a]}")
    total = sum(attrs.get(a, 0) for a in _MAIN_ATTRS + ["教育"])
    lines.append(f"合计:{total}")
    derived = []
    for a in ["HP", "MP", "SAN", "DB"]:
        if a in attrs:
            derived.append(f"{a}:{attrs[a]}")
    if derived:
        lines.append(" ".join(derived))
    return "\n".join(lines)


# ============ 疯狂症状表 ============

TI_TABLE = [
    "失忆：调查员回过神来，发现自己身处一个陌生的地方，不知道自己是怎么到这里的。",
    "假性残疾：调查员陷入了心理性的失明、失聪或躯体缺失感中。",
    "暴力倾向：调查员陷入了暴力狂潮，对周围的人和物进行攻击。",
    "偏执：调查员陷入了严重的偏执幻想中。",
    "人际依赖：调查员变得极度依赖某个在场的人。",
    "昏厥：调查员当场昏倒。",
    "逃跑：调查员竭尽全力试图逃离当前的场景。",
    "歇斯底里：调查员陷入了大笑、大哭或无法控制的尖叫中。",
    "恐惧症：调查员获得一个新的恐惧症，并在当前场景中表现出来。",
    "狂躁症：调查员获得一个新的狂躁症，并在当前场景中表现出来。",
    "梦游：调查员开始无意识地在周围走动。",
    "自残倾向：调查员试图伤害自己。",
    "强迫行为：调查员反复进行某种无意义的动作。",
    "幻觉：调查员产生了强烈的幻觉。",
    "回声现象：调查员不断重复别人说的话或动作。",
    "胡言乱语：调查员说出令人费解的话语。",
    "抑郁发作：调查员陷入极度悲伤，对一切失去兴趣。",
    "恐慌发作：心跳加速、呼吸困难、大量出汗。",
    "退行行为：调查员表现得像个小孩子一样。",
    "食欲异常：调查员开始不受控制地进食或拒绝食物。",
]

LI_TABLE = [
    "失忆：调查员回过神来，发现自己身处一个陌生的地方，失去了大段记忆。",
    "被窃取：调查员相信某个实体或个人窃取了自己的某样东西。",
    "信念/精神障碍：调查员出现了某种精神障碍。",
    "恐惧症：调查员获得了一个持续性的恐惧症。",
    "狂躁症：调查员获得了一个持续性的狂躁症。",
    "偏执：调查员产生了长期的偏执妄想。",
    "强迫症：调查员获得了强迫性行为模式。",
    "妄想：调查员坚信一个与现实不符的信念。",
    "精神分裂：调查员出现了分裂症样症状。",
    "焦虑症：调查员获得了持续性焦虑障碍。",
    "人格改变：调查员的性格发生了显著变化。",
    "依赖症：调查员对某种物质或行为产生了依赖。",
    "梦魇：调查员长期被噩梦折磨，影响睡眠。",
    "创伤应激：调查员出现了PTSD相关症状。",
    "社交恐惧：调查员开始害怕社交活动。",
    "暴食/厌食：调查员出现了饮食障碍。",
    "幻觉持续：调查员间歇性出现幻听或幻视。",
    "自伤倾向：调查员会无意识地伤害自己。",
    "解离：调查员将自己从现实中「断开」。",
    "疑病症：调查员坚信自己患有某种严重疾病。",
]

# ============ 指令路由 ============


def _dot_command_rule() -> Rule:
    """检测以 . 开头的指令"""
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        text = event.get_plaintext().strip()
        return bool(text) and text.startswith(".")
    return Rule(_rule)


coc_matcher = on_message(rule=_dot_command_rule(), priority=6, block=True)


@coc_matcher.handle()
async def handle_coc(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    user_id = event.user_id
    group_id = event.group_id

    # 解析指令 — 取第一个空格前的部分
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # .coc 后面可能直接跟数字（如 .coc5 .COC3），提取数字作为参数
    if cmd.startswith(".coc") and cmd != ".coc":
        suffix = cmd[4:]
        if suffix.isdigit():
            arg = suffix + (" " + arg if arg else "")
            cmd = ".coc"

    # 路由
    if cmd in (".r", ".roll"):
        await _cmd_roll(event, arg)
    elif cmd == ".rd":
        await _cmd_roll(event, "d100")
    elif cmd == ".ra":
        await _cmd_ra(event, arg, user_id, group_id)
    elif cmd == ".rh":
        await _cmd_rh(bot, event, arg)
    elif cmd == ".rb":
        await _cmd_rb(event, arg, bonus=True)
    elif cmd == ".rp":
        await _cmd_rb(event, arg, bonus=False)
    elif cmd == ".coc":
        await _cmd_coc(event, arg)
    elif cmd == ".st":
        await _cmd_st(event, arg, user_id, group_id)
    elif cmd == ".sc":
        await _cmd_sc(event, arg, user_id, group_id)
    elif cmd == ".en":
        await _cmd_en(event, arg, user_id, group_id)
    elif cmd == ".ti":
        await _cmd_ti(event)
    elif cmd == ".li":
        await _cmd_li(event)
    elif cmd == ".setcoc":
        await _cmd_setcoc(event, arg, user_id, group_id)
    elif cmd == ".jrrp":
        await _cmd_jrrp(event, user_id)
    elif cmd in (".help", ".h"):
        await _cmd_help(event)
    else:
        # 不是已知指令，不处理（让后续 matcher 继续）
        return


# ============ 指令实现 ============

async def _cmd_roll(event, arg: str):
    """.r 通用掷骰"""
    total, desc = _eval_dice_expr(arg if arg else "d100")
    msg = f"🎲 {event.get_plaintext().strip()}\n"
    if desc:
        msg += f"{desc}\n"
    msg += f"= {total}"
    await coc_matcher.send(msg)


async def _cmd_ra(event, arg: str, user_id: int, group_id: int):
    """.ra 属性检定"""
    parts = arg.split()
    if not parts:
        await coc_matcher.send("用法: .ra 属性名 [属性值]\n例: .ra 侦查 60")
        return

    attr_name = parts[0]
    skill_val = None

    if len(parts) >= 2:
        try:
            skill_val = int(parts[1])
        except ValueError:
            pass

    # 如果没指定值，从角色卡读取
    if skill_val is None:
        session = await get_session()
        try:
            row = (await session.execute(
                select(CocCharacter).where(
                    CocCharacter.user_id == user_id,
                    CocCharacter.group_id == group_id
                )
            )).scalar_one_or_none()
            if row and row.attributes:
                skill_val = row.attributes.get(attr_name)
            coc_rule = row.coc_rule if row else 5
        finally:
            await session.close()
    else:
        session = await get_session()
        try:
            row = (await session.execute(
                select(CocCharacter).where(
                    CocCharacter.user_id == user_id,
                    CocCharacter.group_id == group_id
                )
            )).scalar_one_or_none()
            coc_rule = row.coc_rule if row else 5
        finally:
            await session.close()

    if skill_val is None:
        await coc_matcher.send(f"未找到属性 [{attr_name}]，请用 .st 设置或指定数值\n例: .ra {attr_name} 60")
        return

    roll_val = random.randint(1, 100)
    result = _check_result(roll_val, skill_val, coc_rule)
    await coc_matcher.send(f"🎲 {attr_name}检定: D100={roll_val}/{skill_val}\n{result}")


async def _cmd_rh(bot: Bot, event, arg: str):
    """.rh 暗骰 — 结果私聊"""
    total, desc = _eval_dice_expr(arg if arg else "d100")
    msg = f"🎲 暗骰: {arg or 'd100'}\n"
    if desc:
        msg += f"{desc}\n"
    msg += f"= {total}"
    try:
        await bot.send_private_msg(user_id=event.user_id, message=msg)
        await coc_matcher.send("🤫 已暗骰，结果已私聊发送")
    except Exception:
        await coc_matcher.send(f"暗骰失败（无法私聊），结果: {total}")


async def _cmd_rb(event, arg: str, bonus: bool):
    """.rb 奖励骰 / .rp 惩罚骰"""
    try:
        n = int(arg) if arg else 1
    except ValueError:
        n = 1
    n = max(1, min(n, 10))

    ones = random.randint(0, 9)  # 个位
    tens_list = [random.randint(0, 9) for _ in range(n + 1)]  # 多个十位

    if bonus:
        chosen_ten = min(tens_list)
        label = "奖励骰"
    else:
        chosen_ten = max(tens_list)
        label = "惩罚骰"

    result = chosen_ten * 10 + ones
    if result == 0:
        result = 100

    tens_str = ", ".join(str(t * 10) for t in tens_list)
    await coc_matcher.send(
        f"🎲 {label}(×{n})\n"
        f"十位: [{tens_str}], 个位: {ones}\n"
        f"取{'最小' if bonus else '最大'}十位 → D100 = {result}"
    )


async def _cmd_coc(event, arg: str):
    """.coc 快速建卡"""
    try:
        count = int(arg) if arg else 1
    except ValueError:
        count = 1
    count = max(1, min(count, 10))

    results = []
    for i in range(count):
        attrs = _gen_coc_character()
        header = f"— 角色 {i + 1} —" if count > 1 else "— COC 7版人物作成 —"
        results.append(f"{header}\n{_format_attrs(attrs)}")

    await coc_matcher.send("\n\n".join(results))


async def _cmd_st(event, arg: str, user_id: int, group_id: int):
    """.st 设置/查看属性"""
    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()

        if not arg or arg.lower() == "show":
            if not row or not row.attributes:
                await coc_matcher.send("你还没有角色卡，使用 .coc 建卡或 .st 属性名 值 来设置")
                return
            name_str = f"[{row.name}]" if row.name else ""
            await coc_matcher.send(f"📋 角色卡{name_str}\n{_format_attrs(row.attributes)}")
            return

        if not row:
            row = CocCharacter(user_id=user_id, group_id=group_id, attributes={})
            session.add(row)

        attrs = dict(row.attributes) if row.attributes else {}

        # 解析  .st 力量 60 敏捷 70  格式
        tokens = arg.split()
        i = 0
        updated = []
        while i < len(tokens):
            if i + 1 < len(tokens):
                try:
                    val = int(tokens[i + 1])
                    attrs[tokens[i]] = val
                    updated.append(f"{tokens[i]}={val}")
                    i += 2
                    continue
                except ValueError:
                    pass
            # 尝试匹配 "属性值" 一体格式，如 "力量60"
            m = re.match(r"(.+?)(\d+)$", tokens[i])
            if m:
                attrs[m.group(1)] = int(m.group(2))
                updated.append(f"{m.group(1)}={m.group(2)}")
            i += 1

        if not updated:
            await coc_matcher.send("用法: .st 属性名 值 [属性名 值 ...]\n例: .st 力量 60 敏捷 70")
            return

        row.attributes = attrs
        # 同步 SAN/HP/MP
        if "SAN" in attrs:
            row.san = attrs["SAN"]
        if "HP" in attrs:
            row.hp = attrs["HP"]
        if "MP" in attrs:
            row.mp = attrs["MP"]

        await session.commit()
        await coc_matcher.send(f"✅ 已更新: {', '.join(updated)}")
    finally:
        await session.close()


async def _cmd_sc(event, arg: str, user_id: int, group_id: int):
    """.sc 理智检定  格式: .sc 成功损失/失败损失"""
    if "/" not in arg:
        await coc_matcher.send("用法: .sc 成功损失/失败损失\n例: .sc 1/1d6")
        return

    success_expr, fail_expr = arg.split("/", 1)

    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()

        if not row or not row.attributes or "SAN" not in row.attributes:
            await coc_matcher.send("请先设置 SAN 值: .st SAN 值")
            return

        san = row.attributes.get("SAN", row.san)
        roll_val = random.randint(1, 100)
        success = roll_val <= san

        if success:
            loss, loss_desc = _eval_dice_expr(success_expr)
        else:
            loss, loss_desc = _eval_dice_expr(fail_expr)

        new_san = max(0, san - loss)
        row.attributes = {**row.attributes, "SAN": new_san}
        row.san = new_san
        await session.commit()

        result_str = "成功" if success else "失败"
        msg = (
            f"🧠 理智检定: D100={roll_val}/{san} → {result_str}\n"
            f"SAN损失: {loss}"
        )
        if loss_desc:
            msg += f" ({loss_desc})"
        msg += f"\n当前SAN: {san} → {new_san}"
        if new_san == 0:
            msg += "\n⚠️ SAN值归零，调查员永久疯狂！"
        await coc_matcher.send(msg)
    finally:
        await session.close()


async def _cmd_en(event, arg: str, user_id: int, group_id: int):
    """.en 成长检定"""
    if not arg:
        await coc_matcher.send("用法: .en 属性名\n例: .en 射击")
        return

    attr_name = arg.strip()
    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()

        if not row or not row.attributes or attr_name not in row.attributes:
            await coc_matcher.send(f"未找到属性 [{attr_name}]，请先 .st 设置")
            return

        current = row.attributes[attr_name]
        roll_val = random.randint(1, 100)

        if roll_val > current or roll_val >= 96:
            growth = random.randint(1, 10)
            new_val = current + growth
            row.attributes = {**row.attributes, attr_name: new_val}
            await session.commit()
            await coc_matcher.send(
                f"📈 {attr_name}成长检定: D100={roll_val}/{current} → 成长！\n"
                f"+{growth}（1d10）: {current} → {new_val}"
            )
        else:
            await coc_matcher.send(
                f"📈 {attr_name}成长检定: D100={roll_val}/{current} → 未成长"
            )
    finally:
        await session.close()


async def _cmd_ti(event):
    """.ti 临时疯狂"""
    idx = random.randint(1, len(TI_TABLE))
    await coc_matcher.send(f"🌀 临时疯狂症状 (1d{len(TI_TABLE)}={idx}):\n{TI_TABLE[idx - 1]}")


async def _cmd_li(event):
    """.li 总结疯狂"""
    idx = random.randint(1, len(LI_TABLE))
    await coc_matcher.send(f"🌀 总结疯狂症状 (1d{len(LI_TABLE)}={idx}):\n{LI_TABLE[idx - 1]}")


async def _cmd_setcoc(event, arg: str, user_id: int, group_id: int):
    """.setcoc 设置判定规则"""
    if not arg:
        msg = "当前可用规则:\n"
        for k, v in COC_RULES.items():
            msg += f"  {v}\n"
        msg += "\n用法: .setcoc 5"
        await coc_matcher.send(msg)
        return

    try:
        rule = int(arg)
    except ValueError:
        await coc_matcher.send("规则编号应为 0-5 的整数")
        return

    if rule not in COC_RULES:
        await coc_matcher.send("规则编号应为 0-5")
        return

    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()

        if not row:
            row = CocCharacter(user_id=user_id, group_id=group_id, attributes={}, coc_rule=rule)
            session.add(row)
        else:
            row.coc_rule = rule

        await session.commit()
        await coc_matcher.send(f"✅ 已设置: {COC_RULES[rule]}")
    finally:
        await session.close()


async def _cmd_jrrp(event, user_id: int):
    """.jrrp 今日人品"""
    seed = f"{user_id}:{date.today().isoformat()}"
    h = hashlib.md5(seed.encode()).hexdigest()  # noqa: S324 - not for security
    rp = int(h[:8], 16) % 101
    await coc_matcher.send(f"🍀 今日人品: {rp}/100")


async def _cmd_help(event):
    """.help 帮助"""
    await coc_matcher.send(
        "📖 COC跑团指令帮助\n"
        "—————————————\n"
        ".r [表达式] — 掷骰 (如 .r 3d6+2)\n"
        ".rd — 快速D100\n"
        ".ra 属性 [值] — 属性检定\n"
        ".rh [表达式] — 暗骰(私聊)\n"
        ".rb/.rp [N] — 奖励骰/惩罚骰\n"
        ".coc [N] — 快速建卡\n"
        ".st 属性 值 — 设置属性\n"
        ".st show — 查看角色卡\n"
        ".sc 成功/失败 — 理智检定\n"
        ".en 属性 — 成长检定\n"
        ".ti/.li — 疯狂症状表\n"
        ".setcoc [0-5] — 设置判定规则\n"
        ".jrrp — 今日人品"
    )
