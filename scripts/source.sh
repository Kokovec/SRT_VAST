#!/usr/bin/env bash
# Test content source: ffmpeg test pattern -> SCTE-35 injector -> SRT caller.
set -euo pipefail
source /app/scripts/common.sh

SRT_TARGET=${SRT_TARGET:-stitcher:9000}
SRT_LATENCY=${SRT_LATENCY:-200}

BREAK_FIRST=${BREAK_FIRST:-20}
BREAK_INTERVAL=${BREAK_INTERVAL:-60}
BREAK_DURATION=${BREAK_DURATION:-30}

# Encoder conditioning: an IDR on every splice-out and splice-in frame of the
# break schedule (frame numbers; the cue injector uses the same schedule).
read -r OUT_FRAME IN_FRAME PERIOD < <(python3 -c "f=$FPS; print(round($BREAK_FIRST*f), round(($BREAK_FIRST+$BREAK_DURATION)*f), round($BREAK_INTERVAL*f))")
FORCE_KEYS="expr:gte(n,$OUT_FRAME)*not(mod(n-$OUT_FRAME,$PERIOD))+gte(n,$IN_FRAME)*not(mod(n-$IN_FRAME,$PERIOD))"
echo "conditioning: IDR at frame $OUT_FRAME and $IN_FRAME, every $PERIOD frames"

forever srt-live-transmit -q "udp://:5000" "srt://${SRT_TARGET}?mode=caller&latency=${SRT_LATENCY}" &

ffmpeg -hide_banner -loglevel warning -re \
  -f lavfi -i "testsrc2=size=${VIDEO_SIZE}:rate=${FPS}" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -vf "drawtext=fontfile=${FONT}:text='PROGRAM  %{pts\\:hms}':x=40:y=40:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12" \
  "${ENC_ARGS[@]}" -force_key_frames "$FORCE_KEYS" -forced-idr 1 -flush_packets 1 pipe:1 \
| python -m srtvast.source --udp 127.0.0.1:5000 \
    --first-break "$BREAK_FIRST" --interval "$BREAK_INTERVAL" \
    --duration "$BREAK_DURATION" --mode "${CUE_MODE:-splice_insert}" \
    --preroll "${CUE_PREROLL:-15,10,5,2}"
