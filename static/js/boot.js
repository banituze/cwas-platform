/* Runs before first paint (external, so the CSP allows it): marks that scripts work, installs a safety net so reveal
   animations can never leave content hidden if a script fails, and settles the install card before it is drawn. */
(function () {
    var r = document.documentElement;
    r.classList.add("js");
    setTimeout(function () { if (!window.__fxReady) r.classList.add("fx-safe"); }, 3500);
    // The install card (templates/partials/install.html) is part of the first paint of every page. Before anything is drawn
    // it is taken away inside the installed app, and on every page but the homepage once the visitor has closed it
    // (static/js/install.js remembers that). The homepage always shows it and forgets an earlier close, so a card seen
    // there and left open follows to the other pages. iPhone and iPad install through Share, so they get those two taps
    // instead of a button.
    var installed = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
    var closed = false;
    try {
        if (location.pathname === "/") localStorage.removeItem("cwas-install-closed");
        else closed = localStorage.getItem("cwas-install-closed") === "1";
    } catch (e) { closed = false; }
    if (installed || closed) r.classList.add("no-install");
    addEventListener("beforeinstallprompt", function (e) { e.preventDefault(); window.__bip = e; });
    if (/iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) r.classList.add("is-ios");
})();

// Disable right-click and keyboard shortcuts
document.addEventListener('contextmenu', event => event.preventDefault());
document.addEventListener('keydown', function (e) {
    var code = e.code || '';
    var held = e.ctrlKey || e.metaKey;
    if (e.key === 'F12' || code === 'F12') { e.preventDefault(); return; }
    if (held && (code === 'KeyU' || code === 'KeyS')) { e.preventDefault(); return; }
    if (held && (e.altKey || e.shiftKey) &&
        (code === 'KeyI' || code === 'KeyJ' || code === 'KeyC')) { e.preventDefault(); return; }
    if (!code && held && ['u', 'U', 's', 'S'].indexOf(e.key) !== -1) { e.preventDefault(); }
});