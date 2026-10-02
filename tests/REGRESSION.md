# 必跑回归与验证边界

在仓库根目录运行：

```sh
uv sync --frozen --extra dev --extra web
uv run --extra dev --extra web pytest -q
uv run --extra dev --extra web ruff check src tests scripts
uv run --extra dev --extra web mypy --ignore-missing-imports src
uv build
python scripts/verify_wheel.py
```

完整浏览器回归需要 Playwright Chromium：

```sh
uv run playwright install chromium
FTR_WEB_BROWSER=1 uv run --extra dev --extra web pytest -q
```

PowerShell：

```powershell
$env:FTR_WEB_BROWSER = '1'
uv run --extra dev --extra web pytest -q
Remove-Item Env:FTR_WEB_BROWSER
```

测试全部使用临时目录和隔离数据库，不启动真实政策采集。格式检查针对本次修改文件执行 `ruff format --check`；既有 `test_proxy_and_status.py` 的格式问题须单独记录，不为当前任务批量改写。

| 测试 | 关键行为 |
|---|---|
| `test_core.py` | 来源解析、日期/类型、幂等、资料版本、复核摘要、引用、补丁保护、暂停与恢复 |
| `test_proxy_and_status.py` | 既有回环代理映射网段边界、代理快照、状态只读、请求异常 |
| `test_config.py` | 覆盖与相对路径、未知/重复键、无效输入无副作用、脱敏、运行预算 |
| `test_portability.py` | 跨进程锁与退出释放、旧 Windows 备份、路径越界、代理统一、浏览器参数、无显示环境、续跑审计、局域网 API |
| `test_web.py` | 最新版本、筛选、队列口径、只读并发、原件下载、内容转义和浏览器保位/恢复/窄屏 |

不得为了通过测试修改业务预期或绕过安全门禁。Repair 不得修改可信回归。跳过、缺环境和 mock 通过必须明确区分；CI 与 Linux Xvfb 实采结果不由本机测试代替。

## 新增伙伴交接回归（2026-10-02）

- test_setup.py：配置保护、仅财政部/工作台不安装 Chromium、浏览器安装失败、带中文空格路径的 shell 引导；安装命令使用替身，不证明全新设备下载成功。
- test_rule_repair.py：两来源固定样本与故障注入、拒绝规则代码与越界、候选预算、制品/报告篡改、启用续跑、回退、中断恢复、备份规则；网络使用受控替身，不证明真实源站。
- test_workbench_lifecycle.py：真实本机子进程、健康检查、端口冲突、复用、身份不匹配拒绝、停止与数据库字节不变。

wheel 独立验证同时检查打包规则样本、规则解析和工作台生命周期；Windows CI 解析 PowerShell 引导脚本。四种桌面宿主组合的实机安装、模型调用和真实源站访问单独验收。
