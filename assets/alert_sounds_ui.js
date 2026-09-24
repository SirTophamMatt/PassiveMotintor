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

        onEvents: function (events) {
            if (!events) { state = null; return null; }
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
                if (best === null || rank(e.sound) < rank(best)) { best = e.sound; }
            });
            if (best === null) {
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
