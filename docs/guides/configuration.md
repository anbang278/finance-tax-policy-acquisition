---
type: project_document
status: active
updated_at: 2026-10-02
depends_on: [../../src/ftr/config.py, ../../ftr.example.yaml]
terms: [RuntimeSettings, 配置优先级, 运行预算]
confidence: implementation_verified_locally
---

# 运行参数配置

配置描述机器和运行参数，`TaskRequest` 描述某次业务任务；来源身份和允许主机仍由独立来源信任表管理。所有默认值定义在 `RuntimeSettings` 中，示例模板须与其保持一致。

## 加载与覆盖

```sh
ftr --config /绝对路径/ftr.yaml config validate
ftr --config /绝对路径/ftr.yaml config show
ftr --config /绝对路径/ftr.yaml --data-dir /另一资料目录 serve --port 9000
```

全局参数在子命令之前。`--config` 优先于 `FTR_CONFIG`；两者均未提供时使用内置默认值，不搜索当前目录中的文件。

字段覆盖顺序为显式 CLI 参数 → 环境变量 → YAML → 内置默认值。每层都校验，错误 YAML 不能靠更高优先级值掩盖。文件顶层必须是映射，未知字段和重复键被拒绝；未知 `FTR_*` 环境变量也被拒绝，`FTR_WEB_BROWSER` 是测试专用例外。旧 `FTR_ALLOW_TEST_NET_DNS` 已废弃，不可继续设置。

YAML 的 `data_dir` 与 `browser.executable_path` 相对路径基于配置文件目录；环境变量和 CLI 相对路径基于启动目录。`~` 展开为当前用户目录，不对字符串执行 shell 或替换任意环境变量。建议宿主与服务使用绝对 `FTR_CONFIG` 路径。

配置进程内固定，不支持热加载。`config show/validate` 不创建数据目录、不初始化数据库、不访问政策源站；输出配置来源 `default/yaml/env/cli`，代理用户名及密码隐藏为 `***`。

## 完整参数表

| YAML 字段 | 环境变量 | 默认值 | 校验与作用 |
|---|---|---|---|
| `data_dir` | `FTR_DATA_DIR` | `~/.local/share/ftr` | 非空路径；CLI `--data-dir` 可覆盖 |
| `network.proxy_mode` | `FTR_NETWORK__PROXY_MODE` | `system` | `system/direct/explicit` |
| `network.proxy_url` | `FTR_NETWORK__PROXY_URL` | `null` | explicit 必填；HTTP/HTTPS 地址，可带基本认证；其他模式必须为空 |
| `network.timeout_seconds` | `FTR_NETWORK__TIMEOUT_SECONDS` | `30` | 有限正数；HTTP 与会话请求超时 |
| `network.interval_seconds` | `FTR_NETWORK__INTERVAL_SECONDS` | `2` | 有限非负数；同来源客户端请求间隔 |
| `network.max_file_bytes` | `FTR_NETWORK__MAX_FILE_BYTES` | `50000000` | 正整数；正文、列表、附件单响应字节上限 |
| `browser.headless` | `FTR_BROWSER__HEADLESS` | `false` | 布尔值；不因失败自动切换模式 |
| `browser.executable_path` | `FTR_BROWSER__EXECUTABLE_PATH` | `null` | 非空路径或空值；空值时自动验证已有 Chromium、Edge、Chrome；显式路径失效不替换 |
| `browser.timeout_seconds` | `FTR_BROWSER__TIMEOUT_SECONDS` | `30` | 有限正数；浏览器启动、导航及等待列表响应超时 |
| `collection.max_pages` | `FTR_COLLECTION__MAX_PAGES` | `1000` | 整数 1–1000；CLI `--max-pages` 可覆盖 |
| `collection.max_documents` | `FTR_COLLECTION__MAX_DOCUMENTS` | `1000` | 整数 1–1000；计新增资料版本，CLI `--max-documents` 可覆盖 |
| `collection.max_duration_seconds` | `FTR_COLLECTION__MAX_DURATION_SECONDS` | `1800` | 有限正数；单次执行批次时间预算 |
| `collection.max_bytes_per_source` | `FTR_COLLECTION__MAX_BYTES_PER_SOURCE` | `500000000` | 正整数；每来源、本次批次的保存正文及附件字节预算 |
| `web.host` | `FTR_WEB__HOST` | `127.0.0.1` | IPv4/IPv6 地址或 localhost；CLI `serve --host` 可覆盖 |
| `web.port` | `FTR_WEB__PORT` | `8765` | 整数 1–65535；CLI `serve --port` 可覆盖 |
| `web.poll_interval_ms` | `FTR_WEB__POLL_INTERVAL_MS` | `5000` | 整数，至少 100；可见页面轮询间隔 |
| `web.request_timeout_ms` | `FTR_WEB__REQUEST_TIMEOUT_MS` | `10000` | 整数，至少 100；前端查询超时 |

