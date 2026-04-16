"""涩图插件 — #涩图 发送5张高质量二次元美图

画师库(75%) + Lolicon随机(25%)，合并转发发送。
画师库通过 pixivpy3 直连 Pixiv API 获取指定画师作品。
"""

import asyncio
import base64
import random
from datetime import datetime, timedelta, date

import httpx
import yaml
import redis as redis_lib
try:
    from pixivpy3 import AppPixivAPI
    HAS_PIXIVPY = True
except ImportError:
    HAS_PIXIVPY = False
from nonebot import on_command, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from sqlalchemy import select

from .db import Base, engine, get_session, SetuRecord, RankingCache

# ---------- 配置 ----------
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

setu_cfg = config.get("setu", {})
img_cfg = config.get("image_search", {})

CURATED_ARTISTS: list[int] = setu_cfg.get("curated_artists", [])
CURATED_RATE = setu_cfg.get("curated_rate", 0.75)
NUM = setu_cfg.get("num", 5)
REFRESH_TOKEN = setu_cfg.get("pixiv_refresh_token", "")
USER_DAILY_LIMIT = setu_cfg.get("user_daily_limit", 1)
GROUP_DAILY_LIMIT = setu_cfg.get("group_daily_limit", 3)
MIN_TAGS = setu_cfg.get("min_tags", 5)

ADMIN_USERS: set[int] = set(setu_cfg.get("admin_users", []))
PIXIV_API_PROXY = setu_cfg.get("pixiv_api_proxy", "") or None  # socks5://127.0.0.1:1080

LOLICON_API = img_cfg.get("lolicon_api", "https://api.lolicon.app/setu/v2")
PIXIV_PROXY = img_cfg.get("pixiv_proxy", "i.pixiv.re")
PROXY = img_cfg.get("proxy", "") or None

# R18 相关 tag（过滤用）
_R18_TAGS = {"R-18", "R-18G", "R18", "R18G"}

rds = redis_lib.Redis(
    host=config["redis"]["host"],
    port=config["redis"]["port"],
    decode_responses=True,
)

# ---------- Pixiv API 客户端 ----------
_papi = None  # AppPixivAPI | None
_papi_lock = asyncio.Lock()
_papi_auth_time: float = 0  # 上次鉴权时间戳
_PAPI_TOKEN_TTL = 2700  # 45 分钟刷新一次（token 有效期 3600s，留 buffer）


async def _get_pixiv_api():
    """懒初始化 Pixiv API，自动用 refresh_token 鉴权，token 过期自动刷新"""
    global _papi, _papi_auth_time
    if not HAS_PIXIVPY:
        logger.warning("[setu] pixivpy3 未安装，跳过画师库")
        return None
    if not REFRESH_TOKEN:
        logger.warning("[setu] 未配置 pixiv_refresh_token，跳过画师库")
        return None
    import time
    async with _papi_lock:
        now = time.time()
        # 已初始化但 token 过期 → 刷新
        if _papi is not None and (now - _papi_auth_time) > _PAPI_TOKEN_TTL:
            try:
                logger.info("[setu] Pixiv token 即将过期，主动刷新...")
                await asyncio.wait_for(
                    asyncio.to_thread(_papi.auth, refresh_token=REFRESH_TOKEN),
                    timeout=15,
                )
                _papi_auth_time = time.time()
                logger.info("[setu] Pixiv token 刷新成功")
            except Exception as e:
                logger.error(f"[setu] Pixiv token 刷新失败: {e}")
                _papi = None
                _papi_auth_time = 0
        # 未初始化 → 首次鉴权
        if _papi is None:
            try:
                api = AppPixivAPI()
                proxy = PIXIV_API_PROXY or PROXY
                if proxy:
                    socks_proxy = proxy.replace("socks5://", "socks5h://")
                    api.requests.proxies = {"https": socks_proxy, "http": socks_proxy}
                    logger.info(f"[setu] Pixiv 代理: {socks_proxy}")
                api.set_additional_headers({"Accept-Language": "zh-CN"})
                logger.info("[setu] 正在鉴权 Pixiv API...")
                await asyncio.wait_for(
                    asyncio.to_thread(api.auth, refresh_token=REFRESH_TOKEN),
                    timeout=15,
                )
                _papi = api
                _papi_auth_time = time.time()
                logger.info("[setu] Pixiv API 鉴权成功")
            except Exception as e:
                import traceback
                logger.error(f"[setu] Pixiv API 鉴权失败: {type(e).__name__}: {e}")
                logger.error(f"[setu] 异常详情:\n{traceback.format_exc()}")
                return None
    return _papi


