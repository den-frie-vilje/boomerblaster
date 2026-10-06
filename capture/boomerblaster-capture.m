// boomerblaster-capture: broadcast-grade capture of a CoreAudio input device.
//
// Reads a CoreAudio input device (normally the BlackHole loopback that the DJ
// app plays into) and writes raw PCM (s16le, stereo, --rate) to stdout at
// exactly the output rate against the system clock, so snapserver's process
// source never sees late or missing data. The device's clock is measured from
// the HAL's timestamps and the audio is resampled to match, so a device that
// runs fast or slow against the system clock stays locked instead of drifting
// into a resync; lost input (device overload, a stall) becomes silence of the
// same length rather than a time shift. Nothing is ever dropped under
// backpressure: the audio thread only fills a lock-free ring, and the writer
// thread paces itself.
//
// Build: make (see Makefile). Test: python3 test_capture.py.
//
// --sim replaces the device with a synthetic one whose clock runs a chosen
// number of ppm off real time; the rest of the pipeline is unchanged, which is
// what the end-to-end tests drive.

#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#include <AudioToolbox/AudioToolbox.h>
#include <CoreAudio/CoreAudio.h>
#include <mach/mach_time.h>
#include <mach/thread_policy.h>
#include <mach/thread_act.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>
#include <sys/ioctl.h>

// ------------------------------------------------------------------ clock

static mach_timebase_info_data_t g_timebase;

static uint64_t now_ns(void) {
    return mach_absolute_time() * g_timebase.numer / g_timebase.denom;
}

static uint64_t host_to_ns(uint64_t host) {
    return host * g_timebase.numer / g_timebase.denom;
}

static void sleep_until_ns(uint64_t t) {
    uint64_t ticks = t * g_timebase.denom / g_timebase.numer;
    mach_wait_until(ticks);
}

static volatile sig_atomic_t g_running = 1;
static void on_signal(int sig) { (void)sig; g_running = 0; }

