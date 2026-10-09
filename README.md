# 📻 BoomerBlaster

![Office workers dancing at standing desks in headphones while a colleague works undisturbed](docs/img/hero.jpg)

> BoomerBlaster is a silent disco for the open-plan office. You play music from
> your phone or your Mac; everyone who wants to listen opens a web page and
> hears it in their own headphones, in time with everyone else. The colleague
> who wants quiet hears nothing at all.

The landing page, in the register of a product launch:
**[den-frie-vilje.github.io/boomerblaster](https://den-frie-vilje.github.io/boomerblaster/)**.
This README is the manual.

## Check whether you need it

You need BoomerBlaster if all of the following are true:

- You share an open-plan office and a speaker would annoy someone.
- Everyone has headphones and a laptop or a phone with a browser.
- You want to play from the apps you already use: Music, Spotify, Tidal,
  anything that can AirPlay, or Spotify on any phone.
- You want the listeners in sync with each other, and you want the page to
  show what is playing, with cover art.

BoomerBlaster runs on one Mac, the DJ's. It needs nothing installed anywhere
else. The Mac must be on the same network as the listeners and, if it is a
laptop, it should stay open and awake for the length of the set.

## What you need

- A Mac with [Homebrew](https://brew.sh). If Terminal answers
  `command not found: brew`, install it first.
- A network where devices can reach each other. Office Wi-Fi sometimes
  isolates clients; if colleagues cannot open the page, that is the first
  thing to ask about. A cable into the DJ's Mac helps with twenty listeners.
- For Spotify Connect, a Spotify Premium account. librespot, the receiver
  BoomerBlaster uses, does not work with free accounts.

## Install BoomerBlaster

```sh
brew install den-frie-vilje/tap/boomerblaster
```

This also installs the programs BoomerBlaster drives: `snapcast`,
`shairport-sync` (the tap's build, with AirPlay 2), `nqptp` and `librespot`.

AirPlay 2 casts against a clock, and `nqptp` is that clock. BoomerBlaster
starts it with the server and stops it with the server, as you, no sudo.

Or from a clone of this repository, run `./install.sh`, which checks the
same dependencies and copies the command into `~/.local/bin`.

## Set up BoomerBlaster

1. Run:

   ```sh
   boomerblaster init
   ```

   BoomerBlaster checks that the three programs are present, installs the
   listener page, fetches your own control page (Snapweb, a pinned release
   whose checksum it verifies), and writes its configuration. It answers
   with something like:

   ```
   found snapserver at /opt/homebrew/bin/snapserver
   found shairport-sync at /opt/homebrew/bin/shairport-sync
   found librespot at /opt/homebrew/bin/librespot
   listener page installed from /opt/homebrew/share/boomerblaster/listener
   fetching the admin page (Snapweb 0.9.3)
   admin page unpacked in /Users/you/Library/Application Support/boomerblaster/www/admin
   config written to /Users/you/.config/boomerblaster/config.json
   snapserver.conf written to /Users/you/.config/boomerblaster/snapserver.conf
   ```

   To give the venue a name other than "BoomerBlaster", the name phones will see,
   run `boomerblaster init --name "Third floor"` instead.

2. Start it:

   ```sh
   boomerblaster start
   ```

   The server keeps running in the background, also after you log in again,
   until you run `boomerblaster stop`. If macOS asks whether `snapserver`,
   `shairport-sync`, `nqptp` or `librespot` may accept incoming connections,
   allow it; that is listeners and phones reaching your Mac.

   If the Mac's own AirPlay Receiver is on, it occupies port 7000, which
   AirPlay needs, and `start` says so instead of starting. Turn it off in
   System Settings → General → AirDrop & Handoff → AirPlay Receiver;
   BoomerBlaster is the AirPlay receiver now. `boomerblaster stop` hands
   AirPlay back: it stops `nqptp`, so this Mac casts as normal again, and
   reminds you to turn the AirPlay Receiver back on.

3. Get the address to send round:

   ```sh
   boomerblaster url
   ```

   It prints two: one with your Mac's name, such as `http://oles-mbp.local:1780`,
   and one with its IP address for devices that cannot resolve the name.

4. Check that everything is in place with `boomerblaster doctor`.

## Let colleagues in

![The listener page on a desktop display, a laptop, a phone and a beige CRT monitor, lined up on black: on each, the cover art large with its colours blurred into the background, "Low Tide (Office Edit)" by The Elevation Desks beneath, one volume slider between two speaker glyphs, and a line reading "In sync · 2 listening"](docs/img/listener.jpg)

Colleagues open the address in any browser, tap the artwork and put on their
headphones. The page shows the cover art large, the track and artist
underneath, and one slider with a mute button that moves their own ears only.
There is nothing else on it: play, pause and skip belong to your phone.
The lock screen and keyboard media keys show the track but do not control it.

Your own controls are at `/admin/` on the same address: Snapweb, Snapcast's
control panel, where you can see every listener, set the stream, and rename or
remove a client. Keep that address to yourself.

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

### Play from the Mac itself

To DJ from an app on the server Mac (djay Pro, SoundCloud in a browser,
anything), BoomerBlaster can broadcast whatever that app plays, with the
track from macOS's own Now Playing. AirPlay cannot do this from the same
Mac, so it goes through a virtual audio device instead:

1. `brew install blackhole-2ch nowplaying-cli`, then `sudo killall coreaudiod`
   once (or log out and in) so the device appears. The second program reads
   Now Playing; without it the audio still flows, without the track.
2. `boomerblaster set sysaudio true` and `boomerblaster restart`.
3. In the DJ app, choose **BlackHole 2ch** as the output device. In an app
   without its own output picker, make BlackHole the system output in
   System Settings > Sound, or create a Multi-Output Device in Audio MIDI
   Setup to hear it locally as well.
4. The first time, macOS asks for **Microphone** access: capturing any audio
   device, even a virtual one, counts as that. Allow it. Nothing listens to
   the microphone; `doctor` shows the state of the permission.

The source is called **System** and sits last in line: an AirPlay or Spotify
sender takes over while it plays. `boomerblaster set sysaudio_device "Name"`
picks another input device.

**One app instead of a device.** On macOS 14.2 or later the capture can
take one application's sound directly, with a Core Audio process tap, and
nothing else on the Mac: no BlackHole, no output-device fiddling, the app
keeps playing to your headphones, and notifications stay private.
`boomerblaster capture-app "djay Pro"` (part of the name, or a bundle id)
switches within a second, `capture-app device` goes back to the virtual
device, and `boomerblaster url --admin` prints the address of a picker
page that does the same from a phone. Add `--mute` (or tick the box on the
page) to silence the app on the Mac itself while it plays out: then you
listen on the page like everyone else, in sync, instead of hearing the app
a second ahead. The first time, macOS asks for **System Audio
Recording**; allow it.

## What to expect

- **Sync.** Listeners play each chunk of audio at a time agreed with the
  server, so laptops land within a few tens of milliseconds of each other.
  Nobody hears anyone else's headphones, so that is comfortably enough.
- **Delay.** From your thumb to their ears takes about four seconds: two
  from AirPlay's own buffering, two from the server's, which is what lets a
  phone in a pocket keep playing. Nobody is beat-matching across the office.
- **Cover art.** AirPlay from Music and most players sends title, artist,
  album and the artwork. Spotify Connect sends what librespot reports,
  title and artist at least.
- **Bandwidth.** With the default FLAC codec, each listener takes about
  700 kbit/s. Twenty listeners over Wi-Fi is comfortable; forty wants a cable.
- **Sleep.** While the server runs, the Mac is kept from idle sleep. Closing
  the lid still ends the set.
- **Pockets.** On an iPhone or iPad the page keeps playing with the screen
  locked, shows the track on the lock screen, and ignores the ringer switch:
  it plays through a media element, which iOS treats as music, not as a web
  page's sound.

## Settings

BoomerBlaster writes and reads these files, and nothing else:

- `~/.config/boomerblaster/config.json`, the settings below
- `~/.config/boomerblaster/snapserver.conf`, generated from them
- `~/Library/Application Support/boomerblaster/www/`, the listener page, with
  the admin page under `admin/`; the server's own state sits beside it
- `~/Library/LaunchAgents/dk.denfrievilje.boomerblaster.plist`, while running
- `~/Library/Logs/boomerblaster.log`, the server log

`config.json` keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | `"BoomerBlaster"` | what phones see in the AirPlay and Spotify Connect lists |
| `http_port` | `1780` | the listener page's port |
| `stream_port`, `control_port` | `1704`, `1705` | Snapcast's audio and control ports |
| `codec` | `"flac"` | `pcm`, `flac` or `opus`; pcm is the most robust, opus the lightest |
| `buffer_ms` | `2000` | time between stamping a chunk and playing it; raise on poor Wi-Fi, lower for snappier controls |
| `airplay` | `true` | offer an AirPlay receiver |
| `airplay_version` | `2` | `2`: AirPlay 2 (needs nqptp); `1`: classic, no PTP clock, so this Mac can cast too. `cast on/off` flips it |
| `spotify` | `true` | offer a Spotify Connect receiver |
| `spotify_bitrate` | `320` | 96, 160 or 320 |
| `sysaudio` | `false` | broadcast what this Mac plays into a virtual audio device |
| `sysaudio_device` | `"BlackHole 2ch"` | the input device to capture when `sysaudio` is on |
| `sysaudio_app` | `""` | capture this application instead (macOS 14.2+); `capture-app` sets it |
| `sysaudio_app_mute` | `false` | silence the captured app on this Mac; `capture-app --mute` sets it |

Change any of them from the command line, for example
`boomerblaster set codec opus` or `boomerblaster set spotify false`, then
`boomerblaster restart`.

## All commands

```
init            check programs, fetch the listener page, write config
start           run in the background until stop, also after login
stop            stop and remove the background job
restart         apply changed settings
run             run in the foreground (what start runs for you)
status          listeners, live source, current track (--json for scripts)
url             the address to send round (--admin for yours; --json for scripts)
doctor          check programs, files, ports, hostname and firewall
set KEY VALUE   change a setting
config          print settings and file locations
logs [-f]       show the server log
disarm          same as stop; run before brew uninstall
cast [on|off]   on: cast AirPlay from this Mac (receiver drops to classic AirPlay); off: AirPlay 2 again
capture-app [NAME|device] [--mute]  which application's sound the System source carries; no restart
version         print the version
```

`boomerblaster status --json` returns Snapcast's full server state and exits 3
when the server is down, so a script can poll it. `boomerblaster doctor` exits 2
when something needs attention.

## Uninstall

```sh
boomerblaster stop
brew uninstall boomerblaster
```

Or `./uninstall.sh` from a clone. Both leave the configuration, the listener
page and the log in place; the paths above tell you what to delete for a
clean slate.

## How it works

BoomerBlaster is one Python file that configures and supervises the
open-source programs that do the work:

- [shairport-sync](https://github.com/mikebrady/shairport-sync) receives
  AirPlay (2 and classic) and hands over audio and metadata.
- [nqptp](https://github.com/mikebrady/nqptp) keeps the PTP clock AirPlay 2
  senders cast against. It runs as a Homebrew service, outside
  BoomerBlaster's supervision, because it must own UDP ports 319 and 320
  for as long as anything AirPlays.
- [librespot](https://github.com/librespot-org/librespot) receives Spotify
  Connect.
- [snapserver](https://github.com/snapcast/snapcast) takes both, stamps every
  20 ms of audio against one clock and streams it to listeners.
- The listener page in `listener/` plays the stream in sync and shows the
  track. Its audio engine and control client are Snapweb's own modules,
  copied in with their GPL-3 licence; the page itself adds the artwork, the
  volume and nothing else. It is built with Vite into `listener/dist`, which
  is committed and installed with the formula.
- [Snapweb](https://github.com/snapcast/snapweb), Snapcast's control panel,
  is served at `/admin/` for the DJ. `init` downloads a pinned release and
  verifies its checksum.

- `capture/boomerblaster-capture`, a small CoreAudio program of our own, is
  the System source when `sysaudio` is on. It reads the virtual device,
  measures the device's clock against the system clock from the HAL's
  timestamps, resamples to 44.1 kHz at the ratio that keeps the two locked
  (a windowed-sinc resampler, 128 taps), and writes PCM to snapserver paced
  to real time, so snapserver never finds a chunk late and never resyncs.
  Audio the device skips becomes silence of the same length, not a time
  shift. `capture/test_capture.py` proves this end to end against a
  simulated device whose clock runs hundreds of ppm off, against a real
  snapserver, and, where BlackHole is present, against the real device.
  With `capture-app`, the same program reads a Core Audio process tap of
  the chosen application instead of the device, through a plain HAL IO
  proc, and re-taps by itself when the app starts, quits or is changed.
  `plug-ins/meta_nowplaying.py` reads macOS's Now Playing (via
  [nowplaying-cli](https://github.com/kirtan-shah/nowplaying-cli)) and
  attaches title, artist, album and artwork to the stream; it also serves
  the app picker page, one port above the listener page.

`boomerblaster init` writes a `snapserver.conf` with one stream per receiver and
a `meta` stream that wraps them, set as the default source, so a new listener
lands on a single stream named after the venue and hears whichever source is
playing. Both receivers deliver
44.1 kHz stereo, so nothing is resampled; only the System capture resamples,
and only because the virtual device may run at another rate.

## Limits

- **Chromecast is not a source.** Commercial apps check that a Cast receiver
  holds a certificate issued by Google, and no open-source receiver can get
  one. Spotify Connect covers the usual reason for wanting it. If you need a
  Chromecast anyway, a physical one with audio out into a USB interface can
  be captured with a `process://` source; title and cover art are lost.
- **AirPlay 2 needs its clock.** The tap's shairport-sync is built for
  AirPlay 2 (it answers classic AirPlay too, on the same port, 7000), so
  AirPlay-2-only apps see it. The timing daemon it casts against, `nqptp`,
  runs as a Homebrew service that `start` starts and `stop` stops; under
  `brew services start boomerblaster`, start it yourself with
  `brew services start nqptp`. The build is pinned to shairport-sync's development
  branch until AirPlay-2-on-macOS ships in a release. The same two ports
  are what macOS's own AirPlay sender uses, so while the receiver runs as
  AirPlay 2, this Mac cannot cast to other AirPlay 2 speakers.
  `boomerblaster cast on` drops the receiver to classic AirPlay, which
  needs no PTP clock, stops nqptp and restarts the server: phones still
  cast to it, AirPlay-2-only apps no longer list it, and this Mac can cast.
  `cast off` brings AirPlay 2 back.
- **Browsers vary.** Chrome, Edge and Firefox report their output latency
  precisely; Safari less so, and may sit a few tens of milliseconds off.

## Licence

MIT for the command, the installer and this documentation. The listener page in
`listener/` is GPL-3.0-or-later, because it builds on Snapweb's modules; its
licence file sits in that directory. The programs BoomerBlaster installs and
downloads carry their own licences: Snapcast and Snapweb GPL-3, shairport-sync
and librespot MIT, nqptp GPL-2.
