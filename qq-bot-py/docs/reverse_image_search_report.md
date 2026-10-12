# 反向图片搜索引擎技术调研报告

> 日期: 2026-04-11
> 目标: QQ Bot 搜图功能选型（大陆服务器部署）

---

## 核心发现：推荐使用 PicImageSearch 库

**`PicImageSearch`** (pip install PicImageSearch) 是目前最成熟的 Python 聚合搜图库（676+ stars），v3.12.11，支持 async/sync 双模式，基于 httpx，**完美适配你的 NoneBot2 架构**。

**一个库统一封装了以下所有引擎：**
ASCII2D, SauceNAO, TraceMoe, IQDB, Yandex, Bing, BaiDu, Google Lens, TinEye, EHentai, Copyseeker, AnimeTrace

安装: `pip install PicImageSearch`（清华源: `-i https://pypi.tuna.tsinghua.edu.cn/simple`）

---

## 一、ASCII2D (ascii2d.net) ⭐⭐⭐⭐⭐ — 最推荐

### 概述
日本的二次元图片搜索引擎，**不需要 API Key**，通过 HTML 表单提交 + 页面解析（scraping）工作。对 **Twitter/X 和 Pixiv** 的源溯源能力是所有引擎中最强的。

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **不需要** |
| 费用 | **完全免费** |
| 覆盖平台 | **Pixiv, Twitter/X, Fanbox, Fantia, Misskey, ニコニコ静画, ニジエ** |
| R18 过滤 | 来自 2ch 的图片会自带马赛克，但无主动 R18 过滤机制 |
| 大陆可访问 | ⚠️ **需要代理**，ascii2d.net 日本服务器，大陆直连不稳定 |
| 文件限制 | 最大 10MB，支持 JPEG/PNG/WEBP |

### 两种搜索模式

1. **色合検索 (Color Search)** — 默认模式，按颜色组合匹配，**推荐日常使用**
2. **特徴検索 (Feature/BoVW Search)** — 按视觉特征匹配，适用于：
   - 被裁剪的图片
   - 被旋转的图片
   - 颜色被修改的图片
   - ⚠️ 注意：Twitter 头像因裁剪过多（<50%原图），通常找不到

### URL 格式（手动请求方式）

```
# 通过 URL 搜索（Color 模式）
POST https://ascii2d.net/search/uri
Form Data: uri=<image_url>
→ 302 重定向到结果页 https://ascii2d.net/search/color/<hash>

# 切换到 Feature 模式
GET https://ascii2d.net/search/bovw/<hash>  (将 /color/ 替换为 /bovw/)

# 通过文件上传搜索
POST https://ascii2d.net/search/file
Form Data: file=<image_file>
→ 302 重定向到结果页
```

### 使用 PicImageSearch 的代码实现

```python
import asyncio
from PicImageSearch import Ascii2D, Network

async def search_ascii2d(image_url: str = None, image_bytes: bytes = None):
    """ASCII2D 搜图"""
    # bovw=False → 色合検索(推荐)  bovw=True → 特徴検索
    async with Network(proxies="http://your-proxy:port") as client:
        ascii2d = Ascii2D(
            base_url="https://ascii2d.net",
            bovw=False,  # Color search
            client=client,
        )

        if image_url:
            resp = await ascii2d.search(url=image_url)
        elif image_bytes:
            resp = await ascii2d.search(file=image_bytes)

        # resp.url → 搜索结果页链接
        # resp.raw → list[Ascii2DItem]
        for item in resp.raw:
            if item.title or item.url_list:
                print(f"标题: {item.title}")
                print(f"作者: {item.author}")
                print(f"作者主页: {item.author_url}")
                print(f"来源URL: {item.url}")
                print(f"缩略图: {item.thumbnail}")
                print(f"Hash: {item.hash}")
                print(f"所有链接: {item.url_list}")  # list[URL(href, text)]
                break  # 取第一个有效结果
```

