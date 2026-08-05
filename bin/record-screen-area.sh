#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  record-screen-area.sh [options] X Y WIDTH HEIGHT [OUTPUT]

Records a fixed rectangle of the macOS screen using ffmpeg.

Arguments:
  X Y WIDTH HEIGHT   Capture rectangle, in screen pixels.
  OUTPUT             Output video path. Defaults to ./screen-area-YYYYmmdd-HHMMSS.mov.

Options:
  --screen INDEX     ffmpeg avfoundation screen device index. Default: 1.
  --fps FPS          Capture frame rate. Default: 30.
  --duration SEC     Stop after this many seconds. Default: record until Ctrl-C.
  --no-cursor        Do not capture the mouse cursor.
  --list-devices     Print avfoundation devices and exit.
  -h, --help         Show this help.

Examples:
  bin/record-screen-area.sh 100 200 1280 720
  bin/record-screen-area.sh --duration 10 --fps 60 0 0 1920 1080 demo.mov
  bin/record-screen-area.sh --list-devices
EOF
}

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "error: ffmpeg is required. Install it with: brew install ffmpeg" >&2
  exit 1
fi

screen_index=1
fps=30
duration=""
capture_cursor=1
args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --screen)
      [[ $# -ge 2 ]] || { echo "error: --screen requires an index" >&2; exit 1; }
      screen_index="$2"
      shift 2
      ;;
    --fps)
      [[ $# -ge 2 ]] || { echo "error: --fps requires a value" >&2; exit 1; }
      fps="$2"
      shift 2
      ;;
    --duration)
      [[ $# -ge 2 ]] || { echo "error: --duration requires seconds" >&2; exit 1; }
      duration="$2"
      shift 2
      ;;
    --no-cursor)
      capture_cursor=0
      shift
      ;;
    --list-devices)
      ffmpeg -f avfoundation -list_devices true -i "" || true
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      args+=("$@")
      break
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

if [[ ${#args[@]} -lt 4 || ${#args[@]} -gt 5 ]]; then
  usage >&2
  exit 1
fi

x="${args[0]}"
y="${args[1]}"
width="${args[2]}"
height="${args[3]}"
output="${args[4]:-screen-area-$(date +%Y%m%d-%H%M%S).mov}"

for value_name in x y width height fps screen_index; do
  value="${!value_name}"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "error: $value_name must be a non-negative integer, got: $value" >&2
    exit 1
  fi
done

ffmpeg_args=(
  -f avfoundation
  -framerate "$fps"
  -capture_cursor "$capture_cursor"
)

if [[ -n "$duration" ]]; then
  ffmpeg_args+=(-t "$duration")
fi

ffmpeg_args+=(
  -i "${screen_index}:none"
  -vf "crop=${width}:${height}:${x}:${y}"
  -c:v libx264
  -preset veryfast
  -pix_fmt yuv420p
  "$output"
)

echo "Recording ${width}x${height} at (${x},${y}) to ${output}"
echo "Press Ctrl-C to stop."
exec ffmpeg "${ffmpeg_args[@]}"
