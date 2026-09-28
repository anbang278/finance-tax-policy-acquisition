# 架构与运行状态

一份 Python 包、五个 Skill、两个宿主清单。`config/sources.yaml` 为来源信任表，`contracts/official-material.yaml` 为采集意图契约。`ftr` 提供 JSON CLI，原始资料按 SHA-256 写入 `FTR_DATA_DIR/evidence`，任务和版本存于 SQLite。同一数据目录使用独占锁。

采集：`CREATED → RUNNING → WAITING_DECISION / PARTIAL / COMPLETED / COMPLETED_EMPTY`。程序硬失败或附件不足进入隔离；新资料需要语义复核才能成为可检索的已验证版本。

修复：`FAILURE → PATCH_PROPOSED → BLOCKED_NO_ISOLATION`。当前仅保存候选和静态路径审查。隔离候选执行、可信报告、独立人工收据和正式发布管理器是后续阶段，不能用本地同身份演示冒充。

财政部 Adapter 使用 HTTP 与静态列表。税务 Adapter 先由有界面 Playwright 正常打开栏目，读取页面实际成功请求中的 `channelId`、页容量及当前会话 Cookie；随后在同一执行批次用 `requests.Session` 获取列表分页、详情和附件。Cookie 仅驻留内存，不写入日志或数据库；列表响应作为来源证据保存。无界面浏览器在本机返回 403 时不继续请求。来源新增主机需修改信任配置，不属于自动修复。