### 手动 httpx 实现（不用 PicImageSearch）

```python
import httpx
from pyquery import PyQuery

SUPPORTED_SOURCES = ["fanbox", "fantia", "misskey", "pixiv", "twitter", "ニコニコ静画", "ニジエ"]

async def ascii2d_search(image_url: str = None, image_bytes: bytes = None, bovw: bool = False):
    """直接 scraping ASCII2D"""
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        proxies="http://your-proxy:port",  # 大陆必须代理
    ) as client:
        if image_url:
            resp = await client.post(
                "https://ascii2d.net/search/uri",
                data={"uri": image_url},
            )
        elif image_bytes:
            resp = await client.post(
                "https://ascii2d.net/search/file",
                files={"file": ("image.jpg", image_bytes, "image/jpeg")},
            )

        # 默认返回 color 搜索结果
        result_url = str(resp.url)

        # 如果要切换到 bovw (特征搜索)
        if bovw:
            bovw_url = result_url.replace("/color/", "/bovw/")
            resp = await client.get(bovw_url)

        # 解析 HTML
        doc = PyQuery(resp.text)
        items = list(doc("div.row.item-box").items())

        results = []
        for item in items:
            hash_val = item("div.hash").eq(0).text()
            detail = item("small").eq(0).text()
            img_src = item("img").eq(0).attr("src")
            thumbnail = f"https://ascii2d.net{img_src}" if img_src and img_src.startswith("/") else img_src

            detail_box = item.find("div.detail-box.gray-link")
            if not detail_box:
                continue

            links = detail_box.find("a")
            if not links:
                continue

            link_items = list(links.items())
            # 检查来源标记
            mark = ""
            for small in detail_box("small").items():
                if small.text() in SUPPORTED_SOURCES:
                    mark = small.text()
                    break

            title = link_items[0].text() if link_items else ""
            url = link_items[0].attr("href") if link_items else ""
            author = link_items[1].text() if len(link_items) > 1 else ""
            author_url = link_items[1].attr("href") if len(link_items) > 1 else ""

            if title or url:
                results.append({
                    "hash": hash_val,
                    "thumbnail": thumbnail,
                    "title": title,
                    "url": url,
                    "author": author,
                    "author_url": author_url,
                    "source": mark,  # "pixiv", "twitter" 等
                })

        return results
```

### ASCII2D 的关键优势
- **Twitter/X 溯源最强**：能找到被转发/搬运的原推文
- **Pixiv 溯源强**：直接返回 Pixiv 作品链接和作者主页
- **无需 API Key**：直接 POST 表单即可
- **结果结构清晰**：返回 source 标识 (pixiv/twitter)，链接分层

### ASCII2D 的局限
- 大陆需代理
- 无 JSON API，必须解析 HTML（PicImageSearch 已处理好）
- 无明确的速率限制文档，但高频使用可能被拒

---

## 二、SauceNAO (saucenao.com) ⭐⭐⭐⭐⭐ — 必接的引擎

### 概述
最广泛使用的反向图片搜索引擎，**有正式 JSON API**，覆盖数据库最多（Pixiv, Danbooru, Twitter, Yandere, Gelbooru 等 30+ 数据库）。

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **推荐**（免费注册即得） |
| 费用 | **免费**：150次/天, 4次/30秒；付费用户更多配额 |
| 覆盖平台 | **Pixiv, Twitter, Danbooru, Yandere, Gelbooru, Konachan, Sankaku, E-Hentai, DeviantArt, ArtStation, 更多...** |
| R18 过滤 | ✅ `hide` 参数：0=全部, 1=隐藏明确R18, 2=隐藏疑似R18, **3=只显示安全内容** |
| 大陆可访问 | ✅ **可直连**（偶尔不稳定，建议设置超时重试） |
| API 端点 | `POST https://saucenao.com/search.php` |
| 响应格式 | JSON (`output_type=2`) |

