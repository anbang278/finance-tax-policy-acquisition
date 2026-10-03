# 财税资料获取插件技术设计

## 资产位置

工程与插件统一位于仓库根目录。本页记录当前研发设计；原始交接文档保留在 `docs/history/`。`src/ftr/` 运行确定性代码，`skills/` 是七个宿主共享 Skill，`config/sources.yaml` 为精确来源清单，`contracts/` 描述接受的资料类型。Codex 使用 `.codex-plugin/plugin.json`，Hermes 使用根目录 `plugin.json`。

## 数据与调用

`ftr` 的标准输出为版本化 JSON。`TaskRequest` 冻结时间范围、来源、日期口径与预算。列表原件先落 SHA-256 文件，再事务保存发现记录和分页检查点。资料原件、提取内容及语义判断分开；相同 URL 更新时增加来源版本。新资料在语义复核前停于 `WAITING_DECISION` 或隔离。

时间范围必须来自用户可确定的表达。Controller 对缺失或模糊的期限先反问；CLI 对缺少任一起止日期的采集命令在创建数据目录前返回输入错误，`TaskRequest` 仍要求两个日期字段。这样不会由运行时代替用户选择“过去一年”。

外部文件不可作为系统指令。字段缺失用空值和原因表示。来源主机逐个登记，重定向每跳核验。仅实际使用有效回环 IP 的 HTTP/HTTPS 代理时，允许登记域名解析到 `198.18.0.0/15` 代理映射网段；无代理或远端代理仍拒绝。旧 DNS 放行开关已废弃，不能无代理放宽该边界。

## 来源适配

财政部列表是静态 HTML，页面脚本给出总页数；详情位于司局子域名，页面仍可能使用 HTTP 链接，采集时对已登记主机升级为 HTTPS。税务列表由浏览器发起动态请求；本机无界面 Playwright 的列表响应为 403，有界面 Playwright 正常导航返回 200。税务 Adapter 从成功的浏览器请求读取 `channelId`、页容量和会话 Cookie，再用同一会话的 `requests.Session` 获取列表分页、详情及附件。`channelId` 是页面请求中的栏目参数，不将其误称为独立授权令牌；Cookie 仅在内存中使用。来源端出现登录、验证码或封禁时暂停。

## 修复与治理

Python 补丁保存已解释的 unified diff 并按来源限制路径，仍固定 `BLOCKED_NO_ISOLATION`。受限规则闭环按 2026-10-02 扩展支持固定门禁验证、自动本机启用和回退。未来 Level 4 的接口按“候选摘要—可信测试报告—人工签名收据—发布账本”扩展，但本期没有可绕过的空实现。

## ADR

- ADR-001：双宿主只共享业务包和 Skill，分发清单各自验证。
- ADR-002：税务站由有界面 Playwright 建立正常会话，再以 `requests` 复用浏览器实际取得的参数和 Cookie；HTTP 403 不通过代理或伪造身份处理。
- ADR-003：栏目全类型保存，按类型复核；研究引用优先原文。
- ADR-004：旧版办公附件保存原件，内容未解析时不声称完整。
- ADR-005：未知 Python 候选缺少隔离环境时停止；固定语言的规则按门禁本机启用。

## 本机政策工作台（2026-09-30）

新增 `ftr serve --port 8765`，默认监听 `127.0.0.1`，可显式指定非回环地址用于局域网。FastAPI 和 Uvicorn 属于 `web` 可选依赖；模块化 HTML、CSS、JavaScript 随 Python 包分发，无 Node 构建链。默认首页是政策库，采集监控为第二入口，来源信息放入详情面板。

独立 `ReadQueries` 每次使用 SQLite `mode=ro` 和短事务快照，不初始化目录或表，不持有采集器独占锁。Web 启动分支位于 CLI 写入路径之前；不新增数据库表或改变采集器。按 `(source_id, canonical_url)` 选择最高来源版本、最高提取版本后筛选；历史记录单独读取。Web 默认浏览全部质量状态，CLI 与研究检索继续执行已复核准入规则。

`/api/overview`、`/api/sources` 提供概况；`/api/policies` 及其详情/版本接口提供资料阅读；`/api/tasks` 及任务详情、`items`、`events` 提供队列与事件。列表统一分页，响应附查询时间。来源日期筛选使用 `listing_date`，前端同时显示各来源的 `listing_date_kind`，不猜补缺失日期。

