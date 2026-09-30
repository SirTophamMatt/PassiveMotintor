/* Calm graphs: stop maps (and charts) redrawing when nothing changed, and stop
 * a backgrounded tab replaying every missed update when it comes back.
 *
 * Why: every page refreshes on a timer, and dcc.Graph hands each new figure to
 * Plotly.react. For a map, Plotly.react tears down and re-adds every MapLibre
 * layer and source even when the figure is byte-identical (measured: ~44 layers
 * and ~48 sources per refresh on /map) — that rebuild is the flicker. Most
 * refreshes change nothing, so most of the flicker was for nothing.
 *
 * And in a hidden tab MapLibre's render loop is paused, so each queued
 * Plotly.react waits; returning to the tab flushed the whole backlog one
 * redraw at a time.
 *
 * What this does, for every graph on every page (dcc.Graph looks Plotly up on
 * window at call time, so wrapping Plotly.react covers them all):
 *   1. an update identical to the last one drawn on that graph is skipped;
 *   2. while the tab is hidden, only the LATEST update per graph is kept and
 *      drawn once when the tab is shown again.
 * A graph's first draw always goes through (dcc.Graph binds its events after
 * it), so nothing here can leave a graph blank.
 */
(function () {
    function hash(text) {
        // FNV-1a over the figure JSON: stores a number, not a megabyte string.
        var h = 0x811c9dc5;
        for (var i = 0; i < text.length; i++) {
            h ^= text.charCodeAt(i);
            h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
        }
        return text.length + ":" + h;
    }

    function signature(args) {
        try {
            // dcc.Graph calls Plotly.react(gd, {data, layout, frames, config}).
            var fig = args.length === 2 ? args[1] : {
                data: args[1], layout: args[2], config: args[3]};
            return hash(JSON.stringify([fig.data, fig.layout, fig.config,
                                        fig.frames || null]));
        } catch (e) {
            return null;                    // unhashable: always draw
        }
    }

    function install(Plotly) {
        var react = Plotly.react;
        if (!react || react.__wdCalm) return;

        function draw(gd, args, sig) {
            gd.__wdLast = "draw";          // last decision, for diagnosis
            gd.__wdSig = sig;
            gd.__wdPending = null;
            return react.apply(Plotly, args);
        }

        var calm = function (gd) {
            var args = Array.prototype.slice.call(arguments);
            // Hash BEFORE drawing: Plotly decodes arrays inside the figure it
            // is given, so the object afterwards no longer matches the JSON.
            var sig = signature(args);
            if (!gd || !gd._fullLayout) {                       // first draw
                if (gd) { gd.__wdSig = sig; gd.__wdLast = "first"; }
                return react.apply(Plotly, args);
            }
            if (sig !== null && sig === gd.__wdSig && !gd.__wdPending) {
                gd.__wdLast = "skip";
                return Promise.resolve(gd);                     // nothing changed
            }
            if (document.hidden) {
                gd.__wdPending = {args: args, sig: sig};        // keep the latest
                return Promise.resolve(gd);
            }
            return draw(gd, args, sig);
        };
        calm.__wdCalm = true;
        Plotly.react = calm;

        document.addEventListener("visibilitychange", function () {
            if (document.hidden) return;
            document.querySelectorAll(".js-plotly-plot").forEach(function (gd) {
                var p = gd.__wdPending;
                if (!p) return;
                if (p.sig !== null && p.sig === gd.__wdSig) {
                    gd.__wdPending = null;                      // came back to the same
                    return;
                }
                draw(gd, p.args, p.sig);
            });
        });
    }

    // dcc.Graph loads plotly.js lazily. Wrap it the moment it is assigned to
    // window, before the first figure is drawn — a graph drawn unwrapped has no
    // signature, so its first refresh would redraw even when nothing changed.
    if (window.Plotly && window.Plotly.react) {
        install(window.Plotly);
    } else {
        var current;
        try {
            Object.defineProperty(window, "Plotly", {
                configurable: true, enumerable: true,
                get: function () { return current; },
                set: function (value) {
                    current = value;
                    if (value && value.react) install(value);
                }
            });
        } catch (e) {
            (function wait() {                       // fallback: poll
                if (window.Plotly && window.Plotly.react) install(window.Plotly);
                else setTimeout(wait, 50);
            })();
        }
    }
})();