static void logmsg(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void logmsg(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("boomerblaster-capture: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    fflush(stderr);
    va_end(ap);
}

// ------------------------------------------------------------------- ring
// Single producer (audio thread), single consumer (writer). Interleaved
// stereo float frames. Indices grow monotonically; capacity is a power of two.

#define RING_FRAMES (1u << 19)   // ~11 s at 48 kHz

typedef struct {
    float *buf;
    _Atomic uint64_t head;   // frames written
    _Atomic uint64_t tail;   // frames read
    _Atomic uint64_t dropped;
} ring_t;

static void ring_init(ring_t *r) {
    r->buf = calloc(RING_FRAMES * 2, sizeof(float));
    atomic_store(&r->head, 0);
    atomic_store(&r->tail, 0);
    atomic_store(&r->dropped, 0);
}

static uint64_t ring_count(const ring_t *r) {
    return atomic_load(&r->head) - atomic_load(&r->tail);
}

// Producer side. Drops (and counts) what does not fit; the ring is sized so
// that only a stuck consumer gets there.
static void ring_push(ring_t *r, const float *frames, uint32_t n) {
    uint64_t head = atomic_load_explicit(&r->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&r->tail, memory_order_acquire);
    uint64_t room = RING_FRAMES - (head - tail);
    if (n > room) {
        atomic_fetch_add(&r->dropped, n - room);
        n = (uint32_t)room;
    }
    for (uint32_t i = 0; i < n; i++) {
        uint32_t slot = (uint32_t)((head + i) & (RING_FRAMES - 1));
        if (frames) {
            r->buf[slot * 2] = frames[i * 2];
            r->buf[slot * 2 + 1] = frames[i * 2 + 1];
        } else {
            r->buf[slot * 2] = 0;
            r->buf[slot * 2 + 1] = 0;
        }
    }
    atomic_store_explicit(&r->head, head + n, memory_order_release);
}

// Consumer side; returns frames copied.
static uint32_t ring_pop(ring_t *r, float *dst, uint32_t max) {
    uint64_t head = atomic_load_explicit(&r->head, memory_order_acquire);
    uint64_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
    uint64_t avail = head - tail;
    if (max > avail) max = (uint32_t)avail;
    for (uint32_t i = 0; i < max; i++) {
        uint32_t slot = (uint32_t)((tail + i) & (RING_FRAMES - 1));
        dst[i * 2] = r->buf[slot * 2];
        dst[i * 2 + 1] = r->buf[slot * 2 + 1];
    }
    atomic_store_explicit(&r->tail, tail + max, memory_order_release);
    return max;
}

static void ring_clear(ring_t *r) {
    atomic_store(&r->tail, atomic_load(&r->head));
}

// --------------------------------------------------------------- clockinfo
// The latest (host time, device sample time) pair the producer saw, plus
// counters, published through a seqlock so the writer can read a consistent
// snapshot without the audio thread ever blocking.

typedef struct {
    _Atomic uint64_t gen;
    uint64_t host_ns;        // host time of the first frame of the latest buffer
    double sample_time;      // device sample time of that frame
    uint64_t pushed;         // frames pushed into the ring in total
    uint64_t lost;           // frames the device skipped (zeros were inserted)
    uint64_t discontinuities;// host-time gaps > 250 ms between buffers
    uint64_t buffers;
    int ts_valid;
} clockinfo_t;

typedef struct {
    uint64_t host_ns;
    double sample_time;
    uint64_t pushed, lost, discontinuities, buffers;
    int ts_valid;
} clocksnap_t;

static void clockinfo_publish(clockinfo_t *c, const clocksnap_t *s) {
    uint64_t g = atomic_load_explicit(&c->gen, memory_order_relaxed);
    atomic_store_explicit(&c->gen, g + 1, memory_order_release);
    c->host_ns = s->host_ns;
    c->sample_time = s->sample_time;
    c->pushed = s->pushed;
    c->lost = s->lost;
    c->discontinuities = s->discontinuities;
    c->buffers = s->buffers;
    c->ts_valid = s->ts_valid;
    atomic_store_explicit(&c->gen, g + 2, memory_order_release);
}

static void clockinfo_read(clockinfo_t *c, clocksnap_t *s) {
    for (;;) {
        uint64_t g1 = atomic_load_explicit(&c->gen, memory_order_acquire);
        if (g1 & 1) continue;
        s->host_ns = c->host_ns;
        s->sample_time = c->sample_time;
        s->pushed = c->pushed;
        s->lost = c->lost;
        s->discontinuities = c->discontinuities;
        s->buffers = c->buffers;
        s->ts_valid = c->ts_valid;
        atomic_thread_fence(memory_order_acquire);
        uint64_t g2 = atomic_load_explicit(&c->gen, memory_order_relaxed);
        if (g1 == g2) return;
    }
}

// ------------------------------------------------------------------ input
// Common delivery path for the real device and the simulator.

typedef struct {
    ring_t ring;
    clockinfo_t clock;
    // producer-private state
    int have_prev;
    double prev_sample;
    uint32_t prev_frames;
    uint64_t prev_host;
    clocksnap_t snap;
    double in_rate;   // the device's nominal rate
} input_t;

static void input_reset(input_t *in) {
    in->have_prev = 0;
    memset(&in->snap, 0, sizeof in->snap);
    ring_clear(&in->ring);
    clockinfo_publish(&in->clock, &in->snap);
}

// Called on the audio thread with one buffer of interleaved stereo frames.
static void input_deliver(input_t *in, const float *frames, uint32_t n,
                          uint64_t host_ns, double sample_time, int ts_valid) {
    if (in->have_prev && ts_valid) {
        double expected = in->prev_sample + in->prev_frames;
        double gap = sample_time - expected;
        // The device skipped frames (overload, a stall): keep time by
        // inserting the same amount of silence. Ignore nonsense jumps.
        if (gap > 0.5 && gap < 5.0 * in->in_rate) {
            uint32_t z = (uint32_t)llround(gap);
            ring_push(&in->ring, NULL, z);
            in->snap.pushed += z;
            in->snap.lost += z;
        }
        if (host_ns > in->prev_host && host_ns - in->prev_host > 250000000ull)
            in->snap.discontinuities++;
    }
    ring_push(&in->ring, frames, n);
    in->snap.pushed += n;
    in->snap.buffers++;
    in->snap.host_ns = host_ns;
    in->snap.sample_time = sample_time;
    in->snap.ts_valid = ts_valid;
    in->have_prev = 1;
    in->prev_sample = sample_time;
    in->prev_frames = n;
    in->prev_host = host_ns;
    clockinfo_publish(&in->clock, &in->snap);
}

// -------------------------------------------------------------- resampler
// Arbitrary-ratio windowed-sinc (Kaiser, beta 10, 128 taps at the lower rate),
// polyphase table with linear interpolation between phases. The step (input
// frames per output frame) may change per call, which is how drift is taken
// out. Quality is well beyond 16-bit; cost is a few percent of one core.

typedef struct {
    int W;          // half-width in input frames; taps = 2W
    int P;          // phases in the table
    float *tab;     // (P+1) rows of 2W coefficients, each row normalised to DC gain 1
    float *in;      // interleaved stereo input fifo
    int cap, count;
    double pos;     // read position, in frames, into `in`
} resampler_t;

static double bessel_i0(double x) {
    double sum = 1, term = 1;
    for (int k = 1; k < 200; k++) {
        double t = x / (2.0 * k);
        term *= t * t;
        sum += term;
        if (term < 1e-14 * sum) break;
    }
    return sum;
}

static void resampler_free(resampler_t *rs) {
    free(rs->tab);
    free(rs->in);
    memset(rs, 0, sizeof *rs);
}

static void resampler_init(resampler_t *rs, double in_rate, double out_rate) {
    resampler_free(rs);
    double rho = in_rate / out_rate;              // input frames per output frame
    double stretch = fmax(1.0, rho);              // sinc is stretched when downsampling
    const int HW = 64;                            // half-width in lower-rate samples
    rs->W = (int)ceil(HW * stretch);
    rs->P = 512;
    int taps = 2 * rs->W;
    rs->tab = malloc(sizeof(float) * (size_t)(rs->P + 1) * taps);
    double fc = 0.5 * 0.92 / stretch;             // cutoff, cycles per input frame
    double beta = 10.0, i0b = bessel_i0(beta);
    for (int p = 0; p <= rs->P; p++) {
        double frac = (double)p / rs->P;
        float *row = rs->tab + (size_t)p * taps;
        double sum = 0;
        for (int j = 0; j < taps; j++) {
            double t = (j - rs->W + 1) - frac;    // distance from the output instant
            double w = t / rs->W;
            double h = 0;
            if (fabs(w) < 1.0) {
                double kaiser = bessel_i0(beta * sqrt(1.0 - w * w)) / i0b;
                double x = 2.0 * fc * t;
                double sinc = (fabs(x) < 1e-12) ? 1.0 : sin(M_PI * x) / (M_PI * x);
                h = 2.0 * fc * sinc * kaiser;
            }
            row[j] = (float)h;
            sum += h;
        }
        for (int j = 0; j < taps; j++) row[j] = (float)(row[j] / sum);
    }
    rs->cap = 16384 + taps;
    rs->in = calloc((size_t)rs->cap * 2, sizeof(float));
    // Start with W zero frames of history so the first output instant has a
    // full window of context.
    rs->count = rs->W;
    rs->pos = rs->W;
}

// Forget the input history (after a gap, so stale audio does not follow it).
static void resampler_clear(resampler_t *rs) {
    memset(rs->in, 0, sizeof(float) * 2 * (size_t)rs->W);
    rs->count = rs->W;
    rs->pos = rs->W;
}

static double resampler_buffered(const resampler_t *rs) {
    return rs->count - rs->pos;   // input frames ahead of the read position
}

// Produces up to n output frames at the given step; returns how many it made
// (fewer only when the ring ran dry).
static uint32_t resampler_pull(resampler_t *rs, ring_t *ring, float *out, uint32_t n, double step) {
    int taps = 2 * rs->W;
    uint32_t produced = 0;
    while (produced < n) {
        int idx = (int)rs->pos;
        if (rs->count < idx + rs->W + 1) {
            int keep_from = idx - rs->W + 1;
            if (keep_from > 0) {
                memmove(rs->in, rs->in + (size_t)keep_from * 2,
                        sizeof(float) * 2 * (size_t)(rs->count - keep_from));
                rs->count -= keep_from;
                rs->pos -= keep_from;
                idx -= keep_from;
            }
            uint32_t got = ring_pop(ring, rs->in + (size_t)rs->count * 2, (uint32_t)(rs->cap - rs->count));
            rs->count += (int)got;
            if (rs->count < idx + rs->W + 1) break;
        }
        double frac = rs->pos - idx;
        double fp = frac * rs->P;
        int p = (int)fp;
        if (p >= rs->P) p = rs->P - 1;
        float a = (float)(fp - p);
        const float *r0 = rs->tab + (size_t)p * taps;
        const float *r1 = r0 + taps;
        const float *x = rs->in + (size_t)(idx - rs->W + 1) * 2;
        float L = 0, R = 0;
        for (int j = 0; j < taps; j++) {
            float c = r0[j] + a * (r1[j] - r0[j]);
            L += x[j * 2] * c;
            R += x[j * 2 + 1] * c;
        }
        out[produced * 2] = L;
        out[produced * 2 + 1] = R;
        produced++;
        rs->pos += step;
    }
    return produced;
}

// ------------------------------------------------------------ simulator
// A device whose clock runs `ppm` off real time and whose timestamps claim
// `ts_ppm` (equal to ppm for an honest device), delivering `buffer` frames of
// a test tone per callback with up to `jitter_ms` of delivery jitter, and an
// optional stall during which the device clock runs but nothing is delivered.

typedef struct {
    double rate, ppm, ts_ppm, jitter_ms, tone_hz, stall_at, stall_dur;
    int buffer;
    input_t *in;
    pthread_t thread;
} sim_t;

static void *sim_thread(void *arg) {
    sim_t *s = arg;
    float *buf = malloc(sizeof(float) * 2 * (size_t)s->buffer);
    uint64_t t0 = now_ns();
    double phase = 0, sample_time = 0;
    uint64_t n = 0;
    unsigned seed = 12345;
    double actual_rate = s->rate * (1.0 + s->ppm * 1e-6);
    double claimed_rate = s->rate * (1.0 + s->ts_ppm * 1e-6);
    while (g_running) {
        double due_s = n * (double)s->buffer / actual_rate;
        uint64_t due = t0 + (uint64_t)(due_s * 1e9);
        uint64_t claimed = t0 + (uint64_t)(n * (double)s->buffer / claimed_rate * 1e9);
        double jitter = s->jitter_ms * 1e-3 * (rand_r(&seed) / (double)RAND_MAX);
        sleep_until_ns(due + (uint64_t)(jitter * 1e9));
        if (!g_running) break;
        int stalled = s->stall_dur > 0 && due_s >= s->stall_at && due_s < s->stall_at + s->stall_dur;
        for (int i = 0; i < s->buffer; i++) {
            float v = (float)(0.5 * sin(phase));
            phase += 2.0 * M_PI * s->tone_hz / s->rate;
            if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
            buf[i * 2] = v;
            buf[i * 2 + 1] = v;
        }
        if (!stalled)
            input_deliver(s->in, buf, (uint32_t)s->buffer, claimed, sample_time, 1);
        sample_time += s->buffer;
        n++;
    }
    free(buf);
    return NULL;
}

// --------------------------------------------------------------- devices

static CFStringRef device_name_cf(AudioObjectID dev) {
    CFStringRef name = NULL;
    UInt32 size = sizeof name;
    AudioObjectPropertyAddress addr = { kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    if (AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, &name) != noErr) return NULL;
    return name;
}

static int device_name(AudioObjectID dev, char *out, size_t cap) {
    CFStringRef name = device_name_cf(dev);
    if (!name) return 0;
    Boolean ok = CFStringGetCString(name, out, (CFIndex)cap, kCFStringEncodingUTF8);
    CFRelease(name);
    return ok ? 1 : 0;
}

static UInt32 device_channels(AudioObjectID dev, AudioObjectPropertyScope scope) {
    AudioObjectPropertyAddress addr = { kAudioDevicePropertyStreamConfiguration, scope, kAudioObjectPropertyElementMain };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(dev, &addr, 0, NULL, &size) != noErr || size == 0) return 0;
    AudioBufferList *abl = malloc(size);
    UInt32 channels = 0;
    if (AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, abl) == noErr)
        for (UInt32 i = 0; i < abl->mNumberBuffers; i++) channels += abl->mBuffers[i].mNumberChannels;
    free(abl);
    return channels;
}

