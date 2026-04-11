"""临时脚本：检查数据库 image 字段和 MinIO 对象名"""
import asyncio
import yaml
from minio import Minio
from sqlalchemy import text

# DB
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from plugins.db import SessionFactory

with open(os.path.join(os.path.dirname(__file__), "..", "config.yaml"), encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

minio_cfg = cfg.get("meme", {}).get("minio", {})
mc = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=False,
)

async def main():
    # 1) 列出 MinIO 前10个对象名
    print("=== MinIO wiki-images bucket 前10个对象 ===")
    objs = list(mc.list_objects("wiki-images", recursive=True))
    for i, o in enumerate(objs[:10]):
        print(f"  {o.object_name!r}  ({o.size} bytes)")
    print(f"  ... 共 {len(objs)} 个对象\n")

    # 2) 列出 DB 前10个 image 字段
    print("=== DB wiki_cards 前10个 image 字段 ===")
    async with SessionFactory() as s:
        r = await s.execute(text("SELECT name, image FROM wiki_cards LIMIT 10"))
        for row in r.fetchall():
            print(f"  name={row[0]!r}  image={row[1]!r}")

    # 3) 对比一下具体例子
    print("\n=== 对比 Twin Strike ===")
    async with SessionFactory() as s:
        r = await s.execute(text("SELECT name, image FROM wiki_cards WHERE image LIKE '%win%trike%' LIMIT 3"))
        for row in r.fetchall():
            db_name = row[1]
            print(f"  DB: {db_name!r}")
            # 检查 MinIO 是否有此键
            try:
                stat = mc.stat_object("wiki-images", db_name)
                print(f"  MinIO 命中: {stat.size} bytes")
            except Exception as e:
                print(f"  MinIO 未命中: {e}")
                # 尝试空格→下划线
                alt = db_name.replace(" ", "_") if db_name else ""
                if alt != db_name:
                    try:
                        stat = mc.stat_object("wiki-images", alt)
                        print(f"  MinIO 用替代名命中: {alt!r} ({stat.size} bytes)")
                    except:
                        print(f"  替代名也未命中: {alt!r}")

asyncio.run(main())
