#!/usr/bin/env python3
"""Snapcast stream control script: macOS "Now Playing" -> stream metadata.

BoomerBlaster can broadcast whatever plays on the DJ's Mac by capturing the
system audio through a virtual device. That audio carries no metadata, so this
script supplies it separately: it reads macOS's system-wide Now Playing
information -- the same title, artist and artwork that appear in Control Center,
for *any* app that reports it (a browser playing SoundCloud, djay Pro, Music,
Spotify) -- and pushes it into the stream over Snapcast's plug-in protocol, so
the listener page shows it exactly as it does for AirPlay.

It speaks the plug-in protocol on stdio: newline-delimited JSON-RPC. On start it
announces Plugin.Stream.Ready; thereafter it sends Plugin.Stream.Player.Properties
whenever the track or playback state changes, and answers GetProperties. Control
commands are refused on purpose -- transport belongs to the player, not the
listeners.

Metadata comes from `nowplaying-cli` (reads the private MediaRemote API).
Artwork arrives as TIFF; it is converted to JPEG with `sips` and written under
the served web root, so every listener loads it from their own origin.

Usage (as a Snapcast controlscript):
    controlscript=meta_nowplaying.py&controlscriptparams=--www <doc_root>
"""

import hashlib
import html
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

POLL_SECONDS = 1.5
ART_SUBDIR = "np-art"

NOWPLAYING = None  # resolved at startup
SIPS = "/usr/bin/sips"


def which(name):
    for p in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ):
        if os.access(p, os.X_OK):
            return p
    from shutil import which as _which
    return _which(name)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def log(msg):
    # Surfaces in the snapserver log via the plug-in's Log channel.
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Log",
          "params": {"severity": "Info", "message": f"[nowplaying] {msg}"}})


# ------------------------------------------------------------ now playing

def np_raw():
    """Read all Now Playing fields from MediaRemote in one call.

    `nowplaying-cli get elapsedTime` is unreliable (returns 0 while the real
    value sits in the raw record), so parse the raw JSON. Artwork is left as
    base64 here and only decoded when the track changes.
    """
    try:
        out = subprocess.run(
            [NOWPLAYING, "get-raw"],
            capture_output=True, text=True, timeout=6,
        ).stdout
        data = json.loads(out) if out.strip() else {}
    except Exception:
        return {}
    k = "kMRMediaRemoteNowPlayingInfo"
    return {
        "title": data.get(k + "Title"),
        "artist": data.get(k + "Artist"),
        "album": data.get(k + "Album"),
        "duration": data.get(k + "Duration"),
        "elapsed": data.get(k + "ElapsedTime"),
        "rate": data.get(k + "PlaybackRate"),
        "artData": data.get(k + "ArtworkData"),
    }


def np_artwork_jpeg(www_dir, key, b64):
    """Transcode the given artwork (base64 TIFF/PNG/JPEG) to a web JPEG under
    the web root and return a same-origin path. Cached by track key, so each
    track is transcoded once, not once per poll."""
    art_dir = os.path.join(www_dir, ART_SUBDIR)
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    jpeg = os.path.join(art_dir, digest + ".jpg")
    rel = "/" + ART_SUBDIR + "/" + digest + ".jpg"
    if os.path.exists(jpeg):
        return rel
    if not b64:
        return None
    os.makedirs(art_dir, exist_ok=True)
    try:
        import base64
        raw = base64.b64decode(b64)
        src = os.path.join(art_dir, digest + ".src")
        with open(src, "wb") as fh:
            fh.write(raw)
        r = subprocess.run(
            [SIPS, "-s", "format", "jpeg", src, "--out", jpeg],
            capture_output=True, timeout=8,
        )
        os.remove(src)
        if r.returncode != 0 or not os.path.exists(jpeg):
            return None
        _prune_art(art_dir, keep=jpeg)
        return rel
    except Exception:
        return None


