// Clientside callbacks for the sidebar "Sounds" toggle (app/sound_alerts.py).
//
// The server sends the full set of currently-active alertable items each
// tick; this file decides which are NEW to this browser and plays one sound
// for the tick — the most urgent — so a burst of simultaneous changes is one
// alert, not a pile-up of overlapping ones.
(function () {
    // Most urgent first; also the order of SOUNDS in alert_sounds.js.
    var PRIORITY = ["emergency", "escalation", "pager", "advice", "system", "resolved"];
    function rank(s) { var i = PRIORITY.indexOf(s); return i < 0 ? 99 : i; }

    // null until the first snapshot after the page loads or sounds are
    // switched on: that snapshot only seeds, it never plays the backlog.
    var state = null;
    var volumeSeen = false;   // first volume call is the page load, not a drag

    window.dash_clientside = window.dash_clientside || {};
    window.dash_clientside.wdsound = {
        toggle: function (_n, enabled) {
            var on = !enabled;
            if (on && window.WDSounds) {
                // This click is the gesture the browser needs; a short
                // chime confirms audio really works on this device.
                window.WDSounds.unlock();
                setTimeout(function () { window.WDSounds.play("advice"); }, 60);
            }
            return on;
        },

        label: function (enabled) {
            return enabled
                ? ["🔊 Sounds on", "btn sound-btn sound-on"]
                : ["🔇 Sounds off", "btn sound-btn"];
        },

        panel: function (_n, cls) {
            var hidden = (cls || "").indexOf("sound-panel-hidden") >= 0;
            return hidden ? "sound-panel" : "sound-panel sound-panel-hidden";
        },

        // Slider is 0-100. Releasing it plays a short preview at the new
        // level, so "how loud is 60?" is answered by ear, not guesswork.
        volume: function (value) {
            var v = value == null ? 80 : value;
            if (window.WDSounds) {
                window.WDSounds.setVolume(v / 100);
                if (volumeSeen && window.WDSounds.ready()) { window.WDSounds.play("advice"); }
            }
            volumeSeen = true;
            return "Volume " + v + "%";
        },

        // `cats` is the list of enabled sound names from the Settings panel
        // (null before Dash has restored it = everything on). A switched-off
        // category is still marked SEEN, so turning it back on later does not
        // replay whatever arrived while it was off.
        onEvents: function (events, cats) {
            if (!events) { state = null; return null; }
            var allowed = function (s) { return !cats || cats.indexOf(s) >= 0; };
            var active = {};
            events.forEach(function (e) { active[e.entity] = e.resolvable; });
            if (!state) {
                state = { seen: {}, active: active };
                events.forEach(function (e) { state.seen[e.key] = true; });
                return null;
            }
            var best = null;
            events.forEach(function (e) {
                if (state.seen[e.key]) { return; }
                state.seen[e.key] = true;
                if (!allowed(e.sound)) { return; }
                if (best === null || rank(e.sound) < rank(best)) { best = e.sound; }
            });
            if (best === null && allowed("resolved")) {
                // Something ended: a resolvable entity that has left the set
                // entirely (a level change is a new key, not a resolution).
                for (var ent in state.active) {
                    if (state.active[ent] && !(ent in active)) { best = "resolved"; break; }
                }
            }
            state.active = active;
            if (best && window.WDSounds) { window.WDSounds.play(best); }
            return best;
        }
    };
})();
