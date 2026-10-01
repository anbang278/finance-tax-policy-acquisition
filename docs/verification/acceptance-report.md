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
| 跨平台与远程 CI | 未验证 | 本机没有运行 Windows/Linux；未推送，因此 GitHub Actions 尚未运行 |

本结果证明仓库结构在当前 macOS 环境可安装、测试和构建，不代表完整跨平台、来源覆盖、宿主会话或生产验收通过。公开前的许可证确认与完整敏感信息审查仍需单独完成。

环境与待验收入口见 [environment-report](environment-report.md)，操作入口见 [installation](../guides/installation.md)、[configuration](../guides/configuration.md)、[usage](../guides/usage.md)、[operations](../guides/operations.md)；当前为本机可验证交付，不提升为正式权限隔离或生产验收通过。

最终文档核验：18 份项目/工程说明中的 42 条内部链接及 frontmatter 依赖均可解析，代码块闭合；17 个配置字段在模型、模板与参数表一致；三平台工作流结构有效。修改文件格式检查及两层 Git diff --check 通过。原有 test_proxy_and_status.py 保持原文，既有全库格式问题未借本轮改写。
