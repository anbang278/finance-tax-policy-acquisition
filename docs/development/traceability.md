# 交接要求追踪矩阵

原文编号见《财税研究 Agent 插件 交接文档 v1.0.md》。这里的状态仅反映当前实施，不改写原要求。`已实现`不等于真实来源或正式宿主已验收。

| 范围 | 对应产物 | 当前状态 |
|---|---|---|
| U01—U03、U06—U07 | 独立工程、七个 Skill、`ftr` 协议、`TaskRequest`、双清单、研发四件套 | 已实现骨架；宿主实装待验收 |
| U04、P01—P16 | `DESIGN_CONSTITUTION.md`、`ARCHITECTURE.md`、`docs/development/design.md`、受保护路径门禁 | 文档和部分程序门禁已实现 |
| U05 | `repair.py` 与 Level 4 接口设计 | 首版候选阶段；隔离测试、签名发布延期 |
| T00—T03 | 环境勘察、模型、SQLite、证据仓、网络边界、备份 | 本地实现；异常及大规模恢复覆盖不足 |
| T04—T05 | 两 Adapter、类型、日期、版本、范围 | 财政部单页单详情通过；税务站有界面会话支持两页、详情及附件有界实测；全年覆盖与多类型仍待验收 |
| T06 | 五 Skill、语义回调、研究引用 | 文件与程序接口已实现；双宿主真实模型案例待验收 |
| T07、T10 | 故障记录、候选补丁门禁与审查包 | 静态候选已实现，自动生成质量未评估 |
| T08—T09 | 隔离执行、可信回归、审批与发布 | 延期，运行时固定阻断 |
| T11—T12 | 端到端、恢复、打包与报告 | 打包及有限恢复通过；双来源/双宿主端到端未通过 |
| A01—A02 | 安装和官方来源 | 部分；参见验收报告 |
| A03—A08 | 空结果、短文、类型、分页、版本、附件 | 部分离线覆盖；真实多样本和失败场景待补 |
| A09—A10 | 决策与研究 | 摘要/证据门禁实现；真实模型评估待补 |
| A11—A13 | 修复、访问限制、保护清单 | 候选阶段部分实现；访问受限显式记录 |
| A14—A20 | 可信回归、签名、发布、回退 | 按本期边界延期，不能计为通过 |
| A21—A22 | 不可信输入与安全边界 | 主机白名单、非公网拒绝、大小限额和补丁路径检查；仍需安全审查 |
| A23—A26 | 恢复、缺环境、安装包、设计保留 | 本地部分通过；正式权限和宿主实装待验收 |

A05 按本轮确认改为“错误内容或错误分类不得通过”；合法栏目新闻应保留并正确标记。A11、A14—A20 所依赖的真实候选执行及正式发布环节明确延期。


## 本轮扩展追踪（2026-09-30）

| 已确认目标 | 实施与验证入口 | 当前边界 |
|---|---|---|
| 整套项目说明 | 根 README、Agent 契约、安装/配置/使用/运维/开发手册 | 文档与当前实现核对；原始交接不覆盖 |
| 参数支持跨环境 | config.py、CLI、采集器、网络与 Web，test_config/test_portability | 本机验证；来源与复核门禁独立 |
| Windows 原生与迁移 | Portalocker、备份规范路径与 URI、verify_wheel、三平台 CI | Windows/Linux 实际执行待验证 |
| Linux 无桌面税务采集 | 惰性 Chromium、显示检查、Xvfb 手册 | 参数与缺环境测试通过，Linux 源站实采待验证 |
| 无认证局域网工作台 | serve host、Host 校验、ui-config、同源页面 | 本机监听/只读验证，另一台电脑待验收 |

## 伙伴开箱使用与受限规则闭环（2026-10-02）

本轮在原两来源范围内新增 setup、workbench Skill；用户级环境引导返回绝对运行路径，按来源/查询能力安装依赖，不触发采集。只读工作台新增启动、复用、身份验证停止及自动打开浏览器接口。

受限规则自修复使用固定执行器与可信样本，支持列表/正文定位、预定义日期、分页和 JSON 字段映射；失败原件绑定候选及验证报告，最多两轮候选。有界真实来源验证通过后自动本机启用并续跑，失败及中断回退；不执行未知 Python，不更改资料复核门槛，governed 仍未开放。详见 ../guides/self-repair.md 与当前架构。

追踪入口：setup → scripts/setup.sh、setup.ps1、setup_runtime.py → tests/test_setup.py；规则闭环 → rules.py、rule_repair.py、runtime.py → tests/test_rule_repair.py；工作台 → workbench.py → tests/test_workbench_lifecycle.py。既有回归、wheel 与三平台 CI 继续执行。

验收须分开记录离线故障注入、真实源站及 macOS/Windows × Codex/Hermes 会话。受控网络替身不代表真实访问成功，缺宿主/设备不能计通过。当前实现结果以 acceptance-report.md 本轮补充为准。

## 首次配置与可靠性追踪（2026-10-02）

| 目标 | 实施与回归 | 验收边界 |
|---|---|---|
| 通用自动配置 | setup/run 脚本、browser.py；test_setup.py、test_reliability.py | 模拟下载与本机启动分开；干净桌面待验证 |
| 服务身份与诊断 | workbench.py；test_workbench_lifecycle.py、test_reliability.py | 实际子进程与包装 PID 注入；旧状态兼容 |
| 网络失败分级 | network.py/runtime.py；test_reliability.py、test_rule_repair.py | 502/访问限制/实际重试请求限额，网络为受控替身 |
| 进度与恢复 | runtime.py/diagnostics.py/cli.py；test_reliability.py | stderr、信号与强制退出残留、独占锁后恢复 |
| 质量解释与历史兼容 | Web query/app.js、PDF 警告证据；test_reliability.py、test_web.py、旧备份回归 | 派生解释不改历史数据库；语义复核另验 |
| 分发与跨系统 | 三平台 CI、verify_wheel.py | 本机 wheel 实测；新远程 CI 和桌面宿主另验 |

