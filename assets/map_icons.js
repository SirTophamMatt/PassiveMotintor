/* Map icons: registers the Watchdesk icon set on every map on the page.
 *
 * A Scattermap trace whose marker.symbol is "wd-…" becomes a MapLibre symbol
 * layer asking for an image of that name. Nothing on the basemap provides one
 * (OSM / Carto raster tiles carry no sprite), so this script supplies them:
 * the SVGs are inlined into the page as window.WD_MAP_ICONS (app/map_icons.py,
 * via factory), rasterised once, and added to each map.
 *
 * An image added AFTER a layer first asked for it still draws (MapLibre
 * re-lays out the tiles that use it), so a sweep that finds new maps a moment
 * after Plotly creates them is enough; `styleimagemissing` catches anything
 * asked for in between, and a basemap switch (a new style, images dropped) is
 * covered by the sweep re-checking each map.
 */
(function () {
    var SOURCE = window.WD_MAP_ICONS || {};
    var RASTER = 64;               // image pixels; pixelRatio 2 = 32 CSS px
    var images = {};               // name -> ImageData, once decoded
    var names = Object.keys(SOURCE);
    var pending = names.length;

    function decode(name) {
        var img = new Image(RASTER, RASTER);
        img.onload = function () {
            try {
                var c = document.createElement("canvas");
                c.width = RASTER; c.height = RASTER;
                var ctx = c.getContext("2d");
                ctx.drawImage(img, 0, 0, RASTER, RASTER);
                images[name] = ctx.getImageData(0, 0, RASTER, RASTER);
            } catch (e) { /* leave it out: the marker just isn't drawn */ }
            pending -= 1;
        };
        img.onerror = function () { pending -= 1; };
        img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(SOURCE[name]);
    }
    names.forEach(decode);

    function add(map, name) {
        var data = images[name];
        if (!data) return;
        try {
            if (!map.hasImage(name)) map.addImage(name, data, {pixelRatio: 2});
        } catch (e) { /* style mid-switch: the next sweep adds it */ }
    }

    function hook(map) {
        if (map.__wdIcons) return;
        map.__wdIcons = true;
        map.on("styleimagemissing", function (e) {
            if (e && e.id && e.id.indexOf("wd-") === 0) add(map, e.id);
        });
    }

    function maps() {
        var found = [];
        document.querySelectorAll(".js-plotly-plot").forEach(function (gd) {
            var full = gd._fullLayout;
            if (!full) return;
            Object.keys(full).forEach(function (key) {
                if (!/^map\d*$/.test(key)) return;
                var sp = full[key] && full[key]._subplot;
                if (sp && sp.map) found.push(sp.map);
            });
        });
        return found;
    }

    function sweep() {
        if (!names.length) return;
        maps().forEach(function (map) {
            hook(map);
            // Cheap when nothing is missing: one hasImage per icon. Re-adds
            // after a basemap switch replaced the style (and its images).
            for (var i = 0; i < names.length; i++) add(map, names[i]);
        });
    }

    setInterval(sweep, 400);
    window.WDMapIcons = {sweep: sweep, ready: function () { return pending === 0; }};
})();
