#!/usr/bin/env bash
# 取消 /ahe 注册（只移除对应条目/文件，不删除其他任何东西）：
#   bash uninstall.sh --pi      # 移除 Pi settings.json 里的 AHE 注册
#   bash uninstall.sh --codex   # 删除 ~/.codex/prompts/ahe.md
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$script_dir/install.py" --uninstall "$@"
