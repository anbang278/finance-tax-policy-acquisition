# 测试与验收计划

## 自动测试

在仓库根目录运行 `uv run --extra dev --extra web pytest -q`、`uv run --extra dev --extra web ruff check src tests scripts`、`uv run --extra dev --extra web mypy --ignore-missing-imports src`、`uv build` 和 `python scripts/verify_wheel.py`。安装包从非源码目录读取配置和静态资源。真实来源测试与离线测试分开记录。

| 验收 | 预期 |
|---|---|
| 来源列表与详情 | 财政部、税务总局各有真实样本，第一页和第二页字段可核验 |
| 时间范围缺失 | Skill 先反问；CLI 缺任一起止日期时拒绝创建任务，不自动补默认期限 |
| 日期 | 财政部栏目日期、税务成文日期及正文日期分离 |
| 内容分类 | 原文、解读、发布消息保留，不允许错误页进入正式检索 |
| 附件 | PDF 有文本或扫描状态；Word、Excel 原件保存且标注未解析 |
| 恢复 | 分页中断、资料失败和重复调用不会假报完成 |
| 决策 | 陈旧摘要、错误证据、重复不同结果被拒绝 |
| 版本 | 相同 URL 原文修订新增版本，旧证据不被覆盖 |
| 修复 | 越界补丁拒绝，缺隔离环境时候选不可测试和发布 |
| 研究 | 每项引用与已验证版本及原文摘录匹配 |
| 双宿主 | Codex 与 Hermes 分别确认加载与调用，不以静态清单代替运行验收 |

## 分级报告

`PASS` 表示该项实测成功，`PARTIAL` 表示功能只覆盖部分范围，`BLOCKED` 表示环境或本期边界阻断。离线模拟、真实站点、模型语义、宿主加载、正式隔离分别记录。原交接文档 A05 改为“错误内容或错误分类不得通过”；A11、A14—A20 中涉及隔离执行和正式发布的部分延期，保留编号。

## 本机政策工作台验收（2026-09-30）

在仓库根目录运行 `FTR_WEB_BROWSER=1 uv run --extra dev --extra web pytest -q`、`uv run --extra dev --extra web ruff check src tests scripts`、`uv run --extra dev --extra web mypy --ignore-missing-imports src`、`uv build` 和 `python scripts/verify_wheel.py`。新增代码和改动的 CLI 另做 `ruff format --check src/ftr/web src/ftr/cli.py tests/test_web.py`。浏览器需要已安装 Chromium；普通测试不设置 `FTR_WEB_BROWSER` 时跳过浏览器用例。

| 验收 | 预期 |
|---|---|
| 实际数据 | 首页最新资料与各质量状态计数等于只读 SQL 结果，不使用 Demo 内嵌数据 |
| 默认准入 | Web 默认展示全部质量；CLI 和研究检索仍仅返回已复核最新资料 |
| 筛选与版本 | 中文标题、文号、正文及组合筛选正确；最新版本先选取后筛选，历史正文可单独阅读 |
| 队列口径 | 五种已有条目状态单列；规范化 URL 可关联资料，复用资料不增加本任务版本数 |
| 内容与下载 | 不执行正文 HTML；保存与解析状态分开，原件哈希、路径边界和下载附件头正确 |
| 错误区分 | 缺库与空库、坏库、锁冲突、不存在记录、证据缺失和损坏均可识别 |
| 并发只读 | 采集锁持有时查询可用；Web 请求不改数据库，后续查询能读取新提交的状态 |
| 浏览器链路 | 搜索→阅读→版本→附件→关联任务→失败队列→事件与失败可操作 |
| 刷新 | 5 秒刷新不丢筛选或阅读位置；断连保留旧内容，恢复可查询；隐藏页面不轮询 |
| 窄屏与安装 | 390px 宽度可进入详情并返回列表；独立 wheel 从源码目录之外启动，资源/API 可用 |

