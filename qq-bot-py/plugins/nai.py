"""NAI 图片生成插件 — #生图 <提示词> [附图/回复图作为参考]

调用 NovelAI image API 生成图片，支持中文提示词自动翻译。
"""
import base64
import asyncio
import hashlib
import io
import json
import random
import re
import time
from datetime import date
import zipfile

import httpx
import yaml
from PIL import Image
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

nai_cfg = config.get("nai", {})
TOKEN = nai_cfg.get("token", "")
MODEL = nai_cfg.get("model", "nai-diffusion-4-5-full")
WIDTH = nai_cfg.get("width", 832)
HEIGHT = nai_cfg.get("height", 1216)
STEPS = nai_cfg.get("steps", 28)
SCALE = nai_cfg.get("scale", 6.0)
SAMPLER = nai_cfg.get("sampler", "k_euler_ancestral")
NEGATIVE_PROMPT = nai_cfg.get(
    "negative_prompt",
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, bad feet, "
    "multiple views, poorly drawn face, mutation, deformed, extra limbs",
)
PROMPT_PREFIX = nai_cfg.get(
    "prompt_prefix",
    "best quality, amazing quality, very aesthetic, absurdres, masterpiece",
)
REF_STRENGTH = nai_cfg.get("ref_strength", 0.6)
REF_INFO_EXTRACTED = nai_cfg.get("ref_info_extracted", 1.0)
COOLDOWN = nai_cfg.get("cooldown", 60)
TIMEOUT = nai_cfg.get("timeout", 120)
ALLOWED_GROUPS: set[int] = set(nai_cfg.get("allowed_groups", []))
# 优先用 SOCKS5（比 HTTP 代理更稳定，不会在 CONNECT 隧道中断 TLS）
PROXY: str = nai_cfg.get("proxy_socks5") or nai_cfg.get("proxy", config.get("proxy", ""))
DAILY_LIMIT = nai_cfg.get("daily_limit", 5)  # 每群每天最大调用次数（0=不限）
GROUP_OVERRIDES: dict = nai_cfg.get("group_overrides", {})  # 每群单独覆盖配置
ADMIN_USERS: set[int] = set(nai_cfg.get("admin_users", []))

# 中文提示词翻译统一使用 Antigravity。

NAI_ENDPOINT = "https://image.novelai.net/ai/generate-image"

_cooldown: dict[int, float] = {}
# 每日计数：{group_id: (date_str, count)}
_daily_count: dict[int, tuple[str, int]] = {}