async def _refresh_pixiv_auth():
    """强制刷新 access_token（API 返回异常时调用）"""
    global _papi, _papi_auth_time
    if _papi and REFRESH_TOKEN:
        try:
            await asyncio.to_thread(_papi.auth, refresh_token=REFRESH_TOKEN)
            import time
            _papi_auth_time = time.time()
            logger.info("[setu] Pixiv token 已强制刷新")
        except Exception as e:
            logger.error(f"[setu] Pixiv token 刷新失败: {e}")
            _papi = None
            _papi_auth_time = 0


# ---------- 建表 ----------
@get_driver().on_startup
async def _create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[setu] 数据表已就绪")
    # 后台预热 Pixiv API，不阻塞启动
    asyncio.create_task(_get_pixiv_api())


# ---------- 限流工具 ----------
def _user_key(user_id: int) -> str:
    return f"setu:user:{user_id}:{date.today().isoformat()}"


def _group_key(group_id: int) -> str:
    return f"setu:group:{group_id}:{date.today().isoformat()}"


def _check_limit(user_id: int, group_id: int) -> str | None:
    """检查限流，返回提示语或 None（可以继续）"""
    # 管理员跳过个人限额
    if user_id not in ADMIN_USERS:
        uk = _user_key(user_id)
        user_count = int(rds.get(uk) or 0)
        if user_count >= USER_DAILY_LIMIT:
            return "今天已经看过涩图了哦，注意身体~"

    gk = _group_key(group_id)
    group_count = int(rds.get(gk) or 0)
    if group_count >= GROUP_DAILY_LIMIT:
        return "涩图太多啦，休息一会吧！"

    return None


def _incr_limit(user_id: int, group_id: int):
    """消费一次限额"""
    ttl = 86400  # 一天

    uk = _user_key(user_id)
    rds.incr(uk)
    rds.expire(uk, ttl)

    gk = _group_key(group_id)
    rds.incr(gk)
    rds.expire(gk, ttl)


# ---------- 去重检查 ----------
async def _was_sent_recently(pid: int) -> bool:
    """检查 7 天内是否发过此 PID"""
    session = await get_session()
    try:
        cutoff = datetime.now() - timedelta(days=7)
        row = (await session.execute(
            select(SetuRecord).where(
                SetuRecord.pid == pid,
                SetuRecord.sent_at >= cutoff,
            )
        )).scalar_one_or_none()
        return row is not None
    finally:
        await session.close()


async def _record_sent(pid: int, uid: int, title: str, author: str, url: str):
    """记录发送或更新 sent_at"""
    session = await get_session()
    try:
        existing = (await session.execute(
            select(SetuRecord).where(SetuRecord.pid == pid)
        )).scalar_one_or_none()
        if existing:
            existing.sent_at = datetime.now()
        else:
            session.add(SetuRecord(
                pid=pid, uid=uid, title=title, author=author,
                url=url, sent_at=datetime.now(),
            ))
        await session.commit()
    finally:
        await session.close()


# ---------- 画师库抽图 ----------
async def _pick_from_curated(used_artists: set[int], used_pids: set[int]) -> dict | None:
    """从画师库抽1张图，返回 {pid, uid, title, author, url} 或 None"""
    api = await _get_pixiv_api()
    if not api:
        logger.warning("[setu] Pixiv API 不可用")
        return None
    if not CURATED_ARTISTS:
        logger.warning("[setu] 画师列表为空")
        return None

    available = [a for a in CURATED_ARTISTS if a not in used_artists]
    if not available:
        logger.debug("[setu] 所有画师已用过")
        return None

    random.shuffle(available)

    for artist_uid in available[:3]:  # 最多尝试3个画师
        try:
            result = await asyncio.to_thread(api.user_illusts, artist_uid, type="illust")
        except Exception as e:
            logger.warning(f"[setu] Pixiv user_illusts({artist_uid}) 失败: {e}")
            await _refresh_pixiv_auth()
            continue

        illusts = result.get("illusts", [])
        logger.info(f"[setu] 画师 {artist_uid}: 获取到 {len(illusts)} 张作品")
        if not illusts:
            logger.warning(f"[setu] 画师 {artist_uid} 原始返回: {str(result)[:500]}")

        filtered = [
            ill for ill in illusts
            if len(ill.get("tags", [])) >= MIN_TAGS
            and ill.get("sanity_level", 0) < 6
            and ill.get("type") == "illust"
            and ill.get("id") not in used_pids
            and not any(t.get("name") in _R18_TAGS for t in ill.get("tags", []))
        ]
        logger.info(f"[setu] 画师 {artist_uid}: 过滤后 {len(filtered)} 张 (tags>={MIN_TAGS}, sanity<6, 非R18)")

        if not filtered:
            continue

        filtered.sort(key=lambda x: x.get("create_date", ""), reverse=True)
        candidates = filtered[:20]

        for _attempt in range(3):
            chosen = random.choice(candidates)
            pid = chosen["id"]

            if await _was_sent_recently(pid):
                if random.random() < 0.2:
                    pass
                else:
                    continue

            original_url = (
                chosen.get("meta_single_page", {}).get("original_image_url")
                or chosen.get("image_urls", {}).get("large", "")
            )
            if not original_url:
                continue

            url = original_url.replace("i.pximg.net", PIXIV_PROXY)
            return {
                "pid": pid,
                "uid": artist_uid,
                "title": chosen.get("title", ""),
                "author": chosen.get("user", {}).get("name", ""),
                "url": url,
            }

    return None


