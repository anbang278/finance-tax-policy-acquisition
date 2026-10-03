# 本地研发验收记录

核验日期：2026-09-28。状态是当前环境和当前工程的实测结果；源站结构和访问策略可能变化。本报告不代表正式发布批准。

| 验收面 | 结果 | 可复核证据与边界 |
|---|---|---|
| 离线代码 | PASS | `uv run pytest -q`：16 项通过；`ruff check`、`mypy` 通过。覆盖两来源列表解析、浏览器拒绝后不发请求、失败条目重试、税务正文与附件定位、会话 Cookie 主机限制、日期及类型、缺失期限拒绝、样式过滤、幂等/版本、决策摘要、补丁保护、网络主机、暂停信号、备份恢复。其余计划场景仍需增加测试。 |
| 构建与资源 | PASS | `uv build` 成功；wheel 内含 `ftr/data/sources.yaml` 和采集契约；独立虚拟环境安装 wheel 后 `ftr doctor` 返回 `LOCAL_DEMO_READY`。该检查不包含两个宿主加载。 |
| Codex 插件 | PARTIAL | `.codex-plugin/plugin.json` 通过本地清单验证；尚未在目标 Codex 会话中安装并加载五个 Skill。 |
| Hermes 插件 | PARTIAL | `hermes plugins validate . --json` 无警告，`doctor . --ci` 通过。隔离的 `HERMES_HOME` 中启用插件后，`skills_list` 返回五个命名空间 Skill，`skill_view` 可读取。尚未验证真实模型会话中的 `ftr` 命令调用；用户现有 Hermes 配置未改动。 |
| 财政部真实来源 | PARTIAL | 对[财政部政策发布](https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/)执行单页、单详情有界采集，任务 `8b6882b36e0c4f72b63e7267458f5287`：1 页、1 份资料、1 个待语义复核项；因预算返回 `PARTIAL`。样本《紧急采购管理暂行办法》通知的完整办法正文为 3904 字符，类型 `policy_file`，栏目日期为 2026-09-14，成文与发布日期未被猜测。第二页、附件多样本及全年覆盖尚未验收。 |
| 税务总局真实来源 | PARTIAL | [政策法规库入口](https://fgk.chinatax.gov.cn/zcfgk/c100006/listflfg.html)在本机有界面 Playwright 返回列表 HTTP 200；动态读取页面请求的 `channelId` 与会话 Cookie 后，`requests` 列表、详情及附件请求返回 200。任务 `c739fea75bdf4c2eaa32186e410313d5` 有界保存第一页、1 份公告正文和 2 个原件附件，因预算返回 `PARTIAL`；主附件未解析使资料隔离。独立实测第二页 10 条，断点续跑在任务 `bf1c1dbc36a143379d43f2f5f5e1e1aa` 中保存第二份资料。无界面 Playwright 仍返回 403；全年覆盖未验证。 |
| 模型语义与研究 | PARTIAL | 决策请求、摘要与证据校验、检索和引用接口已实现；真实模型回调与陈述支持性评估尚未完成宿主级验收。没有把待复核真实样本自动记为通过。 |
| 候选修复 | PARTIAL | 越界补丁拒绝；候选静态登记可用。`repair test` 固定返回 `BLOCKED_NO_ISOLATION` 语义，未知补丁不执行，审查包标注未执行回归。 |
| 正式治理 | BLOCKED | `doctor --mode governed` 返回 `GOVERNED_NOT_IMPLEMENTED`；隔离执行、可信测试、独立签名身份、发布及回退尚未实施。不得标记 `MVP_ACCEPTED`。 |
| 备份恢复 | PASS（本地范围） | `backup create/verify/restore` 在空库命令链路通过；带证据文件的离线测试验证哈希与篡改拒绝。大库和故障恢复仍需实测。 |

当前为**可复查的本地研发演示**，但未满足计划中的 `LOCAL_DEMO_ACCEPTED`：Codex 宿主实际加载、两来源的完整范围覆盖和完整研究案例尚未通过。不得将有界样本、清单有效或网页人工可见折算为这些验收项通过。

下一步应分别在 Codex 与 Hermes 执行五个 Skill 的同协议调用，补测全年覆盖、复杂中断和引用研究案例；正式 Level 4 另设里程碑。

## 本机政策工作台验收补充（2026-09-30）

本次仅验收新增的本机查询与观察服务，不改变上文关于宿主加载、政策覆盖范围、模型复核和正式治理的状态。

| 验收面 | 结果 | 实测证据与边界 |
|---|---|---|
| 真实数据接入 | PASS | `/api/overview` 返回 60 份最新资料：33 份 `collected`、27 份 `quarantined`；3 个任务均为 `PARTIAL`。页面显示一致，没有将待复核资料显示为已复核。 |
| 自动测试 | PASS | 包含浏览器的完整命令 `FTR_WEB_BROWSER=1 uv run --extra dev --extra web pytest -q`：56 项通过，其中既有核心 30 项，新增 Web 26 项。普通运行为 55 通过、1 浏览器用例跳过。测试位于 `tests/test_web.py`。 |
| 查询与版本 | PASS | 标题/文号/正文、组合筛选、空结果、分页、缺失日期置后、最新版本先筛选、历史正文读取通过；Web 全量浏览不改变研究检索准入。 |
| 队列与事件 | PASS | 五种队列状态、检查点、失败详情、事件分页和规范化 URL 关联通过。隔离数据验证已保存 1 条但新增版本 0 个；未虚构心跳、当前项或全量百分比。 |
| 只读与并发 | PASS | `mode=ro` 写入被 SQLite 拒绝；采集锁持有时查询可用；隔离数据库新提交的任务状态可被下一次查询读取。真实数据库在页面/API/安装包读取验收前后的 SHA-256 一致。 |
| 原件下载 | PASS | Word 附件未解析状态与原件下载通过；下载为 `attachment` 和 `application/octet-stream`。缺失、篡改、路径穿越及越界软链接均显式拒绝。 |
| 浏览器交互 | PASS | Chromium 完成搜索、阅读、历史版本、附件下载、关联任务、失败与事件、来源面板；验证轮询保位、断连恢复、后台停止轮询及 390px 窄屏返回列表。隔离正文 `<script>` 作为文本显示，未执行。真实页面与独立 wheel 浏览器检查无脚本或控制台错误。 |
| 静态检查 | PASS（指定口径） | `ruff check src tests` 通过；`mypy --ignore-missing-imports src` 通过，覆盖 18 个源文件。该类型检查忽略第三方缺失声明，现有 `lxml` 未安装 stubs。 |
| 格式检查 | PASS / PARTIAL | 新增 Web、测试和改动的 CLI 格式检查通过。全库格式检查仍有 2 个既有问题，位于 `network.py` 和 `test_proxy_and_status.py`；通过 `git show HEAD` 核对为本次修改前已存在，未扩大本次改动。 |
| 安装包 | PASS | `uv build` 成功，wheel 包含 HTML/CSS/JS/图标与 Web 查询代码。独立虚拟环境安装本地 wheel 的 `[web]` extra 后，从 `/tmp` 启动，页面、资源和各主要 API 返回 200，并可读取真实资料库。 |

历史记录（迁移前）：在 `finance-tax-research/` 执行 `uv run --extra web ftr serve --port 8765`，访问 <http://127.0.0.1:8765>。数据库沿用 `FTR_DATA_DIR`，默认 `~/.local/share/ftr`；端口占用报错，缺库不初始化。服务未提交或推送到远端。

## 跨环境配置与整套说明验收（2026-09-30）

本轮基线为原有 55 通过、1 浏览器用例跳过。以下是配置和兼容性改造后的实际结果；上文旧计数和源站样本保留为历史证据。

| 验收面 | 结果 | 实际证据与边界 |
|---|---|---|
| 配置与旧入口 | PASS（本机） | RuntimeSettings 及 CLI/env/YAML/default 覆盖、路径基准、逐层校验、未知/重复键、无效类型、非法范围、凭据脱敏测试通过；FTR_DATA_DIR、旧命令与旧默认目录保留 |
| 本地检查 | PASS（本机） | config validate/show 只读；doctor 分项报告依赖、Python、浏览器文件、显示变量；缺浏览器不初始化数据，并明确未验证启动及源站 |
| 采集参数与续跑 | PASS（隔离测试） | 代理每次启动只解析一次；网络限额、浏览器参数、运行预算生效；原任务预算冻结，续跑审计记录脱敏设置；财政部不启动 Playwright，缺显示环境税务返回 PARTIAL |
| 锁与数据迁移 | PASS（本机） | 跨进程非阻塞竞争、os._exit 后锁释放；新 POSIX 清单与模拟旧 Windows 清单校验及恢复；特殊路径 URI、盘符/UNC/越界拒绝、恢复目标已存在拒绝 |
| 工作台配置 | PASS（本机） | ui-config 仅返回两个页面参数及查询时间；浏览器实际使用 1500ms 轮询、2000ms 请求计时；旧阅读/下载/刷新/断连/窄屏回归通过 |
| 实际监听 | PASS（本机范围） | 临时数据库上启动 0.0.0.0 与 ::1，HTTP 查询配置和概况成功；IPv4 接受局域网 Host；服务停止，数据库前后 SHA-256 一致；没有另一台电脑参与 |
| 全套自动测试 | PASS | `FTR_WEB_BROWSER=1 uv run --frozen --no-sync --extra dev --extra web pytest -q`：91 项通过，包含真实 Chromium 工作台交互；正常运行 90 项通过、1 浏览器用例跳过 |
| 静态检查与构建 | PASS | ruff check 源码、测试和验证脚本通过；mypy 指定忽略第三方缺失声明的口径覆盖 19 个源文件通过；uv build 生成 sdist/wheel |
| 独立安装 | PASS（macOS） | 迁移前从工程子目录执行 `python3 ../scripts/verify_wheel.py`，以临时独立 venv 从源码目录之外验证 CLI、wheel 内配置模板、静态资源和主要只读 API；使用隔离数据库，无真实采集 |
| 项目文档 | PASS | 总入口、安装/配置/使用/运维/开发手册、Agent 契约、REGRESSION 与环境记录补齐；研发四件套、架构和追踪矩阵同步；废弃 DNS 口径修正 |
| 跨平台与跨电脑 | PARTIAL | Python 3.13 的 macOS/Windows/Linux CI 文件已形成，未远程运行；Windows/Linux 真机、Linux Xvfb 源站实采及另一台电脑的局域网访问尚未验证 |

本轮没有变更数据库 schema，也没有提交、推送、常驻部署、宿主设置修改或资料状态人工代判。原始交接文件保留原文，原有未提交工作台内容继续保留。默认回环监听，显式局域网模式按确认范围不增加认证。

补充口径：资料数预算按新增资料版本计数；字节预算统计每来源执行批次的已保存正文和附件，列表原件不计入该现有计数；时间预算在工作检查点评估，不是强制杀进程时钟。Windows 路径测试只使用该 OS 合法文件名，不能把 Unix 的 `?` 文件名要求照搬到 Windows。Windows 身份无符号链接权限时该负向用例明确跳过。

## 仓库结构迁移验收（2026-10-01）

本次将 Python 工程、五个 Skill 和两个宿主清单统一至 Git 仓库根目录，并按用户、开发者、Agent 的阅读顺序重整入口与当前文档。原始交接材料移入 `docs/history/` 并保留正文；旧工程子目录内的本地虚拟环境、采集数据、缓存和独立 Git 历史未纳入新目录结构，保留在当前工作区。

| 验收面 | 结果 | 实际证据与边界 |
|---|---|---|
| 根目录安装 | PASS（本机） | `uv sync --frozen --extra dev --extra web` 使用根目录 `pyproject.toml` 与锁文件成功 |
| 自动测试 | PASS（本机） | `FTR_WEB_BROWSER=1 uv run --frozen --no-sync --extra dev --extra web pytest -q`：91 项通过，含 Chromium 工作台交互 |
| 静态检查 | PASS（本机） | `ruff check src tests scripts` 与 `mypy --ignore-missing-imports src` 通过，覆盖 19 个源文件 |
| 构建与独立安装 | PASS（macOS） | 根目录 `uv build` 生成 sdist/wheel；`python3 scripts/verify_wheel.py` 从源码目录外安装 wheel，并验证模板、静态资源及只读 API |
| 文档导航 | PASS（本机） | 21 份当前 Markdown 的相对链接与 frontmatter `depends_on` 路径通过检查；当前文档不再使用 Obsidian 双链 |
| 宿主入口 | PASS（静态） | 两个 JSON 清单可解析，Codex Skill 目录存在且包含五份 `SKILL.md`；未执行宿主实际加载或模型会话 |
| 文件迁移完整性 | PASS | 对照迁移前项目外快照核验 36 个代码、测试及插件文件字节一致；历史交接原文保持一致 |
| 跨平台与远程 CI | PASS | 2026-10-01 GitHub Actions 三平台工作流 [36875496920](https://github.com/anbang278/finance-tax-policy-acquisition/actions/runs/36875496920)：macOS、Windows、Ubuntu 的锁定依赖与 Chromium 安装、完整测试与静态检查、构建及独立 wheel 安装均通过。Actions 对 `checkout@v4`、`setup-python@v5` 给出 Node.js 20 迁移警告，不影响本次通过结果 |

本结果证明仓库结构在当前 macOS 环境可安装、测试和构建，不代表完整跨平台、来源覆盖、宿主会话或生产验收通过。公开前的许可证确认与完整敏感信息审查仍需单独完成。

环境与待验收入口见 [environment-report](environment-report.md)，操作入口见 [installation](../guides/installation.md)、[configuration](../guides/configuration.md)、[usage](../guides/usage.md)、[operations](../guides/operations.md)；当前为本机可验证交付，不提升为正式权限隔离或生产验收通过。

最终文档核验：18 份项目/工程说明中的 42 条内部链接及 frontmatter 依赖均可解析，代码块闭合；17 个配置字段在模型、模板与参数表一致；三平台工作流结构有效。修改文件格式检查及两层 Git diff --check 通过。原有 test_proxy_and_status.py 保持原文，既有全库格式问题未借本轮改写。

## 伙伴开箱使用与受限规则闭环（2026-10-02）

本轮工程实现与本机回归 **PASS**；伙伴实机/宿主/真实源站验收 **PARTIAL**。本轮不执行真实政策采集、宿主插件安装、Git 提交/推送或常驻业务部署。所有网络采集测试均使用受控替身，工作台进程测试使用隔离数据库并在结束后停止。

| 验收面 | 结果 | 实际证据与边界 |
|---|---|---|
| 覆盖与环境说明 | PASS（文档） | README/安装指南提供两来源、直接附件、日期口径与桌面/Linux 环境矩阵；明确完整覆盖、源站准入和附件可读性的限制 |
| 环境引导 | PASS（本机测试） | setup Skill、macOS shell、Windows PowerShell 入口；6 项引导测试通过，覆盖已有配置保护、指定配置缺失/非法、仅财政部/工作台、Chromium 缺失及安装失败/启动、中文空格路径、重复运行、缺 uv 的哈希拒绝、固定项目虚拟环境。下载/命令部分使用替身，未在新设备真实下载完整环境 |
| 规则闭环 | PASS（受控故障） | 21 项规则测试通过；财政部/税务列表与正文故障恢复，历史契约样本、固定门禁、两轮预算、报告/规则篡改拒绝、跨主机和无限分页拒绝、暂停停止、网络最多两次重试、自动启用续跑、失败回退、中断恢复、备份迁移、重复版本详情请求限额。包内样本为既有解析回归与补充契约数据，不冒充真实历史原件 |
| 权限与质量边界 | PASS（代码及测试） | 规则只表达固定选择器/字段/分页；不执行候选 Python，不修改来源主机/Cookie/依赖/质量状态/可信测试。真实验证入口使用临时数据，最多一页、两次详情及 120 秒检查点预算；报告绑定执行器/契约/配置哈希且 15 分钟有效，首次续跑受同样上限约束 |
| 工作台生命周期 | PASS（本机真实进程） | 2 项测试验证缺库不初始化、端口冲突、真实子进程启动与健康检查、复用、身份不匹配拒绝、浏览器打开失败仍有 URL、停止与 SQLite 字节不变。独立 wheel 环境另验证启动/状态/停止 |
| 完整自动测试 | PASS（macOS） | `FTR_WEB_BROWSER=1 uv run --frozen --no-sync --extra dev --extra web pytest -q`：120 项通过，含 Chromium 页面回归；最后环境脚本变更后额外定向执行 6 项 setup 测试通过 |
| 静态与格式 | PASS | ruff check 源码/测试/脚本通过；mypy 指定忽略第三方缺失声明口径覆盖 22 个源文件通过；本轮 16 个 Python 文件格式检查通过；shell `sh -n` 通过，git diff --check 通过。既有未改动测试不批量格式化 |
| 构建与独立安装 | PASS（macOS） | sdist/wheel 构建；临时独立 venv 从源码目录外验证 config/doctor/schema、包内规则样本、模板、HTML/CSS/JS、主要只读 API 及工作台启动/状态/停止 |
| 双宿主清单与 Skill | PASS（静态） / PARTIAL（模型） | 两个清单可解析、七份 SKILL.md 存在；Controller 接 setup/repair/workbench。未安装宿主或执行模型会话，不计自然语言全链路通过 |
| Windows / Linux 与 CI | PARTIAL（本轮） | 三平台 CI 继续保留，新增 Windows PowerShell 语法检查。2026-10-01 旧基线远程 CI 通过；本轮未推送，未执行新远程 CI/Windows 真机/Xvfb 实采 |
| 真实源站与伙伴验收 | 未执行 | 未进行两个来源的真实小范围采集；macOS/Windows × Codex/Hermes 四组合的新环境与模型调用待单独授权/设备条件具备后验收 |

本轮无数据库 schema 变更；旧任务、备份与默认数据目录兼容。内置规则保留原 parser_version 去重口径，活动自修复规则追加版本哈希。备份包含 rules 制品/报告和数据库审计；旧备份无 rules 时使用内置规则。正式 Python 治理仍 BLOCKED，不能提升为生产验收或全国政策覆盖证明。

使用入口：[安装](../guides/installation.md)、[工作台与 CLI](../guides/usage.md)、[规则自修复](../guides/self-repair.md)。

## 首次配置与运行可靠性升级（2026-10-02）

本轮**本机工程验证 PASS，干净桌面／真实宿主／源站验收 PARTIAL**。伙伴执行总结仅作为问题线索，未把其中的数量、网络归因或“已跑通”作为本仓库实测证据。实施为普通授权开发，不扩大受限 Repair 的权限。

| 验收面 | 结果 | 实际证据与边界 |
|---|---|---|
| 自动配置与运行入口 | PASS（隔离测试） | 保护已有配置与数据，固定 Python 3.13，子进程隔离 PYTHONHOME/PYTHONPATH，保留已有 Web extra；运行包装器参数透传、中文空格路径、显式浏览器失效不替换、缺失浏览器下载失败与安装后验证。下载命令使用替身，未证明全新设备真实下载完成 |
| 浏览器与环境说明 | PASS（本机／故障注入） | 自动选择已有 Chromium、Edge、Chrome，按顺序验证启动；doctor 仅展示候选文件，不宣称启动或源站通过。完整浏览器回归实际运行 Chromium；本机 Edge/Chrome 策略兼容与干净 Windows 安装另验 |
| 工作台生命周期 | PASS（本机真实进程） | 包装启动器 PID 与服务 PID 分离，令牌／目录／实例握手、旧状态文件识别、错误 PID/实例停止拒绝、慢启动、端口冲突、解释器退出不轮试十端口、总启动超时、缺 Web 依赖分类、日志凭据与令牌脱敏；数据库字节不变 |
| 网络与连续失败 | PASS（受控替身） | 两客户端 502/503/504 最多三次尝试，默认 1/2 秒基础退避加抖动；403/429 不重试，未知故障停止；单条瞬态失败继续、连续三条停止、成功重置、列表失败原检查点保持及续跑幂等 |
| Repair 门禁兼容 | PASS（受控替身） | 重试与重定向经过实际 HTTP 请求计数，验证至多一次列表、两次详情及附件请求；等待可被暂停/取消/预算阻止。浏览器代码纳入执行器指纹；已保存资料后收到 INTERRUPTED/PAUSE_REQUESTED/CANCEL_REQUESTED 的首次续跑仍回退，不启用规则 |
| 进度与中断 | PASS（本机／故障注入） | stderr 定期输出最后检查点快照，不改变最终 stdout JSON。实际 POSIX SIGTERM 子进程以 PARTIAL 和中断原因收束；真实硬退出留下 RUNNING，仅取得独占锁后修正并审计。查询状态不写库、不把 RUNNING 当在线证明 |
| 质量解释与旧数据 | PASS（隔离测试） | 最新隔离资料多原因统计、未知历史原因保留原文、详情派生原因及任务停止原因；读取前后数据库字节一致。PDF 警告保留证据并审计绑定附件，新增完整性复核限制；旧任务/备份回归继续通过，无新增数据库表，无历史资料摘要或质量状态重写 |
| 完整自动测试 | PASS（macOS） | `FTR_WEB_BROWSER=1 uv run --frozen --no-sync --extra dev --extra web pytest -q`：**158 passed, 1 skipped**，31.92 秒。唯一跳过为 Windows 原生 PowerShell 入口测试；包含真实 Chromium 页面交互 |
| 静态／格式／脚本 | PASS | `ruff check src tests scripts` 通过；`mypy --ignore-missing-imports src` 覆盖 24 个源文件通过，仍使用项目指定忽略第三方缺失声明口径；16 个新增/修改 Python 文件格式检查通过；`sh -n scripts/setup.sh scripts/run.sh` 与 `git diff --check` 通过 |
| 构建与独立安装 | PASS（macOS） | `uv build` 构建 sdist/wheel；`uv run --frozen --no-sync --extra dev --extra web python scripts/verify_wheel.py` 从源码目录外创建独立环境安装 wheel，验证新模块与参数、打包模板/规则样本、静态资源、只读 API 和工作台实例身份及启动/状态/停止 |
| 文档与参数契约 | PASS | 当前文档相对链接、frontmatter depends_on 与代码块闭合检查通过；RuntimeSettings 与模板一致，共 22 个参数。README、操作指南、三个有关 Skill、PRD/设计/实施/测试计划/追踪矩阵同步 |
| 三平台 CI | PASS | 升级与 Linux 无桌面模拟测试修正已提交、推送；[GitHub Actions 36968262695](https://github.com/anbang278/finance-tax-policy-acquisition/actions/runs/36968262695) 在提交 `3d953b5` 上三平台全部通过。macOS：158 passed / 1 skipped；Windows：155 passed / 4 skipped；Ubuntu：157 passed / 2 skipped。包含 Chromium 页面回归、Windows PowerShell 脚本语法与原生入口、静态检查、构建和脱离源码的 wheel 验证；跳过项不计通过 |
| 干净桌面／真实宿主 | PARTIAL | 干净 Windows/macOS 的完整首次安装、Codex/Hermes 真实模型调用仍未验收；GitHub runner 不替代伙伴桌面或 AI 宿主验收 |
| 真实来源 | 未执行 | 未执行真实财政部/税务采集，不能宣称源站准入、全年覆盖或新重试策略在真实源站的效果已经通过 |

新增五个运行参数和派生响应字段，保持 CLI 主协议、冻结 TaskRequest、默认资料目录与旧状态/备份兼容。setup 返回 Git 提交号以及运行关键文件摘要，提交号不能单独代表未提交代码。用户追加授权后完成 Git 提交与推送；未执行宿主插件安装、部署、业务资料状态代判或长期记忆写入。

GitHub 交付：功能提交 `22d9c5f`，合并远端 README 更新 `8f70c26`，测试修正 `3d953b5`。首次远程验证在 Ubuntu 无显示环境下暴露模拟浏览器测试仍使用桌面设置的问题；该测试明确设为 headless 后，24 项本机 Repair 回归与上述三平台完整 CI 均通过。生产环境显示检查和 Repair 门禁未放宽。验收记录的后续提交仅更新文档，使用 `[skip ci]` 避免重复执行相同代码的三平台流水线；上面链接是代码与测试版本的实际验证证据。

遗留验收需要真实设备、宿主会话及当前任务的采集授权；本轮不将这些缺口计为通过。后续使用入口见 [安装](../guides/installation.md)、[运行参数](../guides/configuration.md)、[使用与诊断](../guides/usage.md)。

## 政策采集体验升级（2026-10-03）

工程与隔离恢复回归 PASS；完整浏览器命令 187 passed、1 skipped，33.72 秒；静态检查、构建与独立 wheel 通过。七项确认缺陷修复；D-02 采用完整性优先回退并保留效率 PARTIAL。两来源分别/合并真实首批通过，原件、可读性和复核分别报告；未读范围未知，没有宣称区间完整。

日期为空不单纯隔离，新任务严格拒绝错字段；旧任务、旧待复核摘要、备份、只读查询和附件新版本兼容。原审计不改写，既有业务目录的 565 个文件与复核状态保持；实施与本机验收阶段未提交或推送，后续用户已授权 GitHub 交付。未执行宿主安装、常驻部署或独立测试 Agent。Windows/Hermes/干净设备/真实新手及独立模型会话未验收。完整缺陷关闭表、请求 ID 和证据见[升级复验](usability-upgrade-2026-10-03.md)。


## 正文公文排版验收（2026-10-03）

PASS（本机）：完整 Chromium 回归 `FTR_WEB_BROWSER=1 .venv/bin/pytest -q` 为 195 passed、1 skipped，32.03 秒；跳过 Windows 原生入口，不计通过。ruff、mypy（25 个源文件，既有 ignore-missing-imports 口径）、修改 Python 文件格式检查、git diff --check、构建和独立 wheel 验证均通过。首次运行生命周期用例因真实工作台占用 8765 失败，经本应用 stop 释放端口后完整复跑通过，未修改该测试预期。

截图公告（财政部 2026 年第 25 号）实际恢复 27 段；1440×960 与 390×844 Chromium 检查居中文号、右对齐日期、长文滚动和刷新保位、无横向溢出及无 JavaScript 异常。浏览器查询前后数据库和全部已有原件 SHA-256 一致。两来源容器、嵌套/行内标签、br、已有换行、独立重复标题、脚本排除、内容不一致、无结构/表格回退、证据缺失/损坏均有回归。税务总局同公告按保存 HTML 恢复 2 个原始块，不按条款编号猜测额外段落。

原 body_text、哈希、版本与质量状态保持；无法可靠恢复时显示保存文本与回退说明。未访问官网、采集或提交/推送。本机证据不代替其他平台视觉验收。


### 当前浏览器交付修正（2026-10-03）

用户反馈限售股公告仍为连续正文。实际后端已恢复 14 块，但用户 Codex 浏览器 `.body-block` 数量为 0，普通 reload 后仍加载旧前端。新增 UI 静态资源版本查询参数与 HTML/静态资源 `Cache-Control: no-store`；相关 Web/正文回归 34 passed、1 skipped，ruff 通过。重启后工作台健康入口为 127.0.0.1:8766，并在用户原浏览器 tab 中导航到截图对应公告，实际确认 14 个正文块且段距生效。此前仅在独立浏览器验证，未确认用户现有页面已更新，完成结论过早。

只读遍历现有最新 60 份资料：财政部 14 份、税务总局 45 份可恢复结构，税务总局 1 份无法可靠恢复，按契约使用纯文本回退；这不是对全部资料视觉效果逐份人工验收。


## 全库与后续采集排版验收（2026-10-03）

PASS（本机）：只读扫描全部 60 条记录（本次无额外历史版本），财政部 14、税务总局 46 全部 structured；恢复文字非空白字符及顺序与存储一致（独立重复标题仅展示一次），数据库及全部证据 SHA-256 不变。结果见 [全库报告](body-layout-audit-2026-10-03.json)。此前回退的储蓄利息通知含纯图片空表格，修复后 6 段，用户实际 Codex tab 已确认。纯图片表格不展示图片或解析图中文字，原件仍保留；这次验收针对正文段落。

完整真实 Chromium 回归：201 passed、1 skipped，33.73 秒；Windows 原生入口跳过不计通过。ruff、mypy（26 文件）、构建与独立 wheel 验证通过。新采集正文使用共享块提取与 parser_version 0.1.4；两来源 parse_detail、正文哈希、br、嵌套行内容器、注释过滤、表格行边界及 Repair 严格门禁回归通过。未来含文字的复杂表格仍使用保存纯文本及保留换行，未宣称表格视觉复刻。

未执行新的真实采集，未来策略证据为两来源解析和隔离回归。旧正文/复核摘要不重写；共享代码已纳入执行器指纹，既有规则报告需按门禁重新验证。用户浏览器曾缓存旧的无查询参数 HTML，因此本次入口使用 ?ui=body-layout-2 加载最新 HTML，后续响应使用 no-store。工作台最新健康入口为 127.0.0.1:8765；未提交/推送。
