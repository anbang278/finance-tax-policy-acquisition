---
type: project_document
status: active
updated_at: 2026-10-02
depends_on:
  - docs/development/prd.md
  - docs/verification/acceptance-report.md
terms: [公开财税资料, 来源版本, 语义复核, 跨环境配置]
confidence: implementation_verified_locally
---

# 财税 AI 共创：政策获取子模块

本项目从财政部政策发布和国家税务总局政策法规库获取公开资料，保存原始证据、日期出处和资料版本；经语义复核后，供带引用的研究使用。核心是一份 Python 包，既可从 CLI 运行，也可由 Codex/Hermes 的七个 Skill 调用；工作台用于只读查询与任务观察。

## 最简使用：一句话开始采集

将下面这句话直接发给具备本机终端和文件访问能力的 AI（如 Codex、Hermes）：

```text
请加载 https://github.com/anbang278/finance-tax-policy-acquisition ，按仓库说明完成配置，并采集【去年】至【今日】的财政部和税务总局政策，汇报结果并启动可视化界面。
```

## 按角色开始

- **使用者**：按[安装与部署](docs/guides/installation.md)获取完整项目，让 AI 调用 setup 自动准备用户级环境，再按[使用流程](docs/guides/usage.md)运行；已有配置和数据会保留。迁移与故障处理见[运维手册](docs/guides/operations.md)。
- **开发者**：从根目录运行 `uv sync --frozen --extra dev --extra web`；阅读[开发说明](docs/development/development.md)、[架构](ARCHITECTURE.md)和[回归要求](tests/REGRESSION.md)。
- **Agent**：先读本页、[当前需求基线](docs/development/prd.md)、[验收记录](docs/verification/acceptance-report.md)和[Agent 契约](AGENTS.md)，再按任务阅读相关专题。`CONTRIBUTING.md` 提供贡献入口。


## 覆盖范围与环境要求

| 使用范围 | 实际覆盖与日期语义 | 本机环境要求 |
|---|---|---|
| 财政部 `mof` | 仅“政策发布”栏目直接条目及直接附件；默认日期为栏目日期 | uv、Python 3.13、锁定核心依赖；HTTP 采集无需 Chromium |
| 税务总局 `chinatax` | 仅登记的政策法规库栏目直接条目及直接附件；默认日期为成文日期 | 核心环境、Chromium、可用显示环境；浏览器会话与本机网络出口须源站接受 |
| 本机查询工作台 | 已保存资料、任务、版本和原件；不代表官网实时状态 | 核心环境与 Web extra；无需 Chromium、无需政策源站网络 |
| Linux 无桌面税务 | 与税务栏目范围相同；源站准入需实测 | Chromium 系统库、Xvfb/xauth；管理员系统安装由部署者完成，本轮桌面验收不含此场景 |

当前两个入口不等于全国官方财税法规政策全集，不覆盖地方政策、其他官方机构或递归关联页面。两来源均支持指定起止日期、历史回填和断点续跑，但不能把有限预算、14 天增量回看或 CI 测试称为完整历史覆盖证明。

HTML 和文本 PDF 可提取；Word、Excel、扫描件仅保留原件并显示限制。附件下载成功不代表正文可供研究；未复核资料默认不进入研究检索。成文、发布、施行日期与法律效力需分别判断，缺失日期不猜补。

## 快速启动

获取完整项目：使用 `git clone https://github.com/anbang278/finance-tax-policy-acquisition`，或在仓库页面选择 Code → Download ZIP 后解压。下载失败先检查网络和已配置的代理；不自动修改全局代理。源码包须包含 `pyproject.toml`、`uv.lock`、`scripts/` 和 `skills/`。

让 AI 使用 `setup` Skill；手动入口为：

```sh
# macOS；仅财政部选 mof，仅查询选 workbench
sh scripts/setup.sh all
# 后续命令不重新安装依赖
sh scripts/run.sh config validate
sh scripts/run.sh workbench start --open
```

```powershell
# Windows 原生 PowerShell，无需 WSL 或 Docker
& .\scripts\setup.ps1 -Capability all
& .\scripts\run.ps1 config validate
& .\scripts\run.ps1 workbench start --open
```

