---
type: project_document
status: active
updated_at: 2026-09-30
depends_on: [acceptance-report.md, ../guides/installation.md, ../../uv.lock]
terms: [实测环境, 测试矩阵, 未验证门槛]
confidence: local_execution_evidence
---

# 环境与跨平台验证记录

记录日期：2026-09-30。本轮不执行真实源站采集、宿主安装或正式发布；历史源站观察见 [source-observation](../history/source-observation.md)，不能自动外推到另一台服务器。

## 本机执行环境

| 项目 | 实测值 |
|---|---|
| 操作系统 / 架构 | macOS 26.4.1 / arm64 |
| Python | CPython 3.13.11 |
| 包版本 | finance-tax-research 0.1.0 |
| 核心依赖 | Pydantic 2.13.5、httpx 0.28.1、lxml 6.1.3、pypdf 6.19.0、Playwright 1.63.0、PyYAML 6.0.3、requests 2.34.2、Portalocker 4.4.0 |
| 源码 Web 依赖 | FastAPI 0.142.1、Uvicorn 0.54.0 |
| 独立 wheel Web 依赖 | FastAPI 0.142.2、Uvicorn 0.54.0；按包版本约束重新解析 |
| 浏览器 | Playwright Chromium 文件存在；完整浏览器回归执行通过 |
| 网络观察 | 运行配置解析到回环 HTTP 代理；本轮未验证政策源站网络可达性 |

源码通过 uv.lock 安装锁定版本；独立 wheel 通过版本约束解析依赖，两者的实际版本分别记录。doctor 检查配置、Python、分项依赖、浏览器文件和显示变量，未启动浏览器验证显示连接或访问源站；不能将 LOCAL_DEMO_READY 当成全链路已通过。

## 分环境结果

| 环境 / 能力 | 状态 | 证据与未验证项 |
|---|---|---|
| macOS 核心、Web 与配置 | PASS | 完整浏览器回归 91 项通过；静态检查与构建通过；无真实采集写入 |
| macOS 独立 wheel | PASS | 临时独立 venv，从源码之外运行 config/doctor，包内模板、静态资源与主要只读 API 可用 |
| IPv4 局域网监听能力 | PASS（本机范围） | 真正启动 0.0.0.0 服务，通过本机连接并使用局域网 Host 查询；查询前后隔离数据库哈希一致 |
| IPv6 回环监听 | PASS（本机范围） | 真正启动 ::1，通过 IPv6 查询配置和资料 API，隔离数据库未改写 |
| Windows 原生 | PARTIAL | 移除 fcntl，加入跨平台锁、Windows 清单、盘符/UNC 拒绝及 PowerShell 文档；真实 Windows 运行待验证 |
| Linux / Xvfb | PARTIAL | 显示检查、启动参数与安装手册具备；Linux 运行、Xvfb 会话、税务实采待验证 |
| 三平台 CI | PARTIAL | 工作流已形成；尚未提交、推送或远程执行，不计各平台通过 |
| 另一台电脑访问工作台 | PARTIAL | 监听能力本机通过；真实网络、客户端与端口可达性待验收 |
| Codex/Hermes 模型全链路 | PARTIAL | 本轮仅更新配置说明与 Controller 入口，未重新实装或执行真实模型复核/研究 |
| 正式治理与发布 | BLOCKED | 可信隔离执行、独立批准、发布与回退仍未实施 |

## 下一轮实际验收

- 在 Windows 原生执行 REGRESSION，并记录符号链接负向测试是否因身份权限跳过；有跳过不能称为全部测试通过。
- 在 Linux 安装浏览器依赖与 Xvfb，分别运行 HTTP 链路、浏览器回归及有界税务实采；记录显示和网络准入结果。
- 从另一台电脑打开服务器实际 IP，验证筛选、原件下载、轮询和断连恢复；保留只读数据检查证据。
- 分别验证两个宿主能使用同一安装环境与绝对配置调用 CLI，完成实际语义回调与引用研究。

项目与工程目录各自存在 Git 仓库，本轮检查两层状态并保留原有工作台修改。未提交、推送、部署常驻服务或写入长期记忆。
