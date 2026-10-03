---
type: project_document
status: active
updated_at: 2026-10-02
depends_on: [../../src/ftr/cli.py, ../../src/ftr/models.py]
terms: [TaskRequest, SemanticDecision, ResearchDraft, JSON CLI]
confidence: implementation_verified_locally
---

# 使用流程与接口

以下命令使用安装后的 `ftr`；首次 setup 后，源码目录优先使用 `sh scripts/run.sh`（macOS）或 `& .\scripts\run.ps1`（Windows）替代 ftr，它固定解释器与配置且不重新同步依赖。全局配置参数置于子命令之前，或通过绝对 `FTR_CONFIG` 路径统一指定。所有 ID 为实际响应值，示例中的占位符不可直接作为真实任务提交。

## 从采集到研究

先运行 `ftr config validate` 与 `ftr doctor`。选择 `mof`、`chinatax` 或两者；时间范围必须同时提供开始和结束日期，开始不能晚于结束。不明确的“最近”不能由 Controller 自行解释成过去一年。

```sh
ftr collect --sources mof --date-from 2026-09-01 --date-to 2026-09-28 --max-pages 1 --max-documents 2
ftr task status --task TASK_ID
ftr task pause --task TASK_ID
ftr task resume --task TASK_ID
ftr task cancel --task TASK_ID
```

默认 `--date-basis source_listing` 使用来源列表日期：财政部是栏目日期，税务法规库是成文日期。也可选择 `issued_date` 或 `published_date`；缺少该日期时保留限制，不猜补。`initial/incremental/backfill/rescan` 为任务模式；现有 incremental 对已见 URL 使用 14 天回看策略，不能据此声称全范围完整覆盖。

预算达到或部分来源失败返回 `PARTIAL`。页数和新增资料版本预算按每来源、每次执行增量计算，续跑从持久化检查点继续。暂停/取消是持久化请求；`STOP_REQUESTED` 不表示执行进程已退出。取消后的任务不会重新采集。工作台的 `RUNNING` 也不证明进程在线。

任务 JSON 可通过 `collect --request request.json` 提交；独立使用，不与业务 CLI 参数混合：

```json
{
  "source_ids": ["mof"],
  "date_from": "2026-09-01",
  "date_to": "2026-09-28",
  "date_basis": "source_listing",
  "mode": "initial",
  "max_pages": 1,
  "max_documents": 2,
  "idempotency_key": "本次任务的唯一标识"
}
```

相同幂等键必须对应相同任务输入。已经创建的任务范围不随运行配置改变。

## 语义复核

```sh
ftr decision list --task TASK_ID
ftr document show --id RECORD_ID
ftr evidence show --id EVIDENCE_ID
ftr schema
```

从待办取出实际 `decision_id`、`input_digest` 与证据 ID，阅读资料后形成决策文件。以下仅为结构模板，判断与理由须基于原件：

```json
{
  "decision_id": "实际待办ID",
  "input_digest": "实际输入摘要",
  "result": "UNCERTAIN",
  "reasons": ["实际待确认问题"],
  "evidence_ids": ["实际证据ID"],
  "model_id": null
}
```

`result` 可为 `PASS/REJECT/UNCERTAIN`。用 `ftr decision submit --file decision.json` 提交；摘要或证据不匹配会拒绝。没有语义复核不能自动标记 PASS；资料验证与候选代码批准也是两个不同流程。

## 检索、导出与研究

```sh
ftr search --query 增值税
ftr search --query 增值税 --include-limited
ftr export --query 增值税 --output /新的导出文件.jsonl
ftr research prepare --query 增值税
ftr research submit --file draft.json
```

默认 search、export、research prepare 使用已验证的最新资料；`--include-limited` 用于明确查看受限资料，不能提升其状态或使其成为合格研究引用。导出为 JSONL，已有目标文件拒绝覆盖。

研究草稿结构由 `ftr schema` 提供。每条陈述包含 `text/kind/citations`，引用包含 `record_id/evidence_id/quote`；摘录须能在指定版本正文或已提取附件中定位。`question/as_of/claims/limitations` 组成完整草稿。程序检查资料状态、证据版本与摘录位置；陈述是否被摘录支持仍须语义复核。成功响应的 `data.markdown` 为渲染后的研究稿。

## 修复与发布接口边界

```sh
ftr repair prepare --failure FAILURE_ID --patch candidate.patch
ftr candidate report --candidate CANDIDATE_ID
ftr repair test --candidate CANDIDATE_ID
ftr release status --adapter mof
ftr doctor --mode governed
```

只有允许类别的失败和故障来源 Adapter 路径能登记候选，未知补丁不执行。`repair test`、`release status`、governed 检查返回 BLOCKED；任何静态候选或报告不表示已完成隔离验证、批准或发布。

## JSON 协议与退出码

CLI 响应字段为 `schema_version/operation/task_id/status/data/errors/warnings`，当前协议版本 `1.0`。错误项含 `code/message/retryable`。

| 退出码 | 实际含义 |
|---|---|
| `0` | 命令被处理；仍需读 status，PARTIAL、WAITING_DECISION 不是业务完成 |
| `2` | 输入、配置、ID或文件错误；argparse 参数语法错误输出常规帮助文本，不保证 JSON |
| `3` | 明确的 BLOCKED 治理或发布门禁 |
| `5` | 执行失败、锁占用、缺运行依赖等；当前没有独立退出码 4 |

`serve` 前台运行，服务日志可能输出 stderr；停止后 CLI 输出 STOPPED。工作台 API 与 CLI 响应格式不同。

## 只读工作台 API

接口完整参数见运行服务的 `/openapi.json`，页面与 API 同源，不额外添加跨域调用协议。

