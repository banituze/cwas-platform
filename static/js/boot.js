/* Runs before first paint (external, so the CSP allows it): marks that scripts work, and installs a safety net
   so reveal animations can never leave content hidden if a script fails. */
(function () {
  var r = document.documentElement;
  r.classList.add("js");
  setTimeout(function () { if (!window.__fxReady) r.classList.add("fx-safe"); }, 3500);
})();

