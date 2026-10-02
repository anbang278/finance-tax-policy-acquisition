---
type: project_document
status: active
updated_at: 2026-09-30
depends_on: [../../pyproject.toml, configuration.md]
terms: [Python 3.13, wheel, Xvfb, 局域网]
confidence: implementation_verified_locally
---

# 安装与部署

需要 Python 3.13（当前包声明 `>=3.13,<3.14`）。优先使用 uv 与仓库 `uv.lock` 重现依赖；Web 是可选 extra，Chromium 仅用于税务采集及浏览器测试。不要复制其他机器的 `.venv` 或浏览器安装目录，每台机器重新安装。

## 非技术伙伴与宿主引导

完整插件目录提供 `setup` Skill 和引导脚本。macOS 执行 `sh scripts/setup.sh all`，Windows 执行 `& .\scripts\setup.ps1 -Capability all`；能力选项为 mof/chinatax/workbench/all。先检测 uv/Python，再按需要同步依赖；无需先有 ftr。已有 Python 由独立虚拟环境复用，缺 Python 时由 uv 下载到用户级 managed storage。不会修改系统 Python、shell profile、全局代理或已有配置。

缺 uv 的桌面机器从 [Astral 官方 release 0.8.22](https://github.com/astral-sh/uv/releases/tag/0.8.22) 下载对应架构制品并核对仓库固定 SHA-256；已有 PATH 或专属用户目录中的 uv 直接复用。用户目录为 macOS `~/.local/share/ftr/bootstrap/bin`，Windows `%LOCALAPPDATA%/FTR/bootstrap/bin`。本机依赖仍由 uv.lock 锁定，浏览器由已锁定 Playwright 安装。Python 自动下载方式见 [uv 官方说明](https://docs.astral.sh/uv/guides/install-python/)。

引导返回 ENVIRONMENT_READY/BLOCKED、绝对 python/config/data_dir、分项依赖和浏览器实际启动结果；后续调用 `"绝对 Python" -m ftr.cli --config "绝对配置" ...`，避免宿主 PATH 差异。已指定 FTR_CONFIG 但文件缺失时明确失败，不另建配置。网络安装失败保留配置，重复运行继续补齐。安装只是本机准备，不证明源站接受访问。

Linux 缺 uv 或浏览器系统库时按官方安装和下文系统依赖说明人工处理；不自动 sudo。Windows 执行策略由用户按组织规则处理，脚本不修改永久执行策略。Chromium 启动空白页检查不采集，不建立数据库。


## 覆盖范围与环境要求

| 使用范围 | 实际覆盖与日期语义 | 本机环境要求 |
|---|---|---|
| 财政部 `mof` | 仅“政策发布”栏目直接条目及直接附件；默认日期为栏目日期 | uv、Python 3.13、锁定核心依赖；HTTP 采集无需 Chromium |
| 税务总局 `chinatax` | 仅登记的政策法规库栏目直接条目及直接附件；默认日期为成文日期 | 核心环境、Chromium、可用显示环境；浏览器会话与本机网络出口须源站接受 |
| 本机查询工作台 | 已保存资料、任务、版本和原件；不代表官网实时状态 | 核心环境与 Web extra；无需 Chromium、无需政策源站网络 |
| Linux 无桌面税务 | 与税务栏目范围相同；源站准入需实测 | Chromium 系统库、Xvfb/xauth；管理员系统安装由部署者完成，本轮桌面验收不含此场景 |

当前两个入口不等于全国官方财税法规政策全集，不覆盖地方政策、其他官方机构或递归关联页面。两来源均支持指定起止日期、历史回填和断点续跑，但不能把有限预算、14 天增量回看或 CI 测试称为完整历史覆盖证明。

HTML 和文本 PDF 可提取；Word、Excel、扫描件仅保留原件并显示限制。附件下载成功不代表正文可供研究；未复核资料默认不进入研究检索。成文、发布、施行日期与法律效力需分别判断，缺失日期不猜补。

## macOS / Linux 源码安装

安装 uv 后在仓库根目录运行：

```sh
uv python install 3.13
uv sync --frozen --extra web
uv run playwright install chromium
# 目标 ftr.local.yaml 应不存在；已有配置先保留
cp ftr.example.yaml ftr.local.yaml
uv run ftr --config "$PWD/ftr.local.yaml" config validate
uv run ftr --config "$PWD/ftr.local.yaml" doctor
```

仅财政部 HTTP 采集或工作台可省略 Chromium 安装。开发时执行 `uv sync --frozen --extra dev --extra web`。不安装 `web` extra 时 CLI 仍可使用，`serve` 会明确提示缺少依赖。

## Windows 原生 PowerShell

无需 WSL；在 Python 3.13 和 uv 可用的 PowerShell 中从仓库根目录运行：

```powershell
uv python install 3.13
uv sync --frozen --extra web
uv run playwright install chromium
if (Test-Path .\ftr.local.yaml) { throw '配置已存在，请保留并编辑现有文件' }
Copy-Item .\ftr.example.yaml .\ftr.local.yaml
$env:FTR_CONFIG = (Resolve-Path .\ftr.local.yaml).Path
$env:FTR_DATA_DIR = 'C:\FTR\data'
uv run ftr config validate
uv run ftr doctor
uv run --extra web ftr serve --port 8765
```

可将 YAML 数据目录改为 `C:/FTR/data`，再移除环境变量覆盖：`Remove-Item Env:FTR_DATA_DIR`。命令成功处理工作流时 `$LASTEXITCODE` 为 0，具体完成程度读取 JSON 状态。

## 独立 wheel 安装

开发者先在仓库根目录执行 `uv build`，分发 wheel 后无需源码目录运行。以下路径为示例，替换为实际 wheel：

```sh
python3.13 -m venv /目标位置/ftr-venv
/目标位置/ftr-venv/bin/python -m pip install '/制品位置/finance_tax_research-0.1.0-py3-none-any.whl[web]'
/目标位置/ftr-venv/bin/python -m playwright install chromium
/目标位置/ftr-venv/bin/ftr doctor
```

Windows 对应入口为 `ftr-venv\Scripts\python.exe` 和 `ftr-venv\Scripts\ftr.exe`：

```powershell
py -3.13 -m venv C:\FTR\venv
C:\FTR\venv\Scripts\python.exe -m pip install 'C:\packages\finance_tax_research-0.1.0-py3-none-any.whl[web]'
C:\FTR\venv\Scripts\python.exe -m playwright install chromium
C:\FTR\venv\Scripts\ftr.exe doctor
```

模板随 wheel 安装。可以用安装环境的 Python 将资源写入一个不存在的新文件：

```sh
python -c "from importlib.resources import files; p=open('ftr.local.yaml','x',encoding='utf-8'); p.write((files('ftr')/'data'/'ftr.example.yaml').read_text(encoding='utf-8')); p.close()"
```

这里的 `python` 须为已安装 wheel 的解释器。完整独立安装自动验证入口为项目根目录的 `scripts/verify_wheel.py`。wheel 不安装操作手册或宿主 Skill；宿主接入仍使用完整插件目录。

## Linux 无桌面税务采集

以下以 Debian/Ubuntu 的 apt 环境为例，由部署者安装系统依赖：

```sh
uv run playwright install --with-deps chromium
sudo apt-get install -y xvfb xauth
export FTR_CONFIG=/srv/ftr/ftr.yaml
xvfb-run -a uv run ftr doctor
xvfb-run -a uv run ftr collect --sources chinatax --date-from 2026-09-01 --date-to 2026-09-28 --max-pages 1 --max-documents 1
```

Xvfb 为有界面 Chromium 提供虚拟显示，是 [Playwright 官方支持的执行方式](https://playwright.dev/python/docs/ci#running-headed)。保留 `browser.headless: false`；它解决显示依赖，不保证税务站接受该服务器的网络出口。403、验证码或登录限制停止并记录失败，不自动切换代理或绕过。

财政部和 Web 不启动浏览器，无需 Xvfb。`doctor` 检查浏览器文件和显示变量，不实际启动浏览器或测试显示连接、源站访问。

## 局域网工作台

配置 `web.host: 0.0.0.0` 或服务器网卡地址，或执行：

```sh
uv run --extra web ftr --config /srv/ftr/ftr.yaml serve --host 0.0.0.0 --port 8765
```

使用浏览器访问 `http://服务器实际IP:8765`；`0.0.0.0` 不是客户端访问地址。IPv6 示例：监听 `--host ::`，访问 `http://[服务器IPv6]:8765`。客户端需能到达该端口，网络或操作系统防火墙的放行由部署环境负责。本次不新增认证或访问名单。

前台按 `Ctrl+C` 停止。服务器常驻运行由部署者接入已有进程管理器，明确工作目录、安装环境、绝对配置路径和运行用户；本次不新增定时采集或后台 Agent。工作台 lifecycle 命令可以单独管理只读查询进程。
