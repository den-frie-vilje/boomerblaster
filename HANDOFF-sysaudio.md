# Handoff: system-audio (local DJing) prototype

Branch: `system-audio-source` (off `main`). Goal of the work: let the DJ play
from **any app on the Mac itself** (djay Pro, a browser on SoundCloud) and have
listeners hear it in sync on the listener page, with Now Playing metadata.

This file is the state at the end of a long session. Audio quality is close but
**not finished** — occasional gaps remain — and productionization is not done.
Delete this file once the work lands.

## Why capture at all (two architectural dead-ends ruled out first)

1. **AirPlay 2 cannot work from the same Mac.** The AP2 receiver's clock,
   `nqptp`, binds UDP **319/320 exclusively** (verified: even with
   SO_REUSEADDR/REUSEPORT a second bind fails, errno 48). macOS's own AirPlay-2
   *sender* (Music, daftcloud, anything using the system stack) also needs PTP
   on 319/320, so sender+receiver can't coexist on one Mac. Classic AirPlay
   worked same-host (no PTP); AP2 can't. A sender on a **different** device on
   the same Wi-Fi works fine (proven with pyatv streaming to the receiver).
2. **All macOS audio capture needs a TCC permission.** Reading any input device
   (incl. a virtual loopback like BlackHole) is "Microphone" to macOS. There is
   no permission-free capture. ScreenCaptureKit would use "Screen Recording"
   instead (no mic), but needs a native helper.

So: capture the Mac's output via a virtual device and stream it. That's this
prototype.

## Architecture (what the committed code does)

```
djay Pro / browser → [BlackHole 2ch output] → capture.sh (ffmpeg, clock-locked)
   → snapserver `process` source "System" → meta stream "BoomerBlaster" → listeners
                                           ↑ controlscript = plug-ins/meta_nowplaying.py
                                             (reads macOS Now Playing → stream metadata)
```

- `boomerblaster`: new `sysaudio` + `sysaudio_device` config. When on,
  `render_conf()` emits a `process://…/capture.sh?...&controlscript=meta_nowplaying.py`
  source named **System**, adds it LAST in the meta order, and sets `plugin_dir`.
  `write_capture()` generates `~/.config/boomerblaster/capture.sh`.
- `plug-ins/meta_nowplaying.py`: Snapcast control script. Reads `nowplaying-cli
  get-raw` (MediaRemote), transcodes TIFF artwork → JPEG with `sips` into the
  web root, sends `Plugin.Stream.Player.Properties` every poll.
- `listener/`: playbar (from the earlier branch) + a client-only **stop** button.

## What WORKS (verified)

- **Metadata end-to-end.** Title/artist/album/artwork/duration reach the stream
  and render on the page, for SoundCloud-in-browser and djay Pro.
- **Position.** Anchored elapsed advances monotonically, consistent across
  listeners; uses the source's real value when it provides one.
- **Stop button.** Leaves the stream for that listener only; server unaffected.
- **Audio quality — mostly.** The clock-locked adaptive capture + `process`
  source cut resyncs from ~4/sec (clicks) to ~0.3/sec. Proven: a 10 s capture
  to a file is 8.94 s raw vs exactly 10.00 s with the adaptive resampler.

## What's NOT done (the two hard problems + one minor)

### 1. Capture still has occasional GAPS (the blocker)
~0.3 resync/sec is close to AirPlay, but the user still hears occasional
dropouts under load. Not yet root-caused. Leads to try next:
- Set **BlackHole to 44.1 kHz** in Audio MIDI Setup so there's no 48→44.1
  resample at all (djay drives it to 48 k now). Capture native, skip resample.
- Tune `buffer_ms` (user is fine with latency — raise it) and `chunk_ms`.
- The gaps may be djay/BlackHole underruns (between tracks, cueing) — the
  adaptive resampler stalls when input pauses; `aresample` needs `min_hard_comp`
  tuning to insert silence through gaps WITHOUT the stall that `first_pts=0`
  caused. (`min_hard_comp=0.100` was tried briefly; stalled via the FIFO path,
  not re-tried on the process source.)