## 体验审计整改追踪（2026-10-03）

| 缺陷/目标 | 实施 | 验证 |
|---|---|---|
| D-01 附件恢复/新版本 | repository/runtime attachment_work 与版本摘要 | test_usability_upgrade 跨实例、混合附件、预算/403/取消/旧队列 |
| D-02 完整性与效率 | listing_pages 检查点、持久原件、report 未读范围 | 置顶/乱序/空日期/边界/续跑；官方筛选尚未证明，效率 PARTIAL |
| D-03 失败解释 | diagnostics/runtime、Web 中文报告及事件 | 429/403/超时/浏览器/未知、脱敏、实际浏览器 |
| D-04 范围保护 | TaskRequest extra forbid、from_saved 历史读取 | 错字段无写入、旧请求原摘要保持 |
| D-05 复核状态 | submit_decision 显式映射 | PASS/REJECT/UNCERTAIN、幂等/错误摘要、研究排除 |
| D-06 每批等待 | Controller/Repair 统一规则 | 专家契约走查；独立模型未测 |
| D-07 普通只读 | Repository readonly、CLI read_operation | 写锁/残留 RUNNING 下读取字节不变 |
| D-08 分页证据 | MofAdapter 实际请求、runtime 最终响应 | 跨批分页 URL、真实三页元数据 |
| 空日期可采集 | 非硬限制、查询保留未知分组 | 空日期 collected/待复核、筛选仍可见 |


## 正文公文排版（2026-10-03）

正文排版 → web/body_display.py、web/query.py、web/static/app.js/style.css → test_body_display.py、test_web.py；真实公告浏览器检查段落、对齐、轮询保位及窄屏。


## 全库与后续采集段落保留（2026-10-03）

全库及后续采集排版 → content_layout.py、adapters/common.py、web/body_display.py → test_body_display.py、test_core.py、test_rule_repair.py；全库只读结果见 ../verification/body-layout-audit-2026-10-03.json。

## 定时获取追踪（2026-10-03）

| 确认能力 | 实现 | 验证入口 |
|---|---|---|
| 双时点与 30 天持续回看 | scheduler 时点/窗口、单来源 rescan TaskRequest | test_scheduler 时区/迟到文件/14 天以上更新 |
| 自动续跑与停机补漏 | 持久水位、活动任务/合并窗口、轮转、幂等键 | 40 天停机、预算/崩溃恢复/附件跨窗 |
| 故障隔离与人工边界 | 来源暂停、额外重试/无进展、人工标记 | 网络耗尽、单源故障、暂停取消/手动任务 |
| 可观察与 Linux 托管 | CLI/API/工作台、心跳、systemd 模板 | 只读/Chromium/信号/锁/wheel；云端待验 |
| 数据兼容与质量 | schema v3、原版本/复核门禁保持 | 旧库/备份迁移、版本去重/原记录不变 |

## 本机管理追踪（2026-10-03）

| 要求 | 实现 | 隔离验证 |
|---|---|---|
| 一次性管理及跨源拒绝 | web/management.py, workbench.py | test_session_single_use_origin_readonly_and_remote |
| 幂等、版本、崩溃事务 | management.py, repository.AtomicConnection | test_queue_is_readonly_and_apply_idempotent, test_stale_plan_conflict, test_atomic_rollback_on_crash_and_replay |
| 独立后台与互斥 | worker.py, scheduler.py | test_detached_worker_duplicate_identity_lock_and_restart, test_independent_scheduler_refuses_takeover |
| 冻结请求、停用、窗口合并 | scheduler.py, management.py | test_one_off_disabled_and_source_pause；既有 test_scheduler.py |
| 人工门禁、草稿、轮次 | management.py | test_draft_review_reopen_immutable_rounds, test_hard_limits_forbid_pass_without_existing_decision, test_stale_version_preserves_draft |
| 定向补取与新版本 | Collector.recover_record | test_recovery_atomic_version_and_no_arbitrary_url；test_real_collector_targeted_recovery_atomic, test_real_recovery_budget_partial_checkpoint_and_resume |
| 备份不恢复会话 | backup.py | test_backup_pending_commands_excludes_sessions |
| 页面/窄屏/后台继续 | static/manage.js | test_browser_management_review_draft_plan_narrow_and_detached |

## GitHub 更新检测追踪（2026-10-04）

| 要求 | 实现 | 验证 |
|---|---|---|
| main 身份与提交关系 | update_check.local_identity/_compare | test_update_check 的相同/祖先/分叉/浅克隆/未知/脏工作区 |
| 失败不阻断与缓存 | check_for_updates、CLI/setup | 截止时间、限流/退避/缓存过期/损坏/无权限/锁/原业务结果 |
| 工作台后台及运行身份 | UpdateMonitor、/api/update-status、前端 | lifespan/查询不等待/冻结身份/Chromium 桌面窄屏与状态刷新 |
| 构建制品来源 | hatch_build.py、verify_wheel.py | sdist→wheel 身份一致、独立安装识别 |
