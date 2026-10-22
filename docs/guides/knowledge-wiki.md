# 财税知识与领域 Wiki

本功能在采集与证据管理之上增加知识条目和专题编纂。数据库 v5 保存运行事实，原件目录保留原始证据，Markdown 用于导出阅读。首个可编辑导航为企业所得税优惠：适用主体、优惠条件、申报与资料留存。建立导航不会自动产生政策结论。

## 从工作台开始

通过已有运行入口执行 `workbench start --manage --open`，在政策工作台点击“知识与 Wiki”。本人会话沿用当前浏览器 Cookie；普通或非回环入口仍只读。新页面在 `/wiki`，可直接阅读领域、候选、历史与正式内容。

在“加工与维护”依次建立导航、重建证据索引、选择已复核资料与专题、创建任务。在加工任务中选择 Codex/Hermes 或配置好的模型 API。新资料和变化由后台每 30 秒分批核查，进入“待处理变化”；认领后才执行加工，不会自动消耗模型调用量。

候选完成后，对照原文、条件与例外，必要时发起独立 AI 预审，再填写人工审核理由并勾选确认。审核通过后，“启用正式知识”或“发布 Wiki”分别操作。人工草稿保存即改变编辑修订号；旧 AI 输入不能覆盖它。编辑历史版本会建立新草稿，不改变历史正文。

## CLI 与宿主交接

以下 `ftr` 均指当前项目 setup 给出的运行入口；全局 `--config`、`--data-dir` 放在子命令之前。测试请使用新建的隔离数据目录，真实库的写入先按当前任务授权执行。

```sh
ftr knowledge seed
ftr knowledge index rebuild
ftr knowledge domain list
ftr knowledge topic list
ftr knowledge search --query 企业所得税 --kind all
ftr knowledge job create --file extraction-request.json
ftr knowledge job start --id JOB_ID --executor host
ftr knowledge job export --id JOB_ID --output task-package.json
ftr knowledge job submit --file result.json
ftr knowledge job show --id JOB_ID
ftr knowledge job retry --id JOB_ID
ftr knowledge job cancel --id JOB_ID
ftr knowledge job list
```

创建任务示例：

```json
{"job_type":"extract","topic_id":"cit-conditions","records":["ACTUAL_RECORD_ID"],"max_calls":3,"max_input_chars":60000}
```

`job_type` 支持 extract、merge、compile、revise、precheck。compile/revise 传 `knowledge_versions`；更新已有内容传 `object_id`。precheck 指定 `target_version`；它只保存 AI 问题清单。

任务包包含完整 ProcessingResult、ItemContent、PageContent JSON Schema、来源快照、片段、模板、人工基线与输入摘要。宿主应返回任务和尝试身份、原摘要、submission_key、outputs、findings 和实际执行者信息。未知模型标记 unknown。输出字段不能携带人工审核或发布权限。

每个引用给出 record_id、evidence_id、source_part、segment_id、text_version_digest、quote、start_offset、end_offset、page_number；位置为片段内 Unicode 码点 `[start_offset,end_offset)`，不采用 JavaScript UTF-16 下标。PDF 页码从 1 开始，必须来自任务包。相同摘录出现在另一个附件或位置，不能替代实际引用。

同一提交键不同内容会报冲突；相同内容重复提交返回原接受结果。已接受结果修改时显式新建尝试。输入变化则 retry 建立冻结最新输入的新任务；最新来源尚未复核时须先处理来源资格。

```sh
ftr knowledge review submit --file human-review.json
ftr knowledge publish submit --file publication.json
ftr knowledge edit submit --file edit.json
ftr wiki draft --file compilation-request.json
ftr wiki show --id PAGE_ID
ftr wiki diff --id PAGE_ID --version CANDIDATE_ID
ftr wiki review --file human-review.json
ftr wiki publish --file publication.json
ftr wiki export --id PAGE_ID --output topic.md
ftr knowledge impact scan
ftr knowledge impact list
ftr knowledge impact resolve --file resolution.json
```