### API 参数

```
POST https://saucenao.com/search.php
参数:
  api_key     — API Key（注册即得）
  output_type — 2 (JSON)
  numres      — 结果数量 1-40，默认5
  db          — 数据库索引 999=全部
  hide        — R18过滤 0-3
  minsim      — 最低相似度 0-100
  url         — 图片URL（与file二选一）
  file        — 图片文件上传（multipart）
```

### 使用 PicImageSearch

```python
from PicImageSearch import SauceNAO, Network

async def search_saucenao(image_url: str = None, image_bytes: bytes = None):
    async with Network() as client:  # SauceNAO 大陆可直连，通常不需代理
        saucenao = SauceNAO(
            api_key="你的KEY",
            numres=5,
            hide=3,     # 只返回安全内容
            minsim=50,  # 最低相似度50%
            client=client,
        )

        if image_url:
            resp = await saucenao.search(url=image_url)
        elif image_bytes:
            resp = await saucenao.search(file=image_bytes)

        # resp.short_remaining → 30秒内剩余次数
        # resp.long_remaining → 当天剩余次数
        for item in resp.raw:
            print(f"相似度: {item.similarity}%")
            print(f"标题: {item.title}")
            print(f"来源URL: {item.url}")        # 自动构造 Pixiv/Twitter 链接
            print(f"作者: {item.author}")
            print(f"作者主页: {item.author_url}")  # 自动构造作者主页
            print(f"NSFW标记: {item.hidden}")      # 0=安全
            print(f"来源: {item.source}")
            print(f"缩略图: {item.thumbnail}")
            print(f"数据库: {item.index_name}")
            print(f"扩展URL: {item.ext_urls}")
```

### 你现有代码的改进建议

历史 `plugins/image_search.py` 曾接入 SauceNAO，直接用 httpx 手动调用。本报告属于方案调研，不表示当前公开版已启用搜图；未来可评估 PicImageSearch 以统一管理多引擎。

---

## 三、TraceMoe (trace.moe) ⭐⭐⭐ — 仅限动画截图

### 概述
**专门用于动画场景识别**，给一张动画截图，返回它来自哪部动画、哪一集、哪个时间点。**不能用于一般插画/照片搜索**。

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **不需要**（免费100次/天）；赞助可获更多 |
| 费用 | 免费100次/天；$1/月=1000次，$5=5000次... |
| 覆盖平台 | **仅动画数据库**（AniList） |
| R18 过滤 | 结果本身是动画信息，不涉及 R18 分类 |
| 大陆可访问 | ⚠️ **需要代理**（trace.moe 和 api.trace.moe 大陆不稳定） |
| API 端点 | `POST https://api.trace.moe/search` |
| 速率限制 | 100请求/分/IP |

### API 用法

```
# 通过 URL
POST https://api.trace.moe/search?url=<encoded_image_url>&cutBorders=true&anilistInfo=

# 通过文件上传
POST https://api.trace.moe/search?cutBorders=true&anilistInfo=
Body: multipart/form-data, field "file"

# 带 API Key
Header: x-trace-key: <your_key>
```

### 使用 PicImageSearch

```python
from PicImageSearch import TraceMoe, Network

async def search_anime(image_url: str = None, image_bytes: bytes = None):
    async with Network(proxies="http://your-proxy:port") as client:
        tracemoe = TraceMoe(client=client)

        if image_url:
            resp = await tracemoe.search(url=image_url, chinese_title=True)
        elif image_bytes:
            resp = await tracemoe.search(file=image_bytes, chinese_title=True)

        for item in resp.raw:
            print(f"相似度: {item.similarity}")
            print(f"动画名称: {item.title_chinese or item.title_native}")
            print(f"集数: 第{item.episode}集")
            print(f"时间: {item.time_str}")  # 如 "12:34"
            print(f"预览图: {item.cover_image}")
```

