"""AI 聊天插件 — @机器人 触发对话

对话历史存储于 MySQL，按 user_id 隔离上下文。
被@时注入最近75条群聊记录（含图片URL、bot自己的发言） + 用户40条对话历史。
支持处理引用消息、合并转发、上下文内图片等多模态内容。
"""

import asyncio
import base64
import json
import re
import random
import pathlib
import time
from datetime import datetime
from uuid import uuid4

import httpx
import yaml
from minio import Minio
from nonebot import on_message, on_command, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, Message
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot.log import logger
from sqlalchemy import select, func, delete

from .db import (
    Base,
    engine,
    get_session,
    ChatHistory,
    UserMemory,
    GroupMessage,
    UserAffinity,
    prune_group_messages,
)
from .meme_collector import maybe_send_reply_meme
from .responses_api import call_responses
from .agent_requests import REQUEST_CONTEXT
from .chat_coordination import (
    format_reply_context,
    mark_group_trigger,
    resolve_reply_context,
    send_group_reply_parts,
)
from .provider_output import (
    extract_json_object,
    looks_like_replies_payload,
    recover_malformed_replies,
    strip_tool_activity,
)
from .persona_manager import get_persona_content, get_active_persona_id, get_active_persona_name

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
_antigravity_cfg = ai_cfg.get("antigravity", {})
ANTIGRAVITY_ENABLED = bool(_antigravity_cfg.get("enabled", False))
ANTIGRAVITY_PROTOCOL = (_antigravity_cfg.get("protocol", "openai") or "openai").lower()
ANTIGRAVITY_URL = _antigravity_cfg.get("api_url", "")
ANTIGRAVITY_KEY = _antigravity_cfg.get("api_key", "")
ANTIGRAVITY_MODEL = _antigravity_cfg.get("model", "gemini-3.8-flash-high")
ANTIGRAVITY_TIMEOUT = _antigravity_cfg.get("timeout", 90)
ANTIGRAVITY_PROXY = _antigravity_cfg.get("proxy", "") or None
ANTIGRAVITY_ENABLE_SEARCH = bool(_antigravity_cfg.get("enable_search", False))
API_URL = ai_cfg["api_url"]
API_KEY = ai_cfg["api_key"]
MODEL = ai_cfg["model"]
PRIMARY_PROTOCOL = ai_cfg.get("protocol", "chat_completions")
PRIMARY_TIMEOUT = ai_cfg.get("primary_timeout", 40)
PRIMARY_PROXY = ai_cfg.get("proxy", "") or None  # 主模型 HTTP 代理（Google 需要香港代理）
MAX_HISTORY = ai_cfg.get("max_history", 100)  # 用户对话保留100条
USER_CONTEXT_LIMIT = 40   # @回复时取用户最近40条（约20轮对话）
GROUP_CONTEXT_LIMIT = 75   # @回复时取群聊最近75条
ENABLED_GROUPS: set[int] = {
    int(g) for g in ai_cfg.get("group_chat", {}).get("enabled_groups", [])
}
ENABLED_GROUPS.update(
    int(g) for g in ((config.get("agent") or {}).get("active_groups", []) or [])
)
LOCAL_DISABLED_GROUPS: set[int] = {
    int(g) for g in config.get("group_mode", {}).get("local_disabled_groups", [])
}
AT_REPLY_QUOTE_RATE = max(
    0.0,
    min(1.0, float(ai_cfg.get("group_chat", {}).get("at_reply_quote_rate", 0.3))),
)


def _is_group_context_enabled(group_id: int) -> bool:
    group_id = int(group_id)
    return group_id in ENABLED_GROUPS and group_id not in LOCAL_DISABLED_GROUPS

# 兜底配置
_fallback_cfg = ai_cfg.get("fallback", {})
FALLBACK_URL = _fallback_cfg.get("api_url", "")
FALLBACK_KEY = _fallback_cfg.get("api_key", "")
FALLBACK_MODEL = _fallback_cfg.get("model", "deepseek-v4-flash")

_RESPONSES_CFG_RAW = ai_cfg.get("openai_responses", {})


def _responses_model_label() -> str:
    return str(_RESPONSES_CFG_RAW.get("model") or "OpenAI Responses")


def _antigravity_available() -> bool:
    return bool(ANTIGRAVITY_ENABLED and ANTIGRAVITY_URL and ANTIGRAVITY_KEY and ANTIGRAVITY_MODEL)

# ── 群图片 MinIO（直接 get_object 读字节流，不走 presign HTTP）────────────────
_minio_cfg_ai = config["meme"]["minio"]
_minio_group_client = Minio(
    _minio_cfg_ai["endpoint"],
    access_key=_minio_cfg_ai["access_key"],
    secret_key=_minio_cfg_ai["secret_key"],
    secure=_minio_cfg_ai.get("secure", False),
)
_GROUP_IMG_BUCKET = _minio_cfg_ai.get("group_img_bucket", "group-images")


