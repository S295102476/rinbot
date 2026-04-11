"""搜图插件 — #搜图 关键词 / #搜图 [图片]

关键词搜图: Lolicon API → Pixiv 插画
以图搜图: SauceNAO + ASCII2D 多引擎级联 + AI 兜底
"""
import asyncio
import base64
import re

import httpx
import yaml
from PicImageSearch import Network, SauceNAO
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

img_cfg = config.get("image_search", {})
LOLICON_API = img_cfg.get("lolicon_api", "https://api.lolicon.app/setu/v2")
SAUCENAO_KEY = img_cfg.get("saucenao_api_key", "")
PIXIV_PROXY = img_cfg.get("pixiv_proxy", "i.pixiv.re")
MAX_RESULTS = img_cfg.get("max_results", 3)
R18_FLAG = img_cfg.get("r18", 0)
PROXY = img_cfg.get("proxy", "") or None  # None 表示不使用代理

# AI Vision 配置 (兜底识图)
ai_cfg = config.get("ai", {})
vision_cfg = ai_cfg.get("vision", {})
VISION_API_URL = vision_cfg.get("api_url", "")
VISION_API_KEY = vision_cfg.get("api_key", "")
VISION_MODEL = vision_cfg.get("model", "qwen3-vl-plus")

# R18 过滤: 已知 NSFW 站点关键词
R18_SITES = {
    "gelbooru", "danbooru", "yande.re", "konachan",
    "e-hentai", "nhentai", "sankaku", "rule34", "tbib",
}

search_img_cmd = on_command("#搜图", priority=5, block=True)


def _is_r18_url(url: str) -> bool:
    """URL 是否指向已知 R18 站点"""
    url_lower = url.lower()
    return any(site in url_lower for site in R18_SITES)


# ━━━━━━━━━━━━━━━━ 入口 ━━━━━━━━━━━━━━━━

