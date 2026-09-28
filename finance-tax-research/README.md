# 财税资料研究插件（本地演示）

支持财政部政策发布和税务总局政策法规库，保存原始资料与版本证据，等待模型复核后供研究使用。当前修复能力止于受限候选补丁；正式发布尚未开放。

## 安装

在本工程目录执行 `uv sync --extra dev` 和 `uv run playwright install chromium`。将 Codex 插件目录或 Hermes 可移植插件目录指向本工程；两端必须能调用同一虚拟环境中的 `ftr`。设置 `FTR_DATA_DIR` 为工作数据目录，默认是用户目录下 `.local/share/ftr`。当前只验证了清单和 wheel，宿主实际安装仍须分别验收。

## 使用

`uv run ftr doctor` 检查配置；用户确认时间范围后，用 `uv run ftr collect --sources mof --date-from 2026-09-01 --date-to 2026-09-28 --max-pages 1 --max-documents 2` 做有界采集演示。`uv run ftr decision list --task ID` 查看待复核资料，使用 `uv run ftr decision submit --file decision.json` 提交结构化决策。`uv run ftr search --query 增值税` 检索已验证资料。`ftr research prepare --query 增值税` 生成已验证资料候选，`ftr research submit --file draft.json` 核对引用并渲染研究草稿。`ftr backup create --output 路径`、`backup verify --path 路径`、`backup restore --path 路径 --target 新目录` 用于一致性备份与非覆盖恢复。

命令均输出 JSON。采集任务必须明确提供起止日期；用户未说明时，Controller Skill 先询问，不自行选择期限。首次全量列表枚举可能达到运行预算并返回 `PARTIAL`，可用 `ftr task resume --task ID` 续跑。`ftr doctor --mode governed` 和 `ftr repair test` 目前固定返回 `BLOCKED`。

本机若已配置使用回环 IP 的 HTTP/HTTPS 代理，HTTP 客户端和税务浏览器会显式使用同一代理。每个进程固定使用首次读取的代理配置，调整系统代理后需重新启动命令。此时允许登记域名解析到 `198.18.0.0/15` 代理映射网段，域名由代理转发；未配置本地代理时仍拒绝该网段。回环、私网和链路本地目标地址仍被拒绝，HTTPS 与登记主机限制保持生效。不再使用 `FTR_ALLOW_TEST_NET_DNS` 无代理放行开关。

## 验收边界

具体宿主是否成功加载、模型语义判断、正式权限隔离均需分别实测。税务站在本机需要有界面 Playwright 建立正常会话；无界面模式返回 HTTP 403。Adapter 从成功的浏览器请求读取栏目参数与会话 Cookie，再用 `requests` 请求分页、详情及附件，不保存 Cookie。Word、Excel 与扫描件仅保留原件；主附件未解析时资料进入受限状态。详见上层 `docs/acceptance-report.md`。
