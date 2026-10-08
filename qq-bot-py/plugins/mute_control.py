"""好感度与禁言风控插件

功能：
1. 好感评分：用户 @bot 后后台异步调用 DeepSeek 分析发言，更新好感度（-100~100）
2. 自动禁言：好感度 ≤ -80 时按概率直接禁言；所有好感度都会由 DeepSeek 独立判断凛是否想禁言
3. 趣味"禁言我"：用户 @bot 并说"禁言我"时直接禁言 1 分钟
4. 查询与管理指令：
   - #好感度 / #查询好感度 — 查看本群好感度榜单（隐藏管理员）
   - #查询全部好感度 [群号] — 完整榜单（仅管理员）
   - #个人好感度 <QQ> — 查看单人好感度详情（setu.admin_users 可用）
   - #重置好感度 <QQ> — 重置好感度为 0（setu.admin_users 可用）
   - #放人 <QQ>    — 解除禁言（mute_control.release_users 可用，管理员也可用）
5. 每周一 00:00 定时任务：禁言次数归零
"""

import base64
import json
import math
import random
import re
import httpx
import yaml
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import unicodedata

from nonebot import on_command, on_message, get_driver, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.rule import Rule
from nonebot.log import logger
from sqlalchemy import select, text, distinct
from PIL import Image, ImageDraw, ImageFont

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from .db import (
    AgentPersonaAffinity,
    AgentPersonaRelationshipState,
    AgentRelationshipState,
    Base,
    engine,
    get_session,
    GroupMessage,
    UserRisk,
    UserAffinity,
    UserMuteRecord,
)
from .affinity import curved_delta, relationship_stage

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_ai_cfg = _config["ai"]
_agent_cfg = _config.get("agent") or {}
_affinity_cfg = _agent_cfg.get("affinity") or {}
_fb_cfg = _ai_cfg.get("fallback", {})
_DS_URL   = _fb_cfg.get("api_url", "")
_DS_KEY   = _fb_cfg.get("api_key", "")
_DS_MODEL = _fb_cfg.get("model", "deepseek-v4-flash")

_mute_cfg = _config.get("mute_control", {})
_AGENT_GROUPS: set[int] = {
    int(value) for value in ((_config.get("agent") or {}).get("active_groups") or [])
}
_ADMINS: set[int]        = {int(u) for u in _config.get("setu", {}).get("admin_users", [])}
# The owner is intentionally hidden from ordinary affinity leaderboards, but
# remains in the database and can be included by the owner-only full-list
# command.
_HIDDEN_AFFINITY_USER_ID = int(
    ((_agent_cfg.get("group") or {}).get("owner_user_id") or 0)
)
_RELEASE_USERS: set[int] = {int(u) for u in _mute_cfg.get("release_users", [])}
_EXTREME_ENABLED: bool = _mute_cfg.get("extreme_enabled", True)
_EXTREME_MUTE_DURATION: int = int(_mute_cfg.get("extreme_mute_duration", 600))
_EXTREME_API_URL: str = _mute_cfg.get("extreme_api_url") or _fb_cfg.get("api_url", "")
_EXTREME_API_KEY: str = _mute_cfg.get("extreme_api_key") or _fb_cfg.get("api_key", "")
_EXTREME_MODEL: str = _mute_cfg.get("extreme_model") or _fb_cfg.get("model", "deepseek-v4-flash")
_EXTREME_PROXY: str | None = _mute_cfg.get("extreme_proxy") or _fb_cfg.get("proxy") or None
_EXTREME_TIMEOUT: int = int(_mute_cfg.get("extreme_timeout", 20))

_EXTREME_HARD_PATTERNS = [
    re.compile(r"所有人.*?(变成|变为|成为).*?(婊|奴|母狗|rbq|肉便器)", re.IGNORECASE),
    re.compile(r"(变成|变为|成为).*?(喷乳|喷奶|榨乳|发情).*?(婊|奴|母狗|rbq|肉便器)", re.IGNORECASE),
    re.compile(r"(喷乳|喷奶|榨乳).*?(婊|奴|母狗|rbq|肉便器)", re.IGNORECASE),
]
_EXTREME_RIN_REPLIES = [
    "这种话也说得出口？退场冷静。",
    "真不像话。先安静十分钟。",
    "下限到此为止，给我反省去。",
    "嘴巴放干净点，别逼我动真格。",
    "这种程度的失礼，可别怪我不客气。",
]


# ---------- 建表 ----------
@get_driver().on_startup  # type: ignore[attr-defined]
async def _init_mute_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            """
            INSERT INTO user_affinity (user_id, affinity_score, last_delta, last_reason, updated_at)
            SELECT user_id, GREATEST(-100, LEAST(100, -risk_score)), 0, 'migrated from user_risk', updated_at
            FROM user_risk
            WHERE user_id NOT IN (SELECT user_id FROM user_affinity)
            """
        ))
    logger.info("[mute_control] 数据表已就绪")


# ---------- 好感评分核心 ----------

async def _get_user_context_text(user_id: int, group_id: int) -> str:
    """取该群最近 30 条消息（含上下文），目标用户发言加【目标用户】标注"""
    session = await get_session()
    try:
        rows = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(30)
        )).scalars().all()
        rows = list(reversed(rows))
        lines = []
        for r in rows:
            if r.is_bot:
                lines.append(f"凛(bot): {r.content}")
            elif r.user_id == user_id:
                lines.append(f"【目标用户】{r.nickname}: {r.content}")
            else:
                lines.append(f"{r.nickname}: {r.content}")
        return "\n".join(lines)
    finally:
        await session.close()


