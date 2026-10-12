# 开发计划：签到 / COC跑团 / 搜图

> 创建日期: 2026-04-09
> 状态: 规划中

---

## 总体依赖变更

```txt
# requirements.txt 新增
Pillow
SQLAlchemy[asyncio]
aiomysql
```

## 数据库基础层（前置）

所有需要持久化的模块共用一个 MySQL 连接池。

**config.yaml 新增：**
```yaml
database:
  host: 127.0.0.1
  port: 3306
  user: qqbot
  password: "xxx"
  database: qqbot
```

**公共模块 `plugins/db.py`：**
- 使用 `SQLAlchemy[asyncio]` + `aiomysql` 创建 `AsyncEngine`
- 提供 `Base`（declarative_base）、`get_session()`
- NoneBot2 `on_startup` 时 `create_all()` 自动建表

---

## 一、每日签到 (`plugins/sign_in.py`)

### 触发
- 指令: `#签到`
- 优先级: 5, block=True

### 功能
用户每日签到一次，生成图片卡片返回，包含：
- 日期、时段问候语（早上好/下午好/晚上好）
- QQ昵称、QQ头像（圆形裁剪）
- 好感度变更（+N）、当前总好感度
- 群内好感度排名
- 今日一言（hitokoto API）
- 随机二次元背景图

### 数据表 `sign_in`
```sql
CREATE TABLE sign_in (
    user_id    BIGINT NOT NULL,
    group_id   BIGINT NOT NULL,
    affection  INT DEFAULT 0,           -- 好感度
    total_days INT DEFAULT 0,           -- 总签到天数
    continuous INT DEFAULT 0,           -- 连续签到天数
    last_date  DATE,                    -- 最后签到日期
    PRIMARY KEY (user_id, group_id)
);
```

### config.yaml 新增
```yaml
sign_in:
  affection_range: [1, 10]    # 每日签到好感度随机范围
  continuous_bonus: 5          # 连续签到额外奖励
  bg_api: "https://www.dmoe.cc/random.php"   # 随机二次元背景图API（返回302跳转图片）
  hitokoto_api: "https://v1.hitokoto.cn"     # 一言API
```

### 图片渲染流程 (Pillow)
1. httpx GET 随机二次元背景图 → `Image.open(BytesIO(data))`
2. Resize 到固定尺寸 (900×500)
3. 左侧绘制半透明黑色矩形遮罩
4. httpx GET QQ头像 `http://q1.qlogo.cn/g?b=qq&nk={qq}&s=640` → 圆形裁剪 → 粘贴
5. `ImageDraw.text()` 写入：问候语、昵称、好感度、排名、一言
6. 导出 BytesIO → `MessageSegment.image(bio)`

### 外部API
| API | 用途 | 国内可访问 |
|-----|------|-----------|
| `www.dmoe.cc/random.php` | 随机二次元壁纸 | ✅ |
| `v1.hitokoto.cn` | 一言 | ✅ |
| `q1.qlogo.cn/g?b=qq&nk={qq}&s=640` | QQ头像 | ✅ |

### 依赖
- Pillow（图片渲染）
- SQLAlchemy + aiomysql（数据库）
- httpx（已有）

---

## 二、COC跑团 (`plugins/coc_dice.py`)

### 触发
- 消息以 `.` 开头的指令，正则匹配
- 优先级: 6, block=True
- `on_message` + 自定义 Rule 检测 `.` 前缀

### 完整指令列表

| 指令 | 格式 | 说明 | 示例 |
|------|------|------|------|
| `.r` | `.r [N]d[M][+/-K]` | 通用掷骰 | `.r 3d6+2`、`.r d100`、`.r 2d10` |
| `.rd` | `.rd` | 快速 d100 | `.rd` |
| `.ra` | `.ra [属性名] [值]` | 属性检定（对比属性值判定成败） | `.ra 力量`、`.ra 侦查 60` |
| `.rh` | `.rh [表达式]` | 暗骰（结果私聊发送者） | `.rh d100` |
| `.rb` | `.rb [N]` | 奖励骰（多投N个十位取最小） | `.rb`、`.rb 2` |
| `.rp` | `.rp [N]` | 惩罚骰（多投N个十位取最大） | `.rp` |
| `.coc` | `.coc [N]` | 快速建卡（生成N套属性） | `.coc`、`.coc 3` |
| `.st` | `.st [属性 值] ...` | 设置角色属性 | `.st 力量 60 敏捷 70` |
| `.st show` | `.st show` | 查看当前角色卡 | `.st show` |
| `.sc` | `.sc [成功损失/失败损失]` | 理智(SAN)检定 | `.sc 1/1d6`、`.sc 0/1d3` |
| `.en` | `.en [属性名]` | 成长检定（投d100>属性值则+1d10） | `.en 射击` |
| `.ti` | `.ti` | 临时疯狂症状表（随机） | `.ti` |
| `.li` | `.li` | 总结疯狂症状表（随机） | `.li` |
| `.setcoc` | `.setcoc [0-5]` | 设置大成功/大失败判定规则 | `.setcoc 5` |
| `.jrrp` | `.jrrp` | 今日人品（当日固定） | `.jrrp` |
| `.help` | `.help` | 显示指令帮助 | `.help` |

### COC 判定规则变体 (setcoc 0-5)
```
规则0: 出1大成功，不满50出96-100大失败，满50出101大失败
规则1: 出1-5且<=成功率 大成功，出96-100且>成功率 大失败
规则2: 出1-5且<=成功率/5 大成功，出96-100 大失败
规则3: 出1-5 大成功，出96-100 大失败
规则4: 出1-5且<=成功率/10 大成功，出>=96+成功率/10 大失败
规则5: 出1-2且<成功率/5 大成功，出96-100且>=96+成功率/10 大失败（默认）
```