# ---------- 标签搜索 ----------
async def _pick_from_tag_search(tags: list[str], used_pids: set[int], count: int) -> list[dict]:
    """按标签搜索 Pixiv，返回最多 count 张符合条件的图"""
    api = await _get_pixiv_api()
    if not api:
        logger.warning("[setu] Pixiv API 不可用，无法进行标签搜索")
        return []

    word = " ".join(tags)
    search_target = "partial_match_for_tags"
    # 随机排序方式减少重复: 70% 热度降序, 30% 时间降序
    sort = "popular_desc" if random.random() < 0.7 else "date_desc"
    logger.info(f"[setu] 标签搜索: '{word}' target={search_target} sort={sort}")

    candidates = []  # 通过基础过滤(R18/type/sanity)的所有候选，含质量数据
    # 随机起始偏移，避免每次搜同一批
    start_offset = random.randint(0, 3) * 30 if sort == "date_desc" else 0
    offset = start_offset
    max_pages = 10
    _dbg_r18 = _dbg_type = _dbg_sanity = 0

    for page in range(max_pages):
        try:
            resp = await asyncio.to_thread(
                api.search_illust,
                word,
                search_target=search_target,
                sort=sort,
                search_ai_type=1,
                offset=offset,
            )
        except Exception as e:
            logger.warning(f"[setu] 标签搜索失败(page={page}): {e}")
            await _refresh_pixiv_auth()
            break

        illusts = resp.get("illusts", [])
        logger.info(f"[setu] 标签搜索 page={page} 获取 {len(illusts)} 条")
        if not illusts:
            break

        for ill in illusts:
            pid = ill.get("id")
            if pid in used_pids:
                continue
            if any(t.get("name") in _R18_TAGS for t in ill.get("tags", [])):
                _dbg_r18 += 1
                continue
            if ill.get("type") != "illust":
                _dbg_type += 1
                continue
            if ill.get("sanity_level", 0) >= 7:
                _dbg_sanity += 1
                continue
            original_url = (
                ill.get("meta_single_page", {}).get("original_image_url")
                or ill.get("image_urls", {}).get("large", "")
            )
            if not original_url:
                continue
            large_url = ill.get("image_urls", {}).get("large", "")
            url = original_url.replace("i.pximg.net", PIXIV_PROXY)
            fallback_url = large_url.replace("i.pximg.net", PIXIV_PROXY) if large_url else url
            candidates.append({
                "pid": pid,
                "uid": ill.get("user", {}).get("id", 0),
                "title": ill.get("title", ""),
                "author": ill.get("user", {}).get("name", ""),
                "url": url,
                "fallback_url": fallback_url,
                "total_view": ill.get("total_view", 0),
                "total_bookmarks": ill.get("total_bookmarks", 0),
            })

        next_url = resp.get("next_url")
        if not next_url:
            break
        offset += 30

    logger.info(
        f"[setu] 标签搜索原始候选 {len(candidates)} 张 "
        f"(过滤: r18={_dbg_r18} type={_dbg_type} sanity={_dbg_sanity})"
    )

    # 三档质量阈值降级
    tiers = [
        (5000, 500),   # 第一档: 浏览≥5000 或 收藏≥500
        (2300, 300),   # 第二档: 浏览≥2300 或 收藏≥300
        (1300, 100),   # 第三档: 浏览≥1300 或 收藏≥100
        (200, None),   # 第四档: 浏览≥200，不限收藏
    ]
    pool = []
    for min_view, min_bm in tiers:
        if min_bm is None:
            pool = [c for c in candidates if c["total_view"] >= min_view]
        else:
            pool = [c for c in candidates if c["total_view"] >= min_view or c["total_bookmarks"] >= min_bm]
        logger.info(f"[setu] 质量阈值(view≥{min_view} bm≥{min_bm}): 候选 {len(pool)} 张")
        if len(pool) >= count:
            break

    if not pool:
        return []

    random.shuffle(pool)
    selected = []
    for item in pool:
        if len(selected) >= count:
            break
        if await _was_sent_recently(item["pid"]):
            if random.random() >= 0.1:  # 已发过的只有10%概率选中
                continue
        selected.append(item)
        used_pids.add(item["pid"])

    return selected


