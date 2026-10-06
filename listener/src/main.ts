// BoomerBlaster listener page.
//
// One job: play the venue's stream in sync and show what is playing.
// The listener controls their own volume and mute, and nothing else;
// transport belongs to the DJ's phone. Playback and the control
// connection come from Snapweb's own modules (snapstream.ts and
// snapcontrol.ts, GPL-3.0-or-later), which this file drives.

import './style.css'
import { SnapStream } from './snapstream.ts'
import { SnapControl, Snapcast } from './snapcontrol.ts'

const host = import.meta.env.VITE_APP_SNAPSERVER_HOST || window.location.host
const wsBase = (window.location.protocol === 'https:' ? 'wss://' : 'ws://') + host
const httpBase = window.location.protocol + '//' + host

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const app = $('app')
const backdrop = $('backdrop')
const artButton = $<HTMLButtonElement>('art')
const titleEl = $('title')
const artistEl = $('artist')
const trackLink = $<HTMLAnchorElement>('track-link')
const playbar = $('playbar')
const pbPos = $('pb-pos')
const pbFill = $('pb-fill')
const pbDur = $('pb-dur')
const muteButton = $<HTMLButtonElement>('mute')
const slider = $<HTMLInputElement>('slider')
const statusEl = $('status')

const clientId = SnapStream.getClientId()
const control = new SnapControl()
let stream: SnapStream | undefined
let lastArt = ''
let volume = { percent: 60, muted: false }
let sliderBusy = false

// ---------------------------------------------------------------- control

control.onConnectionChanged = (_c, connected, error) => {
  if (!connected) setStatus(error ? 'Reconnecting' : 'Connecting', true)
}

control.onChange = (_c, server) => render(server)

control.connect(wsBase)

function myClient(server: Snapcast.Server): Snapcast.Client | null {
  return server.getClient(clientId)
}

function myStream(server: Snapcast.Server): Snapcast.Stream | null {
  const client = myClient(server)
  if (client) {
    const group = server.groups.find(g => g.clients.some(c => c.id === client.id))
    if (group) return server.getStream(group.stream_id)
  }
  // Not registered yet (nobody has tapped Listen): show whatever is playing.
  return server.streams.find(s => s.status === 'playing') || server.streams[0] || null
}

function listenerCount(server: Snapcast.Server): number {
  let n = 0
  for (const g of server.groups) for (const c of g.clients) if (c.connected) n++
  return n
}

// ----------------------------------------------------------------- render

function render(server: Snapcast.Server) {
  const client = myClient(server)
  if (client && !sliderBusy) {
    volume = { percent: client.config.volume.percent, muted: client.config.volume.muted }
    applyVolumeUi()
  }

  const s = myStream(server)
  const meta = s?.properties.metadata
  const playing = s?.status === 'playing'
  app.classList.toggle('idle', !playing)

  if (playing && meta && (meta.title || meta.artist?.length)) {
    const title = meta.title || 'Untitled'
    const line = [meta.artist?.join(', '), meta.album].filter(Boolean).join(' · ')
    titleEl.textContent = title
    artistEl.textContent = line
    document.title = line ? `${title} – ${meta.artist?.join(', ') ?? ''}` : title
    setArt(meta.artUrl ? resolveArt(meta.artUrl) : '')
    setMediaSession(title, meta.artist?.join(', ') ?? '', meta.album ?? '', lastArt)
    setTrackLink(vendorUrl(meta, spotifyIsLive(server)))
    setProgress(meta, s?.properties.position)
  } else if (playing) {
    titleEl.textContent = 'Now playing'
    artistEl.textContent = s?.id ?? ''
    document.title = 'BoomerBlaster'
    setArt('')
    setTrackLink('')
    setProgress(undefined, undefined)
  } else {
    titleEl.textContent = stream ? 'Nothing playing' : 'BoomerBlaster'
    artistEl.textContent = stream ? 'Ask the DJ.' : 'Tap the artwork to listen'
    document.title = 'BoomerBlaster'
    setArt('')
    setTrackLink('')
    setProgress(undefined, undefined)
  }

  const n = listenerCount(server)
  const who = n === 1 ? '1 listening' : `${n} listening`
  setStatus(stream ? `In sync · ${who}` : `Tap the artwork to listen · ${who}`)
}