static double device_rate(AudioObjectID dev) {
    Float64 rate = 0;
    UInt32 size = sizeof rate;
    AudioObjectPropertyAddress addr = { kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    AudioObjectGetPropertyData(dev, &addr, 0, NULL, &size, &rate);
    return rate;
}

static int all_devices(AudioObjectID **out) {
    AudioObjectPropertyAddress addr = { kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &addr, 0, NULL, &size) != noErr) return 0;
    *out = malloc(size);
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &addr, 0, NULL, &size, *out) != noErr) { free(*out); *out = NULL; return 0; }
    return (int)(size / sizeof(AudioObjectID));
}

static int ci_contains(const char *hay, const char *needle) {
    size_t n = strlen(needle);
    for (const char *p = hay; *p; p++)
        if (strncasecmp(p, needle, n) == 0) return 1;
    return 0;
}

// Exact name first, then a case-insensitive substring; only devices with
// channels in the wanted direction count.
static AudioObjectID find_device(const char *wanted, AudioObjectPropertyScope scope) {
    AudioObjectID *devs = NULL;
    int n = all_devices(&devs);
    AudioObjectID exact = 0, partial = 0;
    for (int i = 0; i < n; i++) {
        char name[256];
        if (!device_name(devs[i], name, sizeof name)) continue;
        if (device_channels(devs[i], scope) == 0) continue;
        if (strcmp(name, wanted) == 0 && !exact) exact = devs[i];
        else if (ci_contains(name, wanted) && !partial) partial = devs[i];
    }
    free(devs);
    return exact ? exact : partial;
}

static void list_devices(void) {
    AudioObjectID *devs = NULL;
    int n = all_devices(&devs);
    for (int i = 0; i < n; i++) {
        char name[256];
        if (!device_name(devs[i], name, sizeof name)) continue;
        UInt32 in = device_channels(devs[i], kAudioObjectPropertyScopeInput);
        UInt32 out = device_channels(devs[i], kAudioObjectPropertyScopeOutput);
        printf("%-40s in %u  out %u  %.0f Hz\n", name, in, out, device_rate(devs[i]));
    }
    free(devs);
}

static const char *authorization_name(void) {
    switch ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio]) {
        case AVAuthorizationStatusAuthorized: return "authorized";
        case AVAuthorizationStatusDenied: return "denied";
        case AVAuthorizationStatusRestricted: return "restricted";
        default: return "notDetermined";
    }
}

// ---------------------------------------------------------- real device

typedef struct {
    char name[256];
    AudioObjectID dev;
    AudioUnit unit;
    AudioBufferList *abl;
    float *interleave;
    UInt32 channels;
    UInt32 max_frames;
    input_t *in;
    _Atomic int reconfigure;    // the device changed rate or went away
    _Atomic int granted;        // set by the permission callback
    _Atomic uint64_t rate_bits; // the running device's nominal rate (double bits); 0 when no device
    _Atomic uint64_t generation;// bumped whenever the input chain restarted
    pthread_t thread;
    uint64_t render_errors;
} device_t;

static void device_stop(device_t *d);
static double device_start(device_t *d);

