#!/usr/bin/env bash
set -euo pipefail

# Qwen3.6-27B vLLM OpenAI-compatible server (8 GPU TP)

MODEL_PATH="${MODEL_PATH:-/data/agent/Qwen3.5-0.8B-Base}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-Qwen3.5-0.8B-Base}"

HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-8002}"

API_KEY="${VLLM_API_KEY:-EMPTY}"

# 8 GPU tensor parallel
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"

# GPU memory
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"

# context length
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"

# Qwen BF16
DTYPE="${DTYPE:-bfloat16}"

if [[ ! -d "${MODEL_PATH}" ]]; then
  echo "Model directory does not exist: ${MODEL_PATH}" >&2
  exit 2
fi


if ! command -v python >/dev/null 2>&1; then
  echo "python not found" >&2
  exit 2
fi


if ! python -c "import vllm" >/dev/null 2>&1; then
  echo "vLLM not installed" >&2
  exit 2
fi


ARGS=(
  "--host" "${HOST}"
  "--port" "${PORT}"
  "--api-key" "${API_KEY}"

  "--served-model-name" "${SERVED_MODEL_NAME}"

  "--tensor-parallel-size" "${TENSOR_PARALLEL_SIZE}"
  "--distributed-executor-backend" "mp"

  "--dtype" "${DTYPE}"

  "--max-model-len" "${MAX_MODEL_LEN}"

  "--gpu-memory-utilization" "${GPU_MEMORY_UTILIZATION}"

  "--default-chat-template-kwargs"
  '{"enable_thinking": false}'
)


echo "======================================"
echo "Starting vLLM server"
echo "model_path=${MODEL_PATH}"
echo "served_model_name=${SERVED_MODEL_NAME}"
echo "listen=http://${HOST}:${PORT}/v1"
echo "tensor_parallel_size=${TENSOR_PARALLEL_SIZE}"
echo "max_model_len=${MAX_MODEL_LEN}"
echo "dtype=${DTYPE}"
echo "thinking=false"
echo "======================================"


if command -v vllm >/dev/null 2>&1; then

  exec vllm serve "${MODEL_PATH}" \
    "${ARGS[@]}"

else

  exec python -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_PATH}" \
    "${ARGS[@]}"

fi