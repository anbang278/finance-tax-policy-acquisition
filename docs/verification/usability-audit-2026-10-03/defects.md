# 缺陷清单

基线 `8c31e1a`，2026-10-03。本轮不修复产品。下列问题有代码、实际数据或隔离故障探针证据；自然语言识别缺少真实评测的情况另列为验收缺口，不混入确认缺陷数。

复现脚本、JSON 与截图路径均见[证据索引](evidence-index.md)。故障探针使用固定契约样本和独立资料目录，不代表真实源站发生过同类故障。首次附件测试后关闭 Collector，再创建新 Collector 续跑，模拟 CLI 每次调用的新实例。

## D-01 · P1 · 附件失败后普通续跑不补齐，任务可能显示已完成

- **复现**：创建单来源、列表枚举完毕的一条政策；正文成功，直接附件请求耗尽后抛出 TransientFailure。首次返回 PARTIAL。恢复附件请求响应，关闭并新建 Collector，执行同任务 resume。
- **期望**：重新下载失败附件；附件未补齐时持续指出具体缺失，不能只凭正文队列 SAVED 宣告采集完整。
- **实际**：正文队列被记为 SAVED；首次 `failure_ids=[]`、`stop_reasons=[]`，只有 warnings 提到附件失败。续跑没有任何正文或附件请求，附件仍为 failed、无 evidence_id，返回 COMPLETED、warnings=[]、本批 incomplete_documents=0。资料仍 quarantined，但总体结果会掩盖没有补齐的采集项。
- **原因定位**：[`runtime.py`](../../../src/ftr/runtime.py) 的附件异常只进入附件状态与 warnings；`_process_refs` 随后将正文条目保存为 SAVED；`_run_source` 只重试 FAILED 正文条目，最终完成判定没有检查持久化的缺失附件。
- **证据**：`probe-results.json` 的 ATTACHMENT_RECOVERY；最终请求列表仅包含初次正文和初次附件，各一次。
- **最小修复**：为缺失附件保留可恢复工作项和持久化原因；续跑补齐失败/预算未取附件。资料版本与正文证据继续保持可追溯，不把完整性问题自动改判成通过复核。
- **复验**：下载失败→恢复→续跑；字节/时间预算阻止附件→续跑；一个失败附件和一个已保存附件；重跑幂等；持续失败仍明确 PARTIAL 或独立未完成标记。

## D-02 · P1 · 指定短日期范围仍扫描完整历史栏目

- **复现**：两来源分别采集 2026-09-21 至 2026-09-30，每来源每批 3 页、10 份。
- **期望**：在有证据的日期覆盖判定下，及时结束该区间的发现；对无法证明完整的情况说明所需继续工作，而不是让小白为了十天资料反复扫多年栏目。
- **实际**：财政部三页日期已到 2025-08，税务三页已到 2026-06，仍 discovery_done=false、PARTIAL。原件声明财政部 20 页，税务 5,019 条/每页 10 条。代码持续枚举栏目直到末页，日期仅用于处理时排除条目。
- **原因定位**：[`runtime.py`](../../../src/ftr/runtime.py) `_run_source` 的终止条件为 discovery_done；日期过滤位于 `_process_refs`。没有基于区间的发现完成判定。
- **证据**：三份 collect JSON；`independent-list-inventory.json`；各来源三份 `.raw`。
- **最小修复**：优先验证来源官方日期筛选能力；不能筛选时核实排序、置顶、缺失日期及日期口径，再设计安全收束。不得仅因看到一条旧资料就停止，尤其不能把财政部栏目日期与税务成文日期视为同一排序保证。
- **复验**：最近短区间、历史回填、空区间、置顶/乱序、缺失日期、边界日期、多页跨界；以独立官方清单验证无遗漏，同时记录实际请求量。

## D-03 · P1 · 失败事实未完整持久化，无法给出准确恢复建议

- **复现**：隔离列表阶段抛出 `AccessBlocked('HTTP 429 请求频率受限')`，读取当次结果，再调用 failure_context 与工作台“事件与失败”。
- **期望**：后续仍能区分 403、429、DNS/主机限制等事实；小白能看到受影响来源/阶段、尚未检查内容，以及等待或处理后如何继续。
- **实际**：429 仅出现在当次 warnings；故障 context 保留 category=ACCESS_RESTRICTED、error_type=AccessBlocked，却没有具体 message、状态码或失败 URL。页面没有“429”“限流”或下一步，直接展示英文分类及原始 JSON。列表失败的任务卡还显示 0 条失败，因为计数仅来自已发现条目。
- **原因定位**：[`runtime.py`](../../../src/ftr/runtime.py) `_failure` 没有保存异常事实；[`app.js`](../../../src/ftr/web/static/app.js) renderEvents 直接展示事件名及 JSON，没有面向用户的恢复说明。
- **证据**：probe-results.json 的 FAILURE_REASON_PERSISTENCE；browser/failure-result.json；browser/06-failure-diagnosis.png。
- **最小修复**：保存脱敏错误原因、HTTP 状态、阶段和 URL，区分已失败条目与列表发现失败；由结构化事实产生中文说明及下一步。未知原因须明确未知，不能自动归因为网络。
- **复验**：403、429、超时、依赖/浏览器失败、结构变化、未知错误；关闭原对话后仅依靠任务诊断仍能解释；原因中不得出现 Cookie、令牌或代理密码。

