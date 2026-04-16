"""分析 monster.json 数据结构"""
import json, os

with open(os.path.join("tools", "monster.json"), "r", encoding="utf-8") as f:
    raw = json.load(f)

wt = raw["parse"]["wikitext"]["*"]
tabx = json.loads(wt)

fields = tabx.get("schema", {}).get("fields", [])
print("Fields:")
for i, f in enumerate(fields):
    print(f"  [{i}] {f['name']}")

rows = tabx.get("data", [])
print(f"\nTotal rows: {len(rows)}")

print("\nFirst 3 rows:")
for r in rows[:3]:
    print(f"  {r}")

# Check if there's an image field
img_idx = None
for i, f in enumerate(fields):
    if "image" in f["name"].lower() or "img" in f["name"].lower():
        img_idx = i
        print(f"\nImage field found at index {i}: {f['name']}")
        break

if img_idx is not None:
    print("Image values (first 5):")
    for r in rows[:5]:
        print(f"  {r[img_idx]!r}")
else:
    print("\nNo image field found in schema")
    # Check for any field that might contain image data
    print("Looking for image-like values in first row...")
    for i, val in enumerate(rows[0]):
        if isinstance(val, str) and (".png" in val.lower() or ".jpg" in val.lower()):
            print(f"  [{i}] {fields[i]['name']}: {val!r}")