async def _evaluate_risk_delta(context_text: str, user_message: str | None = None, bot_reply: str | None = None) -> tuple[int, str]:
    """调用 DeepSeek 分析互动对好感度的影响，返回 (delta, reason)。"""
    if not _DS_URL or not _DS_KEY:
        return 0, ""

    # 当前触发消息（最重要的判断依据，直接传入不依赖DB历史）
    current_msg_section = ""
    if user_message:
        current_msg_section = f"\n\n【目标用户本次发言（触发评估的消息）】：\n{user_message}"

    bot_reply_section = ""
    if bot_reply:
        bot_reply_section = f"\n\n【凛的回应】（注意：凛是傲娇，嘴硬、害羞、吐槽不一定代表讨厌）：\n凛(bot): {bot_reply}"

    history_section = f"\n\n【历史上下文（仅供参考）】：\n{context_text}" if context_text else ""

    prompt = (
        "你是远坂凛群聊机器人的好感度评估员。请判断【目标用户本次发言】会让凛对这个用户的好感如何变化。\n"
        "凛是傲娇：嘴硬、吐槽、害羞、说“笨蛋”“才没有”不等于好感下降；看似拒绝也可能是害羞或正常傲娇互动。\n"
        "只输出 JSON，不要任何解释。格式：{\"delta\":0,\"reason\":\"普通闲聊\"}。\n"
        "delta 必须是 -2 到 +2 的整数，reason 用不超过30字中文说明原因。\n\n"
        "评分标准：\n"
        " +2 = 真诚关心、道谢、认真交流、尊重边界、让凛明显放松或开心\n"
        " +1 = 普通友好、轻松玩笑、正常求助、没有越界的亲近\n"
        "  0 = 普通闲聊、信息讨论、玩梗但不明显影响关系\n"
        " -1 = 轻微冒犯、烦人、阴阳怪气、让凛有点不快但不严重\n"
        " -2 = 明显骚扰、恶意辱骂、持续越界、露骨性暗示、让凛强烈厌烦\n"
        "判断顺序：先看目标用户本次发言，再参考凛的回应，历史仅作背景。"
        f"{current_msg_section}"
        f"{bot_reply_section}"
        f"{history_section}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _DS_URL,
                headers={"Authorization": f"Bearer {_DS_KEY}"},
                json={
                    "model": _DS_MODEL,
                    "messages": [{"role": "user", "content": prompt + "\n/no_think"}],
                    "max_tokens": 100,
                    "temperature": 0.2,
                },
            )
            data = resp.json()
            msg = data["choices"][0]["message"]
            raw = (msg.get("content") or "").strip()
            if not raw:
                raw = (msg.get("reasoning_content") or "").strip()
            obj = _extract_json_obj(raw)
            if obj:
                delta_raw = obj.get("delta", 0)
                reason = re.sub(r"\s+", " ", str(obj.get("reason") or "")).strip()[:80]
                try:
                    delta = int(float(delta_raw))
                except Exception:
                    delta = 0
                return max(-2, min(2, delta)), reason
            m = re.search(r"-?\d+", raw)
            if m:
                return max(-2, min(2, int(m.group()))), ""
    except Exception as e:
        logger.warning(f"[mute_control] DeepSeek 好感评分失败: {e}")
    return 0, ""


async def _update_risk_score(user_id: int, delta: int) -> float:
    """兼容旧调用：更新用户好感度（-100 到 100），返回新值"""
    return await _update_affinity_score(user_id, delta)


def _current_persona_id() -> str:
    try:
        from .persona_manager import get_active_persona_id

        return get_active_persona_id()
    except Exception:
        return "rin"


async def get_persona_affinity(user_id: int, persona_id: str | None = None) -> float:
    persona_id = str(persona_id or _current_persona_id()).strip().lower() or "rin"
    session = await get_session()
    try:
        row = (await session.execute(
            select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id == int(user_id),
            )
        )).scalar_one_or_none()
        if row is not None:
            return float(row.affinity_score or 0.0)
        # Migrate the legacy global score lazily into Rin's persona namespace.
        if persona_id == "rin":
            legacy = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == int(user_id))
            )).scalar_one_or_none()
            if legacy is not None:
                return float(legacy.affinity_score or 0.0)
        return 0.0
    finally:
        await session.close()


async def _update_affinity_score(
    user_id: int,
    delta: float,
    reason: str = "",
    *,
    persona_id: str | None = None,
) -> float:
    from .console_services import MEMORY_WRITE_LOCK
    async with MEMORY_WRITE_LOCK:
        return await _update_affinity_score_locked(user_id, delta, reason, persona_id=persona_id)


async def _update_affinity_score_locked(
    user_id: int,
    delta: float,
    reason: str = "",
    *,
    persona_id: str | None = None,
) -> float:
    """Update persona-scoped affinity with diminishing returns, returning the new score."""
    persona_id = str(persona_id or _current_persona_id()).strip().lower() or "rin"
    if not bool(_affinity_cfg.get("enabled", True)):
        return await get_persona_affinity(user_id, persona_id)
    try:
        raw_delta = float(delta)
    except (TypeError, ValueError):
        return await get_persona_affinity(user_id, persona_id)
    step = max(0.01, float(_affinity_cfg.get("step", 0.1)))
    raw_delta = round(round(raw_delta / step) * step, 2)
    session = await get_session()
    try:
        row = (await session.execute(
            select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id == int(user_id),
            )
        )).scalar_one_or_none()
        if row is None:
            legacy_score = 0.0
            if persona_id == "rin":
                legacy = (await session.execute(
                    select(UserAffinity).where(UserAffinity.user_id == int(user_id))
                )).scalar_one_or_none()
                if legacy is not None:
                    legacy_score = float(legacy.affinity_score or 0.0)
            effective_delta = curved_delta(legacy_score, raw_delta)
            new_score = max(-100.0, min(100.0, legacy_score + effective_delta))
            row = AgentPersonaAffinity(
                persona_id=persona_id,
                user_id=int(user_id),
                affinity_score=new_score,
                last_delta=effective_delta,
                last_reason=reason,
                updated_at=datetime.now(),
            )
            session.add(row)
        else:
            effective_delta = curved_delta(float(row.affinity_score or 0.0), raw_delta)
            new_score = max(-100.0, min(100.0, float(row.affinity_score or 0.0) + effective_delta))
            row.affinity_score = new_score
            row.last_delta = effective_delta
            row.last_reason = reason
            row.updated_at = datetime.now()
        if persona_id == "rin":
            legacy = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == int(user_id))
            )).scalar_one_or_none()
            if legacy is None:
                session.add(UserAffinity(
                    user_id=int(user_id),
                    affinity_score=new_score,
                    last_delta=float(effective_delta),
                    last_reason=reason,
                    updated_at=datetime.now(),
                ))
            else:
                legacy.affinity_score = new_score
                legacy.last_delta = float(effective_delta)
                legacy.last_reason = reason
                legacy.updated_at = datetime.now()
        await session.commit()
        return new_score
    finally:
        await session.close()


def _mute_probability(score: float) -> float:
    """好感度 → 禁言概率（>-80→0, -80→5%, -100→50%，线性）"""
    if score > -80:
        return 0.0
    return (abs(score) - 80) / 20 * 0.45 + 0.05


async def _get_and_increment_mute_count(user_id: int, group_id: int) -> int:
    """禁言次数 +1，返回新次数"""
    session = await get_session()
    try:
        row = (await session.execute(
            select(UserMuteRecord)
            .where(UserMuteRecord.user_id == user_id, UserMuteRecord.group_id == group_id)
        )).scalar_one_or_none()
        if row is None:
            session.add(UserMuteRecord(
                user_id=user_id, group_id=group_id,
                mute_count=1, last_muted_at=datetime.now()
            ))
            await session.commit()
            return 1
        else:
            row.mute_count += 1
            row.last_muted_at = datetime.now()
            await session.commit()
            return row.mute_count
    finally:
        await session.close()


