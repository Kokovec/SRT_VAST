# Shared encoder settings. Content and ads MUST match for TS-level splicing:
# same codec/profile, resolution, frame rate, fixed GOP without B-frames,
# same audio format and the same PIDs.

VIDEO_SIZE=${VIDEO_SIZE:-1280x720}
FPS=${FPS:-30}
GOP_SECONDS=${GOP_SECONDS:-2}
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
GOP=$((FPS * GOP_SECONDS))

ENC_ARGS=(
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -pix_fmt yuv420p
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 -bf 0
  -b:v 2500k -maxrate 2500k -bufsize 2500k
  -c:a aac -b:a 128k -ar 48000 -ac 2
  -f mpegts -mpegts_service_id 1 -mpegts_pmt_start_pid 0x1000 -mpegts_start_pid 0x100
)

# Run a command forever, restarting it if it exits (e.g. SRT peer went away).
forever() {
  while true; do "$@" || true; sleep 1; done
}
