"""COC 克苏鲁的呼唤 跑团骰子插件

指令前缀: .
支持: .r .rd .ra/.rc .rh .rb .rp .coc .st .sc .en .ti .li .setcoc .jrrp .help
"""

import hashlib
import ast
import json
import operator
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


class DiceExpressionError(ValueError):
    """Raised when a user supplied dice expression is invalid or unsafe."""


_DICE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?P<count>\d{0,3})d(?P<sides>\d{1,5})"
    r"(?:(?P<keep>kh|kl|k|p)(?P<keep_count>\d{1,3}))?"
    r"(?![A-Za-z0-9_])",
    flags=re.IGNORECASE,
)
_MAX_DICE = 100
_MAX_SIDES = 10_000
_MAX_EXPRESSION_LENGTH = 200


def _safe_integer_eval(expression: str) -> int:
    """Evaluate only integer arithmetic nodes; never execute arbitrary input."""
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise DiceExpressionError("表达式格式错误") from exc

    binary_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: lambda left, right: int(left / right),
    }

    def visit(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return int(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, (ast.Div,)) and right == 0:
                raise DiceExpressionError("不能除以零")
            value = binary_ops[type(node.op)](left, right)
            if abs(value) > 10**9:
                raise DiceExpressionError("计算结果过大")
            return int(value)
        raise DiceExpressionError("只支持整数四则运算")

    return visit(tree)


def _eval_dice_expr(expr: str) -> tuple[int, str]:
    """解析并计算安全骰子表达式，如 ``7d3``、``d100``、``4d6kh3``。"""
    expression = (expr or "").strip().lower() or "d100"
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise DiceExpressionError("表达式过长")

    details: list[str] = []

    def replacer(match: re.Match[str]) -> str:
        count = int(match.group("count") or 1)
        sides = int(match.group("sides"))
        if count < 1 or count > _MAX_DICE:
            raise DiceExpressionError(f"单次最多掷 {_MAX_DICE} 个骰子")
        if sides < 1 or sides > _MAX_SIDES:
            raise DiceExpressionError(f"骰子面数必须在 1-{_MAX_SIDES} 之间")

        rolls = _roll(count, sides)
        keep_mode = (match.group("keep") or "").lower()
        keep_count = int(match.group("keep_count") or count)
        if keep_mode:
            if keep_count < 1 or keep_count > count:
                raise DiceExpressionError("保留骰子的数量必须在 1 到骰子数量之间")
            if keep_mode in {"kh", "k"}:
                kept = sorted(rolls, reverse=True)[:keep_count]
            else:
                kept = sorted(rolls)[:keep_count]
        else:
            kept = rolls

        detail = f"[{'+'.join(str(value) for value in rolls)}]"
        if keep_mode:
            detail += f"→{'+'.join(str(value) for value in kept)}"
        details.append(detail)
        return str(sum(kept))

    result_expression = _DICE_TOKEN.sub(replacer, expression)
    total = _safe_integer_eval(result_expression)
    return total, " ".join(details)


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
    # 主属性
    for a in _MAIN_ATTRS:
        if a in attrs:
            lines.append(f"{a}:{attrs[a]}")
    for a in ["教育", "幸运"]:
        if a in attrs:
            lines.append(f"{a}:{attrs[a]}")
    total = sum(attrs.get(a, 0) for a in _MAIN_ATTRS + ["教育"])
    if any(a in attrs for a in _MAIN_ATTRS):
        lines.append(f"合计:{total}")
    # 派生属性
    derived = []
    for a in ["HP", "MP", "SAN", "DB"]:
        if a in attrs:
            derived.append(f"{a}:{attrs[a]}")
    if derived:
        lines.append(" ".join(derived))
    # 自定义技能（非预定义项）
    _shown = set(_MAIN_ATTRS) | {"教育", "幸运", "HP", "MP", "SAN", "DB"}
    skills = {k: v for k, v in attrs.items() if k not in _shown}
    if skills:
        lines.append("─ 技能 ─")
        lines.extend(f"{k}:{v}" for k, v in sorted(skills.items()))
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


def _parse_command(text: str) -> tuple[str, str]:
    """Split a dot command and normalize compact aliases before dispatch."""
    parts = (text or "").strip().split(None, 1)
    if not parts:
        return "", ""
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # .coc 后面可能直接跟数字（如 .coc5 .COC3）。
    if cmd.startswith(".coc") and cmd != ".coc":
        suffix = cmd[4:]
        if suffix.isdigit():
            arg = suffix + (" " + arg if arg else "")
            cmd = ".coc"

    # .rd 后面可能直接跟面数（如 .rd6 .rd100）。
    if cmd.startswith(".rd") and cmd != ".rd":
        suffix = cmd[3:]
        if suffix.isdigit():
            arg = "d" + suffix + (" " + arg if arg else "")
            cmd = ".rd"

    # 紧凑骰点（如 .r7d3、.r#7 d3、.roll2d20）。仅当后缀明确像骰子
    # 表达式时才拆分，避免把未知的 .random 等命令误判成 .r。
    if cmd not in {".r", ".roll", ".ra", ".rc", ".rd", ".rh", ".rb", ".rp"}:
        compact_m = re.match(r"^\.(?:r|roll)(?P<suffix>(?:\d|d|#).*)$", cmd, re.IGNORECASE)
        if compact_m:
            suffix = compact_m.group("suffix")
            arg = suffix + (" " + arg if arg else "")
            cmd = ".r"

    stat_inline_m = re.match(r"^\.(hp|mp|san)([+-]\d+)$", cmd)
    if stat_inline_m:
        arg = stat_inline_m.group(2)
        cmd = "." + stat_inline_m.group(1)
    return cmd, arg


coc_matcher = on_message(rule=_dot_command_rule(), priority=6, block=True)


@coc_matcher.handle()
async def handle_coc(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    user_id = event.user_id
    group_id = event.group_id
    cmd, arg = _parse_command(text)

    # 路由
    if cmd in (".r", ".roll"):
        await _cmd_roll(event, arg)
    elif cmd == ".rd":
        await _cmd_roll(event, arg if arg else "d100")
    elif cmd in (".ra", ".rc"):
        await _cmd_ra(event, arg, user_id, group_id)
    elif cmd == ".rh":
        await _cmd_rh(bot, event, arg)
    elif cmd == ".rb":
        await _cmd_rb(event, arg, bonus=True)
    elif cmd == ".rp":
        await _cmd_rb(event, arg, bonus=False)
    elif cmd == ".coc":
        await _cmd_coc(event, arg, user_id, group_id)
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
    elif cmd == ".name":
        await _cmd_name(event, arg, user_id, group_id)
    elif cmd in (".del", ".delete"):
        await _cmd_del(event, arg, user_id, group_id)
    elif cmd in (".clr", ".clear"):
        await _cmd_clr(event, user_id, group_id)
    elif cmd in (".hp", ".mp", ".san"):
        await _cmd_stat_mod(event, cmd[1:], arg, user_id, group_id)
    elif cmd in (".help", ".h"):
        await _cmd_help(event)
    else:
        # 不是已知指令，不处理（让后续 matcher 继续）
        return


# ============ 指令实现 ============

async def _cmd_roll(event, arg: str):
    """.r 通用掷骰，支持 NdM、N#expr 和 #N expr 多轮骰。"""
    raw = event.get_plaintext().strip()

    # 多连骰格式: .r 3#d6  或  .r3#1d20
    normalized_arg = (arg or "").strip()
    multi_m = re.match(r"^(?:(\d+)\s*#\s*(.+)|#\s*(\d+)\s+(.+))$", normalized_arg)
    if multi_m:
        count = int(multi_m.group(1) or multi_m.group(3))
        expr = (multi_m.group(2) or multi_m.group(4) or "d100").strip()
        if count < 1 or count > 10:
            await coc_matcher.send("多轮骰次数必须在 1-10 之间")
            return
        results = []
        for _ in range(count):
            try:
                total, desc = _eval_dice_expr(expr)
            except DiceExpressionError as exc:
                await coc_matcher.send(f"骰子表达式错误：{exc}")
                return
            results.append(f"{desc} = {total}" if desc else str(total))
        msg = f"🎲 {raw} (共{count}次)\n"
        msg += "\n".join(f"{i + 1}. {r}" for i, r in enumerate(results))
        await coc_matcher.send(msg)
        return

    try:
        total, desc = _eval_dice_expr(normalized_arg or "d100")
    except DiceExpressionError as exc:
        await coc_matcher.send(f"骰子表达式错误：{exc}")
        return
    msg = f"🎲 {raw}\n"
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
    try:
        total, desc = _eval_dice_expr(arg if arg else "d100")
    except DiceExpressionError as exc:
        await coc_matcher.send(f"骰子表达式错误：{exc}")
        return
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


# 标准派生属性名规范化（用户可能输入小写）
_STAT_UPPER = {s.lower(): s for s in ["HP", "MP", "SAN", "DB"]}


async def _cmd_coc(event, arg: str, user_id: int, group_id: int):
    """.coc 快速建卡"""
    try:
        count = int(arg) if arg else 1
    except ValueError:
        count = 1
    count = max(1, min(count, 10))

    results = []
    single_attrs = None
    for i in range(count):
        attrs = _gen_coc_character()
        header = f"— 角色 {i + 1} —" if count > 1 else "— COC 7版人物作成 —"
        results.append(f"{header}\n{_format_attrs(attrs)}")
        if count == 1:
            single_attrs = attrs

    await coc_matcher.send("\n\n".join(results))

    # 单人建卡自动存档
    if single_attrs:
        session = await get_session()
        try:
            row = (await session.execute(
                select(CocCharacter).where(
                    CocCharacter.user_id == user_id,
                    CocCharacter.group_id == group_id
                )
            )).scalar_one_or_none()
            if not row:
                row = CocCharacter(user_id=user_id, group_id=group_id)
                session.add(row)
            row.attributes = single_attrs
            row.san = single_attrs.get("SAN", 0)
            row.hp = single_attrs.get("HP", 0)
            row.mp = single_attrs.get("MP", 0)
            await session.commit()
            await coc_matcher.send("✅ 已自动存档，.st show 查看，.st 属性 值 修改")
        finally:
            await session.close()


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
                    # 规范化标准属性名大小写（hp→HP, san→SAN 等）
                    key = _STAT_UPPER.get(tokens[i].lower(), tokens[i])
                    attrs[key] = val
                    updated.append(f"{key}={val}")
                    i += 2
                    continue
                except ValueError:
                    pass
            # 尝试匹配 "属性值" 一体格式，如 "力量60"
            m = re.match(r"(.+?)(\d+)$", tokens[i])
            if m:
                key = _STAT_UPPER.get(m.group(1).lower(), m.group(1))
                attrs[key] = int(m.group(2))
                updated.append(f"{key}={m.group(2)}")
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

    try:
        # Validate both loss expressions before reading or changing the card.
        _eval_dice_expr(success_expr)
        _eval_dice_expr(fail_expr)
    except DiceExpressionError as exc:
        await coc_matcher.send(f"理智损失表达式错误：{exc}")
        return

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


async def _cmd_name(event, arg: str, user_id: int, group_id: int):
    """.name 设置角色名"""
    name = arg.strip()
    if not name:
        await coc_matcher.send("用法: .name 角色名\n例: .name 约翰·史密斯")
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
            row = CocCharacter(user_id=user_id, group_id=group_id, attributes={}, name=name)
            session.add(row)
        else:
            row.name = name
        await session.commit()
        await coc_matcher.send(f"✅ 角色名已设置: {name}")
    finally:
        await session.close()


async def _cmd_del(event, arg: str, user_id: int, group_id: int):
    """.del 删除单个属性"""
    attr_name = arg.strip()
    if not attr_name:
        await coc_matcher.send("用法: .del 属性名\n例: .del 射击")
        return
    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()
        if not row or not row.attributes or attr_name not in row.attributes:
            await coc_matcher.send(f"属性 [{attr_name}] 不存在")
            return
        attrs = dict(row.attributes)
        del attrs[attr_name]
        row.attributes = attrs
        await session.commit()
        await coc_matcher.send(f"✅ 已删除属性: {attr_name}")
    finally:
        await session.close()


async def _cmd_clr(event, user_id: int, group_id: int):
    """.clr 清空角色卡"""
    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()
        if not row:
            await coc_matcher.send("你没有角色卡")
            return
        row.attributes = {}
        row.name = ""
        row.san = 0
        row.hp = 0
        row.mp = 0
        await session.commit()
        await coc_matcher.send("✅ 角色卡已清空")
    finally:
        await session.close()


async def _cmd_stat_mod(event, stat_key: str, delta_str: str, user_id: int, group_id: int):
    """.hp/.mp/.san 查看或修改数值"""
    attr_name = stat_key.upper()  # hp→HP mp→MP san→SAN
    session = await get_session()
    try:
        row = (await session.execute(
            select(CocCharacter).where(
                CocCharacter.user_id == user_id,
                CocCharacter.group_id == group_id
            )
        )).scalar_one_or_none()

        if not row or not row.attributes or attr_name not in row.attributes:
            await coc_matcher.send(f"未找到 {attr_name}，请先用 .st {attr_name} 值 设置")
            return

        old_val = row.attributes[attr_name]

        # 无参数 → 仅显示当前值
        if not delta_str:
            await coc_matcher.send(f"当前 {attr_name}: {old_val}")
            return

        try:
            delta = int(delta_str)
        except ValueError:
            await coc_matcher.send(f"用法: .{stat_key}+5  或  .{stat_key}-3")
            return

        new_val = max(0, old_val + delta)
        attrs = dict(row.attributes)
        attrs[attr_name] = new_val
        row.attributes = attrs
        if attr_name == "HP":
            row.hp = new_val
        elif attr_name == "MP":
            row.mp = new_val
        elif attr_name == "SAN":
            row.san = new_val
        await session.commit()

        sign = "+" if delta >= 0 else ""
        msg = f"💊 {attr_name}: {old_val} → {new_val}（{sign}{delta}）"
        if attr_name == "HP" and new_val == 0:
            msg += "\n💀 HP归零，调查员死亡！"
        elif attr_name == "SAN" and new_val == 0:
            msg += "\n⚠️ SAN值归零，调查员永久疯狂！"
        await coc_matcher.send(msg)
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
        "【骰子】\n"
        ".r [表达式] — 掷骰 (如 .r 3d6+2 或 .r7d3)\n"
        ".r N#表达式 / .r#N 表达式 — 多轮骰 (如 .r 3#d6)\n"
        ".rd[面数] — 快速掷骰 (.rd .rd6 .rd100)\n"
        ".ra/.rc 属性 [值] — 属性检定\n"
        ".rh [表达式] — 暗骰(私聊)\n"
        ".rb/.rp [N] — 奖励骰/惩罚骰\n"
        "\n【角色卡】\n"
        ".coc [N] — 快速建卡\n"
        ".name 名字 — 设置角色名\n"
        ".st 属性 值 — 设置属性 (可批量)\n"
        ".st show — 查看角色卡\n"
        ".del 属性 — 删除某属性\n"
        ".clr — 清空角色卡\n"
        "\n【战斗/成长】\n"
        ".hp+N / .hp-N — 修改HP\n"
        ".mp+N / .mp-N — 修改MP\n"
        ".san+N / .san-N — 修改SAN\n"
        ".sc 成功损失/失败损失 — 理智检定\n"
        ".en 属性 — 成长检定\n"
        "\n【其他】\n"
        ".ti/.li — 随机疯狂症状\n"
        ".setcoc [0-5] — 设置判定规则\n"
        ".jrrp — 今日人品"
    )
