"""
离线批量表情包情绪分类脚本
================================
功能：遍历 MinIO memes 桶中的所有图片，使用 meme 用途路由分类。
     同步已有 MySQL 元数据与 Redis 情绪索引，保留管理员停用/删除状态。

Redis 写入结构：
  meme:tags:{object_name}       -> String  情绪标签（如 "happy"）
  meme:emotion:{label}          -> SET     拥有该标签的 object_name 集合

用法：
  cd qq-bot-py
  python tools/classify_memes.py              # 处理全部
  python tools/classify_memes.py --skip-done  # 已有合法 Redis 标签只补同步，不重复调用模型
  python tools/classify_memes.py --dry-run    # 只读预测，不写 DB、Redis、MinIO
"""

import argparse
import asyncio
import base64
import io
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
import httpx
import redis as redis_lib
from minio import Minio
from sqlalchemy import select
from plugins.db import MemeItem, get_session, engine
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ── 加载配置 ──────────────────────────────────────────
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

meme_cfg = config["meme"]
minio_cfg = meme_cfg["minio"]
gemini_cfg = meme_cfg.get("gemini", {})

GEMINI_API_KEY = gemini_cfg.get("api_key", "")
CLASSIFIER_MODEL = gemini_cfg.get("classifier_model", "gemini-2.5-pro")
GEMINI_PROXY = gemini_cfg.get("proxy", "") or None
GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta"

BUCKET = minio_cfg["bucket"]
POOL_KEY = "meme:pool"
TAG_PREFIX = "meme:tags:"
EMOTION_PREFIX = "meme:emotion:"

# 支持的情绪标签（Redis/DB 存英文）
EMOTION_DESCRIPTIONS = {
    "happy": "开心、可爱、甜、正向鼓励、露出笑容",
    "sad": "悲伤、委屈、哭泣、失落、破防",
    "angry": "愤怒、生气、急眼、暴躁、骂骂咧咧",
    "surprised": "惊讶、震惊、瞳孔地震、难以置信",
    "funny": "搞笑、沙雕、抽象、玩梗、喜剧效果",
    "cool": "酷、帅、高冷、装逼、从容、有压迫感",
    "disgusted": "嫌弃、恶心、反感、无语、看不下去",
    "confused": "疑惑、问号、懵圈、不理解、困惑",
    "curious": "好奇、探头、想看、围观、期待后续",
    "calm": "平静、温和、安慰、放松、淡定",
    "shy": "害羞、脸红、不好意思、扭捏、心动",
    "smug": "得意、欠揍、阴阳怪气、坏笑、自信挑衅",
    "neutral": "泛用、无明显情绪、普通反应、难以归类",
}
EMOTIONS = list(EMOTION_DESCRIPTIONS)
_ROUTE_RUNNER = asyncio.Runner()

# ── 客户端初始化 ───────────────────────────────────────
minio_client = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=minio_cfg.get("secure", False),
)

rds = redis_lib.Redis(
    host=config["redis"]["host"],
    port=config["redis"]["port"],
    password=config["redis"].get("password") or None,
    db=int(config["redis"].get("db", 0)),
    decode_responses=True,
)

# ── 工具函数 ──────────────────────────────────────────

MAX_IMAGE_PX = 512  # 压缩后最长边像素，减少 payload


def _compress_image(data: bytes, ext: str) -> tuple[bytes, str] | None:
    """
    用 Pillow 将图片缩小到 MAX_IMAGE_PX 并转为 JPEG。
    返回 None 表示该格式不支持，调用方应跳过此图片。
    """
    if not _HAS_PIL:
        # 没有 PIL 时 GIF 无法处理，跳过
        if ext == "gif":
            return None
        return data, ext
    try:
        img = Image.open(io.BytesIO(data))
        # 动图（GIF/WebP）取第一帧，用 n_frames 判断
        if getattr(img, 'n_frames', 1) > 1:
            img.seek(0)
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "jpg"
    except Exception as e:
        if ext == "gif":
            print(f"  [WARN] GIF 处理失败: {e}，跳过")
            return None
        return data, ext


def _image_to_base64(data: bytes, ext: str) -> str:
    return base64.b64encode(data).decode()