def _prune_art(art_dir, keep):
    try:
        for name in os.listdir(art_dir):
            path = os.path.join(art_dir, name)
            if path != keep and name.endswith(".jpg"):
                os.remove(path)
    except OSError:
        pass


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_nowplaying(www_dir):
    """Return a Snapcast properties dict for the current Now Playing, or a
    'stopped' dict when nothing is playing."""
    f = np_raw()
    title = f.get("title")
    artist = f.get("artist")
    if not title and not artist:
        return {"playbackStatus": "stopped", "canControl": False}

    rate = to_float(f.get("rate"))
    status = "playing" if (rate is None or rate > 0) else "paused"

    key = (title or "") + "\x00" + (str(artist) if artist else "")
    meta = {}
    if title:
        meta["title"] = title
    if artist:
        meta["artist"] = artist if isinstance(artist, list) else [artist]
    if f.get("album"):
        meta["album"] = f["album"]
    dur = to_float(f.get("duration"))
    if dur and dur > 0:
        meta["duration"] = dur
    art = np_artwork_jpeg(www_dir, key, f.get("artData"))
    if art:
        meta["artUrl"] = art

    # Advance the playhead from the last anchor, re-anchoring to the source's
    # reported elapsed on a track change or a seek (a jump away from where we
    # extrapolated). Hold still while paused.
    global _track_key, _anchor_elapsed, _anchor_wall, _last_playing, _last_reported
    now = time.time()
    reported = to_float(f.get("elapsed"))
    # A source that reports a live, advancing position (Apple Music) changes it
    # each poll; one that reports a stale snapshot (djay Pro) keeps the same
    # value. Only a *changed* report that jumps is a real seek -- otherwise keep
    # advancing from our anchor so a stale snapshot doesn't drag the playhead back.
    reported_changed = reported is not None and reported != _last_reported
    cur = _anchor_elapsed + ((now - _anchor_wall) if (_anchor_wall and _last_playing) else 0.0)
    if key != _track_key or not _anchor_wall:
        cur = reported if reported is not None else 0.0
    elif reported_changed and abs(reported - cur) > 3.0:
        cur = reported
    _track_key = key
    _anchor_elapsed = max(0.0, cur)
    _anchor_wall = now
    _last_playing = (status == "playing")
    _last_reported = reported

    return {
        "playbackStatus": status,
        "canControl": False,
        "canGoNext": False,
        "canGoPrevious": False,
        "canPlay": False,
        "canPause": False,
        "canSeek": False,
        "position": _anchor_elapsed,
        "metadata": meta,
    }


# ------------------------------------------------------------ plug-in I/O

_last_sig = None
_last_props = {"playbackStatus": "stopped", "canControl": False}
_lock = threading.Lock()

# MediaRemote gives a snapshot of elapsed time with no timestamp, and some
# players don't advance it at all, so we anchor the last known elapsed to the
# wall-clock moment we read it and advance from there. Re-anchored on a track
# change or a seek. This keeps every listener's playhead in agreement.
_track_key = None
_anchor_elapsed = 0.0
_anchor_wall = 0.0
_last_playing = False
_last_reported = None


def signature(props):
    meta = props.get("metadata", {})
    return (props.get("playbackStatus"), meta.get("title"),
            tuple(meta.get("artist", [])), meta.get("album"),
            meta.get("artUrl"))


def stdin_loop():
    """Answer snapserver's requests. Control is deliberately refused."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        if method == "Plugin.Stream.Player.GetProperties":
            with _lock:
                send({"jsonrpc": "2.0", "id": rid, "result": _last_props})
        elif method in ("Plugin.Stream.Player.Control",
                        "Plugin.Stream.Player.SetProperty"):
            # Transport belongs to the player on the DJ's Mac, not listeners.
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "control not supported"}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": "ok"})


# ----------------------------------------------------------- app picker
#
# A small page for the DJ: which application's sound goes out. Choosing one
# writes the capture helper's tap file; the helper notices within a second
# and re-taps. An empty choice means the virtual device (BlackHole) again.

CAPTURE_HELPER = "boomerblaster-capture"
PICKER_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Which app plays out</title>
<style>
  body {{ font: 17px/1.4 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px; background: #000; color: #f5f5f7; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  p {{ color: #a1a1a6; margin: 0 0 18px; }}
  form {{ max-width: 560px; }}
  label {{ display: flex; align-items: center; gap: 12px; padding: 12px 0; border-top: 1px solid #333; cursor: pointer; }}
  label small {{ color: #a1a1a6; margin-left: auto; text-align: right; }}
  .playing {{ color: #30d158; }}
  label.mute {{ margin-top: 10px; border-top: 1px solid #333; color: #d2d2d7; }}
  button {{ margin-top: 18px; font: inherit; padding: 10px 18px; border-radius: 10px; border: 0; background: #0a84ff; color: #fff; }}
</style>
<h1>Which app plays out</h1>
<p>The sound of one application goes to the listeners; everything else on this Mac stays private. Apps making sound right now are marked.</p>
<form method="post" action="select">
{rows}
<label class="mute"><input type="checkbox" name="mute"{mute_checked}> Silence the app on this Mac while it plays out, so you can listen on the page like everyone else</label>
<button>Use this one</button>
</form>
<script>
setTimeout(function () {{ location.reload(); }}, 15000);
</script>
"""


def picker_rows(apps, current):
    rows = []
    checked = ' checked' if current == "" else ''
    rows.append(f'<label><input type="radio" name="app" value=""{checked}> The virtual device (BlackHole)</label>')
    seen = set()
    for app in sorted(apps, key=lambda a: (not a.get("playing"), (a.get("name") or "").lower())):
        key = app.get("bundle") or f"pid:{app.get('pid')}"
        if key in seen:
            continue
        seen.add(key)
        checked = ' checked' if key == current else ''
        mark = ' <span class="playing">&#9679; playing</span>' if app.get("playing") else ''
        rows.append(
            f'<label><input type="radio" name="app" value="{html.escape(key, quote=True)}"{checked}> '
            f'{html.escape(app.get("name") or key)}{mark}<small>{html.escape(key)}</small></label>'
        )
    return "\n".join(rows)


