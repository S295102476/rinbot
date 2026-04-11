"""
本地运行此脚本，从浏览器保存的 JSON 文件生成 wiki_data.sql
然后将 wiki_data.sql 上传到服务器并导入 MySQL

使用方法:
  1. 用浏览器打开以下 4 个链接，每个页面 Ctrl+S 保存到 tools/ 目录:
       https://sts2.huijiwiki.com/api.php?action=parse&page=Data:Card.tabx&prop=wikitext&format=json
       https://sts2.huijiwiki.com/api.php?action=parse&page=Data:Relic.tabx&prop=wikitext&format=json
       https://sts2.huijiwiki.com/api.php?action=parse&page=Data:Potion.tabx&prop=wikitext&format=json
       https://sts2.huijiwiki.com/api.php?action=parse&page=Data:Modifier.tabx&prop=wikitext&format=json
  2. 保存文件名分别为: card.json, relic.json, potion.json, modifier.json
  3. 运行: py tools/download_wiki_data.py
"""
import json
import sys
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

LOCAL_FILES = {
    "card":     os.path.join(TOOLS_DIR, "card.json"),
    "relic":    os.path.join(TOOLS_DIR, "relic.json"),
    "potion":   os.path.join(TOOLS_DIR, "potion.json"),
    "modifier": os.path.join(TOOLS_DIR, "modifier.json"),
}

def esc(v) -> str:
    """转义字符串用于 SQL INSERT"""
    if v is None:
        return "NULL"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\r", "").replace("\n", "\\n")
    return f"'{s}'"

def load_tabx(cat: str, filepath: str) -> list:
    if not os.path.exists(filepath):
        print(f"  ❌ 找不到文件: {filepath}")
        print(f"     请用浏览器打开对应链接后 Ctrl+S 保存到该路径")
        return []
    print(f"  正在读取 {os.path.basename(filepath)} ...", end=" ")
    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    wikitext = raw.get("parse", {}).get("wikitext", {}).get("*", "")
    if not wikitext.strip():
        print("失败：wikitext 为空")
        return []
    tabx = json.loads(wikitext)
    rows = tabx.get("data", [])
    print(f"OK ({len(rows)} 条)")
    return rows


