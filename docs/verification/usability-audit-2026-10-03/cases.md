# 用例与结果

日期：2026-10-03，代码基线 `8c31e1a`。本文件的 PASS 仅适用于对应证据层；自动测试/替身、真实源站、真实页面与自然语言模型对话不能相互替代。总判断见[主报告](../usability-audit-2026-10-03.md)。

## 工程与环境

| ID | 场景 | 结果 | 证据与边界 |
|---|---|---|---|
| ENG-01 | 根目录冻结依赖安装 | PASS | logs/sync.log；`uv sync --frozen --extra dev --extra web` |
| ENG-02 | 全套回归含 Chromium | PASS | logs/pytest.log、pytest.xml；158 passed / 1 skipped，32.38s |
| ENG-03 | ruff 静态检查 | PASS | logs/ruff.log；src/tests/scripts 全部通过 |
| ENG-04 | mypy | PASS（指定口径） | logs/mypy.log；24 个文件，忽略第三方缺失声明，未类型标注函数体默认不检查 |
| ENG-05 | sdist/wheel 构建 | PASS | logs/build.log |
| ENG-06 | 脱离源码的独立 wheel 安装 | PASS | logs/wheel.log；独立 venv、包内配置/规则/静态资源、API及工作台生命周期 |
| ENG-07 | shell 语法及 Git diff | PASS | `sh -n scripts/setup.sh scripts/run.sh`、`git diff --check` 均退出 0 |
| ENV-01 | setup all 复用已有环境 | PASS | logs/setup-all.json/stderr；ENVIRONMENT_READY、实际 Chromium 启动、未创建业务库 |
| ENV-02 | 缺 uv/浏览器、安装失败、显式错误路径 | PASS（隔离） | test_setup 8 项本机通过；安装下载部分用替身 |
| ENV-03 | 干净 macOS/Windows 首次完整安装 | PARTIAL | 无干净设备；Windows 原生入口测试跳过，本轮未运行远程 CI |

既有回归计数：config 18、core 16、portability 17、proxy_and_status 14、reliability 33、rule_repair 24、setup 8、web 26、workbench_lifecycle 2；合计 158，通过项均实际执行。setup 另有 1 项 Windows PowerShell 用例跳过，不计通过。

## 真实采集

| ID | 场景 | 结果 | 证据与边界 |
|---|---|---|---|
| LIVE-01 | 财政部，2026-09-21—09-30 | PARTIAL | collect-mof.json：3 页、1 份资料，页预算停止；真实正文与原件成功 |
| LIVE-02 | 税务总局，同区间 | PARTIAL | collect-chinatax.json：3 页、1 份资料、1 个 XLS 主附件；内容未解析，页预算停止 |
| LIVE-03 | 两来源合并，同区间 | PARTIAL | collect-combined.json：6 页、2 份资料；两来源均未枚举结束 |
| LIVE-04 | 已读官方列表与保存资料核对 | PASS（已读范围） | independent-list-inventory.json；独立读取原件，各有 1 条区间内资料，均已保存 |
| LIVE-05 | 未读列表与完整区间覆盖 | PARTIAL | 未自动续跑；剩余页是否存在相关条目及数量未知，不宣称无漏项 |
| LIVE-06 | 财政部栏目日期、税务成文日期 | PASS（样本） | 两份真实 DocumentRecord；缺成文/发布字段未被猜补 |
| LIVE-07 | 直接附件原件与内容区分 | PASS / PARTIAL | XLS 已下载及哈希通过；格式不支持，正文和表格内容明确区分 |

三次请求都通过原生 runner 在当前 Codex 会话中执行。所用绝对配置/目录由验收者预先隔离，不代表小白能自行配置，也不代表插件自动加载已验收。真实失败场景未发生，不用替身结果宣称真实网络恢复成功。

## 失败、恢复、范围及质量