@search_img_cmd.handle()
async def handle_search_img(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    image_urls = [
        seg.data.get("url") or seg.data.get("file", "")
        for seg in args
        if seg.type == "image" and (seg.data.get("url") or seg.data.get("file", ""))
    ]

    if not image_urls and event.reply:
        for seg in event.reply.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file", "")
                if url:
                    image_urls.append(url)

    keyword = args.extract_plain_text().strip()

    if image_urls:
        await _search_by_image(bot, event, image_urls[0])
    elif keyword:
        await _search_by_keyword(bot, event, keyword)
    else:
        await search_img_cmd.send(
            "用法:\n#搜图 关键词 — 按关键词搜Pixiv插画\n#搜图 [图片] — 以图搜图(支持回复图片)"
        )


# ━━━━━━━━━━━━━━━━ 关键词搜图 ━━━━━━━━━━━━━━━━

async def _search_by_keyword(bot: Bot, event: GroupMessageEvent, keyword: str):
    """Lolicon API 关键词搜图"""
    await search_img_cmd.send(f"正在搜索「{keyword}」...")

    tags = [t.strip() for t in keyword.replace(",", " ").replace("，", " ").split() if t.strip()]
    payload = {
        "tag": tags,
        "num": MAX_RESULTS,
        "r18": R18_FLAG,
        "size": ["regular"],
        "proxy": PIXIV_PROXY,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(LOLICON_API, json=payload)
            data = resp.json()
    except Exception as e:
        await search_img_cmd.send(f"搜索失败: {e}")
        return

    results = data.get("data", [])
    if not results:
        await search_img_cmd.send(f"没有找到「{keyword}」相关的图片")
        return

    downloaded: list[tuple[dict, bytes | None]] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for item in results:
            img_url = item.get("urls", {}).get("regular", "")
            img_data = None
            if img_url:
                try:
                    img_resp = await client.get(img_url)
                    if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                        img_data = img_resp.content
                except Exception as e:
                    logger.warning(f"[image_search] 图片下载失败: {img_url} → {e}")
            downloaded.append((item, img_data))

    nodes = []
    for item, img_data in downloaded:
        title = item.get("title", "无标题")
        author = item.get("author", "未知")
        pid = item.get("pid", "")
        text = f"🖼 {title}\n👤 {author}\n🔗 PID: {pid}"
        node_content: list = [MessageSegment.text(text)]
        if img_data:
            b64 = base64.b64encode(img_data).decode()
            node_content.append(MessageSegment.image(f"base64://{b64}"))
        nodes.append({
            "type": "node",
            "data": {
                "name": "搜图结果",
                "uin": str(bot.self_id),
                "content": node_content,
            },
        })

    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.warning(f"[image_search] 合并转发失败: {e}, 尝试逐条发送")
        for item, img_data in downloaded[:2]:
            title = item.get("title", "无标题")
            pid = item.get("pid", "")
            msg = f"🖼 {title} (PID:{pid})"
            try:
                if img_data:
                    b64 = base64.b64encode(img_data).decode()
                    await search_img_cmd.send(
                        Message(MessageSegment.text(msg) + MessageSegment.image(f"base64://{b64}"))
                    )
                else:
                    await search_img_cmd.send(msg)
            except Exception as ex:
                logger.warning(f"[image_search] 逐条发送也失败: {ex}")


# ━━━━━━━━━━━━━━━━ 以图搜图: 多引擎级联 ━━━━━━━━━━━━━━━━

async def _search_by_image(bot: Bot, event: GroupMessageEvent, image_url: str):
    """SauceNAO → ASCII2D → AI 多引擎级联以图搜图"""
    await search_img_cmd.send("正在多引擎搜图中...")

    # 1. 下载原图
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as dl:
            img_resp = await dl.get(image_url)
            img_bytes = img_resp.content
    except Exception as e:
        await search_img_cmd.send(f"图片下载失败: {e}")
        return

    results: list[dict] = []
    engines_used: list[str] = []

    # 2. 并行执行 SauceNAO + ASCII2D
    saucenao_task = asyncio.create_task(_search_saucenao(img_bytes))
    ascii2d_task = asyncio.create_task(_search_ascii2d(image_url))
    saucenao_res, ascii2d_res = await asyncio.gather(saucenao_task, ascii2d_task, return_exceptions=True)

    if isinstance(saucenao_res, list) and saucenao_res:
        results.extend(saucenao_res)
        engines_used.append("SauceNAO")
    if isinstance(ascii2d_res, list) and ascii2d_res:
        results.extend(ascii2d_res)
        engines_used.append("Ascii2D")

    # 3. 去重
    results = _deduplicate(results)

    # 4. 没有结果 → AI 兜底
    if not results:
        ai_desc = await _ai_describe(image_url)
        if ai_desc:
            await search_img_cmd.send(f"🔍 未找到图源，AI 识图结果:\n{ai_desc}")
            return
        await search_img_cmd.send("多引擎搜索均未找到结果，请换张图试试~")
        return

    # 5. 构建并发送结果
    header = f"🔍 搜图结果 (引擎: {' + '.join(engines_used)})"
    await _send_results(bot, event, results, header)


# ────────── SauceNAO 引擎 ──────────

async def _search_saucenao(img_bytes: bytes) -> list[dict]:
    """SauceNAO 搜索, 返回结果列表"""
    if not SAUCENAO_KEY:
        return []
    try:
        async with Network(proxies=PROXY) as client:
            saucenao = SauceNAO(
                api_key=SAUCENAO_KEY,
                hide=3,       # 只返回安全内容
                numres=6,
                minsim=40,
                client=client,
            )
            resp = await saucenao.search(file=img_bytes)

        if not resp or not resp.raw:
            return []

        found = []
        for item in resp.raw:
            if item.hidden:
                continue
            if item.similarity < 55:
                continue
            url = item.url or ""
            if not url and item.ext_urls:
                url = item.ext_urls[0]
            if _is_r18_url(url):
                continue
            found.append({
                "engine": "SauceNAO",
                "similarity": f"{item.similarity:.1f}",
                "title": item.title or "",
                "author": item.author or "",
                "url": url,
                "thumbnail": item.thumbnail or "",
                "source": item.source or "",
            })
        return found
    except Exception as e:
        logger.warning(f"[image_search] SauceNAO 搜索失败: {e}")
        return []


# ────────── ASCII2D 引擎（URL 搜索，纯 GET 请求，绕过 Cloudflare POST 拦截） ──────────


def _sync_ascii2d_search(image_url: str, proxy: str | None) -> list[dict]:
    """同步 ASCII2D URL 搜索（在线程池中运行）"""
    import cloudscraper
    from urllib.parse import quote

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
    )
    if proxy:
        scraper.proxies = {"http": proxy, "https": proxy}

    # Step 0: GET 主页通过 Cloudflare
    home = scraper.get("https://ascii2d.net/", timeout=30)
    logger.info(f"[ascii2d] cloudscraper 主页状态: {home.status_code}")
    if home.status_code != 200:
        return []

    # Step 1: URL 搜索 (纯 GET, 不需要 POST 上传文件)
    search_url = f"https://ascii2d.net/search/url/{quote(image_url, safe='')}"
    resp = scraper.get(search_url, allow_redirects=True, timeout=30)
    color_url = resp.url
    logger.info(f"[ascii2d] 色合URL: {color_url}, 状态: {resp.status_code}")

    if resp.status_code != 200:
        logger.warning(f"[ascii2d] URL搜索失败, 状态: {resp.status_code}")
        return []

    # Step 2: 尝试切换到 bovw 特征搜索, 失败则用色合结果
    if "/search/color/" in color_url:
        bovw_url = color_url.replace("/search/color/", "/search/bovw/")
        resp2 = scraper.get(bovw_url, timeout=30)
        logger.info(f"[ascii2d] bovw状态: {resp2.status_code}")
        if resp2.status_code == 200:
            return _parse_ascii2d_html(resp2.text)
        # bovw 失败, 用色合结果
        logger.info("[ascii2d] bovw 不可用, 使用色合搜索结果")
        return _parse_ascii2d_html(resp.text)

    # 如果直接返回了结果页（没有 color 重定向），直接解析
    return _parse_ascii2d_html(resp.text)


async def _search_ascii2d(image_url: str) -> list[dict]:
    """Ascii2D URL 搜索 (cloudscraper, 在线程池中运行)"""
    try:
        return await asyncio.to_thread(_sync_ascii2d_search, image_url, PROXY)
    except Exception as e:
        logger.warning(f"[image_search] ASCII2D 搜索失败: {e}")
        return []


def _parse_ascii2d_html(html: str) -> list[dict]:
    """解析 ASCII2D 结果页 HTML"""
    try:
        from pyquery import PyQuery as pq
        doc = pq(html)
        found = []

        for item in doc("div.item-box").items():
            detail = item.find("div.detail-box")
            links = list(detail.find("h6 a").items())
            if not links:
                links = list(detail.find("a").items())

            url = links[0].attr("href") or "" if links else ""
            title = links[0].text() or "" if links else ""
            author = links[1].text() or "" if len(links) > 1 else ""
            author_url = links[1].attr("href") or "" if len(links) > 1 else ""

            if not url or _is_r18_url(url):
                continue

            src = item.find("img").attr("src") or ""
            thumbnail = ("https://ascii2d.net" + src if src.startswith("/") else src) if src else ""

            extra_urls = [author_url] if author_url and not _is_r18_url(author_url) else []

            found.append({
                "engine": "Ascii2D",
                "similarity": None,
                "title": title,
                "author": author,
                "url": url,
                "thumbnail": thumbnail,
                "source": "",
                "extra_urls": extra_urls,
            })

        return found[:5]
    except Exception as e:
        logger.warning(f"[ascii2d] HTML解析失败: {e}")
        return []


# ────────── AI 兜底识图 ──────────

async def _ai_describe(image_url: str) -> str:
    """Qwen-VL AI 兜底: 描述图片内容、识别角色/作品"""
    if not VISION_API_URL or not VISION_API_KEY:
        return ""
    try:
        headers = {
            "Authorization": f"Bearer {VISION_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {
                            "type": "text",
                            "text": (
                                "请简洁描述这张图片的内容，包括可能的来源、角色名称、作品名称等信息。"
                                "如果能识别出具体的动漫/游戏角色，请指出。控制在100字以内。"
                            ),
                        },
                    ],
                }
            ],
            "max_tokens": 200,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(VISION_API_URL, json=payload, headers=headers)
            data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    except Exception as e:
        logger.warning(f"[image_search] AI 识图失败: {e}")
        return ""