static OSStatus input_callback(void *refcon, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                               UInt32 bus, UInt32 nframes, AudioBufferList *ioData) {
    (void)ioData;
    device_t *d = refcon;
    if (nframes > d->max_frames) nframes = d->max_frames;
    for (UInt32 c = 0; c < d->abl->mNumberBuffers; c++)
        d->abl->mBuffers[c].mDataByteSize = nframes * sizeof(float);
    OSStatus st = AudioUnitRender(d->unit, flags, ts, bus, nframes, d->abl);
    if (st != noErr) { d->render_errors++; return st; }
    const float *L = d->abl->mBuffers[0].mData;
    const float *R = d->channels > 1 ? d->abl->mBuffers[1].mData : L;
    for (UInt32 i = 0; i < nframes; i++) {
        d->interleave[i * 2] = L[i];
        d->interleave[i * 2 + 1] = R[i];
    }
    int valid = (ts->mFlags & kAudioTimeStampSampleTimeValid) && (ts->mFlags & kAudioTimeStampHostTimeValid);
    input_deliver(d->in, d->interleave, nframes, host_to_ns(ts->mHostTime), ts->mSampleTime, valid);
    return noErr;
}

static OSStatus device_listener(AudioObjectID obj, UInt32 n, const AudioObjectPropertyAddress *addrs, void *refcon) {
    (void)obj; (void)n; (void)addrs;
    device_t *d = refcon;
    atomic_store(&d->reconfigure, 1);
    return noErr;
}

// The device list changed: only interesting while ours is missing.
static OSStatus devices_listener(AudioObjectID obj, UInt32 n, const AudioObjectPropertyAddress *addrs, void *refcon) {
    (void)obj; (void)n; (void)addrs;
    device_t *d = refcon;
    if (!d->unit) atomic_store(&d->reconfigure, 1);
    return noErr;
}

// All CoreAudio calls happen here, off the writer thread: coreaudiod can
// block a caller for as long as it likes (it does while a permission prompt
// is open), and the output must keep flowing meanwhile.
static void *device_thread(void *arg) {
    device_t *d = arg;
    uint64_t last_try = 0;
    int warned_missing = 0;
    while (g_running) {
        if (atomic_load(&d->reconfigure)) {
            uint64_t now = now_ns();
            if (now - last_try >= 1000000000ull) {
                last_try = now;
                atomic_store(&d->reconfigure, 0);
                int had = d->unit != NULL;
                double old_rate;
                { uint64_t b = atomic_load(&d->rate_bits); memcpy(&old_rate, &b, sizeof b); }
                device_stop(d);
                input_reset(d->in);
                double rate = device_start(d);
                d->in->in_rate = rate;
                uint64_t bits;
                memcpy(&bits, &rate, sizeof bits);
                atomic_store(&d->rate_bits, bits);
                atomic_fetch_add(&d->generation, 1);
                if (rate > 0) {
                    if (!had || rate != old_rate)
                        logmsg("capturing \"%s\" at %.0f Hz, %u channel(s)", d->name, rate, d->channels);
                    warned_missing = 0;
                } else {
                    if (!warned_missing)
                        logmsg(had ? "device \"%s\" went away; sending silence until it returns"
                                   : "no input device named \"%s\"; sending silence until it appears", d->name);
                    warned_missing = 1;
                    atomic_store(&d->reconfigure, 1);   // retry, once a second
                }
            }
        }
        usleep(20000);
    }
    device_stop(d);
    return NULL;
}

