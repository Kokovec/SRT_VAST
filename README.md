# SRT VAST

Proof of concept: receive a live **SRT** stream carrying **SCTE-35** cues, make a
**VAST** request to an ad decision server when a break is signalled, and splice
the returned ad into the outgoing **MPEG-TS** at the packet level.

```
┌──────────────┐  SRT :9000   ┌───────────────────────────────────────────────┐  SRT :9001  ┌──────────┐
│ source       │ ───────────▶ │ stitcher                                      │ ──────────▶ │ recorder │
│ ffmpeg       │              │ srt-live-transmit ─udp─▶ python ─udp─▶ srt-lt │             │ / VLC    │
│ + SCTE-35    │              │   receive → SCTE-35 detect → VAST → TS splice │             └──────────┘
│   injector   │              └───┬───────────┬───────────────────▲───────┬───┘
└──────────────┘    incoming tap  │           │ VAST GET          │       │ SRT :9002 (stitched)
                     (udp :5200)  │           ▼                   │       ▼
                                  │   ┌───────────────────────────┴─┐   ┌───────────────────────────┐
                                  │   │ ads  (mock ADS, :8080)      │   │ viewer (web monitor :8090)│
                                  │   │ VAST 4.2 + mezzanine        │   │ program + incoming preview│
                                  │   │ masters + beacons           │   │ + live event log          │
                                  │   └─────────────────────────────┘   └───────────▲───────────────┘
                                  └─────────────────────────────────────────────────┘
```