// snapserver builds its cover-art links from its own idea of the host
// name, which other devices often cannot resolve; the page and the
// server share an origin, so point those links back at it. Art hosted
// elsewhere (Spotify's CDN) passes through untouched.
function resolveArt(artUrl: string): string {
  try {
    const url = new URL(artUrl, httpBase + '/')
    if (url.pathname.startsWith('/__image_cache')) return httpBase + url.pathname + url.search
    return url.toString()
  } catch {
    return ''
  }
}

// ---------------------------------------------------------------- playbar
//
// Noninteractive on purpose: seeking belongs to the DJ's phone. The time
// shown is the playhead of the track, never "how long this page has been
// open": a position reported by the source is used as is; without one
// (AirPlay sends none through snapserver) the page counts from a track
// boundary it has witnessed, and until it has witnessed one — a page
// opened mid-track — it shows –:–– rather than a number that lies.

let trackKey = ''
let trackStartedAt = 0
let anchored = false
let idleSeen = false
let knownPos: number | undefined
let knownPosAt = 0
let knownDur: number | undefined
let progressing = false

function setProgress(meta: Snapcast.Metadata | undefined, position: number | undefined) {
  progressing = !!meta
  if (!meta) {
    idleSeen = true
    return
  }
  const key = [meta.title, meta.artist?.join(','), meta.album].join(' ')
  const changed = key !== trackKey
  if (changed) {
    // The first key after connecting is a track already in flight,
    // unless the stream was idle until now; every later change is a
    // track boundary seen live, and the count from it is the playhead.
    anchored = trackKey !== '' || idleSeen
    trackKey = key
    trackStartedAt = performance.now()
  }
  knownDur = meta.duration
  if (position !== undefined) {
    knownPos = position
    knownPosAt = performance.now()
  } else if (changed) {
    knownPos = undefined
  }
}

function fmtTime(s: number): string {
  s = Math.max(0, Math.floor(s))
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

window.setInterval(() => {
  if (!progressing || document.visibilityState !== 'visible') return
  const now = performance.now()
  const pos = knownPos !== undefined
    ? knownPos + (now - knownPosAt) / 1000
    : anchored ? (now - trackStartedAt) / 1000 : undefined
  if (pos !== undefined && knownDur) {
    playbar.classList.remove('no-dur')
    const clamped = Math.min(pos, knownDur)
    pbPos.textContent = fmtTime(clamped)
    pbDur.textContent = fmtTime(knownDur)
    pbFill.style.width = (100 * clamped / knownDur).toFixed(2) + '%'
  } else {
    playbar.classList.add('no-dur')
    pbPos.textContent = pos !== undefined ? fmtTime(pos) : '–:––'
  }
}, 500)

// ------------------------------------------------------------ vendor link

function spotifyIsLive(server: Snapcast.Server): boolean {
  const playing = (id: string) => server.streams.some(st => st.id === id && st.status === 'playing')
  // Mirrors the meta stream's order: AirPlay wins when both play.
  return playing('Spotify') && !playing('AirPlay')
}

function vendorUrl(meta: Snapcast.Metadata, spotify: boolean): string {
  if (meta.url && /^https?:\/\//.test(meta.url)) return meta.url
  const spotifyTrack = meta.trackId?.match(/spotify(?::|\/)track(?::|\/)([A-Za-z0-9]+)/)
  if (spotifyTrack) return 'https://open.spotify.com/track/' + spotifyTrack[1]
  const q = [meta.title, meta.artist?.join(' ')].filter(Boolean).join(' ')
  if (!q) return ''
  return spotify
    ? 'https://open.spotify.com/search/' + encodeURIComponent(q)
    : 'https://music.apple.com/search?term=' + encodeURIComponent(q)
}

function setTrackLink(href: string) {
  if (href) {
    trackLink.href = href
    trackLink.classList.add('linked')
    trackLink.setAttribute('aria-label', 'Open this track in your music service')
  } else {
    trackLink.removeAttribute('href')
    trackLink.classList.remove('linked')
    trackLink.removeAttribute('aria-label')
  }
}

// Two stacked layers for the artwork and two for the blurred backdrop:
// the next image loads into the hidden layer and fades in over the old
// one, so a track change never cuts. A load that fails leaves the old
// art in place; a stream without art fades out to the glyph.
const artLayers = [$<HTMLImageElement>('art-a'), $<HTMLImageElement>('art-b')]
const bdLayers = [$('bd-a'), $('bd-b')]
let front = 0
let loading: HTMLImageElement | null = null

function setArt(url: string) {
  if (url === lastArt) return
  lastArt = url
  if (!url) {
    artLayers.forEach(l => l.classList.remove('show'))
    bdLayers.forEach(l => l.classList.remove('show'))
    app.classList.remove('has-art')
    return
  }
  const probe = new Image()
  loading = probe
  probe.decoding = 'async'
  probe.onload = () => {
    if (loading !== probe || lastArt !== url) return
    const back = 1 - front
    artLayers[back].src = url
    bdLayers[back].style.backgroundImage = `url("${url}")`
    // Next frame, so the browser paints the hidden layer before fading it.
    requestAnimationFrame(() => {
      artLayers[back].classList.add('show')
      bdLayers[back].classList.add('show')
      artLayers[front].classList.remove('show')
      bdLayers[front].classList.remove('show')
      front = back
      app.classList.add('has-art')
      window.setTimeout(() => {
        // Free the old layer once the fade has finished.
        const old = 1 - front
        if (!artLayers[old].classList.contains('show')) artLayers[old].removeAttribute('src')
      }, 900)
    })
  }
  probe.onerror = () => { if (loading === probe) loading = null }
  probe.src = url
}

function setStatus(text: string, warn = false) {
  statusEl.textContent = text
  statusEl.classList.toggle('warn', warn)
}

function setMediaSession(title: string, artist: string, album: string, art: string) {
  if (!('mediaSession' in navigator)) return
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title, artist, album,
      artwork: art ? [{ src: art, sizes: '512x512' }] : [],
    })
    // No transport handlers on purpose: play, pause and skip belong to the DJ.
    for (const action of ['play', 'pause', 'previoustrack', 'nexttrack', 'seekto'] as MediaSessionAction[]) {
      try { navigator.mediaSession.setActionHandler(action, null) } catch { /* unsupported action */ }
    }
  } catch { /* older browsers */ }
}