### 适用场景
- 用户发动画截图问"这是什么番" → 用 TraceMoe
- **不适用于**：插画、照片、漫画、同人图

---

## 四、IQDB (iqdb.org) ⭐⭐⭐ — 二次元 Booru 站聚合

### 概述
聚合搜索多个动漫图片网站（Danbooru, Konachan, Yandere, Gelbooru, Sankaku, Zerochan 等）。

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **不需要** |
| 费用 | **完全免费** |
| 覆盖平台 | **Danbooru, Konachan, Yandere, Gelbooru, Sankaku, e-shuushuu, Zerochan, Anime-Pictures** |
| R18 过滤 | ❌ 无内建过滤，Booru 站本身多为 R18 内容 |
| 大陆可访问 | ✅ **可直连**（iqdb.org 比较稳定） |
| 文件限制 | 最大 8192 KB，维度 ≤ 7500×7500，JPEG/PNG/GIF |

### API 用法

```
POST https://iqdb.org/
Form Data:
  url=<image_url>    (URL搜索)
  file=<image_file>  (文件上传，二选一)

返回: HTML 页面（需解析）
```

### 使用 PicImageSearch

```python
from PicImageSearch import Iqdb, Network

async def search_iqdb(image_url: str = None, image_bytes: bytes = None):
    async with Network() as client:  # 大陆可直连
        iqdb = Iqdb(client=client)

        if image_url:
            resp = await iqdb.search(url=image_url)
        elif image_bytes:
            resp = await iqdb.search(file=image_bytes)

        # resp.saucenao_url → 跳转到 SauceNAO 搜索的链接
        # resp.ascii2d_url  → 跳转到 ASCII2D 搜索的链接
        for item in resp.raw:
            print(f"相似度: {item.similarity}")
            print(f"来源URL: {item.url}")
            print(f"缩略图: {item.thumbnail}")
            print(f"尺寸: {item.size}")
            print(f"来源站: {item.source}")
```

### 适用场景
- 用户发的是从 Danbooru/Yandere 等站下载的图 → IQDB 很强
- 不适合找 Twitter/Pixiv 原帖（这是 ASCII2D 和 SauceNAO 的强项）
- ⚠️ 搜索结果大部分来自 R18 Booru 站，需在 QQ Bot 中做额外过滤

---

## 五、Yandex Image Search ⭐⭐⭐⭐ — 通用搜图

### 概述
俄罗斯搜索引擎，反向图片搜索能力强，覆盖面广，对照片/实物/二次元均有效。

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **不需要**（通过 scraping） |
| 费用 | **免费**（scraping 方式） |
| 覆盖平台 | **全网**（包括 Twitter, Instagram, Pinterest, 博客等） |
| R18 过滤 | 默认有安全搜索，可通过参数控制 |
| 大陆可访问 | ⚠️ **不稳定**（yandex.com 部分CDN被墙，需代理更可靠） |

### 使用 PicImageSearch

```python
from PicImageSearch import Yandex, Network

async def search_yandex(image_url: str = None, image_bytes: bytes = None):
    async with Network(proxies="http://your-proxy:port") as client:
        yandex = Yandex(client=client)

        if image_url:
            resp = await yandex.search(url=image_url)
        elif image_bytes:
            resp = await yandex.search(file=image_bytes)

        for item in resp.raw:
            print(f"标题: {item.title}")
            print(f"来源URL: {item.url}")
            print(f"缩略图: {item.thumbnail}")
            print(f"尺寸: {item.size}")
            print(f"来源站: {item.source}")
```

### 注意
- Yandex 无官方图片搜索 API（其官方 API 是付费的文字搜索 API）
- PicImageSearch 通过模拟浏览器表单提交 + HTML 解析实现
- 适合找照片原图、非二次元内容

---

## 六、Google Vision API / Google Lens ⭐⭐ — 昂贵且复杂

### Google Cloud Vision API

