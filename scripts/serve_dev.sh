#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-}"
CTX="${2:-8192}"
PORT="${3:-8080}"

if [ -z "$MODEL" ]; then
  echo "usage: $0 <path-to-native-precision.gguf> [ctx] [port]" >&2
  exit 1
fi

case "$MODEL" in
  *Q4*|*Q5*|*Q6*|*Q8*|*q4*|*q5*|*q6*|*q8*)
    echo "REFUSING: '$MODEL' looks quantized. This study is native precision." >&2
    exit 1 ;;
esac

echo "development server only. no energy number from this machine means anything."
echo "model $MODEL"
echo "ctx   $CTX"

exec llama-server \
  --model "$MODEL" \
  --ctx-size "$CTX" \
  --port "$PORT" \
  --n-gpu-layers 999 \
  --parallel 1 \
  --no-context-shift \
  --no-mmap \
  --cache-type-k f16 \
  --cache-type-v f16