async def _decrement_mute_count(user_id: int, group_id: int):
    """禁言失败时回滚计数"""
    session = await get_session()
    try:
        row = (await session.execute(
            select(UserMuteRecord)
            .where(UserMuteRecord.user_id == user_id, UserMuteRecord.group_id == group_id)
        )).scalar_one_or_none()
        if row and row.mute_count > 0:
            row.mute_count -= 1
            await session.commit()
    finally:
        await session.close()


async def _execute_affinity_mute(
    bot: Bot,
    group_id: int,
    user_id: int,
    score: float,
    source: str,
    reason: str,
) -> bool:
    """执行一次好感系统禁言，并维护本周禁言次数。"""
    mute_count = await _get_and_increment_mute_count(user_id, group_id)
    duration = mute_count * 60  # 第 N 次 = N 分钟
    try:
        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=duration)
        logger.info(
            f"[mute_control] {source}: user={user_id} group={group_id} "
            f"好感={score:.1f} 第{mute_count}次 时长={duration}s reason={reason}"
        )
        return True
    except Exception as e:
        logger.warning(f"[mute_control] 禁言失败: {e}")
        await _decrement_mute_count(user_id, group_id)
        return False


async def _confirm_affinity_mute(
    user_id: int,
    group_id: int,
    score: float,
    context_text: str,
    user_message: str | None = None,
    bot_reply: str | None = None,
) -> tuple[bool, str]:
    """让 DeepSeek 独立判断凛是否真的恼火到想让用户闭嘴。"""
    if not _DS_URL or not _DS_KEY:
        return False, "DeepSeek 未配置"

    prompt = (
        "你是远坂凛群聊机器人的禁言意图判定器。请根据群聊上下文和好感度，判断凛此刻是否真的恼火到想让目标用户闭嘴或滚蛋。\n"
        "凛是傲娇：吐槽、嘴硬、说“哈？”“笨蛋”“哼”不等于真的想禁言；看似生气也可能只是普通互动。\n"
        "只有目标用户持续骚扰、恶意辱骂、反复越界、故意挑衅，或上下文显示凛已经明显不想继续交流时，才允许 mute=true。\n"
        "高好感用户的普通玩笑更应判为 false；低好感也不能只因一句普通话就禁言。\n"
        "只输出 JSON，不要解释。格式：{\"mute\":false,\"reason\":\"只是普通玩笑\"}。reason 不超过40字。\n\n"
        f"目标用户QQ：{user_id}\n"
        f"群号：{group_id}\n"
        f"凛对该用户当前好感度：{score:.1f}/100\n\n"
        f"【目标用户本次发言】\n{user_message or '（无）'}\n\n"
        f"【凛刚才的回复】\n{bot_reply or '（无）'}\n\n"
        f"【最近群聊上下文】\n{context_text or '（无）'}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _DS_URL,
                headers={"Authorization": f"Bearer {_DS_KEY}"},
                json={
                    "model": _DS_MODEL,
                    "messages": [{"role": "user", "content": prompt + "\n/no_think"}],
                    "max_tokens": 120,
                    "temperature": 0.1,
                },
            )
            data = resp.json()
            msg = data["choices"][0]["message"]
            raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            obj = _extract_json_obj(raw)
            if not obj:
                logger.warning(f"[mute_control] DeepSeek禁言判断返回非 JSON: {raw[:120]}")
                return False, "判断返回非JSON"
            reason = re.sub(r"\s+", " ", str(obj.get("reason") or "")).strip()[:80]
            mute_value = obj.get("mute", False)
            if isinstance(mute_value, str):
                mute_value = mute_value.strip().lower() in {"true", "1", "yes", "是", "要"}
            return bool(mute_value), reason or "no reason"
    except Exception as e:
        logger.warning(f"[mute_control] DeepSeek禁言判断失败: {type(e).__name__}: {e}")
        return False, "判断调用失败"


def _make_extreme_transport() -> httpx.AsyncHTTPTransport:
    return (
        httpx.AsyncHTTPTransport(proxy=_EXTREME_PROXY, http2=False)
        if _EXTREME_PROXY
        else httpx.AsyncHTTPTransport(http2=False)
    )


