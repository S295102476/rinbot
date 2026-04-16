"""杀戮尖塔2 Wiki 查询插件 (本地数据库版)

数据来源: sts2.huijiwiki.com 的 tabx 数据页面
方案: 一次性下载数据存入 MySQL，后续全部从本地数据库查询

命令:
  #尖塔 卡牌 <名称>   搜索卡牌
  #尖塔 遗物 <名称>   搜索遗物
  #尖塔 药水 <名称>   搜索药水
  #尖塔 词条 <名称>   搜索每日挑战词条
  #尖塔 <名称>        全局搜索
  #尖塔 今日挑战      每日挑战信息
  #尖塔 更新数据      (管理员) 重新下载数据
"""
import json
import re
import io
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import httpx
import yaml
from minio import Minio
from minio.error import S3Error
from pyquery import PyQuery as pq
from sqlalchemy import select, delete
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from plugins.db import (
    engine, Base, SessionFactory,
    WikiCard, WikiRelic, WikiPotion, WikiModifier, WikiMonster,
)

# ━━━━━━━━━━━━━━━━ 配置 ━━━━━━━━━━━━━━━━

with open("config.yaml", "r", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

wiki_cfg = _config.get("wiki", {})
API_BASE = wiki_cfg.get("api_base", "https://sts2.huijiwiki.com/api.php")
WIKI_BASE = wiki_cfg.get("wiki_base", "https://sts2.huijiwiki.com/wiki/")
ADMIN_USERS = wiki_cfg.get("admin_users", [])
WIKI_PROXY = wiki_cfg.get("proxy", "")
_WIKI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# MinIO 图片缓存
minio_cfg = _config.get("meme", {}).get("minio", {})
WIKI_BUCKET = "wiki-images"
_minio_client: Minio | None = None
try:
    _minio_client = Minio(
        minio_cfg["endpoint"],
        access_key=minio_cfg["access_key"],
        secret_key=minio_cfg["secret_key"],
        secure=minio_cfg.get("secure", False),
    )
    if not _minio_client.bucket_exists(WIKI_BUCKET):
        _minio_client.make_bucket(WIKI_BUCKET)
        logger.info(f"[wiki] 已创建 MinIO bucket: {WIKI_BUCKET}")
except Exception as e:
    logger.warning(f"[wiki] MinIO 初始化失败: {e}，图片缓存不可用")

CATEGORY_KEYWORDS = {
    "卡牌": "card", "卡": "card",
    "遗物": "relic",
    "药水": "potion",
    "词条": "modifier", "修改器": "modifier",
    "怪物": "monster", "敌人": "monster",
}

wiki_cmd = on_command("#尖塔", priority=5, block=True)


# ━━━━━━━━━━━━━━━━ 工具函数 ━━━━━━━━━━━━━━━━

def clean_wiki_text(text: str) -> str:
    """清理 wiki 模板标记，提取纯文本"""
    if not text:
        return ""
    # {{颜色|xxx|yyy}} → yyy
    text = re.sub(r'\{\{颜色\|[^|]+\|([^}]+)\}\}', r'\1', text)
    # [[File:xxx|...|link=yyy]] → yyy
    text = re.sub(r'\[\[File:[^\]]*?\|link=([^\]|]+)\]\]', r'\1', text)
    # [[File:xxx]] → (移除)
    text = re.sub(r'\[\[File:[^\]]+\]\]', '', text)
    # <br> → 换行
    text = re.sub(r'<br\s*/?>', '\n', text)
    return text.strip()


def _escape_like(keyword: str) -> str:
    """转义 SQL LIKE 通配符"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def download_image_b64(url: str, cache_key: str = "") -> str | None:
    """获取图片 base64，优先从 MinIO 缓存读取，回退到在线下载"""
    if not url:
        return None

    if not cache_key:
        cache_key = url.rsplit("/", 1)[-1]

    # 1) 尝试从 MinIO 读取
    if _minio_client and cache_key:
        try:
            resp = _minio_client.get_object(WIKI_BUCKET, cache_key)
            data = resp.read()
            resp.close()
            resp.release_conn()
            if data and len(data) > 500:
                return base64.b64encode(data).decode()
        except S3Error:
            pass
        except Exception as e:
            logger.warning(f"[wiki] MinIO 读取失败: {e}")

    # 2) 尝试在线下载（可能 403）
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": _WIKI_UA}, proxy=WIKI_PROXY or None) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 500:
                if _minio_client and cache_key:
                    try:
                        content_type = resp.headers.get("content-type", "image/png")
                        _minio_client.put_object(
                            WIKI_BUCKET, cache_key,
                            io.BytesIO(resp.content), len(resp.content),
                            content_type=content_type,
                        )
                    except Exception:
                        pass
                return base64.b64encode(resp.content).decode()
    except Exception:
        pass
    return None


def _normalize_wiki_filename(filename: str) -> str:
    """规范化 wiki 文件名: 空格→下划线, 首字母大写 (MediaWiki 惯例)"""
    if not filename:
        return filename
    fname = filename.replace(" ", "_")
    return fname[0].upper() + fname[1:]


def get_image_from_minio(filename: str) -> str | None:
    """直接从 MinIO 获取图片 base64（用于已预缓存的图片）"""
    if not _minio_client or not filename:
        return None
    key = _normalize_wiki_filename(filename)
    try:
        resp = _minio_client.get_object(WIKI_BUCKET, key)
        data = resp.read()
        resp.close()
        resp.release_conn()
        if data and len(data) > 500:
            return base64.b64encode(data).decode()
    except S3Error:
        pass
    except Exception as e:
        logger.warning(f"[wiki] MinIO 读取失败 {key}: {e}")
    return None


def make_image_url(image_filename: str) -> str:
    """构建 wiki 图片直链，使用 huijistatic CDN 绕过 Cloudflare
    MediaWiki 路径格式: /uploads/{md5[0]}/{md5[0:2]}/{filename}
    md5 基于文件名（空格替换为下划线）
    """
    if not image_filename:
        return ""
    # MediaWiki 规范化：空格→下划线，首字母大写
    fname = image_filename.replace(" ", "_")
    fname = fname[0].upper() + fname[1:] if fname else fname
    md5 = hashlib.md5(fname.encode("utf-8")).hexdigest()
    return f"https://huiji-public.huijistatic.com/sts2/uploads/{md5[0]}/{md5[:2]}/{quote(fname)}"


# ━━━━━━━━━━━━━━━━ 数据下载 ━━━━━━━━━━━━━━━━

TABX_PAGES = {
    "card": "Data:Card.tabx",
    "relic": "Data:Relic.tabx",
    "potion": "Data:Potion.tabx",
    "modifier": "Data:Modifier.tabx",
}
TABX_LABELS = {"card": "卡牌", "relic": "遗物", "potion": "药水", "modifier": "词条"}


async def download_and_store() -> dict[str, int]:
    """从 wiki 下载全部 tabx 数据页面，存入 MySQL (仅在管理员触发时调用)"""
    # 确保表存在
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    counts: dict[str, int] = {}

    logger.info(f"[wiki] 使用代理: {WIKI_PROXY!r}")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": _WIKI_UA}, proxy=WIKI_PROXY or None) as client:
        for cat, page_name in TABX_PAGES.items():
            logger.info(f"[wiki] 正在下载 {page_name} ...")
            resp = await client.get(API_BASE, params={
                "action": "parse", "page": page_name,
                "prop": "wikitext", "format": "json",
            })
            if resp.status_code != 200:
                logger.error(f"[wiki] {page_name} HTTP {resp.status_code}")
                continue
            text = resp.text
            if not text.strip():
                logger.error(f"[wiki] {page_name} 返回空响应")
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as e:
                logger.error(f"[wiki] {page_name} JSON 解析失败: {e}, 前100字符: {text[:100]}")
                continue
            wikitext = raw.get("parse", {}).get("wikitext", {}).get("*", "")
            if not wikitext.strip():
                logger.error(f"[wiki] {page_name} wikitext 为空")
                continue
            try:
                tabx = json.loads(wikitext)
            except json.JSONDecodeError as e:
                logger.error(f"[wiki] {page_name} wikitext JSON 解析失败: {e}")
                continue
            rows = tabx.get("data", [])

            async with SessionFactory() as session:
                async with session.begin():
                    if cat == "card":
                        await session.execute(delete(WikiCard))
                        for r in rows:
                            session.add(WikiCard(
                                card_id=r[1] or "", name=r[2] or "",
                                color=r[3] or "", rarity=r[4] or "",
                                card_type=r[5] or "", cost=r[6] or "",
                                description=r[7] or "", description_raw=r[8] or "",
                                upgrade_ref=r[9] or "",
                                compendium_order=int(r[10]) if r[10] else 0,
                                image=r[11] or "", page=r[12] or "",
                            ))
                    elif cat == "relic":
                        await session.execute(delete(WikiRelic))
                        for r in rows:
                            session.add(WikiRelic(
                                relic_id=r[1] or "", name=r[2] or "",
                                pool=r[3] or "", tier=r[4] or "",
                                description=r[5] or "", description_raw=r[6] or "",
                                flavor=r[7] or "", ancient=r[8] or "",
                                compendium_order=int(r[9]) if r[9] else 0,
                                image=r[10] or "", page=r[11] or "",
                            ))
                    elif cat == "potion":
                        await session.execute(delete(WikiPotion))
                        for r in rows:
                            session.add(WikiPotion(
                                potion_id=r[1] or "", name=r[2] or "",
                                color=r[3] or "", tier=r[4] or "",
                                description=r[5] or "", description_raw=r[6] or "",
                                compendium_order=int(r[7]) if r[7] else 0,
                                image=r[8] or "", page=r[9] or "",
                            ))
                    elif cat == "modifier":
                        await session.execute(delete(WikiModifier))
                        for r in rows:
                            session.add(WikiModifier(
                                modifier_id=r[1] or "", name=r[2] or "",
                                description=r[3] or "",
                                image=r[4] or "", kind=r[5] or "",
                            ))

            counts[cat] = len(rows)
            logger.info(f"[wiki] {page_name} → {len(rows)} 条记录")

    return counts


# ━━━━━━━━━━━━━━━━ 数据库查询 ━━━━━━━━━━━━━━━━

async def search_cards(keyword: str, limit: int = 10) -> list[WikiCard]:
    kw = _escape_like(keyword)
    async with SessionFactory() as session:
        stmt = (
            select(WikiCard)
            .where(WikiCard.name.like(f"%{kw}%"))
            .order_by(WikiCard.name != keyword, WikiCard.compendium_order)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def search_relics(keyword: str, limit: int = 10) -> list[WikiRelic]:
    kw = _escape_like(keyword)
    async with SessionFactory() as session:
        stmt = (
            select(WikiRelic)
            .where(WikiRelic.name.like(f"%{kw}%"))
            .order_by(WikiRelic.name != keyword, WikiRelic.compendium_order)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def search_potions(keyword: str, limit: int = 10) -> list[WikiPotion]:
    kw = _escape_like(keyword)
    async with SessionFactory() as session:
        stmt = (
            select(WikiPotion)
            .where(WikiPotion.name.like(f"%{kw}%"))
            .order_by(WikiPotion.name != keyword, WikiPotion.compendium_order)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def search_modifiers(keyword: str, limit: int = 10) -> list[WikiModifier]:
    kw = _escape_like(keyword)
    async with SessionFactory() as session:
        stmt = (
            select(WikiModifier)
            .where(WikiModifier.name.like(f"%{kw}%"))
            .order_by(WikiModifier.name != keyword)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def search_monsters(keyword: str, limit: int = 10) -> list[WikiMonster]:
    kw = _escape_like(keyword)
    async with SessionFactory() as session:
        stmt = (
            select(WikiMonster)
            .where(WikiMonster.name.like(f"%{kw}%"))
            .order_by(WikiMonster.name != keyword)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def find_card_by_id(card_id: str) -> WikiCard | None:
    async with SessionFactory() as session:
        stmt = select(WikiCard).where(WikiCard.card_id == card_id)
        return (await session.execute(stmt)).scalar_one_or_none()


# ━━━━━━━━━━━━━━━━ 格式化输出 ━━━━━━━━━━━━━━━━

def format_card(card: WikiCard, upgrade: WikiCard | None = None,
                colors: list[str] | None = None) -> str:
    """格式化卡牌信息"""
    lines = [f"📖 {card.name}"]
    color_str = "/".join(colors) if colors and len(colors) > 1 else card.color
    if color_str:
        lines.append(f"角色: {color_str}")
    if card.rarity:
        lines.append(f"稀有度: {card.rarity}")
    if card.cost:
        lines.append(f"耗能: {card.cost}")
    if card.card_type:
        lines.append(f"类型: {card.card_type}")
    if card.description_raw:
        lines.append(f"效果: {card.description_raw}")
    if upgrade and upgrade.description_raw:
        extra = ""
        if upgrade.cost and upgrade.cost != card.cost:
            extra = f" (耗能: {upgrade.cost})"
        lines.append(f"升级后描述: {upgrade.description_raw}{extra}")
    lines.append("📚 来源: 灰机wiki sts2")
    return "\n".join(lines)


def format_relic(relic: WikiRelic) -> str:
    lines = [f"📖 {relic.name}"]
    if relic.pool:
        lines.append(f"所属: {relic.pool}")
    if relic.tier:
        lines.append(f"稀有度: {relic.tier}")
    desc = relic.description_raw or clean_wiki_text(relic.description)
    if desc:
        lines.append(f"效果: {desc}")
    if relic.flavor:
        flavor = clean_wiki_text(relic.flavor)
        if flavor:
            lines.append(f"引言: {flavor}")
    lines.append("📚 来源: 灰机wiki sts2")
    return "\n".join(lines)


def format_potion(potion: WikiPotion) -> str:
    lines = [f"📖 {potion.name}"]
    if potion.color:
        lines.append(f"角色: {potion.color}")
    if potion.tier:
        lines.append(f"稀有度: {potion.tier}")
    desc = potion.description_raw or clean_wiki_text(potion.description)
    if desc:
        lines.append(f"效果: {desc}")
    lines.append("📚 来源: 灰机wiki sts2")
    return "\n".join(lines)


def format_modifier(mod: WikiModifier) -> str:
    lines = [f"📖 {mod.name}"]
    if mod.kind:
        kind_str = "正面" if mod.kind == "good" else "负面" if mod.kind == "bad" else mod.kind
        lines.append(f"类型: {kind_str}")
    desc = clean_wiki_text(mod.description)
    if desc:
        lines.append(f"效果: {desc}")
    return "\n".join(lines)


def format_monster(monster: WikiMonster) -> str:
    lines = [f"📖 {monster.name}"]
    # HP
    if monster.min_hp and monster.max_hp:
        if monster.min_hp == monster.max_hp:
            lines.append(f"HP: {monster.min_hp}")
        else:
            lines.append(f"HP: {monster.min_hp}~{monster.max_hp}")
    # 勇者HP
    if monster.ascender_min_hp and monster.ascender_max_hp:
        if monster.ascender_min_hp == monster.ascender_max_hp:
            lines.append(f"勇者HP: {monster.ascender_min_hp}")
        else:
            lines.append(f"勇者HP: {monster.ascender_min_hp}~{monster.ascender_max_hp}")
    if monster.tier:
        lines.append(f"等级: {monster.tier}")
    if monster.stage:
        lines.append(f"出现地区: {monster.stage}")
    if monster.note and monster.note != "无":
        lines.append(f"备注: {clean_wiki_text(monster.note)}")
    lines.append("📚 来源: 灰机wiki sts2")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━ 搜索流程 ━━━━━━━━━━━━━━━━

async def do_card_search(keyword: str) -> tuple[str, list[str], str]:
    """搜索卡牌, 返回 (主结果文本, 其他匹配名列表, 图片URL)"""
    results = await search_cards(keyword)
    if not results:
        return "", [], ""

    # 只展示基础卡
    base_cards = [c for c in results if c.upgrade_ref != "已升级"]
    if not base_cards:
        base_cards = results[:1]

    card = base_cards[0]
    # 同名多角色 → 合并角色
    same_name = [c for c in base_cards if c.name == card.name]
    colors = list(dict.fromkeys(c.color for c in same_name if c.color))

    # 查找升级版
    upgrade = None
    if card.upgrade_ref and card.upgrade_ref != "已升级":
        upgrade = await find_card_by_id(card.upgrade_ref)

    text = format_card(card, upgrade, colors if len(colors) > 1 else None)
    seen = {card.name}
    others = []
    for c in base_cards:
        if c.name not in seen:
            others.append(c.name)
            seen.add(c.name)
        if len(others) >= 4:
            break
    image_name = card.image or ""
    # 构建升级版图片名: xxx.png -> xxx_upgrade.png
    upgrade_image = ""
    if image_name:
        base, ext = image_name.rsplit(".", 1)
        upgrade_image = f"{base}_upgrade.{ext}"
    return text, others, [image_name, upgrade_image]


async def do_relic_search(keyword: str) -> tuple[str, list[str], str]:
    results = await search_relics(keyword)
    if not results:
        return "", [], ""
    text = format_relic(results[0])
    others = list(dict.fromkeys(r.name for r in results[1:5] if r.name != results[0].name))
    image_name = results[0].image or ""
    return text, others, image_name


async def do_potion_search(keyword: str) -> tuple[str, list[str], str]:
    results = await search_potions(keyword)
    if not results:
        return "", [], ""
    text = format_potion(results[0])
    others = list(dict.fromkeys(p.name for p in results[1:5] if p.name != results[0].name))
    image_name = results[0].image or ""
    return text, others, image_name


async def do_modifier_search(keyword: str) -> tuple[str, list[str], str]:
    results = await search_modifiers(keyword)
    if not results:
        return "", [], ""
    text = format_modifier(results[0])
    others = list(dict.fromkeys(m.name for m in results[1:5] if m.name != results[0].name))
    return text, others, ""


async def do_monster_search(keyword: str) -> tuple[str, list[str], str]:
    results = await search_monsters(keyword)
    if not results:
        return "", [], ""
    text = format_monster(results[0])
    others = list(dict.fromkeys(m.name for m in results[1:5] if m.name != results[0].name))
    image_name = results[0].image or ""
    return text, others, image_name


async def _send_with_image(
    bot: Bot, event: GroupMessageEvent,
    text: str, image_filename: str | list[str],
    others_text: str = "",
) -> None:
    """用合并转发发送文字+图片，避免刷屏"""
    filenames = image_filename if isinstance(image_filename, list) else [image_filename]
    filenames = [f for f in filenames if f]

    # 收集图片 base64
    images_b64: list[str] = []
    for fn in filenames:
        b64 = get_image_from_minio(fn)
        if not b64:
            url = make_image_url(fn)
            b64 = await download_image_b64(url, cache_key=_normalize_wiki_filename(fn))
        if b64:
            images_b64.append(b64)

    if not images_b64:
        # 没有图片，直接发文字
        await wiki_cmd.send(text)
        return

    BOT_NAME = "尖塔百科"
    nodes = []
    # 第一条: 文字信息
    nodes.append({
        "type": "node",
        "data": {
            "name": BOT_NAME,
            "uin": str(bot.self_id),
            "content": text,
        },
    })
    # 每张图片一条
    for b64 in images_b64:
        nodes.append({
            "type": "node",
            "data": {
                "name": BOT_NAME,
                "uin": str(bot.self_id),
                "content": MessageSegment.image(f"base64://{b64}"),
            },
        })
    # 相关条目
    if others_text:
        nodes.append({
            "type": "node",
            "data": {
                "name": BOT_NAME,
                "uin": str(bot.self_id),
                "content": others_text,
            },
        })

    await bot.call_api(
        "send_group_forward_msg",
        group_id=event.group_id,
        messages=nodes,
    )


async def search_and_reply(bot: Bot, event: GroupMessageEvent,
                           keyword: str, category: str | None):
    """根据分类搜索并回复"""
    cat_map = {
        "card": ("卡牌", do_card_search),
        "relic": ("遗物", do_relic_search),
        "potion": ("药水", do_potion_search),
        "modifier": ("词条", do_modifier_search),
        "monster": ("怪物", do_monster_search),
    }

    if category and category in cat_map:
        label, search_fn = cat_map[category]
        text, others, image_url = await search_fn(keyword)
        if not text:
            await wiki_cmd.send(f"未找到{label}「{keyword}」")
            return
        others_text = f"相关{label}: " + "、".join(others) if others else ""
        await _send_with_image(bot, event, text, image_url, others_text)
        return

    # 无分类 → 逐类搜索, 返回第一个命中
    all_others: list[str] = []
    for cat_key, (label, search_fn) in cat_map.items():
        text, others, image_url = await search_fn(keyword)
        if text:
            for name in others:
                all_others.append(f"{name}({label})")
            # 继续搜索其他类别
            for cat2, (label2, fn2) in cat_map.items():
                if cat2 == cat_key:
                    continue
                t2, o2, _ = await fn2(keyword)
                if t2:
                    first_name = t2.split("\n")[0].replace("📖 ", "")
                    all_others.append(f"{first_name}({label2})")
                for n in o2[:2]:
                    all_others.append(f"{n}({label2})")
            others_text = "相关条目: " + "、".join(all_others[:6]) if all_others else ""
            await _send_with_image(bot, event, text, image_url, others_text)
            return

    await wiki_cmd.send(
        f"未找到「{keyword}」的相关结果\n"
        "提示: 若数据库为空，请管理员先执行 #尖塔 更新数据"
    )


# ━━━━━━━━━━━━━━━━ 每日挑战 ━━━━━━━━━━━━━━━━

_daily_cache: dict[str, dict] = {}


def parse_daily_html(html: str) -> dict | None:
    """解析每日挑战模板展开后的 HTML"""
    doc = pq(html)

    first_p = doc("p").eq(0)
    char_link = first_p.find("a[title]").eq(0)
    character = char_link.text().strip() if char_link else ""

    asc_span = first_p.find("span.ascension_icon")
    ascension = asc_span.text().strip() if asc_span else ""

    modifiers = []
    for tt in doc("span.huiji-tt").items():
        name_el = tt.find("b span")
        name = name_el.text().strip() if name_el else ""
        color_class = name_el.attr("class") if name_el else ""
        is_negative = "color-red" in (color_class or "")

        desc_el = tt.find(".huiji-tt-preload span[style*='color:#fff6e2']")
        desc = desc_el.text().strip() if desc_el else ""

        if name:
            modifiers.append({
                "name": name,
                "description": desc,
                "is_negative": is_negative,
            })

    if not character:
        return None
    return {
        "character": character,
        "ascension": ascension,
        "modifiers": modifiers,
    }


async def handle_daily_challenge(bot: Bot, event: GroupMessageEvent):
    """每日挑战 — 使用 wiki 模板展开 (每日仅调用一次, 缓存结果)"""
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    if date_str not in _daily_cache:
        await wiki_cmd.send("正在获取今日挑战信息...")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": _WIKI_UA}, proxy=WIKI_PROXY or None) as client:
                resp = await client.get(API_BASE, params={
                    "action": "parse",
                    "text": f"{{{{#invoke:DailyModifiers|main|date={date_str}}}}}",
                    "title": "Test",
                    "prop": "text",
                    "contentmodel": "wikitext",
                    "format": "json",
                })
                data = resp.json()
            html = data.get("parse", {}).get("text", {}).get("*")
            if not html:
                await wiki_cmd.send("获取每日挑战信息失败")
                return
        except Exception as e:
            logger.error(f"[wiki] 每日挑战 API 失败: {e}")
            await wiki_cmd.send(f"获取每日挑战信息失败: {e}")
            return

        result = parse_daily_html(html)
        if not result:
            await wiki_cmd.send("解析每日挑战信息失败")
            return
        _daily_cache.clear()
        _daily_cache[date_str] = result
    else:
        result = _daily_cache[date_str]

    # 构建消息
    beijing_tz = timezone(timedelta(hours=8))
    now_beijing = datetime.now(beijing_tz)
    date_display = now_beijing.strftime("%Y-%m-%d")

    lines = [f"🗡️ 杀戮尖塔2 每日挑战 {date_display}"]
    lines.append(f"角色: {result['character']}  进阶{result['ascension']}")
    lines.append("")

    for mod in result["modifiers"]:
        icon = "🔴" if mod["is_negative"] else "🟢"
        line = f"{icon} {mod['name']}"
        if mod["description"]:
            line += f"\n   {mod['description']}"
        lines.append(line)

    next_reset = now_beijing.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_beijing >= next_reset:
        next_reset += timedelta(days=1)
    remaining = next_reset - now_beijing
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes, _ = divmod(rem, 60)
    lines.append(f"\n⏰ 刷新倒计时: {hours}小时{minutes}分")

    text = "\n".join(lines)

    char_images = {
        "铁甲战士": "https://huiji-public.huijistatic.com/sts2/uploads/b/b4/Char_select_ironclad.png",
        "静默猎手": "https://huiji-public.huijistatic.com/sts2/uploads/a/a2/Char_select_silent.png",
        "储君": "https://huiji-public.huijistatic.com/sts2/uploads/e/ee/Char_select_regent.png",
        "亡灵契约师": "https://huiji-public.huijistatic.com/sts2/uploads/7/72/Char_select_necrobinder.png",
        "故障机器人": "https://huiji-public.huijistatic.com/sts2/uploads/6/65/Char_select_defect.png",
    }
    img_url = char_images.get(result["character"], "")
    b64 = await download_image_b64(img_url)

    if b64:
        msg = Message(MessageSegment.image(f"base64://{b64}") + MessageSegment.text(text))
    else:
        msg = Message(MessageSegment.text(text))
    await wiki_cmd.send(msg)


# ━━━━━━━━━━━━━━━━ 命令入口 ━━━━━━━━━━━━━━━━

@wiki_cmd.handle()
async def handle_wiki(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()

    if not raw:
        await wiki_cmd.send(
            "用法: #尖塔 <分类> <名称>\n"
            "分类: 卡牌/遗物/药水/词条/怪物\n"
            "示例: #尖塔 卡牌 打击\n"
            "　　  #尖塔 遗物 燃烧之血\n"
            "　　  #尖塔 怪物 史莱姆\n"
            "　　  #尖塔 储君\n"
            "　　  #尖塔 今日挑战\n"
            "管理: #尖塔 更新数据"
        )
        return

    # ── 更新数据 (管理员) ──
    if raw == "更新数据":
        if event.user_id not in ADMIN_USERS:
            await wiki_cmd.send("仅管理员可执行此操作")
            return
        await wiki_cmd.send("正在从 Wiki 下载数据，请稍候...")
        try:
            counts = await download_and_store()
            parts = [f"{TABX_LABELS[k]}: {v}条" for k, v in counts.items()]
            await wiki_cmd.send("✅ 数据更新完成！\n" + "\n".join(parts))
        except Exception as e:
            logger.exception("[wiki] 数据更新失败")
            await wiki_cmd.send(f"❌ 数据更新失败: {e}")
        return

    # ── 每日挑战 ──
    if raw in ("今日挑战", "每日挑战", "日常"):
        await handle_daily_challenge(bot, event)
        return

    # ── 解析分类 + 关键词 ──
    parts = raw.split(None, 1)
    category = None
    keyword = raw

    if len(parts) >= 2 and parts[0] in CATEGORY_KEYWORDS:
        category = CATEGORY_KEYWORDS[parts[0]]
        keyword = parts[1].strip()

    if not keyword:
        await wiki_cmd.send("请输入要搜索的名称")
        return

    await wiki_cmd.send(f"正在搜索「{keyword}」...")
    try:
        await search_and_reply(bot, event, keyword, category)
    except Exception as e:
        logger.exception(f"[wiki] 搜索异常: {e}")
        await wiki_cmd.send(f"搜索出错: {e}")
