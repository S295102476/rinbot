"""#搜本子 — 以图搜漫画，调用 soutubot.moe API（NH/EH/Panda）

用法：
  #搜本子 [附图]       → 搜索当前消息中的图片
  #搜本子（回复含图）  → 搜索被回复消息中的图片
"""

import httpx
import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

_cfg = config.get("manga_search", {})
ALLOWED_GROUPS: set[int] = set(_cfg.get("allowed_groups", []))
THRESHOLD: float = float(_cfg.get("similarity_threshold", 60))
MAX_RESULTS: int = int(_cfg.get("max_results", 5))
PROXY: str = _cfg.get("proxy", "")

_SOURCE_BASE = {
    "nhentai": "https://nhentai.net",
    "ehentai": "https://e-hentai.org",
    "panda":   "https://exhentai.org",
    "jmcomic": "https://18comic.vip",
}
_SOURCE_NAME = {
    "nhentai": "nhentai",
    "ehentai": "E-Hentai",
    "panda":   "ExHentai",
    "jmcomic": "JMComic",
}
_LANG_NAME = {
    "cn": "中文",
    "gb": "英文",
    "jp": "日文",
}

manga_cmd = on_command("#搜本子", priority=5, block=True)


async def _search_via_playwright(image_bytes: bytes) -> list[dict]:
    import os
    import tempfile
    from urllib.parse import unquote as _unquote

    from playwright.async_api import async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            proxy={"server": PROXY} if PROXY else None,
        )
        context = await browser.new_context()

        # 拦截所有出站请求，记录 /api/ 的请求头（诊断 auth 机制）
        async def on_request(req):
            if "soutubot.moe" in req.url and "/api/" in req.url:
                hdrs = {k: v for k, v in req.headers.items()
                        if k.lower() not in ("cookie",)}  # cookie 太长，单独打
                cookie_hdr = req.headers.get("cookie", "")
                logger.info(
                    f"[manga_search] → {req.method} {req.url}\n"
                    f"  headers={hdrs}\n"
                    f"  cookie-names={[p.split('=')[0] for p in cookie_hdr.split('; ') if p]}"
                )

        context.on("request", on_request)

        page = await context.new_page()
        try:
            await page.goto("https://soutubot.moe", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeout:
                pass

            # 将图片写入临时文件，通过真实 <input type="file"> 上传
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name

            try:
                file_input = page.locator('input[type="file"]').first
                await file_input.set_input_files(tmp_path)

                # 等待页面发出搜索请求，超时 30 秒
                async with page.expect_response(
                    lambda r: "soutubot.moe/api/search" in r.url,
                    timeout=30000,
                ) as resp_info:
                    # 某些 UI 点击 input 后需要额外触发，先尝试等待自动触发
                    # 如果 30s 内没有请求，下面会 TimeoutError
                    pass
            finally:
                os.unlink(tmp_path)

            resp = await resp_info.value
            logger.info(f"[manga_search] UI 搜索 HTTP {resp.status}")
            if resp.ok:
                data = await resp.json()
                return data.get("data", []) if isinstance(data, dict) else []
            body = await resp.text()
            logger.warning(f"[manga_search] UI 搜索失败 {resp.status}: {body[:300]}")
            return []

        except PlaywrightTimeout as e:
            logger.warning(f"[manga_search] Playwright 超时（文件上传后无请求？）: {e}")
            return []
        except Exception as e:
            logger.warning(f"[manga_search] Playwright 异常: {e}")
            return []
        finally:
            await browser.close()


async def _extract_image(event: GroupMessageEvent) -> bytes | None:
    """从当前消息或被回复消息中提取第一张图片"""
    sources = list(event.message)
    if event.reply:
        sources = sources + list(event.reply.message)
    for seg in sources:
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file", "")
            if url and url.startswith("http"):
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        r = await client.get(url)
                    if r.status_code == 200:
                        return r.content
                except Exception as e:
                    logger.warning(f"[manga_search] 下载图片失败: {e}")
    return None


def _process_results(data: list[dict]) -> list[dict]:
    """过滤低相似度 → 按 title 去重（优先 cn，次看相似度）→ 排序（cn 优先，其余按相似度降序）"""
    # 1. 过滤低相似度
    data = [d for d in data if d.get("similarity", 0) >= THRESHOLD]

    # 2. 按 title 去重
    title_best: dict[str, dict] = {}
    for item in data:
        title = item.get("title", "").strip()
        if not title:
            continue
        existing = title_best.get(title)
        if existing is None:
            title_best[title] = item
        else:
            # cn 优先保留
            if item.get("language") == "cn" and existing.get("language") != "cn":
                title_best[title] = item
            # 同语言取更高相似度
            elif item.get("language") == existing.get("language"):
                if item.get("similarity", 0) > existing.get("similarity", 0):
                    title_best[title] = item

    # 3. 排序：cn 置顶（相似度降序），其余在后（相似度降序）
    results = list(title_best.values())
    results.sort(key=lambda x: (
        0 if x.get("language") == "cn" else 1,
        -x.get("similarity", 0),
    ))

    return results[:MAX_RESULTS]


@manga_cmd.handle()
async def handle_manga_search(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    logger.debug(f"[manga_search] 触发: group={group_id} PROXY={PROXY!r}")
    if ALLOWED_GROUPS and group_id not in ALLOWED_GROUPS:
        logger.debug(f"[manga_search] 群 {group_id} 不在白名单")
        return

    img_bytes = await _extract_image(event)
    logger.debug(f"[manga_search] 图片提取结果: {len(img_bytes) if img_bytes else None} bytes")
    if img_bytes is None:
        await manga_cmd.finish("请附上图片或回复含图消息后使用 #搜本子")
        return

    logger.debug("[manga_search] 开始 Playwright 搜索...")
    raw_data: list[dict] = await _search_via_playwright(img_bytes)
    if not raw_data:
        return  # 静默处理

    results = _process_results(raw_data)
    if not results:
        return  # 静默处理

    # 构建合并转发消息
    nodes = []
    for item in results:
        source = item.get("source", "")
        subject_path = item.get("subjectPath", "")
        base = _SOURCE_BASE.get(source, "")
        url = f"{base}{subject_path}" if base and subject_path else subject_path

        lang = _LANG_NAME.get(item.get("language", ""), item.get("language", ""))
        src_name = _SOURCE_NAME.get(source, source)
        sim = item.get("similarity", 0)
        title = item.get("title", "未知标题")

        text = (
            f"{title}\n"
            f"相似度: {sim:.1f}%  {lang}  {src_name}\n"
            f"{url}"
        )
        nodes.append({
            "type": "node",
            "data": {
                "name": "匿名用户",
                "uin": "10000",
                "content": [MessageSegment.text(text)],
            },
        })

    try:
        await bot.call_api(
            "send_group_forward_msg",
            group_id=group_id,
            messages=nodes,
        )
    except Exception:
        # 降级：逐条发送，异常也静默
        try:
            for node in nodes:
                await bot.send(event, node["data"]["content"][0])
        except Exception:
            pass  # 静默处理