| ID | 场景 | 结果 | 证据 |
|---|---|---|---|
| ERR-01 | HTTP 502/503/504、连接/超时重试 | PASS（受控） | test_reliability 的两客户端重试及成功恢复用例 |
| ERR-02 | HTTP 403/429 停止、不无限重试 | PASS（受控） | 两客户端限制状态测试、税务浏览器拒绝后不发列表请求 |
| ERR-03 | 详情失败后继续其他条目、连续三条停止 | PASS（受控） | test_transient_item_streak_continue_and_resume |
| ERR-04 | 正文失败恢复、版本幂等 | PASS（受控） | 同上；再次续跑不重复建立资料 |
| ERR-05 | 列表失败检查点保持 | PASS（受控） | test_listing_failure_keeps_same_checkpoint |
| ERR-06 | 两来源中税务浏览器失败、财政部成功保留 | PASS（受控） | mixed-source-results.json；mof 保存 1 份，chinatax 保存 0，总体 PARTIAL |
| ERR-07 | 结构异常定位、规则门禁、启用、回退 | PASS（受控） | test_rule_repair 24 项；真实结构故障本轮未发生 |
| ERR-08 | 普通附件下载失败→网络恢复→新实例续跑 | FAIL | probe-results.json，ATTACHMENT_RECOVERY；D-01 |
| ERR-09 | 列表 429 的原因持久化 | FAIL | FAILURE_REASON_PERSISTENCE；D-03 |
| ERR-10 | 页面中文失败说明与下一步 | FAIL | browser/failure-result.json；无 429/限流/恢复步骤，展示英文与 JSON；D-03 |
| ERR-11 | 主附件不支持/扫描/警告限制 | PASS（受控及样本） | 真实 XLS 限制；PDF 警告与提取限制的回归，不冒充本轮扫描件实测 |
| ERR-12 | 页/资料/时间/字节预算 | PASS（协议） / FAIL（区间效率） | 既有配置回归，真实页预算正确停止；范围发现不收束，D-02 |
| ERR-13 | 中断、硬退出残留、暂停 | PASS（隔离） | 实际 POSIX SIGTERM 子进程、硬退出、暂停阻止重试等回归 |
| ERR-14 | 写锁冲突 | PASS（本机隔离） | 跨进程非阻塞回归及 cli-results.json；退出 5，不删除锁 |
| ERR-15 | 取消后 resume | PASS（本机隔离） | cli-results.json；STOP_REQUESTED 后 resume 返回 CANCELLED，未发网络请求 |
| ERR-16 | 缺日期、倒序、非法日期、非法来源代码 | PASS（输入保护） | 四项 CLI 检查退出 2，均没有创建数据目录；argparse 错误为文本，非 JSON |
| ERR-17 | JSON 业务范围字段 sources 错写 | FAIL | request-field-probe.json；默认扩大成两来源，未执行真实采集；D-04 |
| QA-01 | 正确摘要/证据的 PASS 映射 | PASS（隔离） | DECISION_PASS→validated |
| QA-02 | 正确摘要/证据的 REJECT 映射 | FAIL | DECISION_REJECT→quarantined；D-05 |
| QA-03 | 未验证资料不进入正式检索/研究 | PASS | 真实两份资料的 search、research prepare 均为空；既有引用门禁回归通过 |
| QA-04 | 真实模型语义复核与引用研究 | PARTIAL | 未提交真实资料复核；无独立研究会话，不计通过 |
| QA-05 | 财政部第二/三页原件来源定位 | FAIL | mof-listing-evidence-metadata.json；URL 都为首页；D-08 |

## 工作台与读取

| ID | 场景 | 结果 | 证据 |
|---|---|---|---|
| UI-01 | 启动并自动打开浏览器 | PASS | workbench-start.json，browser_opened=true，回环 8875 |
| UI-02 | 同资料目录再次启动复用 | PASS | workbench-reuse.json，同 PID 与 URL |
| UI-03 | 缺资料库 | PASS | workbench-empty.json；中文提示先明确范围采集，没有初始化资料库 |
| UI-04 | 两份真实资料、中文关键词与来源筛选 | PASS | browser/result.json、01-library.png |
| UI-05 | 正文、限制、版本、证据 | PASS | result.json、02-tax-body.png |
| UI-06 | 下载 XLS 原件 | PASS | 03-tax-attachment.png；下载哈希与证据一致，94,720 字节 |
| UI-07 | 关联任务、未枚举提示、预算原因、事件 | PASS（展示） | 04-task-budget.png、task-events-text.txt；是否易懂另见 D-03 |
| UI-08 | 390px 窄屏返回 | PASS | 05-mobile-tasks.png |
| UI-09 | 真实 Web 只读、无页面/控制台错误 | PASS | result.json；查询前后 DB 哈希一致，两类错误均为空 |
| UI-10 | 故障页面技术渲染、只读 | PASS | failure-result.json；没有脚本错误、DB 不变；恢复说明 FAIL |
| UI-11 | 原件篡改/越界、断连恢复、轮询保位 | PASS（受控） | test_web 26 项回归，含浏览器用例 |
| UI-12 | 实例身份停止、端口冲突、启动失败分类 | PASS（本机/受控） | test_workbench_lifecycle、test_reliability；两实例均停止及状态复查 |
| READ-01 | task status 只读 | PASS | 既有只读回归、cli-results.json |
| READ-02 | CLI search 对残留 RUNNING 的只读性 | FAIL | read-probe-results.json，状态及数据库哈希改变；D-07 |

