#!/usr/bin/env bash
# SRT receiver -> stitcher -> SRT output listener (players/recorder connect to :9001).
set -euo pipefail
source /app/scripts/common.sh

SRT_LATENCY=${SRT_LATENCY:-200}
mkdir -p /out

forever srt-live-transmit -q "srt://:9000?mode=listener&latency=${SRT_LATENCY}" "udp://127.0.0.1:5001" &
forever srt-live-transmit -q "udp://:5002" "srt://:9001?mode=listener&latency=${SRT_LATENCY}" &

# Slate for short pods: one GOP, encoded exactly like the content.
SLATE=/tmp/slate.ts
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0x101010:size=${VIDEO_SIZE}:rate=${FPS}" \
  -f lavfi -i "anullsrc=r=48000:cl=stereo" \
  -vf "drawtext=fontfile=${FONT}:text='BACK SHORTLY':x=(w-tw)/2:y=(h-th)/2:fontsize=64:fontcolor=white" \
  -t "$GOP_SECONDS" "${ENC_ARGS[@]}" "$SLATE"

exec python -m srtvast.stitcher --slate "$SLATE" \
  --input-udp 127.0.0.1:5001 --output-udp 127.0.0.1:5002 \
  --record /out/stitched.ts --ads-url "${ADS_URL}"