static void device_stop(device_t *d) {
    if (d->unit) {
        AudioOutputUnitStop(d->unit);
        AudioUnitUninitialize(d->unit);
        AudioComponentInstanceDispose(d->unit);
        d->unit = NULL;
    }
    if (d->dev) {
        AudioObjectPropertyAddress a1 = { kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        AudioObjectPropertyAddress a2 = { kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        AudioObjectRemovePropertyListener(d->dev, &a1, device_listener, d);
        AudioObjectRemovePropertyListener(d->dev, &a2, device_listener, d);
        d->dev = 0;
    }
    if (d->abl) {
        for (UInt32 c = 0; c < d->abl->mNumberBuffers; c++) free(d->abl->mBuffers[c].mData);
        free(d->abl);
        d->abl = NULL;
    }
    free(d->interleave);
    d->interleave = NULL;
}

// Opens the device and starts input. Returns the device's nominal rate, or 0
// (with a message) when the device is missing or refuses.
static double device_start(device_t *d) {
    device_stop(d);
    atomic_store(&d->reconfigure, 0);
    d->dev = find_device(d->name, kAudioObjectPropertyScopeInput);
    if (!d->dev) return 0;
    double rate = device_rate(d->dev);
    UInt32 dev_channels = device_channels(d->dev, kAudioObjectPropertyScopeInput);
    d->channels = dev_channels >= 2 ? 2 : 1;

    AudioComponentDescription desc = { kAudioUnitType_Output, kAudioUnitSubType_HALOutput, kAudioUnitManufacturer_Apple, 0, 0 };
    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    OSStatus st = AudioComponentInstanceNew(comp, &d->unit);
    if (st != noErr) { logmsg("cannot create the audio unit (%d)", (int)st); return 0; }
    UInt32 one = 1, zero = 0;
    AudioUnitSetProperty(d->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &one, sizeof one);
    AudioUnitSetProperty(d->unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &zero, sizeof zero);
    st = AudioUnitSetProperty(d->unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &d->dev, sizeof d->dev);
    if (st != noErr) { logmsg("cannot select device \"%s\" (%d)", d->name, (int)st); device_stop(d); return 0; }

    AudioStreamBasicDescription fmt = {0};
    fmt.mSampleRate = rate;
    fmt.mFormatID = kAudioFormatLinearPCM;
    fmt.mFormatFlags = kAudioFormatFlagsNativeFloatPacked | kAudioFormatFlagIsNonInterleaved;
    fmt.mChannelsPerFrame = d->channels;
    fmt.mBitsPerChannel = 32;
    fmt.mBytesPerFrame = fmt.mBytesPerPacket = 4;
    fmt.mFramesPerPacket = 1;
    st = AudioUnitSetProperty(d->unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &fmt, sizeof fmt);
    if (st != noErr) { logmsg("device \"%s\" refuses float input at %.0f Hz (%d)", d->name, rate, (int)st); device_stop(d); return 0; }

    UInt32 frames = 512;
    AudioUnitSetProperty(d->unit, kAudioDevicePropertyBufferFrameSize, kAudioUnitScope_Global, 0, &frames, sizeof frames);
    d->max_frames = 8192;
    AudioUnitSetProperty(d->unit, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global, 0, &d->max_frames, sizeof d->max_frames);

    d->abl = calloc(1, sizeof(AudioBufferList) + sizeof(AudioBuffer) * 2);
    d->abl->mNumberBuffers = d->channels;
    for (UInt32 c = 0; c < d->channels; c++) {
        d->abl->mBuffers[c].mNumberChannels = 1;
        d->abl->mBuffers[c].mDataByteSize = d->max_frames * sizeof(float);
        d->abl->mBuffers[c].mData = calloc(d->max_frames, sizeof(float));
    }
    d->interleave = calloc((size_t)d->max_frames * 2, sizeof(float));

    AURenderCallbackStruct cb = { input_callback, d };
    AudioUnitSetProperty(d->unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 0, &cb, sizeof cb);
    st = AudioUnitInitialize(d->unit);
    if (st != noErr) { logmsg("cannot initialise input from \"%s\" (%d)", d->name, (int)st); device_stop(d); return 0; }
    st = AudioOutputUnitStart(d->unit);
    if (st != noErr) { logmsg("cannot start input from \"%s\" (%d)", d->name, (int)st); device_stop(d); return 0; }

    AudioObjectPropertyAddress a1 = { kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    AudioObjectPropertyAddress a2 = { kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    AudioObjectAddPropertyListener(d->dev, &a1, device_listener, d);
    AudioObjectAddPropertyListener(d->dev, &a2, device_listener, d);
    return rate;
}

// Asks macOS for microphone access (every capture of an input device counts
// as that) so the prompt appears right away, attributed to whatever launched
// us, and so a denial is reported in words instead of as silence.
static void request_permission(device_t *d) {
    const char *status = authorization_name();
    if (strcmp(status, "authorized") == 0) { atomic_store(&d->granted, 1); return; }
    if (strcmp(status, "notDetermined") == 0) {
        logmsg("asking macOS for microphone access (that is how it classes capturing an audio device)");
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
            logmsg(granted ? "microphone access granted" : "microphone access denied; the stream will stay silent");
            if (granted) { atomic_store(&d->granted, 1); atomic_store(&d->reconfigure, 1); }
        }];
        return;
    }
    logmsg("microphone access is %s for the process that launched this one; allow it in "
           "System Settings > Privacy & Security > Microphone, or the stream stays silent", status);
}

// ------------------------------------------------------------- test tone
// Plays a sine into a device's output; the device test uses it to drive
// BlackHole so the capture side can be checked against a known signal.

typedef struct { double phase, step; } tone_t;

static OSStatus tone_render(void *refcon, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts,
                            UInt32 bus, UInt32 nframes, AudioBufferList *ioData) {
    (void)flags; (void)ts; (void)bus;
    tone_t *t = refcon;
    for (UInt32 i = 0; i < nframes; i++) {
        float v = (float)(0.5 * sin(t->phase));
        t->phase += t->step;
        if (t->phase > 2 * M_PI) t->phase -= 2 * M_PI;
        for (UInt32 c = 0; c < ioData->mNumberBuffers; c++) ((float *)ioData->mBuffers[c].mData)[i] = v;
    }
    return noErr;
}

static int play_tone(const char *devname, double hz) {
    AudioObjectID dev = find_device(devname, kAudioObjectPropertyScopeOutput);
    if (!dev) { logmsg("no output device named \"%s\"", devname); return 2; }
    double rate = device_rate(dev);
    AudioComponentDescription desc = { kAudioUnitType_Output, kAudioUnitSubType_HALOutput, kAudioUnitManufacturer_Apple, 0, 0 };
    AudioUnit unit;
    AudioComponentInstanceNew(AudioComponentFindNext(NULL, &desc), &unit);
    // Output only: with the device's input streams left enabled, the unit
    // would need the microphone permission just to play.
    UInt32 one = 1, zero = 0;
    AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &one, sizeof one);
    AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &zero, sizeof zero);
    AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &dev, sizeof dev);
    AudioStreamBasicDescription fmt = {0};
    fmt.mSampleRate = rate;
    fmt.mFormatID = kAudioFormatLinearPCM;
    fmt.mFormatFlags = kAudioFormatFlagsNativeFloatPacked | kAudioFormatFlagIsNonInterleaved;
    fmt.mChannelsPerFrame = 2;
    fmt.mBitsPerChannel = 32;
    fmt.mBytesPerFrame = fmt.mBytesPerPacket = 4;
    fmt.mFramesPerPacket = 1;
    OSStatus st = AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &fmt, sizeof fmt);
    if (st != noErr) { logmsg("cannot set the output format (%d)", (int)st); return 1; }
    tone_t tone = { 0, 2 * M_PI * hz / rate };
    AURenderCallbackStruct cb = { tone_render, &tone };
    AudioUnitSetProperty(unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &cb, sizeof cb);
    if (AudioUnitInitialize(unit) != noErr || AudioOutputUnitStart(unit) != noErr) { logmsg("cannot start output"); return 1; }
    logmsg("playing %.1f Hz into \"%s\" at %.0f Hz until stopped", hz, devname, rate);
    while (g_running) usleep(50000);
    AudioOutputUnitStop(unit);
    AudioComponentInstanceDispose(unit);
    return 0;
}

// ----------------------------------------------------------------- writer

typedef struct { uint64_t host_ns; double sample_time; } histpoint_t;

typedef struct {
    double out_rate;
    double in_rate;            // nominal device rate (0 until known)
    uint64_t lead_frames;      // output frames the writer runs ahead of real time
    uint64_t setpoint_frames;  // input buffering target, in output frames
    int status;                // print a status line every status_ns
    uint64_t status_ns;
    // state
    resampler_t rs;
    uint64_t written;          // output frames handed to the kernel
    uint64_t t0_ns;            // real-time origin: target(now) = lead + (now - t0) * out_rate
    int starved;               // emitting silence until the input has refilled
    int started;               // has ever left the starved state
    double fill_avg;
    double ref_frames;         // buffering level the controller holds; <0 while learning it
    uint64_t ref_learn_from_ns, ref_learn_until_ns;
    double ref_sum, ref_sumsq;
    double deadband;           // level noise measured while learning; no trim inside it
    int ref_n;
    double rate_in_est;        // measured device rate against the system clock
    int rate_measured;         // rate_in_est comes from timestamps, not the nominal
    double correction;         // fractional trim from the fill controller
    histpoint_t hist[128];     // one (host, sample) point per second for the rate window
    int hist_count, hist_head;
    uint64_t hist_last_ns;
    uint64_t underrun_frames, silence_frames, skipped_frames;
    uint64_t seen_discontinuities, seen_lost;
    uint64_t last_status_ns;
    int rate_warned;
} writer_t;

static int write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN) { usleep(1000); continue; }
            return -1;
        }
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

