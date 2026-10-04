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

## 首次配置与运行可靠性增量（2026-10-02）

新增 test_reliability.py 覆盖 HTTP 故障注入、连续失败与续跑、Repair 实际请求计数、进度/信号/硬退出、工作台包装 PID 与慢启动、历史隔离原因及 PDF 警告。test_setup.py 增加污染环境和运行入口透传；三平台 CI 解析 setup.ps1/run.ps1，Windows 执行原生入口用例。独立 wheel 检查覆盖新增模块、参数与服务实例身份。

完整命令及隔离边界沿用上文；本机通过不代替新远程 CI、干净桌面安装或真实源站。脚本读取 UTF-8；源码包下载、依赖下载与实际浏览器启动分别记录。

## 体验升级回归（2026-10-03）

新增 test_usability_upgrade.py，覆盖八项审计缺陷的行为回归及空日期、附件新版本、迁移与只读兼容。完整浏览器模式另检查 429 中文原因/下一步、未知范围、日期未知分组和窄屏。可选测试制品目录用 POLICY_AUDIT_EVIDENCE（仅测试使用，不是运行参数）；不得引入未知 FTR_ 环境变量绕过配置校验。

## 定时采集回归（2026-10-03）

test_scheduler.py 覆盖冻结时钟/停机补漏/轮转/三轮追加重试/暂停取消/幂等崩溃恢复/受控来源版本与附件；实际子进程验证双调度拒绝、SIGTERM 和硬退出锁释放。进程测试持有写锁防止真实访问。FTR_WEB_BROWSER=1 另运行定时监控 Chromium/窄屏与只读字节检查；完整命令沿用本页。SQLite 当前 v4 的迁移预期与 wheel 检查同步；新远程三平台 CI 和 systemd 云端不由本机结果替代。

## 本机管理增量（2026-10-03）

test_management.py 覆盖受保护会话、持久队列、期望版本、原子事务回放、单写者/实例握手、草稿/复核门禁/新一轮/补取/备份。完整 Chromium 模式包含桌面和390px完整管理流程及关闭工作台后后台继续；每个实际子进程测试在 finally 中只停止本测试认证实例。

## GitHub 更新检测（2026-10-04）

test_update_check.py 使用 HTTP 替身和临时缓存验证提交关系、离线/限流/总截止时间、代理脱敏、失败不阻断、只读入口、并发缓存与工作台运行身份。tests/conftest.py 默认禁用子进程自动联网、隔离本进程缓存并替换检测网络；专用测试显式启用，不访问政策源站。FTR_WEB_BROWSER=1 覆盖桌面/390px提醒、伪时钟长期刷新、重启/过期/失败状态。verify_wheel.py 额外比对 sdist→wheel 构建身份并验证独立安装/只读更新 API；真实 GitHub 连通性另用临时缓存抽查。

浏览器全流程另覆盖受控启停/立即获取/来源操作/补取版本跳转；回归发现并修复来源按钮事件与原来源弹窗冲突、嵌套 SQLite 快照导致的并发等待。测试按来源 ID 定位，不依赖 SQLite 行顺序。
