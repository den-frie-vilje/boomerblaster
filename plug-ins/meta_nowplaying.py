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
import json
import os
import subprocess
import sys
import threading
import time

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