// The first write, while the pipe is still empty, is a large block of
// silence: XNU grows a pipe's buffer to fit a big write, so this both buys the
// room for the lead and measures how much room there is. Returns the frames
// written.
static uint64_t preroll(writer_t *w) {
    const size_t bytes = 65536;
    char *zeros = calloc(1, bytes);
    int flags = fcntl(STDOUT_FILENO, F_GETFL, 0);
    fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK);
    size_t done = 0;
    for (int attempt = 0; attempt < 50 && done < bytes; attempt++) {
        ssize_t r = write(STDOUT_FILENO, zeros + done, bytes - done);
        if (r > 0) done += (size_t)r;
        else if (r < 0 && errno == EAGAIN) break;
        else if (r < 0 && errno == EINTR) continue;
        else break;
    }
    fcntl(STDOUT_FILENO, F_SETFL, flags);
    free(zeros);
    uint64_t frames = done / 4;
    uint64_t chunk = (uint64_t)(w->out_rate * 0.02);
    if (frames < w->lead_frames + 2 * chunk) {
        // The pipe is smaller than the lead we wanted; run as far ahead as fits.
        uint64_t fits = frames > 3 * chunk ? frames - 2 * chunk : chunk;
        logmsg("output pipe holds only %llu ms; lead reduced from %.0f ms to %.0f ms",
               (unsigned long long)(frames * 1000 / (uint64_t)w->out_rate),
               w->lead_frames * 1000.0 / w->out_rate, fits * 1000.0 / w->out_rate);
        w->lead_frames = fits;
    }
    return frames;
}

static void writer_reset(writer_t *w) {
    if (w->in_rate > 0) resampler_init(&w->rs, w->in_rate, w->out_rate);
    w->starved = 1;
    w->started = 0;
    w->hist_count = w->hist_head = 0;
    w->hist_last_ns = 0;
    w->rate_in_est = w->in_rate;
    w->correction = 0;
    w->fill_avg = (double)w->setpoint_frames;
    w->ref_frames = -1;
}

// Device rate against the system clock from the HAL's own timestamps over a
// window of up to 60 s; falls back to nominal until 2 s of history exist.
static void update_rate_estimate(writer_t *w, const clocksnap_t *s, uint64_t now) {
    w->rate_measured = 0;
    if (!s->ts_valid || s->buffers == 0) { w->rate_in_est = w->in_rate; return; }
    if (w->hist_last_ns == 0 || now - w->hist_last_ns >= 1000000000ull) {
        histpoint_t pt = { s->host_ns, s->sample_time };
        w->hist[w->hist_head] = pt;
        w->hist_head = (w->hist_head + 1) % 128;
        if (w->hist_count < 128) w->hist_count++;
        w->hist_last_ns = now;
    }
    // Oldest point within 60 s of the latest, at least 2 s older.
    const histpoint_t *best = NULL;
    for (int i = 0; i < w->hist_count; i++) {
        const histpoint_t *pt = &w->hist[(w->hist_head - 1 - i + 256) % 128];
        if (s->host_ns <= pt->host_ns) continue;
        uint64_t age = s->host_ns - pt->host_ns;
        if (age > 60000000000ull) break;
        if (age >= 2000000000ull) best = pt;
    }
    if (!best) { w->rate_in_est = w->in_rate; return; }
    double rate = (s->sample_time - best->sample_time) / ((s->host_ns - best->host_ns) / 1e9);
    if (fabs(rate / w->in_rate - 1.0) > 0.02) {
        if (!w->rate_warned) {
            logmsg("device timestamps imply %.0f Hz against a nominal %.0f; ignoring them", rate, w->in_rate);
            w->rate_warned = 1;
        }
        w->rate_in_est = w->in_rate;
        return;
    }
    w->rate_in_est = rate;
    w->rate_measured = 1;
}

static void float_to_s16(const float *in, int16_t *out, size_t samples) {
    for (size_t i = 0; i < samples; i++) {
        float v = in[i] * 32767.0f;
        if (v > 32767.0f) v = 32767.0f;
        if (v < -32768.0f) v = -32768.0f;
        out[i] = (int16_t)lrintf(v);
    }
}

static void print_status(writer_t *w, const clocksnap_t *s, input_t *in, double fill_frames, double step, uint64_t now) {
    fprintf(stderr,
            "{\"t\":%.3f,\"fill_ms\":%.1f,\"fill_avg_ms\":%.1f,\"ref_ms\":%.1f,\"deadband_ms\":%.1f,\"rate_in\":%.3f,\"rate_nominal\":%.0f,"
            "\"step\":%.9f,\"corr_ppm\":%.1f,\"lead_ms\":%.1f,\"written\":%llu,\"underrun_frames\":%llu,"
            "\"silence_frames\":%llu,\"skipped_frames\":%llu,\"lost_frames\":%llu,\"dropped_frames\":%llu,\"discontinuities\":%llu,"
            "\"buffers\":%llu,\"starved\":%d}\n",
            (now - w->t0_ns) / 1e9, fill_frames * 1000.0 / w->out_rate, w->fill_avg * 1000.0 / w->out_rate,
            w->ref_frames * 1000.0 / w->out_rate, w->deadband * 1000.0 / w->out_rate, w->rate_in_est, w->in_rate, step, w->correction * 1e6, w->lead_frames * 1000.0 / w->out_rate,
            (unsigned long long)w->written, (unsigned long long)w->underrun_frames,
            (unsigned long long)w->silence_frames, (unsigned long long)w->skipped_frames, (unsigned long long)s->lost,
            (unsigned long long)atomic_load(&in->ring.dropped), (unsigned long long)s->discontinuities,
            (unsigned long long)s->buffers, w->starved);
    fflush(stderr);
}