| 属性 | 值 |
|---|---|
| API Key | ✅ **必须** (Google Cloud 项目 + 计费) |
| 费用 | $1.50/1000次（前1000次/月免费） |
| 功能 | 图片标签、OCR、人脸检测、地标 — **不是反向图片搜索** |
| 返回源URL | ❌ **不返回** — 只返回标签和文字 |
| 大陆可访问 | ❌ **完全不可用**（需翻墙 + Google Cloud 账号） |

**结论：Google Vision API 不适合"以图搜源"场景。它做的是图像理解(标签/OCR)，不是找原图。**

### Google Lens

| 属性 | 值 |
|---|---|
| API Key | **不需要**（scraping 方式） |
| 费用 | **免费**（scraping） |
| 覆盖平台 | 全网，但偏向商品/实物识别 |
| 大陆可访问 | ❌ **完全不可用**（lens.google.com 大陆不通） |

PicImageSearch 支持 Google Lens，但 **Google 已标记为 deprecated**，且大陆完全不可用。

**结论：Pass。大陆服务器用不了。**

---

## 七、百度识图 ⭐⭐⭐ — 大陆备选

### 技术细节

| 属性 | 值 |
|---|---|
| API Key | **不需要** |
| 费用 | **免费** |
| 覆盖平台 | 中文网站为主，对百度贴吧/微博等中文平台较强 |
| R18 过滤 | 百度自带内容审查 |
| 大陆可访问 | ✅ **完美可用，无需代理** |

### 使用 PicImageSearch

```python
from PicImageSearch import BaiDu, Network

async def search_baidu(image_url: str = None, image_bytes: bytes = None):
    async with Network() as client:
        baidu = BaiDu(client=client)
        if image_url:
            resp = await baidu.search(url=image_url)
        elif image_bytes:
            resp = await baidu.search(file=image_bytes)

        for item in resp.raw:
            print(f"来源URL: {item.url}")
            print(f"缩略图: {item.thumbnail}")
```

---

## 八、AI 视觉模型辅助方案 (Qwen-VL) ⭐⭐⭐

### 概述
**不是反向图片搜索**，但可以作为补充：当所有引擎都没找到源时，用 AI 描述图片内容辅助用户。

### 你现有的 Qwen-VL 集成

你已经有 `qwen3-vl-plus` 配置在 config.yaml 中，可以直接复用：

```python
async def ai_describe_image(image_url: str) -> str:
    """用 AI 描述图片内容，辅助手动搜索"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {vision_api_key}"},
            json={
                "model": "qwen3-vl-plus",
                "messages": [
                    {"role": "system", "content": "请描述这张图片的内容，包括角色名、作品名（如果能识别的话）、画风特征等信息。"},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": "这张图片是什么？请尽量识别角色和作品。"}
                    ]}
                ],
            }
        )
        return resp.json()["choices"][0]["message"]["content"]
```

### 适用场景
- 所有搜图引擎都无结果时的 fallback
- 识别动漫角色名 → 帮用户提供搜索关键词
- **大陆完美可用**（阿里云达摩院 API）

---

## 综合对比表

| 引擎 | API Key | 费用 | Pixiv | Twitter | 通用 | R18过滤 | 大陆直连 | JSON API |
|---|---|---|---|---|---|---|---|---|
| **ASCII2D** | ❌ | 免费 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | ❌ | ❌需代理 | ❌HTML |
| **SauceNAO** | ✅推荐 | 免费150/天 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ✅hide参数 | ✅ | ✅ |
| **IQDB** | ❌ | 免费 | ⭐⭐ | ❌ | ⭐⭐ | ❌ | ✅ | ❌HTML |
| **TraceMoe** | ❌ | 免费100/天 | ❌ | ❌ | ❌ | N/A | ❌需代理 | ✅ |
| **Yandex** | ❌ | 免费 | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | ⚠️不稳定 | ❌HTML |
| **百度** | ❌ | 免费 | ⭐ | ❌ | ⭐⭐⭐ | ✅ | ✅ | ❌HTML |
| **Google Lens** | ❌ | 免费 | ⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | ❌完全不通 | ❌HTML |
| **Qwen-VL** | ✅ | 付费(便宜) | N/A | N/A | 辅助 | ✅ | ✅ | ✅ |

