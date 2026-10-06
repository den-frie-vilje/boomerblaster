#!/usr/bin/env python3
"""End-to-end tests for boomerblaster-capture: does the output stay locked to
the system clock when the device's clock does not?

Each test runs the real binary for a while, reads what it writes to stdout
(timing each read), parses the status lines it prints to stderr, and then
analyses the audio itself. The device is the built-in simulator unless
BlackHole is present and this process may use the microphone, in which case
one test also drives the real device with a test tone.

The sensitive drift detector is the tone's frequency: a simulated device
running +300 ppm fast plays a 1000 Hz tone that is really 1000.3 Hz, and the
output, played at exactly 44100 frames per second of real time, must contain
1000.3 Hz. A pipeline that ignored the drift would output 1000.0 Hz and the
test fails by 300 ppm against a 20 ppm tolerance. Continuity is checked with a
second-difference residual that is zero for a pure sine and spikes on any gap,
click or repeated block.

Run: python3 test_capture.py            (about 3 minutes; the slow test is 60 s)
     BB_QUICK=1 python3 test_capture.py (skips the slow convergence test)
Python 3 standard library only.
"""

import array
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("BB_CAPTURE", os.path.join(HERE, "boomerblaster-capture"))
RATE = 44100
QUICK = bool(os.environ.get("BB_QUICK"))


# ----------------------------------------------------------------- running