def _classify_image(data: bytes, ext: str, retries: int = 3) -> str | None:
    """
    发送图片到 Gemini Pro，返回情绪标签或 "invalid"（非表情包）。
    失败返回 None。自动压缩图片、支持最多 retries 次重试（指数退避）。
    """
    b64 = _image_to_base64(data, ext)
    mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
    prompt = (
        f"请判断这张图片：\n"
        f"第一步：它是否是表情包/贴纸/搞笑反应图/颜文字/gif动图（即人们在聊天中发送用来表达情绪、调侃、互动的图）？\n"
        f"如果不是表情包（例如游戏卡牌、游戏截图、风景照片、产品图、正经内容、纯文字图片等），只回复: invalid\n"
        f"第二步：如果是表情包，只能从下面标签中选最匹配的一个，输出左侧英文标签：\n"
        + "\n".join(f"- {k}: {v}" for k, v in EMOTION_DESCRIPTIONS.items())
        + "\n只回复一个英文标签或 invalid，不要解释，不要标点。"
    )

    from plugins.model_routes import complete
    for attempt in range(1, retries + 1):
        try:
            result = _ROUTE_RUNNER.run(complete("meme", [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}], purpose="meme_classification", timeout=30))
            label = result.strip().lower().split()[0].rstrip(".,!?;:")
            return label if label in EMOTIONS or label == "invalid" else None
        except Exception as exc:
            print(f"  [WARN] classification failed attempt={attempt}: {type(exc).__name__}")
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None


def _download_from_minio(object_name: str) -> bytes | None:
    """从 MinIO 下载对象内容"""
    try:
        resp = minio_client.get_object(BUCKET, object_name)
        data = resp.read()
        resp.close()
        return data
    except Exception as e:
        print(f"  [ERROR] MinIO 下载失败 {object_name}: {e}")
        return None


async def _sync_classification(object_name: str, label: str, *, deleted=False):
    """Update existing metadata only; never reactivate administrative exclusions."""
    if not deleted and label not in EMOTIONS:
        raise ValueError("Invalid emotion label")
    async with await get_session() as session:
        item = (await session.execute(select(MemeItem).where(
            MemeItem.bucket == BUCKET, MemeItem.object_name == object_name).with_for_update())).scalar_one_or_none()
        if item is None:
            if deleted:
                # An untracked rejected object still needs a tombstone. If its
                # storage deletion fails, the sync command must not reactivate it.
                now = datetime.now()
                session.add(MemeItem(bucket=BUCKET, object_name=object_name, status="deleted",
                                     collected_at=now, updated_at=now, tags="[]"))
                await session.commit()
                return "deleted"
            return None
        now = datetime.now()
        if deleted:
            item.status = "deleted"
        else:
            if item.emotion != label or item.classified_at is None:
                item.classified_at = now
            item.emotion = label
        item.updated_at = now
        status = item.status
        await session.commit()
        return status


async def _load_routes_readonly():
    """Read console overrides without triggering the normal startup migration."""
    import json
    from plugins import model_routes
    from plugins.console_state import ConsoleSetting
    async with await get_session() as session:
        rows = (await session.execute(select(ConsoleSetting).where(ConsoleSetting.key.like("route:%")))).scalars().all()
    model_routes._OVERRIDES.clear()
    model_routes._VERSIONS.clear()
    for row in rows:
        role = row.key.split(":", 1)[1]
        if role in model_routes.DEFAULTS:
            model_routes._OVERRIDES[role] = model_routes.validate_patch(json.loads(row.values_json))
            model_routes._VERSIONS[role] = row.version


def _write_to_redis(object_name: str, label: str, dry_run: bool):
    """Synchronize DB first, then rebuild only this object's Redis membership."""
    if label not in EMOTIONS:
        raise ValueError("Invalid emotion label")
    tag_key = f"{TAG_PREFIX}{object_name}"
    emotion_key = f"{EMOTION_PREFIX}{label}"

    if dry_run:
        print(f"  [DRY-RUN] 将同步已有数据库标签与 Redis: {tag_key} = {label}")
        return
    status = _ROUTE_RUNNER.run(_sync_classification(object_name, label))
    pipe = rds.pipeline()
    for emotion in EMOTIONS:
        pipe.srem(f"{EMOTION_PREFIX}{emotion}", object_name)
    pipe.srem(POOL_KEY, object_name)
    pipe.set(tag_key, label)
    if status == "active":
        pipe.sadd(POOL_KEY, object_name)
        pipe.sadd(emotion_key, object_name)
    pipe.execute()
    if status is None:
        print("  [WARN] 缺少数据库元数据，标签已缓存；运行 tools/sync_memes_db.py 后加入有效池")