| Component | Where | What it does |
|---|---|---|
| SRT source | [scripts/source.sh](scripts/source.sh), [srtvast/source.py](srtvast/source.py) | ffmpeg test pattern, **conditioned**: an IDR is forced on every splice-out and splice-in frame of the break schedule → adds an SCTE-35 PID (0x1F4) to the PMT and inserts `splice_insert` (or `time_signal`) cues 15/10/5/2 s ahead of each break (`CUE_PREROLL`), on whole frames → SRT caller |
| SRT receiver | [scripts/stitcher.sh](scripts/stitcher.sh) | `srt-live-transmit` listener on :9000, hands TS to Python over loopback UDP |
| SCTE-35 detector | [srtvast/scte35.py](srtvast/scte35.py), `Stitcher._on_splice_info` | Finds the 0x86 PID from the PMT, reassembles and CRC-checks sections, normalises `splice_insert` and `time_signal`+segmentation descriptors into out/in/cancel cues, applies `pts_adjustment`, dedupes repeats |
| VAST integrator | [srtvast/vast.py](srtvast/vast.py), `VastPodLoader` in [srtvast/stitcher.py](srtvast/stitcher.py) | On a cue, requests VAST from the ADS, follows wrappers, orders the pod by `sequence`. For each ad it takes the VAST 4 `<Mezzanine>` and conforms it (see below), or else a ready-to-splice MediaFile (HLS, falling back to `video/MP2T`). Everything is ready before the splice point. Fires impression / start / quartile / complete / error beacons |
| Creative conform | [scripts/conform.sh](scripts/conform.sh) | Transcodes a mezzanine master to the stream's exact profile (settings from [scripts/common.sh](scripts/common.sh)) at low CPU priority, so the live path is never starved; results are cached per creative |
| Mock ADS | [scripts/ads.sh](scripts/ads.sh), [srtvast/ads.py](srtvast/ads.py) | Renders 4 creatives (10–20 s), each as a 1080p25 **mezzanine** master (deliberately unlike the stream) and as a ready-to-splice copy packaged as HLS VOD. Serves VAST 4.2 pods filling the requested duration: by default with only the `<Mezzanine>` element (`ADS_DELIVERY=mezzanine`), or the HLS MediaFile (`streaming`), or both. Serves the media and logs beacons |
| HLS client / transmux | [srtvast/hls.py](srtvast/hls.py) | Resolves master → highest-bandwidth variant → media playlist, downloads `EXT-X-MAP` init + segments, remuxes the fMP4 to one MPEG-TS with `ffmpeg -c copy` (TS segments are just concatenated) |
| MPEG-TS stitcher | [srtvast/stitcher.py](srtvast/stitcher.py) | Frame-accurate splicing per PID: video cut on the exact cue frame and the exact break-end frame, audio cut on whole AAC frames and moved onto the content's audio frame grid (gapless, no overlap), PES straddling a splice split and re-packetized; breaks filled to the frame (long ads cut, short pods padded with slate); ad PTS/DTS/PCR rebased onto the content timeline, continuity counters rewritten, ad packets paced by the content PCR; content outside breaks passes through bit for bit |
| Web monitor | [srtvast/viewer.py](srtvast/viewer.py), [srtvast/web/index.html](srtvast/web/index.html) | Plays the stitched output (pulled over SRT from `:9002`) with a picture-in-picture **incoming (pre-stitch) preview**, a live event log, an on-air badge and splice-timing charts. See [Web monitor](#web-monitor). Optional Cognito sign-in for the AWS deployment ([srtvast/auth.py](srtvast/auth.py)) |
| Analyzer | [srtvast/analyze.py](srtvast/analyze.py) | Reports CC errors, PTS/PCR jumps, audio continuity per AAC frame (overlaps/gaps), and for every cue whether the output has an IDR on the exact splice-out and splice-in frame and how many frames the break holds |

Everything in `srtvast/` is standard-library Python 3.12; ffmpeg and
`srt-live-transmit` come from the Docker image.

## Run it

```bash
docker compose up --build
```

- 1st break 21.5 s in (deliberately off the 2 s GOP grid), then every 60 s, 30 s long (`BREAK_*` in [docker-compose.yml](docker-compose.yml)). The second break gets a 25 s pod and is padded with 5 s of slate.
- Watch the `stitcher` and `ads` logs (or the web monitor): cue detected 15 s ahead → VAST request → mezzanine fetched and conformed (or `cache hit`) → pod ready → break armed (splice frames) → `>>> SPLICE OUT` → ad starts + beacons → `<<< SPLICE IN` → break summary (frames, slate, audio grid shift per ad).
- Output: `out/stitched.ts` (written by the stitcher) and `out/received.ts` (pulled back over SRT by `recorder`).
- **Web monitor: http://localhost:8090** (details [below](#web-monitor)).
- Live view: `srt://localhost:9001` in VLC/ffplay (stop `recorder` first; the listener takes one caller). From Windows, UDP port forwarding into WSL2 may need mirrored networking.

Check a recording:

```bash
python3 -m srtvast.analyze out/received.ts
```

A frame-accurate stitch shows no video PTS jumps, no CC errors, no PCR jumps,
0 audio overlaps and 0 audio gaps, and for each break `out pts=… IDR, in pts=… IDR,
900 frames inside` (for a 30 s break at 30 fps).

## Web monitor

http://localhost:8090 (locally; on AWS, behind sign-in at the deployment's URL):

- **Program window:** the stitched output, pulled over SRT from the stitcher's
  second egress (`:9002`) and served unchanged as HTTP MPEG-TS (`/live.ts`),
  played with mpegts.js.
- **Incoming (pre-stitch) preview:** a picture-in-picture window. The stitcher
  forwards a copy of its raw input (`TAP_UDP`); the viewer re-encodes it to
  320×180, 15 fps, about 200 kb/s, no audio (`/preview.ts`), keeping the
  incoming timestamps. You can hide it, and the page remembers.
- **Kept in sync:** both feeds carry the same content timeline. The viewer
  matches timestamps across them and holds back whichever is ahead (shown as
  `preview_lag_ms` in `/api/status`), and the page keeps both players at the
  same 1 s buffer by nudging playback speed by at most ±5 %. Measured on the
  server, the two feeds leave the viewer within a few milliseconds of each
  other. The monitor hop uses 40 ms of SRT latency (`MONITOR_SRT_LATENCY`).
- **Event log:** every component sends its log records as JSON over UDP when
  `EVENTS_UDP` is set; the page shows cues, VAST, conforms, splices and
  beacons, with an on-air badge (PROGRAM / AD BREAK n, current ad, break
  progress) and a slider that delays events to match the player.
- **Splice timing:** expected (cue) vs. actual break times, and per-break
  splice accuracy in milliseconds.

## Deploying to AWS

[deploy/README.md](deploy/README.md) runs the stack on one EC2 instance behind
CloudFront, all in AWS on your existing Route 53 domain. Each person signs in
with their own Cognito login, sessions last 30 minutes, the instance starts when
someone signs in and stops after 5 minutes with nobody watching, and only
CloudFront can reach the instance. Terraform in [deploy/](deploy/) provisions all of it.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

`tests/tsgen.py` builds synthetic transport streams (ADTS audio muxed like ffmpeg,
8 frames per PES, behind the video) so the splice logic is tested without ffmpeg.
Every test checks that video PTS is continuous, audio is exactly contiguous, CC
and PCR are clean, and that every video frame and every audio frame comes from the
source that owns its time: frame-accurate out/in, splice-out mid-GOP, cue between
frames, ad longer than the break (cut on the frame, no `complete` beacon), short
pod padded with slate, unconditioned content (held to the next IDR), bit-exact
passthrough outside breaks, ad decision failure, and `time_signal` cues.

The other suites cover SCTE-35 and TS parsing, VAST and wrappers, the HLS client,
the mezzanine conform/cache flow, the web monitor (event hub, preview tap, stream
aligner, session handling), JWT verification, and the AWS starter Lambda's
sign-in and wake-up routes.

## SSAI creative flow (mezzanine → conform → cache)

By default the mock ADS answers like an SSAI-integrated ad server: each ad's
`<MediaFiles>` carries only a VAST 4 **`<Mezzanine>`**. That is a 1080p25 High-profile
master with B-frames and 44.1 kHz audio, deliberately unlike the 720p30 stream. On a
cue the stitcher:

1. requests VAST and reads the pod;
2. **fetches each mezzanine** (`GET /mez/<id>.mp4`) and **conforms** it to the stream
   profile with [scripts/conform.sh](scripts/conform.sh): scale, frame rate, fixed GOP,
   no B-frames, 48 kHz AAC and the stream's PIDs, all taken from
   [scripts/common.sh](scripts/common.sh), the content's own encoder settings;
3. **caches** the conformed creative per mezzanine URL (in memory, so it starts
   empty whenever the stitcher restarts), so later breaks that use it only log
   `cache hit`. Ads in a pod are conformed in parallel, and a transcode
   already running for another break is shared, not repeated;
4. splices as usual once every ad is ready.

The conform runs at low CPU priority (`nice 15`), so the live encode and the stitcher
always come first. Measured: a 15 s creative conforms in about 1.5–5 s and the heavy
20 s ad4 master (47 MB) in about 2.5–9 s, depending on the machine and what else is
transcoding. That is inside the 15 s cue preroll (`CUE_PREROLL`). When a transcode
isn't done by the splice point, the break stays on program and the transcode still
completes into the cache. That is the "first impression miss" real SSAI systems
have. The aired ad shows a `mezzanine 1920x1080 25p` corner label, so you can see the
conformed master on air.

`ADS_DELIVERY=streaming` (ready-to-splice HLS) or `both` on the `ads` service
switches the VAST back. The stitcher prefers the mezzanine when it has `--conform`.

## How the splice works

Output timeline = content timeline. For a break at *T* with duration *D* (both
snapped to whole content frames): splice-out *S_out = T*, splice-in
*S_in = T + D*. Each ad *k* owns the slot *[S_out + offset_k, S_out + offset_k +
length_k)*, clipped to *S_in*; slate clips fill whatever the pod leaves.

1. **Cue.** `splice_insert(out_of_network=1, pts_time=T, break_duration=D)` arrives
   15 s early (repeated at 10, 5 and 2 s; the first copy wins). The stitcher starts
   the VAST request and the creative fetch/conform on worker threads. When the pod is ready the break is **armed**: every ES PID gets the
   splice points.
2. **Per-PID lanes.** Each PES from the content or the pod is placed on the output
   timeline and kept only inside its source's window. Video PES are single frames:
   the last content frame is the one before *S_out*, the first ad frame is presented
   at *S_out*, content resumes on the frame at *S_in*. Splice-out needs no content
   IDR (ads start with one); splice-in needs one, which the conditioned encoder
   provides.
3. **Audio.** PES within 1 s of a splice point are held until complete and split
   into ADTS frames. Each ad's audio is first shifted onto the content's audio frame
   grid (by at most half a frame, ±10.7 ms at 48 kHz, well inside the EBU R37
   lip-sync tolerance of +40/−60 ms). A frame belongs to the side its midpoint falls
   on and may not overlap the frame before it. A PES only partly kept is
   re-packetized. The result is gapless, overlap-free audio with the content audio
   untouched.
4. **Ad playout.** Each ad packet gets `PTS += S_out + offset_k − ad_first_pts` (same
   for DTS and PCR×300) and a fresh continuity counter. Ads are released when the
   content PCR has advanced as far as the ad's own PCR, so output stays real time.
   PAT/PMT/SCTE-35 keep coming from the content.
5. **Ordering.** On each PID the pod waits until the content has passed *S_out*, and
   the content waits until both the pod and the content itself have passed *S_in*
   (ffmpeg muxes audio behind video, so the content audio PES straddling *S_in* can
   arrive after the video IDR and must still be trimmed).
6. **Failure paths.** If the pod is not ready at *S_out*, the break is skipped and
   content plays through. If the content has no IDR on *S_in* (unconditioned
   encoder), the stitcher logs an error, keeps the slate running and returns on the
   next content IDR. That return is not frame-accurate, but the decoder never
   sees a broken GOP.

## Limitations / next steps

- The conform target profile comes from configuration ([scripts/common.sh](scripts/common.sh)),
  not from analysing the incoming stream. A production SSAI system would detect
  the live profile and transcode to it. The conform cache is in memory only.
- A creative is used only once it is fully prepared, before the splice point.
  Nothing is streamed mid-break. If it isn't ready in time, the break is skipped
  (no partial pod).
- In `streaming` delivery mode, ads come as HLS VOD and are remuxed
  (`-c copy`), so the creative must already match the content encode. The HLS
  client handles no encryption, byte ranges or alternate audio renditions.
- Frame accuracy on the return needs a conditioned encoder (an IDR on the
  splice-in frame). That is standard for SCTE-104/35 workflows, but an
  unconditioned feed returns late, on its next IDR.
- Audio splicing is compressed-domain: whole AAC frames, no crossfade. A decoder
  may produce a small click at a hard cut between unrelated audio. Removing that
  needs a decode/re-encode of a few frames around each splice. Only AAC ADTS is
  frame-split. Other audio codecs (AC-3, E-AC-3, MPEG-1 Layer II) fall back to
  whole-PES decisions.
- No T-STD/VBV verification across the splice. It relies on content and ads
  sharing the same CBR VBV settings and mux delay (they share `ENC_ARGS`). The
  analyzer confirms PCR continuity, not buffer compliance.
- Output pacing is "as received". PCR jitter on the SRT egress is whatever
  `srt-live-transmit` produces. A broadcast handoff (ASI/SMPTE 2022) would need a
  CBR re-mux with null-packet stuffing.
- No encrypted SCTE-35, no component-level splicing, single program only.
