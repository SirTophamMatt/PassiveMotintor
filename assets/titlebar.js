// Wires the custom title-bar window controls to pywebview's window API.
// Uses event delegation so it works even though Dash renders the buttons
// after page load. No-ops in the web build (window.pywebview is undefined).
(function () {
    function callApi(action) {
        if (!window.pywebview || !window.pywebview.api) return;
        if (action === "min") window.pywebview.api.minimize();
        else if (action === "max") window.pywebview.api.toggle_maximize();
        else if (action === "close") window.pywebview.api.close();
    }

    document.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-win]");
        if (btn) callApi(btn.getAttribute("data-win"));
    });

    // Double-clicking the drag region toggles maximize, like a native title bar.
    document.addEventListener("dblclick", function (e) {
        if (e.target.closest(".tb-drag")) callApi("max");
    });
})();
