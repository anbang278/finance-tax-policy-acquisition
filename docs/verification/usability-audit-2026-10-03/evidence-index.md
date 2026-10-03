# 证据索引与复现

全部原始证据保存在本机独立目录：

[/Users/anbang/.local/share/ftr-acceptance/2026-10-03-8c31e1a](/Users/anbang/.local/share/ftr-acceptance/2026-10-03-8c31e1a)

临时执行目录为 `/tmp/ftr-acceptance-20261003`；配置与 CLI 原始响应中的路径保留实际执行值，不改写证据。归档副本不含工作台控制令牌文件和运行锁。`SHA256.json` 校验最终归档文件，清单本身不自引用。首次归档后补入单来源失败探针和文档交付前的收尾证据，以最终清单为准。

文档结论可随仓库读取；源站原件、SQLite、截图及机器环境记录仅在测试机器保留，未纳入仓库。下载源码不等于持有这次实机证据，复验需在新的独立目录重建。

## 工程证据

| 文件（相对于归档目录） | 支持的结论 |
|---|---|
| logs/sync.log | 冻结依赖及 dev/web 安装成功 |
| logs/pytest.log、logs/pytest.xml、test-counts.json | 158 通过、1 跳过，各测试文件计数和详细用例 |
| logs/ruff.log、logs/mypy.log | 静态检查通过及实际检查口径 |
| logs/build.log、logs/wheel.log | sdist/wheel、源码外独立环境及 API/生命周期验证 |
| logs/setup-all.json、logs/setup-all.stderr、logs/doctor.json | 复用环境、版本/关键文件摘要、浏览器实际启动；不证明源站 |
| baseline.json、preservation-check.json | 初始代码版本、环境及跟踪文件摘要；原有 564 个业务文件、83 个跟踪文件未变化。仅本机保存，包含本地路径，不写入公共仓库 |

工程回归的实际命令：

```sh
uv sync --frozen --extra dev --extra web
FTR_WEB_BROWSER=1 uv run --frozen --no-sync --extra dev --extra web pytest -q -ra --junitxml=/tmp/ftr-acceptance-20261003/logs/pytest.xml
uv run --frozen --no-sync --extra dev --extra web ruff check src tests scripts
uv run --frozen --no-sync --extra dev --extra web mypy --ignore-missing-imports src
uv build
uv run --frozen --no-sync --extra dev --extra web python scripts/verify_wheel.py
sh -n scripts/setup.sh scripts/run.sh
git diff --check
```

setup 的原生入口会移除开发依赖；因此全套回归先执行，setup 之后为验收探针重新同步 dev/web。没有同时运行 setup 与依赖这些包的测试。

## 真实来源证据

| 文件/目录 | 支持的结论 |
|---|---|
| mof.yaml、chinatax.yaml、combined.yaml | 独立目录、来源批次预算、网络和浏览器设置 |
| logs/collect-mof.json/.stderr | 财政部首批实际结果与续跑参数 |
| logs/collect-chinatax.json/.stderr | 税务首批实际结果、附件限制 |
| logs/collect-combined.json/.stderr | 合并请求结果，两来源独立计数 |
| live/mof、live/chinatax、live/combined | 各自 SQLite 与正文、列表、附件原始证据；没有改动旧业务库 |
| mof-*.raw、chinatax-*.raw | 每来源三份官方列表响应原件副本 |
| independent-list-inventory.json | 独立读取官方原件得到的标题、链接、日期及来源总量 |
| mof-listing-evidence-metadata.json | 财政部三份列表证据都记录首页 URL 的实际证据，D-08 |
| chinatax-listing-evidence-metadata.json | 税务列表接口原件元数据 |

