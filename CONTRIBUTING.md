# 贡献指南

开始修改前请阅读 [Agent 工作契约](AGENTS.md)、[开发说明](docs/development/development.md)和[回归要求](tests/REGRESSION.md)。本仓库从根目录安装、运行、测试和构建：

```sh
uv sync --frozen --extra dev --extra web
uv run --extra dev --extra web pytest -q
uv run --extra dev --extra web ruff check src tests scripts
uv run --extra dev --extra web mypy --ignore-missing-imports src
uv build
python scripts/verify_wheel.py
```

需要真实 Chromium 交互用例时，先执行 `uv run playwright install chromium`，再运行：

```sh
FTR_WEB_BROWSER=1 uv run --extra dev --extra web pytest -q
```

提交说明应区分代码实现、自动化验证、真实来源与宿主验收。不要将跳过测试、CI 配置存在或本地 `doctor` 状态描述成跨环境或生产验收通过。提交和推送按用户明确授权执行。
