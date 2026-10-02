# 架构与运行状态

一份 Python 包、七个 Skill、两个宿主清单。`config/sources.yaml` 为来源信任表，`contracts/official-material.yaml` 为采集意图契约。`ftr` 提供 JSON CLI，原始资料按 SHA-256 写入 `FTR_DATA_DIR/evidence`，任务和版本存于 SQLite。同一数据目录使用独占锁。

采集：`CREATED → RUNNING → WAITING_DECISION / PARTIAL / COMPLETED / COMPLETED_EMPTY`。程序硬失败或附件不足进入隔离；新资料需要语义复核才能成为可检索的已验证版本。

修复分两条接口：任意 Python 补丁继续 `PATCH_PROPOSED → BLOCKED_NO_ISOLATION`；受限规则由宿主生成，固定 schema/执行器、包内可信样本、失败原件和临时库有界真实来源验证后，进入 `RULE_VERIFIED → RULE_ACTIVE / RULE_ROLLED_BACK`。通过门禁后自动本机启用，不开放正式代码治理。活动规则与上一版本原子保存，probation 未完成时下一次写入操作先回退；单一 CLI 写入锁保护切换，旧库无规则时使用内置规则。详情见 [规则自修复](docs/guides/self-repair.md)。

列表规则在 Adapter 内固定执行；正文的选择器由通用可信解析器执行。规则不包含 Python、网络调用、Cookie、主机或复核状态。故障留存原件及检查点，不把未知故障自动当作结构漂移。宿主负责候选生成和后续编排，CLI 不引入模型 API 或后台调度。

环境引导使用平台原生入口先检测 uv/Python，再补齐用户级环境。setup 与 Controller/workbench 共享返回的绝对 Python 与配置路径。工作台 lifecycle 默认回环、复用已健康服务、十端口尝试；专属随机令牌控制通道核对资料目录和进程身份后停止服务，不按猜测 PID 杀进程。

财政部 Adapter 使用 HTTP 与静态列表。税务 Adapter 先由有界面 Playwright 正常打开栏目，读取页面实际成功请求中的 `channelId`、页容量及当前会话 Cookie；随后在同一执行批次用 `requests.Session` 获取列表分页、详情和附件。Cookie 仅驻留内存，不写入日志或数据库；列表响应作为来源证据保存。无界面浏览器在本机返回 403 时不继续请求。来源新增主机需修改信任配置，不属于自动修复。


## 运行配置与跨环境（2026-09-30）

RuntimeSettings 经显式 CLI → 环境变量 → YAML → 默认值解析，并逐层校验；不会隐式读取工作目录配置。启动时解析并注入代理快照，客户端不再另读环境改变出口。网络、浏览器、运行预算与 Web 参数分别控制执行点，来源信任表与复核规则保持独立。参数字段与默认值见[配置指南](docs/guides/configuration.md)。

数据目录沿用旧默认；锁改为 Portalocker 的非阻塞独占锁。备份清单使用 POSIX 相对路径，兼容旧 Windows 清单并拒绝外平台越界；SQLite 使用编码文件 URI。财政部链路不启动浏览器；税务按配置启动 Chromium，Linux 有界面依赖 Xvfb/可用显示环境。无界面参数可配置但不自动启用，访问限制仍停止。

Web 为同源只读服务，默认回环；显式非回环地址允许团队局域网查询与下载，不增加认证。IPv4/IPv6 分别使用对应监听 socket。/api/ui-config 仅发布页面轮询与请求超时，其他运行配置不通过 Web 暴露。CLI 固定 UTF-8 JSON 输出，现有响应结构和数据库 schema 不变。

源码安装使用 uv.lock；wheel 分发内含默认来源、契约、治理材料、配置模板与静态页面。双宿主复用同一安装环境及绝对配置路径；跨机器使用备份恢复，不共享多机器写入目录。三平台 CI 与真实源站/宿主/局域网验收分别记录，详见[环境验证记录](docs/verification/environment-report.md)。
