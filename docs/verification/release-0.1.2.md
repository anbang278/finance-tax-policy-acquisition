# 0.1.2 发布检查

日期：2026-10-22。用户明确授权提交 GitHub 版本；实现基线为 096c7ea（0.1.1）。

版本范围：知识加工与领域 Wiki，功能与未验证项见 [验收记录](knowledge-wiki-acceptance.md)。六处版本源（包配置、包常量、两份插件清单、锁文件、README）同步为 0.1.2。

两处既有用户链接修改保留在原工作区，并从提交中排除。旧嵌套仓库不改动；不升级真实资料库，不执行实际资料复核、模型调用、政策采集或部署。

## 本机发布前验证

- 完整 Chromium 回归：**347 passed / 1 skipped / 1 warning，63.07 秒**。唯一跳过为 Windows 原生入口用例；警告为已有 Starlette TestClient 上游弃用提示。
- ruff 检查、mypy（42 个源文件）、27 个改动 Python 文件格式检查、wiki.js 语法与暂存差异检查全部通过。
- 六处版本源均为 0.1.2；uv.lock 只变更本项目版本，不改第三方依赖。锁定依赖安装通过。
- `uv build --offline` 和源码目录外独立 wheel 验证通过：CLI、配置模板、Schema、新静态资源、只读 API、本人管理会话及后台。
- 暂存变更安全扫描未发现硬编码密钥、危险执行、序列化或私人绝对路径；两处 SQL 表名插值经独立审查确认使用固定表名。

独立代码审查 **PASS**。首轮发现并修复四项问题：无效已保存结果阻塞恢复队列、迟到 API 失败覆盖已完成状态、正式检索误用草稿/旧标题、生效与失效日期复用提醒身份。新增 12 个隔离边界场景；修复前 11 失败/1 通过，修复后全部通过。独立审查员另行运行 12 项针对性回归通过；第二轮无安全或逻辑阻塞。

## GitHub 交付与更新检测

- 功能提交：`dab7c235421670f286b97ea860c3292f54e21c25`。
- [PR #1](https://github.com/anbang278/finance-tax-policy-acquisition/pull/1) 已合入官方 main，合并提交 `989123c1b70c61c63a7d11b094d4c4926e113fab`；已核对合并树与被审查、测试的功能树一致。
- [PR 三平台 CI](https://github.com/anbang278/finance-tax-policy-acquisition/actions/runs/39766848271) completed/success：macOS **347 passed / 1 skipped**（94.61 秒），Ubuntu **346 passed / 2 skipped**（82.93 秒），Windows **343 passed / 5 skipped**（241.79 秒）。三平台均通过 ruff、mypy、构建与源码目录外 wheel；Windows 同时执行原生入口及 PowerShell 语法验证。跳过不计通过。
- 已实际回读官方 main 提交及 pyproject 版本 **0.1.2**；在干净检出、临时缓存中以默认超时实际检查：新版本 `UP_TO_DATE`，0.1.1 基线身份 `UPDATE_AVAILABLE`。没有初始化业务数据库。

本次收尾提交仅更新文档，使用 `[skip ci]`；上述功能与测试源码不变，工程证据对应 `dab7c23`。合入 main 自动触发的相同源码复跑不替代或扩大上述已完成的 PR 验证范围。

正式下载制品在本记录提交后，从干净 main 重建，并以最终提交写入构建身份。发布时同时提供 sdist、wheel、SHA256SUMS 与 release-verification.json；后者记录实际构建提交、更新检测与制品摘要。发布后回读 tag/main 并下载资产核对摘要，不携带本机两处既有用户修改。