| `network.retry_attempts` | `FTR_NETWORK__RETRY_ATTEMPTS` | `3` | 整数 1–5；连接失败、超时、502/503/504 的最多尝试次数，含首次；Repair 限额优先 |
| `network.retry_backoff_seconds` | `FTR_NETWORK__RETRY_BACKOFF_SECONDS` | `1` | 有限正数，至多 30；重试指数退避基数，额外抖动 0–0.25 秒 |
| `collection.consecutive_failure_limit` | `FTR_COLLECTION__CONSECUTIVE_FAILURE_LIMIT` | `3` | 整数 1–10；同来源连续详情瞬态失败阈值，成功后归零 |
| `collection.progress_interval_seconds` | `FTR_COLLECTION__PROGRESS_INTERVAL_SECONDS` | `30` | 有限正数；stderr 定期进度间隔，显示最后检查点，不作为全量进度百分比 |
| `web.startup_timeout_seconds` | `FTR_WEB__STARTUP_TIMEOUT_SECONDS` | `15` | 有限正数，至多 120；工作台启动总截止时间，区别于页面请求超时 |

数字环境变量用十进制值；布尔值用 `true/false`。路径、地址和代理 URL 按字符串读取；可选 proxy_url/executable_path 的环境变量空字符串表示清除配置值。切换显式代理为 direct/system 时须同时清除继承的 proxy_url。单响应限额与批次预算是不同口径；运行时间按工作检查点评估，正在执行的请求受请求超时控制，并非强制在预算秒数处杀进程。列表原件保存，但现有批次字节计数只统计正文与附件。

## 任务冻结与续跑

新任务将生效的页数和资料数预算写入 `TaskRequest`；`task resume` 继续使用任务原值、来源、日期口径和范围。网络、浏览器、执行时间与字节预算采用续跑时配置，每次记录脱敏 `runtime_config` 审计事件。

`collect --request request.json` 使用 JSON 中的任务定义，不用环境配置改写其任务字段；运行参数照常生效。它不能与来源、日期、模式、预算或幂等 CLI 参数混用。

## 代理模式

- `system`：启动时读取系统/环境 HTTPS 代理（Python `getproxies()` 的 `https` 项）；无代理则直连。有效代理固定注入 HTTP、requests 和 Chromium，后续不由客户端另读代理环境；不应用 `NO_PROXY` 分流。
- `direct`：忽略系统与环境代理，显式直连。
- `explicit`：使用 `proxy_url` 指定 HTTP/HTTPS 代理，可指定其他机器上的代理；浏览器基本认证由独立字段传入。

环境已有不支持的 SOCKS 代理时，改用 HTTP/HTTPS 代理或显式 direct，不能静默降级。修改代理后重启进程。URL 白名单、HTTPS、逐跳重定向和非公网目标拒绝继续生效；只有实际使用回环 IP 代理时才允许登记域名解析到 `198.18.0.0/15` 映射网段。远端代理不享有该例外。代理服务器在内网与被采集目标在内网是两个不同概念。

真实凭据优先通过进程环境提供，不写入仓库模板。配置展示及运行审计隐藏凭据；排障时不要开启会暴露请求认证信息的第三方详细日志。

## 场景示例

本地换机只改数据目录即可，默认使用系统代理与有界面浏览器。Windows YAML 路径可用 `C:/FTR/data`，避免双引号字符串中的反斜杠转义。

Linux 团队服务器可以使用以下配置，再通过 `xvfb-run` 执行税务采集：

```yaml
data_dir: /srv/ftr/data
network:
  proxy_mode: direct
browser:
  headless: false
web:
  host: 0.0.0.0
  port: 8765
```

工作台启动后，能访问服务器监听地址的成员即可查看和下载资料，本次不提供登录。日期范围、允许来源、语义复核准入、HTTPS、哈希校验、发布阻断、选择器与来源动态栏目参数均不属于可关闭的运行参数。
