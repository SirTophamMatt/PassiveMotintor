// Alert sounds, synthesised with the Web Audio API.
//
// Generated rather than shipped as audio files: no licensing question, no
// extra requests, and every tone can be tuned here. Nothing plays on its own;
// callers use window.WDSounds.play("<name>").
//
// Browsers refuse to start audio until the page has had a user gesture, so the
// AudioContext is created (or resumed) on the first click/keypress. A play()
// before that is dropped silently rather than queued, because an alert that
// fires late is a wrong alert.
//
// None of these imitate the Standard Emergency Warning Signal. SEWS is
// reserved for official broadcast warnings, and a dashboard sounding like it
// would be mistaken for one.
//
// Why they sound "rich" rather than like a test tone — each voice can combine:
//   * detuned stacks  — several oscillators a few cents apart beat against
//                       each other, giving width/chorus instead of one pure line
//   * additive partials — a sum of sines at chosen ratios (slightly
//                       inharmonic ones make bells/chimes); higher partials
//                       decay faster, which is what real struck objects do
//   * FM              — a modulator wobbling the carrier's frequency; a
//                       decaying index gives the bright-attack/mellow-tail of
//                       an electric piano or bell
//   * filter envelopes — a low-pass that opens as the note starts (brass swell)
//   * ADSR envelopes  — shaped attack/decay/sustain/release instead of on/off
//   * a shared bus    — synthetic reverb (decaying-noise impulse) for space,
//                       then a compressor so layered notes can't clip
(function () {
    var ctx = null;
    var bus = null;        // { input, reverb, master }
    var volume = 1.0;

    function buildBus() {
        var master = ctx.createGain();
        master.gain.value = volume;
        var comp = ctx.createDynamicsCompressor();
        comp.threshold.value = -18;
        comp.ratio.value = 4;
        comp.connect(master).connect(ctx.destination);

        // Reverb impulse: stereo noise with an exponential tail. Independent
        // noise per channel is what makes it sound wide rather than mono.
        var secs = 1.6, len = Math.floor(ctx.sampleRate * secs);
        var ir = ctx.createBuffer(2, len, ctx.sampleRate);
        for (var ch = 0; ch < 2; ch++) {
            var d = ir.getChannelData(ch);
            for (var i = 0; i < len; i++) {
                d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.5);
            }
        }
        var conv = ctx.createConvolver();
        conv.buffer = ir;
        var wet = ctx.createGain();
        wet.gain.value = 0.6;
        conv.connect(wet).connect(comp);

        bus = { input: comp, reverb: conv, master: master };
    }

    function unlock() {
        try {
            if (!ctx) {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (!AC) { return; }
                ctx = new AC();
                buildBus();
            }
            if (ctx.state === "suspended") { ctx.resume(); }
        } catch (e) { /* audio unavailable: stay silent */ }
    }
    ["pointerdown", "keydown", "touchstart"].forEach(function (ev) {
        document.addEventListener(ev, unlock, { passive: true });
    });

    var FLOOR = 0.0001;    // exponential ramps can't reach 0

    // Attack / decay / sustain / release on a gain param. `dur` is how long
    // the note is held; the release runs after it.
    function adsr(param, t0, peak, o, dur) {
        var a = o.attack || 0.005, d = o.decay || 0.1;
        var s = Math.max(peak * (o.sustain == null ? 0.6 : o.sustain), FLOOR);
        var r = o.release || 0.15;
        if (a + d > dur) { d = Math.max(dur - a, 0.001); }
        param.setValueAtTime(FLOOR, t0);
        param.exponentialRampToValueAtTime(peak, t0 + a);
        param.exponentialRampToValueAtTime(s, t0 + a + d);
        param.setValueAtTime(s, t0 + dur);
        param.exponentialRampToValueAtTime(FLOOR, t0 + dur + r);
        return t0 + dur + r;
    }

    // One note. See the header for what each option adds.
    //   wave, voices, detune (cents across the stack), glide (Hz at note end)
    //   partials: [[ratio, gain], ...]   — additive instead of `wave`
    //   fm: { ratio, index, decay, sustain }
    //   filter: { type, from, to, time, q }
    //   gain, wet (reverb send 0..1), attack, decay, sustain, release
    function voice(at, freq, dur, o) {
        o = o || {};
        var t0 = ctx.currentTime + at;
        var out = ctx.createGain();
        var end = adsr(out.gain, t0, o.gain || 0.2, o, dur);
        var stopAt = end + 0.1;

        var sink = out;
        if (o.filter) {
            var f = ctx.createBiquadFilter();
            f.type = o.filter.type || "lowpass";
            f.Q.value = o.filter.q || 1;
            f.frequency.setValueAtTime(o.filter.from, t0);
            if (o.filter.to) {
                f.frequency.exponentialRampToValueAtTime(o.filter.to, t0 + (o.filter.time || dur));
            }
            f.connect(out);
            sink = f;
        }

        function osc(type, hz) {
            var n = ctx.createOscillator();
            n.type = type;
            n.frequency.setValueAtTime(hz, t0);
            if (o.glide) { n.frequency.linearRampToValueAtTime(hz * o.glide / freq, t0 + dur); }
            n.start(t0);
            n.stop(stopAt);
            return n;
        }

        if (o.partials) {
            o.partials.forEach(function (p) {
                var n = osc("sine", freq * p[0]);
                var g = ctx.createGain();
                // Upper partials die away first — the shimmer settles to the
                // fundamental, like a real bell or bar.
                g.gain.setValueAtTime(p[1], t0);
                g.gain.exponentialRampToValueAtTime(FLOOR, t0 + (end - t0) / Math.sqrt(p[0]));
                n.connect(g).connect(sink);
            });
        } else {
            var count = o.voices || 1, spread = o.detune || 0;
            for (var k = 0; k < count; k++) {
                var n = osc(o.wave || "sine", freq);
                if (count > 1) { n.detune.value = spread * (k / (count - 1) - 0.5); }
                if (o.fm) {
                    var m = osc("sine", freq * o.fm.ratio);
                    var depth = ctx.createGain();
                    var peakDev = freq * o.fm.ratio * o.fm.index;
                    depth.gain.setValueAtTime(peakDev, t0);
                    depth.gain.exponentialRampToValueAtTime(
                        Math.max(peakDev * (o.fm.sustain || 0.05), FLOOR), t0 + (o.fm.decay || dur));
                    m.connect(depth).connect(n.frequency);
                }
                var g = ctx.createGain();
                g.gain.value = 1 / count;
                n.connect(g).connect(sink);
            }
        }

        out.connect(bus.input);
        if (o.wet) {
            var send = ctx.createGain();
            send.gain.value = o.wet;
            out.connect(send).connect(bus.reverb);
        }
    }

    // Ordered from most to least urgent. Urgency is carried by tempo, pitch
    // and timbre (buzzy saws > brass > bells), so the levels stay
    // distinguishable without looking at the screen.
    var SOUNDS = {
        // Emergency Warning / SEWS-flagged BoM warning. A wide, detuned saw
        // stack through a bright filter, with a sub-octave for weight; three
        // bursts, then it stops by itself.
        emergency: function () {
            var lead = { wave: "sawtooth", voices: 3, detune: 22, gain: 0.26, wet: 0.12,
                         attack: 0.004, decay: 0.06, sustain: 0.75, release: 0.05,
                         filter: { from: 1500, to: 5000, time: 0.06, q: 3 } };
            var sub = { wave: "square", gain: 0.08, attack: 0.004, sustain: 0.8, release: 0.05,
                        filter: { from: 900, q: 0.7 } };
            [0, 0.62, 1.24].forEach(function (b) {
                [[0, 988], [0.18, 740], [0.36, 988]].forEach(function (n) {
                    voice(b + n[0], n[1], 0.16, lead);
                    voice(b + n[0], n[1] / 2, 0.16, sub);
                });
            });
        },
        // Watch and Act, or a gauge crossing into flood. A brass-like rising
        // call: the filter opens on each note so it "swells" like a horn.
        escalation: function () {
            var brass = { wave: "sawtooth", voices: 2, detune: 12, gain: 0.3, wet: 0.3,
                          attack: 0.03, decay: 0.15, sustain: 0.65, release: 0.3,
                          filter: { from: 350, to: 2800, time: 0.12, q: 5 } };
            voice(0.00, 523, 0.20, brass);
            voice(0.20, 659, 0.20, brass);
            voice(0.40, 880, 0.45, brass);
            voice(0.40, 1760, 0.45, { partials: [[1, 0.5]], gain: 0.06, wet: 0.4, attack: 0.02 });
        },
        // CFA pager escalation (MAKE TANKERS / appliance REQUIRED). FM chirps
        // with a hard, metallic edge; nearly dry so they stay tight and "pager".
        pager: function () {
            var chirp = { fm: { ratio: 1.5, index: 2.2, decay: 0.05, sustain: 0.3 }, gain: 0.22,
                          wet: 0.08, attack: 0.002, decay: 0.03, sustain: 0.6, release: 0.03 };
            [0, 0.5].forEach(function (b) {
                [0, 0.09, 0.18].forEach(function (s) { voice(b + s, 2093, 0.055, chirp); });
            });
        },
        // Advice, new BoM warning, new road closure. A glassy chime: additive
        // partials, slightly inharmonic, with a long reverb tail.
        advice: function () {
            var chime = { partials: [[1, 1], [2.01, 0.4], [3.0, 0.18], [4.16, 0.1], [5.43, 0.05]],
                          gain: 0.22, wet: 0.45, attack: 0.003, decay: 0.3, sustain: 0.35, release: 0.9 };
            voice(0.00, 784, 0.5, chime);
            voice(0.15, 1175, 0.7, chime);
        },
        // Warning cleared, road reopened, power restored. A warm electric-
        // piano pair (FM with a decaying index) landing on a soft low octave —
        // the "resolved" counterpart to `advice`.
        resolved: function () {
            var keys = { fm: { ratio: 1, index: 1.8, decay: 0.35, sustain: 0.08 }, gain: 0.2,
                         wet: 0.35, attack: 0.005, decay: 0.4, sustain: 0.3, release: 0.6 };
            voice(0.00, 880, 0.35, keys);
            voice(0.18, 659, 0.55, keys);
            voice(0.18, 330, 0.55, { fm: { ratio: 1, index: 0.8, decay: 0.3 }, gain: 0.09,
                                     wet: 0.35, sustain: 0.3, release: 0.6 });
        },
        // Collector error / stale data. A low, muffled double buzz; the two
        // detuned squares beat against each other, and it bends slightly flat.
        // Deliberately unlike every hazard sound — this is about the system,
        // not the world.
        system: function () {
            var buzz = { wave: "square", voices: 2, detune: 30, glide: 104, gain: 0.3, wet: 0.05,
                         attack: 0.01, decay: 0.05, sustain: 0.8, release: 0.06,
                         filter: { from: 520, q: 1.2 } };
            voice(0.00, 110, 0.18, buzz);
            voice(0.26, 110, 0.18, buzz);
        }
    };

    window.WDSounds = {
        names: Object.keys(SOUNDS),
        play: function (name) {
            var fn = SOUNDS[name];
            if (!fn || !ctx || ctx.state === "closed") { return false; }
            // Resuming straight after the unlocking gesture takes a few ms;
            // tones scheduled meanwhile still sound once it completes.
            if (ctx.state === "suspended") { ctx.resume(); }
            try { fn(); return true; } catch (e) { return false; }
        },
        setVolume: function (v) {
            volume = Math.max(0, Math.min(1, Number(v) || 0));
            if (bus) { bus.master.gain.value = volume; }
        },
        unlock: unlock
    };
})();
