#!/usr/bin/env bash
# Conform an ad mezzanine to the live stream's encoding profile: the SSAI
# "creative transcode". Resolution, frame rate, GOP, codec settings, audio rate
# and PIDs all come from common.sh, the settings the content is encoded with.
# Usage: conform.sh <mezzanine file>   (MPEG-TS on stdout)
set -euo pipefail
source /app/scripts/common.sh

# Low CPU priority: the live encode and stitching must never wait for an ad transcode.
exec nice -n 15 ffmpeg -hide_banner -loglevel error -nostdin -i "$1" \
  -map 0:v:0 -map 0:a:0 \
  -vf "scale=${VIDEO_SIZE/x/:}:flags=bicubic,fps=${FPS},setsar=1" -af "aresample=48000" \
  "${ENC_ARGS[@]}" pipe:1
