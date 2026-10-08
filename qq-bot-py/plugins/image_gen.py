"""图片生成插件。

``#图片生成`` / ``#图片编辑`` 使用 OpenAI Images 兼容接口；
``#生图`` 是独立的 NovelAI 插件（见 :mod:`plugins.nai`）。
"""
import base64
import io
import os
import time

import httpx
import yaml
from PIL import Image
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

img_gen_cfg = config.get("image_gen", {})


def _secret_from_config(raw: dict, default_env: str = "") -> str:
    """Resolve a secret without requiring it to be stored in config.yaml."""
    direct = str(raw.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(raw.get("api_key_env") or default_env).strip()
    if not env_name:
        return ""
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    # NoneBot loads .env for its own settings; read it as a fallback for this
    # module as well.  This keeps image requests consistent with Responses.
    try:
        from dotenv import dotenv_values

        return str(dotenv_values(".env").get(env_name) or "").strip()
    except Exception:
        return ""


# The image commands have their own provider settings so that legacy image
# utilities (#手办化/#高质量化) can remain on their existing endpoint.
_openai_image_cfg = img_gen_cfg.get("openai") or {}
if not isinstance(_openai_image_cfg, dict):
    _openai_image_cfg = {}
_openai_image_api_url = str(
    _openai_image_cfg.get("api_url")
    or img_gen_cfg.get("openai_api_url")
    or ""
).strip().rstrip("/")
if not _openai_image_cfg.get("api_url") and _openai_image_cfg.get("base_url"):
    # A bare OpenAI base URL conventionally exposes its REST resources below
    # /v1.  An explicit api_url always wins for relays with another layout.
    _openai_image_api_url = str(_openai_image_cfg["base_url"]).strip().rstrip("/")
    if not _openai_image_api_url.endswith("/v1"):
        _openai_image_api_url += "/v1"
OPENAI_IMAGE_API_URL = _openai_image_api_url
OPENAI_IMAGE_API_KEY = _secret_from_config(
    _openai_image_cfg,
    default_env=str(
        _openai_image_cfg.get("api_key_env")
        or img_gen_cfg.get("openai_api_key_env")
        or "OPENAI_RESPONSES_API_KEY"
    ),
)
OPENAI_IMAGE_MODEL = str(
    _openai_image_cfg.get("model")
    or img_gen_cfg.get("openai_model")
    or "gpt-image-2.5-sunburst"
).strip()
OPENAI_IMAGE_EDIT_MODEL = str(
    _openai_image_cfg.get("edit_model") or img_gen_cfg.get("openai_edit_model") or OPENAI_IMAGE_MODEL
).strip()
OPENAI_IMAGE_FALLBACK_MODEL = str(
    _openai_image_cfg.get("fallback_model", "gpt-image-2") or ""
).strip()
OPENAI_IMAGE_EDIT_FALLBACK_MODEL = str(
    _openai_image_cfg.get("fallback_edit_model", OPENAI_IMAGE_FALLBACK_MODEL) or ""
).strip()

# Existing endpoint used by the other image utilities below.  Keep these
# names for backwards-compatible configuration and behaviour.
API_URL = img_gen_cfg.get("api_url", "")
API_KEY = img_gen_cfg.get("api_key", "")
MODEL = img_gen_cfg.get("model", "gpt-image-2")
SIZE = img_gen_cfg.get("size", "1024x1024")
QUALITY = img_gen_cfg.get("quality", "low")
COOLDOWN = img_gen_cfg.get("cooldown", 60)
TIMEOUT = img_gen_cfg.get("timeout", 120)
STYLE_SUFFIX = img_gen_cfg.get("style_suffix", "")
ALLOWED_GROUPS: set[int] = set(img_gen_cfg.get("allowed_groups", []))


def _to_png(data: bytes, size: int = 256) -> bytes:
    """将任意格式图片转为 RGBA PNG，裁正方形后 resize 到指定边长，并最大压缩。
    gpt-image-2 edits 接口要求真实 PNG 且文件 <4MB；256px RGBA PNG 约 80KB，减少上传量。
    """
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    # 裁成正方形
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    return buf.getvalue()


FIGURE_COOLDOWN = img_gen_cfg.get("figure_cooldown", 60)
ENHANCE_COOLDOWN = img_gen_cfg.get("enhance_cooldown", 60)
ENHANCE_TIMEOUT = img_gen_cfg.get("enhance_timeout", 200)

# 群冷却记录 {group_id: last_call_timestamp}
_cooldown: dict[int, float] = {}
_figure_cooldown: dict[int, float] = {}   # 改为按用户 {user_id: timestamp}
_enhance_cooldown: dict[int, float] = {}


def _is_review_rejected(response: httpx.Response) -> bool:
    """判断 API 响应是否为 AI 审核拒绝（而非配额不足、服务异常等其他错误）"""
    if response.status_code not in (400, 403, 451):
        return False
    try:
        data = response.json()
        err = data.get("error", {})
        combined = " ".join([
            str(err.get("type", "")),
            str(err.get("message", "")),
            str(err.get("code", "")),
        ]).lower()
        keywords = ("content_filter", "审核", "reject", "moderat", "policy", "safety", "violation")
        return any(k in combined for k in keywords)
    except Exception:
        return False


async def _image_item_bytes(
    client: httpx.AsyncClient,
    item: dict,
    *,
    api_key: str = "",
) -> bytes | None:
    """Decode either b64_json or url returned by an OpenAI image endpoint."""
    encoded = item.get("b64_json") or item.get("base64")
    if encoded:
        try:
            # A few relays return a complete data URL instead of bare base64.
            if isinstance(encoded, str) and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            return base64.b64decode(encoded)
        except (ValueError, TypeError, IndexError) as exc:
            logger.warning(f"[image_gen] 无法解码 b64_json: {type(exc).__name__}")
            return None

    url = str(item.get("url") or "").strip()
    if not url:
        return None
    if url.startswith("data:"):
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except (ValueError, TypeError, IndexError):
            return None
    if not url.startswith(("http://", "https://")):
        return None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.content


async def _image_response_bytes(
    client: httpx.AsyncClient,
    payload: dict,
    *,
    api_key: str = "",
) -> bytes | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("data") or payload.get("images") or []
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        image_bytes = await _image_item_bytes(client, item, api_key=api_key)
        if not image_bytes:
            continue
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except (OSError, ValueError, SyntaxError):
            logger.warning("[image_gen] 返回内容不是有效图片")
            continue
        return image_bytes
    return None


class _NoImageError(RuntimeError):
    """The image endpoint returned no usable image."""


async def _request_openai_image(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    group_id: int,
    png_bytes: bytes | None = None,
) -> bytes:
    is_edit = png_bytes is not None
    primary = OPENAI_IMAGE_EDIT_MODEL if is_edit else OPENAI_IMAGE_MODEL
    fallback = OPENAI_IMAGE_EDIT_FALLBACK_MODEL if is_edit else OPENAI_IMAGE_FALLBACK_MODEL
    models = [primary]
    if fallback and fallback != primary:
        models.append(fallback)
    operation = "edits" if is_edit else "generations"
    for attempt, model in enumerate(models, 1):
        logger.info(
            f"[image_gen] 图片请求 | group={group_id} operation={operation} "
            f"model={model} attempt={attempt}/{len(models)}"
        )
        try:
            headers = {"Authorization": f"Bearer {OPENAI_IMAGE_API_KEY}"}
            if is_edit:
                # Rebuild multipart data so the fallback receives the full source image.
                response = await client.post(
                    f"{OPENAI_IMAGE_API_URL}/images/edits",
                    headers=headers,
                    data={"model": model, "prompt": prompt, "n": "1", "size": SIZE},
                    files={"image": ("image.png", io.BytesIO(png_bytes), "image/png")},
                )
            else:
                response = await client.post(
                    f"{OPENAI_IMAGE_API_URL}/images/generations",
                    headers=headers,
                    json={"model": model, "prompt": prompt, "n": 1,
                          "size": SIZE, "quality": QUALITY},
                )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise _NoImageError("Image endpoint returned invalid JSON") from exc
            result = await _image_response_bytes(client, payload, api_key=OPENAI_IMAGE_API_KEY)
            if not result:
                raise _NoImageError("Image endpoint returned no usable image")
            logger.info(f"[image_gen] 图片已生成 | group={group_id} model={model}")
            return result
        except (httpx.TimeoutException, httpx.HTTPStatusError, _NoImageError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                if exc.response.status_code not in (502, 503, 504):
                    raise
                reason = f"HTTP {exc.response.status_code}"
            else:
                reason = type(exc).__name__
            if attempt == len(models):
                raise
            logger.warning(
                f"[image_gen] 首次请求失败，使用兜底模型进行最后一次尝试 | "
                f"group={group_id} {model} -> {fallback} reason={reason}"
            )
    raise _NoImageError("No image model configured")

image_gen_cmd = on_command("#图片生成", aliases={"#生成图片"}, priority=5, block=True)


@image_gen_cmd.handle()
async def handle_image_gen(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    # 群白名单检查（空列表=全部允许）
    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        return

    prompt = args.extract_plain_text().strip()
    if not prompt:
        return
    if not OPENAI_IMAGE_API_KEY or not OPENAI_IMAGE_API_URL or not OPENAI_IMAGE_MODEL:
        logger.error(
            "[image_gen] OpenAI Images API key is not configured; "
            "set the image_gen.openai.api_key_env variable in .env"
        )
        await bot.send(event, "图片生成尚未配置，请管理员设置 image_gen.openai 的 api_url、api_key 和 model。")
        return

    # 检查是否 @某人 → 获取其头像作为参考图，走 edits 端点
    at_uid: int | None = None
    for seg in args:
        if seg.type == "at":
            try:
                uid = int(seg.data.get("qq", 0))
                if uid and uid != int(bot.self_id):
                    at_uid = uid
            except (ValueError, TypeError):
                pass
            break
    if at_uid is None:
        for seg in event.message:
            if seg.type == "at":
                try:
                    uid = int(seg.data.get("qq", 0))
                    if uid and uid != int(bot.self_id):
                        at_uid = uid
                except (ValueError, TypeError):
                    pass
                break

    # 冷却检查
    now = time.time()
    if group_id in _cooldown and now - _cooldown[group_id] < COOLDOWN:
        return

    _cooldown[group_id] = now

    await bot.send(event, "正在生成图片，请稍候…")

    full_prompt = f"{prompt}{STYLE_SUFFIX}" if STYLE_SUFFIX else prompt
    t_start = time.time()
    logger.info(
        f"[image_gen] 开始生成 | group={group_id} provider=openai-images "
        f"model={OPENAI_IMAGE_MODEL} endpoint={OPENAI_IMAGE_API_URL} "
        f"size={SIZE} quality={QUALITY} at_uid={at_uid} prompt={prompt[:50]!r}"
    )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
            png_bytes = None
            if at_uid:
                # 有 @：下载头像 → 走 edits 端点
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={at_uid}&s=640"
                avatar_resp = await client.get(avatar_url)
                avatar_resp.raise_for_status()
                png_bytes = _to_png(avatar_resp.content)
            img_bytes = await _request_openai_image(
                client, full_prompt, group_id=group_id, png_bytes=png_bytes
            )
        elapsed = time.time() - t_start
        # Keep the result and timing in one QQ message so the duration stays
        # attached to the image instead of becoming a separate follow-up.
        await bot.send(
            event,
            Message([
                MessageSegment.image(img_bytes),
                MessageSegment.text(f"\n耗时 {elapsed:.1f}s"),
            ]),
        )
        logger.info(f"[image_gen] 生成成功 | group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r}")

    except httpx.TimeoutException:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.warning(
            f"[image_gen] 请求超时（>{TIMEOUT}s，已等待 {elapsed:.1f}s）| "
            f"group={group_id} model={OPENAI_IMAGE_MODEL} prompt={prompt[:50]!r} — 冷却已重置"
        )
        await bot.send(event, "图片生成超时，请稍后重试。")
    except _NoImageError:
        _cooldown.pop(group_id, None)
        logger.warning(f"[image_gen] 未返回可用图片 | group={group_id}")
        await bot.send(event, "图片生成失败：服务未返回可用图片，请稍后重试。")
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_gen] HTTP {e.response.status_code} 错误 | "
            f"group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r} | "
            f"响应: {e.response.text[:200]}"
        )
        if _is_review_rejected(e.response):
            await bot.send(event, "⚠️ 提示词未通过内容审核，请修改后重试。")
        else:
            await bot.send(event, f"图片生成失败（HTTP {e.response.status_code}），请稍后重试。")
    except Exception as e:
        elapsed = time.time() - t_start
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_gen] 生成异常 {type(e).__name__} | "
            f"group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r} | {e}"
        )


