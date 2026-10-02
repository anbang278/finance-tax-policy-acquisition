---
type: project_document
status: active
updated_at: 2026-09-30
depends_on: [../../src/ftr/cli.py, ../../src/ftr/models.py]
terms: [TaskRequest, SemanticDecision, ResearchDraft, JSON CLI]
confidence: implementation_verified_locally
---

# 使用流程与接口

以下命令使用安装后的 `ftr`；源码环境在命令前加 `uv run`。全局配置参数置于子命令之前，或通过绝对 `FTR_CONFIG` 路径统一指定。所有 ID 为实际响应值，示例中的占位符不可直接作为真实任务提交。

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

start 仅回环监听，复用同资料目录已健康服务，按 8765–8774 尝试端口。启动后校验私有进程身份及只读 API；浏览器打开失败仍返回 URL。缺资料库不创建；stop 验证身份后停止本应用，不杀端口上的其他程序。已有前台 serve 命令保持可用，单独管理的 serve 不属于 lifecycle 进程。

规则自修复使用增量 repair 子命令，见[专门指南](self-repair.md)。旧 repair test 仍禁止未知代码执行，release status/governed 仍阻塞。RULE_ACTIVE 仅表示规则本机生效及首次续跑成功，不表示任务全部完成或资料自动通过复核。