---

## 推荐实现方案：多引擎级联搜索

### 策略

```
用户发图 → #搜图 [图片]
  │
  ├─ 第1步: SauceNAO (大陆直连、有JSON API、覆盖广)
  │    ├─ 相似度 > 80% → 直接返回结果
  │    └─ 相似度 < 80% 或无结果 → 继续
  │
  ├─ 第2步: ASCII2D (找 Twitter/Pixiv 原帖，需代理)
  │    ├─ 找到有效来源 → 返回结果
  │    └─ 无结果 → 继续
  │
  ├─ 第3步 (可选): 判断是否为动画截图
  │    └─ TraceMoe → 返回动画信息
  │
  └─ 兜底: Qwen-VL 描述图片内容
       → "未找到确切来源，AI 识别结果：..."
```

### config.yaml 新增配置

```yaml
image_search:
  # 已有
  lolicon_api: "https://api.lolicon.app/setu/v2"
  saucenao_api_key: "你的key"
  pixiv_proxy: "i.pixiv.re"
  max_results: 3
  r18: 0

  # 新增
  ascii2d:
    enabled: true
    base_url: "https://ascii2d.net"
    bovw: false          # 默认色合検索
    proxy: "http://your-proxy:port"  # 大陆需代理

  tracemoe:
    enabled: true
    proxy: "http://your-proxy:port"

  # SauceNAO NSFW 过滤等级
  saucenao_hide: 3       # 0=全部 1=隐藏R18 2=隐藏疑似 3=仅安全
  min_similarity: 50     # 最低相似度

  # 多引擎级联
  cascade_search: true   # 开启级联搜索
  saucenao_threshold: 80 # SauceNAO 相似度达到此值则不再搜其他引擎
```

### 依赖变更

```txt
# requirements.txt 新增
PicImageSearch>=3.12.0
```

---

## 关于代理方案

ASCII2D 和 TraceMoe 在大陆需要代理，有两种方式：

### 方案 A: 环境变量代理（推荐）
```yaml
# config.yaml
proxy:
  http: "http://127.0.0.1:7890"
  https: "http://127.0.0.1:7890"
```

```python
# 代码中
proxy = config.get("proxy", {}).get("https", "")
async with Network(proxies=proxy) as client:
    ascii2d = Ascii2D(client=client)
```

### 方案 B: 通过海外 VPS 反代 ASCII2D
在海外 VPS 上用 nginx 反代 ascii2d.net：
```nginx
server {
    listen 443 ssl;
    server_name ascii2d-proxy.yourdomain.com;

    location / {
        proxy_pass https://ascii2d.net;
        proxy_set_header Host ascii2d.net;
    }
}
```
然后在代码中使用自定义 base_url：
```python
ascii2d = Ascii2D(base_url="https://ascii2d-proxy.yourdomain.com", client=client)
```

---

## 总结

| 优先级 | 引擎 | 用途 | 实现难度 |
|---|---|---|---|
| **P0** | SauceNAO | 主力搜图，大陆直连 | 已有代码，微调即可 |
| **P0** | ASCII2D | Twitter/Pixiv 溯源 | 需加代理，用 PicImageSearch |
| **P1** | TraceMoe | 动画截图识别 | 简单，需代理 |
| **P1** | Qwen-VL | AI 兜底描述 | 已有代码，可复用 |
| **P2** | IQDB | Booru站搜索 | 简单，直连 |
| **P2** | 百度识图 | 中文内容备选 | 简单，直连 |
| **P3** | Yandex | 通用图搜 | 需代理，不稳定 |
| ❌ | Google | 大陆不可用 | 放弃 |