class Run:
    """The helper's output for one run: raw bytes, timed reads, status lines."""

    def __init__(self, args, seconds, stdout_hold=0.0):
        self.args = args
        self.seconds = seconds
        self.stdout_hold = stdout_hold   # leave stdout unread for this long first
        self.chunks = []                 # (monotonic time, cumulative bytes)
        self.data = bytearray()
        self.status = []
        self.log = []
        self.max_stall_ms = 0.0          # worst scheduling stall seen by the harness itself

    def _watch_stalls(self, stop):
        """Sleep 10 ms at a time and note how late each wake-up was. A busy or
        throttled host (a shared CI runner) stalls every process on it, this
        one included, and a stall longer than the helper's lead shows up as an
        underrun or a resync that says nothing about the helper."""
        last = time.monotonic()
        while not stop.is_set():
            time.sleep(0.010)
            now = time.monotonic()
            late = (now - last - 0.010) * 1000
            if late > self.max_stall_ms:
                self.max_stall_ms = late
            last = now

    def _read_stdout(self, pipe):
        if self.stdout_hold:
            time.sleep(self.stdout_hold)
        total = 0
        while True:
            buf = pipe.read1(65536)
            if not buf:
                break
            total += len(buf)
            self.chunks.append((time.monotonic(), total))
            self.data += buf

    def _read_stderr(self, pipe):
        for line in pipe:
            line = line.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("{"):
                try:
                    self.status.append(json.loads(line))
                    continue
                except json.JSONDecodeError:
                    pass
            self.log.append(line)

    def go(self):
        proc = subprocess.Popen([BIN] + self.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.t_start = time.monotonic()
        t1 = threading.Thread(target=self._read_stdout, args=(proc.stdout,))
        t2 = threading.Thread(target=self._read_stderr, args=(proc.stderr,))
        stop = threading.Event()
        t3 = threading.Thread(target=self._watch_stalls, args=(stop,), daemon=True)
        t1.start()
        t2.start()
        t3.start()
        time.sleep(self.seconds)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        stop.set()
        t1.join()
        t2.join()
        t3.join()
        proc.stdout.close()
        proc.stderr.close()
        self.returncode = proc.returncode
        return self

    # -- views

    def left(self):
        a = array.array("h")
        a.frombytes(bytes(self.data[: len(self.data) // 4 * 4]))
        return a[0::2]

    def frames(self):
        return len(self.data) // 4

    def final_status(self):
        return self.status[-1] if self.status else {}

    def status_after(self, t):
        return [s for s in self.status if s["t"] >= t]


# --------------------------------------------------------------- analysis

def regions(samples, rate=RATE, thresh=100, window_ms=2):
    """Split into (start, end, loud) runs, judged per 2 ms window."""
    w = max(1, rate * window_ms // 1000)
    out = []
    n = len(samples)
    i = 0
    cur = None
    while i < n:
        block = samples[i:i + w]
        loud = max(abs(x) for x in block) > thresh
        if cur is None or cur[2] != loud:
            if cur is not None:
                out.append(tuple(cur))
            cur = [i, min(i + w, n), loud]
        else:
            cur[1] = min(i + w, n)
        i += w
    if cur is not None:
        out.append(tuple(cur))
    return out


def tone_regions(samples, rate=RATE, min_ms=50):
    return [(a, b) for a, b, loud in regions(samples, rate) if loud and (b - a) >= rate * min_ms // 1000]


def silent_regions(samples, rate=RATE, min_ms=20):
    return [(a, b) for a, b, loud in regions(samples, rate) if not loud and (b - a) >= rate * min_ms // 1000]


def frequency(samples, start, end, rate=RATE):
    """Frequency of a sine from its upward zero crossings, interpolated."""
    first = last = None
    count = 0
    prev = samples[start]
    for i in range(start + 1, end):
        x = samples[i]
        if prev < 0 <= x:
            t = (i - 1) + (-prev) / (x - prev) if x != prev else float(i)
            if first is None:
                first = t
            last = t
            count += 1
        prev = x
    if count < 2:
        raise ValueError("no tone in the window")
    return (count - 1) / (last - first) * rate


def discontinuities(samples, freq, start, end, rate=RATE, rel=0.01, guard_ms=10):
    """Indices where x[n+1] - 2cos(w)x[n] + x[n-1] is not ~0, which for a pure
    sine of frequency `freq` means a gap, click or repeat. The first and last
    guard_ms of the window are skipped (region edges)."""
    guard = rate * guard_ms // 1000
    c = 2.0 * math.cos(2.0 * math.pi * freq / rate)
    amp = max(abs(x) for x in samples[start:end])
    thresh = rel * amp
    bad = []
    lo, hi = start + guard, end - guard
    xm, x0 = samples[lo - 1], samples[lo]
    for i in range(lo, hi - 1):
        xp = samples[i + 1]
        if abs(xp - c * x0 + xm) > thresh:
            bad.append(i)
            if len(bad) > 50:
                break
        xm, x0 = x0, xp
    return bad


def ppm(actual, expected):
    return (actual / expected - 1.0) * 1e6


# The helper keeps 300 ms of audio ahead of real time; a host that stalls
# longer than this stalls the simulator and the writer too, and the run says
# nothing about the helper. Shared CI runners do this.
STALL_LIMIT_MS = float(os.environ.get("BB_STALL_LIMIT_MS", "150"))


def skip_if_host_stalled(test, run):
    if run.max_stall_ms > STALL_LIMIT_MS:
        test.skipTest(f"the host stalled this process for {run.max_stall_ms:.0f} ms during the run; "
                      "timing assertions are not meaningful on this machine right now")


def pacing(run, after_s=1.0):
    """Spread (max - min, in seconds) of 'bytes delivered vs real time' over the
    reads after `after_s`, and the delivery rate's error in ppm over that span."""
    pts = [(t, b) for t, b in run.chunks if t - run.t_start >= after_s]
    if len(pts) < 10:
        raise ValueError("too few reads")
    devs = [b / 4 / RATE - (t - run.t_start) for t, b in pts]
    (t0, b0), (t1, b1) = pts[0], pts[-1]
    rate = (b1 - b0) / 4 / (t1 - t0)
    return max(devs) - min(devs), ppm(rate, RATE)


# ------------------------------------------------------------------ tests

class SimulatedDevice(unittest.TestCase):

    def check_locked(self, run, device_rate, device_ppm, settle_s, tone_hz=1000.0, freq_tol_ppm=20):
        """The common assertions: the tone comes out at its real-world frequency,
        is continuous, the output is paced at the output rate, and the input
        buffering sits at the setpoint with nothing lost."""
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, f"expected one continuous tone, got regions {tones}")
        a, b = tones[0]
        start = max(a, int(settle_s * RATE))
        self.assertGreater(b - start, 5 * RATE, "need at least 5 s of settled tone")
        f = frequency(left, start, b)
        expect = tone_hz * (1 + device_ppm * 1e-6)
        self.assertLess(abs(ppm(f, expect)), freq_tol_ppm,
                        f"tone is {f:.4f} Hz, expected {expect:.4f} Hz: {ppm(f, expect):+.1f} ppm off "
                        f"(an unlocked pipeline would give {tone_hz:.1f})")
        bad = discontinuities(left, f, a, b)
        self.assertEqual(bad, [], f"discontinuities at samples {bad[:10]} (t={[round(i / RATE, 3) for i in bad[:10]]})")
        spread, rate_err = pacing(run)
        self.assertLess(spread, 0.040, f"output timing wanders by {spread * 1000:.1f} ms")
        self.assertLess(abs(rate_err), 300, f"output delivered at {rate_err:+.0f} ppm off {RATE}")
        fin = run.final_status()
        self.assertEqual(fin["underrun_frames"], 0)
        self.assertEqual(fin["lost_frames"], 0)
        self.assertEqual(fin["dropped_frames"], 0)
        # Only the first start may trim the ring (to set the latency), nothing after.
        self.assertLess(fin["skipped_frames"], device_rate * 0.5)
        self.assert_level_held(fin)
        self.assertEqual(fin["starved"], 0)
        return f

    def assert_level_held(self, fin, band=(40, 140)):
        """The input buffering settled near the setpoint and stayed where the
        controller learned it (a drift would show as a growing offset)."""
        self.assertTrue(band[0] <= fin["fill_avg_ms"] <= band[1], f"buffering at {fin['fill_avg_ms']:.1f} ms")
        self.assertGreater(fin["ref_ms"], 0, "controller never learned its reference")
        self.assertLess(abs(fin["fill_avg_ms"] - fin["ref_ms"]), 10,
                        f"buffering {fin['fill_avg_ms']:.1f} ms drifted from the learned {fin['ref_ms']:.1f} ms")

    def test_fast_48k_device_is_locked(self):
        run = Run(["--sim", "48000,ppm=300,jitter_ms=4", "--status"], 20).go()
        self.check_locked(run, 48000, 300, settle_s=8)
        fin = run.final_status()
        # The timestamp estimate should have found the device's true rate.
        self.assertLess(abs(ppm(fin["rate_in"], 48000 * 1.0003)), 2,
                        f"rate estimate {fin['rate_in']:.3f} vs 48014.4")
        self.assertLess(abs(fin["corr_ppm"]), 400)

    def test_slow_44k1_device_is_locked(self):
        run = Run(["--sim", "44100,ppm=-200,jitter_ms=4", "--status"], 20).go()
        self.check_locked(run, 44100, -200, settle_s=8)

    def test_large_buffers_and_heavy_jitter(self):
        # 2048-frame callbacks (43 ms) with up to 25 ms of delivery jitter: the
        # 60 ms setpoint plus the lead must absorb it without a single underrun.
        run = Run(["--sim", "48000,ppm=150,jitter_ms=25,buffer=2048", "--buffer-ms", "90", "--status"], 20).go()
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, tones)
        a, b = tones[0]
        f = frequency(left, max(a, 8 * RATE), b)
        self.assertLess(abs(ppm(f, 1000 * 1.00015)), 20)
        self.assertEqual(discontinuities(left, f, a, b), [])
        fin = run.final_status()
        self.assertEqual(fin["underrun_frames"], 0)
        self.assertLess(pacing(run)[0], 0.040)

    @unittest.skipIf(QUICK, "BB_QUICK set")
    def test_dishonest_timestamps_still_converge(self):
        # The device runs +300 ppm but its timestamps claim nominal, so only the
        # buffer-level controller can find the rate: the level drifts out of
        # the controller's deadband after ~25 s, then converges with a time
        # constant of about 9 s. After 45 s the tone must be within 60 ppm of
        # its real frequency and the buffering must not have run away.
        run = Run(["--sim", "48000,ppm=300,ts_ppm=0,jitter_ms=4", "--status"], 60).go()
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, tones)
        a, b = tones[0]
        early = frequency(left, 3 * RATE, 8 * RATE)
        late = frequency(left, 45 * RATE, b)
        self.assertLess(abs(ppm(late, 1000.3)), 60, f"late tone {late:.4f} Hz is {ppm(late, 1000.3):+.1f} ppm off 1000.3")
        self.assertLess(abs(ppm(late, 1000.3)), abs(ppm(early, 1000.3)), "the controller should be converging")
        self.assertEqual(discontinuities(left, late, a, b), [])
        fin = run.final_status()
        self.assertEqual(fin["underrun_frames"], 0)
        self.assertTrue(40 <= fin["fill_avg_ms"] <= 140, f"buffering at {fin['fill_avg_ms']:.1f} ms")
        spread, rate_err = pacing(run)
        self.assertLess(spread, 0.040)
        self.assertLess(abs(rate_err), 50, f"output rate off by {rate_err:+.1f} ppm over a minute")

    def test_device_stall_becomes_silence_of_the_same_length(self):
        # The device stops delivering for 400 ms at t=6 s while its clock keeps
        # running. The output must keep flowing on time, carry one stretch of
        # silence of about that length, and resume locked with no extra latency.
        run = Run(["--sim", "48000,ppm=100,jitter_ms=4,stall=6:0.4", "--status"], 20).go()
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 2, f"expected the tone in two pieces around the stall, got {tones}")
        gaps = [(a, b) for a, b in silent_regions(left) if a > tones[0][0]]
        self.assertEqual(len(gaps), 1, f"expected one gap, got {[(a / RATE, b / RATE) for a, b in gaps]}")
        gap_ms = (gaps[0][1] - gaps[0][0]) * 1000 / RATE
        self.assertTrue(380 <= gap_ms <= 480, f"gap is {gap_ms:.0f} ms, stall was 400 ms")
        spread, _ = pacing(run)
        self.assertLess(spread, 0.040, f"output timing wandered by {spread * 1000:.0f} ms across the stall")
        a, b = tones[1]
        f = frequency(left, a + RATE, b)
        self.assertLess(abs(ppm(f, 1000.1)), 20, f"after the stall the tone is {f:.4f} Hz")
        self.assertEqual(discontinuities(left, f, a, b), [])
        f0 = frequency(left, tones[0][0] + 2 * RATE, tones[0][1])
        self.assertEqual(discontinuities(left, f0, *tones[0]), [])
        fin = run.final_status()
        self.assertAlmostEqual(fin["lost_frames"], 0.4 * 48000, delta=2 * 512)
        self.assertGreater(fin["underrun_frames"], 0)
        # Latency is back where it was: the status before and after agree on fill.
        before = [s for s in run.status if 4 <= s["t"] <= 5.5][-1]
        self.assertLess(abs(fin["fill_avg_ms"] - before["fill_avg_ms"]), 15,
                        f"buffering {before['fill_avg_ms']:.1f} ms before, {fin['fill_avg_ms']:.1f} ms after")

    def test_preroll_grows_the_pipe_and_keeps_the_lead(self):
        # Nothing reads stdout for the first 300 ms. The first write is 64 KB of
        # silence, which makes the kernel grow the pipe to hold it, so the
        # writer can then stay 300 ms ahead without ever blocking.
        run = Run(["--sim", "48000,ppm=0", "--status"], 6, stdout_hold=0.3).go()
        skip_if_host_stalled(self, run)
        self.assertGreaterEqual(run.chunks[0][1], 65536, "first read should return the whole 64 KB preroll")
        self.assertEqual(run.final_status()["lead_ms"], 300.0)
        self.assertEqual(run.final_status()["underrun_frames"], 0)
        self.assertLess(pacing(run)[0], 0.040)

    def test_unreadable_pipe_is_not_fatal(self):
        # A 15 ms lead cannot be reduced further, and output to a never-read pipe
        # must still exit cleanly on SIGTERM instead of hanging.
        run = Run(["--sim", "48000,ppm=0", "--lead-ms", "15"], 2, stdout_hold=1.5).go()
        self.assertEqual(run.returncode, 0)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(shutil.which("snapserver"), "snapserver not installed")
class WithSnapserver(unittest.TestCase):
    """snapserver reading the helper through a process source for 30 s must
    never resync (each resync is a timestamp jump listeners hear as a click)."""

    def test_no_resync_in_30s(self):
        tmp = tempfile.mkdtemp(prefix="bbcap-")
        try:
            wrapper = os.path.join(tmp, "capture.sh")
            with open(wrapper, "w") as fh:
                fh.write(f'#!/bin/sh\nexec "{BIN}" --sim 48000,ppm=250,jitter_ms=4 --status\n')
            os.chmod(wrapper, 0o755)
            tcp, stream = free_port(), free_port()
            conf = os.path.join(tmp, "snapserver.conf")
            with open(conf, "w") as fh:
                fh.write(
                    "[server]\nthreads = -1\n"
                    f"datadir = {tmp}\n"
                    "[http]\nenabled = false\n"
                    f"[tcp]\nenabled = true\nport = {tcp}\n"
                    f"[stream]\nport = {stream}\nsampleformat = 44100:16:2\ncodec = pcm\nbuffer = 1000\nchunk_ms = 20\n"
                    f"source = process://{wrapper}?name=Sim&sampleformat=44100:16:2&log_stderr=true\n"
                    "[logging]\nsink = stderr\nfilter = *:info\n"
                )
            proc = subprocess.Popen(["snapserver", "-c", conf], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            watcher = Run([], 0)
            stop = threading.Event()
            threading.Thread(target=watcher._watch_stalls, args=(stop,), daemon=True).start()
            try:
                time.sleep(10)
                state = self.rpc(tcp, "Server.GetStatus")
                streams = {s["id"]: s["status"] for s in state["server"]["streams"]}
                self.assertEqual(streams.get("Sim"), "playing", streams)
                time.sleep(20)
            finally:
                stop.set()
                proc.send_signal(signal.SIGTERM)
                out = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
            skip_if_host_stalled(self, watcher)
            resyncs = [l for l in out.splitlines() if "resync" in l.lower()]
            self.assertEqual(resyncs, [], "snapserver resynced:\n" + "\n".join(resyncs[:10]))
            self.assertIn("Sim", out)
            stat = [json.loads(l[l.index("{"):]) for l in out.splitlines() if '"underrun_frames"' in l]
            self.assertTrue(stat, "no status lines from the helper reached snapserver's log")
            self.assertEqual(stat[-1]["underrun_frames"], 0)
            self.assertLess(abs(stat[-1]["fill_avg_ms"] - stat[-1]["ref_ms"]), 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def rpc(port, method):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall((json.dumps({"id": 1, "jsonrpc": "2.0", "method": method}) + "\r\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)["result"]


def blackhole_ready():
    try:
        out = subprocess.run([BIN, "--check", "--device", "BlackHole 2ch"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False, "cannot run the helper"
    if out.returncode == 2:
        return False, "BlackHole 2ch is not installed"
    if out.returncode == 3:
        return False, "this process may not use the microphone (macOS classes capture as that): " + out.stdout.strip()
    return out.returncode == 0, out.stdout.strip()


class RealDevice(unittest.TestCase):
    """A 1 kHz tone played into BlackHole must come out of the capture at
    1000 Hz against the system clock, continuous, with nothing lost."""

    def test_blackhole_round_trip(self):
        ok, why = blackhole_ready()
        if not ok:
            self.skipTest(why)
        tone = subprocess.Popen([BIN, "--tone-to", "BlackHole 2ch", "--tone-hz", "1000"], stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.5)
            run = Run(["--device", "BlackHole 2ch", "--status"], 15).go()
        finally:
            tone.send_signal(signal.SIGTERM)
            tone.wait(timeout=5)
        skip_if_host_stalled(self, run)
        self.assertTrue(any("capturing" in l for l in run.log), run.log)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, f"expected one continuous tone, got {tones}; log: {run.log}")
        a, b = tones[0]
        f = frequency(left, max(a, 5 * RATE), b)
        self.assertLess(abs(ppm(f, 1000)), 20, f"captured tone is {f:.4f} Hz ({ppm(f, 1000):+.1f} ppm)")
        self.assertEqual(discontinuities(left, f, a, b), [])
        fin = run.final_status()
        self.assertEqual(fin["underrun_frames"], 0)
        self.assertEqual(fin["lost_frames"], 0)
        self.assertLess(abs(fin["fill_avg_ms"] - fin["ref_ms"]), 10)
        spread, rate_err = pacing(run)
        self.assertLess(spread, 0.040)
        self.assertLess(abs(rate_err), 300)


def tap_ready():
    """Process taps need macOS 14.2 and the System Audio Recording permission;
    the helper's --check-tap reports both."""
    try:
        out = subprocess.run([BIN, "--check-tap"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False, "cannot run the helper"
    return out.returncode == 0, out.stdout.strip() or out.stderr.strip()


class ProcessTap(unittest.TestCase):
    """Capturing one application's audio with a Core Audio process tap, no
    virtual device involved. The application under test is the helper's own
    tone player, so the tests know exactly what it plays."""

    def setUp(self):
        ok, why = tap_ready()
        if not ok:
            self.skipTest(why)
        # The tone player needs an output device to play into; BlackHole keeps
        # that silent. Without it (a CI runner) there is nothing to tap.
        out = subprocess.run([BIN, "--check", "--device", "BlackHole 2ch"], capture_output=True, text=True, timeout=10)
        if out.returncode == 2:
            self.skipTest("BlackHole 2ch is not installed; the tap tests play their tone into it")

    def tone(self, hz):
        proc = subprocess.Popen([BIN, "--tone-to", "BlackHole 2ch", "--tone-hz", str(hz)], stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop, proc)
        time.sleep(1.5)
        return proc

    @staticmethod
    def _stop(proc):
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def test_list_apps_names_the_processes_playing_audio(self):
        tone = self.tone(1000)
        out = subprocess.run([BIN, "--list-apps"], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        apps = json.loads(out.stdout)
        self.assertIsInstance(apps, list)
        mine = [a for a in apps if a["pid"] == tone.pid]
        self.assertEqual(len(mine), 1, f"tone player pid {tone.pid} missing from {apps}")
        self.assertTrue(mine[0]["playing"], mine[0])
        self.assertIn("name", mine[0])
        self.assertIn("bundle", mine[0])

    def test_tap_captures_one_process(self):
        tone = self.tone(1000)
        run = Run(["--tap", f"pid:{tone.pid}", "--status"], 14).go()
        skip_if_host_stalled(self, run)
        self.assertTrue(any("tapping" in l for l in run.log), run.log)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, f"expected one continuous tone, got {tones}; log: {run.log}")
        a, b = tones[0]
        f = frequency(left, max(a, 6 * RATE), b)
        self.assertLess(abs(ppm(f, 1000)), 20, f"tapped tone is {f:.4f} Hz ({ppm(f, 1000):+.1f} ppm)")
        self.assertEqual(discontinuities(left, f, a, b), [])
        fin = run.final_status()
        self.assertEqual(fin["underrun_frames"], 0)
        self.assertEqual(fin["lost_frames"], 0)
        self.assertLess(pacing(run)[0], 0.040)

    def test_tap_follows_the_tap_file_at_runtime(self):
        # The DJ picks another app: the helper re-taps within a couple of
        # seconds, and the output carries the new app's audio.
        first, second = self.tone(1000), self.tone(1500)
        tmp = tempfile.mkdtemp(prefix="bbtap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tap_file = os.path.join(tmp, "app")
        with open(tap_file, "w") as fh:
            fh.write(f"pid:{first.pid}\n")
        switcher = threading.Timer(7.0, lambda: open(tap_file, "w").write(f"pid:{second.pid}\n"))
        switcher.start()
        run = Run(["--tap-file", tap_file, "--status"], 16).go()
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertGreaterEqual(len(tones), 1, run.log)
        f_early = frequency(left, 3 * RATE, 6 * RATE)
        f_late = frequency(left, 12 * RATE, len(left) - RATE // 2)
        self.assertLess(abs(ppm(f_early, 1000)), 20, f"before the switch: {f_early:.3f} Hz")
        self.assertLess(abs(ppm(f_late, 1500)), 20, f"after the switch: {f_late:.3f} Hz")
        self.assertLess(pacing(run)[0], 0.040)

    def test_muted_tap_silences_the_app_on_this_mac(self):
        # "mute" on the tap file's second line: the app's sound still reaches
        # the tap but no longer the device it plays to, so the DJ can listen
        # through the page, in sync, without hearing the app twice.
        tone = self.tone(1000)
        tmp = tempfile.mkdtemp(prefix="bbtap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tap_file = os.path.join(tmp, "app")
        with open(tap_file, "w") as fh:
            fh.write(f"pid:{tone.pid}\nmute\n")
        # What BlackHole (the device the tone plays into) carries meanwhile.
        device_out = open(os.path.join(tmp, "device.raw"), "wb")
        device = subprocess.Popen([BIN, "--device", "BlackHole 2ch"], stdout=device_out, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.0)
            run = Run(["--tap-file", tap_file, "--device", "no such device", "--status"], 10).go()
        finally:
            self._stop(device)
            device_out.close()
        skip_if_host_stalled(self, run)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, f"the tap should carry the tone: {tones}; log: {run.log}")
        a, b = tones[0]
        f = frequency(left, max(a, 4 * RATE), b)
        self.assertLess(abs(ppm(f, 1000)), 20)
        with open(os.path.join(tmp, "device.raw"), "rb") as fh:
            data = fh.read()
        dev = array.array("h")
        dev.frombytes(data[: len(data) // 4 * 4])
        dev_left = dev[0::2]
        # Skip the first 3 s (before the tap attached the tone was audible).
        tail = dev_left[3 * RATE:]
        self.assertGreater(len(tail), 4 * RATE, "device capture too short")
        self.assertLess(max(abs(x) for x in tail), 50, "the muted app was still audible on the device")

    def test_tap_waits_for_an_app_that_is_not_running_yet(self):
        # Nothing matches at first: silence, no error, no exit. When the app
        # appears the tap attaches by itself.
        tmp = tempfile.mkdtemp(prefix="bbtap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tap_file = os.path.join(tmp, "app")
        with open(tap_file, "w") as fh:
            fh.write("pid:0\n")   # matches nothing
        holder = {}

        def start_tone():
            holder["tone"] = subprocess.Popen([BIN, "--tone-to", "BlackHole 2ch", "--tone-hz", "1000"], stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            with open(tap_file, "w") as fh:
                fh.write(f"pid:{holder['tone'].pid}\n")
        threading.Timer(4.0, start_tone).start()
        try:
            # No real device behind the tap file's empty state, so the only
            # way audio can appear is through the tap.
            run = Run(["--tap-file", tap_file, "--device", "no such device", "--status"], 14).go()
        finally:
            if "tone" in holder:
                self._stop(holder["tone"])
        skip_if_host_stalled(self, run)
        self.assertEqual(run.returncode, 0)
        left = run.left()
        tones = tone_regions(left)
        self.assertEqual(len(tones), 1, f"expected the tone to appear once the app started, got {tones}; log: {run.log}")
        self.assertGreater(tones[0][0], 3 * RATE, "tone should start only after the app appeared")
        f = frequency(left, tones[0][0] + 2 * RATE, tones[0][1])
        self.assertLess(abs(ppm(f, 1000)), 20)


if __name__ == "__main__":
    if not os.path.exists(BIN):
        sys.exit(f"build the helper first: make -C {HERE}")
    unittest.main(verbosity=2)
