# 文档索引与事实职责

根 [README](../README.md) 是所有读者的入口；本页说明不同文档的职责，避免从历史稿或派生材料误判当前能力。

| 目录 | 内容与事实职责 |
|---|---|
| [guides/](guides/) | 用户和部署者的安装、配置、使用、运维步骤 |
| [development/](development/) | 当前需求基线（PRD）、设计方案、开发说明、实施记录和需求追踪 |
| [verification/](verification/) | 测试计划及实际验收、环境验证证据；状态以证据和边界为准 |
| [history/](history/) | 原始交接与来源观察等历史资料，不覆盖当前规则或实现 |

代码与测试分别位于仓库根 `src/`、`tests/`。架构概览和设计宪章分别在根目录 `ARCHITECTURE.md`、`DESIGN_CONSTITUTION.md`。发生差异时，按当前 PRD 确认范围、按设计与代码核对行为、按验收记录判断实际验证状态；历史材料用于追溯来源，不作为实现证据。

- [本机受限规则自修复](guides/self-repair.md)：规则语言、验证、启用、回退与能力边界。
- [定时获取与自动补漏](guides/scheduled-acquisition.md)：30 天回看、自动续跑、来源故障暂停、工作台观察与 Linux 服务模板。

- [本机定时任务管理与人工复核](guides/local-management.md)：一次性入口、持久命令、异步执行、草稿与轮次。

- [项目代码更新检测](guides/update-check.md)：main 提交比较、CLI/工作台提醒、缓存与离线设置。
