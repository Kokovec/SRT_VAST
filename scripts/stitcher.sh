#!/usr/bin/env bash
# SRT receiver -> stitcher -> SRT output listener (players/recorder connect to :9001).
set -euo pipefail
source /app/scripts/common.sh

SRT_LATENCY=${SRT_LATENCY:-200}
RECORD=${RECORD-/out/stitched.ts}   # set RECORD to empty to disable the recording
mkdir -p /out

forever srt-live-transmit -q "srt://:9000?mode=listener&latency=${SRT_LATENCY}" "udp://127.0.0.1:5001" &
forever srt-live-transmit -q "udp://:5002" "srt://:9001?mode=listener&latency=${SRT_LATENCY}" &
# Second egress for the web viewer (an SRT listener takes one caller at a time).
# Same host as the viewer, so a small latency buffer is plenty (keeps it in step with the preview).
forever srt-live-transmit -q "udp://:5003" "srt://:9002?mode=listener&latency=${MONITOR_SRT_LATENCY:-40}" &

# Slate for short pods: one GOP, encoded exactly like the content.
SLATE=/tmp/slate.ts
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0x101010:size=${VIDEO_SIZE}:rate=${FPS}" \
  -f lavfi -i "anullsrc=r=48000:cl=stereo" \
  -vf "drawtext=fontfile=${FONT}:text='BACK SHORTLY':x=(w-tw)/2:y=(h-th)/2:fontsize=64:fontcolor=white" \
  -t "$GOP_SECONDS" "${ENC_ARGS[@]}" "$SLATE"

exec python -m srtvast.stitcher --slate "$SLATE" --conform /app/scripts/conform.sh \
  --input-udp 127.0.0.1:5001 --output-udp 127.0.0.1:5002,127.0.0.1:5003 \
  ${RECORD:+--record "$RECORD"} ${TAP_UDP:+--tap-udp "$TAP_UDP"} --ads-url "${ADS_URL}"
