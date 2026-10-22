#!/usr/bin/env bash
# Idle watchdog (srt-vast-idle.timer, every 30 s). Shuts the OS down, which
# STOPS the instance (shutdown behaviour = stop), when:
#   * nobody has had the viewer open for IDLE_MINUTES, or
#   * the viewer has not come up within BOOT_GRACE_MINUTES of boot (broken stack), or
#   * the instance has been up for MAX_UPTIME_HOURS (cost backstop).
# The starter Lambda (CloudFront failover) starts it again on the next sign-in.
set -euo pipefail
source /etc/srt-vast.conf
IDLE_MINUTES=${IDLE_MINUTES:-5}
BOOT_GRACE_MINUTES=${BOOT_GRACE_MINUTES:-15}
MAX_UPTIME_HOURS=${MAX_UPTIME_HOURS:-12}

up=$(cut -d. -f1 /proc/uptime)
stop() { logger -t srt-vast-idle "stopping instance: $1"; systemctl poweroff; exit 0; }

(( up > MAX_UPTIME_HOURS * 3600 )) && stop "up for more than ${MAX_UPTIME_HOURS} h"

if ! status=$(curl -fsS --max-time 5 http://127.0.0.1:8090/api/status); then
  (( up > BOOT_GRACE_MINUTES * 60 )) && stop "viewer unreachable ${BOOT_GRACE_MINUTES} min after boot"
  exit 0
fi

idle=$(python3 -c 'import json,sys; print(int(json.load(sys.stdin)["idle_seconds"]))' <<<"$status")
(( idle >= IDLE_MINUTES * 60 )) && stop "no viewers for ${IDLE_MINUTES} min"
exit 0
