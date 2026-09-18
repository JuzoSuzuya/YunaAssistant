#!/usr/bin/env bash
# Локальный Ollama Юны на game-диске (порт 11435).
set -euo pipefail
ROOT=/mnt/disk03/SteamLibrary/YunaAI_ollama
mkdir -p "$ROOT/models" "$ROOT/home/.ollama"
ln -sfn "$ROOT/models" "$ROOT/home/.ollama/models"
export HOME="$ROOT/home"
export OLLAMA_MODELS="$ROOT/models"
export OLLAMA_HOST=127.0.0.1:11435

if curl -sf "http://127.0.0.1:11435/api/tags" >/dev/null 2>&1; then
  echo "Ollama уже на 11435"
  curl -s http://127.0.0.1:11435/api/tags | head -c 500; echo
  exit 0
fi

echo "Стартую Ollama → $OLLAMA_MODELS"
exec /usr/local/bin/ollama serve
