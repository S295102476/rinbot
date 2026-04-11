"""
批量上传 wiki 图片到 MinIO

此脚本分两步工作:

━━ 第一步: 生成下载链接 ━━
  py tools/upload_wiki_images.py --gen-urls

  会从本地 JSON 文件中提取所有图片文件名，生成:
  - tools/image_urls.txt   (所有图片的 CDN 直链，一行一个)

  然后用浏览器扩展(如 DownThemAll)或 IDM 批量下载到 tools/wiki_images/ 目录

━━ 第二步: 上传到 MinIO ━━
  py tools/upload_wiki_images.py --upload

  将 tools/wiki_images/ 目录中的所有图片文件上传到 MinIO 的 wiki-images bucket

依赖: pip install minio
"""
import json
import os
import sys
import hashlib
from urllib.parse import quote

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(TOOLS_DIR, "wiki_images")

LOCAL_FILES = {
    "card":     os.path.join(TOOLS_DIR, "card.json"),
    "relic":    os.path.join(TOOLS_DIR, "relic.json"),
    "potion":   os.path.join(TOOLS_DIR, "potion.json"),
    "modifier": os.path.join(TOOLS_DIR, "modifier.json"),
}

# tabx 各类别中 image 字段的位序
IMAGE_INDEX = {
    "card": 11,
    "relic": 10,
    "potion": 8,
    "modifier": 4,
}

# 角色选择图（每日挑战用）
EXTRA_IMAGES = [
    "Char_select_ironclad.png",
    "Char_select_silent.png",
    "Char_select_regent.png",
    "Char_select_necrobinder.png",
    "Char_select_defect.png",
]


def make_cdn_url(filename: str) -> str:
    fname = filename.replace(" ", "_")
    fname = fname[0].upper() + fname[1:] if fname else fname
    md5 = hashlib.md5(fname.encode("utf-8")).hexdigest()
    return f"https://huiji-public.huijistatic.com/sts2/uploads/{md5[0]}/{md5[:2]}/{quote(fname)}"


def collect_image_names() -> list[str]:
    """从本地 JSON 文件提取所有图片文件名"""
    names = set()
    for cat, filepath in LOCAL_FILES.items():
        if not os.path.exists(filepath):
            print(f"  跳过: {filepath} (不存在)")
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            raw = json.load(f)
        wikitext = raw.get("parse", {}).get("wikitext", {}).get("*", "")
        tabx = json.loads(wikitext)
        rows = tabx.get("data", [])
        idx = IMAGE_INDEX[cat]
        for r in rows:
            img = r[idx] if idx < len(r) else ""
            if img and img.strip():
                names.add(img.strip())
        print(f"  {cat}: {len(rows)} 条数据")

    for name in EXTRA_IMAGES:
        names.add(name)

    return sorted(names)


def gen_urls():
    print("正在提取图片文件名...")
    names = collect_image_names()
    print(f"共 {len(names)} 张图片\n")

    urls_path = os.path.join(TOOLS_DIR, "image_urls.txt")
    with open(urls_path, "w", encoding="utf-8") as f:
        for name in names:
            url = make_cdn_url(name)
            f.write(url + "\n")

    print(f"✅ 已生成 {urls_path}")
    print(f"\n请用浏览器下载工具批量下载这些图片到:\n  {IMAGES_DIR}/")
    print(f"\n推荐方法:")
    print(f"  1) 安装 Chrome 扩展 'DownThemAll' 或使用 IDM")
    print(f"  2) 导入 {urls_path} 批量下载")
    print(f"  3) 保存到 {IMAGES_DIR}/ 目录")
    print(f"\n下载完成后运行:")
    print(f"  py tools/upload_wiki_images.py --upload")


def upload():
    if not os.path.isdir(IMAGES_DIR):
        print(f"❌ 找不到图片目录: {IMAGES_DIR}")
        print("请先运行 --gen-urls 并下载图片")
        return

    files = [f for f in os.listdir(IMAGES_DIR) if os.path.isfile(os.path.join(IMAGES_DIR, f))]
    if not files:
        print(f"❌ 图片目录为空: {IMAGES_DIR}")
        return

    print(f"找到 {len(files)} 个文件，正在上传到 MinIO...")

    from minio import Minio
    import yaml

    config_path = os.path.join(os.path.dirname(TOOLS_DIR), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    minio_cfg = config["meme"]["minio"]
    client = Minio(
        minio_cfg["endpoint"],
        access_key=minio_cfg["access_key"],
        secret_key=minio_cfg["secret_key"],
        secure=minio_cfg.get("secure", False),
    )

    bucket = "wiki-images"
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        print(f"  已创建 bucket: {bucket}")

    success = 0
    for f in files:
        filepath = os.path.join(IMAGES_DIR, f)
        size = os.path.getsize(filepath)
        if size < 100:
            print(f"  跳过 {f} (太小，可能是错误页面)")
            continue

        ext = f.rsplit(".", 1)[-1].lower() if "." in f else "png"
        content_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "webp": "image/webp",
        }.get(ext, "image/png")

        try:
            client.fput_object(bucket, f, filepath, content_type=content_type)
            success += 1
        except Exception as e:
            print(f"  ❌ {f}: {e}")

    print(f"\n✅ 上传完成: {success}/{len(files)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  py tools/upload_wiki_images.py --gen-urls    生成下载链接")
        print("  py tools/upload_wiki_images.py --upload      上传到 MinIO")
        sys.exit(1)

    if sys.argv[1] == "--gen-urls":
        gen_urls()
    elif sys.argv[1] == "--upload":
        upload()
    else:
        print(f"未知参数: {sys.argv[1]}")