def _extract_json_obj(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _clean_extreme_reply(reply: str) -> str:
    reply = re.sub(r"\s+", " ", (reply or "")).strip()
    reply = reply.replace("*", "").replace("`", "")
    if not reply:
        return _random_extreme_reply()
    return reply[:60]


def _random_extreme_reply() -> str:
    return random.choice(_EXTREME_RIN_REPLIES)


def _local_extreme_hit(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return any(p.search(normalized) for p in _EXTREME_HARD_PATTERNS)


async def _evaluate_extreme_content(user_message: str, nickname: str) -> tuple[bool, str]:
    """DeepSeek 前置判定极端内容，并生成一句凛式短回复。"""
    if not _EXTREME_ENABLED or not _EXTREME_API_URL or not _EXTREME_API_KEY or not user_message.strip():
        return False, ""

    system_prompt = (
        "你是QQ群机器人“凛”的前置风控判定器。请只判断【目标用户本次发言】是否达到极端阈值。\n"
        "极端阈值定义：明确、强攻击性、露骨且恶意的性骚扰；对群体/他人/机器人进行群体性性化辱骂；"
        "以羞辱、支配、物化为目的的大段露骨性描写或恶意骚扰。\n"
        "不要因为普通玩笑、轻度擦边、一般脏话、正常讨论、引用别人原文、创作设定讨论而触发。\n"
        "如果不确定，必须判定 extreme=false。\n"
        "命中时，生成一句符合当前场景的简短中文回复，口吻参考远坂凛：嫌弃、骄傲、果断、像在训斥失礼的人。"
        "回复可以带一点魔术制裁感，但不要中二过头；不要复述或改写用户的露骨原文，不要包含露骨性词汇，不要 Markdown，最多 30 个中文字符。\n"
        "只输出 JSON，不要解释。格式：{\"extreme\": true, \"reply\": \"这种话也说得出口？退场冷静。\"}"
    )
    user_prompt = (
        f"目标用户昵称：{nickname}\n"
        f"目标用户本次发言：{user_message}"
    )

    payload = {
        "model": _EXTREME_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 160,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }

    async def _post(payload_obj: dict) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=_EXTREME_TIMEOUT,
            mounts={"https://": _make_extreme_transport()},
        ) as client:
            return await client.post(
                _EXTREME_API_URL,
                headers={"Authorization": f"Bearer {_EXTREME_API_KEY}"},
                json=payload_obj,
            )

    resp = await _post(payload)
    if resp.status_code == 400 and "response_format" in resp.text:
        logger.warning("[mute_control] 当前极端检测接口不支持 response_format，降级为普通 JSON 提示")
        payload.pop("response_format", None)
        resp = await _post(payload)

    if resp.status_code != 200:
        raise RuntimeError(f"极端检测模型 HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"极端检测模型 API error: {data['error']}")

    msg = data.get("choices", [{}])[0].get("message", {})
    raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not raw:
        logger.warning(f"[mute_control] 极端检测模型空回复: {str(data)[:500]}")

    obj = _extract_json_obj(raw)
    if not obj:
        raise ValueError(f"极端检测模型返回非 JSON: {raw[:120]}")

    extreme = bool(obj.get("extreme"))
    reply = _clean_extreme_reply(str(obj.get("reply") or ""))
    return extreme, reply


async def handle_extreme_at_bot(
    bot: Bot,
    group_id: int,
    user_id: int,
    nickname: str,
    user_message: str | None,
    message_id: int,
) -> bool:
    """
    @bot 前置极端内容拦截。
    返回 True 表示已经处理，应阻止后续正常聊天与普通好感评分。
    """
    text = (user_message or "").strip()
    if not text:
        return False

    try:
        extreme, reply = await _evaluate_extreme_content(text, nickname)
    except Exception as e:
        if _local_extreme_hit(text):
            logger.warning(
                f"[mute_control] 极端内容检测失败但本地硬规则命中，执行拦截: "
                f"{type(e).__name__}: {e}"
            )
            extreme = True
            reply = _random_extreme_reply()
        else:
            logger.warning(f"[mute_control] 极端内容检测失败，放行: {type(e).__name__}: {e}")
            return False

    if not extreme:
        return False

    try:
        await bot.set_group_ban(
            group_id=group_id,
            user_id=user_id,
            duration=_EXTREME_MUTE_DURATION,
        )
    except Exception as e:
        logger.warning(
            f"[mute_control] 极端内容禁言失败: user={user_id} group={group_id} "
            f"duration={_EXTREME_MUTE_DURATION}s err={e}"
        )
        return False

    try:
        await bot.send_group_msg(
            group_id=group_id,
            message=Message(MessageSegment.reply(message_id)) + reply,
        )
    except Exception as e:
        logger.warning(f"[mute_control] 极端内容回复发送失败: {e}")

    logger.info(
        f"[mute_control] 极端内容前置禁言: user={user_id}({nickname}) "
        f"group={group_id} duration={_EXTREME_MUTE_DURATION}s"
    )
    return True


# ---------- 对外入口（供 ai_chat.py 调用）----------

async def on_at_bot(bot: Bot, group_id: int, user_id: int, nickname: str, user_message: str | None = None, bot_reply: str | None = None):
    """@bot 触发后台好感度评估 — 由 ai_chat.py 通过 asyncio.create_task 调用"""
    try:
        context_text = await _get_user_context_text(user_id, group_id)
        delta, reason = await _evaluate_risk_delta(context_text, user_message=user_message, bot_reply=bot_reply)
        new_score = await _update_affinity_score(user_id, delta, reason or "auto")
        logger.debug(
            f"[mute_control] 好感评分: user={user_id}({nickname}) "
            f"delta={delta:+d} → {new_score:.1f} reason={reason}"
        )

        # 低好感随机直禁不等待模型，命中后立即执行。
        prob = _mute_probability(new_score)
        direct_hit = False
        if prob > 0:
            roll = random.random()
            direct_hit = roll <= prob
            logger.debug(
                f"[mute_control] 低好感随机直禁: user={user_id}({nickname}) "
                f"好感={new_score:.1f} roll={roll:.4f} prob={prob:.4f} hit={direct_hit}"
            )

        if direct_hit:
            await _execute_affinity_mute(
                bot,
                group_id,
                user_id,
                new_score,
                source="低好感随机直禁",
                reason=f"随机命中（概率 {prob * 100:.1f}%）",
            )

        # DeepSeek 禁言意图判断覆盖完整好感区间，与低好感随机直禁互相独立。
        mute_ok, mute_reason = await _confirm_affinity_mute(
            user_id=user_id,
            group_id=group_id,
            score=new_score,
            context_text=context_text,
            user_message=user_message,
            bot_reply=bot_reply,
        )
        logger.debug(
            f"[mute_control] DeepSeek禁言判断: user={user_id}({nickname}) "
            f"好感={new_score:.1f} mute={mute_ok} reason={mute_reason}"
        )

        # 同一条消息最多禁言一次；随机直禁已执行时忽略模型的重复决定。
        if not direct_hit and mute_ok:
            await _execute_affinity_mute(
                bot,
                group_id,
                user_id,
                new_score,
                source="DeepSeek意图禁言",
                reason=mute_reason,
            )
    except Exception as e:
        logger.debug(f"[mute_control] on_at_bot 异常: {e}")


# ---------- 定时任务：每周一 00:00 禁言次数归零 ----------

@scheduler.scheduled_job("cron", day_of_week="mon", hour=0, minute=0, id="mute_weekly_reset")
async def _weekly_reset():
    session = await get_session()
    try:
        from sqlalchemy import update as _sa_update
        await session.execute(_sa_update(UserMuteRecord).values(mute_count=0))
        await session.commit()
        logger.info("[mute_control] 每周重置完成：禁言次数归零")
    except Exception as e:
        logger.warning(f"[mute_control] 每周重置失败: {e}")
    finally:
        await session.close()


# ---------- 趣味功能：@bot + "禁言我" → 直接禁言 1 分钟（priority=4，先于 ai_chat） ----------

def _mute_me_rule() -> Rule:
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        from .console_runtime import ready
        if ready():
            return False
        if int(event.group_id) in _AGENT_GROUPS:
            return False
        has_at = any(
            seg.type == "at" and str(seg.data.get("qq")) == str(bot.self_id)
            for seg in event.message
        ) or event.to_me  # 兼容昵称触发（无 AT 段时 to_me 也为 True）
        return has_at and "禁言我" in event.message.extract_plain_text()
    return Rule(_rule)


mute_me = on_message(rule=_mute_me_rule(), priority=4, block=True)


@mute_me.handle()
async def handle_mute_me(bot: Bot, event: GroupMessageEvent):
    user_id  = event.user_id
    group_id = event.group_id
    try:
        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=60)
        await mute_me.send(Message(MessageSegment.reply(event.message_id)) + "哼，满足你！")
    except Exception as e:
        logger.warning(f"[mute_control] 禁言我 失败: {e}")
        await mute_me.send(Message(MessageSegment.reply(event.message_id)) + "满足不了你那无理的要求！")


# ---------- 管理员指令 ----------

def _is_admin(event: GroupMessageEvent) -> bool:
    return int(event.user_id) in _ADMINS


def _can_release(event: GroupMessageEvent) -> bool:
    uid = int(event.user_id)
    return uid in _RELEASE_USERS or uid in _ADMINS


def _parse_qq(text: str) -> int | None:
    """解析 QQ 号，支持纯数字格式"""
    text = text.strip()
    return int(text) if text.isdigit() else None


def _parse_qq_from_event(event: GroupMessageEvent) -> int | None:
    """从消息中解析 QQ 号：优先 AT segment，否则从所有分词中找纯数字（5-12位）"""
    for seg in event.message:
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq.isdigit():
                return int(qq)
    plain = event.message.extract_plain_text()
    for part in plain.split():
        if part.isdigit() and 5 <= len(part) <= 12:
            return int(part)
    return None


# #个人好感度 <QQ>
personal_affinity_cmd = on_command("#个人好感度", priority=3, block=True)

@personal_affinity_cmd.handle()
async def handle_risk_query(event: GroupMessageEvent):
    if not _is_admin(event):
        return
    target_id = _parse_qq_from_event(event)
    if target_id is None:
        await personal_affinity_cmd.finish("用法：#个人好感度 @某人  或  #个人好感度 QQ号")
    persona_id = _current_persona_id()
    session = await get_session()
    try:
        row = (await session.execute(
            select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id == target_id,
            )
        )).scalar_one_or_none()
        if row is None and persona_id == "rin":
            row = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == target_id)
            )).scalar_one_or_none()
        mute_rows = (await session.execute(
            select(UserMuteRecord).where(UserMuteRecord.user_id == target_id)
        )).scalars().all()
    finally:
        await session.close()

    score = float(row.affinity_score or 0.0) if row is not None else 0.0
    prob = _mute_probability(score)
    prob_str = f"{prob * 100:.1f}%" if prob > 0 else "无"
    total_mutes = sum(r.mute_count for r in mute_rows)
    updated_at = row.updated_at.strftime('%m-%d %H:%M') if row is not None else "暂无互动记录"
    last_delta = float(row.last_delta or 0.0) if row is not None else 0.0
    await personal_affinity_cmd.finish(
        f"QQ {target_id}\n"
        f"好感度：{score:.1f} / 100\n"
        f"关系阶段：{relationship_stage(score)}\n"
        f"负好感禁言概率：{prob_str}\n"
        f"本周禁言次数（各群合计）：{total_mutes} 次\n"
        f"最近变化：{last_delta:+.1f}\n"
        f"更新时间：{updated_at}"
    )


