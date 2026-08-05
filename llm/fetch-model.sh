#!/usr/bin/env bash
# Download the GGUF weights. ONLINE STEP — run once via `make model`.
#
# ── Why this model ───────────────────────────────────────────────────────────
# The agent loop is entirely tool-driven: every question turns into MCP calls
# that browse the catalog, run SQL, and restyle map layers. A model that chats
# well but emits malformed tool calls is useless here — it degrades to a
# chatbot that cannot see the data.
#
# Qwen3-8B is the smallest model tested that reliably emits well-formed
# OpenAI-style tool calls across multiple turns. Two families that look like
# obvious candidates and are not:
#
#   * Qwen-Coder — tuned for code completion, measurably worse at multi-turn
#     tool selection than the same-size instruct model.
#   * Gemma — no native tool-call tokens; tool use is prompt-emulated and
#     brittle in exactly the multi-step case this app depends on.
#
# Q4_K_M is the quantization sweet spot: ~5 GB, and tool-call formatting holds
# up. Q3 and below start producing malformed JSON arguments.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p models

REPO="${LLM_MODEL_REPO:-Qwen/Qwen3-8B-GGUF}"
FILE="${LLM_MODEL_FILE:-Qwen3-8B-Q4_K_M.gguf}"
URL="${LLM_MODEL_URL:-https://huggingface.co/${REPO}/resolve/main/${FILE}}"

if [ -s "models/${FILE}" ]; then
    echo "==> models/${FILE} already present ($(du -h "models/${FILE}" | cut -f1)), skipping"
    echo "    delete it to re-download"
    exit 0
fi

echo "==> downloading ${FILE}"
echo "    from ${URL}"
echo "    ~5 GB; --continue-at means an interrupted download resumes"

curl -fL --retry 3 --retry-delay 5 --continue-at - \
    -o "models/${FILE}" "${URL}"

# A truncated download produces a file llama.cpp rejects with an unhelpful
# "invalid magic" at container start, minutes later. Catch it here instead.
SIZE=$(wc -c < "models/${FILE}")
if [ "$SIZE" -lt 1000000000 ]; then
    echo "" >&2
    echo "Downloaded file is only ${SIZE} bytes — far too small for a 4-bit 8B" >&2
    echo "model. This is usually an HTML error page saved under a .gguf name," >&2
    echo "often because the repo requires accepting a licence while signed in." >&2
    echo "Check models/${FILE}, delete it, and retry." >&2
    exit 1
fi

echo ""
echo "==> done: models/${FILE} ($(du -h "models/${FILE}" | cut -f1))"
echo "    Set LLM_MODEL_FILE=${FILE} in .env"