- Consider a purpose-built capture: a small Swift/CoreAudio or **ScreenCaptureKit**
  helper that does its own ring-buffer + drift compensation. More robust than
  ffmpeg-avfoundation, and SCK avoids the mic permission (uses Screen Recording).

### 2. Mic permission under launchd (productionization)
The capture only works because the stack is run **foreground** via
`boomerblaster run` from a session whose responsible process has the Microphone
grant. Under the normal launchd job, the snapserver-spawned ffmpeg gets no mic →
System stays idle. Options: a dedicated capture **LaunchAgent** with its own TCC
identity (grant once); a signed GUI helper; or ScreenCaptureKit (Screen
Recording grant). Needs investigation of how TCC attributes a launchd-spawned
child.

### 3. Default stream assignment (minor)
New listeners default to the **AirPlay** stream (first source), not the venue
meta stream, so they see "nothing playing" until moved. Fix: make listeners
default to `BoomerBlaster` — try listing the `meta` source FIRST in
`render_conf` (verify Snapcast still resolves its references), or a startup RPC
that sets all groups to the venue stream, or find a Snapcast default-stream
setting.

## Gotchas discovered (don't re-derive these)

- **MediaRemote WORKS on macOS 26** (26.6.2) via `nowplaying-cli` 2.1.0 — the
  15.4 lockdown assumption was wrong here.
- `nowplaying-cli get elapsedTime` returns **0**; the real value is in
  `get-raw` as `kMRMediaRemoteNowPlayingInfoElapsedTime`. No timestamp field, so
  it's a snapshot → anchor it.
- **djay Pro** reports a stale/snapshot elapsed (often 0, sometimes a frozen
  value); not AppleScript-scriptable; bundle `com.algoriddim.djay-iphone-free`.
- Artwork from MediaRemote is **TIFF** (`MM\0*`); browsers need JPEG → `sips`.
- **BlackHole runs at 48 kHz** (driven by djay). The meta stream is 44.1 k
  (AirPlay/Spotify), so System must be 44.1 k to join it → resample needed
  unless BlackHole is set to 44.1 k.
- ffmpeg avfoundation: no `-audio_buffer_size`; `-drop_late_frames` exists
  (default true). Capturing any input device triggers **Mic TCC**.
- Adaptive capture that works: `-use_wallclock_as_timestamps 1 -af
  aresample=async=1000`. **Do NOT add `first_pts=0`** — it stalls the feed.
- Snapcast **`process` source ≫ `pipe`/FIFO** for timing. The FIFO's 64 KB
  kernel buffer overflows on the resampler's startup burst and stalls; the
  process source lets snapserver pace the read.
- Not installed/available: `timeout`, `mbuffer`. Installed this session:
  `blackhole-2ch` (cask, needs sudo + `sudo killall coreaudiod`),
  `nowplaying-cli`, `pv`.

## How to run the demo (current working-ish setup)

1. djay Pro output device → **BlackHole 2ch** (its own output picker; keeps
   system sounds out of the stream; headphone cue stays separate).
2. Microphone permission granted to the session's responsible process.
3. `./boomerblaster set sysaudio true` (regenerates conf + capture.sh).
4. `./boomerblaster stop` then run FOREGROUND: `./boomerblaster run`
   (so the spawned capture inherits the mic grant).
5. Move listeners to the venue stream:
   `Group.SetStream {id, stream_id:"BoomerBlaster"}` over TCP 1705, until the
   default-assignment fix lands.

## Related state

- AirPlay 2 work is on **main** (merged earlier): shairport-sync AP2 + nqptp via
  the tap, `prgr` progress in snapserver. Tap PRs #1 (prgr) and #2 (AP2) are
  OPEN, pinned to upstream dev branches. `main` is pushed to origin.
- `main` also has the landing-page fixes (phone-corner Safari clip, hero crop).
- Spotify Connect works locally (librespot, no PTP) — a real alternative to
  capture for Spotify.