# ---------- Lolicon 随机抽图 ----------
async def _pick_from_lolicon(used_pids: set[int]) -> dict | None:
    """从 Lolicon API 抽1张高质量图"""
    payload = {
        "num": 1,
        "r18": 0,
        "excludeAI": True,

        "size": ["regular"],
        "proxy": PIXIV_PROXY,
    }

    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(LOLICON_API, json=payload)
                data = resp.json()
        except Exception as e:
            logger.warning(f"[setu] Lolicon API 请求失败: {e}")
            return None

        results = data.get("data", [])
        logger.info(f"[setu] Lolicon 返回 {len(results)} 条结果 (attempt={_attempt})")
        if not results:
            logger.warning(f"[setu] Lolicon 无结果, 原始: {str(data)[:300]}")
            return None

        item = results[0]
        pid = item.get("pid", 0)

        if pid in used_pids:
            continue

        # 7天去重
        if await _was_sent_recently(pid):
            if random.random() < 0.2:
                pass
            else:
                continue

        url = item.get("urls", {}).get("regular", "")
        if not url:
            continue

        return {
            "pid": pid,
            "uid": item.get("uid", 0),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "url": url,
        }

    return None


# ---------- 综合抽图 ----------
async def _pick_images(count: int) -> list[dict]:
    """抽取 count 张图，画师库优先，失败才降级 Lolicon"""
    results = []
    used_artists: set[int] = set()
    used_pids: set[int] = set()

    for i in range(count):
        logger.info(f"[setu] 第{i+1}张: 走画师库")
        item = await _pick_from_curated(used_artists, used_pids)

        if item is None:
            logger.info(f"[setu] 第{i+1}张: 画师库失败，降级 Lolicon")
            item = await _pick_from_lolicon(used_pids)

        if item:
            used_pids.add(item["pid"])
            used_artists.add(item["uid"])
            results.append(item)
            logger.info(f"[setu] 第{i+1}张: ✓ PID={item['pid']} by {item['author']}")
        else:
            logger.warning(f"[setu] 第{i+1}张: ✗ 未找到")

    logger.info(f"[setu] 共获取 {len(results)}/{count} 张图")
    return results


# ---------- 下载 + 发送工具 ----------
async def _download_images(images: list[dict]) -> list[tuple[dict, bytes | None]]:
    """并发下载图片，返回 [(img_info, data_or_None), ...]"""
    async def _dl(client: httpx.AsyncClient, img: dict) -> tuple[dict, bytes | None]:
        try:
            resp = await client.get(img["url"])
            if resp.status_code == 200 and len(resp.content) > 1000:
                size_kb = len(resp.content) // 1024
                if size_kb > 2048 and img.get("fallback_url") and img["fallback_url"] != img["url"]:
                    logger.info(f"[setu] 原图过大({size_kb}KB)，降级: PID={img['pid']}")
                    resp2 = await client.get(img["fallback_url"])
                    if resp2.status_code == 200 and len(resp2.content) > 1000:
                        logger.info(f"[setu] 降级下载成功: PID={img['pid']} {len(resp2.content)//1024}KB")
                        return img, resp2.content
                elif size_kb > 8192:
                    logger.warning(f"[setu] 跳过超大图片: PID={img['pid']} {size_kb}KB")
                    return img, None
                logger.info(f"[setu] 下载成功: PID={img['pid']} {size_kb}KB")
                return img, resp.content
            logger.warning(f"[setu] 下载异常: PID={img['pid']} status={resp.status_code}")
        except Exception as e:
            logger.warning(f"[setu] 下载失败: PID={img['pid']} {img['url'][:60]}… → {e}")
        return img, None

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        return list(await asyncio.gather(*[_dl(client, img) for img in images]))


async def _send_forward_or_individual(
    bot: Bot, group_id: int, success_data: list[tuple[dict, str]]
) -> list[tuple[dict, str]]:
    """尝试合并转发发送，失败降级逐条发送。返回逐条也失败的列表。"""
    if not success_data:
        return []

    # 构建合并转发
    nodes = []
    for img, b64 in success_data:
        text = f"🖼 {img['title']}\n👤 {img['author']}\n🔗 PID: {img['pid']}"
        node_content: list = [MessageSegment.text(text), MessageSegment.image(f"base64://{b64}")]
        nodes.append({
            "type": "node",
            "data": {"name": "rin", "uin": str(bot.self_id), "content": node_content},
        })

    try:
        await bot.call_api(
            "send_group_forward_msg", group_id=group_id, messages=nodes, _timeout=120,
        )
        return []  # 全部成功
    except Exception as e:
        logger.warning(f"[setu] 合并转发失败，降级逐条发送: {type(e).__name__}: {e}")

    failed = []
    for img, b64 in success_data:
        try:
            msg = MessageSegment.text(f"🖼 {img['title']} (PID:{img['pid']})\n")
            msg += MessageSegment.image(f"base64://{b64}")
            await bot.send_group_msg(group_id=group_id, message=msg)
            await asyncio.sleep(1.5)
        except Exception as e2:
            logger.warning(f"[setu] 逐条发送失败 PID={img['pid']}: {e2}")
            failed.append((img, b64))
    return failed


