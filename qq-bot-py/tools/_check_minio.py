"""诊断：比较 MinIO 对象名和 JSON 中的 image 字段"""
import json, os, yaml
from minio import Minio

os.chdir(os.path.dirname(__file__) or ".")

with open(os.path.join("..", "config.yaml"), encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

minio_cfg = cfg.get("meme", {}).get("minio", {})
mc = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=False,
)

# 1) MinIO 对象名
minio_names = set()
for o in mc.list_objects("wiki-images", recursive=True):
    minio_names.add(o.object_name)

print(f"MinIO objects: {len(minio_names)}")
sorted_names = sorted(minio_names)
for n in sorted_names[:5]:
    print(f"  {n!r}")

# 2) JSON card image field
print()
with open("card.json", "r", encoding="utf-8") as f:
    raw = json.load(f)

wt = raw["parse"]["wikitext"]["*"]
tabx = json.loads(wt)
fields = tabx.get("schema", {}).get("fields", [])
print("Fields:")
for i, f in enumerate(fields):
    print(f"  [{i}] {f['name']}")

rows = tabx.get("data", [])
print(f"\nRows: {len(rows)}")

# image is fields[11]
print("\nFirst 5 image values:")
for row in rows[:5]:
    img = row[11] if len(row) > 11 else "N/A"
    in_minio = img in minio_names
    alt = img.replace(" ", "_") if isinstance(img, str) else ""
    alt_in = alt in minio_names if alt else False
    print(f"  DB={img!r}  direct={in_minio}  underscore={alt_in}")

# 3) Count matches with normalization
def normalize(fn):
    fn = fn.replace(" ", "_")
    return fn[0].upper() + fn[1:] if fn else fn

hit = 0
miss = 0
miss_examples = []
for row in rows:
    img = row[11] if len(row) > 11 else ""
    if not img:
        continue
    normed = normalize(img)
    if normed in minio_names:
        hit += 1
    else:
        miss += 1
        if len(miss_examples) < 10:
            miss_examples.append((img, normed))

print(f"\nWith normalization - Matches: {hit}, Misses: {miss}")
if miss_examples:
    print("Miss examples:")
    for orig, normed in miss_examples:
        print(f"  DB={orig!r} -> normed={normed!r}")
