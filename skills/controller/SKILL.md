---
name: controller
description: 接收财税政策采集或研究任务，确定范围与来源，调用本地 ftr 命令并处理待复核资料。
---

# 财税资料主控

先读取插件的 `DESIGN_CONSTITUTION.md` 和 `ARCHITECTURE.md`。首次操作先复用 `setup` Skill，按来源选择 mof/chinatax/all，读取 runner 与配置；后续优先使用绝对 runner 调用，避免宿主 Python 环境污染。确认运行环境 `ftr doctor --mode local-demo` 返回的状态及分项检查。使用宿主继承的绝对 `FTR_CONFIG`/`FTR_DATA_DIR` 或显式全局参数，不自行切换资料目录。doctor 不证明源站访问；Linux 有界面税务采集缺显示环境时使用部署者配置的 Xvfb，缺依赖则报告阻塞。只选已登记的财政部与税务总局来源。

发起采集前，先从用户原话确定时间范围。用户给出明确起止日期，或“2026 年”“上个月”“最近 30 天”等可唯一换算的区间时，将其转换为具体起止日并在执行前告知用户。“最近”“近期”“最新政策”等没有确定长度的说法，以及只给出起点或终点，都视为时间范围未明确。此时先反问用户希望采集哪个时间段；收到答案前不要调用 `ftr collect`，不要自行采用过去一年或其他默认区间。可简短询问：“您希望获取哪个时间范围的政策？请给出起止日期，或说明如‘最近 30 天’。”

调用 `ftr collect` 或 `ftr task resume` 后读取 JSON 状态。`WAITING_DECISION` 时使用 `ftr decision list --task ID`，按需加载 `semantic-review`。研究时加载 `research`。读取 data.stop_reasons、remaining_queue、resume_argv；达到批次预算时报告进度并等待用户要求续跑，不自动连续运行。stderr 进度是最后检查点，不是全量完成百分比。遇到 data.failure_ids 时先按类别区分：TRANSIENT_NETWORK 报告网络失败，ACCESS_RESTRICTED 停止，不因这些类别生成结构规则。只有 STRUCTURE_DRIFT 按类别加载 repair，执行规则验证、自动本机启用与续跑闭环；访问限制与未知故障停止并报告。打开查询页交给 `workbench`。

不要把 `PARTIAL`、`BLOCKED` 或 `WAITING_DECISION` 写成完成。不要启用未知来源、修改正式资料状态、批准未知 Python 候选或调用未开放的正式发布器。当前版本为本地演示，不能承诺已具备正式 Level 4 权限隔离。


## 小白表达与范围保护

财政部、财正部明确映射为 mof；税务总局、国家税务总局、税总明确映射为 chinatax。“财税”“税务局”等不能唯一确认来源时只作必要澄清。地方政策、全国政策全集不在登记范围，说明能力边界，不自动改为两来源。调用前告知实际来源与日期口径；JSON 只用 schema 中字段，INPUT_ERROR 时修正请求，不改默认范围来凑成功。

按用户本地日期换算“上个月”（上一个自然月）、“最近 30 天”（含今日的 30 个自然日）；“从去年开始”缺结束边界时追问结束时间。错误或不存在的日期需用户纠正，不猜补。

当前会话记录已绑定任务；“继续”只对该任务执行一批 resume。缺绑定时先只读 task list，根据来源、日期与用户语境匹配；多个候选则用来源/范围作简短选择，不让用户复制任务 ID。“还差什么”“为什么没采到”读取 task report/task missing；“打开看看”交 workbench。用户取消后不续跑。

## 每次采集后的五项说明

读取 data.report；旧响应没有报告时执行 task report --task ID。按来源说明已保存资料数、正文与附件、日期口径和查看入口；列出 missing_items 的标题或链接，未遍历范围的匹配数量未知；按 failures 的事实与 certainty 说明原因；给出 next_step 的行动、执行者与恢复条件；分别说明 completion 的覆盖、原件、可读性与复核。技术命令由 Agent 执行，普通用户不需要填写来源代码、任务 ID 或配置。

日期字段为空仍可采集成功，不猜补、不单纯因日期缺失隔离；但日期未知资料不能声称属于指定区间。Office、扫描件已保存原件时说明如何下载阅读；重复采集不会增加解析能力。预算是单次共享的检查点预算，不是硬超时，也不是采集故障。达到预算后停止，等待用户说“继续”；修复验证也不能授权追加普通批次。
