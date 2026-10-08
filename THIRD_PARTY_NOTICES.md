# 第三方内容与致谢

RinBot 的原创代码使用 [GPL-3.0-only](LICENSE)。下面的第三方软件、角色、图片、词库与数据分别遵循其自身许可或权利归属；仓库的代码许可证不自动授权这些内容。

## 头像与角色

- 首页圆形头像 `docs/assets/rinbot-avatar.png` 由现有 `qq-bot-py/data/dutyroster/eres.jpg` 居中裁切并添加透明圆形蒙版，原图保留。维护者提供的原作者为 **KOTATSU ROOM**，原作品：[Pixiv · 71936630](https://www.pixiv.net/artworks/71936630)。
- 艾蕾头像标注来源不代表取得了 GPL 再许可；本项目不将该图声明为 GPL 授权素材。值班图片还包括 `rin.jpg`、`ishtar.jpg`，这两张图的来源及授权待补充。素材再分发或商用授权需另行向权利方确认，或替换为自己有权使用的图片。
- 远坂凛、艾蕾、伊什塔尔等名称与角色设定涉及 Fate 系列作品。这里的角色文档是社区项目的提示词与设定整理，项目与原作品权利方没有官方关联。角色名称、形象、游戏原文等权利属于各自权利方。

## 项目效果截图

`docs/screenshots/` 中的 QQ 对话、小游戏菜单与绘图示例由维护者提供，用于介绍实际使用效果，公开副本已匿名化用户信息。截图中的第三方头像、角色形象、表情和图片不因出现在 README 中而自动适用项目代码的 GPL 许可证。

## 成语题库

成语填空使用 THUNLP 的 THUOCL 成语词频数据，固定版本、来源链接、校验值与筛选规则见[题库说明](qq-bot-py/data/minigames/idioms/README.md)。原始 MIT 许可证保留在[题库 LICENSE](qq-bot-py/data/minigames/idioms/LICENSE)，筛选列表不改变原始数据的许可。

## 游戏数据与棋类引擎

- GBVSR 抓取工具通过 Dustloop 的公开页面与 API 获取数据。大体积游戏图片和私有数据库不随公开包分发；抓取内容的权利与再利用条件以对应来源为准。
- Fairy-Stockfish、KataGo、Rapfi 是独立项目，使用时请保留对应发行包的许可证与来源记录。引擎二进制、网络模型及其许可不因由安装工具下载而变为 RinBot 原创内容。
- GenshinUID / Core 及其游戏插件由各自上游维护，RinBot 仅提供可选的通信桥接。

## 软件依赖

感谢 NoneBot2、OneBot 适配器、FastAPI、SQLAlchemy、Redis、MinIO、Pillow、Playwright、React、Vite、Recharts、Lucide 等项目。精确依赖版本以 Python 锁定文件与前端 `package-lock.json` 为准，许可证请查看对应依赖发行物。

Compose 的 MinIO 服务从 [上游 `RELEASE.2025-10-15T17-29-55Z` 源码](https://github.com/minio/minio/tree/RELEASE.2025-10-15T17-29-55Z) 构建（提交 `9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`），遵循其 [GNU AGPL v3 许可证](https://github.com/minio/minio/blob/RELEASE.2025-10-15T17-29-55Z/LICENSE)。RinBot 主项目许可证不替代该组件的许可证。

Docker 镜像包含系统组件、中文字体及浏览器运行组件，它们遵循各自发行包许可。重新发布镜像或依赖时应同时保留相关声明。