// ----------------------------------------------------------------- listen

function startListening() {
  if (stream) return
  // Must happen inside the tap: browsers only let a gesture start audio.
  stream = new SnapStream(wsBase)
  app.classList.add('playing')
  artButton.setAttribute('aria-label', 'Listening')
  setStatus('In sync')
  // Our own client appears in the server state a moment after Hello.
  window.setTimeout(() => control.connect(wsBase), 800)
}

let hideStop: number | undefined
function revealStop() {
  app.classList.add('show-stop')
  window.clearTimeout(hideStop)
  hideStop = window.setTimeout(() => app.classList.remove('show-stop'), 3000)
}

// Leaves the stream for THIS listener only — closes this client's socket and
// audio; the server stream plays on for everyone else. Returns the page to its
// just-loaded state.
function stopListening() {
  window.clearTimeout(hideStop)
  app.classList.remove('show-stop', 'playing')
  if (stream) { stream.stop(); stream = undefined }
  artButton.setAttribute('aria-label', 'Listen')
  setProgress(undefined, undefined)
}

// Tap to listen; while playing, a tap reveals a stop button, and a tap on that
// leaves the stream.
artButton.addEventListener('click', () => {
  if (!stream) startListening()
  else if (app.classList.contains('show-stop')) stopListening()
  else revealStop()
})

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && stream) stream.resume()
})

// ----------------------------------------------------------------- volume

function applyVolumeUi() {
  slider.value = String(volume.percent)
  slider.style.setProperty('--pct', volume.percent + '%')
  app.classList.toggle('muted', volume.muted)
  muteButton.setAttribute('aria-pressed', String(volume.muted))
  muteButton.setAttribute('aria-label', volume.muted ? 'Unmute' : 'Mute')
}

function pushVolume() {
  // Immediate for the ear; the server copy is what Snapcast remembers for
  // this device and echoes back as a settings message.
  if (stream?.gainNode) stream.gainNode.gain.value = volume.muted ? 0 : volume.percent / 100
  try { control.setVolume(clientId, volume.percent, volume.muted) } catch { /* not registered yet */ }
  applyVolumeUi()
}

slider.addEventListener('input', () => {
  sliderBusy = true
  volume.percent = Number(slider.value)
  if (volume.percent > 0 && volume.muted) volume.muted = false
  pushVolume()
})
slider.addEventListener('change', () => { sliderBusy = false })
slider.addEventListener('pointerup', () => { sliderBusy = false })

muteButton.addEventListener('click', () => {
  volume.muted = !volume.muted
  pushVolume()
})

applyVolumeUi()
