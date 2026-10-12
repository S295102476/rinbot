# 文档入口

用户文档统一在仓库根目录维护，避免历史部署记录与当前配置不同步：

- [项目介绍](../../README.md)
- [功能与指令](../../docs/features.md)
- [部署与维护](../../docs/deployment.md)
- [配置参考](../../docs/configuration.md)
- [可选扩展](../../docs/extensions.md)
- [开发说明](../README.md)

此目录提供功能实现与进阶配置说明：

- [Agent 升级与上线](agent-upgrade.md)、[长期记忆 V2](agent-memory-v2.md)
- [自然语言工具与 GPT 专家](agent-tools-v2.md)、[隔离开发任务](developer-tasks.md)
- [控制台外观](console-appearance.md)、[人设与交流边界](persona-interaction.md)
- [表情分类、随机发送与收集规则](meme_classification.md)
- [可选 Core 接入](gsuid_integration.md)、[Responses 接口](responses_api.md)

`*.example.yaml` 是按需合并的补充示例，不覆盖完整业务配置或密钥。`minigames_phase3.example.yaml` 需要先安装对应棋类引擎。历史开发计划与调研文档只供了解实现背景，部署时以根文档和当前配置模板为准。
