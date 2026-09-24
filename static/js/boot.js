/* Runs before first paint (external, so the CSP allows it): marks that scripts work, installs a safety net so reveal
   animations can never leave content hidden if a script fails, and settles the install card before it is drawn. */
(function () {
  var r = document.documentElement;
  r.classList.add("js");
  setTimeout(function () { if (!window.__fxReady) r.classList.add("fx-safe"); }, 3500);
  // The install card (templates/partials/install.html) is part of the first paint of every page. Inside the installed
  // app it is taken away before anything is drawn, so it never flashes there. iPhone and iPad install through Share, so
  // they get those two taps instead of a button.
  var installed = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  if (installed) r.classList.add("no-install");
  if (/iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) r.classList.add("is-ios");
})();

// Disable right-click and keyboard shortcuts
document.addEventListener('contextmenu', event => event.preventDefault());
document.addEventListener('keydown', function(e) {
    var code = e.code || '';
    var held = e.ctrlKey || e.metaKey;
    if (e.key === 'F12' || code === 'F12') { e.preventDefault(); return; }
    if (held && (code === 'KeyU' || code === 'KeyS')) { e.preventDefault(); return; }
    if (held && (e.altKey || e.shiftKey) &&
        (code === 'KeyI' || code === 'KeyJ' || code === 'KeyC')) { e.preventDefault(); return; }
    if (!code && held && ['u','U','s','S'].indexOf(e.key) !== -1) { e.preventDefault(); }
});