## D-04 · P1 · 错写范围字段被静默忽略，来源扩大为两来源

- **复现**：`TaskRequest.model_validate({'sources':['mof'],'date_from':'2026-09-21','date_to':'2026-09-30'})`；`collect --request` 使用相同模型。
- **期望**：拒绝未知字段 sources，提示正确字段 source_ids，交给 Agent 修正。
- **实际**：校验成功，sources 静默丢弃，source_ids 使用默认值 `['mof','chinatax']`。仅验证了模型的错误接受与范围扩大，**没有执行这份错误请求的真实采集**。
- **原因定位**：[`models.py`](../../../src/ftr/models.py) TaskRequest 未禁止 extra，来源字段又有默认值。RuntimeSettings 的严格未知字段检查不能保护 TaskRequest。
- **证据**：request-field-probe.json。
- **最小修复**：业务请求拒绝未知字段，核验旧任务及已保存 JSON 兼容；合法的省略字段行为单独保留，不让拼写错误等同于省略。
- **复验**：sources/source_id/source_ids、未知 date 字段、合法默认来源、错误输入无业务写入、旧任务读取和续跑。

## D-05 · P2 · REJECT 与 UNCERTAIN 被保存为同一质量状态

- **复现**：隔离完整资料生成待办，以正确 digest 与 evidence_ids 提交 REJECT；对另一条隔离资料提交 PASS 作对照。
- **期望**：REJECT→rejected，UNCERTAIN→quarantined，证据充分的 PASS→validated。
- **实际**：REJECT→quarantined；PASS→validated。页面无法按 rejected 展示该拒绝记录。没有把拒绝资料放进默认正式研究，因此不是已验证检索准入绕过。
- **原因定位**：[`repository.py`](../../../src/ftr/repository.py) submit_decision 的二分映射将所有非 PASS 都设为 quarantined。
- **证据**：probe-results.json 的 DECISION_REJECT 与 DECISION_PASS。
- **最小修复**：采用三个结果的显式映射，保留硬限制不能被 PASS 覆盖的门槛。
- **复验**：三种结果、重复提交、过期摘要、错误证据、页面筛选与正式检索排除。

## D-06 · P2 · Controller 与 Repair 的预算续跑规则冲突

- **复现**：对照 [`controller/SKILL.md`](../../../skills/controller/SKILL.md) 第 12 行与 [`repair/SKILL.md`](../../../skills/repair/SKILL.md) 第 15 行。
- **期望**：本任务已确认每个批次结束后等待用户说“继续”。修复验证内的首次有界续跑与后续普通批次边界明确。
- **实际**：Controller 要求预算后等待；Repair 允许仅预算 PARTIAL 时在原授权范围继续。属于确定的规则冲突，**本轮未实测模型因此自动续跑**。本轮 Agent 遵循用户明确约定并停止真实首批。
- **最小修复**：两 Skill 统一预算停止规则，避免加载 Repair 后改变用户交互约定。
- **复验**：普通批次预算、修复成功后的普通批次预算、Repair 回退及候选耗尽，确认没有未请求的下一批。

## D-07 · P2 · 普通 CLI search 会修正任务状态并写审计

- **复现**：隔离库建立一条残留 RUNNING 任务，不持有写锁，执行 `ftr --data-dir DIR search --query 测试`。
- **期望**：普通检索只读，残留任务修正发生在明确写操作。
- **实际**：search 返回 COMPLETED，任务却从 RUNNING 改为 PARTIAL，数据库 SHA-256 改变，并写入异常结束审计。`task status` 与 Web 的只读性通过，两者不受此缺陷影响。
- **原因定位**：[`cli.py`](../../../src/ftr/cli.py) 多种业务命令共用 data_lock/Repository/recover_interrupted 路径。
- **证据**：read-probe-results.json 与 read_probe.py。
- **最小修复**：搜索、资料读取、待办读取等按需使用只读连接；只有明确写入者执行恢复逻辑。
- **复验**：残留 RUNNING、采集锁持有、空库、search/document/evidence 等读取；数据库字节和审计数量不变，写操作仍可安全恢复残留任务。

## D-08 · P2 · 财政部分页原件缺少准确来源 URL

- **复现**：财政部真实采集三页，读取三份列表证据的 source_url/final_url。
- **期望**：第二、三页证据定位到实际请求页与最终响应页。
- **实际**：三份不同列表原件的 source_url/final_url 均是栏目入口 `/zhengcefabu/`，无法通过证据元数据辨认 `index_1.htm` 与 `index_2.htm`。原件、哈希仍存在，因此不是证据文件丢失。
- **原因定位**：[`runtime.py`](../../../src/ftr/runtime.py) `_run_source` 对财政部把 listing_url 固定为 config.entry；Adapter 保存的实际 final_url 未用于 EvidenceStore。
- **证据**：mof-listing-evidence-metadata.json，三份原件哈希互不相同但 URL 相同。
- **最小修复**：记录实际分页请求及最终 URL，与页面检查点关联；兼容已有证据，不伪造旧证据出处。
- **复验**：首页、二/三页、合法重定向、断点续跑和旧资料库读取。