# ──────────────────────────────────────────
# #图片编辑 指令
# ──────────────────────────────────────────

image_edit_cmd = on_command("#图片编辑", aliases={"#编辑图片"}, priority=5, block=True)


def _extract_image_url(message: Message) -> str | None:
    """从消息段中提取第一张图片的 URL"""
    for seg in message:
        if seg.type == "image":
            return seg.data.get("url") or seg.data.get("file")
    return None


@image_edit_cmd.handle()
async def handle_image_edit(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        return
    if not OPENAI_IMAGE_API_KEY or not OPENAI_IMAGE_API_URL or not OPENAI_IMAGE_EDIT_MODEL:
        logger.error(
            "[image_edit] OpenAI Images API key is not configured; "
            "set the image_gen.openai.api_key_env variable in .env"
        )
        await bot.send(event, "图片编辑功能尚未配置 API Key，请联系管理员")
        return

    prompt = args.extract_plain_text().strip()

    # 优先 @某人 → 使用其头像；否则从消息/回复中找图片
    at_uid: int | None = None
    for seg in args:
        if seg.type == "at":
            try:
                uid = int(seg.data.get("qq", 0))
                if uid and uid != int(bot.self_id):
                    at_uid = uid
            except (ValueError, TypeError):
                pass
            break
    if at_uid is None:
        for seg in event.message:
            if seg.type == "at":
                try:
                    uid = int(seg.data.get("qq", 0))
                    if uid and uid != int(bot.self_id):
                        at_uid = uid
                except (ValueError, TypeError):
                    pass
                break

    if at_uid:
        img_url: str | None = f"https://q1.qlogo.cn/g?b=qq&nk={at_uid}&s=640"
    else:
        img_url = _extract_image_url(event.message)
        if not img_url and event.reply:
            img_url = _extract_image_url(event.reply.message)

    if not img_url:
        await bot.send(event, "请附带一张图片、回复一张图片，或 @某人 后使用此指令")
        return

    if not prompt:
        await bot.send(event, "请提供编辑描述，例：#图片编辑 换成赛博朋克风格")
        return

    # 冷却检查（和生成共用同一个冷却表）
    now = time.time()
    if group_id in _cooldown and now - _cooldown[group_id] < COOLDOWN:
        return

    _cooldown[group_id] = now
    await bot.send(event, "正在处理图片，请稍候…")

    t_start = time.time()
    logger.info(
        f"[image_edit] 开始编辑 | group={group_id} provider=openai-images "
        f"model={OPENAI_IMAGE_EDIT_MODEL} endpoint={OPENAI_IMAGE_API_URL} size={SIZE} "
        f"prompt={prompt[:50]!r} img_url={img_url[:80]}"
    )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
            img_resp = await client.get(img_url)
            img_resp.raise_for_status()
            png_bytes = _to_png(img_resp.content)
            full_prompt = f"{prompt}{STYLE_SUFFIX}" if STYLE_SUFFIX else prompt
            result_bytes = await _request_openai_image(
                client, full_prompt, group_id=group_id, png_bytes=png_bytes
            )
        elapsed = time.time() - t_start
        await bot.send(
            event,
            Message([
                MessageSegment.image(result_bytes),
                MessageSegment.text(f"\n耗时 {elapsed:.1f}s"),
            ]),
        )
        logger.info(f"[image_edit] 编辑成功 | group={group_id} 耗时={elapsed:.1f}s prompt={prompt[:50]!r}")
    except httpx.TimeoutException:
        _cooldown.pop(group_id, None)
        logger.warning(f"[image_edit] 请求超时 | group={group_id} 耗时={time.time() - t_start:.1f}s")
        await bot.send(event, "图片编辑超时，请稍后重试。")
    except _NoImageError:
        _cooldown.pop(group_id, None)
        logger.warning(f"[image_edit] 未返回可用图片 | group={group_id}")
        await bot.send(event, "图片编辑失败：服务未返回可用图片，请稍后重试。")
    except httpx.HTTPStatusError as e:
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_edit] HTTP {e.response.status_code} 错误 | "
            f"group={group_id} 耗时={time.time() - t_start:.1f}s | 响应: {e.response.text[:200]}"
        )
        if _is_review_rejected(e.response):
            await bot.send(event, "⚠️ 提示词未通过内容审核，请修改后重试。")
        else:
            await bot.send(event, f"图片编辑失败（HTTP {e.response.status_code}），请稍后重试。")
    except Exception as e:
        _cooldown.pop(group_id, None)
        logger.error(
            f"[image_edit] 异常 {type(e).__name__} | "
            f"group={group_id} 耗时={time.time() - t_start:.1f}s | {e}"
        )