def valid_app_choice(value):
    """A bundle id, pid:N, or empty. Nothing that could be a path or a flag."""
    if value == "":
        return True
    if len(value) > 200 or value.startswith("-"):
        return False
    return all(c.isalnum() or c in ".-_: " for c in value)


def read_tap_file(path):
    """(app, mute): line 1 names the app, line 2 may say "mute"."""
    try:
        with open(path) as fh:
            lines = [l.strip() for l in fh.read().split("\n")]
    except OSError:
        return "", False
    app = lines[0] if lines else ""
    mute = len(lines) > 1 and lines[1].lower() == "mute"
    return app, mute


def write_tap_file(path, app, mute):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(app + "\n" + ("mute\n" if mute else ""))


def picker_server(address, list_apps, tap_file):
    """An HTTP server for the picker; call serve_forever() on it."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, ctype, body, extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                apps = list_apps()
            except Exception as exc:  # the helper may be missing
                log(f"picker: cannot list apps: {exc}")
                apps = []
            current, mute = read_tap_file(tap_file)
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8",
                           PICKER_PAGE.format(rows=picker_rows(apps, current), mute_checked=" checked" if mute else ""))
            elif path == "/apps.json":
                self._send(200, "application/json", json.dumps({"apps": apps, "current": current, "mute": mute}))
            else:
                self._send(404, "text/plain", "not found")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path != "/select":
                return self._send(404, "text/plain", "not found")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode(errors="replace") if length else ""
            form = urllib.parse.parse_qs(body, keep_blank_values=True)
            value = (form.get("app") or [""])[0].strip()
            if not valid_app_choice(value):
                return self._send(400, "text/plain", "not an application")
            mute = bool(form.get("mute")) and value != ""
            write_tap_file(tap_file, value, mute)
            log(f"picker: capture set to {value or 'the virtual device'}{' (muted here)' if mute else ''}")
            self._send(303, "text/plain", "", {"Location": "/"})

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    return http.server.ThreadingHTTPServer(address, Handler)


def find_capture_helper():
    here = os.path.dirname(os.path.realpath(__file__))
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(here, "..", "libexec", "boomerblaster", CAPTURE_HELPER),
        os.path.join(here, "..", "capture", CAPTURE_HELPER),
        os.path.join(home, "Library", "Application Support", "boomerblaster", CAPTURE_HELPER),
    ):
        candidate = os.path.normpath(candidate)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def list_apps_via_helper():
    helper = find_capture_helper()
    if not helper:
        return []
    out = subprocess.run([helper, "--list-apps"], capture_output=True, text=True, timeout=10)
    return json.loads(out.stdout) if out.returncode == 0 else []


def start_picker(port, tap_file):
    try:
        server = picker_server(("0.0.0.0", port), list_apps=list_apps_via_helper, tap_file=tap_file)
    except OSError as exc:
        log(f"picker: cannot listen on port {port}: {exc}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"picker: app picker on port {port}")


def main():
    global NOWPLAYING, _last_sig, _last_props
    www_dir = os.environ.get("BOOMERBLASTER_WWW", "")
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--www" and i + 1 < len(args):
            www_dir = args[i + 1]
    if not www_dir:
        home = os.path.expanduser("~")
        www_dir = os.path.join(home, "Library", "Application Support",
                               "boomerblaster", "www")

    # --picker PORT --tap-file PATH: serve the app picker (see above).
    picker_port, tap_file = 0, ""
    for i, a in enumerate(args):
        if a == "--picker" and i + 1 < len(args):
            picker_port = int(args[i + 1])
        if a == "--tap-file" and i + 1 < len(args):
            tap_file = args[i + 1]
    if picker_port and tap_file:
        start_picker(picker_port, tap_file)

    NOWPLAYING = which("nowplaying-cli")
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"})
    if not NOWPLAYING:
        log("nowplaying-cli not found; metadata disabled "
            "(brew install nowplaying-cli)")
        # Stay alive so the audio source keeps running.
        stdin_loop()
        return

    threading.Thread(target=stdin_loop, daemon=True).start()

    while True:
        try:
            props = read_nowplaying(www_dir)
        except Exception as exc:
            log(f"poll error: {exc}")
            props = {"playbackStatus": "stopped", "canControl": False}
        with _lock:
            _last_props = props
        # Send every poll, not only on track change: the position advances, and
        # snapserver caches the latest properties for listeners who join later,
        # so a fresh position keeps every page's playhead in agreement.
        send({"jsonrpc": "2.0",
              "method": "Plugin.Stream.Player.Properties",
              "params": props})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
