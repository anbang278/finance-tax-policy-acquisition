# 财税知识与领域 Wiki 升级验收

日期：2026-10-22。实现分支：`codex/finance-tax-wiki`。基线：`096c7ea` / 应用 0.1.1。本功能纳入应用 0.1.2，运行数据库 schema 升级到 5；GitHub 发布进展与结果见 [发布记录](release-0.1.2.md)。

## 结论与范围

**PASS：本机隔离工程验收及三平台 CI。PARTIAL：真实供应商 API、真实财税资料质量与人工业务验收。**

已实现资料/附件证据定位、中文检索、知识版本、宿主与兼容 API 执行器、独立 AI 预审、人工审核启用、跨资料 Wiki、人工草稿保护、原子发布、历史/差异/导出、来源影响与待认领队列、管理页面及备份恢复。没有以静态页面或占位结果替代加工。

真实资料库只读回查仍为 schema 0，60 个资料版本：collected 33、quarantined 27、validated 0。本次所有状态推进、采集质量模拟与发布均在新建隔离库中，未直接升级真实库、真实采集、真实资料复核、真实知识发布或部署。Git 提交推送由后续单独授权的 0.1.2 发布流程完成。保留了任务开始时 README 与 implement 两处既有链接修改及旧嵌套仓库现场。

## M0–M5 对照

| 阶段 | 本次交付 | 验证与边界 |
|---|---|---|
| M0 | 根目录、契约、工作树和原库状态核对；独立功能分支 | 基线测试先跑，所有新增 fixture 均隔离 |
| M1 | v5 原子迁移/迁移前备份、正文与附件快照、PDF 页码、Unicode 锚点、索引代切换、短词回退与历史日期 | 实际生成文本 PDF；早期迁移故障回滚；附件词可搜；错误位置/附件拒绝 |
| M2 | 统一包/回传 Schema、冻结输入、重试尝试、预算、两种执行器、持久结果与恢复、独立预审、人工门禁 | MockTransport 验证 API；本轮 Codex 实际加工虚构任务包；没有真实供应商调用 |
| M3 | 领域专题多入口、知识复用、Wiki 服务、人工新草稿、差异、发布历史、Markdown | 桌面与 390px 完整 UI 流程；发布事务故障与人工冲突；人工维护章节不可被 AI 覆盖 |
| M4 | 持久观察指纹、分批来源核查、已引用传播/新资料待认领、等价核查、模板/资格/日期核查 | 修订三年→四年的宿主演示；旧版本保留；需要审核的新来源不自动通过 |
| M5 | /wiki 页面、受保护上传/命令、CLI/Skill/文档、安装包 | 全量 Chromium、静态检查、构建与源码外独立安装通过 |

领域/专题、item/page 的共享版本和审核逻辑使用统一仓储；Wiki 差异/渲染/导出独立在 knowledge/wiki.py，证据索引、加工执行、契约与接口分别拆分。未引入额外模型 SDK、向量库或 Web 框架。

## 实际工程检查

