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

## 首次配置与运行可靠性升级（2026-10-02）

面向 Windows/macOS 由 AI 协助的普通用户，以原生 setup/run 为主；不交付 Docker 或特定宿主适配。用户提供来源和明确日期，环境优先复用、缺失补装，预算结束后等待用户要求续跑。

自动配置隔离子进程 PYTHONHOME/PYTHONPATH、保留 Web extra；显式浏览器优先，自动候选按已有 Chromium、Edge、Chrome 顺序逐个启动验证，缺失才安装。配置不重写，报告运行入口及 Git/源码包版本证据。工作台通过令牌、目录和实例握手登记实际服务 PID，保留旧状态识别；启动失败分类且仅端口冲突换端口。

网络默认三次尝试、1/2 秒基础退避加抖动；详情连续三条瞬态失败才停止，列表失败保留检查点。暂停、取消、预算和 Repair 实际请求限额约束重试。进度独立写 stderr，最终 JSON 增加停止原因、队列和续跑参数。强制中断残留只在取得独占锁后修正；查询保持只读。隔离原因由现有 limitations 派生，PDF 警告保留证据并增加内容限制，不改变旧资料状态与摘要。

接口变更为 RuntimeSettings 新增五个参数、schema 增加 runtime_settings、Web 增加 quarantine_reasons/limitation_reasons/writer_activity/last_batch，无新增数据库表，不改冻结 TaskRequest。浏览器模块纳入 Repair 执行器指纹，旧验证报告需重验。实施与真实验收分别记录，详见 acceptance-report.md 本轮记录。

## 体验升级补充（2026-10-03）

运行数据库版本 2 增加 attachment_work 与 listing_pages；CLI 写入持独占锁并迁移，普通 Repository(readonly=True) 和 Web 只读快照不初始化、不迁移、不恢复任务。附件缺失先于列表推进恢复，补齐产生新提取版本，旧资料与复核保留。日期未知是范围信息，不是硬内容限制。

diagnostics.task_report 为核心统一报告，供采集 data.report、只读 task list/report/missing 与 Web 消费。覆盖完整性、原件完整性、内容可读性与复核独立表达；失败事实脱敏后持久化，修复后保留 resolved 历史。分页记录实际请求与最终地址。未经证明的日期筛选不用于完成判定；预算后等待用户继续。
