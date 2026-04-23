"""
离线批量表情包情绪分类脚本
================================
功能：遍历 MinIO memes 桶中的所有图片，通过 Gemini Pro（OpenClaw 代理）
     判断每张图片的情绪标签，并将结果写入 Redis。

Redis 写入结构：
  meme:tags:{object_name}       -> String  情绪标签（如 "happy"）
  meme:emotion:{label}          -> SET     拥有该标签的 object_name 集合

用法：
  cd qq-bot-py
  python tools/classify_memes.py              # 处理全部
  python tools/classify_memes.py --skip-done  # 跳过已分类的
  python tools/classify_memes.py --dry-run    # 不写 Redis，仅打印结果
"""

import argparse
import base64
import io
import sys
import time
import yaml
import httpx
import redis as redis_lib
from minio import Minio
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
GEMINI_BASE_URL = gemini_cfg.get("base_url", "https://openclawroot.com/v1")
CLASSIFIER_MODEL = gemini_cfg.get("classifier_model", "gemini-2.5-pro")

BUCKET = minio_cfg["bucket"]
POOL_KEY = "meme:pool"
TAG_PREFIX = "meme:tags:"
EMOTION_PREFIX = "meme:emotion:"

# 支持的情绪标签（中英文对照仅供提示词使用，Redis 存英文）
EMOTIONS = [
    "happy",      # 开心、搞笑、可爱萌
    "sad",        # 悲伤、委屈、哭泣
    "angry",      # 愤怒、生气、嫌弃
    "surprised",  # 惊讶、震惊
    "funny",      # 滑稽、搞怪、沙雕
    "cool",       # 酷、高冷
    "disgusted",  # 恶心、反感
    "confused",   # 疑惑、懵圈、问号
    "neutral",    # 无明显情绪
]

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
    if not GEMINI_API_KEY:
        print("[ERROR] gemini.api_key 未配置，请在 config.yaml 中填入 API Key", file=sys.stderr)
        sys.exit(1)

    b64 = _image_to_base64(data, ext)
    mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
    data_uri = f"data:{mime};base64,{b64}"

    prompt = (
        f"请判断这张图片：\n"
        f"第一步：它是否是表情包/贴纸/搞笑反应图/颜文字/gif动图（即人们在聊天中发送用来表达情绪、调侃、互动的图）？\n"
        f"如果不是表情包（例如游戏卡牌、游戏截图、风景照片、产品图、正经内容、纯文字图片等），只回复: invalid\n"
        f"第二步：如果是表情包，从以下标签中选最匹配的一个：{', '.join(EMOTIONS)}\n"
        f"只回复一个英文单词，不要解释，不要标点。"
    )

    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 10,
        "temperature": 0,
    }

    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(f"{GEMINI_BASE_URL}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                result = resp.json()
                label = result["choices"][0]["message"]["content"].strip().lower()
                label = label.split()[0].rstrip(".,!?;:")
                if label == "invalid":
                    return "invalid"
                if label not in EMOTIONS:
                    print(f"  [WARN] 模型返回未知标签 '{label}'，归为 neutral")
                    label = "neutral"
                return label
        except httpx.HTTPStatusError as e:
            wait = 2 ** attempt
            print(f"  [WARN] HTTP {e.response.status_code} (第{attempt}次), {wait}s后重试: {e.response.text[:120]}")
            if attempt < retries:
                time.sleep(wait)
        except Exception as e:
            wait = 2 ** attempt
            print(f"  [WARN] 请求失败 (第{attempt}次), {wait}s后重试: {e}")
            if attempt < retries:
                time.sleep(wait)
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


def _write_to_redis(object_name: str, label: str, dry_run: bool):
    """将情绪标签写入 Redis"""
    tag_key = f"{TAG_PREFIX}{object_name}"
    emotion_key = f"{EMOTION_PREFIX}{label}"

    if dry_run:
        print(f"  [DRY-RUN] 将写入: {tag_key} = {label}  |  SADD {emotion_key}")
        return

    # 如果该对象之前有旧标签，先从旧标签集合中移除
    old_label = rds.get(tag_key)
    if old_label and old_label != label:
        rds.srem(f"{EMOTION_PREFIX}{old_label}", object_name)

    rds.set(tag_key, label)
    rds.sadd(emotion_key, object_name)


def _delete_meme(object_name: str, dry_run: bool):
    """从 MinIO + Redis 完全删除一张无效的非表情包图片"""
    tag_key = f"{TAG_PREFIX}{object_name}"

    if dry_run:
        print(f"  [DRY-RUN] 将删除: MinIO {object_name} + Redis 索引")
        return

    # 清除 Redis 中所有索引
    old_label = rds.get(tag_key)
    if old_label:
        rds.srem(f"{EMOTION_PREFIX}{old_label}", object_name)
    rds.delete(tag_key)
    rds.srem(POOL_KEY, object_name)

    # 从 MinIO 删除
    try:
        minio_client.remove_object(BUCKET, object_name)
    except Exception as e:
        print(f"  [WARN] MinIO 删除失败: {e}")


# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="批量分类 MinIO 表情包情绪")
    parser.add_argument("--skip-done", action="store_true", help="跳过已有标签的表情包")
    parser.add_argument("--dry-run", action="store_true", help="不写入 Redis，仅打印预测结果")
    parser.add_argument("--delay", type=float, default=1.0, help="每次 API 调用间隔秒数（默认 1.0）")
    parser.add_argument("--purge-invalid", action="store_true", help="将 AI 判断为非表情包的图片从 MinIO+Redis 中删除")
    args = parser.parse_args()

    print(f"[classify_memes] 模型: {CLASSIFIER_MODEL}")
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
        if args.skip_done and rds.exists(tag_key):
            existing = rds.get(tag_key)
            print(f"{prefix} 跳过（已分类 {existing}）: {name}")
            skipped += 1
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
                _delete_meme(name, dry_run=args.dry_run)
                failed += 1  # 统计为已处理（用 failed 字段复用表示删除数）
            else:
                print("非表情包（跳过，使用 --purge-invalid 可自动删除）")
                skipped += 1
            if i < total:
                time.sleep(args.delay)
            continue

        print(label)
        _write_to_redis(name, label, dry_run=args.dry_run)
        done += 1

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
    main()
