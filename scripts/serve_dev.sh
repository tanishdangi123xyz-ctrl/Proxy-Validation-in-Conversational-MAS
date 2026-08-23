#!/usr/bin/env bash
# Development llama-server. No energy number from this machine means anything.
#
# Context size and server flags come from config.py rather than from defaults
# written here. A dry run measured at a context the campaign will not use sizes
# nothing, and a flag that lives only in this file is a frozen parameter that
# config_hash() never sees.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

MODEL="${1:-}"
CTX="${2:-}"
PORT="${3:-}"

if [ -z "$MODEL" ]; then
  echo "usage: $0 <path-to-native-precision.gguf> [ctx] [port]" >&2
  exit 1
fi

case "$MODEL" in
  *Q4*|*Q5*|*Q6*|*Q8*|*q4*|*q5*|*q6*|*q8*)
    echo "REFUSING: '$MODEL' looks quantized. This study is native precision." >&2
    exit 1 ;;
esac

read -r CFG_CTX CFG_PORT <<EOF
$(PYTHONPATH="$ROOT/src" python3 -c 'from masenergy import config; print(config.CTX_SIZE, config.SERVER_PORT)')
EOF
CTX="${CTX:-$CFG_CTX}"
PORT="${PORT:-$CFG_PORT}"
KV="$(PYTHONPATH="$ROOT/src" python3 -c 'from masenergy import config; print(config.KV_CACHE_TYPE)')"

# LLAMA_FLAGS is the frozen list. Read it rather than restating it.
# A read loop, not mapfile: macOS still ships bash 3.2 and this script is run
# on the laptop as often as on the Jetson.
FLAGS=()
while IFS= read -r flag; do
  [ -n "$flag" ] && FLAGS+=("$flag")
done < <(PYTHONPATH="$ROOT/src" python3 -c \
  'from masenergy import config; print("\n".join(config.LLAMA_FLAGS))')

echo "development server only. no energy number from this machine means anything."
echo "model $MODEL"
echo "ctx   $CTX  (config.CTX_SIZE is $CFG_CTX)"
echo "flags ${FLAGS[*]}"
if [ "$CTX" != "$CFG_CTX" ]; then
  echo "WARNING: serving at ctx $CTX, not the campaign's $CFG_CTX." >&2
  echo "         Screening candidates with longer prompts is the only reason to." >&2
fi

exec llama-server \
  --model "$MODEL" \
  --ctx-size "$CTX" \
  --port "$PORT" \
  --cache-type-k "$KV" \
  --cache-type-v "$KV" \
  "${FLAGS[@]}"
