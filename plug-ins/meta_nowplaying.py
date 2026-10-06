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

def np_get(fields):
    """Fetch the light fields in one call; returns a dict (missing -> None)."""
    try:
        out = subprocess.run(
            [NOWPLAYING, "get"] + fields,
            capture_output=True, text=True, timeout=4,
        ).stdout.splitlines()
    except Exception:
        return {}
    values = {}
    for i, field in enumerate(fields):
        v = out[i].strip() if i < len(out) else ""
        values[field] = None if v in ("", "null") else v
    return values


def np_artwork_jpeg(www_dir, key):
    """Pull the current artwork, transcode to JPEG under the web root, and
    return a same-origin path (or None). Cached by track key so we transcode
    once per track, not once per poll."""
    art_dir = os.path.join(www_dir, ART_SUBDIR)
    os.makedirs(art_dir, exist_ok=True)
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    jpeg = os.path.join(art_dir, digest + ".jpg")
    rel = "/" + ART_SUBDIR + "/" + digest + ".jpg"
    if os.path.exists(jpeg):
        return rel
    try:
        b64 = subprocess.run(
            [NOWPLAYING, "get", "artworkData"],
            capture_output=True, text=True, timeout=6,
        ).stdout.strip()
        if not b64 or b64 == "null":
            return None
        import base64
        raw = base64.b64decode(b64)
        src = os.path.join(art_dir, digest + ".src")
        with open(src, "wb") as fh:
            fh.write(raw)
        # sips reads TIFF/PNG/JPEG and writes a web-friendly JPEG.
        r = subprocess.run(
            [SIPS, "-s", "format", "jpeg", src, "--out", jpeg],
            capture_output=True, timeout=8,
        )
        os.remove(src)
        if r.returncode != 0 or not os.path.exists(jpeg):
            return None
        # Keep the directory from growing without bound.
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
    f = np_get(["title", "artist", "album", "duration",
                "elapsedTime", "playbackRate"])
    title = f.get("title")
    artist = f.get("artist")
    if not title and not artist:
        return {"playbackStatus": "stopped", "canControl": False}

    rate = to_float(f.get("playbackRate"))
    status = "playing" if (rate is None or rate > 0) else "paused"

    meta = {}
    if title:
        meta["title"] = title
    if artist:
        meta["artist"] = [artist]
    if f.get("album"):
        meta["album"] = f["album"]
    dur = to_float(f.get("duration"))
    if dur and dur > 0:
        meta["duration"] = dur
    art = np_artwork_jpeg(www_dir, (title or "") + "" + (artist or ""))
    if art:
        meta["artUrl"] = art

    props = {
        "playbackStatus": status,
        "canControl": False,
        "canGoNext": False,
        "canGoPrevious": False,
        "canPlay": False,
        "canPause": False,
        "canSeek": False,
        "metadata": meta,
    }
    pos = to_float(f.get("elapsedTime"))
    if pos is not None:
        props["position"] = pos
    return props


# ------------------------------------------------------------ plug-in I/O

_last_sig = None
_last_props = {"playbackStatus": "stopped", "canControl": False}
_lock = threading.Lock()


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
        sig = signature(props)
        with _lock:
            _last_props = props
        if sig != _last_sig:
            _last_sig = sig
            send({"jsonrpc": "2.0",
                  "method": "Plugin.Stream.Player.Properties",
                  "params": props})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
