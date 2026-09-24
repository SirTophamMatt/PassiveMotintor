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
(function () {
    var ctx = null;
    var volume = 1.0;

    function unlock() {
        try {
            if (!ctx) {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (!AC) { return; }
                ctx = new AC();
            }
            if (ctx.state === "suspended") { ctx.resume(); }
        } catch (e) { /* audio unavailable: stay silent */ }
    }
    ["pointerdown", "keydown", "touchstart"].forEach(function (ev) {
        document.addEventListener(ev, unlock, { passive: true });
    });

    // One enveloped tone. `at` is seconds from now; `glide` bends the pitch
    // to that frequency over the tone's length.
    function tone(at, freq, dur, opts) {
        opts = opts || {};
        var t0 = ctx.currentTime + at;
        var osc = ctx.createOscillator();
        var amp = ctx.createGain();
        osc.type = opts.wave || "sine";
        osc.frequency.setValueAtTime(freq, t0);
        if (opts.glide) { osc.frequency.linearRampToValueAtTime(opts.glide, t0 + dur); }
        var peak = (opts.gain || 0.25) * volume;
        var attack = opts.attack || 0.01;
        amp.gain.setValueAtTime(0.0001, t0);
        amp.gain.exponentialRampToValueAtTime(peak, t0 + attack);
        amp.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
        osc.connect(amp).connect(ctx.destination);
        osc.start(t0);
        osc.stop(t0 + dur + 0.05);
    }

    // Each entry schedules its tones against the shared context. Ordered from
    // most to least urgent; urgency is carried by tempo, pitch and harshness of
    // the waveform, so the levels stay distinguishable without looking.
    var SOUNDS = {
        // Emergency Warning / SEWS-flagged BoM warning. Hard square-wave
        // two-tone, three bursts — impossible to ignore, stops by itself.
        emergency: function () {
            for (var i = 0; i < 3; i++) {
                var b = i * 0.62;
                tone(b, 988, 0.16, { wave: "square", gain: 0.14 });
                tone(b + 0.18, 740, 0.16, { wave: "square", gain: 0.14 });
                tone(b + 0.36, 988, 0.16, { wave: "square", gain: 0.14 });
            }
        },
        // Watch and Act, or a gauge crossing into flood: three rising notes,
        // "this is getting worse".
        escalation: function () {
            tone(0.00, 523, 0.22, { wave: "triangle", gain: 0.3 });
            tone(0.20, 659, 0.22, { wave: "triangle", gain: 0.3 });
            tone(0.40, 880, 0.40, { wave: "triangle", gain: 0.3 });
        },
        // CFA pager escalation (MAKE TANKERS / appliance REQUIRED): the fast
        // repeated chirp a pager makes, so it reads as "page" not "warning".
        pager: function () {
            for (var i = 0; i < 2; i++) {
                for (var j = 0; j < 3; j++) {
                    tone(i * 0.5 + j * 0.09, 2093, 0.06, { wave: "square", gain: 0.08 });
                }
            }
        },
        // Advice, new BoM warning, new road closure: a soft two-note chime
        // that informs without startling.
        advice: function () {
            tone(0.00, 784, 0.6, { gain: 0.25 });
            tone(0.15, 1175, 0.8, { gain: 0.18 });
        },
        // Warning cleared, road reopened, power restored: a descending pair,
        // the "resolved" counterpart to `advice`.
        resolved: function () {
            tone(0.00, 880, 0.35, { gain: 0.2 });
            tone(0.18, 659, 0.55, { gain: 0.2 });
        },
        // Collector error / stale data: a low, dull double buzz. Deliberately
        // unlike every hazard sound — this is about the system, not the world.
        system: function () {
            tone(0.00, 196, 0.18, { wave: "sawtooth", gain: 0.12, glide: 185 });
            tone(0.26, 196, 0.18, { wave: "sawtooth", gain: 0.12, glide: 185 });
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
        setVolume: function (v) { volume = Math.max(0, Math.min(1, Number(v) || 0)); },
        unlock: unlock
    };
})();
