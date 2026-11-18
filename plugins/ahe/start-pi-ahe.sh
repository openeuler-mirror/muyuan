#!/usr/bin/env bash
# 单次拉起一个只带 /ahe 的隔离 Pi 会话（不依赖 Pi 插件注册）。
# 日常使用推荐先注册为 Pi 插件，之后正常启动 Pi 即可用 /ahe：
#   python3 plugins/ahe/install.py --pi
#
# 用法:
#   bash plugins/ahe/start-pi-ahe.sh                # 交互模式，拉起后直接输入 /ahe ...
#   bash plugins/ahe/start-pi-ahe.sh -- --mode rpc  # 透传额外 pi 参数
#
# 拉起后在 Pi 里直接输入（evolve/eval/debugger 自动复用同一份 API 配置）:
#   /ahe 优化harness, harness路径为/path/to/harness,
#       提示词为任务描述（或 eval任务为<benchmark>里面的<项目>）,
#       指标为token降30%, 时间降20%, 准确率不变, 3轮
#
# 环境变量（全部可选）:
#   PI_BIN        pi 可执行文件，默认 "pi"（可设为 node+cli.js 的完整命令）
#   PI_PROVIDER   提供商，默认 csi-provider
#   PI_MODEL      模型，默认 GLM-5.3
#   AHE_ENV_FILE  API env 文件；存在则 source（ahe.py 未设 LLM_* 时也有
#                 自己的内置回退，见 load_api_config）
set -euo pipefail

AHE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTENSION="$AHE_ROOT/assets/pi/extensions/ahe.ts"
PI_BIN="${PI_BIN:-pi}"
PI_PROVIDER="${PI_PROVIDER:-csi-provider}"
PI_MODEL="${PI_MODEL:-GLM-5.3}"

# evolve/eval/debugger 的 LLM API：优先用已导出的 LLM_* 环境变量，
# 其次 source AHE_ENV_FILE（若设置且存在）。
if [ -z "${LLM_BASE_URL:-}" ] && [ -n "${AHE_ENV_FILE:-}" ] && [ -f "$AHE_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$AHE_ENV_FILE"
    set +a
fi

# "--" 之后的所有参数原样透传给 pi（--mode rpc / --thinking xhigh / --no-session 等）
if [ "$#" -gt 0 ] && [ "$1" = "--" ]; then
    shift
fi

exec $PI_BIN \
    --provider "$PI_PROVIDER" \
    --model "$PI_MODEL" \
    --no-extensions \
    --extension "$EXTENSION" \
    "$@"