setup 自动准备用户级 uv、Python 3.13 和锁定依赖，优先复用可启动的 Chromium、Edge、Chrome，缺浏览器时才下载 Chromium；显式浏览器路径失效则报告。报告包含环境、浏览器启动、配置/数据/运行入口及代码版本；不访问源站，也不初始化资料库。无资料库时工作台提示先采集，已有空库可正常浏览。自定义配置用 `FTR_CONFIG`，数据目录用 `FTR_DATA_DIR`；参数见[配置指南](docs/guides/configuration.md)。

用户提供明确来源与起止日期后，AI 用运行入口执行有界采集，例如 `sh scripts/run.sh collect --sources mof --date-from 2026-09-01 --date-to 2026-09-28 --max-pages 1 --max-documents 2`。达到预算或中断后报告 `PARTIAL`、剩余队列与续跑入口，由用户要求续跑。进度写 stderr，最终结果保持 JSON。查询使用 `workbench` Skill；研究仍要求资料经复核。

开发与 Linux Xvfb 入口、局域网监听和独立 wheel 安装见[安装部署](docs/guides/installation.md)。七个 Skill 为 controller、setup、workbench、semantic-review、research、repair、change-review；受限自修复见[本机规则修复](docs/guides/self-repair.md)。

## 能力与边界

完整工作流是：用户明确来源与日期范围 → 创建任务并冻结任务预算 → 保存列表原件与分页检查点 → 下载正文及直接附件 → 提取字段和文本 → 保存资料版本与内容限制 → 语义复核 → 已验证资料检索 → 带证据引用的研究。

列表和原件 HTTP 成功不代表资料已经复核。资料类型包含政策文件、公告、官方解读、发布消息等；成文、发布与栏目日期分别记录，不猜测缺失日期。法律效力和企业适用性需要另外判断。

资料质量状态为 `collected`、`validated`、`quarantined`、`rejected`。工作台展示各状态的最新版本；研究默认仅使用已验证资料。有证据的结构故障可由宿主生成受限规则，经可信样本与有界真实来源验证后自动本机启用、续跑及失败回退。未知 Python 补丁仍只登记，正式代码隔离、发布治理未开放。

当前交付是本地可验证研发演示，不代表正式权限隔离或生产发布已完成。跨环境真实运行、Codex/Hermes 模型会话、两来源完整覆盖及跨电脑局域网访问仍需分别验收；详见[验收记录](docs/verification/acceptance-report.md)和[环境记录](docs/verification/environment-report.md)。

团队局域网工作台不增加认证，能够访问监听地址的成员可查询及下载已保存资料。请在部署前评估网络暴露范围。

## 许可证

本项目依据 Apache License 2.0 发布，详见 [LICENSE](LICENSE)。

## 文档导航

| 内容 | 入口 |
|---|---|
| 文档索引与职责 | [docs/README.md](docs/README.md) |
| 需求范围与成功标准 | [PRD](docs/development/prd.md) |
| 设计、实施与追踪 | [设计](docs/development/design.md)、[实施记录](docs/development/implement.md)、[追踪矩阵](docs/development/traceability.md) |
| 安装、配置、使用与运维 | [指南目录](docs/guides/) |
| Agent 与开发交接 | [AGENTS.md](AGENTS.md)、[开发说明](docs/development/development.md)、[CONTRIBUTING.md](CONTRIBUTING.md) |
| 测试、验收与环境证据 | [测试计划](docs/verification/test-plan.md)、[验收记录](docs/verification/acceptance-report.md)、[环境记录](docs/verification/environment-report.md) |
| 历史交接与来源观察 | [历史材料](docs/history/)；仅作历史证据，不覆盖当前文档或代码 |

## 工程入口

仓库根目录是唯一工程与插件入口。运行代码和可信测试分别位于 `src/ftr/` 与 `tests/`；来源信任表在 `config/sources.yaml`，资料契约在 `contracts/official-material.yaml`。Codex 与 Hermes 共享 `skills/`，各自清单位于 `.codex-plugin/plugin.json` 和根 `plugin.json`。SQLite 与原件存放在运行数据目录，独立于源码与虚拟环境。
