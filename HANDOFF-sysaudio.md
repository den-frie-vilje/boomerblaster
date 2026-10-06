# Handoff: system-audio (local DJing) source

Branch: `system-audio-source` (off `main`). Goal: let the DJ play from **any
app on the Mac itself** (djay Pro, a browser on SoundCloud) and have listeners
hear it in sync on the listener page, with Now Playing metadata.

State at the end of the second session: the capture is rewritten and its clock
lock is proven by tests; the remaining open items are the two below. Delete
this file once the work lands.

## Architecture

```
djay Pro / browser → [BlackHole 2ch output] → capture.sh → capture/boomerblaster-capture
   → snapserver `process` source "System" → meta stream "BoomerBlaster" → listeners
                                           ↑ controlscript = plug-ins/meta_nowplaying.py
                                             (reads macOS Now Playing → stream metadata)
```

- `boomerblaster`: `sysaudio` + `sysaudio_device` config. `render_conf()`
  emits `process://…/capture.sh?…&controlscript=meta_nowplaying.py&log_stderr=true`
  as source **System**, last in the meta order. `write_capture()` writes
  `~/.config/boomerblaster/capture.sh`, which execs the helper.
  `capture_helper()` finds the built helper (brew `libexec/boomerblaster/`,
  the clone's `capture/`, or `DATA_DIR`) and `init` compiles it into
  `DATA_DIR` from `capture/` when none is found (needs clang). `doctor` has a
  "system audio" section (`--check`: device found, mic permission state).
- `capture/boomerblaster-capture.m` (ObjC/C, CoreAudio): the capture. See
  "Why ffmpeg was dropped" and the file's header comment.
- `capture/test_capture.py`: the end-to-end tests (`make -C capture test`,
  about 3 minutes, stdlib only). CI runs them on `macos-latest`.
- `plug-ins/meta_nowplaying.py`: Snapcast control script, unchanged from the
  first session (works: title/artist/album/artwork/duration/position).
- `listener/`: playbar + client-only **stop** button (first session).

## Why ffmpeg was dropped (the gaps, root-caused)

ffmpeg's avfoundation input keeps **one** audio frame: when its read loop is
late (any backpressure on stdout, any scheduling hiccup), the frame is
replaced and audio is lost. `aresample=async` then stretched the remainder to
fill real time, which is exactly the "8.94 s raw vs 10.00 s resampled" seen in
session one, and the audible gaps under load. No ffmpeg flag changes this.

The replacement never drops anything: the CoreAudio IO thread only fills a
lock-free ring; a writer thread resamples from it and writes to stdout paced
to the system clock, so snapserver's process source always has the next 20 ms
chunk waiting. Specifically:

- **Clock lock.** The device's real rate is measured from the HAL's
  `(mHostTime, mSampleTime)` pairs over a 2–60 s window (exact to ~1 ppm);
  the resampling step follows it. A second, gentle controller (τ = 20 s)
  holds the input buffering at the level it settled at, so it never bends the
  pitch to chase a startup offset. Test: a simulated device +300 ppm fast
  produces a 1000 Hz tone that must come out at 1000.3 Hz within 20 ppm.
- **Resampler.** Windowed sinc (Kaiser β = 10, 128 taps at the lower rate),
  512-phase polyphase table with linear interpolation, variable step. Costs a
  few percent of a core. Continuity test: second-difference residual of the
  output sine, zero for a clean sine, spikes on any gap, click or repeat.
- **Pacing.** The first write is 64 KB of silence: XNU grows a pipe to fit a
  big write, so the pipe then holds the 300 ms lead (the max a 64 KB pipe can:
  371 ms). The writer thread has real-time (time-constraint) scheduling.
  snapserver (v0.35, `AsioStream::do_read`) resyncs whenever a chunk read
  completes after its scheduled tick + 20 ms, so the lead is what matters.
  Test: real snapserver + process source for 30 s, zero `onResync` lines.
- **Gaps.** Frames the device skips (sample-time jump) become the same
  number of zero frames; after a real gap the ring is trimmed back to the
  setpoint so the gap is one stretch of silence and latency does not grow.
  Test: 400 ms stall → one 380–480 ms silence, pacing undisturbed, lock kept.
- **CoreAudio off the writer.** All CoreAudio calls run on a device thread.
  coreaudiod blocks callers indefinitely while a permission prompt is open
  (seen this session: it blocked *every* process opening BlackHole, including
  ffmpeg and the server's own capture), and the output must keep flowing
  (silence) through that. Device gone / rate changed / permission granted →
  the device thread restarts input; the writer follows a generation counter.

Status lines (`--status`, JSON per second on stderr) carry fill, measured rate,
correction ppm, underruns, lost/skipped frames; the tests assert on them.

## What is NOT done

### 1. Verify on the real device (blocked on a prompt, then 15 minutes)

Everything above is proven against the simulator and a real snapserver, not
yet against BlackHole, because this session's shell has no microphone grant
and its permission prompt is sitting unanswered on screen (it also blocks
coreaudiod for everyone, see above). To finish:

1. Answer the prompt (Allow) for the terminal / Claude app. Then
   `make -C capture test` runs `RealDevice.test_blackhole_round_trip`
   (tone into BlackHole via `--tone-to`, captured, must be 1000 Hz ± 20 ppm,
   continuous, no underruns). Expect ~0 ppm: BlackHole runs on the system
   clock.
2. `boomerblaster logs -f`, play djay Pro into BlackHole for a few minutes,
   confirm **no `onResync (System)` lines** and listen on a browser.
   Earlier this session one `onResync (System): 13.7 ms` appeared, during a
   coreaudiod stall caused by the pending prompt, with the old 150 ms lead
   and no real-time writer; both were changed afterwards. If any resync
   appears with the new build, the status lines are the first thing to read:
   `boomerblaster-capture --device "BlackHole 2ch" --status > /dev/null`.
3. If the stream is still not clean, `--lead-ms` cannot go above ~350 (pipe
   size); the remaining knob is snapserver's `buffer_ms` (irrelevant to
   resyncs) or a `tcp://` source instead of the pipe (unbounded lead).

### 2. Microphone permission under launchd (productionization)

Observed this session: under the LaunchAgent the helper reached "capturing"
within 150 ms with **no permission prompt and no denial message**, i.e. the
job's identity (`sys.executable`, python3.11) is already authorized on this
Mac, probably from a grant in the first session. Unknown: what a fresh Mac
does. The helper asks explicitly via `AVCaptureDevice requestAccess…` at
start and logs `asking…` / `granted` / `denied` to the server log, and the
embedded Info.plist carries a `NSMicrophoneUsageDescription`, so on a fresh
Mac the prompt should appear attributed to the launchd job's program. To
verify: `tccutil reset Microphone` is too blunt (all apps); better test on
another Mac or a fresh user account. If the prompt is attributed to
"python3" that is ugly but workable; a dedicated LaunchAgent for the helper
feeding snapserver over `tcp://` would give it its own identity and a stable
name in the prompt.

### 3. Default stream assignment (minor, unchanged)

New listeners default to the **AirPlay** stream (first source), not the venue
meta stream. Fix options: list the `meta` source first in `render_conf` (check
Snapcast still resolves it), or a startup RPC setting all groups to the venue
stream.

### 4. Homebrew formula (release time)

See RELEASING.md step 4: the formula must `make -C capture` and install the
helper to `libexec/boomerblaster/` and `plug-ins` to `pkgshare`.

## Gotchas (don't re-derive)

- **coreaudiod blocks on an open TCC prompt** for every client of a device
  with input streams, even output-only clients (ffmpeg's audiotoolbox output
  hung too). Symptom: a process stuck in `AudioUnitSetProperty` →
  `AudioDeviceCreateIOProcID` → `mach_msg`. Answer the prompt.
- **MediaRemote works on macOS 26** (26.6.2) via `nowplaying-cli` 2.1.0.
- `nowplaying-cli get elapsedTime` returns 0; use `get-raw`'s
  `kMRMediaRemoteNowPlayingInfoElapsedTime`, a snapshot → anchor it.
- **djay Pro** reports a stale elapsed; not AppleScript-scriptable; bundle
  `com.algoriddim.djay-iphone-free`.
- Artwork from MediaRemote is TIFF; browsers need JPEG → `sips`.
- **BlackHole runs at 48 kHz** here (driven by djay). The helper resamples
  whatever the device rate is, so this no longer matters; setting BlackHole
  to 44.1 kHz would only make the ratio ~1.
- Snapcast **`process` source** runs the command with cwd = its directory,
  splits parameters on spaces (hence `capture.sh`), reads stdout
  non-blocking, and logs the child's stderr only with `log_stderr=true`.
- macOS pipes start at 16 KB and grow to 64 KB only when a single write
  larger than the buffer lands in an empty pipe (XNU `pipe_write`); the
  helper's 64 KB preroll relies on this.
- `timeout`, `mbuffer` are not installed; `pv`, `nowplaying-cli`,
  `blackhole-2ch` are.

## How to run the demo

1. djay Pro output device → **BlackHole 2ch**.
2. `boomerblaster set sysaudio true`, `boomerblaster restart`
   (sysaudio is currently **on** in this Mac's config).
3. Allow the Microphone prompt if one appears.
4. Move listeners to the venue stream until item 3 lands:
   `Group.SetStream {id, stream_id:"BoomerBlaster"}` over TCP 1705.

## Related state

- AirPlay 2 on **main**: shairport-sync AP2 + nqptp via the tap, `prgr`
  progress in snapserver. Tap PRs #1 (prgr) and #2 (AP2) are OPEN, pinned to
  upstream dev branches. AP2 cannot work from the same Mac (nqptp owns UDP
  319/320; the Mac's own sender needs them too); a sender on another device
  on the same Wi-Fi works.
- Spotify Connect works locally (librespot, no PTP).