# ---------- Handler ----------
setu_cmd = on_command("#涩图", priority=5, block=True)


@setu_cmd.handle()
async def handle_setu(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    user_id = event.user_id
    group_id = event.group_id

    cmd_arg = args.extract_plain_text().strip()
    tags = [t.strip() for t in cmd_arg.split() if t.strip()]

    limit_msg = _check_limit(user_id, group_id)
    if limit_msg:
        await setu_cmd.send(limit_msg)
        return

    if tags:
        tag_str = " ".join(tags)
        await setu_cmd.send(f"正在搜索标签「{tag_str}」，注意身体哦……")
    else:
        await setu_cmd.send("注意身体哦，请稍等大约1分钟……")

    # 消费限额（提前扣减，防止重复触发）
    _incr_limit(user_id, group_id)

    used_pids: set[int] = set()
    all_success: list[tuple[dict, str]] = []  # (img, b64)
    target = NUM  # 目标发送数
    max_rounds = 3

    for round_idx in range(max_rounds):
        need = target - len(all_success)
        if need <= 0:
            break

        logger.info(f"[setu] 第{round_idx+1}轮: 还需 {need} 张")

        # 搜图
        if tags:
            images = await _pick_from_tag_search(tags, used_pids, need)
        else:
            images = await _pick_images(need)

        if not images:
            if round_idx == 0:
                await setu_cmd.send(
                    "没有找到合适的图片……" if not tags
                    else f"没有找到标签「{'·'.join(tags)}」的高质量作品……"
                )
                return
            break

        for img in images:
            used_pids.add(img["pid"])

        # 下载
        downloaded = await _download_images(images)

        # 记录到 DB
        for img, _ in downloaded:
            await _record_sent(
                pid=img["pid"], uid=img["uid"],
                title=img["title"], author=img["author"], url=img["url"],
            )

        # 组装成功的 (img, b64)
        round_success = []
        for img, img_data in downloaded:
            if img_data:
                round_success.append((img, base64.b64encode(img_data).decode()))
        all_success.extend(round_success)

    if not all_success:
        await setu_cmd.send("图片全部下载失败了……")
        return

    # 发送（合并转发 → 降级逐条）
    await _send_forward_or_individual(bot, group_id, all_success)


# ============================================================
# Pixiv 排行榜 — #p日榜 #p周榜 #p月榜 #p原创榜 #p新人榜
# 图片上传 MinIO 多群复用，支持日期参数（仅日榜）
# ============================================================
import re as _re

ranking_cfg = config.get("ranking", {})
RANKING_NUM = ranking_cfg.get("num", 10)
RANKING_AUTO_GROUPS: list[int] = ranking_cfg.get("auto_groups", [])
RANKING_HOUR = ranking_cfg.get("schedule_hour", 12)
RANKING_MINUTE = ranking_cfg.get("schedule_minute", 0)
RANKING_BUCKET = ranking_cfg.get("minio_bucket", "ranking")

# 命令 → (pixiv mode, 中文名)
_RANKING_MODES: dict[str, tuple[str, str]] = {
    "#p日榜":  ("day",            "日榜"),
    "#p周榜":  ("week",           "周榜"),
    "#p月榜":  ("month",          "月榜"),
    "#p原创榜": ("week_original",  "原创榜"),
    "#p新人榜": ("week_rookie",    "新人榜"),
}

# ---------- 中文数字映射 ----------
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
    "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30, "三十一": 31,
}
_CN_MONTH = {
    "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
    "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
}


def _parse_date_arg(text: str) -> date | str | None:
    """
    解析日期参数，返回 date 对象 / 错误提示字符串 / None（无参数）。
    支持: 416, 0416, 4-16, 4.16, 四月十六, 4月16, 4月16日
    限制: 30天内。
    """
    text = text.strip()
    if not text:
        return None

    month = day = 0
    today = date.today()
    year = today.year

    # 中文: "四月十六" / "四月十六日"
    cn_match = _re.match(r"^(.+月)(.+?)日?$", text)
    if cn_match:
        m_str, d_str = cn_match.group(1), cn_match.group(2)
        month = _CN_MONTH.get(m_str, 0)
        day = _CN_NUM.get(d_str, 0)
    else:
        # "4月16" / "4月16日"
        m2 = _re.match(r"^(\d{1,2})月(\d{1,2})日?$", text)
        if m2:
            month, day = int(m2.group(1)), int(m2.group(2))
        else:
            # "4-16" / "4.16"
            m3 = _re.match(r"^(\d{1,2})[.\-](\d{1,2})$", text)
            if m3:
                month, day = int(m3.group(1)), int(m3.group(2))
            else:
                # "0416" / "416" — 纯数字 3~4 位
                m4 = _re.match(r"^(\d{1,2})(\d{2})$", text)
                if m4:
                    month, day = int(m4.group(1)), int(m4.group(2))

    if month == 0 or day == 0:
        return "日期格式不对哦，试试 416、4-16、4月16 这样的格式~"

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return "日期不太对哦~"

    # 智能年份: 如果构造出的日期在未来，则回退到去年
    try:
        target = date(year, month, day)
    except ValueError:
        return "这个日期不存在哦~"

    if target > today:
        target = date(year - 1, month, day)

    delta = (today - target).days
    if delta > 30:
        return "过去太久了，换个时间吧~"
    if delta < 0:
        return "未来的排行榜还没出哦~"

    return target