def _delete_meme(object_name: str, dry_run: bool):
    """Keep historical DB counts while marking a rejected object as deleted."""
    tag_key = f"{TAG_PREFIX}{object_name}"

    if dry_run:
        print(f"  [DRY-RUN] 将标记数据库 deleted 并删除: MinIO {object_name} + Redis 索引")
        return
    _ROUTE_RUNNER.run(_sync_classification(object_name, "", deleted=True))
    pipe = rds.pipeline()
    for emotion in EMOTIONS:
        pipe.srem(f"{EMOTION_PREFIX}{emotion}", object_name)
    pipe.delete(tag_key)
    pipe.srem(POOL_KEY, object_name)
    pipe.execute()

    # 从 MinIO 删除
    try:
        minio_client.remove_object(BUCKET, object_name)
    except Exception as e:
        print(f"  [WARN] MinIO 删除失败: {e}")


# ── 主流程 ────────────────────────────────────────────

def main():
    from plugins import model_routes
    parser = argparse.ArgumentParser(description="批量分类 MinIO 表情包情绪")
    parser.add_argument("--skip-done", action="store_true", help="已有合法标签只补数据库与索引，不再调用模型")
    parser.add_argument("--dry-run", action="store_true", help="不写入数据库、Redis 或 MinIO，仅打印预测结果")
    parser.add_argument("--delay", type=float, default=1.0, help="每次 API 调用间隔秒数（默认 1.0）")
    parser.add_argument("--purge-invalid", action="store_true", help="将 AI 判断为非表情包的图片从 MinIO+Redis 中删除")
    args = parser.parse_args()
    try:
        _ROUTE_RUNNER.run(_load_routes_readonly())
    except Exception as exc:
        print(f"[ERROR] Cannot read effective model routes: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(f"[classify_memes] 模型: {__import__('plugins.model_routes', fromlist=['resolve']).resolve('meme').model}")
    print(f"[classify_memes] skip-done={args.skip_done}  dry-run={args.dry_run}  purge-invalid={args.purge_invalid}  delay={args.delay}s")
    print()

    # 获取所有 MinIO 对象
    objects = list(minio_client.list_objects(BUCKET))
    total = len(objects)
    print(f"[classify_memes] MinIO bucket '{BUCKET}' 共 {total} 个对象")
    if total == 0:
        print("[classify_memes] 没有可分类的表情包，退出")
        return

    done = 0
    skipped = 0
    failed = 0

    for i, obj in enumerate(objects, 1):
        name = obj.object_name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "jpg"
        tag_key = f"{TAG_PREFIX}{name}"

        prefix = f"[{i}/{total}]"

        # 跳过已分类
        existing = rds.get(tag_key) if args.skip_done else None
        if existing in EMOTIONS:
            print(f"{prefix} 跳过（已分类 {existing}）: {name}")
            try:
                _write_to_redis(name, existing, dry_run=args.dry_run)
                skipped += 1
            except Exception as exc:
                print(f"  [ERROR] Metadata/index sync failed: {type(exc).__name__}")
                failed += 1
            continue

        print(f"{prefix} 下载: {name} ...", end=" ", flush=True)
        data = _download_from_minio(name)
        if data is None:
            print("下载失败")
            failed += 1
            continue
        print(f"{len(data)//1024}KB", end=" → ", flush=True)

        # 压缩图片减少 payload（同时处理 GIF 取帧）
        result = _compress_image(data, ext)
        if result is None:
            print("格式不支持，跳过（建议在服务器安装 Pillow）")
            skipped += 1
            continue
        data, ext = result
        print(f"({len(data)//1024}KB压缩后)", end=" ", flush=True)

        label = _classify_image(data, ext)
        if label is None:
            print("分类失败")
            failed += 1
            continue

        if label == "invalid":
            if args.purge_invalid:
                print("非表情包，删除")
                try:
                    _delete_meme(name, dry_run=args.dry_run)
                except Exception as exc:
                    print(f"  [ERROR] Purge failed: {type(exc).__name__}")
                failed += 1  # 统计为已处理（用 failed 字段复用表示删除数）
            else:
                print("非表情包（跳过，使用 --purge-invalid 可自动删除）")
                skipped += 1
            if i < total:
                time.sleep(args.delay)
            continue

        print(label)
        try:
            _write_to_redis(name, label, dry_run=args.dry_run)
            done += 1
        except Exception as exc:
            print(f"  [ERROR] Metadata/index sync failed: {type(exc).__name__}")
            failed += 1

        if i < total:
            time.sleep(args.delay)

    print()
    print(f"[classify_memes] 完成: 成功={done}, 跳过={skipped}, 失败={failed}")

    if not args.dry_run:
        # 打印各情绪统计
        print("\n当前情绪分布：")
        for em in EMOTIONS:
            count = rds.scard(f"{EMOTION_PREFIX}{em}")
            if count > 0:
                print(f"  {em}: {count} 张")


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    finally:
        _ROUTE_RUNNER.run(engine.dispose())
        _ROUTE_RUNNER.close()
