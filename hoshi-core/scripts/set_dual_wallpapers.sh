#!/usr/bin/env bash
# Per-monitor dual wallpapers for Hyprland (ML4W / hyprpaper)
set -euo pipefail

SRC_LEFT="/home/hoshikojima/.cursor/projects/home-hoshikojima-Documents-Projects-Yuna-by-Hoshi-hoshi-core/assets/wallpaper_left_aincrad.png"
SRC_RIGHT="/home/hoshikojima/.cursor/projects/home-hoshikojima-Documents-Projects-Yuna-by-Hoshi-hoshi-core/assets/wallpaper_right_crystal.png"
DEST_DIR="$HOME/Pictures/Wallpapers"
LEFT="$DEST_DIR/wallpaper_left_aincrad.png"
RIGHT="$DEST_DIR/wallpaper_right_crystal.png"
HYPRPAPER_CONF="$HOME/.config/hypr/hyprpaper.conf"

mkdir -p "$DEST_DIR"
cp -f "$SRC_LEFT" "$LEFT"
cp -f "$SRC_RIGHT" "$RIGHT"

echo "=== Copied wallpapers ==="
ls -la "$LEFT" "$RIGHT"

echo
echo "=== Monitors (hyprctl) ==="
if ! command -v hyprctl >/dev/null 2>&1; then
  echo "ERROR: hyprctl not found — are you on Hyprland?" >&2
  exit 1
fi
hyprctl monitors

MONITOR_JSON="$(hyprctl monitors -j)"
MONITOR_COUNT="$(echo "$MONITOR_JSON" | grep -o '"name"' | wc -l)"

if [[ "$MONITOR_COUNT" -lt 2 ]]; then
  echo "WARNING: Only $MONITOR_COUNT monitor detected; dual setup may not apply." >&2
fi

if command -v jq >/dev/null 2>&1; then
  LEFT_MON="$(echo "$MONITOR_JSON" | jq -r 'sort_by(.x) | .[0].name')"
  RIGHT_MON="$(echo "$MONITOR_JSON" | jq -r 'sort_by(.x) | .[-1].name')"
  LEFT_RES="$(echo "$MONITOR_JSON" | jq -r 'sort_by(.x) | .[0] | "\(.width)x\(.height)@\(.refreshRate)Hz"')"
  RIGHT_RES="$(echo "$MONITOR_JSON" | jq -r 'sort_by(.x) | .[-1] | "\(.width)x\(.height)@\(.refreshRate)Hz"')"
else
  mapfile -t MON_NAMES < <(echo "$MONITOR_JSON" | grep -o '"name":"[^"]*"' | cut -d'"' -f4)
  LEFT_MON="${MON_NAMES[0]:-}"
  RIGHT_MON="${MON_NAMES[-1]:-}"
  LEFT_RES="unknown"
  RIGHT_RES="unknown"
fi

echo
echo "Left  ($LEFT_MON, $LEFT_RES)  -> aincrad"
echo "Right ($RIGHT_MON, $RIGHT_RES) -> crystal"

apply_hyprpaper() {
  # Ensure hyprpaper is running (ML4W / waypaper default backend)
  if ! pgrep -x hyprpaper >/dev/null 2>&1; then
    hyprpaper &
    sleep 0.5
  fi

  # Persist config for reboot
  cat > "$HYPRPAPER_CONF" <<EOF
preload = $LEFT
preload = $RIGHT
wallpaper = $LEFT_MON,$LEFT
wallpaper = $RIGHT_MON,$RIGHT
splash = false
EOF

  # Live update via IPC (no restart needed)
  hyprctl hyprpaper preload "$LEFT"
  hyprctl hyprpaper preload "$RIGHT"
  hyprctl hyprpaper wallpaper "$LEFT_MON,$LEFT"
  hyprctl hyprpaper wallpaper "$RIGHT_MON,$RIGHT"
}

apply_swww() {
  if ! swww query >/dev/null 2>&1; then
    swww-daemon &
    sleep 0.5
  fi
  swww img "$LEFT" --outputs "$LEFT_MON" --resize fill
  swww img "$RIGHT" --outputs "$RIGHT_MON" --resize fill
}

echo
echo "=== Applying wallpapers ==="

BACKEND="$(grep -E '^backend\s*=' "$HOME/.config/waypaper/config.ini" 2>/dev/null | awk -F= '{print $2}' | tr -d ' ' || true)"

if [[ "$BACKEND" == "hyprpaper" ]] || command -v hyprpaper >/dev/null 2>&1; then
  TOOL="hyprpaper (hyprctl IPC)"
  apply_hyprpaper
elif command -v swww >/dev/null 2>&1; then
  TOOL="swww"
  apply_swww
else
  echo "ERROR: No hyprpaper or swww found." >&2
  exit 1
fi

# Update ML4W cache so wallpaper-restore.sh uses left wallpaper on next login
CACHE="$HOME/.cache/ml4w/hyprland-dotfiles/current_wallpaper"
mkdir -p "$(dirname "$CACHE")"
echo "$LEFT" > "$CACHE"

echo
echo "=== SUCCESS ==="
echo "Tool:     $TOOL"
echo "Left:     $LEFT_MON ($LEFT_RES) -> $LEFT"
echo "Right:    $RIGHT_MON ($RIGHT_RES) -> $RIGHT"
echo "Persist:  $HYPRPAPER_CONF"