# ---------- MinIO 客户端（复用 meme.minio 凭证，独立 bucket）----------
try:
    from minio import Minio as _Minio
    _minio_cfg = config.get("meme", {}).get("minio", {})
    _ranking_minio = _Minio(
        _minio_cfg["endpoint"],
        access_key=_minio_cfg["access_key"],
        secret_key=_minio_cfg["secret_key"],
        secure=_minio_cfg.get("secure", False),
    )
    HAS_MINIO = True
except Exception as _e:
    logger.warning(f"[ranking] MinIO 初始化失败，图片将使用 base64 发送: {_e}")
    _ranking_minio = None
    HAS_MINIO = False


@get_driver().on_startup
async def _ensure_ranking_bucket():
    if not HAS_MINIO:
        return
    try:
        exists = await asyncio.to_thread(_ranking_minio.bucket_exists, RANKING_BUCKET)
        if not exists:
            await asyncio.to_thread(_ranking_minio.make_bucket, RANKING_BUCKET)
            logger.info(f"[ranking] 已创建 MinIO bucket: {RANKING_BUCKET}")
        else:
            logger.info(f"[ranking] MinIO bucket 就绪: {RANKING_BUCKET}")
    except Exception as e:
        logger.error(f"[ranking] bucket 初始化失败: {e}")


def _upload_to_ranking_bucket(data: bytes, object_name: str) -> str | None:
    """上传图片到 MinIO ranking bucket，返回 7 天有效预签名 URL"""
    import io as _io
    from datetime import timedelta as _td
    ext = object_name.rsplit(".", 1)[-1].lower() if "." in object_name else "jpg"
    ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    try:
        _ranking_minio.put_object(
            RANKING_BUCKET, object_name, _io.BytesIO(data), length=len(data), content_type=ct,
        )
        url = _ranking_minio.presigned_get_object(
            RANKING_BUCKET, object_name, expires=_td(days=7),
        )
        return url
    except Exception as e:
        logger.error(f"[ranking] MinIO 上传失败 {object_name}: {e}")
        return None


# ---------- 获取 / 构建排行榜缓存 ----------

