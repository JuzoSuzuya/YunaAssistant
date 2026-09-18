#!/bin/bash
# Добавляет кнопку Юны в Waybar (после рабочих столов)
set -euo pipefail

DOTFILES="$HOME/.mydotfiles/com.ml4w.dotfiles/.config/waybar"
MODULES="$DOTFILES/modules.json"
YUNA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATUS_SCRIPT="$YUNA_ROOT/yuna-desktop/waybar/yuna-status.sh"
LAUNCHER="$YUNA_ROOT/yuna-desktop/waybar/yuna-launch.sh"

chmod +x "$STATUS_SCRIPT" "$LAUNCHER" "$YUNA_ROOT/yuna-desktop/yuna_ctl.sh"

# Модуль waybar
if ! grep -q '"custom/yuna"' "$MODULES" 2>/dev/null; then
  python3 <<PY
import json
from pathlib import Path
p = Path("$MODULES")
data = json.loads(p.read_text(encoding="utf-8"))
data["custom/yuna"] = {
    "format": "{}",
    "return-type": "json",
    "interval": 0.5,
    "exec": "$STATUS_SCRIPT",
    "on-click": "$LAUNCHER",
    "tooltip": True,
}
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("modules.json: custom/yuna added")
PY
fi

# Вставить после hyprland/workspaces во все активные темы
for THEME in "$DOTFILES/themes/ml4w-modern/config" "$DOTFILES/themes/ml4w-glass/config"; do
  if [[ -f "$THEME" ]] && grep -q '"hyprland/workspaces"' "$THEME" && ! grep -q '"custom/yuna"' "$THEME"; then
    sed -i '/"hyprland\/workspaces",/a\        "custom/yuna",' "$THEME"
    echo "theme: custom/yuna → $THEME"
  fi
done

# CSS для логотипа и анимаций
YUNA_CSS="$YUNA_ROOT/yuna-desktop/waybar/yuna.css"
for STYLE in "$DOTFILES/themes/ml4w-glass/style.css" "$DOTFILES/themes/ml4w-modern/style.css"; do
  if [[ -f "$STYLE" ]] && ! grep -q "yuna.css" "$STYLE" 2>/dev/null; then
    printf '\n/* Yuna waybar */\n@import "%s";\n' "$YUNA_CSS" >> "$STYLE"
    echo "css: yuna → $STYLE"
  fi
done

# Автозапуск после входа
AUTOSTART_SRC="$YUNA_ROOT/yuna-desktop/autostart/yuna-desktop.desktop"
AUTOSTART_DST="$HOME/.config/autostart/yuna-desktop.desktop"
mkdir -p "$HOME/.config/autostart"
cp "$AUTOSTART_SRC" "$AUTOSTART_DST"
echo "autostart: $AUTOSTART_DST"

echo "Готово. Перезапусти waybar: ~/.config/waybar/launch.sh или hyprctl reload"
