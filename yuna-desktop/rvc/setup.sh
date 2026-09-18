#!/bin/bash
# Установка RVC (Python 3.10 venv + Haruka model). Запускать один раз.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export TMPDIR="${TMPDIR:-$HOME/.cache/uv-tmp}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
mkdir -p "$TMPDIR" "$UV_CACHE_DIR" "$ROOT/models"

if [[ ! -f "$ROOT/models/haruka/VT-TTS_Haruka.pth" ]]; then
  echo "Скачиваю модель Haruka (аниме-девочка)…"
  curl -L -o "$ROOT/models/haruka.zip" \
    "https://huggingface.co/AIHeaven/rvc-models/resolve/main/VT-TTS_Haruka_rmvpe_250epoch.zip"
  unzip -o "$ROOT/models/haruka.zip" -d "$ROOT/models/haruka"
  rm -f "$ROOT/models/haruka.zip"
fi

echo "Создаю venv Python 3.10…"
UV_VENV_CLEAR=1 uv venv "$ROOT/.venv" --python 3.10
uv pip install --python "$ROOT/.venv/bin/python" "numpy==1.23.5"
uv pip install --python "$ROOT/.venv/bin/python" \
  torch==2.11.0+cu128 torchaudio==2.11.0+cu128 \
  --index-url https://download.pytorch.org/whl/cu128
uv pip install --python "$ROOT/.venv/bin/python" rvc-python librosa soundfile edge-tts

echo "Готово. Запуск: yuna_ctl.sh rvc  или  yuna_ctl.sh voice"