# ────────── 工具函数 ──────────

def _deduplicate(results: list[dict]) -> list[dict]:
    """按 URL 去重, 保留先出现的条目"""
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        url = r.get("url", "")
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        deduped.append(r)
    return deduped


async def _send_results(bot: Bot, event: GroupMessageEvent, results: list[dict], header: str):
    """构造合并转发消息并发送"""
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "搜图结果",
                "uin": str(bot.self_id),
                "content": [MessageSegment.text(header)],
            },
        }
    ]

    for r in results[:6]:
        lines: list[str] = []
        if r.get("similarity"):
            lines.append(f"📊 相似度: {r['similarity']}%")
        lines.append(f"🔎 引擎: {r['engine']}")
        if r.get("title"):
            lines.append(f"🖼 {r['title']}")
        if r.get("author"):
            lines.append(f"👤 {r['author']}")
        if r.get("url"):
            lines.append(f"🔗 {r['url']}")
        if r.get("source"):
            lines.append(f"📎 来源: {r['source']}")
        for extra in r.get("extra_urls", []):
            lines.append(f"🌐 {extra}")

        text = "\n".join(lines)
        node_content: list = [MessageSegment.text(text)]

        thumbnail = r.get("thumbnail", "")
        if thumbnail:
            thumb_data = await _download_thumbnail(thumbnail)
            if thumb_data:
                b64 = base64.b64encode(thumb_data).decode()
                node_content.append(MessageSegment.image(f"base64://{b64}"))
            else:
                node_content.append(MessageSegment.image(thumbnail))

        nodes.append({
            "type": "node",
            "data": {
                "name": "搜图结果",
                "uin": str(bot.self_id),
                "content": node_content,
            },
        })

    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.warning(f"[image_search] 合并转发失败: {e}, 尝试逐条发送")
        for r in results[:3]:
            title = r.get("title") or "未知"
            url = r.get("url", "")
            sim = f" {r['similarity']}%" if r.get("similarity") else ""
            msg = f"[{r['engine']}]{sim} {title}\n{url}"
            try:
                await search_img_cmd.send(msg)
            except Exception as ex:
                logger.warning(f"[image_search] 逐条发送失败: {ex}")


async def _download_thumbnail(url: str) -> bytes | None:
    """下载缩略图, 失败返回 None"""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
    except Exception:
        pass
    return None