| 路由 | 内容 |
|---|---|
| `GET /api/ui-config` | 轮询及查询超时毫秒值，不返回目录、代理或其他配置 |
| `GET /api/overview`、`GET /api/sources` | 状态统计及登记来源信息，不实时检测源站可用性 |
| `GET /api/policies` | q、source_id、quality_state、document_type、date_from/date_to、sort、page/page_size |
| `GET /api/policies/{id}`、`GET /api/policies/{id}/versions` | 正文、附件元数据及历史版本 |
| `GET /api/tasks`、`GET /api/tasks/{id}` | 任务、来源检查点、队列与新增版本计数 |
| `GET /api/tasks/{id}/items`、`GET /api/tasks/{id}/events` | 发现队列、状态事件与失败信息 |
| `GET /api/evidence/{id}/download` | 登记原件，校验路径与哈希后以附件下载 |

分页返回 `items/total/page/page_size`，page 从 1 开始，page_size 上限 100。业务响应包含 `queried_at`；错误包含 `error.code/error.message`。日期筛选使用 listing_date，最新版本先选取再筛选。Web 不提供采集、复核或其他写入接口。

## 环境与工作台入口（2026-10-02）

完整插件目录可通过 setup Skill 自动准备用户级环境；入口、能力选项与绝对调用路径见安装指南。后续调用不要依赖宿主 PATH 中的 ftr。

```sh
ftr workbench start --open
ftr workbench status
ftr workbench stop
```

start 仅回环监听，复用同资料目录已健康服务，默认从 8765 起尝试十个端口；显式 web.port 从指定端口起尝试，只有端口冲突换端口。启动后校验私有进程身份及只读 API；浏览器打开失败仍返回 URL。缺资料库不创建；stop 验证身份后停止本应用，不杀端口上的其他程序。已有前台 serve 命令保持可用，单独管理的 serve 不属于 lifecycle 进程。

规则自修复使用增量 repair 子命令，见[专门指南](self-repair.md)。旧 repair test 仍禁止未知代码执行，release status/governed 仍阻塞。RULE_ACTIVE 仅表示规则本机生效及首次续跑成功，不表示任务全部完成或资料自动通过复核。

## 可靠性与结果解释（2026-10-02）

HTTP 连接失败、超时及 502/503/504 默认最多尝试三次，退避 1 秒、2 秒加少量抖动，仍执行主机和请求间隔约束；401/403/429、越界和结构异常不按瞬态故障继续。详情瞬态失败耗尽后登记 FAILED 并继续，同来源连续三条才停止，成功后重置计数。列表失败保留原页检查点。Repair 验证的实际列表请求至多一次、详情及附件 HTTP 请求合计至多两次，重试和重定向均计数；时间与任务暂停仍受限。

每 30 秒向 stderr 输出最后检查点进度（发现、保存、失败、当前来源页数、耗时），请求执行中重复显示上次快照；不代表每条进度都是新检查点，不显示未知总量百分比。stdout 仍为一个最终 JSON，schema 命令增加 runtime_settings 定义。

采集结果 data 包含 stop_reasons、remaining_queue、resume_argv；remaining_queue 是本任务全部队列状态计数，待续跑数量由 PENDING/FAILED 判断。resume_argv 是命令参数数组，宿主须按原参数传递而非直接拼接 shell 字符串。预算结束后报告结果，等待用户要求续跑。

SIGINT/SIGTERM 登记中断意图，在检查点以 PARTIAL 收束；外部强制终止无法即时更新数据库，后续写入者取得独占锁后才将残留 RUNNING 修正为 PARTIAL 并登记 batch_stopped 审计。task status 与 Web 保持只读，不自行修正记录；writer_activity 只报告目录是否观察到写入锁，不确认具体任务在线。

Web 概览 quarantine_reasons 按最新隔离资料统计原因代码；详情 limitation_reasons 包含 code/label/original。多原因资料可计入多项，历史未知原因保留原文。任务接口增加 writer_activity 与 last_batch（停止原因与记录时间）。派生解释不改变资料 JSON、摘要、日期和复核状态。

PDF 警告汇总到结果 warnings，完整警告保存为证据并通过 pdf_warnings 审计绑定原附件；有警告的资料增加内容完整性待复核限制，不自动视为解析完整。

## 查看还差什么与恢复（2026-10-03）

普通用户可直接说“继续”“还差什么”“为什么没采到”“打开看看”。Agent 记录当前任务，存在歧义时只按来源与日期作选择；以下命令由 Agent 执行：

```sh
ftr task list --page 1 --page-size 20
ftr task report --task ID
ftr task missing --task ID
```

这三个入口及 search/document/evidence/decision list/research 查询均只读，不初始化数据库、不修正任务。报告列出已知条目与附件缺失、脱敏失败原因和处理建议；未读匹配数量 null 表示未知，不能解释为零。附件下载失败或预算未取可说“继续”补齐；已保存但不支持解析的 Office/扫描件请下载原件，重复采集不会补出解析能力。

缺日期资料仍允许采集，日期为空；日期筛选中保留未知分组，区间归属待确认。不能把四项中的任意一项成功等同全部完成：范围核查、已发现原件下载、内容可读性、语义复核分别显示。


## 正文公文排版（2026-10-03）

政策详情新增可选 body_display 展示结构，原 body_text 保持兼容。工作台从已校验的保存原件恢复段落，不访问官网；无法恢复时提示并显示保存文本。恢复排版不表示资料已通过复核。


## 全库与后续采集段落保留（2026-10-03）

后续采集由解析器 0.1.4 直接保留段落与换行，正文哈希按实际保存文本生成。已有资料通过保存原件恢复展示，不需重采集或批量改库。旧版本与复核记录继续可追溯；复杂表格回退文本时保留可用换行，不执行来源页面代码。
