// Escape closes the feedback modal.
//
// Done here rather than in a Dash callback because a keypress isn't a component
// property: clicking the existing close button is the one way to reach the same
// callback without duplicating an Output. The listener is delegated to the
// document, so it survives Dash re-rendering the modal's contents.
document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    var modal = document.getElementById("fb-modal");
    if (!modal || modal.classList.contains("fb-hidden")) { return; }
    var close = document.getElementById("fb-close");
    if (close) { close.click(); }
});
