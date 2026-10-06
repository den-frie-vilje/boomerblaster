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
const artwork = $<HTMLImageElement>('artwork')
const titleEl = $('title')
const artistEl = $('artist')
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
    setArt(meta.artUrl ? new URL(meta.artUrl, httpBase + '/').toString() : '')
    setMediaSession(title, meta.artist?.join(', ') ?? '', meta.album ?? '', lastArt)
  } else if (playing) {
    titleEl.textContent = 'Now playing'
    artistEl.textContent = s?.id ?? ''
    document.title = 'BoomerBlaster'
    setArt('')
  } else {
    titleEl.textContent = stream ? 'Nothing playing' : 'BoomerBlaster'
    artistEl.textContent = stream ? 'Ask the DJ.' : 'Tap the artwork to listen'
    document.title = 'BoomerBlaster'
    setArt('')
  }

  const n = listenerCount(server)
  const who = n === 1 ? '1 listening' : `${n} listening`
  setStatus(stream ? `In sync · ${who}` : who)
}

function setArt(url: string) {
  if (url === lastArt) return
  lastArt = url
  if (url) {
    artwork.src = url
    artwork.onload = () => {
      artwork.classList.add('has')
      app.classList.add('has-art')
      backdrop.style.backgroundImage = `url("${url}")`
      backdrop.classList.add('on')
    }
    artwork.onerror = () => clearArt()
  } else {
    clearArt()
  }
}

function clearArt() {
  artwork.classList.remove('has')
  artwork.removeAttribute('src')
  app.classList.remove('has-art')
  backdrop.classList.remove('on')
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

artButton.addEventListener('click', startListening)

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