任务统计分别查询发现条目的状态、来源检查点与本任务创建的资料版本。队列通过来源和规范化 URL 关联最新已保存资料，明确标记是否由本任务创建该版本，不能把复用资料折算为新增。没有心跳或当前项记录，不显示实际进程在线状态、当前下载项或未知总量的完成百分比。

可见页面默认每 5 秒刷新（间隔与超时可配置）；后台标签暂停轮询。渲染避免替换未变化内容，保存选中项、筛选与滚动位置；断连时保留上次成功内容并提示过期。页面使用文本转义展示外部内容。原件下载限定数据库登记 ID、证据目录边界及哈希，统一以附件提供，页面原件不作为 HTML 执行。


## 跨环境配置与部署（2026-09-30）

`RuntimeSettings` 按 data/network/browser/collection/web 分组，通过 CLI、环境、显式 YAML 和内置默认值逐层覆盖并校验。路径在各层按约定基准规范化，未知字段与重复 YAML 键拒绝。运行期固定解析后的代理，HTTP、会话请求和 Chromium 使用同一出口；展示与审计脱敏。

新任务冻结页数和新增版本数预算，续跑沿用请求，执行时间与字节预算采用当前配置。每次运行追加 runtime_config 审计，不改既有任务摘要或数据库结构。财政部不再启动 Playwright；税务按配置启动 Chromium，有界面 Linux 通过 Xvfb 提供显示。配置只能调运行参数，不能关闭来源信任、复核或发布门禁。

Portalocker 替代 fcntl，使用原锁文件与非阻塞独占语义。备份规范相对路径、兼容旧 Windows 分隔符；SQLite URI 使用 Path.as_uri 编码；verify/restore 不初始化无关运行目录。CLI JSON 固定 UTF-8 输出。

Web 默认回环监听，显式非回环监听时允许局域网 Host；不增加认证或写入接口。IPv4/IPv6 监听使用对应 socket family。新增 /api/ui-config 仅提供前端轮询与超时；页面从接口取得参数后启动轮询，界面不再固定显示“本机”。

- ADR-006：运行配置与来源信任分离；显式配置入口及逐层校验优先于隐式目录搜索，便于双宿主与独立 wheel 运行。
- ADR-007：保持旧默认数据目录和锁文件，跨平台库承担 OS 锁差异；跨机器数据通过完整备份迁移，不支持共享文件系统多机器写入。
- ADR-008：团队局域网访问按本轮明确要求不增加认证；默认本机监听继续适用于独立使用。

操作入口和字段定义见 [configuration](../guides/configuration.md)、[installation](../guides/installation.md)、[usage](../guides/usage.md)、[operations](../guides/operations.md)，验证边界见 [environment-report](../verification/environment-report.md)。

## 伙伴开箱使用与受限规则闭环（2026-10-02）

本轮在原两来源范围内新增 setup、workbench Skill；用户级环境引导返回绝对运行路径，按来源/查询能力安装依赖，不触发采集。只读工作台新增启动、复用、身份验证停止及自动打开浏览器接口。

受限规则自修复使用固定执行器与可信样本，支持列表/正文定位、预定义日期、分页和 JSON 字段映射；失败原件绑定候选及验证报告，最多两轮候选。有界真实来源验证通过后自动本机启用并续跑，失败及中断回退；不执行未知 Python，不更改资料复核门槛，governed 仍未开放。详见 ../guides/self-repair.md 与当前架构。

追踪入口：setup → scripts/setup.sh、setup.ps1、setup_runtime.py → tests/test_setup.py；规则闭环 → rules.py、rule_repair.py、runtime.py → tests/test_rule_repair.py；工作台 → workbench.py → tests/test_workbench_lifecycle.py。既有回归、wheel 与三平台 CI 继续执行。

验收须分开记录离线故障注入、真实源站及 macOS/Windows × Codex/Hermes 会话。受控网络替身不代表真实访问成功，缺宿主/设备不能计通过。当前实现结果以 acceptance-report.md 本轮补充为准。

## 首次配置与运行可靠性升级（2026-10-02）

面向 Windows/macOS 由 AI 协助的普通用户，以原生 setup/run 为主；不交付 Docker 或特定宿主适配。用户提供来源和明确日期，环境优先复用、缺失补装，预算结束后等待用户要求续跑。

