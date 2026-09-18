#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEFT="/home/hoshikojima/.cursor/projects/home-hoshikojima-Documents-Projects-Yuna-by-Hoshi-hoshi-core/assets/wallpaper_left_aincrad.png"
RIGHT="/home/hoshikojima/.cursor/projects/home-hoshikojima-Documents-Projects-Yuna-by-Hoshi-hoshi-core/assets/wallpaper_right_crystal.png"
DEST_DIR="$HOME/Pictures/Wallpapers"
HYPRPAPER_CONF="$HOME/.config/hypr/hyprpaper.conf"
LOCAL_CONF="$SCRIPT_DIR/hyprpaper_dual.conf"

mkdir -p "$DEST_DIR"
cp -f "$LEFT" "$DEST_DIR/wallpaper_left_aincrad.png"
cp -f "$RIGHT" "$DEST_DIR/wallpaper_right_crystal.png"

echo "=== Copied ==="
ls -la "$DEST_DIR/wallpaper_left_aincrad.png" "$DEST_DIR/wallpaper_right_crystal.png"

echo
echo "=== Monitors ==="
hyprctl monitors -j | jq 'sort_by(.x) | .[] | {name, x, width, height, refreshRate}'

LEFT_MON="$(hyprctl monitors -j | jq -r 'sort_by(.x) | .[0].name')"
RIGHT_MON="$(hyprctl monitors -j | jq -r 'sort_by(.x) | .[-1].name')"

# Prefer detected monitors; fall back to known names
LEFT_MON="${LEFT_MON:-HDMI-A-1}"
RIGHT_MON="${RIGHT_MON:-DP-1}"

cat > "$HYPRPAPER_CONF" <<EOF
preload = $LEFT
preload = $RIGHT
wallpaper = $LEFT_MON,$LEFT
wallpaper = $RIGHT_MON,$RIGHT
splash = false
EOF

echo
echo "=== hyprpaper.conf ==="
cat "$HYPRPAPER_CONF"

killall hyprpaper 2>/dev/null || true
sleep 0.3
hyprpaper &
sleep 0.5

hyprctl hyprpaper preload "$LEFT"
hyprctl hyprpaper preload "$RIGHT"
hyprctl hyprpaper wallpaper "$LEFT_MON,$LEFT"
hyprctl hyprpaper wallpaper "$RIGHT_MON,$RIGHT"

echo
echo "=== listactive ==="
hyprctl hyprpaper listactive

echo
echo "=== Mapping ==="
echo "Left  ($LEFT_MON) -> aincrad: $LEFT"
echo "Right ($RIGHT_MON) -> crystal: $RIGHT"