来源是[财政部政策发布](https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/)和[国家税务总局政策法规库](https://fgk.chinatax.gov.cn/zcfgk/c100006/listflfg.html)。本机使用已有系统代理，未变更代理；Cookie 仅由产品驻留内存，没有保存请求头或浏览器存储状态。

真实采集的命令形式：

```sh
FTR_CONFIG=/tmp/ftr-acceptance-20261003/mof.yaml \
FTR_DATA_DIR=/tmp/ftr-acceptance-20261003/live/mof \
sh scripts/run.sh collect --sources mof \
  --date-from 2026-09-21 --date-to 2026-09-30 --max-pages 3 --max-documents 10
```

税务使用 chinatax 配置与来源；合并请求使用 combined 配置和 `--sources mof chinatax`。配置的 `collection.max_duration_seconds=120` 为单次运行检查点预算。本轮没有执行这些真实任务的 resume，也没有真实 Repair/decision submit。

## 故障与接口证据

| 文件 | 支持的结论 |
|---|---|
| audit_probes.py、probe-results.json、logs/audit-probes.log | 固定样本：附件失败后新实例续跑、列表 429 原因留存、PASS/REJECT 映射；D-01/D-03/D-05 |
| request-field-probe.json | TaskRequest 错误来源字段静默忽略，D-04；仅模型校验，没有发网络请求 |
| cli_checks.py、cli-results.json、logs/cli-checks.log | 无效输入无数据写入、锁、取消、真实任务状态/研究读取 |
| read_probe.py、read-probe-results.json | CLI 普通 search 修正残留 RUNNING、数据库字节改变，D-07 |
| mixed_source_probe.py、mixed-source-results.json | 财政部成功、税务浏览器失败时总体 PARTIAL，保留已采集内容 |
| probes-run3、cli-cases | 最终探针与 CLI 场景的隔离库，不是实际政策业务库 |

固定探针修改的是外部验收脚本与替身行为，没有修改产品源码或可信回归。脚本的 ROOT 是本轮临时目录。复现时先复制脚本，将 ROOT/R 替换为**新的空白验收目录**，创建需要的日志父目录，再从安装好本项目 dev/web 环境的仓库根运行；不要直接在旧业务目录或已有探针库上重跑。`cli_checks.py` 还依赖本轮 combined 资料库与 collect JSON，用于真实只读核验；复现核心输入保护可只运行其前四项。

`request-field-probe.json` 的无网络最小复现：

```python
from ftr.models import TaskRequest
request = TaskRequest.model_validate({
    "sources": ["mof"],
    "date_from": "2026-09-21",
    "date_to": "2026-09-30",
})
print(request.source_ids)  # 实际为 ['mof', 'chinatax']
```

## 浏览器证据及收尾

| 文件 | 支持的结论 |
|---|---|
| browser_audit.py、browser/result.json | 真实资料页面 9 项流程、无 console/page error、数据库前后哈希相同 |
| browser/discovery.json、browser/initial-dom.html | 实际 DOM/控件侦察记录 |
| browser/01-library.png、02-tax-body.png、03-tax-attachment.png | 两来源资料、正文、质量限制与可下载原件 |
| browser/tax-attachment.xls | 浏览器实际下载文件，94,720 字节，哈希与证据一致 |
| browser/04-task-budget.png、05-mobile-tasks.png | 预算停止、列表未完成及 390px 窄屏 |
| failure_browser_audit.py、browser/failure-result.json | 隔离故障页正常渲染、只读，但缺 429/限流/下一步 |
| browser/06-failure-diagnosis.png、failure-events-text.txt、failure-events.json | 失败分类及原始 JSON 的实际页面/API记录，D-03 |
| logs/workbench-start.json、workbench-reuse.json | 真实服务启动、自动打开、复用 |
| logs/workbench-empty.json | 缺资料库明确提示且不初始化 |
| logs/workbench-stop.json、workbench-status-after.json | 8875 实例已停止与状态复查 |
| logs/failure-workbench-stop.json、failure-workbench-status-after.json | 8876 实例已停止与状态复查 |

截图方法应用了 webapp-testing Skill，使用本机 Python Playwright。产品 CSP 保持原样；测试用 Playwright locator 的等待断言，未放开 unsafe-eval。页面、API、下载和截图没有上传外部服务。

## 尚缺证据

- 独立 Codex 模型对自然语言用例逐条执行及工具调用轨迹；当前受指导会话不代替该评测。
- 干净 Windows/macOS 首次安装、Hermes 会话、真实新手操作及澄清/技术操作次数。
- 两来源完整区间枚举、真实中断恢复、真实结构故障 Repair，以及真实复核和有引用研究。
- 别称、错别字、多个任务“继续”的稳定识别率；当前只建立用例规格，未报告虚构百分比。