# ──────────────────────────────────────────
# 公共图片来源解析 helper
# ──────────────────────────────────────────

async def _resolve_image(bot: Bot, event: GroupMessageEvent, args: Message) -> bytes | None:
    """按优先级获取图片字节：
    1. args/@某人 → 取该用户的 QQ 头像
    2. 当前消息附图
    3. 引用消息附图
    4. 兜底：发送者自己的 QQ 头像
    """
    # 1. 取 args 中第一个 @
    at_uid: int | None = None
    for seg in args:
        if seg.type == "at":
            try:
                at_uid = int(seg.data.get("qq", 0))
            except (ValueError, TypeError):
                pass
            break
    # 有时 @ 在消息正文里而不在 args 里
    if at_uid is None:
        for seg in event.message:
            if seg.type == "at":
                try:
                    at_uid = int(seg.data.get("qq", 0))
                except (ValueError, TypeError):
                    pass
                break

    async with httpx.AsyncClient(timeout=15) as dl:
        # 1. @某人 → 头像
        if at_uid and at_uid != bot.self_id:
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={at_uid}&s=640"
            try:
                r = await dl.get(avatar_url)
                if r.status_code == 200:
                    return r.content
            except Exception as e:
                logger.warning(f"[image_helper] 下载头像失败(uid={at_uid}): {e}")

        # 2. 当前消息附图
        img_url = _extract_image_url(event.message)
        if img_url:
            try:
                r = await dl.get(img_url)
                if r.status_code == 200:
                    return r.content
            except Exception as e:
                logger.warning(f"[image_helper] 下载当前消息图片失败: {e}")

        # 3. 引用消息附图
        if event.reply:
            img_url = _extract_image_url(event.reply.message)
            if img_url:
                try:
                    r = await dl.get(img_url)
                    if r.status_code == 200:
                        return r.content
                except Exception as e:
                    logger.warning(f"[image_helper] 下载引用消息图片失败: {e}")

        # 4. 发送者自己头像
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={event.user_id}&s=640"
        try:
            r = await dl.get(avatar_url)
            if r.status_code == 200:
                return r.content
        except Exception as e:
            logger.warning(f"[image_helper] 下载自己头像失败: {e}")

    return None


