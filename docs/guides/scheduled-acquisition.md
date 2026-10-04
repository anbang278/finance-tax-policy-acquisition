---
type: project_document
status: active
updated_at: 2026-10-03
depends_on: [../../src/ftr/scheduler.py, ../../deploy/systemd/ftr-scheduler.service]
terms: [持续获取, 定时采集, 自动补漏, Linux, systemd]
confidence: implementation_verified_locally
---

# 定时获取与自动补漏

明确授权持续采集并启用后，独立 Python 服务每天北京时间 09:00、18:00 检查两个登记栏目，每次回看含当天在内的最近 30 天。首次启动立即处理当天窗口，后续按计划执行；不依赖 Codex/Hermes 会话在线。

财政部按栏目日期，税务总局按成文日期筛选。例如文件日期 6 月 25 日、6 月 30 日才展示，后续窗口会重新发现；超过 14 天的已收录窗口内资料也重新检查。`rescan` 使用已有去重与版本机制。30 天以前的文件在今日才首次公开，可能不属于该窗口；此功能不承诺发现延迟超过窗口的全部资料。两个栏目仍不代表全国政策全集。

## 配置与命令

在已有外部配置中增加以下字段，不覆盖现有配置与数据：

```yaml
scheduler:
  enabled: true
  timezone: Asia/Shanghai
  times: ['09:00', '18:00']
  lookback_days: 30
  batch_interval_seconds: 60
  retry_delays_seconds: [300, 900, 3600]
```

```sh
ftr --config /etc/ftr/ftr.yaml config validate
ftr --config /etc/ftr/ftr.yaml scheduler preview
ftr --config /etc/ftr/ftr.yaml scheduler status
# 以下命令实际持续采集，须已获得本任务授权
xvfb-run -a /opt/ftr/.venv/bin/ftr --config /etc/ftr/ftr.yaml scheduler run
```

`preview/status` 只读，缺库不初始化；默认 `enabled: false`，关闭时 `run` 返回 DISABLED、不创建目录或任务。`run` 前台运行，stderr 为进度和批次日志，stdout 退出时为最终 JSON；停止用 Ctrl+C 或 SIGTERM。配置在启动时冻结，调整后停止并重启服务。设为 false 后重启不会创建或续跑任务；已经保存的检查点保留。

## 续跑、补漏和故障

两个来源分别创建带明确日期的冻结 TaskRequest，串行轮转，每来源最多一个活动任务和一个合并待执行窗口。达到普通批次预算后自动续跑；新时点到来合并等待窗口，不改活动任务范围。停机后恢复原检查点，并从最早遗漏时点的回看起点补到最近到期窗口；停机超过 30 天也不直接丢掉遗漏日期。旧任务已发现的正文/附件继续补齐，即使日期已移出当前窗口。

只管理定时创建的任务，不自动接管手动采集。`WAITING_DECISION` 不阻塞下一轮；正文与附件下载、可读性、语义复核分别报告。新版本须重新复核；没有新增模型调用或自动复核。

瞬态网络问题沿用请求级重试；失败批次之外最多再试 3 次，默认等待 5、15、60 分钟，耗尽暂停该来源。访问限制、结构变化、浏览器和未知故障直接暂停；正常预算耗尽不算失败，连续三批无进展才暂停。另一来源继续运行，不自动修复结构或绕过访问限制。

恢复条件确认后执行：

```sh
ftr --config /etc/ftr/ftr.yaml scheduler resume --source mof
```

该命令清除来源调度的故障暂停和重试计数，不立即发网络请求。人工 task pause 不会被清除：须先明确执行原任务的 task resume，再恢复来源。人工取消任务永不续跑；明确 scheduler resume 后只恢复未来窗口，保留原任务取消标记和历史记录。

## Linux systemd 部署

仓库提供 [服务模板](../../deploy/systemd/ftr-scheduler.service)。模板固定使用非 root 用户 ftr，代码 `/opt/ftr`、解释器 `/opt/ftr/.venv/bin/ftr`、配置 `/etc/ftr/ftr.yaml`；实际数据目录建议 `/srv/ftr/data`。部署者按自身机器调整这些绝对路径、用户和权限。

1. 在服务器按[安装指南](installation.md)准备锁定 Python 环境、Chromium 及其系统库、Xvfb/xauth；以服务用户验证浏览器可启动。仅设置 DISPLAY 或 doctor 就绪不证明税务源站准入。
2. 为 ftr 用户准备可写的持久化数据目录与可读配置，配置 `browser.headless: false`、`scheduler.enabled: true`；代理按服务器实际网络设置，不复用本机回环代理。
3. 先执行 config validate、scheduler preview，取得真实采集及常驻部署授权后，安装模板并启动：

```sh
sudo install -m 0644 deploy/systemd/ftr-scheduler.service /etc/systemd/system/ftr-scheduler.service
sudo systemctl daemon-reload
sudo systemctl enable --now ftr-scheduler.service
sudo systemctl status ftr-scheduler.service
sudo journalctl -u ftr-scheduler.service -n 100
```

4. 停止使用 `sudo systemctl stop ftr-scheduler.service`；配置变更后 `sudo systemctl restart ftr-scheduler.service`。模板异常退出 30 秒后重启，并设置启动频率限制。来源故障暂停保存于数据库，进程重启不会清除。

模板不自动安装、创建系统用户、部署工作台或执行采集。工作台按已有安装方式单独启动，默认回环监听；本功能不增加公网访问或认证能力。

## 观察、备份和升级

工作台“采集监控”的定时区域及 `/api/scheduler` 展示当前工作台配置、最近调度配置、计划时点、来源窗口/检查页数/缺失项、积压、异常与建议。配置不一致时提示核对并重启。心跳每 15 秒更新，90 秒内且 running=true 才显示新鲜；过期不证明在线，计算的下一时点不证明执行过。计划检查水位和进程心跳是不同概念。

SQLite schema v3 增量加入 scheduler_meta/scheduler_sources；首次明确写操作迁移，普通只读不迁移。备份包含冻结请求、任务关联、窗口、重试和暂停状态；`.scheduler.lock`、`.scheduler-heartbeat.json` 是本机临时状态，不进入备份，恢复后重新启动。版本高于当前程序拒绝写入；旧程序不能写 v3 库，回退旧代码须在新目录恢复升级前备份。

单目录只允许一个调度服务和一个实际 CLI 写入者。定时独立锁防重复进程；每批取得原有写锁，批间释放。手动采集占锁时延后 60 秒，不增加失败次数。不要删除锁文件。

本机隔离测试不能代替 Linux systemd 安装、开机恢复、两个真实来源的准入和真实时点验收。上线前分别检查这些条件；本轮未执行云端部署或真实采集。

## 本机工作台持久计划

网页管理保存后，同一数据库持久计划优先于 YAML，CLI 与后台状态统一显示配置来源。管理后台与独立 scheduler 共用进程锁，拒绝隐式接管；不改变 systemd。详见 [本机管理](local-management.md)。