| 检查 | 结果 |
|---|---|
| `FTR_WEB_BROWSER=1 ... pytest -q` | **347 passed, 1 skipped，63.07 秒**；含 1440px 与 390px 新流程 |
| 最后补充的已保存 API 结果恢复专项 | PASS；不重新调用模型，使用已持久结果完成入库 |
| PR 三平台 CI | [completed/success](https://github.com/anbang278/finance-tax-policy-acquisition/actions/runs/39766848271)；macOS 347/1、Ubuntu 346/2、Windows 343/5（通过/跳过），见 [发布记录](release-0.1.2.md) |
| `ruff check src tests scripts` | PASS |
| `mypy --ignore-missing-imports src` | PASS，42 个源文件；沿既有忽略第三方缺失声明口径 |
| 改动 Python 文件格式检查 | PASS，27 个文件 |
| `node --check .../wiki.js` | PASS |
| `git diff --check` | PASS |
| `uv build --offline` | PASS，sdist 与 wheel |
| `UV_OFFLINE=1 ... python scripts/verify_wheel.py` | PASS：独立 venv，源码外 CLI/模板/Schema/知识模块/新静态资源/API/本机管理及后台 |

1 个跳过是 macOS 上的 Windows PowerShell 专属用例；真实 Windows/Linux 实机未执行；本轮远程 CI 的后续结果见 [发布记录](release-0.1.2.md)。FastAPI/Starlette testclient 提示一个已有上游弃用警告，不影响通过；未为了消除警告增加无关依赖。

开发中发现并修复：空闲 API 线程不必要写库导致争锁、未完成任务的空结果读取、旧命令故障注入调用兼容、长结果等待写锁时的落盘/回放、人工维护内容的冻结与覆盖阻止。失败不计入最终 PASS；以上表最终结果为准。

## A01–A28 验收矩阵

以下 PASS 均指受控资料下的工程行为，不代表真实政策覆盖或财税结论正确。

| 编号 | 观察结果与证据 |
|---|---|
| A01 | PASS：两份互补资料产生带独立引用的知识；宿主演示区分申请资格、基数排除、留存要求 |
| A02 | PASS：PDF 第二页独有词可搜，引用保留实际附件证据及第 2 页 |
| A03 | PASS：未解析附件阻止正式加工，索引 issues 与页面展示解析缺口 |
| A04 | PASS：同一知识身份被两个领域检索复用 |
| A05 | PASS：附件/片段/位置/摘要任一不匹配则拒绝；旧 research 接口同步支持精确附件锚点 |
| A06 | PASS：采集 validated 不会自动批准知识；独立预审不产生人工批准，人工退回不能发布。真实遗漏识别质量仍待人工业务评测 |
| A07 | PASS：同 URL 新资料版本使下游知识/章节待核查，旧快照保留 |
| A08 | PASS：无引用的新资料进入 unclaimed_material 队列，不判定无影响 |
| A09 | PASS：导航变化经过正文/字段/附件等价记录后恢复旧依赖，不批准新 collected 资料 |
| A10 | PASS：人工草稿修订号冲突阻止旧任务回传；人工维护章节进入冻结输入，AI 替换被拒 |
| A11 | PASS：宿主包可导出、等待状态可见；无模型配置或执行结果不生成知识 |
| A12 | PASS：非法回传留痕并可用新键纠正；同键异文冲突；同内容重复返回原结果；新尝试保留旧结果 |
| A13 | PASS：进程中断标远端结果不确定；已保存回传可恢复，失败不会误报完成 |
| A14 | PASS：发布故障注入后旧指针/历史保持完整，不出现半发布 |
| A15 | PASS：来源资格失效时阻止发布，默认研究检索排除受限知识；撤回的已发布内容明确受限 |
| A16 | PASS：人工核验的明确期间可筛选；未知区间不算匹配；没有推断完整法律效力 |
| A17 | PASS：中文短词/长词、文号、正文与附件统一检索，分 material/item/page |
| A18 | PASS：只读 DB 字节不变；会话/Origin/客户端不符拒写；上传不直写业务 DB |
| A19 | PASS：schema 0/旧版本兼容、迁移前备份、迁移失败全事务回滚及恢复 |
| A20 | PASS：导出包含身份/版本、核验、条件、期间、原始引用和缺口，无运行数据本机路径 |
| A21 | PASS：页面与引文均转义；外部文字不作为命令/权限或模型工具调用执行；沿既有脚本注入回归 |
| A22 | PASS：脱离源码安装的 wheel 包含知识模块、Schema 生成、新页面与配置模板 |
| A23 | PASS：宿主/API 使用同一回传校验与候选服务；API 不取得人工审核权限 |
| A24 | PASS：鉴权、限流、超时、拒绝、截断、无效输出与预算失败均不会变成成功候选 |
| A25 | PASS：模型等待时业务锁可取得；第二执行器被拒；中断不会自动重复计费；已保存结果可回放 |
| A26 | PASS：大于 64 KB 的结果可上传；4 MiB 上限、路径/哈希校验与任务制品备份恢复 |
| A27 | PASS：实际早期迁移故障无残留新表/版本推进；查询不迁移；原真实库仍为 v0 |
| A28 | PASS：短词与长词均能检索，模型/伪造请求不能指定人工 actor 或跳过确认发布 |

主要自动化入口为 tests/test_knowledge.py，既有 management/scheduler/web/portability 等回归继续执行。没有削弱已有断言；三处旧迁移测试及 wheel 的当前 schema 预期由 4 更新到 5。

## 宿主语义加工与变更演示

本轮 Codex 读取任务包中两份明确标识的虚构资料，实际组织三条知识和一个专题，并单独完成 AI 预审。没有使用额外远端 API，实际模型标识无法独立确认，记录 unknown。

初始资料分别说明申请资格以及基数排除/留存；候选没有把“可申请”写成“自动享受”，也没有把支出排除扩大为主体资格排除。资料 B 修订留存三年为四年后，旧页面先进入 needs_review，再生成新的知识与 Wiki 版本，最终依赖 checked，两个 Wiki 历史版本均保留。

**演示中的采集质量批准、人工审核与发布均由隔离夹具程序模拟；AI 自检不代替财税人员人工验收。**

- [摘要与版本身份](knowledge-wiki-host-demo.json)
- [实际读取的宿主任务包](knowledge-wiki/host-task-package.json)、[宿主加工结果](knowledge-wiki/host-result.json)
- [初始 Wiki 导出](knowledge-wiki/initial-wiki.md)、[修订 Wiki 导出](knowledge-wiki/revised-wiki.md)、[修订差异](knowledge-wiki/revision.diff)
- [桌面截图](knowledge-wiki/desktop.png)、[390px 截图](knowledge-wiki/mobile.png)

示例 ID 绑定演示任务，不能直接导入另一资料库冒充实际结果。

## 使用、迁移与未验证项

入口见 [知识与 Wiki 操作指南](../guides/knowledge-wiki.md)。现有工作台本人管理会话下点击“知识与 Wiki”；普通模式保持只读。API 默认禁用，用户明确配置服务和密钥环境变量后，再启动指定任务。自动发现只进入待处理队列。

写入升级前自动备份到数据目录相邻独立目录；旧库升级不是普通浏览的副作用。回退旧程序在新目录恢复升级前备份，禁止旧程序写 v5。备份包含版本、审核、发布、依赖、任务、命令与大结果制品。

尚未验证：真实供应商/网关连通性及具体模型兼容、真实企业所得税资料覆盖与财税人员语义评测、Hermes 实际插件会话、新 Windows/Linux 实机。远程 CI 后续结果见 [发布记录](release-0.1.2.md)。已支持的处理器不等于这些场景已通过。OCR、Office 专门解析、完整法律时点推理、多用户和互联网托管保留为后续范围。

本次复盘：需要持续保留的两条约束是模型等待与单写队列隔离、草稿修订号独立于发布指针；两者都有对应失败注入和实际浏览器证据。

## 变更文件清单

- `.codex-plugin/plugin.json`
- `ARCHITECTURE.md`
- `CHANGELOG.md`
- `README.md`
- `docs/README.md`
- `docs/development/design.md`
- `docs/development/implement.md`
- `docs/development/prd.md`
- `docs/development/traceability.md`
- `docs/guides/configuration.md`
- `docs/guides/knowledge-wiki.md`
- `docs/verification/acceptance-report.md`
- `docs/verification/knowledge-wiki-acceptance.md`
- `docs/verification/knowledge-wiki-host-demo.json`
- `docs/verification/knowledge-wiki/desktop.png`
- `docs/verification/knowledge-wiki/host-result.json`
- `docs/verification/knowledge-wiki/host-task-package.json`
- `docs/verification/knowledge-wiki/initial-wiki.md`
- `docs/verification/knowledge-wiki/mobile.png`
- `docs/verification/knowledge-wiki/revised-wiki.md`
- `docs/verification/knowledge-wiki/revision.diff`
- `docs/verification/release-0.1.2.md`
- `docs/verification/test-plan.md`
- `ftr.example.yaml`
- `plugin.json`
- `pyproject.toml`
- `scripts/verify_wheel.py`
- `skills/knowledge-wiki/SKILL.md`
- `src/ftr/__init__.py`
- `src/ftr/adapters/common.py`
- `src/ftr/backup.py`
- `src/ftr/cli.py`
- `src/ftr/config.py`
- `src/ftr/knowledge/__init__.py`
- `src/ftr/knowledge/cli.py`
- `src/ftr/knowledge/commands.py`
- `src/ftr/knowledge/contracts.py`
- `src/ftr/knowledge/indexing.py`
- `src/ftr/knowledge/models.py`
- `src/ftr/knowledge/processing.py`
- `src/ftr/knowledge/schema.py`
- `src/ftr/knowledge/service.py`
- `src/ftr/knowledge/web.py`
- `src/ftr/knowledge/wiki.py`
- `src/ftr/management.py`
- `src/ftr/repository.py`
- `src/ftr/research.py`
- `src/ftr/web/app.py`
- `src/ftr/web/management.py`
- `src/ftr/web/static/index.html`
- `src/ftr/web/static/wiki.css`
- `src/ftr/web/static/wiki.html`
- `src/ftr/web/static/wiki.js`
- `src/ftr/worker.py`
- `tests/REGRESSION.md`
- `tests/test_knowledge.py`
- `tests/test_management.py`
- `tests/test_scheduler.py`
- `tests/test_usability_upgrade.py`
- `uv.lock`
