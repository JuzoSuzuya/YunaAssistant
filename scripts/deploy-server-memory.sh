#!/bin/bash
# Деплой патча единой памяти на сервер (desktop_context + agent_prompt)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOSHI="$ROOT/hoshi-core"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

HOST="${YUNA_SERVER_USER:-root}@${YUNA_SERVER_HOST:-151.247.197.252}"
REMOTE="${YUNA_SERVER_HOSHI_PATH:-/root/projects/Hoshi}"
SSH_OPTS=(-F /dev/null -o StrictHostKeyChecking=no)

rsync_cmd() {
  if [[ -n "${YUNA_SERVER_SSH_PASS:-}" ]]; then
    RSYNC_SSH="sshpass -p ${YUNA_SERVER_SSH_PASS} ssh ${SSH_OPTS[*]}"
  else
    RSYNC_SSH="ssh ${SSH_OPTS[*]}"
  fi
  rsync -avz -e "$RSYNC_SSH" "$@"
}

echo "→ $HOST:$REMOTE"
rsync_cmd "$HOSHI/desktop_context.py" "$HOST:$REMOTE/"
rsync_cmd "$HOSHI/agent_prompt.py" "$HOST:$REMOTE/"

if [[ -n "${YUNA_SERVER_SSH_PASS:-}" ]]; then
  sshpass -p "$YUNA_SERVER_SSH_PASS" ssh "${SSH_OPTS[@]}" "$HOST" \
    "cd '$REMOTE' && ./hoshi_ctl.sh restart"
else
  ssh "${SSH_OPTS[@]}" "$HOST" "cd '$REMOTE' && ./hoshi_ctl.sh restart"
fi

echo "Сервер обновлён. Единая память: local_context теперь в промпте Telegram-бота."