async def _get_or_build_ranking(
    mode: str = "day",
    target_date: date | None = None,
    count: int = RANKING_NUM,
) -> list[RankingCache]:
    """
    返回排行榜记录列表（RankingCache）。
    先查 DB 缓存，命中则直接返回；否则从 Pixiv 抓取 → 下载 → 上传 MinIO → 写入 DB。
    """
    t_date = target_date or date.today()

    # 先查 DB
    async with await get_session() as session:
        rows = (await session.execute(
            select(RankingCache)
            .where(
                RankingCache.rank_date == t_date,
                RankingCache.mode == mode,
            )
            .order_by(RankingCache.rank_pos)
        )).scalars().all()

    if len(rows) >= count:
        logger.info(f"[ranking] 命中缓存 mode={mode} date={t_date} {len(rows)} 条")
        return list(rows)

    # 从 Pixiv 获取
    mode_cn = dict(v for v in _RANKING_MODES.values()).get(mode, mode)
    logger.info(f"[ranking] 缓存不足，从 Pixiv 获取 {mode_cn} date={t_date}…")
    api = await _get_pixiv_api()
    if not api:
        logger.warning("[ranking] Pixiv API 不可用")
        return []

    try:
        kwargs: dict = {"mode": mode}
        if target_date:
            kwargs["date"] = target_date.isoformat()
        resp = await asyncio.to_thread(api.illust_ranking, **kwargs)
    except Exception as e:
        logger.error(f"[ranking] illust_ranking 失败: {e}")
        await _refresh_pixiv_auth()
        return []

    illusts = resp.get("illusts", [])
    logger.info(f"[ranking] 获取到 {len(illusts)} 条 ({mode})")

    # 筛选
    candidates = []
    for rank, ill in enumerate(illusts, 1):
        if any(t.get("name") in _R18_TAGS for t in ill.get("tags", [])):
            continue
        if ill.get("sanity_level", 0) >= 7:
            continue
        original_url = (
            ill.get("meta_single_page", {}).get("original_image_url")
            or ill.get("image_urls", {}).get("large", "")
        )
        if not original_url:
            continue
        large_url = ill.get("image_urls", {}).get("large", "")
        candidates.append({
            "rank": rank,
            "pid": ill.get("id"),
            "uid": ill.get("user", {}).get("id", 0),
            "title": ill.get("title", ""),
            "author": ill.get("user", {}).get("name", ""),
            "url": original_url.replace("i.pximg.net", PIXIV_PROXY),
            "fallback_url": (large_url.replace("i.pximg.net", PIXIV_PROXY) if large_url else ""),
            "total_view": ill.get("total_view", 0),
            "total_bookmarks": ill.get("total_bookmarks", 0),
        })
        if len(candidates) >= count:
            break

    # 并发下载
    downloaded = await _download_images(candidates)

    # 上传 MinIO + 写 DB
    date_str = t_date.strftime("%Y%m%d")
    new_rows: list[RankingCache] = []
    rank_pos = 0
    for img, img_data in downloaded:
        if not img_data:
            logger.warning(f"[ranking] 下载失败，跳过 PID={img['pid']}")
            continue
        rank_pos += 1

        minio_url = ""
        if HAS_MINIO:
            obj_name = f"{mode}/{date_str}/{rank_pos:02d}_{img['pid']}.jpg"
            minio_url = await asyncio.to_thread(_upload_to_ranking_bucket, img_data, obj_name) or ""
            if minio_url:
                logger.info(f"[ranking] 上传 MinIO: mode={mode} rank={rank_pos} PID={img['pid']}")

        row = RankingCache(
            rank_date=t_date,
            mode=mode,
            rank_pos=rank_pos,
            pid=img["pid"],
            uid=img["uid"],
            title=img["title"],
            author=img["author"],
            total_view=img["total_view"],
            total_bookmarks=img["total_bookmarks"],
            pixiv_url=img["url"],
            minio_url=minio_url,
        )
        row._img_data = img_data  # type: ignore[attr-defined]
        new_rows.append(row)

    if new_rows:
        async with await get_session() as session:
            for row in new_rows:
                try:
                    session.add(row)
                    await session.flush()
                except Exception:
                    await session.rollback()
            await session.commit()
        logger.info(f"[ranking] 写入 DB {len(new_rows)} 条 (mode={mode})")

    return new_rows


# ---------- 发送排行榜到群 ----------

async def _send_ranking(
    bot: Bot, group_id: int, records: list[RankingCache], title_text: str,
):
    """发送排行榜到单个群。优先用 minio_url，否则降级 base64。"""
    if not records:
        return

    nodes = []
    nodes.append({
        "type": "node",
        "data": {"name": "rin", "uin": str(bot.self_id),
                 "content": [MessageSegment.text(title_text)]},
    })

    for row in records:
        text = (
            f"#{row.rank_pos} {row.title}\n"
            f"作者: {row.author}\n"
            f"PID: {row.pid}\n"
            f"浏览量: {row.total_view:,}  收藏量: {row.total_bookmarks:,}"
        )
        if row.minio_url:
            img_seg = MessageSegment.image(row.minio_url)
        elif hasattr(row, "_img_data") and row._img_data:  # type: ignore[attr-defined]
            b64 = base64.b64encode(row._img_data).decode()  # type: ignore[attr-defined]
            img_seg = MessageSegment.image(f"base64://{b64}")
        else:
            nodes.append({
                "type": "node",
                "data": {"name": "rin", "uin": str(bot.self_id),
                         "content": [MessageSegment.text(text)]},
            })
            continue

        nodes.append({
            "type": "node",
            "data": {"name": "rin", "uin": str(bot.self_id),
                     "content": [MessageSegment.text(text), img_seg]},
        })

    try:
        for attempt in range(3):
            try:
                await bot.call_api(
                    "send_group_forward_msg", group_id=group_id, messages=nodes, _timeout=180,
                )
                logger.info(f"[ranking] 群 {group_id}: 发送成功 (attempt={attempt + 1})")
                return
            except Exception as e:
                logger.warning(f"[ranking] 群 {group_id}: 合并转发失败 attempt={attempt + 1}/3: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)

        # 3 次全败：去掉所有图片节点，只发文字合并转发
        logger.warning(f"[ranking] 群 {group_id}: 3 次重试均失败，降级纯文字合并转发")
        text_nodes = [
            n for n in nodes
            if not any(
                seg.get("type") == "image"
                for seg in (
                    n["data"]["content"]
                    if isinstance(n["data"]["content"], list)
                    else []
                )
            )
        ]
        # 把带图片的节点改为只保留文字部分
        text_only_nodes = []
        for n in nodes:
            content = n["data"].get("content", [])
            text_parts = [seg for seg in content if isinstance(seg, MessageSegment) and seg.type == "text"]
            if text_parts:
                text_only_nodes.append({
                    "type": "node",
                    "data": {
                        "name": n["data"]["name"],
                        "uin": n["data"]["uin"],
                        "content": text_parts,
                    },
                })
        if text_only_nodes:
            try:
                await bot.call_api(
                    "send_group_forward_msg", group_id=group_id, messages=text_only_nodes, _timeout=60,
                )
                logger.info(f"[ranking] 群 {group_id}: 纯文字合并转发成功")
            except Exception as e2:
                logger.error(f"[ranking] 群 {group_id}: 纯文字合并转发也失败: {e2}")
    except Exception as e:
        logger.error(f"[ranking] 群 {group_id}: 发送异常: {e}")


# ---------- 构建标题文本 ----------

def _ranking_title(mode_cn: str, count: int, target_date: date | None) -> str:
    d = target_date or date.today()
    return f"Pixiv {mode_cn} Top {count} ({d.isoformat()})"


# ---------- 通用 handler ----------

async def _handle_ranking_cmd(
    bot: Bot, event: GroupMessageEvent,
    mode: str, mode_cn: str, date_arg: str = "",
):
    """所有排行榜命令的通用处理逻辑"""
    target_date: date | None = None

    # 仅日榜支持日期参数
    if mode == "day" and date_arg:
        result = _parse_date_arg(date_arg)
        if isinstance(result, str):
            # 错误提示
            await bot.send_group_msg(group_id=event.group_id, message=result)
            return
        if isinstance(result, date):
            target_date = result

    date_hint = f" ({target_date.isoformat()})" if target_date else ""
    await bot.send_group_msg(
        group_id=event.group_id,
        message=f"正在获取 Pixiv {mode_cn}{date_hint}，请稍等……",
    )

    records = await _get_or_build_ranking(mode=mode, target_date=target_date)
    if not records:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=f"获取{mode_cn}失败了……",
        )
        return

    title = _ranking_title(mode_cn, len(records), target_date)
    await _send_ranking(bot, event.group_id, records, title)