# #重置好感度 <QQ>
risk_reset = on_command("#重置好感度", priority=3, block=True)

@risk_reset.handle()
async def handle_risk_reset(event: GroupMessageEvent):
    if not _is_admin(event):
        return
    target_id = _parse_qq_from_event(event)
    if target_id is None:
        await risk_reset.finish("用法：#重置好感度 @某人  或  #重置好感度 QQ号")
    persona_id = _current_persona_id()
    session = await get_session()
    try:
        row = (await session.execute(
            select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id == target_id,
            )
        )).scalar_one_or_none()
        if row is None and persona_id == "rin":
            row = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == target_id)
            )).scalar_one_or_none()
        if row:
            row.affinity_score = 0.0
            row.last_delta = 0.0
            row.last_reason = "reset"
            row.updated_at = datetime.now()
            # Rin keeps the pre-persona table as a compatibility mirror.
            # Reset both sides so a later migration cannot resurrect an old
            # non-zero score into the ranking.
            if persona_id == "rin" and not isinstance(row, UserAffinity):
                legacy = (await session.execute(
                    select(UserAffinity).where(UserAffinity.user_id == target_id)
                )).scalar_one_or_none()
                if legacy is None:
                    session.add(UserAffinity(
                        user_id=target_id,
                        affinity_score=0.0,
                        last_delta=0.0,
                        last_reason="reset",
                        updated_at=datetime.now(),
                    ))
                else:
                    legacy.affinity_score = 0.0
                    legacy.last_delta = 0.0
                    legacy.last_reason = "reset"
                    legacy.updated_at = datetime.now()
            await session.commit()
            await risk_reset.finish(f"已将 QQ {target_id} 的好感度重置为 0")
        else:
            await risk_reset.finish(f"QQ {target_id} 暂无好感记录")
    finally:
        await session.close()


# #设好感度 <QQ> <数值>
risk_set = on_command("#设好感度", priority=3, block=True)

@risk_set.handle()
async def handle_risk_set(event: GroupMessageEvent):
    if not _is_admin(event):
        return
    # 用统一的 QQ 解析（AT 优先，否则找5-12位纯数字 token）
    target_id = _parse_qq_from_event(event)
    # 找分值：plain text 中第一个不等于 target_id 的数字 token
    score_str: str | None = None
    for part in event.message.extract_plain_text().split():
        if not part.replace('.', '', 1).lstrip('-').isdigit():
            continue
        try:
            maybe = float(part)
        except ValueError:
            continue
        # 跳过 target_id 本身
        if target_id is not None and part.isdigit() and int(part) == target_id:
            continue
        score_str = part
        break
    if target_id is None or score_str is None:
        await risk_set.finish("用法：#设好感度 @某人 80\n    或：#设好感度 123456 -85")
    try:
        new_score = max(-100.0, min(100.0, float(score_str)))
    except ValueError:
        await risk_set.finish("数值格式错误，请输入 -100 到 100 的数字")
    persona_id = _current_persona_id()
    session = await get_session()
    try:
        row = (await session.execute(
            select(AgentPersonaAffinity).where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id == target_id,
            )
        )).scalar_one_or_none()
        if row is None:
            session.add(AgentPersonaAffinity(
                persona_id=persona_id,
                user_id=target_id,
                affinity_score=new_score,
                last_delta=0,
                last_reason="manual set",
                updated_at=datetime.now(),
            ))
        else:
            row.affinity_score = new_score
            row.last_delta = 0
            row.last_reason = "manual set"
            row.updated_at = datetime.now()
        if persona_id == "rin":
            legacy = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == target_id)
            )).scalar_one_or_none()
            if legacy is None:
                session.add(UserAffinity(
                    user_id=target_id,
                    affinity_score=new_score,
                    last_delta=0,
                    last_reason="manual set",
                    updated_at=datetime.now(),
                ))
            else:
                legacy.affinity_score = new_score
                legacy.last_delta = 0
                legacy.last_reason = "manual set"
                legacy.updated_at = datetime.now()
        await session.commit()
    finally:
        await session.close()
    prob = _mute_probability(new_score)
    prob_str = f"{prob * 100:.1f}%" if prob > 0 else "无"
    await risk_set.finish(
        f"已将 QQ {target_id} 好感度设为 {new_score:.1f}\n负好感禁言概率：{prob_str}"
    )


# #放人 <QQ>
release_cmd = on_command("#放人", priority=3, block=True)