// One pass of the writer: bring the output up to real time plus the lead.
// Returns -1 when stdout is gone.
static int writer_tick(writer_t *w, input_t *in, uint64_t now, float *fbuf, int16_t *sbuf, size_t bufcap) {
    clocksnap_t s;
    clockinfo_read(&in->clock, &s);
    if (s.discontinuities != w->seen_discontinuities) {
        // The device paused; its old timestamps no longer describe its clock.
        w->seen_discontinuities = s.discontinuities;
        w->hist_count = w->hist_head = 0;
        w->hist_last_ns = 0;
    }
    update_rate_estimate(w, &s, now);

    double step_nominal = (w->in_rate > 0 ? w->rate_in_est : w->out_rate) / w->out_rate;
    double fill_frames = (ring_count(&in->ring) + (w->in_rate > 0 ? resampler_buffered(&w->rs) : 0)) / step_nominal;
    const double tau_s = 10.0;
    if (!w->starved) {
        // Smooth the fill over about a second (the loop runs every 10 ms). For
        // the first two seconds after (re)starting just learn where the level
        // settles (the setpoint plus roughly half a device buffer); from then
        // on trim the rate so it stays there. A real drift is already taken
        // out by the timestamp estimate; this holds the rest, without ever
        // bending the pitch to chase a startup offset.
        if (w->ref_frames < 0) {
            // Learn only once the measured rate is in use (2 s of timestamps);
            // before that the step is nominal and the level still moves.
            int ready = w->rate_measured || !s.ts_valid;
            if (!ready) {
                w->ref_learn_from_ns = now + 500000000ull;
                w->ref_learn_until_ns = now + 2500000000ull;
            }
            if (ready && now >= w->ref_learn_from_ns) {
                w->ref_sum += fill_frames;
                w->ref_sumsq += fill_frames * fill_frames;
                w->ref_n++;
            }
            if (ready && now >= w->ref_learn_until_ns && w->ref_n > 0) {
                w->ref_frames = w->ref_sum / w->ref_n;
                double var = w->ref_sumsq / w->ref_n - w->ref_frames * w->ref_frames;
                // The level jitters by about a device buffer plus delivery
                // jitter; a reference learned from that has some error, and
                // chasing it would bend the pitch. Only act outside the noise.
                w->deadband = 0.001 * w->out_rate + 1.5 * sqrt(var > 0 ? var : 0);
                w->fill_avg = w->ref_frames;
            }
            w->correction = 0;
        } else {
            w->fill_avg += (fill_frames - w->fill_avg) * 0.01;
            double err = w->fill_avg - w->ref_frames;
            if (err > w->deadband) err -= w->deadband;
            else if (err < -w->deadband) err += w->deadband;
            else err = 0;
            double corr = err / (w->out_rate * tau_s);
            if (corr > 0.002) corr = 0.002;
            if (corr < -0.002) corr = -0.002;
            w->correction = corr;
        }
    }
    double step = step_nominal * (1.0 + w->correction);

    int64_t target = (int64_t)w->lead_frames + (int64_t)((now - w->t0_ns) * w->out_rate / 1e9);
    int64_t need = target - (int64_t)w->written;
    while (need > 0) {
        uint32_t n = need > (int64_t)bufcap ? (uint32_t)bufcap : (uint32_t)need;
        uint32_t got = 0;
        if (w->in_rate > 0) {
            if (w->starved && fill_frames >= (double)w->setpoint_frames + (double)need) {
                // Refilled. At the first start, and after a real gap in the
                // device's output (frames it skipped, now zeros in the ring),
                // the ring holds more than the setpoint: keep only that much,
                // so the latency is the same every time and a gap comes out as
                // exactly one stretch of silence. A mere late delivery keeps
                // everything and is trimmed back gently by the controller.
                if (!w->started || s.lost != w->seen_lost) {
                    uint64_t keep = (uint64_t)((w->setpoint_frames + (uint64_t)need) * step_nominal);
                    uint64_t have = ring_count(&in->ring);
                    if (have > keep) {
                        uint64_t drop = have - keep;
                        atomic_fetch_add(&in->ring.tail, drop);
                        w->skipped_frames += drop;
                    }
                    resampler_clear(&w->rs);
                }
                w->seen_lost = s.lost;
                w->starved = 0;
                w->started = 1;
                w->fill_avg = (double)w->setpoint_frames;
                w->ref_frames = -1;
                w->ref_sum = w->ref_sumsq = 0;
                w->ref_n = 0;
                w->ref_learn_from_ns = now + 500000000ull;
                w->ref_learn_until_ns = now + 2500000000ull;
                w->correction = 0;
            }
            if (!w->starved) {
                got = resampler_pull(&w->rs, &in->ring, fbuf, n, step);
                if (got < n) {
                    w->starved = 1;
                    w->underrun_frames += n - got;
                }
            }
        }
        if (got < n) {
            memset(fbuf + (size_t)got * 2, 0, sizeof(float) * 2 * (n - got));
            w->silence_frames += n - got;
        }
        float_to_s16(fbuf, sbuf, (size_t)n * 2);
        if (write_all(STDOUT_FILENO, sbuf, (size_t)n * 4) < 0) return -1;
        w->written += n;
        need -= n;
    }
    if (w->status && now - w->last_status_ns >= w->status_ns) {
        w->last_status_ns = now;
        print_status(w, &s, in, fill_frames, step, now);
    }
    return 0;
}

// ------------------------------------------------------------------- main

static void usage(void) {
    fputs("usage: boomerblaster-capture [--device NAME] [--rate HZ] [--lead-ms N] [--buffer-ms N] [--status]\n"
          "       boomerblaster-capture --sim RATE[,ppm=P,ts_ppm=T,jitter_ms=J,buffer=N,tone=HZ,stall=AT:DUR] ...\n"
          "       boomerblaster-capture --list | --check [--device NAME] | --tone-to NAME [--tone-hz HZ]\n"
          "Writes s16le stereo PCM at --rate (default 44100) to stdout, locked to the system clock.\n",
          stderr);
}

