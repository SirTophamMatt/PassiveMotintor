/* Breathing warning areas.
 *
 * A warning area first seen in the last few minutes is drawn by the server as
 * its own fill + outline map layer named "wd-pulse:<epoch ms deadline>"
 * (fire.warning_area_layers). Plotly cannot animate a layer, so this script
 * finds those layers on every map on the page and drives their paint directly
 * on the underlying MapLibre map until the deadline, then restores them.
 *
 * Plotly names its layout layers "plotly-layout-layer-<subplot uid>-<index>",
 * with <index> the layer's position in layout.map.layers — that is how a named
 * layer is found. Plotly re-applies its own paint whenever the figure updates;
 * the next tick simply overrides it again.
 */
(function () {
    var PREFIX = "wd-pulse:";
    var PERIOD_MS = 2200;          // one breath
    var TICK_MS = 50;              // ~20 fps is plenty for a slow breath
    var reduced = window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function pulseLayers(gd) {
        var full = gd._fullLayout, user = gd.layout;
        if (!full || !user) return [];
        var found = [];
        Object.keys(full).forEach(function (key) {
            if (!/^map\d*$/.test(key)) return;
            var sp = full[key] && full[key]._subplot;
            var layers = user[key] && user[key].layers;
            if (!sp || !sp.map || !layers) return;
            layers.forEach(function (layer, i) {
                var name = (layer && layer.name) || "";
                if (name.indexOf(PREFIX) !== 0) return;
                found.push({
                    map: sp.map,
                    id: "plotly-layout-layer-" + sp.uid + "-" + i,
                    type: layer.type,
                    until: Number(name.slice(PREFIX.length)) || 0,
                    opacity: layer.opacity == null ? 0.35 : layer.opacity,
                    width: (layer.line && layer.line.width) || 2
                });
            });
        });
        return found;
    }

    function paint(p, breath) {
        var live = Date.now() < p.until;
        try {
            if (!p.map.getLayer(p.id)) return;
            if (p.type === "fill") {
                p.map.setPaintProperty(p.id, "fill-opacity",
                    live ? 0.12 + 0.5 * breath : p.opacity);
            } else if (p.type === "line") {
                p.map.setPaintProperty(p.id, "line-width",
                    live ? p.width + 4 * breath : p.width);
            }
        } catch (e) { /* map mid-rebuild: try again next tick */ }
    }

    setInterval(function () {
        var plots = document.querySelectorAll(".js-plotly-plot");
        if (!plots.length) return;
        // Reduced motion: hold the "inhale" (a bolder, still area) instead.
        var breath = reduced ? 1
            : (1 - Math.cos(2 * Math.PI * (Date.now() % PERIOD_MS) / PERIOD_MS)) / 2;
        plots.forEach(function (gd) {
            pulseLayers(gd).forEach(function (p) { paint(p, breath); });
        });
    }, TICK_MS);
})();
