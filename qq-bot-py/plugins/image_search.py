"""搜图插件 — #搜图 关键词 / #搜图 [图片]

关键词搜图: Lolicon API → Pixiv 插画
以图搜图: SauceNAO + ASCII2D 多引擎级联 + AI 兜底
"""
import asyncio
import base64
import io
import re
import uuid
from datetime import timedelta

import httpx
import yaml
from minio import Minio
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
FLARESOLVERR = img_cfg.get("flaresolverr", "")  # FlareSolverr 地址，用于绕过 Cloudflare

# MinIO 客户端（用于生成临时预签名 URL 供 ASCII2D 下载）
_minio_cfg = config["meme"]["minio"]
_minio_client = Minio(
    _minio_cfg["endpoint"],
    access_key=_minio_cfg["access_key"],
    secret_key=_minio_cfg["secret_key"],
    secure=_minio_cfg.get("secure", False),
)
_SEARCH_BUCKET = "search-temp"  # 临时搜图 bucket，对象 60 秒后可删


def _upload_and_presign(img_bytes: bytes) -> str:
    """把图片上传到 MinIO，返回 60 秒有效的预签名 URL（外网可访问）。"""
    if not _minio_client.bucket_exists(_SEARCH_BUCKET):
        _minio_client.make_bucket(_SEARCH_BUCKET)
    obj_name = f"tmp/{uuid.uuid4().hex}.jpg"
    _minio_client.put_object(
        _SEARCH_BUCKET, obj_name,
        io.BytesIO(img_bytes), len(img_bytes),
        content_type="image/jpeg",
    )
    url = _minio_client.presigned_get_object(
        _SEARCH_BUCKET, obj_name, expires=timedelta(seconds=120)
    )
    return url


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
        # 先从 reply 消息段直接取
        for seg in event.reply.message:
            if seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file", "")
                if url:
                    image_urls.append(url)
        # 取不到时（别人的消息 NapCat 可能不内联图片），用 get_msg 重新拉取
        if not image_urls:
            try:
                msg_data = await bot.get_msg(message_id=event.reply.message_id)
                for seg in Message(msg_data.get("message", [])):
                    if seg.type == "image":
                        url = seg.data.get("url") or seg.data.get("file", "")
                        if url:
                            image_urls.append(url)
            except Exception as e:
                logger.warning(f"[image_search] get_msg 失败: {e}")

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
    await search_img_cmd.send("正在搜图中...请稍后")

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
    ascii2d_res = await _search_ascii2d(img_bytes)

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


# ────────── ASCII2D 引擎（真实 Google Chrome，绕过 Cloudflare WAF） ──────────

_CHROME_PATH = "/usr/bin/google-chrome"