static int parse_sim(sim_t *s, const char *spec) {
    char *copy = strdup(spec), *save = NULL;
    char *tok = strtok_r(copy, ",", &save);
    if (!tok) { free(copy); return 0; }
    s->rate = atof(tok);
    s->buffer = 512;
    s->tone_hz = 1000;
    while ((tok = strtok_r(NULL, ",", &save))) {
        char *eq = strchr(tok, '=');
        if (!eq) { free(copy); return 0; }
        *eq = 0;
        const char *v = eq + 1;
        if (strcmp(tok, "ppm") == 0) { s->ppm = atof(v); s->ts_ppm = s->ppm; }
        else if (strcmp(tok, "ts_ppm") == 0) s->ts_ppm = atof(v);
        else if (strcmp(tok, "jitter_ms") == 0) s->jitter_ms = atof(v);
        else if (strcmp(tok, "buffer") == 0) s->buffer = atoi(v);
        else if (strcmp(tok, "tone") == 0) s->tone_hz = atof(v);
        else if (strcmp(tok, "stall") == 0) {
            if (sscanf(v, "%lf:%lf", &s->stall_at, &s->stall_dur) != 2) { free(copy); return 0; }
        } else { free(copy); return 0; }
    }
    free(copy);
    return s->rate > 0 && s->buffer > 0;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        mach_timebase_info(&g_timebase);
        signal(SIGPIPE, SIG_IGN);
        signal(SIGTERM, on_signal);
        signal(SIGINT, on_signal);

        const char *devname = "BlackHole 2ch";
        double out_rate = 44100, lead_ms = 300, buffer_ms = 60, tone_hz = 1000, status_ms = 1000;
        int status = 0, do_list = 0, do_check = 0;
        const char *tone_to = NULL, *sim_spec = NULL;
        for (int i = 1; i < argc; i++) {
            const char *a = argv[i];
            const char *v = i + 1 < argc ? argv[i + 1] : NULL;
            if (strcmp(a, "--device") == 0 && v) { devname = v; i++; }
            else if (strcmp(a, "--rate") == 0 && v) { out_rate = atof(v); i++; }
            else if (strcmp(a, "--lead-ms") == 0 && v) { lead_ms = atof(v); i++; }
            else if (strcmp(a, "--buffer-ms") == 0 && v) { buffer_ms = atof(v); i++; }
            else if (strcmp(a, "--tone-hz") == 0 && v) { tone_hz = atof(v); i++; }
            else if (strcmp(a, "--tone-to") == 0 && v) { tone_to = v; i++; }
            else if (strcmp(a, "--sim") == 0 && v) { sim_spec = v; i++; }
            else if (strcmp(a, "--status") == 0) status = 1;
            else if (strcmp(a, "--status-ms") == 0 && v) { status = 1; status_ms = atof(v); i++; }
            else if (strcmp(a, "--list") == 0) do_list = 1;
            else if (strcmp(a, "--check") == 0) do_check = 1;
            else if (strcmp(a, "--help") == 0 || strcmp(a, "-h") == 0) { usage(); return 0; }
            else { usage(); return 64; }
        }
        if (out_rate < 8000 || out_rate > 192000) { logmsg("--rate must be 8000..192000"); return 64; }
        if (do_list) { list_devices(); return 0; }
        if (do_check) {
            AudioObjectID dev = find_device(devname, kAudioObjectPropertyScopeInput);
            char name[256] = "";
            if (dev) device_name(dev, name, sizeof name);
            const char *auth = authorization_name();
            printf("{\"device\":\"%s\",\"found\":%s,\"name\":\"%s\",\"rate\":%.0f,\"channels\":%u,\"microphone\":\"%s\"}\n",
                   devname, dev ? "true" : "false", name, dev ? device_rate(dev) : 0.0,
                   dev ? device_channels(dev, kAudioObjectPropertyScopeInput) : 0u, auth);
            if (!dev) return 2;
            return strcmp(auth, "authorized") == 0 ? 0 : 3;
        }
        if (tone_to) return play_tone(tone_to, tone_hz);

        // The writer must wake every 10 ms whatever else the Mac is doing:
        // give it the time-constraint (real-time) scheduling audio threads use.
        {
            thread_time_constraint_policy_data_t policy;
            double ns_per_tick = (double)g_timebase.numer / g_timebase.denom;
            policy.period = (uint32_t)(10e6 / ns_per_tick);
            policy.computation = (uint32_t)(1e6 / ns_per_tick);
            policy.constraint = (uint32_t)(5e6 / ns_per_tick);
            policy.preemptible = 1;
            if (thread_policy_set(pthread_mach_thread_np(pthread_self()), THREAD_TIME_CONSTRAINT_POLICY,
                                  (thread_policy_t)&policy, THREAD_TIME_CONSTRAINT_POLICY_COUNT) != KERN_SUCCESS)
                pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
        }

        static input_t in;
        ring_init(&in.ring);
        writer_t w = {0};
        w.out_rate = out_rate;
        w.lead_frames = (uint64_t)(lead_ms * out_rate / 1000.0);
        w.setpoint_frames = (uint64_t)(buffer_ms * out_rate / 1000.0);
        w.status = status;
        w.status_ns = (uint64_t)(status_ms * 1e6);

        sim_t sim = {0};
        device_t dev = {0};
        if (sim_spec) {
            if (!parse_sim(&sim, sim_spec)) { logmsg("bad --sim spec"); return 64; }
            sim.in = &in;
            in.in_rate = w.in_rate = sim.rate;
            input_reset(&in);
            writer_reset(&w);
            pthread_create(&sim.thread, NULL, sim_thread, &sim);
            logmsg("simulated device at %.0f Hz, %+.0f ppm (timestamps %+.0f ppm), %d-frame buffers",
                   sim.rate, sim.ppm, sim.ts_ppm, sim.buffer);
        } else {
            snprintf(dev.name, sizeof dev.name, "%s", devname);
            dev.in = &in;
            input_reset(&in);
            logmsg("output %.0f Hz, lead %.0f ms, buffer %.0f ms; opening \"%s\"", out_rate, lead_ms, buffer_ms, dev.name);
            request_permission(&dev);
            // Notice new devices so a missing one is picked up when it arrives.
            AudioObjectPropertyAddress a = { kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
            AudioObjectAddPropertyListener(kAudioObjectSystemObject, &a, devices_listener, &dev);
            atomic_store(&dev.reconfigure, 1);
            pthread_create(&dev.thread, NULL, device_thread, &dev);
        }

        uint64_t pre = preroll(&w);
        w.written = pre;
        uint64_t now = now_ns();
        // The pipe now holds the preroll. Output resumes once real time has
        // caught up to all but the lead of it, so the pipe settles at the lead
        // and writes never block: target(t) = lead + (t - t0) * rate. When the
        // pipe took less than the lead, the origin moves ahead instead so the
        // writer tops it up straight away.
        if (pre >= w.lead_frames)
            w.t0_ns = now;
        else
            w.t0_ns = now + (uint64_t)((double)(w.lead_frames - pre) / out_rate * 1e9);
        w.last_status_ns = now;

        const size_t bufcap = 8192;
        float *fbuf = malloc(sizeof(float) * 2 * bufcap);
        int16_t *sbuf = malloc(sizeof(int16_t) * 2 * bufcap);
        const uint64_t period = 10000000ull;   // 10 ms
        uint64_t next = now + period;
        uint64_t seen_generation = 0;
        while (g_running) {
            now = now_ns();
            if (!sim_spec) {
                // The device thread restarted the input chain (rate change,
                // device gone or back, permission granted): follow it. Output
                // keeps flowing throughout, silence in between.
                uint64_t gen = atomic_load(&dev.generation);
                if (gen != seen_generation) {
                    seen_generation = gen;
                    uint64_t bits = atomic_load(&dev.rate_bits);
                    double rate;
                    memcpy(&rate, &bits, sizeof rate);
                    in.in_rate = w.in_rate = rate;
                    writer_reset(&w);
                }
            }
            if (writer_tick(&w, &in, now, fbuf, sbuf, bufcap) < 0) {
                logmsg("stdout closed; exiting");
                break;
            }
            next += period;
            if (next < now) next = now + period;
            sleep_until_ns(next);
        }
        g_running = 0;
        if (sim_spec) pthread_join(sim.thread, NULL);
        else pthread_join(dev.thread, NULL);
        return 0;
    }
}
