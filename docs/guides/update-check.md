# 项目更新检测

项目运行前会检查官方 `anbang278/finance-tax-policy-acquisition` 的 `main` 分支；这是代码更新提醒，和政策资料的更新、复核是独立概念。提醒后继续原任务，不自动下载或覆盖代码。

## 使用入口

macOS/Linux：`sh scripts/run.sh update check`；Windows：`& .\scripts\run.ps1 update check`。直接安装 wheel 的用户可执行 `ftr update check`。追加 `--force` 绕过普通缓存，GitHub 明确限流时仍等待服务端指定时间。

setup 完成后，以及 collect、task resume、workbench start、serve、已启用 scheduler run 的启动阶段自动检测。帮助、配置校验、状态查询、暂停和停止不发起检测。成功结果默认缓存 24 小时，失败退避 1 小时，每轮联网最多等待 3 秒。首次离线检查可能等待至截止时间，但原任务继续执行。

工作台顶部显示本地版本与状态，有更新时显示官方项目链接。后台每分钟检查缓存是否到期；页面每分钟读取快照，刷新和重新进入页面也会读取。API `GET /api/update-status` 不联网、不写业务 SQLite。磁盘代码发生变化时提示重启；正在运行的进程仍按启动身份展示。

## 结果解释

| state | 意义 |
|---|---|
| UP_TO_DATE | 本地基准提交与 main 相同；dirty=true 时仍存在本地修改 |
| UPDATE_AVAILABLE | 官方 main 在提交关系上领先本地 |
| LOCAL_AHEAD | 本地提交领先官方 main |
| DIVERGED | 本地和 main 已分叉，更新前需人工核对 |
| UNKNOWN | 缺本地身份或无法证明提交关系，不能确认是否最新 |
| CHECK_FAILED | 网络、限流、缓存竞争等使检查未完成 |
| DISABLED | 已关闭自动及手动联网检测 |

Git 安装比较提交号与祖先关系，不根据本地 origin 切换比较目标，也不执行 fetch。版本号相同可能存在不同提交。新 sdist/wheel 内包含构建提交、包版本和构建时修改标记；后者为 true 时只能说明基准提交关系，不能证明构建代码与远端完全一致。普通 GitHub Download ZIP、旧安装包缺身份时显示 UNKNOWN。

CLI stdout 保持 OperationResponse JSON，更新检测结果位于 data；stderr 承载提醒。检测结果不改变原业务退出码。setup 报告的 update_check 与环境就绪独立。checked_at 是 UTC Unix 秒数；cached 表示使用缓存，stale 表示当前检查未成功，last_success 保留上次成功结果，不能视为当前确认。

## 配置与故障

```yaml
update_check:
  enabled: true
  cache_seconds: 86400
  failure_retry_seconds: 3600
  timeout_seconds: 3
```

对应环境变量为 `FTR_UPDATE_CHECK__ENABLED`、`FTR_UPDATE_CHECK__CACHE_SECONDS`、`FTR_UPDATE_CHECK__FAILURE_RETRY_SECONDS`、`FTR_UPDATE_CHECK__TIMEOUT_SECONDS`。配置默认和覆盖顺序沿用现有运行配置；不改变 TaskRequest。

使用匿名 GitHub API，复用 network 的 system/direct/explicit 代理模式，不发送政策网站 Cookie，也不需要 PAT。错误只返回固定分类，不记录代理密码或服务器响应原文。禁止重定向到其他主机。

缓存独立于资料目录：macOS `~/Library/Caches/ftr/update-check/`，Windows `%LOCALAPPDATA%\ftr\update-check\`，Linux `$XDG_CACHE_HOME/ftr/update-check/`（未设则 `~/.cache/`）。每个安装路径使用独立键、原子状态文件和非阻塞锁。无缓存写权限时仍可检查；锁冲突等待下次检测，不删除锁。源码身份变化后重新比较。检查不初始化资料库，缓存不进入业务备份。

实际更新由用户另行授权并按备份、配置保留和 Git 协作流程执行。本期没有更新执行按钮或自动更新命令。