真实政策数据只用于只读核验；运行中任务、状态更新、正文注入和失败场景均使用临时数据库，不额外启动真实采集。


## 跨环境配置回归（2026-09-30）

迁移前在工程子目录运行 `uv sync --frozen --extra dev --extra web`、完整 pytest、ruff、mypy 与 `uv build`，并使用 `python ../scripts/verify_wheel.py` 验证独立安装。仓库重排后的命令统一从根目录执行，见 [REGRESSION](../../tests/REGRESSION.md)。浏览器运行设置 FTR_WEB_BROWSER=1；PowerShell 用 `$env:FTR_WEB_BROWSER='1'`。CI 使用 Python 3.13 的 macOS、Windows、Ubuntu 三平台矩阵。

新增覆盖：逐层覆盖与相对路径、未知/重复键、无效输入无目录写入、凭据脱敏、预算实际生效与旧任务冻结；跨进程非阻塞锁与异常退出释放；旧 Windows 清单迁移、特殊路径 URI、外平台盘符/UNC 越界；直连/代理一致性与映射网段例外；浏览器启动与导航参数、财政部不启动浏览器、Linux 无显示失败；续跑配置审计；局域网 Host、ui-config 参数白名单与只读。

人工独立验收：Windows 原生全链路、Linux Xvfb + 税务实采、另一台电脑访问局域网工作台、Codex/Hermes 实际模型会话。未运行保持 PARTIAL/BLOCKED，不由 mock、CI 配置文件或本机浏览器替代。

## 伙伴开箱使用与受限规则闭环（2026-10-02）

本轮在原两来源范围内新增 setup、workbench Skill；用户级环境引导返回绝对运行路径，按来源/查询能力安装依赖，不触发采集。只读工作台新增启动、复用、身份验证停止及自动打开浏览器接口。

受限规则自修复使用固定执行器与可信样本，支持列表/正文定位、预定义日期、分页和 JSON 字段映射；失败原件绑定候选及验证报告，最多两轮候选。有界真实来源验证通过后自动本机启用并续跑，失败及中断回退；不执行未知 Python，不更改资料复核门槛，governed 仍未开放。详见 ../guides/self-repair.md 与当前架构。

追踪入口：setup → scripts/setup.sh、setup.ps1、setup_runtime.py → tests/test_setup.py；规则闭环 → rules.py、rule_repair.py、runtime.py → tests/test_rule_repair.py；工作台 → workbench.py → tests/test_workbench_lifecycle.py。既有回归、wheel 与三平台 CI 继续执行。

验收须分开记录离线故障注入、真实源站及 macOS/Windows × Codex/Hermes 会话。受控网络替身不代表真实访问成功，缺宿主/设备不能计通过。当前实现结果以 acceptance-report.md 本轮补充为准。

## 首次配置与运行可靠性（2026-10-02）

- 自动配置：已有/缺失浏览器、候选启动失败回退、显式失效不替换、配置保护、Web 依赖保留、中文空格路径、污染环境隔离、下载失败分阶段诊断与运行参数透传。
- 服务：真实启动/复用/停止、包装 PID、旧状态、慢启动与总超时、错误身份拒绝、进程退出不轮试十端口；数据字节不变与日志脱敏。
- 网络与采集：两客户端 502/503/504 有限重试、403/429 不重试、单条失败继续与连续失败停止、成功重置、列表检查点、预算与暂停取消中止等待、Repair 实际请求次数、续跑幂等。
- 恢复及质量：周期 stderr 与单个 stdout JSON、信号及强制退出残留、只读活动观察、独占锁后恢复、旧备份、隔离多原因与旧未知原因、PDF 警告证据及质量限制。
- 分发：完整浏览器回归、ruff/mypy、sdist/wheel、源码目录外独立安装；CI 包含 Windows setup/run PowerShell 语法和运行入口测试。

真实源站、干净 Windows/macOS 安装和 Codex/Hermes 模型调用需分别记录；无设备或未获当前采集授权时为待验收，不以 mock/旧 CI 代替。