### 检定等级
- **大成功** → 特殊规则
- **极难成功** → ≤ 属性值/5
- **困难成功** → ≤ 属性值/2
- **普通成功** → ≤ 属性值
- **失败** → > 属性值
- **大失败** → 特殊规则

### 数据表 `coc_characters`
```sql
CREATE TABLE coc_characters (
    user_id    BIGINT NOT NULL,
    group_id   BIGINT NOT NULL,
    name       VARCHAR(50) DEFAULT '',
    attributes JSON,                    -- {"力量":60,"敏捷":70,"外貌":50,...}
    san        INT DEFAULT 0,           -- 当前SAN值
    hp         INT DEFAULT 0,
    mp         INT DEFAULT 0,
    coc_rule   TINYINT DEFAULT 5,       -- 当前使用的判定规则
    PRIMARY KEY (user_id, group_id)
);
```

### COC 七大属性 (3d6×5)
力量STR、体质CON、体型SIZ、敏捷DEX、外貌APP、智力INT、意志POW
### 派生属性
- 教育EDU: 2d6+6 ×5
- 幸运LUCK: 3d6×5
- HP = (CON+SIZ)/10
- MP = POW/5
- SAN = POW
- DB(伤害加值) = STR+SIZ 查表

### 疯狂症状表
- 临时疯狂(.ti): 内置20条（失忆、假性残疾、暴力倾向、偏执、昏厥……）
- 总结疯狂(.li): 内置20条（信念/精神障碍、恐惧症、狂躁症……）

### 依赖
- 无额外依赖（纯 Python random + re）
- 复用 MySQL 连接（角色卡存储）

---

## 三、搜图 (`plugins/image_search.py`)

### 触发
- 指令: `#搜图 关键词` 或 `#搜图 [图片]`
- 优先级: 5, block=True

### 核心问题：国内大陆腾讯云无法直连 Pixiv

### 解决方案

| 场景 | API | 方案 |
|------|-----|------|
| 关键词搜图 | **Lolicon API** (`api.lolicon.app/setu/v2`) | 免费，按tag搜 Pixiv 插画，返回反代图片URL，国内直连 ✅ |
| 以图搜图 | **SauceNAO** (`saucenao.com/search.php`) | 传图→返回来源信息+缩略图，国内可访问 ✅，免费200次/天 |
| 图片下载 | **Pixiv反代** (`i.pixiv.re`) | 将 `i.pximg.net` 替换为 `i.pixiv.re`，国内直连 ✅ |

### Lolicon API 说明
```
POST https://api.lolicon.app/setu/v2
Body: {"tag": ["关键词"], "num": 3, "r18": 0, "size": ["regular"]}
Response: {
  "data": [{
    "pid": 12345,
    "title": "xxx",
    "author": "xxx",
    "tags": ["tag1","tag2"],
    "urls": {"regular": "https://i.pixiv.re/..."}
  }]
}
```
- 免费无需API Key
- 自带 `i.pixiv.re` 反代链接，无需额外处理
- `r18` 参数: 0=非R18, 1=R18, 2=混合

### SauceNAO API 说明
```
POST https://saucenao.com/search.php
Params: api_key=xxx, output_type=2, numres=3
Files: file=<图片二进制>
Response: {
  "results": [{
    "header": {"similarity": "95.5", "thumbnail": "url"},
    "data": {"source": "xxx", "pixiv_id": 12345, "title": "xxx", "member_name": "xxx"}
  }]
}
```
- 需要注册获取 API Key（免费账户 200次/天）
- 注册地址: https://saucenao.com/user.php

### config.yaml 新增
```yaml
image_search:
  lolicon_api: "https://api.lolicon.app/setu/v2"
  saucenao_api_key: ""            # 去 saucenao.com 注册获取
  pixiv_proxy: "i.pixiv.re"       # Pixiv图片反代域名
  max_results: 3                  # 每次返回图片数
  r18: 0                          # 0=非R18, 1=R18, 2=混合
```

### 功能流程

**关键词搜图：**
1. 用户发 `#搜图 原神 刻晴`
2. POST Lolicon API → 获取匹配插画列表
3. 遍历结果，构造消息：标题 + 作者 + PID + 图片
4. 发送合并转发消息（`send_group_forward_msg`）避免刷屏

**以图搜图：**
1. 用户发 `#搜图 [图片]`
2. 下载用户图片 → POST SauceNAO API
3. 解析返回结果 → 相似度 + 来源 + 缩略图
4. 发送匹配结果

### 依赖
- 无额外依赖（httpx 已有）
- SauceNAO 需注册 API Key

---

## 实施顺序

```
1. [前置] 搭建 MySQL + SQLAlchemy 基础层 (plugins/db.py)
2. [功能一] 签到 (plugins/sign_in.py) — 高优先级，日常活跃核心
3. [功能二] COC跑团 (plugins/coc_dice.py) — 纯逻辑，无外部依赖
4. [功能三] 搜图 (plugins/image_search.py) — 需注册 SauceNAO key
```

## 待确认事项

- [ ] MySQL 连接信息（host/port/user/password/database）
- [ ] SauceNAO API Key 注册
- [ ] 签到背景图 API 选择确认（dmoe.cc 或其他）
- [ ] 搜图 R18 过滤策略确认