自动配置隔离子进程 PYTHONHOME/PYTHONPATH、保留 Web extra；显式浏览器优先，自动候选按已有 Chromium、Edge、Chrome 顺序逐个启动验证，缺失才安装。配置不重写，报告运行入口及 Git/源码包版本证据。工作台通过令牌、目录和实例握手登记实际服务 PID，保留旧状态识别；启动失败分类且仅端口冲突换端口。

网络默认三次尝试、1/2 秒基础退避加抖动；详情连续三条瞬态失败才停止，列表失败保留检查点。暂停、取消、预算和 Repair 实际请求限额约束重试。进度独立写 stderr，最终 JSON 增加停止原因、队列和续跑参数。强制中断残留只在取得独占锁后修正；查询保持只读。隔离原因由现有 limitations 派生，PDF 警告保留证据并增加内容限制，不改变旧资料状态与摘要。

接口变更为 RuntimeSettings 新增五个参数、schema 增加 runtime_settings、Web 增加 quarantine_reasons/limitation_reasons/writer_activity/last_batch，无新增数据库表，不改冻结 TaskRequest。浏览器模块纳入 Repair 执行器指纹，旧验证报告需重验。实施与真实验收分别记录，详见 acceptance-report.md 本轮记录。

## 持久化恢复与统一诊断（2026-10-03）

SQLite 写入入口将 `user_version` 从旧值 0 增量迁移为 2，新增 attachment_work 与 listing_pages；只读入口不建库、不迁移、不取得采集锁，也不执行残留任务恢复。高于支持版本的数据库拒绝写入。旧备份可读取，在首次明确写入时迁移；原 request_json/request_digest、资料与决策不迁移改写。

附件队列以任务、来源、资料地址、附件地址唯一关联当前资料版本。下载缺失项，下载完成后同一正文的附件变化生成新的提取版本；正文原件变化仍生成来源版本。资料去重排除 record_id、quality_state 和任务派生 date_range_status，纳入附件、内容及限制变化。旧任务在明确续跑时从已保存资料重建附件队列。恢复优先使用当前任务关联的正文版本，在该来源版本下追加提取版本；其他任务的较新来源版本保持最新位置，不因补旧附件倒退。

列表检查点与实际请求/响应 URL、原件 ID、逐页枚举策略共同保存。不采用未验证的官方日期过滤，不以日期排序假设终止；连续批次使用检查点和已存条目，已保存原件不会被附件续跑重复请求。

TaskRequest 新输入禁止额外字段，历史读取只取已知字段而保留原始序列化事实。DocumentRecord.date_range_status 为 within_range/unknown；缺日期不进入硬 limitations。质量决策显式区分 PASS→validated（须无硬限制）、REJECT→rejected、UNCERTAIN→quarantined。

失败保留脱敏事实、HTTP 状态、阶段、地址、异常及原因类型，恢复后标记 resolved，不删除原失败。diagnostics.task_report 在只读快照中派生来源覆盖、缺失原件、内容限制、复核、未知范围及下一步；不依赖 FastAPI。采集响应增加 data.report/pending_attachments；CLI 增加 task list/report/missing，Web 增加任务 report/missing 子接口。现有响应与命令保留。


## 正文公文排版（2026-10-03）

详情响应新增 body_display：mode 为 structured/plain，blocks 为受控 type（paragraph/heading）、text、align（left/center/right），reason 为回退原因或 null。复用 evidence_download 路径与哈希校验；仅匹配已保存正文非空白字符顺序的正文容器可以恢复。嵌套段落去重，保留 br/已有换行，表格和 pre 回退；独立且完全匹配详情标题的首块不重复展示。全部文本转义，无原站 HTML/CSS 执行。

工作台 HTML、静态资源及 API 使用 no-store；前端入口携带排版资源更新标识，以刷新已打开浏览器的旧脚本与样式。


## 全库与后续采集段落保留（2026-10-03）

content_layout.extract_blocks 为采集与展示共用的纯文本结构提取。段落之间使用两个换行，br 使用单换行；嵌套行内容器中的段落也保留，评论/脚本/样式不进入正文。table 行边界保留、单元格文字以空格分隔；展示中的纯图片空表格不再触发整篇回退，含文字的复杂表格仍回退保存文本。新 parse_detail 标注 parser_version=0.1.4；原件未变但重新提取正文改变时沿用现有提取版本机制，旧摘要保持原样。