def main():
    output_file = "wiki_data.sql"
    lines = []

    lines.append("-- 由 tools/download_wiki_data.py 自动生成")
    lines.append("-- 导入方式: mysql -u qqbot -p***REDACTED*** qqbot < wiki_data.sql")
    lines.append("")
    lines.append("SET NAMES utf8mb4;")
    lines.append("")
    lines.append("-- 建表（如已存在则跳过）")
    lines.append("""CREATE TABLE IF NOT EXISTS wiki_cards (
  id INT AUTO_INCREMENT PRIMARY KEY,
  card_id VARCHAR(64),
  name VARCHAR(128),
  color VARCHAR(64),
  rarity VARCHAR(64),
  card_type VARCHAR(64),
  cost VARCHAR(16),
  description TEXT,
  description_raw TEXT,
  upgrade_ref VARCHAR(64),
  compendium_order INT DEFAULT 0,
  image VARCHAR(256),
  page VARCHAR(256)
) CHARACTER SET utf8mb4;""")
    lines.append("")
    lines.append("""CREATE TABLE IF NOT EXISTS wiki_relics (
  id INT AUTO_INCREMENT PRIMARY KEY,
  relic_id VARCHAR(64),
  name VARCHAR(128),
  pool VARCHAR(64),
  tier VARCHAR(64),
  description TEXT,
  description_raw TEXT,
  flavor TEXT,
  ancient VARCHAR(16),
  compendium_order INT DEFAULT 0,
  image VARCHAR(256),
  page VARCHAR(256)
) CHARACTER SET utf8mb4;""")
    lines.append("")
    lines.append("""CREATE TABLE IF NOT EXISTS wiki_potions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  potion_id VARCHAR(64),
  name VARCHAR(128),
  color VARCHAR(64),
  tier VARCHAR(64),
  description TEXT,
  description_raw TEXT,
  compendium_order INT DEFAULT 0,
  image VARCHAR(256),
  page VARCHAR(256)
) CHARACTER SET utf8mb4;""")
    lines.append("")
    lines.append("""CREATE TABLE IF NOT EXISTS wiki_modifiers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  modifier_id VARCHAR(64),
  name VARCHAR(128),
  description TEXT,
  image VARCHAR(256),
  kind VARCHAR(32)
) CHARACTER SET utf8mb4;""")
    lines.append("")

    # ── Card ──────────────────────────────────────────────
    rows = load_tabx("card", LOCAL_FILES["card"])
    if rows:
        lines.append("DELETE FROM wiki_cards;")
        for r in rows:
            vals = ", ".join([
                esc(r[1]),  # card_id
                esc(r[2]),  # name
                esc(r[3]),  # color
                esc(r[4]),  # rarity
                esc(r[5]),  # card_type
                esc(r[6]),  # cost
                esc(r[7]),  # description
                esc(r[8]),  # description_raw
                esc(r[9]),  # upgrade_ref
                esc(int(r[10]) if r[10] else 0),  # compendium_order
                esc(r[11]), # image
                esc(r[12]), # page
            ])
            lines.append(
                f"INSERT INTO wiki_cards "
                f"(card_id,name,color,rarity,card_type,cost,description,description_raw,upgrade_ref,compendium_order,image,page) "
                f"VALUES ({vals});"
            )
        lines.append("")

    # ── Relic ─────────────────────────────────────────────
    rows = load_tabx("relic", LOCAL_FILES["relic"])
    if rows:
        lines.append("DELETE FROM wiki_relics;")
        for r in rows:
            vals = ", ".join([
                esc(r[1]),  # relic_id
                esc(r[2]),  # name
                esc(r[3]),  # pool
                esc(r[4]),  # tier
                esc(r[5]),  # description
                esc(r[6]),  # description_raw
                esc(r[7]),  # flavor
                esc(r[8]),  # ancient
                esc(int(r[9]) if r[9] else 0),  # compendium_order
                esc(r[10]), # image
                esc(r[11]), # page
            ])
            lines.append(
                f"INSERT INTO wiki_relics "
                f"(relic_id,name,pool,tier,description,description_raw,flavor,ancient,compendium_order,image,page) "
                f"VALUES ({vals});"
            )
        lines.append("")

    # ── Potion ────────────────────────────────────────────
    rows = load_tabx("potion", LOCAL_FILES["potion"])
    if rows:
        lines.append("DELETE FROM wiki_potions;")
        for r in rows:
            vals = ", ".join([
                esc(r[1]),  # potion_id
                esc(r[2]),  # name
                esc(r[3]),  # color
                esc(r[4]),  # tier
                esc(r[5]),  # description
                esc(r[6]),  # description_raw
                esc(int(r[7]) if r[7] else 0),  # compendium_order
                esc(r[8]),  # image
                esc(r[9]),  # page
            ])
            lines.append(
                f"INSERT INTO wiki_potions "
                f"(potion_id,name,color,tier,description,description_raw,compendium_order,image,page) "
                f"VALUES ({vals});"
            )
        lines.append("")

    # ── Modifier ──────────────────────────────────────────
    rows = load_tabx("modifier", LOCAL_FILES["modifier"])
    if rows:
        lines.append("DELETE FROM wiki_modifiers;")
        for r in rows:
            vals = ", ".join([
                esc(r[1]),  # modifier_id
                esc(r[2]),  # name
                esc(r[3]),  # description
                esc(r[4]),  # image
                esc(r[5]),  # kind
            ])
            lines.append(
                f"INSERT INTO wiki_modifiers "
                f"(modifier_id,name,description,image,kind) "
                f"VALUES ({vals});"
            )
        lines.append("")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n✅ 已生成 {output_file}")
    print("\n下一步——把文件传到服务器并导入：")
    print("  scp wiki_data.sql root@<服务器IP>:/opt/qq-bot-py/")
    print("  ssh root@<服务器IP>")
    print("  mysql -u qqbot -p***REDACTED*** qqbot < /opt/qq-bot-py/wiki_data.sql")


if __name__ == "__main__":
    main()