async def _search_ascii2d(img_bytes: bytes) -> list[dict]:
    """ASCII2D 以图搜图（真实 Chrome + 虚拟显示器，绕过 CF JS 挑战）。

    headless=True 时 CF 的 JS 挑战无法自动通过，必须用有头模式。
    优先复用系统已有的 Xvfb（检测 /tmp/.X{N}-lock），避免僵尸进程和 display 冲突问题。
    若不存在则自行启动一个。
    """
    import os
    import tempfile
    import asyncio as _asyncio
    import subprocess
    from playwright.async_api import async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    # ── 1. 找到可用的虚拟显示器 ──
    xvfb_proc = None
    display = ""

    def _x_alive(n: int) -> bool:
        """测试 DISPLAY :N 背后是否真的有 X server 在监听"""
        import socket as _sock
        sock_path = f"/tmp/.X11-unix/X{n}"
        if not os.path.exists(sock_path):
            return False
        try:
            s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s.settimeout(1)
            s.connect(sock_path)
            s.close()
            return True
        except Exception:
            return False

    # 扫描 /tmp/.X11-unix/ 中的 socket，取第一个真正存活的
    x11_dir = "/tmp/.X11-unix"
    if os.path.isdir(x11_dir):
        for _sock_name in sorted(os.listdir(x11_dir), key=lambda s: int(s[1:]) if s[1:].isdigit() else 9999):
            if _sock_name.startswith("X") and _sock_name[1:].isdigit():
                _n = int(_sock_name[1:])
                if _x_alive(_n):
                    display = f":{_n}"
                    logger.info(f"[ascii2d] 复用已有虚拟显示: {display}")
                    break
                else:
                    logger.debug(f"[ascii2d] 跳过死 socket: :{_n}")

    if not display:
        # 没有可用 X server，自行启动一个 Xvfb
        for _n in range(20, 200):
            if not os.path.exists(f"/tmp/.X11-unix/X{_n}") and not os.path.exists(f"/tmp/.X{_n}-lock"):
                try:
                    xvfb_proc = subprocess.Popen(
                        ["Xvfb", f":{_n}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    await _asyncio.sleep(1.5)
                    if xvfb_proc.poll() is not None:
                        xvfb_proc = None
                        continue
                    if not _x_alive(_n):
                        xvfb_proc.kill()
                        xvfb_proc = None
                        continue
                    display = f":{_n}"
                    logger.info(f"[ascii2d] 新建虚拟显示: {display}")
                    break
                except FileNotFoundError:
                    logger.warning("[ascii2d] Xvfb 未安装，请运行: sudo apt-get install -y xvfb")
                    return []

    if not display:
        logger.warning("[ascii2d] 无法获取虚拟显示器，搜索中止")
        return []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=_CHROME_PATH,
                headless=False,  # 必须有头模式，CF JS 挑战在无头 Chrome 下无法自动通过
                env={**os.environ, "DISPLAY": display},
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1920,1080",
                    "--start-maximized",
                ],
                ignore_default_args=["--enable-automation"],
                proxy={"server": PROXY} if PROXY else None,
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()
            try:
                await page.goto("https://ascii2d.net", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeout:
                    pass
                title_home = await page.title()
                logger.info(f"[ascii2d] 首页加载完成: {page.url} title={title_home!r}")

                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name

                async def _submit() -> None:
                    fi = page.locator('#file_upload input[type="file"]')
                    sb = page.locator('#file_upload button[type="submit"]')
                    await fi.set_input_files(tmp_path)
                    await sb.click()

                async def _wait_result(timeout_s: int) -> str:
                    """
                    轮询等待，返回：
                    - 'color'  : 到达 /search/color/（成功）
                    - 'form'   : CF 挑战通过后重定向回表单 GET /search/file（body 丢失，需重提交）
                    - 'timeout': 超时
                    """
                    cf_seen = False
                    start = _asyncio.get_event_loop().time()
                    while _asyncio.get_event_loop().time() - start < timeout_s:
                        await _asyncio.sleep(0.5)
                        url = page.url
                        if "/search/color/" in url:
                            return "color"
                        try:
                            title = await page.title()
                        except Exception:
                            title = ""
                        if "Just a moment" in title or "Cloudflare" in title:
                            cf_seen = True
                        elif cf_seen and "ascii2d.net" in url:
                            # CF 挑战曾出现且已消失，重定向回了 ascii2d 页面
                            return "form"
                    return "timeout"

                try:
                    logger.info("[ascii2d] 第一次提交...")
                    await _submit()
                    state = await _wait_result(60)
                    logger.info(f"[ascii2d] 第一次结果: state={state!r}, url={page.url}")

                    if state == "timeout":
                        try:
                            _title = await page.title()
                            _body = (await page.content())[:500]
                        except Exception:
                            _title, _body = "N/A", "N/A"
                        logger.warning(f"[ascii2d] 第一次超时: title={_title!r}")
                        logger.warning(f"[ascii2d] 页面内容: {_body}")
                        return []

                    if state == "form":
                        # CF 挑战通过，有两种情况：
                        # 1. CF 自动重放 POST → 会很快跳到 /search/color/（URL 含 __cf_chl_tk 时常见）
                        # 2. CF 重放但 body 丢失 → URL 回到 /search/file（无 token），需要主动重提交
                        if "__cf_chl_tk" in page.url:
                            logger.info("[ascii2d] CF挑战通过且正在重放 POST，等待跳转...")
                            state = await _wait_result(60)  # CF 重放可能较慢，给足时间
                            logger.info(f"[ascii2d] 重放跳转结果: state={state!r}, url={page.url}")
                        else:
                            # body 丢失，cf_clearance 已缓存，直接重新提交
                            logger.info("[ascii2d] CF通过但 POST body 丢失，重新提交...")
                            await _submit()
                            state = await _wait_result(60)
                            logger.info(f"[ascii2d] 重新提交结果: state={state!r}, url={page.url}")

                    if state != "color":
                        try:
                            _title = await page.title()
                            _body = (await page.content())[:500]
                        except Exception:
                            _title, _body = "N/A", "N/A"
                        logger.warning(f"[ascii2d] 最终失败: state={state!r} title={_title!r}")
                        logger.warning(f"[ascii2d] 页面内容: {_body}")
                        return []
                finally:
                    os.unlink(tmp_path)

                color_url = page.url
                logger.info(f"[ascii2d] 色合搜索URL: {color_url}")

                bovw_url = color_url.replace("/search/color/", "/search/bovw/")
                if bovw_url != color_url:
                    await page.goto(bovw_url, timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeout:
                        pass
                    logger.info("[ascii2d] 已切换到 bovw 特征搜索")

                html = await page.content()
                results = _parse_ascii2d_html(html)

                # 用浏览器上下文下载缩略图（携带 cf_clearance cookie，普通 httpx 会被 CF 拒绝）
                for item in results:
                    thumb_url = item.get("thumbnail", "")
                    if thumb_url:
                        for _attempt in range(3):
                            try:
                                resp = await context.request.get(thumb_url)
                                if resp.ok:
                                    item["thumbnail_bytes"] = await resp.body()
                                    break
                                logger.warning(f"[ascii2d] 缩略图 HTTP {resp.status}: {thumb_url}")
                                break  # 非网络错误，不重试
                            except Exception as e:
                                if _attempt < 2:
                                    await _asyncio.sleep(1)
                                else:
                                    logger.warning(f"[ascii2d] 缩略图下载失败(重试{_attempt+1}次): {e}")

                return results

            except PlaywrightTimeout as e:
                logger.warning(f"[ascii2d] Playwright 超时: {e}")
                return []
            except Exception as e:
                logger.warning(f"[ascii2d] Playwright 异常: {type(e).__name__}: {e}")
                return []
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"[ascii2d] 搜索失败: {type(e).__name__}: {e}")
        return []
    finally:
        if xvfb_proc is not None:
            xvfb_proc.terminate()
            try:
                xvfb_proc.wait(timeout=3)
            except Exception:
                xvfb_proc.kill()
            # 兜底：手动清理 socket 和 lock（SIGKILL 时进程来不及清理）
            try:
                _dn = display.lstrip(":")
                os.remove(f"/tmp/.X11-unix/X{_dn}")
            except OSError:
                pass
            try:
                os.remove(f"/tmp/.X{_dn}-lock")
            except OSError:
                pass


def _parse_ascii2d_html(html: str) -> list[dict]:
    """解析 ASCII2D 结果页 HTML"""
    from pyquery import PyQuery as pq
    try:
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

        return found[:2]
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
    """发送第一条搜图结果（普通消息，避免合并转发缩略图被 Forbidden）"""
    if not results:
        await search_img_cmd.send("未找到相关结果~")
        return

    r = results[0]
    lines: list[str] = []
    if r.get("title"):
        lines.append(f"发布日期：{r['title']}")
    if r.get("author"):
        lines.append(f"作者：{r['author']}")
    if r.get("url"):
        lines.append(r["url"])
    if r.get("source"):
        lines.append(r["source"])
    for extra in r.get("extra_urls", []):
        if extra != r.get("url"):
            lines.append(extra)

    text = "\n".join(lines)
    msg = Message(MessageSegment.text(text))

    thumbnail = r.get("thumbnail", "")
    thumb_data = r.get("thumbnail_bytes")
    if not thumb_data and thumbnail:
        thumb_data = await _download_thumbnail(thumbnail)
        if not thumb_data:
            logger.warning(f"[ascii2d] 缩略图下载失败: {thumbnail}")
    if thumb_data:
        b64 = base64.b64encode(thumb_data).decode()
        msg += MessageSegment.image(f"base64://{b64}")

    await search_img_cmd.send(msg)


async def _download_thumbnail(url: str) -> bytes | None:
    """下载缩略图, 失败返回 None"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://ascii2d.net/",
        }
        proxy = PROXY if PROXY else None
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers, proxy=proxy) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
            logger.warning(f"[ascii2d] 缩略图响应 {resp.status_code}: {url}")
    except Exception as e:
        logger.warning(f"[ascii2d] 缩略图下载异常: {e}")
    return None
