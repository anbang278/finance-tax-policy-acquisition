---
type: project_document
status: active
updated_at: 2026-09-30
depends_on: [../../AGENTS.md, design.md, ../../tests/REGRESSION.md]
terms: [Adapter, RuntimeSettings, 双宿主, 独立安装]
confidence: implementation_verified_locally
---

# 开发与交接

## 结构与职责

| 模块 | 责任 |
|---|---|
| `config.py` / `environment.py` | 运行参数加载、逐层校验、配置来源、脱敏展示与本地检查 |
| `cli.py` / `runtime.py` | JSON 协议、单写者锁、任务运行、检查点、预算与审计 |
| `network.py` / `adapters/` | 统一代理、受限下载、来源分页、详情解析与浏览器会话 |
| `repository.py` / `evidence.py` | SQLite 状态与版本、按哈希保存原件；当前无数据库迁移 |
| `research.py` / `repair.py` / `backup.py` | 引用校验、受限静态候选、完整性备份与恢复 |
| `web/` | 同源静态工作台、独立只读快照、证据下载与前端参数接口 |
| `skills/` 与两宿主清单 | 主控、语义复核、修复、变更审查、研究；模型推理由宿主提供 |

代码位于 `src/ftr/`，可信回归位于 `tests/`。`config/sources.yaml` 是来源信任表，不是机器运行参数；`contracts/official-material.yaml` 是资料接受契约；宪章和通用网络规则不属于运行期 Repair 可修改范围。

## 数据与控制流

`tasks` 保存任务输入摘要和状态；`source_runs` 保存各来源检查点与预算计数；`discovered` 保存发现队列；`documents` 保存来源版本与提取版本；`evidence` 保存原件元数据；`decisions/failures/audit` 保存复核、故障和状态事实。

原件以 SHA-256 保存，证据路径相对运行数据目录。每次运行审计追加脱敏配置与实际代理，不修改既有 TaskRequest 的摘要或预算。网页查询不拿采集锁，不初始化表。当前没有独立进程心跳，状态显示必须保留“数据库记录”的含义。

## 配置开发规则

新增字段先定义类型、默认值、单位和约束，再接入对应执行点；同步模板、参数手册、环境变量映射和测试。新增分组字段自动映射为 `FTR_<GROUP>__<FIELD>`，路径字段须显式实现相对路径基准。

CLI 仅传递显式参数，避免 argparse 默认值抢占 YAML/环境配置。Runtime 在业务写入前解析配置和代理，一次注入各客户端；禁止客户端在后续读取时自行改变出口。对进程外变化重新启动，不加入未经设计的热加载。

默认数据目录延续既有路径，不改为不同 OS 的原生应用目录，避免旧资料突然不可见。数据目录锁使用 Portalocker 的非阻塞独占锁，不能只靠锁文件是否存在判断占用。

## Adapter 扩展

修改现有 Adapter 前读宪章、对应来源、契约、样本与相关测试；用离线样本复现，再做最小修改。动态栏目参数继续来自实际成功的浏览器请求，不硬编码会话 Cookie。

新增来源不仅是增加 YAML 一行：需核验公开入口与允许主机、实现 Adapter、扩展 TaskRequest/CLI/Web 来源枚举及页面名称、加入日期/类型/分页/附件测试，再做来源独立实采。不能借财政部通过宣称另一来源通过。

Adapter 修复与普通授权工程开发分开。Repair 仅登记允许路径的静态补丁，不可改配置、可信测试、权限、资料状态或发布器来绕过失败。

## Codex / Hermes 接入

完整插件目录分别包含 `.codex-plugin/plugin.json` 和根 `plugin.json`，共享七个 Skill。两个宿主必须能找到同一个已安装 `ftr`，并继承相同绝对 `FTR_CONFIG` 或 FTR_DATA_DIR。Windows 入口是 `Scripts/ftr.exe`，macOS/Linux 是 `bin/ftr`；优先由 setup 返回绝对 Python 与配置，用 `python -m ftr.cli --config CONFIG` 调用。

宿主安装方式以目标环境实际能力为准，本次不自动修改用户宿主设置。分别验收：清单校验 → 七个 Skill 可加载 → 模型会话成功调用 doctor/CLI → 待复核资料回调 → 带引用研究。仅清单有效或 wheel 可运行不能计为宿主全链路通过。

## 验证与交接材料

命令见 [REGRESSION](../../tests/REGRESSION.md)。三平台工作流位于项目根 `.github/workflows/verify.yml`，测试 Python 3.13、核心与 Web、浏览器、静态检查、构建与独立 wheel 安装。工作流尚未提交或推送，远程执行结果须另外记录。

`scripts/verify_wheel.py` 在临时目录建立独立环境，移除源码 PYTHONPATH 与业务环境覆盖，校验模板、命令、静态资源和主要 API；它不进行真实来源采集或模型调用。

交接时附当前 Git 状态、安装环境、版本与检查命令、配置脱敏摘要、测试结果和未验证事项。用户草稿与其他未提交文件必须保留，未获授权不提交、推送或部署。
