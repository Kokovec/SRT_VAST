#!/usr/bin/env bash
# Mock ADS: render the ad creatives (matching the content encode), package them
# as HLS, then serve VAST.
set -euo pipefail
source /app/scripts/common.sh

MEDIA=/ads
mkdir -p "$MEDIA"

make_ad() {  # id seconds title lavfi-video tone-hz
  local id=$1 secs=$2 title=$3 video=$4 tone=$5
  [[ -f "$MEDIA/$id.ts" ]] && return
  echo "rendering $id ($secs s): $title"
  local sep="="; [[ $video == *=* ]] && sep=":"
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "${video}${sep}size=${VIDEO_SIZE}:rate=${FPS}" \
    -f lavfi -i "sine=frequency=${tone}:sample_rate=48000" \
    -vf "drawtext=fontfile=${FONT}:text='AD  ${title}':x=(w-tw)/2:y=h/2-80:fontsize=72:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=16,drawtext=fontfile=${FONT}:text='%{pts\\:hms}':x=(w-tw)/2:y=h/2+40:fontsize=48:fontcolor=yellow" \
    -t "$secs" "${ENC_ARGS[@]}" "$MEDIA/$id.ts"
  echo "$title" > "$MEDIA/$id.title"
}

make_hls() {  # id: package the creative as HLS VOD, fMP4 segments on GOP boundaries
  local id=$1 dir="$MEDIA/hls/$1"
  [[ -f "$dir/master.m3u8" ]] && return
  mkdir -p "$dir"
  ffmpeg -hide_banner -loglevel error -y -i "$MEDIA/$id.ts" -map 0:v -map 0:a -c copy -bsf:a aac_adtstoasc \
    -f hls -hls_time "$GOP_SECONDS" -hls_playlist_type vod -hls_segment_type fmp4 \
    -hls_fmp4_init_filename init.mp4 -hls_segment_filename "$dir/seg_%03d.m4s" \
    -master_pl_name master.m3u8 "$dir/index.m3u8"
}

make_mez() {  # id seconds title lavfi-video tone-hz
  # Mezzanine master: what an agency delivers. Deliberately NOT the stream's
  # profile (1080p25, High profile with B-frames, 44.1 kHz audio), so the
  # stitcher has to fetch and conform it, like an SSAI system does.
  local id=$1 secs=$2 title=$3 video=$4 tone=$5 out="$MEDIA/mez/$1.mp4"
  [[ -f "$out" ]] && return
  mkdir -p "$MEDIA/mez"
  echo "rendering mezzanine $id ($secs s, 1920x1080@25)"
  local sep="="; [[ $video == *=* ]] && sep=":"
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "${video}${sep}size=1920x1080:rate=25" \
    -f lavfi -i "sine=frequency=${tone}:sample_rate=44100" \
    -vf "drawtext=fontfile=${FONT}:text='AD  ${title}':x=(w-tw)/2:y=h/2-120:fontsize=108:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=24,drawtext=fontfile=${FONT}:text='%{pts\\:hms}':x=(w-tw)/2:y=h/2+60:fontsize=72:fontcolor=yellow,drawtext=fontfile=${FONT}:text='mezzanine 1920x1080 25p':x=w-tw-40:y=h-th-40:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=10" \
    -t "$secs" -c:v libx264 -preset medium -profile:v high -crf 16 -g 50 -bf 3 -pix_fmt yuv420p \
    -c:a aac -b:a 256k -ar 44100 -ac 2 -movflags +faststart "$out"
}

make_ad ad1 15 "ACME Cola"      "smptehdbars"         880
make_ad ad2 15 "Widget Co"      "color=c=0x1f4e9a"    660
make_ad ad3 10 "Rocket Fuel"    "color=c=0x8a1c1c"    550
make_ad ad4 20 "Moonshine Bank" "mandelbrot"          990

make_mez ad1 15 "ACME Cola"      "smptehdbars"         880
make_mez ad2 15 "Widget Co"      "color=c=0x1f4e9a"    660
make_mez ad3 10 "Rocket Fuel"    "color=c=0x8a1c1c"    550
make_mez ad4 20 "Moonshine Bank" "mandelbrot"          990

for ts in "$MEDIA"/*.ts; do make_hls "$(basename "$ts" .ts)"; done

exec python -m srtvast.ads --media-dir "$MEDIA" --port 8080 --public-url "${PUBLIC_URL:-http://ads:8080}" \
  --delivery "${ADS_DELIVERY:-mezzanine}"