@release_cmd.handle()
async def handle_release(bot: Bot, event: GroupMessageEvent):
    if not _can_release(event):
        return
    target_id = _parse_qq_from_event(event)
    if target_id is None:
        await release_cmd.finish("用法：#放人 @某人  或  #放人 QQ号")
    group_id  = event.group_id
    try:
        await bot.set_group_ban(group_id=group_id, user_id=target_id, duration=0)
    except Exception as e:
        logger.warning(f"[mute_control] 解除禁言失败: {e}")


# ---------- 好感度群榜 ----------

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]

_EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/local/share/fonts/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]
_SYMBOL_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
]
_emoji_font_warning_logged = False


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=32)
def _load_emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    """加载彩色 emoji 字体；兼容 Noto Color Emoji 的固定像素字号。"""
    global _emoji_font_warning_logged
    fallback_sizes = (size, 109, 128, 96, 72, 64, 32, 20, 16)
    for path in _EMOJI_FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        for candidate_size in dict.fromkeys(fallback_sizes):
            try:
                return ImageFont.truetype(path, candidate_size)
            except (OSError, IOError):
                continue
    if not _emoji_font_warning_logged:
        logger.warning(
            "[mute_control] 未找到 emoji 字体；请在 Ubuntu 安装 fonts-noto-color-emoji 后重启 bot"
        )
        _emoji_font_warning_logged = True
    return None


@lru_cache(maxsize=32)
def _load_symbol_font(size: int) -> ImageFont.FreeTypeFont | None:
    """加载颜文字、希腊扩展字母和组合符号所需的普通符号字体。"""
    for path in _SYMBOL_FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return None


def _is_emoji_base(char: str) -> bool:
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2300 <= code <= 0x23FF
        or 0x2B00 <= code <= 0x2BFF
        or code in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def _text_clusters(value: str) -> list[str]:
    """按 emoji 所需的最小字素簇拆分文本，不拆散肤色、国旗和 ZWJ 序列。"""
    clusters: list[str] = []
    index = 0
    while index < len(value):
        cluster = value[index]
        index += 1

        code = ord(cluster)
        if 0x1F1E6 <= code <= 0x1F1FF and index < len(value):
            next_code = ord(value[index])
            if 0x1F1E6 <= next_code <= 0x1F1FF:
                cluster += value[index]
                index += 1

        while index < len(value):
            next_code = ord(value[index])
            next_category = unicodedata.category(value[index])
            if (
                next_category in ("Mn", "Mc", "Me")
                or next_code in (0xFE0E, 0xFE0F, 0x20E3)
                or 0x1F3FB <= next_code <= 0x1F3FF
            ):
                cluster += value[index]
                index += 1
                continue
            if next_code == 0x200D and index + 1 < len(value):
                cluster += value[index:index + 2]
                index += 2
                continue
            break
        clusters.append(cluster)
    return clusters


def _is_emoji_cluster(cluster: str) -> bool:
    return any(_is_emoji_base(char) for char in cluster)


def _normalize_symbol_cluster(cluster: str) -> str:
    """把少数颜文字扩展字符映射为常见字体都能绘制的等价组合。"""
    replacements = {
        "\uff65": "\u00b7",       # HALFWIDTH KATAKANA MIDDLE DOT -> middle dot
        "\u1dc4": "\u0304\u0301",  # COMBINING MACRON-ACUTE
        "\u1dc5": "\u0300\u0304",  # COMBINING GRAVE-MACRON
    }
    return "".join(replacements.get(char, char) for char in cluster)


def _needs_symbol_font(cluster: str) -> bool:
    """判断是否应使用符号字体，覆盖颜文字常用的扩展字符。"""
    normalized = _normalize_symbol_cluster(cluster)
    for char in normalized:
        code = ord(char)
        category = unicodedata.category(char)
        # 扩展组合附加符号（例如 ᷄/᷅）经归一化后会变成 ̄/́。
        # 中文字体经常没有这些字形，整簇交给 DejaVu/Symbol 字体才能保留颜文字。
        if category in ("Mn", "Mc", "Me") and (
            0x0300 <= code <= 0x036F
            or 0x1AB0 <= code <= 0x1AFF
            or 0x1DC0 <= code <= 0x1DFF
            or 0x20D0 <= code <= 0x20FF
        ):
            return True
        if category in ("Mn", "Mc", "Me"):
            continue
        if 0x0370 <= code <= 0x03FF or 0x1F00 <= code <= 0x1FFF:
            return True
        if 0x2000 <= code <= 0x206F or 0x20A0 <= code <= 0x20CF:
            return True
        if 0x2100 <= code <= 0x214F or 0x2190 <= code <= 0x22FF:
            return True
    return False


def _font_for_cluster(
    cluster: str,
    primary_font: ImageFont.ImageFont,
    display_size: int,
) -> ImageFont.ImageFont:
    if _needs_symbol_font(cluster):
        return _load_symbol_font(display_size) or primary_font
    return primary_font