人工审核文件：`{"version_id":"...","decision":"approved","reason":"实际核对理由","confirmed":true}`。退回和撤回使用 rejected / withdrawn。正式启用或发布文件：`{"version_id":"...","expected_revision":0,"expected_current":null,"confirmed":true}`；首次发布 current 为 null，后续必须使用读取到的当前版本和修订号。

编辑文件包含 object_id、title、content、expected_revision。AI 不能设置 `applicability_verified=true`；人工在编辑新草稿时核验期间。`as_of` 仅筛选明确且已人工核验的适用区间，未知不会当作匹配。

批量人工操作：`knowledge batch --file batch.json`，文件形如 `{"operations":[{"operation":"review","arguments":{...}},{"operation":"publish","arguments":{...}}]}`。最多 100 项，仅支持 review/publish；逐项保存成功或失败，不因一项冲突静默覆盖其他内容。

## 可选模型 API

在外部运行配置的 processing 分组设置 enabled、base_url、model、api_key_env；默认禁用且不预设模型。配置只保存密钥环境变量名，实际密钥由宿主环境提供，变量名避免使用保留的 FTR_ 前缀。支持本机 HTTP；远程地址要求 HTTPS，禁止内嵌密码和查询参数。复用既有代理配置，禁止自动重定向。

服务根地址例如 `https://YOUR-PROVIDER/v1`；程序追加 `/chat/completions`。单实例并发为 1，默认超时 120 秒、输出 8192 tokens。兼容服务按实际能力显式选择 response_format（json_schema/json_object/text）和 token_limit_parameter（max_completion_tokens/max_tokens）；不会静默更改协议或换供应商。

```sh
ftr knowledge job start --id JOB_ID --executor api
ftr knowledge job run-api --id JOB_ID
```

工作台后台自动执行明确启动的 API 任务。模型调用在业务锁之外；结果先保存为不可变文件并入队，再在短事务中入库。429/明确可重试的服务端错误最多 3 次，全部计入任务预算；超时和进程中断显示远端结果不确定，需显式重试，可能再次产生远端费用。超出输入大小预算要求拆分任务，不静默截断。未返回 usage 时用量为未知。

API_KEY_MISSING / MODEL_NOT_CONFIGURED：配置密钥或模型后 retry 并重新启动；AUTH_FAILED：检查供应商权限；OUTPUT_INCOMPLETE：检查输出预算或缩小任务；INVALID_RESULT / CITATION_MISMATCH：核对 Schema 与实际锚点；EDIT_CONFLICT / INPUT_STALE：刷新人工基线或资料，再 retry。真实兼容性按所选网关单独验收，离线替身测试不代表供应商可用。

## 索引、PDF 与迁移恢复

FTS5 trigram 用于长词召回排序，子串核验保证确定性匹配；一到两个字符使用子串检索。无 tokenizer 时明确降级。索引准备和 PDF 解析在业务写锁外进行，输入变化或建索引失败保留原活动代。未索引资料仍可用确定性检索发现，但加工需先建立实际证据片段。

历史 PDF 重解析内容与旧文本不一致时，索引 issues 显示 EXTRACTION_CHANGED。显式执行 `knowledge index refresh --id RECORD_ID` 创建新的 collected 提取版本和复核请求；原记录、原片段与历史审核保留。新版本复核后再重建索引。OCR、Office 专门解析尚不在本轮。

第一次显式写入会把 schema 0/2/3/4 升级到 5，执行前把原库与证据备份到数据目录相邻的 `数据目录名-migration-随机ID`。迁移事务失败完整回滚；只读查询不会升级。旧库不会被自动加工或批准。

备份覆盖新增版本、审核、任务、发布记录、commands、processing_blobs 和 processing_receipts；公开前勿把运行库、任务包或密钥加入源码。回退旧程序须在新数据目录恢复升级前备份，旧程序会拒写 v5，不直接降级运行库。

Wiki 导出附专题与版本身份、核验状态、适用条件、引用和缺口；原件留在证据库。没有自动 Markdown 双向同步，也不提供完整历史法律效力裁决。