def _read_group_img_b64(obj_name: str) -> tuple[str, str] | None:
    """从 MinIO 直接 get_object() 读取群历史图片并转 base64。
    始终通过 Pillow 转成 JPEG，保证 mime type 正确。大图缩略到长边 ≤ 512px。
    """
    import io
    from PIL import Image as _Image
    try:
        resp = _minio_group_client.get_object(_GROUP_IMG_BUCKET, obj_name)
        data = resp.read()
        resp.close()
        resp.release_conn()
        img = _Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        w, h = img.size
        max_side = 256  # 256px 约 450 token/张，512px 约 1800 token，差 4 倍
        if w > max_side or h > max_side:
            ratio = min(max_side / w, max_side / h)
            img = img.resize((int(w * ratio), int(h * ratio)), _Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ---------- 人设知识库加载 ----------
def _load_persona() -> str:
    """
    加载 persona/ 目录下所有 .md 文件，按文件名排序后合并。
    character.md（设定）排在 prompt.md（规则）之前（字母序 c < p）。
    """
    # PersonaManager also supports the legacy persona/*.md layout and keeps
    # the content hot-swappable for the admin persona command.
    return get_persona_content()


PERSONA_CONTENT: str = _load_persona()


# ---------- Markdown 后处理 ----------
_MD_BOLD  = re.compile(r'\*{1,2}(.+?)\*{1,2}', re.DOTALL)
_MD_UNDER = re.compile(r'__(.+?)__', re.DOTALL)
_MD_HEAD  = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_LIST  = re.compile(r'^[\-\*•]\s+', re.MULTILINE)
_MD_OL    = re.compile(r'^\d+\.\s+', re.MULTILINE)
_MD_BLANK = re.compile(r'\n{3,}')

def _strip_markdown(text: str) -> str:
    """剥除 GPT 回复中惯用的 Markdown 格式，让文字在 QQ 里显示正常。"""
    text = _MD_BOLD.sub(r'\1', text)
    text = _MD_UNDER.sub(r'\1', text)
    text = _MD_HEAD.sub('', text)
    text = _MD_LIST.sub('', text)
    text = _MD_OL.sub('', text)
    text = _MD_BLANK.sub('\n\n', text)
    return text.strip()


# 过滤 prompt 内部标注（避免泄漏到回复中）
_INTERNAL_TAG = re.compile(
    r'\[=.*?\]'           # [=管理员，你认识的好友]
    r'|\[当前.*?\]'       # [当前北京时间: ...] [当前对话用户QQ: ...]
    r'|\[注意：.*?\]',    # [注意：以上是群聊背景记录...]
    re.DOTALL
)
_TOOL_TEXT_CALL = re.compile(
    r'^\s*(?:search|google[_-]?search|tool[_-]?call|function[_-]?call)\s*[\{\(].*$',
    re.IGNORECASE | re.DOTALL,
)
_BOT_COMMAND_REPLY = re.compile(
    r'^\s*[#＃]\s*(?:搜索|搜图|搜本子|jm|GB|gb|生图|nai|图片生成|图片编辑|翻译|鹿|🦌|切换模型)\b.*$',
    re.IGNORECASE | re.DOTALL,
)
_COMMAND_HELP_HINT = re.compile(
    r'(?:用法|可以|试试|例如|比如|输入|发送|查询|格式|指令|这样|就行|应该)',
    re.IGNORECASE,
)
_LEADING_LABEL_LINE = re.compile(r'^[^\n：:。！？!?，,、；;（）()【】\[\]{}]{1,32}\n+')
_PROVIDER_DIAGNOSTIC_LINE = re.compile(
    r'^\s*[\[({"\']*\s*(?:'
    r'char(?:acter)?\s*[:=]?\s*\d+'
    r'|line\s+\d+(?:\s*[,，:]?\s*column\s+\d+)?'
    r'(?:\s*\(?\s*char(?:acter)?\s*[:=]?\s*\d+\s*\)?)?'
    r')\s*[\])}"\']*\s*[.。]?\s*$',
    re.IGNORECASE,
)
_PROVIDER_PARSE_ERROR_LINE = re.compile(
    r'^\s*(?:json(?:decode)?error|syntaxerror|invalid\s+json|failed\s+to\s+parse|expecting\b)'
    r'.*(?:char(?:acter)?\s*[:=]?\s*\d+|position\s*[:=]?\s*\d+'
    r'|line\s+\d+.*column\s+\d+).*$',
    re.IGNORECASE,
)
_NO_COMMAND_REPLY_RULE = (
    "不要把机器人指令当成你自己的回复内容，尤其不要单独输出“#搜索 xxx”“#生图 xxx”这类命令来代替回答。"
    "如果用户是在询问某个功能怎么用，可以自然说明用法，并允许举例写出如“#GB 卡姐 2b”这样的指令；"
    "但不要只丢一行命令，也不要假装自己正在调用这些命令。"
)
_MULTI_REPLY_RULE = (
    "回复时请输出 JSON：{\"replies\":[\"第一句\",\"第二句\"]}。"
    "replies 必须是 1 到 3 句自然中文短句，每句尽量不超过45字，总字数不超过120字。"
    "请根据内容自然决定句数：简单回应、无语、短吐槽用1句；一般日常聊天默认2句；"
    "只有需要解释、安慰、回答问题或承接复杂上下文时才用3句。不要为了填满格式总写满3句。"
    "不要输出 JSON 以外的解释、Markdown 或代码块。"
    "句子之间要像真实聊天一样承接，不要把同一句硬拆碎。"
)


def _strip_prompt_echo_lines(text: str) -> str:
    """Remove short leading nickname/label lines leaked before the real reply."""
    for _ in range(2):
        m = _LEADING_LABEL_LINE.match(text)
        if not m:
            break
        first = m.group(0).strip()
        rest = text[m.end():].lstrip()
        if not first or not rest:
            break
        text = rest
    return text


def _is_provider_diagnostic_line(line: str) -> bool:
    return bool(
        _PROVIDER_DIAGNOSTIC_LINE.fullmatch(line)
        or _PROVIDER_PARSE_ERROR_LINE.fullmatch(line)
    )


def _strip_provider_diagnostic_lines(text: str) -> str:
    """移除上游错误处理泄漏出的独立字符定位行，如 char:56。"""
    lines = [
        line for line in text.splitlines()
        if not _is_provider_diagnostic_line(line)
    ]
    return "\n".join(lines).strip()


def _is_provider_diagnostic_only(text: str | None) -> bool:
    """判断整个响应是否只包含上游解析位置，而非可发送的自然语言。"""
    raw = (text or "").strip()
    if not raw:
        return False
    nonempty_lines = [line for line in raw.splitlines() if line.strip()]
    if nonempty_lines and all(_is_provider_diagnostic_line(line) for line in nonempty_lines):
        return True
    try:
        value = json.loads(raw)
    except Exception:
        return False
    if not isinstance(value, dict) or not value:
        return False
    replies = value.get("replies")
    if isinstance(replies, str):
        return _is_provider_diagnostic_line(replies)
    if isinstance(replies, list) and replies:
        return all(
            isinstance(item, str) and _is_provider_diagnostic_line(item)
            for item in replies
        )
    diagnostic_keys = {"char", "character", "line", "column", "position", "offset"}
    return set(map(str.lower, value)) <= diagnostic_keys and all(
        isinstance(item, int) and not isinstance(item, bool)
        for item in value.values()
    )


def _clean_reply(text: str) -> str:
    """去除 <think> 块 + Markdown 格式 + 内部 prompt 标注。"""
    text = strip_tool_activity(text)
    if not text:
        return ""
    if _is_provider_policy_refusal(text) or _is_provider_diagnostic_only(text):
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = _strip_provider_diagnostic_lines(text)
    if not text:
        return ""
    if _TOOL_TEXT_CALL.match(text) or (_BOT_COMMAND_REPLY.match(text) and not _COMMAND_HELP_HINT.search(text)):
        return ""
    text = _INTERNAL_TAG.sub('', text).strip()
    text = _strip_prompt_echo_lines(text)
    if _TOOL_TEXT_CALL.match(text) or (_BOT_COMMAND_REPLY.match(text) and not _COMMAND_HELP_HINT.search(text)):
        return ""
    return _strip_markdown(text)


def _extract_json_from_text(text: str) -> dict | None:
    return extract_json_object(text)


def split_reply_messages(
    text: str | None,
    max_parts: int = 3,
    *,
    max_part_chars: int = 80,
    max_total_chars: int = 160,
) -> list[str]:
    """Parse model JSON replies; fallback to one cleaned text message."""
    raw = strip_tool_activity(text)
    obj = _extract_json_from_text(raw)
    parts: list[str] = []
    structured_replies = False
    if obj is not None:
        # 任何 JSON 对象都视为结构化输出；字段异常时走 Provider 兜底，
        # 不能把内部 JSON/工具结果当作普通聊天文本发送。
        structured_replies = True
        replies = obj.get("replies")
        if isinstance(replies, list):
            for item in replies:
                if not isinstance(item, str):
                    continue
                cleaned = _clean_reply(item)
                if cleaned:
                    parts.append(cleaned)
                if len(parts) >= max_parts:
                    break
        elif isinstance(replies, str):
            cleaned = _clean_reply(replies)
            if cleaned:
                parts.append(cleaned)
    elif looks_like_replies_payload(raw):
        # 搜索 Provider 偶尔会截坏 replies JSON。只恢复其中完整的字符串；
        # 无法安全恢复时保持为空，交给下一 Provider，绝不发送 JSON 外壳。
        structured_replies = True
        for item in recover_malformed_replies(raw):
            cleaned = _clean_reply(item)
            if cleaned:
                parts.append(cleaned)
            if len(parts) >= max_parts:
                break

    # JSON replies 存在但内容全被过滤时，必须保持为空，不能把原始 JSON 当聊天文本发送。
    if not parts and not structured_replies:
        cleaned = _clean_reply(raw)
        if cleaned:
            lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
            parts.extend(lines[:max_parts] if len(lines) > 1 else [cleaned])

    clipped: list[str] = []
    total = 0
    for part in parts[:max_parts]:
        part = part.strip()
        if not part:
            continue
        if max_part_chars > 0 and len(part) > max_part_chars:
            suffix_len = 3 if max_part_chars > 3 else 0
            part = part[: max_part_chars - suffix_len].rstrip() + ("..." if suffix_len else "")
        if max_total_chars > 0 and total + len(part) > max_total_chars and clipped:
            break
        total += len(part)
        clipped.append(part)
    return clipped


def _join_reply_messages(parts: list[str]) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


class RetryableProviderResponse(RuntimeError):
    """Provider 以 HTTP 200 返回、但值得精简上下文重试的无效响应。"""


class ProviderPolicyRefusal(RetryableProviderResponse):
    """Provider 以普通文本返回的内容审核拒绝提示。"""


class ProviderDiagnosticLeak(RetryableProviderResponse):
    """Provider 以普通文本返回的内部解析诊断残片。"""


def _is_provider_policy_refusal(text: str | None) -> bool:
    """识别 Google/Gemini 被中转包装成普通文本的内容审核拒绝。"""
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not normalized:
        return False

    prompt_rejected = (
        "the prompt could not be submitted" in normalized
        or "prompt was blocked" in normalized
    )
    policy_reason = (
        "prompt contains sensitive words" in normalized
        or "generative ai prohibited use policy" in normalized
        or "prohibited use policy" in normalized
    )
    return prompt_rejected and policy_reason


def _require_provider_reply(text: str | None, provider: str) -> str:
    """过滤 Provider 的伪成功响应，失败时交给重试或下一级兜底。"""
    if _is_provider_policy_refusal(text):
        raise ProviderPolicyRefusal(f"{provider} 返回内容审核拒绝")
    if _is_provider_diagnostic_only(text):
        raise ProviderDiagnosticLeak(f"{provider} 返回上游诊断残片")
    if not split_reply_messages(text or ""):
        raise RuntimeError(f"{provider} 返回空文本")
    return text or ""


def _compact_policy_retry_messages(messages: list) -> list:
    """内容审核拒绝后，只保留 system 与当前用户消息，排除历史误触发。"""
    system_messages = [msg for msg in messages if msg.get("role") == "system"]
    current_user = next(
        (msg for msg in reversed(messages) if msg.get("role") == "user"),
        None,
    )
    if current_user is None:
        return messages
    return [*system_messages, current_user]


async def call_provider_with_policy_retry(call, messages: list, provider: str) -> str:
    """调用 Provider；若收到内容审核拒绝，精简上下文后原路重试一次。"""
    reply = await call(messages)
    try:
        return _require_provider_reply(reply, provider)
    except RetryableProviderResponse as exc:
        logger.warning(f"[ai_chat] {exc}，精简上下文重试一次")
        retry_messages = _compact_policy_retry_messages(messages)
        retry_reply = await call(retry_messages)
        return _require_provider_reply(retry_reply, provider)


# ---------- 错误日志 ----------
_log_dir = pathlib.Path("logs")
_log_dir.mkdir(exist_ok=True)
_err_log_path = _log_dir / "ai_errors.log"

def _log_err(tag: str, detail: str):
    """将错误明细写入 logs/ai_errors.log，不向用户暴露任何技术信息。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_err_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{tag}] {detail}\n")
    except Exception:
        pass
    logger.debug(f"[{tag}] {detail}")


_SEARCH_AVAILABLE_RULE = (
    "当前 Provider 可以使用联网搜索。遇到近期新闻、版本变化、陌生的新梗/缩写、实时信息或你不能确认时效的事实时，"
    "先使用搜索核实；普通闲聊、稳定知识和已知机器人用法不必搜索。"
    "不理解梗的含义时可以先搜索再自然接话，宁可不硬接也不要装懂。"
    "搜索后直接自然回答，不要输出工具调用过程，也不要声称自己搜索过却不给答案。"
)
_SEARCH_UNAVAILABLE_RULE = (
    "当前 Provider 没有联网能力。不要假装已经搜索或掌握实时信息；"
    "遇到无法确认的近期事实时，直接说明目前不能可靠核实。"
)


def _with_provider_capability(
    messages: list, *, can_search: bool, agent_search_tool: bool = False,
) -> list:
    """为单次 Provider 调用追加能力边界，不污染共享消息列表。"""
    result = [{**msg} for msg in messages]
    rule = _SEARCH_AVAILABLE_RULE if can_search else _SEARCH_UNAVAILABLE_RULE
    if not can_search and agent_search_tool:
        rule = (
            "本轮决策未启用自动联网，但你仍可按工具协议调用 search_web 获取实时资料。"
            "普通闲聊不必搜索；需要核实时先调用 search_web，再根据真实工具结果回答，"
            "不要声称已经搜索，也不要误称搜索工具不可用。"
        )
    for msg in result:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            msg["content"] = msg["content"].rstrip() + "\n" + rule
            break
    else:
        result.insert(0, {"role": "system", "content": rule})
    return result


def _period_name(dt: datetime) -> str:
    hour = dt.hour
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 8:
        return "早上"
    if 8 <= hour < 11:
        return "上午"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 20:
        return "傍晚"
    if 20 <= hour < 23:
        return "晚上"
    return "深夜"


def _affinity_label(score: float) -> str:
    from .affinity import relationship_stage

    return relationship_stage(score)


async def get_user_affinity(user_id: int) -> float:
    try:
        from .mute_control import get_persona_affinity

        return await get_persona_affinity(int(user_id))
    except Exception:
        session = await get_session()
        try:
            row = (await session.execute(
                select(UserAffinity).where(UserAffinity.user_id == int(user_id))
            )).scalar_one_or_none()
            return float(row.affinity_score) if row else 0.0
        finally:
            await session.close()


def build_dynamic_chat_prompt(user_id: int, nickname: str, affinity: float, now: datetime) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    period = _period_name(now)
    time_hint = (
        f"[当前北京时间: {now.strftime('%Y-%m-%d %H:%M')}（{weekdays[now.weekday()]}，{period}）]\n"
        f"当前是{period}，不要说与此矛盾的时间称呼，例如晚上不要说大清早。"
    )
    persona_name = get_active_persona_name()
    affinity_text = (
        f"[当前人设: {persona_name}（{get_active_persona_id()}）]\n"
        f"[当前对话用户QQ: {user_id} 昵称: {nickname}]\n"
        f"[{persona_name}对该用户的好感度: {affinity:.1f}/100，关系阶段: {_affinity_label(affinity)}]\n"
        "好感度只影响熟悉程度和耐心，不代表恋爱关系；不要对所有人默认亲密。"
        "好感度为0时只是初识：仍要礼貌、稍微温和地回应，保持一点距离即可，不能冷漠或刻薄。"
        f"好感度高于0时可随熟悉程度自然放松；只有好感度降到-10及以下，才可以明显变得较冷淡和警惕，但仍保持{persona_name}的体面。"
    )
    style_rule = (
        "说话更像日常群聊：短句、自然、有上下文承接。"
        "不要机械堆叠“哼”“真是的”“拿你没办法”。"
        "只有在疑问、害羞、生气、被冒犯等明显情绪时，才自然使用“哈？”“才……才没有”“哼”等语气。"
    )
    return "\n".join([affinity_text, time_hint, _NO_COMMAND_REPLY_RULE, style_rule, _MULTI_REPLY_RULE])


# ---------- 建表 ----------
@get_driver().on_startup  # type: ignore[attr-defined]
async def _create_chat_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[ai_chat] 数据表已就绪")
    # 启动时创建 Gemini Context Cache（仅当主模型是 Gemini 时有效）
    if PRIMARY_PROTOCOL == "gemini_native":
        try:
            from .gemini_native import upload_persona_files, start_refresh_task
            asyncio.create_task(upload_persona_files(MODEL))
            start_refresh_task(MODEL)
        except Exception as e:
            logger.warning(f"[ai_chat] persona cache 任务启动失败: {e}")


async def record_bot_reply(group_id: int, content: str) -> None:
    """将 bot 自己的回复写入群消息历史，让 AI 下次能感知自己说过的话。"""
    if not _is_group_context_enabled(group_id):
        return

    try:
        session = await get_session()
        try:
            session.add(GroupMessage(
                group_id=group_id,
                message_id=0,
                user_id=0,
                nickname="凛",
                content=content,
                raw_message=content,
                at_users="[]",
                has_image=False,
                image_url="",
                is_bot=True,
                created_at=datetime.now(),
            ))
            await prune_group_messages(group_id, session=session)
            await session.commit()
        finally:
            await session.close()
        try:
            from .agent_context import CACHE, CachedMessage

            CACHE.add(CachedMessage(
                group_id=int(group_id),
                message_id=0,
                user_id=0,
                nickname="凛",
                content=str(content),
                raw_message=str(content),
                is_bot=True,
                created_at=datetime.now(),
            ))
        except Exception as cache_exc:
            logger.debug(f"[context_cache] bot reply cache failed: {type(cache_exc).__name__}")
    except Exception as e:
        logger.debug(f"[ai_chat] record_bot_reply 失败: {e}")


# ---------- 运行时模型切换 ----------
# None 表示默认链路：Antigravity（如已配置）→ DeepSeek → 官方 Gemini → OpenAI Responses
_active_model_override: str | None = None

_MODEL_ALIASES = {
    "ag": "ag",
    "antigravity": "ag",
    "反重力": "ag",
    "gemini": "gemini",
    "官方gemini": "gemini",
    "deepseek": "deepseek",
    "ds": "deepseek",
    "openai": "openai_responses",
    "responses": "openai_responses",
}

_ADMINS_AI: set[int] = {int(u) for u in config.get("setu", {}).get("admin_users", [])}

switch_model_cmd = on_command("#切换模型", priority=2, block=True)

@switch_model_cmd.handle()
async def handle_switch_model(event: GroupMessageEvent, cmd_arg: Message = CommandArg()):
    if int(event.user_id) not in _ADMINS_AI:
        return
    global _active_model_override
    arg = re.sub(r"\s+", "", cmd_arg.extract_plain_text().strip().lower())
    if not arg or arg == "默认" or arg == "reset":
        _active_model_override = None
        await switch_model_cmd.finish(f"已切换回默认模型链（{_default_chain_label()}）")
    elif arg in _MODEL_ALIASES:
        target = _MODEL_ALIASES[arg]
        if target == "ag" and not _antigravity_available():
            await switch_model_cmd.finish("Antigravity 未启用或缺少 api_key，请先配置 ai.antigravity.api_key")
        _active_model_override = target
        names = {
            "ag": f"Antigravity（{ANTIGRAVITY_MODEL}）",
            "gemini": f"官方 Gemini（{MODEL}）",
            "deepseek": f"DeepSeek（{FALLBACK_MODEL}）",
            "openai_responses": f"OpenAI Responses（{_responses_model_label()}）",
        }
        await switch_model_cmd.finish(f"已切换主力模型为 {names[_active_model_override]}")
    else:
        await switch_model_cmd.finish("用法：#切换模型 ag / gemini / deepseek / openai / 默认")


def _default_chain_label() -> str:
    return " -> ".join((config.get("agent") or {}).get("provider_chain") or ["primary"])


def _get_active_model() -> str:
    return _active_model_override or "primary"



def _message_has_at_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    bot_id = str(bot.self_id)
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq")) == bot_id:
            return True

    # NapCat/OneBot 在「回复 + @」场景下偶尔不会把 @ 稳定解析成 at segment，
    # 但 raw_message 或 Message 字符串里仍能看到 [CQ:at,qq=...] / [at:qq=...]。
    raw = getattr(event, "raw_message", "") or ""
    rendered = str(event.message)
    pattern = rf"\[(?:CQ:)?at[:,]qq={re.escape(bot_id)}(?:[,\]])"
    return bool(re.search(pattern, raw) or re.search(pattern, rendered))


def _is_command_like_message(event: GroupMessageEvent) -> bool:
    plain = (event.get_plaintext() or "").strip().replace("＃", "#")
    plain = re.sub(r"^#\s+", "#", plain)
    return plain.startswith("#")


def _reply_text_starts_with_bot_name(event: GroupMessageEvent) -> bool:
    """引用消息时，只检查用户新输入的文字是否显式叫了 bot。"""
    raw = (getattr(event, "raw_message", "") or "").strip()
    if raw:
        # QQ 的“引用”通常会在 reply 后自动附带一个 @被引用者。NoneBot 又
        # 可能先移除昵称“凛”，所以优先从 raw_message 剥掉这些前置元数据，
        # 再检查用户真正输入的文字。
        leading_meta = re.compile(
            r"^\s*(?:"
            r"\[(?:CQ:)?reply(?:[:,][^\]]*)?\]"
            r"|\[(?:CQ:)?at[:,]qq=[^,\]]+(?:,[^\]]*)?\]"
            r")\s*",
            flags=re.IGNORECASE,
        )
        while True:
            cleaned = leading_meta.sub("", raw, count=1).lstrip()
            if cleaned == raw:
                break
            raw = cleaned
        try:
            persona_name = re.escape(get_active_persona_name())
        except Exception:
            persona_name = "远坂凛"
        if re.match(rf"^(?:远坂凛|凛|rin|{persona_name})", raw, flags=re.IGNORECASE):
            return True

    # raw_message 缺失时，回退检查当前消息中的文字段。
    current_text = "".join(
        str(seg.data.get("text", ""))
        for seg in event.message
        if seg.type == "text"
    ).strip()
    try:
        persona_name = re.escape(get_active_persona_name())
    except Exception:
        persona_name = "远坂凛"
    return bool(re.match(rf"^(?:远坂凛|凛|rin|{persona_name})", current_text, flags=re.IGNORECASE))


def _chat_trigger_kind(event: GroupMessageEvent, bot: Bot) -> str:
    """区分名字前缀与显式 @，用于决定消息发送方式。"""
    if _reply_text_starts_with_bot_name(event):
        return "name_prefix"
    if _message_has_at_bot(event, bot):
        return "direct_at"
    if event.reply:
        return ""
    if event.to_me:
        return "name_prefix"
    return ""


def _at_bot_rule() -> Rule:
    """检测消息中任意位置是否有 @bot"""
    async def _rule(bot: Bot, event: GroupMessageEvent) -> bool:
        from .console_runtime import ready
        if ready():
            return False
        if _is_command_like_message(event):
            return False

        # Active Agent groups use the unified batch router.  Keep this module's
        # provider helpers available, but never let the legacy matcher answer
        # before the Agent sees a name-prefix or reply message.
        try:
            from .agent_runtime import agent_group_enabled

            if agent_group_enabled(event.group_id):
                return False
        except Exception:
            pass

        return bool(_chat_trigger_kind(event, bot))
    return Rule(_rule)


ai_chat = on_message(rule=_at_bot_rule(), priority=5, block=True)


async def _send_chat_parts(
    bot: Bot,
    event: GroupMessageEvent,
    parts: list[str],
    trigger_kind: str,
    *,
    force_quote: bool = False,
) -> None:
    """按群锁定整批回复，避免多用户的多句回答互相穿插。"""
    await send_group_reply_parts(
        bot,
        event.group_id,
        parts,
        reply_message_id=event.message_id,
        quote_rate=AT_REPLY_QUOTE_RATE if trigger_kind == "direct_at" else 0.0,
        force_quote=force_quote,
    )


@ai_chat.handle()
async def handle_chat(bot: Bot, event: GroupMessageEvent):
    _t0 = time.monotonic()
    user_id = event.user_id
    group_id = event.group_id
    trigger_kind = _chat_trigger_kind(event, bot)
    rapid_chat_trigger = mark_group_trigger(group_id)

    # 任何显式聊天触发都会打断普通群聊的复读序列。
    try:
        from .group_chat import reset_repeat_state
        reset_repeat_state(group_id)
    except Exception:
        pass

    # 获取用户昵称
    try:
        member = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        nickname = member.get("card") or member.get("nickname") or str(user_id)
    except Exception:
        nickname = str(user_id)
    logger.debug(f"[timing] T+{time.monotonic()-_t0:.2f}s 昵称获取")

    # 构建消息文本，将 @某人 解析为真实昵称，避免 AI 收到空白或无法识别的 QQ 号
    # 若被 @ 的是 special_users 中定义的特殊用户，附加身份标注让 AI 能正确识别
    special_users = ai_cfg.get("special_users", {})
    message_parts = []
    for seg in event.message:
        if seg.type == "text":
            text = seg.data.get("text", "").strip()
            if text:
                message_parts.append(text)
        elif seg.type == "at":
            at_qq = str(seg.data.get("qq", ""))
            if at_qq == str(bot.self_id):
                continue  # 跳过 @机器人 本身
            try:
                at_member = await bot.get_group_member_info(group_id=group_id, user_id=int(at_qq))
                at_name = at_member.get("card") or at_member.get("nickname") or at_qq
            except Exception:
                at_name = at_qq
            # 特殊用户附加身份标注（如 @S.管理员[=管理员，你的好友]），帮助 AI 识别关系
            message_parts.append(f"@{at_name}")
    message = " ".join(message_parts).strip()
    current_user_text = message

    # ---- C: 处理引用/回复消息 ----
    # 提取被引用消息的文字和图片，拼成「引用X: ...」前缀让 AI 知道对话背景
    # 注意：引用图片也加入 image_urls，让视觉模型能看到
    image_urls = [
        {
            "url": seg.data.get("url") or seg.data.get("file", ""),
            "label": f"当前对话用户「{nickname}」发送的图片",
        }
        for seg in event.message
        if seg.type == "image"
        and seg.data.get("sub_type", 0) != 1
        and (seg.data.get("url") or seg.data.get("file", ""))
    ]
    reply_context = await resolve_reply_context(bot, event)
    if reply_context:
        # 统一使用带发送者身份的引用前缀；raw reply+at 只有在这里解析后
        # 才不会被 AI 误认为是当前用户或凛自己说的话。
        message = format_reply_context(reply_context, message)
        image_urls = list(reply_context.image_refs) + image_urls

    # ---- D: 处理合并转发（仅第一层） ----
    for seg in event.message:
        if seg.type != "forward":
            continue
        fwd_id = seg.data.get("id", "")
        if not fwd_id:
            continue
        try:
            fwd_data = await bot.call_api("get_forward_msg", message_id=fwd_id)
            fwd_messages = fwd_data.get("messages") or fwd_data.get("msg", [])
            lines = []
            for fwd_item in fwd_messages:
                sender_info = fwd_item.get("sender") or {}
                fwd_name = sender_info.get("card") or sender_info.get("nickname") or "某人"
                # 展开消息内容：文字 + 标注是否含图
                fwd_msg = fwd_item.get("message", [])
                if isinstance(fwd_msg, str):
                    fwd_text = fwd_msg.strip()
                    fwd_has_img = False
                else:
                    fwd_text = "".join(
                        s.get("data", {}).get("text", "")
                        for s in fwd_msg if s.get("type") == "text"
                    ).strip()
                    fwd_has_img = any(s.get("type") == "image" for s in fwd_msg)
                line = f"{fwd_name}: {fwd_text}" if fwd_text else f"{fwd_name}:"
                if fwd_has_img:
                    line += "（含图片）"
                lines.append(line)
            if lines:
                fwd_summary = "[合并转发内容]\n" + "\n".join(lines)
                message = (message + "\n" + fwd_summary).strip() if message else fwd_summary
                logger.info(f"[ai_chat] 展开转发 id={fwd_id}，共 {len(lines)} 条")
        except Exception as fwd_err:
            logger.warning(f"[ai_chat] 获取合并转发内容失败: {fwd_err}")
        break  # 只处理第一个 forward segment

    empty_direct_at = False
    if not message and not image_urls:
        if trigger_kind != "direct_at":
            return
        empty_direct_at = True
        message = "只@了你，没有附带文字。请结合最近的群聊上下文自然回应。"

    logger.debug(f"[timing] T+{time.monotonic()-_t0:.2f}s 消息解析完毕 img={len(image_urls)}")

    # 白名单群注入群上下文
    context_group_id = group_id if _is_group_context_enabled(group_id) else None

    if image_urls:
        reply = await chat_with_vision(user_id, nickname, message, image_urls, context_group_id)
    else:
        reply = await chat(
            user_id,
            nickname,
            message,
            context_group_id,
            persist_history=not empty_direct_at,
        )

    logger.debug(f"[timing] T+{time.monotonic()-_t0:.2f}s chat()返回")
    if not reply:
        return

    reply_parts = split_reply_messages(reply, max_parts=3)
    if not reply_parts:
        return

    await _send_chat_parts(
        bot,
        event,
        reply_parts,
        trigger_kind,
        force_quote=rapid_chat_trigger,
    )
    logger.debug(f"[timing] T+{time.monotonic()-_t0:.2f}s 消息发送完毕")

    # B: 将 bot 回复写入群消息历史，让 AI 下次能感知自己说过的话
    for part in reply_parts:
        asyncio.create_task(record_bot_reply(group_id, part))

    # 概率附带情绪表情包（独立消息，稍后发出）。Agent 和旧聊天路径共用
    # 同一套表情池、去重窗口与冷却。
    if reply_parts:
        await maybe_send_reply_meme(
            bot,
            group_id,
            _join_reply_messages(reply_parts),
            rate=ai_cfg.get("group_chat", {}).get("reply_meme_rate", 0.15),
        )


# ---------- 文字对话 (MySQL 存储) ----------

_FATAL_CODES = {"service_unavailable", "rate_limit_exceeded", "insufficient_quota"}


async def _call_primary(messages: list, *, timeout: float | None = None) -> str:
    """Use the explicitly configured protocol and the console's current model."""
    from . import console_runtime
    context = REQUEST_CONTEXT.get() or {}
    model = context.get("model") or console_runtime.global_value("model", MODEL)
    if PRIMARY_PROTOCOL == "gemini_native":
        from .gemini_native import call_gemini_native
        return await call_gemini_native(model, messages)
    from .model_transport import complete
    started = time.monotonic()
    usage = {}
    status = "failed"
    try:
        reply, usage = await complete(ai_cfg, messages, model=model,
            timeout=timeout or context.get("timeout") or PRIMARY_TIMEOUT)
        status = "success"
        return reply
    finally:
        console_runtime.record("model_request", int(context.get("group_id") or 0),
            provider="chat_completions", model=model, source=context.get("purpose", "chat"),
            request_id=context.get("request_id") or uuid4().hex[:12], attempt=1, status=status,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"))


def _strip_images(messages: list) -> list:
    """将消息列表中所有 image_url 内容块剔除，转为纯文本消息（兜底模型不支持多模态）"""
    result = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            text_parts = [p["text"] for p in msg["content"] if p.get("type") == "text"]
            content = "\n".join(text_parts)
            if content:
                result.append({**msg, "content": content})
        else:
            result.append(msg)
    return result


async def _call_antigravity(messages: list) -> str:
    """调用 Antigravity OpenAI 兼容接口，作为主文字聊天一级 Provider。"""
    if not _antigravity_available():
        raise RuntimeError("Antigravity 未启用或缺少 api_key")

    if ANTIGRAVITY_PROTOCOL != "openai":
        logger.warning(
            f"[ai_chat] ai.antigravity.protocol={ANTIGRAVITY_PROTOCOL!r} 已忽略，"
            "主聊天固定使用 OpenAI 兼容口"
        )
    context = REQUEST_CONTEXT.get() or {}
    enable_search = (
        bool(context.get("backend_search", True))
        if context.get("purpose") == "group_decision" else None
    )
    return await _call_antigravity_openai(
        messages, preserve_images=True, enable_search=enable_search,
    )


def _build_antigravity_payload(messages: list, *, enable_search: bool) -> dict:
    payload = {
        "model": ANTIGRAVITY_MODEL,
        "messages": messages,
        "stream": False,
    }
    if enable_search:
        payload["tools"] = [{"googleSearch": {}}]
    return payload


def _is_antigravity_search_tool_error(status_code: int, detail: object) -> bool:
    if status_code not in (400, 422):
        return False
    if isinstance(detail, str):
        text = detail.lower()
    else:
        text = json.dumps(detail, ensure_ascii=False).lower()
    search_markers = (
        "googlesearch",
        "google_search",
        "google search",
        "builtin_web_search",
    )
    tool_error_markers = (
        "unknown field",
        "invalid argument",
        "not supported",
        "unsupported",
        "tool declaration",
    )
    return any(marker in text for marker in search_markers) or (
        "tool" in text and any(marker in text for marker in tool_error_markers)
    )


async def _call_antigravity_openai(
    messages: list,
    *,
    enable_search: bool | None = None,
    preserve_images: bool = False,
    timeout: float | None = None,
) -> str:
    """调用 Antigravity OpenAI 兼容接口。"""

    search_enabled = ANTIGRAVITY_ENABLE_SEARCH if enable_search is None else bool(enable_search)

    context = REQUEST_CONTEXT.get() or {}
    messages = _with_provider_capability(
        messages, can_search=search_enabled,
        agent_search_tool=context.get("purpose") == "group_decision",
    )
    if not preserve_images:
        messages = _strip_images(messages)
    request_id = context.get("request_id") or uuid4().hex[:12]
    purpose = context.get("purpose") or "chat"
    group_id = context.get("group_id") or "-"
    request_timeout = (
        timeout if timeout is not None else context.get("timeout") or ANTIGRAVITY_TIMEOUT
    )
    text_chars = 0
    image_count = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_chars += len(part.get("text") or "")
                    elif part.get("type") == "image_url":
                        image_count += 1

    started = time.monotonic()
    resp = None
    response_id = "-"
    attempt = 0
    data = None
    attempt_started = started
    payload_bytes = 0
    request_model = context.get("model") or ANTIGRAVITY_MODEL
    dispatched = False

    def log_result(status: str, error: BaseException | None = None) -> None:
        upstream_id = "-"
        if resp is not None:
            upstream_id = (
                resp.headers.get("x-request-id")
                or resp.headers.get("x-goog-request-id")
                or resp.headers.get("request-id")
                or "-"
            )
        # Log identifiers and timings only; provider error bodies may echo prompts.
        upstream_id = re.sub(r"[^\w.:-]", "_", str(upstream_id))[:128]
        safe_response_id = re.sub(r"[^\w.:-]", "_", str(response_id))[:128]
        log = logger.warning if error is not None else logger.info
        log(
            f"[ag_request] request_id={request_id} group={group_id} purpose={purpose} "
            f"status={status} elapsed={time.monotonic() - started:.2f}s "
            f"timeout={request_timeout}s attempt={attempt} "
            f"http_status={resp.status_code if resp is not None else '-'} "
            f"upstream_id={upstream_id} response_id={safe_response_id} "
            f"error={type(error).__name__ if error is not None else '-'}"
        )
        try:
            from . import console_runtime
            if not dispatched:
                return
            gid = int(group_id) if str(group_id).lstrip("-").isdigit() else 0
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            console_runtime.record(
                "model_request", gid, event_key=f"{request_id}:{attempt}:{status}",
                provider="antigravity", model=request_model, source=purpose,
                status="timeout" if status == "cancelled" and time.monotonic() - started >= float(request_timeout) - 1 else status,
                latency_ms=int((time.monotonic() - attempt_started) * 1000),
                request_id=str(request_id), attempt=attempt, queue_wait_ms=context.get("queue_wait_ms"),
                input_chars=text_chars, image_count=image_count, payload_bytes=payload_bytes,
                input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
                total_tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
            )
        except Exception:
            pass

    _transport = (
        httpx.AsyncHTTPTransport(proxy=ANTIGRAVITY_PROXY, http2=False)
        if ANTIGRAVITY_PROXY
        else httpx.AsyncHTTPTransport(http2=False)
    )
    try:
        async with httpx.AsyncClient(
            timeout=request_timeout,
            mounts={"https://": _transport, "http://": _transport},
        ) as client:
            search_attempts = [True, False] if search_enabled else [False]
            data: dict | None = None
            for attempt, use_search in enumerate(search_attempts, 1):
                dispatched = False
                attempt_started = time.monotonic()
                resp = None
                response_id = "-"
                payload = _build_antigravity_payload(messages, enable_search=use_search)
                payload["model"] = request_model
                payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                logger.info(
                    f"[ag_request] request_id={request_id} group={group_id} purpose={purpose} "
                    f"status=start model={request_model} "
                    f"search={'enabled' if use_search else 'disabled'} attempt={attempt} "
                    f"timeout={request_timeout}s input_chars={text_chars} "
                    f"images={image_count} payload_bytes={payload_bytes}"
                )
                dispatched = True
                resp = await client.post(
                    ANTIGRAVITY_URL,
                    headers={
                        "Authorization": f"Bearer {ANTIGRAVITY_KEY}",
                        "Content-Type": "application/json",
                        "X-Request-ID": str(request_id),
                    },
                    json=payload,
                )
                if not resp.content:
                    raise RuntimeError(f"Antigravity 返回空 body (HTTP {resp.status_code})")
                try:
                    parsed = resp.json()
                except Exception:
                    if use_search and _is_antigravity_search_tool_error(resp.status_code, resp.text):
                        log_result("retry_without_search")
                        continue
                    raise RuntimeError(f"Antigravity 返回非 JSON (HTTP {resp.status_code})")

                if not isinstance(parsed, dict):
                    raise RuntimeError(f"Antigravity 返回异常 JSON: {type(parsed).__name__}")
                response_id = parsed.get("id") or "-"
                if resp.status_code != 200:
                    detail = parsed.get("error", resp.text[:200])
                    if use_search and _is_antigravity_search_tool_error(resp.status_code, detail):
                        log_result("retry_without_search")
                        continue
                    raise RuntimeError(f"Antigravity HTTP {resp.status_code}")
                data = parsed
                break

            if data is None:
                raise RuntimeError("Antigravity 搜索兼容重试失败")
            if "error" in data:
                raise RuntimeError("Antigravity API error")

            message = data["choices"][0].get("message", {})
            finish_reason = str(data["choices"][0].get("finish_reason") or "").strip()
            normalized_finish_reason = finish_reason.lower()
            if normalized_finish_reason in {
                "prohibited_content",
                "content_filter",
                "safety",
                "blocked",
                "blocklist",
                "recitation",
                "length",
                "max_tokens",
            }:
                raise RuntimeError(f"Antigravity 响应未完整结束: {finish_reason}")
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            content = (content or "").strip()
            if not content:
                raise RuntimeError("Antigravity 返回空文本")
            log_result("success")
            return content
    except asyncio.CancelledError as exc:
        log_result("cancelled", exc)
        raise
    except Exception as exc:
        log_result("failed", exc)
        raise


async def _call_fallback(messages: list) -> str:
    """调用第二级兜底模型（DeepSeek V4），自动剥图后发送"""
    messages = _strip_images(_with_provider_capability(messages, can_search=False))
    async with httpx.AsyncClient(timeout=60) as client:
        logger.info(f"[ai_chat] 调用二级兜底模型 {FALLBACK_MODEL}")
        resp = await client.post(
            FALLBACK_URL,
            headers={"Authorization": f"Bearer {FALLBACK_KEY}"},
            json={
                "model": FALLBACK_MODEL,
                "messages": messages,
            },
        )
        if not resp.content:
            _log_err("ai_chat/fallback", f"HTTP {resp.status_code} 空 body")
            raise RuntimeError(f"Fallback 返回空 body (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except Exception:
            _log_err("ai_chat/fallback", f"HTTP {resp.status_code} 非JSON: {resp.text[:200]}")
            raise
        if "error" in data:
            raise RuntimeError(f"Fallback API error: {data['error']}")
        return data["choices"][0]["message"]["content"].strip()


async def _call_openai_responses(messages: list) -> str:
    """调用 OpenAI Responses 兜底；普通聊天不启用联网工具。"""
    logger.info(f"[ai_chat] 调用 OpenAI Responses {_responses_model_label()}")
    return await call_responses(
        config,
        _with_provider_capability(messages, can_search=False),
        enable_web_search=False,
        max_output_tokens=1600,
    )


async def chat(user_id: int, nickname: str, user_message: str, group_id: int = None,
               context_images: list[str] | None = None, *,
               persist_history: bool = True) -> str:
    """主对话 — 用户40条历史 + 群聊75条上下文（含图片）"""
    _tc = time.monotonic()
    # 1. 从 MySQL 加载用户近期对话 (最近40条)
    session = await get_session()
    try:
        rows = (await session.execute(
            select(ChatHistory)
            .where(ChatHistory.user_id == user_id)
            .order_by(ChatHistory.id.desc())
            .limit(USER_CONTEXT_LIMIT)
        )).scalars().all()
        rows.reverse()
        history = [{"role": r.role, "content": r.content} for r in rows]
    finally:
        await session.close()
    logger.debug(f"[timing/chat] +{time.monotonic()-_tc:.2f}s MySQL历史({len(history)}条)")

    # 2. 构建 system prompt
    # Gemini native 路径：人设和角色设定已在 Context Cache 中，仅注入动态 QQ/昵称
    # 非 Gemini 路径：使用 persona/*.md 全文作为 system prompt（含角色设定和人设文件）
    from zoneinfo import ZoneInfo
    _now_cst = datetime.now(ZoneInfo("Asia/Shanghai"))
    affinity = await get_user_affinity(user_id)
    dynamic_prompt = build_dynamic_chat_prompt(user_id, nickname, affinity, _now_cst)
    if PRIMARY_PROTOCOL == "gemini_native":
        prompt = dynamic_prompt
    else:
            prompt = (get_persona_content() or "") + "\n\n" + dynamic_prompt

    messages = [{"role": "system", "content": prompt}]

    # 3. 注入群聊上下文（最近75条；历史图片从 MinIO 下载转 base64，避免端口/过期问题）
    if group_id is not None:
        group_context_text, ctx_img_refs = await _get_group_context(group_id, GROUP_CONTEXT_LIMIT)
        logger.debug(f"[timing/chat] +{time.monotonic()-_tc:.2f}s 群上下文({len(ctx_img_refs)}个图引用)")
        if group_context_text:
            group_context_text += f"\n\n[注意：以上是群聊背景记录，当前正在与你对话的用户是「{nickname}」，请勿混淆。]"
            # 从 MinIO 直接 get_object() 读群历史图，转 base64，并行下载
            valid_refs = [(ref, nick) for ref, nick in ctx_img_refs if ref and not ref.startswith("http")]
            loop = asyncio.get_event_loop()
            download_results = await asyncio.gather(
                *[loop.run_in_executor(None, _read_group_img_b64, ref) for ref, _ in valid_refs],
                return_exceptions=True
            )
            ctx_imgs: list[dict] = []
            for (ref, sender_nick), result in zip(valid_refs, download_results):
                if result is None or isinstance(result, Exception):
                    continue
                mime, b64 = result
                # 先插入归属标签，再插入图片，让模型知道这张图是谁发的
                ctx_imgs.append({"type": "text", "text": f"（以下是「{sender_nick}」发的图片）"})
                ctx_imgs.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            logger.debug(f"[timing/chat] +{time.monotonic()-_tc:.2f}s MinIO下载({len(ctx_imgs) // 2}张)")
            if ctx_imgs:
                ctx_content: list = [{"type": "text", "text": group_context_text}] + ctx_imgs
                messages.append({"role": "user", "content": ctx_content})
            else:
                messages.append({"role": "user", "content": group_context_text})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近的动态。"})

    # 4. 用户历史 + 当前消息 (带昵称标签)
    messages.extend(history)
    tagged_msg = f"[{nickname}]: {user_message}"
    messages.append({"role": "user", "content": tagged_msg})

    # 5. 调用 API（四级兜底：Antigravity → DeepSeek → 官方 Gemini → OpenAI Responses）
    def _messages_with_full_persona() -> list:
        """兜底模型不享有 Context Cache，补全人设 system prompt 后再发"""
        if PRIMARY_PROTOCOL != "gemini_native":
            return messages
        full_prompt = (get_persona_content() or "") + "\n\n" + dynamic_prompt
        return [{"role": "system", "content": full_prompt}] + messages[1:]

    async def _run_with_fallback() -> str | None:
        calls = {"primary": _call_primary, "antigravity": _call_antigravity,
                 "fallback": _call_fallback, "openai_responses": _call_openai_responses}
        chain = (config.get("agent") or {}).get("provider_chain") or ["primary"]
        if _active_model_override:
            chain = [{"ag": "antigravity", "gemini": "primary", "deepseek": "fallback"}.get(
                _active_model_override, _active_model_override)]
        for provider in chain:
            callback = calls.get(provider)
            if callback is None:
                continue
            try:
                return await call_provider_with_policy_retry(callback, _messages_with_full_persona(), provider)
            except Exception as exc:
                logger.warning(f"[ai_chat] provider={provider} failed={type(exc).__name__}")
        return None

    reply = await _run_with_fallback()
    logger.debug(f"[timing/chat] +{time.monotonic()-_tc:.2f}s API调用返回")
    if reply is None:
        return None

    reply_parts = split_reply_messages(reply, max_parts=3)
    reply = _join_reply_messages(reply_parts)
    if not reply:
        _log_err("ai_chat/chat", "所有模型返回内容清洗后为空")
        return None

    # 6. 存入 MySQL (保留100条)。空 @ 的内部提示不进入个人历史或长期记忆。
    if persist_history:
        session = await get_session()
        try:
            session.add(ChatHistory(user_id=user_id, role="user", content=tagged_msg))
            session.add(ChatHistory(user_id=user_id, role="assistant", content=reply))
            await session.commit()
            # 裁剪: 保留最新 MAX_HISTORY 条
            count = (await session.execute(
                select(func.count()).select_from(ChatHistory).where(ChatHistory.user_id == user_id)
            )).scalar()
            if count > MAX_HISTORY:
                old_ids = (await session.execute(
                    select(ChatHistory.id)
                    .where(ChatHistory.user_id == user_id)
                    .order_by(ChatHistory.id.asc())
                    .limit(count - MAX_HISTORY)
                )).scalars().all()
                if old_ids:
                    await session.execute(delete(ChatHistory).where(ChatHistory.id.in_(old_ids)))
                    await session.commit()
        except Exception as e:
            logger.warning(f"[ai_chat] 历史存储失败: {e}")
        finally:
            await session.close()

        # 7. 异步提取记忆
        asyncio.create_task(_extract_memory(user_id, user_message))

    return reply


async def _get_group_context(group_id: int, limit: int = GROUP_CONTEXT_LIMIT) -> tuple[str | None, list[tuple[str, str]]]:
    """从数据库获取指定数量的群聊记录（无摘要，直接原始消息）。
    返回 (text, img_refs)
    """
    session = await get_session()
    try:
        rows = (await session.execute(
            select(GroupMessage)
            .where(GroupMessage.group_id == group_id)
            .order_by(GroupMessage.id.desc())
            .limit(limit)
        )).scalars().all()
        if not rows:
            return None, []
        rows.reverse()
        lines = []
        img_refs: list[tuple[str, str]] = []
        for r in rows:
            if getattr(r, "is_bot", False):
                lines.append(f"凛(我): {r.content}")
                continue
            if r.has_image and r.content:
                lines.append(f"{r.nickname}: [图片] {r.content}")
            elif r.has_image:
                lines.append(f"{r.nickname}: [发了图片]")
            else:
                lines.append(f"{r.nickname}: {r.content}")
            img_url = getattr(r, "image_url", "") or ""
            if img_url:
                img_refs.append((img_url, "凛(我)" if getattr(r, "is_bot", False) else r.nickname))
        recent_imgs = img_refs[-3:]  # 最多3张，控制历史图片带来的 token 消耗
        context_text = "\n".join(lines) if lines else None
        return context_text, recent_imgs
    finally:
        await session.close()


# ---------- 图片识别对话 ----------

async def chat_with_vision(user_id: int, nickname: str, user_message: str,
                           image_urls: list, group_id: int = None) -> str:
    """调用视觉模型识图，先下载图片转 base64 再发送。
    同时注入群聊文字上下文（历史图片已在当前 image_urls 里，此处只加文字）。
    """
    vision_cfg = ai_cfg.get("vision", {})
    api_url = vision_cfg.get("api_url", API_URL)
    api_key = vision_cfg.get("api_key", API_KEY)
    model = vision_cfg.get("model") or ai_cfg["model"]

    content: list = []
    async with httpx.AsyncClient(timeout=30) as dl_client:
        for item in image_urls:
            if isinstance(item, dict):
                url = item.get("url", "")
                label = item.get("label", "")
            else:
                url = str(item)
                label = ""
            if not url:
                continue
            try:
                img_resp = await dl_client.get(url)
                img_resp.raise_for_status()
                mime = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                b64 = base64.b64encode(img_resp.content).decode()
                data_url = f"data:{mime};base64,{b64}"
                if label:
                    content.append({"type": "text", "text": f"（以下图片说明：{label}）"})
                content.append({"type": "image_url", "image_url": {"url": data_url}})
                logger.debug(f"[ai_chat/vision] 图片已下载转 base64 ({len(img_resp.content)//1024}KB)")
            except Exception as e:
                logger.warning(f"[ai_chat/vision] 图片下载失败，跳过: {e}")
    if not content:
        # 所有图片下载失败，降级为普通文字对话
        return await chat(user_id, nickname, user_message or "你看到我发图了吗", group_id)

    prompt = user_message if user_message else "描述一下这张图，然后用你一贯的风格评论一句"
    content.append({"type": "text", "text": f"[{nickname}]: {prompt}"})

    from zoneinfo import ZoneInfo
    _now_cst = datetime.now(ZoneInfo("Asia/Shanghai"))
    affinity = await get_user_affinity(user_id)
    dynamic_prompt = build_dynamic_chat_prompt(user_id, nickname, affinity, _now_cst)

    # Gemini native 路径：人设已在 Context Cache 中，仅注入动态 QQ/昵称
    if PRIMARY_PROTOCOL == "gemini_native":
        system_prompt = dynamic_prompt
    else:
        system_prompt = (get_persona_content() or "") + "\n\n" + dynamic_prompt

    messages = [{"role": "system", "content": system_prompt}]
    # 注入群聊文字上下文（视觉调用时只追加文字，图片已在 content 里）
    if group_id is not None:
        group_context_text, _ = await _get_group_context(group_id, GROUP_CONTEXT_LIMIT)
        if group_context_text:
            group_context_text += f"\n\n[注意：以上是群聊背景记录，当前正在与你对话的用户是「{nickname}」，请勿混淆。]"
            messages.append({"role": "user", "content": group_context_text})
            messages.append({"role": "assistant", "content": "好的，我了解了群里最近的动态。"})

    messages.append({"role": "user", "content": content})

    # Gemini 视觉模型走原生 API（更可靠，支持 Context Cache）
    try:
        reply = await _call_primary(messages, timeout=120)
        return _clean_reply(reply)
    except Exception as exc:
        logger.warning(f"[ai_chat/vision] failed={type(exc).__name__}")
        return await chat(user_id, nickname, user_message or "Please describe the image.", group_id)


# ---------- 记忆提取 ----------

async def _extract_memory(user_id: int, user_message: str):
    """异步提取用户发言记忆存入 MySQL"""
    try:
        prompt = (
            "请把以下用户说的话总结成一句简短的第三人称陈述句（15字以内），"
            "只输出这句话，不要加任何多余内容。\n"
            f"用户说：{user_message}"
        )
        summary = await _call_primary([{"role": "user", "content": prompt}], timeout=60)
        summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()

        session = await get_session()
        try:
            session.add(UserMemory(user_id=user_id, content=summary))
            await session.commit()
            count = (await session.execute(
                select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)
            )).scalar()
            if count > 100:
                old_ids = (await session.execute(
                    select(UserMemory.id)
                    .where(UserMemory.user_id == user_id)
                    .order_by(UserMemory.id.asc())
                    .limit(count - 100)
                )).scalars().all()
                if old_ids:
                    await session.execute(delete(UserMemory).where(UserMemory.id.in_(old_ids)))
                    await session.commit()
        finally:
            await session.close()
    except Exception as e:
        logger.debug(f"[ai_chat] 记忆提取失败: {e}")
