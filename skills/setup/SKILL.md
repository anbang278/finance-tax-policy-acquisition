---
name: setup
description: 财税采集或工作台首次使用前检测并自动补装用户级运行依赖，返回绝对运行路径。
---

# 首次环境准备

从本 Skill 的实际目录向上定位含 `pyproject.toml`、`uv.lock` 和 `scripts/setup.*` 的插件根目录，不使用作者机器路径，不把宿主工作目录当插件根目录。wheel 仅提供 CLI，宿主接入须完整插件目录。

按用户意图选择能力：仅财政部 mof；税务 chinatax；仅查已采集资料 workbench；两来源并查询 all。先检查命令存在与 Python 版本，再调用下面入口处理缺失依赖；已有环境可以复用，但仍应核对配置和所需能力。

- macOS：`sh "插件根/scripts/setup.sh" mof|chinatax|workbench|all`（实际只选一个参数）。
- Windows：在 PowerShell 调用 `& "插件根\scripts\setup.ps1" -Capability mof|chinatax|workbench|all`。若策略阻止脚本，解释限制并由用户按组织规则处理，不擅自永久修改执行策略。
- Linux：同 shell 入口；缺 uv 或系统库/Xvfb 时展示安装指南中的管理员步骤，不自动 sudo。税务须通过部署者已有 Xvfb/显示环境启动，不擅自改成无界面。

入口自动补装缺失的用户级 uv、独立 Python 3.13、项目锁定依赖及税务所需浏览器；优先验证已有 Playwright Chromium、Edge、Chrome，均不可用才下载 Chromium，显式配置失效不替换。不会修改 shell 配置、系统 Python、全局代理或已有配置。下载/权限/依赖失败保留现场并说明阻塞，不通过删除环境或更改依赖版本解锁。

读取最后一条 JSON 的 state、stage、error_code、next_step、runner、version、python、config、data_dir、checks 和 browser。只有 ENVIRONMENT_READY 才继续对应能力。后续优先用返回的绝对 runner（macOS shell、Windows PowerShell），它隔离 PYTHONHOME/PYTHONPATH、固定解释器与配置且不重复安装；直接命令调用时也须仅在子进程中隔离这两个变量，使用绝对 `python -m ftr.cli --config config`，需要覆盖资料目录时显式 `--data-dir`。不依赖 PATH 中 ftr，不再执行会移除额外依赖的默认 uv run。

区分依赖安装、浏览器启动、源站可访问。setup 不访问政策源站或初始化资料库。原请求是采集时回到 Controller 继续原请求；原请求只安装/查询时不采集。安装不授权宿主插件安装、真实采集或开放局域网。