def _to_precise_ref_png(data: bytes) -> bytes:
    """Precise Reference 要求图片尺寸为 1024x1536 / 1536x1024 / 1472x1472，多余部分黑色填充。
    compress_level=6 明显减小文件体积，避免上传时 NAI 服务器 i/o timeout。
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    # 根据图片長短边选择最接近的目标尺寸
    candidates = [(1024, 1536), (1536, 1024), (1472, 1472)]
    ratio = w / h
    target_w, target_h = min(candidates, key=lambda s: abs(s[0] / s[1] - ratio))
    # 缩放至适合目标尺寸内（保持比例）
    scale = min(target_w / w, target_h / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # 黑色填充到目标尺寸
    canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    canvas.paste(img, (paste_x, paste_y))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()

_TRANS_SYSTEM = (
    "你是NovelAI绘图提示词专家。将用户输入的中文翻译为NAI英文格式，逗号分隔，直接输出结果，不要解释。"
    "规则："
    "角色名用英文官方名，后跟作品名括号（如 Asuna(Sword Art Online)）；"
    "用户输入中已带括号的部分（如 角色名（服装名））必须原样保留括号内容，不得展开、补充或替换，直接将括号内文字若已是英文则不变、若是中文则翻译；"
    "服装/特征/动作类用英文短语，单词之间用空格（如 blonde hair, black thighhighs, sitting）；"
    "操作指令类（如参考以上图片、改成金发）翻译为对应英文短语（如 refer to the reference image, change hair to blonde）；"
    "NSFW内容直接英文；已是英文则原样保留；不要添加quality tag。"
)


async def _nai_translate_prompt(text: str) -> str:
    """若提示词含中文则调用 LLM 翻译为 NAI 英文格式，否则原样返回。"""
    if not re.search(r"[\u4e00-\u9fff]", text):
        return text
    try:
        from .ai_chat import _call_primary

        result = await _call_primary(
            [
                {"role": "system", "content": _TRANS_SYSTEM},
                {"role": "user", "content": text},
            ],
        )
        result = result.strip()
        if not result:
            logger.warning("[NAI] Antigravity 翻译返回空内容，使用原始提示词")
            return text
        return result
    except Exception as e:
        logger.warning(f"[NAI] 翻译失败，大模型返回原始提示词: {e}")
        return text


async def _extract_ref_image(bot: Bot, event: GroupMessageEvent) -> bytes | None:
    """从当前消息或被回复消息中提取第一张图片的原始字节，失败返回 None。"""
    for seg in event.message:
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file")
            if url and url.startswith("http"):
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        r = await client.get(url)
                    return r.content
                except Exception as e:
                    logger.warning(f"[NAI] 下载当前消息图片失败: {e}")
            return None

    # 尝试被回复的消息
    reply = event.reply
    if reply:
        for seg in reply.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file")
                if url and url.startswith("http"):
                    try:
                        async with httpx.AsyncClient(timeout=20) as client:
                            r = await client.get(url)
                        return r.content
                    except Exception as e:
                        logger.warning(f"[NAI] 下载回复消息图片失败: {e}")
                return None
    return None


nai_cmd = on_command("#生图", aliases={"#nai"}, priority=5, block=True)


@nai_cmd.handle()
async def handle_nai(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    # 群白名单检查（必须在白名单内才能使用）
    if group_id not in ALLOWED_GROUPS:
        return

    # token 检查
    if not TOKEN:
        await nai_cmd.finish("NAI token 未配置，请联系管理员。")
        return

    prompt = args.extract_plain_text().strip()
    if not prompt:
        await nai_cmd.finish(
            "用法：#生图 <提示词>\n"
            "提示词支持中文，可附图或回复含图消息作为参考图。\n"
            "示例：#生图 雷電将軍 和服 夜景"
        )
        return

    # NSFW 关键词拦截
    if re.search(r"\bnsfw\b", prompt, re.IGNORECASE):
        await nai_cmd.finish("不支持生成该类内容。")
        return

    # 冷却检查
    now = time.time()
    last = _cooldown.get(group_id, 0)
    remaining = COOLDOWN - (now - last)
    if remaining > 0:
        await nai_cmd.finish(f"冷却中，请 {remaining:.0f} 秒后再试。")
        return

    # 每日次数限制（admin_users 免限）
    is_admin = event.user_id in ADMIN_USERS
    g_override = GROUP_OVERRIDES.get(group_id, GROUP_OVERRIDES.get(str(group_id), {}))
    g_limit = g_override.get("daily_limit", DAILY_LIMIT)
    if g_limit > 0 and not is_admin:
        today = date.today().isoformat()
        day, cnt = _daily_count.get(group_id, ("", 0))
        if day != today:
            cnt = 0
        if cnt >= g_limit:
            await nai_cmd.finish(f"今日本群已使用 {g_limit} 次，明天再来吧。")
            return
        _daily_count[group_id] = (today, cnt + 1)

    _cooldown[group_id] = now

    # 翻译提示词
    translated = await _nai_translate_prompt(prompt)

    # 提取参考图
    ref_bytes = await _extract_ref_image(bot, event)

    # 翻译+生成提示合并为一条消息
    if translated != prompt:
        await bot.send(event, f"已翻译为：{translated}\n正在生成，请稍候…")
    else:
        await bot.send(event, "正在生成，请稍候…")

    # 构建请求参数
    params: dict = {
        "width": WIDTH,
        "height": HEIGHT,
        "steps": STEPS,
        "scale": SCALE,
        "sampler": SAMPLER,
        "seed": 0,
        "n_samples": 1,
        "negative_prompt": NEGATIVE_PROMPT,
        "qualityToggle": True,
        "ucPreset": 0,
        "sm": False,
        "sm_dyn": False,
        "params_version": 3,
        "autoSmea": False,
        "dynamic_thresholding": False,
        "cfg_rescale": 0,
        "noise_schedule": "karras",
        "legacy": False,
        "legacy_v3_extend": False,
        "add_original_image": True,
        "use_coords": False,
        "legacy_uc": False,
        "prefer_brownian": True,
        "characterPrompts": [],
        "inpaintImg2ImgStrength": 1,
        "deliberate_euler_ancestral_bug": False,
        "image_format": "png",
        # V4+ 模型必须传 v4_prompt 对象格式，否则返回 HTTP 500
        "v4_prompt": {
            "caption": {
                "base_caption": f"{PROMPT_PREFIX}, {translated}",
                "char_captions": [],
            },
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": NEGATIVE_PROMPT,
                "char_captions": [],
            },
            "legacy_uc": False,
        },
    }

    ref_png: bytes | None = None
    if ref_bytes:
        ref_png = _to_precise_ref_png(ref_bytes)
        cache_key = hashlib.sha256(ref_png).hexdigest()
        field_name = "director_ref_0"
        params["director_reference_images_cached"] = [{
            "cache_secret_key": cache_key,
            "data": field_name,
        }]
        params["director_reference_descriptions"] = [{
            "caption": {"base_caption": "character&style", "char_captions": []},
            "legacy_uc": False,
        }]
        params["director_reference_strength_values"] = [REF_STRENGTH]
        params["director_reference_secondary_strength_values"] = [0]
        params["director_reference_information_extracted"] = [REF_INFO_EXTRACTED]
        params["normalize_reference_strength_multiple"] = True

    json_body = {
        "action": "generate",
        "input": translated,
        "model": MODEL,
        "parameters": params,
    }

    proxy = PROXY or None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        resp = None
        last_err: Exception | None = None
        for _attempt in range(1, 4):  # ConnectError 时最多重试 3 次（代理连接不稳定）
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, verify=False) as client:
                    if ref_png is not None:
                        # Precise Reference 需要 multipart/form-data
                        files = {
                            "director_ref_0": ("director_ref_0", ref_png, "image/png"),
                            "request": ("blob", json.dumps(json_body), "application/json"),
                        }
                        resp = await client.post(NAI_ENDPOINT, headers=headers, files=files)
                    else:
                        resp = await client.post(
                            NAI_ENDPOINT,
                            headers={**headers, "Content-Type": "application/json"},
                            json=json_body,
                        )
                # 5xx Cloudflare 错误（520/521/522/524）静默重试
                if resp is not None and resp.status_code in (400, 520, 521, 522, 524) and _attempt < 3:
                    logger.warning(f"[NAI] HTTP {resp.status_code}，第 {_attempt} 次重试...")
                    await asyncio.sleep(3)
                    continue
                break  # 成功或不可重试状态码则跳出
            except (httpx.ConnectError, httpx.RemoteProtocolError) as ce:
                last_err = ce
                logger.warning(f"[NAI] 连接失败（第 {_attempt} 次）: {ce}，{'重试中...' if _attempt < 3 else '已达上限'}")
                if _attempt < 3:
                    await asyncio.sleep(3)
        if resp is None:
            raise last_err  # 3 次全部失败，抛出异常由下方 except 处理

        if resp.status_code == 401:
            await nai_cmd.finish("NAI token 无效或已过期，请联系管理员。")
            return
        if resp.status_code == 402:
            await nai_cmd.finish("NAI 账户额度不足，请联系管理员充值。")
            return
        if resp.status_code != 200:
            logger.error(f"[NAI] HTTP {resp.status_code}: {resp.text[:500]}")
            await nai_cmd.finish(f"生成失败（HTTP {resp.status_code}），请稍后再试。")
            return

        # 响应可能是 ZIP 或 msgpack 流，统一尝试解压 ZIP
        img_bytes: bytes | None = None
        content_type = resp.headers.get("content-type", "")
        if "zip" in content_type or resp.content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                img_bytes = zf.read(zf.namelist()[0])
        else:
            # msgpack 流：搜索 PNG 魔数字节
            raw = resp.content
            start = raw.find(b"\x89PNG")
            if start != -1:
                img_bytes = raw[start:]
        if not img_bytes:
            logger.error(f"[NAI] 无法解析响应，content-type={content_type}")
            await nai_cmd.finish("生成失败，回巨格式异常。")
            return

        try:
            await bot.send(event, MessageSegment.image(img_bytes))
        except ActionFailed as e:
            if e.retcode == 1200 and "Timeout" in (e.message or ""):
                logger.warning("[NAI] 图片发送已提交但 OneBot 回执超时，忽略错误避免重复报错")
                return
            raise

    except FinishedException:
        raise
    except httpx.TimeoutException:
        await nai_cmd.finish("生成超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"[NAI] 生成异常: {e}")
        await nai_cmd.finish("生成出错，请稍后再试。")


# ──────────────────────────────────────────
# #美少女化 指令
# ──────────────────────────────────────────

_BISHOUJO_PROMPT = (
    "1girl, young girl, beautiful girl, cute face, beautiful face, detailed eyes, sparkling eyes, "
    "female, feminine, girl, "
    "same hair color as reference, same hair style as reference, "
    "same outfit as reference, "
    "2d anime illustration, anime style, soft lighting, masterpiece, best quality"
)
_BISHOUJO_NEG = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, jpeg artifacts, "
    "signature, watermark, username, blurry, realistic, photo, 3d, ugly, gross, "
    "male, boy, man, masculine, beard, mustache, "
    "different hair color, different outfit, different hairstyle"
)

_bishoujo_cooldown: dict[int, float] = {}
_BISHOUJO_COOLDOWN = COOLDOWN  # 复用 nai 的冷却时间

bishoujo_cmd = on_command("#美少女化", priority=5, block=True)


async def _resolve_avatar(bot: Bot, event: GroupMessageEvent, args: Message) -> bytes | None:
    """按优先级取图：@某人头像 → 当前消息图片 → 回复消息图片 → 自己头像"""
    at_uid: int | None = None
    for seg in args:
        if seg.type == "at":
            try:
                at_uid = int(seg.data.get("qq", 0))
            except (ValueError, TypeError):
                pass
            break
    if at_uid is None:
        for seg in event.message:
            if seg.type == "at":
                try:
                    at_uid = int(seg.data.get("qq", 0))
                except (ValueError, TypeError):
                    pass
                break

    async with httpx.AsyncClient(timeout=15) as dl:
        if at_uid and at_uid != bot.self_id:
            try:
                r = await dl.get(f"https://q1.qlogo.cn/g?b=qq&nk={at_uid}&s=640")
                if r.status_code == 200:
                    return r.content
            except Exception as e:
                logger.warning(f"[bishoujo] 下载头像失败(uid={at_uid}): {e}")

        # 当前消息附图
        for seg in event.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file", "")
                if url and url.startswith("http"):
                    try:
                        r = await dl.get(url)
                        if r.status_code == 200:
                            return r.content
                    except Exception as e:
                        logger.warning(f"[bishoujo] 下载消息图片失败: {e}")
                break

        # 回复消息附图
        if event.reply:
            for seg in event.reply.message:
                if seg.type == "image":
                    url = seg.data.get("url") or seg.data.get("file", "")
                    if url and url.startswith("http"):
                        try:
                            r = await dl.get(url)
                            if r.status_code == 200:
                                return r.content
                        except Exception as e:
                            logger.warning(f"[bishoujo] 下载回复图片失败: {e}")
                    break

        # 自己头像
        try:
            r = await dl.get(f"https://q1.qlogo.cn/g?b=qq&nk={event.user_id}&s=640")
            if r.status_code == 200:
                return r.content
        except Exception as e:
            logger.warning(f"[bishoujo] 下载自己头像失败: {e}")

    return None


@bishoujo_cmd.handle()
async def handle_bishoujo(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    if group_id not in ALLOWED_GROUPS:
        return

    # 格式检查：直接看消息第一个文本段
    # 允许：第一段刚好是 "#美少女化"(无后续内容) 或 "#美少女化 "(后跟空格)
    # 拒绝：第一段为 "#美少女化" 但后跟了其他段（如无空格紧跟 @）
    _segs = list(event.message)
    _first_text = _segs[0].data.get("text", "") if _segs and _segs[0].type == "text" else ""
    _CMD = "#美少女化"
    _pure = len(_segs) == 1 and _first_text.strip() == _CMD
    _spaced = _first_text == _CMD + " " or _first_text.startswith(_CMD + " ")
    if not _pure and not _spaced:
        return  # 拒绝无空格紧跟任意内容的口令

    if not TOKEN:
        await bishoujo_cmd.finish("NAI token 未配置，请联系管理员。")
        return

    now = time.time()
    last = _bishoujo_cooldown.get(group_id, 0)
    remaining = _BISHOUJO_COOLDOWN - (now - last)
    if remaining > 0:
        await bishoujo_cmd.finish(f"冷却中，请 {remaining:.0f} 秒后再试。")
        return

    img_bytes = await _resolve_avatar(bot, event, args)
    if img_bytes is None:
        await bishoujo_cmd.finish("获取图片失败，请 @某人、附图或回复图片后使用。")
        return

    _bishoujo_cooldown[group_id] = now
    await bot.send(event, "正在美少女化，请稍候…")

    ref_png = _to_precise_ref_png(img_bytes)
    cache_key = hashlib.sha256(ref_png).hexdigest()

    params: dict = {
        "width": WIDTH,
        "height": HEIGHT,
        "steps": STEPS,
        "scale": SCALE,
        "sampler": SAMPLER,
        "seed": random.randint(0, 2**32 - 1),
        "n_samples": 1,
        "negative_prompt": _BISHOUJO_NEG,
        "qualityToggle": True,
        "ucPreset": 0,
        "sm": False,
        "sm_dyn": False,
        "params_version": 3,
        "autoSmea": False,
        "dynamic_thresholding": False,
        "cfg_rescale": 0,
        "noise_schedule": "karras",
        "legacy": False,
        "legacy_v3_extend": False,
        "add_original_image": True,
        "use_coords": False,
        "legacy_uc": False,
        "prefer_brownian": True,
        "characterPrompts": [],
        "inpaintImg2ImgStrength": 1,
        "deliberate_euler_ancestral_bug": False,
        "image_format": "png",
        "v4_prompt": {
            "caption": {
                "base_caption": f"{PROMPT_PREFIX}, {_BISHOUJO_PROMPT}",
                "char_captions": [],
            },
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": _BISHOUJO_NEG,
                "char_captions": [],
            },
            "legacy_uc": False,
        },
        "director_reference_images_cached": [{
            "cache_secret_key": cache_key,
            "data": "director_ref_0",
        }],
        "director_reference_descriptions": [{
            "caption": {"base_caption": "character&style", "char_captions": []},
            "legacy_uc": False,
        }],
        "director_reference_strength_values": [1.0],
        "director_reference_secondary_strength_values": [0],
        "director_reference_information_extracted": [1.0],
        "normalize_reference_strength_multiple": True,
    }

    json_body = {
        "action": "generate",
        "input": _BISHOUJO_PROMPT,
        "model": MODEL,
        "parameters": params,
    }

    proxy = PROXY or None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        resp = None
        last_err: Exception | None = None
        for _attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy, verify=False) as client:
                    files = {
                        "director_ref_0": ("director_ref_0", ref_png, "image/png"),
                        "request": ("blob", json.dumps(json_body), "application/json"),
                    }
                    resp = await client.post(NAI_ENDPOINT, headers=headers, files=files)
                # 400/5xx Cloudflare 错误（520/521/522/524）静默重试
                if resp.status_code in (400, 520, 521, 522, 524) and _attempt < 3:
                    logger.warning(f"[bishoujo] HTTP {resp.status_code}，第 {_attempt} 次重试...")
                    await asyncio.sleep(3)
                    continue
                break
            except (httpx.ConnectError, httpx.RemoteProtocolError) as ce:
                last_err = ce
                logger.warning(f"[bishoujo] 连接异常（第 {_attempt} 次）: {ce}")
                if _attempt < 3:
                    await asyncio.sleep(3)
        if resp is None:
            raise last_err

        if resp.status_code == 401:
            await bishoujo_cmd.finish("NAI token 无效或已过期，请联系管理员。")
            return
        if resp.status_code == 402:
            await bishoujo_cmd.finish("NAI 账户额度不足，请联系管理员充值。")
            return
        if resp.status_code != 200:
            logger.error(f"[bishoujo] HTTP {resp.status_code}: {resp.text[:500]}")
            await bishoujo_cmd.finish("生成失败，请稍后再试。")
            return

        img_result: bytes | None = None
        content_type = resp.headers.get("content-type", "")
        if "zip" in content_type or resp.content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                img_result = zf.read(zf.namelist()[0])
        else:
            raw = resp.content
            start = raw.find(b"\x89PNG")
            if start != -1:
                img_result = raw[start:]

        if not img_result:
            logger.error(f"[bishoujo] 无法解析响应，content-type={content_type}")
            await bishoujo_cmd.finish("生成失败，响应格式异常。")
            return

        await bot.send(event, MessageSegment.image(img_result))

    except FinishedException:
        raise
    except httpx.TimeoutException:
        await bishoujo_cmd.finish("生成超时，请稍后再试。")
    except Exception as e:
        logger.exception(f"[bishoujo] 生成异常: {e}")
        await bishoujo_cmd.finish("生成出错，请稍后再试。")