# ──────────────────────────────────────────
# #手办化 指令
# ──────────────────────────────────────────

FIGURE_PROMPT = (
    "Convert the character or person in this image into a high-quality anime collectible figure, showing the FULL BODY from head to toe. "
    "Automatically determine the style based on the source image: "
    "if the character is chibi, cartoon, or Q-style, create a Nendoroid-style Q版 chibi figure with full body visible, oversized head and small body standing pose; "
    "if the character is realistic, semi-realistic, or detailed, create a 1/7 scale figure style with full body, accurate proportions, detailed sculpt and paintwork. "
    "Full body must be visible, standing pose on a simple round base. "
    "Studio lighting, pure white background, product photography style, extremely detailed."
)

figure_cmd = on_command("#手办化", priority=5, block=True)


@figure_cmd.handle()
async def handle_figure(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        return

    now = time.time()
    user_id = event.user_id
    if user_id in _figure_cooldown and now - _figure_cooldown[user_id] < FIGURE_COOLDOWN:
        remaining = int(FIGURE_COOLDOWN - (now - _figure_cooldown[user_id]))
        await figure_cmd.finish(f"手办化冷却中，还需等待 {remaining} 秒~")
        return

    img_bytes = await _resolve_image(bot, event, args)
    if img_bytes is None:
        logger.warning(f"[figure] 获取图片失败 | group={group_id} user={user_id}")
        return

    _figure_cooldown[user_id] = now
    await bot.send(event, "正在手办化，请稍候…")
    t_start = time.time()
    png_bytes = _to_png(img_bytes, 512)
    logger.info(f"[figure] 开始生成 | group={group_id} png_size={len(png_bytes)//1024}KB")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{API_URL}/images/edits",
                headers={"Authorization": f"Bearer {API_KEY}"},
                data={"model": MODEL, "prompt": FIGURE_PROMPT, "n": "1",
                      "size": SIZE, "response_format": "b64_json"},
                files={"image": ("image.png", io.BytesIO(png_bytes), "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data", [])
        if not items:
            logger.error(f"[figure] 返回data为空 | group={group_id} | 完整响应: {data}")
            await bot.send(event, "图片生成失败：服务器未返回图片数据")
            _figure_cooldown.pop(group_id, None)
            return
        b64 = items[0].get("b64_json") or ""
        if not b64:
            # 可能返回的是 url 而非 b64_json
            url = items[0].get("url", "")
            logger.error(f"[figure] b64_json为空 | group={group_id} | url={url} | item={items[0]}")
            await bot.send(event, "图片生成失败：返回格式异常")
            _figure_cooldown.pop(group_id, None)
            return
        result_bytes = base64.b64decode(b64)
        elapsed = time.time() - t_start
        await bot.send(event, MessageSegment.image(result_bytes))
        await bot.send(event, f"生成完成（{elapsed:.1f}s）")
        logger.info(f"[figure] 生成成功 | group={group_id} 耗时={elapsed:.1f}s")

    except httpx.TimeoutException:
        elapsed = time.time() - t_start
        _figure_cooldown.pop(user_id, None)
        logger.warning(f"[figure] 超时 | group={group_id} 耗时={elapsed:.1f}s")
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t_start
        _figure_cooldown.pop(user_id, None)
        logger.error(f"[figure] HTTP错误 {e.response.status_code} | group={group_id} | {e.response.text}")
        if _is_review_rejected(e.response):
            await bot.send(event, "⚠️ 图片未通过内容审核，请换一张图片重试。")
        else:
            await bot.send(event, f"手办化失败（HTTP {e.response.status_code}），请稍后重试。")
    except Exception as e:
        elapsed = time.time() - t_start
        _figure_cooldown.pop(user_id, None)
        logger.error(f"[figure] 异常 {type(e).__name__} | group={group_id} 耗时={elapsed:.1f}s | {e}")


# ──────────────────────────────────────────
# #高质量化 指令
# ──────────────────────────────────────────

ENHANCE_PROMPT = (
    "Enhance and upscale this image to maximum quality: increase sharpness, clarity, and detail. "
    "Fix noise, blur, compression artifacts, and low resolution. "
    "Preserve the original composition, colors, and style faithfully. "
    "Output should look like a professionally retouched high-resolution version of the same image."
)

enhance_cmd = on_command("#高质量化", priority=5, block=True)


@enhance_cmd.handle()
async def handle_enhance(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        return

    now = time.time()
    if group_id in _enhance_cooldown and now - _enhance_cooldown[group_id] < ENHANCE_COOLDOWN:
        return

    img_bytes = await _resolve_image(bot, event, args)
    if img_bytes is None:
        logger.warning(f"[enhance] 获取图片失败 | group={group_id} user={event.user_id}")
        return

    _enhance_cooldown[group_id] = now
    await bot.send(event, "正在高质量化，请稍候…")
    t_start = time.time()
    png_bytes = _to_png(img_bytes, 512)
    logger.info(f"[enhance] 开始处理 | group={group_id} png_size={len(png_bytes)//1024}KB")

    try:
        async with httpx.AsyncClient(timeout=ENHANCE_TIMEOUT) as client:
            resp = await client.post(
                f"{API_URL}/images/edits",
                headers={"Authorization": f"Bearer {API_KEY}"},
                data={"model": MODEL, "prompt": ENHANCE_PROMPT, "n": "1",
                      "size": SIZE, "response_format": "b64_json"},
                files={"image": ("image.png", io.BytesIO(png_bytes), "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data", [])
        if not items or not items[0].get("b64_json"):
            logger.error(f"[enhance] 返回异常 | group={group_id} | 响应: {data}")
            _enhance_cooldown.pop(group_id, None)
            return
        result_bytes = base64.b64decode(items[0]["b64_json"])
        elapsed = time.time() - t_start
        await bot.send(event, MessageSegment.image(result_bytes))
        await bot.send(event, f"生成完成（{elapsed:.1f}s）")
        logger.info(f"[enhance] 处理成功 | group={group_id} 耗时={elapsed:.1f}s")

    except httpx.TimeoutException:
        elapsed = time.time() - t_start
        _enhance_cooldown.pop(group_id, None)
        logger.warning(f"[enhance] 超时 | group={group_id} 耗时={elapsed:.1f}s")
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t_start
        _enhance_cooldown.pop(group_id, None)
        logger.error(f"[enhance] HTTP错误 {e.response.status_code} | group={group_id} | {e.response.text}")
        if _is_review_rejected(e.response):
            await bot.send(event, "⚠️ 图片未通过内容审核，请换一张图片重试。")
        else:
            await bot.send(event, f"高质量化失败（HTTP {e.response.status_code}），请稍后重试。")
    except Exception as e:
        elapsed = time.time() - t_start
        _enhance_cooldown.pop(group_id, None)
        logger.error(f"[enhance] 异常 {type(e).__name__} | group={group_id} 耗时={elapsed:.1f}s | {e}")
