# 本机受限规则自修复

当前自动修复使用声明式规则，执行器是随包安装的固定代码。宿主模型负责分析原件和生成规则，CLI 负责验证、启用、续跑与回退。纯 CLI 可以提交规则，但不具备模型自动生成能力。未知 Python 补丁仍止于静态登记，正式 governed 模式仍未开放。

## 可修复范围

规则 schema 可通过 `ftr schema` 的 `extraction_rules` 查看。支持 HTML 链接/标题/正文定位、预定义日期格式、财政部总页数字段和页文件名，以及税务列表 JSON 路径与字段映射。

选择器采用受限 XPath：`//a[@href]`、`//h2`、`//*[@id='zoom']`、`//div[contains(@class,'newBody')]` 等；不支持任意 XPath 函数、脚本或正则表达式。多个选择器依次匹配，可保留旧结构后备。税务 JSON 和财政部总页数保留内置字段后备。正文的资料类型、文号、效力标记及附件识别仍由可信代码执行。

限制固定在代码：规则文件 32KB、输入 2MB/20,000 HTML 节点、每类至多 8 个选择器、匹配至多 2,000 节点、JSON 列表至多 100 项、分页至多 10,000 页且只能顺序前进。运行预算仍受任务与 RuntimeSettings 约束。规则不允许更改主机、Cookie、访问方式、网络请求、资料状态、依赖、门禁或可信样本。

可自动处理有原件证据的 `STRUCTURE_DRIFT`。访问限制、验证码、登录、限流、UNKNOWN、无样本或语义疑问交人工处理；正常空列表不自动判为可修复故障。网络传输异常最多重试两次，访问限制不重试绕过。

## 命令闭环

以下 ID、文件路径须替换为实际值；源码运行可用环境引导返回的绝对 Python 加 `-m ftr.cli --config CONFIG`。

```sh
ftr repair context --failure FAILURE_ID
ftr schema
ftr repair propose-rule --failure FAILURE_ID --file rules.json
ftr repair test-rule --candidate CANDIDATE_ID
# 需当前任务真实采集授权；访问真实源站，临时数据库不写正式资料
ftr repair test-rule --candidate CANDIDATE_ID --live
# 需真实来源验证通过；自动启用并在原任务执行首次有界续跑
ftr repair activate-rule --candidate CANDIDATE_ID
ftr task resume --task TASK_ID
ftr repair rollback-rule --source mof
```

规则 JSON 是完整规则对象，不是代码补丁；最小示例：

```json
{"schema_version":"1","source_id":"mof","total_variable":"newTotal","bodies":["//div[contains(@class,'newBody')]","//div[contains(@class,'TRS_Editor')]"]}
```

只有真实故障证据支持相应变化时才提交。每任务每来源最多两轮候选，预算耗尽返回失败与人工处理说明。离线测试校验 schema、包内可信回归样本及故障原件；固定样本源自既有解析回归并补充契约场景，不冒充真实历史原件；真实验证限定原日期范围、故障检查点、一页、两份详情及最多 120 秒检查点预算。主附件未解析或缺少范围内资料时验证失败，不可启用。

## 状态与恢复

| 状态 | 含义 |
|---|---|
| RULE_PROPOSED | 登记了规则制品，尚未验证 |
| RULE_OFFLINE_VALIDATED | 历史与故障样本通过，尚无真实来源验证 |
| RULE_VERIFIED | 有界真实来源与内容契约通过 |
| RULE_ACTIVE | 本机已启用，首次有界续跑成功；原任务仍可能 PARTIAL |
| RULE_ROLLED_BACK | 已恢复上一规则，停止当前修复循环 |

启用器重新检查原件、可信样本、制品哈希、SQLite 审计中的验证报告与当前活动版本；报告同时绑定执行器、来源契约、可信样本和运行配置哈希，有效期 15 分钟。配置或程序更新后须重新验证。制品或报告被修改、版本并发变化、任务暂停/取消均拒绝启用。

活动版本位于数据目录 `rules/<source>.json`，候选、报告和历史位于 `rules/candidates/`；原子替换后记录 probation，首次续跑最多一页、两份详情。失败、内容受限或无实际正文处理时回退。异常退出后，下一次 collect/task resume/修复写入操作先恢复未完成启用的上一版本；status 查询不触发恢复写入。正式资料和原件保留，资料仍需语义复核。

`backup create/verify/restore` 包含规则文件与数据库审计，规则文件受备份哈希校验。旧备份没有 rules 目录时使用内置规则。单一数据目录的 CLI 锁保护规则登记、验证、启用与回退，不删除锁来解冲突。

本机规则验证不证明全国资料全集、长期源站可用性、候选代码隔离或模型理解质量。税务浏览器准入变化、可信执行器缺能力以及规则语言无法表达的变化，仍需要普通授权开发。
