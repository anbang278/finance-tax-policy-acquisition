#!/bin/sh
# macOS/Linux: local runtime only. No profile, system Python or proxy edits.
set -eu
unset PYTHONHOME PYTHONPATH
ftr_stage=bootstrap
ftr_reported=0
ftr_tmp=''
ftr_cleanup() {
  ftr_code=$?
  if [ -n "$ftr_tmp" ]; then rm -rf "$ftr_tmp"; fi
  if [ "$ftr_code" -ne 0 ] && [ "$ftr_reported" -eq 0 ]; then
    printf '{"state":"BLOCKED","stage":"%s","error_code":"INSTALLATION_FAILED","next_step":"检查下载网络、权限与锁定依赖后重试；保留已有配置"}\n' "$ftr_stage"
  fi
}
trap ftr_cleanup EXIT
trap 'exit 130' HUP INT TERM
ftr_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ftr_capability=${1:-all}
case "$ftr_capability" in mof|chinatax|workbench|all) ;; *) echo '能力须为 mof/chinatax/workbench/all' >&2; exit 2 ;; esac
ftr_uv=$(command -v uv || true)
if [ -z "$ftr_uv" ] && [ -x "$HOME/.local/share/ftr/bootstrap/bin/uv" ]; then
  ftr_uv="$HOME/.local/share/ftr/bootstrap/bin/uv"
fi
if [ -z "$ftr_uv" ]; then
  [ "$(uname -s)" = Darwin ] || { echo 'Linux 缺 uv：请按官方安装指南完成用户级安装后重试；系统依赖由部署者处理。' >&2; exit 3; }
  case "$(uname -m)" in
    arm64) ftr_target=aarch64-apple-darwin; ftr_sha=3f61099e261e449527141dbf125629fab33ad696468c8c90cebbac40185a306c ;;
    x86_64) ftr_target=x86_64-apple-darwin; ftr_sha=76638fdcfa91357858771551a1c88de1f7c3b270b33ab1866f8a0618d9e442d8 ;;
    *) echo '不支持的架构' >&2; exit 3 ;;
  esac
  ftr_bin="$HOME/.local/share/ftr/bootstrap/bin"
  mkdir -p "$ftr_bin"
  ftr_tmp=$(mktemp -d)
  curl --fail --location --silent --show-error "https://github.com/astral-sh/uv/releases/download/0.8.22/uv-$ftr_target.tar.gz" -o "$ftr_tmp/uv.tar.gz"
  ftr_actual=$(shasum -a 256 "$ftr_tmp/uv.tar.gz" | cut -d ' ' -f 1)
  [ "$ftr_actual" = "$ftr_sha" ] || { echo 'uv 校验失败，禁止执行' >&2; exit 3; }
  tar -xzf "$ftr_tmp/uv.tar.gz" -C "$ftr_tmp"
  cp "$ftr_tmp/uv-$ftr_target/uv" "$ftr_bin/uv"
  chmod 700 "$ftr_bin/uv"
  ftr_uv="$ftr_bin/uv"
fi
cd "$ftr_root"
ftr_stage=dependencies
# uv sync downloads missing Python into managed storage, without installing a global python command.
if [ "$ftr_capability" = workbench ] || [ "$ftr_capability" = all ] || { [ -x "$ftr_root/.venv/bin/python" ] && "$ftr_root/.venv/bin/python" -c "import fastapi, uvicorn" 2>/dev/null; }; then
  UV_PROJECT_ENVIRONMENT="$ftr_root/.venv" "$ftr_uv" sync --frozen --no-dev --python 3.13 --extra web
else
  UV_PROJECT_ENVIRONMENT="$ftr_root/.venv" "$ftr_uv" sync --frozen --no-dev --python 3.13
fi
ftr_stage=verification
set +e
ftr_output=$("$ftr_root/.venv/bin/python" "$ftr_root/scripts/setup_runtime.py" "$ftr_capability")
ftr_exit=$?
set -e
if [ -n "$ftr_output" ]; then printf '%s\n' "$ftr_output"; ftr_reported=1; fi
exit "$ftr_exit"
