#!/bin/sh
set -eu
unset PYTHONHOME PYTHONPATH
ftr_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ftr_python="$ftr_root/.venv/bin/python"
[ -x "$ftr_python" ] || { echo '运行环境不存在，请先执行 scripts/setup.sh' >&2; exit 3; }
ftr_config=${FTR_CONFIG:-"$ftr_root/ftr.local.yaml"}
exec "$ftr_python" -m ftr.cli --config "$ftr_config" "$@"