# ---------- 注册 5 个命令 ----------

_cmd_day = on_command("#p日榜", priority=5, block=True)
_cmd_week = on_command("#p周榜", priority=5, block=True)
_cmd_month = on_command("#p月榜", priority=5, block=True)
_cmd_original = on_command("#p原创榜", priority=5, block=True)
_cmd_rookie = on_command("#p新人榜", priority=5, block=True)


@_cmd_day.handle()
async def _h_day(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await _handle_ranking_cmd(bot, event, "day", "日榜", args.extract_plain_text().strip())


@_cmd_week.handle()
async def _h_week(bot: Bot, event: GroupMessageEvent):
    await _handle_ranking_cmd(bot, event, "week", "周榜")


@_cmd_month.handle()
async def _h_month(bot: Bot, event: GroupMessageEvent):
    await _handle_ranking_cmd(bot, event, "month", "月榜")


@_cmd_original.handle()
async def _h_original(bot: Bot, event: GroupMessageEvent):
    await _handle_ranking_cmd(bot, event, "week_original", "原创榜")


@_cmd_rookie.handle()
async def _h_rookie(bot: Bot, event: GroupMessageEvent):
    await _handle_ranking_cmd(bot, event, "week_rookie", "新人榜")


# ---------- 定时发送日榜 ----------
try:
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job(
        "cron",
        hour=RANKING_HOUR,
        minute=RANKING_MINUTE,
        id="daily_ranking",
    )
    async def _scheduled_ranking():
        if not RANKING_AUTO_GROUPS:
            return

        from nonebot import get_bots
        bots = get_bots()
        if not bots:
            logger.warning("[ranking] 定时任务: 无可用 Bot")
            return
        bot = list(bots.values())[0]

        logger.info(f"[ranking] 定时任务: 构建日榜并推送到 {RANKING_AUTO_GROUPS}")
        records = await _get_or_build_ranking(mode="day")
        if not records:
            logger.warning("[ranking] 定时任务: 获取日榜失败")
            return

        title = _ranking_title("日榜", len(records), None)
        for gid in RANKING_AUTO_GROUPS:
            try:
                await _send_ranking(bot, gid, records, title)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[ranking] 定时推送到群 {gid} 失败: {e}")

    logger.info(
        f"[ranking] 定时任务已注册: 每天 {RANKING_HOUR:02d}:{RANKING_MINUTE:02d} "
        f"推送到 {RANKING_AUTO_GROUPS}"
    )
except ImportError:
    logger.warning("[ranking] nonebot_plugin_apscheduler 未安装，定时排行榜功能不可用")
