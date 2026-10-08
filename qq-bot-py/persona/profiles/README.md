# 多人设目录

为每个人设创建独立目录，例如 `persona/profiles/eres/`，目录内放置 `character.md`、`prompt.md`、`lore.md`、`special_users.md` 等 Markdown 文档。文件会按名称排序合并；同一目录里的 `special_users.md` 只属于该人格。

公开仓库不提供真实用户档案，也不分发 `special_users.md`。如需维护经相关用户同意的必要互动偏好，可仅在自己的私有部署创建该文件；Git 默认忽略它，文件缺失不会影响启动。角色关系与系统权限分开处理，昵称或私人关系不能授予管理员权限。

公共知识放在 `persona/shared/`，目前包含别名、指令和表情包说明。远坂凛仍兼容读取旧的 `persona/*.md` 布局；艾蕾等新人格读取自己的目录并追加公共知识。

当前配置位于项目根目录的 `config.yaml`：

```yaml
agent:
  persona:
    active_id: rin
    profile_dir: persona/profiles
    shared_dir: persona/shared
    registry: persona/registry.yaml
    state_file: data/active_persona.json
```

完成编辑后，管理员私聊执行 `#agent persona reload`，再用 `#agent persona switch eres 今天艾蕾凭依值班` 切换。伊什塔尔使用 `ishtar` 作为 ID，例如 `#agent persona switch ishtar 今天伊什塔尔凭依值班`。切换记录会写入数据库，当前人设会保存到 `data/active_persona.json`，重启后自动恢复；各人格的好感度、关系状态和特殊用户档案分别保存。

当前内置人格：`rin` 远坂凛、`eres` 艾蕾、`ishtar` 伊什塔尔。
