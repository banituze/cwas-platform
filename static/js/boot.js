/* Runs before first paint (external, so the CSP allows it): marks that scripts work, and installs a safety net
   so reveal animations can never leave content hidden if a script fails. */
(function () {
  var r = document.documentElement;
  r.classList.add("js");
  setTimeout(function () { if (!window.__fxReady) r.classList.add("fx-safe"); }, 3500);
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
