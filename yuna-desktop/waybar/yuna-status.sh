#!/usr/bin/env python3
# Статус Юны для Waybar (JSON)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${YUNA_BRIDGE_PORT:-8765}"
export PYTHONPATH="${ROOT}/../hoshi-core:${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

python3 -c "
import json, sys, urllib.request
sys.path.insert(0, '$ROOT')
from yuna_status import waybar_payload

online = False
try:
    with urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=1) as r:
        online = r.status == 200
except Exception:
    pass

p = waybar_payload()
if not online and p.get('class') == 'idle':
    p['tooltip'] = 'Юна — ' + (p.get('tooltip', '').replace('Юна — ', '') or 'на связи')
print(json.dumps(p, ensure_ascii=False))
"