## 自然语言用例：验收缺口与后续评测输入

**下表是专家走查与评测规格，不是已执行的独立模型对话。所有场景的真实对话验收均为 PARTIAL。** 不用本轮 Agent 对这些语句的理解，替代被测宿主在新会话中的行为证据；因此不计算容错成功率、澄清轮次或人工技术操作次数。

评测日期固定为 2026-10-03、时区 Asia/Taipei。除特别注明外，时间范围用“2026-09-21 至 2026-09-30”。用户不得被要求理解 mof/chinatax、JSON、任务 ID 或复制 CLI。

| ID | 用户表达或上下文 | 期望行为 | 走查发现 |
|---|---|---|---|
| NL-01 | “采集财政部和国家税务总局这十天的政策”并给出日期 | 两个已登记来源；执行前用中文说明日期口径 | 主控支持两来源和显式日期；本轮仅受指导流程实测 |
| NL-02 | “帮我取税总这段时间的政策” | 明确简称映射到国家税务总局，不要求用户提供代码 | 没有独立别称容错用例或执行证据 |
| NL-03 | “采集财正部这段时间政策” | 高置信纠错为财政部，简短告知所识别来源 | 未规定错别字确认边界；不能据此认定已误识别 |
| NL-04 | “税务局这段时间有哪些政策” | 地方/中央存在歧义时仅追问机构，不能擅自选全国全集 | 主控只允许两来源，但澄清路径尚未实测 |
| NL-05 | “采集两部门上个月的政策” | 明示 2026-09-01—09-30，无额外日期追问 | Skill 已明确相对月转换 |
| NL-06 | “最近30天的两部门政策” | 明示含今日的 30 个自然日为 2026-09-04—10-03 | Skill 支持该表达，但含首尾/时区约定没有真实对话评测 |
| NL-07 | “采集最近的政策” | 只追问确定的时间范围，收到答复前不采集 | 主控明确禁止默认过去一年 |
| NL-08 | “从去年开始采集” | 起点可解释但终点未明确，追问终点 | 主控禁止只有单侧边界时采集 |
| NL-09 | “去年至今日” | 明示 2025-01-01—2026-10-03 后执行 | 可换算表达原则已写明，未单独评测 |
| NL-10 | “9月31日到今天”或开始晚于结束 | 日期错误由 Agent 用中文纠正，不执行无效采集 | CLI 保护通过，不证明对话层解释易懂 |
| NL-11 | “采集上海地方政策” | 说明当前不覆盖地方来源，保留原目标，不替换为两中央来源 | 来源边界清楚，替代建议未实测 |
| NL-12 | “全国全部财税政策给我” | 说明两个栏目的实际覆盖，不能承诺全国全集 | README 有边界，模型是否继承未验收 |
| NL-13 | 首批 PARTIAL 后说“继续” | 关联刚才任务/原数据目录/原范围，仅执行下一批 | CLI 续跑回归通过；Controller/Repair 规则冲突 D-06；真实首批后未收到继续指令 |
| NL-14 | 有多个任务时只说“继续” | 可由明确最近任务消歧；无法消歧则用中文列候选，不让用户猜 ID | 尚无独立任务关联评测或明确规则 |
| NL-15 | “还差什么、为什么没采到、怎样能成功” | 分来源列具体失败项、未知发现范围、原因证据和行动 | 附件续跑与原因持久化失败，D-01/D-03；固定五项输出契约未建立 |
| NL-16 | “打开看看” | 打开同资料目录健康工作台，给可用地址；不触发新采集 | workbench Skill 指令与本轮页面实测支持，口语路由未单独验收 |
| NL-17 | 浏览器/下载依赖失败 | Agent 自动处理授权范围内依赖；真正需用户操作时给具体步骤与原因 | setup 隔离回归通过；新设备及真实用户交互未验收 |

后续自然语言执行应保留逐条输入、Agent 回复、工具调用、实际 TaskRequest、停止原因和结果说明，按五项核心标准评判。别称可识别不等于允许猜测业务范围；无失败不等于全量采集完成。
