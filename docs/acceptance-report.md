# 本地研发验收记录

核验日期：2026-09-28。状态是当前环境和当前工程的实测结果；源站结构和访问策略可能变化。本报告不代表正式发布批准。

| 验收面 | 结果 | 可复核证据与边界 |
|---|---|---|
| 离线代码 | PASS | `uv run pytest -q`：16 项通过；`ruff check`、`mypy` 通过。覆盖两来源列表解析、浏览器拒绝后不发请求、失败条目重试、税务正文与附件定位、会话 Cookie 主机限制、日期及类型、缺失期限拒绝、样式过滤、幂等/版本、决策摘要、补丁保护、网络主机、暂停信号、备份恢复。其余计划场景仍需增加测试。 |
| 构建与资源 | PASS | `uv build` 成功；wheel 内含 `ftr/data/sources.yaml` 和采集契约；独立虚拟环境安装 wheel 后 `ftr doctor` 返回 `LOCAL_DEMO_READY`。该检查不包含两个宿主加载。 |
| Codex 插件 | PARTIAL | `.codex-plugin/plugin.json` 通过本地清单验证；尚未在目标 Codex 会话中安装并加载五个 Skill。 |
| Hermes 插件 | PARTIAL | `hermes plugins validate . --json` 无警告，`doctor . --ci` 通过。隔离的 `HERMES_HOME` 中启用插件后，`skills_list` 返回五个命名空间 Skill，`skill_view` 可读取。尚未验证真实模型会话中的 `ftr` 命令调用；用户现有 Hermes 配置未改动。 |
| 财政部真实来源 | PARTIAL | 对[财政部政策发布](https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/)执行单页、单详情有界采集，任务 `8b6882b36e0c4f72b63e7267458f5287`：1 页、1 份资料、1 个待语义复核项；因预算返回 `PARTIAL`。样本《紧急采购管理暂行办法》通知的完整办法正文为 3904 字符，类型 `policy_file`，栏目日期为 2026-09-14，成文与发布日期未被猜测。第二页、附件多样本及全年覆盖尚未验收。 |
| 税务总局真实来源 | PARTIAL | [政策法规库入口](https://fgk.chinatax.gov.cn/zcfgk/c100006/listflfg.html)在本机有界面 Playwright 返回列表 HTTP 200；动态读取页面请求的 `channelId` 与会话 Cookie 后，`requests` 列表、详情及附件请求返回 200。任务 `c739fea75bdf4c2eaa32186e410313d5` 有界保存第一页、1 份公告正文和 2 个原件附件，因预算返回 `PARTIAL`；主附件未解析使资料隔离。独立实测第二页 10 条，断点续跑在任务 `bf1c1dbc36a143379d43f2f5f5e1e1aa` 中保存第二份资料。无界面 Playwright 仍返回 403；全年覆盖未验证。 |
| 模型语义与研究 | PARTIAL | 决策请求、摘要与证据校验、检索和引用接口已实现；真实模型回调与陈述支持性评估尚未完成宿主级验收。没有把待复核真实样本自动记为通过。 |
| 候选修复 | PARTIAL | 越界补丁拒绝；候选静态登记可用。`repair test` 固定返回 `BLOCKED_NO_ISOLATION` 语义，未知补丁不执行，审查包标注未执行回归。 |
| 正式治理 | BLOCKED | `doctor --mode governed` 返回 `GOVERNED_NOT_IMPLEMENTED`；隔离执行、可信测试、独立签名身份、发布及回退尚未实施。不得标记 `MVP_ACCEPTED`。 |
| 备份恢复 | PASS（本地范围） | `backup create/verify/restore` 在空库命令链路通过；带证据文件的离线测试验证哈希与篡改拒绝。大库和故障恢复仍需实测。 |

当前为**可复查的本地研发演示**，但未满足计划中的 `LOCAL_DEMO_ACCEPTED`：Codex 宿主实际加载、两来源的完整范围覆盖和完整研究案例尚未通过。不得将有界样本、清单有效或网页人工可见折算为这些验收项通过。

下一步应分别在 Codex 与 Hermes 执行五个 Skill 的同协议调用，补测全年覆盖、复杂中断和引用研究案例；正式 Level 4 另设里程碑。
