# 🎧 Floorfill

![Office workers dancing at standing desks in headphones while a colleague works undisturbed](docs/img/hero.jpg)

> Floorfill is a silent disco for the open-plan office. You play music from
> your phone or your Mac; everyone who wants to listen opens a web page and
> hears it in their own headphones, in time with everyone else. The colleague
> who wants quiet hears nothing at all.

## Check whether you need it

You need Floorfill if all of the following are true:

- You share an open-plan office and a speaker would annoy someone.
- Everyone has headphones and a laptop or a phone with a browser.
- You want to play from the apps you already use: Music, Spotify, Tidal,
  anything that can AirPlay, or Spotify on any phone.
- You want the listeners in sync with each other, and you want the page to
  show what is playing, with cover art.

Floorfill runs on one Mac, the DJ's. It needs nothing installed anywhere
else. The Mac must be on the same network as the listeners and, if it is a
laptop, it should stay open and awake for the length of the set.

## What you need

- A Mac with [Homebrew](https://brew.sh). If Terminal answers
  `command not found: brew`, install it first.
- A network where devices can reach each other. Office Wi-Fi sometimes
  isolates clients; if colleagues cannot open the page, that is the first
  thing to ask about. A cable into the DJ's Mac helps with twenty listeners.
- For Spotify Connect, a Spotify Premium account. librespot, the receiver
  Floorfill uses, does not work with free accounts.

## Install Floorfill

```sh
brew install den-frie-vilje/tap/floorfill
```

This also installs the three programs Floorfill drives: `snapcast`,
`shairport-sync` and `librespot`.

Or from a clone of this repository, run `./install.sh`, which checks the
same dependencies and copies the command into `~/.local/bin`.

## Set up Floorfill

1. Run:

   ```sh
   floorfill init
   ```

   Floorfill checks that the three programs are present, fetches the
   listener page (Snapweb, a pinned release whose checksum it verifies), and
   writes its configuration. It answers with something like:

   ```
   found snapserver at /opt/homebrew/bin/snapserver
   found shairport-sync at /opt/homebrew/bin/shairport-sync
   found librespot at /opt/homebrew/bin/librespot
   fetching the listener page (Snapweb 0.9.3)
   listener page unpacked in /Users/you/Library/Application Support/floorfill/snapweb
   config written to /Users/you/.config/floorfill/config.json
   snapserver.conf written to /Users/you/.config/floorfill/snapserver.conf
   ```

   To give the venue a name other than "Floorfill", the name phones will see,
   run `floorfill init --name "Third floor"` instead.

2. Start it:

   ```sh
   floorfill start
   ```

   The server keeps running in the background, also after you log in again,
   until you run `floorfill stop`. If macOS asks whether `snapserver`,
   `shairport-sync` or `librespot` may accept incoming connections, allow it;
   that is listeners and phones reaching your Mac.

3. Get the address to send round:

   ```sh
   floorfill url
   ```

   It prints two: one with your Mac's name, such as `http://oles-mbp.local:1780`,
   and one with its IP address for devices that cannot resolve the name.

4. Check that everything is in place with `floorfill doctor`.

## Let colleagues in

Colleagues open the address in any browser, tap the play button and put on
their headphones. The page shows the current track, artist, album and cover
art, and a volume control for their own ears only.

Laptops are the best listeners. Phones work too while the page is in the
foreground; iOS stops the audio when the screen locks.

## Play something

From an iPhone, iPad or Mac: open any app that can AirPlay, choose AirPlay
and pick the device named after your venue. Music, Spotify, Tidal, YouTube
and podcasts all qualify.

From any phone with Spotify: open Spotify, choose "connect to a device" and
pick the same name.

Both sources feed the same page. If both play at once, AirPlay wins and
Spotify waits.

## What to expect

- **Sync.** Listeners play each chunk of audio at a time agreed with the
  server, so laptops land within a few tens of milliseconds of each other.
  Nobody hears anyone else's headphones, so that is comfortably enough.
- **Delay.** From your thumb to their ears takes about two seconds. AirPlay
  buffers that much by design. Nobody is beat-matching across the office.
- **Cover art.** AirPlay from Music and most players sends title, artist,
  album and the artwork. Spotify Connect sends what librespot reports,
  title and artist at least.
- **Bandwidth.** With the default FLAC codec, each listener takes about
  700 kbit/s. Twenty listeners over Wi-Fi is comfortable; forty wants a cable.
- **Sleep.** While the server runs, the Mac is kept from idle sleep. Closing
  the lid still ends the set.

## Settings

Floorfill writes and reads these files, and nothing else:

- `~/.config/floorfill/config.json`, the settings below
- `~/.config/floorfill/snapserver.conf`, generated from them
- `~/Library/Application Support/floorfill/`, the listener page and the
  server's own state
- `~/Library/LaunchAgents/dk.denfrievilje.floorfill.plist`, while running
- `~/Library/Logs/floorfill.log`, the server log

`config.json` keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | `"Floorfill"` | what phones see in the AirPlay and Spotify Connect lists |
| `http_port` | `1780` | the listener page's port |
| `stream_port`, `control_port` | `1704`, `1705` | Snapcast's audio and control ports |
| `codec` | `"flac"` | `pcm`, `flac` or `opus`; pcm is the most robust, opus the lightest |
| `buffer_ms` | `1000` | time between stamping a chunk and playing it; raise on poor Wi-Fi |
| `airplay` | `true` | offer an AirPlay receiver |
| `spotify` | `true` | offer a Spotify Connect receiver |
| `spotify_bitrate` | `320` | 96, 160 or 320 |

Change any of them from the command line, for example
`floorfill set codec opus` or `floorfill set spotify false`, then
`floorfill restart`.

## All commands

```
init            check programs, fetch the listener page, write config
start           run in the background until stop, also after login
stop            stop and remove the background job
restart         apply changed settings
run             run in the foreground (what start runs for you)
status          listeners, live source, current track (--json for scripts)
url             the address to send round (--json for scripts)
doctor          check programs, files, ports, hostname and firewall
set KEY VALUE   change a setting
config          print settings and file locations
logs [-f]       show the server log
disarm          same as stop; run before brew uninstall
version         print the version
```

`floorfill status --json` returns Snapcast's full server state and exits 3
when the server is down, so a script can poll it. `floorfill doctor` exits 2
when something needs attention.

## Uninstall

```sh
floorfill stop
brew uninstall floorfill
```

Or `./uninstall.sh` from a clone. Both leave the configuration, the listener
page and the log in place; the paths above tell you what to delete for a
clean slate.

## How it works

Floorfill is one Python file that configures and supervises three
open-source programs:

- [shairport-sync](https://github.com/mikebrady/shairport-sync) receives
  AirPlay and hands over audio and metadata.
- [librespot](https://github.com/librespot-org/librespot) receives Spotify
  Connect.
- [snapserver](https://github.com/snapcast/snapcast) takes both, stamps every
  20 ms of audio against one clock and streams it to listeners.
- [Snapweb](https://github.com/snapcast/snapweb), Snapcast's browser client,
  plays the stream in sync with Web Audio and shows the track. `init`
  downloads a pinned release and verifies its checksum; it is not bundled
  here because it is GPL-3 and Floorfill is MIT.

`floorfill init` writes a `snapserver.conf` with one stream per receiver and
a `meta` stream that wraps them, so listeners sit on a single stream named
after the venue and hear whichever source is playing. Both receivers deliver
44.1 kHz stereo, so nothing is resampled.

## Limits

- **Chromecast is not a source.** Commercial apps check that a Cast receiver
  holds a certificate issued by Google, and no open-source receiver can get
  one. Spotify Connect covers the usual reason for wanting it. If you need a
  Chromecast anyway, a physical one with audio out into a USB interface can
  be captured with a `process://` source; title and cover art are lost.
- **AirPlay 1, not 2.** Homebrew's shairport-sync is built for classic
  AirPlay. Phones and Macs cast to it without noticing; the difference is
  multi-room, which Floorfill does not need.
- **Browsers vary.** Chrome, Edge and Firefox report their output latency
  precisely; Safari less so, and may sit a few tens of milliseconds off.

## Licence

MIT. The programs Floorfill installs and downloads carry their own licences:
Snapcast and Snapweb GPL-3, shairport-sync and librespot MIT.