@lru_cache(maxsize=1024)
def _render_emoji_cluster(cluster: str, display_size: int) -> Image.Image | None:
    """把一个 emoji 字素簇栅格化后缩放到与昵称文字匹配的高度。"""
    emoji_font = _load_emoji_font(display_size)
    if emoji_font is None:
        return None

    native_size = max(16, int(getattr(emoji_font, "size", display_size)))
    canvas_size = max(96, native_size * 4)
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    emoji_draw = ImageDraw.Draw(canvas)
    try:
        emoji_draw.text(
            (canvas_size // 4, canvas_size // 4),
            cluster,
            font=emoji_font,
            fill=(45, 76, 94, 255),
            embedded_color=True,
        )
    except (OSError, ValueError):
        return None

    bbox = canvas.getbbox()
    if bbox is None:
        return None
    glyph = canvas.crop(bbox)
    target_height = max(12, display_size + 2)
    scale = target_height / glyph.height
    target_width = max(1, round(glyph.width * scale))
    return glyph.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _rich_text_width(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.ImageFont) -> float:
    width = 0.0
    display_size = int(getattr(font, "size", 15))
    for cluster in _text_clusters(value):
        glyph = _render_emoji_cluster(cluster, display_size) if _is_emoji_cluster(cluster) else None
        cluster_font = _font_for_cluster(cluster, font, display_size)
        display_cluster = cluster if glyph is not None else _normalize_symbol_cluster(cluster)
        width += glyph.width + 2 if glyph is not None else draw.textlength(display_cluster, font=cluster_font)
    return width


def _draw_rich_text(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    value: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> float:
    """以 CJK 字体和 emoji 字体分段绘制文本，返回绘制后的横坐标。"""
    x, y = position
    display_size = int(getattr(font, "size", 15))
    for cluster in _text_clusters(value):
        glyph = _render_emoji_cluster(cluster, display_size) if _is_emoji_cluster(cluster) else None
        if glyph is not None:
            image.alpha_composite(glyph, (round(x), round(y + 1)))
            x += glyph.width + 2
        else:
            cluster_font = _font_for_cluster(cluster, font, display_size)
            display_cluster = _normalize_symbol_cluster(cluster)
            draw.text((x, y), display_cluster, font=cluster_font, fill=fill)
            x += draw.textlength(display_cluster, font=cluster_font)
    return x


def _rank_column_count(entry_count: int) -> int:
    if entry_count <= 12:
        return 1
    if entry_count <= 30:
        return 2
    if entry_count <= 60:
        return 3
    return 4


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """按像素宽度截断昵称，给 QQ 后四位和分值留出稳定空间。"""
    if max_width <= 0:
        return ""
    if _rich_text_width(draw, value, font) <= max_width:
        return value
    ellipsis = "..."
    ellipsis_width = _rich_text_width(draw, ellipsis, font)
    clusters: list[str] = []
    for cluster in _text_clusters(value):
        candidate = "".join(clusters) + cluster
        if _rich_text_width(draw, candidate, font) + ellipsis_width > max_width:
            break
        clusters.append(cluster)
    return "".join(clusters) + ellipsis if clusters else ""


def _draw_heart(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    size: float,
    fill: tuple[int, int, int, int],
) -> None:
    """绘制矢量心形，避免服务器缺少 emoji 字体。"""
    cx, cy = center
    scale = size / 34
    points = []
    for i in range(101):
        t = math.tau * i / 100
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        points.append((cx + x * scale, cy - y * scale))
    draw.polygon(points, fill=fill)


def _draw_affinity_rank_image(
    entries: list[tuple[str, int, float]],
    group_name: str,
    persona_name: str,
    highlight_user_id: int | None = None,
) -> Image.Image:
    """绘制淡天蓝色多列好感度群榜，entries 按排行榜顺序传入。"""
    columns = _rank_column_count(len(entries))
    rows_per_column = max(1, math.ceil(len(entries) / columns))

    outer = 14
    padding_x = 24
    column_width = 260
    column_gap = 10
    header_h = 100
    row_h = 42
    footer_h = 42
    content_width = columns * column_width + (columns - 1) * column_gap
    width = max(520, padding_x * 2 + content_width + outer * 2)
    if columns == 1:
        column_width = width - outer * 2 - padding_x * 2
    height = outer * 2 + header_h + rows_per_column * row_h + footer_h

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        (outer + 3, outer + 3, width - outer + 3, height - outer + 3),
        radius=24,
        fill=(70, 139, 190, 38),
    )
    draw.rounded_rectangle(
        (outer, outer - 3, width - outer, height - outer - 3),
        radius=24,
        fill=(242, 249, 253, 255),
        outline=(168, 211, 237, 255),
        width=2,
    )

    font_title = _load_font(29)
    font_subtitle = _load_font(13)
    font_rank = _load_font(14)
    font_name = _load_font(15)
    font_qq = _load_font(11)
    font_score = _load_font(15)
    font_foot = _load_font(11)

    icon_x = outer + padding_x
    icon_y = outer + 18
    draw.rounded_rectangle(
        (icon_x, icon_y, icon_x + 52, icon_y + 52),
        radius=15,
        fill=(100, 181, 229, 255),
    )
    _draw_heart(draw, (icon_x + 26, icon_y + 27), 27, (255, 255, 255, 255))

    title_x = icon_x + 68
    draw.text((title_x, icon_y - 1), "好感度", font=font_title, fill=(45, 119, 169, 255))
    subtitle = f"{group_name}  ·  {persona_name}  ·  上榜 {len(entries)} 人"
    subtitle = _truncate_text(draw, subtitle, font_subtitle, width - title_x - outer - padding_x)
    _draw_rich_text(
        img,
        draw,
        (title_x, icon_y + 36),
        subtitle,
        font_subtitle,
        (104, 151, 181, 255),
    )

    content_x = outer + padding_x
    content_y = outer + header_h
    rank_colors = (
        (216, 157, 35, 255),
        (145, 150, 165, 255),
        (180, 105, 73, 255),
    )

    for index, (name, uid, score) in enumerate(entries):
        column = index // rows_per_column
        row = index % rows_per_column
        x = content_x + column * (column_width + column_gap)
        y = content_y + row * row_h
        is_caller = highlight_user_id is not None and int(uid) == int(highlight_user_id)
        draw.rounded_rectangle(
            (x, y + 2, x + column_width, y + row_h - 4),
            radius=10,
            fill=(224, 246, 215, 255) if is_caller else (250, 253, 255, 245),
            outline=(111, 181, 91, 255) if is_caller else (205, 229, 244, 255),
            width=2 if is_caller else 1,
        )

        rank = index + 1
        rank_color = rank_colors[index] if index < 3 else (103, 151, 181, 255)
        if is_caller:
            rank_color = (63, 130, 61, 255)
        rank_text = str(rank)
        rank_width = draw.textlength(rank_text, font=font_rank)
        draw.text((x + 12, y + 10), rank_text, font=font_rank, fill=rank_color)

        score_text = f"{score:.1f}"
        score_width = draw.textlength(score_text, font=font_score)
        score_x = x + column_width - 12 - score_width
        if is_caller:
            score_color = (38, 119, 44, 255)
        elif score > 0:
            score_color = (38, 132, 194, 255)
        elif score < 0:
            score_color = (214, 73, 83, 255)
        else:
            score_color = (117, 137, 150, 255)
        draw.text((score_x, y + 9), score_text, font=font_score, fill=score_color)

        qq_text = str(uid)[-4:]
        qq_width = draw.textlength(qq_text, font=font_qq)
        name_x = x + 12 + max(24, rank_width + 10)
        available_width = max(0, int(score_x - name_x - qq_width - 16))
        shown_name = _truncate_text(draw, name or str(uid), font_name, available_width)
        name_end_x = _draw_rich_text(
            img,
            draw,
            (name_x, y + 9),
            shown_name,
            font_name,
            (43, 93, 46, 255) if is_caller else (54, 79, 95, 255),
        )
        draw.text(
            (name_end_x + 7, y + 13),
            qq_text,
            font=font_qq,
            fill=(91, 143, 86, 255) if is_caller else (142, 176, 197, 255),
        )

    foot_y = content_y + rows_per_column * row_h + 5
    foot_text = f"生成时间  {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}"
    draw.text((content_x, foot_y), foot_text, font=font_foot, fill=(133, 170, 193, 255))

    return img


rank_cmd = on_command("#好感度", aliases={"#查询好感度"}, priority=3, block=True)
all_rank_cmd = on_command("#查询全部好感度", priority=3, block=True)


async def render_affinity_rank(
    bot: Bot,
    event: GroupMessageEvent,
    *,
    target_group: int | None = None,
    include_hidden_users: bool = False,
) -> str | MessageSegment:
    if target_group is None:
        # 只有真实排行榜指令解析文字参数；Agent 工具直接传入当前群号。
        plain = event.message.extract_plain_text().strip()
        arg_text = re.sub(
            r"^#(?:查询全部好感度|查询好感度|好感度)\s*",
            "",
            plain,
            count=1,
        ).strip()
        if arg_text:
            if not arg_text.isdigit() or len(arg_text) < 5:
                return "用法：#好感度\n管理员可用：#好感度 群号"
            target_group = int(arg_text)
            if target_group != event.group_id and not _is_admin(event):
                return "只能查询当前群的好感度。"
        else:
            target_group = int(event.group_id)
    else:
        target_group = int(target_group)

    # 好感度是按 QQ 全局保存的；排行榜应按当前群成员筛选，而不是按
    # group_messages 反推成员。未启用群聊记录的群也可能存在已评分用户。
    member_names: dict[int, str] = {}
    group_uids: set[int] = set()
    try:
        members = await bot.get_group_member_list(group_id=target_group, no_cache=False)
        for member in members:
            uid = int(member.get("user_id", 0))
            if uid:
                group_uids.add(uid)
                member_names[uid] = member.get("card") or member.get("nickname") or str(uid)
    except Exception as e:
        logger.warning(f"[mute_control] 获取群 {target_group} 成员列表失败: {e}")

    persona_id = _current_persona_id()
    try:
        from .persona_manager import get_active_persona_name

        persona_name = get_active_persona_name()
    except Exception:
        persona_name = persona_id
    session = await get_session()
    try:
        # 成员列表接口失败时，回退到历史消息中的用户，保持排行榜可用。
        if not group_uids:
            group_uids = set((await session.execute(
                select(distinct(GroupMessage.user_id))
                .where(GroupMessage.group_id == target_group, GroupMessage.is_bot == False)
            )).scalars().all())

        if not group_uids:
            return "该群暂无好感记录~"

        rows = (await session.execute(
            select(AgentPersonaAffinity)
            .where(
                AgentPersonaAffinity.persona_id == persona_id,
                AgentPersonaAffinity.user_id.in_(group_uids),
            )
        )).scalars().all()
        score_by_user = {
            int(row.user_id): float(row.affinity_score or 0.0)
            for row in rows
        }
        if persona_id == "rin":
            # Compatibility during the first startup after the persona-scoped
            # migration. Do not let one new row hide all old scores.
            legacy_rows = (await session.execute(
                select(UserAffinity)
                .where(UserAffinity.user_id.in_(group_uids))
            )).scalars().all()
            for row in legacy_rows:
                score_by_user.setdefault(int(row.user_id), float(row.affinity_score or 0.0))

        relation_rows = (await session.execute(
            select(AgentPersonaRelationshipState).where(
                AgentPersonaRelationshipState.persona_id == persona_id,
                AgentPersonaRelationshipState.group_id == target_group,
                AgentPersonaRelationshipState.user_id.in_(group_uids),
            )
        )).scalars().all()
        if persona_id == "rin":
            relation_rows = [
                *relation_rows,
                *(await session.execute(
                    select(AgentRelationshipState).where(
                        AgentRelationshipState.group_id == target_group,
                        AgentRelationshipState.user_id.in_(group_uids),
                    )
                )).scalars().all(),
            ]
    finally:
        await session.close()

    # The relationship tables are historical per-group mirrors. They fill a
    # migration gap only when no authoritative persona/global score exists.
    # A real 0 score remains 0 and is intentionally not shown on the ranking.
    for row in relation_rows:
        user_id = int(row.user_id)
        score_by_user.setdefault(user_id, float(row.affinity_score or 0.0))

    try:
        group_info = await bot.get_group_info(group_id=target_group, no_cache=False)
        group_name = group_info.get("group_name") or f"群 {target_group}"
    except Exception as e:
        logger.warning(f"[mute_control] 获取群 {target_group} 信息失败: {e}")
        group_name = f"群 {target_group}"

    entries = [
        (member_names.get(user_id, str(user_id)), user_id, score)
        for user_id, score in score_by_user.items()
        if score != 0
        and (include_hidden_users or user_id != _HIDDEN_AFFINITY_USER_ID)
    ]
    entries.sort(key=lambda item: (-item[2], item[1]))
    if not entries:
        return "目前没有人上榜~"

    caller_user_id = int(getattr(event, "user_id", 0) or 0)
    img = _draw_affinity_rank_image(
        entries,
        group_name,
        persona_name,
        highlight_user_id=caller_user_id or None,
    )
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode()
    return MessageSegment.image(f"base64://{img_b64}")


@rank_cmd.handle()
async def handle_rank(bot: Bot, event: GroupMessageEvent):
    # Keep the explicit command and Agent tool on the same idempotency path.
    # This also protects against a duplicated OneBot delivery of one message.
    from .agent_tools import claim_affinity_query, finish_affinity_query

    group_id = int(event.group_id)
    source_message_id = int(event.message_id)
    if not await claim_affinity_query(group_id, source_message_id):
        logger.debug(
            f"[agent_tool] name=get_affinity status=duplicate "
            f"group={group_id} source_message_id={source_message_id}"
        )
        return
    try:
        result = await render_affinity_rank(bot, event)
    finally:
        await finish_affinity_query(group_id, source_message_id)
    try:
        from .agent_runtime import agent_group_enabled
        from .agent_metrics import record_agent_messages

        if agent_group_enabled(event.group_id):
            await record_agent_messages(event.group_id, 1)
    except Exception:
        pass
    await rank_cmd.finish(result)


@all_rank_cmd.handle()
async def handle_all_rank(bot: Bot, event: GroupMessageEvent):
    # This is an owner/admin diagnostic view. A non-admin request is
    # deliberately silent while still consuming the command event.
    if not _is_admin(event):
        return

    from .agent_tools import claim_affinity_query, finish_affinity_query

    group_id = int(event.group_id)
    source_message_id = int(event.message_id)
    if not await claim_affinity_query(group_id, source_message_id):
        logger.debug(
            f"[agent_tool] name=get_affinity_all status=duplicate "
            f"group={group_id} source_message_id={source_message_id}"
        )
        return
    try:
        result = await render_affinity_rank(
            bot,
            event,
            include_hidden_users=True,
        )
    finally:
        await finish_affinity_query(group_id, source_message_id)
    try:
        from .agent_runtime import agent_group_enabled
        from .agent_metrics import record_agent_messages

        if agent_group_enabled(event.group_id):
            await record_agent_messages(event.group_id, 1)
    except Exception:
        pass
    await all_rank_cmd.finish(result)
