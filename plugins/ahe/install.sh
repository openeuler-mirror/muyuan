#!/usr/bin/env bash
# 一次性安装 /ahe：
#   bash install.sh --pi      # 注册为 Pi 插件（默认，之后所有 Pi 自带 /ahe）
#   bash install.sh --codex   # 安装为 Codex 自定义 prompt（之后所有 Codex 自带 /ahe）
# 卸载用：bash uninstall.sh --pi / bash uninstall.sh --codex
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$script_dir/install.py" "$@"
